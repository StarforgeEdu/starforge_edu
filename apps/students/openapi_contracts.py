"""Explicit contracts for decision-critical student aggregate reads."""

from core.openapi_contracts import SESSION_SECURITY, OperationContract, error_response, json_response

_WINDOW_PARAMETERS = (
    {
        "name": "date_from",
        "in": "query",
        "required": False,
        "description": "Inclusive organization-calendar start date (YYYY-MM-DD).",
        "schema": {"type": "string", "format": "date"},
    },
    {
        "name": "date_to",
        "in": "query",
        "required": False,
        "description": "Inclusive organization-calendar end date (YYYY-MM-DD).",
        "schema": {"type": "string", "format": "date"},
    },
)

LEADERSHIP_PROFILE_GET_CONTRACT = OperationContract(
    method="GET",
    summary="Read one permission-pruned student leadership profile",
    description=(
        "Returns one student already proven visible to the caller. Optional learning, "
        "attendance, family, safeguarding, and finance sections are included only when "
        "the exact membership covering this student grants the corresponding permission."
    ),
    permission="students:read",
    security=SESSION_SECURITY,
    parameters=_WINDOW_PARAMETERS,
    responses={
        "200": json_response("Scoped student leadership profile.", "StudentLeadershipProfileResponse"),
        "400": error_response("The inclusive date window or query parameters are invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks student read authority."),
        "404": error_response("The student does not exist inside the caller's visible scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
        "503": error_response("Organization time configuration is unavailable."),
    },
    operation_id="get_student_leadership_profile",
)

LEADERSHIP_PROFILE_HEAD_CONTRACT = OperationContract(
    method="HEAD",
    summary="Check one scoped student leadership profile",
    description="Same authorization, query, and status semantics as GET, without a response body.",
    permission="students:read",
    security=SESSION_SECURITY,
    parameters=_WINDOW_PARAMETERS,
    responses={
        "200": json_response("The scoped leadership profile is available."),
        "400": error_response("The inclusive date window or query parameters are invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks student read authority."),
        "404": error_response("The student does not exist inside the caller's visible scope."),
        "429": error_response("Authenticated request rate limit exceeded."),
        "503": error_response("Organization time configuration is unavailable."),
    },
    operation_id="head_student_leadership_profile",
)


OPENAPI_SCHEMAS = {
    "StudentLeadershipLabel": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "name": {"type": "string"},
        },
        "required": ["id", "name"],
    },
    "StudentLeadershipWindow": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "date_from": {"type": "string", "format": "date"},
            "date_to": {"type": "string", "format": "date"},
            "inclusive": {"type": "boolean", "enum": [True]},
            "timezone": {
                "type": "string",
                "description": "Authoritative organization IANA timezone.",
            },
        },
        "required": ["date_from", "date_to", "inclusive", "timezone"],
    },
    "StudentLeadershipMoney": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "amount_uzs": {
                "type": "string",
                "pattern": r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{2})$",
                "deprecated": True,
                "description": "Compatibility decimal-major UZS value; never a JSON float.",
            },
            "amount_minor": {"type": "integer", "format": "int64"},
            "currency": {"type": "string", "minLength": 3, "maxLength": 3},
        },
        "required": ["amount_uzs", "amount_minor", "currency"],
    },
    "StudentLeadershipPhoto": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "available": {"type": "boolean"},
            "download_url": {
                "type": "string",
                "format": "uri",
                "nullable": True,
                "description": "Null until tenant and student ownership of the stored key is proven.",
            },
        },
        "required": ["available", "download_url"],
    },
    "StudentLeadershipIdentity": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer", "format": "int64", "minimum": 1},
            "public_student_id": {"type": "string"},
            "username": {"type": "string", "nullable": True},
            "full_name": {"type": "string"},
            "first_name": {"type": "string"},
            "middle_name": {"type": "string"},
            "last_name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "birthdate": {"type": "string", "format": "date", "nullable": True},
            "gender": {"type": "string", "enum": ["", "m", "f"]},
            "status": {"type": "string"},
            "is_active": {"type": "boolean"},
            "branch": {"$ref": "#/components/schemas/StudentLeadershipLabel"},
            "current_group": {
                "type": "object",
                "nullable": True,
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer", "format": "int64", "minimum": 1},
                    "name": {"type": "string"},
                    "level": {"type": "string"},
                    "department": {
                        "nullable": True,
                        "allOf": [{"$ref": "#/components/schemas/StudentLeadershipLabel"}],
                    },
                },
                "required": ["id", "name", "level", "department"],
            },
            "academic_level": {"type": "string"},
            "location": {"type": "string"},
            "previous_school": {"type": "string"},
            "enrollment_date": {"type": "string", "format": "date", "nullable": True},
            "block": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "is_blocked": {"type": "boolean"},
                    "blocked_at": {"type": "string", "format": "date-time", "nullable": True},
                    "reason": {"type": "string"},
                },
                "required": ["is_blocked", "blocked_at", "reason"],
            },
            "photo": {"$ref": "#/components/schemas/StudentLeadershipPhoto"},
        },
        "required": [
            "id",
            "public_student_id",
            "username",
            "full_name",
            "first_name",
            "middle_name",
            "last_name",
            "phone",
            "email",
            "birthdate",
            "gender",
            "status",
            "is_active",
            "branch",
            "current_group",
            "academic_level",
            "location",
            "previous_school",
            "enrollment_date",
            "block",
            "photo",
        ],
    },
    "StudentLeadershipLearning": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "teachers": {
                "type": "array",
                "nullable": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "name": {"type": "string"},
                        "responsibility": {"type": "string"},
                    },
                    "required": ["id", "name", "responsibility"],
                },
            },
            "subjects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "code": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["id", "code", "name"],
                },
            },
            "recent_grades": {
                "type": "array",
                "nullable": True,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "subject": {"$ref": "#/components/schemas/StudentLeadershipLabel"},
                        "term": {"$ref": "#/components/schemas/StudentLeadershipLabel"},
                        "value_raw_pct": {"type": "number", "minimum": 0, "maximum": 100},
                        "value_display": {"type": "string"},
                        "published_at": {
                            "type": "string",
                            "format": "date-time",
                            "nullable": True,
                        },
                        "computed_at": {"type": "string", "format": "date-time"},
                    },
                    "required": [
                        "id",
                        "subject",
                        "term",
                        "value_raw_pct",
                        "value_display",
                        "published_at",
                        "computed_at",
                    ],
                },
            },
            "recent_exam_results": {
                "type": "array",
                "nullable": True,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "exam": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "integer", "format": "int64", "minimum": 1},
                                "title": {"type": "string"},
                                "date": {"type": "string", "format": "date"},
                            },
                            "required": ["id", "title", "date"],
                        },
                        "subject": {"$ref": "#/components/schemas/StudentLeadershipLabel"},
                        "score": {"type": "string", "pattern": r"^[0-9]+\.[0-9]{2}$"},
                        "maximum": {"type": "string", "pattern": r"^[0-9]+\.[0-9]{2}$"},
                        "score_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                        "last_graded_at": {"type": "string", "format": "date-time"},
                    },
                    "required": [
                        "id",
                        "exam",
                        "subject",
                        "score",
                        "maximum",
                        "score_fraction",
                        "last_graded_at",
                    ],
                },
            },
            "assignments": {
                "type": "object",
                "nullable": True,
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in ("assigned", "completed", "open", "late")
                },
                "required": ["assigned", "completed", "open", "late"],
            },
            "latest_transcript": {
                "type": "object",
                "nullable": True,
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer", "format": "int64", "minimum": 1},
                    "term": {"type": "integer", "format": "int64", "nullable": True},
                    "status": {"type": "string"},
                    "generated_at": {"type": "string", "format": "date-time", "nullable": True},
                    "requested_at": {"type": "string", "format": "date-time"},
                },
                "required": ["id", "term", "status", "generated_at", "requested_at"],
            },
        },
        "required": [
            "teachers",
            "subjects",
            "recent_grades",
            "recent_exam_results",
            "assignments",
            "latest_transcript",
        ],
    },
    "StudentLeadershipAttendance": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "metric_definition": {"type": "string"},
            **{
                name: {"type": "integer", "minimum": 0}
                for name in (
                    "present",
                    "late",
                    "absent",
                    "excused",
                    "attended",
                    "countable_sessions",
                    "current_attendance_streak",
                )
            },
            "attendance_rate_fraction": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "nullable": True,
            },
            "last_attendance": {
                "type": "object",
                "nullable": True,
                "additionalProperties": False,
                "properties": {
                    "lesson": {"type": "integer", "format": "int64", "minimum": 1},
                    "group": {"type": "integer", "format": "int64", "minimum": 1},
                    "group_name": {"type": "string"},
                    "starts_at": {"type": "string", "format": "date-time"},
                    "status": {"type": "string", "enum": ["present", "late", "absent", "excused"]},
                },
                "required": ["lesson", "group", "group_name", "starts_at", "status"],
            },
            "per_group": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "group": {"$ref": "#/components/schemas/StudentLeadershipLabel"},
                        "attended": {"type": "integer", "minimum": 0},
                        "countable_sessions": {"type": "integer", "minimum": 0},
                        "attendance_rate_fraction": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "nullable": True,
                        },
                        "excused": {"type": "integer", "minimum": 0},
                    },
                    "required": [
                        "group",
                        "attended",
                        "countable_sessions",
                        "attendance_rate_fraction",
                        "excused",
                    ],
                },
            },
        },
        "required": [
            "metric_definition",
            "present",
            "late",
            "absent",
            "excused",
            "attended",
            "countable_sessions",
            "attendance_rate_fraction",
            "current_attendance_streak",
            "last_attendance",
            "per_group",
        ],
    },
    "StudentLeadershipFamily": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "guardians": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "parent": {"type": "integer", "format": "int64", "minimum": 1},
                        "name": {"type": "string"},
                        "relationship": {"type": "string"},
                        "is_primary": {"type": "boolean"},
                        "contacts": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "phone": {"type": "string", "nullable": True},
                                "email": {"type": "string", "nullable": True},
                                "verification_status": {
                                    "type": "string",
                                    "enum": ["not_recorded"],
                                },
                            },
                            "required": ["phone", "email", "verification_status"],
                        },
                        "custody_notes": {"type": "string"},
                    },
                    "required": ["id", "parent", "name", "relationship", "is_primary", "contacts"],
                },
            },
            "pickup_authorizations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "relationship": {"type": "string"},
                    },
                    "required": ["id", "name", "phone", "relationship"],
                },
            },
            "consent_flags": {
                "type": "object",
                "nullable": True,
                "additionalProperties": {"type": "boolean"},
            },
            "safeguarding": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "medical_notes": {"type": "string"},
                    "emergency_contacts": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                "required": ["medical_notes", "emergency_contacts"],
            },
        },
        "required": ["guardians", "pickup_authorizations", "consent_flags"],
    },
    "StudentLeadershipFinance": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "window": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"$ref": "#/components/schemas/StudentLeadershipMoney"}
                    for name in ("billed", "collected", "refunded")
                },
                "required": ["billed", "collected", "refunded"],
            },
            "all_time": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "outstanding": {"$ref": "#/components/schemas/StudentLeadershipMoney"},
                    "overdue": {"$ref": "#/components/schemas/StudentLeadershipMoney"},
                    "open_invoice_count": {"type": "integer", "minimum": 0},
                    "overdue_invoice_count": {"type": "integer", "minimum": 0},
                },
                "required": ["outstanding", "overdue", "open_invoice_count", "overdue_invoice_count"],
            },
            "fee_schedules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "name": {"type": "string"},
                        "group": {"type": "integer", "format": "int64", "nullable": True},
                        "billing_period": {"type": "string"},
                        "amount": {"$ref": "#/components/schemas/StudentLeadershipMoney"},
                        "due_day_of_month": {"type": "integer", "minimum": 1, "maximum": 31},
                    },
                    "required": ["id", "name", "group", "billing_period", "amount", "due_day_of_month"],
                },
            },
            "discounts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "integer", "format": "int64", "minimum": 1},
                        "type": {"type": "string"},
                        "percent_pct": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                            "nullable": True,
                        },
                        "fixed_amount": {
                            "nullable": True,
                            "allOf": [{"$ref": "#/components/schemas/StudentLeadershipMoney"}],
                        },
                        "valid_from": {"type": "string", "format": "date", "nullable": True},
                        "valid_until": {"type": "string", "format": "date", "nullable": True},
                    },
                    "required": [
                        "id",
                        "type",
                        "percent_pct",
                        "fixed_amount",
                        "valid_from",
                        "valid_until",
                    ],
                },
            },
            "last_payment": {
                "type": "object",
                "nullable": True,
                "additionalProperties": False,
                "properties": {
                    "payment": {"type": "integer", "format": "int64", "minimum": 1},
                    "allocated": {"$ref": "#/components/schemas/StudentLeadershipMoney"},
                    "provider": {"type": "string", "nullable": True},
                    "status": {"type": "string"},
                    "paid_at": {"type": "string", "format": "date-time", "nullable": True},
                },
                "required": ["payment", "allocated", "provider", "status", "paid_at"],
            },
        },
        "required": ["window", "all_time", "fee_schedules", "discounts", "last_payment"],
    },
    "StudentLeadershipCoverageEntry": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["available", "not_authorized"]},
            "window": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date_from": {"type": "string", "format": "date"},
                    "date_to": {"type": "string", "format": "date"},
                    "inclusive": {"type": "boolean", "enum": [True]},
                },
                "required": ["date_from", "date_to", "inclusive"],
            },
        },
        "required": ["status"],
    },
    "StudentLeadershipWarning": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "code": {
                "type": "string",
                "enum": [
                    "family_verification_not_recorded",
                    "student_photo_unavailable",
                    "record_actor_not_recorded",
                ],
            },
            "message": {"type": "string"},
            "affected_sections": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string"},
            },
        },
        "required": ["code", "message", "affected_sections"],
    },
    "StudentLeadershipProfileData": {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "Sections absent from this object were not authorized for the exact student scope; "
            "the coverage map distinguishes that state from an authorized empty dataset."
        ),
        "properties": {
            "generated_at": {"type": "string", "format": "date-time"},
            "window": {"$ref": "#/components/schemas/StudentLeadershipWindow"},
            "identity": {"$ref": "#/components/schemas/StudentLeadershipIdentity"},
            "record_metadata": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "created_at": {"type": "string", "format": "date-time"},
                    "updated_at": {"type": "string", "format": "date-time"},
                    "created_by": {"type": "integer", "format": "int64", "nullable": True},
                    "updated_by": {"type": "integer", "format": "int64", "nullable": True},
                    "custom_fields": {
                        "type": "object",
                        "nullable": True,
                        "additionalProperties": True,
                    },
                },
                "required": ["created_at", "updated_at", "created_by", "updated_by", "custom_fields"],
            },
            "learning": {"$ref": "#/components/schemas/StudentLeadershipLearning"},
            "attendance": {"$ref": "#/components/schemas/StudentLeadershipAttendance"},
            "family": {"$ref": "#/components/schemas/StudentLeadershipFamily"},
            "finance": {"$ref": "#/components/schemas/StudentLeadershipFinance"},
            "coverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"$ref": "#/components/schemas/StudentLeadershipCoverageEntry"}
                    for name in ("identity", "learning", "attendance", "family", "safeguarding", "finance")
                },
                "required": ["identity", "learning", "attendance", "family", "safeguarding", "finance"],
            },
            "warnings": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/StudentLeadershipWarning"},
            },
        },
        "required": ["generated_at", "window", "identity", "record_metadata", "coverage", "warnings"],
    },
    "StudentLeadershipProfileResponse": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "enum": [True]},
            "data": {"$ref": "#/components/schemas/StudentLeadershipProfileData"},
        },
        "required": ["success", "data"],
    },
}
