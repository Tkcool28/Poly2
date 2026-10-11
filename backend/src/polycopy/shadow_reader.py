"""Dedicated idle/autocommit native asyncpg reader for the exact reviewed SQL.

No extra DB driver. All calls run within SQLAlchemy's async greenlet boundary;
source sessions/connections are never reused. Placeholder translation changes
execution only, not the retained reviewed predicate/query digest.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import create_async_engine

from polycopy.shadow_capture import load_protocol


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=()):
        parameters = list(params)
        if "t.ingested_at >= %s" in sql:
            # asyncpg's native timestamptz codec requires datetime, not wire strings.
            parameters[5:7] = [datetime.fromisoformat(x)
                                for x in parameters[5:7]]
        for i in range(sql.count("%s")):
            sql = sql.replace("%s", f"${i + 1}", 1)
        result = self.connection.exec_driver_sql(sql, tuple(parameters))
        self.rows = [tuple(row) for row in result] if result.returns_rows else []
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.rows = []


class ReaderConnection:
    def __init__(self, connection):
        self.connection = connection
        dbapi = connection.connection.dbapi_connection
        self.autocommit = dbapi.autocommit is True
        native = connection.connection.driver_connection
        if native.is_in_transaction():
            raise ValueError("dedicated native reader is not idle")
        from types import SimpleNamespace
        self.info = SimpleNamespace(transaction_status=0)

    def cursor(self):
        return Cursor(self.connection)

    def rollback(self):
        # BEGIN is explicit server SQL under AUTOCOMMIT; end the server snapshot.
        self.connection.exec_driver_sql("ROLLBACK")
        self.connection.rollback()


async def read_operation(database_url, operation):
    """Run recover/freeze/drain using a fresh dedicated default-read-only connection."""
    engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT",
                                 connect_args={"server_settings": {
                                     "default_transaction_read_only": "on",
                                     "timezone": "UTC"}})
    try:
        async with engine.connect() as connection:
            def run(sync):
                reader = ReaderConnection(sync)
                adapter = load_protocol("poly2-comparison-checkpoint.py").SQLAdapter(
                    reader, "postgres", checkpoint=True)
                return operation(reader, adapter)
            return await connection.run_sync(run)
    finally:
        await engine.dispose()
