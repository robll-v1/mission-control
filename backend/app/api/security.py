"""Local loopback Host / Origin security middleware (P5).

Protects the local-only FastAPI server against:
1. DNS rebinding attacks (strict ``Host`` header validation on all HTTP requests).
2. Cross-site request forgery / cross-origin probes (strict ``Origin``,
   ``Sec-Fetch-Site``, and ``Referer`` validation, especially on write and
   administrative routes).

Implemented as pure ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``
so SSE ``StreamingResponse`` endpoints remain zero-overhead and never suffer from
streaming cancellation/buffering interference.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
_UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
_ALLOWED_FETCH_SITES = frozenset({'same-origin', 'same-site', 'none'})


def _extra_allowed_hosts() -> set[str]:
    raw = os.environ.get('AMC_ALLOWED_HOSTS', '')
    return {item.strip().lower() for item in raw.split(',') if item.strip()}


def _extra_allowed_origins() -> set[str]:
    raw = os.environ.get('AMC_ALLOWED_ORIGINS', '')
    return {item.strip().rstrip('/').lower() for item in raw.split(',') if item.strip()}


def parse_host_header(raw_host: str) -> str | None:
    """Strictly parse an HTTP ``Host`` header and return the normalized lowercase hostname.

    Returns ``None`` if the header is empty, malformed, contains whitespace or
    userinfo (``@``), or specifies a non-numeric port.
    """
    value = raw_host.strip()
    if not value or any(ch.isspace() for ch in value) or '@' in value or '/' in value or '\\' in value:
        return None
    if value.startswith('['):
        end = value.find(']')
        if end == -1:
            return None
        host = value[1:end].lower()
        rest = value[end + 1:]
        if rest:
            if not rest.startswith(':'):
                return None
            port_str = rest[1:]
            if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
                return None
        return host or None
    if value.count(':') > 1:
        # Unbracketed IPv6 literal is invalid in HTTP Host header
        return None
    if ':' in value:
        host_part, port_str = value.rsplit(':', 1)
        if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
            return None
        host = host_part.lower()
    else:
        host = value.lower()
    return host or None


def is_allowed_host(raw_host: str, *, client_host: str | None = None) -> bool:
    """Return True if ``raw_host`` points to a local loopback host (or TestClient)."""
    hostname = parse_host_header(raw_host)
    if hostname is None:
        return False
    if hostname in _LOOPBACK_HOSTS:
        return True
    if hostname == 'testserver' and client_host == 'testclient':
        return True
    if hostname in _extra_allowed_hosts():
        return True
    return False


def is_allowed_origin(raw_origin: str, *, client_host: str | None = None) -> bool:
    """Return True if ``raw_origin`` (Origin or Referer URL) is a local loopback origin."""
    value = raw_origin.strip()
    if not value or value.lower() == 'null':
        return False
    normalized = value.rstrip('/').lower()
    if normalized in _extra_allowed_origins():
        return True
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {'http', 'https'}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or '').lower()
    if not hostname:
        return False
    if hostname in _LOOPBACK_HOSTS:
        return True
    if hostname == 'testserver' and client_host == 'testclient':
        return True
    if hostname in _extra_allowed_hosts():
        return True
    return False


async def _send_forbidden(send, detail: str) -> None:
    body = json.dumps({'detail': detail}).encode('utf-8')
    await send({
        'type': 'http.response.start',
        'status': 403,
        'headers': [
            (b'content-type', b'application/json'),
            (b'content-length', str(len(body)).encode('ascii')),
            (b'cache-control', b'no-store'),
        ],
    })
    await send({
        'type': 'http.response.body',
        'body': body,
        'more_body': False,
    })


class LocalSecurityMiddleware:
    """Pure ASGI middleware enforcing local loopback Host and Origin policies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        client = scope.get('client')
        client_host = client[0] if isinstance(client, (tuple, list)) and client else None
        method = str(scope.get('method') or 'GET').upper()

        hosts: list[str] = []
        origins: list[str] = []
        referers: list[str] = []
        fetch_sites: list[str] = []
        for raw_name, raw_value in scope.get('headers') or ():
            name = raw_name.decode('latin-1').lower()
            if name == 'host':
                hosts.append(raw_value.decode('latin-1'))
            elif name == 'origin':
                origins.append(raw_value.decode('latin-1'))
            elif name == 'referer':
                referers.append(raw_value.decode('latin-1'))
            elif name == 'sec-fetch-site':
                fetch_sites.append(raw_value.decode('latin-1'))

        # 1. Host header validation (DNS rebinding protection on all HTTP requests)
        if len(hosts) != 1 or not is_allowed_host(hosts[0], client_host=client_host):
            await _send_forbidden(
                send,
                'Forbidden: invalid Host header (only local loopback hosts are allowed).',
            )
            return

        # 2. Origin header validation whenever present (blocks cross-origin reads & writes)
        if len(origins) > 1:
            await _send_forbidden(send, 'Forbidden: multiple Origin headers are not allowed.')
            return
        if len(origins) == 1 and not is_allowed_origin(origins[0], client_host=client_host):
            await _send_forbidden(
                send,
                'Forbidden: cross-origin requests are not allowed.',
            )
            return

        # 3. Additional CSRF guards on state-changing / high-privilege methods
        if method in _UNSAFE_METHODS:
            if any(site.strip().lower() not in _ALLOWED_FETCH_SITES for site in fetch_sites):
                await _send_forbidden(
                    send,
                    'Forbidden: cross-site write requests are not allowed.',
                )
                return
            if not origins and referers:
                if len(referers) > 1 or not is_allowed_origin(referers[0], client_host=client_host):
                    await _send_forbidden(
                        send,
                        'Forbidden: cross-origin Referer on write request.',
                    )
                    return

        await self.app(scope, receive, send)
