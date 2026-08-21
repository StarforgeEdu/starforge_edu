"""Schedule response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from apps.schedule.models import Lesson, LessonType, RecurrenceRule, Term, TimeSlot


def term_to_dict(term: Term) -> dict:
    return {
        "id": term.id,
        "name": term.name,
        "academic_year": term.academic_year,
        "start_date": term.start_date.isoformat(),
        "end_date": term.end_date.isoformat(),
        "is_current": term.is_current,
    }


def time_slot_to_dict(slot: TimeSlot) -> dict:
    return {
        "id": slot.id,
        "branch": slot.branch_id,
        "branch_name": slot.branch.name,
        "name": slot.name,
        "start_time": slot.start_time.isoformat(),
        "end_time": slot.end_time.isoformat(),
        "order": slot.order,
    }


def lesson_type_to_dict(lt: LessonType) -> dict:
    return {
        "id": lt.id,
        "name": lt.name,
        "slug": lt.slug,
        "color": lt.color,
        "is_active": lt.is_active,
    }


def rule_to_dict(rule: RecurrenceRule) -> dict:
    return {
        "id": rule.id,
        "term": rule.term_id,
        "term_name": rule.term.name,
        "cohort": rule.cohort_id,
        "cohort_name": rule.cohort.name,
        "teacher": rule.teacher_id,
        "teacher_name": rule.teacher.get_full_name(),
        "room": rule.room_id,
        "room_name": rule.room.name if rule.room else None,
        "lesson_type": rule.lesson_type_id,
        "lesson_type_name": rule.lesson_type.name if rule.lesson_type else None,
        "title": rule.title,
        "rrule": rule.rrule,
        "start_date": rule.start_date.isoformat(),
        "end_date": rule.end_date.isoformat(),
        "start_time": rule.start_time.isoformat(),
        "end_time": rule.end_time.isoformat(),
        "is_active": rule.is_active,
        "created_at": rule.created_at.isoformat(),
    }


def lesson_to_dict(lesson: Lesson) -> dict:
    return {
        "id": lesson.id,
        "rule": lesson.rule_id,
        "term": lesson.term_id,
        "term_name": lesson.term.name,
        "cohort": lesson.cohort_id,
        "cohort_name": lesson.cohort.name,
        "teacher": lesson.teacher_id,
        "teacher_name": lesson.teacher.get_full_name(),
        "room": lesson.room_id,
        "room_name": lesson.room.name if lesson.room else None,
        "lesson_type": lesson.lesson_type_id,
        "lesson_type_name": lesson.lesson_type.name if lesson.lesson_type else None,
        "title": lesson.title,
        "starts_at": lesson.starts_at.isoformat(),
        "ends_at": lesson.ends_at.isoformat(),
        "status": lesson.status,
        "detached_from_rule": lesson.detached_from_rule,
        "cancel_reason": lesson.cancel_reason,
    }
