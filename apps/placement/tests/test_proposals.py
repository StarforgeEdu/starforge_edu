"""F1-8 — group placement: reception proposes a cohort for a placed lead; a manager
accepts (→ enrolled) or rejects, gated by CenterSettings.require_group_acceptance.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

PROPOSALS = "/api/v1/placement/proposals/"


def _set_require_acceptance(tenant, value: bool) -> None:
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.require_group_acceptance = value
        cs.save()  # the receiver busts the cached accessor


def _setup(tenant, user_in, as_user, *, lead_status=None):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    reception_u = user_in(tenant, roles=[Role.REGISTRAR], branch=branch)
    hod_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    hod2_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(
            user=lead_u, branch=branch, status=lead_status or StudentProfile.Status.LEAD
        )
        cohort = CohortFactory.create(branch=branch, name="Adv-A", level="advanced", capacity=10)
    return {
        "tenant": tenant,
        "branch": branch,
        "reception": as_user(tenant, reception_u),
        "hod": as_user(tenant, hod_u),
        "hod_u": hod_u,
        "hod2": as_user(tenant, hod2_u),
        "lead": lead,
        "cohort": cohort,
    }


def _is_enrolled(tenant, lead, cohort) -> bool:
    from apps.cohorts.models import CohortMembership
    from apps.students.models import StudentProfile

    with schema_context(tenant.schema_name):
        active = CohortMembership.objects.filter(cohort=cohort, student=lead, end_date__isnull=True).exists()
        current = StudentProfile.objects.get(pk=lead.id).current_cohort_id == cohort.id
    return active and current


def test_toggle_off_proposal_enrolls_directly(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, False)  # the default — reception assigns directly
    r = s["reception"].post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["data"]["status"] == "accepted"
    assert r.json()["data"]["membership"] is not None
    assert _is_enrolled(tenant_a, s["lead"], s["cohort"])


def test_toggle_on_requires_manager_acceptance(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    proposed = s["reception"].post(
        PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json"
    )
    assert proposed.status_code == 201
    assert proposed.json()["data"]["status"] == "pending"
    pid = proposed.json()["data"]["id"]
    # not enrolled yet — it's awaiting a manager
    assert not _is_enrolled(tenant_a, s["lead"], s["cohort"])
    # the manager accepts -> enrolled
    accepted = s["hod"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json")
    assert accepted.status_code == 200
    assert accepted.json()["data"]["status"] == "accepted"
    assert _is_enrolled(tenant_a, s["lead"], s["cohort"])


def test_manager_rejects_with_reason_no_enrollment(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    pid = (
        s["reception"]
        .post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
        .json()["data"]["id"]
    )
    rejected = s["hod"].post(f"{PROPOSALS}{pid}/reject/", {"reason": "Wrong level"}, format="json")
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "rejected"
    assert rejected.json()["data"]["reject_reason"] == "Wrong level"
    assert not _is_enrolled(tenant_a, s["lead"], s["cohort"])


def test_proposer_cannot_self_accept(tenant_a, user_in, as_user):
    """Maker-checker: the manager who proposed can't also accept it."""
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    # an HOD proposes (HOD holds both write + approve)
    pid = (
        s["hod"]
        .post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
        .json()["data"]["id"]
    )
    own = s["hod"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json")
    assert own.status_code == 403
    assert own.json()["code"] == "self_acceptance"
    # a different manager can accept
    assert s["hod2"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json").status_code == 200


def test_reception_cannot_accept(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    pid = (
        s["reception"]
        .post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
        .json()["data"]["id"]
    )
    # reception holds placement:write but not placement:approve
    assert s["reception"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json").status_code == 403


def test_cannot_accept_a_decided_proposal(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    pid = (
        s["reception"]
        .post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
        .json()["data"]["id"]
    )
    assert s["hod"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json").status_code == 200
    again = s["hod2"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json")
    assert again.status_code == 422
    assert again.json()["code"] == "proposal_not_pending"


def test_cannot_propose_a_non_prospective_student(tenant_a, user_in, as_user):
    from apps.students.models import StudentProfile

    s = _setup(tenant_a, user_in, as_user, lead_status=StudentProfile.Status.ACTIVE)
    r = s["reception"].post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "student_not_prospective"


def test_duplicate_pending_proposal_conflicts(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    body = {"student": s["lead"].id, "cohort": s["cohort"].id}
    assert s["reception"].post(PROPOSALS, body, format="json").status_code == 201
    dup = s["reception"].post(PROPOSALS, body, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "already_proposed"


def test_cannot_propose_into_an_archived_cohort(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        archived = CohortFactory.create(branch=s["branch"], name="Old", level="advanced", is_archived=True)
    r = s["reception"].post(PROPOSALS, {"student": s["lead"].id, "cohort": archived.id}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "cohort_archived"


def test_cannot_propose_a_cohort_in_another_branch(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        other_cohort = CohortFactory.create(branch=other_branch, name="Other", level="advanced")
    r = s["reception"].post(PROPOSALS, {"student": s["lead"].id, "cohort": other_cohort.id}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "cross_branch"


def test_toggle_is_settable_through_the_settings_api(tenant_a, user_in, as_user, as_role):
    """The maker-checker toggle must be enableable via the product API, not just ORM."""
    s = _setup(tenant_a, user_in, as_user)
    director, _ = as_role(Role.DIRECTOR)
    patched = director.patch("/api/v1/org/settings/", {"require_group_acceptance": True}, format="json")
    assert patched.status_code == 200
    assert patched.json()["data"]["require_group_acceptance"] is True
    assert director.get("/api/v1/org/settings/").json()["data"]["require_group_acceptance"] is True
    # ...and it actually drives the flow: a proposal now waits for a manager
    proposed = s["reception"].post(
        PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json"
    )
    assert proposed.json()["data"]["status"] == "pending"


def test_accept_revalidates_student_is_still_prospective(tenant_a, user_in, as_user):
    """Symmetric paths: a proposal that goes stale (the lead drifted out of the
    prospective set while pending) is rejected at accept, not silently enrolled."""
    from apps.students.models import StudentProfile

    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    pid = (
        s["reception"]
        .post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
        .json()["data"]["id"]
    )
    with schema_context(tenant_a.schema_name):  # the lead's lifecycle drifts forward
        StudentProfile.objects.filter(pk=s["lead"].id).update(status=StudentProfile.Status.ACTIVE)
    r = s["hod"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "student_not_prospective"
    assert not _is_enrolled(tenant_a, s["lead"], s["cohort"])


@pytest.mark.django_db(transaction=True)
def test_accept_enrolls_under_real_autocommit(tenant_a, user_in, as_user):
    """accept_proposal locks the row and calls enroll_student_in_cohort (both use
    select_for_update / atomic) — exercise the REAL autocommit path end to end."""
    s = _setup(tenant_a, user_in, as_user)
    _set_require_acceptance(tenant_a, True)
    pid = (
        s["reception"]
        .post(PROPOSALS, {"student": s["lead"].id, "cohort": s["cohort"].id}, format="json")
        .json()["data"]["id"]
    )
    res = s["hod"].post(f"{PROPOSALS}{pid}/accept/", {}, format="json")
    assert res.status_code == 200
    assert _is_enrolled(tenant_a, s["lead"], s["cohort"])
