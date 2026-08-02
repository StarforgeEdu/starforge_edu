"""Role-native notification fixtures shared by the notification test suite."""

from __future__ import annotations


def ensure_notification_principal(user, *, kind: str = "staff", branch=None):
    """Attach one exact active role profile to ``user`` and return it.

    The production notification boundary intentionally rejects a bare bridge
    ``User``.  Tests that exercise delivery should therefore model the same
    role-native account shape as real logins instead of relying on the bridge.
    """

    if kind == "student":
        from apps.students.tests.factories import StudentProfileFactory

        profile = StudentProfileFactory(user=user, **({"branch": branch} if branch else {}))
    elif kind == "teacher":
        from apps.teachers.tests.factories import TeacherProfileFactory

        profile = TeacherProfileFactory(user=user, **({"branch": branch} if branch else {}))
    elif kind == "parent":
        from apps.parents.tests.factories import ParentProfileFactory

        profile = ParentProfileFactory(user=user)
    elif kind == "staff":
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
    else:  # pragma: no cover - test helper misuse
        raise ValueError(f"Unsupported notification principal kind: {kind}")

    user.notification_principal_kind = kind
    user.notification_principal_id = profile.pk
    return user


def principal_kwargs(user) -> dict[str, object]:
    return {
        "recipient_principal_kind": user.notification_principal_kind,
        "recipient_principal_id": user.notification_principal_id,
    }


def session_principal_kwargs(user) -> dict[str, object]:
    return {
        "principal_kind": user.notification_principal_kind,
        "principal_id": user.notification_principal_id,
    }
