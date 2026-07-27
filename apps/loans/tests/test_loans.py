"""F21-1 — staff loans: a `kind="loan"` of the A-1 engine (request → approve →
disburse → ledger), plus repayment tracking and an outstanding balance that has to
reach zero. The decision lives in /approvals/; the loan-specific surface in /loans/."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

LOANS = "/api/v1/loans/"
REQ = "/api/v1/approvals/requests/"
LEDGER = "/api/v1/approvals/ledger/"


def _payment_method(tenant) -> int:
    with schema_context(tenant.schema_name):
        from apps.finance.models import PaymentMethod

        return PaymentMethod.objects.create(name="Cash", slug="cash").id


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


def test_loan_lifecycle_request_disburse_repay_settle(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id)

    # disbursed → fully outstanding
    loan = director.get(f"{LOANS}{lid}/").json()["data"]
    assert loan["outstanding_uzs"] == "1000000.00"
    assert loan["repaid_uzs"] == "0.00"
    assert loan["settled"] is False

    # partial repayment
    r1 = cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "400000.00", "payment_method": method_id}, format="json"
    )
    assert r1.status_code == 201, r1.content
    assert r1.json()["data"]["outstanding_uzs"] == "600000.00"
    assert r1.json()["data"]["settled"] is False

    # settling repayment
    r2 = cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "600000.00", "payment_method": method_id}, format="json"
    )
    assert r2.status_code == 201
    assert r2.json()["data"]["outstanding_uzs"] == "0.00"
    assert r2.json()["data"]["settled"] is True

    # a settled loan takes no more money
    over = cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "1.00", "payment_method": method_id}, format="json"
    )
    assert over.status_code == 422
    assert over.json()["code"] == "loan_already_settled"

    # two repayments recorded, each with its own money-IN ledger row
    assert len(cashier.get(f"{LOANS}{lid}/repayments/").json()["data"]) == 2
    entries = cashier.get(LEDGER).json()["data"]
    assert sum(1 for e in entries if e["entry_type"] == "loan" and e["direction"] == "out") == 1
    ins = [e for e in entries if e["entry_type"] == "loan_repayment" and e["direction"] == "in"]
    assert {e["amount_uzs"] for e in ins} == {"400000.00", "600000.00"}


def test_cannot_repay_before_disbursed(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    lid = teacher.post(LOANS, {"title": "Advance", "amount_uzs": "500000.00"}, format="json").json()["data"][
        "id"
    ]
    # approved but NOT yet disbursed → there is no money out to repay
    director.post(f"{REQ}{lid}/approve/", {}, format="json")
    r = cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "1.00", "payment_method": method_id}, format="json"
    )
    assert r.status_code == 422
    assert r.json()["code"] == "loan_not_disbursed"


def test_repayment_cannot_exceed_outstanding(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(
        tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id, amount="1000.00"
    )
    r = cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "1500.00", "payment_method": method_id}, format="json"
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


def test_repay_requires_collect_permission(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id)
    # the borrowing teacher holds loan:write but NOT loan:collect
    r = teacher.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "1.00", "payment_method": method_id}, format="json"
    )
    assert r.status_code == 403


def test_manager_raises_loan_for_another_staff_borrower(tenant_a, as_role, user_in, as_user):
    """A manager borrows ON BEHALF of staff B: B (not the keyer) sees the loan, and
    the ledger names B — the borrower — on both the OUT and IN rows (anti-fraud)."""
    manager, _ = as_role(Role.REGISTRAR)  # loan:write, not the borrower
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    b_user = user_in(tenant_a, roles=[Role.TEACHER])
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
    cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "1000.00", "payment_method": method_id}, format="json"
    )

    entries = cashier.get(LEDGER).json()["data"]
    out = next(e for e in entries if e["entry_type"] == "loan" and e["direction"] == "out")
    inn = next(e for e in entries if e["entry_type"] == "loan_repayment" and e["direction"] == "in")
    assert out["party_label"] == expected_label  # the borrower, not the manager who keyed it
    assert inn["party_label"] == expected_label


def test_borrower_cannot_approve_or_disburse_own_loan(tenant_a, as_role, user_in, as_user):
    """Segregation of duties reaches the beneficiary: a borrower can't sign off or pay
    out their own loan, even keyed by a colleague."""
    manager, _ = as_role(Role.REGISTRAR)
    borrower_user = user_in(tenant_a, roles=[Role.DIRECTOR])  # holds approve + disburse
    borrower = as_user(tenant_a, borrower_user)
    approver, _ = as_role(Role.DIRECTOR)  # a different director
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


def test_repay_with_invalid_payment_method(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    lid = _disbursed_loan(tenant_a, teacher=teacher, director=director, cashier=cashier, method_id=method_id)
    r = cashier.post(f"{LOANS}{lid}/repay/", {"amount_uzs": "1.00", "payment_method": 999999}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "payment_method_invalid"


def test_second_repayment_cannot_exceed_remaining(tenant_a, as_role):
    """The exceed check is against the RUNNING outstanding, not the original amount."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
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
        cashier.post(
            f"{LOANS}{lid}/repay/", {"amount_uzs": "700000.00", "payment_method": method_id}, format="json"
        ).status_code
        == 201
    )
    # only 300,000 remains — a 400,000 repayment must be rejected
    over = cashier.post(
        f"{LOANS}{lid}/repay/", {"amount_uzs": "400000.00", "payment_method": method_id}, format="json"
    )
    assert over.status_code == 422
    assert over.json()["code"] == "loan_repayment_exceeds"


def test_role_without_loan_is_denied(tenant_a, as_role):
    student, _ = as_role(Role.STUDENT)
    assert student.get(LOANS).status_code == 403
