import os
import random
import re
import string
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
)
from typing import (
    Any,
    List,
    Optional,
    Union,
)

import polars as pl
from keepitsql import (
    CopyDDl,
    FromDataframe,
    get_table_column_info,
)
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.engine import (
    Connection,
    Engine,
)
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from keepdataflow._database_engine import DatabaseEngine
from keepdataflow.core.create_partition import partition_dataframe


def table_name_formattter(table_name: str, schema_name: Optional[str]) -> str:
    """
    Format the table name with the schema if provided.

    Args:
        table_name (str): The name of the table.
        schema_name (Optional[str]): The name of the schema.

    Returns:
        str: The formatted table name.
    """
    if schema_name:
        return f"{schema_name}.{table_name}"
    else:
        return table_name


def non_chainable(method: Any) -> Any:
    """
    Decorator to make a method non-chainable.

    Args:
        method (Any): The method to decorate.

    Returns:
        Any: The decorated method.
    """

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Check if the method is being called within a chain
        if hasattr(self, '_in_chain') and self._in_chain:
            raise AttributeError(f"The method '{method.__name__}' cannot be used in a chain")
        return method(self, *args, **kwargs)

    return wrapper


class DatabaseOperations:
    """
    A class that manages database operations using DatabaseEngine.

    Attributes:
        database_engine (DatabaseEngine): The database engine instance.
        dataframe (Optional[pl.DataFrame]): The DataFrame to hold loaded data.
    """

    def __init__(self, database_engine: DatabaseEngine) -> None:
        """
        Initialize the DatabaseOperations with a DatabaseEngine.

        Args:
            database_engine (DatabaseEngine): The database engine instance.
        """
        self.database_engine: DatabaseEngine = database_engine
        self.dataframe: Optional[pl.DataFrame] = None

    def generate_temp_table_name(self, table_name: str) -> str:
        """
        Generate a temporary table name.

        Args:
            table_name (str): The base name of the table.

        Returns:
            str: The generated temporary table name.
        """
        uid = "".join(random.choices(string.ascii_lowercase, k=4))
        temp_name = f"_source_{table_name}_{uid}"
        return temp_name

    def create_temp_table(
        self,
        session: Any,
        table_name: str,
        source_schema: Optional[str] = None,
        new_table_name: Optional[str] = None,
        target_schema: Optional[str] = None,
    ) -> str:
        """
        Create a temporary table in the database.
        """
        metadata = MetaData()
        with session:
            inspector = inspect(session.bind)
            columns_info = inspector.get_columns(table_name, schema=source_schema)
            pk_info = inspector.get_pk_constraint(table_name, schema=source_schema)

            # Define the table object with columns and primary key
            columns = []
            for col in columns_info:
                col_type = col['type']
                col_name = col['name']
                primary_key = col_name in pk_info['constrained_columns']
                columns.append(Column(col_name, col_type, primary_key=primary_key))

            # Use new table name if provided, otherwise use the original table name
            final_table_name = new_table_name if new_table_name else table_name

            # Create the Table object with target schema if provided
            table = Table(final_table_name, metadata, *columns, schema=target_schema)

            create_temp_table_headers = {
                "oracle": "CREATE GLOBAL TEMPORARY TABLE",
                "mssql": "CREATE TABLE ##",  # Global temporary table# Local temporary table
                "db2": "",  # DB2 specific statement not provided
                "postgresql": "CREATE TEMP TABLE ",
                "mysql": "CREATE TEMPORARY TABLE ",
                "sqlite": "CREATE TEMP TABLE ",
                "teradata": "",  # Teradata specific statement not provided
                "hana": "",  # SAP HANA specific statement not provided
                "snowflake": "",  # Snowflake specific statement not provided
                "redshift": "CREATE TEMP TABLE ",
                "bigquery": "",  # BigQuery specific statement not provided
            }
            # Generate the DDL
            dbms_dialect = session.bind.dialect.name
            temp_table_header = create_temp_table_headers.get(dbms_dialect)

            ddl = str(CreateTable(table).compile(session.bind)).replace('"', '')
            temp_ddl = (
                ddl.replace("CREATE TABLE ", temp_table_header).replace(']', '').replace('[', '')
            )  # spacing is important for sql server
            pattern = r'IDENTITY(\(\d+,\d+\))?'
            temp_ddl = re.sub(pattern, '', temp_ddl)

            return str(temp_ddl)

    def insert_data_partition(
        self,
        partition: pl.DataFrame,
        session: Any,
        target_table: str,
        target_schema: Optional[str] = None,
        source_table: Optional[str] = None,
    ) -> None:
        try:
            params_list = [dict(row) for row in partition.iter_rows(named=True)]
            insert_conn = FromDataframe(dataframe=partition)

            tbl_name = table_name_formattter(target_table, target_schema)
            insert_sql = insert_conn.insert(table_name=tbl_name, source_table=source_table)

            session.execute(text(insert_sql), params_list)

        except Exception as e:
            print(f"An error occurred: {e}")

    def upsert_data_partition(
        self,
        session: Any,
        table_name: str,
        dbms: str,
        match_condition: List[str],
        constraint_columns: List[str],
        source_table: str,
        **kwargs: Any,
    ) -> None:
        merge_conn = FromDataframe(dataframe=self.dataframe)

        merge_sql = merge_conn.dbms_merge_generator(
            table_name=table_name,
            match_condition=match_condition,
            dbms=dbms,
            constraint_columns=constraint_columns,
            source_table_name=source_table,
        )

        session.execute(text(merge_sql))
        session.commit()

    def truncate_table(self, target_table: str, target_schema: Optional[str] = None) -> None:
        with self.database_engine as session:
            truncate_text = text(f'DELETE FROM {target_table}')
            session.execute(truncate_text)
            session.commit()

    def load_dataframe(self, dataframe: pl.DataFrame) -> 'DatabaseOperations':
        self.dataframe = dataframe
        return self

    def copy_source_db(
        self,
        source_db_url: str,
        source_table_name: Optional[str] = None,
        source_query: Optional[Union[str, bytes]] = None,
        source_schema_name: Optional[str] = None,
        chunk_size: Optional[int] = None,
        **kwargs,
    ) -> 'DatabaseOperations':
        """
        Copy data from a source database to a DataFrame.

        Parameters:
        source_db_url (str): The URL of the source database.
        source_table_name (Optional[str]): The name of the source table. Default is None.
        source_query (Optional[Union[str, bytes]]): The SQL query or the path to the SQL query file. Default is None.
        chunk_size (Optional[int]): The chunk size for partitioning the data. Default is None.
        **kwargs: Additional keyword arguments.

        Returns:
        DatabaseOperations: The updated instance of the DatabaseOperations class.

        Raises:
        ValueError: If neither source_table_name nor source_query is provided.
        """
        source_engine = create_engine(source_db_url)
        # Example connection string for a PostgreSQL database

        # Render the URL as a string with the password visible
        url_with_password = source_engine.url.render_as_string(hide_password=False)
        # print(url_with_password)

        if not any([source_table_name, source_query]):
            raise ValueError("Either source_table_name or source_query must be provided.")

        def get_sql_query(sql_input: Union[str, bytes]) -> str:
            """
            Get the SQL query from the input.

            Parameters:
            sql_input (Union[str, bytes]): The SQL query or the path to the SQL query file.

            Returns:
            str: The SQL query.
            """
            if os.path.isfile(sql_input) and sql_input.endswith('.sql'):
                with open(sql_input, 'r') as file:
                    sql_query = file.read()
            else:
                sql_query = sql_input

            return sql_query

        table_name = table_name_formattter(source_table_name, source_schema_name)
        query = f"SELECT * FROM {table_name}"
        sql_query = get_sql_query(source_query) if source_query is not None else query

        # with source_engine.connect() as connection:
        self.dataframe = pl.read_database_uri(
            sql_query, url_with_password, engine="connectorx", partition_range=chunk_size
        )

        return self

    def db_insert(
        self,
        target_table: str,
        target_schema: Optional[str] = None,
        session: Optional[Session] = None,
        partition_by: Optional[Union[str, List[str]]] = None,
        full_refresh: str = 'N',
        chunk_size: int = 5000,
        **kwargs,
    ) -> None:
        """
        Insert data from the DataFrame into the target table.

        Args:
            target_table (str): The name of the target table.
            target_schema (Optional[str]): The schema of the target table.
            session (Optional[Session]): The database session.
            partition_by (Optional[Union[str, List[str]]]): The column(s) to partition by.
            full_refresh (str): Whether to perform a full refresh. Default is 'N'.
            chunk_size (int): The size of each chunk. Default is 5000.
        """
        if self.dataframe is None:
            raise ValueError("No DataFrame loaded.")

        if isinstance(partition_by, list):
            partition_by = partition_by[0]

        partition = partition_dataframe(self.dataframe, chunk_size=chunk_size, column_name=partition_by)

        if full_refresh == 'Y':
            self.truncate_table(table_name_formattter(target_table, target_schema))

        def insert_partition_with_session(part: pl.DataFrame) -> None:
            try:
                if session is not None:
                    db_session = session
                    self.insert_data_partition(part, db_session, target_table, target_schema)
                elif session is None:
                    with self.database_engine as db_session:
                        self.insert_data_partition(part, db_session, target_table, target_schema)
            except Exception as e:
                print(f"An error occurred in insert_partition_with_session: {e}")

        with ThreadPoolExecutor() as executor:
            executor.map(insert_partition_with_session, partition)

    def db_merge(
        self,
        target_table: str,
        target_schema: Optional[str] = None,
        match_condition: Optional[List[str]] = None,
        constraint_columns: Optional[List[str]] = None,
        partition_by: Optional[str | list] = None,
        chunk_size: int = 5000,
        **kwargs,
    ) -> None:
        with self.database_engine as session:
            auto_columns, primary_key_list = get_table_column_info(session, target_table, target_schema)
            partition = partition_dataframe(self.dataframe, chunk_size=chunk_size, column_name=partition_by)
            constraint_list = auto_columns if constraint_columns is None else constraint_columns
            match_list = primary_key_list if match_condition is None else match_condition
            dbms = self.database_engine.get_dbms_dialect()

            # Step 1: Create temp table
            gen_temp_table_name = self.generate_temp_table_name(target_table)
            temp_table = self.create_temp_table(
                session, target_table, target_schema, new_table_name=gen_temp_table_name
            )

            session.execute(text(temp_table))

            # format for mssql
            gen_temp_table_name = f'##{gen_temp_table_name}' if dbms == 'mssql' else gen_temp_table_name
            # Step 2: Insert into temp table
            print(f"Begin {gen_temp_table_name} insert")
            self.db_insert(
                session=session, target_table=gen_temp_table_name, partition_by=partition_by, chunk_size=chunk_size
            )
            # Step 3: Load Temp table into Target Table
            params = {
                'table_name': table_name_formattter(target_table, target_schema),
                'partition': partition,  # replace with partiont parameter
                'match_condition': match_list,
                'constraint_columns': constraint_list,
                'dbms': dbms,
                'source_table': gen_temp_table_name,
            }
            self.upsert_data_partition(session=session, **params)

    def db_merge_with_polars(
        self,
        target_table: str,
        target_schema: Optional[str] = None,
        match_condition: Optional[List[str]] = None,
        constraint_columns: Optional[List[str]] = None,
        partition_by: Optional[str | list] = None,
        chunk_size: int = 5000,
        **kwargs,
    ) -> None:
        with self.database_engine as session:
            auto_columns, primary_key_list = get_table_column_info(session, target_table, target_schema)
            partition = partition_dataframe(self.dataframe, chunk_size=chunk_size, column_name=partition_by)
            constraint_list = auto_columns if constraint_columns is None else constraint_columns
            match_list = primary_key_list if match_condition is None else match_condition
            dbms = self.database_engine.get_dbms_dialect()

            # Step 1: Create temp table
            gen_temp_table_name = self.generate_temp_table_name(target_table)
            temp_table = self.create_temp_table(
                session, target_table, target_schema, new_table_name=gen_temp_table_name
            )

            session.execute(text(temp_table))

            # format for mssql
            gen_temp_table_name = f'##{gen_temp_table_name}' if dbms == 'mssql' else gen_temp_table_name
            # Step 2: Insert into temp table
            print(f"Begin {gen_temp_table_name} insert")
            for p in partition:
                p.write_database(table_name=gen_temp_table_name, connection=session.bind, if_table_exists="append")

            # self.db_insert(
            #     session=session, target_table=gen_temp_table_name, partition_by=partition_by, chunk_size=chunk_size
            # )

            # Step 3: Load Temp table into Target Table
            params = {
                'table_name': table_name_formattter(target_table, target_schema),
                'partition': partition,  # replace with partiont parameter
                'match_condition': match_list,
                'constraint_columns': constraint_list,
                'dbms': dbms,
                'source_table': gen_temp_table_name,
            }
            self.upsert_data_partition(session=session, **params)
