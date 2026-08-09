"""Migration coverage for immutable workflow creator attribution."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role
from tests.migration_isolation import IsolatedMigrationHarness

pytestmark = pytest.mark.django_db(transaction=True)

LEGACY_TARGETS = (
    ("forms_app", "0002_form_audience_roles_form_audience_user_ids"),
    ("staff_tasks", "0002_task_task_created_idx"),
    ("meetings", "0001_initial"),
)
CURRENT_TARGETS = (
    ("forms_app", "0003_role_principal_attribution"),
    ("staff_tasks", "0003_task_assignee_principal"),
    ("meetings", "0002_attendee_principal_attribution"),
)


def _legacy_models():
    executor = MigrationExecutor(connection)
    state = executor.loader.project_state(list(LEGACY_TARGETS))
    return (
        state.apps.get_model("forms_app", "Form"),
        state.apps.get_model("staff_tasks", "Task"),
        state.apps.get_model("meetings", "StaffMeeting"),
    )


def test_creator_backfills_resolve_one_role_and_quarantine_ambiguity(
    tenant_a,
    user_in,
):
    from apps.org.tests.factories import BranchFactory
    from tests.role_principal_helpers import shared_staff_teacher_bridge

    row_ids: dict[str, int] = {}
    migrations = IsolatedMigrationHarness(connection, CURRENT_TARGETS)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            exact_user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
            ambiguous_user, _teacher, _staff = shared_staff_teacher_bridge(
                branch=branch,
                staff_role=Role.SUPPORT,
            )

            migrations.downgrade()
            LegacyForm, LegacyTask, LegacyMeeting = _legacy_models()
            exact_form = LegacyForm.objects.create(
                title="Exact legacy form",
                created_by_id=exact_user.pk,
            )
            ambiguous_form = LegacyForm.objects.create(
                title="Ambiguous legacy form",
                created_by_id=ambiguous_user.pk,
            )
            exact_task = LegacyTask.objects.create(
                title="Exact legacy task",
                created_by_id=exact_user.pk,
            )
            ambiguous_task = LegacyTask.objects.create(
                title="Ambiguous legacy task",
                created_by_id=ambiguous_user.pk,
            )
            start = timezone.now() + timedelta(days=1)
            exact_meeting = LegacyMeeting.objects.create(
                title="Exact legacy meeting",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                created_by_id=exact_user.pk,
                status="cancelled",
                cancelled_by_id=exact_user.pk,
                cancelled_at=timezone.now(),
            )
            ambiguous_meeting = LegacyMeeting.objects.create(
                title="Ambiguous legacy meeting",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                created_by_id=ambiguous_user.pk,
                status="cancelled",
                cancelled_by_id=ambiguous_user.pk,
                cancelled_at=timezone.now(),
            )
            row_ids = {
                "exact_form": exact_form.pk,
                "ambiguous_form": ambiguous_form.pk,
                "exact_task": exact_task.pk,
                "ambiguous_task": ambiguous_task.pk,
                "exact_meeting": exact_meeting.pk,
                "ambiguous_meeting": ambiguous_meeting.pk,
            }

            migrations.upgrade()

            from apps.forms.models import Form
            from apps.meetings.models import StaffMeeting
            from apps.tasks.models import Task

            exact_form_row = Form.objects.get(pk=row_ids["exact_form"])
            assert exact_form_row.created_by_principal_kind == "staff"
            assert exact_form_row.created_by_principal_id == exact_user.test_principal_id
            assert exact_form_row.created_by_attribution_status == "resolved"
            ambiguous_form_row = Form.objects.get(pk=row_ids["ambiguous_form"])
            assert ambiguous_form_row.created_by_principal_kind == ""
            assert ambiguous_form_row.created_by_principal_id is None
            assert ambiguous_form_row.created_by_attribution_status == "quarantined"

            exact_task_row = Task.objects.get(pk=row_ids["exact_task"])
            assert exact_task_row.created_by_principal_kind == "staff"
            assert exact_task_row.created_by_principal_id == exact_user.test_principal_id
            assert exact_task_row.created_by_attribution_status == "resolved"
            ambiguous_task_row = Task.objects.get(pk=row_ids["ambiguous_task"])
            assert ambiguous_task_row.created_by_principal_kind == ""
            assert ambiguous_task_row.created_by_principal_id is None
            assert ambiguous_task_row.created_by_attribution_status == "quarantined"

            exact_meeting_row = StaffMeeting.objects.get(pk=row_ids["exact_meeting"])
            assert exact_meeting_row.created_by_principal_kind == "staff"
            assert exact_meeting_row.created_by_principal_id == exact_user.test_principal_id
            assert exact_meeting_row.created_by_attribution_status == "resolved"
            assert exact_meeting_row.cancelled_by_principal_kind == "staff"
            assert exact_meeting_row.cancelled_by_principal_id == exact_user.test_principal_id
            assert exact_meeting_row.cancelled_by_attribution_status == "resolved"
            ambiguous_meeting_row = StaffMeeting.objects.get(pk=row_ids["ambiguous_meeting"])
            assert ambiguous_meeting_row.created_by_principal_kind == ""
            assert ambiguous_meeting_row.created_by_principal_id is None
            assert ambiguous_meeting_row.created_by_attribution_status == "quarantined"
            assert ambiguous_meeting_row.cancelled_by_principal_kind == ""
            assert ambiguous_meeting_row.cancelled_by_principal_id is None
            assert ambiguous_meeting_row.cancelled_by_attribution_status == "quarantined"
    finally:
        try:
            with schema_context(tenant_a.schema_name):
                migrations.downgrade()
                LegacyForm, LegacyTask, LegacyMeeting = _legacy_models()
                if row_ids:
                    LegacyForm.objects.filter(
                        pk__in=(row_ids["exact_form"], row_ids["ambiguous_form"])
                    ).delete()
                    LegacyTask.objects.filter(
                        pk__in=(row_ids["exact_task"], row_ids["ambiguous_task"])
                    ).delete()
                    LegacyMeeting.objects.filter(
                        pk__in=(row_ids["exact_meeting"], row_ids["ambiguous_meeting"])
                    ).delete()
                migrations.upgrade()
        finally:
            connection.set_schema_to_public()
