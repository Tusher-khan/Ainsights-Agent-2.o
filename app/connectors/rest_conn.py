"""REST API connector.

An analytics-oriented fetcher for JSON APIs: it calls one configured endpoint (with
optional pagination), maps the response to rows, and hands back a DataFrame that the
normal profiling and pandas-analysis path then treats like any uploaded table.

Safety properties:
* Credentials (API key, bearer token, basic-auth password) are stored encrypted and
  used only here; they are never sent to the language model or returned by the API.
* The target host passes the network guard on every request and on every redirect.
* Secret-looking values are refused in plain headers / query parameters so they cannot
  end up stored unencrypted in the connection's config.
* Hard caps on response size, page count and row count.
* POST is allowed (many reporting APIs need it) only after the user confirms the
  endpoint reads data and does not change anything.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import pandas as pd

from config import Config
from app.connectors.base import ColumnInfo, DataConnector, TableInfo, TableRef
from app.security.network_guard import NetworkPolicyError, pinned_session, resolve_public_host
from app.utils.exceptions import ConnectorError

HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SECRETISH_RE = re.compile(r"(auth|token|secret|key|password|passwd|cookie|bearer|signature)", re.I)
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}
AUTH_TYPES = {"none", "bearer", "api_key_header", "api_key_query", "basic"}
PAGINATION_TYPES = {"none", "page", "offset", "cursor", "link"}


def get_path(obj: Any, path: str | None) -> Any:
    """Read a dotted path such as 'data.items' or 'result.0.rows' from parsed JSON."""
    if not path:
        return obj
    current = obj
    for part in str(path).split("."):
        if isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


class RESTConnector(DataConnector):
    kind = "rest"
    dialect = ""
    supports_sql = False
    enforces_read_only_session = False

    # -- validation ------------------------------------------------------
    def validate_config(self) -> None:
        cfg = self.config
        parsed = urlparse(str(cfg.get("base_url", "")).strip())
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ConnectorError("Enter a full web address starting with https://")
        if parsed.scheme == "http" and not Config.CONNECTOR_ALLOW_INSECURE_HTTP:
            raise ConnectorError("Only secure https:// addresses are allowed on this server.")
        if parsed.username or parsed.password:
            raise ConnectorError("Do not put a user name or password in the address. Use the authentication options.")
        if cfg.get("method", "GET").upper() not in {"GET", "POST"}:
            raise ConnectorError("Only GET and POST requests are supported.")
        if cfg.get("method", "GET").upper() == "POST" and not cfg.get("read_only_confirmed"):
            raise ConnectorError("Confirm that this POST endpoint only reads data and does not change anything.")
        if cfg.get("auth_type", "none") not in AUTH_TYPES:
            raise ConnectorError("Unknown authentication method.")
        if cfg.get("pagination", {}).get("type", "none") not in PAGINATION_TYPES:
            raise ConnectorError("Unknown pagination method.")
        for name, value in (cfg.get("headers") or {}).items():
            if not HEADER_NAME_RE.match(str(name)) or str(name).lower() in FORBIDDEN_HEADERS:
                raise ConnectorError(f"The header name '{name}' is not allowed.")
            if re.search(r"[\r\n]", str(value)):
                raise ConnectorError("Header values cannot contain line breaks.")
            if SECRETISH_RE.search(str(name)):
                raise ConnectorError(
                    f"'{name}' looks like a secret. Put keys and tokens in the authentication section so they are stored encrypted."
                )
        for name in (cfg.get("params") or {}):
            if SECRETISH_RE.search(str(name)):
                raise ConnectorError(
                    f"The query parameter '{name}' looks like a secret. Use 'API key in query string' authentication instead."
                )
        endpoint = str(cfg.get("endpoint", ""))
        if "://" in endpoint or endpoint.startswith("//"):
            raise ConnectorError("Enter only the path of the endpoint, e.g. /v1/orders")
        auth = cfg.get("auth_type", "none")
        if auth == "bearer" and not self.secrets.get("token"):
            raise ConnectorError("Enter the bearer token.")
        if auth in {"api_key_header", "api_key_query"} and not self.secrets.get("api_key"):
            raise ConnectorError("Enter the API key.")
        if auth == "basic" and not (cfg.get("username") and self.secrets.get("password")):
            raise ConnectorError("Enter the user name and password.")

    # -- http ------------------------------------------------------------
    def _url(self, extra_params: dict | None = None) -> tuple[str, dict]:
        base = str(self.config["base_url"]).rstrip("/") + "/"
        endpoint = str(self.config.get("endpoint", "")).lstrip("/")
        url = urljoin(base, endpoint)
        params = dict(self.config.get("params") or {})
        params.update(extra_params or {})
        if self.config.get("auth_type") == "api_key_query":
            params[self.config.get("auth_name") or "api_key"] = self.secrets["api_key"]
        return url, params

    def _auth(self) -> tuple[dict, Any]:
        headers = {"Accept": "application/json", "User-Agent": "AInsight-connector/1.0"}
        headers.update({str(k): str(v) for k, v in (self.config.get("headers") or {}).items()})
        auth = self.config.get("auth_type", "none")
        basic = None
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {self.secrets['token']}"
        elif auth == "api_key_header":
            headers[self.config.get("auth_name") or "X-API-Key"] = self.secrets["api_key"]
        elif auth == "basic":
            basic = (self.config["username"], self.secrets["password"])
        return headers, basic

    def _request(self, url: str, params: dict) -> Any:
        import requests

        headers, basic = self._auth()
        method = self.config.get("method", "GET").upper()
        body = self.config.get("body") if method == "POST" else None
        base_host = urlparse(str(self.config["base_url"])).hostname
        timeout = Config.CONNECTOR_CONNECT_TIMEOUT + Config.QUERY_TIMEOUT_SECONDS
        current = url
        session = pinned_session()  # connects only to validated addresses (no DNS rebinding window)
        for hop in range(4):
            parsed = urlparse(current)
            resolve_public_host(parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
            try:
                resp = session.request(
                    method, current, params=params if hop == 0 else None, headers=headers, auth=basic,
                    json=body, timeout=timeout, allow_redirects=False, stream=True,
                )
            except NetworkPolicyError:
                raise
            except requests.RequestException as exc:
                if isinstance(getattr(exc, "args", [None])[0] if exc.args else None, Exception) and "NetworkPolicyError" in repr(exc):
                    raise NetworkPolicyError("That address is on a private or internal network, which this server is not allowed to connect to.") from exc
                raise self.fail("The API could not be reached. Check the address and that it is online.", exc) from exc
            if resp.is_redirect:
                nxt = urljoin(current, resp.headers.get("Location", ""))
                if urlparse(nxt).hostname != base_host:
                    resp.close()
                    raise ConnectorError("The API redirected to a different site, which is not allowed.")
                current = nxt
                resp.close()
                continue
            break
        else:
            raise ConnectorError("The API redirected too many times.")

        try:
            if resp.status_code in (401, 403):
                raise ConnectorError("The API rejected the credentials (authentication failed).", f"HTTP {resp.status_code}")
            if resp.status_code == 404:
                raise ConnectorError("The endpoint was not found (404). Check the address and path.")
            if resp.status_code == 429:
                raise ConnectorError("The API is rate-limiting requests. Try again in a few minutes.")
            if resp.status_code >= 400:
                raise ConnectorError(f"The API returned an error (HTTP {resp.status_code}).", f"HTTP {resp.status_code}")
            cap = Config.REST_MAX_RESPONSE_MB * 1024 * 1024
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                size += len(chunk)
                if size > cap:
                    raise ConnectorError(f"The API response is larger than {Config.REST_MAX_RESPONSE_MB} MB.")
                chunks.append(chunk)
        finally:
            resp.close()
        try:
            return json.loads(b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace"))
        except ValueError as exc:
            raise ConnectorError("The API did not return valid JSON.", str(exc)) from exc

    # -- data ------------------------------------------------------------
    def _records(self, payload: Any) -> list[dict]:
        data = get_path(payload, self.config.get("records_path"))
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ConnectorError(
                "No list of records was found in the response. Set the 'records path' (for example data.items).",
                f"records_path={self.config.get('records_path')!r} gave {type(data).__name__}",
            )
        return [r for r in data if isinstance(r, dict)]

    def fetch_records(self, max_rows: int | None = None, max_pages: int | None = None) -> list[dict]:
        self.validate_config()
        max_rows = max_rows or Config.REST_MAX_ROWS
        pages_cap = max_pages or Config.REST_MAX_PAGES
        pg = self.config.get("pagination") or {}
        ptype = pg.get("type", "none")
        size = int(pg.get("page_size") or 100)
        rows: list[dict] = []
        extra: dict = {}
        url, params = self._url()
        page_no = int(pg.get("start") or (1 if ptype == "page" else 0))
        cursor = None
        for _ in range(1 if ptype == "none" else pages_cap):
            q = dict(params)
            if ptype == "page":
                q[pg.get("page_param") or "page"] = page_no
                q[pg.get("size_param") or "per_page"] = size
            elif ptype == "offset":
                q[pg.get("offset_param") or "offset"] = len(rows)
                q[pg.get("size_param") or "limit"] = size
            elif ptype == "cursor" and cursor:
                q[pg.get("cursor_param") or "cursor"] = cursor
            payload = self._request(url, q)
            batch = self._records(payload)
            rows.extend(batch)
            if ptype == "none" or not batch or len(rows) >= max_rows:
                break
            if ptype == "page":
                page_no += 1
                if len(batch) < size:
                    break
            elif ptype == "offset":
                if len(batch) < size:
                    break
            elif ptype == "cursor":
                cursor = get_path(payload, pg.get("next_cursor_path"))
                if not cursor:
                    break
            elif ptype == "link":
                nxt = get_path(payload, pg.get("next_url_path"))
                if not nxt:
                    break
                nxt_url = urljoin(url, str(nxt))
                if urlparse(nxt_url).hostname != urlparse(url).hostname:
                    raise ConnectorError("The API's next-page link points to a different site, which is not allowed.")
                parsed = urlparse(nxt_url)
                url = parsed._replace(query="").geturl()
                params = dict(parse_qsl(parsed.query))
        return rows[:max_rows]

    def fetch_frame(self, max_rows: int | None = None) -> pd.DataFrame:
        records = self.fetch_records(max_rows)
        mapping = self.config.get("fields") or {}
        if mapping:
            records = [{out: get_path(r, path) for out, path in mapping.items()} for r in records]
        if not records:
            raise ConnectorError("The API returned no records.")
        frame = pd.json_normalize(records, sep="_")
        # Keep scalar columns only: analysis needs tabular values, not nested objects.
        for col in list(frame.columns):
            if frame[col].map(lambda v: isinstance(v, (list, dict))).any():
                frame = frame.drop(columns=[col])
        if frame.empty or len(frame.columns) == 0:
            raise ConnectorError("The records contain no simple (non-nested) fields to analyse.")
        return frame

    # -- contract --------------------------------------------------------
    def test(self) -> dict:
        started = time.perf_counter()
        self.validate_config()
        sample = self.fetch_records(max_rows=50, max_pages=1)
        if not sample:
            raise ConnectorError("The API responded but returned no records.")
        return {
            "ok": True,
            "message": f"Connected. The first page returned {len(sample)} record(s).",
            "server_version": "",
            "warnings": [] if self.config.get("auth_type", "none") != "none"
            else ["This API is called without authentication."],
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    def list_tables(self) -> list[TableRef]:
        return [TableRef(schema="", name=str(self.config.get("endpoint", "")).strip("/") or "data", kind="endpoint")]

    def describe_table(self, ref: TableRef) -> TableInfo:
        frame = self.fetch_frame(max_rows=200)
        return TableInfo(
            ref=ref,
            columns=[ColumnInfo(name=str(c), type=str(frame[c].dtype)) for c in frame.columns],
            row_estimate=None,
        )
