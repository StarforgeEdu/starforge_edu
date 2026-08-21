"""DB-free stable-principal throttling for authenticated print daemons."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import JsonResponse
from django.test import RequestFactory, override_settings

from apps.printing.agent_auth import require_branch_agent
from apps.printing.models import BranchAgent
from core.exceptions import ServiceUnavailableException


@override_settings(API_RATELIMIT_AGENT="7/min")
def test_valid_agent_is_charged_to_tenant_device_bucket(monkeypatch):
    agent = BranchAgent(pk=73, branch_id=5)
    monkeypatch.setattr(
        "apps.printing.authentication.BranchAgentAuthentication.authenticate",
        lambda _self, _request: (AnonymousUser(), agent),
    )
    calls: list[dict] = []
    monkeypatch.setattr("core.ratelimit.check_rate", lambda **kwargs: calls.append(kwargs))

    protected = require_branch_agent(lambda _request: JsonResponse({"ok": True}))
    request = RequestFactory().post(
        "/api/v1/printing/agent/claim/",
        HTTP_AUTHORIZATION=f"Agent {'a' * 64}",
    )

    assert protected(request).status_code == 200
    assert len(calls) == 1
    assert calls[0]["scope"] == "api_branch_agent"
    assert calls[0]["key"].endswith(":73")
    assert (calls[0]["limit"], calls[0]["window"]) == (7, 60)


@override_settings(API_RATELIMIT_AGENT="0/min")
def test_invalid_agent_rate_configuration_fails_closed(monkeypatch):
    agent = BranchAgent(pk=73, branch_id=5)
    monkeypatch.setattr(
        "apps.printing.authentication.BranchAgentAuthentication.authenticate",
        lambda _self, _request: (AnonymousUser(), agent),
    )
    protected = require_branch_agent(lambda _request: JsonResponse({"ok": True}))
    request = RequestFactory().post(
        "/api/v1/printing/agent/claim/",
        HTTP_AUTHORIZATION=f"Agent {'a' * 64}",
    )

    with pytest.raises(ServiceUnavailableException) as caught:
        protected(request)

    assert caught.value.code == "temporarily_unavailable"
