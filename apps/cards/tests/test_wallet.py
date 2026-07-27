"""F12-1 — stored-value wallet: a cashier/reception (wallet:write) loads money onto a
student's wallet and charges it (canteen); the balance is the running total of an
append-only transaction ledger, mutated under a lock so it can't overdraw; a student
reads their OWN wallet (never a classmate's)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ME = "/api/v1/cards/wallets/me/"


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        student = StudentProfileFactory.create(
            user=student_user, branch=branch, status=StudentProfile.Status.ACTIVE
        )
    return {
        "branch": branch,
        "student": student,
        "cashier": as_user(tenant, user_in(tenant, roles=[Role.CASHIER], branch=branch)),
        "teacher": as_user(tenant, user_in(tenant, roles=[Role.TEACHER], branch=branch)),
        "student_c": as_user(tenant, student_user),
    }


def _topup(s, amount, **over):
    sid = over.pop("sid", s["student"].id)
    return s["cashier"].post(f"/api/v1/cards/wallets/{sid}/topup/", {"amount": str(amount)}, format="json")


def _spend(s, amount, **over):
    sid = over.pop("sid", s["student"].id)
    return s["cashier"].post(f"/api/v1/cards/wallets/{sid}/spend/", {"amount": str(amount)}, format="json")


def test_top_up_credits_the_wallet(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _topup(s, "50000")
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["kind"] == "topup"
    assert body["amount_uzs"] == "50000.00"
    assert body["balance_after_uzs"] == "50000.00"


def test_spend_debits_the_wallet(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _topup(s, "50000")
    r = _spend(s, "12000")
    assert r.status_code == 201, r.content
    assert r.json()["data"]["kind"] == "spend"
    assert r.json()["data"]["balance_after_uzs"] == "38000.00"


def test_cannot_overdraw(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _topup(s, "5000")
    r = _spend(s, "9000")
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_funds"


def test_balance_is_the_running_ledger_total(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _topup(s, "100000")
    _spend(s, "30000")
    _topup(s, "5000")
    detail = s["cashier"].get(f"/api/v1/cards/wallets/{s['student'].id}/").json()["data"]
    assert detail["wallet"]["balance_uzs"] == "75000.00"
    assert len(detail["transactions"]) == 3  # append-only ledger


def test_student_reads_their_own_wallet(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _topup(s, "20000")
    body = s["student_c"].get(ME).json()["data"]
    assert body["wallet"]["balance_uzs"] == "20000.00"
    assert body["wallet"]["student"] == s["student"].id


def test_a_student_cannot_read_another_students_wallet(tenant_a, user_in, as_user):
    """A student has no wallet:read, so they can't pull a classmate's wallet by id (the
    /me/ self route is the only one open to them)."""
    s = _setup(tenant_a, user_in, as_user)
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        other = StudentProfileFactory.create(branch=s["branch"])
    assert s["student_c"].get(f"/api/v1/cards/wallets/{other.id}/").status_code == 403


def test_top_up_is_branch_scoped(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        outsider = StudentProfileFactory.create(branch=other_branch)
    r = _topup(s, "1000", sid=outsider.id)
    assert r.status_code == 403
    assert r.json()["code"] == "branch_out_of_scope"


def test_a_role_without_wallet_write_cannot_top_up(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["teacher"].post(
        f"/api/v1/cards/wallets/{s['student'].id}/topup/", {"amount": "1000"}, format="json"
    )
    assert r.status_code == 403


def test_a_non_positive_amount_is_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _topup(s, "0")
    assert r.status_code == 400  # serializer min_value


def test_a_topup_that_would_overflow_the_balance_is_a_clean_422(tenant_a, user_in, as_user):
    """A single amount fits NUMERIC(18,2), but the CUMULATIVE balance must too — an
    overflowing total is a clean 422, never a DB-overflow 500."""
    s = _setup(tenant_a, user_in, as_user)
    big = "9000000000000000"  # 16 digits, < 1e16: passes per-amount validation
    assert _topup(s, big).status_code == 201
    r = _topup(s, big)  # would push the balance to 1.8e16 -> overflow the column
    assert r.status_code == 422
    assert r.json()["code"] == "balance_overflow"
