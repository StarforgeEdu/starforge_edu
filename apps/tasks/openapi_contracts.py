"""Executable OpenAPI contracts for task boards and role-grade configuration."""

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

_ROLES = [
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
_TASK_STATUSES = ["open", "in_progress", "blocked", "done", "cancelled"]
_TASK_PRIORITIES = ["low", "normal", "high", "urgent"]


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


_GRADE_FILTERS = _page_parameters(
    {
        "name": "ordering",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["level", "-level", "role", "-role"]},
    }
)

_TASK_FILTERS = _page_parameters(
    {
        "name": "status",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": _TASK_STATUSES},
    },
    {
        "name": "priority",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": _TASK_PRIORITIES},
    },
    {
        "name": "assignee_kind",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["staff", "teacher"]},
    },
    {
        "name": "assignee_principal_id",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1},
    },
    *(
        {
            "name": name,
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1},
        }
        for name in ("assignee", "department", "branch")
    ),
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
            "enum": [
                "created_at",
                "-created_at",
                "due_at",
                "-due_at",
                "priority",
                "-priority",
            ],
        },
    },
)


def _grade_read(method: str, *, collection: bool = False) -> OperationContract:
    responses = {
        "200": (
            json_response("Role-grade page.", "RoleGradePageResponse")
            if method == "GET" and collection
            else json_response("Role grade.", "RoleGradeResponse")
            if method == "GET"
            else json_response("Role-grade visibility confirmed.")
        ),
        "400": error_response("A declared query parameter is malformed or unsupported."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks task read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not collection:
        responses["404"] = error_response("The role grade does not exist.")
    return OperationContract(
        method=method,
        summary="List role grades" if collection else "Read a role grade",
        description=(
            "Reads the organization-wide seniority table used by hierarchy-gated task "
            "assignment. This table is configuration, not a branch-scoped task record."
        ),
        permission="tasks:read",
        security=SESSION_SECURITY,
        parameters=_GRADE_FILTERS if collection else (),
        responses=responses,
        operation_id=f"{method.lower()}_task_role_grades_{'collection' if collection else 'detail'}",
    )


def _grade_update(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="Replace a role grade" if method == "PUT" else "Patch a role grade",
        description=(
            "Changes an organization-wide hierarchy entry. The operation requires an "
            "unscoped tasks:assign_any grant; branch- or department-scoped grants fail closed."
        ),
        permission="tasks:assign_any",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request(
            "RoleGradeReplaceRequest" if method == "PUT" else "RoleGradePatchRequest",
            required=method == "PUT",
        ),
        responses={
            "200": json_response("Role grade updated.", "RoleGradeResponse"),
            "400": error_response("The closed role-grade DTO is invalid or duplicates a role."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks unscoped role-grade authority."),
            "404": error_response("The role grade does not exist."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_task_role_grades_detail",
    )


ROLE_GRADES_COLLECTION_CONTRACTS = (
    _grade_read("GET", collection=True),
    _grade_read("HEAD", collection=True),
    OperationContract(
        method="POST",
        summary="Create an organization-wide role grade",
        description=(
            "Creates one bounded seniority level for a canonical role. Requires an unscoped "
            "tasks:assign_any grant and rejects duplicate role rows under concurrent requests."
        ),
        permission="tasks:assign_any",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("RoleGradeCreateRequest"),
        responses={
            "201": json_response("Role grade created.", "RoleGradeResponse"),
            "400": error_response("The closed role-grade DTO is invalid or duplicates a role."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks unscoped role-grade authority."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="post_task_role_grades_collection",
    ),
)

ROLE_GRADE_DETAIL_CONTRACTS = (
    _grade_read("GET"),
    _grade_read("HEAD"),
    _grade_update("PUT"),
    _grade_update("PATCH"),
    OperationContract(
        method="DELETE",
        summary="Delete an organization-wide role grade",
        description="Requires unscoped tasks:assign_any authority and is idempotent for a locked row.",
        permission="tasks:assign_any",
        security=UNSAFE_SESSION_SECURITY,
        responses={
            "204": json_response("Role grade deleted."),
            "400": error_response("Query parameters are not accepted by this operation."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks unscoped role-grade authority."),
            "404": error_response("The role grade does not exist."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="delete_task_role_grades_detail",
    ),
)


def _task_read(method: str, *, collection: bool = False, mine: bool = False) -> OperationContract:
    parameters = _page_parameters() if mine else _TASK_FILTERS if collection else ()
    responses = {
        "200": (
            json_response("Visible task page.", "TaskPageResponse")
            if method == "GET" and (collection or mine)
            else json_response("Visible task.", "TaskResponse")
            if method == "GET"
            else json_response("Task visibility confirmed.")
        ),
        "400": error_response("A declared query parameter is malformed or unsupported."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks task read authority."),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not (collection or mine):
        responses["404"] = error_response("The task is outside the caller's visible scope.")
    return OperationContract(
        method=method,
        summary=(
            "List tasks assigned to the current principal"
            if mine
            else "List visible tasks"
            if collection
            else "Read a visible task"
        ),
        description=(
            "Task visibility is the union of exact role-native assignment and the active "
            "branch/department scope granting tasks:read. Quarantined legacy assignees are "
            "never exposed as authoritative identities. Out-of-scope IDs return not found."
        ),
        permission="tasks:read",
        security=SESSION_SECURITY,
        parameters=parameters,
        responses=responses,
        operation_id=(
            f"{method.lower()}_tasks_mine"
            if mine
            else f"{method.lower()}_tasks_{'collection' if collection else 'detail'}"
        ),
    )


TASKS_COLLECTION_CONTRACTS = (
    _task_read("GET", collection=True),
    _task_read("HEAD", collection=True),
    OperationContract(
        method="POST",
        summary="Create a scoped task",
        description=(
            "Creates a bounded task within the exact tasks:write branch/department scope. "
            "Use either the legacy public user selector or the explicit role-account selector, "
            "never both. The target must be one active staff or teacher principal in the same "
            "task boundary, and hierarchy rules are enforced before creation."
        ),
        permission="tasks:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("TaskCreateRequest"),
        responses={
            "201": json_response("Task created.", "TaskResponse"),
            "400": error_response("The closed task DTO or related selector is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The requested scope or assignee exceeds the caller's authority."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id="post_tasks_collection",
    ),
)

TASKS_MINE_CONTRACTS = (
    _task_read("GET", mine=True),
    _task_read("HEAD", mine=True),
)

TASK_DETAIL_CONTRACTS = (
    _task_read("GET"),
    _task_read("HEAD"),
)

TASK_ASSIGN_CONTRACT = OperationContract(
    method="POST",
    summary="Assign or clear a task target",
    description=(
        "Atomically rechecks the current task and exact tasks:write boundary before changing "
        "the assignee or department. Use either assignee or assignee_principal, never both. "
        "At least one field must be supplied; null explicitly clears that field. Hierarchy "
        "and role-principal ambiguity fail closed."
    ),
    permission="tasks:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("TaskAssignRequest"),
    responses={
        "200": json_response("Task assignment updated.", "TaskResponse"),
        "400": error_response("The closed assignment DTO or related selector is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The assignment exceeds scope or hierarchy authority."),
        "404": error_response("The task is outside the caller's tasks write scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_tasks_assign",
)

TASK_TRANSITION_CONTRACT = OperationContract(
    method="POST",
    summary="Transition a task through its state machine",
    description=(
        "Locks and rechecks the task before applying one declared status transition. The exact "
        "assignee may transition their own task; broader changes require transition_any or "
        "assign_any at the same task boundary. Repeating the current state is idempotent."
    ),
    permission="tasks:read",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("TaskTransitionRequest"),
    responses={
        "200": json_response("Task status updated or already current.", "TaskResponse"),
        "400": error_response("The closed transition DTO or status value is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The principal cannot transition this task."),
        "404": error_response("The task is outside the caller's visible scope."),
        "422": error_response("The requested edge is invalid from the current task state."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_tasks_transition",
)

TASK_AUTO_ASSIGN_CONTRACT = OperationContract(
    method="POST",
    summary="Distribute a bounded task batch within one department",
    description=(
        "Serializes concurrent runs for the department. Fair mode assigns deterministically "
        "against exact-principal open-task load; free mode clears assignees. The batch is "
        "bounded to 500 unique open tasks and every row must belong to the selected department."
    ),
    permission="tasks:write",
    security=UNSAFE_SESSION_SECURITY,
    request_body=json_request("TaskAutoAssignRequest"),
    responses={
        "200": json_response("Task batch distributed.", "TaskAutoAssignResponse"),
        "400": error_response("The closed batch DTO, mode, or department selector is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The department or assignment hierarchy is outside caller authority."),
        "422": error_response("The batch contains non-open tasks or has no eligible staff."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_tasks_auto_assign",
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


_ROLE_GRADE_PROPERTIES: dict[str, Any] = {
    "role": {"type": "string", "enum": _ROLES},
    "level": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
    "label": {"type": "string", "maxLength": 64},
}

_NULLABLE_ID = {"type": "integer", "format": "int64", "minimum": 1, "nullable": True}

OPENAPI_SCHEMAS: dict[str, dict[str, Any]] = {
    "RoleGradeCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(_ROLE_GRADE_PROPERTIES),
        "required": ["role", "level"],
    },
    "RoleGradeReplaceRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(_ROLE_GRADE_PROPERTIES),
        "required": ["role", "level"],
    },
    "RoleGradePatchRequest": {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "properties": dict(_ROLE_GRADE_PROPERTIES),
    },
    "RoleGrade": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            **_ROLE_GRADE_PROPERTIES,
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
        "required": ["id", "role", "level", "label", "created_at", "updated_at"],
    },
    "RoleGradeResponse": _success_schema({"$ref": "#/components/schemas/RoleGrade"}),
    "RoleGradePageResponse": _page_schema("RoleGrade"),
    "TaskCreateRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": {"type": "string", "maxLength": 20_000},
            "priority": {"type": "string", "enum": _TASK_PRIORITIES, "default": "normal"},
            "assignee": dict(_NULLABLE_ID),
            "assignee_principal": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/TaskPrincipalSelector"}],
            },
            "department": dict(_NULLABLE_ID),
            "branch": dict(_NULLABLE_ID),
            "due_at": {"type": "string", "format": "date-time", "nullable": True},
        },
        "required": ["title"],
        "not": {"required": ["assignee", "assignee_principal"]},
    },
    "TaskAssignRequest": {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "maxProperties": 2,
        "properties": {
            "assignee": dict(_NULLABLE_ID),
            "assignee_principal": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/TaskPrincipalSelector"}],
            },
            "department": dict(_NULLABLE_ID),
        },
        "anyOf": [
            {"required": ["assignee"]},
            {"required": ["assignee_principal"]},
            {"required": ["department"]},
        ],
        "not": {"required": ["assignee", "assignee_principal"]},
    },
    "TaskTransitionRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"status": {"type": "string", "enum": _TASK_STATUSES}},
        "required": ["status"],
    },
    "TaskAutoAssignRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 500,
                "uniqueItems": True,
                "items": {"type": "integer", "format": "int64", "minimum": 1},
            },
            "department": {"type": "integer", "format": "int64", "minimum": 1},
            "mode": {"type": "string", "enum": ["fair", "free"], "default": "fair"},
        },
        "required": ["task_ids", "department"],
    },
    "TaskPrincipalSelector": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["staff", "teacher"]},
            "id": {"type": "integer", "format": "int64", "minimum": 1},
        },
        "required": ["kind", "id"],
    },
    "TaskPrincipal": {
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
    "Task": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": _TASK_STATUSES},
            "priority": {"type": "string", "enum": _TASK_PRIORITIES},
            "assignee": {"type": "integer", "format": "int64", "nullable": True},
            "assignee_principal": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/TaskPrincipal"}],
            },
            "assignee_name": {"type": "string", "nullable": True},
            "assignee_attribution_status": {"type": "string", "enum": ["captured", "quarantined"]},
            "department": {"type": "integer", "format": "int64", "nullable": True},
            "department_name": {"type": "string", "nullable": True},
            "branch": {"type": "integer", "format": "int64", "nullable": True},
            "branch_name": {"type": "string", "nullable": True},
            "due_at": {"type": "string", "format": "date-time", "nullable": True},
            "created_by": {
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/TaskPrincipal"}],
            },
            "created_by_name": {"type": "string", "nullable": True},
            "created_by_attribution_status": {
                "type": "string",
                "enum": ["captured", "resolved", "quarantined"],
            },
            "completed_at": {"type": "string", "format": "date-time", "nullable": True},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "id",
            "title",
            "description",
            "status",
            "priority",
            "assignee",
            "assignee_principal",
            "assignee_name",
            "assignee_attribution_status",
            "department",
            "department_name",
            "branch",
            "branch_name",
            "due_at",
            "created_by",
            "created_by_name",
            "created_by_attribution_status",
            "completed_at",
            "created_at",
        ],
    },
    "TaskResponse": _success_schema({"$ref": "#/components/schemas/Task"}),
    "TaskPageResponse": _page_schema("Task"),
    "TaskAutoAssignment": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task": {"type": "integer", "format": "int64"},
            "assignee": {"type": "integer", "format": "int64"},
        },
        "required": ["task", "assignee"],
    },
    "TaskAutoAssignData": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["fair", "free"]},
            "assigned": {"type": "integer", "minimum": 0, "maximum": 500},
            "freed": {"type": "integer", "minimum": 0, "maximum": 500},
            "assignments": {
                "type": "array",
                "maxItems": 500,
                "items": {"$ref": "#/components/schemas/TaskAutoAssignment"},
            },
        },
        "required": ["mode", "assigned", "freed", "assignments"],
    },
    "TaskAutoAssignResponse": _success_schema({"$ref": "#/components/schemas/TaskAutoAssignData"}),
}
