"""Exact permission-bearing scope regressions for content operations."""

from __future__ import annotations

from typing import Any

import pytest
from django_tenants.utils import schema_context

from apps.access.models import AccountType, AccountTypePermission
from apps.content.tests.factories import ContentLibraryFactory, FolderFactory, LessonFileFactory
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.users.models import RoleMembership
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _account_type(slug: str, permission: str) -> AccountType:
    account_type = AccountType.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.create(account_type=account_type, permission=permission)
    return account_type


def test_read_and_write_grants_cannot_borrow_each_others_scope(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        department_a = DepartmentFactory(branch=branch_a)
        branch_b = BranchFactory()
        department_b = DepartmentFactory(branch=branch_b)
        library_a = ContentLibraryFactory(
            visibility="department",
            department=department_a,
        )
        library_b = ContentLibraryFactory(
            visibility="department",
            department=department_b,
        )
        read_type = _account_type("content-reader-a", "content:read")
        write_type = _account_type("content-writer-b", "content:write")

    user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch_a)
    with schema_context(tenant_a.schema_name):
        user.role_memberships.update(account_type=read_type, department=department_a)
        RoleMembership.objects.create(
            user=user,
            role=Role.SUPPORT,
            account_type=write_type,
            branch=branch_b,
            department=department_b,
        )

    client = as_user(tenant_a, user)
    assert client.get(f"/api/v1/content/libraries/{library_a.id}/").status_code == 200
    assert client.get(f"/api/v1/content/libraries/{library_b.id}/").status_code == 404
    assert (
        client.patch(
            f"/api/v1/content/libraries/{library_a.id}/",
            {"description": "must not change"},
            format="json",
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/v1/content/libraries/{library_b.id}/",
            {"description": "authorized write"},
            format="json",
        ).status_code
        == 200
    )


def test_draft_visibility_requires_management_permission_in_same_read_scope(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        department_a = DepartmentFactory(branch=branch_a)
        branch_b = BranchFactory()
        department_b = DepartmentFactory(branch=branch_b)
        library_a = ContentLibraryFactory(visibility="department", department=department_a)
        library_b = ContentLibraryFactory(visibility="department", department=department_b)
        published: Any = LessonFileFactory(folder=FolderFactory(library=library_a))
        draft_a: Any = LessonFileFactory(
            folder=FolderFactory(library=library_a),
            is_approved_teacher=False,
            is_approved_manager=False,
        )
        draft_b: Any = LessonFileFactory(
            folder=FolderFactory(library=library_b),
            is_approved_teacher=False,
            is_approved_manager=False,
        )
        read_type = _account_type("content-draft-reader-a", "content:read")
        write_type = _account_type("content-draft-writer-b", "content:write")

    user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch_a)
    with schema_context(tenant_a.schema_name):
        user.role_memberships.update(account_type=read_type, department=department_a)
        RoleMembership.objects.create(
            user=user,
            role=Role.SUPPORT,
            account_type=write_type,
            branch=branch_b,
            department=department_b,
        )

    response = as_user(tenant_a, user).get("/api/v1/content/files/?page_size=100")
    assert response.status_code == 200, response.content
    visible_ids = {item["id"] for item in response.json()["data"]}
    assert published.id in visible_ids
    assert draft_a.id not in visible_ids
    assert draft_b.id not in visible_ids


def test_publish_grant_does_not_reach_pending_file_outside_its_scope(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        department_a = DepartmentFactory(branch=branch_a)
        branch_b = BranchFactory()
        department_b = DepartmentFactory(branch=branch_b)
        pending_a: Any = LessonFileFactory(
            folder=FolderFactory(
                library=ContentLibraryFactory(visibility="department", department=department_a)
            ),
            is_approved_teacher=True,
            is_approved_manager=False,
        )
        pending_b: Any = LessonFileFactory(
            folder=FolderFactory(
                library=ContentLibraryFactory(visibility="department", department=department_b)
            ),
            is_approved_teacher=True,
            is_approved_manager=False,
        )
        publisher_type = _account_type("content-publisher-b", "content:publish")

    user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch_b)
    with schema_context(tenant_a.schema_name):
        user.role_memberships.update(account_type=publisher_type, department=department_b)

    client = as_user(tenant_a, user)
    assert (
        client.post(
            f"/api/v1/content/files/{pending_a.id}/approve-manager/",
            {},
            format="json",
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/content/files/{pending_b.id}/approve-manager/",
            {},
            format="json",
        ).status_code
        == 200
    )
    with schema_context(tenant_a.schema_name):
        pending_a.refresh_from_db()
        assert pending_a.is_approved_manager is False


def test_scoped_writer_cannot_mutate_tenant_wide_library(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        tenant_library = ContentLibraryFactory(visibility="tenant")
        writer_type = _account_type("branch-content-writer", "content:write")

    user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    with schema_context(tenant_a.schema_name):
        user.role_memberships.update(account_type=writer_type, department=department)

    response = as_user(tenant_a, user).patch(
        f"/api/v1/content/libraries/{tenant_library.id}/",
        {"description": "cross-scope mutation"},
        format="json",
    )
    assert response.status_code == 404
    with schema_context(tenant_a.schema_name):
        tenant_library.refresh_from_db()
        assert tenant_library.description == ""
