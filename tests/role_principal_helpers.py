"""Role-native account helpers for workflow integration tests.

The production login surface always binds a session to one concrete role profile.
Workflow tests should exercise that same identity shape instead of relying on the
temporary bridge-user union adapter.
"""

from __future__ import annotations

from core.permissions import Role


def ensure_role_principal(user, *, roles, branch=None):
    """Create the one role profile represented by ``roles`` and annotate ``user``."""

    role_values = {str(role) for role in roles}
    kinds = set()
    for role in role_values:
        if role == Role.STUDENT:
            kinds.add("student")
        elif role == Role.TEACHER:
            kinds.add("teacher")
        elif role == Role.PARENT:
            kinds.add("parent")
        else:
            kinds.add("staff")
    if len(kinds) != 1:
        raise ValueError("Workflow test users must represent exactly one role-account kind.")
    kind = kinds.pop()

    if kind == "student":
        from apps.students.tests.factories import StudentProfileFactory

        profile = StudentProfileFactory(user=user, **({"branch": branch} if branch else {}))
    elif kind == "teacher":
        from apps.teachers.tests.factories import TeacherProfileFactory

        profile = TeacherProfileFactory(user=user, **({"branch": branch} if branch else {}))
    elif kind == "parent":
        from apps.parents.tests.factories import ParentProfileFactory

        profile = ParentProfileFactory(user=user)
    else:
        from apps.org.models import StaffProfile

        profile = StaffProfile.objects.create(
            user=user,
            username=user.username,
            password=user.password,
            first_name=user.first_name,
            last_name=user.last_name,
            middle_name=user.middle_name,
            phone=user.phone or "",
            email=user.email or "",
        )

    user.test_principal_kind = kind
    user.test_principal_id = profile.pk
    return profile


def exact_session_client(
    client_for,
    tenant,
    user,
    *,
    principal_kind: str | None = None,
    principal_id: int | None = None,
):
    """Authenticate a test client with the role identity attached above."""

    from django_tenants.utils import schema_context

    from core.session_auth import create_session

    with schema_context(tenant.schema_name):
        session = create_session(
            user,
            principal_kind=(principal_kind if principal_kind is not None else user.test_principal_kind),
            principal_id=principal_id if principal_id is not None else user.test_principal_id,
        )
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    return client


def shared_staff_teacher_bridge(*, branch, staff_role: str):
    """Build the legacy ambiguity that workflow attribution must not collapse."""

    from apps.org.models import StaffProfile
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    user = UserFactory()
    teacher = TeacherProfileFactory(user=user, branch=branch)
    staff = StaffProfile.objects.create(
        user=user,
        username=f"staff-{user.username}",
        password=user.password,
        first_name=user.first_name,
        last_name=user.last_name,
        middle_name=user.middle_name,
        phone=user.phone or "",
        email=user.email or "",
    )
    RoleMembership.objects.create(user=user, branch=branch, role=Role.TEACHER)
    RoleMembership.objects.create(user=user, branch=branch, role=staff_role)
    return user, teacher, staff
