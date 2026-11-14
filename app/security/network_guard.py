"""Outbound-connection guard for connectors.

A connector makes the *server* open a network connection to a host the user typed.
Left unchecked that lets any account probe the server's own network (localhost
admin ports, cloud metadata endpoints at 169.254.169.254, internal services).

`resolve_public_host` resolves the name and refuses anything that is not a public
address, unless CONNECTOR_ALLOW_PRIVATE_NETWORK is enabled by the operator.

DNS rebinding: checking a name and then letting the HTTP/DB library resolve it again
leaves a window in which a hostile DNS server can answer differently. To close it the
connection is made to the *validated address itself*:

* REST: `pinned_session()` replaces the socket factory, so the address that is validated
  is the address that is connected to (every request and every redirect hop).
* PostgreSQL: `hostaddr` pins the IP while `host` still drives TLS verification.
* MySQL: the socket is created here and handed to the driver.
* SQL Server: the validated IP is used in the connection string (certificate name kept).

Cloud-metadata addresses are refused even when CONNECTOR_ALLOW_PRIVATE_NETWORK is on.
"""

from __future__ import annotations

import ipaddress
import socket

from config import Config
from app.utils.exceptions import AInsightError


class NetworkPolicyError(AInsightError):
    status_code = 400


_METADATA = {ipaddress.ip_address(a) for a in ("169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200")}


def _is_metadata(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip in _METADATA


def _blocked(ip: ipaddress._BaseAddress) -> bool:
    """True for anything that is not an ordinary public internet address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _blocked(ip.ipv4_mapped)
        if ip.sixtofour is not None and _blocked(ip.sixtofour):
            return True
        if ip.teredo is not None:
            return True
    return (
        not ip.is_global  # private, CGNAT (100.64/10), loopback, link-local, reserved, documentation ranges
        or ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def resolve_public_host(host: str, port: int) -> list[str]:
    host = (host or "").strip()
    if not host:
        raise NetworkPolicyError("Enter a host name for this connection.")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkPolicyError(
            "That host name could not be found. Check the spelling and that it is reachable from the internet.",
            f"dns failure for {host}: {exc}",
        ) from exc
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if _is_metadata(ip) or (_blocked(ip) and not Config.CONNECTOR_ALLOW_PRIVATE_NETWORK):
            raise NetworkPolicyError(
                "That address is on a private or internal network, which this server is not allowed to "
                "connect to. Use a database or API that is reachable on the public internet.",
                f"{host} resolved to a non-public address",
            )
        addresses.append(str(ip))
    if not addresses:
        raise NetworkPolicyError("That host name did not resolve to an address.")
    return addresses


def pick_address(host: str, port: int) -> str:
    """Validate `host` and return one address to connect to (IPv4 preferred)."""
    addresses = resolve_public_host(host, port)
    addresses.sort(key=lambda a: ":" in a)
    return addresses[0]


def safe_create_connection(address, timeout=None, source_address=None, socket_options=None):
    """Drop-in for urllib3.util.connection.create_connection that connects only to
    addresses that passed the policy, resolving exactly once."""
    host, port = address
    host = host.strip("[]")
    last: Exception | None = None
    for ip in sorted(resolve_public_host(host, port), key=lambda a: ":" in a):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            for opt in socket_options or []:
                sock.setsockopt(*opt)
            if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect((ip, port))
            return sock
        except OSError as exc:
            last = exc
            if sock is not None:
                sock.close()
    raise last or OSError("no usable address")


def pinned_session():
    """A `requests` session whose connections go only to validated addresses.

    Environment proxies are ignored on purpose: with a proxy, the proxy would resolve
    the destination and the guard could not see where the request really goes.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    def _pinned_new_conn(self):
        try:
            return safe_create_connection((self._dns_host, self.port), self.timeout,
                                          source_address=self.source_address, socket_options=self.socket_options)
        except NetworkPolicyError:
            raise
        except socket.timeout as exc:
            from urllib3.exceptions import ConnectTimeoutError

            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out.") from exc
        except OSError as exc:
            from urllib3.exceptions import NewConnectionError

            raise NewConnectionError(self, f"Failed to establish a new connection: {exc}") from exc

    class _Conn(HTTPConnection):
        _new_conn = _pinned_new_conn

    class _SConn(HTTPSConnection):
        _new_conn = _pinned_new_conn

    class _Pool(HTTPConnectionPool):
        ConnectionCls = _Conn

    class _SPool(HTTPSConnectionPool):
        ConnectionCls = _SConn

    class _Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": _Pool, "https": _SPool}

    session = requests.Session()
    session.trust_env = False
    session.mount("http://", _Adapter())
    session.mount("https://", _Adapter())
    return session
