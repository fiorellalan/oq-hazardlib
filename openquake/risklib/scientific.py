# -*- coding: utf-8 -*-

# Copyright (c) 2012-2014, GEM Foundation.
#
# OpenQuake Risklib is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# OpenQuake Risklib is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with OpenQuake Risklib. If not, see
# <http://www.gnu.org/licenses/>.


"""
This module includes the scientific API of the oq-risklib
"""
from __future__ import division
import abc
import copy
import bisect

import numpy
from numpy.testing import assert_equal
from scipy import interpolate, stats, random

from openquake.baselib.general import CallableDict
from openquake.risklib import utils
from openquake.baselib.python3compat import with_metaclass

F32 = numpy.float32


class Output(object):
    """
    A generic container of attributes. Only assets, loss_type, hid and weight
    are always defined.

    :param assets: a list of assets
    :param loss_type: a loss type string
    :param hid: ordinal of the hazard realization (can be None)
    :param weight: weight of the realization (can be None)
    """
    def __init__(self, assets, loss_type, hid=None, weight=0, **attrs):
        self.assets = assets
        self.loss_type = loss_type
        self.hid = hid
        self.weight = weight
        vars(self).update(attrs)

    def __repr__(self):
        return '<%s %s, hid=%s>' % (
            self.__class__.__name__, self.loss_type, self.hid)

    def __str__(self):
        items = '\n'.join('%s=%s' % item for item in vars(self).items())
        return '<%s\n%s>' % (self.__class__.__name__, items)


def fine_graining(points, steps):
    """
    :param points: a list of floats
    :param int steps: expansion steps (>= 2)

    >>> fine_graining([0, 1], steps=0)
    [0, 1]
    >>> fine_graining([0, 1], steps=1)
    [0, 1]
    >>> fine_graining([0, 1], steps=2)
    array([ 0. ,  0.5,  1. ])
    >>> fine_graining([0, 1], steps=3)
    array([ 0.        ,  0.33333333,  0.66666667,  1.        ])
    >>> fine_graining([0, 0.5, 0.7, 1], steps=2)
    array([ 0.  ,  0.25,  0.5 ,  0.6 ,  0.7 ,  0.85,  1.  ])

    N points become S * (N - 1) + 1 points with S > 0
    """
    if steps < 2:
        return points
    ls = numpy.concatenate([numpy.linspace(x, y, num=steps + 1)[:-1]
                            for x, y in utils.pairwise(points)])
    return numpy.concatenate([ls, [points[-1]]])

#
# Input models
#


class VulnerabilityFunction(object):
    def __init__(self, vf_id, imt, imls, mean_loss_ratios, covs=None,
                 distribution="LN"):
        """
        A wrapper around a probabilistic distribution function
        (currently only the log normal distribution is supported).
        It is meant to be pickeable to allow distributed computation.
        The only important method is `.apply_to`, which applies
        the vulnerability function to a given set of ground motion
        fields and epsilons and return a loss matrix with N x R
        elements.

        :param str vf_id: Vulnerability Function ID
        :param str imt: Intensity Measure Type as a string

        :param list imls: Intensity Measure Levels for the
            vulnerability function. All values must be >= 0.0, values
            must be arranged in ascending order with no duplicates

        :param list mean_loss_ratios: Mean Loss ratio values, equal in
        length to imls, where value >= 0.

        :param list covs: Coefficients of Variation. Equal in length
        to mean loss ratios. All values must be >= 0.0.

        :param str distribution_name: The probabilistic distribution
            related to this function.
        """
        self.id = vf_id
        self.imt = imt
        self._check_vulnerability_data(
            imls, mean_loss_ratios, covs, distribution)
        self.imls = numpy.array(imls)
        self.mean_loss_ratios = numpy.array(mean_loss_ratios)

        if covs is not None:
            self.covs = numpy.array(covs)
        else:
            self.covs = numpy.zeros(self.imls.shape)

        for lr, cov in zip(self.mean_loss_ratios, self.covs):
            if lr == 0.0 and cov > 0.0:
                msg = ("It is not valid to define a loss ratio = 0.0 with a "
                       "corresponding coeff. of variation > 0.0")
                raise ValueError(msg)

        self.distribution_name = distribution

        # to be set in .init(), called also by __setstate__
        (self.stddevs, self._mlr_i1d, self._covs_i1d,
         self.distribution) = None, None, None, None
        self.init()

    def init(self):
        self.stddevs = self.covs * self.mean_loss_ratios
        self._mlr_i1d = interpolate.interp1d(self.imls, self.mean_loss_ratios)
        self._covs_i1d = interpolate.interp1d(self.imls, self.covs)
        self.set_distribution(None)

    def set_distribution(self, epsilons=None):
        if (self.covs > 0).any():
            self.distribution = DISTRIBUTIONS[self.distribution_name]()
        else:
            self.distribution = DegenerateDistribution()
        self.distribution.epsilons = (numpy.array(epsilons)
                                      if epsilons is not None else None)

    def apply_to(self, ground_motion_values, epsilons):
        """
        Apply a copy of the vulnerability function to a set of N
        ground motion vectors, by using N epsilon vectors of length R,
        where N is the number of assets and R the number of realizations.

        :param ground_motion_values:
           matrix of floats N x R
        :param epsilons:
           matrix of floats N x R
        """
        # NB: changing the order of the ground motion values for a given
        # asset without changing the order of the corresponding epsilon
        # values gives inconsistent results, see the MeanLossTestCase
        assert len(epsilons) == len(ground_motion_values), (
            len(epsilons), len(ground_motion_values))
        vulnerability_function = copy.copy(self)
        vulnerability_function.set_distribution(epsilons)
        return utils.numpy_map(
            vulnerability_function._apply, ground_motion_values)

    @utils.memoized
    def strictly_increasing(self):
        """
        :returns:
          a new vulnerability function that is strictly increasing.
          It is built by removing piece of the function where the mean
          loss ratio is constant.
        """
        imls, mlrs, covs = [], [], []

        previous_mlr = None
        for i, mlr in enumerate(self.mean_loss_ratios):
            if previous_mlr == mlr:
                continue
            else:
                mlrs.append(mlr)
                imls.append(self.imls[i])
                covs.append(self.covs[i])
                previous_mlr = mlr

        return self.__class__(
            self.id, self.imt, imls, mlrs, covs, self.distribution_name)

    def mean_loss_ratios_with_steps(self, steps):
        """
        Split the mean loss ratios, producing a new set of loss ratios. The new
        set of loss ratios always includes 0.0 and 1.0

        :param int steps:
            the number of steps we make to go from one loss
            ratio to the next. For example, if we have [0.5, 0.7]::

             steps = 1 produces [0.0,  0.5, 0.7, 1]
             steps = 2 produces [0.0, 0.25, 0.5, 0.6, 0.7, 0.85, 1]
             steps = 3 produces [0.0, 0.17, 0.33, 0.5, 0.57, 0.63,
                                 0.7, 0.8, 0.9, 1]
        """
        loss_ratios = self.mean_loss_ratios

        if min(loss_ratios) > 0.0:
            # prepend with a zero
            loss_ratios = numpy.concatenate([[0.0], loss_ratios])

        if max(loss_ratios) < 1.0:
            # append a 1.0
            loss_ratios = numpy.concatenate([loss_ratios, [1.0]])

        return fine_graining(loss_ratios, steps)

    def _cov_for(self, imls):
        """
        Clip `imls` to the range associated with the support of the
        vulnerability function and returns the corresponding
        covariance values by linear interpolation. For instance
        if the range is [0.005, 0.0269] and the imls are
        [0.0049, 0.006, 0.027], the clipped imls are
        [0.005,  0.006, 0.0269].
        """
        return self._covs_i1d(
            numpy.piecewise(
                imls,
                [imls > self.imls[-1], imls < self.imls[0]],
                [self.imls[-1], self.imls[0], lambda x: x]))

    def __getstate__(self):
        return (self.id, self.imt, self.imls, self.mean_loss_ratios,
                self.covs, self.distribution_name)

    def __setstate__(self, state):
        self.id = state[0]
        self.imt = state[1]
        self.imls = state[2]
        self.mean_loss_ratios = state[3]
        self.covs = state[4]
        self.distribution_name = state[5]
        self.init()

    def _check_vulnerability_data(self, imls, loss_ratios, covs, distribution):
        assert_equal(imls, sorted(set(imls)))
        assert all(x >= 0.0 for x in imls)
        assert covs is None or len(covs) == len(imls)
        assert len(loss_ratios) == len(imls)
        assert all(x >= 0.0 for x in loss_ratios)
        assert covs is None or all(x >= 0.0 for x in covs)
        assert distribution in ["LN", "BT"]

    def _apply(self, imls):
        """
        Given IML values, interpolate the corresponding loss ratio
        value(s) on the curve.

        Input IML value(s) is/are clipped to IML range defined for this
        vulnerability function.

        :param float array iml: IML value

        :returns: :py:class:`numpy.ndarray` containing a number of interpolated
            values equal to the size of the input (1 or many)
        """
        # for imls < min(iml) we return a loss of 0 (default)
        ret = numpy.zeros(len(imls))

        # imls are clipped to max(iml)
        imls_curve = numpy.piecewise(
            imls,
            [imls > self.imls[-1]],
            [self.imls[-1], lambda x: x])

        # for imls such that iml > min(iml) we get a mean loss ratio
        # by interpolation and sample the distribution
        idxs, = numpy.where(imls_curve >= self.imls[0])
        imls_curve = numpy.array(imls_curve)[idxs]
        means = self._mlr_i1d(imls_curve)

        # apply uncertainty
        covs = self._cov_for(imls_curve)
        ret[idxs] = self.distribution.sample(
            means, covs, covs * imls_curve, idxs)
        return ret

    @utils.memoized
    def loss_ratio_exceedance_matrix(self, steps):
        """Compute the LREM (Loss Ratio Exceedance Matrix).

        :param int steps:
            Number of steps between loss ratios.
        """

        # add steps between mean loss ratio values
        loss_ratios = self.mean_loss_ratios_with_steps(steps)

        # LREM has number of rows equal to the number of loss ratios
        # and number of columns equal to the number of imls
        lrem = numpy.empty((loss_ratios.size, self.imls.size), float)

        for row, loss_ratio in enumerate(loss_ratios):
            for col, (mean_loss_ratio, stddev) in enumerate(
                    zip(self.mean_loss_ratios, self.stddevs)):
                lrem[row][col] = self.distribution.survival(
                    loss_ratio, mean_loss_ratio, stddev)
        return loss_ratios, lrem

    @utils.memoized
    def mean_imls(self):
        """
        Compute the mean IMLs (Intensity Measure Level)
        for the given vulnerability function.

        :param vulnerability_function: the vulnerability function where
            the IMLs (Intensity Measure Level) are taken from.
        :type vuln_function:
           :py:class:`openquake.risklib.vulnerability_function.\
           VulnerabilityFunction`
        """
        return numpy.array(
            [max(0, self.imls[0] - (self.imls[1] - self.imls[0]) / 2.)] +
            [numpy.mean(pair) for pair in utils.pairwise(self.imls)] +
            [self.imls[-1] + (self.imls[-1] - self.imls[-2]) / 2.])

    def __repr__(self):
        return '<VulnerabilityFunction(%s, %s)>' % (self.id, self.imt)


class VulnerabilityFunctionWithPMF(object):
    """
    Vulnerability function with an explicit distribution of probabilities

    :param str vf_id: vulnerability function ID
    :param str imt: Intensity Measure Type
    :param imls: intensity measure levels (L)
    :param ratios: mean ratios (M)
    :param probs: a matrix of probabilities of shape (M, L)
    """
    def __init__(self, vf_id, imt, imls, loss_ratios, probs, seed):
        self.id = vf_id
        self.imt = imt
        self._check_vulnerability_data(imls, loss_ratios, probs)
        self.imls = imls
        self.loss_ratios = loss_ratios
        self.probs = probs
        self.seed = seed
        self.distribution_name = "PM"

        # to be set in .init(), called also by __setstate__
        (self._probs_i1d, self.distribution) = None, None
        self.init()

    def init(self):
        self._probs_i1d = interpolate.interp1d(self.imls, self.probs)
        self.set_distribution(None)

    def set_distribution(self, epsilons=None):
        self.distribution = DISTRIBUTIONS[self.distribution_name]()
        self.distribution.epsilons = epsilons
        self.distribution.seed = self.seed

    def apply_to(self, ground_motion_values, epsilons=None):
        """
        :param ground_motion_values:
           matrix of floats N x M
        :param epsilons:
           not used
        :returns: a N x M loss matrix
        """
        vulnerability_function = copy.copy(self)
        vulnerability_function.set_distribution(epsilons)
        mat = utils.numpy_map(
            vulnerability_function._apply, ground_motion_values)
        return mat

    def __getstate__(self):
        return (self.id, self.imt, self.imls, self.loss_ratios,
                self.probs, self.distribution_name, self.seed)

    def __setstate__(self, state):
        self.id = state[0]
        self.imt = state[1]
        self.imls = state[2]
        self.loss_ratios = state[3]
        self.probs = state[4]
        self.distribution_name = state[5]
        self.seed = state[6]
        self.init()

    def _check_vulnerability_data(self, imls, loss_ratios, probs):
        assert all(x >= 0.0 for x in imls)
        assert all(x >= 0.0 for x in loss_ratios)
        assert all([1.0 >= x >= 0.0 for x in y] for y in probs)
        assert probs.shape[0] == len(loss_ratios)
        assert probs.shape[1] == len(imls)

    def _apply(self, imls):
        """
        Given IML values, interpolate the corresponding loss ratio
        value(s) on the curve.

        Input IML value(s) is/are clipped to IML range defined for this
        vulnerability function.

        :param float array iml: IML value

        :returns: :py:class:`numpy.ndarray` containing a number of interpolated
            values equal to the size of the input (1 or many)
        """
        # for imls < min(iml) we return a loss of 0 (default)
        ret = numpy.zeros(len(imls))

        # imls are clipped to max(iml)
        imls_curve = numpy.piecewise(
            imls,
            [imls > self.imls[-1]],
            [self.imls[-1], lambda x: x])

        # for imls such that iml > min(iml) we get a mean loss ratio
        # by interpolation and sample the distribution
        idxs, = numpy.where(imls_curve >= self.imls[0])
        imls_curve = numpy.array(imls_curve)[idxs]
        probs = self._probs_i1d(imls_curve)

        # apply uncertainty
        ret[idxs] = self.distribution.sample(self.loss_ratios, probs)
        return ret

    @utils.memoized
    def loss_ratio_exceedance_matrix(self, steps):
        """Compute the LREM (Loss Ratio Exceedance Matrix).
        Required for the Classical Risk and BCR Calculators.
        Currently left unimplemented as the PMF format is used only for the
        Scenario and Event Based Risk Calculators
        :param int steps:
            Number of steps between loss ratios.
        """
        # TODO: to be implemented if the classical risk calculator
        # needs to support the pmf vulnerability format

    def __repr__(self):
        return '<VulnerabilityFunctionWithPMF(%s, %s)>' % (self.id, self.imt)


class FragilityFunctionContinuous(object):
    # FIXME (lp). Should be re-factored with LogNormalDistribution
    def __init__(self, limit_state, mean, stddev):
        self.limit_state = limit_state
        self.mean = mean
        self.stddev = stddev

    def __call__(self, iml):
        """
        Compute the Probability of Exceedance (PoE) for the given
        Intensity Measure Level (IML).
        """
        variance = self.stddev ** 2.0
        sigma = numpy.sqrt(numpy.log(
            (variance / self.mean ** 2.0) + 1.0))

        mu = self.mean ** 2.0 / numpy.sqrt(
            variance + self.mean ** 2.0)

        return stats.lognorm.cdf(iml, sigma, scale=mu)

    def __getstate__(self):
        return dict(limit_state=self.limit_state,
                    mean=self.mean, stddev=self.stddev)

    def __repr__(self):
        return '<%s(%s, %s, %s)>' % (
            self.__class__.__name__, self.limit_state, self.mean, self.stddev)


class FragilityFunctionDiscrete(object):

    def __init__(self, limit_state, imls, poes, no_damage_limit=None):
        self.limit_state = limit_state
        self.imls = imls
        self.poes = poes
        self._interp = None
        self.no_damage_limit = no_damage_limit

    @property
    def interp(self):
        if self._interp is not None:
            return self._interp
        self._interp = interpolate.interp1d(self.imls, self.poes)
        return self._interp

    def __call__(self, iml):
        """
        Compute the Probability of Exceedance (PoE) for the given
        Intensity Measure Level (IML).
        """
        highest_iml = self.imls[-1]

        if self.no_damage_limit is not None and iml < self.no_damage_limit:
            return 0.
        # when the intensity measure level is above
        # the range, we use the highest one
        return self.interp(highest_iml if iml > highest_iml else iml)

    # so that the curve is pickeable
    def __getstate__(self):
        return dict(limit_state=self.limit_state,
                    poes=self.poes, imls=self.imls, _interp=None,
                    no_damage_limit=self.no_damage_limit)

    def __eq__(self, other):
        return (self.poes == other.poes and self.imls == other.imls)

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return '<%s(%s, %s, %s)>' % (
            self.__class__.__name__, self.limit_state, self.imls, self.poes)


class FragilityFunctionList(list):
    """
    A list of fragility functions with common attributes
    """
    def __init__(self, elements, **attrs):
        list.__init__(self, elements)
        vars(self).update(attrs)

    def __repr__(self):
        kvs = ['%s=%s' % item for item in vars(self).items()]
        return '<FragilityFunctionList %s>' % ', '.join(kvs)

#
# Distribution & Sampling
#

DISTRIBUTIONS = CallableDict()


class Distribution(with_metaclass(abc.ABCMeta)):
    """
    A Distribution class models continuous probability distribution of
    random variables used to sample losses of a set of assets. It is
    usually registered with a name (e.g. LN, BT, PM) by using
    :class:`openquake.baselib.general.CallableDict`
    """

    @abc.abstractmethod
    def sample(self, means, covs, stddevs, idxs):
        """
        :returns: sample a set of losses
        :param means: an array of mean losses
        :param covs: an array of covariances
        :param stddevs: an array of stddevs
        """
        raise NotImplementedError

    @abc.abstractmethod
    def survival(self, loss_ratio, mean, stddev):
        """
        Return the survival function of the distribution with `mean`
        and `stddev` applied to `loss_ratio`
        """
        raise NotImplementedError


class DegenerateDistribution(Distribution):
    """
    The degenerate distribution. E.g. a distribution with a delta
    corresponding to the mean.
    """
    def sample(self, means, _covs, _stddev, _idxs):
        return means

    def survival(self, loss_ratio, mean, _stddev):
        return numpy.piecewise(
            loss_ratio, [loss_ratio > mean or not mean], [0, 1])


class EpsilonProvider(object):
    """
    A provider of epsilons. If the correlation coefficient is nonzero,
    it builds at instantiation time an NxN correlation matrix for the N
    assets. The `.sample` method returns an array of NxS elements,
    where S is the number of seeds passed.

    Here is an example without correlation:

    >>> ep = EpsilonProvider(num_assets=3, correlation=0)
    >>> ep.sample(seeds=[42, 43])
    array([[ 0.49671415,  0.25739993],
           [-0.1382643 , -0.90848143],
           [ 0.64768854, -0.37850311]])

    Here is an example with full correlation, i.e. all the assets get the same
    epsilon for a given seed:

    >>> ep = EpsilonProvider(num_assets=3, correlation=1)
    >>> ep.sample(seeds=[42, 43])
    array([[-0.49671415, -0.25739993],
           [-0.49671415, -0.25739993],
           [-0.49671415, -0.25739993]])
    """
    def __init__(self, num_assets, correlation):
        """
        :param int num_assets: the number of assets
        :param float correlation: coefficient in the range [0, 1]
        """
        assert num_assets > 0, num_assets
        assert 0 <= correlation <= 1, correlation
        self.num_assets = num_assets
        self.correlation = correlation
        if self.correlation:
            self.means_vector = numpy.zeros(num_assets)
            self.covariance_matrix = (
                numpy.ones((num_assets, num_assets)) * correlation +
                numpy.diag(numpy.ones(num_assets)) * (1 - correlation))

    def sample_one(self, seed):
        """
        :param int seed: the random seed used to generate the epsilons
        :returns: an array with `num_assets` epsilons
        """
        numpy.random.seed(seed)
        if not self.correlation:
            return numpy.random.normal(size=self.num_assets)
        return numpy.random.multivariate_normal(
            self.means_vector, self.covariance_matrix, 1).reshape(-1)

    def sample(self, seeds):
        """
        :param seeds: a sequence of stochastic seeds
        :returns: an array with shape `(num_assets, num_seeds)`
        """
        return numpy.array([self.sample_one(seed) for seed in seeds]).T


def make_epsilons(matrix, seed, correlation):
    """
    Given a matrix N * R returns a matrix of the same shape N * R
    obtained by applying the multivariate_normal distribution to
    N points and R samples, by starting from the given seed and
    correlation.
    """
    if seed is not None:
        numpy.random.seed(seed)
    asset_count = len(matrix)
    samples = len(matrix[0])
    if not correlation:  # avoid building the covariance matrix
        return numpy.random.normal(size=(samples, asset_count)).transpose()
    means_vector = numpy.zeros(asset_count)
    covariance_matrix = (
        numpy.ones((asset_count, asset_count)) * correlation +
        numpy.diag(numpy.ones(asset_count)) * (1 - correlation))
    return numpy.random.multivariate_normal(
        means_vector, covariance_matrix, samples).transpose()


@DISTRIBUTIONS.add('LN')
class LogNormalDistribution(Distribution):
    """
    Model a distribution of a random variable whoose logarithm are
    normally distributed.

    :attr epsilons: A matrix of random numbers generated with
                    :func:`numpy.random.multivariate_normal` with dimensions
                    assets_num x samples_num.
    :attr asset_idx: a counter used in sampling to iterate over the
                     attribute `epsilons`
    """
    def __init__(self, epsilons=None):
        self.epsilons = epsilons
        self.asset_idx = 0

    def sample(self, means, covs, _stddevs, idxs):
        if self.epsilons is None:
            raise ValueError("A LogNormalDistribution must be initialized "
                             "before you can use it")
        eps = self.epsilons[self.asset_idx, idxs]
        self.asset_idx += 1
        sigma = numpy.sqrt(numpy.log(covs ** 2.0 + 1.0))
        probs = means / numpy.sqrt(1 + covs ** 2) * numpy.exp(eps * sigma)
        return probs

    def survival(self, loss_ratio, mean, stddev):
        # scipy does not handle correctly the limit case stddev = 0.
        # In that case, when `mean` > 0 the survival function
        # approaches to a step function, otherwise (`mean` == 0) we
        # returns 0
        if stddev == 0:
            return numpy.piecewise(
                loss_ratio, [loss_ratio > mean or not mean], [0, 1])

        variance = stddev ** 2.0

        sigma = numpy.sqrt(numpy.log((variance / mean ** 2.0) + 1.0))
        mu = mean ** 2.0 / numpy.sqrt(variance + mean ** 2.0)
        return stats.lognorm.sf(loss_ratio, sigma, scale=mu)


@DISTRIBUTIONS.add('BT')
class BetaDistribution(Distribution):
    def sample(self, means, _covs, stddevs, _idxs=None):
        alpha = self._alpha(means, stddevs)
        beta = self._beta(means, stddevs)
        return numpy.random.beta(alpha, beta, size=None)

    def survival(self, loss_ratio, mean, stddev):
        return stats.beta.sf(loss_ratio,
                             self._alpha(mean, stddev),
                             self._beta(mean, stddev))

    @staticmethod
    def _alpha(mean, stddev):
        return ((1 - mean) / stddev ** 2 - 1 / mean) * mean ** 2

    @staticmethod
    def _beta(mean, stddev):
        return ((1 - mean) / stddev ** 2 - 1 / mean) * (mean - mean ** 2)


@DISTRIBUTIONS.add('PM')
class DiscreteDistribution(Distribution):
    seed = None  # to be set

    def sample(self, loss_ratios, probs):
        ret = []
        r = numpy.arange(len(loss_ratios))
        for i in range(probs.shape[1]):
            random.seed(self.seed)
            # the seed is set inside the loop to avoid block-size dependency
            pmf = stats.rv_discrete(name='pmf', values=(r, probs[:, i])).rvs()
            ret.append(loss_ratios[pmf])
        return ret

    def survival(self, loss_ratios, probs):
        """
        Required for the Classical Risk and BCR Calculators.
        Currently left unimplemented as the PMF format is used only for the
        Scenario and Event Based Risk Calculators.

        :param int steps: number of steps between loss ratios.
        """
        # TODO: to be implemented if the classical risk calculator
        # needs to support the pmf vulnerability format
        return


#
# Event Based
#

class CurveBuilder(object):
    """
    Build loss ratio curves. The loss ratios can be provided
    by the user or automatically generated (user_provided=False).
    The usage is something like this:

      builder = CurveBuilder(loss_type, loss_ratios, user_provided=True)
      counts = builder.build_counts(loss_matrix)
    """
    def __init__(self, loss_type, loss_ratios, user_provided):
        self.loss_type = loss_type
        self.ratios = numpy.array(loss_ratios, F32)
        self.user_provided = user_provided
        self.curve_resolution = len(loss_ratios)
        R = self.curve_resolution or 1  # avoid bug in hdf5 2.0
        self.loss_curve_dt = numpy.dtype([
            ('losses', (F32, R)), ('poes', (F32, R)), ('avg', F32)])
        self.lr_dt = numpy.dtype([('poes', (F32, R))])

    def get_counts(self, N, count_dicts):
        """
        Return a matrix of shape (N, C), with nonzero entries at
        the indices given by the count_dicts

        :param N: the number of assets
        :param count_dicts: a list of maps asset_idx -> [C indices]

        >>> cb = CurveBuilder('structural', [0.1, 0.2, 0.3, 0.9], True)
        >>> cb.get_counts(3, [{1: [4, 3, 2, 1]}, {2: [4, 0, 0, 0]},
        ...                   {1: [1, 0, 0, 0]}, {2: [2, 0, 0, 0]}])
        array([[0, 0, 0, 0],
               [5, 3, 2, 1],
               [6, 0, 0, 0]], dtype=uint32)
        """
        counts = numpy.zeros((N, self.curve_resolution), numpy.uint32)
        for count_dict in count_dicts:
            counts[list(count_dict)] += count_dict.values()
        return counts

    def build_counts(self, loss_matrix):
        """
        :param loss_matrix:
            a matrix of loss ratios of size N x R, N = #assets, R = #ruptures
        """
        counts = self.get_counts(len(loss_matrix), {})
        for i, loss_ratios in enumerate(loss_matrix):
            # build the counts for each asset
            counts[i, :] = numpy.array([(loss_ratios >= ratio).sum()
                                        for ratio in self.ratios])
        return counts

    def build_poes(self, N, count_dicts, ses_ratio):
        """
        :param N: the number of assets
        :param count_dicts: a list of maps asset_idx -> [C indices]
        :param ses_ratio: event based factor
        """
        counts_matrix = self.get_counts(N, count_dicts)
        poes = build_poes(counts_matrix, 1. / ses_ratio)
        return poes.view(self.lr_dt)

    def build_loss_curves(self, assetcol, losses_by_aid, ses_ratio):
        """
        :param assetcol: asset collection object
        :param losses_by_aid: a dictionary asset_idx -> losses
        :param ses_ratio: event based factor
        """
        lcs = numpy.zeros(len(assetcol), self.loss_curve_dt)
        zeros = numpy.zeros(self.curve_resolution)
        for i, value in enumerate(assetcol[self.loss_type]):
            all_losses = losses_by_aid.get(i)
            if all_losses is None:  # no losses for the given asset
                lcs[i] = (self.ratios * value, zeros, 0)
            else:  # build the loss curve
                losses, poes = event_based(all_losses, ses_ratio,
                                           self.curve_resolution)
                avg = average_loss((losses, poes))
                lcs[i] = (losses, poes, avg)
        return lcs

    def __repr__(self):
        return '<%s %s=%s user_provided=%s>' % (
            self.__class__.__name__, self.loss_type,
            self.loss_ratios, self.user_provided)


# should I use the ses_ratio here?
def build_poes(counts, nses):
    """
    :param counts: an array of counts of exceedence for the bins
    :param nses: number of stochastic event sets
    :returns: an array of PoEs
    """
    return 1. - numpy.exp(- numpy.array(counts, F32) / nses)


def event_based(loss_values, ses_ratio, curve_resolution):
    """
    Compute a loss (or loss ratio) curve.

    :param loss_values: The loss ratios (or the losses) computed by
                        applying the vulnerability function

    :param ses_ratio: Time representative of the stochastic event set

    :param curve_resolution: The number of points the output curve is
                             defined by
    """
    reference_losses = numpy.linspace(
        0, numpy.max(loss_values), curve_resolution)
    # counts how many loss_values are bigger than the reference loss
    counts = [(loss_values > loss).sum() for loss in reference_losses]
    # NB: (loss_values > loss).sum() is MUCH more efficient than
    # sum(loss_values > loss). Incredibly more efficient in memory.
    return numpy.array(
        [reference_losses, build_poes(counts, 1. / ses_ratio)])


#
# Scenario Damage
#

def scenario_damage(fragility_functions, gmv):
    """
    Compute the damage state fractions for the given ground motion value.
    Return am array of M values where M is the numbers of damage states.
    """
    return pairwise_diff(
        [1] + [ff(gmv) for ff in fragility_functions] + [0])

#
# Classical Damage
#


def annual_frequency_of_exceedence(poe, t_haz):
    """
    :param poe: hazard probability of exceedence
    :param t_haz: hazard investigation time
    """
    return - numpy.log(1. - poe) / t_haz


def classical_damage(
        fragility_functions, hazard_imls, hazard_poes,
        investigation_time, risk_investigation_time):
    """
    :param fragility_functions:
        a list of fragility functions for each damage state
    :param hazard_imls:
        Intensity Measure Levels
    :param hazard_poes:
        hazard curve
    :param investigation_time:
        hazard investigation time
    :param risk_investigation_time:
        risk investigation time
    :returns:
        an array of M probabilities of occurrence where M is the numbers
        of damage states.
    """
    imls = numpy.array(fragility_functions.imls)
    if fragility_functions.steps_per_interval:  # interpolate
        min_val, max_val = hazard_imls[0], hazard_imls[-1]
        numpy.putmask(imls, imls < min_val, min_val)
        numpy.putmask(imls, imls > max_val, max_val)
        poes = interpolate.interp1d(hazard_imls, hazard_poes)(imls)
    else:
        poes = numpy.array(hazard_poes)
    afe = annual_frequency_of_exceedence(poes, investigation_time)
    annual_frequency_of_occurrence = pairwise_diff(
        pairwise_mean([afe[0]] + list(afe) + [afe[-1]]))
    poes_per_damage_state = []
    for ff in fragility_functions:
        frequency_of_exceedence_per_damage_state = numpy.dot(
            annual_frequency_of_occurrence, list(map(ff, imls)))
        poe_per_damage_state = 1. - numpy.exp(
            - frequency_of_exceedence_per_damage_state
            * risk_investigation_time)
        poes_per_damage_state.append(poe_per_damage_state)
    poos = pairwise_diff([1] + poes_per_damage_state + [0])
    return poos

#
# Classical
#


def classical(vulnerability_function, hazard_imls, hazard_poes, steps=10):
    """
    :param vulnerability_function:
        an instance of
        :py:class:`openquake.risklib.scientific.VulnerabilityFunction`
        representing the vulnerability function used to compute the curve.
    :param hazard_imls:
        the hazard intensity measure type and levels
    :type hazard_poes:
        the hazard curve
    :param int steps:
        Number of steps between loss ratios.
    """
    vf = vulnerability_function.strictly_increasing()
    imls = vf.mean_imls()
    loss_ratios, lrem = vf.loss_ratio_exceedance_matrix(steps)

    # saturate imls to hazard imls
    min_val, max_val = hazard_imls[0], hazard_imls[-1]
    numpy.putmask(imls, imls < min_val, min_val)
    numpy.putmask(imls, imls > max_val, max_val)

    # interpolate the hazard curve
    poes = interpolate.interp1d(hazard_imls, hazard_poes)(imls)

    # compute the poos
    pos = pairwise_diff(poes)
    lrem_po = numpy.empty(lrem.shape)
    for idx, po in enumerate(pos):
        lrem_po[:, idx] = lrem[:, idx] * po  # column * po

    return numpy.array([loss_ratios, lrem_po.sum(axis=1)])


def conditional_loss_ratio(loss_ratios, poes, probability):
    """
    Return the loss ratio corresponding to the given PoE (Probability
    of Exceendance). We can have four cases:

      1. If `probability` is in `poes` it takes the bigger
         corresponding loss_ratios.

      2. If it is in `(poe1, poe2)` where both `poe1` and `poe2` are
         in `poes`, then we perform a linear interpolation on the
         corresponding losses

      3. if the given probability is smaller than the
         lowest PoE defined, it returns the max loss ratio .

      4. if the given probability is greater than the highest PoE
         defined it returns zero.

    :param loss_ratios: an iterable over non-decreasing loss ratio
                        values (float)
    :param poes: an iterable over non-increasing probability of
                 exceedance values (float)
    :param float probability: the probability value used to
                              interpolate the loss curve
    """

    rpoes = poes[::-1]
    if probability > poes[0]:  # max poes
        return 0.0
    elif probability < poes[-1]:  # min PoE
        return loss_ratios[-1]
    if probability in poes:
        return max([loss
                    for i, loss in enumerate(loss_ratios)
                    if probability == poes[i]])
    else:
        interval_index = bisect.bisect_right(rpoes, probability)

        if interval_index == len(poes):  # poes are all nan
            return float('nan')
        elif interval_index == 1:  # boundary case
            x1, x2 = poes[-2:]
            y1, y2 = loss_ratios[-2:]
        else:
            x1, x2 = poes[-interval_index-1:-interval_index + 1]
            y1, y2 = loss_ratios[-interval_index-1:-interval_index + 1]

        return (y2 - y1) / (x2 - x1) * (probability - x1) + y1


#
# Insured Losses
#

def insured_losses(losses, deductible, insured_limit):
    """
    Compute insured losses for the given asset and losses

    :param losses: an array of ground-up loss ratios
    :param float deductible: the deductible limit in fraction form
    :param float insured_limit: the insured limit in fraction form

    >>> insured_losses(numpy.array([3, 20, 101]), 5, 100)
    array([ 0, 15, 95])
    """
    return numpy.piecewise(
        losses,
        [losses < deductible, losses > insured_limit],
        [0, insured_limit - deductible, lambda x: x - deductible])


def insured_loss_curve(curve, deductible, insured_limit):
    """
    Compute an insured loss ratio curve given a loss ratio curve

    :param curve: an array 2 x R (where R is the curve resolution)
    :param float deductible: the deductible limit in fraction form
    :param float insured_limit: the insured limit in fraction form
    """
    losses, poes = curve[:, curve[0] <= insured_limit]
    limit_poe = interpolate.interp1d(
        *curve,
        bounds_error=False, fill_value=1)(deductible)
    return numpy.array([
        losses,
        numpy.piecewise(poes, [poes > limit_poe], [limit_poe, lambda x: x])])


#
# Benefit Cost Ratio Analysis
#


def bcr(eal_original, eal_retrofitted, interest_rate,
        asset_life_expectancy, asset_value, retrofitting_cost):
    """
    Compute the Benefit-Cost Ratio.

    BCR = (EALo - EALr)(1-exp(-r*t))/(r*C)

    Where:

    * BCR -- Benefit cost ratio
    * EALo -- Expected annual loss for original asset
    * EALr -- Expected annual loss for retrofitted asset
    * r -- Interest rate
    * t -- Life expectancy of the asset
    * C -- Retrofitting cost
    """
    return ((eal_original - eal_retrofitted) * asset_value
            * (1 - numpy.exp(- interest_rate * asset_life_expectancy))
            / (interest_rate * retrofitting_cost))


# ####################### statistics #################################### #

def pairwise_mean(values):
    "Averages between a value and the next value in a sequence"
    return numpy.array([numpy.mean(pair) for pair in utils.pairwise(values)])


def pairwise_diff(values):
    "Differences between a value and the next value in a sequence"
    return numpy.array([x - y for x, y in utils.pairwise(values)])


def mean_std(fractions):
    """
    Given an N x M matrix, returns mean and std computed on the rows,
    i.e. two M-dimensional vectors.
    """
    return numpy.mean(fractions, axis=0), numpy.std(fractions, axis=0, ddof=1)


def loss_map_matrix(poes, curves):
    """
    Wrapper around :func:`openquake.risklib.scientific.conditional_loss_ratio`.
    Return a matrix of shape (num-poes, num-curves). The curves are lists of
    pairs (loss_ratios, poes).
    """
    return numpy.array(
        [[conditional_loss_ratio(curve[0], curve[1], poe)
          for curve in curves] for poe in poes]
    ).reshape((len(poes), len(curves)))


def calc_loss_maps(conditional_loss_poes, asset_values, ratios, poe_matrix):
    """
    Compute loss maps from the PoE matrix (i.e. the loss curves).

    :param conditional_loss_poes: an array of P PoEs
    :param asset_values: asset values for the current loss type
    :param ratios: loss ratios for the current loss type
    :poe_matrix: an N x C matrix of PoEs
    :returns: a matrix N x P
    """
    curves = []
    for avalue, poes in zip(asset_values, poe_matrix):
        curves.append((ratios * avalue, poes))
    return loss_map_matrix(conditional_loss_poes, curves).T


def mean_curve(values, weights=None):
    """
    Compute the mean by using numpy.average on the first axis.
    """
    if weights:
        weights = list(map(float, weights))
        assert abs(sum(weights) - 1.) < 1E-12, sum(weights) - 1.
    else:
        weights = [1. / len(values)] * len(values)
    if isinstance(values[0], (numpy.ndarray, list, tuple)):  # fast lane
        return numpy.average(values, axis=0, weights=weights)
    return sum(value * weight for value, weight in zip(values, weights))


def quantile_curve(curves, quantile, weights=None):
    """
    Compute the weighted quantile aggregate of a set of curves
    when using the logic tree end-branch enumeration approach, or just the
    standard quantile when using the sampling approach.

    :param curves:
        2D array-like of curve PoEs. Each row represents the PoEs for a single
        curve
    :param quantile:
        Quantile value to calculate. Should in the range [0.0, 1.0].
    :param weights:
        Array-like of weights, 1 for each input curve, or None
    :returns:
        A numpy array representing the quantile aggregate
    """
    if weights is None:
        # this implementation is an alternative to
        # numpy.array(mstats.mquantiles(curves, prob=quantile, axis=0))[0]
        # more or less copied from the scipy mquantiles function, just special
        # cased for what we need (and a lot faster)
        arr = numpy.array(curves).reshape(len(curves), -1)
        p = numpy.array(quantile)
        m = 0.4 + p * 0.2
        n = len(arr)
        aleph = n * p + m
        k = numpy.floor(aleph.clip(1, n - 1)).astype(int)
        gamma = (aleph - k).clip(0, 1)
        data = numpy.sort(arr, axis=0).transpose()
        return (1.0 - gamma) * data[:, k - 1] + gamma * data[:, k]

    # Each curve needs to be associated with a weight
    assert len(weights) == len(curves)
    weights = numpy.array(weights, dtype=numpy.float64)

    result_curve = []
    np_curves = numpy.array(curves).reshape(len(curves), -1)
    np_weights = numpy.array(weights)
    for poes in np_curves.transpose():
        sorted_poe_idxs = numpy.argsort(poes)
        sorted_weights = np_weights[sorted_poe_idxs]
        sorted_poes = poes[sorted_poe_idxs]
        cum_weights = numpy.cumsum(sorted_weights)
        result_curve.append(numpy.interp(quantile, cum_weights, sorted_poes))
    return numpy.array(result_curve)


# TODO: remove this from openquake.risklib.qa_tests.bcr_test
def average_loss(losses_poes):
    """
    Given a loss curve with `poes` over `losses` defined on a given
    time span it computes the average loss on this period of time.

    :note: As the loss curve is supposed to be piecewise linear as it
           is a result of a linear interpolation, we compute an exact
           integral by using the trapeizodal rule with the width given by the
           loss bin width.
    """
    losses, poes = losses_poes
    return numpy.dot(-pairwise_diff(losses), pairwise_mean(poes))


def quantile_matrix(values, quantiles, weights):
    """
    :param curves:
        a matrix R x N, where N is the number of assets and R the number
        of realizations
    :param quantile:
        a list of Q quantiles
    :param weights:
        a list of R weights
    :returns:
        a matrix Q x N
    """
    result = numpy.zeros((len(quantiles), values.shape[1]))
    for i, q in enumerate(quantiles):
        result[i] = quantile_curve(values, q, weights)
    return result


def exposure_statistics(
        loss_curves, map_poes, weights, quantiles):
    """
    Compute exposure statistics for N assets and R realizations.

    :param loss_curves:
        a list with N loss curves data. Each item holds a 2-tuple with
        1) the loss ratios on which the curves have been defined on
        2) the poes of the R curves
    :param map_poes:
        a numpy array with P poes used to compute loss maps
    :param weights:
        a list of N weights used to compute mean/quantile weighted statistics
    :param quantiles:
        the quantile levels used to compute quantile results

    :returns:
        a tuple with four elements:
            1. a numpy array with N mean loss curves
            2. a numpy array with P x N mean map values
            3. a numpy array with Q x N quantile loss curves
            4. a numpy array with Q x P quantile map values
    """
    curve_resolution = len(loss_curves[0][0])
    map_nr = len(map_poes)

    # Collect per-asset statistic along the last dimension of the
    # following arrays
    mean_curves = numpy.zeros((0, 2, curve_resolution))
    mean_maps = numpy.zeros((map_nr, 0))
    quantile_curves = numpy.zeros((len(quantiles), 0, 2, curve_resolution))
    quantile_maps = numpy.zeros((len(quantiles), map_nr, 0))

    for loss_ratios, curves_poes in loss_curves:
        _mean_curve, _mean_maps, _quantile_curves, _quantile_maps = (
            asset_statistics(
                loss_ratios, curves_poes, quantiles, weights, map_poes))

        mean_curves = numpy.vstack(
            (mean_curves, _mean_curve[numpy.newaxis, :]))
        mean_maps = numpy.hstack((mean_maps, _mean_maps[:, numpy.newaxis]))

        quantile_curves = numpy.hstack(
            (quantile_curves, _quantile_curves[:, numpy.newaxis]))
        quantile_maps = numpy.dstack(
            (quantile_maps, _quantile_maps[:, :, numpy.newaxis]))

    return (mean_curves, mean_maps, quantile_curves, quantile_maps)


def asset_statistics(
        losses, curves_poes, quantiles, weights, poes):
    """
    Compute output statistics (mean/quantile loss curves and maps)
    for a single asset

    :param losses:
       the losses on which the loss curves are defined
    :param curves_poes:
       a numpy matrix with the poes of the different curves
    :param list quantiles:
       an iterable over the quantile levels to be considered for
       quantile outputs
    :param list poes:
       the poe taken into account for computing loss maps
    :returns:
       a tuple with
       1) mean loss curve
       2) a list of quantile curves
       3) mean loss map
       4) a list of quantile loss maps
    """
    mean_curve_ = numpy.array([losses, mean_curve(curves_poes, weights)])
    mean_map = loss_map_matrix(poes, [mean_curve_]).reshape(len(poes))
    quantile_curves = numpy.array(
        [[losses, quantile_curve(curves_poes, quantile, weights)]
         for quantile in quantiles]).reshape((len(quantiles), 2, len(losses)))
    quantile_maps = loss_map_matrix(poes, quantile_curves).transpose()
    return (mean_curve_, mean_map, quantile_curves, quantile_maps)


def normalize_curves(curves):
    """
    :param curves: a list of pairs (losses, poes)
    :returns: first losses, all_poes
    """
    return curves[0][0], [poes for _losses, poes in curves]


def normalize_curves_eb(curves):
    """
    A more sophisticated version of normalize_curves, used in the event
    based calculator.

    :param curves: a list of pairs (losses, poes)
    :returns: first losses, all_poes
    """
    # we assume non-decreasing losses, so losses[-1] is the maximum loss
    non_zero_curves = [(losses, poes)
                       for losses, poes in curves if losses[-1] > 0]
    if not non_zero_curves:  # no damage. all zero curves
        return curves[0][0], [poes for _losses, poes in curves]
    else:  # standard case
        max_losses = [losses[-1] for losses, _poes in non_zero_curves]
        reference_curve = non_zero_curves[numpy.argmax(max_losses)]
        loss_ratios = reference_curve[0]
        curves_poes = [interpolate.interp1d(
            losses, poes, bounds_error=False, fill_value=0)(loss_ratios)
            for losses, poes in curves]
    return loss_ratios, curves_poes


class SimpleStats(object):
    """
    A class to perform statistics on the average losses. The average losses
    are stored as N x 2 arrays (non-insured and insured losses) where N is
    the number of assets. Here is an example of usage:

    >>> from collections import namedtuple
    >>> Realization = namedtuple('Realization', 'ordinal uid weight')
    >>> rlzs = [Realization(0, 'b1', 0.3), Realization(1, 'b2', 0.7)]
    >>> dstore = {
    ...     'avg_losses-rlzs':
    ...        {'structural':
    ...            {'b1': numpy.array([[0.10, 0.20], [0.30, 0.40]]),
    ...             'b2': numpy.array([[0.12, 0.22], [0.33, 0.44]]),
    ...            }}}
    >>> stats = SimpleStats(rlzs, quantiles=[0.2])
    >>> stats.compute_and_store('avg_losses', dstore)
    >>> dstore['avg_losses-stats/structural/mean']
    array([[ 0.114,  0.214],
           [ 0.321,  0.428]])
    >>> dstore['avg_losses-stats/structural/quantile-0.2']
    array([[ 0.1,  0.2],
           [ 0.3,  0.4]])
    """
    def __init__(self, rlzs, quantiles=()):
        self.rlzs = rlzs
        self.quantiles = quantiles

    def compute_and_store(self, name, dstore):
        """
        Compute mean and quantiles from the data in the datastore
        under the group `<name>-rlzs` and store them under the group
        `<name>-stats`.
        """
        group = dstore[name + '-rlzs']
        loss_types = sorted(group)
        weights = [rlz.weight for rlz in self.rlzs]
        for loss_type in loss_types:
            data = [group[loss_type][rlz.uid][:] for rlz in self.rlzs]
            dstore['%s-stats/%s/mean' % (name, loss_type)] = mean_curve(
                data, weights)
            for q in self.quantiles:
                values = quantile_curve([d.T[0] for d in data], q, weights)
                ins_values = quantile_curve([d.T[1] for d in data], q, weights)
                path = '%s-stats/%s/quantile-%s' % (name, loss_type, q)
                dstore[path] = numpy.array([values, ins_values]).T  # N x 2


class StatsBuilder(object):
    """
    A class to build risk statistics
    """
    def __init__(self, quantiles,
                 conditional_loss_poes, poes_disagg,
                 normalize_curves=normalize_curves):
        self.quantiles = quantiles
        self.conditional_loss_poes = conditional_loss_poes
        self.poes_disagg = poes_disagg
        self.normalize_curves = normalize_curves
        self.mean_quantiles = ['mean']
        for q in quantiles:
            self.mean_quantiles.append('quantile-%s' % q)

    def normalize(self, loss_curves):
        """
        Normalize the loss curves by using the provided normalization function
        """
        return list(map(self.normalize_curves,
                        numpy.array(loss_curves).transpose(1, 0, 2, 3)))

    def build(self, all_outputs, prefix=''):
        """
        Build all statistics from a set of risk outputs.

        :param all_outputs:
            a non empty sequence of risk outputs referring to the same assets
            and loss_type. Each output must have attributes assets, loss_type,
            hid, weight, loss_curves and insured_curves (the latter is
            possibly None).
        :returns:
            an Output object with the following attributes
            (numpy arrays; the shape is in parenthesis, N is the number of
            assets, R the resolution of the loss curve, P the number of
            conditional loss poes, Q the number of quantiles):

            01. assets (N)
            02. loss_type (1)
            03. mean_curves (N, 2, R)
            04. mean_average_losses (N)
            05. mean_map (P, N)
            06. mean_fractions (P, N)
            07. quantile_curves (Q, N, 2, R)
            08. quantile_average_losses (Q, N)
            09. quantile_maps (Q, P, N)
            10. quantile_fractions (Q, P, N)
            11. mean_insured_curves (N)
            12. mean_average_insured_losses (N)
            13. quantile_insured_curves (Q, N, 2, R)
            14. quantile_average_insured_losses (Q, N)
            15. quantiles (Q)
        """
        outputs = []
        weights = []
        loss_curves = []
        average_losses = []
        average_insured_losses = []
        for out in all_outputs:
            outputs.append(out)
            weights.append(out.weight)
            loss_curves.append(out.loss_curves)
            average_losses.append(out.average_losses)
            average_insured_losses.append(out.average_insured_losses)
        average_losses = numpy.array(average_losses, F32)
        mean_average_losses = mean_curve(average_losses, weights)
        quantile_average_losses = quantile_matrix(
            average_losses, self.quantiles, weights)
        (mean_curves, mean_maps, quantile_curves, quantile_maps) = (
            exposure_statistics(
                self.normalize(loss_curves),
                self.conditional_loss_poes + self.poes_disagg,
                weights, self.quantiles))

        if outputs[0].insured_curves is not None:
            average_insured_losses = numpy.array(average_insured_losses, F32)
            mean_average_insured_losses = mean_curve(
                average_insured_losses, weights)
            quantile_average_insured_losses = quantile_matrix(
                average_insured_losses, self.quantiles, weights)
            loss_curves = [out.insured_curves for out in outputs]
            (mean_insured_curves, _, quantile_insured_curves, _) = (
                exposure_statistics(
                    self.normalize(loss_curves), [], weights, self.quantiles))
        else:
            mean_insured_curves = None
            mean_average_insured_losses = None
            quantile_insured_curves = None
            quantile_average_insured_losses = None

        clp = len(self.conditional_loss_poes)
        loss_type = outputs[0].loss_type
        paths = ['/'.join([prefix + '%s-stats', loss_type, name])
                 for name in self.mean_quantiles]
        # generate paths like
        # <prefix>%s-stats/structural/mean
        # <prefix>%s-stats/structural/quantile-0.1
        # <prefix>%s-stats/structural/quantile-0.2
        # ...
        return Output(
            assets=outputs[0].assets,
            loss_type=loss_type,
            mean_curves=mean_curves,
            mean_average_losses=mean_average_losses,
            mean_maps=mean_maps[0:clp, :],  # P x N matrix
            mean_fractions=mean_maps[clp:, :],  # P x N matrix
            quantile_curves=quantile_curves,
            quantile_average_losses=quantile_average_losses,
            quantile_maps=quantile_maps[:, 0:clp],  # Q x P x N matrix
            quantile_fractions=quantile_maps[:, clp:],  # Q x P x N matrix
            mean_insured_curves=mean_insured_curves,
            mean_average_insured_losses=mean_average_insured_losses,
            quantile_insured_curves=quantile_insured_curves,
            quantile_average_insured_losses=quantile_average_insured_losses,
            quantiles=self.quantiles,
            conditional_loss_poes=self.conditional_loss_poes,
            prefix=prefix, paths=paths)


def _combine_mq(mean, quantile):
    # combine mean and quantile into a single array of length Q + 1
    shape = mean.shape
    Q = len(quantile)
    assert quantile.shape[1:] == shape, (quantile.shape[1:], shape)
    array = numpy.zeros((Q + 1,) + shape)
    array[0] = mean
    array[1:] = quantile
    return array


def _loss_curves(assets, mean, mean_averages, quantile, quantile_averages):
    R = mean.shape[-1]
    loss_curve_dt = numpy.dtype([('losses', (float, R)), ('poes', (float, R)),
                                 ('avg', float)])
    mq_curves = _combine_mq(mean, quantile)  # shape (Q + 1, N, 2, R)
    mq_avgs = _combine_mq(mean_averages, quantile_averages)  # (Q + 1, N)
    acc = []
    for mq_curve, mq_avg in zip(mq_curves.transpose(1, 0, 2, 3), mq_avgs.T):
        lcs = []
        for (losses, poes), avg in zip(mq_curve, mq_avg):
            lcs.append((losses, poes, avg))
        acc.append(numpy.array(lcs, loss_curve_dt))
    return numpy.array(acc, loss_curve_dt).T  # (Q + 1, N)


def get_stat_curves(stats):
    """
    :param stats:
        an object with attributes mean_curves, mean_average_losses, mean_maps,
        quantile_curves, quantile_average_losses, quantile_loss_curves,
        quantile_maps, mean_insured_curves, mean_average_insured_losses,
        quantile_insured_curves, quantile_average_insured_losses, assets.
        There is also a loss_type attribute which must be always the same.
    :returns:
        statistical loss curves per asset
    """
    curves = _loss_curves(
        stats.assets, stats.mean_curves, stats.mean_average_losses,
        stats.quantile_curves, stats.quantile_average_losses)

    insured_curves = [] if stats.mean_insured_curves is None else _loss_curves(
        stats.assets, stats.mean_insured_curves,
        stats.mean_average_insured_losses,
        stats.quantile_insured_curves,
        stats.quantile_average_insured_losses)

    if stats.conditional_loss_poes:
        mq = _combine_mq(stats.mean_maps, stats.quantile_maps)

        poes = ['poe~%s' % clp for clp in stats.conditional_loss_poes]
        loss_map_dt = numpy.dtype([(poe, float) for poe in poes])

        Q1, P, N = mq.shape
        maps = numpy.zeros((Q1, N), loss_map_dt)
        for i, imaps in enumerate(mq):
            for poe, imap in zip(poes, imaps):
                maps[i, :][poe] = imap
    else:
        maps = []
    return curves, insured_curves, maps  # shape (Q1, N)
