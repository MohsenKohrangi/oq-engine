# Copyright (c) 2013, GEM Foundation.
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


from nose.plugins.attrib import attr as noseattr
from qa_tests import risk


class EventBaseQATestCase(risk.CompleteTestCase, risk.FixtureBasedQATestCase):
    hazard_calculation_fixture = ("QA (regression) test for Risk Event "
                                  "Based from Stochastic Event Set")

    @noseattr('qa', 'risk', 'event_based')
    def test(self):
        expected_losses = [216.691262757, 18.8081710563, 10.5954191755,
                           6.64207106457, 4.54817690713, 3.72047720209,
                           2.90501254159, 1.99267449305, 1.33347897195,
                           1.28734166564, 1.0496680519, 0.485506800572,
                           0.344485529959, 0.284905963952, 0.262058117633,
                           0.218392528855, 0.128050029105, 0.0680244237371,
                           0.0539187782403]

        outputs = self._run_test().output_set
        losses = outputs.get(output_type="event_loss").event_loss
        # print [l.aggregate_loss for l in losses]

        for event_loss, expected in zip(losses, expected_losses):
            self.assertAlmostEqual(
                expected, event_loss.aggregate_loss,
                msg="loss for rupture %r is %s (expected %s)" % (
                    event_loss.rupture.tag, event_loss.aggregate_loss,
                    expected))
