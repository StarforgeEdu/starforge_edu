"""Executable OpenAPI contracts for the forms and survey workflow.

The forms API mixes management-only lifecycle operations with responder-facing
reads and submissions.  Keeping the contracts beside the domain makes the
permission split, bounded DTOs, and state-dependent errors reviewable without
falling back to the legacy source-code inference in :mod:`core.openapi`.
"""

from __future__ import annotations

from typing import Any

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

_ROLE_VALUES = [
    "director",
    "head_of_dept",
    "teacher",
    "student",
    "parent",
    "accountant",
    "cashier",
    "librarian",
    "security",
    "it",
    "registrar",
    "support",
]
_FIELD_TYPES = [
    "text",
    "textarea",
    "number",
    "boolean",
    "single_choice",
    "multi_choice",
    "rating",
    "date",
]


def _page_parameters(*extra: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return (
        *extra,
        {
            "name": "page",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1},
        },
        {
            "name": "page_size",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
    )


_FORM_FILTERS = _page_parameters(
    {
        "name": "status",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["draft", "published", "closed"]},
    },
    {
        "name": "branch",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
    },
    {
        "name": "is_anonymous",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    },
    {
        "name": "search",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "maxLength": 200},
    },
    {
        "name": "ordering",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": ["created_at", "-created_at", "title", "-title"],
        },
    },
)


def _read_form(method: str, *, collection: bool = False) -> OperationContract:
    responses = {
        "200": (
            json_response("Visible form page.", "FormPageResponse")
            if method == "GET" and collection
            else json_response("Visible form.", "FormResponse")
            if method == "GET"
            else json_response("Form visibility confirmed.")
        ),
        "400": error_response("A declared query parameter is malformed or unsupported."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks forms read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not collection:
        responses["404"] = error_response("The form is not visible to the current principal.")
    return OperationContract(
        method=method,
        summary="List visible forms" if collection else "Read a visible form",
        description=(
            "Returns only forms visible to the current role-native principal and exact "
            "permission scope. Management-only audience and creator fields are omitted "
            "when the caller may respond but may not manage the form. Out-of-scope IDs "
            "are indistinguishable from missing records."
        ),
        permission="forms:read",
        security=SESSION_SECURITY,
        parameters=_FORM_FILTERS if collection else (),
        responses=responses,
        operation_id=f"{method.lower()}_forms_{'collection' if collection else 'detail'}",
    )


def _form_update(method: str) -> OperationContract:
    schema = "FormReplaceRequest" if method == "PUT" else "FormPatchRequest"
    return OperationContract(
        method=method,
        summary="Replace draft form metadata" if method == "PUT" else "Patch draft form metadata",
        description=(
            "Changes only a manageable draft form. Branch ownership cannot be moved through "
            "this operation; exact audience principals are resolved server-side from the "
            "bounded public user selectors."
        ),
        permission="forms:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request(schema, required=method == "PUT"),
        responses={
            "200": json_response("Draft form updated.", "FormResponse"),
            "400": error_response("The closed request DTO, time window, or audience is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks scoped forms write authority."),
            "404": error_response("The form is outside the caller's management scope."),
            "422": error_response("The form is no longer a draft."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_forms_detail",
    )


FORMS_COLLECTION_CONTRACTS = (
    _read_form("GET", collection=True),
    _read_form("HEAD", collection=True),
    OperationContract(
        method="POST",
        summary="Create a draft form",
        description=(
            "Creates one branch-scoped or organization-wide draft. A scoped caller cannot "
            "probe another branch through the branch selector, and every explicit audience "
            "user must resolve to one active role-native principal in the form scope."
        ),
        permission="forms:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("FormCreateRequest"),
        responses={
            "201": json_response("Draft form created.", "FormResponse"),
            "400": error_response("The closed request DTO, time window, or audience is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The branch is outside the caller's forms write scope."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="post_forms_collection",
    ),
)

FORM_DETAIL_CONTRACTS = (
    _read_form("GET"),
    _read_form("HEAD"),
    _form_update("PUT"),
    _form_update("PATCH"),
    OperationContract(
        method="DELETE",
        summary="Delete a draft form",
        description="Deletes only a manageable draft; published response history cannot be erased here.",
        permission="forms:write",
        security=UNSAFE_SESSION_SECURITY,
        responses={
            "204": json_response("Draft form deleted."),
            "400": error_response("The request contains unsupported query parameters."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks scoped forms write authority."),
            "404": error_response("The form is outside the caller's management scope."),
            "422": error_response("The form is no longer a draft."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="delete_forms_detail",
    ),
)

FORM_ADD_FIELD_CONTRACT = OperationContract(
    method="POST",
    summary="Append one field to a draft form",
    description=(
        "Adds at most the hundredth field. Choice options and all field text are bounded; "
        "only choice fields may carry unique, non-empty options."
    ),
    permission="forms:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("FormFieldCreateRequest"),
    responses={
        "201": json_response("Field appended.", "FormFieldResponse"),
        "400": error_response("The closed field DTO or its options are invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks scoped forms write authority."),
        "404": error_response("The form is outside the caller's management scope."),
        "422": error_response("The form is no longer a draft."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_forms_add_field",
)


def _empty_lifecycle_contract(*, action: str, response_schema: str = "FormResponse") -> OperationContract:
    verbs = {
        "publish": ("publishes", "published"),
        "close": ("closes", "closed"),
    }
    present, past = verbs[action]
    return OperationContract(
        method="POST",
        summary=f"{action.capitalize()} a form",
        description=(
            f"Idempotently {present} the manageable form when it is already in that state. "
            "The optional JSON body must be an empty object."
        ),
        permission="forms:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("EmptyWorkflowRequest", required=False),
        responses={
            "200": json_response(f"Form {past}.", response_schema),
            "400": error_response("The optional body is malformed or contains fields."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks scoped forms write authority."),
            "404": error_response("The form is outside the caller's management scope."),
            "422": error_response("The requested lifecycle transition is not allowed."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"post_forms_{action}",
    )


FORM_PUBLISH_CONTRACT = _empty_lifecycle_contract(action="publish")
FORM_CLOSE_CONTRACT = _empty_lifecycle_contract(action="close")

FORM_SUBMIT_CONTRACT = OperationContract(
    method="POST",
    summary="Submit one bounded form response",
    description=(
        "Submits at most one answer per field. Field values are validated against the "
        "published form definition. Anonymous forms return only a receipt; identified "
        "single-response forms deduplicate by the exact role-native principal."
    ),
    permission="forms:read",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("FormSubmitRequest"),
    responses={
        "201": json_response("Response accepted.", "FormSubmissionReceiptResponse"),
        "400": error_response("The answer list, field IDs, or typed values are invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks forms read authority."),
        "404": error_response("The form is not visible to the current principal."),
        "409": error_response("The exact principal has already answered this single-response form."),
        "422": error_response("The form is not currently open for responses."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_forms_submit",
)


def _managed_read(method: str, *, kind: str) -> OperationContract:
    schemas = {
        "responses": "FormResponsePageResponse",
        "summary": "FormSummaryResponse",
    }
    parameters = _page_parameters() if kind == "responses" else ()
    return OperationContract(
        method=method,
        summary=f"Read managed form {kind}",
        description=(
            "Available only at the exact forms:write scope that manages the form. "
            "Ambiguous legacy bridge identities remain hidden rather than attributed "
            "to the wrong role-native account."
        ),
        permission="forms:write",
        security=SESSION_SECURITY,
        parameters=parameters,
        responses={
            "200": (
                json_response(f"Managed form {kind}.", schemas[kind])
                if method == "GET"
                else json_response(f"Managed form {kind} is visible.")
            ),
            "400": error_response("Pagination input is malformed or outside the supported bound."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks scoped forms write authority."),
            "404": error_response("The form is outside the caller's management scope."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_forms_{kind}",
    )


FORM_RESPONSES_CONTRACTS = (
    _managed_read("GET", kind="responses"),
    _managed_read("HEAD", kind="responses"),
)
FORM_SUMMARY_CONTRACTS = (
    _managed_read("GET", kind="summary"),
    _managed_read("HEAD", kind="summary"),
)

FORM_ANALYZE_CONTRACT = OperationContract(
    method="POST",
    summary="Queue a bounded AI analysis of form responses",
    description=(
        "Reserves the configured AI budget and queues analysis after commit. The optional "
        "body must be empty. Poll the returned AI request through the AI request endpoint."
    ),
    permission="forms:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("EmptyWorkflowRequest", required=False),
    responses={
        "202": json_response("Analysis accepted for asynchronous processing.", "FormAnalysisResponse"),
        "400": error_response("The optional body is malformed or contains fields."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks scoped forms write authority or AI budget authority."),
        "404": error_response("The form is outside the caller's management scope."),
        "409": error_response("An equivalent analysis request already exists."),
        "422": error_response("The form has no responses to analyze."),
        "429": error_response("Authenticated request or AI budget rate limit exceeded."),
        "503": error_response("AI analysis is temporarily unavailable."),
    },
    operation_id="post_forms_analyze",
)


def _success_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": data_schema,
        },
        "required": ["success", "data"],
    }


def _page_schema(item_name: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {
                "type": "array",
                "items": {"$ref": f"#/components/schemas/{item_name}"},
            },
            "pagination": {"$ref": "#/components/schemas/Pagination"},
        },
        "required": ["success", "data", "pagination"],
    }


_FORM_MUTABLE_PROPERTIES: dict[str, Any] = {
    "title": {"type": "string", "minLength": 1, "maxLength": 200},
    "description": {"type": "string", "maxLength": 20_000},
    "is_anonymous": {"type": "boolean", "default": False},
    "allow_multiple": {"type": "boolean", "default": False},
    "opens_at": {"type": "string", "format": "date-time", "nullable": True},
    "closes_at": {"type": "string", "format": "date-time", "nullable": True},
    "audience_roles": {
        "type": "array",
        "maxItems": len(_ROLE_VALUES),
        "description": "Duplicate role names are accepted and normalized to first occurrence.",
        "items": {"type": "string", "enum": _ROLE_VALUES},
    },
    "audience_user_ids": {
        "type": "array",
        "maxItems": 500,
        "description": "Duplicate public user selectors are accepted and normalized to first occurrence.",
        "items": {"type": "integer", "format": "int64", "minimum": 1},
    },
}


OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "EmptyWorkflowRequest": {
        "type": "object",
        "additionalProperties": False,
        "maxProperties": 0,
    },
    "FormCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **_FORM_MUTABLE_PROPERTIES,
            "branch": {"type": "integer", "format": "int64", "minimum": 1, "nullable": True},
        },
        "required": ["title"],
    },
    "FormReplaceRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(_FORM_MUTABLE_PROPERTIES),
        "required": ["title"],
    },
    "FormPatchRequest": {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "properties": dict(_FORM_MUTABLE_PROPERTIES),
    },
    "FormFieldCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "minLength": 1, "maxLength": 255},
            "field_type": {"type": "string", "enum": _FIELD_TYPES},
            "required": {"type": "boolean", "default": False},
            "order": {"type": "integer", "minimum": 0, "nullable": True},
            "options": {
                "type": "array",
                "maxItems": 100,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "help_text": {"type": "string", "maxLength": 255},
        },
        "required": ["label", "field_type"],
    },
    "FormAnswerValue": {
        "oneOf": [
            {"type": "string", "maxLength": 20_000},
            {"type": "number", "minimum": -1_000_000_000_000, "maximum": 1_000_000_000_000},
            {"type": "boolean"},
            {
                "type": "array",
                "maxItems": 100,
                "uniqueItems": True,
                "items": {"type": "string", "maxLength": 200},
            },
        ]
    },
    "FormSubmitRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answers": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "field": {"type": "integer", "format": "int64", "minimum": 1},
                        "value": {"$ref": "#/components/schemas/FormAnswerValue"},
                    },
                    "required": ["field"],
                },
            }
        },
        "required": ["answers"],
    },
    "FormPrincipal": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["student", "teacher", "parent", "staff"]},
            "id": {"type": "integer", "format": "int64", "minimum": 1},
        },
        "required": ["kind", "id"],
    },
    "FormAudiencePrincipal": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["student", "teacher", "parent", "staff"]},
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "user_id": {"type": "integer", "format": "int64", "minimum": 1},
        },
        "required": ["kind", "id", "user_id"],
    },
    "FormCreatorPrincipal": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["staff", "teacher"]},
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "display_name": {"type": "string", "nullable": True},
            "account_label": {"type": "string", "enum": ["Staff", "Teacher"]},
        },
        "required": ["kind", "id", "display_name", "account_label"],
    },
    "FormField": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "label": {"type": "string"},
            "field_type": {"type": "string", "enum": _FIELD_TYPES},
            "required": {"type": "boolean"},
            "order": {"type": "integer", "minimum": 0},
            "options": {"type": "array", "items": {"type": "string"}},
            "help_text": {"type": "string"},
        },
        "required": ["id", "label", "field_type", "required", "order", "options", "help_text"],
    },
    "Form": {
        "type": "object",
        "additionalProperties": False,
        "description": "Management-only properties are omitted for responder-only visibility.",
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["draft", "published", "closed"]},
            "is_anonymous": {"type": "boolean"},
            "allow_multiple": {"type": "boolean"},
            "branch": {"type": "integer", "format": "int64", "nullable": True},
            "opens_at": {"type": "string", "format": "date-time", "nullable": True},
            "closes_at": {"type": "string", "format": "date-time", "nullable": True},
            "published_at": {"type": "string", "format": "date-time", "nullable": True},
            "closed_at": {"type": "string", "format": "date-time", "nullable": True},
            "created_at": {"type": "string", "format": "date-time"},
            "form_fields": {
                "type": "array",
                "maxItems": 100,
                "items": {"$ref": "#/components/schemas/FormField"},
            },
            "audience_roles": {"type": "array", "items": {"type": "string", "enum": _ROLE_VALUES}},
            "audience_user_ids": {
                "type": "array",
                "items": {"type": "integer", "format": "int64"},
            },
            "audience_principals": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/FormAudiencePrincipal"},
            },
            "audience_unresolved_count": {"type": "integer", "minimum": 0},
            "created_by": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/FormCreatorPrincipal"}],
            },
            "created_by_attribution_status": {
                "type": "string",
                "enum": ["captured", "resolved", "quarantined"],
            },
        },
        "required": [
            "id",
            "title",
            "description",
            "status",
            "is_anonymous",
            "allow_multiple",
            "branch",
            "opens_at",
            "closes_at",
            "published_at",
            "closed_at",
            "created_at",
            "form_fields",
        ],
    },
    "FormResponse": _success_schema({"$ref": "#/components/schemas/Form"}),
    "FormPageResponse": _page_schema("Form"),
    "FormFieldResponse": _success_schema({"$ref": "#/components/schemas/FormField"}),
    "FormSubmissionReceipt": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": ["id", "created_at"],
    },
    "FormSubmissionReceiptResponse": _success_schema({"$ref": "#/components/schemas/FormSubmissionReceipt"}),
    "FormAnswer": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "field": {"type": "integer", "format": "int64"},
            "value": {"$ref": "#/components/schemas/FormAnswerValue"},
        },
        "required": ["field", "value"],
    },
    "FormManagedResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "form": {"type": "integer", "format": "int64"},
            "respondent": {"type": "integer", "format": "int64", "nullable": True},
            "respondent_principal": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/FormPrincipal"}],
            },
            "respondent_attribution_status": {
                "type": "string",
                "enum": [
                    "captured",
                    "resolved",
                    "anonymous",
                    "unresolved",
                    "conflicting",
                    "quarantined",
                ],
            },
            "created_at": {"type": "string", "format": "date-time"},
            "answers": {
                "type": "array",
                "maxItems": 100,
                "items": {"$ref": "#/components/schemas/FormAnswer"},
            },
        },
        "required": [
            "id",
            "form",
            "respondent",
            "respondent_principal",
            "respondent_attribution_status",
            "created_at",
            "answers",
        ],
    },
    "FormResponsePageResponse": _page_schema("FormManagedResponse"),
    "FormFieldSummary": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answered": {"type": "integer", "minimum": 0},
            "counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
            "true": {"type": "integer", "minimum": 0},
            "false": {"type": "integer", "minimum": 0},
            "avg": {"type": "number"},
            "min": {"type": "number"},
            "max": {"type": "number"},
        },
        "required": ["answered"],
    },
    "FormSummaryData": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "response_count": {"type": "integer", "minimum": 0},
            "fields": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "field": {"type": "integer", "format": "int64"},
                        "label": {"type": "string"},
                        "field_type": {"type": "string", "enum": _FIELD_TYPES},
                        "summary": {"$ref": "#/components/schemas/FormFieldSummary"},
                    },
                    "required": ["field", "label", "field_type", "summary"],
                },
            },
        },
        "required": ["response_count", "fields"],
    },
    "FormSummaryResponse": _success_schema({"$ref": "#/components/schemas/FormSummaryData"}),
    "FormAnalysisData": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "request_id": {"type": "integer", "format": "int64"},
            "status": {"type": "string"},
        },
        "required": ["request_id", "status"],
    },
    "FormAnalysisResponse": _success_schema({"$ref": "#/components/schemas/FormAnalysisData"}),
}
