"""F3-2 — the teacher dashboard surfaces forms the teacher must fill: published, open
forms that TARGET them (by role or by user id) and that they have not yet answered."""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/teachers/dashboard/"


def _teacher(tenant, branch=None):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant.schema_name):
        branch = branch or BranchFactory()
        teacher = TeacherProfileFactory(branch=branch)
        RoleMembership.objects.create(user=teacher.user, branch=branch, role=Role.TEACHER)
        teacher.user.refresh_from_db()
        return teacher, branch


def _published_form(tenant, *, branch, roles=(), user_ids=(), status=None):
    from apps.forms.models import Form

    with schema_context(tenant.schema_name):
        return Form.objects.create(
            title="Staff survey",
            status=status or Form.Status.PUBLISHED,
            branch=branch,
            audience_roles=list(roles),
            audience_user_ids=list(user_ids),
            published_at=timezone.now(),
        )


@pytest.fixture
def as_user_client(as_user):
    def _make(tenant, teacher):
        return as_user(tenant, teacher.user)

    return _make


def test_form_targeting_the_role_appears(tenant_a, as_user_client):
    teacher, branch = _teacher(tenant_a)
    form = _published_form(tenant_a, branch=branch, roles=[Role.TEACHER])
    body = as_user_client(tenant_a, teacher).get(URL).json()["data"]
    assert form.id in {f["id"] for f in body["pending_forms"]}


def test_form_targeting_the_user_appears(tenant_a, as_user_client):
    teacher, branch = _teacher(tenant_a)
    form = _published_form(tenant_a, branch=branch, user_ids=[teacher.user_id])
    body = as_user_client(tenant_a, teacher).get(URL).json()["data"]
    assert form.id in {f["id"] for f in body["pending_forms"]}


def test_untargeted_form_does_not_appear(tenant_a, as_user_client):
    teacher, branch = _teacher(tenant_a)
    open_form = _published_form(tenant_a, branch=branch)  # empty audience = not a personal to-do
    body = as_user_client(tenant_a, teacher).get(URL).json()["data"]
    assert open_form.id not in {f["id"] for f in body["pending_forms"]}


def test_answered_form_drops_off(tenant_a, as_user_client):
    from apps.forms.models import FormResponse

    teacher, branch = _teacher(tenant_a)
    form = _published_form(tenant_a, branch=branch, roles=[Role.TEACHER])
    before = as_user_client(tenant_a, teacher).get(URL).json()["data"]["pending_forms"]
    assert form.id in {f["id"] for f in before}

    with schema_context(tenant_a.schema_name):
        FormResponse.objects.create(form=form, respondent=teacher.user)

    after = as_user_client(tenant_a, teacher).get(URL).json()["data"]["pending_forms"]
    assert form.id not in {f["id"] for f in after}


def test_other_branch_form_does_not_appear(tenant_a, as_user_client):
    from apps.org.tests.factories import BranchFactory

    teacher, _branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        other = BranchFactory()
    form = _published_form(tenant_a, branch=other, roles=[Role.TEACHER])  # different branch
    body = as_user_client(tenant_a, teacher).get(URL).json()["data"]
    assert form.id not in {f["id"] for f in body["pending_forms"]}


def test_draft_form_does_not_appear(tenant_a, as_user_client):
    from apps.forms.models import Form

    teacher, branch = _teacher(tenant_a)
    form = _published_form(tenant_a, branch=branch, roles=[Role.TEACHER], status=Form.Status.DRAFT)
    body = as_user_client(tenant_a, teacher).get(URL).json()["data"]
    assert form.id not in {f["id"] for f in body["pending_forms"]}
