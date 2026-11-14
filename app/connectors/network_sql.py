"""PostgreSQL, MySQL and SQL Server connectors.

All three open a TCP connection to a host the user supplied, so every connect first
passes the network guard (no loopback / private / metadata addresses unless the
operator allows it). Drivers are imported lazily: a server without the driver can
still run everything else and tells the user precisely what to install.

IMPORTANT — these connectors are implemented against the documented DB-API of each
driver and unit-tested with a fake driver, but they have NOT been exercised against
real PostgreSQL / MySQL / SQL Server servers in this repository's CI. Treat the first
real connection as an integration test (README, "Manual steps").
"""

from __future__ import annotations

import importlib
import ssl as ssl_lib

from config import Config
from app.connectors.sql_base import SQLConnector
from app.security.network_guard import pick_address, safe_create_connection
from app.utils.exceptions import ConnectorError


def _driver(module: str, package: str, label: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ConnectorError(
            f"{label} support is not installed on this server.",
            f"missing python package '{package}' (pip install {package})",
        ) from exc


class _NetworkSQL(SQLConnector):
    default_port = 0

    def _host_port(self) -> tuple[str, int, str]:
        """Validated (host, port, pinned_ip). Connect to the pinned IP, not to a fresh DNS answer."""
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port") or self.default_port)
        if not 1 <= port <= 65535:
            raise ConnectorError("The port number is not valid.")
        if any(ch in host for ch in "/@ \\"):
            raise ConnectorError("The host name is not valid. Enter only the server name, e.g. db.example.com.")
        return host, port, pick_address(host, port)

    def _require(self, *keys: str) -> None:
        missing = [k for k in keys if not str(self.config.get(k, "")).strip()]
        if missing:
            raise ConnectorError(f"Fill in: {', '.join(missing)}.")


class PostgreSQLConnector(_NetworkSQL):
    kind = "postgresql"
    dialect = "postgresql"
    default_schema = "public"
    default_port = 5432
    enforces_read_only_session = True

    def _open(self, timeout_s: int):
        psycopg = _driver("psycopg", "psycopg[binary]", "PostgreSQL")
        self._require("host", "database", "username")
        host, port, ip = self._host_port()
        mode = self.config.get("ssl_mode", "require")
        if mode not in {"require", "verify-full", "verify-ca", "prefer", "disable"}:
            mode = "require"
        conn = psycopg.connect(
            host=host, hostaddr=ip, port=port, dbname=self.config["database"], user=self.config["username"],
            password=self.secrets.get("password", ""), connect_timeout=Config.CONNECTOR_CONNECT_TIMEOUT,
            sslmode=mode,
            options=f"-c default_transaction_read_only=on -c statement_timeout={int(timeout_s) * 1000}",
        )
        conn.read_only = True
        return conn

    def _version_sql(self): return "SELECT version()"
    def _tables_sql(self):
        return ("SELECT table_schema, table_name, table_type FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_schema NOT LIKE 'pg_toast%' "
                "ORDER BY table_schema, table_name")
    def _columns_sql(self):
        return ("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position")
    def _pk_sql(self):
        return ("SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_schema = kcu.table_schema WHERE tc.constraint_type = 'PRIMARY KEY' "
                "AND tc.table_schema = %s AND tc.table_name = %s ORDER BY kcu.ordinal_position")
    def _fk_sql(self):
        return ("SELECT kcu.column_name, ccu.table_name, ccu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name "
                "AND ccu.constraint_schema = tc.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s AND tc.table_name = %s")

    def _write_privilege_warnings(self, conn):
        _, rows = self._rows(
            conn,
            "SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
            "(SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = current_user "
            "AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE'))",
        )
        superuser, writes = rows[0]
        out = []
        if superuser:
            out.append("This account is a database superuser. Create a read-only account for AInsight instead.")
        elif writes:
            out.append("This account has write permissions on some tables. A read-only account is strongly recommended.")
        return out


class MySQLConnector(_NetworkSQL):
    kind = "mysql"
    dialect = "mysql"
    default_port = 3306
    enforces_read_only_session = True

    @property
    def default_schema(self):  # type: ignore[override]
        return str(self.config.get("database", ""))

    def _open(self, timeout_s: int):
        pymysql = _driver("pymysql", "PyMySQL", "MySQL")
        self._require("host", "database", "username")
        host, port, _ip = self._host_port()
        kwargs = dict(
            host=host, port=port, user=self.config["username"], password=self.secrets.get("password", ""),
            database=self.config["database"], connect_timeout=Config.CONNECTOR_CONNECT_TIMEOUT,
            read_timeout=int(timeout_s), charset="utf8mb4",
        )
        if self.config.get("ssl_mode", "require") != "disable":
            kwargs["ssl"] = ssl_lib.create_default_context()
        conn = pymysql.connect(defer_connect=True, **kwargs)
        # Hand the driver a socket to the validated address; TLS still verifies the real host name.
        conn.connect(safe_create_connection((host, port), Config.CONNECTOR_CONNECT_TIMEOUT))
        cur = conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_s) * 1000}")
        cur.close()
        return conn

    def quote(self, identifier: str) -> str:
        return "`" + str(identifier).replace("`", "``") + "`"

    def _version_sql(self): return "SELECT VERSION()"
    def _tables_sql(self):
        return ("SELECT table_schema, table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name")
    def _columns_sql(self):
        return ("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position")
    def _pk_sql(self):
        return ("SELECT column_name FROM information_schema.key_column_usage WHERE constraint_name = 'PRIMARY' "
                "AND table_schema = %s AND table_name = %s ORDER BY ordinal_position")
    def _fk_sql(self):
        return ("SELECT column_name, referenced_table_name, referenced_column_name FROM information_schema.key_column_usage "
                "WHERE referenced_table_name IS NOT NULL AND table_schema = %s AND table_name = %s")

    def _write_privilege_warnings(self, conn):
        _, rows = self._rows(conn, "SHOW GRANTS FOR CURRENT_USER()")
        text = " ".join(str(r[0]).upper() for r in rows)
        risky = ("ALL PRIVILEGES", " INSERT", " UPDATE", " DELETE", " DROP", " ALTER", " CREATE")
        if any(k in text for k in risky):
            return ["This account has write or admin permissions. A SELECT-only account is strongly recommended."]
        return []


class SQLServerConnector(_NetworkSQL):
    kind = "sqlserver"
    dialect = "sqlserver"
    default_schema = "dbo"
    default_port = 1433
    # SQL Server has no session-level read-only switch AInsight can turn on.
    enforces_read_only_session = False

    def _open(self, timeout_s: int):
        self._require("host", "database", "username")
        host, port, ip = self._host_port()
        try:
            pyodbc = importlib.import_module("pyodbc")
            drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        except ImportError:
            pyodbc, drivers = None, []
        if pyodbc and drivers:
            self.paramstyle = "?"
            encrypt = "no" if self.config.get("ssl_mode") == "disable" else "yes"
            conn = pyodbc.connect(
                f"DRIVER={{{sorted(drivers)[-1]}}};SERVER={ip},{port};DATABASE={self.config['database']};"
                f"UID={self.config['username']};PWD={self.secrets.get('password', '')};Encrypt={encrypt};"
                f"ApplicationIntent=ReadOnly;TrustServerCertificate=no;HostNameInCertificate={host}",
                timeout=Config.CONNECTOR_CONNECT_TIMEOUT,
            )
            conn.timeout = int(timeout_s)
            return conn
        pymssql = _driver("pymssql", "pymssql", "SQL Server")
        self.paramstyle = "%s"
        return pymssql.connect(
            server=ip, port=str(port), user=self.config["username"], password=self.secrets.get("password", ""),
            database=self.config["database"], login_timeout=Config.CONNECTOR_CONNECT_TIMEOUT, timeout=int(timeout_s),
        )

    def _version_sql(self): return "SELECT @@VERSION"
    def _tables_sql(self):
        return ("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA NOT IN ('sys','INFORMATION_SCHEMA') ORDER BY TABLE_SCHEMA, TABLE_NAME")
    def _columns_sql(self):
        return ("SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION")
    def _pk_sql(self):
        return ("SELECT kcu.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                "AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
                "AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s ORDER BY kcu.ORDINAL_POSITION")
    def _fk_sql(self):
        return ("SELECT cp.name, ro.name, rc.name FROM sys.foreign_key_columns fkc "
                "JOIN sys.objects po ON po.object_id = fkc.parent_object_id "
                "JOIN sys.schemas ps ON ps.schema_id = po.schema_id "
                "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
                "JOIN sys.objects ro ON ro.object_id = fkc.referenced_object_id "
                "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id "
                "WHERE ps.name = %s AND po.name = %s")

    def _sample_sql(self, table_sql: str, n: int) -> str:
        return f"SELECT TOP {int(n)} * FROM {table_sql}"

    def _write_privilege_warnings(self, conn):
        _, rows = self._rows(conn, "SELECT IS_SRVROLEMEMBER('sysadmin'), IS_MEMBER('db_owner'), IS_MEMBER('db_datawriter')")
        sysadmin, owner, writer = (rows[0] + (None, None, None))[:3]
        if sysadmin or owner or writer:
            return ["This account can modify data or administer the database. Use a read-only (db_datareader) account."]
        return []
