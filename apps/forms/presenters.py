"""Forms-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from apps.forms.models import Form, FormAnswer, FormField, FormResponse


def _creator_identity(form: Form) -> dict[str, Any] | None:
    if (
        form.created_by_attribution_status
        not in {
            Form.CreatorAttributionStatus.CAPTURED,
            Form.CreatorAttributionStatus.RESOLVED,
        }
        or form.created_by_principal_kind not in {"staff", "teacher"}
        or form.created_by_principal_id is None
    ):
        return None
    display_name = None
    if form.created_by is not None:
        relation = "staff_profile" if form.created_by_principal_kind == "staff" else "teacher_profile"
        try:
            profile = getattr(form.created_by, relation)
        except ObjectDoesNotExist:
            profile = None
        if profile is not None and profile.pk == form.created_by_principal_id:
            display_name = (
                " ".join(
                    value.strip()
                    for value in (profile.first_name, profile.middle_name, profile.last_name)
                    if isinstance(value, str) and value.strip()
                )
                or profile.username
            )
    return {
        "kind": form.created_by_principal_kind,
        "id": form.created_by_principal_id,
        "display_name": display_name,
        "account_label": "Teacher" if form.created_by_principal_kind == "teacher" else "Staff",
    }


def field_to_dict(f: FormField) -> dict[str, Any]:
    return {
        "id": f.id,
        "label": f.label,
        "field_type": f.field_type,
        "required": f.required,
        "order": f.order,
        "options": f.options,
        "help_text": f.help_text,
    }


def form_to_dict(form: Form, *, include_management: bool = True) -> dict[str, Any]:
    data = {
        "id": form.id,
        "title": form.title,
        "description": form.description,
        "status": form.status,
        "is_anonymous": form.is_anonymous,
        "allow_multiple": form.allow_multiple,
        "branch": form.branch_id,
        "opens_at": form.opens_at.isoformat() if form.opens_at else None,
        "closes_at": form.closes_at.isoformat() if form.closes_at else None,
        "published_at": form.published_at.isoformat() if form.published_at else None,
        "closed_at": form.closed_at.isoformat() if form.closed_at else None,
        "created_at": form.created_at.isoformat(),
        "response_submitted": bool(getattr(form, "response_submitted", False)),
        "form_fields": [field_to_dict(fld) for fld in form.fields.all()],
    }
    if include_management:
        captured_user_ids = {
            item.get("user_id")
            for item in form.audience_principals
            if isinstance(item, dict) and isinstance(item.get("user_id"), int)
        }
        safe_audience_user_ids = [
            user_id for user_id in form.audience_user_ids if user_id in captured_user_ids
        ]
        data.update(
            {
                "audience_roles": form.audience_roles,
                # Never expose an ambiguous legacy bridge id. Operators receive
                # only reviewed exact targets plus a count requiring migration review.
                "audience_user_ids": safe_audience_user_ids,
                "audience_principals": form.audience_principals,
                "audience_unresolved_count": max(
                    0,
                    len(set(form.audience_user_ids)) - len(set(safe_audience_user_ids)),
                ),
                "created_by": _creator_identity(form),
                "created_by_attribution_status": form.created_by_attribution_status,
            }
        )
    return data


def _answer_to_dict(a: FormAnswer) -> dict[str, Any]:
    return {"field": a.field_id, "value": a.value}


def response_to_dict(r: FormResponse) -> dict[str, Any]:
    attributed = r.respondent_attribution_status in {
        FormResponse.AttributionStatus.CAPTURED,
        FormResponse.AttributionStatus.RESOLVED,
    }
    return {
        "id": r.id,
        "form": r.form_id,
        # Review-only legacy rows are neither anonymous nor safe to attribute.
        # The explicit status lets managers distinguish those cases without
        # exposing a bridge User as if it were an exact account identity.
        "respondent": r.respondent_id if attributed else None,
        "respondent_principal": (
            {"kind": r.respondent_principal_kind, "id": r.respondent_principal_id} if attributed else None
        ),
        "respondent_attribution_status": r.respondent_attribution_status,
        "created_at": r.created_at.isoformat(),
        "answers": [_answer_to_dict(a) for a in r.answers.all()],
    }
