"""F12-1 — student access/ID cards + scan check-in: a manager defines card types and
issues cards (unique scan code) to their branch's students; security scans a code at the
door to check the student in (a revoked/unknown card is rejected); a student sees only
their own card(s)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TYPES = "/api/v1/cards/types/"
CARDS = "/api/v1/cards/"
SCAN = "/api/v1/cards/scan/"
SCANS = "/api/v1/cards/scans/"


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
        "registrar": as_user(tenant, user_in(tenant, roles=[Role.REGISTRAR], branch=branch)),
        "security": as_user(tenant, user_in(tenant, roles=[Role.SECURITY], branch=branch)),
        "teacher": as_user(tenant, user_in(tenant, roles=[Role.TEACHER], branch=branch)),
        "student_c": as_user(tenant, student_user),
    }


def _card_type(s, name="Student ID"):
    return s["registrar"].post(TYPES, {"name": name}, format="json").json()["data"]["id"]


def _issue(s, **over):
    payload = {"student": s["student"].id, "card_type": _card_type(s)}
    payload.update(over)
    return s["registrar"].post(CARDS, payload, format="json")


def test_create_card_type(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["registrar"].post(TYPES, {"name": "Access pass"}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["data"]["is_active"] is True


def test_issue_a_card(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _issue(s)
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["is_active"] is True
    assert body["student"] == s["student"].id
    assert len(body["code"]) > 10  # a real scan code was generated


def test_scan_a_valid_card_checks_the_student_in(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    code = _issue(s).json()["data"]["code"]
    r = s["security"].post(SCAN, {"code": code}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    assert body["valid"] is True
    assert body["student"] == s["student"].id


def test_scan_a_revoked_card_is_invalid(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    card = _issue(s).json()["data"]
    rev = s["registrar"].post(f"{CARDS}{card['id']}/revoke/", {"reason": "lost"}, format="json")
    assert rev.status_code == 200
    assert rev.json()["data"]["is_active"] is False
    body = s["security"].post(SCAN, {"code": card["code"]}, format="json").json()["data"]
    assert body["valid"] is False  # logged, but rejected at the door


def test_scan_an_unknown_code_is_404(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["security"].post(SCAN, {"code": "no-such-code"}, format="json")
    assert r.status_code == 404
    assert r.json()["code"] == "card_not_found"


def test_a_card_cannot_be_revoked_twice(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = _issue(s).json()["data"]["id"]
    s["registrar"].post(f"{CARDS}{cid}/revoke/", {}, format="json")
    again = s["registrar"].post(f"{CARDS}{cid}/revoke/", {}, format="json")
    assert again.status_code == 422
    assert again.json()["code"] == "card_not_active"


def test_cannot_issue_with_a_retired_card_type(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = _card_type(s)
    s["registrar"].patch(f"{TYPES}{tid}/", {"is_active": False}, format="json")
    r = s["registrar"].post(CARDS, {"student": s["student"].id, "card_type": tid}, format="json")
    # an inactive card type isn't even in the IssueCardSerializer queryset -> 400, or 422
    assert r.status_code in (400, 422)


def test_cannot_issue_to_a_student_in_another_branch(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other = BranchFactory.create()
        outsider = StudentProfileFactory.create(branch=other)
    r = _issue(s, student=outsider.id)
    assert r.status_code == 403
    assert r.json()["code"] == "branch_out_of_scope"


def test_student_sees_only_their_own_card(tenant_a, user_in, as_user):
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    my_card = _issue(s).json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        other_student = StudentProfileFactory.create(branch=s["branch"])
    other_card = _issue(s, student=other_student.id).json()["data"]["id"]

    ids = [c["id"] for c in s["student_c"].get(CARDS).json()["data"]]
    assert my_card in ids
    assert other_card not in ids


def test_security_can_scan_but_not_issue(tenant_a, user_in, as_user):
    """Door staff scan (card:scan) but do not issue cards (card:write)."""
    s = _setup(tenant_a, user_in, as_user)
    tid = _card_type(s)
    assert (
        s["security"].post(CARDS, {"student": s["student"].id, "card_type": tid}, format="json").status_code
        == 403
    )


def test_a_role_without_card_scan_cannot_scan(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    code = _issue(s).json()["data"]["code"]
    assert s["teacher"].post(SCAN, {"code": code}, format="json").status_code == 403


def test_security_can_read_branch_cards(tenant_a, user_in, as_user):
    """Door staff (SECURITY: card:read/scan, no card:write) can look up cards in their
    branch — their granted card:read must not be inert."""
    s = _setup(tenant_a, user_in, as_user)
    cid = _issue(s).json()["data"]["id"]
    listed = s["security"].get(CARDS).json()["data"]
    assert cid in [c["id"] for c in listed]


def test_revoke_with_junk_reason_is_a_clean_400_not_500(tenant_a, user_in, as_user):
    """The revoke body is validated — an over-long reason (or a non-dict body) is a clean
    400, never an unhandled 500 from a raw value hitting the column / .get()."""
    s = _setup(tenant_a, user_in, as_user)
    cid = _issue(s).json()["data"]["id"]
    too_long = s["registrar"].post(f"{CARDS}{cid}/revoke/", {"reason": "x" * 300}, format="json")
    assert too_long.status_code == 400
    # a non-dict JSON body must not 500 either
    bad_body = s["registrar"].post(f"{CARDS}{cid}/revoke/", ["lost"], format="json")
    assert bad_body.status_code in (400, 422)


def test_type_and_card_detail_read_routes_are_wired_and_scoped(tenant_a, user_in, as_user):
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    type_id = _card_type(s, "Video access")
    assert s["registrar"].get(TYPES).status_code == 200
    assert s["registrar"].get(f"{TYPES}{type_id}/").json()["data"]["name"] == "Video access"

    own_card = _issue(s).json()["data"]
    assert s["student_c"].get(f"{CARDS}{own_card['id']}/").status_code == 200
    with schema_context(tenant_a.schema_name):
        classmate = StudentProfileFactory.create(branch=s["branch"])
    classmate_card = _issue(s, student=classmate.id).json()["data"]
    # List and detail must enforce the same self-only boundary for a student.
    assert s["student_c"].get(f"{CARDS}{classmate_card['id']}/").status_code == 404

    assert s["registrar"].head(TYPES).status_code == 200
    assert s["registrar"].head(f"{TYPES}{type_id}/").status_code == 200
    assert s["student_c"].head(f"{CARDS}{own_card['id']}/").status_code == 200


def test_scan_note_and_branch_scoped_audit_log(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    card = _issue(s).json()["data"]
    scanned = s["security"].post(SCAN, {"code": card["code"], "note": "  Side entrance  "}, format="json")
    assert scanned.status_code == 200, scanned.content
    assert scanned.json()["data"]["note"] == "Side entrance"

    listed = s["security"].get(SCANS)
    assert listed.status_code == 200, listed.content
    row = listed.json()["data"][0]
    assert row["card"] == card["id"]
    assert row["student"] == s["student"].id
    assert row["note"] == "Side entrance"
    assert row["was_valid"] is True
    assert s["security"].head(SCANS).status_code == 200
    assert s["student_c"].get(SCANS).status_code == 403

    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
    other_security = as_user(tenant_a, user_in(tenant_a, roles=[Role.SECURITY], branch=other_branch))
    assert other_security.get(SCANS).json()["data"] == []
