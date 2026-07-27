"""Regression coverage for role-native Django admin forms."""

import pytest
from django.contrib import admin
from django.test import RequestFactory
from django_tenants.utils import schema_context

from apps.org.models import Department, StaffProfile
from apps.org.tests.factories import BranchFactory
from apps.parents.models import ParentProfile
from apps.students.models import StudentProfile
from apps.teachers.models import TeacherProfile
from apps.users.models import RoleMembership, User
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _request(admin_user):
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    return request


def test_role_admin_forms_never_expose_user_bridge(tenant_a):
    with schema_context(tenant_a.schema_name):
        operator = User.objects.create_superuser(username="admin-forms", password="Admin-pass-42")
        request = _request(operator)
        for model in (StudentProfile, TeacherProfile, ParentProfile, StaffProfile):
            form_class = admin.site._registry[model].get_form(request)
            assert "user" not in form_class.base_fields
            assert "password" not in form_class.base_fields
            assert "username" in form_class.base_fields
            assert "password1" in form_class.base_fields
            assert "password2" in form_class.base_fields
        membership_form = admin.site._registry[RoleMembership].get_form(request)
        assert "user" not in membership_form.base_fields
        assert "granted_by" not in membership_form.base_fields
        assert {
            "staff_account",
            "teacher_account",
            "student_account",
            "parent_account",
        } <= set(membership_form.base_fields)
        department_form = admin.site._registry[Department].get_form(request)
        assert "head" not in department_form.base_fields
        assert "teacher_head" in department_form.base_fields


def test_student_admin_provisions_bridge_and_membership_automatically(tenant_a):
    with schema_context(tenant_a.schema_name):
        operator = User.objects.create_superuser(username="admin-save", password="Admin-pass-42")
        branch = BranchFactory()
        model_admin = admin.site._registry[StudentProfile]
        request = _request(operator)
        form_class = model_admin.get_form(request)
        form = form_class(
            data={
                "username": "admin.student",
                "password1": "Starlight-Map-42",
                "password2": "Starlight-Map-42",
                "is_active": "on",
                "student_id": "ADMIN-STUDENT-1",
                "first_name": "Admin",
                "last_name": "Student",
                "status": StudentProfile.Status.LEAD,
                "branch": branch.pk,
            }
        )
        assert form.is_valid(), form.errors
        student = form.save(commit=False)
        model_admin.save_model(request, student, form, change=False)

        student.refresh_from_db()
        assert student.check_password("Starlight-Map-42")
        assert not student.user.has_usable_password()
        assert RoleMembership.objects.filter(
            user=student.user,
            role=Role.STUDENT,
            branch=branch,
            revoked_at__isnull=True,
        ).exists()


def test_staff_admin_uses_staff_fields_and_creates_scoped_role(tenant_a):
    with schema_context(tenant_a.schema_name):
        operator = User.objects.create_superuser(username="staff-admin", password="Admin-pass-42")
        branch = BranchFactory()
        model_admin = admin.site._registry[StaffProfile]
        request = _request(operator)
        form_class = model_admin.get_form(request)
        form = form_class(
            data={
                "username": "admin.cashier",
                "password1": "Cashier-Map-42",
                "password2": "Cashier-Map-42",
                "is_active": "on",
                "first_name": "Casey",
                "last_name": "Cashier",
                "role": Role.CASHIER,
                "branch": branch.pk,
            }
        )
        assert form.is_valid(), form.errors
        staff = form.save(commit=False)
        model_admin.save_model(request, staff, form, change=False)

        assert staff.check_password("Cashier-Map-42")
        assert not staff.user.has_usable_password()
        assert RoleMembership.objects.filter(
            user=staff.user,
            role=Role.CASHIER,
            branch=branch,
            revoked_at__isnull=True,
        ).exists()
