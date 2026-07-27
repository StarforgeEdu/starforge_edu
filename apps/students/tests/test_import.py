"""CSV import (D1-LD-5): partial errors, Excel BOM, encoding and size guards."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django_tenants.utils import schema_context

from apps.org.models import CenterSettings
from apps.org.tests.factories import BranchFactory
from apps.students.models import StudentProfile
from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/students/import/"


def _csv_with_two_bad_rows() -> str:
    rows = ["phone,first_name,last_name"]
    rows += [f"+99890555600{i},Student{i},Imported" for i in range(1, 9)]  # 8 good
    rows.append(",NoIdentifier,Bad")  # no phone and no email
    rows.append("not-a-phone,Garbled,Bad")  # unparseable phone
    return "\n".join(rows) + "\n"


@pytest.fixture
def registrar(tenant_a, user_in, as_user):
    # A registrar SCOPED to the target branch — import now enforces the same
    # create-scope as the single-student POST (a branch-scoped role must import
    # only into its own branches).
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    return client, branch


def _post_csv(client, branch, payload: bytes):
    upload = SimpleUploadedFile("students.csv", payload, content_type="text/csv")
    return client.post(URL, {"file": upload, "branch": branch.id}, format="multipart")


def test_csv_import_partial_errors(registrar, tenant_a):
    client, branch = registrar
    resp = _post_csv(client, branch, _csv_with_two_bad_rows().encode())
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["created"] == 8
    assert [e["row"] for e in body["errors"]] == [9, 10]
    with schema_context(tenant_a.schema_name):
        assert StudentProfile.objects.filter(branch=branch).count() == 8

    # Idempotent re-run: existing phones are skipped (as row errors), no dupes.
    resp = _post_csv(client, branch, _csv_with_two_bad_rows().encode())
    assert resp.status_code == 201
    assert resp.json()["data"]["created"] == 0
    with schema_context(tenant_a.schema_name):
        assert StudentProfile.objects.filter(branch=branch).count() == 8


def test_csv_import_handles_excel_bom(registrar, tenant_a):
    """Excel's 'CSV UTF-8' writes a BOM; the first header must not become
    '﻿phone' (which used to fail every row with identifier_required)."""
    client, branch = registrar
    payload = "phone,first_name\n+998905556101,Bilol\n".encode("utf-8-sig")
    resp = _post_csv(client, branch, payload)
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body == {"created": 1, "errors": []}


def test_csv_import_non_utf8_400(registrar):
    client, branch = registrar
    payload = "phone,first_name\n+998905556201,Фёдор\n".encode("cp1251")
    resp = _post_csv(client, branch, payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_encoding"


def test_csv_import_over_size_cap_400(registrar, tenant_a):
    client, branch = registrar
    with schema_context(tenant_a.schema_name):
        settings_obj = CenterSettings.load()
        settings_obj.max_upload_mb = 1
        settings_obj.save()
    payload = b"phone\n" + b"x" * (1024 * 1024)  # > 1 MB
    resp = _post_csv(client, branch, payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "file_too_large"
