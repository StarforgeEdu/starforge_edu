"""DNS ownership verification must not become a configurable SSRF primitive."""

from __future__ import annotations

import json

import pytest

from apps.tenancy import services


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///etc/passwd",
        "http://cloudflare-dns.com/dns-query",
        "https://user:secret@cloudflare-dns.com/dns-query",
        "https://cloudflare-dns.com/dns-query#fragment",
        "https://cloudflare-dns.com/dns-query?name=attacker.example&type=TXT",
        "https://unapproved.example/dns-query",
        "https://127.0.0.1/dns-query",
        "https://cloudflare-dns.com:444/dns-query",
        "https://cloudflare-dns.com:invalid/dns-query",
        "https://cloudflare-dns.com/dns-query\\@evil.invalid",
        "https://cloudflare-dns.com/dns-query\n",
    ],
)
def test_dns_endpoint_rejects_unsafe_destinations(settings, endpoint):
    settings.DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS = [
        "cloudflare-dns.com",
        "127.0.0.1",
    ]

    assert services._validated_dns_endpoint(endpoint) is None


def test_dns_endpoint_accepts_exact_allowlisted_https_host(settings):
    settings.DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS = ["cloudflare-dns.com"]
    endpoint = "https://cloudflare-dns.com/dns-query"

    assert services._validated_dns_endpoint(endpoint) == endpoint


def test_invalid_dns_endpoint_never_reaches_network(settings, monkeypatch):
    settings.DOMAIN_VERIFICATION_DNS_URL = "file:///etc/passwd"
    settings.DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS = ["cloudflare-dns.com"]

    def unexpected_network(*_args, **_kwargs):  # pragma: no cover - failure sentinel
        raise AssertionError("unsafe endpoint reached urlopen")

    monkeypatch.setattr(services, "urlopen", unexpected_network)

    assert services._lookup_txt_records("_starforge-verification.example.com") == ()


def test_dns_redirect_handler_fails_closed():
    assert services._NoDNSRedirects().redirect_request(None, None, 302, "Found", {}, "https://evil") is None


class _DNSResponse:
    status = 200

    def __init__(self, *, body: bytes, url: str, headers: dict[str, str]):
        self.body = body
        self.url = url
        self.headers = headers
        self.read_called = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, _limit):
        self.read_called = True
        return self.body


def test_dns_response_length_is_checked_before_materialization(settings, monkeypatch):
    settings.DOMAIN_VERIFICATION_DNS_URL = "https://cloudflare-dns.com/dns-query"
    settings.DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS = ["cloudflare-dns.com"]
    response = _DNSResponse(
        body=b"should not be read",
        url="https://cloudflare-dns.com/dns-query?name=x&type=TXT",
        headers={
            "Content-Type": "application/dns-json",
            "Content-Length": str(64 * 1024 + 1),
        },
    )
    monkeypatch.setattr(services, "urlopen", lambda *_args, **_kwargs: response)

    assert services._lookup_txt_records("_starforge-verification.example.com") == ()
    assert response.read_called is False


def test_dns_response_final_origin_and_strict_json_are_enforced(settings, monkeypatch):
    settings.DOMAIN_VERIFICATION_DNS_URL = "https://cloudflare-dns.com/dns-query"
    settings.DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS = ["cloudflare-dns.com"]
    redirected = _DNSResponse(
        body=b'  {"Status": 0}',
        url="https://metadata.invalid/latest",
        headers={"Content-Type": "application/dns-json"},
    )
    monkeypatch.setattr(services, "urlopen", lambda *_args, **_kwargs: redirected)
    assert services._lookup_txt_records("_starforge-verification.example.com") == ()
    assert redirected.read_called is False

    duplicate_keys = _DNSResponse(
        body=b'{"Status":0,"Status":0}',
        url="https://cloudflare-dns.com/dns-query?name=x&type=TXT",
        headers={"Content-Type": "application/dns-json"},
    )
    monkeypatch.setattr(services, "urlopen", lambda *_args, **_kwargs: duplicate_keys)
    assert services._lookup_txt_records("_starforge-verification.example.com") == ()

    valid_body = json.dumps(
        {
            "Status": 0,
            "Answer": [
                {"type": 16, "data": '"starforge-domain-verification=abc"'},
            ],
        }
    ).encode()
    valid = _DNSResponse(
        body=valid_body,
        url="https://cloudflare-dns.com/dns-query?name=x&type=TXT",
        headers={
            "Content-Type": "application/dns-json; charset=utf-8",
            "Content-Length": str(len(valid_body)),
        },
    )
    monkeypatch.setattr(services, "urlopen", lambda *_args, **_kwargs: valid)
    assert services._lookup_txt_records("_starforge-verification.example.com") == (
        "starforge-domain-verification=abc",
    )
