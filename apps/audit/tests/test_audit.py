"""Audit lane tests (D3-D).

Covers the full "Tests required" contract:
- model receivers: User create+update with before/after diff; delete row;
  ProviderConfig credential masking.
- the read-only API: PUT/PATCH/DELETE/POST -> 405; filters; cursor pagination
  stable under inserts; audit:read permission matrix + cross-tenant isolation.
- the audit_log() helper masking + ip/ua extraction.
- the retention beat task (frozen time): 7y vs 1y cohorts.
- auth-flow audit: login success/failure + OTP request/verify write rows.
- CSV export: streams rows, audits itself, 400 over the row cap.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.audit.models import AuditLog
from apps.audit.services import MASKED_FIELDS, audit_log, mask_snapshot, serialize_instance
from apps.audit.tests.factories import AuditLogFactory
from apps.users.models import RoleMembership
from apps.users.tests.factories import UserFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

AUDIT_URL = "/api/v1/audit/"
EXPORT_URL = "/api/v1/audit/export/"

Action = AuditLog.Action


# --------------------------------------------------------------------------- #
# Model receivers (D3-D-2)
# --------------------------------------------------------------------------- #


class TestModelReceivers:
    # Model receivers write via `transaction.on_commit`, so tests wrap the write
    # in `django_capture_on_commit_callbacks(execute=True)` to actually run them
    # (the repo convention — see apps/assignments/tests + apps/notifications/tests).

    def test_user_create_then_update_produces_rows_with_diff(
        self, tenant_a, django_capture_on_commit_callbacks
    ):
        with schema_context(tenant_a.schema_name):
            with django_capture_on_commit_callbacks(execute=True):
                user = UserFactory(first_name="Alpha")
            create_rows = AuditLog.objects.filter(
                resource_type="users.User", resource_id=str(user.pk), action=Action.CREATE
            )
            assert create_rows.count() == 1
            assert create_rows.first().after["username"] == user.username

            with django_capture_on_commit_callbacks(execute=True):
                user.first_name = "Beta"
                user.save(update_fields=["first_name"])

            update_rows = AuditLog.objects.filter(
                resource_type="users.User", resource_id=str(user.pk), action=Action.UPDATE
            )
            assert update_rows.count() == 1
            row = update_rows.first()
            # before/after diff isolates the changed field.
            assert row.before["first_name"] == "Alpha"
            assert row.after["first_name"] == "Beta"

    def test_delete_produces_entry(self, tenant_a, django_capture_on_commit_callbacks):
        with schema_context(tenant_a.schema_name):
            user = UserFactory()
            pk = user.pk
            with django_capture_on_commit_callbacks(execute=True):
                user.delete()
            del_rows = AuditLog.objects.filter(
                resource_type="users.User", resource_id=str(pk), action=Action.DELETE
            )
            assert del_rows.count() == 1
            assert del_rows.first().before is not None

    def test_rolemembership_audited(self, tenant_a, django_capture_on_commit_callbacks):
        from apps.org.tests.factories import BranchFactory

        with schema_context(tenant_a.schema_name):
            user = UserFactory()
            branch = BranchFactory()
            with django_capture_on_commit_callbacks(execute=True):
                membership = RoleMembership.objects.create(user=user, branch=branch, role=Role.TEACHER)
            rows = AuditLog.objects.filter(
                resource_type="users.RoleMembership",
                resource_id=str(membership.pk),
                action=Action.CREATE,
            )
            assert rows.count() == 1

    def test_permission_override_create_and_update_are_audited(
        self, tenant_a, django_capture_on_commit_callbacks
    ):
        from apps.access.models import RolePermissionOverride

        with schema_context(tenant_a.schema_name):
            with django_capture_on_commit_callbacks(execute=True):
                override = RolePermissionOverride.objects.create(
                    role=Role.TEACHER,
                    permission="students:write",
                    effect=RolePermissionOverride.Effect.GRANT,
                )
            assert AuditLog.objects.filter(
                resource_type="access.RolePermissionOverride",
                resource_id=str(override.pk),
                action=Action.CREATE,
            ).count() == 1

            with django_capture_on_commit_callbacks(execute=True):
                override.effect = RolePermissionOverride.Effect.REVOKE
                override.save(update_fields=["effect"])
            row = AuditLog.objects.get(
                resource_type="access.RolePermissionOverride",
                resource_id=str(override.pk),
                action=Action.UPDATE,
            )
            assert row.before["effect"] == RolePermissionOverride.Effect.GRANT
            assert row.after["effect"] == RolePermissionOverride.Effect.REVOKE

    def test_providerconfig_masks_credentials(self, tenant_a, django_capture_on_commit_callbacks):
        from apps.payments.models import ProviderConfig

        with schema_context(tenant_a.schema_name):
            with django_capture_on_commit_callbacks(execute=True):
                cfg = ProviderConfig.objects.create(
                    provider="payme",
                    payme_merchant_id="m-123",
                    payme_key="super-secret-key",
                    payme_test_key="test-secret",
                )
            row = AuditLog.objects.filter(
                resource_type="payments.ProviderConfig",
                resource_id=str(cfg.pk),
                action=Action.CREATE,
            ).first()
            assert row is not None
            # Credentials masked; the non-secret merchant id is preserved.
            assert row.after["payme_key"] == "***"
            assert row.after["payme_test_key"] == "***"
            assert row.after["payme_merchant_id"] == "m-123"


# --------------------------------------------------------------------------- #
# Public-schema guard (audit is TENANT-ONLY; audit_auditlog absent in public)
# --------------------------------------------------------------------------- #


class TestPublicSchemaGuard:
    """``apps.audit`` is tenant-only, so ``audit_auditlog`` does not exist in the
    public schema. Platform-staff writes to the SHARED users.User /
    users.RoleMembership tables fire the audit receivers while the connection is
    on ``public`` — those must NO-OP, not raise ProgrammingError."""

    def test_public_schema_user_save_does_not_raise_or_audit(
        self, public_tenant, django_capture_on_commit_callbacks
    ):
        from django.db import connection
        from django_tenants.utils import get_public_schema_name, schema_context

        from apps.users.tests.factories import UserFactory

        with schema_context(get_public_schema_name()):
            assert connection.schema_name == get_public_schema_name()
            # The on_commit audit hook would target a non-existent table; the
            # public-schema guard must keep this from being scheduled/raised.
            with django_capture_on_commit_callbacks(execute=True):
                user = UserFactory()
            # User actually persisted on the public schema.
            from apps.users.models import User

            assert User.objects.filter(pk=user.pk).exists()

    def test_public_schema_rolemembership_save_does_not_raise(
        self, public_tenant, django_capture_on_commit_callbacks
    ):
        from django_tenants.utils import get_public_schema_name, schema_context

        from apps.users.tests.factories import UserFactory

        with schema_context(get_public_schema_name()):
            user = UserFactory()
            # org_branch does not exist on public; the branch FK is db_constraint=
            # False (ADR-007), so a public-schema RoleMembership uses a bare
            # branch_id. The audit receiver still fires on this save and must
            # no-op rather than hit the non-existent audit_auditlog table.
            with django_capture_on_commit_callbacks(execute=True):
                membership = RoleMembership.objects.create(user=user, branch_id=1, role=Role.IT)
            assert RoleMembership.objects.filter(pk=membership.pk).exists()

    def test_audit_log_helper_noops_on_public_schema(self, public_tenant):
        from django_tenants.utils import get_public_schema_name, schema_context

        with schema_context(get_public_schema_name()):
            # Synchronous audit_log() (the auth-flow path) must also no-op.
            row = audit_log(
                actor=None,
                action=Action.LOGIN_FAILED,
                resource_type="users.User",
                after={"username": "platform-admin"},
            )
            assert row is None


# --------------------------------------------------------------------------- #
# Before-snapshot thread-local: schema-scoped + self-cleaning (D3-F)
# --------------------------------------------------------------------------- #


class TestBeforeSnapshotThreadLocal:
    """A pre_save whose write fails (post_save never fires) must not corrupt a
    LATER save's before/after diff — especially in a DIFFERENT tenant reusing the
    same worker thread. The store key includes the schema and is self-cleaning."""

    def test_store_key_is_schema_scoped(self, tenant_a, tenant_b):
        from django_tenants.utils import schema_context

        from apps.audit import receivers

        with schema_context(tenant_a.schema_name):
            key_a = receivers._store_key("users.User", 5)
        with schema_context(tenant_b.schema_name):
            key_b = receivers._store_key("users.User", 5)
        # Same label+pk in different tenants must NOT collide.
        assert key_a != key_b
        assert tenant_a.schema_name in key_a
        assert tenant_b.schema_name in key_b

    def test_stale_pre_save_does_not_corrupt_next_tenant_diff(
        self, tenant_a, tenant_b, django_capture_on_commit_callbacks
    ):
        """Simulate a leaked before-snapshot from tenant A (a failed save where
        post_save never popped it), then do a real update of the same label:pk in
        tenant B. B's diff must reflect B's true prior state, not A's stale one."""
        from django_tenants.utils import schema_context

        from apps.audit import receivers
        from apps.users.tests.factories import UserFactory

        # Leak a stale tenant-A snapshot for users.User pk that we'll reuse in B.
        with schema_context(tenant_a.schema_name):
            user_a = UserFactory(first_name="LeakedTenantA")
            stale_key = receivers._store_key("users.User", user_a.pk)
            store = receivers._before_store.__dict__.setdefault("data", {})
            store[stale_key] = {"first_name": "LeakedTenantA"}

        with schema_context(tenant_b.schema_name):
            # Create a user in B that happens to share the pk (or any pk) and
            # update it: its before/after diff must be B's own, never A's leak.
            user_b = UserFactory(first_name="RealTenantB")
            with django_capture_on_commit_callbacks(execute=True):
                user_b.first_name = "RealTenantB-Updated"
                user_b.save(update_fields=["first_name"])
            row = (
                AuditLog.objects.filter(
                    resource_type="users.User", resource_id=str(user_b.pk), action=Action.UPDATE
                )
                .order_by("-id")
                .first()
            )
            assert row is not None
            assert row.before["first_name"] == "RealTenantB"
            assert row.before["first_name"] != "LeakedTenantA"


# --------------------------------------------------------------------------- #
# audit_log() helper (D3-D-3)
# --------------------------------------------------------------------------- #


class TestAuditLogHelper:
    def test_masks_sensitive_after(self, tenant_a):
        with schema_context(tenant_a.schema_name):
            row = audit_log(
                actor=None,
                action=Action.UPDATE,
                resource_type="students.StudentProfile",
                resource_id=7,
                after={"national_id": "AA1234567", "medical_notes": "asthma", "status": "active"},
            )
            assert row.after["national_id"] == "***"
            assert row.after["medical_notes"] == "***"
            assert row.after["status"] == "active"

    def test_extracts_ip_and_ua_from_request(self, tenant_a, rf):
        with schema_context(tenant_a.schema_name):
            request = rf.get("/", HTTP_USER_AGENT="pytest-ua", REMOTE_ADDR="10.0.0.9")
            request.user = UserFactory()
            row = audit_log(actor=request.user, action=Action.EXPORT, request=request)
            assert row.ip == "10.0.0.9"
            assert row.user_agent == "pytest-ua"
            assert row.actor_repr == request.user.username

    def test_anonymous_actor_never_raises(self, tenant_a):
        from django.contrib.auth.models import AnonymousUser

        with schema_context(tenant_a.schema_name):
            row = audit_log(actor=AnonymousUser(), action=Action.LOGIN_FAILED)
            assert row.actor_id is None
            assert row.actor_repr == "anonymous"

    def test_masked_fields_cover_td9_set(self):
        for field in ("national_id", "medical_notes", "password", "click_secret_key", "uzum_api_key"):
            assert field in MASKED_FIELDS
        assert mask_snapshot({"password": "x"})["password"] == "***"
        assert mask_snapshot(None) is None

    def test_serialize_instance_stores_fk_ids(self, tenant_a):
        from apps.org.tests.factories import BranchFactory

        with schema_context(tenant_a.schema_name):
            user = UserFactory()
            branch = BranchFactory()
            membership = RoleMembership.objects.create(user=user, branch=branch, role=Role.IT)
            snap = serialize_instance(membership)
            assert snap["user_id"] == user.pk
            assert snap["role"] == Role.IT


# --------------------------------------------------------------------------- #
# Auth-flow audit (D3-D-3)
# --------------------------------------------------------------------------- #


class TestAuthFlowAudit:
    def test_login_success_writes_row(self, tenant_a):
        from apps.auth.services import login_with_password

        with schema_context(tenant_a.schema_name):
            user = UserFactory()
            user.set_password("Sup3r-Secret!")
            user.save(update_fields=["password"])
            login_with_password(username=user.username, password="Sup3r-Secret!", ip="1.2.3.4")
            rows = AuditLog.objects.filter(action=Action.LOGIN, resource_id=str(user.pk))
            assert rows.count() == 1
            assert rows.first().ip == "1.2.3.4"

    def test_login_failure_writes_row(self, tenant_a):
        from apps.auth.services import login_with_password
        from core.exceptions import AuthenticationException

        with schema_context(tenant_a.schema_name):
            with pytest.raises(AuthenticationException):
                login_with_password(username="ghost-user", password="nope", ip="9.9.9.9")
            rows = AuditLog.objects.filter(action=Action.LOGIN_FAILED)
            assert rows.filter(after__username="ghost-user").exists()

    def test_otp_request_and_verify_write_rows(self, tenant_a):
        from apps.users.models import OTP

        with schema_context(tenant_a.schema_name):
            from apps.auth.signals import otp_requested, otp_verified
            from core.utils import current_schema

            otp_requested.send(
                sender=OTP,
                identifier="+998901112233",
                purpose=OTP.PURPOSE_RESET,
                ip="2.2.2.2",
                user_agent="ua",
                schema_name=current_schema(),
            )
            otp_verified.send(
                sender=OTP,
                identifier="+998901112233",
                purpose=OTP.PURPOSE_RESET,
                ip="2.2.2.2",
                user_agent="ua",
                schema_name=current_schema(),
            )
            assert AuditLog.objects.filter(action=Action.OTP_REQUEST).exists()
            assert AuditLog.objects.filter(action=Action.OTP_VERIFY).exists()


# --------------------------------------------------------------------------- #
# Read-only API (D3-D-4): 405s, perms, cross-tenant, pagination
# --------------------------------------------------------------------------- #


class TestAuditAPIAppendOnly:
    @pytest.mark.parametrize("role", [Role.DIRECTOR, Role.IT, Role.SUPPORT, Role.HEAD_OF_DEPT])
    def test_list_allowed_roles(self, as_role, role):
        client, _ = as_role(role)
        assert client.get(AUDIT_URL).status_code == 200

    @pytest.mark.parametrize("role", [Role.TEACHER, Role.STUDENT, Role.CASHIER])
    def test_list_denied_roles(self, as_role, role):
        resp = as_role(role)[0].get(AUDIT_URL)
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"

    def test_anonymous_denied(self, tenant_a, client_for):
        assert client_for(tenant_a).get(AUDIT_URL).status_code == 401

    def test_put_patch_delete_post_405(self, tenant_a, as_role):
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            row = AuditLogFactory()
        detail = f"{AUDIT_URL}{row.pk}/"
        assert client.put(detail, {}, format="json").status_code == 405
        assert client.patch(detail, {}, format="json").status_code == 405
        assert client.delete(detail).status_code == 405
        assert client.post(AUDIT_URL, {}, format="json").status_code == 405

    def test_cross_tenant_token_rejected(self, tenant_a, tenant_b, user_in, client_for):
        from apps.auth.services import issue_token

        user = user_in(tenant_a, roles=[Role.DIRECTOR])
        with schema_context(tenant_a.schema_name):
            access = issue_token(user)["access"]
        client_b = client_for(tenant_b)
        client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = client_b.get(AUDIT_URL)
        assert resp.status_code == 401
        assert resp.json()["code"] == "authentication_failed"

    def test_tenant_isolation_rows_not_leaked(self, tenant_a, tenant_b, as_role):
        with schema_context(tenant_a.schema_name):
            AuditLogFactory(resource_type="secret.A", resource_id="999")
        with schema_context(tenant_b.schema_name):
            AuditLogFactory(resource_type="secret.B", resource_id="888")
        client_b, _ = as_role(Role.DIRECTOR, tenant_b)
        body = client_b.get(AUDIT_URL + "?resource_type=secret.A").json()
        assert body["results"] == []

    @pytest.mark.parametrize("url", [AUDIT_URL, EXPORT_URL])
    @pytest.mark.parametrize("param", ["ts_from", "ts_to"])
    def test_out_of_range_ts_param_is_400_not_500(self, tenant_a, as_role, url, param):
        """A regex-valid but impossible datetime (parse_datetime RAISES ValueError,
        not None) must be a clean 400, never a 500 — on both the list and the export."""
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        resp = client.get(f"{url}?{param}=2026-02-30T00:00:00")
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"

    def test_filters_by_action_and_resource(self, tenant_a, as_role):
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            AuditLogFactory(action=Action.EXPORT, resource_type="audit.AuditLog", resource_id="1")
            AuditLogFactory(action=Action.LOGIN, resource_type="users.User", resource_id="2")
        body = client.get(AUDIT_URL + "?action=export").json()
        assert all(r["action"] == "export" for r in body["results"])
        assert len(body["results"]) >= 1

    def test_cursor_pagination_stable_under_inserts(self, tenant_a, as_role):
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            AuditLog.objects.all().delete()
            base = timezone.now() - timedelta(hours=1)
            for i in range(120):
                row = AuditLogFactory(resource_id=str(i))
                AuditLog.objects.filter(pk=row.pk).update(created_at=base + timedelta(seconds=i))

        page1 = client.get(AUDIT_URL).json()
        assert len(page1["results"]) == 50
        assert page1["next"]
        first_ids = {r["id"] for r in page1["results"]}

        # Insert NEWER rows (top of the -created_at timeline) between page reads.
        with schema_context(tenant_a.schema_name):
            for i in range(10):
                AuditLogFactory(resource_id=f"new{i}")

        page2 = client.get(page1["next"]).json()
        second_ids = {r["id"] for r in page2["results"]}
        # Cursor pagination on -created_at must not re-serve page-1 rows even
        # though newer rows were inserted at the head of the timeline.
        assert first_ids.isdisjoint(second_ids)

    def test_list_envelope_and_query_budget(self, tenant_a, as_role, django_assert_max_num_queries):
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            for i in range(60):
                AuditLogFactory(actor=UserFactory(), resource_id=str(i))
        with django_assert_max_num_queries(9):  # +1: A-2 per-request permission-override load
            body = client.get(AUDIT_URL).json()
        assert set(body) == {"results", "next", "previous"}


# --------------------------------------------------------------------------- #
# CSV export (D3-D-7)
# --------------------------------------------------------------------------- #


class TestAuditExport:
    def test_export_streams_csv_and_audits_itself(self, tenant_a, as_role):
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            AuditLogFactory(resource_type="finance.Invoice", resource_id="42")
            before = AuditLog.objects.filter(action=Action.EXPORT).count()
        resp = client.get(EXPORT_URL)
        assert resp.status_code == 200
        assert resp["Content-Type"] == "text/csv"
        content = b"".join(resp.streaming_content).decode()
        assert content.splitlines()[0].startswith("id,created_at,actor_id")
        with schema_context(tenant_a.schema_name):
            after = AuditLog.objects.filter(action=Action.EXPORT).count()
        assert after == before + 1

    def test_export_neutralizes_csv_formula_injection(self, tenant_a, as_role):
        """R6/PLAUS3: an attacker-controlled User-Agent / actor_repr beginning with a
        formula char (= + - @) must be neutralized (apostrophe-prefixed) in the export so
        it can't execute when an admin opens the spreadsheet."""
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            AuditLogFactory(user_agent="=cmd|'/c calc'!A1", actor_repr="@SUM(A1:A9)")
        resp = client.get(EXPORT_URL)
        content = b"".join(resp.streaming_content).decode()
        assert "'=cmd|" in content  # user_agent guarded
        assert "'@SUM(" in content  # actor_repr guarded
        assert "\n=cmd|" not in content  # never a bare formula cell at row start
        assert ",=cmd|" not in content  # nor mid-row

    def test_export_over_cap_400(self, tenant_a, as_role, monkeypatch):
        import apps.audit.views.v1.audit_views as views

        monkeypatch.setattr(views, "MAX_EXPORT_ROWS", 1)
        client, _ = as_role(Role.DIRECTOR, tenant_a)
        with schema_context(tenant_a.schema_name):
            AuditLogFactory(resource_id="a")
            AuditLogFactory(resource_id="b")
        resp = client.get(EXPORT_URL)
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"

    def test_export_denied_for_non_audit_role(self, tenant_a, as_role):
        client, _ = as_role(Role.TEACHER, tenant_a)
        assert client.get(EXPORT_URL).status_code == 403


# --------------------------------------------------------------------------- #
# Retention beat task (D3-D-6)
# --------------------------------------------------------------------------- #


class TestRetentionTask:
    def test_deletes_correct_cohorts(self, tenant_a):
        from celery_tasks.audit_tasks import cleanup_old_audit_logs_for_schema

        with schema_context(tenant_a.schema_name):
            AuditLog.objects.all().delete()
            now = timezone.now()

            def _aged(*, resource_type, days_ago, resource_id):
                row = AuditLogFactory(resource_type=resource_type, resource_id=resource_id)
                AuditLog.objects.filter(pk=row.pk).update(created_at=now - timedelta(days=days_ago))
                return row.pk

            # Long-retention type, 6y old -> KEPT (< 7y).
            keep_long = _aged(resource_type="finance.Invoice", days_ago=365 * 6, resource_id="1")
            # Long-retention type, 8y old -> DELETED (> 7y).
            del_long = _aged(resource_type="payments.Payment", days_ago=365 * 8, resource_id="2")
            # Short type, 6 months old -> KEPT (< 1y).
            keep_short = _aged(resource_type="users.User", days_ago=180, resource_id="3")
            # Short type, 2y old -> DELETED (> 1y).
            del_short = _aged(resource_type="users.User", days_ago=365 * 2, resource_id="4")

            deleted = cleanup_old_audit_logs_for_schema()
            assert deleted == 2
            remaining = set(AuditLog.objects.values_list("pk", flat=True))
            assert keep_long in remaining
            assert keep_short in remaining
            assert del_long not in remaining
            assert del_short not in remaining

    def test_retention_idempotent_second_run_deletes_zero(self, tenant_a):
        from celery_tasks.audit_tasks import cleanup_old_audit_logs_for_schema

        with schema_context(tenant_a.schema_name):
            AuditLog.objects.all().delete()
            row = AuditLogFactory(resource_type="users.User", resource_id="x")
            AuditLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=365 * 3))
            assert cleanup_old_audit_logs_for_schema() == 1
            assert cleanup_old_audit_logs_for_schema() == 0

    def test_dispatcher_fans_out_per_active_center(self, tenant_a, tenant_b):
        from celery_tasks.audit_tasks import cleanup_old_audit_logs

        # eager mode: the dispatcher returns the active-center count it fanned to.
        count = cleanup_old_audit_logs()
        assert count >= 2
