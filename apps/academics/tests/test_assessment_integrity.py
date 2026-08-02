"""Adversarial tests for the versioned assessment-integrity contract."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from django.db import DatabaseError, IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.academics import services
from apps.academics.models import Exam, ExamLifecycleEvent, ExamResult, Grade
from apps.academics.tests.factories import ExamFactory, ExamTypeFactory
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from core.exceptions import ValidationException

pytestmark = pytest.mark.django_db


def _assessment(*, student_count: int = 1):
    branch = BranchFactory()
    cohort = CohortFactory(branch=branch)
    exam = ExamFactory(cohort=cohort, max_score=Decimal("100"))
    students = [
        StudentProfileFactory(branch=branch, current_cohort=cohort) for _index in range(student_count)
    ]
    for student in students:
        CohortMembershipFactory(cohort=cohort, student=student)
    return exam, students


def _record(exam: Exam, students, *, scores=None, actor=None):
    scores = scores or [Decimal("80")] * len(students)
    return services.record_results(
        exam=exam,
        rows=[
            {"student": student, "score": score, "note": "reviewed"}
            for student, score in zip(students, scores, strict=True)
        ],
        actor=actor,
    )


def test_publication_requires_current_version_confirmation_and_complete_readiness(
    tenant_a,
    user_in,
    as_user,
):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment(student_count=2)
        _record(exam, students[:1], actor=director)
        exam_id = exam.pk

    client = as_user(tenant_a, director)
    readiness = client.get(f"/api/v1/academics/exams/{exam_id}/readiness/")
    assert readiness.status_code == 200
    snapshot = readiness.json()["data"]
    assert snapshot["version"] == 1
    assert snapshot["eligible"] == 2
    assert snapshot["graded"] == 1
    assert snapshot["missing"] == 1
    assert snapshot["excluded"] == 0
    assert snapshot["coverage_fraction"] == 0.5
    assert snapshot["ready"] is False

    missing_contract = client.post(f"/api/v1/academics/exams/{exam_id}/publish/", {}, format="json")
    assert missing_contract.status_code == 400
    refused = client.post(
        f"/api/v1/academics/exams/{exam_id}/publish/",
        {"expected_version": 1, "confirmed": False},
        format="json",
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "publication_confirmation_required"
    incomplete = client.post(
        f"/api/v1/academics/exams/{exam_id}/publish/",
        {"expected_version": 1, "confirmed": True},
        format="json",
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["code"] == "exam_not_ready"

    with schema_context(tenant_a.schema_name):
        exam = Exam.objects.get(pk=exam_id)
        _record(exam, students[1:], actor=director)

    stale = client.post(
        f"/api/v1/academics/exams/{exam_id}/publish/",
        {"expected_version": 9, "confirmed": True},
        format="json",
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "exam_version_conflict"
    published = client.post(
        f"/api/v1/academics/exams/{exam_id}/publish/",
        {"expected_version": 1, "confirmed": True},
        format="json",
    )
    assert published.status_code == 200, published.content
    assert published.json()["data"]["exam"]["is_published"] is True

    with schema_context(tenant_a.schema_name):
        exam = Exam.objects.get(pk=exam_id)
        assert (
            exam.lifecycle_events.filter(
                event_type=ExamLifecycleEvent.EventType.PUBLISHED,
                exam_version=1,
            ).count()
            == 1
        )
        grades = Grade.objects.filter(
            student_id__in=[student.pk for student in students],
            subject=exam.subject,
            term=exam.term,
        )
        assert grades.count() == 2
        assert all(grade.is_valid and grade.is_published for grade in grades)


def test_published_exam_ordinary_edit_delete_and_result_paths_are_locked(
    tenant_a,
    user_in,
    as_user,
):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment()
        _record(exam, students, actor=director)
        exam, _readiness = services.publish_exam(
            exam=exam,
            actor=director,
            expected_version=1,
            confirmed=True,
        )
        exam_id = exam.pk
        student_id = students[0].pk
        student_code = students[0].student_id

    client = as_user(tenant_a, director)
    update = client.patch(
        f"/api/v1/academics/exams/{exam_id}/",
        {"title": "Silent rewrite"},
        format="json",
    )
    assert update.status_code == 409
    assert update.json()["code"] == "exam_locked"
    deleted = client.delete(f"/api/v1/academics/exams/{exam_id}/")
    assert deleted.status_code == 409
    assert deleted.json()["code"] == "exam_locked"
    result = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/",
        [{"student": student_id, "score": "90"}],
        format="json",
    )
    assert result.status_code == 409
    assert result.json()["code"] == "exam_results_locked"
    upload = io.BytesIO(f"student_id,score\n{student_code},90\n".encode())
    upload.name = "results.csv"
    csv_result = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/import-csv/",
        {"file": upload},
        format="multipart",
    )
    assert csv_result.status_code == 409
    assert csv_result.json()["code"] == "exam_results_locked"

    with schema_context(tenant_a.schema_name):
        exam = Exam.objects.get(pk=exam_id)
        assert exam.title != "Silent rewrite"
        assert ExamResult.objects.get(exam=exam, student_id=student_id).score == Decimal("80")


def test_explicit_correction_withdraws_grades_records_public_history_and_republishes(
    tenant_a,
    user_in,
    as_user,
):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment()
        _record(exam, students, actor=director)
        exam, _readiness = services.publish_exam(
            exam=exam,
            actor=director,
            expected_version=1,
            confirmed=True,
        )
        exam_id = exam.pk
        student_code = students[0].student_id

    client = as_user(tenant_a, director)
    corrected = client.post(
        f"/api/v1/academics/exams/{exam_id}/correct/",
        {
            "expected_version": 1,
            "reason": "The source paper was rechecked against the answer key.",
            "changes": {"max_score": "120.00", "weight": "0.750"},
            "results": [{"student_code": student_code, "score": "96.00", "note": "Verified"}],
        },
        format="json",
    )
    assert corrected.status_code == 200, corrected.content
    payload = corrected.json()["data"]
    assert payload["exam"]["version"] == 2
    assert payload["exam"]["is_published"] is False
    assert payload["exam"]["requires_republish"] is True
    event = payload["correction"]
    assert event["exam_version"] == 2
    assert event["actor"] == director.pk
    assert event["reason"].startswith("The source paper")
    change = event["details"]["result_changes"][0]
    assert change["student_code"] == student_code
    assert change["before"]["score"] == "80.00"
    assert change["after"]["score"] == "96.00"

    with schema_context(tenant_a.schema_name):
        grade = Grade.objects.get(student=students[0], subject=exam.subject, term=exam.term)
        assert grade.is_valid is False
        assert grade.is_published is False
        assert grade.invalidation_reason == "exam_correction_pending"

    ordinary_write = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/",
        [{"student_code": student_code, "score": "97"}],
        format="json",
    )
    assert ordinary_write.status_code == 409
    assert ordinary_write.json()["code"] == "exam_results_locked"
    stale = client.post(
        f"/api/v1/academics/exams/{exam_id}/correct/",
        {
            "expected_version": 1,
            "reason": "Stale browser correction.",
            "changes": {"title": "Wrong"},
        },
        format="json",
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "exam_version_conflict"

    history = client.get(f"/api/v1/academics/exams/{exam_id}/history/")
    assert history.status_code == 200
    assert [row["event_type"] for row in history.json()["data"]] == ["corrected", "published"]

    republished = client.post(
        f"/api/v1/academics/exams/{exam_id}/publish/",
        {"expected_version": 2, "confirmed": True},
        format="json",
    )
    assert republished.status_code == 200, republished.content
    assert republished.json()["data"]["exam"]["requires_republish"] is False
    with schema_context(tenant_a.schema_name):
        grade.refresh_from_db()
        assert grade.is_valid is True
        assert grade.is_published is True
        assert grade.value_raw == Decimal("80.000")
        assert Grade.objects.get(pk=grade.pk).components[0]["exam_version"] == 2


def test_correction_rejects_movement_and_maximum_below_recorded_score(
    tenant_a,
    user_in,
    as_user,
):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment()
        _record(exam, students, scores=[Decimal("88")], actor=director)
        exam, _readiness = services.publish_exam(
            exam=exam,
            actor=director,
            expected_version=1,
            confirmed=True,
        )
        other_cohort = CohortFactory(branch=exam.cohort.branch)
        exam_id = exam.pk

    client = as_user(tenant_a, director)
    too_low = client.post(
        f"/api/v1/academics/exams/{exam_id}/correct/",
        {
            "expected_version": 1,
            "reason": "Attempt to shrink the score range.",
            "changes": {"max_score": "80"},
        },
        format="json",
    )
    assert too_low.status_code == 409
    assert too_low.json()["code"] == "max_score_below_result"
    moved = client.post(
        f"/api/v1/academics/exams/{exam_id}/correct/",
        {
            "expected_version": 1,
            "reason": "Attempt to move recorded evidence.",
            "changes": {"cohort": other_cohort.pk},
        },
        format="json",
    )
    assert moved.status_code == 409
    assert moved.json()["code"] == "exam_has_results"


@pytest.mark.parametrize(
    ("score", "note"),
    [
        ("NaN", ""),
        ("Infinity", ""),
        ("1.234", ""),
        ("50", "x" * 256),
    ],
)
def test_json_and_csv_result_validation_have_identical_rejection_rules(
    tenant_a,
    user_in,
    as_user,
    score,
    note,
):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment()
        exam_id = exam.pk
        code = students[0].student_id
    client = as_user(tenant_a, director)
    json_result = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/",
        [{"student_code": code, "score": score, "note": note}],
        format="json",
    )
    assert json_result.status_code == 400

    csv_data = f"student_id,score,note\n{code},{score},{note}\n".encode()
    upload = io.BytesIO(csv_data)
    upload.name = "invalid.csv"
    csv_result = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/import-csv/",
        {"file": upload},
        format="multipart",
    )
    assert csv_result.status_code == 422
    assert csv_result.json()["code"] == "csv_row_errors"
    with schema_context(tenant_a.schema_name):
        assert not ExamResult.objects.filter(exam_id=exam_id).exists()


def test_result_payload_returns_and_accepts_public_student_code(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment()
        exam_id = exam.pk
        code = students[0].student_id
    client = as_user(tenant_a, director)
    response = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/",
        [{"student_code": code, "score": "77.25", "note": "Checked"}],
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["data"]["results"][0]["student_code"] == code
    listing = client.get(f"/api/v1/academics/exams/{exam_id}/results/")
    assert listing.json()["data"][0]["student_code"] == code


def test_database_guards_reject_raw_published_mutation_and_history_rewrite(tenant_a, user_in):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        exam, students = _assessment(student_count=2)
        _record(exam, students, actor=director)
        exam, _readiness = services.publish_exam(
            exam=exam,
            actor=director,
            expected_version=1,
            confirmed=True,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Exam.objects.filter(pk=exam.pk).update(title="Raw rewrite")
        with pytest.raises(IntegrityError), transaction.atomic():
            ExamResult.objects.filter(exam=exam).update(score=Decimal("99"))
        newcomer = StudentProfileFactory(branch=exam.cohort.branch, current_cohort=exam.cohort)
        CohortMembershipFactory(cohort=exam.cohort, student=newcomer)
        with pytest.raises(IntegrityError), transaction.atomic():
            ExamResult.objects.create(exam=exam, student=newcomer, score=Decimal("75"))

        event = exam.lifecycle_events.first()
        assert event is not None
        with pytest.raises(DatabaseError), transaction.atomic():
            ExamLifecycleEvent.objects.filter(pk=event.pk).update(reason="rewritten")
        with pytest.raises(DatabaseError), transaction.atomic():
            ExamLifecycleEvent.objects.filter(pk=event.pk).delete()


def test_catalogue_mutations_are_org_scoped_case_insensitive_and_protect_references(
    tenant_a,
    user_in,
    as_user,
):
    director = user_in(tenant_a, roles=["director"])
    teacher = user_in(tenant_a, roles=["teacher"])
    hod = user_in(tenant_a, roles=["head_of_dept"])
    assert (
        as_user(tenant_a, teacher)
        .post("/api/v1/academics/exam-types/", {"name": "Unauthorized"}, format="json")
        .status_code
        == 403
    )
    assert (
        as_user(tenant_a, hod)
        .post("/api/v1/academics/exam-types/", {"name": "Branch local"}, format="json")
        .status_code
        == 403
    )

    client = as_user(tenant_a, director)
    created = client.post(
        "/api/v1/academics/exam-types/",
        {"name": "Mock Final"},
        format="json",
    )
    assert created.status_code == 201
    duplicate = client.post(
        "/api/v1/academics/exam-types/",
        {"name": "mock final", "slug": "different-slug"},
        format="json",
    )
    assert duplicate.status_code == 400

    subject = client.post(
        "/api/v1/academics/subjects/",
        {"name": "Mathematics", "code": "math"},
        format="json",
    )
    assert subject.status_code == 201
    duplicate_subject = client.post(
        "/api/v1/academics/subjects/",
        {"name": "mathematics", "code": "math-2"},
        format="json",
    )
    assert duplicate_subject.status_code == 400

    with schema_context(tenant_a.schema_name):
        exam_type = ExamTypeFactory()
        ExamFactory(exam_type=exam_type)
        exam_type_id = exam_type.pk
    protected = client.delete(f"/api/v1/academics/exam-types/{exam_type_id}/")
    assert protected.status_code == 409
    assert protected.json()["code"] == "exam_type_in_use"


def test_exact_permission_membership_prevents_read_scope_from_lending_to_write_scope(
    tenant_a,
    as_user,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        read_staff = AccountType.objects.create(
            name="Academic viewer",
            slug="academic-viewer",
            account_kind=AccountType.AccountKind.STAFF,
        )
        write_teacher = AccountType.objects.create(
            name="Assessment marker",
            slug="assessment-marker",
            account_kind=AccountType.AccountKind.TEACHER,
        )
        AccountTypePermission.objects.create(
            account_type=read_staff,
            permission="academics:read",
        )
        AccountTypePermission.objects.create(
            account_type=write_teacher,
            permission="academics:write",
        )
        user = UserFactory()
        RoleMembership.objects.create(
            user=user,
            branch=branch_a,
            role="support",
            account_type=read_staff,
        )
        RoleMembership.objects.create(
            user=user,
            branch=branch_b,
            role="teacher",
            account_type=write_teacher,
        )
        teacher = TeacherProfileFactory(user=user, branch=branch_b)
        cohort_a = CohortFactory(branch=branch_a)
        cohort_b = CohortFactory(branch=branch_b, primary_teacher=teacher)
        exam_a = ExamFactory(cohort=cohort_a, title="Readable only")
        exam_b = ExamFactory(cohort=cohort_b, title="Writable only")
        user.refresh_from_db()

    client = as_user(tenant_a, user)
    listed = client.get("/api/v1/academics/exams/")
    assert {row["id"] for row in listed.json()["data"]} == {exam_a.pk}
    denied = client.patch(
        f"/api/v1/academics/exams/{exam_a.pk}/",
        {"title": "Borrowed scope"},
        format="json",
    )
    assert denied.status_code == 404
    allowed = client.patch(
        f"/api/v1/academics/exams/{exam_b.pk}/",
        {"title": "Scoped marker edit"},
        format="json",
    )
    assert allowed.status_code == 200, allowed.content


def test_csv_byte_ceiling_is_independent_from_generic_upload_limit(tenant_a, monkeypatch):
    with schema_context(tenant_a.schema_name):
        exam, _students = _assessment()
        monkeypatch.setattr(services, "MAX_IMPORT_BYTES", 32)
        upload = io.BytesIO(b"student_id,score,note\n" + b"x" * 64)
        upload.name = "too-large.csv"
        with pytest.raises(ValidationException) as exc:
            services.bulk_grade_import(exam=exam, csv_file=upload)
        assert getattr(exc.value, "code", None) == "file_too_large"
