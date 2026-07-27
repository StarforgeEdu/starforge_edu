"""Academics lane tests (D2-C): weighted grade math + scheme display, CSV import
atomicity, publication gating, transcript task lifecycle, the grade-changed
signal, honor-roll knob, scoping, cross-tenant, and query budgets."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django_tenants.utils import schema_context

from apps.academics import selectors, services
from apps.academics.grading import display_for
from apps.academics.models import ExamResult, Transcript
from apps.academics.signals import grade_changed
from apps.academics.tests.factories import (
    ExamFactory,
    ExamTypeFactory,
    GradeFactory,
    SubjectFactory,
)
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.org.models import CenterSettings
from apps.org.tests.factories import BranchFactory
from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
from apps.schedule.tests.factories import TermFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.exceptions import UnprocessableEntity

pytestmark = pytest.mark.django_db


def _set_scheme(scheme: str) -> None:
    from django.core.cache import cache

    settings = CenterSettings.load()
    settings.grading_scheme = scheme
    settings.save(update_fields=["grading_scheme"])
    cache.clear()


def _three_weighted_exams(*, subject, cohort, term, student, scores, weights, published=(True, True, True)):
    """Create 3 published-by-default exams (max 100) with `weights` and record
    `scores` for `student`. Returns the exams."""
    exams = []
    for i, (score, weight, pub) in enumerate(zip(scores, weights, published, strict=True)):
        exam: Any = ExamFactory(
            subject=subject,
            cohort=cohort,
            term=term,
            title=f"E{i}",
            max_score=Decimal("100"),
            weight=Decimal(weight),
            is_published=pub,
        )
        ExamResult.objects.create(exam=exam, student=student, score=Decimal(score))
        exams.append(exam)
    return exams


# --------------------------------------------------------------------------- #
# grade math + display scheme
# --------------------------------------------------------------------------- #


def test_weighted_term_grade_fixture(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        subject = SubjectFactory()
        cohort = CohortFactory(branch=branch)
        term: Any = TermFactory()
        student: Any = StudentProfileFactory(branch=branch)
        # weights .2/.3/.5, scores 90/80/100 → 100*(.18+.24+.50) = 92.000
        _three_weighted_exams(
            subject=subject,
            cohort=cohort,
            term=term,
            student=student,
            scores=("90", "80", "100"),
            weights=(".2", ".3", ".5"),
        )
        grade = services.compute_term_grade(student=student, subject=subject, term=term)
        assert grade is not None
        assert grade.value_raw == Decimal("92.000")
        assert grade.value_display == "92.0"
        assert len(grade.components) == 3


def test_unpublished_exam_excluded(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        subject = SubjectFactory()
        cohort = CohortFactory(branch=branch)
        term: Any = TermFactory()
        student: Any = StudentProfileFactory(branch=branch)
        # 3rd exam (the 100, weight .5) is UNPUBLISHED → drops out. Remaining
        # weights .2/.3 → 100*(.18+.24)/.5 = 84.000.
        _three_weighted_exams(
            subject=subject,
            cohort=cohort,
            term=term,
            student=student,
            scores=("90", "80", "100"),
            weights=(".2", ".3", ".5"),
            published=(True, True, False),
        )
        grade = services.compute_term_grade(student=student, subject=subject, term=term)
        assert grade is not None
        assert grade.value_raw == Decimal("84.000")
        assert len(grade.components) == 2


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [("percentage", "92.0"), ("letter", "A"), ("gpa", "3.68")],
)
def test_value_display_percentage_letter_gpa(tenant_a, scheme, expected):
    with schema_context(tenant_a.schema_name):
        _set_scheme(scheme)
        branch = BranchFactory()
        subject = SubjectFactory()
        cohort = CohortFactory(branch=branch)
        term: Any = TermFactory()
        student: Any = StudentProfileFactory(branch=branch)
        _three_weighted_exams(
            subject=subject,
            cohort=cohort,
            term=term,
            student=student,
            scores=("90", "80", "100"),
            weights=(".2", ".3", ".5"),
        )
        grade = services.compute_term_grade(student=student, subject=subject, term=term)
        assert grade is not None
        assert grade.value_display == expected


def test_display_for_letter_bands_pure():
    assert display_for(Decimal("90"), "letter") == "A"
    assert display_for(Decimal("89.99"), "letter") == "B"
    assert display_for(Decimal("60"), "letter") == "D"
    assert display_for(Decimal("59.9"), "letter") == "F"


# --------------------------------------------------------------------------- #
# exam results — validation + grade_changed signal
# --------------------------------------------------------------------------- #


def test_record_results_score_out_of_range_422(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam: Any = ExamFactory(max_score=Decimal("100"))
        student: Any = StudentProfileFactory(branch=branch)
        with pytest.raises(UnprocessableEntity) as exc:
            services.record_results(
                exam=exam, rows=[{"student": student, "score": Decimal("101")}], actor=None
            )
        assert exc.value.code == "score_out_of_range"
        assert "0" in (exc.value.fields or {})


def test_record_results_rejects_student_outside_exam_cohort(tenant_a):
    """A student not enrolled in the exam's cohort cannot be graded (422), even
    though ResultEntrySerializer.student is an unscoped StudentProfile queryset."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam: Any = ExamFactory(max_score=Decimal("100"))
        outsider: Any = StudentProfileFactory(branch=branch)  # NOT enrolled in exam.cohort
        with pytest.raises(UnprocessableEntity) as exc:
            services.record_results(
                exam=exam, rows=[{"student": outsider, "score": Decimal("50")}], actor=None
            )
        assert exc.value.code == "student_not_in_cohort"
        assert ExamResult.objects.filter(exam=exam).count() == 0


def test_grade_changed_emitted_once_on_overwrite(tenant_a, django_capture_on_commit_callbacks):
    received: list[dict] = []

    def _recv(sender, **kwargs):
        received.append(kwargs)

    grade_changed.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            exam: Any = ExamFactory(max_score=Decimal("100"))
            student: Any = StudentProfileFactory(branch=branch)
            CohortMembershipFactory(cohort=exam.cohort, student=student)

            with django_capture_on_commit_callbacks(execute=True):
                services.record_results(
                    exam=exam, rows=[{"student": student, "score": Decimal("70")}], actor=None
                )
            assert received == []  # first entry does NOT emit

            with django_capture_on_commit_callbacks(execute=True):
                services.record_results(
                    exam=exam, rows=[{"student": student, "score": Decimal("88")}], actor=None
                )
            assert len(received) == 1  # overwrite emits exactly once
            assert received[0]["old_score"] == Decimal("70")
            assert received[0]["new_score"] == Decimal("88")
    finally:
        grade_changed.disconnect(_recv)


def test_grade_changed_not_emitted_on_identical_reentry(tenant_a, django_capture_on_commit_callbacks):
    """Re-POSTing the SAME score is a no-op and must emit zero signals (no audit
    churn for D3-D)."""
    received: list[dict] = []

    def _recv(sender, **kwargs):
        received.append(kwargs)

    grade_changed.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            exam: Any = ExamFactory(max_score=Decimal("100"))
            student: Any = StudentProfileFactory(branch=branch)
            CohortMembershipFactory(cohort=exam.cohort, student=student)

            with django_capture_on_commit_callbacks(execute=True):
                services.record_results(
                    exam=exam, rows=[{"student": student, "score": Decimal("70")}], actor=None
                )
            assert received == []  # first entry does NOT emit

            # Re-enter the identical score → updated (was_created False) but no change.
            with django_capture_on_commit_callbacks(execute=True):
                result = services.record_results(
                    exam=exam, rows=[{"student": student, "score": Decimal("70")}], actor=None
                )
            assert result["updated"] == 1  # update_or_create still touched the row
            assert received == []  # ...but no grade_changed because score is unchanged
    finally:
        grade_changed.disconnect(_recv)


# --------------------------------------------------------------------------- #
# CSV import
# --------------------------------------------------------------------------- #


def test_csv_import_atomic_and_row_errors(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam: Any = ExamFactory(max_score=Decimal("100"))
        s1: Any = StudentProfileFactory(branch=branch)
        s2: Any = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=exam.cohort, student=s1)
        CohortMembershipFactory(cohort=exam.cohort, student=s2)

        # One unknown student_id + one out-of-range score → 422, zero written.
        bad = io.BytesIO(
            f"student_id,score,note\n{s1.student_id},80,ok\nNOPE-99999,75,\n{s2.student_id},150,\n".encode()
        )
        bad.name = "bad.csv"
        with pytest.raises(UnprocessableEntity) as exc:
            services.bulk_grade_import(exam=exam, csv_file=bad, actor=None)
        assert exc.value.code == "csv_row_errors"
        bad_rows = {e["row"] for e in (exc.value.fields or {})["rows"]}
        assert bad_rows == {3, 4}  # header is line 1
        assert ExamResult.objects.filter(exam=exam).count() == 0

        # Clean CSV → all rows imported.
        good = io.BytesIO(f"student_id,score\n{s1.student_id},80\n{s2.student_id},90\n".encode())
        good.name = "good.csv"
        result = services.bulk_grade_import(exam=exam, csv_file=good, actor=None)
        assert result["created"] == 2
        assert ExamResult.objects.filter(exam=exam).count() == 2


def test_csv_import_nan_score_is_row_error_not_500(tenant_a):
    """Decimal("NaN") parses without raising, but a NaN comparison would raise
    InvalidOperation (an uncaught 500). It must be reported as a bad row (422)."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam: Any = ExamFactory(max_score=Decimal("100"))
        s1: Any = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=exam.cohort, student=s1)
        nan_csv = io.BytesIO(f"student_id,score\n{s1.student_id},NaN\n".encode())
        nan_csv.name = "nan.csv"
        with pytest.raises(UnprocessableEntity) as exc:
            services.bulk_grade_import(exam=exam, csv_file=nan_csv, actor=None)
        assert exc.value.code == "csv_row_errors"
        assert ExamResult.objects.filter(exam=exam).count() == 0


def test_csv_import_non_utf8_is_400_not_500(tenant_a):
    """A Latin-1 export (byte 0xE9 = 'é') would raise an uncaught UnicodeDecodeError;
    it must surface as a clean 400 bad_encoding."""
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        exam: Any = ExamFactory(max_score=Decimal("100"))
        latin1 = io.BytesIO("student_id,score,note\nX,80,caf\xe9\n".encode("latin-1"))
        latin1.name = "latin1.csv"
        with pytest.raises(ValidationException) as exc:
            services.bulk_grade_import(exam=exam, csv_file=latin1, actor=None)
        assert exc.value.code == "bad_encoding"


# --------------------------------------------------------------------------- #
# transcript task lifecycle
# --------------------------------------------------------------------------- #


def test_transcript_task_lifecycle_idempotent(tenant_a, monkeypatch):
    uploads: list[tuple[str, bytes]] = []

    def fake_render(transcript):
        return b"%PDF-1.5\nfake transcript bytes"

    def fake_upload(key, data, *, content_type="application/octet-stream"):
        uploads.append((key, data))
        return key

    monkeypatch.setattr(services, "render_transcript_pdf", fake_render)
    monkeypatch.setattr(services, "upload_bytes", fake_upload)

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student: Any = StudentProfileFactory(branch=branch)
        transcript: Any = Transcript.objects.create(student=student)

        key = services.generate_transcript(transcript.id)
        transcript.refresh_from_db()
        assert transcript.status == Transcript.Status.DONE
        assert key == f"{tenant_a.schema_name}/transcripts/{transcript.id}.pdf"
        assert transcript.pdf_key == key
        assert uploads[0][1].startswith(b"%PDF")

        # Re-run is idempotent: done short-circuits, no second upload.
        services.generate_transcript(transcript.id)
        assert len(uploads) == 1


def test_transcript_post_returns_202_pending(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student: Any = StudentProfileFactory(branch=branch)
        student_id = student.id

    resp = as_user(tenant_a, director).post(
        "/api/v1/academics/transcripts/", {"student": student_id}, format="json"
    )
    assert resp.status_code == 202
    body = resp.json()["data"]
    assert body["status"] == "pending"
    with schema_context(tenant_a.schema_name):
        assert Transcript.objects.filter(pk=body["id"], status="pending").exists()


def test_transcript_request_hides_student_existence_from_unauthorized_reader(
    tenant_a, user_in, as_user
):
    viewer = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        target: Any = StudentProfileFactory()
        existing_id = target.pk
        missing_id = target.pk + 100_000

    client = as_user(tenant_a, viewer)
    existing = client.post(
        "/api/v1/academics/transcripts/",
        {"student": existing_id},
        format="json",
    )
    missing = client.post(
        "/api/v1/academics/transcripts/",
        {"student": missing_id},
        format="json",
    )

    assert existing.status_code == missing.status_code == 404
    assert existing.json()["code"] == missing.json()["code"] == "not_found"


# weasyprint needs GTK native libs (cairo/pango) absent on the Windows dev box;
# this runs the REAL renderer on CI/Linux and skips locally.
try:  # pragma: no cover - import probe
    import weasyprint  # noqa: F401

    _HAS_WEASYPRINT = True
except Exception:  # OSError too when native libs are missing
    _HAS_WEASYPRINT = False


@pytest.mark.skipif(not _HAS_WEASYPRINT, reason="weasyprint native libs unavailable (CI/Linux runs it)")
def test_weasyprint_renders_real_pdf(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student: Any = StudentProfileFactory(branch=branch)
        transcript: Any = Transcript.objects.create(student=student)
        pdf = services.render_transcript_pdf(transcript)
        assert pdf.startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# publication gating + scoping
# --------------------------------------------------------------------------- #


def test_publication_gating_parent_student_teacher(tenant_a, user_in, as_user):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    student_user = user_in(tenant_a, roles=["student"])
    parent_user = user_in(tenant_a, roles=["parent"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        subject = SubjectFactory()
        term = TermFactory()

        my_student: Any = StudentProfileFactory(user=student_user, branch=branch)
        CohortMembershipFactory(cohort=cohort, student=my_student)
        parent_profile = ParentProfileFactory(user=parent_user)
        GuardianFactory(parent=parent_profile, student=my_student)

        published: Any = GradeFactory(
            student=my_student, subject=subject, term=term, is_published=True, value_display="A"
        )
        draft: Any = GradeFactory(
            student=my_student,
            subject=SubjectFactory(),
            term=term,
            is_published=False,
            value_display="B",
        )

        # A different student's published grade — must never be visible to ours.
        other: Any = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=other)
        GradeFactory(student=other, subject=subject, term=term, is_published=True)
        published_id, draft_id = published.id, draft.id

    student_body = as_user(tenant_a, student_user).get("/api/v1/academics/grades/").json()
    assert {g["id"] for g in student_body["data"]} == {published_id}

    parent_body = as_user(tenant_a, parent_user).get("/api/v1/academics/grades/").json()
    assert {g["id"] for g in parent_body["data"]} == {published_id}

    # Teacher of the cohort sees BOTH published + draft for their students.
    teacher_body = as_user(tenant_a, teacher_user).get("/api/v1/academics/grades/").json()
    teacher_ids = {g["id"] for g in teacher_body["data"]}
    assert published_id in teacher_ids
    assert draft_id in teacher_ids


def test_teacher_grade_read_allowed_matrix(tenant_a, as_role):
    """Day-1 asymmetry fix: TEACHER now has academics:read (D2-C-7)."""
    from core.permissions import Role

    client, _ = as_role(Role.TEACHER)
    resp = client.get("/api/v1/academics/grades/")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# exam write-path cohort scoping (D2-C-? — teacher can only touch own cohort)
# --------------------------------------------------------------------------- #


def test_exam_results_write_path_teacher_cohort_scoped(tenant_a, user_in, as_user):
    """A teacher of cohort A cannot reach cohort B's exam: GET/POST on the
    results / import-csv / publish actions 404 (via get_object()), while the
    owning-cohort teacher and a director succeed."""
    owner_teacher_user = user_in(tenant_a, roles=["teacher"])
    other_teacher_user = user_in(tenant_a, roles=["teacher"])
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        owner_profile = TeacherProfileFactory(user=owner_teacher_user, branch=branch)
        other_profile = TeacherProfileFactory(user=other_teacher_user, branch=branch)
        owned_cohort = CohortFactory(branch=branch, name="Owned", primary_teacher=owner_profile)
        # The other teacher teaches an UNRELATED cohort (so they hold the role but
        # not access to the exam under test).
        CohortFactory(branch=branch, name="Other", primary_teacher=other_profile)

        subject = SubjectFactory()
        term: Any = TermFactory()
        exam: Any = ExamFactory(subject=subject, cohort=owned_cohort, term=term, max_score=Decimal("100"))
        student: Any = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=owned_cohort, student=student)
        exam_id, student_id = exam.id, student.id

    base = f"/api/v1/academics/exams/{exam_id}"
    rows = [{"student": student_id, "score": "85"}]

    # --- out-of-cohort teacher: 404 on every write action ---
    other = as_user(tenant_a, other_teacher_user)
    assert other.get(f"{base}/results/").status_code == 404
    assert other.post(f"{base}/results/", rows, format="json").status_code == 404
    csv_payload = io.BytesIO(b"student_id,score\nX,85\n")
    csv_payload.name = "r.csv"
    assert (
        other.post(f"{base}/results/import-csv/", {"file": csv_payload}, format="multipart").status_code
        == 404
    )
    assert other.post(f"{base}/publish/").status_code == 404

    # --- owning-cohort teacher: results action succeeds ---
    owner = as_user(tenant_a, owner_teacher_user)
    assert owner.get(f"{base}/results/").status_code == 200
    assert owner.post(f"{base}/results/", rows, format="json").status_code == 200

    # --- director (staff): tenant-wide, publish succeeds ---
    director_client = as_user(tenant_a, director)
    assert director_client.get(f"{base}/results/").status_code == 200
    assert director_client.post(f"{base}/publish/").status_code == 200
    # Empty body is a 400 contract error (parity with the old DRF ParseError); an
    # explicit empty JSON array [] is a valid no-op (200).
    assert (
        director_client.post(f"{base}/results/", data="", content_type="application/json").status_code == 400
    )
    assert director_client.post(f"{base}/results/", [], format="json").status_code == 200


def test_json_results_array_is_capped(tenant_a, user_in, as_user, monkeypatch):
    """R3/PLAUS1: the JSON results array must be bounded like the CSV import (which
    caps at MAX_IMPORT_ROWS) — an uncapped array means a per-row student lookup + a
    per-row upsert inside one long transaction. Cap lowered so a tiny array trips it."""
    from apps.academics import services as academics_services

    monkeypatch.setattr(academics_services, "MAX_IMPORT_ROWS", 2)
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam: Any = ExamFactory(
            subject=SubjectFactory(), cohort=CohortFactory(branch=branch), term=TermFactory(),
            max_score=Decimal("100"),
        )
        students = [StudentProfileFactory(branch=branch).id for _ in range(3)]
    rows = [{"student": sid, "score": "80"} for sid in students]  # 3 rows > cap of 2
    resp = as_user(tenant_a, director).post(
        f"/api/v1/academics/exams/{exam.id}/results/", rows, format="json"
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"


def test_exam_list_teacher_cohort_scoped(tenant_a, user_in, as_user):
    """List is scoped too: a teacher sees only exams of cohorts they teach."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        mine = CohortFactory(branch=branch, name="Mine", primary_teacher=profile)
        theirs = CohortFactory(branch=branch, name="Theirs")
        term: Any = TermFactory()
        my_exam: Any = ExamFactory(cohort=mine, term=term)
        ExamFactory(cohort=theirs, term=term)
        my_exam_id = my_exam.id

    body = as_user(tenant_a, teacher_user).get("/api/v1/academics/exams/").json()
    assert {e["id"] for e in body["data"]} == {my_exam_id}


# --------------------------------------------------------------------------- #
# honor roll knob
# --------------------------------------------------------------------------- #


def test_honor_roll_knob_flip(tenant_a):
    from django.core.cache import cache

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        term: Any = TermFactory()
        student: Any = StudentProfileFactory(branch=branch)
        GradeFactory(
            student=student,
            subject=SubjectFactory(),
            term=term,
            value_raw=Decimal("92"),
            is_published=True,
        )

        assert selectors.honor_roll(term_id=term.id).count() == 1  # default min 90

        settings = CenterSettings.load()
        settings.honor_roll_min = Decimal("95")
        settings.save(update_fields=["honor_roll_min"])
        cache.clear()
        assert selectors.honor_roll(term_id=term.id).count() == 0  # 92 < 95


def test_honor_roll_endpoint_staff_only(tenant_a, user_in, as_user):
    student_user = user_in(tenant_a, roles=["student"])
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        term: Any = TermFactory()
        term_id = term.id

    # Student holds academics:read but honor roll is staff-only.
    student_resp = as_user(tenant_a, student_user).get(f"/api/v1/academics/honor-roll/?term={term_id}")
    assert student_resp.status_code == 403
    director_resp = as_user(tenant_a, director).get(f"/api/v1/academics/honor-roll/?term={term_id}")
    assert director_resp.status_code == 200


def test_honor_roll_and_warnings_teacher_cohort_scoped(tenant_a, user_in, as_user):
    """Honor-roll / warnings mirror grade scoping for TEACHER: a teacher sees only
    students in cohorts they teach; a director stays tenant-wide."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        my_cohort = CohortFactory(branch=branch, name="Mine", primary_teacher=profile)
        other_cohort = CohortFactory(branch=branch, name="Other")
        subject = SubjectFactory()
        term: Any = TermFactory()

        # Honor-roll candidate (>=90) in MY cohort, and one in another cohort.
        mine = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=my_cohort, student=mine)
        mine_grade: Any = GradeFactory(
            student=mine, subject=subject, term=term, value_raw=Decimal("95"), is_published=True
        )
        theirs = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=other_cohort, student=theirs)
        GradeFactory(student=theirs, subject=subject, term=term, value_raw=Decimal("96"), is_published=True)

        # Warning candidates (<=60), one per cohort.
        mine_warn = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=my_cohort, student=mine_warn)
        mine_warn_grade: Any = GradeFactory(
            student=mine_warn, subject=subject, term=term, value_raw=Decimal("55"), is_published=True
        )
        theirs_warn = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=other_cohort, student=theirs_warn)
        GradeFactory(
            student=theirs_warn, subject=subject, term=term, value_raw=Decimal("50"), is_published=True
        )

        term_id = term.id
        mine_grade_id, mine_warn_id = mine_grade.id, mine_warn_grade.id

    teacher = as_user(tenant_a, teacher_user)
    director_client = as_user(tenant_a, director)

    honor = teacher.get(f"/api/v1/academics/honor-roll/?term={term_id}").json()["data"]
    assert {g["id"] for g in honor} == {mine_grade_id}  # teacher: own cohort only
    warn = teacher.get(f"/api/v1/academics/warnings/?term={term_id}").json()["data"]
    assert {g["id"] for g in warn} == {mine_warn_id}

    # Director sees the whole tenant (both cohorts).
    director_honor = director_client.get(f"/api/v1/academics/honor-roll/?term={term_id}").json()["data"]
    assert len(director_honor) == 2
    director_warn = director_client.get(f"/api/v1/academics/warnings/?term={term_id}").json()["data"]
    assert len(director_warn) == 2


# --------------------------------------------------------------------------- #
# API gating, cross-tenant, query budgets
# --------------------------------------------------------------------------- #


def test_exam_create_requires_write(tenant_a, as_role):
    from core.permissions import Role

    client, _ = as_role(Role.STUDENT)
    resp = client.post("/api/v1/academics/exams/", {}, format="json")
    assert resp.status_code == 403


def test_teacher_cannot_create_exam_in_non_taught_cohort(tenant_a, user_in, as_user):
    """Write-path scoping (mirrors the assignment finding): a teacher with
    academics:write may NOT POST an exam into a cohort they don't teach. The
    out-of-scope cohort PK is filtered from the serializer queryset → 400; the
    teacher's own cohort succeeds (201)."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        own_cohort: Any = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        foreign_cohort: Any = CohortFactory(branch=branch)
        subject = SubjectFactory()
        term: Any = TermFactory()
        exam_type = ExamTypeFactory()
        own_id, foreign_id = own_cohort.id, foreign_cohort.id
        subject_id, term_id, exam_type_id = subject.id, term.id, exam_type.id

    client = as_user(tenant_a, teacher_user)
    base = {
        "subject": subject_id,
        "term": term_id,
        "exam_type": exam_type_id,
        "title": "Midterm",
        "exam_date": date(2026, 3, 1).isoformat(),
        "max_score": "100",
        "weight": "1.0",
    }

    foreign = client.post("/api/v1/academics/exams/", {**base, "cohort": foreign_id}, format="json")
    assert foreign.status_code == 400  # cohort not in the teacher's writable queryset

    ok = client.post("/api/v1/academics/exams/", {**base, "cohort": own_id}, format="json")
    assert ok.status_code == 201
    data = ok.json()["data"]
    assert data["cohort"] == own_id
    # The exam carries its per-Center type as both an id and an expanded object.
    assert data["exam_type"] == exam_type_id
    assert data["exam_type_detail"]["id"] == exam_type_id


def test_manager_creates_and_lists_exam_types(as_role):
    """Per-Center configurable exam kinds: a manager creates one (name auto-slugs),
    a duplicate slug is rejected, and it shows up in the list."""
    from core.permissions import Role

    director, _ = as_role(Role.DIRECTOR)
    created = director.post(
        "/api/v1/academics/exam-types/", {"name": "Mock IELTS", "color": "#3b82f6"}, format="json"
    )
    assert created.status_code == 201, created.content
    body = created.json()["data"]
    assert body["slug"] == "mock-ielts"  # auto-derived from the label
    assert body["name"] == "Mock IELTS"

    dup = director.post("/api/v1/academics/exam-types/", {"name": "Mock IELTS"}, format="json")
    assert dup.status_code == 400  # slug collision

    listing = director.get("/api/v1/academics/exam-types/")
    assert listing.status_code == 200
    assert any(t["slug"] == "mock-ielts" for t in listing.json()["data"])


def test_student_cannot_create_exam_type(as_role):
    from core.permissions import Role

    student, _ = as_role(Role.STUDENT)
    resp = student.post("/api/v1/academics/exam-types/", {"name": "X"}, format="json")
    assert resp.status_code == 403


def test_teacher_cannot_recompute_grades_for_non_taught_cohort(tenant_a, user_in, as_user):
    """Authz parity with the exam write-path (security finding): a TEACHER holding
    academics:write may NOT recompute/publish grades for a cohort they don't teach —
    otherwise they could force-publish another cohort's (or another branch's) grades.
    Their own cohort is allowed."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        own_cohort: Any = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        foreign_cohort: Any = CohortFactory(branch=branch)
        subject = SubjectFactory()
        term: Any = TermFactory()
        own_id, foreign_id = own_cohort.id, foreign_cohort.id
        subject_id, term_id = subject.id, term.id

    client = as_user(tenant_a, teacher_user)
    body = {"subject": subject_id, "term": term_id, "publish": True}

    foreign = client.post(
        "/api/v1/academics/grades/recompute/", {**body, "cohort": foreign_id}, format="json"
    )
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "forbidden"

    ok = client.post(
        "/api/v1/academics/grades/recompute/", {**body, "cohort": own_id}, format="json"
    )
    assert ok.status_code == 200


def test_grades_cross_tenant_isolated(tenant_a, tenant_b, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student: Any = StudentProfileFactory(branch=branch)
        GradeFactory(student=student, subject=SubjectFactory(), term=TermFactory(), is_published=True)

    director_b = user_in(tenant_b, roles=["director"])
    body = as_user(tenant_b, director_b).get("/api/v1/academics/grades/").json()
    assert body["pagination"]["total"] == 0


def test_grades_list_query_budget(tenant_a, user_in, as_user, django_assert_max_num_queries):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        term = TermFactory()
        for _ in range(5):
            student = StudentProfileFactory(branch=branch)
            GradeFactory(student=student, subject=SubjectFactory(), term=term, is_published=True)

    client = as_user(tenant_a, director)
    with django_assert_max_num_queries(8):
        body = client.get("/api/v1/academics/grades/").json()
    assert set(body) == {"success", "data", "pagination"}
    assert body["pagination"]["total"] == 5


def test_exams_list_query_budget(tenant_a, user_in, as_user, django_assert_max_num_queries):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        term = TermFactory()
        for i in range(5):
            ExamFactory(
                subject=SubjectFactory(),
                cohort=cohort,
                term=term,
                title=f"E{i}",
                exam_date=date(2026, 3, 1 + i),
            )

    client = as_user(tenant_a, director)
    with django_assert_max_num_queries(9):  # +1: A-2 per-request permission-override load
        body = client.get("/api/v1/academics/exams/").json()
    assert body["pagination"]["total"] == 5
