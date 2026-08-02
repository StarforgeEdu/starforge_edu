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
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.org.interfaces.services import ICenterSettingsService
from apps.org.models import CenterSettings
from core.exceptions import ServiceUnavailableException, ValidationException

# Writable knobs (mirrors the explicit settings contract minus server-managed
# ``updated_at`` and ``disabled_apps``). Unknown fields are rejected: silently
# accepting a misspelled policy or security knob creates a false-success hazard.
_WRITABLE = frozenset(
    {
        "open_registration",
        "require_group_acceptance",
        "default_language",
        "organization_timezone",
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
        "absence_deduction_enabled",
        "absence_deduction_excused_only",
        "placement_test_creation_mobile_only",
    }
)


def _verr(field: str, msg: str) -> ValidationException:
    return ValidationException(msg, code="validation_error", fields={field: [msg]})


class CenterSettingsService(ICenterSettingsService):
    def read(self) -> CenterSettings:
        from apps.org.selectors import get_center_settings

        return get_center_settings()

    @transaction.atomic
    def update(self, changes: dict[str, Any]) -> CenterSettings:
        unknown = sorted(set(changes) - _WRITABLE)
        if unknown:
            raise ValidationException(
                _("Request contains unsupported settings."),
                code="validation_error",
                fields={field: [_("This field is not supported.")] for field in unknown},
            )
        # Tenant provisioning owns singleton creation. Reconstructing every
        # policy/security default as a side effect of a partial PATCH would
        # silently replace lost organization configuration with guesses.
        instance = CenterSettings.objects.select_for_update().filter(pk=1).first()
        if instance is None:
            raise ServiceUnavailableException(
                _("Organization settings are not ready."),
                code="configuration_unavailable",
            )
        if not changes:
            return instance
        for key, raw in changes.items():
            if key == "allowed_file_types":
                instance.allowed_file_types = self._clean_allowed_file_types(raw)
            elif key == "otp_channel_prefs":
                instance.otp_channel_prefs = self._clean_otp_prefs(raw)
            elif key == "placement_allowed_question_types":
                instance.placement_allowed_question_types = self._clean_placement_types(raw)
            elif key in {"currency_primary", "currency_secondary"}:
                instance_value = self._clean_model_field(instance, key, raw)
                instance_value = self._clean_currency(key, instance_value)
                setattr(instance, key, instance_value)
            else:
                setattr(instance, key, self._clean_model_field(instance, key, raw))
        if "student_id_pattern" in changes or "center_code" in changes:
            from apps.org.services import validate_student_id_pattern

            validate_student_id_pattern(instance.student_id_pattern, center_code=instance.center_code or "")
        self._validate_cross_fields(instance)
        try:
            instance.full_clean(validate_unique=False, validate_constraints=False)
        except DjangoValidationError as exc:
            fields = {
                field: [str(message) for message in messages]
                for field, messages in getattr(exc, "message_dict", {"field": exc.messages}).items()
            }
            raise ValidationException(
                _("Please review the organization settings."),
                code="validation_error",
                fields=fields,
            ) from exc
        instance.save(update_fields=[*sorted(changes), "updated_at"])
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
        if len(raw) > 64:
            raise _verr("allowed_file_types", "At most 64 file types are allowed.")
        normalized: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise _verr("allowed_file_types", "Each item must be a slug string.")
            item = item.strip().lower().lstrip(".")
            if not item or len(item) > 32:
                raise _verr("allowed_file_types", "Each file type must contain 1-32 characters.")
            try:
                validate_slug(item)
            except DjangoValidationError as exc:
                raise _verr("allowed_file_types", f"'{item}' is not a valid slug.") from exc
            if item in normalized:
                raise _verr("allowed_file_types", "Duplicate file types are not allowed.")
            normalized.append(item)
        return normalized

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
        if set(raw) != {"sms", "email"}:
            raise _verr("otp_channel_prefs", "Both sms and email preferences are required.")
        if not any(raw.values()):
            raise _verr("otp_channel_prefs", "At least one OTP channel must remain enabled.")
        return dict(raw)

    @staticmethod
    def _clean_placement_types(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            raise _verr("placement_allowed_question_types", "Must be a list.")
        from apps.placement.models import PlacementQuestion

        valid = set(PlacementQuestion.QuestionType.values)
        unknown = [t for t in raw if not isinstance(t, str) or t not in valid]
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

    @staticmethod
    def _clean_currency(field: str, value: Any) -> str:
        if not isinstance(value, str):
            raise _verr(field, "Enter a three-letter ISO 4217 currency code.")
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise _verr(field, "Enter a three-letter ISO 4217 currency code.")
        return value

    @staticmethod
    def _validate_cross_fields(instance: CenterSettings) -> None:
        if not 0 <= instance.academic_warning_max <= instance.honor_roll_min <= 100:
            raise ValidationException(
                _("Grade thresholds are inconsistent."),
                code="validation_error",
                fields={
                    "academic_warning_max": [
                        _("Use a value from 0 to 100 that is not above honor_roll_min.")
                    ],
                    "honor_roll_min": [_("Use a value from 0 to 100.")],
                },
            )
        if not 0 <= instance.sibling_discount_percent <= 100:
            raise _verr("sibling_discount_percent", "Use a percentage from 0 to 100.")
        if instance.fx_source not in {"cbu", "manual"}:
            raise _verr("fx_source", "Choose cbu or manual.")
        if instance.fx_rate_usd_manual is not None and instance.fx_rate_usd_manual <= 0:
            raise _verr("fx_rate_usd_manual", "Enter a positive exchange rate.")
        if instance.fx_source == "manual" and instance.fx_rate_usd_manual is None:
            raise _verr("fx_rate_usd_manual", "A manual exchange rate is required.")
        if instance.currency_primary == instance.currency_secondary:
            raise ValidationException(
                _("Primary and secondary currencies must differ."),
                code="validation_error",
                fields={"currency_secondary": [_("Choose a different currency.")]},
            )
