"""Small, fail-closed helpers for outbound provider HTTP calls.

Provider endpoints are configuration, but they are still an external trust
boundary: a typo or compromised setting must not turn a worker into an SSRF
client or make it forward a bearer credential through a redirect.  Callers
therefore supply an exact hostname allowlist, redirects stay disabled, and
responses are read under a hard byte ceiling before JSON decoding.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

DEFAULT_PROVIDER_TIMEOUT = (3.05, 15.0)
DEFAULT_PROVIDER_RESPONSE_BYTES = 128 * 1024


class InvalidProviderEndpoint(ValueError):
    """The configured provider URL is outside the approved HTTPS origin."""


class InvalidProviderResponse(requests.RequestException):
    """The provider returned a response that is unsafe or not valid JSON."""


class ProviderResponseTooLarge(InvalidProviderResponse):
    """The provider response exceeded the bounded integration contract."""


def _hostname(value: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    if not value:
        return None
    try:
        normalized = value.encode("idna").decode("ascii")
        ipaddress.ip_address(normalized)
    except UnicodeError:
        return None
    except ValueError:
        return normalized
    # Provider allowlists name TLS hosts, never literal IP addresses.  This also
    # blocks the obvious loopback/link-local/cloud-metadata SSRF forms.
    return None


def normalized_allowed_hosts(values: Iterable[str]) -> frozenset[str]:
    hosts = {_hostname(value) for value in values if isinstance(value, str)}
    hosts.discard(None)
    return frozenset(host for host in hosts if host is not None)


def validate_https_endpoint(url: str, *, allowed_hosts: Iterable[str]) -> str:
    """Validate and return a provider URL on one exact allowlisted HTTPS host.

    Paths are allowed because several providers publish an API base path.
    Credentials, query strings, fragments, non-default ports, control
    characters, backslashes, IP literals, and protocol-relative forms are not.
    """

    if (
        not isinstance(url, str)
        or not url
        or len(url) > 2048
        or "\\" in url
        or any(ord(char) <= 32 or ord(char) == 127 for char in url)
    ):
        raise InvalidProviderEndpoint("Provider URL is invalid.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise InvalidProviderEndpoint("Provider URL is invalid.") from exc
    host = _hostname(parsed.hostname or "")
    approved = normalized_allowed_hosts(allowed_hosts)
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or host is None
        or host not in approved
    ):
        raise InvalidProviderEndpoint("Provider URL is outside the approved HTTPS origins.")
    # Preserve the configured path exactly. Some provider routes distinguish a
    # trailing slash and would otherwise respond with a redirect, which callers
    # correctly refuse to follow.
    return url


def _reject_duplicate_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"Invalid JSON numeric constant: {value}")


def strict_json_loads(raw: bytes) -> Any:
    """Decode strict UTF-8 JSON, rejecting duplicate keys and NaN/Infinity."""

    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_key,
            parse_constant=_reject_non_finite,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidProviderResponse("Provider returned invalid JSON.") from exc


def request_json_limited(
    request_callable: Callable[..., requests.Response],
    url: str,
    *,
    allowed_hosts: Iterable[str],
    timeout: tuple[float, float] = DEFAULT_PROVIDER_TIMEOUT,
    max_response_bytes: int = DEFAULT_PROVIDER_RESPONSE_BYTES,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
    form_body: Mapping[str, Any] | None = None,
) -> Any:
    """POST to an approved provider and decode a small JSON response.

    The callable is injected so callers and unit tests can use the same helper
    without a global session.  Redirects are never followed: even same-origin
    redirects can change a signed/idempotent POST into a GET, while cross-origin
    redirects risk forwarding credentials.
    """

    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    endpoint = validate_https_endpoint(url, allowed_hosts=allowed_hosts)
    response = request_callable(
        endpoint,
        headers=dict(headers or {}),
        json=dict(json_body) if json_body is not None else None,
        data=dict(form_body) if form_body is not None else None,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    try:
        if 300 <= response.status_code < 400:
            raise InvalidProviderResponse("Provider redirects are not allowed.", response=response)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            # Real ``requests.Response.raise_for_status`` attaches the response;
            # retain that contract for injected transports as well so callers can
            # distinguish an expired credential (401) from other failures.
            if exc.response is None:
                exc.response = response
            raise
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError) as exc:
                raise InvalidProviderResponse(
                    "Provider returned an invalid Content-Length.", response=response
                ) from exc
            if content_length < 0 or content_length > max_response_bytes:
                raise ProviderResponseTooLarge(
                    "Provider response exceeded the configured limit.", response=response
                )

        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/json", "text/json"} and not content_type.endswith("+json"):
            raise InvalidProviderResponse("Provider response is not JSON.", response=response)

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_response_bytes:
                raise ProviderResponseTooLarge(
                    "Provider response exceeded the configured limit.", response=response
                )
            chunks.append(chunk)
        return strict_json_loads(b"".join(chunks))
    finally:
        response.close()
