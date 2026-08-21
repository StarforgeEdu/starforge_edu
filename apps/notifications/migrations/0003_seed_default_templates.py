# Day-3 Lane C: seed default notification templates (uz/ru/en) for every event
# type, per channel. Idempotent (update_or_create on the unique triple).
# Bodies use string.Template ($placeholder) — rendered via safe_substitute.

from django.db import migrations

# (event_type, channel) -> {locale: (subject, body)}
# Channels seeded: in_app (always), plus sms/email/push where the default matrix
# enables them. Placeholders are flat context keys passed by receivers.
_TEMPLATES = {
    ("attendance.absent", "in_app"): {
        "uz": ("Davomat", "Farzandingiz darsda yo'q ($lesson_id)."),
        "ru": ("Посещаемость", "Ваш ребёнок отсутствовал на уроке ($lesson_id)."),
        "en": ("Attendance", "Your child was absent from a lesson ($lesson_id)."),
    },
    ("attendance.absent", "sms"): {
        "uz": ("", "Farzandingiz bugun darsda qatnashmadi."),
        "ru": ("", "Ваш ребёнок сегодня отсутствовал на уроке."),
        "en": ("", "Your child was absent from class today."),
    },
    ("attendance.absent", "push"): {
        "uz": ("Davomat", "Farzandingiz darsda yo'q."),
        "ru": ("Посещаемость", "Ваш ребёнок отсутствовал."),
        "en": ("Attendance", "Your child was absent."),
    },
    ("academics.grades_published", "in_app"): {
        "uz": ("Baholar", "Yangi baho e'lon qilindi: $new_score."),
        "ru": ("Оценки", "Опубликована новая оценка: $new_score."),
        "en": ("Grades", "A new grade was published: $new_score."),
    },
    ("academics.grades_published", "push"): {
        "uz": ("Baholar", "Yangi baho: $new_score."),
        "ru": ("Оценки", "Новая оценка: $new_score."),
        "en": ("Grades", "New grade: $new_score."),
    },
    ("assignments.created", "in_app"): {
        "uz": ("Yangi topshiriq", "Yangi topshiriq qo'shildi ($assignment_id)."),
        "ru": ("Новое задание", "Добавлено новое задание ($assignment_id)."),
        "en": ("New assignment", "A new assignment was posted ($assignment_id)."),
    },
    ("assignments.created", "push"): {
        "uz": ("Yangi topshiriq", "Yangi topshiriq qo'shildi."),
        "ru": ("Новое задание", "Добавлено новое задание."),
        "en": ("New assignment", "A new assignment was posted."),
    },
    ("assignments.due_soon", "in_app"): {
        "uz": ("Topshiriq muddati", "Topshiriq muddati yaqinlashmoqda: $due_at."),
        "ru": ("Срок задания", "Приближается срок задания: $due_at."),
        "en": ("Assignment due", "An assignment is due soon: $due_at."),
    },
    ("assignments.due_soon", "push"): {
        "uz": ("Topshiriq muddati", "Topshiriq muddati yaqinlashmoqda."),
        "ru": ("Срок задания", "Приближается срок задания."),
        "en": ("Assignment due", "An assignment is due soon."),
    },
    ("assignments.graded", "in_app"): {
        "uz": ("Topshiriq baholandi", "Topshiriqingiz baholandi: $score."),
        "ru": ("Задание оценено", "Ваше задание оценено: $score."),
        "en": ("Assignment graded", "Your submission was graded: $score."),
    },
    ("assignments.graded", "push"): {
        "uz": ("Topshiriq baholandi", "Topshiriqingiz baholandi: $score."),
        "ru": ("Задание оценено", "Ваше задание оценено: $score."),
        "en": ("Assignment graded", "Your submission was graded: $score."),
    },
    ("schedule.lesson_reminder", "in_app"): {
        "uz": ("Dars eslatmasi", "Dars haqida eslatma ($lesson_id)."),
        "ru": ("Напоминание об уроке", "Напоминание об уроке ($lesson_id)."),
        "en": ("Lesson reminder", "Lesson reminder ($lesson_id)."),
    },
    ("schedule.lesson_reminder", "push"): {
        "uz": ("Dars eslatmasi", "Tez orada darsingiz bor."),
        "ru": ("Напоминание об уроке", "Скоро у вас урок."),
        "en": ("Lesson reminder", "You have a lesson soon."),
    },
    ("auth.new_device_login", "in_app"): {
        "uz": ("Yangi qurilmadan kirish", "Hisobingizga yangi qurilmadan kirildi ($ip)."),
        "ru": ("Вход с нового устройства", "Вход в аккаунт с нового устройства ($ip)."),
        "en": ("New device login", "Your account was accessed from a new device ($ip)."),
    },
    ("auth.new_device_login", "push"): {
        "uz": ("Yangi qurilmadan kirish", "Hisobingizga yangi qurilmadan kirildi."),
        "ru": ("Вход с нового устройства", "Вход с нового устройства."),
        "en": ("New device login", "New device login on your account."),
    },
    ("students.enrollment_changed", "in_app"): {
        "uz": ("Ro'yxat o'zgardi", "O'quvchi guruhi o'zgardi ($to_cohort_id)."),
        "ru": ("Изменение зачисления", "Группа учащегося изменена ($to_cohort_id)."),
        "en": ("Enrollment changed", "Student group changed ($to_cohort_id)."),
    },
    ("students.enrollment_changed", "push"): {
        "uz": ("Ro'yxat o'zgardi", "O'quvchi guruhi o'zgardi."),
        "ru": ("Изменение зачисления", "Группа учащегося изменена."),
        "en": ("Enrollment changed", "Student group changed."),
    },
    ("finance.invoice_issued", "in_app"): {
        "uz": ("Yangi hisob-faktura", "Yangi hisob-faktura yaratildi ($invoice_id)."),
        "ru": ("Новый счёт", "Выставлен новый счёт ($invoice_id)."),
        "en": ("New invoice", "A new invoice was issued ($invoice_id)."),
    },
    ("finance.invoice_issued", "sms"): {
        "uz": ("", "Yangi hisob-faktura yaratildi."),
        "ru": ("", "Выставлен новый счёт."),
        "en": ("", "A new invoice was issued."),
    },
    ("finance.invoice_issued", "email"): {
        "uz": ("Yangi hisob-faktura", "Yangi hisob-faktura yaratildi ($invoice_id)."),
        "ru": ("Новый счёт", "Выставлен новый счёт ($invoice_id)."),
        "en": ("New invoice", "A new invoice was issued ($invoice_id)."),
    },
    ("finance.invoice_issued", "push"): {
        "uz": ("Yangi hisob-faktura", "Yangi hisob-faktura yaratildi."),
        "ru": ("Новый счёт", "Выставлен новый счёт."),
        "en": ("New invoice", "A new invoice was issued."),
    },
    ("finance.payment_reminder", "in_app"): {
        "uz": ("To'lov eslatmasi", "To'lov muddati o'tdi ($invoice_id)."),
        "ru": ("Напоминание об оплате", "Срок оплаты истёк ($invoice_id)."),
        "en": ("Payment reminder", "A payment is overdue ($invoice_id)."),
    },
    ("finance.payment_reminder", "sms"): {
        "uz": ("", "To'lov muddati o'tdi. Iltimos, to'lovni amalga oshiring."),
        "ru": ("", "Срок оплаты истёк. Пожалуйста, оплатите."),
        "en": ("", "A payment is overdue. Please pay."),
    },
    ("finance.payment_reminder", "email"): {
        "uz": ("To'lov eslatmasi", "To'lov muddati o'tdi ($invoice_id)."),
        "ru": ("Напоминание об оплате", "Срок оплаты истёк ($invoice_id)."),
        "en": ("Payment reminder", "A payment is overdue ($invoice_id)."),
    },
    ("finance.payment_reminder", "push"): {
        "uz": ("To'lov eslatmasi", "To'lov muddati o'tdi."),
        "ru": ("Напоминание об оплате", "Срок оплаты истёк."),
        "en": ("Payment reminder", "A payment is overdue."),
    },
    ("payments.payment_completed", "in_app"): {
        "uz": ("To'lov qabul qilindi", "To'lovingiz qabul qilindi: $amount_uzs UZS."),
        "ru": ("Платёж получен", "Ваш платёж получен: $amount_uzs UZS."),
        "en": ("Payment received", "Your payment was received: $amount_uzs UZS."),
    },
    ("payments.payment_completed", "sms"): {
        "uz": ("", "To'lovingiz qabul qilindi: $amount_uzs UZS."),
        "ru": ("", "Ваш платёж получен: $amount_uzs UZS."),
        "en": ("", "Your payment was received: $amount_uzs UZS."),
    },
    ("payments.payment_completed", "push"): {
        "uz": ("To'lov qabul qilindi", "To'lovingiz qabul qilindi."),
        "ru": ("Платёж получен", "Ваш платёж получен."),
        "en": ("Payment received", "Your payment was received."),
    },
    ("payments.payment_failed", "in_app"): {
        "uz": ("To'lov amalga oshmadi", "To'lovingiz amalga oshmadi."),
        "ru": ("Платёж не прошёл", "Ваш платёж не прошёл."),
        "en": ("Payment failed", "Your payment failed."),
    },
    ("payments.payment_failed", "sms"): {
        "uz": ("", "To'lovingiz amalga oshmadi."),
        "ru": ("", "Ваш платёж не прошёл."),
        "en": ("", "Your payment failed."),
    },
    ("payments.payment_failed", "push"): {
        "uz": ("To'lov amalga oshmadi", "To'lovingiz amalga oshmadi."),
        "ru": ("Платёж не прошёл", "Ваш платёж не прошёл."),
        "en": ("Payment failed", "Your payment failed."),
    },
    ("cohorts.announcement", "in_app"): {
        "uz": ("$title", "$body"),
        "ru": ("$title", "$body"),
        "en": ("$title", "$body"),
    },
    ("cohorts.announcement", "push"): {
        "uz": ("$title", "$body"),
        "ru": ("$title", "$body"),
        "en": ("$title", "$body"),
    },
    ("billing.subscription_past_due", "in_app"): {
        "uz": ("Obuna muddati o'tgan", "Markaz obunasi to'lanmagan."),
        "ru": ("Подписка просрочена", "Подписка центра просрочена."),
        "en": ("Subscription past due", "The center's subscription is past due."),
    },
    ("billing.subscription_past_due", "email"): {
        "uz": ("Obuna muddati o'tgan", "Markaz obunasi to'lanmagan."),
        "ru": ("Подписка просрочена", "Подписка центра просрочена."),
        "en": ("Subscription past due", "The center's subscription is past due."),
    },
    ("billing.subscription_suspended", "in_app"): {
        "uz": ("Obuna to'xtatildi", "Markaz obunasi to'xtatildi."),
        "ru": ("Подписка приостановлена", "Подписка центра приостановлена."),
        "en": ("Subscription suspended", "The center's subscription is suspended."),
    },
    ("billing.subscription_suspended", "email"): {
        "uz": ("Obuna to'xtatildi", "Markaz obunasi to'xtatildi."),
        "ru": ("Подписка приостановлена", "Подписка центра приостановлена."),
        "en": ("Subscription suspended", "The center's subscription is suspended."),
    },
}


def seed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for (event_type, channel), by_locale in _TEMPLATES.items():
        for locale, (subject, body) in by_locale.items():
            NotificationTemplate.objects.update_or_create(
                event_type=event_type,
                channel=channel,
                locale=locale,
                defaults={"subject": subject, "body": body, "is_active": True},
            )


def unseed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_notification_models"),
    ]

    operations = [
        migrations.RunPython(seed_templates, unseed_templates),
    ]
