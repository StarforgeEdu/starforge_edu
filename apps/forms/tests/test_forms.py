"""F3-3 — forms / surveys engine: build → publish → submit → summarize, with
type/required validation, anonymity, one-per-respondent dedupe, lifecycle guards,
and permission scoping (builders vs responders)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

FORMS = "/api/v1/forms/"


def _rows(body):
    return body["data"] if isinstance(body, dict) and "data" in body else body


def _build_published_form(client, **form_kwargs):
    """A 3-field form: required single-choice, required rating, optional textarea."""
    fid = client.post(FORMS, {"title": "Feedback", **form_kwargs}, format="json").json()["data"]["id"]
    f1 = client.post(
        f"{FORMS}{fid}/fields/",
        {"label": "Liked?", "field_type": "single_choice", "required": True, "options": ["yes", "no"]},
        format="json",
    ).json()["data"]["id"]
    f2 = client.post(
        f"{FORMS}{fid}/fields/",
        {"label": "Rating", "field_type": "rating", "required": True},
        format="json",
    ).json()["data"]["id"]
    f3 = client.post(
        f"{FORMS}{fid}/fields/", {"label": "Comments", "field_type": "textarea"}, format="json"
    ).json()["data"]["id"]
    pub = client.post(f"{FORMS}{fid}/publish/", {}, format="json")
    assert pub.status_code == 200, pub.content
    return fid, (f1, f2, f3)


def test_create_and_update_form_audience(tenant_a, as_role):
    """F3-2: a form can target roles and/or specific users; the audience round-trips."""
    director, _ = as_role(Role.DIRECTOR)
    _teacher_client, teacher = as_role(Role.TEACHER)
    _registrar_client, registrar = as_role(Role.REGISTRAR)
    created = director.post(
        FORMS,
        {
            "title": "Staff survey",
            "audience_roles": ["teacher", "teacher"],
            "audience_user_ids": [teacher.pk, teacher.pk, registrar.pk],
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    data = created.json()["data"]
    assert data["audience_roles"] == ["teacher"]  # deduped
    assert data["audience_user_ids"] == [teacher.pk, registrar.pk]  # deduped

    fid = data["id"]
    patched = director.patch(f"{FORMS}{fid}/", {"audience_roles": ["registrar"]}, format="json")
    assert patched.status_code == 200, patched.content
    assert patched.json()["data"]["audience_roles"] == ["registrar"]


def test_shared_bridge_principals_do_not_share_form_target_or_dedupe_identity(tenant_a, as_role, client_for):
    from apps.forms.models import Form, FormResponse
    from apps.org.tests.factories import BranchFactory
    from tests.role_principal_helpers import exact_session_client, shared_staff_teacher_bridge

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user, teacher, staff = shared_staff_teacher_bridge(
            branch=branch,
            staff_role=Role.REGISTRAR,
        )
        form = Form.objects.create(
            title="Teacher-only",
            status=Form.Status.PUBLISHED,
            # Centre-wide keeps the staff account on the responder path. If this
            # were branch-bound, the registrar's forms:write grant would make it
            # visible as a manageable form independently of its audience.
            branch=None,
            audience_user_ids=[user.pk],
            audience_principals=[{"kind": "teacher", "id": teacher.pk, "user_id": user.pk}],
            published_at=timezone.now(),
        )

    teacher_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="teacher",
        principal_id=teacher.pk,
    )
    staff_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="staff",
        principal_id=staff.pk,
    )
    assert form.pk in {row["id"] for row in _rows(teacher_client.get(FORMS).json())}
    assert form.pk not in {row["id"] for row in _rows(staff_client.get(FORMS).json())}

    # The legacy public user selector cannot safely choose between those accounts.
    ambiguous_target = director.post(
        FORMS,
        {"title": "Unsafe target", "audience_user_ids": [user.pk]},
        format="json",
    )
    assert ambiguous_target.status_code == 400
    assert ambiguous_target.json()["errors"] == {
        "audience_user_ids": ["Choose active recipients in the form's scope."]
    }

    # On an open form, the two role accounts remain independent respondents even
    # though they share the compatibility User FK.
    with schema_context(tenant_a.schema_name):
        open_form = Form.objects.create(
            title="Open",
            status=Form.Status.PUBLISHED,
            branch=branch,
            published_at=timezone.now(),
        )
    assert (
        teacher_client.post(f"{FORMS}{open_form.pk}/submit/", {"answers": []}, format="json").status_code
        == 201
    )
    assert (
        staff_client.post(f"{FORMS}{open_form.pk}/submit/", {"answers": []}, format="json").status_code == 201
    )
    with schema_context(tenant_a.schema_name):
        responses = list(
            FormResponse.objects.filter(form=open_form)
            .order_by("respondent_principal_kind")
            .values_list(
                "respondent_principal_kind",
                "respondent_principal_id",
                "respondent_attribution_status",
            )
        )
    assert responses == [
        ("staff", staff.pk, FormResponse.AttributionStatus.CAPTURED),
        ("teacher", teacher.pk, FormResponse.AttributionStatus.CAPTURED),
    ]


def test_form_windows_unknown_fields_and_resource_bounds_are_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    now = timezone.now()

    reversed_window = director.post(
        FORMS,
        {
            "title": "Bad window",
            "opens_at": now.isoformat(),
            "closes_at": (now - timedelta(minutes=1)).isoformat(),
        },
        format="json",
    )
    assert reversed_window.status_code == 400
    assert set(reversed_window.json()["errors"]) == {"closes_at"}
    assert director.post(FORMS, {"title": "x", "surprise": True}, format="json").status_code == 400
    assert director.get(f"{FORMS}?surprise=true").status_code == 400

    form_id = director.post(FORMS, {"title": "Bounded"}, format="json").json()["data"]["id"]
    too_many_options = director.post(
        f"{FORMS}{form_id}/fields/",
        {
            "label": "Choice",
            "field_type": "single_choice",
            "options": [f"option-{number}" for number in range(101)],
        },
        format="json",
    )
    assert too_many_options.status_code == 400


def test_form_number_and_text_answer_bounds_are_enforced(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    form_id = director.post(FORMS, {"title": "Bounded answers"}, format="json").json()["data"]["id"]
    number_id = director.post(
        f"{FORMS}{form_id}/fields/",
        {"label": "Number", "field_type": "number"},
        format="json",
    ).json()["data"]["id"]
    text_id = director.post(
        f"{FORMS}{form_id}/fields/",
        {"label": "Text", "field_type": "text"},
        format="json",
    ).json()["data"]["id"]
    director.post(f"{FORMS}{form_id}/publish/", {}, format="json")

    huge_number = student.post(
        f"{FORMS}{form_id}/submit/",
        {"answers": [{"field": number_id, "value": 1_000_000_000_001}]},
        format="json",
    )
    assert huge_number.status_code == 400
    assert huge_number.json()["code"] == "field_number_range"
    huge_text = student.post(
        f"{FORMS}{form_id}/submit/",
        {"answers": [{"field": text_id, "value": "x" * 1001}]},
        format="json",
    )
    assert huge_text.status_code == 400
    assert huge_text.json()["code"] == "field_text_too_long"


def test_bad_audience_is_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    bad_role = director.post(FORMS, {"title": "x", "audience_roles": ["teacher", "wizard"]}, format="json")
    assert bad_role.status_code == 400
    assert bad_role.json()["code"] == "validation_error"

    bad_uid = director.post(FORMS, {"title": "x", "audience_user_ids": [1, "nope"]}, format="json")
    assert bad_uid.status_code == 400
    assert bad_uid.json()["code"] == "validation_error"


def test_build_publish_submit_and_summary(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, (f1, f2, f3) = _build_published_form(director)

    resp = student.post(
        f"{FORMS}{fid}/submit/",
        {
            "answers": [
                {"field": f1, "value": "yes"},
                {"field": f2, "value": 5},
                {"field": f3, "value": "great class"},
            ]
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content

    rows = _rows(director.get(f"{FORMS}{fid}/responses/").json())
    assert len(rows) == 1
    assert rows[0]["respondent_attribution_status"] == "captured"
    assert {a["field"]: a["value"] for a in rows[0]["answers"]}[f1] == "yes"

    summary = director.get(f"{FORMS}{fid}/summary/").json()["data"]
    assert summary["response_count"] == 1
    by_field = {x["field"]: x for x in summary["fields"]}
    assert by_field[f1]["summary"]["counts"] == {"yes": 1, "no": 0}
    assert by_field[f2]["summary"]["avg"] == 5


def test_required_and_type_validation(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, (f1, f2, _f3) = _build_published_form(director)

    def submit(answers):
        return student.post(f"{FORMS}{fid}/submit/", {"answers": answers}, format="json")

    missing = submit([{"field": f2, "value": 3}])  # required f1 omitted
    assert missing.status_code == 400
    assert missing.json()["code"] == "field_required"

    bad_choice = submit([{"field": f1, "value": "maybe"}, {"field": f2, "value": 3}])
    assert bad_choice.status_code == 400
    assert bad_choice.json()["code"] == "field_choice_invalid"

    bad_rating = submit([{"field": f1, "value": "yes"}, {"field": f2, "value": 9}])
    assert bad_rating.status_code == 400
    assert bad_rating.json()["code"] == "field_rating_range"

    # text where a rating is expected
    bad_type = submit([{"field": f1, "value": "yes"}, {"field": f2, "value": "five"}])
    assert bad_type.status_code == 400
    assert bad_type.json()["code"] == "field_rating_range"


def test_anonymous_form_does_not_record_respondent(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, (f1, f2, _f3) = _build_published_form(director, is_anonymous=True)
    student.post(
        f"{FORMS}{fid}/submit/",
        {"answers": [{"field": f1, "value": "no"}, {"field": f2, "value": 2}]},
        format="json",
    )
    rows = _rows(director.get(f"{FORMS}{fid}/responses/").json())
    assert rows[0]["respondent"] is None
    assert rows[0]["respondent_principal"] is None
    assert rows[0]["respondent_attribution_status"] == "anonymous"


def test_response_principal_owner_and_snapshot_are_database_guarded(tenant_a, as_role):
    from django.db import IntegrityError, transaction

    from apps.forms.models import Form, FormResponse

    _first_client, first = as_role(Role.STUDENT)
    _second_client, second = as_role(Role.STUDENT)
    with schema_context(tenant_a.schema_name):
        form = Form.objects.create(title="Guarded attribution")
        with pytest.raises(IntegrityError), transaction.atomic():
            FormResponse.objects.create(
                form=form,
                respondent=first,
                respondent_principal_kind="student",
                respondent_principal_id=second.test_principal_id,
                respondent_attribution_status=FormResponse.AttributionStatus.CAPTURED,
            )

        response = FormResponse.objects.create(
            form=form,
            respondent=first,
            respondent_principal_kind="student",
            respondent_principal_id=first.test_principal_id,
            respondent_attribution_status=FormResponse.AttributionStatus.CAPTURED,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            FormResponse.objects.filter(pk=response.pk).update(
                respondent_attribution_status=FormResponse.AttributionStatus.QUARANTINED,
                respondent_principal_kind="",
                respondent_principal_id=None,
            )


def test_one_response_per_respondent_then_allow_multiple(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, (f1, f2, _f3) = _build_published_form(director)
    answer = {"answers": [{"field": f1, "value": "yes"}, {"field": f2, "value": 4}]}
    assert student.post(f"{FORMS}{fid}/submit/", answer, format="json").status_code == 201
    dup = student.post(f"{FORMS}{fid}/submit/", answer, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "already_responded"

    # a form that allows multiple accepts repeat submissions
    fid2, (g1, g2, _g3) = _build_published_form(director, allow_multiple=True)
    a2 = {"answers": [{"field": g1, "value": "no"}, {"field": g2, "value": 1}]}
    assert student.post(f"{FORMS}{fid2}/submit/", a2, format="json").status_code == 201
    assert student.post(f"{FORMS}{fid2}/submit/", a2, format="json").status_code == 201


def test_lifecycle_guards(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)

    # publish with no fields -> 422
    empty = director.post(FORMS, {"title": "empty"}, format="json").json()["data"]["id"]
    no_fields = director.post(f"{FORMS}{empty}/publish/", {}, format="json")
    assert no_fields.status_code == 422
    assert no_fields.json()["code"] == "form_has_no_fields"

    # submit to a draft -> 422 form_not_open
    director.post(f"{FORMS}{empty}/fields/", {"label": "q", "field_type": "text"}, format="json")
    draft_submit = director.post(f"{FORMS}{empty}/submit/", {"answers": []}, format="json")
    assert draft_submit.status_code == 422
    assert draft_submit.json()["code"] == "form_not_open"

    # add a field to a published form -> 422 form_not_draft
    fid, _f = _build_published_form(director)
    late = director.post(f"{FORMS}{fid}/fields/", {"label": "late", "field_type": "text"}, format="json")
    assert late.status_code == 422
    assert late.json()["code"] == "form_not_draft"

    # closing then submitting -> 422
    assert director.post(f"{FORMS}{fid}/close/", {}, format="json").status_code == 200
    closed = director.post(
        f"{FORMS}{fid}/submit/", {"answers": [{"field": _f[0], "value": "yes"}]}, format="json"
    )
    assert closed.status_code == 422


def test_responder_cannot_build_or_see_responses(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    # a student (forms:read) cannot create a form
    assert student.post(FORMS, {"title": "x"}, format="json").status_code == 403

    fid, _f = _build_published_form(director)
    # nor read responses / summary (forms:write)
    assert student.get(f"{FORMS}{fid}/responses/").status_code == 403
    assert student.get(f"{FORMS}{fid}/summary/").status_code == 403


def test_responder_lists_only_published_forms(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    director.post(FORMS, {"title": "hidden draft"}, format="json")  # stays draft
    _build_published_form(director)

    rows = _rows(student.get(FORMS).json())
    assert rows  # sees something
    assert {r["status"] for r in rows} == {"published"}  # never a draft


# --------------------------------------------------------------------------- #
# review hardening
# --------------------------------------------------------------------------- #
def _build_typed_form(client):
    fid = client.post(FORMS, {"title": "Typed"}, format="json").json()["data"]["id"]
    f = {}
    for spec in (
        {"label": "agree", "field_type": "boolean", "required": True},
        {"label": "age", "field_type": "number"},
        {"label": "when", "field_type": "date"},
        {"label": "langs", "field_type": "multi_choice", "options": ["en", "uz", "ru"]},
    ):
        f[spec["label"]] = client.post(f"{FORMS}{fid}/fields/", spec, format="json").json()["data"]["id"]
    client.post(f"{FORMS}{fid}/publish/", {}, format="json")
    return fid, f


def test_all_field_types_submit_and_summary(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    s1, _ = as_role(Role.STUDENT)
    s2, _ = as_role(Role.STUDENT)
    fid, f = _build_typed_form(director)

    r1 = s1.post(
        f"{FORMS}{fid}/submit/",
        {
            "answers": [
                {"field": f["agree"], "value": True},
                {"field": f["age"], "value": 20},
                {"field": f["when"], "value": "2026-06-01"},
                {"field": f["langs"], "value": ["en", "uz"]},
            ]
        },
        format="json",
    )
    assert r1.status_code == 201, r1.content
    # required boolean answered False must be accepted (not treated as "empty")
    r2 = s2.post(
        f"{FORMS}{fid}/submit/",
        {
            "answers": [
                {"field": f["agree"], "value": False},
                {"field": f["age"], "value": 30},
                {"field": f["langs"], "value": ["en"]},
            ]
        },
        format="json",
    )
    assert r2.status_code == 201, r2.content

    by = {x["field"]: x["summary"] for x in director.get(f"{FORMS}{fid}/summary/").json()["data"]["fields"]}
    assert by[f["agree"]]["true"] == 1
    assert by[f["agree"]]["false"] == 1
    assert (by[f["age"]]["avg"], by[f["age"]]["min"], by[f["age"]]["max"]) == (25, 20, 30)
    assert by[f["langs"]]["counts"] == {"en": 2, "uz": 1, "ru": 0}


def test_multi_choice_duplicate_selection_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid = director.post(FORMS, {"title": "m"}, format="json").json()["data"]["id"]
    mc = director.post(
        f"{FORMS}{fid}/fields/",
        {"label": "langs", "field_type": "multi_choice", "options": ["en", "uz"]},
        format="json",
    ).json()["data"]["id"]
    director.post(f"{FORMS}{fid}/publish/", {}, format="json")
    r = student.post(
        f"{FORMS}{fid}/submit/", {"answers": [{"field": mc, "value": ["en", "en"]}]}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "field_choice_duplicate"


def test_add_field_validates_options(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    fid = director.post(FORMS, {"title": "o"}, format="json").json()["data"]["id"]

    def add(options):
        return director.post(
            f"{FORMS}{fid}/fields/",
            {"label": "x", "field_type": "single_choice", "options": options},
            format="json",
        )

    assert add(["a", "a"]).json()["code"] == "duplicate_options"
    assert add(["a", "  "]).json()["code"] == "invalid_options"
    assert add([]).json()["code"] == "choice_needs_options"


def test_duplicate_and_unknown_field_ids_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, (f1, f2, _f3) = _build_published_form(director)

    dup = student.post(
        f"{FORMS}{fid}/submit/",
        {"answers": [{"field": f1, "value": "yes"}, {"field": f1, "value": "no"}, {"field": f2, "value": 3}]},
        format="json",
    )
    assert dup.status_code == 400
    assert dup.json()["code"] == "duplicate_field"

    unknown = student.post(
        f"{FORMS}{fid}/submit/",
        {
            "answers": [
                {"field": f1, "value": "yes"},
                {"field": f2, "value": 3},
                {"field": 999999, "value": "x"},
            ]
        },
        format="json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "unknown_field"


@pytest.mark.parametrize("bad_field", [[1], {"a": 1}, "3", 1.5, True])
def test_non_scalar_field_id_is_400_not_500(tenant_a, as_role, bad_field):
    """A non-integer answer 'field' id (list/dict/str/float/bool) must be a clean 400,
    never a 500 (it would otherwise hash-fail against the fields map)."""
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, _fields = _build_published_form(director)
    r = student.post(
        f"{FORMS}{fid}/submit/", {"answers": [{"field": bad_field, "value": "x"}]}, format="json"
    )
    assert r.status_code == 400, r.content


def test_submission_window_enforced(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    now = timezone.now()

    def published_with(window):
        fid = director.post(FORMS, {"title": "w", **window}, format="json").json()["data"]["id"]
        director.post(f"{FORMS}{fid}/fields/", {"label": "q", "field_type": "text"}, format="json")
        director.post(f"{FORMS}{fid}/publish/", {}, format="json")
        return fid

    early = student.post(
        f"{FORMS}{published_with({'opens_at': (now + timedelta(days=1)).isoformat()})}/submit/",
        {"answers": []},
        format="json",
    )
    assert early.status_code == 422
    assert early.json()["code"] == "form_not_open"

    late = student.post(
        f"{FORMS}{published_with({'closes_at': (now - timedelta(days=1)).isoformat()})}/submit/",
        {"answers": []},
        format="json",
    )
    assert late.status_code == 422
    assert late.json()["code"] == "form_closed"


def test_non_builder_cannot_manage_a_form(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, _f = _build_published_form(director)
    # forms:write actions are closed to a forms:read-only responder
    assert (
        student.post(f"{FORMS}{fid}/fields/", {"label": "x", "field_type": "text"}, format="json").status_code
        == 403
    )
    assert student.post(f"{FORMS}{fid}/publish/", {}, format="json").status_code == 403
    assert student.post(f"{FORMS}{fid}/close/", {}, format="json").status_code == 403


def test_cross_branch_builder_isolation(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
    teacher_a = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a))
    teacher_b = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_b))

    fid, _f = _build_published_form(teacher_b, branch=branch_b.id)
    # the other branch's builder cannot read this form's responses or summary
    assert teacher_a.get(f"{FORMS}{fid}/responses/").status_code == 404
    assert teacher_a.get(f"{FORMS}{fid}/summary/").status_code == 404
    # nor create a form pinned to a branch that isn't theirs
    cross = teacher_a.post(FORMS, {"title": "x", "branch": branch_b.id}, format="json")
    assert cross.status_code == 403
    assert cross.json()["code"] == "cross_branch"


def test_creator_bridge_does_not_survive_write_scope_revocation(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a)
    teacher = as_user(tenant_a, teacher_user)
    form_id = teacher.post(
        FORMS,
        {"title": "No creator backdoor", "branch": branch_a.pk},
        format="json",
    ).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=teacher_user, branch=branch_a).delete()
        RoleMembership.objects.create(user=teacher_user, branch=branch_b, role=Role.TEACHER)

    # The bridge creator FK is attribution, not enduring authorization.
    assert teacher.get(f"{FORMS}{form_id}/").status_code == 404
    assert teacher.patch(f"{FORMS}{form_id}/", {"title": "Recovered"}, format="json").status_code == 404


def test_form_creator_uses_exact_principal_and_survives_bridge_deletion(
    tenant_a,
    client_for,
    django_assert_num_queries,
):
    from django.db import DatabaseError, transaction

    from apps.forms.models import Form
    from apps.forms.presenters import form_to_dict
    from apps.forms.repositories.form_repository import FormRepository
    from apps.org.tests.factories import BranchFactory
    from tests.role_principal_helpers import exact_session_client, shared_staff_teacher_bridge

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user, teacher, staff = shared_staff_teacher_bridge(
            branch=branch,
            staff_role=Role.SUPPORT,
        )
        teacher_id = teacher.pk
    teacher_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="teacher",
        principal_id=teacher_id,
    )
    response = teacher_client.post(
        FORMS,
        {"title": "Exact creator", "branch": branch.pk},
        format="json",
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["created_by"] == {
        "kind": "teacher",
        "id": teacher_id,
        "display_name": payload["created_by"]["display_name"],
        "account_label": "Teacher",
    }
    assert payload["created_by"]["display_name"]
    assert payload["created_by_attribution_status"] == "captured"

    with schema_context(tenant_a.schema_name):
        form_id = payload["id"]
        with pytest.raises(DatabaseError), transaction.atomic():
            Form.objects.filter(pk=form_id).update(created_by_principal_id=staff.pk)

        # A raw legacy bridge row is explicitly quarantined and never serialized
        # as either of the two active role accounts.
        legacy = Form.objects.create(title="Ambiguous legacy creator", created_by=user)
        legacy_payload = form_to_dict(legacy)
        assert legacy_payload["created_by"] is None
        assert legacy_payload["created_by_attribution_status"] == "quarantined"

        from apps.teachers.models import TeacherProfile

        TeacherProfile.objects.filter(pk=teacher_id).update(is_active=False)
        exact = Form.objects.get(pk=form_id)
        exact.title = "Historical exact creator"
        exact.save()

        user.delete()
        with django_assert_num_queries(2):
            loaded = FormRepository().get_queryset().get(pk=form_id)
            historical = form_to_dict(loaded)
        assert loaded.created_by_id is None
        assert historical["created_by"] == {
            "kind": "teacher",
            "id": teacher_id,
            "display_name": None,
            "account_label": "Teacher",
        }
        assert historical["created_by_attribution_status"] == "captured"


def test_department_only_form_write_does_not_expand_to_every_branch_form(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=teacher_user, branch=branch).update(department=department)
    teacher = as_user(tenant_a, teacher_user)

    response = teacher.post(
        FORMS,
        {"title": "Needs department attribution", "branch": branch.pk},
        format="json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "cross_branch"


def test_draft_form_can_be_deleted(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    fid = director.post(FORMS, {"title": "Draft"}, format="json").json()["data"]["id"]
    assert director.delete(f"{FORMS}{fid}/").status_code == 204
    assert director.get(f"{FORMS}{fid}/").status_code == 404


def test_published_and_closed_forms_cannot_be_deleted(tenant_a, as_role):
    """A published/closed form holds collected responses — a builder must not be
    able to hard-delete it (would CASCADE the responses away with no audit)."""
    director, _ = as_role(Role.DIRECTOR)
    fid, _fields = _build_published_form(director)
    published_delete = director.delete(f"{FORMS}{fid}/")
    assert published_delete.status_code == 422
    assert published_delete.json()["code"] == "form_not_draft"
    # closing it doesn't make it deletable either
    director.post(f"{FORMS}{fid}/close/", {}, format="json")
    assert director.delete(f"{FORMS}{fid}/").status_code == 422


def test_branch_builder_can_answer_but_not_manage_centre_wide_form(tenant_a, user_in, as_user, as_role):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    fid, (choice, rating, _text) = _build_published_form(director)

    before = {row["id"]: row for row in _rows(teacher.get(FORMS).json())}
    assert fid in before
    assert before[fid]["response_submitted"] is False
    submitted = teacher.post(
        f"{FORMS}{fid}/submit/",
        {
            "answers": [
                {"field": choice, "value": "yes"},
                {"field": rating, "value": 4},
            ]
        },
        format="json",
    )
    assert submitted.status_code == 201, submitted.content
    after = {row["id"]: row for row in _rows(teacher.get(FORMS).json())}
    assert after[fid]["response_submitted"] is True

    # forms:write does not turn centre-wide read access into response/lifecycle
    # management for a branch-scoped builder.
    assert teacher.get(f"{FORMS}{fid}/responses/").status_code == 404
    assert teacher.get(f"{FORMS}{fid}/summary/").status_code == 404
    assert teacher.patch(f"{FORMS}{fid}/", {"title": "hijack"}, format="json").status_code == 404
    assert teacher.post(f"{FORMS}{fid}/close/", {}, format="json").status_code == 404


def test_form_put_is_full_replacement_while_patch_is_sparse(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    created = director.post(
        FORMS,
        {
            "title": "Original",
            "description": "Keep me",
            "is_anonymous": True,
            "allow_multiple": True,
            "audience_roles": [Role.TEACHER],
        },
        format="json",
    ).json()["data"]
    url = f"{FORMS}{created['id']}/"

    patched = director.patch(url, {"title": "Patched"}, format="json")
    assert patched.status_code == 200
    assert patched.json()["data"]["description"] == "Keep me"
    assert patched.json()["data"]["is_anonymous"] is True

    replaced = director.put(url, {"title": "Replacement"}, format="json")
    assert replaced.status_code == 200, replaced.content
    data = replaced.json()["data"]
    assert data["description"] == ""
    assert data["is_anonymous"] is False
    assert data["allow_multiple"] is False
    assert data["audience_roles"] == []
    assert data["audience_user_ids"] == []
    assert data["opens_at"] is None
    assert data["closes_at"] is None
    assert director.put(url, {"description": "missing title"}, format="json").status_code == 400
    assert director.patch(url, {"description": None}, format="json").status_code == 400


def test_form_boolean_parsing_is_strict(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    accepted = director.post(
        FORMS,
        {"title": "Strict bool", "is_anonymous": "on", "allow_multiple": "false"},
        format="json",
    )
    assert accepted.status_code == 201
    assert accepted.json()["data"]["is_anonymous"] is True
    assert accepted.json()["data"]["allow_multiple"] is False
    assert director.post(FORMS, {"title": "Typo", "is_anonymous": "treu"}, format="json").status_code == 400
    assert director.post(FORMS, {"title": "Null", "is_anonymous": None}, format="json").status_code == 400


def test_form_collection_detail_filters_pagination_ordering_and_head(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    alpha = director.post(FORMS, {"title": "Alpha", "is_anonymous": True}, format="json").json()["data"]
    director.post(FORMS, {"title": "Beta"}, format="json")

    filtered = director.get(
        f"{FORMS}?status=draft&is_anonymous=true&search=Alpha&ordering=title&page=1&page_size=1"
    )
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()["data"]] == [alpha["id"]]
    invalid_ordering = director.get(f"{FORMS}?ordering=--created_at")
    assert invalid_ordering.status_code == 400
    assert invalid_ordering.json()["errors"] == {"ordering": ["Choose a declared ordering field."]}
    detail = director.get(f"{FORMS}{alpha['id']}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["title"] == "Alpha"
    assert director.head(FORMS).status_code == 200
    assert director.head(f"{FORMS}{alpha['id']}/").status_code == 200

    field = director.post(
        f"{FORMS}{alpha['id']}/fields/",
        {"label": "Question", "field_type": "text"},
        format="json",
    )
    assert field.status_code == 201
    director.post(f"{FORMS}{alpha['id']}/publish/", {}, format="json")
    assert director.head(f"{FORMS}{alpha['id']}/responses/").status_code == 200
    assert director.head(f"{FORMS}{alpha['id']}/summary/").status_code == 200


def test_field_validation_rejects_blank_junk_options_and_negative_order(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    fid = director.post(FORMS, {"title": "Fields"}, format="json").json()["data"]["id"]
    url = f"{FORMS}{fid}/fields/"
    assert director.post(url, {"label": "   ", "field_type": "text"}, format="json").status_code == 400
    assert (
        director.post(
            url,
            {"label": "Text", "field_type": "text", "options": ["junk"]},
            format="json",
        ).status_code
        == 400
    )
    assert (
        director.post(
            url,
            {"label": "Ordered", "field_type": "text", "order": -1},
            format="json",
        ).status_code
        == 400
    )


def test_archived_membership_branch_cannot_auto_create_centre_wide_form(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        branch.archived_at = timezone.now()
        branch.save(update_fields=["archived_at"])
    teacher = as_user(tenant_a, teacher_user)

    response = teacher.post(FORMS, {"title": "Must stay scoped"}, format="json")
    assert response.status_code == 400
    assert response.json()["code"] == "branch_required"


def test_stale_form_instances_cannot_mutate_or_submit_after_lifecycle_change(tenant_a, as_role):
    from apps.forms.models import Form
    from apps.forms.services import close_form, publish_form, submit_response, update_form
    from core.exceptions import UnprocessableEntity

    director, director_user = as_role(Role.DIRECTOR)
    fid = director.post(FORMS, {"title": "Racy"}, format="json").json()["data"]["id"]
    director.post(
        f"{FORMS}{fid}/fields/",
        {"label": "Question", "field_type": "text"},
        format="json",
    )
    with schema_context(tenant_a.schema_name):
        stale_draft = Form.objects.get(pk=fid)
        publish_form(form=Form.objects.get(pk=fid))

        with pytest.raises(UnprocessableEntity) as update_exc:
            update_form(form=stale_draft, title="Stale overwrite")
        assert update_exc.value.code == "form_not_draft"

        stale_for_submit = Form.objects.get(pk=fid)
        close_form(form=Form.objects.get(pk=fid))
        with pytest.raises(UnprocessableEntity) as submit_exc:
            submit_response(form=stale_for_submit, respondent=director_user, answers=[])
        assert submit_exc.value.code == "form_not_open"


def test_form_summary_query_count_does_not_scale_with_fields_or_answers(
    tenant_a, django_assert_max_num_queries
):
    from apps.forms.models import Form, FormAnswer, FormField, FormResponse
    from apps.forms.services import form_summary

    with schema_context(tenant_a.schema_name):
        form = Form.objects.create(title="Large summary", status=Form.Status.PUBLISHED)
        fields = FormField.objects.bulk_create(
            [
                FormField(form=form, label=f"Rating {i}", field_type=FormField.FieldType.RATING, order=i)
                for i in range(20)
            ]
        )
        responses = FormResponse.objects.bulk_create(
            [
                FormResponse(
                    form=form,
                    respondent_attribution_status=FormResponse.AttributionStatus.ANONYMOUS,
                )
                for _ in range(30)
            ]
        )
        FormAnswer.objects.bulk_create(
            [
                FormAnswer(response=response, field=field, value=4)
                for response in responses
                for field in fields
            ]
        )

        with django_assert_max_num_queries(3):
            summary = form_summary(form)
    assert summary["response_count"] == 30
    assert len(summary["fields"]) == 20
