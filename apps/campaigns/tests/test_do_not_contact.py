"""F10-1 (consent) — the SMS do-not-contact list: a phone that has opted out is
suppressed from every campaign, at BOTH build time (never queued) and send time (an
opt-out recorded after the build is still honoured). Consent is per-phone, so a guardian
who said stop is never texted again about any of their children (dignity / anti-spam)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

CAMPAIGNS = "/api/v1/campaigns/"
DNC = "/api/v1/campaigns/do-not-contact/"


def _student_with_phone(branch):
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    student = StudentProfileFactory.create(branch=branch, status=StudentProfile.Status.ACTIVE)
    parent = ParentProfileFactory.create()
    GuardianFactory.create(parent=parent, student=student, is_primary=True)
    return student


def _branch(tenant):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        return BranchFactory.create()


def _phone_of(student):
    from apps.campaigns.services import _resolve_phone

    return _resolve_phone(student)


def _create(client, branch):
    return client.post(CAMPAIGNS, {"name": "x", "message": "hi", "branch": branch.id}, format="json").json()[
        "data"
    ]["id"]


def _send(client, cid):
    return client.post(f"{CAMPAIGNS}{cid}/send/", {}, format="json").json()["data"]


def _recipients(client, cid):
    return client.get(f"{CAMPAIGNS}{cid}/recipients/").json()["data"]


def test_build_suppresses_an_opted_out_phone(tenant_a, user_in, as_user, sms_outbox):
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        from apps.campaigns.models import DoNotContact

        opted_out = _student_with_phone(branch)
        _student_with_phone(branch)  # a second family that DID NOT opt out
        DoNotContact.objects.create(phone=_phone_of(opted_out), reason="asked to stop")

    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    cid = _create(client, branch)

    recipients = _recipients(client, cid)
    by_student = {r["student"]: r for r in recipients}
    assert by_student[opted_out.id]["status"] == "skipped"
    assert by_student[opted_out.id]["error"] == "do_not_contact"
    assert sum(1 for r in recipients if r["status"] == "pending") == 1  # only the other family

    sent = _send(client, cid)
    assert sent["skipped_count"] == 1
    assert sent["sent_count"] == 1
    assert len(sms_outbox) == 1  # the opted-out phone was never texted
    assert _phone_of(opted_out) not in {m["phone"] for m in sms_outbox}


def test_send_honours_an_opt_out_recorded_after_the_build(tenant_a, user_in, as_user, sms_outbox):
    """Consent wins over the frozen recipient list: a phone added to do-not-contact
    between build and send is skipped, not texted."""
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        student = _student_with_phone(branch)
        phone = _phone_of(student)

    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    cid = _create(client, branch)

    # the family opts out AFTER the campaign was built (recipient is still PENDING)
    with schema_context(tenant_a.schema_name):
        from apps.campaigns.models import DoNotContact

        DoNotContact.objects.create(phone=phone, reason="late opt-out")

    sent = _send(client, cid)
    assert sent["sent_count"] == 0
    assert len(sms_outbox) == 0  # never texted despite being PENDING at build
    recipients = _recipients(client, cid)
    assert recipients[0]["status"] == "skipped"
    assert recipients[0]["error"] == "do_not_contact"


def test_opt_out_phone_is_normalized_so_a_non_canonical_format_still_suppresses(
    tenant_a, user_in, as_user, sms_outbox
):
    """User.phone is always stored E.164; a do-not-contact typed in a different but
    equivalent format must be canonicalized to match, or the opt-out silently fails and
    the family is texted anyway (the feature's hard invariant)."""
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        student = _student_with_phone(branch)
        e164 = _phone_of(student)
    variant = e164[:5] + " " + e164[5:]  # same number, a space inserted (phonenumbers ignores it)
    assert variant != e164

    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    created = client.post(DNC, {"phone": variant}, format="json")
    assert created.status_code == 201, created.content
    assert created.json()["data"]["phone"] == e164  # stored canonicalized to E.164

    cid = _create(client, branch)
    sent = _send(client, cid)
    assert sent["sent_count"] == 0
    assert len(sms_outbox) == 0  # suppressed despite the format difference


def test_an_unparseable_phone_is_a_clean_400(tenant_a, user_in, as_user):
    branch = _branch(tenant_a)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    r = client.post(DNC, {"phone": "not-a-number"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_phone"


def test_manage_the_list_through_the_api(tenant_a, user_in, as_user, as_role):
    branch = _branch(tenant_a)
    registrar = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch)
    client = as_user(tenant_a, registrar)

    created = client.post(DNC, {"phone": "+998901112233", "reason": "complaint"}, format="json")
    assert created.status_code == 201, created.content
    entry_id = created.json()["data"]["id"]
    assert created.json()["data"]["phone"] == "+998901112233"

    listed = client.get(DNC).json()
    assert any(e["id"] == entry_id for e in listed["data"])

    # Removing a center-wide consent record is director-only; a branch registrar can
    # add an opt-out immediately but cannot silently opt a family back in.
    assert client.delete(f"{DNC}{entry_id}/").status_code == 403
    director, director_user = as_role(Role.DIRECTOR)
    assert director.delete(f"{DNC}{entry_id}/").status_code == 204
    with schema_context(tenant_a.schema_name):
        from apps.audit.models import AuditLog
        from apps.campaigns.models import DoNotContact

        assert not DoNotContact.objects.filter(pk=entry_id).exists()
        created_log = AuditLog.objects.get(
            action="create",
            resource_type="campaign_do_not_contact",
            resource_id=str(entry_id),
        )
        deleted_log = AuditLog.objects.get(
            action="delete",
            resource_type="campaign_do_not_contact",
            resource_id=str(entry_id),
        )
        assert created_log.actor_id == registrar.pk
        assert deleted_log.actor_id == director_user.pk
        assert deleted_log.before["phone"] == "+998901112233"


def test_duplicate_phone_is_a_clean_conflict(tenant_a, user_in, as_user):
    branch = _branch(tenant_a)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    assert client.post(DNC, {"phone": "+998900000001"}, format="json").status_code == 201
    dup = client.post(DNC, {"phone": "+998900000001"}, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "already_opted_out"


def test_managing_the_list_needs_campaign_write(tenant_a, as_role):
    student, _ = as_role(Role.STUDENT)  # holds no campaign:write
    assert student.post(DNC, {"phone": "+998900000009"}, format="json").status_code == 403
