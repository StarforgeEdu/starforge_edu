"""F24-1 — penalty point-threshold auto-escalation: when a student's total ACTIVE
demerit points cross the center's CenterSettings.penalty_escalation_threshold, the
crossing penalty is flagged (escalated=True) and the branch's managers are notified,
so a pattern of breaches surfaces automatically (accountability DNA). Fires once at
the UPWARD crossing — not on every later penalty, and waived points don't count."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

SETTINGS = "/api/v1/org/settings/"


def _set_threshold(tenant, value):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.penalty_escalation_threshold = value
        cs.save()  # the receiver busts the cached accessor


def _student_and_issuer(tenant):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch)
        issuer = UserFactory.create()
        return branch, student, issuer


def _issue(tenant, student, issuer, points, reason="breach"):
    from apps.compliance import services

    with schema_context(tenant.schema_name):
        return services.issue_penalty(student=student, points=points, reason=reason, issued_by=issuer)


def test_threshold_disabled_never_escalates(tenant_a):
    _, student, issuer = _student_and_issuer(tenant_a)  # threshold defaults to 0
    p = _issue(tenant_a, student, issuer, 50)
    assert p.escalated is False


def test_upward_crossing_flags_only_the_crossing_penalty(tenant_a):
    _set_threshold(tenant_a, 10)
    _, student, issuer = _student_and_issuer(tenant_a)
    p1 = _issue(tenant_a, student, issuer, 6)  # total 6 < 10
    p2 = _issue(tenant_a, student, issuer, 5)  # total 11 >= 10 -> crosses
    assert p1.escalated is False
    assert p2.escalated is True


def test_already_over_threshold_does_not_re_escalate(tenant_a):
    _set_threshold(tenant_a, 10)
    _, student, issuer = _student_and_issuer(tenant_a)
    _issue(tenant_a, student, issuer, 12)  # crosses on its own
    p_next = _issue(tenant_a, student, issuer, 3)  # already over -> not a new crossing
    assert p_next.escalated is False


def test_single_penalty_landing_exactly_on_threshold_crosses(tenant_a):
    _set_threshold(tenant_a, 10)
    _, student, issuer = _student_and_issuer(tenant_a)
    p = _issue(tenant_a, student, issuer, 10)  # before 0 < 10 <= 10 after
    assert p.escalated is True


def test_waived_points_do_not_count_toward_the_threshold(tenant_a):
    _set_threshold(tenant_a, 10)
    _, student, issuer = _student_and_issuer(tenant_a)
    from apps.compliance import services

    first = _issue(tenant_a, student, issuer, 8)
    with schema_context(tenant_a.schema_name):
        services.waive_penalty(penalty_id=first.id, actor=issuer, reason="appeal upheld")
    # active total is now 0; a 5-point penalty -> 5 < 10, no crossing
    p = _issue(tenant_a, student, issuer, 5)
    assert p.escalated is False


def test_escalation_notifies_branch_managers_only(tenant_a, user_in):
    """The crossing flags the penalty AND notifies the student's-branch managers
    (penalty:waive holders) — never a non-manager. The service defers the dispatch to
    on_commit (which doesn't fire inside a test transaction), so we assert the row the
    notifier creates synchronously; the escalation DECISION is covered by the flag."""
    from apps.compliance import services
    from apps.notifications.models import Notification

    _set_threshold(tenant_a, 10)
    branch, student, issuer = _student_and_issuer(tenant_a)
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)  # holds penalty:waive
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)  # no penalty:waive

    with schema_context(tenant_a.schema_name):
        p = services.issue_penalty(student=student, points=12, reason="major breach", issued_by=issuer)
        assert p.escalated is True
        services._notify_escalation(penalty=p, total_points=12, threshold=10)

        mgr_notes = Notification.objects.filter(user=manager, event_type="penalty.escalated")
        assert mgr_notes.exists(), "the branch manager should be notified of the escalation"
        assert mgr_notes.first().data["student_id"] == student.id
        assert not Notification.objects.filter(user=teacher, event_type="penalty.escalated").exists()


def test_threshold_round_trips_through_the_settings_api(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    patched = director.patch(SETTINGS, {"penalty_escalation_threshold": 25}, format="json")
    assert patched.status_code == 200, patched.content
    assert patched.json()["data"]["penalty_escalation_threshold"] == 25
    assert director.get(SETTINGS).json()["data"]["penalty_escalation_threshold"] == 25
