"""Archive a Center: rename its schema and deactivate it (D1-LB-8).

python manage.py archive_center <slug>
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tenancy.models import Center
from apps.tenancy.services import archive_center


class Command(BaseCommand):
    help = "Archive a Center by slug: rename the schema to _archived_<slug>_<date> and deactivate."

    def add_arguments(self, parser) -> None:
        parser.add_argument("slug", help="The Center slug to archive.")

    def handle(self, *args, **options) -> None:
        slug = options["slug"]
        center = Center.objects.filter(slug=slug).first()
        if center is None:
            raise CommandError(f"No Center with slug '{slug}'.")
        if center.archived_at is not None:
            raise CommandError(f"Center '{slug}' is already archived.")
        archive_center(center)
        self.stdout.write(self.style.SUCCESS(f"Archived '{slug}' → schema '{center.schema_name}'."))
