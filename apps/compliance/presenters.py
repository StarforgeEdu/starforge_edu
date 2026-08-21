"""Compliance response presenters (the DRF Rule/Penalty serializer shapes)."""

from __future__ import annotations

from apps.compliance.models import Penalty, Rule


def rule_to_dict(rule: Rule) -> dict:
    return {
        "id": rule.id,
        "title": rule.title,
        "body": rule.body,
        "version": rule.version,
        "applies_to_roles": rule.applies_to_roles,
        "is_active": rule.is_active,
        "created_at": rule.created_at.isoformat(),
        "updated_at": rule.updated_at.isoformat(),
    }


def rule_with_ack_to_dict(rule: Rule, acked_ids: set[int]) -> dict:
    return {**rule_to_dict(rule), "acknowledged": rule.id in acked_ids}


def penalty_to_dict(penalty: Penalty) -> dict:
    return {
        "id": penalty.id,
        "rule": penalty.rule_id,
        "student": penalty.student_id,
        "staff": penalty.staff_id,
        "points": penalty.points,
        "reason": penalty.reason,
        "branch": penalty.branch_id,
        "status": penalty.status,
        "issued_by": penalty.issued_by_id,
        "issued_at": penalty.issued_at.isoformat(),
        "waived_by": penalty.waived_by_id,
        "waived_at": penalty.waived_at.isoformat() if penalty.waived_at else None,
        "waive_reason": penalty.waive_reason,
        "escalated": penalty.escalated,
    }
