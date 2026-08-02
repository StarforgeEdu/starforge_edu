"""Public error envelopes must not echo framework or provider exception text."""

from django.core.exceptions import PermissionDenied
from rest_framework.exceptions import APIException, AuthenticationFailed

from core.exceptions import drf_exception_handler


def test_django_permission_denial_redacts_internal_detail():
    secret = "postgresql://operator:secret@internal/tenant"

    response = drf_exception_handler(PermissionDenied(secret), {})

    assert response is not None
    assert response.status_code == 403
    assert response.data["code"] == "forbidden"
    assert secret not in str(response.data)


def test_drf_authentication_failure_redacts_internal_detail():
    secret = "bearer credential abc.def.ghi rejected by tenant resolver"

    response = drf_exception_handler(AuthenticationFailed(secret), {})

    assert response is not None
    assert response.status_code == 401
    assert response.data["code"] == "authentication_failed"
    assert secret not in str(response.data)


def test_unknown_api_exception_redacts_internal_detail():
    secret = "provider response at /private/path contained credential=hidden"

    response = drf_exception_handler(APIException(secret), {})

    assert response is not None
    assert response.status_code == 500
    assert response.data["code"] == "api_error"
    assert secret not in str(response.data)
