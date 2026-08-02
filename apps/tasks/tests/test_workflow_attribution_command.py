"""Release-gate coverage for workflow principal-attribution reporting."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_workflow_attribution_report_counts_resolved_and_review_rows(tenant_a, user_in):
    from apps.forms.models import Form, FormResponse
    from apps.forms.presenters import form_to_dict
    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.meetings.presenters import meeting_to_dict
    from apps.org.tests.factories import BranchFactory
    from apps.tasks.management.commands.check_workflow_principal_attribution import (
        _schema_report,
    )
    from apps.tasks.models import Task
    from apps.tasks.presenters import task_to_dict

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    principal_id = user.test_principal_id
    now = timezone.now()

    with schema_context(tenant_a.schema_name):
        resolved_form = Form.objects.create(
            title="Resolved audience",
            audience_user_ids=[user.pk],
            audience_principals=[{"kind": "staff", "id": principal_id, "user_id": user.pk}],
        )
        unresolved_form = Form.objects.create(
            title="Unresolved audience",
            audience_user_ids=[user.pk],
        )
        FormResponse.objects.create(
            form=resolved_form,
            respondent=user,
            respondent_principal_kind="staff",
            respondent_principal_id=principal_id,
            respondent_attribution_status=FormResponse.AttributionStatus.CAPTURED,
        )
        FormResponse.objects.create(form=unresolved_form, respondent=user)

        meeting = StaffMeeting.objects.create(
            title="Attribution",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=1),
        )
        MeetingAttendee.objects.create(
            meeting=meeting,
            user=user,
            principal_kind="staff",
            principal_id=principal_id,
        )
        second_meeting = StaffMeeting.objects.create(
            title="Needs review",
            starts_at=now + timedelta(days=2),
            ends_at=now + timedelta(days=2, hours=1),
        )
        MeetingAttendee.objects.create(meeting=second_meeting, user=user)

        quarantined_task = Task.objects.create(
            title="Resolved task",
            assignee=user,
            assignee_principal_kind="staff",
            assignee_principal_id=principal_id,
        )
        Task.objects.create(
            title="Quarantined task",
            assignee=user,
            assignee_attribution_status="quarantined",
        )
        report = _schema_report(tenant_a.schema_name, batch_size=1)

        unresolved_form_payload = form_to_dict(unresolved_form)
        unresolved_meeting_payload = meeting_to_dict(second_meeting)
        quarantined_task_payload = task_to_dict(quarantined_task)

    assert report["form_audience_resolved"] == 1
    assert report["form_audience_unresolved"] == 1
    assert report["form_response_resolved"] == 1
    assert report["form_response_unresolved"] == 1
    assert report["meeting_attendee_resolved"] == 1
    assert report["meeting_attendee_unresolved"] == 1
    assert report["task_assignee_resolved"] == 1
    assert report["task_assignee_quarantined"] == 1
    assert unresolved_form_payload["audience_user_ids"] == []
    assert unresolved_form_payload["audience_unresolved_count"] == 1
    assert unresolved_meeting_payload["attendees"] == []
    assert unresolved_meeting_payload["unresolved_attendee_count"] == 1
    assert quarantined_task_payload["assignee"] is None


def test_workflow_report_rejects_wrong_owner_and_inactive_principal_pairs(tenant_a, user_in):
    from django.db import connection

    from apps.forms.models import Form, FormResponse
    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.tasks.management.commands.check_workflow_principal_attribution import (
        _schema_report,
    )
    from apps.tasks.models import Task

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    owner = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    other = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    now = timezone.now()

    with schema_context(tenant_a.schema_name):
        form = Form.objects.create(
            title="Wrong owner",
            audience_user_ids=[owner.pk],
            audience_principals=[{"kind": "staff", "id": other.test_principal_id, "user_id": owner.pk}],
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE forms_app_formresponse DISABLE TRIGGER forms_response_principal_guard"
            )
            try:
                FormResponse.objects.create(
                    form=form,
                    respondent=owner,
                    respondent_principal_kind="staff",
                    respondent_principal_id=other.test_principal_id,
                    respondent_attribution_status=FormResponse.AttributionStatus.CAPTURED,
                )
            finally:
                cursor.execute(
                    "ALTER TABLE forms_app_formresponse ENABLE TRIGGER forms_response_principal_guard"
                )
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE staff_tasks_task DISABLE TRIGGER tasks_assignee_principal_guard")
            try:
                Task.objects.create(
                    title="Wrong owner",
                    assignee=owner,
                    assignee_principal_kind="staff",
                    assignee_principal_id=other.test_principal_id,
                )
            finally:
                cursor.execute("ALTER TABLE staff_tasks_task ENABLE TRIGGER tasks_assignee_principal_guard")
        meeting = StaffMeeting.objects.create(
            title="Corruption probe",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=1),
        )
        attendee = MeetingAttendee.objects.create(
            meeting=meeting,
            user=owner,
            principal_kind="staff",
            principal_id=owner.test_principal_id,
        )
        # Simulate pre-guard corruption. The release checker must inspect ownership,
        # not merely trust a non-null pair. Re-enable the trigger immediately.
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE meetings_meetingattendee DISABLE TRIGGER meetings_attendee_principal_guard"
            )
            try:
                cursor.execute(
                    "UPDATE meetings_meetingattendee SET principal_id = %s WHERE id = %s",
                    [other.test_principal_id, attendee.pk],
                )
            finally:
                cursor.execute(
                    "ALTER TABLE meetings_meetingattendee ENABLE TRIGGER meetings_attendee_principal_guard"
                )

        report = _schema_report(tenant_a.schema_name, batch_size=1)
        assert report["form_audience_unresolved"] == 1
        assert report["form_response_unresolved"] == 1
        assert report["meeting_attendee_unresolved"] == 1
        assert report["task_assignee_quarantined"] == 1

        # Liveness is part of the same release gate; a once-valid snapshot stops
        # counting as resolved when its exact account is deactivated.
        StaffProfile.objects.filter(pk=other.test_principal_id).update(is_active=False)
        report = _schema_report(tenant_a.schema_name, batch_size=1)
        assert report["form_response_unresolved"] == 1
        assert report["task_assignee_quarantined"] == 1
