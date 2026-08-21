"""Schema regressions for the scoped forms, meetings, and tasks workflows.

These checks are database-free.  ``build_schema`` walks the registered callbacks,
so schema generation also proves that each critical contract still matches the
runtime URL, methods, authentication guard, and JSON transport boundary.
"""

from __future__ import annotations

from core.openapi import build_schema

HTTP_OPERATIONS = {"get", "head", "post", "put", "patch", "delete"}
SAFE_SECURITY = [{"sessionAuth": []}, {"cookieSession": []}]
UNSAFE_SECURITY = [
    {"sessionAuth": []},
    {"cookieSession": [], "csrfHeader": []},
]
TENANT_RUNTIME_RESPONSES = {"402", "503"}


def _methods(item: dict) -> set[str]:
    return set(item).intersection(HTTP_OPERATIONS)


def _request_schema(operation: dict) -> str:
    return operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]


def _response_schema(operation: dict, status: str) -> str:
    return operation["responses"][status]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]


def _parameter_names(operation: dict) -> set[str]:
    return {parameter["name"] for parameter in operation.get("parameters", [])}


def test_every_workflow_route_has_an_exact_explicit_method_contract():
    paths = build_schema(None)["paths"]
    expected = {
        "/api/v1/forms/": {"get", "head", "post"},
        "/api/v1/forms/{pk}/": {"get", "head", "put", "patch", "delete"},
        "/api/v1/forms/{pk}/fields/": {"post"},
        "/api/v1/forms/{pk}/publish/": {"post"},
        "/api/v1/forms/{pk}/close/": {"post"},
        "/api/v1/forms/{pk}/submit/": {"post"},
        "/api/v1/forms/{pk}/responses/": {"get", "head"},
        "/api/v1/forms/{pk}/summary/": {"get", "head"},
        "/api/v1/forms/{pk}/analyze/": {"post"},
        "/api/v1/meetings/": {"get", "head", "post"},
        "/api/v1/meetings/upcoming/": {"get", "head"},
        "/api/v1/meetings/{pk}/": {"get", "head"},
        "/api/v1/meetings/{pk}/cancel/": {"post"},
        "/api/v1/meetings/{pk}/respond/": {"post"},
        "/api/v1/tasks/grades/": {"get", "head", "post"},
        "/api/v1/tasks/grades/{pk}/": {"get", "head", "put", "patch", "delete"},
        "/api/v1/tasks/": {"get", "head", "post"},
        "/api/v1/tasks/mine/": {"get", "head"},
        "/api/v1/tasks/auto-assign/": {"post"},
        "/api/v1/tasks/{pk}/": {"get", "head"},
        "/api/v1/tasks/{pk}/assign/": {"post"},
        "/api/v1/tasks/{pk}/transition/": {"post"},
    }

    assert {path: _methods(paths[path]) for path in expected} == expected


def test_workflow_safe_and_unsafe_operations_publish_cookie_csrf_semantics():
    paths = build_schema(None)["paths"]
    for path in (
        "/api/v1/forms/",
        "/api/v1/forms/{pk}/",
        "/api/v1/forms/{pk}/responses/",
        "/api/v1/forms/{pk}/summary/",
        "/api/v1/meetings/",
        "/api/v1/meetings/upcoming/",
        "/api/v1/meetings/{pk}/",
        "/api/v1/tasks/grades/",
        "/api/v1/tasks/grades/{pk}/",
        "/api/v1/tasks/",
        "/api/v1/tasks/mine/",
        "/api/v1/tasks/{pk}/",
    ):
        for method in _methods(paths[path]).intersection({"get", "head"}):
            assert paths[path][method]["security"] == SAFE_SECURITY

    for path, methods in (
        ("/api/v1/forms/", {"post"}),
        ("/api/v1/forms/{pk}/", {"put", "patch", "delete"}),
        ("/api/v1/forms/{pk}/fields/", {"post"}),
        ("/api/v1/forms/{pk}/publish/", {"post"}),
        ("/api/v1/forms/{pk}/close/", {"post"}),
        ("/api/v1/forms/{pk}/submit/", {"post"}),
        ("/api/v1/forms/{pk}/analyze/", {"post"}),
        ("/api/v1/meetings/", {"post"}),
        ("/api/v1/meetings/{pk}/cancel/", {"post"}),
        ("/api/v1/meetings/{pk}/respond/", {"post"}),
        ("/api/v1/tasks/grades/", {"post"}),
        ("/api/v1/tasks/grades/{pk}/", {"put", "patch", "delete"}),
        ("/api/v1/tasks/", {"post"}),
        ("/api/v1/tasks/auto-assign/", {"post"}),
        ("/api/v1/tasks/{pk}/assign/", {"post"}),
        ("/api/v1/tasks/{pk}/transition/", {"post"}),
    ):
        for method in methods:
            assert paths[path][method]["security"] == UNSAFE_SECURITY


def test_forms_contract_declares_bounded_closed_dtos_and_real_statuses():
    schema = build_schema(None)
    paths = schema["paths"]
    schemas = schema["components"]["schemas"]

    collection = paths["/api/v1/forms/"]
    assert _parameter_names(collection["get"]) == {
        "status",
        "branch",
        "is_anonymous",
        "search",
        "ordering",
        "page",
        "page_size",
    }
    assert _request_schema(collection["post"]) == "FormCreateRequest"
    assert _response_schema(collection["post"], "201") == "FormResponse"

    create = schemas["FormCreateRequest"]
    assert create["additionalProperties"] is False
    assert create["required"] == ["title"]
    assert create["properties"]["title"]["maxLength"] == 200
    assert create["properties"]["description"]["maxLength"] == 20_000
    assert create["properties"]["audience_user_ids"]["maxItems"] == 500
    assert "uniqueItems" not in create["properties"]["audience_user_ids"]
    assert create["properties"]["audience_roles"]["maxItems"] == 12

    detail = paths["/api/v1/forms/{pk}/"]
    assert _request_schema(detail["put"]) == "FormReplaceRequest"
    assert detail["put"]["requestBody"]["required"] is True
    assert _request_schema(detail["patch"]) == "FormPatchRequest"
    assert detail["patch"]["requestBody"]["required"] is False
    assert (
        set(detail["put"]["responses"])
        == {
            "200",
            "400",
            "401",
            "403",
            "404",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )
    assert detail["delete"]["responses"]["204"] == {"description": "Draft form deleted."}

    field = schemas["FormFieldCreateRequest"]
    assert field["additionalProperties"] is False
    assert field["properties"]["options"]["maxItems"] == 100
    assert field["properties"]["options"]["items"]["maxLength"] == 200

    submit_operation = paths["/api/v1/forms/{pk}/submit/"]["post"]
    assert _request_schema(submit_operation) == "FormSubmitRequest"
    assert (
        set(submit_operation["responses"])
        == {
            "201",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )
    answers = schemas["FormSubmitRequest"]["properties"]["answers"]
    assert answers["maxItems"] == 100
    assert answers["items"]["additionalProperties"] is False

    for action in ("publish", "close"):
        operation = paths[f"/api/v1/forms/{{pk}}/{action}/"]["post"]
        assert _request_schema(operation) == "EmptyWorkflowRequest"
        assert operation["requestBody"]["required"] is False
    analyze = paths["/api/v1/forms/{pk}/analyze/"]["post"]
    assert (
        set(analyze["responses"])
        == {
            "202",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
            "503",
        }
        | TENANT_RUNTIME_RESPONSES
    )
    assert _response_schema(analyze, "202") == "FormAnalysisResponse"


def test_meeting_contract_is_scoped_private_and_bounded():
    schema = build_schema(None)
    paths = schema["paths"]
    schemas = schema["components"]["schemas"]

    collection = paths["/api/v1/meetings/"]
    assert _parameter_names(collection["get"]) == {
        "status",
        "branch",
        "ordering",
        "page",
        "page_size",
    }
    assert _request_schema(collection["post"]) == "MeetingCreateRequest"
    assert _response_schema(collection["post"], "201") == "MeetingResponse"
    create = schemas["MeetingCreateRequest"]
    assert create["additionalProperties"] is False
    assert create["required"] == ["title", "starts_at", "ends_at"]
    assert create["oneOf"] == [{"required": ["attendees"]}, {"required": ["invitees"]}]
    assert create["not"] == {"required": ["attendees", "invitees"]}
    assert create["properties"]["agenda"]["maxLength"] == 20_000
    assert create["properties"]["attendees"]["minItems"] == 1
    assert create["properties"]["attendees"]["maxItems"] == 200
    assert create["properties"]["attendees"]["uniqueItems"] is True
    assert create["properties"]["invitees"]["items"] == {
        "$ref": "#/components/schemas/MeetingPrincipalSelector"
    }
    assert schemas["MeetingPrincipalSelector"]["additionalProperties"] is False

    upcoming = paths["/api/v1/meetings/upcoming/"]["get"]
    assert _parameter_names(upcoming) == {"page", "page_size"}
    assert _response_schema(upcoming, "200") == "MeetingPageResponse"

    respond = paths["/api/v1/meetings/{pk}/respond/"]["post"]
    assert _request_schema(respond) == "MeetingRespondRequest"
    assert schemas["MeetingRespondRequest"]["properties"]["response"]["enum"] == [
        "accepted",
        "declined",
    ]
    assert (
        set(respond["responses"])
        == {
            "200",
            "400",
            "401",
            "403",
            "404",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )

    cancel = paths["/api/v1/meetings/{pk}/cancel/"]["post"]
    assert _request_schema(cancel) == "EmptyWorkflowRequest"
    assert cancel["requestBody"]["required"] is False
    # Identity-bearing properties are optional because invitees receive a reduced shape.
    assert {"created_by", "cancelled_by", "unresolved_attendee_count"}.isdisjoint(
        schemas["Meeting"]["required"]
    )
    assert "principal" not in schemas["MeetingAttendee"]["required"]
    assert "user" not in schemas["MeetingAttendee"]["properties"]
    assert schemas["MeetingAttendee"]["additionalProperties"] is False
    assert "branch_name" in schemas["Meeting"]["required"]
    assert schemas["Meeting"]["additionalProperties"] is False


def test_tasks_contract_separates_hierarchy_configuration_and_row_scoped_work():
    schema = build_schema(None)
    paths = schema["paths"]
    schemas = schema["components"]["schemas"]

    grade_collection = paths["/api/v1/tasks/grades/"]
    assert _parameter_names(grade_collection["get"]) == {"ordering", "page", "page_size"}
    assert _request_schema(grade_collection["post"]) == "RoleGradeCreateRequest"
    grade = schemas["RoleGradeCreateRequest"]
    assert grade["additionalProperties"] is False
    assert grade["required"] == ["role", "level"]
    assert grade["properties"]["level"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 1_000_000,
    }

    grade_detail = paths["/api/v1/tasks/grades/{pk}/"]
    assert _request_schema(grade_detail["put"]) == "RoleGradeReplaceRequest"
    assert _request_schema(grade_detail["patch"]) == "RoleGradePatchRequest"
    assert grade_detail["delete"]["responses"]["204"] == {"description": "Role grade deleted."}
    assert "tasks:assign_any" in grade_detail["put"]["description"]

    task_collection = paths["/api/v1/tasks/"]
    assert _parameter_names(task_collection["get"]) == {
        "status",
        "priority",
        "assignee",
        "assignee_kind",
        "assignee_principal_id",
        "department",
        "branch",
        "search",
        "ordering",
        "page",
        "page_size",
    }
    create = schemas["TaskCreateRequest"]
    assert create["additionalProperties"] is False
    assert create["properties"]["description"]["maxLength"] == 20_000
    assert create["properties"]["priority"]["enum"] == ["low", "normal", "high", "urgent"]
    assert create["properties"]["assignee_principal"]["allOf"] == [
        {"$ref": "#/components/schemas/TaskPrincipalSelector"}
    ]
    assert create["not"] == {"required": ["assignee", "assignee_principal"]}

    assign = schemas["TaskAssignRequest"]
    assert assign["additionalProperties"] is False
    assert assign["minProperties"] == 1
    assert assign["anyOf"] == [
        {"required": ["assignee"]},
        {"required": ["assignee_principal"]},
        {"required": ["department"]},
    ]
    assert schemas["TaskPrincipalSelector"]["additionalProperties"] is False

    task = schemas["Task"]
    assert {
        "assignee_name",
        "department_name",
        "branch_name",
        "created_by_name",
    } <= set(task["required"])

    transition = paths["/api/v1/tasks/{pk}/transition/"]["post"]
    assert _request_schema(transition) == "TaskTransitionRequest"
    assert (
        set(transition["responses"])
        == {
            "200",
            "400",
            "401",
            "403",
            "404",
            "422",
            "429",
        }
        | TENANT_RUNTIME_RESPONSES
    )

    auto_operation = paths["/api/v1/tasks/auto-assign/"]["post"]
    assert _request_schema(auto_operation) == "TaskAutoAssignRequest"
    auto = schemas["TaskAutoAssignRequest"]
    assert auto["additionalProperties"] is False
    assert auto["properties"]["task_ids"]["minItems"] == 1
    assert auto["properties"]["task_ids"]["maxItems"] == 500
    assert auto["properties"]["task_ids"]["uniqueItems"] is True
    assert _response_schema(auto_operation, "200") == "TaskAutoAssignResponse"


def test_every_workflow_request_component_rejects_unknown_fields():
    schemas = build_schema(None)["components"]["schemas"]
    request_names = {
        "EmptyWorkflowRequest",
        "FormCreateRequest",
        "FormReplaceRequest",
        "FormPatchRequest",
        "FormFieldCreateRequest",
        "FormSubmitRequest",
        "MeetingCreateRequest",
        "MeetingRespondRequest",
        "RoleGradeCreateRequest",
        "RoleGradeReplaceRequest",
        "RoleGradePatchRequest",
        "TaskCreateRequest",
        "TaskAssignRequest",
        "TaskTransitionRequest",
        "TaskAutoAssignRequest",
    }
    assert all(schemas[name]["additionalProperties"] is False for name in request_names)


def test_every_internal_schema_reference_resolves_to_a_published_component():
    schema = build_schema(None)
    schemas = schema["components"]["schemas"]
    references: set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
                references.add(reference.rsplit("/", 1)[-1])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema["paths"])
    visit(schemas)
    assert references <= set(schemas)


def test_every_workflow_operation_publishes_its_exact_status_set():
    paths = build_schema(None)["paths"]
    collection_read = {"200", "400", "401", "403", "429"}
    detail_read = collection_read | {"404"}
    form_update = {"200", "400", "401", "403", "404", "422", "429"}
    expected = {
        ("/api/v1/forms/", "get"): collection_read,
        ("/api/v1/forms/", "head"): collection_read,
        ("/api/v1/forms/", "post"): {"201", "400", "401", "403", "429"},
        ("/api/v1/forms/{pk}/", "get"): detail_read,
        ("/api/v1/forms/{pk}/", "head"): detail_read,
        ("/api/v1/forms/{pk}/", "put"): form_update,
        ("/api/v1/forms/{pk}/", "patch"): form_update,
        ("/api/v1/forms/{pk}/", "delete"): {"204", "400", "401", "403", "404", "422", "429"},
        ("/api/v1/forms/{pk}/fields/", "post"): {"201", "400", "401", "403", "404", "422", "429"},
        ("/api/v1/forms/{pk}/publish/", "post"): form_update,
        ("/api/v1/forms/{pk}/close/", "post"): form_update,
        ("/api/v1/forms/{pk}/submit/", "post"): {
            "201",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
        },
        ("/api/v1/forms/{pk}/responses/", "get"): detail_read,
        ("/api/v1/forms/{pk}/responses/", "head"): detail_read,
        ("/api/v1/forms/{pk}/summary/", "get"): detail_read,
        ("/api/v1/forms/{pk}/summary/", "head"): detail_read,
        ("/api/v1/forms/{pk}/analyze/", "post"): {
            "202",
            "400",
            "401",
            "403",
            "404",
            "409",
            "422",
            "429",
            "503",
        },
        ("/api/v1/meetings/", "get"): collection_read,
        ("/api/v1/meetings/", "head"): collection_read,
        ("/api/v1/meetings/", "post"): {"201", "400", "401", "403", "429"},
        ("/api/v1/meetings/upcoming/", "get"): collection_read,
        ("/api/v1/meetings/upcoming/", "head"): collection_read,
        ("/api/v1/meetings/{pk}/", "get"): detail_read,
        ("/api/v1/meetings/{pk}/", "head"): detail_read,
        ("/api/v1/meetings/{pk}/cancel/", "post"): form_update,
        ("/api/v1/meetings/{pk}/respond/", "post"): form_update,
        ("/api/v1/tasks/grades/", "get"): collection_read,
        ("/api/v1/tasks/grades/", "head"): collection_read,
        ("/api/v1/tasks/grades/", "post"): {"201", "400", "401", "403", "429"},
        ("/api/v1/tasks/grades/{pk}/", "get"): detail_read,
        ("/api/v1/tasks/grades/{pk}/", "head"): detail_read,
        ("/api/v1/tasks/grades/{pk}/", "put"): {"200", "400", "401", "403", "404", "429"},
        ("/api/v1/tasks/grades/{pk}/", "patch"): {"200", "400", "401", "403", "404", "429"},
        ("/api/v1/tasks/grades/{pk}/", "delete"): {"204", "400", "401", "403", "404", "429"},
        ("/api/v1/tasks/", "get"): collection_read,
        ("/api/v1/tasks/", "head"): collection_read,
        ("/api/v1/tasks/", "post"): {"201", "400", "401", "403", "429"},
        ("/api/v1/tasks/mine/", "get"): collection_read,
        ("/api/v1/tasks/mine/", "head"): collection_read,
        ("/api/v1/tasks/{pk}/", "get"): detail_read,
        ("/api/v1/tasks/{pk}/", "head"): detail_read,
        ("/api/v1/tasks/{pk}/assign/", "post"): {"200", "400", "401", "403", "404", "429"},
        ("/api/v1/tasks/{pk}/transition/", "post"): form_update,
        ("/api/v1/tasks/auto-assign/", "post"): {"200", "400", "401", "403", "422", "429"},
    }
    assert {(path, method) for path, method in expected} == {
        (path, method)
        for path, item in paths.items()
        if path.startswith(("/api/v1/forms/", "/api/v1/meetings/", "/api/v1/tasks/"))
        for method in _methods(item)
    }
    for (path, method), statuses in expected.items():
        assert set(paths[path][method]["responses"]) == statuses | TENANT_RUNTIME_RESPONSES


def test_workflow_request_bodies_are_present_only_at_registered_json_boundaries():
    paths = build_schema(None)["paths"]
    expected = {
        ("/api/v1/forms/", "post"): ("FormCreateRequest", True),
        ("/api/v1/forms/{pk}/", "put"): ("FormReplaceRequest", True),
        ("/api/v1/forms/{pk}/", "patch"): ("FormPatchRequest", False),
        ("/api/v1/forms/{pk}/fields/", "post"): ("FormFieldCreateRequest", True),
        ("/api/v1/forms/{pk}/publish/", "post"): ("EmptyWorkflowRequest", False),
        ("/api/v1/forms/{pk}/close/", "post"): ("EmptyWorkflowRequest", False),
        ("/api/v1/forms/{pk}/submit/", "post"): ("FormSubmitRequest", True),
        ("/api/v1/forms/{pk}/analyze/", "post"): ("EmptyWorkflowRequest", False),
        ("/api/v1/meetings/", "post"): ("MeetingCreateRequest", True),
        ("/api/v1/meetings/{pk}/cancel/", "post"): ("EmptyWorkflowRequest", False),
        ("/api/v1/meetings/{pk}/respond/", "post"): ("MeetingRespondRequest", True),
        ("/api/v1/tasks/grades/", "post"): ("RoleGradeCreateRequest", True),
        ("/api/v1/tasks/grades/{pk}/", "put"): ("RoleGradeReplaceRequest", True),
        ("/api/v1/tasks/grades/{pk}/", "patch"): ("RoleGradePatchRequest", False),
        ("/api/v1/tasks/", "post"): ("TaskCreateRequest", True),
        ("/api/v1/tasks/{pk}/assign/", "post"): ("TaskAssignRequest", True),
        ("/api/v1/tasks/{pk}/transition/", "post"): ("TaskTransitionRequest", True),
        ("/api/v1/tasks/auto-assign/", "post"): ("TaskAutoAssignRequest", True),
    }
    operations = {
        (path, method): operation
        for path, item in paths.items()
        if path.startswith(("/api/v1/forms/", "/api/v1/meetings/", "/api/v1/tasks/"))
        for method, operation in item.items()
        if method in HTTP_OPERATIONS
    }
    assert {key for key, operation in operations.items() if "requestBody" in operation} == set(expected)
    for key, (schema_name, required) in expected.items():
        operation = operations[key]
        assert _request_schema(operation) == schema_name
        assert operation["requestBody"]["required"] is required


def test_workflow_error_responses_use_the_canonical_flat_error_component():
    paths = build_schema(None)["paths"]
    for path, item in paths.items():
        if not path.startswith(("/api/v1/forms/", "/api/v1/meetings/", "/api/v1/tasks/")):
            continue
        for method in _methods(item):
            for status, _response in item[method]["responses"].items():
                if status.startswith("2"):
                    continue
                component = _response_schema(item[method], status)
                if status == "402":
                    assert component == "SubscriptionRequiredError"
                elif status == "503":
                    # A domain operation may document a more specific in-view 503 and wins
                    # over the shared middleware fallback through setdefault semantics.
                    assert component in {"Error", "TemporarilyUnavailableError"}
                else:
                    assert component == "Error"
