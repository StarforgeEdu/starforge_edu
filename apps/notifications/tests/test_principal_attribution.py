"""Role-principal ownership, quarantine, backfill, and delivery regressions."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.notifications.models import (
    Channel,
    EventType,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    RecipientAttributionStatus,
)

pytestmark = pytest.mark.django_db


def _multi_role_account(tenant):
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory()
        user = UserFactory()
        student = StudentProfileFactory(user=user, branch=branch)
        staff = StaffProfile.objects.create(
            user=user,
            username=f"staff.{user.username}",
            password=user.password,
            first_name=user.first_name,
            last_name=user.last_name,
            phone="",
            email="",
        )
        RoleMembership.objects.create(user=user, branch=branch, role="student")
        RoleMembership.objects.create(user=user, branch=branch, role="director")
        return user, student, staff


def _client_for_principal(tenant, client_for, user, *, kind: str, principal_id: int):
    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        session = create_session(user, principal_kind=kind, principal_id=principal_id)
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    return client


def test_same_bridge_user_has_separate_http_feeds_and_read_receipts(tenant_a, client_for):
    from apps.notifications.services import dispatch

    user, student, staff = _multi_role_account(tenant_a)
    with schema_context(tenant_a.schema_name):
        student_notification = dispatch(
            event_type=EventType.REPORT_READY,
            recipient_id=user.pk,
            recipient_principal_kind="student",
            recipient_principal_id=student.pk,
            context={"scope": "student"},
            dedupe_key="same-domain-event",
            channels=[Channel.IN_APP],
        )
        staff_notification = dispatch(
            event_type=EventType.REPORT_READY,
            recipient_id=user.pk,
            recipient_principal_kind="staff",
            recipient_principal_id=staff.pk,
            context={"scope": "staff"},
            dedupe_key="same-domain-event",
            channels=[Channel.IN_APP],
        )
        assert student_notification is not None
        assert staff_notification is not None
        assert Notification.objects.filter(user=user, dedupe_key="same-domain-event").count() == 2

    student_client = _client_for_principal(
        tenant_a,
        client_for,
        user,
        kind="student",
        principal_id=student.pk,
    )
    staff_client = _client_for_principal(
        tenant_a,
        client_for,
        user,
        kind="staff",
        principal_id=staff.pk,
    )

    student_ids = {row["id"] for row in student_client.get("/api/v1/notifications/").json()["results"]}
    staff_ids = {row["id"] for row in staff_client.get("/api/v1/notifications/").json()["results"]}
    assert student_ids == {student_notification.pk}
    assert staff_ids == {staff_notification.pk}
    assert student_client.get("/api/v1/notifications/unread-count/").json()["data"]["count"] == 1
    assert staff_client.get("/api/v1/notifications/unread-count/").json()["data"]["count"] == 1

    hidden = student_client.post(f"/api/v1/notifications/{staff_notification.pk}/read/")
    assert hidden.status_code == 404
    assert student_client.post(f"/api/v1/notifications/{student_notification.pk}/read/").status_code == 200
    assert staff_client.get("/api/v1/notifications/unread-count/").json()["data"]["count"] == 1


def test_ambiguous_or_invalid_dispatch_is_quarantined(tenant_a):
    from apps.notifications.services import dispatch
    from apps.students.tests.factories import StudentProfileFactory

    user, _student, _staff = _multi_role_account(tenant_a)
    with schema_context(tenant_a.schema_name):
        ambiguous = dispatch(
            event_type=EventType.REPORT_READY,
            recipient_id=user.pk,
            context={},
            dedupe_key="ambiguous-recipient",
        )
        other_student = StudentProfileFactory()
        invalid = dispatch(
            event_type=EventType.REPORT_READY,
            recipient_id=user.pk,
            recipient_principal_kind="student",
            recipient_principal_id=other_student.pk,
            context={},
            dedupe_key="invalid-recipient",
        )
        assert ambiguous is not None
        assert invalid is not None
        assert ambiguous.attribution_status == RecipientAttributionStatus.CONFLICTING
        assert invalid.attribution_status == RecipientAttributionStatus.QUARANTINED
        assert not NotificationDelivery.objects.filter(
            notification_id__in=(ambiguous.pk, invalid.pk)
        ).exists()


def test_backfill_resolves_only_single_role_evidence(tenant_a):
    from apps.org.models import StaffProfile
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        resolvable_user = UserFactory()
        parent = ParentProfileFactory(user=resolvable_user)
        legacy_resolvable = Notification(
            user=resolvable_user,
            event_type=EventType.REPORT_READY,
            title="legacy parent",
        )
        Notification.objects.bulk_create([legacy_resolvable])

        ambiguous_user = UserFactory()
        ParentProfileFactory(user=ambiguous_user)
        StaffProfile.objects.create(
            user=ambiguous_user,
            username=f"staff.{ambiguous_user.username}",
            password=ambiguous_user.password,
        )
        legacy_ambiguous = Notification(
            user=ambiguous_user,
            event_type=EventType.REPORT_READY,
            title="legacy ambiguous",
        )
        Notification.objects.bulk_create([legacy_ambiguous])
    stdout = StringIO()
    call_command(
        "backfill_notification_principals",
        schema_names=[tenant_a.schema_name],
        apply=True,
        stdout=stdout,
    )

    with schema_context(tenant_a.schema_name):
        legacy_resolvable.refresh_from_db()
        legacy_ambiguous.refresh_from_db()
        assert legacy_resolvable.attribution_status == RecipientAttributionStatus.RESOLVED
        assert legacy_resolvable.recipient_principal_kind == "parent"
        assert legacy_resolvable.recipient_principal_id == parent.pk
        assert legacy_ambiguous.attribution_status == RecipientAttributionStatus.CONFLICTING
        assert legacy_ambiguous.recipient_principal_kind is None
        assert legacy_ambiguous.recipient_principal_id is None


def test_backfill_defaults_to_dry_run(tenant_a):
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = UserFactory()
        ParentProfileFactory(user=user)
        legacy = Notification(
            user=user,
            event_type=EventType.REPORT_READY,
            title="review only",
        )
        Notification.objects.bulk_create([legacy])

    call_command(
        "backfill_notification_principals",
        schema=tenant_a.schema_name,
        stdout=StringIO(),
    )

    with schema_context(tenant_a.schema_name):
        legacy.refresh_from_db()
        assert legacy.attribution_status == RecipientAttributionStatus.UNRESOLVED
        assert legacy.recipient_principal_kind is None
        assert legacy.recipient_principal_id is None


def test_backfill_cross_batch_duplicates_match_review_and_apply(tenant_a, tmp_path):
    """Global identity conflicts cannot depend on the operator's batch size."""
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = UserFactory()
        ParentProfileFactory(user=user)
        rows = [
            Notification(
                user=user,
                event_type=EventType.REPORT_READY,
                title=f"legacy duplicate {index}",
                dedupe_key="cross-batch-duplicate",
            )
            for index in range(2)
        ]
        Notification.objects.bulk_create(rows)

    report_path = tmp_path / "notification-review.json"
    call_command(
        "backfill_notification_principals",
        schema=tenant_a.schema_name,
        batch_size=1,
        report=str(report_path),
        stdout=StringIO(),
    )
    review = json.loads(report_path.read_text())
    reviewed_rows = [row for row in review["rows"] if row["id"] in {item.pk for item in rows}]
    assert len(reviewed_rows) == 2
    assert {row["status"] for row in reviewed_rows} == {RecipientAttributionStatus.CONFLICTING}

    call_command(
        "backfill_notification_principals",
        schema=tenant_a.schema_name,
        batch_size=1,
        apply=True,
        stdout=StringIO(),
    )
    with schema_context(tenant_a.schema_name):
        assert set(
            Notification.objects.filter(pk__in=[row.pk for row in rows]).values_list(
                "attribution_status",
                flat=True,
            )
        ) == {RecipientAttributionStatus.CONFLICTING}


def test_batch_principal_resolution_has_constant_query_count(
    tenant_a,
    django_assert_num_queries,
):
    from apps.notifications.principals import resolve_recipient_principals
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user_ids = []
        for _ in range(30):
            user = UserFactory()
            ParentProfileFactory(user=user)
            user_ids.append(user.pk)

        # One user query, four role-profile queries, and one historical
        # membership-evidence query regardless of the batch population.
        with django_assert_num_queries(6):
            resolutions = resolve_recipient_principals(user_ids)
        assert len(resolutions) == len(user_ids)
        assert all(resolution.is_deliverable for resolution in resolutions.values())


def test_database_guards_snapshot_and_quarantined_delivery(tenant_a):
    from apps.notifications.tests.helpers import ensure_notification_principal, principal_kwargs
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = ensure_notification_principal(UserFactory())
        notification = Notification.objects.create(
            user=user,
            event_type=EventType.REPORT_READY,
            title="captured",
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            Notification.objects.filter(pk=notification.pk).update(
                recipient_principal_id=user.notification_principal_id + 1
            )

        other_user = ensure_notification_principal(UserFactory())
        with pytest.raises(DatabaseError), transaction.atomic():
            Notification.objects.filter(pk=notification.pk).update(user=other_user)

        # bulk_create bypasses model.save; the database still rejects a forged
        # exact role snapshot that does not belong to the bridge user.
        with pytest.raises(DatabaseError), transaction.atomic():
            Notification.objects.bulk_create(
                [
                    Notification(
                        user=user,
                        event_type=EventType.REPORT_READY,
                        title="forged",
                        recipient_principal_kind="staff",
                        recipient_principal_id=other_user.notification_principal_id,
                        attribution_status=RecipientAttributionStatus.CAPTURED,
                    )
                ]
            )

        inactive_user = ensure_notification_principal(UserFactory())
        type(inactive_user).objects.filter(pk=inactive_user.pk).update(is_active=False)
        with pytest.raises(DatabaseError), transaction.atomic():
            Notification.objects.bulk_create(
                [
                    Notification(
                        user=inactive_user,
                        event_type=EventType.REPORT_READY,
                        title="inactive bridge",
                        attribution_status=RecipientAttributionStatus.CAPTURED,
                        **principal_kwargs(inactive_user),
                    )
                ]
            )

        unresolved = Notification(
            user=UserFactory(),
            event_type=EventType.REPORT_READY,
            title="legacy unresolved",
        )
        Notification.objects.bulk_create([unresolved])
        with pytest.raises(DatabaseError), transaction.atomic():
            NotificationDelivery.objects.create(
                notification=unresolved,
                channel=Channel.IN_APP,
                status=NotificationDelivery.Status.SENT,
            )


def test_delivery_revalidates_live_role_principal(tenant_a):
    from apps.notifications.tests.helpers import ensure_notification_principal
    from apps.users.tests.factories import UserFactory
    from celery_tasks.notification_tasks import dispatch_notification

    with schema_context(tenant_a.schema_name):
        user = ensure_notification_principal(UserFactory())
        notification = Notification.objects.create(
            user=user,
            event_type=EventType.REPORT_READY,
            title="no longer authorized",
        )
        from apps.org.models import StaffProfile

        StaffProfile.objects.filter(pk=user.notification_principal_id).update(is_active=False)
        result = dispatch_notification(notification.pk, channels=[Channel.IN_APP])

        assert result["status"] == "recipient_inactive"
        assert not NotificationDelivery.objects.filter(notification=notification).exists()


def test_guardian_revocation_blocks_already_queued_family_delivery(tenant_a, monkeypatch):
    """A historical parent snapshot is not current authority to contact a family.

    The guardian relationship may be revoked after dispatch is queued (for example,
    during quiet hours). The worker must re-check that exact relationship immediately
    before any external adapter runs.
    """
    import celery_tasks.notification_tasks as notification_tasks
    from apps.parents.tests.factories import GuardianFactory

    monkeypatch.setattr(
        notification_tasks,
        "_deliver",
        lambda *_args, **_kwargs: pytest.fail("revoked guardian reached an external adapter"),
    )
    with schema_context(tenant_a.schema_name):
        guardian = GuardianFactory(is_primary=True)
        notification = Notification.objects.create(
            user=guardian.parent.user,
            recipient_principal_kind="parent",
            recipient_principal_id=guardian.parent_id,
            event_type=EventType.ATTENDANCE_ABSENT,
            title="Attendance update",
            data={"student_id": guardian.student_id},
        )
        guardian.revoked_at = timezone.now()
        guardian.save(update_fields=["revoked_at"])

        result = notification_tasks.dispatch_notification(
            notification.pk,
            channels=[Channel.SMS],
        )

        assert result["status"] == "recipient_inactive"
        assert not NotificationDelivery.objects.filter(notification=notification).exists()


def test_explicit_empty_channel_whitelist_creates_no_delivery(
    tenant_a,
    django_capture_on_commit_callbacks,
):
    """An explicit ``[]`` is a deny-all whitelist, never an alias for defaults."""
    from apps.notifications.services import dispatch
    from apps.notifications.tests.helpers import ensure_notification_principal, principal_kwargs
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name), django_capture_on_commit_callbacks(execute=True):
        user = ensure_notification_principal(UserFactory())
        notification = dispatch(
            event_type=EventType.REPORT_READY,
            recipient_id=user.pk,
            context={},
            channels=[],
            **principal_kwargs(user),
        )

        assert notification is not None
        assert not NotificationDelivery.objects.filter(notification=notification).exists()


def test_upsert_preserves_resolved_preference_attribution(tenant_a):
    """Changing an opt-in must not rewrite an immutable backfill provenance."""
    from apps.notifications.services import upsert_preferences
    from apps.notifications.tests.helpers import ensure_notification_principal, principal_kwargs
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = ensure_notification_principal(UserFactory())
        principal = principal_kwargs(user)
        NotificationPreference.objects.bulk_create(
            [
                NotificationPreference(
                    user=user,
                    event_type=EventType.REPORT_READY,
                    channel=Channel.IN_APP,
                    enabled=True,
                    attribution_status=RecipientAttributionStatus.RESOLVED,
                    **principal,
                )
            ]
        )

        [preference] = upsert_preferences(
            user=user,
            rows=[
                {
                    "event_type": EventType.REPORT_READY,
                    "channel": Channel.IN_APP,
                    "enabled": False,
                }
            ],
            **principal,
        )
        preference.refresh_from_db()

        assert preference.enabled is False
        assert preference.attribution_status == RecipientAttributionStatus.RESOLVED
