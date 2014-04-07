# -*- coding: utf-8 -*-

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

"""Base RiskCalculator class."""

import collections
import itertools
import operator

from openquake.engine import logs, export
from openquake.engine.utils import config, general, tasks
from openquake.engine.db import models
from openquake.engine.calculators import base
from openquake.engine.calculators.risk import writers, validation, loaders

from django.db import connections


@tasks.oqtask
def assoc_site_assets(job_id, taxonomy):
    """
    Build the dictionary associating the assets to the closest
    hazard sites for the current exposure model and the given taxonomy
    """
    rc = models.OqJob.objects.get(pk=job_id).risk_calculation
    max_dist = rc.best_maximum_distance * 1000  # km to meters
    hc_id = rc.get_hazard_calculation().id
    cursor = connections['job_init'].cursor()
    cursor.execute("""
  SELECT site_id, array_agg(asset_id ORDER BY asset_id) AS asset_ids FROM (
  SELECT DISTINCT ON (exp.id) exp.id AS asset_id, hsite.id AS site_id
  FROM riski.exposure_data AS exp
  JOIN hzrdi.hazard_site AS hsite
  ON ST_DWithin(exp.site, hsite.location, %s)
  WHERE hsite.hazard_calculation_id = %s
  AND exposure_model_id = %s AND taxonomy=%s
  AND ST_COVERS(ST_GeographyFromText(%s), exp.site)
  ORDER BY exp.id, ST_Distance(exp.site, hsite.location, false)) AS x
  GROUP BY site_id ORDER BY site_id;
   """, (max_dist, hc_id, rc.exposure_model.id, taxonomy,
         rc.region_constraint.wkt))
    return cursor.fetchall()


class RiskCalculator(base.Calculator):
    """
    Abstract base class for risk calculators. Contains a bunch of common
    functionality, including initialization procedures and the core
    distribution/execution logic.

    :attribute dict taxonomies_asset_count:
        A dictionary mapping each taxonomy with the number of assets the
        calculator will work on. Assets are extracted from the exposure input
        and filtered according to the `RiskCalculation.region_constraint`.

    :attribute dict risk_models:
        A nested dict taxonomy -> loss type -> instances of `RiskModel`.
    """

    # a list of :class:`openquake.engine.calculators.risk.validation` classes
    validators = [validation.HazardIMT, validation.EmptyExposure,
                  validation.OrphanTaxonomies, validation.ExposureLossTypes,
                  validation.NoRiskModels]

    def __init__(self, job):
        super(RiskCalculator, self).__init__(job)

        self.taxonomies_asset_count = None
        self.risk_models = None

    def pre_execute(self):
        """
        In this phase, the general workflow is:
            1. Parse the exposure to get the taxonomies
            2. Parse the available risk models
            3. Initialize progress counters
            4. Validate exposure and risk models
        """
        with self.monitor('get exposure'):
            self.taxonomies_asset_count = \
                (self.rc.preloaded_exposure_model or loaders.exposure(
                    self.job, self.rc.inputs['exposure'])
                 ).taxonomies_in(self.rc.region_constraint)

        with self.monitor('parse risk models'):
            self.risk_models = self.get_risk_models()

            # consider only the taxonomies in the risk models if
            # taxonomies_from_model has been set to True in the
            # job.ini
            if self.rc.taxonomies_from_model:
                self.taxonomies_asset_count = dict(
                    (t, count)
                    for t, count in self.taxonomies_asset_count.items()
                    if t in self.risk_models)

        n_assets = sum(self.taxonomies_asset_count.itervalues())
        logs.LOG.info('Considering %d assets of %d distinct taxonomies',
                      n_assets, len(self.taxonomies_asset_count))
        for taxonomy, counts in self.taxonomies_asset_count.iteritems():
            logs.LOG.info('taxonomy=%s, assets=%d', taxonomy, counts)

        for validator_class in self.validators:
            validator = validator_class(self)
            error = validator.get_error()
            if error:
                raise ValueError("""Problems in calculator configuration:
                                 %s""" % error)

        with self.monitor('getting asset chunks'):
            asset_dict = dict(
                (asset.id, asset)
                for asset in models.ExposureData.objects.get_asset_chunk(
                    self.rc))

        def update_dict(acc, site_asset_ids):
            for site_id, asset_ids in site_asset_ids:
                acc[site_id].extend(asset_ids)
            return acc

        arglist = [(self.job.id, taxonomy)
                   for taxonomy in self.taxonomies_asset_count]
        site_asset_ids = tasks.map_reduce(
            assoc_site_assets, arglist, update_dict,
            collections.defaultdict(list))

        self.site_assets = {}
        ok_assets = set()  # assets close to a hazard site within the distance
        for site_id, asset_ids in site_asset_ids.iteritems():
            self.site_assets[site_id] = [asset_dict[asset_id]
                                         for asset_id in asset_ids]
            ok_assets.update(asset_ids)
        missing_assets = set(asset_dict) - ok_assets
        if missing_assets:
            logs.LOG.warn('%d assets are too far from the hazard sites '
                          'and the risk cannot be computed',
                          len(missing_assets))
            for asset_id in missing_assets:
                logs.LOG.info('missing hazard for %s', asset_dict[asset_id])

    # TODO: remove
    def concurrent_tasks(self):
        """
        Number of tasks to be in queue at any given time.
        """
        return int(config.get('risk', 'concurrent_tasks'))

    def task_arg_gen(self):
        """
        Generator function for creating the arguments for each task.

        It is responsible for the distribution strategy. It divides
        the considered exposure into chunks of homogeneous assets
        (i.e. having the same taxonomy). The chunk size is given by
        the `block_size` openquake config parameter.

        :returns:
            An iterator over a list of arguments. Each contains:

            1. the job id
            2. a getter object needed to get the hazard data
            3. the needed risklib calculators
            4. the output containers to be populated
            5. the specific calculator parameter set
        """
        output_containers = writers.combine_builders(
            [builder(self) for builder in self.output_builders])

        nblocks = int(config.get('hazard', 'concurrent_tasks'))
        blocks = general.SequenceSplitter(nblocks).split_on_max_weight(
            [(site_id, len(assets))
             for site_id, assets in self.site_assets.iteritems()])

        for site_ids in blocks:
            taxonomy_site_assets = {}  # {taxonomy: {site_id: assets}}
            for site_id in site_ids:
                for taxonomy, assets in itertools.groupby(
                        self.site_assets[site_id],
                        key=operator.attrgetter('taxonomy')):
                    if (self.rc.taxonomies_from_model and taxonomy not in
                            self.risk_models):
                        # ignore taxonomies not in the risk models
                        # if the parameter taxonomies_from_model is set
                        continue
                    taxonomy_site_assets.setdefault(taxonomy, {})
                    taxonomy_site_assets[taxonomy][site_id] = list(assets)

            calculation_units = []
            for loss_type in models.loss_types(self.risk_models):
                calculation_units.extend(
                    self.calculation_units(loss_type, taxonomy_site_assets))

            yield [self.job.id,
                   calculation_units,
                   output_containers,
                   self.calculator_parameters]

    def _get_outputs_for_export(self):
        """
        Util function for getting :class:`openquake.engine.db.models.Output`
        objects to be exported.
        """
        return export.core.get_outputs(self.job.id)

    def _do_export(self, output_id, export_dir, export_type):
        """
        Risk-specific implementation of
        :meth:`openquake.engine.calculators.base.Calculator._do_export`.

        Calls the risk exporter.
        """
        return export.risk.export(output_id, export_dir, export_type)

    @property
    def rc(self):
        """
        A shorter and more convenient way of accessing the
        :class:`~openquake.engine.db.models.RiskCalculation`.
        """
        return self.job.risk_calculation

    @property
    def hc(self):
        """
        A shorter and more convenient way of accessing the
        :class:`~openquake.engine.db.models.HazardCalculation`.
        """
        return self.rc.get_hazard_calculation()

    @property
    def calculator_parameters(self):
        """
        The specific calculation parameters passed as args to the
        celery task function. A calculator must override this to
        provide custom arguments to its celery task
        """
        return []

    def get_risk_models(self, retrofitted=False):
        """
        Parse vulnerability models for each loss type in
        `openquake.engine.db.models.LOSS_TYPES`,
        then set the `risk_models` attribute.

        :param bool retrofitted:
            True if retrofitted models should be retrieved
        :returns:
            A nested dict taxonomy -> loss type -> instances of `RiskModel`.
        """
        risk_models = collections.defaultdict(dict)

        for v_input, loss_type in self.rc.vulnerability_inputs(retrofitted):
            for taxonomy, model in loaders.vulnerability(v_input):
                risk_models[taxonomy][loss_type] = model

        return risk_models

#: Calculator parameters are used to compute derived outputs like loss
#: maps, disaggregation plots, quantile/mean curves. See
#: :class:`openquake.engine.db.models.RiskCalculation` for a description

CalcParams = collections.namedtuple(
    'CalcParams', [
        'conditional_loss_poes',
        'poes_disagg',
        'sites_disagg',
        'insured_losses',
        'quantiles',
        'asset_life_expectancy',
        'interest_rate',
        'mag_bin_width',
        'distance_bin_width',
        'coordinate_bin_width',
        'damage_state_ids'
    ])


def make_calc_params(conditional_loss_poes=None,
                     poes_disagg=None,
                     sites_disagg=None,
                     insured_losses=None,
                     quantiles=None,
                     asset_life_expectancy=None,
                     interest_rate=None,
                     mag_bin_width=None,
                     distance_bin_width=None,
                     coordinate_bin_width=None,
                     damage_state_ids=None):
    """
    Constructor of CalculatorParameters
    """
    return CalcParams(conditional_loss_poes,
                      poes_disagg,
                      sites_disagg,
                      insured_losses,
                      quantiles,
                      asset_life_expectancy,
                      interest_rate,
                      mag_bin_width,
                      distance_bin_width,
                      coordinate_bin_width,
                      damage_state_ids)
