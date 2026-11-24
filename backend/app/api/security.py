"""Local loopback Host / Origin security middleware (P5).

Protects the local-only FastAPI server against:
1. DNS rebinding attacks (strict ``Host`` header validation on all HTTP requests,
   with zero hardcoded test-host backdoors in production).
2. Cross-site & cross-port request forgery (exact-origin matching against the
   request's own ``Host`` header, plus explicit ``AMC_ALLOWED_ORIGINS`` for the
   Vite dev server, and strict ``Sec-Fetch-Site`` / ``Referer`` checks on write
   and administrative routes).

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
_STRICT_FETCH_SITES = frozenset({'same-origin', 'none'})


def _extra_allowed_hosts() -> set[str]:
    raw = os.environ.get('AMC_ALLOWED_HOSTS', '')
    return {item.strip().lower() for item in raw.split(',') if item.strip()}


def _extra_allowed_origins() -> set[str]:
    raw = os.environ.get('AMC_ALLOWED_ORIGINS', '')
    return {item.strip().rstrip('/').lower() for item in raw.split(',') if item.strip()}


def parse_host_and_port(raw_host: str) -> tuple[str, int | None] | None:
    """Strictly parse an HTTP ``Host`` header into ``(hostname, port)``.

    Returns ``None`` if the header is empty, malformed, contains whitespace or
    userinfo (``@``), or specifies an invalid port.
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
            return (host, int(port_str)) if host else None
        return (host, None) if host else None
    if value.count(':') > 1:
        # Unbracketed IPv6 literal is invalid in HTTP Host header
        return None
    if ':' in value:
        host_part, port_str = value.rsplit(':', 1)
        if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
            return None
        host = host_part.lower()
        return (host, int(port_str)) if host else None
    host = value.lower()
    return (host, None) if host else None


def parse_host_header(raw_host: str) -> str | None:
    """Return normalized lowercase hostname from an HTTP ``Host`` header, or ``None``."""
    parsed = parse_host_and_port(raw_host)
    return parsed[0] if parsed is not None else None


def is_allowed_host(raw_host: str) -> bool:
    """Return True if ``raw_host`` points to a local loopback host or explicit allowlist."""
    hostname = parse_host_header(raw_host)
    if hostname is None:
        return False
    if hostname in _LOOPBACK_HOSTS:
        return True
    if hostname in _extra_allowed_hosts():
        return True
    return False


def _default_port(scheme: str) -> int:
    return 443 if scheme == 'https' else 80


def is_allowed_origin(raw_origin: str, *, expected_host: tuple[str, int | None] | None = None) -> bool:
    """Return True if ``raw_origin`` (Origin or Referer URL) matches the server origin or allowlist.

    When ``expected_host`` is provided, requires exact ``(hostname, effective_port)``
    matching against the request's ``Host`` header unless the origin is explicitly
    listed in ``AMC_ALLOWED_ORIGINS`` (e.g., the Vite dev server port). This
    prevents cross-port attacks from unrelated localhost services.
    """
    value = raw_origin.strip()
    if not value or value.lower() == 'null':
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme not in {'http', 'https'}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or '').lower()
    if not hostname:
        return False

    # Construct canonical origin (scheme://host[:port]) for allowlist check
    if ':' in hostname:
        host_repr = f'[{hostname}]'
    else:
        host_repr = hostname
    canonical = f'{scheme}://{host_repr}:{port}' if port is not None else f'{scheme}://{host_repr}'
    if canonical in _extra_allowed_origins():
        return True

    if hostname not in _LOOPBACK_HOSTS and hostname not in _extra_allowed_hosts():
        return False

    if expected_host is not None:
        req_host, req_port = expected_host
        eff_origin_port = port if port is not None else _default_port(scheme)
        eff_req_port = req_port if req_port is not None else _default_port(scheme)
        return hostname == req_host and eff_origin_port == eff_req_port

    return True


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
    """Pure ASGI middleware enforcing local loopback Host and exact-Origin policies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

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
        if len(hosts) != 1 or not is_allowed_host(hosts[0]):
            await _send_forbidden(
                send,
                'Forbidden: invalid Host header (only local loopback hosts are allowed).',
            )
            return
        expected_host = parse_host_and_port(hosts[0])

        # 2. Origin header validation whenever present (blocks cross-origin & cross-port probes/writes)
        if len(origins) > 1:
            await _send_forbidden(send, 'Forbidden: multiple Origin headers are not allowed.')
            return
        explicit_origin_ok = False
        if len(origins) == 1:
            if not is_allowed_origin(origins[0], expected_host=expected_host):
                await _send_forbidden(
                    send,
                    'Forbidden: cross-origin requests are not allowed.',
                )
                return
            explicit_origin_ok = True

        # 3. Additional CSRF guards on state-changing / high-privilege methods
        if method in _UNSAFE_METHODS:
            for site in fetch_sites:
                normalized_site = site.strip().lower()
                if normalized_site in _STRICT_FETCH_SITES:
                    continue
                # Allow `same-site` ONLY when an explicit, validated Origin was present
                # (e.g. explicitly configured Vite dev origin on another loopback port).
                if normalized_site == 'same-site' and explicit_origin_ok:
                    continue
                await _send_forbidden(
                    send,
                    'Forbidden: cross-site write requests are not allowed.',
                )
                return
            if not origins and referers:
                if len(referers) > 1 or not is_allowed_origin(referers[0], expected_host=expected_host):
                    await _send_forbidden(
                        send,
                        'Forbidden: cross-origin Referer on write request.',
                    )
                    return

        await self.app(scope, receive, send)
