"""Messaging-specific actor fixtures with an explicit communication scope.

The project-wide ``as_role`` helper intentionally gives each new actor an
independent branch and no role-native profile.  That is useful for generic
permission tests, but it cannot represent a valid messaging directory: thread
creation is now fail-closed to the exact branch/department membership that
grants ``messaging:write`` and teachers may only contact students they teach.

Keep the richer setup local to this package so unrelated suites retain the
generic fixture semantics.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role


@pytest.fixture
def as_user(client_for):
    """Authenticate the one active role account represented by a test user."""

    def _make(tenant, user):
        from core.role_principals import resolve_unambiguous_user_principal
        from core.session_auth import create_session

        with schema_context(tenant.schema_name):
            principal = resolve_unambiguous_user_principal(
                user.pk,
                field="user",
                message="The messaging test actor needs one active role account.",
            )
            session = create_session(
                user,
                principal_kind=principal.kind,
                principal_id=principal.principal_id,
            )
        client = client_for(tenant)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
        return client

    return _make


@pytest.fixture
def as_role(tenant_a, user_in, as_user):
    """Create actors in one real, messaging-visible teaching network."""

    networks: dict[str, dict] = {}

    def network_for(tenant):
        network = networks.get(tenant.schema_name)
        if network is not None:
            return network

        from apps.cohorts.tests.factories import CohortFactory
        from apps.org.tests.factories import BranchFactory

        with schema_context(tenant.schema_name):
            branch = BranchFactory()
            cohort = CohortFactory(branch=branch)
        network = {
            "branch": branch,
            "cohort": cohort,
            "parents": [],
            "students": [],
        }
        networks[tenant.schema_name] = network
        return network

    def _make(role, tenant=None):
        tenant = tenant or tenant_a
        network = network_for(tenant)
        user = user_in(tenant, roles=[role], branch=network["branch"])

        with schema_context(tenant.schema_name):
            if role == Role.TEACHER:
                from apps.cohorts.tests.factories import CohortTeacherFactory
                from apps.teachers.tests.factories import TeacherProfileFactory

                teacher = TeacherProfileFactory(user=user, branch=network["branch"])
                CohortTeacherFactory(cohort=network["cohort"], teacher=teacher)
            elif role == Role.STUDENT:
                from apps.parents.models import Guardian
                from apps.students.tests.factories import StudentProfileFactory

                student = StudentProfileFactory(
                    user=user,
                    branch=network["branch"],
                    current_cohort=network["cohort"],
                )
                network["students"].append(student)
                for parent in network["parents"]:
                    Guardian.objects.get_or_create(
                        parent=parent,
                        student=student,
                        defaults={"relationship": Guardian.Relationship.OTHER},
                    )
            elif role == Role.PARENT:
                from apps.parents.models import Guardian
                from apps.parents.tests.factories import ParentProfileFactory

                parent = ParentProfileFactory(user=user)
                network["parents"].append(parent)
                for student in network["students"]:
                    Guardian.objects.get_or_create(
                        parent=parent,
                        student=student,
                        defaults={"relationship": Guardian.Relationship.OTHER},
                    )
            else:
                from apps.org.models import StaffProfile

                StaffProfile.objects.create(
                    user=user,
                    username=user.username,
                    password=user.password,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    phone=user.phone or "",
                )

        return as_user(tenant, user), user

    return _make
