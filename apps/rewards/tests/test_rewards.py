"""F17-1 — staff rewards: reward types + grants, with cash rewards routed through
the A-1 Approvals + Ledger engine (approve → disburse → ledger)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TYPES = "/api/v1/rewards/types/"
GRANTS = "/api/v1/rewards/grants/"
APPROVALS = "/api/v1/approvals/requests/"


def _rows(body):
    # Migrated (layered) endpoints return {"data": [...]}; still-DRF ones (approvals
    # ledger) return {"results": [...]}; some actions returned a bare list.
    if isinstance(body, dict):
        if "data" in body:
            return body["data"]
        if "results" in body:
            return body["results"]
    return body


def test_cash_reward_routes_through_a1_to_the_ledger(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    hod_client, _h = as_role(Role.HEAD_OF_DEPT)
    teacher_client, teacher = as_role(Role.TEACHER)
    cashier, _c = as_role(Role.CASHIER)
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import PaymentMethod

        method_id = PaymentMethod.objects.create(name="Cash", slug="cash").id

    rt = director.post(
        TYPES,
        {"name": "Performance bonus", "is_cash": True, "default_amount_uzs": "500000.00"},
        format="json",
    )
    assert rt.status_code == 201, rt.content

    # HOD grants it (so the A-1 requester != the director who will approve — maker-checker)
    granted = hod_client.post(
        GRANTS,
        {"reward_type": rt.json()["data"]["id"], "recipient": teacher.id, "reason": "Great results"},
        format="json",
    )
    assert granted.status_code == 201, granted.content
    body = granted.json()["data"]
    assert body["amount_uzs"] == "500000.00"  # defaulted from the type
    assert body["approval_request"] is not None
    assert body["approval_status"] == "pending"
    req_id = body["approval_request"]

    # the recipient sees the reward on their wall
    assert any(r["id"] == body["id"] for r in _rows(teacher_client.get(f"{GRANTS}mine/").json()))

    # the money flows through A-1: approve -> disburse -> immutable ledger row
    assert director.post(f"{APPROVALS}{req_id}/approve/", {}, format="json").status_code == 200
    dis = cashier.post(f"{APPROVALS}{req_id}/disburse/", {"payment_method": method_id}, format="json")
    assert dis.status_code == 200
    assert dis.json()["data"]["status"] == "disbursed"
    entries = cashier.get("/api/v1/approvals/ledger/").json()["data"]
    reward_entry = next(e for e in entries if e["entry_type"] == "reward")
    assert reward_entry["amount_uzs"] == "500000.00"
    # the ledger payee is the RECIPIENT (not the HOD who granted/requested it)
    assert reward_entry["party_label"] == (teacher.get_full_name() or teacher.username)


def test_reward_recipient_cannot_self_disburse(tenant_a, as_role):
    """R2-01: a cash reward's recipient may not disburse their own payout. The
    beneficiary maker-checker guard (previously loan-only) now covers rewards, so a
    cashier who is granted a cash reward cannot pay it out to themselves."""
    director, _ = as_role(Role.DIRECTOR)
    hod_client, _h = as_role(Role.HEAD_OF_DEPT)
    cashier, cashier_user = as_role(Role.CASHIER)  # holds approvals:disburse
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import PaymentMethod

        method_id = PaymentMethod.objects.create(name="Cash", slug="cash").id

    rt = director.post(
        TYPES, {"name": "Bonus", "is_cash": True, "default_amount_uzs": "100000.00"}, format="json"
    ).json()["data"]["id"]
    # HOD grants the cash reward TO the cashier (requester != recipient).
    granted = hod_client.post(
        GRANTS, {"reward_type": rt, "recipient": cashier_user.id, "reason": "x"}, format="json"
    )
    assert granted.status_code == 201, granted.content
    req_id = granted.json()["data"]["approval_request"]
    # An independent director approves — fine.
    assert director.post(f"{APPROVALS}{req_id}/approve/", {}, format="json").status_code == 200
    # The RECIPIENT (cashier) tries to pay the reward out to themselves -> blocked.
    dis = cashier.post(f"{APPROVALS}{req_id}/disburse/", {"payment_method": method_id}, format="json")
    assert dis.status_code == 403, dis.content
    assert dis.json()["code"] == "reward_self_dealing"


def test_reward_recipient_cannot_self_approve(tenant_a, as_role):
    """R2-01 (approve leg): a recipient who holds approvals:approve cannot approve
    their own reward request either."""
    granter, _ = as_role(Role.DIRECTOR)
    recipient_client, recipient = as_role(Role.DIRECTOR)  # a second director holds approvals:approve
    rt = granter.post(
        TYPES, {"name": "Bonus2", "is_cash": True, "default_amount_uzs": "100000.00"}, format="json"
    ).json()["data"]["id"]
    granted = granter.post(
        GRANTS, {"reward_type": rt, "recipient": recipient.id, "reason": "x"}, format="json"
    )
    assert granted.status_code == 201, granted.content
    req_id = granted.json()["data"]["approval_request"]
    resp = recipient_client.post(f"{APPROVALS}{req_id}/approve/", {}, format="json")
    assert resp.status_code == 403, resp.content
    assert resp.json()["code"] == "reward_self_dealing"


def test_non_cash_reward_recorded_without_approval(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _tc, teacher = as_role(Role.TEACHER)
    rt = director.post(TYPES, {"name": "Extra day off", "is_cash": False}, format="json").json()["data"]["id"]
    grant = director.post(GRANTS, {"reward_type": rt, "recipient": teacher.id}, format="json")
    assert grant.status_code == 201
    assert grant.json()["data"]["approval_request"] is None  # nothing to disburse
    assert grant.json()["data"]["amount_uzs"] is None


def test_cash_reward_requires_an_amount(tenant_a, as_role):
    hod_client, _h = as_role(Role.HEAD_OF_DEPT)
    _tc, teacher = as_role(Role.TEACHER)
    rt = hod_client.post(TYPES, {"name": "Bonus", "is_cash": True}, format="json").json()["data"]["id"]  # no default
    grant = hod_client.post(GRANTS, {"reward_type": rt, "recipient": teacher.id}, format="json")  # no amount
    assert grant.status_code == 400
    assert grant.json()["code"] == "amount_required"


def test_recipient_sees_only_their_own_rewards(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _ac, alice = as_role(Role.TEACHER)
    bob_client, _b = as_role(Role.TEACHER)
    rt = director.post(TYPES, {"name": "Certificate", "is_cash": False}, format="json").json()["data"]["id"]
    director.post(GRANTS, {"reward_type": rt, "recipient": alice.id}, format="json")

    # bob (a teacher, rewards:read) sees none of alice's rewards, and can't list all
    assert _rows(bob_client.get(f"{GRANTS}mine/").json()) == []
    assert bob_client.get(GRANTS).status_code == 403  # listing all grants is rewards:write


def test_staff_cannot_define_types_or_grant(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)  # rewards:read only
    assert teacher_client.post(TYPES, {"name": "x", "is_cash": False}, format="json").status_code == 403
    assert teacher_client.post(GRANTS, {"reward_type": 1, "recipient": 1}, format="json").status_code == 403


def test_cannot_reward_a_non_staff_user(tenant_a, user_in, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student_user = user_in(tenant_a, roles=[Role.STUDENT])
    rt = director.post(TYPES, {"name": "x", "is_cash": False}, format="json").json()["data"]["id"]
    # a student is not a valid reward recipient
    grant = director.post(GRANTS, {"reward_type": rt, "recipient": student_user.id}, format="json")
    assert grant.status_code == 400


# --------------------------------------------------------------------------- #
# review hardening
# --------------------------------------------------------------------------- #
def test_cannot_self_grant_a_reward(tenant_a, as_role):
    hod_client, hod = as_role(Role.HEAD_OF_DEPT)
    rt = hod_client.post(TYPES, {"name": "Day off", "is_cash": False}, format="json").json()["data"]["id"]
    r = hod_client.post(GRANTS, {"reward_type": rt, "recipient": hod.id}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "self_grant"


def test_reward_type_cannot_be_deleted(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    rt = director.post(TYPES, {"name": "Permanent", "is_cash": False}, format="json").json()["data"]["id"]
    # no DELETE — types are retired via is_active, never removed (PROTECT + audit history)
    assert director.delete(f"{TYPES}{rt}/").status_code == 405


def test_inactive_reward_type_cannot_be_granted(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _tc, teacher = as_role(Role.TEACHER)
    rt = director.post(TYPES, {"name": "Retired", "is_cash": False}, format="json").json()["data"]["id"]
    assert director.patch(f"{TYPES}{rt}/", {"is_active": False}, format="json").status_code == 200
    r = director.post(GRANTS, {"reward_type": rt, "recipient": teacher.id}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "reward_type_inactive"


def test_cash_reward_zero_amount_rejected(tenant_a, as_role):
    hod_client, _h = as_role(Role.HEAD_OF_DEPT)
    _tc, teacher = as_role(Role.TEACHER)
    rt = hod_client.post(TYPES, {"name": "Z", "is_cash": True}, format="json").json()["data"]["id"]
    r = hod_client.post(
        GRANTS, {"reward_type": rt, "recipient": teacher.id, "amount_uzs": "0"}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "amount_required"


def test_cash_reward_nan_amount_is_400_not_500(tenant_a, as_role):
    """decimal_field rejects a non-finite amount (would silently corrupt the money
    column / 500) with a clean 400."""
    hod_client, _h = as_role(Role.HEAD_OF_DEPT)
    _tc, teacher = as_role(Role.TEACHER)
    rt = hod_client.post(TYPES, {"name": "NaNbonus", "is_cash": True}, format="json").json()["data"]["id"]
    r = hod_client.post(
        GRANTS, {"reward_type": rt, "recipient": teacher.id, "amount_uzs": "NaN"}, format="json"
    )
    assert r.status_code == 400
    assert "amount_uzs" in r.json()["errors"]


def test_duplicate_type_name_is_400_field_error(tenant_a, as_role):
    """The unique-name validation the old ModelSerializer applied is preserved as a
    400 field error (not a generic 409)."""
    director, _ = as_role(Role.DIRECTOR)
    director.post(TYPES, {"name": "Uniq", "is_cash": False}, format="json")
    dup = director.post(TYPES, {"name": "Uniq", "is_cash": True}, format="json")
    assert dup.status_code == 400
    assert "name" in dup.json()["errors"]


def test_blank_type_name_on_update_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    rt = director.post(TYPES, {"name": "Named", "is_cash": False}, format="json").json()["data"]["id"]
    r = director.patch(f"{TYPES}{rt}/", {"name": ""}, format="json")
    assert r.status_code == 400
    assert "name" in r.json()["errors"]


def test_negative_amount_rejected(tenant_a, as_role):
    hod_client, _h = as_role(Role.HEAD_OF_DEPT)
    _tc, teacher = as_role(Role.TEACHER)
    rt = hod_client.post(TYPES, {"name": "NegT", "is_cash": False}, format="json").json()["data"]["id"]
    r = hod_client.post(
        GRANTS, {"reward_type": rt, "recipient": teacher.id, "amount_uzs": "-5"}, format="json"
    )
    assert r.status_code == 400
    assert "amount_uzs" in r.json()["errors"]


def test_non_manager_cannot_retrieve_anothers_grant(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _ac, alice = as_role(Role.TEACHER)
    bob_client, _b = as_role(Role.TEACHER)
    rt = director.post(TYPES, {"name": "Cert", "is_cash": False}, format="json").json()["data"]["id"]
    gid = director.post(GRANTS, {"reward_type": rt, "recipient": alice.id}, format="json").json()["data"]["id"]
    # bob (a teacher) can't fetch alice's reward by id — it's not in his scope
    assert bob_client.get(f"{GRANTS}{gid}/").status_code == 404
