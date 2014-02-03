# Copyright (c) 2010-2013, GEM Foundation.
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

"""
Core calculator functionality for computing stochastic event sets and ground
motion fields using the 'event-based' method.

Stochastic events sets (which can be thought of as collections of ruptures) are
computed iven a set of seismic sources and investigation time span (in years).

For more information on computing stochastic event sets, see
:mod:`openquake.hazardlib.calc.stochastic`.

One can optionally compute a ground motion field (GMF) given a rupture, a site
collection (which is a collection of geographical points with associated soil
parameters), and a ground shaking intensity model (GSIM).

For more information on computing ground motion fields, see
:mod:`openquake.hazardlib.calc.gmf`.
"""

import random
import collections

import numpy.random

from django.db import transaction
from openquake.hazardlib.calc import filters
from openquake.hazardlib.calc import gmf
from openquake.hazardlib.imt import from_string

from openquake.engine import writer, logs
from openquake.engine.calculators.hazard import general
from openquake.engine.calculators.hazard.classical import (
    post_processing as cls_post_proc)
from openquake.engine.calculators.hazard.event_based import post_processing
from openquake.engine.db import models
from openquake.engine.input import logictree
from openquake.engine.utils import tasks
from openquake.engine.performance import EnginePerformanceMonitor, LightMonitor


#: Always 1 for the computation of ground motion fields in the event-based
#: hazard calculator.
DEFAULT_GMF_REALIZATIONS = 1

# NB: beware of large caches
inserter = writer.CacheInserter(models.GmfData, 1000)


class RuptureCollector(object):
    """
    Keep in memory all the ruptures of the given ses_collection
    with number of occurrencies greater than zero.
    """
    def __init__(self, ses_collection, ordinal):
        self.ses_collection = ses_collection
        self.ordinal = ordinal
        self._dd = collections.defaultdict(list)

    def add(self, ses_ordinal, src_id, rup, num_occurrencies):
        if num_occurrencies:
            self._dd[src_id, rup].append((ses_ordinal, num_occurrencies))

    def get_ruptures(self):
        """
        """
        for src_id, rup in sorted(self._dd):
            for ses, num_occurrencies in self._dd[src_id, rup]:
                for _ in range(num_occurrencies):
                    yield rup

    def __len__(self):  # number of unique ruptures
        return len(self._dd)

    def __cmp__(self, other):
        return cmp(self.ordinal, other.ordinal)

    def clear(self):
        self._dd.clear()

    def save_ses_ruptures(self):
        """
        """
        ses_coll = self.ses_collection
        all_ses = list(models.SES.objects.filter(ses_collection=ses_coll))
        with transaction.commit_on_success(using='job_init'):
            for src_id, rup in sorted(self._dd):
                for ses_ordinal, num_occurrencies in self._dd[src_id, rup]:
                    for i in range(num_occurrencies):
                        models.SESRupture.objects.create(
                            ses=all_ses[ses_ordinal - 1],
                            rupture=rup,
                            tag='rlz=%02d|ses=%04d|src=%s|i=%03d'
                            % (ses_coll.lt_realization.ordinal,
                               ses_ordinal, src_id, i),
                            hypocenter=rup.hypocenter.wkt2d,
                            magnitude=rup.mag,
                        )


# Disabling pylint for 'Too many local variables'
# pylint: disable=R0914
@tasks.oqtask
def compute_ses(job_id, src_seeds, ses_coll, task_no):
    """
    Celery task for the stochastic event set calculator.

    Samples logic trees and calls the stochastic event set calculator.

    Once stochastic event sets are calculated, results will be saved to the
    database. See :class:`openquake.engine.db.models.SESCollection`.

    Optionally (specified in the job configuration using the
    `ground_motion_fields` parameter), GMFs can be computed from each rupture
    in each stochastic event set. GMFs are also saved to the database.

    :param int job_id:
        ID of the currently running job.
    :param src_seeds:
        List of pairs (source, seed)
    :param ses_coll:
        an instance of :class:`openquake.engine.db.models.SESCollection`
    """
    rnd = random.Random()
    all_ses = models.SES.objects.filter(ses_collection=ses_coll)
    collector = RuptureCollector(ses_coll, task_no)

    mon1 = LightMonitor('generating ruptures', job_id, compute_ses)
    mon2 = LightMonitor('sampling ruptures', job_id, compute_ses)

    # Compute and save stochastic event sets
    for src, seed in src_seeds:
        rnd.seed(seed)
        with mon1:
            rupts = list(src.iter_ruptures())

        for ses in all_ses:
            numpy.random.seed(rnd.randint(0, models.MAX_SINT_32))
            for i, r in enumerate(rupts):
                with mon2:
                    collector.add(ses.ordinal, src.source_id,
                                  r, r.sample_number_of_occurrences())
    mon1.flush()
    mon2.flush()
    with EnginePerformanceMonitor('saving ses', job_id, compute_ses):
        collector.save_ses_ruptures()
    return collector


@tasks.oqtask
def compute_gmf(job_id, gmf_coll, gsims, collector, seed):
    """
    Compute and save the GMFs for all the ruptures in the given block.
    """
    hc = models.HazardCalculation.objects.get(oqjob=job_id)
    imts = map(from_string, hc.intensity_measure_types)
    params = dict(
        correl_model=general.get_correl_model(hc),
        truncation_level=hc.truncation_level,
        maximum_distance=hc.maximum_distance)
    with EnginePerformanceMonitor(
            'computing gmfs', job_id, compute_gmf):
        gmvs_per_site = _compute_gmf(
            params, imts, gsims, hc.site_collection, collector, seed)

    with EnginePerformanceMonitor('saving gmfs', job_id, compute_gmf):
        _save_gmfs(gmf_coll, gmvs_per_site, hc.site_collection,
                   collector.ordinal)


# NB: I tried to return a single dictionary {site_id: [(gmv, rupt_id),...]}
# but it takes a lot more memory (MS)
def _compute_gmf(params, imts, gsims, site_coll, collector, seed):
    """
    Compute a ground motion field value for each rupture, for all the
    points affected by that rupture, for the given IMT. Returns a
    dictionary with the nonzero contributions to each site id, and a dictionary
    with the ids of the contributing ruptures for each site id.
    assert len(ruptures) == len(rupture_seeds)

    :param params:
        a dictionary containing the keys
        correl_model, truncation_level, maximum_distance
    :param imts:
        a list of hazardlib intensity measure types
    :param gsims:
        a dictionary {tectonic region type -> GSIM instance}
    :param site_coll:
        a SiteCollection instance
    :param ruptures:
        a list of SESRupture objects
    :param seed:
        the collector master seed
    """
    gmvs_per_site = collections.defaultdict(list)
    rnd = random.Random()
    rnd.seed(seed)

    # Compute and save ground motion fields
    for rupture in collector.get_ruptures():
        gmf_calc_kwargs = {
            'rupture': rupture,
            'sites': site_coll,
            'imts': imts,
            'gsim': gsims[rupture.tectonic_region_type],
            'truncation_level': params['truncation_level'],
            'realizations': DEFAULT_GMF_REALIZATIONS,
            'correlation_model': params['correl_model'],
            'rupture_site_filter': filters.rupture_site_distance_filter(
                params['maximum_distance']),
        }
        numpy.random.seed(rnd.randint(0, models.MAX_SINT_32))
        gmf_dict = gmf.ground_motion_fields(**gmf_calc_kwargs)
        for imt, gmf_1_realiz in gmf_dict.iteritems():
            # since DEFAULT_GMF_REALIZATIONS is 1, gmf_1_realiz is a matrix
            # with n_sites rows and 1 column
            for site, gmv in zip(site_coll, gmf_1_realiz):
                gmv = float(gmv)  # convert a 1x1 matrix into a float
                if gmv:  # nonzero contribution to site
                    gmvs_per_site[imt, site.id].append(gmv)
    return gmvs_per_site


@transaction.commit_on_success(using='job_init')
def _save_gmfs(gmf, gmvs_per_site, sites, task_no):
    """
    Helper method to save computed GMF data to the database.

    :param gmf:
        The Gmf instance where to save
    :param gmf_per_site:
        The GMFs per rupture
    :param sites:
        An :class:`openquake.hazardlib.site.SiteCollection` object,
        representing the sites of interest for a calculation.
    :param task_no:
        The ordinal of the task which generated the current GMFs to save
    """
    for imt, site_id in gmvs_per_site:
        imt_name, sa_period, sa_damping = imt
        inserter.add(models.GmfData(
            gmf=gmf,
            task_no=task_no,
            imt=imt_name,
            sa_period=sa_period,
            sa_damping=sa_damping,
            site_id=site_id,
            gmvs=gmvs_per_site[imt, site_id],
            ))
    inserter.flush()


class EventBasedHazardCalculator(general.BaseHazardCalculator):
    """
    Probabilistic Event-Based hazard calculator. Computes stochastic event sets
    and (optionally) ground motion fields.
    """
    core_calc_task = compute_ses

    def pre_execute(self):
        """
        Do pre-execution work. At the moment, this work entails:
        parsing and initializing sources, parsing and initializing the
        site model (if there is one), parsing vulnerability and
        exposure files, and generating logic tree realizations. (The
        latter piece basically defines the work to be done in the
        `execute` phase.)
        """
        super(EventBasedHazardCalculator, self).pre_execute()
        self.collectors = collections.defaultdict(list)
        for rlz in self._get_realizations():
            self.initialize_ses_db_records(rlz)

    def task_arg_gen(self, _block_size=None):
        """
        Loop through realizations and sources to generate a sequence of
        task arg tuples. Each tuple of args applies to a single task.
        Yielded results are tuples of the form job_id, sources, ses, seeds
        (seeds will be used to seed numpy for temporal occurence sampling).
        """
        hc = self.hc
        rnd = random.Random()
        rnd.seed(hc.random_seed)
        for lt_rlz in self._get_realizations():
            path = tuple(lt_rlz.sm_lt_path)
            blocks = self.source_blocks_per_ltpath[path]
            ses_coll = models.SESCollection.objects.get(lt_realization=lt_rlz)
            for task_no, block in enumerate(blocks):
                ss = [(src, rnd.randint(0, models.MAX_SINT_32))
                      for src in block]  # source, seed pairs
                yield self.job.id, ss, ses_coll, task_no

        # now the source_blocks_per_ltpath dictionary can be cleared
        self.source_blocks_per_ltpath.clear()

    def task_completed(self, collector):
        """
        Collect the ruptures
        """
        self.collectors[collector.ses_collection].append(collector)
        self.log_percent()

    def compute_gmf_arg_gen(self):
        """
        Argument generator for the task compute_gmf. For each SES yields a
        tuple of the form (job_id, gmf_coll, gsims, rupture_ids, rupture_seeds,
        task_no).
        """
        rnd = random.Random()
        rnd.seed(self.hc.random_seed)
        for lt_rlz in self._get_realizations():
            ltp = logictree.LogicTreeProcessor.from_hc(self.hc)
            gsims = ltp.parse_gmpe_logictree_path(lt_rlz.gsim_lt_path)
            ses_coll = models.SESCollection.objects.get(lt_realization=lt_rlz)
            gmf_coll = models.Gmf.objects.get(
                lt_realization=ses_coll.lt_realization)
            for collector in self.collectors[ses_coll]:  # now ordered
                num_ruptures = len(collector)
                if num_ruptures == 0:
                    continue
                logs.LOG.info('Sending %d ruptures', num_ruptures)
                seed = rnd.randint(0, models.MAX_SINT_32)
                yield self.job.id, gmf_coll, gsims, collector, seed
        self.collectors.clear()  # to save memory

    def post_execute(self):
        """
        Optionally compute_gmf in parallel.
        """
        for collectors in self.collectors.itervalues():
            collectors.sort()

        if self.hc.ground_motion_fields:
            self.parallelize(compute_gmf,
                             self.compute_gmf_arg_gen(),
                             self.log_percent)

    def initialize_ses_db_records(self, lt_rlz):
        """
        Create :class:`~openquake.engine.db.models.Output`,
        :class:`~openquake.engine.db.models.SESCollection` and
        :class:`~openquake.engine.db.models.SES` "container" records for
        a single realization.

        Stochastic event set ruptures computed for this realization will be
        associated to these containers.

        NOTE: Many tasks can contribute ruptures to the same SES.
        """
        output = models.Output.objects.create(
            oq_job=self.job,
            display_name='SES Collection rlz-%s' % lt_rlz.id,
            output_type='ses')

        ses_coll = models.SESCollection.objects.create(
            output=output, lt_realization=lt_rlz)

        if self.job.hazard_calculation.ground_motion_fields:
            output = models.Output.objects.create(
                oq_job=self.job,
                display_name='GMF rlz-%s' % lt_rlz.id,
                output_type='gmf')

            models.Gmf.objects.create(
                output=output, lt_realization=lt_rlz)

        all_ses = []
        for i in xrange(1, self.hc.ses_per_logic_tree_path + 1):
            all_ses.append(
                models.SES.objects.create(
                    ses_collection=ses_coll,
                    investigation_time=self.hc.investigation_time,
                    ordinal=i))
        return all_ses

    def post_process(self):
        """
        If requested, perform additional processing of GMFs to produce hazard
        curves.
        """
        if self.hc.hazard_curves_from_gmfs:
            with EnginePerformanceMonitor('generating hazard curves',
                                          self.job.id):
                self.parallelize(
                    post_processing.gmf_to_hazard_curve_task,
                    post_processing.gmf_to_hazard_curve_arg_gen(self.job),
                    self.log_percent)

            # If `mean_hazard_curves` is True and/or `quantile_hazard_curves`
            # has some value (not an empty list), do this additional
            # post-processing.
            if self.hc.mean_hazard_curves or self.hc.quantile_hazard_curves:
                with EnginePerformanceMonitor(
                        'generating mean/quantile curves', self.job.id):
                    self.do_aggregate_post_proc()

            if self.hc.hazard_maps:
                with EnginePerformanceMonitor(
                        'generating hazard maps', self.job.id):
                    self.parallelize(
                        cls_post_proc.hazard_curves_to_hazard_map_task,
                        cls_post_proc.hazard_curves_to_hazard_map_task_arg_gen(
                            self.job),
                        self.log_percent)
