from django.contrib import admin

from apps.ai.models import AIPrompt, AIRequest, TenantAIBudget
from core.admin_mixins import ReadOnlyAdmin


@admin.register(TenantAIBudget)
class TenantAIBudgetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "daily_token_limit",
        "monthly_token_limit",
        "tokens_used_today",
        "tokens_used_month",
        "is_enabled",
        "updated_at",
    )
    list_filter = ("is_enabled",)


@admin.register(AIPrompt)
class AIPromptAdmin(admin.ModelAdmin):
    list_display = ("feature", "version", "is_active", "max_output_tokens", "effort", "token_cost_cap")
    list_filter = ("feature", "is_active")
    search_fields = ("feature",)


@admin.register(AIRequest)
class AIRequestAdmin(ReadOnlyAdmin):
    """One AI feature invocation — a system-written request LOG (queued/run via
    Celery, tokens/cost reconciled by the budget service), so view-only here
    (matches the audit/ledger pattern)."""

    list_display = (
        "id",
        "feature",
        "status",
        "requested_by",
        "input_tokens",
        "output_tokens",
        "cost_microusd",
        "created_at",
    )
    list_filter = ("feature", "status")
    search_fields = ("idempotency_key", "source_app")
    autocomplete_fields = ("prompt", "requested_by")
    list_select_related = ("requested_by",)
    # redaction_map is the decrypted PII the redaction subsystem exists to protect,
    # and output_text can carry un-redacted model output. readonly_fields still
    # RENDERS them on the change page, so exclude them from the form entirely; they
    # remain on the model only for programmatic restore().
    exclude = ("redaction_map", "output_text")
    readonly_fields = ("idempotency_key", "celery_task_id")
    date_hierarchy = "created_at"
