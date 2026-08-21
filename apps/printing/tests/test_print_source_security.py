"""Security regressions for the print-job document capability boundary.

Branch agents receive a presigned object URL when they claim a job.  These tests
prove that the staff API can name only an authorized domain record: storage keys,
routing scope, and cohort attribution are derived by the server and revalidated
immediately before signing.
"""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.printing import services
from apps.printing.models import PrintJob
from apps.printing.tests.factories import PrintJobFactory, attach_trusted_assignment_files
from core.permissions import Role

pytestmark = pytest.mark.django_db

JOBS_URL = "/api/v1/printing/jobs/"
CLAIM_URL = "/api/v1/printing/agent/claim/"


def _agent_client(client_for, tenant, raw_token: str):
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Agent {raw_token}")
    return client


def test_missing_print_source_returns_not_found_without_creating_job(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)

    response = client.post(
        JOBS_URL,
        {"source": "report", "source_id": 9_999_999, "pages": 1},
        format="json",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        assert not PrintJob.objects.exists()


def test_assignment_attachment_is_selected_and_all_routing_is_derived(as_role, tenant_a):
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        assignment = AssignmentFactory(cohort=cohort)
        keys = attach_trusted_assignment_files(
            schema=tenant_a.schema_name,
            assignment=assignment,
            filenames=["first.pdf", "second.pdf"],
        )

    missing_selector = client.post(
        JOBS_URL,
        {"source": "assignment", "source_id": assignment.pk, "pages": 1},
        format="json",
    )
    assert missing_selector.status_code == 400
    assert missing_selector.json()["code"] == "attachment_index_required"
    assert "attachment_index" in missing_selector.json()["errors"]

    response = client.post(
        JOBS_URL,
        {
            "source": "assignment",
            "source_id": assignment.pk,
            "attachment_index": 1,
            "pages": 2,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=response.json()["data"]["id"])
        assert job.payload_s3_key == keys[1]
        assert job.branch_id == branch.pk
        assert job.cohort_id == cohort.pk


def test_assignment_cannot_cross_branch_scope(tenant_a, user_in, as_user):
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory

    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        foreign_branch = BranchFactory(slug="foreign-print-source")
        foreign_cohort = CohortFactory(branch=foreign_branch)
        assignment = AssignmentFactory(
            cohort=foreign_cohort,
            attachments=[f"{tenant_a.schema_name}/assignments/foreign/secret.pdf"],
        )

    response = as_user(tenant_a, manager).post(
        JOBS_URL,
        {"source": "assignment", "source_id": assignment.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_assignment_source_cannot_be_used_to_sign_another_storage_domain(as_role, tenant_a):
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory()
        assignment = AssignmentFactory(
            cohort=cohort,
            # Direct model construction simulates a legacy/corrupt row that bypassed
            # the assignment upload-grant service.
            attachments=[f"{tenant_a.schema_name}/receipts/42.pdf"],
        )

    response = client.post(
        JOBS_URL,
        {"source": "assignment", "source_id": assignment.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "print_source_not_ready"


def test_assignment_source_cannot_borrow_another_assignments_canonical_key(as_role, tenant_a):
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory()
        owner = AssignmentFactory(cohort=cohort)
        borrowed = attach_trusted_assignment_files(
            schema=tenant_a.schema_name,
            assignment=owner,
            filenames=["owned.pdf"],
        )[0]
        poisoned = AssignmentFactory(cohort=cohort, attachments=[borrowed])

    response = client.post(
        JOBS_URL,
        {"source": "assignment", "source_id": poisoned.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "print_source_not_ready"


def test_transcript_requires_completed_canonical_file_and_derives_scope(as_role, tenant_a):
    from apps.academics.models import Transcript
    from apps.cohorts.tests.factories import CohortFactory
    from apps.students.tests.factories import StudentProfileFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(branch=branch, current_cohort=cohort)
        transcript = Transcript.objects.create(student=student)
        expected_key = f"{tenant_a.schema_name}/transcripts/{transcript.pk}.pdf"

    pending = client.post(
        JOBS_URL,
        {"source": "transcript", "source_id": transcript.pk, "pages": 1},
        format="json",
    )
    assert pending.status_code == 422
    assert pending.json()["code"] == "print_source_not_ready"

    with schema_context(tenant_a.schema_name):
        transcript.status = Transcript.Status.DONE
        transcript.pdf_key = f"{tenant_a.schema_name}/transcripts/another.pdf"
        transcript.save(update_fields=["status", "pdf_key"])
    forged = client.post(
        JOBS_URL,
        {"source": "transcript", "source_id": transcript.pk, "pages": 1},
        format="json",
    )
    assert forged.status_code == 422
    assert forged.json()["code"] == "print_source_not_ready"

    with schema_context(tenant_a.schema_name):
        transcript.pdf_key = expected_key
        transcript.save(update_fields=["pdf_key"])
    response = client.post(
        JOBS_URL,
        {"source": "transcript", "source_id": transcript.pk, "pages": 1},
        format="json",
    )
    assert response.status_code == 201, response.content
    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=response.json()["data"]["id"])
        assert job.payload_s3_key == expected_key
        assert job.branch_id == branch.pk
        assert job.cohort_id == cohort.pk


def test_transcript_scope_hides_foreign_branch_from_manager(tenant_a, user_in, as_user):
    from apps.academics.models import Transcript
    from apps.students.tests.factories import StudentProfileFactory

    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        foreign_branch = BranchFactory(slug="foreign-transcript-branch")
        student = StudentProfileFactory(branch=foreign_branch)
        transcript = Transcript.objects.create(
            student=student,
            status=Transcript.Status.DONE,
        )
        transcript.pdf_key = f"{tenant_a.schema_name}/transcripts/{transcript.pk}.pdf"
        transcript.save(update_fields=["pdf_key"])

    response = as_user(tenant_a, manager).post(
        JOBS_URL,
        {"source": "transcript", "source_id": transcript.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("scope_kind", ["none", "tenant", "multiple", "malformed"])
def test_report_requires_one_authoritative_branch_scope(as_role, tenant_a, scope_kind):
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        first = BranchFactory()
        second = BranchFactory()
        params_by_kind = {
            "none": {},
            "tenant": {"_scope_branch_ids": []},
            "multiple": {"_scope_branch_ids": [first.pk, second.pk]},
            "malformed": {"_scope_branch_ids": [str(first.pk)]},
        }
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params=params_by_kind[scope_kind],
        )
        run.s3_key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.save(update_fields=["s3_key"])

    response = client.post(
        JOBS_URL,
        {"source": "report", "source_id": run.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "print_source_not_ready"


def test_report_canonical_key_and_cohort_are_derived(as_role, tenant_a):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [branch.pk], "cohort_id": cohort.pk},
        )
        expected_key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.s3_key = expected_key
        run.save(update_fields=["s3_key"])

    response = client.post(
        JOBS_URL,
        {"source": "report", "source_id": run.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 201, response.content
    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=response.json()["data"]["id"])
        assert job.payload_s3_key == expected_key
        assert job.branch_id == branch.pk
        assert job.cohort_id == cohort.pk


def test_report_cannot_borrow_read_permission_from_another_branch(tenant_a, user_in, as_user):
    """An own historical run does not bypass the current permission-bearing scope."""
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory
    from apps.users.models import RoleMembership

    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        reports_branch = BranchFactory(slug="reports-grant")
        printing_branch = BranchFactory(slug="report-printing-grant")
        RoleMembership.objects.create(user=user, branch=reports_branch, role=Role.HEAD_OF_DEPT)
        RoleMembership.objects.create(user=user, branch=printing_branch, role=Role.REGISTRAR)
        run = ReportRunFactory(
            requested_by=user,
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [printing_branch.pk]},
        )
        run.s3_key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.save(update_fields=["s3_key"])
        user.refresh_from_db()

    response = as_user(tenant_a, user).post(
        JOBS_URL,
        {"source": "report", "source_id": run.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_receipt_uses_trusted_file_field_not_provider_payload(as_role, tenant_a):
    from apps.payments.models import FiscalReceipt, Payment
    from apps.payments.tests.factories import FiscalReceiptFactory, PaymentFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        payment = PaymentFactory(branch_at_payment=branch, status=Payment.Status.COMPLETED)
        expected_key = f"{tenant_a.schema_name}/receipts/{payment.pk}.pdf"
        FiscalReceiptFactory(
            payment=payment,
            status=FiscalReceipt.Status.CONFIRMED,
            pdf_key=expected_key,
            # Deprecated mixed-trust/provider payload must have no authority over
            # which object a branch agent downloads.
            payload={"pdf_key": "another_tenant/private/payroll.pdf"},
        )

    response = client.post(
        JOBS_URL,
        {"source": "receipt", "source_id": payment.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 201, response.content
    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=response.json()["data"]["id"])
        assert job.payload_s3_key == expected_key
        assert job.branch_id == branch.pk
        assert job.cohort_id is None


def test_receipt_rejects_noncanonical_trusted_file(as_role, tenant_a):
    from apps.payments.models import FiscalReceipt, Payment
    from apps.payments.tests.factories import FiscalReceiptFactory, PaymentFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        payment = PaymentFactory(status=Payment.Status.COMPLETED)
        FiscalReceiptFactory(
            payment=payment,
            status=FiscalReceipt.Status.CONFIRMED,
            pdf_key="another_tenant/private/payroll.pdf",
            payload={"pdf_key": f"{tenant_a.schema_name}/receipts/{payment.pk}.pdf"},
        )

    response = client.post(
        JOBS_URL,
        {"source": "receipt", "source_id": payment.pk, "pages": 1},
        format="json",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "print_source_not_ready"


def test_claim_quarantines_forged_legacy_job_without_signing(
    tenant_a,
    client_for,
    monkeypatch,
):
    from apps.printing.views.v1 import printing_views as views
    from apps.reports.models import ReportRun
    from apps.reports.tests.factories import ReportRunFactory

    signed_keys: list[str] = []
    monkeypatch.setattr(
        views,
        "presign_download",
        lambda key, **_kwargs: signed_keys.append(key) or "signed://unexpected",
    )

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        agent, raw_token = services.register_agent(branch_id=branch.pk, name="Secure agent")
        run = ReportRunFactory(
            status=ReportRun.Status.DONE,
            params={"_scope_branch_ids": [branch.pk]},
        )
        canonical_key = f"{tenant_a.schema_name}/reports/{run.pk}.pdf"
        run.s3_key = canonical_key
        run.save(update_fields=["s3_key"])
        job = PrintJobFactory(
            branch=branch,
            source=PrintJob.Source.REPORT,
            source_id=run.pk,
            payload_s3_key=f"{tenant_a.schema_name}/private/payroll.pdf",
            next_attempt_at=timezone.now(),
        )

    response = _agent_client(client_for, tenant_a, raw_token).post(CLAIM_URL)

    assert response.status_code == 409
    assert response.json()["code"] == "print_source_invalid"
    assert signed_keys == []
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.FAILED
        assert job.attempts == services.MAX_ATTEMPTS
        assert job.last_error == "invalid_print_source"
        assert job.agent_id == agent.pk
        assert job.next_attempt_at is None
        assert job.finished_at is not None


def test_claim_never_signs_a_canonical_key_borrowed_from_another_assignment(
    tenant_a,
    client_for,
    monkeypatch,
):
    from apps.assignments.tests.factories import AssignmentFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.printing.views.v1 import printing_views as views

    signed_keys: list[str] = []
    monkeypatch.setattr(
        views,
        "presign_download",
        lambda key, **_kwargs: signed_keys.append(key) or "signed://unexpected",
    )

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        owner = AssignmentFactory(cohort=cohort)
        borrowed = attach_trusted_assignment_files(
            schema=tenant_a.schema_name,
            assignment=owner,
            filenames=["owned.pdf"],
        )[0]
        poisoned = AssignmentFactory(cohort=cohort, attachments=[borrowed])
        _agent, raw_token = services.register_agent(branch_id=branch.pk, name="Secure agent")
        job = PrintJobFactory(
            branch=branch,
            source=PrintJob.Source.ASSIGNMENT,
            source_id=poisoned.pk,
            payload_s3_key=borrowed,
            cohort_id=cohort.pk,
            next_attempt_at=timezone.now(),
        )

    response = _agent_client(client_for, tenant_a, raw_token).post(CLAIM_URL)

    assert response.status_code == 409
    assert response.json()["code"] == "print_source_invalid"
    assert signed_keys == []
    with schema_context(tenant_a.schema_name):
        job.refresh_from_db()
        assert job.status == PrintJob.Status.FAILED
