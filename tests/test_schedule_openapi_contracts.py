"""Database-free executable contracts for signed iCalendar URLs and feeds."""

from core.openapi import build_schema


def test_ical_url_issuance_is_session_secured_and_exact():
    operation = build_schema(None)["paths"]["/api/v1/schedule/ical-url/"]

    assert {method for method in operation if method in {"get", "head", "post", "patch", "delete"}} == {
        "get",
        "head",
    }
    assert operation["get"]["security"] == [
        {"sessionAuth": []},
        {"cookieSession": []},
    ]
    assert operation["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "additionalProperties"
    ] is False


def test_ical_feed_is_explicitly_token_authenticated_and_returns_calendar():
    operation = build_schema(None)["paths"]["/api/v1/schedule/ical/{token}/"]

    assert {method for method in operation if method in {"get", "head", "post", "patch", "delete"}} == {
        "get",
        "head",
    }
    assert "security" not in operation["get"]
    assert operation["get"]["responses"]["200"]["content"] == {
        "text/calendar": {"schema": {"type": "string", "format": "binary"}}
    }
    assert set(operation["get"]["responses"]) >= {"200", "401", "402", "405", "429", "503"}
    assert operation["parameters"] == [
        {
            "name": "token",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
