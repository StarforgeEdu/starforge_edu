from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from apps.payroll.dto import PayrollPeriodCreateDTO
from apps.payroll.models import PayrollPeriod
from apps.payroll.services import create_period
from core.permissions import get_user_roles_for_user
from core.role_principals import RolePrincipal


@dataclass(frozen=True, slots=True)
class PayrollActor:
    staff: object
    user: object
    principal: RolePrincipal
    roles: object


def make_actor(*, branch, permissions: tuple[str, ...], department=None) -> PayrollActor:
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.services import create_staff_account

    suffix = uuid.uuid4().hex[:12]
    account_type = AccountType.objects.create(
        name=f"Payroll test account {suffix}",
        slug=f"payroll-test-{suffix}",
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in permissions
        ]
    )
    staff = create_staff_account(
        branch=branch,
        department=department,
        account_type=account_type,
        username=f"payroll.{suffix}",
        first_name="Payroll",
        last_name=suffix,
    )
    staff.user.refresh_from_db()
    principal = RolePrincipal(kind="staff", principal_id=staff.pk, user_id=staff.user_id)
    roles = get_user_roles_for_user(
        staff.user,
        principal_kind=principal.kind,
        principal_id=principal.principal_id,
        principal_validated=True,
    )
    return PayrollActor(staff=staff, user=staff.user, principal=principal, roles=roles)


def make_teacher(*, branch, department=None, amount: str = "3000000.00"):
    from apps.teachers.models import PayoutPolicy
    from apps.teachers.tests.factories import TeacherProfileFactory

    teacher = TeacherProfileFactory(
        branch=branch,
        department=department,
        first_name="Formula-safe",
        last_name="Teacher",
    )
    PayoutPolicy.objects.create(
        teacher=teacher,
        method=PayoutPolicy.Method.FLAT_MONTHLY,
        flat_amount_uzs=Decimal(amount),
        is_active=True,
    )
    return teacher


def make_period(
    *,
    actor: PayrollActor,
    branch,
    department=None,
    label: str = "June 2026 payroll",
    correction_of: PayrollPeriod | None = None,
    correction_reason: str = "",
) -> PayrollPeriod:
    return create_period(
        dto=PayrollPeriodCreateDTO(
            branch_id=branch.pk,
            department_id=department.pk if department else None,
            label=label,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            pay_date=date(2026, 7, 1),
            currency="UZS",
            correction_of_id=correction_of.pk if correction_of else None,
            correction_reason=correction_reason,
        ),
        actor=actor.user,
        principal=actor.principal,
        roles=actor.roles,
    )
