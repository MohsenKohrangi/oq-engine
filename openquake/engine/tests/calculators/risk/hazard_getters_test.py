# Copyright (c) 2010-2014, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.


import numpy
from openquake.engine.tests.utils import helpers
import unittest
import cPickle as pickle

from openquake.engine.db import models
from openquake.engine.calculators.risk import hazard_getters
from openquake.engine.calculators.risk.base import RiskCalculator

from openquake.engine.tests.utils.helpers import get_data_path


class HazardCurveGetterPerAssetTestCase(unittest.TestCase):

    hazard_demo = get_data_path('simple_fault_demo_hazard/job.ini')
    risk_demo = get_data_path('classical_psha_based_risk/job.ini')
    hazard_output_type = 'curve'
    getter_class = hazard_getters.HazardCurveGetterPerAsset
    taxonomy = 'VF'

    def setUp(self):
        self.job, _ = helpers.get_fake_risk_job(
            self.risk_demo, self.hazard_demo, self.hazard_output_type)

        # need to run pre-execute to parse exposure model
        calc = RiskCalculator(self.job)
        models.JobStats.objects.create(oq_job=self.job)
        calc.pre_execute()

        site_assets = {}
        self.assets = []
        for site_id, assets in calc.rc.get_site_assets_dict().iteritems():
            site_assets[site_id] = [asset for asset in assets
                                    if asset.taxonomy == self.taxonomy]
            self.assets.extend(site_assets[site_id])

        self.getter = self.getter_class(self.ho(), site_assets, "PGA")

    def test_is_pickleable(self):
        pickle.dumps(self.getter)  # raises an error if not

    def ho(self):
        return [self.job.risk_calculation.hazard_output]

    def test_call(self):
        _hid, assets, values = self.getter().next()
        self.assertEqual(
            set(a.id for a in self.assets), set(a.id for a in assets))
        numpy.testing.assert_allclose(
            [[(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)],
             [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)]], values)


class GroundMotionValuesGetterTestCase(HazardCurveGetterPerAssetTestCase):

    hazard_demo = get_data_path('event_based_hazard/job.ini')
    risk_demo = get_data_path('event_based_risk/job.ini')
    hazard_output_type = 'gmf'
    getter_class = hazard_getters.GroundMotionValuesGetter
    taxonomy = 'RM'

    def test_call(self):
        _hid, assets, (gmfs, _ruptures) = self.getter().next()
        for gmvs in gmfs:
            numpy.testing.assert_allclose([0.1, 0.2, 0.3], gmvs)


class GroundMotionScenarioGetterTestCase(HazardCurveGetterPerAssetTestCase):

    hazard_demo = get_data_path('scenario_hazard/job.ini')
    risk_demo = get_data_path('scenario_risk/job.ini')
    hazard_output_type = 'gmf_scenario'
    getter_class = hazard_getters.GroundMotionValuesGetter
    taxonomy = 'RM'

    def test_call(self):
        hazard = list(self.getter())
        self.assertEqual(1, len(hazard))
        _hid, _assets, gmfs = hazard[0]
        for gmvs in gmfs:
            numpy.testing.assert_allclose([0.1, 0.2, 0.3], gmvs)
