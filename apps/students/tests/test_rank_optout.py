"""F15-1 — a center can switch classroom RANK off (dignity): CenterSettings.
show_classroom_rank=False omits the rank from the student's own report and the parent
view alike, leaving attendance + payment untouched."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ME_REPORT = "/api/v1/students/me/report/"
SETTINGS = "/api/v1/org/settings/"


def _disable_rank(tenant):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.show_classroom_rank = False
        cs.save()  # the receiver busts the cached accessor


def test_selector_gates_rank_on_the_setting(tenant_a, monkeypatch):
    """The rank is computed + returned by default, and replaced with None (never
    computed) once the center disables it — attendance/payment stay."""
    from apps.students import selectors
    from apps.students.tests.factories import StudentProfileFactory

    # a sentinel so a suppressed rank (None) is distinguishable from a bare student's None
    monkeypatch.setattr(selectors, "_classroom_rank", lambda s: {"position": 2, "of": 5})
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()
        report = selectors.student_report(student=student)
        assert report["rank"] == {"position": 2, "of": 5}  # shown by default
        assert "attendance" in report
        assert "payment" in report

    _disable_rank(tenant_a)
    with schema_context(tenant_a.schema_name):
        off = selectors.student_report(student=student)
        assert off["rank"] is None  # suppressed
        assert "attendance" in off  # the rest is untouched
        assert "payment" in off


def test_student_report_endpoint_respects_the_setting(tenant_a, user_in, as_user, monkeypatch):
    from apps.org.tests.factories import BranchFactory
    from apps.students import selectors
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    monkeypatch.setattr(selectors, "_classroom_rank", lambda s: {"position": 1, "of": 9})
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create(user=student_user, branch=branch, status=StudentProfile.Status.ACTIVE)
    client = as_user(tenant_a, student_user)

    assert client.get(ME_REPORT).json()["data"]["rank"] == {"position": 1, "of": 9}  # on by default
    _disable_rank(tenant_a)
    assert client.get(ME_REPORT).json()["data"]["rank"] is None  # suppressed end-to-end


def test_setting_round_trips_through_the_api(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    assert director.get(SETTINGS).json()["data"]["show_classroom_rank"] is True  # default
    patched = director.patch(SETTINGS, {"show_classroom_rank": False}, format="json")
    assert patched.status_code == 200, patched.content
    assert patched.json()["data"]["show_classroom_rank"] is False
    assert director.get(SETTINGS).json()["data"]["show_classroom_rank"] is False
