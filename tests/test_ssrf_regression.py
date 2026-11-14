"""SSRF regression suite: localhost, loopback, private v4/v6, link-local, metadata, redirects, re-resolution."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from config import Config
from tests.test_auth import auth_on, post, signup
from tests.test_connectors import REST_OVERRIDES, rest_settings


class _Redirector(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", _Redirector.target)
        self.end_headers()


@pytest.fixture(scope="module")
def redirect_server():
    server = HTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _test_rest(client, base):
    return post(client, "/api/data-sources/test", {"kind": "rest", "settings": rest_settings(base)}).get_json()


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "10.0.0.5", "192.168.0.10", "172.16.4.4",
                                  "169.254.169.254", "[fe80::1]", "[fd00::1]", "0.0.0.0", "100.64.0.1"])
def test_internal_hosts_are_refused_by_default(host):
    with auth_on(CONNECTOR_ALLOW_INSECURE_HTTP=True):
        client, _ = signup()
        r = _test_rest(client, f"http://{host}:9")
        assert not r["ok"] and "private or internal" in r["message"], (host, r)


def test_metadata_address_is_refused_even_when_private_networks_are_allowed():
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        for host in ("169.254.169.254", "[::ffff:169.254.169.254]", "[fd00:ec2::254]"):
            r = _test_rest(client, f"http://{host}:80")
            assert not r["ok"], host


def test_redirect_to_another_host_is_refused(redirect_server):
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        for target in ("http://169.254.169.254/latest/meta-data/", "http://localhost:9/x", "http://10.0.0.1/"):
            _Redirector.target = target
            r = _test_rest(client, redirect_server)
            assert not r["ok"] and "redirected" in r["message"].lower(), (target, r)


def test_redirect_loop_is_cut_off(redirect_server):
    with auth_on(**REST_OVERRIDES):
        client, _ = signup()
        _Redirector.target = redirect_server + "/again"
        r = _test_rest(client, redirect_server)
        assert not r["ok"] and "too many" in r["message"].lower()


def test_secrets_do_not_leak_into_refusal_messages():
    with auth_on(CONNECTOR_ALLOW_INSECURE_HTTP=True):
        client, _ = signup()
        s = rest_settings("http://10.0.0.5:9", token="tok_LEAKCHECK_987")
        text = str(post(client, "/api/data-sources/test", {"kind": "rest", "settings": s}).get_json())
        assert "tok_LEAKCHECK_987" not in text
