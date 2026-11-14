"""Connector registry: kind → class, plus the catalogue the wizard shows."""

from __future__ import annotations

from app.connectors.base import DataConnector
from app.connectors.network_sql import MySQLConnector, PostgreSQLConnector, SQLServerConnector
from app.connectors.rest_conn import RESTConnector
from app.connectors.sqlite_conn import SQLiteConnector
from app.utils.exceptions import ConnectorError

CONNECTORS: dict[str, type[DataConnector]] = {
    "postgresql": PostgreSQLConnector,
    "mysql": MySQLConnector,
    "sqlserver": SQLServerConnector,
    "sqlite": SQLiteConnector,
    "rest": RESTConnector,
}

# Fields accepted per kind. `secret` fields go to the encrypted store, everything
# else to the plain config. Anything not listed is dropped, so a client cannot smuggle
# extra settings (or a secret into the plain config) through the API.
FIELDS: dict[str, dict[str, list[str]]] = {
    "postgresql": {"config": ["host", "port", "database", "username", "ssl_mode"], "secret": ["password"]},
    "mysql": {"config": ["host", "port", "database", "username", "ssl_mode"], "secret": ["password"]},
    "sqlserver": {"config": ["host", "port", "database", "username", "ssl_mode"], "secret": ["password"]},
    "sqlite": {"config": ["stored_file", "original_name"], "secret": []},
    "rest": {
        "config": ["base_url", "endpoint", "method", "auth_type", "auth_name", "username", "headers", "params",
                   "body", "records_path", "fields", "pagination", "read_only_confirmed"],
        "secret": ["token", "api_key", "password"],
    },
}


def get_class(kind: str) -> type[DataConnector]:
    try:
        return CONNECTORS[kind]
    except KeyError as exc:
        raise ConnectorError("That type of data source is not supported.", f"unknown kind {kind!r}") from exc


def build(kind: str, config: dict, secrets: dict | None = None) -> DataConnector:
    return get_class(kind)(config, secrets)


def split_fields(kind: str, payload: dict) -> tuple[dict, dict]:
    spec = FIELDS.get(kind)
    if spec is None:
        raise ConnectorError("That type of data source is not supported.")
    config = {k: payload[k] for k in spec["config"] if k in payload and payload[k] not in (None, "")}
    secrets = {k: payload[k] for k in spec["secret"] if k in payload and payload[k] not in (None, "")}
    return config, secrets


def catalogue() -> list[dict]:
    """What the connection wizard offers (labels and plain-language guidance)."""
    return [
        {"kind": "file", "label": "Excel or CSV file", "group": "Files", "help": "Upload a spreadsheet. AInsight reads it and keeps a copy for analysis."},
        {"kind": "postgresql", "label": "PostgreSQL", "group": "Databases", "default_port": 5432,
         "help": "Use a dedicated read-only database account whenever possible."},
        {"kind": "mysql", "label": "MySQL / MariaDB", "group": "Databases", "default_port": 3306,
         "help": "Use a dedicated read-only database account whenever possible."},
        {"kind": "sqlserver", "label": "Microsoft SQL Server", "group": "Databases", "default_port": 1433,
         "help": "Use a dedicated read-only account (db_datareader)."},
        {"kind": "sqlite", "label": "SQLite file", "group": "Databases", "help": "Upload a .sqlite / .db file."},
        {"kind": "rest", "label": "REST API", "group": "APIs", "help": "Connect a JSON API that returns your business data."},
    ]
