from django.db import migrations, models

_TEMPLATES = {
    "uz": (
        "Imtihon darsi yaqinlashmoqda",
        "$lesson_title guruh siklining yakuniy imtihon darsi. Boshlanish vaqti: $starts_at.",
    ),
    "ru": (
        "Приближается экзаменационный урок",
        "$lesson_title — итоговый экзаменационный урок цикла. Начало: $starts_at.",
    ),
    "en": (
        "Exam lesson coming up",
        "$lesson_title is the final exam lesson in this cycle. It starts at $starts_at.",
    ),
}


def seed_cycle_exam_templates(apps, schema_editor):
    template = apps.get_model("notifications", "NotificationTemplate")
    for channel in ("in_app", "push"):
        for locale, (subject, body) in _TEMPLATES.items():
            template.objects.update_or_create(
                event_type="schedule.cycle_exam_reminder",
                channel=channel,
                locale=locale,
                defaults={"subject": subject, "body": body, "is_active": True},
            )


def unseed_cycle_exam_templates(apps, schema_editor):
    template = apps.get_model("notifications", "NotificationTemplate")
    template.objects.filter(event_type="schedule.cycle_exam_reminder").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0014_rename_delivery_status_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("attendance.absent", "Attendance: absent"),
                    ("attendance.late", "Attendance: late"),
                    ("academics.grades_published", "Academics: grades published"),
                    ("assignments.created", "Assignment created"),
                    ("assignments.due_soon", "Assignment due soon"),
                    ("assignments.graded", "Assignment graded"),
                    ("schedule.lesson_reminder", "Lesson reminder"),
                    ("schedule.cycle_exam_reminder", "Cycle exam reminder"),
                    ("auth.new_device_login", "New device login"),
                    ("students.enrollment_changed", "Enrollment changed"),
                    ("finance.invoice_issued", "Invoice issued"),
                    ("finance.payment_reminder", "Payment reminder"),
                    ("payments.payment_completed", "Payment completed"),
                    ("payments.payment_failed", "Payment failed"),
                    ("cohorts.announcement", "Cohort announcement"),
                    ("billing.subscription_past_due", "Subscription past due"),
                    ("billing.subscription_suspended", "Subscription suspended"),
                    ("print.failed", "Print job failed"),
                    ("approval.approved", "Request approved"),
                    ("approval.rejected", "Request rejected"),
                    ("approval.awaiting_disbursement", "Approved — awaiting disbursement"),
                    ("approval.disbursed", "Request disbursed"),
                    ("penalty.escalated", "Penalty: escalation threshold crossed"),
                    ("message.received", "Message received"),
                    ("report.ready", "Report ready"),
                    ("cover.requested", "Cover requested"),
                    ("cover.approved", "Cover approved"),
                    ("cover.pool_opened", "Cover pool opened"),
                    ("cover.rejected", "Cover rejected"),
                ],
                db_index=True,
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="notificationpreference",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("attendance.absent", "Attendance: absent"),
                    ("attendance.late", "Attendance: late"),
                    ("academics.grades_published", "Academics: grades published"),
                    ("assignments.created", "Assignment created"),
                    ("assignments.due_soon", "Assignment due soon"),
                    ("assignments.graded", "Assignment graded"),
                    ("schedule.lesson_reminder", "Lesson reminder"),
                    ("schedule.cycle_exam_reminder", "Cycle exam reminder"),
                    ("auth.new_device_login", "New device login"),
                    ("students.enrollment_changed", "Enrollment changed"),
                    ("finance.invoice_issued", "Invoice issued"),
                    ("finance.payment_reminder", "Payment reminder"),
                    ("payments.payment_completed", "Payment completed"),
                    ("payments.payment_failed", "Payment failed"),
                    ("cohorts.announcement", "Cohort announcement"),
                    ("billing.subscription_past_due", "Subscription past due"),
                    ("billing.subscription_suspended", "Subscription suspended"),
                    ("print.failed", "Print job failed"),
                    ("approval.approved", "Request approved"),
                    ("approval.rejected", "Request rejected"),
                    ("approval.awaiting_disbursement", "Approved — awaiting disbursement"),
                    ("approval.disbursed", "Request disbursed"),
                    ("penalty.escalated", "Penalty: escalation threshold crossed"),
                    ("message.received", "Message received"),
                    ("report.ready", "Report ready"),
                    ("cover.requested", "Cover requested"),
                    ("cover.approved", "Cover approved"),
                    ("cover.pool_opened", "Cover pool opened"),
                    ("cover.rejected", "Cover rejected"),
                ],
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("attendance.absent", "Attendance: absent"),
                    ("attendance.late", "Attendance: late"),
                    ("academics.grades_published", "Academics: grades published"),
                    ("assignments.created", "Assignment created"),
                    ("assignments.due_soon", "Assignment due soon"),
                    ("assignments.graded", "Assignment graded"),
                    ("schedule.lesson_reminder", "Lesson reminder"),
                    ("schedule.cycle_exam_reminder", "Cycle exam reminder"),
                    ("auth.new_device_login", "New device login"),
                    ("students.enrollment_changed", "Enrollment changed"),
                    ("finance.invoice_issued", "Invoice issued"),
                    ("finance.payment_reminder", "Payment reminder"),
                    ("payments.payment_completed", "Payment completed"),
                    ("payments.payment_failed", "Payment failed"),
                    ("cohorts.announcement", "Cohort announcement"),
                    ("billing.subscription_past_due", "Subscription past due"),
                    ("billing.subscription_suspended", "Subscription suspended"),
                    ("print.failed", "Print job failed"),
                    ("approval.approved", "Request approved"),
                    ("approval.rejected", "Request rejected"),
                    ("approval.awaiting_disbursement", "Approved — awaiting disbursement"),
                    ("approval.disbursed", "Request disbursed"),
                    ("penalty.escalated", "Penalty: escalation threshold crossed"),
                    ("message.received", "Message received"),
                    ("report.ready", "Report ready"),
                    ("cover.requested", "Cover requested"),
                    ("cover.approved", "Cover approved"),
                    ("cover.pool_opened", "Cover pool opened"),
                    ("cover.rejected", "Cover rejected"),
                ],
                db_index=True,
                max_length=64,
            ),
        ),
        migrations.RunPython(seed_cycle_exam_templates, unseed_cycle_exam_templates),
    ]
