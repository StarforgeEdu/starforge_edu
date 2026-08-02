"""Adversarial schedule permission-to-membership scope regressions."""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


def _account_type(*, name: str, slug: str, permissions: tuple[str, ...], kind: str):
    from apps.access.models import AccountType, AccountTypePermission

    account_type = AccountType.objects.create(
        name=name,
        slug=slug,
        account_kind=kind,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in permissions
        ]
    )
    return account_type


def test_teacher_relationship_cannot_borrow_schedule_grant_across_branches(tenant_a, as_user):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson, RecurrenceRule
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        allowed_branch = BranchFactory()
        unrelated_branch = BranchFactory()
        schedule_teacher = AccountType.objects.create(
            name="Scoped schedule teacher",
            slug="scoped-schedule-teacher",
            account_kind=AccountType.AccountKind.TEACHER,
        )
        AccountTypePermission.objects.create(
            account_type=schedule_teacher,
            permission="schedule:read",
        )
        unrelated_teacher = AccountType.objects.create(
            name="Schedule-unrelated teacher",
            slug="schedule-unrelated-teacher",
            account_kind=AccountType.AccountKind.TEACHER,
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=allowed_branch,
            account_type=schedule_teacher,
            role=schedule_teacher.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=unrelated_branch,
            account_type=unrelated_teacher,
            role=unrelated_teacher.compatibility_role,
        )
        teacher = TeacherProfileFactory(user=actor, branch=allowed_branch)
        allowed_cohort = CohortFactory(branch=allowed_branch, primary_teacher=teacher)
        # Deliberately malformed cross-branch relation; assignment cannot widen
        # the exact scope of the schedule:read membership.
        unrelated_cohort = CohortFactory(branch=unrelated_branch, primary_teacher=teacher)
        term = TermFactory()
        today = timezone.localdate()
        rule_fields = {
            "term": term,
            "teacher": teacher,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "start_date": today,
            "end_date": today + timedelta(days=7),
            "start_time": time(9),
            "end_time": time(10),
        }
        allowed_rule = RecurrenceRule.objects.create(
            cohort=allowed_cohort,
            title="Allowed rule",
            **rule_fields,
        )
        unrelated_rule = RecurrenceRule.objects.create(
            cohort=unrelated_cohort,
            title="Unrelated rule",
            **rule_fields,
        )
        starts_at = timezone.now() + timedelta(days=1)
        allowed_lesson = Lesson.objects.create(
            term=term,
            cohort=allowed_cohort,
            teacher=teacher,
            title="Allowed lesson",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )
        unrelated_lesson = Lesson.objects.create(
            term=term,
            cohort=unrelated_cohort,
            teacher=teacher,
            title="Unrelated lesson",
            starts_at=starts_at + timedelta(hours=2),
            ends_at=starts_at + timedelta(hours=3),
        )
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    rules = client.get("/api/v1/schedule/rules/")
    assert rules.status_code == 200, rules.content
    assert {row["id"] for row in rules.json()["data"]} == {allowed_rule.pk}
    assert client.get(f"/api/v1/schedule/rules/{unrelated_rule.pk}/").status_code == 404

    lessons = client.get("/api/v1/schedule/lessons/")
    assert lessons.status_code == 200, lessons.content
    assert {row["id"] for row in lessons.json()["data"]} == {allowed_lesson.pk}
    assert client.get(f"/api/v1/schedule/lessons/{unrelated_lesson.pk}/").status_code == 404


def test_split_read_write_grants_resolve_schedule_actions_through_write_scope(tenant_a, as_user):
    from apps.access.models import AccountType
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson, RecurrenceRule, TimeSlot
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        read_branch = BranchFactory()
        write_branch = BranchFactory()
        reader = _account_type(
            name="Scoped schedule reader",
            slug="scoped-schedule-reader-split",
            kind=AccountType.AccountKind.STAFF,
            permissions=("schedule:read",),
        )
        writer = _account_type(
            name="Scoped schedule writer",
            slug="scoped-schedule-writer-split",
            kind=AccountType.AccountKind.STAFF,
            permissions=("schedule:write",),
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=read_branch,
            account_type=reader,
            role=reader.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=write_branch,
            account_type=writer,
            role=writer.compatibility_role,
        )
        term = TermFactory()
        read_teacher = TeacherProfileFactory(branch=read_branch)
        write_teacher = TeacherProfileFactory(branch=write_branch)
        read_cohort = CohortFactory(branch=read_branch)
        write_cohort = CohortFactory(branch=write_branch)
        today = timezone.localdate()
        base_fields = {
            "term": term,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "start_date": today,
            "end_date": today + timedelta(days=7),
            "start_time": time(9),
            "end_time": time(10),
        }
        read_rule = RecurrenceRule.objects.create(
            cohort=read_cohort,
            teacher=read_teacher,
            title="Read-only rule",
            **base_fields,
        )
        write_rule = RecurrenceRule.objects.create(
            cohort=write_cohort,
            teacher=write_teacher,
            title="Write-only rule",
            **base_fields,
        )
        starts_at = timezone.now() + timedelta(days=1)
        read_lesson = Lesson.objects.create(
            term=term,
            cohort=read_cohort,
            teacher=read_teacher,
            title="Read-only lesson",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )
        write_lesson = Lesson.objects.create(
            term=term,
            cohort=write_cohort,
            teacher=write_teacher,
            title="Write-only lesson",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )
        read_slot = TimeSlot.objects.create(
            branch=read_branch,
            name="Read slot",
            start_time=time(8),
            end_time=time(9),
        )
        write_slot = TimeSlot.objects.create(
            branch=write_branch,
            name="Write slot",
            start_time=time(8),
            end_time=time(9),
        )
        missing_rule_id = max(read_rule.pk, write_rule.pk) + 1000
        missing_lesson_id = max(read_lesson.pk, write_lesson.pk) + 1000
        missing_slot_id = max(read_slot.pk, write_slot.pk) + 1000
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    assert {row["id"] for row in client.get("/api/v1/schedule/rules/").json()["data"]} == {read_rule.pk}

    for target_id in (read_rule.pk, missing_rule_id):
        response = client.delete(f"/api/v1/schedule/rules/{target_id}/")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert client.delete(f"/api/v1/schedule/rules/{write_rule.pk}/").status_code == 204

    for target_id in (read_lesson.pk, missing_lesson_id):
        response = client.post(
            f"/api/v1/schedule/lessons/{target_id}/cancel/",
            {"reason": "scope probe"},
            format="json",
        )
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert (
        client.post(
            f"/api/v1/schedule/lessons/{write_lesson.pk}/cancel/",
            {"reason": "authorized"},
            format="json",
        ).status_code
        == 200
    )

    for target_id in (read_slot.pk, missing_slot_id):
        response = client.patch(
            f"/api/v1/schedule/timeslots/{target_id}/",
            {"name": "scope probe"},
            format="json",
        )
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
    assert (
        client.patch(
            f"/api/v1/schedule/timeslots/{write_slot.pk}/",
            {"name": "Authorized slot"},
            format="json",
        ).status_code
        == 200
    )


def test_branch_writer_cannot_mutate_tenant_global_schedule_catalogues(tenant_a, as_user):
    from apps.access.models import AccountType
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import LessonType
    from apps.schedule.tests.factories import TermFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        writer = _account_type(
            name="Branch schedule writer",
            slug="branch-schedule-writer-global-denial",
            kind=AccountType.AccountKind.STAFF,
            permissions=("schedule:read", "schedule:write"),
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=branch,
            account_type=writer,
            role=writer.compatibility_role,
        )
        term = TermFactory()
        lesson_type = LessonType.objects.create(name="Global type", slug="global-type")
        missing_term_id = term.pk + 1000
        missing_type_id = lesson_type.pk + 1000
        actor.refresh_from_db()

    client = as_user(tenant_a, actor)
    assert client.get("/api/v1/schedule/terms/").status_code == 200
    assert client.get("/api/v1/schedule/lesson-types/").status_code == 200

    denied = (
        client.post(
            "/api/v1/schedule/terms/",
            {
                "name": "Unauthorized term",
                "academic_year": "2030-2031",
                "start_date": "2030-01-01",
                "end_date": "2030-06-01",
            },
            format="json",
        ),
        client.post(
            "/api/v1/schedule/lesson-types/",
            {"name": "Unauthorized type"},
            format="json",
        ),
        client.patch(
            f"/api/v1/schedule/terms/{term.pk}/",
            {"name": "Unauthorized edit"},
            format="json",
        ),
        client.patch(
            f"/api/v1/schedule/terms/{missing_term_id}/",
            {"name": "Probe"},
            format="json",
        ),
        client.patch(
            f"/api/v1/schedule/lesson-types/{lesson_type.pk}/",
            {"name": "Unauthorized edit"},
            format="json",
        ),
        client.patch(
            f"/api/v1/schedule/lesson-types/{missing_type_id}/",
            {"name": "Probe"},
            format="json",
        ),
    )
    assert {(response.status_code, response.json()["code"]) for response in denied} == {(403, "out_of_scope")}


def test_rule_foreign_key_scope_does_not_reveal_remote_ids(tenant_a, as_user):
    from apps.access.models import AccountType
    from apps.cohorts.models import Cohort
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        remote_branch = BranchFactory()
        writer = _account_type(
            name="Rule branch writer",
            slug="rule-branch-writer-no-oracle",
            kind=AccountType.AccountKind.STAFF,
            permissions=("schedule:write",),
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=branch,
            account_type=writer,
            role=writer.compatibility_role,
        )
        term = TermFactory()
        local_teacher = TeacherProfileFactory(branch=branch)
        remote_teacher = TeacherProfileFactory(branch=remote_branch)
        local_cohort = CohortFactory(branch=branch)
        remote_cohort = CohortFactory(branch=remote_branch)
        missing_cohort_id = (Cohort.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1000
        actor.refresh_from_db()

    payload = {
        "term": term.pk,
        "teacher": local_teacher.pk,
        "title": "Scoped rule",
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
        "start_date": timezone.localdate().isoformat(),
        "end_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
        "start_time": "09:00",
        "end_time": "10:00",
    }
    client = as_user(tenant_a, actor)
    remote = client.post(
        "/api/v1/schedule/rules/",
        {**payload, "cohort": remote_cohort.pk, "teacher": remote_teacher.pk},
        format="json",
    )
    missing = client.post(
        "/api/v1/schedule/rules/",
        {**payload, "cohort": missing_cohort_id},
        format="json",
    )
    assert remote.status_code == missing.status_code == 404
    assert remote.json()["code"] == missing.json()["code"] == "not_found"

    allowed = client.post(
        "/api/v1/schedule/rules/",
        {**payload, "cohort": local_cohort.pk},
        format="json",
    )
    assert allowed.status_code == 201, allowed.content
