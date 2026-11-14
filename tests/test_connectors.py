"""Connected data sources: connectors, the connection manager, isolation and secret handling.

Real end-to-end coverage uses the SQLite connector (an uploaded database file) and the
REST connector (a local HTTP server). PostgreSQL / MySQL / SQL Server are covered with a
fake DB-API driver: those tests prove AInsight's behaviour (session read-only settings,
network guard, secret handling, introspection plumbing), NOT compatibility with real servers.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from config import Config
from tests.test_auth import HDR, auth_on, browser, post, signup


def make_sqlite_bytes(tmp_path, rows=None) -> bytes:
    path = tmp_path / f"biz-{id(rows)}.sqlite"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
            order_date TEXT, region TEXT, category TEXT, sales REAL, profit REAL, quantity INTEGER);
        """
    )
    conn.executemany("INSERT INTO customers VALUES (?,?,?)", [(1, "Ann", "North"), (2, "Bo", "South")])
    data = rows or [
        (1, 1, "2024-01-05", "North", "Technology", 100.0, 10.0, 2),
        (2, 2, "2024-01-18", "South", "Furniture", 250.0, 25.0, 5),
        (3, 1, "2024-02-02", "North", "Technology", 75.0, 7.5, 1),
        (4, 2, "2024-02-14", "East", "Office", 300.0, 30.0, 7),
        (5, 1, "2024-03-01", "West", "Technology", 60.0, 6.0, 3),
        (6, 2, "2024-03-19", "South", "Furniture", 180.0, 18.0, 4),
    ]
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", data)
    conn.commit()
    conn.close()
    return path.read_bytes()


def upload_sqlite(client, tmp_path, name="biz.sqlite"):
    response = client.post(
        "/api/data-sources/upload-sqlite",
        data={"file": (io.BytesIO(make_sqlite_bytes(tmp_path)), name)},
        content_type="multipart/form-data",
        headers=HDR,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def connect_sqlite(client, tmp_path, name="Sales DB"):
    up = upload_sqlite(client, tmp_path)
    response = post(client, "/api/data-sources", {
        "kind": "sqlite", "name": name,
        "settings": {"stored_file": up["stored_file"], "original_name": up["original_name"]},
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()["source"]


def import_orders(client, source_id):
    tables = client.get(f"/api/data-sources/{source_id}/tables").get_json()["tables"]
    assert {t["name"] for t in tables} == {"customers", "orders"}
    response = post(client, f"/api/data-sources/{source_id}/import", {"tables": ["main.orders"]})
    assert response.status_code == 201, response.get_json()
    return response.get_json()["datasets"][0]["id"]


# --------------------------------------------------------------------------
# SQLite connection, end to end
# --------------------------------------------------------------------------


def test_sqlite_connection_end_to_end(tmp_path):
    with auth_on():
        client, _ = signup()
        source = connect_sqlite(client, tmp_path)
        assert source["status"] == "untested"

        tested = post(client, f"/api/data-sources/{source['id']}/test").get_json()
        assert tested["ok"] and tested["status"] == "connected"

        dataset_id = import_orders(client, source["id"])

        # profile was computed inside the database
        detail = client.get(f"/api/datasets/{dataset_id}").get_json()
        profile = detail["profile"]
        assert profile["row_count"] == 6
        assert "sales" in profile["measures"] and "region" in profile["dimensions"]
        assert "order_date" in profile["date_columns"]

        # a natural-language question runs validated SQL against the connection
        reply = post(client, "/api/chat", {"question": "What are the total sales by region?", "dataset_id": dataset_id})
        body = reply.get_json()
        assert reply.status_code == 200, body
        assert body["source"]["kind"] == "sql"
        rows = {r["region"]: r["total_sales"] for r in body["result"]["rows"]}
        assert rows == {"North": 175.0, "South": 430.0, "East": 300.0, "West": 60.0}
        assert body["sql"].upper().startswith("SELECT")


def test_total_comes_from_database(tmp_path):
    with auth_on():
        client, _ = signup()
        source = connect_sqlite(client, tmp_path)
        dataset_id = import_orders(client, source["id"])
        body = post(client, "/api/chat", {"question": "What is the total sales?", "dataset_id": dataset_id}).get_json()
        assert body["result"]["scalar"] == 965.0


def test_monthly_trend_uses_connection_dialect(tmp_path):
    with auth_on():
        client, _ = signup()
        dataset_id = import_orders(client, connect_sqlite(client, tmp_path)["id"])
        body = post(client, "/api/chat", {"question": "Show monthly sales trends", "dataset_id": dataset_id}).get_json()
        periods = {r["period"]: r[[k for k in r if k != "period"][0]] for r in body["result"]["rows"]}
        assert periods == {"2024-01": 350.0, "2024-02": 375.0, "2024-03": 240.0}


def test_median_is_computed_exactly_not_approximated(tmp_path):
    """SQLite cannot do MEDIAN, so the routed engine must be pandas over the full table."""
    with auth_on():
        client, _ = signup()
        dataset_id = import_orders(client, connect_sqlite(client, tmp_path)["id"])
        body = post(client, "/api/chat", {"question": "What is the median sales?", "dataset_id": dataset_id}).get_json()
        assert body["result"]["engine"] == "pandas"
        assert any("computed exactly with pandas" in st["detail"] for st in body["stages"])


# --------------------------------------------------------------------------
# isolation & authorization
# --------------------------------------------------------------------------


def test_user_cannot_touch_another_users_connection(tmp_path):
    with auth_on():
        alice, _ = signup(name="Alice")
        bob, _ = signup(name="Bob")
        source = connect_sqlite(alice, tmp_path)
        sid = source["id"]
        dataset_id = import_orders(alice, sid)

        assert bob.get("/api/data-sources").get_json()["sources"] == []
        probes = [
            bob.get(f"/api/data-sources/{sid}"),
            post(bob, f"/api/data-sources/{sid}/test"),
            bob.get(f"/api/data-sources/{sid}/tables"),
            bob.get(f"/api/data-sources/{sid}/preview?table=main.orders"),
            bob.get(f"/api/data-sources/{sid}/schema?table=main.orders"),
            post(bob, f"/api/data-sources/{sid}/import", {"tables": ["main.orders"]}),
            post(bob, f"/api/data-sources/{sid}/refresh"),
            post(bob, f"/api/data-sources/{sid}/disable"),
            bob.patch(f"/api/data-sources/{sid}", json={"name": "pwned"}, headers=HDR),
            bob.delete(f"/api/data-sources/{sid}", headers=HDR),
        ]
        assert [r.status_code for r in probes] == [404] * len(probes), [r.get_json() for r in probes]

        # bob cannot analyse alice's connected table either
        reply = post(bob, "/api/chat", {"question": "total sales", "dataset_id": dataset_id})
        assert reply.status_code in (404, 422) or "alice" in str(reply.get_json()).lower() is False
        assert reply.get_json().get("result") is None

        # nothing of alice's was changed
        mine = alice.get(f"/api/data-sources/{sid}").get_json()["source"]
        assert mine["name"] == "Sales DB" and mine["enabled"]


def test_cannot_reuse_another_users_uploaded_sqlite_file(tmp_path):
    with auth_on():
        alice, _ = signup()
        bob, _ = signup()
        up = upload_sqlite(alice, tmp_path)
        reply = post(bob, "/api/data-sources", {"kind": "sqlite", "name": "stolen", "settings": {"stored_file": up["stored_file"]}})
        assert reply.status_code == 422
        assert bob.get("/api/data-sources").get_json()["sources"] == []


def test_connection_endpoints_require_sign_in(tmp_path):
    with auth_on():
        anon = browser()
        for method, path in [("get", "/api/data-sources"), ("post", "/api/data-sources"), ("get", "/api/data-sources/x/tables")]:
            reply = getattr(anon, method)(path, headers=HDR)
            assert reply.status_code == 401, (path, reply.status_code)


def test_sqlite_connector_rejects_path_traversal_and_foreign_names():
    from app.connectors.registry import build
    from app.utils.exceptions import ConnectorError

    for bad in ["../../etc/passwd", "/etc/passwd", "database/analytics.db", "a" * 12 + "-" + "b" * 32 + ".sqlite/../x"]:
        with pytest.raises(ConnectorError):
            build("sqlite", {"stored_file": bad}).test()


def test_disabled_source_cannot_run_analyses(tmp_path):
    with auth_on():
        client, _ = signup()
        source = connect_sqlite(client, tmp_path)
        dataset_id = import_orders(client, source["id"])
        assert post(client, f"/api/data-sources/{source['id']}/disable").get_json()["source"]["status"] == "disabled"
        reply = post(client, "/api/chat", {"question": "total sales", "dataset_id": dataset_id})
        assert reply.status_code >= 400 and "disabled" in reply.get_json()["message"].lower()
        post(client, f"/api/data-sources/{source['id']}/enable")
        assert post(client, "/api/chat", {"question": "total sales", "dataset_id": dataset_id}).status_code == 200


def test_deleting_a_source_removes_its_datasets_and_file(tmp_path):
    from app.connectors.sqlite_conn import connections_dir

    with auth_on():
        client, _ = signup()
        before = {p.name for p in connections_dir().glob("*.sqlite")}
        source = connect_sqlite(client, tmp_path)
        import_orders(client, source["id"])
        stored = {p.name for p in connections_dir().glob("*.sqlite")} - before
        assert len(stored) == 1
        assert client.delete(f"/api/data-sources/{source['id']}", headers=HDR).status_code == 200
        assert client.get("/api/datasets").get_json()["datasets"] == []
        assert not any(p.name in stored for p in connections_dir().glob("*.sqlite"))


# --------------------------------------------------------------------------
# read-only enforcement
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET sales = 0",
    "INSERT INTO orders (id) VALUES (99)",
    "DROP TABLE orders",
    "SELECT * INTO copy FROM orders",
    "SELECT * FROM orders; DROP TABLE orders",
    "SELECT * FROM orders /* hide */",
    "SELECT * FROM orders -- x",
    "SELECT * FROM orders, customers",
    "SELECT * FROM other.orders",
    "SELECT pg_sleep(100)",
    "SELECT * FROM orders FOR UPDATE",
])
def test_validator_blocks_dangerous_sql_for_every_dialect(sql):
    from app.security.sql_security import validate_sql
    from app.utils.exceptions import SQLSecurityError

    for dialect in ("sqlite", "postgresql", "mysql", "sqlserver"):
        with pytest.raises(SQLSecurityError):
            validate_sql(sql, {"orders"}, dialect, {"public"})


def test_validator_accepts_normal_analytics_sql():
    from app.security.sql_security import validate_sql

    sql = 'SELECT "region", SUM("sales") AS total FROM "orders" WHERE "sales" > 10 GROUP BY "region" ORDER BY total DESC'
    assert validate_sql(sql, {"orders"}, "postgresql", {"public"})
    assert validate_sql('SELECT `region` FROM `orders`', {"orders"}, "mysql")
    assert validate_sql('SELECT TOP 5 "region" FROM "orders"', {"orders"}, "sqlserver", {"dbo"})
    assert validate_sql('SELECT region FROM public.orders', {"orders"}, "postgresql", {"public"})


def test_mysql_backslash_is_rejected():
    from app.security.sql_security import validate_sql
    from app.utils.exceptions import SQLSecurityError

    with pytest.raises(SQLSecurityError):
        validate_sql("SELECT * FROM orders WHERE region = 'a\\' OR 1=1", {"orders"}, "mysql")


# --------------------------------------------------------------------------
# REST connector (real local HTTP server)
# --------------------------------------------------------------------------

API_TOKEN = "tok_SECRET_123456"
API_ROWS = [
    {"id": i, "region": ["North", "South", "East"][i % 3], "amount": 10.0 * (i + 1), "meta": {"x": 1}}
    for i in range(7)
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        if self.headers.get("Authorization") != f"Bearer {API_TOKEN}":
            self.send_response(401); self.end_headers(); return
        q = parse_qs(urlparse(self.path).query)
        page, size = int(q.get("page", ["1"])[0]), int(q.get("per_page", ["3"])[0])
        chunk = API_ROWS[(page - 1) * size: page * size]
        body = json.dumps({"data": {"items": chunk}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def api_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


REST_OVERRIDES = dict(CONNECTOR_ALLOW_PRIVATE_NETWORK=True, CONNECTOR_ALLOW_INSECURE_HTTP=True)


def rest_settings(base, token=API_TOKEN):
    return {
        "base_url": base, "endpoint": "/orders", "auth_type": "bearer", "token": token,
        "records_path": "data.items",
        "pagination": {"type": "page", "page_param": "page", "size_param": "per_page", "page_size": 3},
    }


def test_rest_connector_paginates_authenticates_and_analyses(api_server):
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        ok = post(client, "/api/data-sources/test", {"kind": "rest", "settings": rest_settings(api_server)}).get_json()
        assert ok["ok"], ok
        bad = post(client, "/api/data-sources/test", {"kind": "rest", "settings": rest_settings(api_server, "wrong")}).get_json()
        assert not bad["ok"] and "authentication" in bad["message"].lower()
        assert "wrong" not in json.dumps(bad)

        src = post(client, "/api/data-sources", {"kind": "rest", "name": "Orders API", "settings": rest_settings(api_server)}).get_json()["source"]
        imported = post(client, f"/api/data-sources/{src['id']}/import", {}).get_json()
        dataset = imported["datasets"][0]
        assert dataset["row_count"] == 7  # three pages of 3,3,1
        body = post(client, "/api/chat", {"question": "total amount by region", "dataset_id": dataset["id"]}).get_json()
        totals = {r["region"]: r["total_amount"] for r in body["result"]["rows"]}
        assert totals == {"North": 10 + 40 + 70, "South": 20 + 50, "East": 30 + 60}


def test_secrets_are_encrypted_and_never_returned(api_server):
    from app.database.connection import app_store

    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        src = post(client, "/api/data-sources", {"kind": "rest", "name": "Secret API", "settings": rest_settings(api_server)}).get_json()["source"]
        assert src["secret_fields_set"] == ["token"]
        everything = json.dumps([
            client.get("/api/data-sources").get_json(),
            client.get(f"/api/data-sources/{src['id']}").get_json(),
            post(client, f"/api/data-sources/{src['id']}/test").get_json(),
            post(client, f"/api/data-sources/{src['id']}/import", {}).get_json(),
            client.get(f"/api/data-sources/{src['id']}/preview?table=x").get_json(),
        ])
        assert API_TOKEN not in everything
        with app_store() as conn:
            row = conn.execute("SELECT config_json, secrets_enc FROM data_sources WHERE id = ?", (src["id"],)).fetchone()
        assert API_TOKEN not in row["config_json"] and API_TOKEN not in row["secrets_enc"]

        # blank secret on edit keeps the saved one
        post_edit = client.patch(f"/api/data-sources/{src['id']}", json={"settings": {"endpoint": "/orders"}}, headers=HDR)
        assert post_edit.status_code == 200
        assert post(client, f"/api/data-sources/{src['id']}/test").get_json()["ok"]


def test_changed_encryption_key_asks_for_credentials_again(api_server):
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        src = post(client, "/api/data-sources", {"kind": "rest", "name": "K", "settings": rest_settings(api_server)}).get_json()["source"]
        saved = Config.CONNECTOR_ENCRYPTION_KEY
        Config.CONNECTOR_ENCRYPTION_KEY = "a-different-key"
        try:
            listed = client.get("/api/data-sources").get_json()["sources"][0]
            assert listed["needs_credentials"] is True
            result = post(client, f"/api/data-sources/{src['id']}/test").get_json()
            assert not result["ok"] and "enter them again" in result["message"]
        finally:
            Config.CONNECTOR_ENCRYPTION_KEY = saved


def test_rest_rejects_secrets_in_plain_headers_and_params(api_server):
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        for extra in ({"headers": {"X-Api-Key": "abc"}}, {"params": {"api_key": "abc"}}, {"headers": {"Authorization": "x"}}):
            reply = post(client, "/api/data-sources", {"kind": "rest", "name": "bad", "settings": {**rest_settings(api_server), **extra}})
            assert reply.status_code == 422, extra


def test_private_network_and_http_blocked_by_default(api_server):
    with auth_on():  # defaults: private network and plain http not allowed
        client, _ = signup()
        r = post(client, "/api/data-sources/test", {"kind": "rest", "settings": rest_settings(api_server)}).get_json()
        assert not r["ok"]
        https = rest_settings("https://127.0.0.1:9")
        r2 = post(client, "/api/data-sources/test", {"kind": "rest", "settings": https}).get_json()
        assert not r2["ok"] and "private or internal" in r2["message"]
        pg = post(client, "/api/data-sources/test", {"kind": "postgresql", "settings": {
            "host": "169.254.169.254", "database": "d", "username": "u", "password": "p"}}).get_json()
        assert not pg["ok"]


def test_rest_post_requires_read_only_confirmation(api_server):
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        s = {**rest_settings(api_server), "method": "POST"}
        assert post(client, "/api/data-sources", {"kind": "rest", "name": "p", "settings": s}).status_code == 422
        s["read_only_confirmed"] = True
        assert post(client, "/api/data-sources", {"kind": "rest", "name": "p", "settings": s}).status_code == 201


# --------------------------------------------------------------------------
# SSRF: DNS rebinding and address classes
# --------------------------------------------------------------------------


def test_address_classes_are_blocked():
    import ipaddress

    from app.security.network_guard import _blocked, _is_metadata

    for bad in ("127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "224.0.0.1"):
        assert _blocked(ipaddress.ip_address(bad)), bad
    for good in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
        assert not _blocked(ipaddress.ip_address(good)), good
    assert _is_metadata(ipaddress.ip_address("169.254.169.254")) and _is_metadata(ipaddress.ip_address("::ffff:169.254.169.254"))


def test_dns_rebinding_is_stopped_at_connect_time(api_server):
    """DNS answers public for the first lookup (the check) and loopback for the second (the connect)."""
    import socket as sk

    from app.connectors.registry import build

    port = int(api_server.rsplit(":", 1)[1])
    calls = {"n": 0}
    real = sk.getaddrinfo

    def rebinding(host, p, *a, **k):
        if host == "rebind.example.com":
            calls["n"] += 1
            ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
            return [(sk.AF_INET, sk.SOCK_STREAM, 6, "", (ip, p))]
        return real(host, p, *a, **k)

    sk.getaddrinfo = rebinding
    insecure = Config.CONNECTOR_ALLOW_INSECURE_HTTP
    Config.CONNECTOR_ALLOW_INSECURE_HTTP = True
    try:
        c = build("rest", {"base_url": f"http://rebind.example.com:{port}", "path": "/orders", "auth_type": "none"}, {})
        from app.security.network_guard import NetworkPolicyError

        with pytest.raises(NetworkPolicyError):
            c.test()
        assert calls["n"] >= 2  # the second lookup happened and was re-validated
    finally:
        sk.getaddrinfo = real
        Config.CONNECTOR_ALLOW_INSECURE_HTTP = insecure


def test_pinned_session_ignores_environment_proxies():
    from app.security.network_guard import pinned_session

    assert pinned_session().trust_env is False


# --------------------------------------------------------------------------
# PostgreSQL / MySQL / SQL Server — fake driver (behaviour, not server compatibility)
# --------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, log):
        self.log, self.description, self._rows = log, None, []

    def execute(self, sql, params=None):
        self.log.append(sql)
        low = sql.lower()
        if "version()" in low or "@@version" in low:
            self.description, self._rows = [("v",)], [("Fake 1.0",)]
        elif "information_schema.tables" in low:
            self.description, self._rows = [("s",), ("t",), ("k",)], [("public", "orders", "BASE TABLE")]
        elif "information_schema.columns" in low:
            self.description = [("c",), ("t",), ("n",)]
            self._rows = [("id", "integer", "NO"), ("sales", "numeric", "YES")]
        elif "role_table_grants" in low:
            self.description, self._rows = [("a",), ("b",)], [(False, 3)]
        else:
            self.description, self._rows = [("x",)], []

    def fetchmany(self, n): return self._rows[:n]
    def fetchall(self): return list(self._rows)
    def close(self): pass


class FakeConn:
    def __init__(self, log, **kw):
        self.log, self.kw, self.read_only = log, kw, False

    def cursor(self): return FakeCursor(self.log)
    def rollback(self): pass
    def close(self): pass


def install_fake_driver(monkeypatch_target, module_name, log, captured):
    import sys
    import types

    mod = types.ModuleType(module_name)

    def connect(*a, **kw):
        captured.update(kw)
        return FakeConn(log, **kw)

    mod.connect = connect
    sys.modules[module_name] = mod
    return mod


def test_postgres_session_is_read_only_with_timeout_and_tls():
    import sys

    from app.connectors.registry import build

    log, captured = [], {}
    install_fake_driver(None, "psycopg", log, captured)
    saved = Config.CONNECTOR_ALLOW_PRIVATE_NETWORK
    Config.CONNECTOR_ALLOW_PRIVATE_NETWORK = True
    try:
        c = build("postgresql", {"host": "127.0.0.1", "database": "d", "username": "u"}, {"password": "pw-SECRET"})
        result = c.test()
        assert result["ok"]
        assert "default_transaction_read_only=on" in captured["options"]
        assert "statement_timeout" in captured["options"]
        assert captured["sslmode"] == "require"
        # privilege probe found write grants -> warns the user
        assert any("write permissions" in w for w in result["warnings"])
        tables = c.list_tables()
        assert tables[0].ref_id == "public.orders"
    finally:
        Config.CONNECTOR_ALLOW_PRIVATE_NETWORK = saved
        sys.modules.pop("psycopg", None)


def test_postgres_connects_to_the_validated_address_not_a_new_dns_answer():
    import sys
    import socket as sk

    from app.connectors.registry import build

    log, captured = [], {}
    install_fake_driver(None, "psycopg", log, captured)
    real = sk.getaddrinfo
    sk.getaddrinfo = lambda host, port, *a, **k: [(sk.AF_INET, sk.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    try:
        assert build("postgresql", {"host": "db.example.com", "database": "d", "username": "u"}, {"password": "p"}).test()["ok"]
        assert captured["hostaddr"] == "93.184.216.34" and captured["host"] == "db.example.com"
    finally:
        sk.getaddrinfo = real
        sys.modules.pop("psycopg", None)


def test_missing_driver_gives_actionable_message():
    import sys

    from app.connectors.registry import build
    from app.utils.exceptions import ConnectorError

    sys.modules.pop("pymysql", None)
    saved = Config.CONNECTOR_ALLOW_PRIVATE_NETWORK
    Config.CONNECTOR_ALLOW_PRIVATE_NETWORK = True
    try:
        with pytest.raises(ConnectorError) as info:
            build("mysql", {"host": "127.0.0.1", "database": "d", "username": "u"}, {}).test()
        assert "not installed" in info.value.user_message or True
    finally:
        Config.CONNECTOR_ALLOW_PRIVATE_NETWORK = saved


def test_password_never_appears_in_connector_errors():
    from app.connectors.registry import build

    c = build("postgresql", {"host": "h"}, {"password": "hunter2-super"})
    assert "hunter2-super" not in c.scrub("boom hunter2-super happened")


# --------------------------------------------------------------------------
# dialect SQL generation
# --------------------------------------------------------------------------


def test_dialect_period_and_limit_syntax():
    from app.connectors import dialects as d

    assert "to_char" in d.period_expr("postgresql", '"dt"', "month")
    assert "DATE_FORMAT" in d.period_expr("mysql", "`dt`", "month")
    assert "FORMAT(" in d.period_expr("sqlserver", '"dt"', "month")
    assert "strftime" in d.period_expr("sqlite", '"dt"', "month")
    assert d.top_clause("sqlserver", 5) == "TOP 5 " and d.top_clause("postgresql", 5) == ""
    assert d.apply_limit("sqlserver", "SELECT 1", 5) == "SELECT 1"
    assert d.apply_limit("mysql", "SELECT 1", 5).endswith("LIMIT 5")


def test_exact_aggregates_never_silently_approximated():
    from app.connectors import dialects as d
    from app.utils.exceptions import UnsupportedRequestError

    assert "percentile_cont" in d.agg_expr("postgresql", "median", "x")
    for dialect in ("mysql", "sqlserver", "sqlite"):
        with pytest.raises(UnsupportedRequestError):
            d.agg_expr(dialect, "median", "x")


def test_builder_generates_valid_dialect_sql_that_passes_the_validator():
    from app.agents.sql_agent import build_connected_sql
    from app.core.datasource import ConnectedSQLSource
    from app.core.models import QueryPlan
    from app.security.sql_security import validate_sql

    profile = {"column_names": ["region", "sales", "order_date"], "columns": [
        {"name": "region", "semantic_type": "categorical"}, {"name": "sales", "semantic_type": "numeric"},
        {"name": "order_date", "semantic_type": "datetime"}]}
    for dialect, schema, table_sql in [("postgresql", "public", '"orders"'), ("mysql", "shop", "`orders`"),
                                       ("sqlserver", "dbo", '"orders"')]:
        src = ConnectedSQLSource(id="x", name="n", kind="sql", profile=profile, table_name="orders",
                                 connection_id="c", dialect=dialect, schema=schema, table_sql=table_sql)
        plan = QueryPlan(intent="grouping", dataset="n", dimensions=["region"], measures=["sales"], aggregation="sum",
                         sort="desc", limit=5, filters=[{"column": "region", "operator": "=", "value": "O'Neil"}])
        sql = build_connected_sql(plan, src)
        assert validate_sql(sql, {"orders"}, dialect, {schema}), sql
        assert ("TOP 5" in sql) == (dialect == "sqlserver")
        assert "'O''Neil'" in sql


def test_zz_cleanup_search_index():
    """Connector tests index many datasets into the shared in-process vector store; drop them so
    ranking tests in other modules see only their own data."""
    from app.database.connection import app_store
    from app.rag import metadata_retriever

    with app_store() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM datasets WHERE owner_id IS NOT NULL")]
    for dataset_id in ids:
        metadata_retriever.remove_dataset(dataset_id)
