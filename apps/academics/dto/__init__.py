"""Validated input contracts for the academics domain."""

from apps.academics.dto.results import (
    ResultFieldError,
    ResultValues,
    validate_result_values,
)

__all__ = ("ResultFieldError", "ResultValues", "validate_result_values")
