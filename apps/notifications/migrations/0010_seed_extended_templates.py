from django.db import migrations


# Every event added after the original Day-3 seed defaults to in-app and push,
# so both channels need a complete uz/ru/en template set.
_COPY = {
    "attendance.late": {
        "uz": ("Kechikish", "Farzandingiz darsga kechikdi."),
        "ru": ("Опоздание", "Ваш ребёнок опоздал на урок."),
        "en": ("Late attendance", "Your child arrived late to a lesson."),
    },
    "print.failed": {
        "uz": ("Chop etish xatosi", "Chop etish vazifasi bajarilmadi."),
        "ru": ("Ошибка печати", "Задание печати не выполнено."),
        "en": ("Print failed", "A print job could not be completed."),
    },
    "approval.approved": {
        "uz": ("So'rov tasdiqlandi", "So'rovingiz tasdiqlandi."),
        "ru": ("Запрос одобрен", "Ваш запрос одобрен."),
        "en": ("Request approved", "Your request was approved."),
    },
    "approval.rejected": {
        "uz": ("So'rov rad etildi", "So'rovingiz rad etildi."),
        "ru": ("Запрос отклонён", "Ваш запрос отклонён."),
        "en": ("Request rejected", "Your request was rejected."),
    },
    "approval.awaiting_disbursement": {
        "uz": ("To'lov kutilmoqda", "Tasdiqlangan so'rov to'lovni kutmoqda."),
        "ru": ("Ожидает выплаты", "Одобренный запрос ожидает выплаты."),
        "en": ("Awaiting disbursement", "An approved request is awaiting disbursement."),
    },
    "approval.disbursed": {
        "uz": ("To'lov berildi", "So'rov bo'yicha to'lov berildi."),
        "ru": ("Выплата произведена", "Выплата по запросу произведена."),
        "en": ("Request disbursed", "Your approved request was disbursed."),
    },
    "penalty.escalated": {
        "uz": ("Jarima ballari", "O'quvchi jarima ballari chegarasiga yetdi."),
        "ru": ("Штрафные баллы", "Ученик достиг порога штрафных баллов."),
        "en": ("Penalty escalation", "A student reached the penalty-point threshold."),
    },
    "message.received": {
        "uz": ("Yangi xabar", "Sizga yangi xabar keldi."),
        "ru": ("Новое сообщение", "Вы получили новое сообщение."),
        "en": ("New message", "You received a new message."),
    },
    "report.ready": {
        "uz": ("Hisobot tayyor", "So'ralgan hisobot tayyor."),
        "ru": ("Отчёт готов", "Запрошенный отчёт готов."),
        "en": ("Report ready", "Your requested report is ready."),
    },
    "cover.requested": {
        "uz": ("O'rinbosar kerak", "Yangi o'rinbosar so'rovi ko'rib chiqishni kutmoqda."),
        "ru": ("Нужна замена", "Новый запрос на замену ожидает рассмотрения."),
        "en": ("Cover requested", "A new cover request needs review."),
    },
    "cover.approved": {
        "uz": ("O'rinbosar tasdiqlandi", "O'rinbosar so'rovingiz tasdiqlandi."),
        "ru": ("Замена одобрена", "Ваш запрос на замену одобрен."),
        "en": ("Cover approved", "Your cover request was approved."),
    },
    "cover.pool_opened": {
        "uz": ("Ochiq dars", "O'rinbosarlik uchun yangi dars mavjud."),
        "ru": ("Открытая замена", "Доступен новый урок для замены."),
        "en": ("Cover available", "A lesson is available to claim for cover."),
    },
    "cover.rejected": {
        "uz": ("O'rinbosar rad etildi", "O'rinbosar so'rovingiz rad etildi."),
        "ru": ("Замена отклонена", "Ваш запрос на замену отклонён."),
        "en": ("Cover rejected", "Your cover request was rejected."),
    },
}


def seed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for event_type, by_locale in _COPY.items():
        for channel in ("in_app", "push"):
            for locale, (subject, body) in by_locale.items():
                NotificationTemplate.objects.get_or_create(
                    event_type=event_type,
                    channel=channel,
                    locale=locale,
                    defaults={"subject": subject, "body": body, "is_active": True},
                )


def unseed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(
        event_type__in=tuple(_COPY), channel__in=("in_app", "push")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0009_alter_notification_event_type_and_more")]

    operations = [migrations.RunPython(seed_templates, unseed_templates)]
