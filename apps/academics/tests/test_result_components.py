"""Bounded skill-component evidence for exam results."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.academics import services
from apps.academics.dto import ResultFieldError, validate_result_values
from apps.academics.models import ExamResult
from apps.academics.tests.factories import ExamFactory
from apps.cohorts.tests.factories import CohortMembershipFactory
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def _components():
    return [
        {"name": " Listening ", "score": "67", "max_score": "100"},
        {"name": "Speaking", "score": 18.5, "max_score": 25},
    ]


def test_component_validator_normalizes_without_deriving_an_overall_score():
    values = validate_result_values(
        score="81",
        max_score=Decimal("100"),
        components=_components(),
    )
    assert values.score == Decimal("81")
    assert values.components == (
        {"name": "Listening", "score": "67", "max_score": "100"},
        {"name": "Speaking", "score": "18.5", "max_score": "25"},
    )


@pytest.mark.parametrize(
    "components",
    [
        "not-an-array",
        [{"name": "Listening", "score": 5}],
        [{"name": "Listening", "score": 5, "max_score": 10, "extra": True}],
        [{"name": "Listening", "score": -1, "max_score": 10}],
        [{"name": "Listening", "score": 11, "max_score": 10}],
        [{"name": "Listening", "score": 1, "max_score": 0}],
        [{"name": "Listening", "score": "NaN", "max_score": 10}],
        [
            {"name": "Listening", "score": 5, "max_score": 10},
            {"name": "  LISTENING ", "score": 6, "max_score": 10},
        ],
        [
            {"name": "Ｃａｓｅ", "score": 5, "max_score": 10},
            {"name": "case", "score": 6, "max_score": 10},
        ],
        [{"name": "x" * 65, "score": 5, "max_score": 10}],
        [{"name": f"Skill {index}", "score": 1, "max_score": 1} for index in range(21)],
    ],
)
def test_component_validator_rejects_unbounded_or_ambiguous_evidence(components):
    with pytest.raises(ResultFieldError):
        validate_result_values(
            score=50,
            max_score=Decimal("100"),
            components=components,
        )


def test_result_components_round_trip_preserve_omission_and_explicit_clear(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam = ExamFactory(max_score=Decimal("100"))
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=exam.cohort, student=student)

        created = services.record_results(
            exam=exam,
            rows=[{"student": student, "score": 81, "components": _components()}],
        )["results"][0]
        assert created.components[0] == {
            "name": "Listening",
            "score": "67",
            "max_score": "100",
        }

        preserved = services.record_results(
            exam=exam,
            rows=[{"student": student, "score": 82}],
        )["results"][0]
        assert len(preserved.components) == 2

        cleared = services.record_results(
            exam=exam,
            rows=[{"student": student, "score": 82, "components": []}],
        )["results"][0]
        assert cleared.components == []


def test_result_components_api_round_trip_and_raw_results_remain_staff_only(tenant_a, user_in, as_user):
    director = user_in(tenant_a, roles=["director"])
    learner = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        exam = ExamFactory(max_score=Decimal("100"))
        student = StudentProfileFactory(branch=branch, user=learner)
        CohortMembershipFactory(cohort=exam.cohort, student=student)
        exam_id = exam.pk
        student_id = student.pk

    client = as_user(tenant_a, director)
    response = client.post(
        f"/api/v1/academics/exams/{exam_id}/results/",
        [{"student": student_id, "score": 81, "components": _components()}],
        format="json",
    )
    assert response.status_code == 200, response.content
    components = response.json()["data"]["results"][0]["components"]
    assert [item["name"] for item in components] == ["Listening", "Speaking"]

    listing = client.get(f"/api/v1/academics/exams/{exam_id}/results/")
    assert listing.status_code == 200
    assert listing.json()["data"][0]["components"] == components

    denied = as_user(tenant_a, learner).get(f"/api/v1/academics/exams/{exam_id}/results/")
    assert denied.status_code == 403
    with schema_context(tenant_a.schema_name):
        assert ExamResult.objects.get(exam_id=exam_id).components == components
