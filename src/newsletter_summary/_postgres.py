"""PostgreSQL transport for the application's existing parameterized SQL.

DATABASE_URL_FILE takes precedence; a configured but unavailable server never
falls back to SQLite. Production schemas are provisioned by homelab-backups.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def configured() -> bool:
    return "DATABASE_URL_FILE" in os.environ or "DATABASE_URL" in os.environ


class Record(dict):
    def __getitem__(self, key: Any) -> Any:
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor: Any) -> Any:
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values, strict=True))


def bind_sql(query: str) -> str:
    # App SQL uses qmarks. Ignore quoted question marks, and escape percent
    # literals for psycopg's binding protocol. Values are always bound separately.
    result = []
    quote = None
    i = 0
    while i < len(query):
        char = query[i]
        if char in ("'", '"'):
            if quote == char and i + 1 < len(query) and query[i + 1] == char:
                result.append(char * 2)
                i += 2
                continue
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        result.append("%s" if char == "?" and quote is None else "%%" if char == "%" else char)
        i += 1
    return "".join(result)


class Connection:
    """Keep SQLite's commit/rollback context semantics without closing on exit."""
    def __init__(self, raw: Any):
        self.raw = raw

    def execute(self, query: str, parameters: Any = None) -> Any:
        if parameters is None:
            return self.raw.execute(query)
        # Existing SQLite INTEGER flags are intentionally BIGINT in PostgreSQL.
        values = tuple(int(v) if isinstance(v, bool) else v for v in parameters)
        return self.raw.execute(bind_sql(query), values)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


def connect(schema: str, *, readonly: bool = False) -> Any:
    import psycopg

    if not schema.replace("_", "").isalnum():
        raise ValueError("Invalid database schema")
    file = os.environ.get("DATABASE_URL_FILE")
    dsn = Path(file).read_text().strip() if file is not None else os.environ["DATABASE_URL"]
    if not dsn:
        raise ValueError("Configured database connection is empty")
    options = f"-c search_path={schema},pg_catalog -c statement_timeout=120000 -c lock_timeout=10000"
    if readonly:
        options += " -c default_transaction_read_only=on"
    raw = psycopg.connect(dsn, connect_timeout=10, application_name=schema, options=options, row_factory=row_factory)
    try:
        row = raw.execute("SELECT version FROM _schema_version").fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("Unsupported PostgreSQL application schema version")
        raw.commit()
    except Exception:
        raw.close()
        raise
    return Connection(raw)
