import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.users.models import RoleMembership
from apps.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_null_department_role_membership_is_unique(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user = UserFactory()
        RoleMembership.objects.create(user=user, branch=branch, role="teacher")

        with pytest.raises(IntegrityError), transaction.atomic():
            RoleMembership.objects.create(user=user, branch=branch, role="teacher")
