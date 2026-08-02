"""Prove that an explicit first-install migration targets an empty database."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


def database_is_empty() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_namespace
             WHERE nspname NOT IN ('public', 'pg_catalog', 'information_schema')
               AND nspname NOT LIKE 'pg_toast%'
               AND nspname NOT LIKE 'pg_temp_%'
            """
        )
        custom_schemas = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_class relation
              JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname = 'public'
               AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            """
        )
        public_relations = int(cursor.fetchone()[0])
    return custom_schemas == 0 and public_relations == 0


class Command(BaseCommand):
    help = "Return 'empty' only for a database with no application schema or relation."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--token", action="store_true")

    def handle(self, *args, **options) -> None:
        if not database_is_empty():
            raise CommandError("The database is not an empty first-install target.")
        self.stdout.write("empty" if options["token"] else "Production database is empty.")
