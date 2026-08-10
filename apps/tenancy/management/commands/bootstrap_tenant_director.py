"""Create the first role-native director for one existing tenant."""

from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django_tenants.utils import get_public_schema_name, schema_context

from apps.access.models import AccountType
from apps.audit.scopes import scoped_audit_scope
from apps.audit.services import audit_log
from apps.org.models import Branch
from apps.org.services import create_staff_account
from apps.tenancy.models import Center
from apps.users.models import RoleMembership
from apps.users.services import generate_temp_password, set_role_account_password
from core.exceptions import StarforgeError
from core.permissions import Role

_TEMPORARY_PASSWORD_LENGTH = 20


class Command(BaseCommand):
    help = (
        "Create the first active director in one tenant. The command refuses to run "
        "when an active owner already exists and emits a one-time temporary password."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", required=True, help="Exact non-public tenant schema name.")
        parser.add_argument("--branch", required=True, help="Exact active branch slug for the owner grant.")
        parser.add_argument("--username", required=True, help="Director login username.")
        parser.add_argument("--first-name", required=True, help="Director first name.")
        parser.add_argument("--last-name", required=True, help="Director last name.")
        parser.add_argument("--email", default="", help="Recovery email; email or phone is required.")
        parser.add_argument("--phone", default="", help="Recovery phone; email or phone is required.")

    def handle(self, *args, **options) -> None:
        schema_name = str(options["schema"]).strip()
        branch_slug = str(options["branch"]).strip()
        username = str(options["username"]).strip()
        first_name = str(options["first_name"]).strip()
        last_name = str(options["last_name"]).strip()
        email = str(options["email"]).strip()
        phone = str(options["phone"]).strip()

        if not schema_name or schema_name == get_public_schema_name():
            raise CommandError("--schema must name one non-public tenant schema")
        if not branch_slug:
            raise CommandError("--branch must name one active branch slug")
        if not username or not first_name or not last_name:
            raise CommandError("--username, --first-name, and --last-name may not be blank")
        if not email and not phone:
            raise CommandError("Provide at least one recovery contact with --email or --phone")

        public_schema = get_public_schema_name()
        with schema_context(public_schema):
            center_exists = Center.objects.filter(
                schema_name=schema_name,
                is_active=True,
                archived_at__isnull=True,
            ).exists()
        if not center_exists:
            raise CommandError(f"No active tenant uses schema '{schema_name}'.")

        try:
            with schema_context(schema_name), transaction.atomic():
                # Lock the protected owner type so two concurrent bootstrap
                # attempts cannot both pass the no-owner check.
                owner_type = (
                    AccountType.objects.select_for_update()
                    .filter(
                        is_system=True,
                        is_active=True,
                        account_kind=AccountType.AccountKind.STAFF,
                        slug=Role.DIRECTOR,
                    )
                    .first()
                )
                if owner_type is None:
                    raise CommandError(
                        "The active system director account type is unavailable; repair migrations first."
                    )

                active_owner = RoleMembership.objects.filter(
                    Q(account_type=owner_type)
                    | Q(account_type__isnull=True, role=Role.DIRECTOR),
                    revoked_at__isnull=True,
                    user__is_active=True,
                    user__staff_profile__is_active=True,
                ).exists()
                if active_owner:
                    raise CommandError(
                        "This tenant already has an active director; create additional owners "
                        "through the authenticated staff and responsibility APIs."
                    )

                branch = (
                    Branch.objects.select_for_update()
                    .filter(slug=branch_slug, is_active=True, archived_at__isnull=True)
                    .first()
                )
                if branch is None:
                    raise CommandError(
                        f"No active, unarchived branch uses slug '{branch_slug}' in '{schema_name}'."
                    )

                director = create_staff_account(
                    branch=branch,
                    account_type=owner_type,
                    username=username,
                    phone=phone,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                )
                temporary_password = generate_temp_password(_TEMPORARY_PASSWORD_LENGTH)
                try:
                    validate_password(temporary_password, user=director.user)
                except DjangoValidationError as exc:  # pragma: no cover - secure generator invariant
                    raise CommandError("The generated temporary password failed policy validation.") from exc
                set_role_account_password(director, temporary_password, must_change=True)
                audit_log(
                    actor=None,
                    action="create",
                    resource_type="org.StaffProfile",
                    resource_id=director.pk,
                    after={
                        "username": director.username,
                        "account_type": owner_type.slug,
                        "bootstrap": "first_director",
                    },
                    scope=scoped_audit_scope(branch.pk),
                )
        except CommandError:
            raise
        except StarforgeError as exc:
            raise CommandError(f"Director bootstrap failed [{exc.code}]: {exc.detail}") from exc

        self.stdout.write(
            self.style.SUCCESS(f"Created the first director for tenant schema '{schema_name}'.")
        )
        self.stdout.write(f"Username: {director.username}")
        self.stdout.write(f"Temporary password: {temporary_password}")
        self.stdout.write(
            self.style.WARNING(
                "Store the temporary password securely; it is shown once and must be changed at first login."
            )
        )
