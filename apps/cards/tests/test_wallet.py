"""F12-1 — stored-value wallet: a cashier/reception (wallet:write) loads money onto a
student's wallet and charges it (canteen); the balance is the running total of an
append-only transaction ledger, mutated under a lock so it can't overdraw; a student
reads their OWN wallet (never a classmate's)."""

from __future__ import annotations

from itertools import count

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role
from tests.role_principal_helpers import ensure_role_principal, exact_session_client

pytestmark = pytest.mark.django_db

ME = "/api/v1/cards/wallets/me/"
_KEYS = count(1)


def _key(prefix: str = "wallet-test") -> str:
    return f"{prefix}-{next(_KEYS):08d}"


def _setup(tenant, user_in, client_for):
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    student_user = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    cashier_user = user_in(tenant, roles=[Role.CASHIER], branch=branch)
    teacher_user = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant.schema_name):
        student = StudentProfileFactory.create(
            user=student_user, branch=branch, status=StudentProfile.Status.ACTIVE
        )
        cashier_principal = ensure_role_principal(
            cashier_user,
            roles=[Role.CASHIER],
            branch=branch,
        )
        teacher_principal = ensure_role_principal(
            teacher_user,
            roles=[Role.TEACHER],
            branch=branch,
        )
    return {
        "branch": branch,
        "student": student,
        "cashier_user": cashier_user,
        "cashier": exact_session_client(
            client_for,
            tenant,
            cashier_user,
            principal_kind="staff",
            principal_id=cashier_principal.pk,
        ),
        "teacher": exact_session_client(
            client_for,
            tenant,
            teacher_user,
            principal_kind="teacher",
            principal_id=teacher_principal.pk,
        ),
        "student_c": exact_session_client(
            client_for,
            tenant,
            student_user,
            principal_kind="student",
            principal_id=student.pk,
        ),
    }


def _topup(s, amount, **over):
    sid = over.pop("sid", s["student"].id)
    key = over.pop("key", _key("wallet-topup"))
    body = {"amount": str(amount), **over}
    return s["cashier"].post(
        f"/api/v1/cards/wallets/{sid}/topup/",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def _spend(s, amount, **over):
    sid = over.pop("sid", s["student"].id)
    key = over.pop("key", _key("wallet-spend"))
    body = {"amount": str(amount), **over}
    return s["cashier"].post(
        f"/api/v1/cards/wallets/{sid}/spend/",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def test_top_up_credits_the_wallet(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    r = _topup(s, "50000")
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["kind"] == "topup"
    assert body["amount_uzs"] == "50000.00"
    assert body["balance_after_uzs"] == "50000.00"


def test_spend_debits_the_wallet(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    _topup(s, "50000")
    r = _spend(s, "12000")
    assert r.status_code == 201, r.content
    assert r.json()["data"]["kind"] == "spend"
    assert r.json()["data"]["balance_after_uzs"] == "38000.00"


def test_cannot_overdraw(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    _topup(s, "5000")
    r = _spend(s, "9000")
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_funds"


def test_balance_is_the_running_ledger_total(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    _topup(s, "100000")
    _spend(s, "30000")
    _topup(s, "5000")
    detail = s["cashier"].get(f"/api/v1/cards/wallets/{s['student'].id}/").json()["data"]
    assert detail["wallet"]["balance_uzs"] == "75000.00"
    assert len(detail["transactions"]) == 3  # append-only ledger


def test_student_reads_their_own_wallet(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    _topup(s, "20000")
    body = s["student_c"].get(ME).json()["data"]
    assert body["wallet"]["balance_uzs"] == "20000.00"
    assert body["wallet"]["student"] == s["student"].id


def test_get_and_head_never_provision_missing_staff_or_self_wallets(tenant_a, user_in, client_for):
    """Reads are observational; only a top-up/spend/refund write may provision."""
    from apps.cards.models import Wallet

    s = _setup(tenant_a, user_in, client_for)
    staff_url = f"/api/v1/cards/wallets/{s['student'].id}/"

    with schema_context(tenant_a.schema_name):
        assert Wallet.objects.count() == 0

    staff_get = s["cashier"].get(staff_url)
    staff_head = s["cashier"].head(staff_url)
    self_get = s["student_c"].get(ME)
    self_head = s["student_c"].head(ME)

    for response in (staff_get, staff_head, self_get, self_head):
        assert response.status_code == 200, response.content
    for response in (staff_get, self_get):
        assert response.json()["data"] == {"wallet": None, "transactions": []}

    with schema_context(tenant_a.schema_name):
        assert Wallet.objects.count() == 0


def test_a_student_cannot_read_another_students_wallet(tenant_a, user_in, client_for):
    """A student has no wallet:read, so they can't pull a classmate's wallet by id (the
    /me/ self route is the only one open to them)."""
    s = _setup(tenant_a, user_in, client_for)
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        other = StudentProfileFactory.create(branch=s["branch"])
    assert s["student_c"].get(f"/api/v1/cards/wallets/{other.id}/").status_code == 403


def test_top_up_is_branch_scoped(tenant_a, user_in, client_for):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, client_for)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        outsider = StudentProfileFactory.create(branch=other_branch)
    r = _topup(s, "1000", sid=outsider.id)
    assert r.status_code == 404
    assert r.json()["code"] == "student_not_found"


def test_a_role_without_wallet_write_cannot_top_up(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    r = s["teacher"].post(
        f"/api/v1/cards/wallets/{s['student'].id}/topup/", {"amount": "1000"}, format="json"
    )
    assert r.status_code == 403


def test_a_non_positive_amount_is_rejected(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    r = _topup(s, "0")
    assert r.status_code == 400  # serializer min_value


def test_a_topup_that_would_overflow_the_balance_is_a_clean_422(tenant_a, user_in, client_for):
    """A single amount fits NUMERIC(18,2), but the CUMULATIVE balance must too — an
    overflowing total is a clean 422, never a DB-overflow 500."""
    s = _setup(tenant_a, user_in, client_for)
    big = "9000000000000000"  # 16 digits, < 1e16: passes per-amount validation
    assert _topup(s, big).status_code == 201
    r = _topup(s, big)  # would push the balance to 1.8e16 -> overflow the column
    assert r.status_code == 422
    assert r.json()["code"] == "balance_overflow"


def test_refund_credits_wallet_with_an_explicit_ledger_kind(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    response = s["cashier"].post(
        f"/api/v1/cards/wallets/{s['student'].id}/refund/",
        {"amount": "2500", "note": "Reversed canteen sale"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=_key("wallet-refund"),
    )
    assert response.status_code == 201, response.content
    assert response.json()["data"]["kind"] == "refund"
    assert response.json()["data"]["balance_after_uzs"] == "2500.00"
    assert response.json()["data"]["note"] == "Reversed canteen sale"


def test_exact_wallet_retry_returns_one_transaction_and_hashes_the_key(tenant_a, user_in, client_for):
    from apps.cards.models import Wallet, WalletTransaction

    s = _setup(tenant_a, user_in, client_for)
    key = _key("wallet-exact-retry")
    first = _topup(s, "12500", key=key, note="Lunch credit")
    replay = _topup(s, "12500.00", key=key, note="Lunch credit")

    assert first.status_code == 201, first.content
    assert replay.status_code == 201, replay.content
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        transaction = WalletTransaction.objects.get()
        assert WalletTransaction.objects.count() == 1
        assert Wallet.objects.get(student=s["student"]).balance_uzs == 12500
        assert transaction.idempotency_key_hash != key
        assert len(transaction.idempotency_key_hash or "") == 64
        assert transaction.operation_fingerprint
        assert transaction.actor_principal_kind == "staff"
        assert transaction.actor_principal_id is not None


def test_wallet_key_reuse_rejects_changed_body_action_and_student(tenant_a, user_in, client_for):
    from apps.cards.models import WalletTransaction
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, client_for)
    with schema_context(tenant_a.schema_name):
        other = StudentProfileFactory.create(branch=s["branch"])
    key = _key("wallet-conflict")
    first = _topup(s, "10000", key=key, note="Original")
    changed_amount = _topup(s, "10001", key=key, note="Original")
    changed_note = _topup(s, "10000", key=key, note="Changed")
    changed_action = _spend(s, "10000", key=key, note="Original")
    changed_student = _topup(s, "10000", key=key, note="Original", sid=other.pk)

    assert first.status_code == 201, first.content
    for response in (changed_amount, changed_note, changed_action, changed_student):
        assert response.status_code == 409, response.content
        assert response.json()["code"] == "idempotency_key_reused"
    with schema_context(tenant_a.schema_name):
        assert WalletTransaction.objects.count() == 1
        assert WalletTransaction.objects.get().balance_after_uzs == 10000


def test_wallet_replay_rechecks_current_branch_scope(tenant_a, user_in, client_for):
    from apps.cards.models import WalletTransaction
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile

    s = _setup(tenant_a, user_in, client_for)
    key = _key("wallet-scope-recheck")
    first = _topup(s, "3000", key=key)
    assert first.status_code == 201, first.content

    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        StudentProfile.objects.filter(pk=s["student"].pk).update(branch=other_branch)
    replay = _topup(s, "3000", key=key)

    assert replay.status_code == 404, replay.content
    assert replay.json()["code"] == "student_not_found"
    with schema_context(tenant_a.schema_name):
        assert WalletTransaction.objects.count() == 1


def test_wallet_write_rechecks_scope_after_student_lookup(
    tenant_a, user_in, client_for, monkeypatch
):
    """A transfer racing the fast view lookup cannot authorize a stale-branch write."""

    from apps.cards.models import Wallet, WalletTransaction
    from apps.cards.services.v1.card_service import WalletService
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile

    s = _setup(tenant_a, user_in, client_for)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
    original = WalletService.get_student_in_scope

    def transfer_after_lookup(service, **kwargs):
        student = original(service, **kwargs)
        StudentProfile.objects.filter(pk=student.pk).update(branch=other_branch)
        return student

    monkeypatch.setattr(WalletService, "get_student_in_scope", transfer_after_lookup)
    response = _topup(s, "4500", key=_key("wallet-transfer-race"))

    assert response.status_code == 404, response.content
    assert response.json()["code"] == "student_not_found"
    with schema_context(tenant_a.schema_name):
        assert Wallet.objects.count() == 0
        assert WalletTransaction.objects.count() == 0


def test_wallet_key_namespace_is_role_principal_scoped(tenant_a, user_in, client_for):
    from apps.cards.models import WalletTransaction

    s = _setup(tenant_a, user_in, client_for)
    second_user = user_in(tenant_a, roles=[Role.CASHIER], branch=s["branch"])
    with schema_context(tenant_a.schema_name):
        second_principal = ensure_role_principal(
            second_user,
            roles=[Role.CASHIER],
            branch=s["branch"],
        )
    second = exact_session_client(
        client_for,
        tenant_a,
        second_user,
        principal_kind="staff",
        principal_id=second_principal.pk,
    )
    key = _key("wallet-principal-scope")
    first = _topup(s, "1000", key=key)
    other = second.post(
        f"/api/v1/cards/wallets/{s['student'].pk}/topup/",
        {"amount": "1000"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )

    assert first.status_code == 201, first.content
    assert other.status_code == 201, other.content
    assert other.json()["data"]["id"] != first.json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        assert WalletTransaction.objects.count() == 2
        assert len(set(WalletTransaction.objects.values_list("idempotency_key_hash", flat=True))) == 2


def test_wallet_mutation_requires_canonical_key_and_closed_body(tenant_a, user_in, client_for):
    from apps.cards.models import Wallet, WalletTransaction

    s = _setup(tenant_a, user_in, client_for)
    url = f"/api/v1/cards/wallets/{s['student'].pk}/topup/"
    missing = s["cashier"].post(url, {"amount": "1000"}, format="json")
    short = s["cashier"].post(
        url,
        {"amount": "1000"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="too-short",
    )
    unknown = s["cashier"].post(
        url,
        {"amount": "1000", "student": s["student"].pk},
        format="json",
        HTTP_IDEMPOTENCY_KEY=_key("wallet-unknown-field"),
    )

    for response in (missing, short):
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "invalid_idempotency_key"
        assert set(response.json()["errors"]) == {"Idempotency-Key"}
    assert unknown.status_code == 400, unknown.content
    assert unknown.json()["code"] == "validation_error"
    assert unknown.json()["errors"] == {"student": ["Unknown field."]}
    with schema_context(tenant_a.schema_name):
        assert not Wallet.objects.exists()
        assert not WalletTransaction.objects.exists()
