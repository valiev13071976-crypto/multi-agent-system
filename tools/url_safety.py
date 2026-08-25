from ipaddress import ip_address, ip_network
from urllib.parse import urlparse, urlunparse


ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "metadata.google.internal",
        "host.docker.internal",
    }
)
PRIVATE_NETWORKS = tuple(
    ip_network(item)
    for item in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "::ffff:127.0.0.0/104",
        "::ffff:10.0.0.0/104",
        "::ffff:172.16.0.0/108",
        "::ffff:192.168.0.0/112",
        "::ffff:169.254.0.0/112",
    )
)


class UnsafeUrlError(ValueError):
    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(reason)


def _hostname(url) -> str:
    parsed = urlparse(str(url or "").strip())
    return (parsed.hostname or "").strip().rstrip(".").lower()


def _is_blocked_host(host: str) -> bool:
    if not host:
        return True
    if host in BLOCKED_HOSTS:
        return True
    if host.endswith(".localhost") or host.endswith(".local"):
        return True
    return False


def _is_private_ip(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        return True
    if address.is_multicast or address.is_unspecified:
        return True
    for network in PRIVATE_NETWORKS:
        if address in network:
            return True
    return False


def is_safe_http_url(url: str) -> bool:
    try:
        validate_http_url(url)
        return True
    except UnsafeUrlError:
        return False


def validate_http_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeUrlError(raw, "empty_url")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(raw, "scheme_not_allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrlError(raw, "url_userinfo_not_allowed")
    host = _hostname(raw)
    if _is_blocked_host(host):
        raise UnsafeUrlError(raw, "blocked_host")
    if _is_private_ip(host):
        raise UnsafeUrlError(raw, "private_or_loopback_ip")
    if parsed.port in (22, 25, 3306, 5432, 6379, 11211, 27017):
        raise UnsafeUrlError(raw, "blocked_port")
    normalized = urlunparse(
        (
            scheme,
            parsed.netloc.lower(),
            parsed.path or "",
            parsed.params,
            parsed.query,
            "",
        )
    )
    return normalized


def validate_redirect(from_url: str, to_url: str) -> str:
    validate_http_url(from_url)
    return validate_http_url(to_url)


def source_domain(url: str) -> str:
    host = _hostname(url)
    if host.startswith("www."):
        return host[4:]
    return host


def normalize_url_for_dedup(url: str) -> str:
    parsed = urlparse(validate_http_url(url))
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", parsed.query, ""))
