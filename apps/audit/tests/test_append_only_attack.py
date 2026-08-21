"""D3-F-7 — audit trail is append-only (attack surface).

Two layers of defense, both adversarially asserted:

  API layer: PUT / PATCH / DELETE / POST on `/api/v1/audit/{id}/` (and the
  collection) return **405** — even for a director AND a superuser. The viewset
  is read-only (`http_method_names = ["get","head","options"]`), so there is no
  mutation path to the immutable model.

  ORM layer: a grep-style source scan asserts no application code mutates
  AuditLog rows — no `AuditLog.objects...update(`, and `.delete(` only in the
  retention task (`celery_tasks/audit_tasks.py`), which deletes by AGE, never
  edits. (The model has no `updated_at` and no update path by construction.)

Coordinated names with Lane D's own 405 test (D3-D acceptance) — this is the
adversarial duplicate from the attacker's seat (director + superuser).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django_tenants.utils import schema_context

from apps.audit.tests.factories import AuditLogFactory

pytestmark = pytest.mark.django_db

DETAIL_URL = "/api/v1/audit/{}/"
LIST_URL = "/api/v1/audit/"


def _seed_one(tenant):
    with schema_context(tenant.schema_name):
        return AuditLogFactory().id


# --------------------------------------------------------------------------- #
# API layer: 405 on every mutating verb, as director AND superuser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_detail_mutations_405_as_director(tenant_a, user_in, as_user, method):
    row_id = _seed_one(tenant_a)
    director = user_in(tenant_a, roles=["director"])
    client = as_user(tenant_a, director)
    resp = getattr(client, method)(DETAIL_URL.format(row_id), {}, format="json")
    assert resp.status_code == 405


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_detail_mutations_405_as_superuser(tenant_a, client_for, method):
    row_id = _seed_one(tenant_a)
    from apps.auth.services import issue_token
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        su = UserFactory(is_superuser=True, is_staff=True)
        access = issue_token(su)["access"]
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = getattr(client, method)(DETAIL_URL.format(row_id), {}, format="json")
    assert resp.status_code == 405


def test_collection_post_405_as_director(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    resp = as_user(tenant_a, director).post(
        LIST_URL, {"action": "create", "resource_type": "x"}, format="json"
    )
    assert resp.status_code == 405


def test_collection_post_405_as_superuser(tenant_a, client_for):
    from apps.auth.services import issue_token
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        su = UserFactory(is_superuser=True, is_staff=True)
        access = issue_token(su)["access"]
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = client.post(LIST_URL, {"action": "create"}, format="json")
    assert resp.status_code == 405


def test_get_still_works_for_director(tenant_a, user_in, as_user):
    """Control: the read path is open to audit:read (director)."""
    _seed_one(tenant_a)
    director = user_in(tenant_a, roles=["director"])
    resp = as_user(tenant_a, director).get(LIST_URL)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# ORM layer: grep-style source scan — no app code edits/deletes AuditLog
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_DIRS = ["apps", "core"]  # application code (NOT celery_tasks — see below)
# The ONE sanctioned deleter: the retention task removes rows by AGE only.
_ALLOWED_DELETE_FILE = _REPO_ROOT / "celery_tasks" / "audit_tasks.py"

# Match `AuditLog.objects ... .update(` / `.delete(` possibly across whitespace
# (the queryset may be split across lines), but on the same statement.
_UPDATE_RE = re.compile(r"AuditLog\.objects[\s\S]{0,200}?\.update\(")
_DELETE_RE = re.compile(r"AuditLog\.objects[\s\S]{0,200}?\.delete\(")


def _python_files(*dirs):
    for d in dirs:
        base = _REPO_ROOT / d
        for path in base.rglob("*.py"):
            if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                continue
            yield path


def test_no_app_code_updates_auditlog():
    offenders = []
    for path in _python_files(*_SCAN_DIRS):
        text = path.read_text(encoding="utf-8")
        if _UPDATE_RE.search(text):
            offenders.append(path.relative_to(_REPO_ROOT).as_posix())
    assert not offenders, f"AuditLog must never be updated; found .update( in: {offenders}"


def test_no_app_code_deletes_auditlog_except_retention():
    offenders = []
    # Application code (apps/, core/) must NEVER delete AuditLog rows.
    for path in _python_files(*_SCAN_DIRS):
        text = path.read_text(encoding="utf-8")
        if _DELETE_RE.search(text):
            offenders.append(path.relative_to(_REPO_ROOT).as_posix())
    assert not offenders, f"AuditLog delete only allowed in the retention task; found in: {offenders}"


def test_celery_tasks_only_delete_in_retention_task():
    """Inside celery_tasks/, the only file allowed to delete AuditLog is the
    retention task (and it deletes by created_at age, never .update)."""
    base = _REPO_ROOT / "celery_tasks"
    for path in base.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _UPDATE_RE.search(text):
            pytest.fail(f"AuditLog.update() found in a celery task: {path}")
        if _DELETE_RE.search(text) and path != _ALLOWED_DELETE_FILE:
            pytest.fail(f"AuditLog.delete() found outside the retention task: {path}")
    # And the retention task deletes strictly by age (created_at filter present).
    retention = _ALLOWED_DELETE_FILE.read_text(encoding="utf-8")
    assert "created_at__lt" in retention, "retention task must delete by created_at age, not arbitrarily"
