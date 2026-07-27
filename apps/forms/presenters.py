"""Forms-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from apps.forms.models import Form, FormAnswer, FormField, FormResponse


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


def form_to_dict(form: Form) -> dict[str, Any]:
    return {
        "id": form.id,
        "title": form.title,
        "description": form.description,
        "status": form.status,
        "is_anonymous": form.is_anonymous,
        "allow_multiple": form.allow_multiple,
        "branch": form.branch_id,
        "audience_roles": form.audience_roles,
        "audience_user_ids": form.audience_user_ids,
        "opens_at": form.opens_at.isoformat() if form.opens_at else None,
        "closes_at": form.closes_at.isoformat() if form.closes_at else None,
        "created_by": form.created_by_id,
        "published_at": form.published_at.isoformat() if form.published_at else None,
        "closed_at": form.closed_at.isoformat() if form.closed_at else None,
        "created_at": form.created_at.isoformat(),
        "form_fields": [field_to_dict(fld) for fld in form.fields.all()],
    }


def _answer_to_dict(a: FormAnswer) -> dict[str, Any]:
    return {"field": a.field_id, "value": a.value}


def response_to_dict(r: FormResponse) -> dict[str, Any]:
    return {
        "id": r.id,
        "form": r.form_id,
        "respondent": r.respondent_id,
        "created_at": r.created_at.isoformat(),
        "answers": [_answer_to_dict(a) for a in r.answers.all()],
    }
