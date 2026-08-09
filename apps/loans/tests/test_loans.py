"""F21-1 — staff loans: a `kind="loan"` of the A-1 engine (request → approve →
disburse → ledger), plus repayment tracking and an outstanding balance that has to
reach zero. The decision lives in /approvals/; the loan-specific surface in /loans/."""

from __future__ import annotations

from itertools import count

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role
from tests.role_principal_helpers import ensure_role_principal, exact_session_client

pytestmark = pytest.mark.django_db

LOANS = "/api/v1/loans/"
REQ = "/api/v1/approvals/requests/"
LEDGER = "/api/v1/approvals/ledger/"
_KEYS = count(1)


def _key(prefix: str = "loan-repay-test") -> str:
    return f"{prefix}-{next(_KEYS):08d}"


def _payment_method(tenant) -> int:
    with schema_context(tenant.schema_name):
        from apps.finance.models import PaymentMethod

        return PaymentMethod.objects.create(name="Cash", slug="cash").id


def _same_branch_clients(tenant, user_in, client_for, *roles):
    with schema_context(tenant.schema_name):
        from apps.org.tests.factories import BranchFactory

        branch = BranchFactory()
    users = [user_in(tenant, roles=[role], branch=branch) for role in roles]
    actors = []
    with schema_context(tenant.schema_name):
        for role, user in zip(roles, users, strict=True):
            principal = ensure_role_principal(user, roles=[role], branch=branch)
            actors.append(
                (
                    exact_session_client(
                        client_for,
                        tenant,
                        user,
                        principal_kind=user.test_principal_kind,
                        principal_id=principal.pk,
                    ),
                    user,
                )
            )
    return branch, actors


def _repay(client, loan_id: int, body: dict, *, key: str | None = None):
    return client.post(
        f"{LOANS}{loan_id}/repay/",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or _key(),
    )


def _disbursed_loan(tenant, *, teacher, director, cashier, method_id, amount="1000000.00") -> int:
    """Drive a loan all the way to DISBURSED and return its id."""
    loan = teacher.post(LOANS, {"title": "Advance", "amount_uzs": amount}, format="json")
    assert loan.status_code == 201, loan.content
    body = loan.json()["data"]
    lid = body["id"]
    assert body["status"] == "pending"
    assert body["outstanding_uzs"] is None  # nothing owed until money goes out
    assert director.post(f"{REQ}{lid}/approve/", {"note": "ok"}, format="json").status_code == 200
    dis = cashier.post(f"{REQ}{lid}/disburse/", {"payment_method": method_id}, format="json")
    assert dis.status_code == 200, dis.content
    assert dis.json()["data"]["status"] == "disbursed"
    return lid


def test_loan_lifecycle_request_disburse_repay_settle(tenant_a, user_in, client_for):
    _branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.TEACHER, Role.DIRECTOR, Role.CASHIER
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id)

    # disbursed → fully outstanding
    loan = director.get(f"{LOANS}{lid}/").json()["data"]
    assert loan["outstanding_uzs"] == "1000000.00"
    assert loan["repaid_uzs"] == "0.00"
    assert loan["settled"] is False

    # partial repayment
    r1 = _repay(
        cashier,
        lid,
        {"amount_uzs": "400000.00", "payment_method": method_id},
    )
    assert r1.status_code == 201, r1.content
    assert r1.json()["data"]["outstanding_uzs"] == "600000.00"
    assert r1.json()["data"]["settled"] is False

    # settling repayment
    r2 = _repay(
        cashier,
        lid,
        {"amount_uzs": "600000.00", "payment_method": method_id},
    )
    assert r2.status_code == 201
    assert r2.json()["data"]["outstanding_uzs"] == "0.00"
    assert r2.json()["data"]["settled"] is True

    # a settled loan takes no more money
    over = _repay(
        cashier,
        lid,
        {"amount_uzs": "1.00", "payment_method": method_id},
    )
    assert over.status_code == 422
    assert over.json()["code"] == "loan_already_settled"

    # two repayments recorded, each with its own money-IN ledger row
    assert len(cashier.get(f"{LOANS}{lid}/repayments/").json()["data"]) == 2
    entries = cashier.get(LEDGER).json()["data"]
    assert sum(1 for e in entries if e["entry_type"] == "loan" and e["direction"] == "out") == 1
    ins = [e for e in entries if e["entry_type"] == "loan_repayment" and e["direction"] == "in"]
    assert {e["amount_uzs"] for e in ins} == {"400000.00", "600000.00"}


def test_cannot_repay_before_disbursed(tenant_a, user_in, client_for):
    _branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.TEACHER, Role.DIRECTOR, Role.CASHIER
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)

    lid = teacher.post(LOANS, {"title": "Advance", "amount_uzs": "500000.00"}, format="json").json()["data"][
        "id"
    ]
    # approved but NOT yet disbursed → there is no money out to repay
    director.post(f"{REQ}{lid}/approve/", {}, format="json")
    r = _repay(
        cashier,
        lid,
        {"amount_uzs": "1.00", "payment_method": method_id},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "loan_not_disbursed"


def test_repayment_cannot_exceed_outstanding(tenant_a, user_in, client_for):
    _branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.TEACHER, Role.DIRECTOR, Role.CASHIER
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(
        tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id, amount="1000.00"
    )
    r = _repay(
        cashier,
        lid,
        {"amount_uzs": "1500.00", "payment_method": method_id},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "loan_repayment_exceeds"


def test_loan_request_validates_amount_and_borrower(tenant_a, as_role):
    """The engine-level guards on the loan kind (reachable via the generic queue)."""
    teacher, teacher_user = as_role(Role.TEACHER)
    # no amount → loan_amount_required
    no_amount = teacher.post(
        REQ, {"kind": "loan", "title": "x", "payload": {"borrower_id": teacher_user.id}}, format="json"
    )
    assert no_amount.status_code == 400
    assert no_amount.json()["code"] == "loan_amount_required"
    # bad borrower → loan_borrower_required
    bad_borrower = teacher.post(
        REQ,
        {"kind": "loan", "title": "x", "amount_uzs": "100.00", "payload": {"borrower_id": 999999}},
        format="json",
    )
    assert bad_borrower.status_code == 400
    assert bad_borrower.json()["code"] == "loan_borrower_required"


def test_requester_cannot_approve_own_loan(tenant_a, as_role):
    """Maker-checker: even a director who raised a loan cannot sign it off."""
    director, _ = as_role(Role.DIRECTOR)
    lid = director.post(LOANS, {"title": "Self advance", "amount_uzs": "100.00"}, format="json").json()[
        "data"
    ]["id"]
    r = director.post(f"{REQ}{lid}/approve/", {}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "self_approval"  # /approvals/ is still DRF


def test_borrower_sees_only_own_loans(tenant_a, as_role, user_in, as_user):
    teacher, _ = as_role(Role.TEACHER)
    other = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    teacher.post(LOANS, {"title": "Mine", "amount_uzs": "100.00"}, format="json")

    assert teacher.get(LOANS).json()["pagination"]["total"] == 1  # borrower sees own
    assert other.get(LOANS).json()["pagination"]["total"] == 0  # another borrower sees none of it


def test_same_branch_borrower_cannot_read_colleagues_loan(tenant_a, user_in, client_for):
    branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.TEACHER,
        Role.CASHIER,
    )
    (borrower, _), (colleague, _), (collector, _) = actors
    created = borrower.post(
        LOANS,
        {"title": "Private advance", "amount_uzs": "100.00", "branch": branch.pk},
        format="json",
    )
    assert created.status_code == 201, created.content
    loan_id = created.json()["data"]["id"]

    colleague_list = colleague.get(LOANS)
    assert colleague_list.status_code == 200, colleague_list.content
    assert loan_id not in {row["id"] for row in colleague_list.json()["data"]}
    assert colleague.get(f"{LOANS}{loan_id}/").status_code == 404
    assert colleague.get(f"{LOANS}{loan_id}/repayments/").status_code == 404

    # A genuine collector sees the same branch through its separate collect grant.
    assert loan_id in {row["id"] for row in collector.get(LOANS).json()["data"]}
    assert collector.get(f"{LOANS}{loan_id}/").status_code == 200


def test_scoped_collector_cannot_see_or_repay_unattributed_legacy_loan(
    tenant_a,
    user_in,
    client_for,
):
    """A null historical branch is quarantined, never treated as every branch."""
    from apps.approvals.models import ApprovalRequest
    from apps.approvals.services import KIND_LOAN

    _branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.CASHIER,
    )
    (_teacher, teacher_user), (cashier, _) = actors
    method_id = _payment_method(tenant_a)
    with schema_context(tenant_a.schema_name):
        legacy = ApprovalRequest.objects.create(
            kind=KIND_LOAN,
            branch=None,
            requested_by=teacher_user,
            title="Unattributed legacy advance",
            amount_uzs="1000.00",
            payload={"borrower_id": teacher_user.id, "party_label": teacher_user.username},
            status=ApprovalRequest.Status.DISBURSED,
        )

    listed = cashier.get(LOANS)
    detail = cashier.get(f"{LOANS}{legacy.pk}/")
    repayment = _repay(
        cashier,
        legacy.pk,
        {"amount_uzs": "100.00", "payment_method": method_id},
    )

    assert listed.status_code == 200, listed.content
    assert legacy.pk not in {row["id"] for row in listed.json()["data"]}
    for response in (detail, repayment):
        assert response.status_code == 404, response.content
        assert response.json()["code"] == "not_found"


def test_repay_requires_collect_permission(tenant_a, user_in, client_for):
    _branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.TEACHER, Role.DIRECTOR, Role.CASHIER
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id)
    # the borrowing teacher holds loan:write but NOT loan:collect
    r = _repay(
        teacher,
        lid,
        {"amount_uzs": "1.00", "payment_method": method_id},
    )
    assert r.status_code == 403


def test_manager_raises_loan_for_another_staff_borrower(tenant_a, user_in, client_for, as_user):
    """A manager borrows ON BEHALF of staff B: B (not the keyer) sees the loan, and
    the ledger names B — the borrower — on both the OUT and IN rows (anti-fraud)."""
    branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.REGISTRAR, Role.DIRECTOR, Role.CASHIER
    )
    (manager, _), (director, _), (cashier, _) = actors
    b_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    b_client = as_user(tenant_a, b_user)
    other = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    expected_label = (b_user.get_full_name() or b_user.username)[:200]
    method_id = _payment_method(tenant_a)

    lid = manager.post(
        LOANS, {"title": "Advance for B", "amount_uzs": "1000.00", "borrower": b_user.id}, format="json"
    ).json()["data"]["id"]

    # the named borrower sees the loan (payload__borrower_id scope), so does the
    # keyer; an unrelated teacher sees nothing
    assert any(row["id"] == lid for row in b_client.get(LOANS).json()["data"])
    assert any(row["id"] == lid for row in manager.get(LOANS).json()["data"])
    assert other.get(LOANS).json()["pagination"]["total"] == 0

    director.post(f"{REQ}{lid}/approve/", {}, format="json")
    cashier.post(f"{REQ}{lid}/disburse/", {"payment_method": method_id}, format="json")
    _repay(
        cashier,
        lid,
        {"amount_uzs": "1000.00", "payment_method": method_id},
    )

    entries = cashier.get(LEDGER).json()["data"]
    out = next(e for e in entries if e["entry_type"] == "loan" and e["direction"] == "out")
    inn = next(e for e in entries if e["entry_type"] == "loan_repayment" and e["direction"] == "in")
    assert out["party_label"] == expected_label  # the borrower, not the manager who keyed it
    assert inn["party_label"] == expected_label


def test_borrower_cannot_approve_or_disburse_own_loan(tenant_a, user_in, as_user):
    """Segregation of duties reaches the beneficiary: a borrower can't sign off or pay
    out their own loan, even keyed by a colleague."""
    with schema_context(tenant_a.schema_name):
        from apps.org.tests.factories import BranchFactory

        branch = BranchFactory.create()
    manager = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch),
    )
    borrower_user = user_in(
        tenant_a,
        roles=[Role.DIRECTOR],
        branch=branch,
    )  # holds approve + disburse
    borrower = as_user(tenant_a, borrower_user)
    approver = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch),
    )  # a different director
    method_id = _payment_method(tenant_a)

    lid = manager.post(
        LOANS, {"title": "Advance", "amount_uzs": "100.00", "borrower": borrower_user.id}, format="json"
    ).json()["data"]["id"]
    # the borrower cannot approve their own loan
    bad_approve = borrower.post(f"{REQ}{lid}/approve/", {}, format="json")
    assert bad_approve.status_code == 403
    assert bad_approve.json()["code"] == "loan_self_dealing"
    # someone else approves; the borrower still cannot disburse to themselves
    assert approver.post(f"{REQ}{lid}/approve/", {}, format="json").status_code == 200
    bad_disburse = borrower.post(f"{REQ}{lid}/disburse/", {"payment_method": method_id}, format="json")
    assert bad_disburse.status_code == 403
    assert bad_disburse.json()["code"] == "loan_self_dealing"


def test_loan_borrower_must_be_staff(tenant_a, as_role, user_in):
    """A "staff loan" cannot name a student/parent as borrower."""
    teacher, _ = as_role(Role.TEACHER)
    student = user_in(tenant_a, roles=[Role.STUDENT])
    r = teacher.post(
        REQ,
        {"kind": "loan", "title": "x", "amount_uzs": "100.00", "payload": {"borrower_id": student.id}},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "loan_borrower_required"


def test_repay_with_invalid_payment_method(tenant_a, user_in, client_for):
    _branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.TEACHER, Role.DIRECTOR, Role.CASHIER
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id)
    r = _repay(cashier, lid, {"amount_uzs": "1.00", "payment_method": 999999})
    assert r.status_code == 422
    assert r.json()["code"] == "payment_method_invalid"


def test_second_repayment_cannot_exceed_remaining(tenant_a, user_in, client_for):
    """The exceed check is against the RUNNING outstanding, not the original amount."""
    _branch, actors = _same_branch_clients(
        tenant_a, user_in, client_for, Role.TEACHER, Role.DIRECTOR, Role.CASHIER
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
        amount="1000000.00",
    )
    assert (
        _repay(
            cashier,
            lid,
            {"amount_uzs": "700000.00", "payment_method": method_id},
        ).status_code
        == 201
    )
    # only 300,000 remains — a 400,000 repayment must be rejected
    over = _repay(
        cashier,
        lid,
        {"amount_uzs": "400000.00", "payment_method": method_id},
    )
    assert over.status_code == 422
    assert over.json()["code"] == "loan_repayment_exceeds"


def test_role_without_loan_is_denied(tenant_a, as_role):
    student, _ = as_role(Role.STUDENT)
    assert student.get(LOANS).status_code == 403


def test_repayment_exact_retry_returns_original_snapshot_after_loan_is_settled(
    tenant_a,
    user_in,
    client_for,
):
    from apps.approvals.models import LedgerEntry
    from apps.finance.models import PaymentMethod
    from apps.loans.models import LoanRepayment

    _branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.DIRECTOR,
        Role.CASHIER,
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)
    loan_id = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
    )
    key = _key("loan-exact-retry")
    first = _repay(
        cashier,
        loan_id,
        {"amount_uzs": "400000.00", "payment_method": method_id, "note": "First"},
        key=key,
    )
    assert first.status_code == 201, first.content
    assert first.json()["data"]["outstanding_uzs"] == "600000.00"
    assert (
        _repay(
            cashier,
            loan_id,
            {"amount_uzs": "600000.00", "payment_method": method_id},
        ).status_code
        == 201
    )
    with schema_context(tenant_a.schema_name):
        PaymentMethod.objects.filter(pk=method_id).update(is_active=False)

    replay = _repay(
        cashier,
        loan_id,
        {"amount_uzs": "400000.00", "payment_method": method_id, "note": "First"},
        key=key,
    )
    assert replay.status_code == 201, replay.content
    assert replay.json()["data"] == first.json()["data"]
    with schema_context(tenant_a.schema_name):
        rows = list(LoanRepayment.objects.filter(loan_id=loan_id).order_by("created_at"))
        assert len(rows) == 2
        assert rows[0].idempotency_key_hash != key
        assert len(rows[0].idempotency_key_hash or "") == 64
        assert rows[0].recorded_by_principal_kind == "staff"
        assert rows[0].response_snapshot == first.json()["data"]
        assert rows[0].repaid_after_uzs == 400000
        assert rows[0].outstanding_after_uzs == 600000
        assert (
            LedgerEntry.objects.filter(
                entry_type="loan_repayment",
                source_kind="approval_request",
                source_id=loan_id,
            ).count()
            == 2
        )


def test_repayment_key_reuse_with_changed_body_or_resource_conflicts(
    tenant_a,
    user_in,
    client_for,
):
    from apps.approvals.models import LedgerEntry
    from apps.loans.models import LoanRepayment

    _branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.DIRECTOR,
        Role.CASHIER,
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)
    first_loan = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
        amount="1000.00",
    )
    second_loan = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
        amount="1000.00",
    )
    key = _key("loan-key-reuse")
    assert (
        _repay(
            cashier,
            first_loan,
            {"amount_uzs": "100.00", "payment_method": method_id, "note": "Original"},
            key=key,
        ).status_code
        == 201
    )

    changed_body = _repay(
        cashier,
        first_loan,
        {"amount_uzs": "100.00", "payment_method": method_id, "note": "Changed"},
        key=key,
    )
    changed_resource = _repay(
        cashier,
        second_loan,
        {"amount_uzs": "100.00", "payment_method": method_id, "note": "Original"},
        key=key,
    )
    for response in (changed_body, changed_resource):
        assert response.status_code == 409, response.content
        assert response.json()["code"] == "idempotency_key_reused"
    with schema_context(tenant_a.schema_name):
        assert LoanRepayment.objects.filter(loan_id__in=(first_loan, second_loan)).count() == 1
        assert (
            LedgerEntry.objects.filter(
                entry_type="loan_repayment",
                source_kind="approval_request",
                source_id__in=(first_loan, second_loan),
            ).count()
            == 1
        )


def test_repayment_replay_rechecks_current_collect_branch_scope(tenant_a, user_in, client_for):
    from apps.loans.models import LoanRepayment
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    _branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.DIRECTOR,
        Role.CASHIER,
    )
    (teacher, _), (director, _), (cashier, cashier_user) = actors
    method_id = _payment_method(tenant_a)
    loan_id = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
    )
    key = _key("loan-scope-recheck")
    body = {"amount_uzs": "100.00", "payment_method": method_id}
    assert _repay(cashier, loan_id, body, key=key).status_code == 201
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        RoleMembership.objects.filter(user=cashier_user).update(branch=other_branch)

    replay = _repay(cashier, loan_id, body, key=key)
    assert replay.status_code == 404, replay.content
    with schema_context(tenant_a.schema_name):
        assert LoanRepayment.objects.filter(loan_id=loan_id).count() == 1


def test_repayment_key_namespace_is_exact_role_principal(tenant_a, user_in, client_for):
    from apps.loans.models import LoanRepayment

    branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.DIRECTOR,
        Role.CASHIER,
    )
    (teacher, _), (director, _), (cashier, _) = actors
    second_user = user_in(tenant_a, roles=[Role.CASHIER], branch=branch)
    with schema_context(tenant_a.schema_name):
        second_principal = ensure_role_principal(
            second_user,
            roles=[Role.CASHIER],
            branch=branch,
        )
    second_cashier = exact_session_client(
        client_for,
        tenant_a,
        second_user,
        principal_kind="staff",
        principal_id=second_principal.pk,
    )
    method_id = _payment_method(tenant_a)
    loan_id = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
        amount="1000.00",
    )
    key = _key("loan-principal-namespace")
    body = {"amount_uzs": "100.00", "payment_method": method_id}
    first = _repay(cashier, loan_id, body, key=key)
    second = _repay(second_cashier, loan_id, body, key=key)

    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["outstanding_uzs"] == "900.00"
    assert second.json()["data"]["outstanding_uzs"] == "800.00"
    with schema_context(tenant_a.schema_name):
        repayments = LoanRepayment.objects.filter(loan_id=loan_id)
        assert repayments.count() == 2
        assert len(set(repayments.values_list("idempotency_key_hash", flat=True))) == 2


def test_repayment_requires_canonical_key_and_closed_body(tenant_a, user_in, client_for):
    from apps.loans.models import LoanRepayment

    _branch, actors = _same_branch_clients(
        tenant_a,
        user_in,
        client_for,
        Role.TEACHER,
        Role.DIRECTOR,
        Role.CASHIER,
    )
    (teacher, _), (director, _), (cashier, _) = actors
    method_id = _payment_method(tenant_a)
    loan_id = _disbursed_loan(
        tenant_a,
        teacher=teacher,
        director=director,
        cashier=cashier,
        method_id=method_id,
    )
    body = {"amount_uzs": "100.00", "payment_method": method_id}
    missing = cashier.post(f"{LOANS}{loan_id}/repay/", body, format="json")
    short = _repay(cashier, loan_id, body, key="too-short")
    unknown = _repay(cashier, loan_id, {**body, "loan": loan_id})

    for response in (missing, short):
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "invalid_idempotency_key"
        assert set(response.json()["errors"]) == {"Idempotency-Key"}
    assert unknown.status_code == 400, unknown.content
    assert set(unknown.json()["errors"]) == {"loan"}
    with schema_context(tenant_a.schema_name):
        assert not LoanRepayment.objects.filter(loan_id=loan_id).exists()
