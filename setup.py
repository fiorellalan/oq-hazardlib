# The OpenQuake Library
# Copyright (C) 2012-2015, GEM Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import sys
from setuptools import setup, find_packages, Extension
from setuptools import setup, find_packages


import numpy

def get_version():
    version_re = r"^__version__\s+=\s+['\"]([^'\"]*)['\"]"
    version = None

    package_init = 'openquake/hazardlib/__init__.py'
    for line in open(package_init, 'r'):
        version_match = re.search(version_re, line, re.M)
        if version_match:
            version = version_match.group(1)
            break
    else:
        sys.exit('__version__ variable not found in %s' % package_init)

    return version
version = get_version()

url = "http://github.com/gem/oq-hazardlib"

cd = os.path.dirname(os.path.join(__file__))

setup(
    name='openquake.hazardlib',
    version=version,
    description="oq-hazardlib is a library for performing seismic analysis",
    long_description=open(os.path.join(cd, 'README.md')).read(),
    url=url,
    packages=find_packages(exclude=['tests', 'tests.*']),
    install_requires=[
        'numpy',
        'scipy',
        'shapely',
        'psutil',
    ],
    ext_modules=[geodetic_speedups, geoutils_speedups],
    include_dirs=include_dirs,
    scripts=['openquake/hazardlib/tests/gsim/check_gsim.py'],
    author='GEM Foundation',
    author_email='devops@openquake.org',
    maintainer='GEM Foundation',
    maintainer_email='devops@openquake.org',
    classifiers=(
        'Development Status :: 4 - Beta',
        'Intended Audience :: Education',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: GNU Affero General Public License v3',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 2',
        'Topic :: Scientific/Engineering',
    ),
    keywords="seismic hazard risk",
    license="AGPL3",
    platforms=["any"],
    package_data={"openquake.hazardlib": [
        "README.rst", "LICENSE", "CONTRIBUTORS.txt"]},
    namespace_packages=['openquake'],
    entry_points={
        'console_scripts': [
            'oq-lite = openquake.commonlib.commands.__main__:oq_lite']},
    include_package_data=True,
    namespace_packages=['openquake'],
    test_loader='openquake.baselib.runtests:TestLoader',
    test_suite='openquake.baselib,openquake.hazardlib,openquake.risklib,openquake.commonlib',
    zip_safe=False,
)
