"""Focused contracts for private intelligence read caching."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from django.http import HttpResponse
from django.test import override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.intelligence.cache import (
    CachePolicy,
    CacheResult,
    apply_cache_headers,
    get_or_compute,
    intelligence_cache_key,
)
from core.permissions import EffectivePermissionScope
from core.role_principals import RolePrincipal


@dataclass
class FakeCache:
    values: dict[str, Any] = field(default_factory=dict)
    lock_acquired: bool = True
    fail_on: str | None = None
    get_values: list[Any] = field(default_factory=list)
    get_calls: int = 0
    add_calls: int = 0
    set_calls: int = 0

    def get(self, key: str, default: Any = None) -> Any:
        self.get_calls += 1
        if self.fail_on == "get":
            raise ConnectionError("cache unavailable")
        if self.get_values:
            return self.get_values.pop(0)
        return self.values.get(key, default)

    def add(self, key: str, value: Any, timeout: int | None = None) -> bool:
        del value, timeout
        self.add_calls += 1
        if self.fail_on == "add":
            raise ConnectionError("cache unavailable")
        if not self.lock_acquired or key in self.values:
            return False
        self.values[key] = 1
        return True

    def set(self, key: str, value: Any, timeout: int | None = None) -> None:
        del timeout
        self.set_calls += 1
        if self.fail_on == "set":
            raise ConnectionError("cache unavailable")
        self.values[key] = value


def _entry(payload: dict[str, Any], *, stored_at: float) -> dict[str, Any]:
    return {"version": 1, "stored_at": stored_at, "payload": payload}


def _policy() -> CachePolicy:
    return CachePolicy(fresh_seconds=120, stale_seconds=600, lock_seconds=30)


def _logger() -> logging.Logger:
    return logging.getLogger("test.intelligence.cache")


def test_cold_load_is_cached_and_fresh_hit_skips_loader() -> None:
    backend = FakeCache()
    calls = 0

    def load() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    cold = get_or_compute(
        backend=backend,
        key="private-key",
        policy=_policy(),
        loader=load,
        logger=_logger(),
        clock=lambda: 100.0,
    )
    hit = get_or_compute(
        backend=backend,
        key="private-key",
        policy=_policy(),
        loader=load,
        logger=_logger(),
        clock=lambda: 110.0,
    )

    assert cold.status == "miss"
    assert hit.status == "hit"
    assert hit.age_seconds == 10
    assert hit.payload == {"value": 1}
    assert calls == 1
    assert backend.set_calls == 1


def test_stale_loser_returns_immediately_without_sql_or_sleep() -> None:
    backend = FakeCache(
        values={"private-key": _entry({"value": "old"}, stored_at=100.0)},
        lock_acquired=False,
    )

    result = get_or_compute(
        backend=backend,
        key="private-key",
        policy=_policy(),
        loader=lambda: pytest.fail("stale loser must not call SQL"),
        logger=_logger(),
        clock=lambda: 250.0,
        sleeper=lambda _seconds: pytest.fail("stale loser must not wait"),
    )

    assert result.status == "stale"
    assert result.freshness == "stale"
    assert result.age_seconds == 150
    assert result.payload == {"value": "old"}


def test_stale_lock_winner_refreshes_and_retains_stale_horizon() -> None:
    backend = FakeCache(values={"private-key": _entry({"value": "old"}, stored_at=100.0)})

    result = get_or_compute(
        backend=backend,
        key="private-key",
        policy=_policy(),
        loader=lambda: {"value": "new"},
        logger=_logger(),
        clock=lambda: 250.0,
    )

    assert result.status == "refresh"
    assert result.payload == {"value": "new"}
    assert backend.values["private-key"] == _entry(
        {"value": "new"},
        stored_at=250.0,
    )


def test_cold_loser_polls_briefly_and_reuses_winner_payload() -> None:
    winner = _entry({"value": "winner"}, stored_at=100.0)
    backend = FakeCache(
        lock_acquired=False,
        get_values=[None, None, winner],
    )
    monotonic_now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return monotonic_now

    def sleep(seconds: float) -> None:
        nonlocal monotonic_now
        sleeps.append(seconds)
        monotonic_now += seconds

    result = get_or_compute(
        backend=backend,
        key="private-key",
        policy=_policy(),
        loader=lambda: pytest.fail("the winner filled the cold key"),
        logger=_logger(),
        clock=lambda: 100.0,
        monotonic=monotonic,
        sleeper=sleep,
        cold_wait_seconds=0.2,
        poll_interval_seconds=0.05,
    )

    assert result.status == "hit"
    assert result.payload == {"value": "winner"}
    assert sleeps == [0.05]


@pytest.mark.parametrize("operation", ["get", "add", "set"])
def test_cache_failures_fall_open_to_authoritative_loader(operation: str) -> None:
    backend = FakeCache(fail_on=operation)
    calls = 0

    def load() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"authoritative": True}

    result = get_or_compute(
        backend=backend,
        key="private-key",
        policy=_policy(),
        loader=load,
        logger=_logger(),
        clock=lambda: 100.0,
    )

    assert result.status == "bypass"
    assert result.payload == {"authoritative": True}
    assert calls == 1


def test_cache_key_isolated_by_every_authorization_and_query_dimension(monkeypatch) -> None:
    context = {
        "tenant": "tenant-a",
        "permissions": ("intelligence:read",),
        "scopes": (
            EffectivePermissionScope(
                branch_id=1,
                department_id=2,
                permissions=("intelligence:read",),
            ),
        ),
    }
    monkeypatch.setattr(
        "apps.intelligence.cache.current_schema",
        lambda: context["tenant"],
    )
    monkeypatch.setattr(
        "apps.intelligence.cache.get_effective_permission_context",
        lambda _request: (context["permissions"], context["scopes"]),
    )
    request = object()
    principal = RolePrincipal(kind="staff", principal_id=11, user_id=7)

    def key(
        *,
        selected_principal: RolePrincipal = principal,
        scope: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> str:
        return intelligence_cache_key(
            request,
            namespace="risk-list",
            principal=selected_principal,
            scope=scope or {"branch": 1},
            query=query or {"page": 1},
        )

    baseline = key()
    variations: list[str] = []
    context["tenant"] = "tenant-b"
    variations.append(key())
    context["tenant"] = "tenant-a"
    variations.append(key(selected_principal=RolePrincipal("staff", 12, 7)))
    context["permissions"] = ("finance:read", "intelligence:read")
    variations.append(key())
    context["permissions"] = ("intelligence:read",)
    context["scopes"] = (EffectivePermissionScope(3, None, ("intelligence:read",)),)
    variations.append(key())
    context["scopes"] = (EffectivePermissionScope(1, 2, ("intelligence:read",)),)
    variations.append(key(scope={"branch": 2}))
    variations.append(key(query={"page": 2}))

    assert all(candidate != baseline for candidate in variations)
    assert len(set(variations)) == len(variations)
    assert "tenant-a" not in baseline


def test_freshness_headers_expose_age_not_private_key() -> None:
    response = HttpResponse()
    result = CacheResult(
        payload={"generated_at": "2026-08-10T12:00:00+00:00"},
        status="stale",
        stored_at=100.0,
        age_seconds=321,
    )

    apply_cache_headers(response, result)

    assert response["X-Cache-Status"] == "stale"
    assert response["X-Data-Freshness"] == "stale"
    assert response["X-Data-Age-Seconds"] == "321"
    assert response["X-Data-Generated-At"] == "2026-08-10T12:00:00+00:00"
    assert "private-key" not in str(response.headers)


@pytest.mark.django_db
@override_settings(
    INTELLIGENCE_RISK_CACHE_FRESH_SECONDS=120,
    INTELLIGENCE_RISK_CACHE_STALE_SECONDS=600,
)
def test_teacher_assignment_removal_cannot_return_cached_risk_rows(
    tenant_a,
    user_in,
    client_for,
) -> None:
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.permissions import Role
    from tests.role_principal_helpers import ensure_role_principal, exact_session_client

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        teacher = ensure_role_principal(
            teacher_user,
            roles=[Role.TEACHER],
            branch=branch,
        )
        cohort = CohortFactory.create(branch=branch, primary_teacher=teacher)
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        exam = ExamFactory.create(cohort=cohort, is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=10)

    client = exact_session_client(client_for, tenant_a, teacher_user)
    first = client.get("/api/v1/intelligence/risk/")
    assert first.status_code == 200, first.content
    assert student.pk in {row["student"] for row in first.json()["data"]["results"]}
    assert "X-Cache-Status" not in first

    with schema_context(tenant_a.schema_name):
        cohort.primary_teacher = None
        cohort.save(update_fields=("primary_teacher", "updated_at"))

    after_removal = client.get("/api/v1/intelligence/risk/")
    assert after_removal.status_code == 200, after_removal.content
    assert student.pk not in {row["student"] for row in after_removal.json()["data"]["results"]}
    assert "X-Cache-Status" not in after_removal


@pytest.mark.django_db
@override_settings(
    INTELLIGENCE_RISK_CACHE_FRESH_SECONDS=120,
    INTELLIGENCE_RISK_CACHE_STALE_SECONDS=600,
)
def test_typed_teacher_assignment_removal_rechecks_list_and_cached_detail_scope(
    tenant_a,
    user_in,
    client_for,
) -> None:
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory, CohortTeacherFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.permissions import Role
    from tests.role_principal_helpers import ensure_role_principal, exact_session_client

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        teacher = ensure_role_principal(
            teacher_user,
            roles=[Role.TEACHER],
            branch=branch,
        )
        cohort = CohortFactory.create(branch=branch, primary_teacher=None)
        assignment = CohortTeacherFactory.create(cohort=cohort, teacher=teacher)
        student = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        exam = ExamFactory.create(cohort=cohort, is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=10)
        lesson_start = timezone.now() - timedelta(days=30)
        Lesson.objects.create(
            term=TermFactory.create(),
            cohort=cohort,
            teacher=teacher,
            title="Historical delivery must not retain current risk access",
            starts_at=lesson_start,
            ends_at=lesson_start + timedelta(hours=1),
            status=Lesson.Status.COMPLETED,
        )

    client = exact_session_client(client_for, tenant_a, teacher_user)
    first_list = client.get("/api/v1/intelligence/risk/")
    first_detail = client.get(f"/api/v1/intelligence/risk/{student.pk}/")
    assert first_list.status_code == 200, first_list.content
    assert first_detail.status_code == 200, first_detail.content
    assert student.pk in {row["student"] for row in first_list.json()["data"]["results"]}
    assert "X-Cache-Status" not in first_list

    with schema_context(tenant_a.schema_name):
        assignment.delete()

    after_list = client.get("/api/v1/intelligence/risk/")
    after_detail = client.get(f"/api/v1/intelligence/risk/{student.pk}/")
    assert after_list.status_code == 200, after_list.content
    assert student.pk not in {row["student"] for row in after_list.json()["data"]["results"]}
    assert "X-Cache-Status" not in after_list
    # Detail authorization runs before cache lookup, so the warmed payload
    # cannot survive loss of the dynamic cohort relationship.
    assert after_detail.status_code == 404, after_detail.content


@pytest.mark.django_db
@override_settings(
    INTELLIGENCE_RISK_CACHE_FRESH_SECONDS=120,
    INTELLIGENCE_RISK_CACHE_STALE_SECONDS=600,
)
def test_scoped_staff_student_transfer_cannot_return_cached_risk_rows(
    tenant_a,
    user_in,
    client_for,
) -> None:
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.permissions import Role
    from tests.role_principal_helpers import ensure_role_principal, exact_session_client

    with schema_context(tenant_a.schema_name):
        source = BranchFactory.create()
        destination = BranchFactory.create()
        source_cohort = CohortFactory.create(branch=source)
        destination_cohort = CohortFactory.create(branch=destination)
        manager_user = user_in(
            tenant_a,
            roles=[Role.HEAD_OF_DEPT],
            branch=source,
        )
        ensure_role_principal(
            manager_user,
            roles=[Role.HEAD_OF_DEPT],
            branch=source,
        )
        student = StudentProfileFactory.create(
            branch=source,
            current_cohort=source_cohort,
        )
        exam = ExamFactory.create(cohort=source_cohort, is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=10)

    client = exact_session_client(client_for, tenant_a, manager_user)
    first = client.get("/api/v1/intelligence/risk/")
    assert first.status_code == 200, first.content
    assert student.pk in {row["student"] for row in first.json()["data"]["results"]}
    assert "X-Cache-Status" not in first

    with schema_context(tenant_a.schema_name):
        student.branch = destination
        student.current_cohort = destination_cohort
        student.save(update_fields=("branch", "current_cohort", "updated_at"))

    after_transfer = client.get("/api/v1/intelligence/risk/")
    assert after_transfer.status_code == 200, after_transfer.content
    assert student.pk not in {row["student"] for row in after_transfer.json()["data"]["results"]}
    assert "X-Cache-Status" not in after_transfer


@pytest.mark.django_db
@override_settings(
    INTELLIGENCE_RISK_CACHE_FRESH_SECONDS=120,
    INTELLIGENCE_RISK_CACHE_STALE_SECONDS=600,
)
def test_organization_wide_staff_risk_list_uses_private_fresh_cache(
    tenant_a,
    user_in,
    client_for,
) -> None:
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.permissions import Role
    from tests.role_principal_helpers import ensure_role_principal, exact_session_client

    with schema_context(tenant_a.schema_name):
        director_user = user_in(tenant_a, roles=[Role.DIRECTOR])
        ensure_role_principal(director_user, roles=[Role.DIRECTOR])
        student = StudentProfileFactory.create()
        exam = ExamFactory.create(is_published=True)
        ExamResultFactory.create(exam=exam, student=student, score=10)

    client = exact_session_client(client_for, tenant_a, director_user)
    cold = client.get("/api/v1/intelligence/risk/")
    hit = client.get("/api/v1/intelligence/risk/")
    head = client.head("/api/v1/intelligence/risk/")

    assert cold.status_code == 200, cold.content
    assert cold["X-Cache-Status"] == "miss"
    assert cold["X-Data-Freshness"] == "fresh"
    assert cold["Cache-Control"] == "private, no-cache, max-age=0, must-revalidate"
    assert hit.status_code == 200, hit.content
    assert hit["X-Cache-Status"] == "hit"
    assert hit.json() == cold.json()
    assert head.status_code == 200
    assert head.content == b""
    assert head["X-Cache-Status"] == "hit"
    assert head["X-Data-Freshness"] == "fresh"


@pytest.mark.parametrize(
    "policy",
    [
        (0, 1, 10),
        (10, 9, 10),
        (10, 20, 0),
    ],
)
def test_invalid_cache_policy_fails_closed(policy: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError, match="seconds"):
        CachePolicy(*policy)
