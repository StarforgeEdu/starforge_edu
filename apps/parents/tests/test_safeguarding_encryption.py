"""Safeguarding encryption, permission, and corrupt-ciphertext regressions."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

PARENT_SECRET = "Restricted family court and welfare note"
CUSTODY_SECRET = "Pickup prohibited by active court order"


def _staff_type(*, name: str, slug: str, permissions: tuple[str, ...]):
    from apps.access.models import AccountType, AccountTypePermission

    account_type = AccountType.objects.create(
        name=name,
        slug=slug,
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.bulk_create(
        AccountTypePermission(account_type=account_type, permission=permission) for permission in permissions
    )
    return account_type


def _actor(*, branch, account_type):
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    actor = UserFactory()
    RoleMembership.objects.create(
        user=actor,
        branch=branch,
        account_type=account_type,
        role=account_type.compatibility_role,
    )
    actor.refresh_from_db()
    return actor


def test_parent_and_custody_notes_are_ciphertext_at_rest(tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian, ParentProfile
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        parent = ParentProfileFactory(notes=PARENT_SECRET)
        guardian = GuardianFactory(
            parent=parent,
            student=StudentProfileFactory(branch=branch),
            custody_notes=CUSTODY_SECRET,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT notes FROM {ParentProfile._meta.db_table} WHERE id = %s",  # nosec B608
                [parent.pk],
            )
            raw_parent = cursor.fetchone()[0]
            cursor.execute(
                f"SELECT custody_notes FROM {Guardian._meta.db_table} WHERE id = %s",  # nosec B608
                [guardian.pk],
            )
            raw_custody = cursor.fetchone()[0]

        assert raw_parent.startswith("gAAAA")
        assert raw_custody.startswith("gAAAA")
        assert PARENT_SECRET not in raw_parent
        assert CUSTODY_SECRET not in raw_custody
        parent.refresh_from_db()
        guardian.refresh_from_db()
        assert parent.notes == PARENT_SECRET
        assert guardian.custody_notes == CUSTODY_SECRET


def test_safeguarding_fields_require_exact_read_and_write_grants(tenant_a, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = StudentProfileFactory(branch=branch)
        parent = ParentProfileFactory(notes=PARENT_SECRET)
        guardian = GuardianFactory(
            parent=parent,
            student=student,
            custody_notes=CUSTODY_SECRET,
        )
        directory_type = _staff_type(
            name="Family directory only",
            slug="family-directory-only-encryption",
            permissions=("parents:read", "parents:write"),
        )
        safeguarding_type = _staff_type(
            name="Scoped safeguarding operator",
            slug="scoped-safeguarding-encryption",
            permissions=(
                "parents:read",
                "parents:write",
                "safeguarding:read",
                "safeguarding:write",
            ),
        )
        directory_actor = _actor(branch=branch, account_type=directory_type)
        safeguarding_actor = _actor(branch=branch, account_type=safeguarding_type)

    directory = as_user(tenant_a, directory_actor)
    listing = directory.get("/api/v1/parents/")
    assert listing.status_code == 200
    assert all("notes" not in row for row in listing.json()["data"])
    parent_detail = directory.get(f"/api/v1/parents/{parent.pk}/")
    guardian_detail = directory.get(f"/api/v1/parents/guardians/{guardian.pk}/")
    assert parent_detail.status_code == 200, parent_detail.content
    assert guardian_detail.status_code == 200, guardian_detail.content
    assert parent_detail.json()["data"]["notes"] is None
    assert guardian_detail.json()["data"]["custody_notes"] is None
    denied = directory.patch(
        f"/api/v1/parents/{parent.pk}/",
        {"notes": "unauthorized replacement"},
        format="json",
    )
    assert denied.status_code == 403

    safeguarding = as_user(tenant_a, safeguarding_actor)
    parent_read = safeguarding.get(f"/api/v1/parents/{parent.pk}/")
    guardian_read = safeguarding.get(f"/api/v1/parents/guardians/{guardian.pk}/")
    assert parent_read.status_code == 200, parent_read.content
    assert guardian_read.status_code == 200, guardian_read.content
    assert parent_read.json()["data"]["notes"] == PARENT_SECRET
    assert guardian_read.json()["data"]["custody_notes"] == CUSTODY_SECRET
    updated = safeguarding.patch(
        f"/api/v1/parents/{parent.pk}/",
        {"notes": "Reviewed safeguarding replacement"},
        format="json",
    )
    assert updated.status_code == 200, updated.content
    assert updated.json()["data"]["notes"] == "Reviewed safeguarding replacement"


def test_corrupt_safeguarding_ciphertext_is_never_exposed(tenant_a, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian, ParentProfile
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        parent = ParentProfileFactory(notes=PARENT_SECRET)
        guardian = GuardianFactory(
            parent=parent,
            student=StudentProfileFactory(branch=branch),
            custody_notes=CUSTODY_SECRET,
        )
        directory_type = _staff_type(
            name="Corrupt family directory",
            slug="corrupt-family-directory",
            permissions=("parents:read",),
        )
        safeguarding_type = _staff_type(
            name="Corrupt safeguarding reader",
            slug="corrupt-safeguarding-reader",
            permissions=("parents:read", "safeguarding:read"),
        )
        directory_actor = _actor(branch=branch, account_type=directory_type)
        safeguarding_actor = _actor(branch=branch, account_type=safeguarding_type)
        with connection.cursor() as cursor:
            # Deliberately bypass immutable-history guards to simulate damaged
            # ciphertext from an offline incident or key-rotation failure.
            cursor.execute("SET LOCAL starforge.identity_history_maintenance = 'on'")
            cursor.execute(
                f"UPDATE {ParentProfile._meta.db_table} SET notes = %s WHERE id = %s",  # nosec B608
                ["corrupt-parent-token", parent.pk],
            )
            cursor.execute(
                f"UPDATE {Guardian._meta.db_table} SET custody_notes = %s WHERE id = %s",  # nosec B608
                ["corrupt-custody-token", guardian.pk],
            )
            cursor.execute("SET LOCAL starforge.identity_history_maintenance = 'off'")

    directory = as_user(tenant_a, directory_actor)
    parent_response = directory.get(f"/api/v1/parents/{parent.pk}/")
    guardian_response = directory.get(f"/api/v1/parents/guardians/{guardian.pk}/")
    assert parent_response.status_code == guardian_response.status_code == 200
    assert parent_response.json()["data"]["notes"] is None
    assert guardian_response.json()["data"]["custody_notes"] is None

    safeguarding = as_user(tenant_a, safeguarding_actor)
    safeguarding.raise_request_exception = False
    parent_failure = safeguarding.get(f"/api/v1/parents/{parent.pk}/")
    guardian_failure = safeguarding.get(f"/api/v1/parents/guardians/{guardian.pk}/")
    assert parent_failure.status_code == guardian_failure.status_code == 500
    assert b"corrupt-parent-token" not in parent_failure.content
    assert b"corrupt-custody-token" not in guardian_failure.content


def test_guardian_collection_decrypts_authorized_notes_in_one_bounded_query(
    tenant_a,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        safeguarding_type = _staff_type(
            name="Safeguarding collection reader",
            slug="safeguarding-collection-reader",
            permissions=("parents:read", "safeguarding:read"),
        )
        actor = _actor(branch=branch, account_type=safeguarding_type)
        for index in range(12):
            GuardianFactory(
                parent=ParentProfileFactory(),
                student=StudentProfileFactory(branch=branch),
                custody_notes=f"Restricted note {index}",
            )

    client = as_user(tenant_a, actor)
    with CaptureQueriesContext(connection) as queries:
        response = client.get("/api/v1/parents/guardians/?page_size=25")

    assert response.status_code == 200, response.content
    assert len(response.json()["data"]) == 12
    custody_selects = [
        query["sql"]
        for query in queries.captured_queries
        if "SELECT" in query["sql"].upper() and "custody_notes" in query["sql"]
    ]
    assert len(custody_selects) == 1
