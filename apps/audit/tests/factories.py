"""Audit-domain factory (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.audit.models import AuditLog


class AuditLogFactory(factory.django.DjangoModelFactory[AuditLog]):
    class Meta:
        model = AuditLog

    actor = None
    actor_repr = "system"
    action = AuditLog.Action.CREATE
    resource_type = "users.User"
    resource_id = factory.Sequence(lambda n: str(n + 1))
    before = None
    after = factory.LazyFunction(dict)
    ip = "127.0.0.1"
    user_agent = "pytest"
