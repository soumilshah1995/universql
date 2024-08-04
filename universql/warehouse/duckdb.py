import datetime
import logging
import os
import time
import typing
from typing import List, Optional

import duckdb
import pyarrow
import pyarrow as pa
import sqlglot
from fakesnow.fakes import FakeSnowflakeCursor, FakeSnowflakeConnection
from pyarrow import Table
from pyarrow.lib import ChunkedArray
from snowflake.connector import DatabaseError
from sqlglot import ParseError
from sqlglot.optimizer.simplify import simplify

from universql.catalog import get_catalog
from universql.catalog.snow.show_iceberg_tables import cloud_logger
from universql.lake.cloud import s3, gcs
from universql.util import get_columns_for_duckdb, SnowflakeError, Compute, Catalog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("🐥")

queries_that_doesnt_need_warehouse = ["show"]


class UniverSQLSession:
    def __init__(self, context, token, credentials: dict, session_parameters: dict):
        self.context = context
        self.credentials = credentials
        self.session_parameters = [{"name": item[0], "value": item[1]} for item in session_parameters.items()]
        self.token = token
        self.catalog = get_catalog(context, self.token,
                                   self.credentials)
        self.duckdb = duckdb.connect(read_only=False, config={
            'max_memory': context.get('max_memory'),
            'temp_directory': os.path.join(context.get('cache_directory'), "duckdb-staging"),
            'max_temp_directory_size': context.get('max_cache_size'),
        })
        self.duckdb.install_extension("iceberg")
        self.duckdb.load_extension("iceberg")
        fake_snowflake_conn = FakeSnowflakeConnection(self.duckdb, "main", "public", False, False)
        fake_snowflake_conn.database_set = True
        fake_snowflake_conn.schema_set = True
        self.duckdb_emulator = FakeSnowflakeCursor(fake_snowflake_conn, self.duckdb)
        self.snowflake = self.catalog.cursor()
        self.register_data_lake(context)
        self.processing = False

    def register_data_lake(self, args: dict):
        self.duckdb.register_filesystem(s3(args.get('cache_directory'), args.get('aws_profile')))
        self.duckdb.register_filesystem(gcs(args.get('cache_directory'), args.get('gcp_project')))

    def sync_duckdb_catalog(self, tables: List[sqlglot.exp.Expression], ast: sqlglot.exp.Expression) -> Optional[
        sqlglot.exp.Expression]:
        try:
            locations = self.catalog.get_table_references(self.duckdb_emulator, tables)
        except DatabaseError as e:
            error_message = (f"[{self.token}] Unable to find location of Iceberg tables. "
                             f"See: https://github.com/buremba/universql#cant-query-native-snowflake-tables. Cause: {e.msg}")
            cloud_logger.warning(error_message)
            return None

        views = [f"CREATE OR REPLACE VIEW main.\"{table.sql()}\" AS SELECT * FROM {expression.sql()};" for
                 table, expression in locations.items()]
        views_sql = "\n".join(views)
        if views:
            self.duckdb.execute(views_sql)
            logger.info(f"[{self.token}] Creating views for Iceberg tables: \n{views_sql}")
        def replace_icebergs_with_duckdb_reference(
                expression: sqlglot.exp.Expression) -> sqlglot.exp.Expression:
            if isinstance(expression, sqlglot.exp.Table):
                if expression.name != "":
                    new_table = sqlglot.exp.to_table(f"main.{sqlglot.exp.parse_identifier(expression.sql())}")
                    return new_table
                    # return locations[expression]
                else:
                    return expression

            return expression

        return ast.transform(replace_icebergs_with_duckdb_reference).transform(fix_snowflake_to_duckdb_types)

    def _do_query(self, raw_query: str) -> (str, List, pyarrow.Table):
        start_time = time.perf_counter()
        compute = self.context.get('compute')
        local_error_message = ""

        try:
            queries = sqlglot.parse(raw_query, read="snowflake")
        except ParseError as e:
            local_error_message = f"Unable to parse query with SQLGlot: {e.args}"
            queries = None

        should_run_locally = compute != Compute.SNOWFLAKE.value
        can_run_locally = queries is not None
        run_snowflake_already = False

        if can_run_locally and should_run_locally:
            for ast in queries:
                if ast.key in queries_that_doesnt_need_warehouse:
                    self.do_snowflake_query(queries, raw_query, start_time, local_error_message)
                    run_snowflake_already = True
                else:
                    tables = list(ast.find_all(sqlglot.exp.Table))
                    transformed_ast = self.sync_duckdb_catalog(tables, simplify(ast))
                    if transformed_ast is None:
                        can_run_locally = False
                        break
                    sql = transformed_ast.sql(dialect="duckdb", pretty=True)
                    planned_duration = time.perf_counter() - start_time
                    timedelta = datetime.timedelta(seconds=planned_duration)

                    logger.info("[%s] Re-written for DuckDB as: (%s)\n%s" % (self.token, timedelta, sql))
                    try:
                        self.duckdb_emulator.execute(sql)
                    # except duckdb.Error as e:
                    except DatabaseError as e:
                        local_error_message = f"DuckDB error: {e.args}"
                        can_run_locally = False
                        break

        catalog = self.context.get('catalog')
        if compute == Compute.LOCAL.value or catalog == Catalog.POLARIS.value:
            raise SnowflakeError(self.token, f"Can't run the query locally, {local_error_message}")

        if can_run_locally and not run_snowflake_already:
            formatting = (self.token, datetime.timedelta(seconds=time.perf_counter() - start_time))
            logger.info(f"[{self.token}] Run locally 🚀 ({formatting})")
            return self.get_duckdb_result()
        else:
            self.do_snowflake_query(queries, raw_query, start_time, local_error_message)
            return self.get_snowflake_result()


    def do_snowflake_query(self, queries, raw_query, start_time, local_error_message):
        try:
            self.snowflake.execute(queries, raw_query)
            formatting = (self.token, datetime.timedelta(seconds=time.perf_counter() - start_time))
            logger.info(f"[{self.token}] Query is done. ({formatting})")
        except SnowflakeError as e:
            final_error = f"{local_error_message}. {e.message}"
            cloud_logger.error(f"[{self.token}] {final_error}")
            raise SnowflakeError(self.token, final_error, e.sql_state)

    def do_query(self, raw_query: str) -> (str, List, pyarrow.Table):
        logger.info("[%s] Executing \n%s" % (self.token, raw_query))
        self.processing = True
        try:
            return self._do_query(raw_query)
        finally:
            self.processing = False

    def close(self):
        self.duckdb_emulator.close()
        self.snowflake.close()

    def get_field_from_duckdb(self, column: list[str], arrow_table: Table, idx: int) -> typing.Tuple[
        Optional[ChunkedArray], pa.Field]:
        (field_name, field_type) = column[0], column[1]
        pa_type = arrow_table.schema[idx].type

        metadata = {}
        transformed_data = None

        if field_type == 'NUMBER':
            metadata["logicalType"] = "FIXED"
            metadata["precision"] = "1"
            metadata["scale"] = "0"
            metadata["physicalType"] = "SB1"
        elif field_type == 'Date':
            pa_type = pa.date32()
            metadata["logicalType"] = "DATE"
        elif field_type == 'Time':
            metadata["logicalType"] = "TIME"
        elif field_type == "BINARY":
            metadata["logicalType"] = "BINARY"
        elif field_type == "TIMESTAMP" or field_type == "DATETIME" or field_type == "TIMESTAMP_LTZ":
            metadata["logicalType"] = "TIMESTAMP_LTZ"
            metadata["precision"] = "0"
            metadata["scale"] = "9"
            metadata["physicalType"] = "SB16"
        elif field_type == "TIMESTAMP_NTZ":
            metadata["logicalType"] = "TIMESTAMP_NTZ"
            metadata["precision"] = "0"
            metadata["scale"] = "9"
            metadata["physicalType"] = "SB16"
        elif field_type == "TIMESTAMP_TZ":
            metadata["logicalType"] = "TIMESTAMP_TZ"
            metadata["precision"] = "0"
            metadata["scale"] = "9"
            metadata["physicalType"] = "SB16"
        elif field_type == "JSON":
            pa_type = pa.utf8()
            metadata["logicalType"] = "OBJECT"
            metadata["charLength"] = "16777216"
            metadata["byteLength"] = "16777216"
            metadata["scale"] = "0"
            metadata["precision"] = "38"
            metadata["finalType"] = "T"
        elif pa_type == pa.bool_():
            metadata["logicalType"] = "BOOLEAN"
        elif field_type == 'list':
            pa_type = pa.utf8()
            arrow_to_project = self.duckdb.from_arrow(arrow_table.select([field_name]))
            metadata["logicalType"] = "ARRAY"
            metadata["charLength"] = "16777216"
            metadata["byteLength"] = "16777216"
            metadata["scale"] = "0"
            metadata["precision"] = "38"
            metadata["finalType"] = "T"
            transformed_data = (arrow_to_project.project(f"to_json({field_name})").arrow())[0]
        elif pa_type == pa.string():
            metadata["logicalType"] = "TEXT"
            metadata["charLength"] = "16777216"
            metadata["byteLength"] = "16777216"
        else:
            raise Exception()

        field = pa.field(field_name, type=pa_type, nullable=True, metadata=metadata)
        if transformed_data is None:
            return arrow_table[idx].cast(field.type), field
        else:
            return transformed_data, field

    def get_duckdb_result(self):
        arrow_table = self.duckdb_emulator._arrow_table
        if arrow_table is None:
            raise SnowflakeError(self.token, "No result returned from DuckDB")
        for idx, column in enumerate(self.duckdb.description):
            array, schema = self.get_field_from_duckdb(column, arrow_table, idx)
            arrow_table = arrow_table.set_column(idx, schema, array)
        return "arrow", get_columns_for_duckdb(arrow_table.schema), arrow_table

    def get_snowflake_result(self):
        arrow = self.snowflake.get_as_table()
        columns = self.snowflake.get_v1_columns()
        return "arrow", columns, arrow


def fix_snowflake_to_duckdb_types(
        expression: sqlglot.exp.Expression) -> sqlglot.exp.Expression:
    if isinstance(expression, sqlglot.exp.DataType):
        if expression.this.value in ["TIMESTAMPLTZ", "TIMESTAMPTZ"]:
            return sqlglot.exp.DataType.build("TIMESTAMPTZ")
        if expression.this.value in ["VARIANT"]:
            return sqlglot.exp.DataType.build("JSON")

    return expression
