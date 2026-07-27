"""Printing URLConf (D4-LD) — layered plain views."""

from __future__ import annotations

from django.urls import path

from apps.printing.views.v1.printing_views import (
    agent_claim_view,
    agent_detail_view,
    agent_job_status_view,
    agent_revoke_view,
    agents_collection_view,
    job_detail_view,
    jobs_collection_view,
    printer_detail_view,
    printers_collection_view,
)

urlpatterns = [
    # Agent surface (BranchAgent token) — specific paths first.
    path("agent/claim/", agent_claim_view, name="printing-agent-claim"),
    path("agent/jobs/<int:job_id>/status/", agent_job_status_view, name="printing-agent-status"),
    # Staff: jobs.
    path("jobs/", jobs_collection_view, name="printing-jobs-list"),
    path("jobs/<int:pk>/", job_detail_view, name="printing-jobs-detail"),
    # Staff: printers.
    path("printers/", printers_collection_view, name="printing-printers-list"),
    path("printers/<int:pk>/", printer_detail_view, name="printing-printers-detail"),
    # Staff: branch agents.
    path("agents/", agents_collection_view, name="printing-agents-list"),
    path("agents/<int:pk>/", agent_detail_view, name="printing-agents-detail"),
    path("agents/<int:pk>/revoke/", agent_revoke_view, name="printing-agents-revoke"),
]
