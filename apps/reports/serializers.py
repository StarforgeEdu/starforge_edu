"""Reports serializers — read/write split, explicit fields (no __all__)."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from rest_framework import serializers

from apps.reports.models import Report, ReportFormat, ReportKey, ReportRun, ReportSchedule


class UtcDateTimeField(serializers.DateTimeField):
    """Render timestamps in UTC with a ``+00:00`` offset, matching every layered
    app's presenter (which emits ``value.isoformat()`` on a UTC-aware datetime).

    DRF's default DateTimeField localizes aware values to ``settings.TIME_ZONE``
    (Asia/Tashkent) and swaps a trailing ``+00:00`` to ``Z`` — so without this,
    reports would emit ``...+05:00`` while the rest of the API emits ``...+00:00``
    for the same conceptual field. Forcing UTC + a plain ``isoformat()`` keeps the
    whole API's timestamps byte-identical.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("default_timezone", UTC)
        super().__init__(**kwargs)

    def to_representation(self, value: Any) -> Any:
        if not value:
            return None
        return self.enforce_timezone(value).isoformat()


class ReportSerializer(serializers.ModelSerializer):
    """Library entry (read-only surface)."""

    class Meta:
        model = Report
        fields = ("id", "key", "title", "description", "allowed_roles", "default_format")
        read_only_fields = fields


class ReportRunReadSerializer(serializers.ModelSerializer):
    report_key = serializers.CharField(source="report.key", read_only=True)
    download_url = serializers.SerializerMethodField()
    created_at = UtcDateTimeField(read_only=True)
    started_at = UtcDateTimeField(read_only=True)
    finished_at = UtcDateTimeField(read_only=True)

    class Meta:
        model = ReportRun
        fields = (
            "id",
            "report",
            "report_key",
            "format",
            "status",
            "params",
            "file_bytes",
            "error",
            "download_url",
            "created_at",
            "started_at",
            "finished_at",
        )
        read_only_fields = fields

    def get_download_url(self, obj: ReportRun) -> str | None:
        # A fresh presign, only when the run is done.
        from apps.reports.services import presign_run

        if obj.status != ReportRun.Status.DONE:
            return None
        return presign_run(obj)


class ReportRunCreateSerializer(serializers.Serializer):
    report_key = serializers.ChoiceField(choices=ReportKey.choices)
    format = serializers.ChoiceField(choices=ReportFormat.choices, required=False)
    params = serializers.DictField(required=False, default=dict)


class ReportScheduleReadSerializer(serializers.ModelSerializer):
    report_key = serializers.CharField(source="report.key", read_only=True)
    last_run_at = UtcDateTimeField(read_only=True)
    created_at = UtcDateTimeField(read_only=True)
    updated_at = UtcDateTimeField(read_only=True)

    class Meta:
        model = ReportSchedule
        fields = (
            "id",
            "report",
            "report_key",
            "cadence",
            "weekday",
            "day_of_month",
            "hour",
            "format",
            "params",
            "recipient_ids",
            "is_active",
            "last_run_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "report", "report_key", "last_run_at", "created_at", "updated_at")


class ReportScheduleWriteSerializer(serializers.Serializer):
    report_key = serializers.ChoiceField(choices=ReportKey.choices)
    cadence = serializers.ChoiceField(choices=ReportSchedule.Cadence.choices)
    weekday = serializers.IntegerField(min_value=0, max_value=6, required=False, allow_null=True)
    day_of_month = serializers.IntegerField(min_value=1, max_value=31, required=False, allow_null=True)
    hour = serializers.IntegerField(min_value=0, max_value=23, required=False, default=7)
    format = serializers.ChoiceField(choices=ReportFormat.choices, required=False, default=ReportFormat.PDF)
    params = serializers.DictField(required=False, default=dict)
    recipient_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs: dict) -> dict:
        cadence = attrs["cadence"]
        if cadence == ReportSchedule.Cadence.WEEKLY and attrs.get("weekday") is None:
            raise serializers.ValidationError({"weekday": "Required for a weekly cadence."})
        if cadence == ReportSchedule.Cadence.MONTHLY and attrs.get("day_of_month") is None:
            raise serializers.ValidationError({"day_of_month": "Required for a monthly cadence."})
        return attrs
