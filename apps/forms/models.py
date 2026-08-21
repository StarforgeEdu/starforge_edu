"""Forms / surveys engine (F3-3) — a paper-killing dynamic form builder.

A manager or teacher builds a `Form` out of ordered `FormField`s, publishes it,
and recipients submit a `FormResponse` made of `FormAnswer`s (one per field). A
form can be anonymous (the respondent is not recorded) and, by default, accepts
one response per respondent. The builder reads responses + an aggregate summary.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Form(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")
        CLOSED = "closed", _("Closed")

    class CreatorAttributionStatus(models.TextChoices):
        CAPTURED = "captured", _("Captured")
        RESOLVED = "resolved", _("Resolved from legacy data")
        QUARANTINED = "quarantined", _("Quarantined")

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    is_anonymous = models.BooleanField(default=False)
    # False (default) = one response per respondent; True = respondents may submit
    # repeatedly. Anonymous forms are always effectively multi (no identity to dedupe).
    allow_multiple = models.BooleanField(default=False)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="forms"
    )
    # F3-2: optional AUDIENCE targeting. Empty both = an open form (anyone in the branch may
    # respond, the original behaviour). Non-empty = the form is TARGETED at those roles
    # and/or specific users — it then shows as a "to-fill" warning on their dashboard until
    # they submit. `audience_roles` is a list of Role values; `audience_user_ids` a list of
    # user ids (the union is the target set).
    audience_roles = models.JSONField(default=list, blank=True)
    # Immutable role-native targets resolved from the legacy public ``user_id``
    # selector. A bridge User can back several accounts, so authorization reads this
    # principal pair and never treats ``audience_user_ids`` as sufficient identity.
    audience_principals = models.JSONField(default=list, blank=True)
    audience_user_ids = models.JSONField(default=list, blank=True)
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by_principal_kind = models.CharField(max_length=16, blank=True)
    created_by_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_by_attribution_status = models.CharField(
        max_length=16,
        choices=CreatorAttributionStatus.choices,
        default=CreatorAttributionStatus.QUARANTINED,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status", "created_at"))]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(opens_at__isnull=True)
                    | models.Q(closes_at__isnull=True)
                    | models.Q(closes_at__gt=models.F("opens_at"))
                ),
                name="form_close_after_open",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(created_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(created_by_principal_id__isnull=False)
                        & models.Q(created_by_attribution_status__in=("captured", "resolved"))
                    )
                    | models.Q(
                        created_by_principal_kind="",
                        created_by_principal_id__isnull=True,
                        created_by_attribution_status="quarantined",
                    )
                ),
                name="form_creator_principal_pair",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} ({self.status})"


class FormField(models.Model):
    class FieldType(models.TextChoices):
        TEXT = "text", _("Short text")
        TEXTAREA = "textarea", _("Long text")
        NUMBER = "number", _("Number")
        BOOLEAN = "boolean", _("Yes / no")
        SINGLE_CHOICE = "single_choice", _("Single choice")
        MULTI_CHOICE = "multi_choice", _("Multiple choice")
        RATING = "rating", _("Rating (1–5)")
        DATE = "date", _("Date")

    CHOICE_TYPES = (FieldType.SINGLE_CHOICE, FieldType.MULTI_CHOICE)

    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name="fields")
    label = models.CharField(max_length=255)
    field_type = models.CharField(max_length=16, choices=FieldType.choices)
    required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    options = models.JSONField(default=list, blank=True)  # [str] for choice types
    help_text = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("order", "id")
        indexes = [models.Index(fields=("form", "order"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.form_id}:{self.label}"


class FormResponse(models.Model):
    class AttributionStatus(models.TextChoices):
        CAPTURED = "captured", _("Captured")
        RESOLVED = "resolved", _("Resolved from legacy data")
        ANONYMOUS = "anonymous", _("Anonymous")
        UNRESOLVED = "unresolved", _("Needs review")
        CONFLICTING = "conflicting", _("Conflicting legacy identity")
        QUARANTINED = "quarantined", _("Quarantined")

    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name="responses")
    # Null when the form is anonymous (or the respondent row was deleted).
    respondent = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="form_responses"
    )
    respondent_principal_kind = models.CharField(max_length=16, blank=True)
    respondent_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    respondent_attribution_status = models.CharField(
        max_length=16,
        choices=AttributionStatus.choices,
        default=AttributionStatus.QUARANTINED,
    )
    # Set by the service to the respondent id ONLY when the form dedupes (not
    # anonymous, not allow_multiple); blank otherwise. The partial unique constraint
    # below is the race-safe enforcement of one-response-per-respondent — without
    # blocking the repeat responses that anonymous / multi forms allow.
    dedupe_token = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("form", "created_at")),
            models.Index(
                fields=("form", "respondent_principal_kind", "respondent_principal_id"),
                name="form_response_principal_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("form", "dedupe_token"),
                condition=~models.Q(dedupe_token=""),
                name="one_response_per_dedupe_token",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        respondent__isnull=True,
                        respondent_principal_kind="",
                        respondent_principal_id__isnull=True,
                        respondent_attribution_status="anonymous",
                    )
                    | (
                        models.Q(respondent_principal_kind__in=("student", "teacher", "parent", "staff"))
                        & models.Q(respondent_principal_id__isnull=False)
                        & models.Q(respondent_attribution_status__in=("captured", "resolved"))
                    )
                    | (
                        models.Q(respondent__isnull=False)
                        & models.Q(respondent_principal_kind="")
                        & models.Q(respondent_principal_id__isnull=True)
                        & models.Q(
                            respondent_attribution_status__in=(
                                "unresolved",
                                "conflicting",
                                "quarantined",
                            )
                        )
                    )
                ),
                name="form_response_principal_pair",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"response#{self.pk}:form#{self.form_id}"


class FormAnswer(models.Model):
    response = models.ForeignKey(FormResponse, on_delete=models.CASCADE, related_name="answers")
    field = models.ForeignKey(FormField, on_delete=models.CASCADE, related_name="answers")
    # Flexible per field type: str / number / bool / ISO-date str / [str] for multi.
    value = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("response", "field"), name="one_answer_per_field_per_response"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"answer#{self.pk}:field#{self.field_id}"
