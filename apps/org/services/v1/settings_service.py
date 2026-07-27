"""CenterSettingsService — read (cached) + partial update of the TD-13 singleton.

The old CenterSettingsSerializer validated ~30 mixed-type knobs. Here each provided
writable field is validated through its own model field's ``.clean()`` (type coercion
+ choices + range validators), and the three JSON knobs keep their explicit shape
guards; the (pattern, center_code) cross-field rule runs last.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_slug
from django.utils.translation import gettext_lazy as _

from apps.org.interfaces.services import ICenterSettingsService
from apps.org.models import CenterSettings
from core.exceptions import ValidationException

# Writable knobs (mirrors CenterSettingsSerializer.Meta.fields minus read-only
# updated_at). Anything else in the body is ignored, as DRF ignored unknown fields.
_WRITABLE = frozenset(
    {
        "open_registration",
        "require_group_acceptance",
        "grading_scheme",
        "honor_roll_min",
        "academic_warning_max",
        "late_threshold_minutes",
        "attendance_correction_window_hours",
        "auto_absent_after_minutes",
        "assignment_grace_minutes",
        "assignment_max_resubmits",
        "max_upload_mb",
        "storage_quota_gb",
        "allowed_file_types",
        "currency_primary",
        "currency_secondary",
        "fx_source",
        "fx_rate_usd_manual",
        "sibling_discount_percent",
        "payment_reminder_interval_days",
        "quiet_hours_start",
        "quiet_hours_end",
        "otp_channel_prefs",
        "otp_cooldown_seconds",
        "student_id_pattern",
        "center_code",
        "ai_exam_generation_enabled",
        "placement_allowed_question_types",
        "penalty_escalation_threshold",
        "show_classroom_rank",
        "placement_test_creation_mobile_only",
    }
)


def _verr(field: str, msg: str) -> ValidationException:
    return ValidationException(msg, code="validation_error", fields={field: [msg]})


class CenterSettingsService(ICenterSettingsService):
    def read(self) -> CenterSettings:
        from apps.org.selectors import get_center_settings

        return get_center_settings()

    def update(self, changes: dict[str, Any]) -> CenterSettings:
        instance = CenterSettings.load()
        for key, raw in changes.items():
            if key not in _WRITABLE:
                continue
            if key == "allowed_file_types":
                instance.allowed_file_types = self._clean_allowed_file_types(raw)
            elif key == "otp_channel_prefs":
                instance.otp_channel_prefs = self._clean_otp_prefs(raw)
            elif key == "placement_allowed_question_types":
                instance.placement_allowed_question_types = self._clean_placement_types(raw)
            else:
                setattr(instance, key, self._clean_model_field(instance, key, raw))
        if "student_id_pattern" in changes or "center_code" in changes:
            from apps.org.services import validate_student_id_pattern

            validate_student_id_pattern(
                instance.student_id_pattern, center_code=instance.center_code or ""
            )
        instance.save()
        # Reload so decimals come back quantized to their column scale (numeric(5,2)
        # → "90.00", not the unquantized "90" a fresh Decimal renders) — keeps the
        # PATCH echo byte-identical to a subsequent GET (DRF-parity).
        instance.refresh_from_db()
        return instance

    # --- field cleaners ----------------------------------------------------
    @staticmethod
    def _clean_model_field(instance: CenterSettings, key: str, raw: Any) -> Any:
        field = CenterSettings._meta.get_field(key)  # a concrete Field (never a relation here)
        try:
            return field.clean(raw, instance)  # type: ignore[union-attr]  # to_python + choices + validators
        except DjangoValidationError as exc:
            raise ValidationException(
                _("Invalid value."), code="validation_error", fields={key: list(exc.messages)}
            ) from exc
        except (TypeError, ValueError) as exc:
            # e.g. TimeField.to_python(123) raises a bare TypeError (fromisoformat wants
            # a str), which is NOT a DjangoValidationError — surface it as a clean 400,
            # never a 500.
            raise ValidationException(
                _("Invalid value."),
                code="validation_error",
                fields={key: ["Invalid value for this field."]},
            ) from exc

    @staticmethod
    def _clean_allowed_file_types(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            raise _verr("allowed_file_types", "Must be a list of file-type slugs.")
        for item in raw:
            if not isinstance(item, str):
                raise _verr("allowed_file_types", "Each item must be a slug string.")
            try:
                validate_slug(item)
            except DjangoValidationError as exc:
                raise _verr("allowed_file_types", f"'{item}' is not a valid slug.") from exc
        return raw

    @staticmethod
    def _clean_otp_prefs(raw: Any) -> dict[str, bool]:
        if not isinstance(raw, dict):
            raise _verr("otp_channel_prefs", "Must be an object of channel -> boolean.")
        unknown = set(raw) - {"sms", "email"}
        if unknown:
            raise _verr("otp_channel_prefs", f"Unknown OTP channels: {sorted(unknown)}.")
        for value in raw.values():
            if not isinstance(value, bool):
                raise _verr("otp_channel_prefs", "Channel values must be booleans.")
        return raw

    @staticmethod
    def _clean_placement_types(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            raise _verr("placement_allowed_question_types", "Must be a list.")
        from apps.placement.models import PlacementQuestion

        valid = set(PlacementQuestion.QuestionType.values)
        unknown = [t for t in raw if t not in valid]
        if unknown:
            raise _verr(
                "placement_allowed_question_types",
                "Unknown question type(s): {}.".format(", ".join(map(str, unknown))),
            )
        deduped: list[str] = []
        for t in raw:  # preserve order, drop duplicates
            if t not in deduped:
                deduped.append(t)
        return deduped
