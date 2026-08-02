from __future__ import annotations

from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from apps.crm.models import (
    AcquisitionCampaign,
    CRMLead,
    LeadAttribution,
    LeadDuplicateCandidate,
    LeadFollowUp,
    LeadMerge,
    LeadSource,
    LeadStageHistory,
    LeadTouch,
    PipelineStage,
)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _principal(user, kind: str, principal_id: int | None) -> dict[str, Any] | None:
    if user is None or kind not in {"staff", "teacher"} or principal_id is None:
        return None
    relation = "staff_profile" if kind == "staff" else "teacher_profile"
    try:
        profile = getattr(user, relation)
    except ObjectDoesNotExist:
        profile = None
    display_name = None
    if profile is not None and profile.pk == principal_id:
        display_name = profile.get_full_name() or profile.username
    return {
        "kind": kind,
        "id": principal_id,
        "display_name": display_name,
        "attribution_status": "captured" if display_name is not None else "unavailable",
    }


def stage_to_dict(stage: PipelineStage) -> dict[str, Any]:
    return {
        "id": stage.pk,
        "slug": stage.slug,
        "name": stage.name,
        "category": stage.category,
        "position": stage.position,
        "is_active": stage.is_active,
        "created_at": stage.created_at.isoformat(),
        "updated_at": stage.updated_at.isoformat(),
    }


def source_to_dict(source: LeadSource) -> dict[str, Any]:
    return {
        "id": source.pk,
        "slug": source.slug,
        "name": source.name,
        "is_active": source.is_active,
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat(),
    }


def campaign_to_dict(campaign: AcquisitionCampaign) -> dict[str, Any]:
    return {
        "id": campaign.pk,
        "code": campaign.code,
        "name": campaign.name,
        "source": source_to_dict(campaign.source),
        "branch": campaign.branch_id,
        "branch_name": campaign.branch.name if campaign.branch is not None else None,
        "department": campaign.department_id,
        "department_name": campaign.department.name if campaign.department is not None else None,
        "starts_on": _iso(campaign.starts_on),
        "ends_on": _iso(campaign.ends_on),
        "is_active": campaign.is_active,
        "created_at": campaign.created_at.isoformat(),
        "updated_at": campaign.updated_at.isoformat(),
    }


def lead_to_dict(lead: CRMLead) -> dict[str, Any]:
    student = lead.student
    owner = _principal(lead.owner, lead.owner_principal_kind, lead.owner_principal_id)
    return {
        "id": lead.pk,
        "student": {
            "id": student.pk,
            "public_id": student.student_id,
            "full_name": student.get_full_name(),
            "phone": student.phone or None,
            "email": student.email or None,
            "status": student.status,
            "is_active": student.is_active,
        },
        "branch": lead.branch_id,
        "branch_name": lead.branch.name,
        "department": lead.department_id,
        "department_name": lead.department.name if lead.department is not None else None,
        "stage": stage_to_dict(lead.stage),
        "state": lead.state,
        "owner": owner,
        "initial_source": source_to_dict(lead.initial_source),
        "initial_campaign": (
            {
                "id": lead.initial_campaign_id,
                "code": lead.initial_campaign.code,
                "name": lead.initial_campaign.name,
            }
            if lead.initial_campaign
            else None
        ),
        "next_follow_up_at": _iso(getattr(lead, "next_follow_up_at", None)),
        "loss_reason": lead.loss_reason or None,
        "canonical_lead": lead.canonical_lead_id,
        "version": lead.version,
        "created_at": lead.created_at.isoformat(),
        "updated_at": lead.updated_at.isoformat(),
    }


def stage_history_to_dict(history: LeadStageHistory) -> dict[str, Any]:
    return {
        "id": history.pk,
        "lead": history.lead_id,
        "from_stage": (
            {"id": history.from_stage_id, "slug": history.from_stage.slug, "name": history.from_stage.name}
            if history.from_stage
            else None
        ),
        "to_stage": {
            "id": history.to_stage_id,
            "slug": history.to_stage.slug,
            "name": history.to_stage.name,
        },
        "from_state": history.from_state,
        "to_state": history.to_state,
        "loss_reason": history.loss_reason or None,
        "note": history.note or None,
        "actor": {"kind": history.actor_principal_kind, "id": history.actor_principal_id},
        "created_at": history.created_at.isoformat(),
    }


def touch_to_dict(touch: LeadTouch) -> dict[str, Any]:
    return {
        "id": touch.pk,
        "lead": touch.lead_id,
        "channel": touch.channel,
        "direction": touch.direction,
        "outcome": touch.outcome or None,
        "summary": touch.summary,
        "occurred_at": touch.occurred_at.isoformat(),
        "actor": {"kind": touch.actor_principal_kind, "id": touch.actor_principal_id},
        "created_at": touch.created_at.isoformat(),
    }


def follow_up_to_dict(follow_up: LeadFollowUp) -> dict[str, Any]:
    lead = follow_up.lead
    return {
        "id": follow_up.pk,
        "lead": follow_up.lead_id,
        "lead_summary": {
            "id": lead.pk,
            "student": {
                "id": lead.student_id,
                "public_id": lead.student.student_id,
                "full_name": lead.student.get_full_name(),
            },
            "branch": lead.branch_id,
            "branch_name": lead.branch.name,
            "department": lead.department_id,
            "department_name": lead.department.name if lead.department is not None else None,
        },
        "due_at": follow_up.due_at.isoformat(),
        "purpose": follow_up.purpose,
        "status": follow_up.status,
        "assignee": _principal(
            follow_up.assignee,
            follow_up.assignee_principal_kind,
            follow_up.assignee_principal_id,
        ),
        "created_by": _principal(
            follow_up.created_by,
            follow_up.created_by_principal_kind,
            follow_up.created_by_principal_id,
        ),
        "resolved_by": (
            _principal(
                follow_up.resolved_by,
                follow_up.resolved_by_principal_kind,
                follow_up.resolved_by_principal_id,
            )
            if follow_up.resolved_by_principal_id is not None
            else None
        ),
        "resolution_note": follow_up.resolution_note or None,
        "resolved_at": _iso(follow_up.resolved_at),
        "created_at": follow_up.created_at.isoformat(),
        "updated_at": follow_up.updated_at.isoformat(),
    }


def attribution_to_dict(attribution: LeadAttribution) -> dict[str, Any]:
    return {
        "id": attribution.pk,
        "lead": attribution.lead_id,
        "source": source_to_dict(attribution.source),
        "campaign": (
            {
                "id": attribution.campaign_id,
                "code": attribution.campaign.code,
                "name": attribution.campaign.name,
            }
            if attribution.campaign
            else None
        ),
        "medium": attribution.medium or None,
        "content": attribution.content or None,
        "occurred_at": attribution.occurred_at.isoformat(),
        "actor": {"kind": attribution.actor_principal_kind, "id": attribution.actor_principal_id},
        "created_at": attribution.created_at.isoformat(),
    }


def duplicate_to_dict(candidate: LeadDuplicateCandidate) -> dict[str, Any]:
    return {
        "id": candidate.pk,
        "left": {
            "id": candidate.left_id,
            "student_public_id": candidate.left.student.student_id,
            "student_name": candidate.left.student.get_full_name(),
        },
        "right": {
            "id": candidate.right_id,
            "student_public_id": candidate.right.student.student_id,
            "student_name": candidate.right.student.get_full_name(),
        },
        "score": candidate.score,
        "signals": candidate.signals,
        "status": candidate.status,
        "detected_at": candidate.detected_at.isoformat(),
        "reviewed_by": (
            {
                "kind": candidate.reviewed_by_principal_kind,
                "id": candidate.reviewed_by_principal_id,
            }
            if candidate.reviewed_by_principal_id is not None
            else None
        ),
        "reviewed_at": _iso(candidate.reviewed_at),
        "rationale": candidate.rationale or None,
    }


def merge_to_dict(merge: LeadMerge) -> dict[str, Any]:
    return {
        "id": merge.pk,
        "candidate": merge.candidate_id,
        "canonical_lead": merge.canonical_id,
        "duplicate_lead": merge.duplicate_id,
        "rationale": merge.rationale,
        "reviewed_by": {
            "kind": merge.reviewed_by_principal_kind,
            "id": merge.reviewed_by_principal_id,
        },
        "created_at": merge.created_at.isoformat(),
    }
