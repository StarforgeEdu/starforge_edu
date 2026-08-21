from __future__ import annotations

import json
from collections.abc import Callable
from unittest import mock

import pytest
import requests

from infrastructure.http_client import (
    InvalidProviderEndpoint,
    InvalidProviderResponse,
    ProviderResponseTooLarge,
    request_json_limited,
    validate_https_endpoint,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/api",
        "https://provider.example.evil.invalid/api",
        "https://provider.example@127.0.0.1/api",
        "https://127.0.0.1/api",
        "https://169.254.169.254/latest/meta-data",
        "https://provider.example:444/api",
        "https://provider.example/api?target=http://127.0.0.1",
        "https://provider.example/api#token",
        "https://provider.example\\@evil.invalid/api",
    ],
)
def test_provider_endpoint_rejects_ssrf_and_origin_confusion(url: str):
    with pytest.raises(InvalidProviderEndpoint):
        validate_https_endpoint(url, allowed_hosts=["provider.example"])


def _response(
    status: int,
    body: bytes,
    *,
    content_type: str = "application/json",
    content_length: int | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    if content_length is not None:
        response.headers["Content-Length"] = str(content_length)
    response._content = body
    response._content_consumed = True
    response.url = "https://provider.example/api"
    return response


def _requester(response: requests.Response, calls: list[dict]) -> Callable[..., requests.Response]:
    def request(url: str, **kwargs) -> requests.Response:
        calls.append({"url": url, **kwargs})
        return response

    return request


def test_provider_redirect_is_not_followed_or_forwarded():
    calls: list[dict] = []
    response = _response(302, b"", content_type="text/html")
    response.headers["Location"] = "http://169.254.169.254/latest/meta-data"

    with pytest.raises(InvalidProviderResponse):
        request_json_limited(
            _requester(response, calls),
            "https://provider.example/api",
            allowed_hosts=["provider.example"],
            headers={"Authorization": "Bearer secret"},
        )

    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is False


def test_provider_response_is_bounded_by_header_and_stream():
    calls: list[dict] = []
    by_header = _response(200, b"{}", content_length=10_000)
    with pytest.raises(ProviderResponseTooLarge):
        request_json_limited(
            _requester(by_header, calls),
            "https://provider.example/api",
            allowed_hosts=["provider.example"],
            max_response_bytes=32,
        )

    streamed = _response(200, json.dumps({"value": "x" * 100}).encode())
    with pytest.raises(ProviderResponseTooLarge):
        request_json_limited(
            _requester(streamed, calls),
            "https://provider.example/api",
            allowed_hosts=["provider.example"],
            max_response_bytes=32,
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"id": 1, "id": 2}',
        b'{"amount": NaN}',
        b'{"amount": Infinity}',
        b"\xff",
    ],
)
def test_provider_json_rejects_ambiguous_or_nonstandard_values(body: bytes):
    response = _response(200, body)
    with pytest.raises(InvalidProviderResponse):
        request_json_limited(
            lambda *_args, **_kwargs: response,
            "https://provider.example/api",
            allowed_hosts=["provider.example"],
        )


def test_provider_json_accepts_small_strict_object():
    response = _response(200, b'{"ok": true}')
    assert request_json_limited(
        lambda *_args, **_kwargs: response,
        "https://provider.example/api",
        allowed_hosts=["provider.example"],
    ) == {"ok": True}


def test_cbu_fx_fetch_is_streamed_bounded_and_does_not_follow_redirects(monkeypatch):
    from decimal import Decimal

    from celery_tasks.finance_tasks import _live_cbu_rate

    calls: list[dict] = []
    response = _response(200, b'[{"Ccy":"USD","Rate":"12500.2500"}]')
    monkeypatch.setattr(requests, "get", _requester(response, calls))

    assert _live_cbu_rate() == Decimal("12500.2500")
    assert calls == [
        {
            "url": "https://cbu.uz/uz/arkhiv-kursov-valyut/json/USD/",
            "headers": {},
            "json": None,
            "data": None,
            "timeout": (3.05, 10.0),
            "allow_redirects": False,
            "stream": True,
        }
    ]


def test_cbu_fx_rejects_redirect_and_oversized_header_before_body(monkeypatch):
    from celery_tasks.finance_tasks import _live_cbu_rate

    redirect = _response(302, b"", content_type="text/html")
    redirect.headers["Location"] = "http://169.254.169.254/latest/meta-data"
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: redirect)
    with pytest.raises(InvalidProviderResponse):
        _live_cbu_rate()

    oversized = _response(200, b"[]", content_length=64 * 1024 + 1)
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: oversized)
    with pytest.raises(ProviderResponseTooLarge):
        _live_cbu_rate()

    wrong_currency = _response(200, b'[{"Ccy":"EUR","Rate":"14000.0000"}]')
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: wrong_currency)
    with pytest.raises(ValueError, match="requested USD currency"):
        _live_cbu_rate()


def test_soliq_rejects_unsafe_credential_before_any_request(monkeypatch):
    from infrastructure.fiscal.soliq_client import SoliqClient

    post = mock.Mock(side_effect=AssertionError("network reached"))
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(ValueError, match="Soliq credential"):
        SoliqClient(
            base_url="https://soliq.example/api",
            token="secret\nsmuggled",
            allowed_hosts=("soliq.example",),
        )

    post.assert_not_called()
