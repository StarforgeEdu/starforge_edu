"""Forms engine services (F3-3): build → publish → submit → summarize.

Domain functions live here (imported by the layered service in ``services/v1`` AND by
celery `run_form_analysis` -> `form_summary`). All writes are keyword-only and
`@transaction.atomic`; submission validates every answer against its field's
type/options so a malformed response is a clean 400, never a 500 or a junk row.
"""

from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.forms.models import Form, FormAnswer, FormField, FormResponse
from core.exceptions import ConflictException, NotFoundException, UnprocessableEntity, ValidationException
from core.role_principals import RolePrincipal

_FT = FormField.FieldType
MAX_FORM_DESCRIPTION_CHARS = 20_000
MAX_FORM_FIELDS = 100
MAX_CHOICE_OPTIONS = 100
MAX_CHOICE_OPTION_CHARS = 200
MAX_SHORT_TEXT_CHARS = 1_000
MAX_LONG_TEXT_CHARS = 20_000
MAX_NUMBER_ABS = 1_000_000_000_000


def _locked_form(form: Form) -> Form:
    locked = Form.objects.select_for_update().filter(pk=form.pk).first()
    if locked is None:
        raise NotFoundException(_("Form not found."), code="not_found")
    return locked


@transaction.atomic
def create_form(
    *,
    title: str,
    created_by=None,
    created_by_principal: RolePrincipal | None = None,
    **kwargs,
) -> Form:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValidationException(
            _("Invalid form title."),
            code="validation_error",
            fields={"title": [_("Use between 1 and 200 characters.")]},
        )
    description = kwargs.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_FORM_DESCRIPTION_CHARS:
        raise ValidationException(
            _("The form description is too long."),
            code="validation_error",
            fields={
                "description": [
                    _("Must be at most %(count)s characters.") % {"count": MAX_FORM_DESCRIPTION_CHARS}
                ]
            },
        )
    _validate_window(kwargs.get("opens_at"), kwargs.get("closes_at"))
    if created_by is None:
        if created_by_principal is not None:
            raise ValidationException(
                _("Invalid form creator attribution."),
                code="validation_error",
                fields={"created_by": [_("Clear the creator and its role attribution together.")]},
            )
        creator_fields: dict[str, Any] = {
            "created_by_attribution_status": Form.CreatorAttributionStatus.QUARANTINED,
        }
    else:
        if created_by_principal is None:
            from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

            created_by_principal = resolve_unambiguous_user_principal(
                created_by.pk,
                allowed_kinds=STAFF_PRINCIPAL_KINDS,
                field="created_by",
                message=_("The form creator does not identify one active staff role account."),
            )
        if (
            created_by_principal.kind not in {"staff", "teacher"}
            or created_by_principal.user_id != created_by.pk
        ):
            raise ValidationException(
                _("Invalid form creator attribution."),
                code="validation_error",
                fields={"created_by": [_("Choose the active creator role account.")]},
            )
        creator_fields = {
            "created_by_principal_kind": created_by_principal.kind,
            "created_by_principal_id": created_by_principal.principal_id,
            "created_by_attribution_status": Form.CreatorAttributionStatus.CAPTURED,
        }
    return Form.objects.create(title=title, created_by=created_by, **creator_fields, **kwargs)


def _validate_window(opens_at, closes_at) -> None:
    if opens_at is not None and closes_at is not None and closes_at <= opens_at:
        raise ValidationException(
            _("The close time must be after the open time."),
            code="validation_error",
            fields={"closes_at": [_("Must be after opens_at.")]},
        )


@transaction.atomic
def add_field(
    *,
    form: Form,
    label: str,
    field_type: str,
    required: bool = False,
    order: int | None = None,
    options: list[str] | None = None,
    help_text: str = "",
) -> FormField:
    """Append a field. Only a DRAFT form can be edited — changing fields under a
    live form would orphan already-submitted answers."""
    form = _locked_form(form)
    if form.status != Form.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft form can be edited."), code="form_not_draft")
    if form.fields.count() >= MAX_FORM_FIELDS:
        raise ValidationException(
            _("This form already has the maximum number of fields."),
            code="validation_error",
            fields={"form_fields": [_("At most %(count)s fields are allowed.") % {"count": MAX_FORM_FIELDS}]},
        )
    if not isinstance(label, str) or not isinstance(help_text, str):
        raise ValidationException(
            _("Field text must be text."),
            code="validation_error",
            fields={"label": [_("Use text for labels and help text.")]},
        )
    if field_type not in FormField.FieldType.values:
        raise ValidationException(
            _("Invalid field type."),
            code="validation_error",
            fields={"field_type": [_("Choose a supported field type.")]},
        )
    label = label.strip()
    help_text = help_text.strip()
    if not label:
        raise ValidationException(
            _("A field label is required."),
            code="validation_error",
            fields={"label": ["This field may not be blank."]},
        )
    if len(label) > 255 or len(help_text) > 255:
        raise ValidationException(
            _("Field text is too long."),
            code="validation_error",
            fields={"label": [_("Labels and help text must be at most 255 characters.")]},
        )
    if order is not None and order < 0:
        raise ValidationException(
            _("Field order cannot be negative."),
            code="validation_error",
            fields={"order": ["Must be zero or greater."]},
        )
    if options is not None and not isinstance(options, list):
        raise ValidationException(
            _("Options must be a list."),
            code="validation_error",
            fields={"options": [_("Use a list of choices.")]},
        )
    options = list(options or [])
    if len(options) > MAX_CHOICE_OPTIONS:
        raise ValidationException(
            _("Too many choices."),
            code="validation_error",
            fields={"options": [_("At most %(count)s choices are allowed.") % {"count": MAX_CHOICE_OPTIONS}]},
        )
    if field_type in FormField.CHOICE_TYPES:
        if len(options) < 1:
            raise ValidationException(
                _("A choice field needs at least one option."), code="choice_needs_options"
            )
        if any(not isinstance(o, str) or not o.strip() for o in options):
            raise ValidationException(_("Options must be non-empty text."), code="invalid_options")
        options = [o.strip() for o in options]
        if any(len(option) > MAX_CHOICE_OPTION_CHARS for option in options):
            raise ValidationException(
                _("A choice is too long."),
                code="validation_error",
                fields={
                    "options": [
                        _("Each choice must be at most %(count)s characters.")
                        % {"count": MAX_CHOICE_OPTION_CHARS}
                    ]
                },
            )
        if len(set(options)) != len(options):
            raise ValidationException(_("Options must be unique."), code="duplicate_options")
    elif options:
        raise ValidationException(
            _("Only choice fields may define options."),
            code="invalid_options",
            fields={"options": ["Options are only valid for choice fields."]},
        )
    if order is None:
        last = form.fields.order_by("-order").first()
        order = (last.order + 1) if last else 0
    return FormField.objects.create(
        form=form,
        label=label,
        field_type=field_type,
        required=required,
        order=order,
        options=options,
        help_text=help_text,
    )


@transaction.atomic
def update_form(*, form: Form, **changes) -> Form:
    """Edit form metadata. Draft-only — changing anonymity / windows after responses
    exist would misrepresent data already collected."""
    form = _locked_form(form)
    if form.status != Form.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft form can be edited."), code="form_not_draft")
    allowed = {
        "title",
        "description",
        "is_anonymous",
        "allow_multiple",
        "opens_at",
        "closes_at",
        "audience_roles",
        "audience_user_ids",
        "audience_principals",
    }
    _validate_window(changes.get("opens_at", form.opens_at), changes.get("closes_at", form.closes_at))
    description = changes.get("description", form.description)
    if not isinstance(description, str) or len(description) > MAX_FORM_DESCRIPTION_CHARS:
        raise ValidationException(
            _("The form description is too long."),
            code="validation_error",
            fields={
                "description": [
                    _("Must be at most %(count)s characters.") % {"count": MAX_FORM_DESCRIPTION_CHARS}
                ]
            },
        )
    title = changes.get("title", form.title)
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValidationException(
            _("Invalid form title."),
            code="validation_error",
            fields={"title": [_("Use between 1 and 200 characters.")]},
        )
    changed_fields: list[str] = []
    for key, value in changes.items():
        if key in allowed:
            setattr(form, key, value)
            changed_fields.append(key)
    if changed_fields:
        form.save(update_fields=[*dict.fromkeys(changed_fields), "updated_at"])
    return form


@transaction.atomic
def publish_form(*, form: Form) -> Form:
    form = _locked_form(form)
    if form.status == Form.Status.PUBLISHED:
        return form
    if form.status == Form.Status.CLOSED:
        raise UnprocessableEntity(_("A closed form cannot be re-published."), code="form_closed")
    if not form.fields.exists():
        raise UnprocessableEntity(_("Add at least one field before publishing."), code="form_has_no_fields")
    form.status = Form.Status.PUBLISHED
    form.published_at = timezone.now()
    form.save(update_fields=["status", "published_at", "updated_at"])
    return form


@transaction.atomic
def close_form(*, form: Form) -> Form:
    form = _locked_form(form)
    if form.status == Form.Status.CLOSED:
        return form
    if form.status != Form.Status.PUBLISHED:
        raise UnprocessableEntity(_("Only a published form can be closed."), code="form_not_published")
    form.status = Form.Status.CLOSED
    form.closed_at = timezone.now()
    form.save(update_fields=["status", "closed_at", "updated_at"])
    return form


@transaction.atomic
def delete_form(*, form: Form) -> None:
    """Hard-delete a form. Only a DRAFT may be deleted — a published or closed form
    carries collected responses (CASCADE) and is never erased unilaterally."""
    form = _locked_form(form)
    if form.status != Form.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft form can be deleted."), code="form_not_draft")
    form.delete()


def _is_empty(val: Any) -> bool:
    # None / "" / [] are "not answered"; False and 0 are real answers.
    return val is None or val == "" or val == []


def _validate_field_value(field: FormField, val: Any) -> Any:
    """Coerce/validate one answer for `field`; raise ValidationException (with the
    offending field id) on a type/option mismatch."""

    def bad(msg, code: str):
        return ValidationException(msg, code=code, fields={"field": [str(field.id)]})

    ft = field.field_type
    if ft in (_FT.TEXT, _FT.TEXTAREA):
        if not isinstance(val, str):
            raise bad(_("Expected text."), "field_type_mismatch")
        limit = MAX_SHORT_TEXT_CHARS if ft == _FT.TEXT else MAX_LONG_TEXT_CHARS
        if len(val) > limit:
            raise bad(
                _("Text must be at most %(count)s characters.") % {"count": limit},
                "field_text_too_long",
            )
        return val
    if ft == _FT.NUMBER:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise bad(_("Expected a number."), "field_type_mismatch")
        if isinstance(val, float) and not isfinite(val):
            raise bad(_("Expected a finite number."), "field_number_not_finite")
        if abs(val) > MAX_NUMBER_ABS:
            raise bad(_("The number is outside the supported range."), "field_number_range")
        return val
    if ft == _FT.RATING:
        if isinstance(val, bool) or not isinstance(val, int) or not (1 <= val <= 5):
            raise bad(_("Expected a rating from 1 to 5."), "field_rating_range")
        return val
    if ft == _FT.BOOLEAN:
        if not isinstance(val, bool):
            raise bad(_("Expected yes/no."), "field_type_mismatch")
        return val
    if ft == _FT.DATE:
        if not isinstance(val, str):
            raise bad(_("Expected an ISO date."), "field_type_mismatch")
        try:
            date.fromisoformat(val)
        except ValueError:
            raise bad(_("Expected an ISO date (YYYY-MM-DD)."), "field_date_invalid") from None
        return val
    if ft == _FT.SINGLE_CHOICE:
        if val not in field.options:
            raise bad(_("Not one of the allowed options."), "field_choice_invalid")
        return val
    if ft == _FT.MULTI_CHOICE:
        if (
            not isinstance(val, list)
            or len(val) > MAX_CHOICE_OPTIONS
            or any(not isinstance(opt, str) or opt not in field.options for opt in val)
        ):
            raise bad(_("One or more selections are not allowed options."), "field_choice_invalid")
        if len(set(val)) != len(val):
            raise bad(_("The same option was selected more than once."), "field_choice_duplicate")
        return val
    raise bad(_("Unknown field type."), "field_type_unknown")  # pragma: no cover


@transaction.atomic
def submit_response(
    *,
    form: Form,
    respondent,
    answers: list[dict],
    respondent_principal_kind: str | None = None,
    respondent_principal_id: int | None = None,
) -> FormResponse:
    """Validate and persist a response. `answers` is `[{"field": <id>, "value": <v>}]`.
    Anonymous forms drop the respondent; non-anonymous single-response forms reject
    a second submission from the same person."""
    if (
        not isinstance(answers, list)
        or len(answers) > MAX_FORM_FIELDS
        or any(not isinstance(answer, dict) for answer in answers)
    ):
        raise ValidationException(
            _("Invalid answers."),
            code="validation_error",
            fields={"answers": [_("Submit at most 100 answer objects.")]},
        )

    # Serialize submission against close/publish and field edits. The caller's
    # form instance may have been fetched before a concurrent close; re-reading
    # under the same row lock used by every lifecycle mutation prevents a stale
    # published instance from accepting a response after close commits.
    form = _locked_form(form)
    now = timezone.now()
    if form.status != Form.Status.PUBLISHED:
        raise UnprocessableEntity(_("This form is not open for responses."), code="form_not_open")
    if form.opens_at and now < form.opens_at:
        raise UnprocessableEntity(_("This form is not open yet."), code="form_not_open")
    if form.closes_at and now > form.closes_at:
        raise UnprocessableEntity(_("This form has closed."), code="form_closed")

    real_respondent = None if form.is_anonymous else respondent
    principal_kind = ""
    principal_id = None
    if real_respondent is not None:
        from core.role_principals import (
            resolve_unambiguous_user_principal,
            validate_role_principal,
        )

        if respondent_principal_kind is None or respondent_principal_id is None:
            principal = resolve_unambiguous_user_principal(
                real_respondent.pk,
                field="respondent",
                message=_("The respondent does not identify one role account."),
            )
        else:
            principal = validate_role_principal(
                kind=respondent_principal_kind,
                principal_id=respondent_principal_id,
                user_id=real_respondent.pk,
                field="respondent",
            )
        principal_kind = principal.kind
        principal_id = principal.principal_id

    dedupe_token = (
        f"{principal_kind}:{principal_id}" if real_respondent is not None and not form.allow_multiple else ""
    )
    if dedupe_token and FormResponse.objects.filter(form=form, dedupe_token=dedupe_token).exists():
        raise ConflictException(_("You have already responded to this form."), code="already_responded")

    fields = list(form.fields.all())
    fields_by_id = {f.id: f for f in fields}
    provided: dict[int, Any] = {}
    for item in answers:
        fid = item.get("field")
        if fid not in fields_by_id:
            raise ValidationException(
                _("Unknown field in submission."), code="unknown_field", fields={"field": [str(fid)]}
            )
        if fid in provided:
            raise ValidationException(
                _("Two answers were given for the same field."),
                code="duplicate_field",
                fields={"field": [str(fid)]},
            )
        provided[fid] = item.get("value")

    cleaned: dict[int, Any] = {}
    for field in fields:
        val = provided.get(field.id)
        if _is_empty(val):
            if field.required:
                raise ValidationException(
                    _("This field is required."), code="field_required", fields={"field": [str(field.id)]}
                )
            continue
        cleaned[field.id] = _validate_field_value(field, val)

    # Dedupe only when there is an identity AND the form is single-response; the
    # partial unique constraint then guards against a concurrent double-submit —
    # which surfaces here as a clean 409 (savepoint so the IntegrityError is
    # catchable without poisoning the outer transaction) rather than a 500.
    try:
        with transaction.atomic():
            response = FormResponse.objects.create(
                form=form,
                respondent=real_respondent,
                respondent_principal_kind=principal_kind,
                respondent_principal_id=principal_id,
                respondent_attribution_status=(
                    FormResponse.AttributionStatus.ANONYMOUS
                    if real_respondent is None
                    else FormResponse.AttributionStatus.CAPTURED
                ),
                dedupe_token=dedupe_token,
            )
            FormAnswer.objects.bulk_create(
                [
                    FormAnswer(response=response, field=fields_by_id[fid], value=val)
                    for fid, val in cleaned.items()
                ]
            )
    except IntegrityError:
        raise ConflictException(
            _("You have already responded to this form."), code="already_responded"
        ) from None
    return response


def form_summary(form: Form) -> dict:
    """Aggregate responses per field — choice tallies, rating/number stats, yes/no
    counts — for the manager's analysis view (F3-4 builds charts on top of this)."""
    response_count = form.responses.count()
    fields = list(form.fields.all())
    fields_by_id = {field.id: field for field in fields}
    summaries: dict[int, dict[str, Any]] = {}
    numeric_state: dict[int, dict[str, Any]] = {}

    for field in fields:
        summary: dict[str, Any] = {"answered": 0}
        if field.field_type in FormField.CHOICE_TYPES:
            summary["counts"] = {option: 0 for option in field.options}
        elif field.field_type == _FT.BOOLEAN:
            summary.update({"true": 0, "false": 0})
        elif field.field_type in (_FT.NUMBER, _FT.RATING):
            numeric_state[field.id] = {"count": 0, "sum": 0, "min": None, "max": None}
        summaries[field.id] = summary

    # Stream all answers in one query. The previous implementation issued one
    # query per field and materialised an unbounded list for every field. These
    # incremental states keep memory proportional to fields/options instead of
    # the number of responses.
    answer_rows = FormAnswer.objects.filter(field_id__in=fields_by_id).values_list("field_id", "value")
    for field_id, value in answer_rows.iterator(chunk_size=2000):
        field = fields_by_id[field_id]
        summary = summaries[field_id]
        summary["answered"] += 1
        if field.field_type in FormField.CHOICE_TYPES:
            counts = summary["counts"]
            for picked in value if isinstance(value, list) else [value]:
                if picked in counts:
                    counts[picked] += 1
        elif field.field_type in (_FT.NUMBER, _FT.RATING):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                state = numeric_state[field_id]
                state["count"] += 1
                state["sum"] += value
                state["min"] = value if state["min"] is None else min(state["min"], value)
                state["max"] = value if state["max"] is None else max(state["max"], value)
        elif field.field_type == _FT.BOOLEAN:
            if value is True:
                summary["true"] += 1
            elif value is False:
                summary["false"] += 1

    fields_out = []
    for field in fields:
        summary = summaries[field.id]
        numeric = numeric_state.get(field.id)
        if numeric and numeric["count"]:
            summary.update(
                {
                    "avg": round(numeric["sum"] / numeric["count"], 2),
                    "min": numeric["min"],
                    "max": numeric["max"],
                }
            )
        fields_out.append(
            {"field": field.id, "label": field.label, "field_type": field.field_type, "summary": summary}
        )
    return {"response_count": response_count, "fields": fields_out}


# ---------------------------------------------------------------------------
# AI response analysis (F3-4) — reuses the apps.ai budget/redaction pipeline
# ---------------------------------------------------------------------------


def request_form_analysis(
    *,
    form: Form,
    requested_by,
    requested_principal: RolePrincipal,
):
    """Ask the AI to analyze this form's responses (narrative + key takeaways). The
    output text is stored on the AIRequest; charts come from form_summary. Budget-
    reserved and enqueued on commit. One analysis per form (per prompt version)."""
    from apps.ai.models import AIFeature
    from apps.ai.services import check_and_reserve_budget
    from core.utils import current_schema

    if not form.responses.exists():
        raise UnprocessableEntity(_("This form has no responses to analyze yet."), code="no_responses")
    ai_request = check_and_reserve_budget(
        feature=AIFeature.FORM_ANALYSIS,
        requested_by=requested_by,
        requested_principal=requested_principal,
        source_app="forms",
        source_id=form.id,
        params={},
    )
    if getattr(ai_request, "_should_enqueue", False):
        schema = current_schema()
        transaction.on_commit(lambda: _enqueue_form_analysis(ai_request.pk, form.id, schema))
    return ai_request


def _enqueue_form_analysis(ai_request_id: int, form_id: int, schema: str) -> None:
    from celery_tasks.ai_tasks import run_form_analysis

    run_form_analysis.delay(ai_request_id, params={"form_id": form_id}, _schema_name=schema)
