"""#8 — book/material cash sales: a sale writes an immutable money-IN ledger row; a
refund writes a compensating money-OUT row (the ledger is never mutated)."""

from __future__ import annotations

from itertools import count

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role
from tests.role_principal_helpers import ensure_role_principal, exact_session_client

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales/"
LEDGER = "/api/v1/approvals/ledger/"
_KEYS = count(1)


def _key(prefix: str = "sale-test") -> str:
    return f"{prefix}-{next(_KEYS):08d}"


def _setup(tenant, user_in, client_for):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch)
        method = PaymentMethod.objects.create(name="Cash", slug="cash")
    cashier_user = user_in(tenant, roles=[Role.CASHIER], branch=branch)
    registrar_user = user_in(tenant, roles=[Role.REGISTRAR], branch=branch)
    with schema_context(tenant.schema_name):
        cashier_principal = ensure_role_principal(
            cashier_user,
            roles=[Role.CASHIER],
            branch=branch,
        )
        registrar_principal = ensure_role_principal(
            registrar_user,
            roles=[Role.REGISTRAR],
            branch=branch,
        )
    return {
        "branch": branch,
        "student": student,
        "method": method.id,
        "cashier_user": cashier_user,
        "cashier": exact_session_client(
            client_for,
            tenant,
            cashier_user,
            principal_kind="staff",
            principal_id=cashier_principal.pk,
        ),
        "registrar": exact_session_client(
            client_for,
            tenant,
            registrar_user,
            principal_kind="staff",
            principal_id=registrar_principal.pk,
        ),
    }


def _sale_body(s, **over):
    body = {
        "item": "Course book",
        "quantity": 2,
        "unit_price_uzs": "75000.00",
        "student": s["student"].id,
        "payment_method": s["method"],
    }
    body.update(over)
    return body


def _post_sale(client, s, *, key: str | None = None, **over):
    return client.post(
        SALES,
        _sale_body(s, **over),
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or _key(),
    )


def test_record_sale_writes_money_in_ledger(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    r = _post_sale(s["cashier"], s)
    assert r.status_code == 201, r.content
    assert r.json()["data"]["status"] == "completed"
    assert r.json()["data"]["amount_uzs"] == "150000.00"  # 2 x 75000
    assert r.json()["data"]["ledger_entry"] is not None

    entries = s["cashier"].get(LEDGER).json()["data"]
    assert any(
        e["entry_type"] == "book_sale" and e["direction"] == "in" and e["amount_uzs"] == "150000.00"
        for e in entries
    )


def test_refund_writes_compensating_out_row(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    sid = _post_sale(s["cashier"], s).json()["data"]["id"]

    refunded = s["cashier"].post(f"{SALES}{sid}/refund/", {"reason": "wrong book"}, format="json")
    assert refunded.status_code == 200, refunded.content
    assert refunded.json()["data"]["status"] == "refunded"
    assert refunded.json()["data"]["refund_ledger_entry"] is not None
    # a refunded sale can't be refunded again
    assert s["cashier"].post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 422

    # the OUT row COMPENSATES the IN row (nets to zero); the original IN row is preserved
    entries = s["cashier"].get(LEDGER).json()["data"]
    ins = [e for e in entries if e["entry_type"] == "book_sale"]
    outs = [e for e in entries if e["entry_type"] == "book_sale_refund"]
    assert len(ins) == 1
    assert len(outs) == 1
    assert outs[0]["direction"] == "out"
    assert ins[0]["amount_uzs"] == outs[0]["amount_uzs"] == "150000.00"


def test_refund_is_branch_scoped(tenant_a, user_in, client_for, as_user, as_role):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, client_for)
    sid = _post_sale(s["cashier"], s).json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
    other_cashier = as_user(tenant_a, user_in(tenant_a, roles=[Role.CASHIER], branch=other_branch))
    # a cashier at another branch can neither see nor refund this sale -> 404 (not 422/403)
    assert other_cashier.get(f"{SALES}{sid}/").status_code == 404
    assert other_cashier.post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 404
    # a director (any branch) can refund
    director, _ = as_role(Role.DIRECTOR)
    assert director.post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 200


def test_staff_list_is_branch_scoped(tenant_a, user_in, client_for):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.sales.models import Sale
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, client_for)
    _post_sale(s["cashier"], s)  # a sale in the cashier's branch
    with schema_context(tenant_a.schema_name):  # ...and one in another branch
        other_branch = BranchFactory.create()
        other_student = StudentProfileFactory.create(branch=other_branch)
        method = PaymentMethod.objects.create(name="Card", slug="card")
        Sale.objects.create(
            item="x",
            quantity=1,
            unit_price_uzs="10.00",
            amount_uzs="10.00",
            student=other_student,
            branch=other_branch,
            payment_method=method,
        )

    body = s["cashier"].get(SALES).json()
    assert body["pagination"]["total"] == 1  # the cashier sees only their own branch's till
    assert body["data"][0]["branch"] == s["branch"].id


def test_missing_and_cross_branch_students_are_indistinguishable(tenant_a, user_in, client_for):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, client_for)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        other_student = StudentProfileFactory.create(branch=other_branch)
    cross_branch = _post_sale(s["cashier"], s, student=other_student.id)
    missing = _post_sale(s["cashier"], s, student=999_999_999)
    assert cross_branch.status_code == missing.status_code == 404
    cross_body = cross_branch.json()
    missing_body = missing.json()
    assert set(cross_body) == set(missing_body)
    assert {key: value for key, value in cross_body.items() if key != "request_id"} == {
        key: value for key, value in missing_body.items() if key != "request_id"
    }
    assert cross_body["code"] == "not_found"


def test_refund_requires_refund_permission(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    # reception can ring up a sale (sale:write) but not refund (no sale:refund) — SoD
    sid = _post_sale(s["registrar"], s).json()["data"]["id"]
    assert s["registrar"].post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 403


def test_invalid_payment_method_rejected(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    r = _post_sale(s["cashier"], s, payment_method=999999)
    assert r.status_code == 422
    assert r.json()["code"] == "payment_method_invalid"


def test_quantity_is_capped(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    # an absurd quantity is a clean 400, not a DB-overflow 500
    r = _post_sale(s["cashier"], s, quantity=3_000_000_000)
    assert r.status_code == 400


def test_sub_cent_unit_price_rejected(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    # >2 decimal places would be quantized to 0.01 on the NUMERIC(18,2) column while the
    # ledgered amount is derived from the full-precision input (0.014 x qty) -> the line
    # item would not reconcile with the immutable money-IN row. The old DecimalField 400'd
    # this via validate_precision; the layered path must too.
    r = _post_sale(s["cashier"], s, unit_price_uzs="0.014")
    assert r.status_code == 400


def test_non_finance_roles_cannot_see_the_till(tenant_a, as_role):
    # the till isn't family-facing: teacher / student / parent have no sale:read
    for role in (Role.TEACHER, Role.STUDENT, Role.PARENT):
        client, _ = as_role(role)
        assert client.get(SALES).status_code == 403


def test_sale_detail_filters_head_and_trimmed_text(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    created = _post_sale(s["cashier"], s, item="  Course book  ", note="  Gift copy  ")
    sale_id = created.json()["data"]["id"]
    detail = s["cashier"].get(f"{SALES}{sale_id}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["item"] == "Course book"
    assert detail.json()["data"]["note"] == "Gift copy"

    filtered = s["cashier"].get(
        f"{SALES}?student={s['student'].id}&payment_method={s['method']}&ordering=-amount_uzs"
    )
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()["data"]] == [sale_id]
    assert s["cashier"].head(SALES).status_code == 200
    assert s["cashier"].head(f"{SALES}{sale_id}/").status_code == 200

    refunded = s["cashier"].post(
        f"{SALES}{sale_id}/refund/", {"reason": "  Returned unopened  "}, format="json"
    )
    assert refunded.json()["data"]["refund_reason"] == "Returned unopened"


def test_explicit_null_quantity_is_rejected(tenant_a, user_in, client_for):
    s = _setup(tenant_a, user_in, client_for)
    response = _post_sale(s["cashier"], s, quantity=None)
    assert response.status_code == 400
    assert "quantity" in response.json()["errors"]


def test_sale_exact_retry_returns_original_after_payment_method_is_retired(
    tenant_a,
    user_in,
    client_for,
):
    from apps.approvals.models import LedgerEntry
    from apps.finance.models import PaymentMethod
    from apps.sales.models import Sale

    s = _setup(tenant_a, user_in, client_for)
    key = _key("sale-exact-retry")
    first = _post_sale(s["cashier"], s, key=key, note="Counter 2")
    assert first.status_code == 201, first.content
    sale_id = first.json()["data"]["id"]
    refunded = s["cashier"].post(
        f"{SALES}{sale_id}/refund/",
        {"reason": "Returned after checkout"},
        format="json",
    )
    assert refunded.status_code == 200, refunded.content
    assert refunded.json()["data"]["status"] == "refunded"

    with schema_context(tenant_a.schema_name):
        PaymentMethod.objects.filter(pk=s["method"]).update(is_active=False)
    replay = _post_sale(s["cashier"], s, key=key, note="Counter 2")

    assert replay.status_code == 201, replay.content
    assert replay.json()["data"] == first.json()["data"]
    assert replay.json()["data"]["status"] == "completed"
    assert replay.json()["data"]["refund_ledger_entry"] is None
    with schema_context(tenant_a.schema_name):
        sale = Sale.objects.get(pk=sale_id)
        assert sale.idempotency_key_hash != key
        assert len(sale.idempotency_key_hash or "") == 64
        assert sale.operation_fingerprint
        assert sale.sold_by_principal_kind == "staff"
        assert sale.creation_response_snapshot == first.json()["data"]
        assert Sale.objects.filter(pk=sale_id).count() == 1
        assert LedgerEntry.objects.filter(
            entry_type="book_sale",
            source_kind="sale",
            source_id=sale_id,
        ).count() == 1


def test_sale_changed_key_reuse_conflicts_without_second_money_row(tenant_a, user_in, client_for):
    from apps.approvals.models import LedgerEntry
    from apps.sales.models import Sale

    s = _setup(tenant_a, user_in, client_for)
    key = _key("sale-key-reuse")
    assert _post_sale(s["cashier"], s, key=key, note="Original").status_code == 201
    changed = _post_sale(s["cashier"], s, key=key, note="Changed")

    assert changed.status_code == 409, changed.content
    assert changed.json()["code"] == "idempotency_key_reused"
    with schema_context(tenant_a.schema_name):
        sales = Sale.objects.filter(student=s["student"])
        assert sales.count() == 1
        assert LedgerEntry.objects.filter(
            entry_type="book_sale",
            source_kind="sale",
            source_id__in=sales.values("pk"),
        ).count() == 1


def test_sale_replay_uses_historical_branch_after_student_transfer(tenant_a, user_in, client_for):
    from apps.org.tests.factories import BranchFactory
    from apps.sales.models import Sale
    from apps.students.models import StudentProfile

    s = _setup(tenant_a, user_in, client_for)
    key = _key("sale-scope-recheck")
    first = _post_sale(s["cashier"], s, key=key)
    assert first.status_code == 201
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        StudentProfile.objects.filter(pk=s["student"].pk).update(branch=other_branch)

    replay = _post_sale(s["cashier"], s, key=key)
    assert replay.status_code == 201, replay.content
    assert replay.json()["data"] == first.json()["data"]
    with schema_context(tenant_a.schema_name):
        assert Sale.objects.filter(student=s["student"]).count() == 1


def test_sale_replay_does_not_leak_old_branch_snapshot_after_scope_transfer(
    tenant_a,
    user_in,
    client_for,
):
    from apps.org.tests.factories import BranchFactory
    from apps.sales.models import Sale
    from apps.students.models import StudentProfile
    from apps.users.models import RoleMembership

    s = _setup(tenant_a, user_in, client_for)
    key = _key("sale-historical-scope")
    assert _post_sale(s["cashier"], s, key=key).status_code == 201
    with schema_context(tenant_a.schema_name):
        new_branch = BranchFactory.create()
        StudentProfile.objects.filter(pk=s["student"].pk).update(branch=new_branch)
        RoleMembership.objects.filter(user=s["cashier_user"]).update(branch=new_branch)

    replay = _post_sale(s["cashier"], s, key=key)
    assert replay.status_code == 404, replay.content
    assert replay.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        assert Sale.objects.filter(student=s["student"]).count() == 1


def test_sale_key_namespace_is_exact_role_principal(tenant_a, user_in, client_for):
    from apps.sales.models import Sale

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
    key = _key("sale-principal-namespace")
    first = _post_sale(s["cashier"], s, key=key)
    other = _post_sale(second, s, key=key)

    assert first.status_code == other.status_code == 201
    assert first.json()["data"]["id"] != other.json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        sales = Sale.objects.filter(student=s["student"])
        assert sales.count() == 2
        assert len(set(sales.values_list("idempotency_key_hash", flat=True))) == 2


def test_sale_requires_canonical_key_and_closed_body(tenant_a, user_in, client_for):
    from apps.sales.models import Sale

    s = _setup(tenant_a, user_in, client_for)
    missing = s["cashier"].post(SALES, _sale_body(s), format="json")
    short = _post_sale(s["cashier"], s, key="too-short")
    unknown = _post_sale(s["cashier"], s, internal_total="150000.00")

    for response in (missing, short):
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "invalid_idempotency_key"
        assert set(response.json()["errors"]) == {"Idempotency-Key"}
    assert unknown.status_code == 400, unknown.content
    assert set(unknown.json()["errors"]) == {"internal_total"}
    with schema_context(tenant_a.schema_name):
        assert not Sale.objects.filter(student=s["student"]).exists()
