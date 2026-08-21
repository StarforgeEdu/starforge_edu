"""F1-7 — group suggestion from a placement result: rank the lead's branch cohorts
by level fit + free seats. Staff-only (reception's placing tool); transparent rule.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ATTEMPTS = "/api/v1/placement/attempts/"


def _setup(tenant, user_in, as_user):
    """A graded-equivalent lead (academic_level 'advanced') with an attempt, plus a
    spread of cohorts in/around their branch to suggest from."""
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        other_branch = BranchFactory.create()

    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    hod_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)

    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(
            user=lead_u, branch=branch, status=StudentProfile.Status.LEAD, academic_level="advanced"
        )
        # an approved test + an attempt to hang the suggestion action on
        test = services.create_test(title="T", created_by=teacher_u, branch=branch)
        services.add_question(
            test=test, prompt="2+2?", question_type="single_choice", options=["3", "4"], correct_answer="4"
        )
        services.submit_for_review(test=test)
        test = services.approve_test(test=test, approver=hod_u)  # re-fetches → capture
        attempt = services.assign_test(test=test, student=lead, assigned_by=hod_u)

        # cohorts to rank
        adv = CohortFactory.create(branch=branch, name="Adv-A", level="advanced", capacity=10)
        CohortMembershipFactory.create_batch(2, cohort=adv)  # 2 of 10 -> 8 seats
        adv_uncapped = CohortFactory.create(branch=branch, name="Adv-B", level="Advanced", capacity=None)
        intermediate = CohortFactory.create(branch=branch, name="Int", level="intermediate", capacity=10)
        adv_full = CohortFactory.create(branch=branch, name="Adv-Full", level="advanced", capacity=2)
        CohortMembershipFactory.create_batch(2, cohort=adv_full)  # full -> excluded
        adv_archived = CohortFactory.create(
            branch=branch, name="Adv-Arch", level="advanced", capacity=10, is_archived=True
        )
        adv_other = CohortFactory.create(branch=other_branch, name="Adv-Other", level="advanced", capacity=10)
    return {
        "branch": branch,
        "staff": as_user(tenant, hod_u),
        "lead_c": as_user(tenant, lead_u),
        "attempt": attempt,
        "ids": {
            "adv": adv.id,
            "adv_uncapped": adv_uncapped.id,
            "intermediate": intermediate.id,
            "adv_full": adv_full.id,
            "adv_archived": adv_archived.id,
            "adv_other": adv_other.id,
        },
    }


def test_suggestions_rank_level_match_and_seats(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    res = s["staff"].get(f"{ATTEMPTS}{s['attempt'].id}/suggestions/")
    assert res.status_code == 200, res.content
    rows = res.json()["data"]
    ids = [r["cohort_id"] for r in rows]

    # full / archived / other-branch cohorts are never suggested
    assert s["ids"]["adv_full"] not in ids
    assert s["ids"]["adv_archived"] not in ids
    assert s["ids"]["adv_other"] not in ids
    # the three viable branch cohorts are all present
    assert set(ids) == {s["ids"]["adv"], s["ids"]["adv_uncapped"], s["ids"]["intermediate"]}
    # exact level matches (advanced, case-insensitive) rank ahead of the intermediate one
    assert rows[0]["level_match"] is True
    assert rows[1]["level_match"] is True
    assert rows[2]["cohort_id"] == s["ids"]["intermediate"]
    assert rows[2]["level_match"] is False
    # seats math: capped 2-of-10 -> 8; uncapped -> null (always room)
    by_id = {r["cohort_id"]: r for r in rows}
    assert by_id[s["ids"]["adv"]]["seats_available"] == 8
    assert by_id[s["ids"]["adv_uncapped"]]["seats_available"] is None


def test_lead_cannot_read_group_suggestions(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # suggestions is staff-only (placement:write) — the lead has no placement perm
    assert s["lead_c"].get(f"{ATTEMPTS}{s['attempt'].id}/suggestions/").status_code == 403


def test_ended_cohort_is_not_suggested(tenant_a, user_in, as_user):
    from datetime import date

    from apps.cohorts.tests.factories import CohortFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        ended = CohortFactory.create(
            branch=s["branch"],
            name="Adv-Ended",
            level="advanced",
            capacity=10,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
    rows = s["staff"].get(f"{ATTEMPTS}{s['attempt'].id}/suggestions/").json()["data"]
    assert ended.id not in [r["cohort_id"] for r in rows]
