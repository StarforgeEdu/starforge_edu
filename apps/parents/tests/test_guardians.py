import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.parents.services import create_parent, link_guardian
from apps.students.services import create_student
from core.exceptions import ValidationException

pytestmark = pytest.mark.django_db


def test_one_primary_guardian_per_student(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905551101")
        p1 = create_parent(phone="+998905552101")
        p2 = create_parent(phone="+998905552102")
        link_guardian(parent=p1, student=student, relationship="mother", is_primary=True)
        with pytest.raises(ValidationException) as exc:
            link_guardian(parent=p2, student=student, relationship="father", is_primary=True)
        assert exc.value.code == "primary_guardian_exists"


def test_duplicate_guardian_link_rejected(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = create_student(branch=branch, phone="+998905551102")
        parent = create_parent(phone="+998905552103")
        link_guardian(parent=parent, student=student, relationship="mother", is_primary=True)
        with pytest.raises(ValidationException) as exc:
            link_guardian(parent=parent, student=student, relationship="mother")
        assert exc.value.code == "guardian_exists"
