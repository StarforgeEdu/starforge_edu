"""Backend API contract: every response is JSON — errors use the ONE flat
``{"success": false, "code", "message"}`` envelope (Django's own handlers included),
and there is no HTML browsable-API UI. Mobile/web clients must never receive an HTML
page (a template or a DEBUG error page)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def test_unmatched_url_is_json_404_not_html(tenant_a, client_for):
    resp = client_for(tenant_a).get("/api/v1/definitely-not-a-real-endpoint/")
    assert resp.status_code == 404
    assert resp["Content-Type"].startswith("application/json")
    body = resp.json()
    # Same flat envelope Django's handlers now emit as the layered views — a client
    # branches on top-level success/code for an unmatched-URL 404 exactly like any error.
    assert body["success"] is False
    assert body["code"] == "not_found"


def test_debug_html_error_rewrite_fixes_content_length(tenant_a, client_for, settings):
    """Under DEBUG, Django serves an HTML technical-404/500 page; JsonErrorResponseMiddleware
    rewrites it to the short JSON envelope and MUST re-stamp Content-Length. Otherwise the
    response declares the ORIGINAL (longer) HTML length but sends the short JSON body — HTTP/2
    aborts the stream (ERR_HTTP2_PROTOCOL_ERROR) and HTTP/1.1 clients hang. Regression for the
    root-path outage on the DEBUG test server."""
    settings.DEBUG = True
    resp = client_for(tenant_a).get("/definitely-not-a-real-root-endpoint/")
    assert resp.status_code == 404
    assert resp["Content-Type"].startswith("application/json")
    # The declared length MUST equal the actual body length (the bug: it was the HTML length).
    assert int(resp["Content-Length"]) == len(resp.content)
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "not_found"


def test_unauthenticated_request_is_json_401(tenant_a, client_for):
    resp = client_for(tenant_a).get("/api/v1/users/me/")
    assert resp.status_code == 401
    assert resp["Content-Type"].startswith("application/json")
    assert "code" in resp.json()  # /me is layered: {"success": false, "code": ...}


def test_method_not_allowed_is_json(tenant_a, user_in, as_user):
    # DELETE on a collection that doesn't allow it -> JSON 405, never an HTML page.
    client = as_user(tenant_a, user_in(tenant_a, roles=["teacher"]))
    resp = client.delete("/api/v1/users/me/")
    assert resp.status_code in (403, 405)
    assert resp["Content-Type"].startswith("application/json")


def test_no_browsable_api_html_ui(tenant_a, user_in, as_user):
    """Even when a browser asks for HTML (Accept: text/html), the API answers JSON —
    the DRF browsable-API HTML interface is disabled (this is a backend API)."""
    client = as_user(tenant_a, user_in(tenant_a, roles=["teacher"]))
    resp = client.get("/api/v1/users/me/", HTTP_ACCEPT="text/html")
    assert "text/html" not in resp["Content-Type"]
