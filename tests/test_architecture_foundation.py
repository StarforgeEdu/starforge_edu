"""Clean-architecture foundation: DI container, response envelopes, base repository,
and the domain-error -> JSON mapping that the layered (non-DRF) view style relies on."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

import pytest
from django_tenants.utils import schema_context

from core.container import Container
from core.repositories import BaseRepository
from core.responses import created, error, paginated, success, validation_error

pytestmark = pytest.mark.django_db


# --- DI container ----------------------------------------------------------
class _IGreeter(ABC):
    @abstractmethod
    def greet(self) -> str: ...


class _Greeter(_IGreeter):
    def greet(self) -> str:
        return "hi"


class _Service:
    def __init__(self, greeter: _IGreeter):
        self.greeter = greeter


def test_container_binds_interface_and_autowires_dependencies():
    c = Container()
    c.register(_IGreeter, _Greeter)
    # The service's _IGreeter constructor dependency is injected automatically.
    svc = c.resolve(_Service)
    assert isinstance(svc.greeter, _Greeter)
    assert svc.greeter.greet() == "hi"


def test_container_caches_singletons_and_rejects_unbound_abstract():
    c = Container()
    c.register(_IGreeter, _Greeter)
    assert c.resolve(_IGreeter) is c.resolve(_IGreeter)  # same instance (singleton)
    with pytest.raises(LookupError):
        Container().resolve(_IGreeter)  # no binding for the abstract port


def test_container_rejects_conflicting_registration_but_allows_idempotent_ready_hook():
    c = Container()
    c.register(_IGreeter, _Greeter)
    c.register(_IGreeter, _Greeter)

    class _OtherGreeter(_IGreeter):
        def greet(self) -> str:
            return "other"

    with pytest.raises(RuntimeError, match="different binding"):
        c.register(_IGreeter, _OtherGreeter)


def test_container_constructs_singleton_once_under_concurrent_first_use():
    import threading
    import time

    constructed = 0
    constructed_lock = threading.Lock()

    class _SlowGreeter(_IGreeter):
        def __init__(self) -> None:
            nonlocal constructed
            time.sleep(0.01)
            with constructed_lock:
                constructed += 1

        def greet(self) -> str:
            return "hello"

    c = Container().register(_IGreeter, _SlowGreeter)
    resolved: list[_IGreeter] = []
    threads = [threading.Thread(target=lambda: resolved.append(c.resolve(_IGreeter))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert constructed == 1
    assert len({id(instance) for instance in resolved}) == 1


# --- response envelopes ----------------------------------------------------
def test_response_envelopes_have_the_standard_shape():
    s = json.loads(success({"x": 1}, message="ok").content)
    assert s == {"success": True, "message": "ok", "data": {"x": 1}}

    assert created().status_code == 201
    assert json.loads(error("nope", code="bad", status=409).content) == {
        "success": False,
        "code": "bad",
        "message": "nope",
    }
    ve = validation_error({"name": ["required"]})
    assert ve.status_code == 422
    assert json.loads(ve.content)["code"] == "validation_error"

    pg = paginated([1, 2], total=5, page=1, page_size=2)
    body = json.loads(pg.content)
    assert body["data"] == [1, 2]
    assert body["pagination"] == {
        "total": 5,
        "page": 1,
        "page_size": 2,
        "pages": 3,
        "has_next": True,
        "has_prev": False,
    }


# --- base repository -------------------------------------------------------
def test_base_repository_crud_runs_through_the_orm(tenant_a):
    from datetime import timedelta

    from django.utils import timezone

    from apps.teachers.models import TeacherType

    class _TeacherTypeRepo(BaseRepository[TeacherType]):
        model = TeacherType

    repo = _TeacherTypeRepo()
    with schema_context(tenant_a.schema_name):
        teacher_type = repo.create(name="North", slug="north-x")
        assert repo.get_by_id(teacher_type.pk).name == "North"
        assert repo.exists(slug="north-x") is True
        assert repo.count(slug="north-x") == 1
        stale = timezone.now() - timedelta(days=1)
        TeacherType.objects.filter(pk=teacher_type.pk).update(updated_at=stale)
        teacher_type.refresh_from_db()
        repo.update(teacher_type, name="South")
        updated = repo.get_by_id(teacher_type.pk)
        assert updated.name == "South"
        assert updated.updated_at > stale
        again, made = repo.get_or_create(slug="north-x", defaults={"name": "South"})
        assert made is False
        assert again.pk == teacher_type.pk
        repo.delete(teacher_type)
        assert repo.get_by_id(teacher_type.pk) is None


# --- domain error -> JSON (plain-view path) --------------------------------
def test_domain_error_renders_as_json_envelope():
    from core.exceptions import ValidationException
    from core.middleware import JsonErrorResponseMiddleware

    mw = JsonErrorResponseMiddleware(lambda request: None)  # type: ignore[arg-type,return-value]
    resp = mw.process_exception(None, ValidationException("bad input", code="weak_password"))  # type: ignore[arg-type]
    assert resp is not None
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert body["success"] is False
    assert body["code"] == "weak_password"
    assert body["message"] == "bad input"
    # A non-domain exception is left for Django/DEBUG to handle (returns None).
    assert mw.process_exception(None, ValueError("x")) is None  # type: ignore[arg-type]
