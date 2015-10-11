#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2014, GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import random
import logging
import collections

import numpy

from openquake.hazardlib.calc import filters
from openquake.hazardlib.calc.gmf import GmfComputer
from openquake.commonlib import readinput, parallel, datastore

from openquake.calculators import base, calc

Rupture = collections.namedtuple('Rupture', 'tag seed rupture')


@parallel.litetask
def calc_gmfs(tag_seed_pairs, computer, monitor):
    """
    Computes several GMFs in parallel, one for each tag and seed.

    :param tag_seed_pairs:
        list of pairs (rupture tag, rupture seed)
    :param computer:
        :class:`openquake.hazardlib.calc.gmf.GMFComputer` instance
    :param monitor:
        :class:`openquake.baselib.performance.PerformanceMonitor` instance
    :returns:
        a dictionary tag -> gmf
    """
    tags, seeds = zip(*tag_seed_pairs)
    return dict(zip(tags, computer.compute(seeds)))


@base.calculators.add('scenario')
class ScenarioCalculator(base.HazardCalculator):
    """
    Scenario hazard calculator
    """
    core_func = calc_gmfs
    tags = datastore.persistent_attribute('tags')
    sescollection = datastore.persistent_attribute('sescollection')
    is_stochastic = True

    def pre_execute(self):
        """
        Read the site collection and initialize GmfComputer, tags and seeds
        """
        super(ScenarioCalculator, self).pre_execute()
        trunc_level = self.oqparam.truncation_level
        correl_model = readinput.get_correl_model(self.oqparam)
        n_gmfs = self.oqparam.number_of_ground_motion_fields
        rupture = readinput.get_rupture(self.oqparam)
        self.gsims = readinput.get_gsims(self.oqparam)
        self.rlzs_assoc = readinput.get_rlzs_assoc(self.oqparam)

        with self.monitor('filtering sites', autoflush=True):
            self.sitecol = filters.filter_sites_by_distance_to_rupture(
                rupture, self.oqparam.maximum_distance, self.sitecol)
        if self.sitecol is None:
            raise RuntimeError(
                'All sites were filtered out! '
                'maximum_distance=%s km' % self.oqparam.maximum_distance)
        self.tags = numpy.array(
            sorted(['scenario-%010d' % i for i in range(n_gmfs)]),
            (bytes, 100))
        self.computer = GmfComputer(
            rupture, self.sitecol, self.oqparam.imtls, self.gsims,
            trunc_level, correl_model)
        rnd = random.Random(self.oqparam.random_seed)
        self.tag_seed_pairs = [(tag, rnd.randint(0, calc.MAX_INT))
                               for tag in self.tags]
        self.sescollection = [{tag: Rupture(tag, seed, rupture)
                               for tag, seed in self.tag_seed_pairs}]

    def execute(self):
        """
        Compute the GMFs in parallel and return a dictionary gmf_by_tag
        """
        with self.monitor('computing gmfs', autoflush=True):
            args = (self.tag_seed_pairs, self.computer, self.monitor('calc_gmfs'))
            gmf_by_tag = parallel.apply_reduce(
                self.core_func.__func__, args,
                concurrent_tasks=self.oqparam.concurrent_tasks)
            return gmf_by_tag
    
    def post_execute(self, gmf_by_tag):
        """
        :param gmf_by_tag: a dictionary tag -> gmf
        """
        with self.monitor('saving gmfs', autoflush=True):
            data = []
            for ordinal, tag in enumerate(sorted(gmf_by_tag)):
                gmf = gmf_by_tag[tag]
                gmf['idx'] = ordinal
                data.append(gmf)
            gmfa = numpy.concatenate(data)
            self.datastore['gmfs/col00'] = gmfa
            self.datastore['gmfs'].attrs['nbytes'] = gmfa.nbytes
