"""Connector abstraction.

Every business data source — a PostgreSQL database, an uploaded SQLite file, a REST
API — is reached through a `DataConnector`. The analysis agents never import a
database driver; they ask a connector to list tables, describe a table or run a
validated read-only query, so supporting another system means adding one class.

Credentials arrive as a separate `secrets` mapping that only the connector sees.
They are never part of `config`, never in a prompt and never in an API response.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.security.secrets import redact
from app.utils.exceptions import ConnectorError

DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class TableRef:
    schema: str
    name: str
    kind: str = "table"  # table | view | endpoint

    @property
    def ref_id(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


@dataclass
class TableInfo:
    ref: TableRef
    columns: list[ColumnInfo]
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)
    row_estimate: int | None = None


@dataclass
class QueryOutput:
    columns: list[str]
    rows: list[dict]
    truncated: bool = False
    elapsed_ms: float = 0.0


def clean_value(value: Any) -> Any:
    """Make a driver value JSON-friendly without changing its meaning."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def semantic_from_sql_type(sql_type: str, name: str, primary_key: bool = False, foreign_key: bool = False) -> str:
    """Classify a column from its declared SQL type (no data needed)."""
    t = (sql_type or "").lower()
    n = name.lower()
    id_like = bool(re.search(r"(^|_)(id|uuid|guid|key|code)$", n)) or n == "id"
    if primary_key or foreign_key or (id_like and any(x in t for x in ("int", "uuid", "char", "text", "uniqueidentifier"))):
        return "identifier"
    if any(x in t for x in ("bool", "bit")) and "bigint" not in t:
        return "boolean"
    if any(x in t for x in ("timestamp", "datetime", "date", "time")) and "interval" not in t:
        return "datetime"
    if any(x in t for x in ("int", "numeric", "decimal", "float", "double", "real", "money", "number", "serial")):
        return "numeric"
    return "categorical"


class DataConnector(ABC):
    """Interface every source implements."""

    kind = ""
    dialect = ""
    supports_sql = True
    default_schema = ""
    # True when the *session* itself refuses writes (belt and braces on top of the
    # SQL validator). Shown to the user so they know what protects their data.
    enforces_read_only_session = False

    def __init__(self, config: dict, secrets: dict | None = None) -> None:
        self.config = dict(config or {})
        self.secrets = dict(secrets or {})

    # -- helpers ---------------------------------------------------------
    def scrub(self, text: Any) -> str:
        """Remove secret values from anything that may be shown or logged."""
        return redact(str(text), self.secrets.values())

    def fail(self, user_message: str, exc: Exception | None = None) -> ConnectorError:
        detail = self.scrub(f"{type(exc).__name__}: {exc}") if exc else user_message
        return ConnectorError(user_message, detail)

    # -- contract --------------------------------------------------------
    @abstractmethod
    def test(self) -> dict:
        """Open a connection and report {ok, message, warnings, server_version}."""

    @abstractmethod
    def list_tables(self) -> list[TableRef]: ...

    @abstractmethod
    def describe_table(self, ref: TableRef) -> TableInfo: ...

    def run_query(self, sql: str, max_rows: int, timeout_s: int) -> QueryOutput:  # pragma: no cover
        raise ConnectorError("This kind of data source cannot run SQL.")

    def quote(self, identifier: str) -> str:
        return '"' + str(identifier).replace('"', '""') + '"'

    def table_sql(self, ref: TableRef) -> str:
        if not ref.schema or ref.schema == self.default_schema:
            return self.quote(ref.name)
        return f"{self.quote(ref.schema)}.{self.quote(ref.name)}"

    def describe(self) -> dict:
        """Non-secret description for the UI."""
        return {"kind": self.kind, "dialect": self.dialect, "read_only_session": self.enforces_read_only_session}
