"""Closed public contracts for card and assessment-component evidence."""

from core.openapi import build_schema


def test_attendance_card_contract_is_closed_and_explicit():
    schema = build_schema(None)
    path = schema["paths"]["/api/v1/attendance/lessons/{lesson_id}/mark/"]
    assert set(path) >= {"parameters", "post"}
    request_ref = path["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert request_ref.endswith("/AttendanceMarkRequest")
    entry = schema["components"]["schemas"]["AttendanceMarkEntry"]
    assert entry["additionalProperties"] is False
    assert entry["properties"]["card_type"]["enum"] == ["", "smart", "warning"]
    record = schema["components"]["schemas"]["AttendanceRecord"]
    assert "card_type" in record["required"]


def test_exam_component_contract_is_bounded_closed_and_staff_gated():
    schema = build_schema(None)
    path = schema["paths"]["/api/v1/academics/exams/{pk}/results/"]
    assert {"get", "head", "post"} <= set(path)
    assert "academics:write" in path["get"]["description"]
    component = schema["components"]["schemas"]["ExamResultComponentInput"]
    assert component["additionalProperties"] is False
    assert component["properties"]["name"]["maxLength"] == 64
    assert component["properties"]["max_score"]["oneOf"][0]["minimum"] > 0
    request = schema["components"]["schemas"]["ExamResultWriteRequest"]
    assert request["maxItems"] == 5000
    result = schema["components"]["schemas"]["ExamResult"]
    assert result["properties"]["components"]["maxItems"] == 20
