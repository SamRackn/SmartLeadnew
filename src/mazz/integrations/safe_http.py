"""The one HTTP door for fetching a PROSPECT-controlled address.

Research fetches whatever domain is on an imported lead record, and follows the
redirects that site chooses. On the production VM that is a way to reach
127.0.0.1 (Caddy's admin API, the app, Postgres), RFC1918 and the cloud
metadata service. So every hop is checked here, before any request is made:

  * http/https only, no userinfo;
  * the name is resolved ONCE per hop, every answer must be a public address
    (a mixed answer is rejected -- the fetcher cannot be trusted to pick the
    good one), and the request is then pinned to that validated IP, so the
    check cannot be raced by a second DNS lookup (rebinding);
  * the Host header and TLS SNI/certificate name stay the original hostname,
    so TLS verification is exactly as strict as before;
  * redirects are followed here, by hand, each hop re-validated and bounded;
  * the body is streamed and cut off at ``max_bytes``.

A refusal raises `UnsafeResearchURL` / `ResearchResponseTooLarge`. They are
deliberately not `httpx.HTTPError`: callers that fail open on a network blip
must not be able to mistake a security refusal for one.

ponytail: no 6to4/NAT64/Teredo unwrapping beyond IPv4-mapped; add if the VM
ever gets an IPv6 route to a translator.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

__all__ = ["SafeHTTPClient", "UnsafeResearchURL", "ResearchResponseTooLarge"]

_REDIRECTS = (301, 302, 303, 307, 308)


class UnsafeResearchURL(Exception):
    """The destination is not somewhere research may fetch from."""


class ResearchResponseTooLarge(Exception):
    """The body exceeded the byte cap; reading stopped."""


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class SafeHTTPClient:
    def __init__(
        self,
        *,
        resolver: Callable[..., Any] | None = None,
        transport: httpx.BaseTransport | None = None,
        max_redirects: int = 5,
        max_bytes: int = 2_000_000,
        timeout: float = 10.0,
    ) -> None:
        self._resolver = resolver or socket.getaddrinfo
        self._max_redirects = max_redirects
        self._max_bytes = max_bytes
        # follow_redirects stays False: redirects are ours to validate.
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def _validated_addresses(self, host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            found = [host]  # a literal never goes through the resolver
        except ValueError:
            try:
                answers = self._resolver(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                # Not a security refusal: an unresolvable name is an unreachable
                # site, and callers already retry/classify network errors.
                raise httpx.ConnectError(f"cannot resolve {host}: {exc}") from exc
            found = list(dict.fromkeys(a[4][0] for a in answers))
        if not found:
            raise httpx.ConnectError(f"no addresses for {host}")
        bad = [a for a in found if not _public(a)]
        if bad:
            raise UnsafeResearchURL(f"{host} resolves to a non-public address ({bad[0]})")
        return found

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
        current = url
        for hop in range(self._max_redirects + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https"):
                raise UnsafeResearchURL(f"scheme {parts.scheme!r} is not fetchable")
            if parts.username is not None or parts.password is not None or "@" in parts.netloc:
                raise UnsafeResearchURL("URLs with embedded credentials are not fetched")
            if not parts.hostname:
                raise UnsafeResearchURL("URL has no host")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            addresses = self._validated_addresses(parts.hostname, port)

            response = self._send_pinned(current, parts, addresses, headers)
            location = response.headers.get("location")
            if response.status_code in _REDIRECTS and location:
                response.close()
                current = urljoin(current, location)
                continue
            try:
                return self._bounded(response, current)
            finally:
                response.close()
        raise UnsafeResearchURL(f"more than {self._max_redirects} redirects from {url}")

    def _send_pinned(self, url, parts, addresses, headers) -> httpx.Response:
        extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
        last: Exception | None = None
        for address in addresses:
            request = self._client.build_request(
                "GET",
                httpx.URL(url).copy_with(host=address),
                headers={**(headers or {}), "Host": parts.netloc},
                extensions=extensions,
            )
            try:
                return self._client.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc  # try the next validated address
        raise last  # type: ignore[misc]

    def _bounded(self, response: httpx.Response, url: str) -> httpx.Response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            raise ResearchResponseTooLarge(f"{url} declares {declared} bytes")
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > self._max_bytes:
                raise ResearchResponseTooLarge(f"{url} exceeds {self._max_bytes} bytes")
        # Already decoded by iter_bytes, so drop the headers that describe the
        # wire form or the body would be decoded a second time.
        kept = [(k, v) for k, v in response.headers.items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(
            response.status_code, headers=kept, content=bytes(body),
            request=httpx.Request("GET", url),
        )
