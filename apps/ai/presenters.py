"""AI response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from apps.ai.models import AIRequest, TenantAIBudget


def ai_request_to_dict(req: AIRequest, *, include_output: bool = False) -> dict:
    branch = req.branch_at_request if req.branch_at_request_id is not None else None
    department = req.department_at_request if req.department_at_request_id is not None else None
    content_available = getattr(req, "_has_content", None)
    if content_available is None:
        content_available = bool(req.protected_output and req.content_purged_at is None)
    data = {
        "id": req.id,
        "feature": req.feature,
        "status": req.status,
        "input_tokens": req.input_tokens,
        "output_tokens": req.output_tokens,
        "cache_read_tokens": req.cache_read_tokens,
        "cache_creation_tokens": req.cache_creation_tokens,
        "total_tokens": (
            req.input_tokens + req.output_tokens + req.cache_read_tokens + req.cache_creation_tokens
        ),
        "cost_microusd": req.cost_microusd,
        "created_at": req.created_at.isoformat(),
        "finished_at": req.finished_at.isoformat() if req.finished_at else None,
        "scope": {
            "branch": ({"id": req.branch_at_request_id, "name": branch.name} if branch is not None else None),
            "department": (
                {"id": req.department_at_request_id, "name": department.name}
                if department is not None
                else None
            ),
        },
        "content_available": bool(content_available),
    }
    # Restored model output can contain source-row content/PII. Collection views
    # never expose it; the detail view opts in only for the original requester or
    # an ai:manage holder.
    if include_output and req.content_purged_at is None and req.protected_output:
        data["output_text"] = req.protected_output
    return data


def budget_to_dict(budget: TenantAIBudget) -> dict:
    return {
        "daily_token_limit": budget.daily_token_limit,
        "monthly_token_limit": budget.monthly_token_limit,
        "tokens_used_today": budget.tokens_used_today,
        "tokens_used_month": budget.tokens_used_month,
        "is_enabled": budget.is_enabled,
    }
