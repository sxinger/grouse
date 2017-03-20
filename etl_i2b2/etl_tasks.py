'''etl_tasks -- Source-agnostic Luigi ETL Task support

Note: This is source-agnostic but not target-agnositc; it has i2b2
      knowledge.

'''

from typing import Any, Dict, Iterator, List, Optional as Opt, Tuple, cast
from contextlib import contextmanager
from datetime import datetime
import csv
import logging

from luigi.contrib.sqla import SQLAlchemyTarget
from sqlalchemy import text as sql_text, func, Table, Column
from sqlalchemy.engine import Connection
from sqlalchemy.engine.result import ResultProxy
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DatabaseError
import sqlalchemy as sqla
import luigi
from cx_Oracle import Error as OraError, _Error as Ora_Error

from eventlog import EventLogger, LogState, JSONObject
from param_val import StrParam, IntParam, BoolParam
from script_lib import Script
from sql_syntax import Environment, Params, Name, SQL
from sql_syntax import insert_append_table, param_names, params_used
import ont_load

log = logging.getLogger(__name__)


class DBTarget(SQLAlchemyTarget):
    '''Take advantage of engine caching logic from SQLAlchemyTarget,
    but don't bother with target_table, update_id, etc.

    >>> t = DBTarget(connection_string='sqlite:///')
    >>> t.engine.scalar('select 1 + 1')
    2
    '''
    def __init__(self, connection_string: str,
                 target_table: Opt[str]=None, update_id: Opt[str]=None,
                 echo: bool=False) -> None:
        SQLAlchemyTarget.__init__(
            self, connection_string,
            target_table=target_table,
            update_id=update_id,
            echo=echo)

    def exists(self) -> bool:
        raise NotImplementedError

    def touch(self) -> None:
        raise NotImplementedError


class ETLAccount(luigi.Config):
    account = StrParam(description='see luigi.cfg.example',
                       default='')
    passkey = StrParam(description='see luigi.cfg.example',
                       default='')
    ssh_tunnel = StrParam(description='see luigi.cfg.example',
                          default='')
    echo = BoolParam(description='SQLAlchemy echo logging')


class LoggedConnection(object):
    def __init__(self, conn: Connection, log: EventLogger,
                 step: LogState) -> None:
        self._conn = conn
        self.log = log
        self.step = step

    def _log_args(self, event: str, operation: object,
                  params: Params) -> Tuple[str, JSONObject]:
        msg = '%(event)s %(statement)s' + ('\n%(params)s' if params else '')
        argobj = dict(event=event, statement=str(operation),
                      params=params)
        return msg, argobj

    def execute(self, operation: object, params: Opt[Params] = None) -> ResultProxy:
        msg, argobj = self._log_args('execute', operation, params or {})
        with self.log.step(msg, argobj):
            return self._conn.execute(operation, params or {})

    def scalar(self, operation: object, params: Opt[Params] = None) -> Any:
        msg, argobj = self._log_args('scalar', operation, params or {})
        with self.log.step(msg, argobj) as step:
            result = self._conn.scalar(operation, params or {})
            step.extra.update(result=result)
            return result


class DBAccessTask(luigi.Task):
    account = StrParam(default=ETLAccount().account)
    ssh_tunnel = StrParam(default=ETLAccount().ssh_tunnel,
                          significant=False)
    passkey = StrParam(default=ETLAccount().passkey,
                       significant=False)
    # TODO: proper logging
    echo = BoolParam(default=ETLAccount().echo,
                     significant=False)
    _log = logging.getLogger('DBAccessTask')  # ISSUE: ambient.

    def output(self) -> luigi.Target:
        return self._dbtarget()

    def _dbtarget(self) -> DBTarget:
        return DBTarget(self._make_url(self.account),
                        target_table=None, update_id=self.task_id,
                        echo=self.echo)

    def _make_url(self, account: str) -> str:
        url = make_url(account)
        if self.passkey:
            from os import environ  # ISSUE: ambient
            url.password = environ[self.passkey]
        if self.ssh_tunnel:
            host, port = self.ssh_tunnel.split(':', 1)
            url.host = host
            url.port = port
        return str(url)

    def log_info(self) -> Dict[str, Any]:
        return dict(self.to_str_params(only_significant=True),
                    task_family=self.task_family,
                    task_hash=self.task_id[-luigi.task.TASK_ID_TRUNCATE_HASH:])

    @contextmanager
    def connection(self, event: str='connect') -> Iterator[LoggedConnection]:
        engine = self._dbtarget().engine
        try:
            conn = engine.connect()
        except DatabaseError as exc:
            raise ConnectionProblem.refine(exc, str(engine))
        log = EventLogger(self._log, self.log_info())
        with log.step('%(event)s: <%(account)s>',
                      dict(event=event, account=self.account)) as step:
            yield LoggedConnection(conn, log, step)


class SqlScriptTask(DBAccessTask):
    '''
    >>> variables = dict(I2B2STAR='I2B2DEMODATA', CMS_RIF='CMS',
    ...                  cms_source_cd='X', bene_id_source='b')
    >>> txform = SqlScriptTask(
    ...    account='sqlite:///', passkey=None,
    ...    script=Script.cms_patient_mapping,
    ...    param_vars=variables)

    >>> [task.script for task in txform.requires()]
    ... #doctest: +ELLIPSIS
    [<Script(i2b2_crc_design)>, <Package(cms_keys)>]

    >>> txform.complete()
    False

    TODO: migrate db_util.run_script() docs for .run()
    - links to ora docs
    '''
    script = cast(Script, luigi.EnumParameter(enum=Script))
    param_vars = cast(Environment, luigi.DictParameter(default={}))
    _log = logging.getLogger('sql_scripts')  # ISSUE: ambient. magic-string

    @property
    def variables(self) -> Environment:
        return self.param_vars

    @property
    def vars_for_deps(self) -> Environment:
        return self.variables

    def requires(self) -> List[luigi.Task]:
        return [SqlScriptTask(script=s,
                              param_vars=self.vars_for_deps,
                              account=self.account,
                              passkey=self.passkey,
                              echo=self.echo)
                for s in self.script.deps()]

    def log_info(self) -> Dict[str, Any]:
        return dict(DBAccessTask.log_info(self),
                    script=self.script.name,
                    filename=self.script.fname)

    def complete(self) -> bool:
        '''Each script's last query tells whether it is complete.

        It should be a scalar query that returns non-zero for done
        and either zero or an error for not done.
        '''

        # In order to support run-only variables as in UploadTask,
        # skip statements with unbound &&variables.
        last_query = self.script.statements(
            skip_unbound=True,
            variables=self.variables)[-1]

        params = params_used(dict(task_id=self.task_id), last_query)
        with self.connection(event='complete query') as conn:
            try:
                result = conn.scalar(sql_text(last_query), params)
                return bool(result)
            except DatabaseError as exc:
                conn.log.warning('%(event)s: %(exc)s',
                                 dict(event='complete query error', exc=exc))
                return False

    def run(self) -> None:
        self.run_bound()

    def run_bound(self,
                  script_params: Opt[Params]=None) -> None:
        with self.connection() as conn:
            self.run_event(conn, script_params=script_params)

    def run_event(self,
                  conn: LoggedConnection,
                  run_vars: Opt[Environment]=None,
                  script_params: Opt[Params]=None) -> int:
        bulk_rows = 0
        ignore_error = False
        run_params = dict(script_params or {}, task_id=self.task_id)
        fname = self.script.fname
        variables = dict(run_vars or {}, **self.variables)
        each_statement = self.script.each_statement(variables=variables)

        for line, _comment, statement in each_statement:
            try:
                bulk_target = insert_append_table(statement)
                if bulk_target:
                    bulk_rows = self.bulk_insert(
                        conn, fname, line, statement, run_params,
                        bulk_target, bulk_rows)
                else:
                    ignore_error = self.execute_statement(
                        conn, fname, line, statement, run_params,
                        ignore_error)
            except DatabaseError as exc:
                db = self._dbtarget().engine
                err = SqlScriptError(exc, self.script, line,
                                     statement, str(db))
                if ignore_error:
                    conn.log.warning('%(event)s: %(error)s',
                                     dict(event='ignore', error=err))
                else:
                    raise err from None
        if bulk_rows > 0:
            conn.log.info('%(filename)s %(event)s %(rowtotal)s rows',
                          dict(filename=self.script.fname,
                               event='script bulk inserted',
                               rowtotal=bulk_rows))

        return bulk_rows

    def execute_statement(self, conn: LoggedConnection, fname: str, line: int,
                          statement: SQL, run_params: Params,
                          ignore_error: bool) -> bool:
        sqlerror = Script.sqlerror(statement)
        if sqlerror is not None:
            return sqlerror
        params = params_used(run_params, statement)
        self.set_status_message(
            '%s:%s:\n%s\n%s' % (fname, line, statement, params))
        # ISSUE: how to log lineno?
        conn.execute(statement, params)
        return ignore_error

    def chunks(self, conn: LoggedConnection, names_used: List[Name]) -> List[Params]:
        return [{}]

    def bulk_insert(self, conn: LoggedConnection, fname: str, line: int,
                    statement: SQL, run_params: Params,
                    bulk_target: str, bulk_rows: int) -> int:
        with conn.log.step(
                '%(filename)s:%(lineno)s: %(event)s into %(into)s',
                dict(event='bulk_insert', into=bulk_target,
                     filename=fname, lineno=line)):
            plan = '\n'.join(self.explain_plan(conn, statement))
            conn.log.info('%(filename)s:%(lineno)s: %(event)s:\n%(plan)s',
                          dict(filename=fname, lineno=line, event='plan',
                               plan=plan))

            chunks = self.chunks(conn, param_names(statement))

            for (chunk_ix, chunk) in enumerate(chunks):
                params = dict(params_used(run_params, statement), **chunk)  # type: Params
                self.set_status_message(
                    '%s:%s: %s chunk %d\n%s\n%s\n%s' % (
                        fname, line, bulk_target,
                        chunk_ix + 1,
                        statement, params, plan))
                with conn.log.step(
                        '%(filename)s:%(lineno)s: '
                        '%(event)s %(chunk_num)d into %(into)s: %(chunk_params)s',
                        dict(filename=fname, lineno=line,
                             event='insert chunk', chunk_num=chunk_ix + 1,
                             into=bulk_target,
                             chunk_params=chunk)) as chunk_step:
                    result = conn.execute(statement, params)
                    rowcount = cast(Opt[int], result.rowcount) or 0
                    bulk_rows += rowcount
                    chunk_step.extra.update(
                        dict(statement=statement, params=params))
                    chunk_step.argobj.update(dict(rowcount=rowcount,
                                                  rowsubtotal=bulk_rows))
                    chunk_step.msg_parts.append(
                        ' %(rowcount)d rows (subtotal: %(rowsubtotal)d)')
                conn.execute('commit')  # limit temp space usage

        return bulk_rows

    def explain_plan(self, work: LoggedConnection, statement: SQL) -> List[str]:
        work.execute('explain plan for ' + statement)
        # ref 19 Using EXPLAIN PLAN
        # Oracle 10g Database Performance Tuning Guide
        # https://docs.oracle.com/cd/B19306_01/server.102/b14211/ex_plan.htm
        plan = work.execute(
            'SELECT PLAN_TABLE_OUTPUT line FROM TABLE(DBMS_XPLAN.DISPLAY())')
        return [row.line for row in plan]  # type: ignore  # sqla


def maybe_ora_err(exc: Exception) -> Opt[Ora_Error]:
    if isinstance(exc, DatabaseError) and isinstance(exc.orig, OraError):
        return cast(Ora_Error, exc.orig.args[0])
    return None


class SqlScriptError(IOError):
    '''Include script file, line number in diagnostics
    '''
    def __init__(self, exc: Exception, script: Script, line: int, statement: SQL,
                 conn_label: str) -> None:
        fname = script.name
        message = '%s <%s>\n%s:%s:\n'
        args = [exc, conn_label, fname, line]
        ora_ex = maybe_ora_err(exc)
        if ora_ex:
            offset = ora_ex.offset
            message += '%s<ERROR>%s'
            args[0] = ora_ex.message
            args += [_pick_lines(statement[:offset], -3, None),
                     _pick_lines(statement[offset:], None, 3)]
        else:
            message += '%s'
            args += [statement]

        self.message = message
        self.args = tuple(args)

    def __str__(self) -> str:
        return self.message % self.args


def _pick_lines(s: str, lo: Opt[int], hi: Opt[int]) -> str:
    return '\n'.join(s.split('\n')[lo:hi])  # type: ignore  # mypy/issues/2410


class TimeStampParameter(luigi.Parameter):
    '''A datetime interchanged as milliseconds since the epoch.
    '''

    def parse(self, s: str) -> datetime:
        ms = int(s)
        return datetime.fromtimestamp(ms / 1000.0)

    def serialize(self, dt: datetime) -> str:
        epoch = datetime.utcfromtimestamp(0)
        ms = (dt - epoch).total_seconds() * 1000
        return str(int(ms))


class SourceTask(luigi.Task):
    @property
    def source_cd(self) -> str:
        raise NotImplementedError

    @property
    def download_date(self) -> datetime:
        raise NotImplementedError


class I2B2Task(object):
    @property
    def project(self) -> 'I2B2ProjectCreate':
        return I2B2ProjectCreate()


class UploadTask(I2B2Task, SqlScriptTask):
    @property
    def source(self) -> SourceTask:
        raise NotImplementedError('subclass must implement')

    @property
    def transform_name(self) -> str:
        return self.task_id

    def output(self) -> luigi.Target:
        return self._upload_target()

    def _upload_target(self) -> 'UploadTarget':
        return UploadTarget(self._make_url(self.account),
                            self.project.upload_table,
                            self.transform_name, self.source,
                            echo=self.echo)

    def requires(self) -> List[luigi.Task]:
        return [self.project, self.source] + SqlScriptTask.requires(self)

    def complete(self) -> bool:
        # Belt and suspenders
        return (self.output().exists() and
                SqlScriptTask.complete(self))

    @property
    def label(self) -> str:
        return self.script.title

    def run(self) -> None:
        upload = self._upload_target()
        with upload.job(self,
                        label=self.label,
                        user_id=make_url(self.account).username) as conn_id_r:
            conn, upload_id, result = conn_id_r
            bulk_rows = SqlScriptTask.run_event(
                self, conn,
                run_vars=dict(upload_id=str(upload_id)),
                script_params=dict(upload_id=upload_id,
                                   download_date=self.source.download_date,
                                   project_id=self.project.project_id))
            result[upload.table.c.loaded_record.name] = bulk_rows


class UploadTarget(DBTarget):
    def __init__(self, connection_string: str,
                 table: sqla.Table, transform_name: str, source: SourceTask,
                 echo: bool=False) -> None:
        DBTarget.__init__(self, connection_string,
                          echo=echo)
        self.table = table
        self.source = source
        self.transform_name = transform_name
        self.upload_id = None  # type: Opt[int]

    def exists(self) -> bool:
        conn = self.engine.connect()
        with conn.begin():
            up_t = self.table
            upload_id = conn.scalar(
                sqla.select([sqla.func.max(up_t.c.upload_id)])
                .select_from(up_t)
                .where(sqla.and_(up_t.c.transform_name == self.transform_name,
                                 up_t.c.load_status == 'OK')))
            return upload_id is not None

    @contextmanager
    def job(self, task: SqlScriptTask, label: str, user_id: str) -> Iterator[
            Tuple[LoggedConnection, int, Params]]:
        event = 'upload job'
        with task.connection(event=event) as conn:
            upload_id = self.insert(conn, label, user_id)

            msg = ' %(upload_id)s for %(label)s'
            info = dict(label=label, upload_id=upload_id)
            conn.step.msg_parts.append(msg)
            conn.step.argobj.update(info)
            conn.log.info(msg, info)  # Go ahead and log the upload_id early.

            result = {}  # type: Params
            yield conn, upload_id, result
            up_t = self.table
            conn.execute(up_t.update()  # type: ignore  # TODO: full sqla stubs
                         .where(up_t.c.upload_id == upload_id)
                         .values(load_status='OK', end_date=func.now(),
                                 **result))

    def insert(self, conn: LoggedConnection, label: str, user_id: str) -> int:
        '''
        :param label: a label for related facts for audit purposes
        :param user_id: an indication of who uploaded the related facts

        ISSUE:
        :param input_file_name: path object for input file (e.g. clarity.dmp)
        '''
        up_t = self.table
        next_q = sql_text(
            '''select {i2b2}.sq_uploadstatus_uploadid.nextval
            from dual'''.format(i2b2=self.table.schema))  # type: ignore  # sqla
        upload_id = conn.scalar(next_q)  # type: int

        conn.execute(up_t.insert()  # type: ignore  # sqla
                     .values(upload_id=upload_id,
                             upload_label=label,
                             user_id=user_id,
                             source_cd=self.source.source_cd,
                             load_date=sqla.func.now(),
                             transform_name=self.transform_name))
        self.upload_id = upload_id
        return upload_id


class I2B2ProjectCreate(DBAccessTask):
    star_schema = StrParam(description='see luigi.cfg.example')
    project_id = StrParam(description='see luigi.cfg.example')
    _meta = None          # type: Opt[sqla.MetaData]
    _upload_table = None  # type: Opt[sqla.Table]

    def output(self) -> 'SchemaTarget':
        return SchemaTarget(self._make_url(self.account),
                            schema_name=self.star_schema,
                            table_eg='patient_dimension',
                            echo=self.echo)

    def run(self) -> None:
        raise NotImplementedError('see heron_create.create_deid_datamart etc.')

    @property
    def metadata(self) -> sqla.MetaData:
        if self._meta:
            return self._meta
        self._meta = meta = sqla.MetaData(schema=self.star_schema)  # type: ignore  # sqla
        return meta

    @property
    def upload_table(self) -> sqla.Table:
        if self._upload_table is not None:
            return self._upload_table
        Column, ty = sqla.Column, sqla.types
        t = sqla.Table(  # type: ignore   # TODO: full sqla stubs
            'upload_status', self.metadata,
            Column('upload_id', ty.Numeric(38, 0, asdecimal=False), primary_key=True),
            Column('upload_label', ty.String(500), nullable=False),
            Column('user_id', ty.String(100), nullable=False),
            Column('source_cd', ty.String(50), nullable=False),
            Column('no_of_record', ty.Numeric(asdecimal=False)),
            Column('loaded_record', ty.Numeric(asdecimal=False)),
            Column('deleted_record', ty.Numeric(asdecimal=False)),
            Column('load_date', ty.DateTime, nullable=False),
            Column('end_date', ty.DateTime),
            Column('load_status', ty.String(100)),
            Column('message', ty.Text),
            Column('input_file_name', ty.Text),
            Column('log_file_name', ty.Text),
            Column('transform_name', ty.String(500)),
            schema=self.star_schema)
        self._upload_table = t
        return t  # type: ignore  # sqla


class SchemaTarget(DBTarget):
    def __init__(self, connection_string: str, schema_name: str, table_eg: str,
                 echo: bool=False) -> None:
        DBTarget.__init__(self, connection_string, echo=echo)
        self.schema_name = schema_name
        self.table_eg = table_eg

    def exists(self) -> bool:
        table = Table(self.table_eg, sqla.MetaData(), schema=self.schema_name)  # type: ignore
        return table.exists(bind=self.engine)  # type: ignore


class ConnectionProblem(DatabaseError):
    '''Provide hints about ssh tunnels.
    '''
    # connection closed, no listener
    tunnel_hint_codes = [12537, 12541]

    @classmethod
    def refine(cls, exc: Exception, conn_label: str) -> Exception:
        '''Recognize known connection problems.

        :returns: customized exception for known
                  problem else exc
        '''
        ora_ex = maybe_ora_err(exc)

        if ora_ex:
            return cls(exc, ora_ex, conn_label)
        return exc

    def __init__(self, exc: DatabaseError, ora_ex: Ora_Error, conn_label: str) -> None:
        DatabaseError.__init__(
            self,
            exc.statement, exc.params,
            exc.connection_invalidated)
        message = '%s <%s>'
        args = [ora_ex, conn_label]

        if exc.statement and ora_ex.offset:
            stmt_rest = exc.statement[
                ora_ex.offset:ora_ex.offset + 120]
            message += '\nat: %s'
            args += [stmt_rest]
        local_conn_prob = (
            ora_ex.code in self.tunnel_hint_codes and
            'localhost' in conn_label)
        if local_conn_prob:
            message += '\nhint: ssh tunnel down?'
        message += '\nin: %s'
        args += [ora_ex.context]
        self.message = message
        self.args = tuple(args)

    def __str__(self) -> str:
        return self.message % self.args


class ReportTask(DBAccessTask):
    @property
    def script(self) -> Script:
        raise NotImplementedError('subclass must implement')

    @property
    def report_name(self) -> str:
        raise NotImplementedError('subclass must implement')

    def complete(self) -> bool:
        '''Double-check requirements as well as output.
        '''
        deps = luigi.task.flatten(self.requires())  # type: List[luigi.Task]
        return (self.output().exists() and
                all(t.complete() for t in deps))

    def _csvout(self) -> 'CSVTarget':
        return CSVTarget(path=self.report_name + '.csv')

    def output(self) -> luigi.Target:
        return self._csvout()

    def run(self) -> None:
        with self.connection() as conn:
            query = sql_text(
                'select * from {object}'.format(object=self.report_name))
            result = conn.execute(query)
            cols = result.keys()
            rows = result.fetchall()
            self._csvout().export(cols, rows)


class CSVTarget(luigi.local_target.LocalTarget):
    def export(self, cols: List[str], data: List) -> None:
        with self.open('wb') as stream:
            dest = csv.writer(stream)  # type: ignore  # typeshed/issues/24
            dest.writerow(cols)
            dest.writerows(data)

    @contextmanager
    def dictreader(self,
                   lowercase_fieldnames: bool=False,
                   delimiter: str=',') -> Iterator[csv.DictReader]:
        '''DictReader contextmanager

        @param lowercase_fieldnames: sqlalchemy uses lower-case bind
               parameter names, but SCILHS CSV file headers use the
               actual uppercase column names. So we got:

                 CompileError: The 'oracle' dialect with current
                 database version settings does not support empty
                 inserts.


        '''
        with self.open('rb') as stream:
            dr = csv.DictReader(stream, delimiter=delimiter)  # type: ignore  # typeshed/issues/24
            if lowercase_fieldnames:
                # This is a bit of a kludge, but it works...
                dr.fieldnames = [n.lower() for n in dr.fieldnames]
            yield dr


class AdHoc(DBAccessTask):
    sql = StrParam()
    name = StrParam()

    def _csvout(self) -> CSVTarget:
        return CSVTarget(path=self.name + '.csv')

    def output(self) -> luigi.Target:
        return self._csvout()

    def run(self) -> None:
        with self.connection() as work:
            result = work.execute(self.sql)
            cols = result.keys()
            rows = result.fetchall()
            self._csvout().export(cols, rows)


class KillSessions(DBAccessTask):
    reason = StrParam()
    sql = '''
    begin
      sys.kill_own_sessions(:reason);
    end;
    '''

    def complete(self) -> bool:
        return False

    def run(self) -> None:
        with self.connection() as work:
            work.execute(self.sql, params=dict(reason=self.reason))


class AlterStarNoLogging(DBAccessTask):
    sql = '''
    alter table TABLE nologging
    '''
    tables = ['patient_mapping',
              'encounter_mapping',
              'patient_dimension',
              'visit_dimension',
              'observation_fact']

    def complete(self) -> bool:
        return False

    def run(self) -> None:
        with self.connection() as work:
            for table in self.tables:
                work.execute(self.sql.replace('TABLE', table))


class LoadOntology(DBAccessTask):
    name = StrParam()
    prototype = StrParam()
    filename = StrParam()
    delimiter = StrParam(default=',')
    extra_cols = StrParam(default='')
    rowcount = IntParam(default=1)
    skip = IntParam(default=None)

    def requires(self) -> luigi.Task:
        return SaveOntology(filename=self.filename)

    def complete(self) -> bool:
        db = self._dbtarget().engine
        table = Table(self.name, sqla.MetaData(),  # type: ignore  # sqla
                      Column('c_fullname', sqla.String))
        if not table.exists(bind=db):
            log.info('no such table: %s', self.name)
            return False
        with self.connection() as q:
            actual = q.scalar(sqla.select([func.count(table.c.c_fullname)]))
            log.info('table %s has %d rows', self.name, actual)
            return actual >= self.rowcount  # type: ignore  # sqla

    def run(self) -> None:
        with self.input().dictreader(delimiter=self.delimiter,
                                     lowercase_fieldnames=True) as data:
            ont_load.load(self._dbtarget().engine, data,
                          self.name, self.prototype,
                          skip=self.skip,
                          extra_colnames=self.extra_cols.split(','))


class SaveOntology(luigi.Task):
    filename = StrParam()

    def output(self) -> luigi.Target:
        return CSVTarget(path=self.filename)

    def requires(self) -> List[luigi.Target]:
        return []
