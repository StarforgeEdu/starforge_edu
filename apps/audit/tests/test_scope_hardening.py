"""Branch-safe immutable audit attribution and visibility regressions."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.access.models import AccountType, AccountTypePermission
from apps.audit.models import AuditLog
from apps.audit.scopes import organization_audit_scope, scoped_audit_scope
from apps.audit.services import audit_log
from apps.audit.tests.factories import AuditLogFactory
from apps.org.tests.factories import BranchFactory, DepartmentFactory
from apps.users.models import RoleMembership
from apps.users.tests.factories import UserFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

AUDIT_URL = "/api/v1/audit/"
EXPORT_URL = "/api/v1/audit/export/"
RESOURCE_TYPE = "scope.TestEvent"


def _scoped_row(branch_id: int, department_id: int | None = None) -> AuditLog:
    return AuditLogFactory(
        resource_type=RESOURCE_TYPE,
        scope_status=AuditLog.ScopeStatus.SCOPED,
        scope_branch_id=branch_id,
        scope_department_id=department_id,
    )


def _listed_ids(client) -> set[int]:
    response = client.get(AUDIT_URL, {"resource_type": RESOURCE_TYPE})
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


def _exported_ids(client) -> set[int]:
    response = client.get(EXPORT_URL, {"resource_type": RESOURCE_TYPE})
    assert response.status_code == 200, response.content
    content = b"".join(response.streaming_content).decode()
    return {int(row["id"]) for row in csv.DictReader(io.StringIO(content))}


def test_branch_manager_list_detail_and_export_share_exact_visibility(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        local = BranchFactory(name="Audit Local", slug="audit-local")
        remote = BranchFactory(name="Audit Remote", slug="audit-remote")
        local_department = DepartmentFactory(branch=local)
        local_row = _scoped_row(local.pk)
        local_department_row = _scoped_row(local.pk, local_department.pk)
        remote_row = _scoped_row(remote.pk)
        organization_row = AuditLogFactory(
            resource_type=RESOURCE_TYPE,
            scope_status=AuditLog.ScopeStatus.ORGANIZATION,
        )
        unresolved_row = AuditLogFactory(resource_type=RESOURCE_TYPE)

    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=local)
    client = as_user(tenant_a, manager)
    expected = {local_row.pk, local_department_row.pk}
    assert _listed_ids(client) == expected
    assert _exported_ids(client) == expected

    assert client.get(f"{AUDIT_URL}{local_row.pk}/").status_code == 200
    for hidden in (remote_row, organization_row, unresolved_row):
        response = client.get(f"{AUDIT_URL}{hidden.pk}/")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


def test_department_membership_does_not_expand_to_branch_wide_audit_rows(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Audit Department Branch", slug="audit-department")
        own_department = DepartmentFactory(branch=branch, slug="audit-own")
        sibling_department = DepartmentFactory(branch=branch, slug="audit-sibling")
        viewer = user_in(tenant_a)
        RoleMembership.objects.create(
            user=viewer,
            branch=branch,
            department=own_department,
            role=Role.HEAD_OF_DEPT,
        )
        own = _scoped_row(branch.pk, own_department.pk)
        sibling = _scoped_row(branch.pk, sibling_department.pk)
        branch_wide = _scoped_row(branch.pk)
        viewer.refresh_from_db()

    client = as_user(tenant_a, viewer)
    assert _listed_ids(client) == {own.pk}
    assert client.get(f"{AUDIT_URL}{sibling.pk}/").status_code == 404
    assert client.get(f"{AUDIT_URL}{branch_wide.pk}/").status_code == 404


def test_exact_director_and_superuser_can_review_organization_and_unresolved_rows(
    tenant_a,
    as_role,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Audit Owner Branch", slug="audit-owner")
        scoped = _scoped_row(branch.pk)
        organization = AuditLogFactory(
            resource_type=RESOURCE_TYPE,
            scope_status=AuditLog.ScopeStatus.ORGANIZATION,
        )
        unresolved = AuditLogFactory(resource_type=RESOURCE_TYPE)
        superuser = UserFactory(is_superuser=True, is_staff=True)

    director_client, _director = as_role(Role.DIRECTOR, tenant_a)
    expected = {scoped.pk, organization.pk, unresolved.pk}
    assert _listed_ids(director_client) == expected
    assert _listed_ids(as_user(tenant_a, superuser)) == expected


def test_unrelated_canonical_director_role_cannot_lend_organization_scope(
    tenant_a,
    user_in,
    as_user,
):
    """A malformed compatibility role is not a permission-bearing boundary."""
    with schema_context(tenant_a.schema_name):
        local = BranchFactory(name="Audit Canonical Local", slug="audit-canonical-local")
        remote = BranchFactory(name="Audit Canonical Remote", slug="audit-canonical-remote")
        reader_type = AccountType.objects.create(
            name="Audit Local Reader",
            slug="audit-local-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=reader_type,
            permission="audit:read",
        )
        empty_type = AccountType.objects.create(
            name="Audit Empty Director Compatibility",
            slug="audit-empty-director-compatibility",
            account_kind=AccountType.AccountKind.STAFF,
        )
        viewer = user_in(tenant_a)
        RoleMembership.objects.create(
            user=viewer,
            branch=local,
            account_type=reader_type,
            role=Role.SUPPORT,
        )
        RoleMembership.objects.create(
            user=viewer,
            branch=remote,
            account_type=empty_type,
            role=Role.DIRECTOR,
        )
        local_row = _scoped_row(local.pk)
        remote_row = _scoped_row(remote.pk)
        organization_row = AuditLogFactory(
            resource_type=RESOURCE_TYPE,
            scope_status=AuditLog.ScopeStatus.ORGANIZATION,
        )
        viewer.refresh_from_db()

    client = as_user(tenant_a, viewer)
    assert _listed_ids(client) == {local_row.pk}
    assert client.get(f"{AUDIT_URL}{remote_row.pk}/").status_code == 404
    assert client.get(f"{AUDIT_URL}{organization_row.pk}/").status_code == 404


def test_salary_audit_requires_compensation_permission_at_the_same_scope(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        salary_branch = BranchFactory()
        other_branch = BranchFactory()
        salary_row = AuditLogFactory(
            resource_type="approvals.ApprovalRequest",
            before={"kind": "salary_prep", "amount_uzs": "9000000.00"},
            scope_status=AuditLog.ScopeStatus.SCOPED,
            scope_branch_id=salary_branch.pk,
        )
        ordinary_row = AuditLogFactory(
            resource_type="approvals.ApprovalRequest",
            before={"kind": "expense", "amount_uzs": "100000.00"},
            scope_status=AuditLog.ScopeStatus.SCOPED,
            scope_branch_id=salary_branch.pk,
        )
        viewer = user_in(tenant_a)
        audit_type = AccountType.objects.create(
            name="Salary audit reader",
            slug="salary-audit-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=audit_type,
            permission="audit:read",
        )
        compensation_type = AccountType.objects.create(
            name="Remote compensation reader",
            slug="remote-compensation-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=compensation_type,
            permission="compensation:read",
        )
        RoleMembership.objects.create(
            user=viewer,
            account_type=audit_type,
            role=audit_type.compatibility_role,
            branch=salary_branch,
        )
        # A compensation grant in another branch must not be borrowed.
        RoleMembership.objects.create(
            user=viewer,
            account_type=compensation_type,
            role=compensation_type.compatibility_role,
            branch=other_branch,
        )
        viewer.refresh_from_db()

    client = as_user(tenant_a, viewer)
    response = client.get(
        AUDIT_URL,
        {"resource_type": "approvals.ApprovalRequest"},
    )
    assert response.status_code == 200, response.content
    assert {row["id"] for row in response.json()["results"]} == {ordinary_row.pk}
    assert client.get(f"{AUDIT_URL}{salary_row.pk}/").status_code == 404

    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.create(
            user=viewer,
            account_type=compensation_type,
            role=compensation_type.compatibility_role,
            branch=salary_branch,
        )
        viewer.refresh_from_db()
    client = as_user(tenant_a, viewer)
    response = client.get(
        AUDIT_URL,
        {"resource_type": "approvals.ApprovalRequest"},
    )
    assert {row["id"] for row in response.json()["results"]} == {
        salary_row.pk,
        ordinary_row.pk,
    }


def test_compensation_idempotency_hashes_are_masked_in_audit_storage(tenant_a):
    with schema_context(tenant_a.schema_name):
        row = audit_log(
            action=AuditLog.Action.CREATE,
            resource_type="approvals.ApprovalRequest",
            before=None,
            after={
                "kind": "salary_prep",
                "idempotency_key_hash": "a" * 64,
                "operation_fingerprint": "b" * 64,
                "domain_dedupe_key": "c" * 64,
            },
            scope=scoped_audit_scope(BranchFactory().pk),
        )

    assert row.after["idempotency_key_hash"] == "***"
    assert row.after["operation_fingerprint"] == "***"
    assert row.after["domain_dedupe_key"] == "***"


def test_database_trigger_classifies_legacy_compensation_insert(tenant_a):
    """An old application node cannot downgrade salary history to standard."""
    with schema_context(tenant_a.schema_name):
        row = AuditLog.objects.create(
            action=AuditLog.Action.CREATE,
            resource_type="approvals.ApprovalRequest",
            after={"kind": "salary_prep", "amount_uzs": "9000000.00"},
            scope_status=AuditLog.ScopeStatus.SCOPED,
            scope_branch_id=BranchFactory().pk,
        )
        row.refresh_from_db()

    assert row.sensitivity == AuditLog.Sensitivity.COMPENSATION


def test_write_service_freezes_explicit_and_snapshot_scope(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        explicit = audit_log(
            action=AuditLog.Action.EXPORT,
            resource_type=RESOURCE_TYPE,
            scope=scoped_audit_scope(branch.pk, department.pk),
        )
        inferred = audit_log(
            action=AuditLog.Action.CREATE,
            resource_type="users.RoleMembership",
            resource_id="17",
            after={"branch_id": branch.pk, "department_id": department.pk},
        )
        organization = audit_log(
            action=AuditLog.Action.UPDATE,
            resource_type=RESOURCE_TYPE,
            scope=organization_audit_scope(),
        )

    for row in (explicit, inferred):
        assert row.scope_status == AuditLog.ScopeStatus.SCOPED
        assert row.scope_branch_id == branch.pk
        assert row.scope_department_id == department.pk
    assert organization.scope_status == AuditLog.ScopeStatus.ORGANIZATION
    assert organization.scope_branch_id is None


def test_workflow_receivers_freeze_branch_and_organization_scope(
    tenant_a,
    django_capture_on_commit_callbacks,
):
    """Leadership workflow mutations must be visible through the same immutable
    branch boundary as their operational records."""
    from apps.forms.models import Form, FormField
    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.tasks.models import RoleGrade, Task

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        attendee = UserFactory()
        starts_at = timezone.now() + timedelta(days=1)
        with django_capture_on_commit_callbacks(execute=True):
            form = Form.objects.create(title="Scoped form", branch=branch)
            field = FormField.objects.create(
                form=form,
                label="Safe choice",
                field_type=FormField.FieldType.BOOLEAN,
            )
            meeting = StaffMeeting.objects.create(
                title="Scoped meeting",
                branch=branch,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
            )
            invitation = MeetingAttendee.objects.create(meeting=meeting, user=attendee)
            task = Task.objects.create(
                title="Scoped task",
                branch=branch,
                department=department,
            )
            grade = RoleGrade.objects.create(role="audit_test_role", level=1)

        expected = {
            ("forms_app.Form", str(form.pk)): (AuditLog.ScopeStatus.SCOPED, branch.pk, None),
            ("forms_app.FormField", str(field.pk)): (AuditLog.ScopeStatus.SCOPED, branch.pk, None),
            ("meetings.StaffMeeting", str(meeting.pk)): (
                AuditLog.ScopeStatus.SCOPED,
                branch.pk,
                None,
            ),
            ("meetings.MeetingAttendee", str(invitation.pk)): (
                AuditLog.ScopeStatus.SCOPED,
                branch.pk,
                None,
            ),
            ("staff_tasks.Task", str(task.pk)): (
                AuditLog.ScopeStatus.SCOPED,
                branch.pk,
                department.pk,
            ),
            ("staff_tasks.RoleGrade", str(grade.pk)): (
                AuditLog.ScopeStatus.ORGANIZATION,
                None,
                None,
            ),
        }
        rows = AuditLog.objects.filter(
            action=AuditLog.Action.CREATE,
            resource_type__in={resource_type for resource_type, _resource_id in expected},
        )
        observed = {
            (row.resource_type, row.resource_id): (
                row.scope_status,
                row.scope_branch_id,
                row.scope_department_id,
            )
            for row in rows
        }

    assert {key: observed[key] for key in expected} == expected


def test_workflow_move_between_organization_and_branch_is_quarantined(
    tenant_a,
    django_capture_on_commit_callbacks,
):
    from apps.forms.models import Form

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        with django_capture_on_commit_callbacks(execute=True):
            form = Form.objects.create(title="Moving form")
        with django_capture_on_commit_callbacks(execute=True):
            form.branch = branch
            form.save(update_fields=("branch", "updated_at"))
        movement = AuditLog.objects.get(
            action=AuditLog.Action.UPDATE,
            resource_type="forms_app.Form",
            resource_id=str(form.pk),
        )

    assert movement.scope_status == AuditLog.ScopeStatus.UNRESOLVED
    assert movement.scope_branch_id is None


def test_otp_audit_response_and_export_never_disclose_raw_identifier(
    tenant_a,
    as_role,
):
    from apps.auth.signals import otp_requested
    from apps.users.models import OTP
    from core.privacy import private_fingerprint
    from core.utils import current_schema

    raw_identifier = "+998901234567"
    identifier_ref = private_fingerprint(raw_identifier, namespace="auth-identifier")
    client, _director = as_role(Role.DIRECTOR, tenant_a)
    with schema_context(tenant_a.schema_name):
        # Simulate a row created by an older node during a rolling deployment.
        # The read projection must redact it even before an offline data review.
        AuditLogFactory(
            action=AuditLog.Action.OTP_REQUEST,
            resource_type="auth.OTP",
            after={"identifier": raw_identifier, "purpose": OTP.PURPOSE_RESET},
            scope_status=AuditLog.ScopeStatus.ORGANIZATION,
        )
        otp_requested.send(
            sender=OTP,
            identifier=raw_identifier,
            purpose=OTP.PURPOSE_RESET,
            ip="203.0.113.7",
            user_agent="audit-privacy-test",
            schema_name=current_schema(),
        )
        row = AuditLog.objects.filter(
            action=AuditLog.Action.OTP_REQUEST,
            resource_type="auth.OTP",
        ).latest("pk")
        assert row.scope_status == AuditLog.ScopeStatus.ORGANIZATION
        assert row.after["identifier_ref"] == identifier_ref

    response = client.get(
        AUDIT_URL,
        {"action": AuditLog.Action.OTP_REQUEST, "resource_type": "auth.OTP"},
    )
    assert response.status_code == 200, response.content
    serialized = json.dumps(response.json(), sort_keys=True)
    assert raw_identifier not in serialized
    assert identifier_ref in serialized

    exported = client.get(
        EXPORT_URL,
        {"action": AuditLog.Action.OTP_REQUEST, "resource_type": "auth.OTP"},
    )
    assert exported.status_code == 200, exported.content
    csv_content = b"".join(exported.streaming_content).decode()
    assert raw_identifier not in csv_content


def test_legacy_backfill_reports_and_quarantines_without_current_resource_joins(
    tenant_a,
):
    with schema_context(tenant_a.schema_name):
        local = BranchFactory()
        remote = BranchFactory()
        scoped = AuditLogFactory(
            resource_type="users.RoleMembership",
            after={"branch_id": local.pk, "department_id": None},
        )
        organization = AuditLogFactory(resource_type="users.User")
        conflicting = AuditLogFactory(
            resource_type="users.RoleMembership",
            before={"branch_id": local.pk, "department_id": None},
            after={"branch_id": remote.pk, "department_id": None},
        )
        insufficient = AuditLogFactory(
            resource_type="finance.Invoice",
            after={"student_id": 999999},
        )

    review_output = io.StringIO()
    call_command(
        "backfill_audit_scopes",
        "--schema",
        tenant_a.schema_name,
        stdout=review_output,
    )
    with schema_context(tenant_a.schema_name):
        scoped.refresh_from_db()
        organization.refresh_from_db()
        assert scoped.scope_status == AuditLog.ScopeStatus.UNRESOLVED
        assert organization.scope_status == AuditLog.ScopeStatus.UNRESOLVED

    output = io.StringIO()
    call_command(
        "backfill_audit_scopes",
        "--schema",
        tenant_a.schema_name,
        "--apply",
        stdout=output,
    )
    reports = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
    schema_report = reports[0]
    assert schema_report["resolved_scoped"] >= 1
    assert schema_report["resolved_organization"] >= 1
    assert schema_report["quarantined"] >= 1
    assert schema_report["unresolved"] >= 1

    with schema_context(tenant_a.schema_name):
        scoped.refresh_from_db()
        organization.refresh_from_db()
        conflicting.refresh_from_db()
        insufficient.refresh_from_db()
        assert scoped.scope_status == AuditLog.ScopeStatus.SCOPED
        assert scoped.scope_branch_id == local.pk
        assert organization.scope_status == AuditLog.ScopeStatus.ORGANIZATION
        assert conflicting.scope_status == AuditLog.ScopeStatus.UNRESOLVED
        assert insufficient.scope_status == AuditLog.ScopeStatus.UNRESOLVED


def test_scope_shape_constraint_and_append_only_trigger_are_fail_closed(tenant_a):
    with schema_context(tenant_a.schema_name):
        with pytest.raises(IntegrityError), transaction.atomic():
            AuditLogFactory(
                scope_status=AuditLog.ScopeStatus.SCOPED,
                scope_branch_id=None,
            )

        row = AuditLogFactory()
        with pytest.raises(DatabaseError):
            _attempt_old_maintenance_bypass(row.pk)
        with pytest.raises(DatabaseError):
            _attempt_recent_retention_delete(row.pk)

        row.refresh_from_db()
        assert row.action == AuditLog.Action.CREATE
        assert row.scope_status == AuditLog.ScopeStatus.UNRESOLVED


def _attempt_old_maintenance_bypass(row_id: int) -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            # The old broad maintenance value must no longer bypass append-only
            # protection in production or tests.
            cursor.execute("SET LOCAL starforge.audit_maintenance = 'on'")
        AuditLog.objects.filter(pk=row_id).update(
            action=AuditLog.Action.LOGOUT,
            scope_status=AuditLog.ScopeStatus.ORGANIZATION,
        )


def _attempt_recent_retention_delete(row_id: int) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.audit_maintenance = 'retention-delete'")
        AuditLog.objects.filter(pk=row_id).delete()
