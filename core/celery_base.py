"""Project Celery task base.

tenant-schemas-celery's ``TenantTask`` activates the tenant schema from the
task **headers** (``headers["_schema_name"]``), set by its ``apply``/``send_task``
overrides + the ``task_prerun`` signal. Throughout the codebase, however,
fan-out dispatchers call ``some_task.delay(..., _schema_name=center.schema_name)``
— which passes ``_schema_name`` as a task **kwarg**, so it leaks into the task
signature (``TypeError`` in eager tests, and the schema is never activated in
production). This base lifts a ``_schema_name`` kwarg into the headers, making
the ergonomic ``.delay(_schema_name=...)`` call style correct everywhere.
"""

from __future__ import annotations

from typing import Any

from tenant_schemas_celery.task import TenantTask


class SchemaHeaderTask(TenantTask):
    """Tenant-aware, at-least-once task contract used by every project task.

    A broker acknowledgement is intentionally delayed until the task body has
    finished.  If the worker child disappears, Celery returns the message to
    the broker rather than acknowledging unfinished work.  Every task in this
    project is therefore required to be idempotent (normally through a durable
    domain row, compare-and-swap transition, or a deterministic dedupe key).

    Keeping these attributes on the actual task base is important: decorator
    options are easy to omit and a setting-only default can be bypassed by a
    task class imported before application finalisation.
    """

    abstract = True
    acks_late = True
    acks_on_failure_or_timeout = True
    reject_on_worker_lost = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # tenant-schemas-celery's task_prerun signal has already activated the
        # header-selected schema before Celery invokes the task object. Verify
        # that activation actually happened: upstream deliberately swallows a
        # missing-tenant lookup, which would otherwise execute tenant work in
        # the public schema when a center is removed after enqueue.
        from django.db import connection

        request_stack = getattr(self, "request_stack", None)
        current_request = request_stack.top if request_stack is not None else None
        expected_schema = self._schema_from_request(current_request)
        if expected_schema:
            self._assert_execution_schema(
                expected=str(expected_schema),
                actual=str(getattr(connection, "schema_name", "") or ""),
            )

        # Apply the same authoritative business timezone as HTTP requests and
        # always restore it before this worker thread accepts another tenant's task.
        from core.timezones import organization_timezone_context

        with organization_timezone_context():
            return super().__call__(*args, **kwargs)

    def apply_async(self, args=None, kwargs=None, **options) -> Any:
        if kwargs and "_schema_name" in kwargs:
            kwargs = dict(kwargs)
            schema = kwargs.pop("_schema_name")
            if schema:
                # Fail loudly at dispatch on an unknown tenant schema. The library's
                # prerun swallows a failed lookup and runs the body in the PUBLIC
                # schema instead — which silently corrupts/misroutes per-tenant work.
                self._assert_schema_resolvable(schema)
                headers = dict(options.get("headers") or {})
                existing_schema = headers.get("_schema_name")
                if existing_schema not in (None, schema):
                    # Never let an opaque Celery option silently override the
                    # explicit tenant routing argument. A conflict is a code or
                    # message-integrity defect, not a reason to pick either tenant.
                    raise ValueError("Conflicting tenant routing headers were refused.")
                headers["_schema_name"] = schema
                options["headers"] = headers
        return super().apply_async(args=args, kwargs=kwargs, **options)

    @staticmethod
    def _assert_schema_resolvable(schema: str) -> None:
        from django_tenants.utils import get_public_schema_name

        if schema == get_public_schema_name():
            return
        try:
            # Center is a SHARED model (public table), readable from any schema via
            # the search_path, so no context switch is needed.
            from apps.tenancy.models import Center

            exists = Center.objects.filter(schema_name=schema).exists()
        except Exception as exc:
            # Tenant routing is an authorization boundary. If the registry is
            # unavailable, allowing dispatch would rely on the upstream
            # library's fail-open prerun behavior and can execute tenant work in
            # the public schema. Retry the dispatch after recovery instead.
            raise RuntimeError("Tenant routing is unavailable; task dispatch was refused.") from exc
        if not exists:
            raise ValueError("Refusing to dispatch a task to an unknown tenant schema.")

    @staticmethod
    def _assert_execution_schema(*, expected: str, actual: str) -> None:
        if not expected or expected != actual:
            raise RuntimeError("Tenant schema activation failed; task execution was refused.")

    @staticmethod
    def _schema_from_request(current_request: Any) -> Any:
        """Mirror tenant-schemas-celery's broker-transport lookup exactly.

        AMQP normally retains custom headers in ``request.headers``. Redis can
        merge them into the task request mapping instead. Looking only at the
        former would skip the execution-time tenant assertion on Redis—the
        production broker used by this project.
        """

        headers = getattr(current_request, "headers", None) or {}
        if "_schema_name" in headers:
            return headers.get("_schema_name")
        getter = getattr(current_request, "get", None)
        return getter("_schema_name") if callable(getter) else None
