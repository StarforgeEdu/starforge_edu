from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin

from .models import Message, MessageAttachmentUploadGrant, ThreadRealtimeEvent


@admin.register(Message)
class MessageAdmin(ReadOnlyAdmin):
    """Metadata-only review; private content remains participant-scoped."""

    list_display = ("id", "thread_identifier", "sender_identifier", "created_at")
    date_hierarchy = "created_at"
    exclude = ("body", "attachments")

    def get_queryset(self, request):
        return super().get_queryset(request).defer("body", "attachments")

    @admin.display(description="Thread", ordering="thread_id")
    def thread_identifier(self, obj: Message) -> int:
        return obj.thread_id

    @admin.display(description="Sender", ordering="sender_id")
    def sender_identifier(self, obj: Message) -> int | None:
        return obj.sender_id


@admin.register(MessageAttachmentUploadGrant)
class MessageAttachmentUploadGrantAdmin(ReadOnlyAdmin):
    """Upload policies are immutable capabilities; do not expose their object key."""

    list_display = (
        "id",
        "requested_by",
        "content_type",
        "expected_size_bytes",
        "consumed_at",
        "source_deleted_at",
        "expires_at",
    )
    list_filter = ("content_type", "consumed_at", "source_deleted_at")
    search_fields = ("requested_by__username",)
    list_select_related = ("requested_by",)
    exclude = ("key", "durable_key")


@admin.register(ThreadRealtimeEvent)
class ThreadRealtimeEventAdmin(ReadOnlyAdmin):
    """Durable pointer stream; content remains on the scoped Message resource."""

    list_display = (
        "thread_id",
        "sequence",
        "kind",
        "message_id",
        "actor_principal_kind",
        "actor_principal_id",
        "created_at",
    )
    list_filter = ("kind", "actor_principal_kind")
    date_hierarchy = "created_at"
