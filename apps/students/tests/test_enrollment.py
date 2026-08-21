import pytest
from django_tenants.utils import schema_context

from apps.org.models import CenterSettings
from apps.org.services import archive_branch
from apps.org.tests.factories import BranchFactory
from apps.students.models import StudentIdCounter, StudentProfile
from apps.students.services import create_student, transition_enrollment
from apps.students.tests.factories import StudentProfileFactory
from core.exceptions import ValidationException
from core.permissions import Role

pytestmark = pytest.mark.django_db

S = StudentProfile.Status

# Full D1-LD-3 transition table: every legal pair plus representative illegal
# pairs (terminal graduated, skips, and backward edges).
TRANSITION_CASES = [
    (S.LEAD, S.APPLICATION, True),
    (S.APPLICATION, S.ACCEPTED, True),
    (S.ACCEPTED, S.ENROLLED, True),
    (S.ENROLLED, S.ACTIVE, True),
    (S.ACTIVE, S.GRADUATED, True),
    (S.ACTIVE, S.WITHDRAWN, True),
    (S.WITHDRAWN, S.APPLICATION, True),  # withdrawn re-enrolls
    (S.LEAD, S.ACTIVE, False),
    (S.LEAD, S.GRADUATED, False),
    (S.APPLICATION, S.ENROLLED, False),
    (S.ACCEPTED, S.ACTIVE, False),
    (S.ENROLLED, S.GRADUATED, False),
    (S.GRADUATED, S.ACTIVE, False),  # graduated is terminal
    (S.GRADUATED, S.APPLICATION, False),
    (S.WITHDRAWN, S.ACTIVE, False),
    (S.ACTIVE, S.LEAD, False),
]


@pytest.mark.parametrize(("from_status", "to_status", "allowed"), TRANSITION_CASES)
def test_transition_table(tenant_a, from_status, to_status, allowed):
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create(status=from_status)
        if allowed:
            transition_enrollment(student=student, to_status=to_status)
            student.refresh_from_db()
            assert student.status == to_status
            event = student.enrollment_events.get()
            assert (event.from_status, event.to_status) == (from_status, to_status)
        else:
            with pytest.raises(ValidationException) as exc:
                transition_enrollment(student=student, to_status=to_status)
            assert exc.value.code == "invalid_transition"
            student.refresh_from_db()
            assert student.status == from_status
            assert student.enrollment_events.count() == 0


def test_create_student_generates_patterned_id(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = create_student(branch=branch, phone="+998905551001", first_name="A", last_name="B")
        # pattern {CODE}-{YYYY}-{NNNNN}; CODE defaults to the upper-cased schema.
        assert student.student_id.startswith("TENANT_A-")
        assert student.student_id.endswith("00001")


def test_student_id_sequence_increments(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        first = create_student(branch=branch, phone="+998905551002")
        second = create_student(branch=branch, phone="+998905551003")
        assert first.student_id != second.student_id


def test_legal_transition_records_event_and_sets_enrollment_date(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = create_student(branch=branch, phone="+998905551004")
        assert student.status == StudentProfile.Status.LEAD
        transition_enrollment(student=student, to_status=StudentProfile.Status.APPLICATION)
        transition_enrollment(student=student, to_status=StudentProfile.Status.ACCEPTED)
        transition_enrollment(student=student, to_status=StudentProfile.Status.ENROLLED)
        assert student.status == StudentProfile.Status.ENROLLED
        assert student.enrollment_date is not None
        assert student.enrollment_events.count() == 3


def test_create_student_at_active_writes_synthetic_chain(tenant_a):
    """Creating past 'enrolled' must not bypass D1-LD-3: the implied event
    chain is synthesized and enrollment_date is set (seed_dev relies on this)."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = create_student(branch=branch, phone="+998905551006", status=S.ACTIVE)
        assert student.enrollment_date is not None
        events = list(student.enrollment_events.order_by("id"))
        assert [(e.from_status, e.to_status) for e in events] == [
            (S.LEAD, S.APPLICATION),
            (S.APPLICATION, S.ACCEPTED),
            (S.ACCEPTED, S.ENROLLED),
            (S.ENROLLED, S.ACTIVE),
        ]


def test_create_student_as_lead_writes_no_events(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = create_student(branch=branch, phone="+998905551007")
        assert student.enrollment_date is None
        assert student.enrollment_events.count() == 0


def test_create_student_rejects_archived_branch(as_role, tenant_a):
    client, _ = as_role(Role.REGISTRAR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        archive_branch(branch)
    resp = client.post(
        "/api/v1/students/",
        {"branch": branch.id, "phone": "+998905551008"},
        format="json",
    )
    assert resp.status_code == 400
    assert "branch" in resp.json()["errors"]


def test_generate_student_id_rejects_broken_pattern(tenant_a):
    """A bad pattern row (e.g. seeded directly, bypassing the serializer) must
    surface as a 400-class domain error, not an IntegrityError 500."""
    with schema_context(tenant_a.schema_name):
        settings_obj = CenterSettings.load()
        settings_obj.student_id_pattern = "STU-{YYYY}"  # no {NNNNN} counter
        settings_obj.save()
        branch = BranchFactory.create()
        with pytest.raises(ValidationException) as exc:
            create_student(branch=branch, phone="+998905551009")
        assert exc.value.code == "invalid_id_pattern"
        assert StudentIdCounter.objects.count() == 0  # rejected before the counter advanced
