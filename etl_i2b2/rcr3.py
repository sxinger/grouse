"""rcr3 -- ETL tasks to support CancerRCR#Aim3
"""
from datetime import datetime
from typing import List

import luigi

from script_lib import Script
from sql_syntax import Environment
import etl_tasks as et
import param_val as pv

DateParam = pv._valueOf(datetime(2001, 1, 1, 0, 0, 0), luigi.DateParameter)


class BuildCohort(et.UploadTask):
    script = Script.build_cohort
    site_id = pv.StrParam(description='KUMC or MCW etc.')
    inclusion_concept_cd = pv.StrParam(default='SEER_SITE:26000')
    dx_date_min = DateParam(default=datetime(2011, 1, 1, 0, 0, 0))
    _keys = None
    _key_seqs = ['QT_SQ_QRI_QRIID',
                 'QT_SQ_QM_QMID',
                 'QT_SQ_QRI_QRIID']  # ISSUE: same sequence?

    @property
    def source(self) -> et.SourceTask:
        return SiteI2B2(star_schema='BLUEHERONDATA_' + self.site_id)

    @property
    def variables(self) -> Environment:
        return dict(
            I2B2_STAR=self.project.star_schema,
            I2B2_STAR_SITE=self.source.star_schema,
        )

    def script_params(self, conn: et.LoggedConnection) -> Environment:
        upload_params = et.UploadTask.script_params(self, conn)

        [result_instance_id,
         query_master_id,
         query_instance_id] = self._allocate_keys(conn)
        return dict(upload_params,
                    task_id=self.task_id,
                    inclusion_concept_cd=self.inclusion_concept_cd,
                    dx_date_min=self.dx_date_min,
                    result_instance_id=result_instance_id,
                    query_instance_id=query_instance_id,
                    query_master_id=query_master_id,
                    query_name='%s: %s' % (self.site_id, query_master_id),
                    user_id=self.task_family)

    def _allocate_keys(self, conn: et.LoggedConnection):
        if self._keys is not None:
            return self._keys
        self._keys = out = []
        for seq_name in self._key_seqs:
            x = conn.execute("select {schema}.{seq_name}.nextval from dual".format(
                schema=self.project.star_schema, seq_name=seq_name)).scalar()
            out.append(x)
        return out

    def loaded_record(self, conn: et.LoggedConnection, _bulk_rows: int) -> int:
        [result_instance_id, _, _] = self._keys
        size = conn.execute(
            '''
            select set_size from {i2b2_star}.qt_query_result_instance
            where result_instance_id = :result_instance_id
            '''.format(i2b2_star=self.project.star_schema),
            dict(result_instance_id=result_instance_id)
        ).scalar()
        return size


class SiteI2B2(et.SourceTask, et.DBAccessTask):
    star_schema = pv.StrParam(description='BLUEHERONDATA_KUMC or the like')
    table_eg = 'patient_dimension'

    @property
    def source_cd(self) -> str:
        return "'%s'" % self.star_schema

    @property
    def download_date(self) -> datetime:
        with self.connection('download_date') as conn:
            t = conn.execute(
                '''
                select last_ddl_time from all_objects
                where owner=:owner and object_name=:table_eg
                ''',
                dict(owner=self.star_schema.upper(), table_eg=self.table_eg.upper())
            ).scalar()
        return t

    def _dbtarget(self) -> et.DBTarget:
        return et.SchemaTarget(self._make_url(self.account),
                               schema_name=self.star_schema,
                               table_eg=self.table_eg,
                               echo=self.echo)