"""Research network boundary regressions: fake DNS/transport only."""

import socket

import httpx
import pytest


def client_for(handler, *, addresses=("93.184.216.34",), **kwargs):
    # Import inside the helper so the pre-fix run records missing boundary
    # per case rather than aborting collection of the rest of the audit.
    from mazz.integrations.safe_http import SafeHTTPClient

    def resolve(host, port, **options):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
                for ip in addresses]

    return SafeHTTPClient(resolver=resolve, transport=httpx.MockTransport(handler), **kwargs)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "::1", "fe80::1", "fc00::1", "0.0.0.0",
])
def test_private_dns_answers_are_rejected_before_any_request(address):
    calls = []
    client = client_for(lambda req: calls.append(req) or httpx.Response(200),
                        addresses=(address,))
    from mazz.integrations.safe_http import UnsafeResearchURL

    with pytest.raises(UnsafeResearchURL):
        client.get("https://public.example/robots.txt")
    assert calls == []
    client.close()


def test_mixed_public_and_private_dns_is_rejected():
    calls = []
    client = client_for(lambda req: calls.append(req) or httpx.Response(200),
                        addresses=("93.184.216.34", "169.254.169.254"))
    from mazz.integrations.safe_http import UnsafeResearchURL

    with pytest.raises(UnsafeResearchURL):
        client.get("https://public.example/")
    assert calls == []
    client.close()


def test_request_is_pinned_to_validated_ip_with_original_tls_name_and_host():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, text="public page")

    client = client_for(respond)
    response = client.get("https://public.example/services?q=law")
    assert response.text == "public page"
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "public.example"
    assert seen[0].extensions["sni_hostname"] == "public.example"
    assert seen[0].url.path == "/services"
    assert seen[0].url.query == b"q=law"
    client.close()


def test_redirect_to_metadata_is_rejected_without_following_it():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/opc/v2/"})

    client = client_for(respond)
    from mazz.integrations.safe_http import UnsafeResearchURL

    with pytest.raises(UnsafeResearchURL):
        client.get("https://public.example/")
    assert len(seen) == 1
    client.close()


def test_relative_redirect_keeps_original_origin_and_revalidates():
    seen = []

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "/services"})
        return httpx.Response(200, text="services")

    client = client_for(respond)
    assert client.get("https://public.example/").text == "services"
    assert [r.headers["host"] for r in seen] == ["public.example", "public.example"]
    assert seen[1].url.path == "/services"
    client.close()


def test_redirect_loop_is_bounded():
    seen = []
    client = client_for(lambda req: seen.append(req) or httpx.Response(
        302, headers={"Location": "/again"}), max_redirects=2)
    from mazz.integrations.safe_http import UnsafeResearchURL

    with pytest.raises(UnsafeResearchURL):
        client.get("https://public.example/")
    assert len(seen) <= 3
    client.close()


class Chunks(httpx.SyncByteStream):
    def __iter__(self):
        yield b"a" * 8
        yield b"b" * 8
        raise AssertionError("must stop reading once the size limit is crossed")


def test_streamed_body_is_bounded_without_content_length():
    client = client_for(lambda req: httpx.Response(200, stream=Chunks()), max_bytes=10)
    from mazz.integrations.safe_http import ResearchResponseTooLarge

    with pytest.raises(ResearchResponseTooLarge):
        client.get("https://public.example/")
    client.close()


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://public.example/data", "https://user:pass@public.example/",
])
def test_non_web_schemes_and_embedded_credentials_are_rejected(url):
    seen = []
    client = client_for(lambda req: seen.append(req) or httpx.Response(200))
    from mazz.integrations.safe_http import UnsafeResearchURL

    with pytest.raises(UnsafeResearchURL):
        client.get(url)
    assert seen == []
    client.close()


# -- company.py itself: unsafe is a distinct failure, never "site blocked" ----


def _adapter(handler, *, addresses=("127.0.0.1",)):
    from mazz.integrations.base import RetryPolicy
    from mazz.integrations.company import LiveCompanyAdapter

    return LiveCompanyAdapter(
        http_client=client_for(handler, addresses=addresses),
        retry_policy=RetryPolicy(attempts=3), sleep=lambda s: None, rate_limit_seconds=0.0,
    )


def test_company_adapter_refuses_private_destination_for_robots_and_homepage():
    from mazz.integrations.base import ProviderBlocked
    from mazz.integrations.company import ProviderUnsafeTarget
    from mazz.pipeline.enrich import _give_up_on_site

    calls = []
    adapter = _adapter(lambda req: calls.append(req) or httpx.Response(200, text="x"))

    # robots.txt on its own ...
    with pytest.raises(ProviderUnsafeTarget):
        adapter._robots_allows("https://victim.example/")
    # ... and the whole fetch, which starts at robots then the homepage.
    with pytest.raises(ProviderUnsafeTarget) as caught:
        adapter.fetch_company("victim.example")
    assert calls == []  # nothing reached the transport, robots included
    # Not a blocked site, and not retried: the enrich step only falls back to
    # the lead record for ProviderBlocked, so this can never qualify a lead.
    assert not isinstance(caught.value, ProviderBlocked)
    assert caught.value.retryable is False
    assert not _give_up_on_site(None, caught.value)


def test_robots_redirect_to_metadata_is_refused_and_homepage_never_fetched():
    from mazz.integrations.company import ProviderUnsafeTarget

    seen = []

    def respond(request):
        seen.append(request.url.host)
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        return httpx.Response(200, text="<html></html>")

    adapter = _adapter(respond, addresses=("93.184.216.34",))
    with pytest.raises(ProviderUnsafeTarget):
        adapter.fetch_company("victim.example")
    assert seen == ["93.184.216.34"]  # the robots hop only; metadata not requested


def test_company_adapter_default_client_is_the_safe_client():
    from mazz.integrations.company import LiveCompanyAdapter
    from mazz.integrations.safe_http import SafeHTTPClient

    adapter = LiveCompanyAdapter()
    assert isinstance(adapter._client, SafeHTTPClient)
    adapter.close()
