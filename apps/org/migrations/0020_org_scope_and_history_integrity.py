import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.validators

logger = logging.getLogger("starforge.migrations")
_PREFLIGHT_SAMPLE_LIMIT = 20
_BATCH_SIZE = 1_000


CREATE_HISTORY_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION org_validate_transfer_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    actor_valid boolean := FALSE;
BEGIN
    IF NEW.student_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM students_studentprofile AS student
        WHERE student.id = NEW.student_id
          AND student.user_id = NEW.user_id
          AND student.student_id = NEW.student_public_id
          AND student.branch_id = NEW.to_branch_id
          AND NEW.student_name = LEFT(
              concat_ws(
                  ' ',
                  NULLIF(student.first_name, ''),
                  NULLIF(student.middle_name, ''),
                  NULLIF(student.last_name, '')
              ),
              452
          )
    ) THEN
        RAISE EXCEPTION 'branch transfer student attribution is invalid'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.actor_id IS NULL THEN
        IF NEW.actor_principal_kind <> ''
           OR NEW.actor_principal_id IS NOT NULL
           OR NEW.actor_name <> '' THEN
            RAISE EXCEPTION 'branch transfer actor attribution is invalid'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.actor_principal_kind = 'staff' THEN
        SELECT EXISTS (
            SELECT 1 FROM org_staffprofile AS profile
            JOIN users_user AS bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.actor_principal_id
              AND profile.user_id = NEW.actor_id
              AND profile.is_active = TRUE
              AND bridge.is_active = TRUE
              AND NEW.actor_name = LEFT(
                  COALESCE(
                      NULLIF(
                          concat_ws(
                              ' ',
                              NULLIF(profile.first_name, ''),
                              NULLIF(profile.middle_name, ''),
                              NULLIF(profile.last_name, '')
                          ),
                          ''
                      ),
                      profile.username,
                      ''
                  ),
                  452
              )
        ) INTO actor_valid;
    ELSIF NEW.actor_principal_kind = 'teacher' THEN
        SELECT EXISTS (
            SELECT 1 FROM teachers_teacherprofile AS profile
            JOIN users_user AS bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.actor_principal_id
              AND profile.user_id = NEW.actor_id
              AND profile.is_active = TRUE
              AND bridge.is_active = TRUE
              AND NEW.actor_name = LEFT(
                  COALESCE(
                      NULLIF(
                          concat_ws(
                              ' ',
                              NULLIF(profile.first_name, ''),
                              NULLIF(profile.middle_name, ''),
                              NULLIF(profile.last_name, '')
                          ),
                          ''
                      ),
                      profile.username,
                      ''
                  ),
                  452
              )
        ) INTO actor_valid;
    ELSIF NEW.actor_principal_kind = 'student' THEN
        SELECT EXISTS (
            SELECT 1 FROM students_studentprofile AS profile
            JOIN users_user AS bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.actor_principal_id
              AND profile.user_id = NEW.actor_id
              AND profile.is_active = TRUE
              AND bridge.is_active = TRUE
              AND NEW.actor_name = LEFT(
                  COALESCE(
                      NULLIF(
                          concat_ws(
                              ' ',
                              NULLIF(profile.first_name, ''),
                              NULLIF(profile.middle_name, ''),
                              NULLIF(profile.last_name, '')
                          ),
                          ''
                      ),
                      profile.username,
                      ''
                  ),
                  452
              )
        ) INTO actor_valid;
    ELSIF NEW.actor_principal_kind = 'parent' THEN
        SELECT EXISTS (
            SELECT 1 FROM parents_parentprofile AS profile
            JOIN users_user AS bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.actor_principal_id
              AND profile.user_id = NEW.actor_id
              AND profile.is_active = TRUE
              AND bridge.is_active = TRUE
              AND NEW.actor_name = LEFT(
                  COALESCE(
                      NULLIF(
                          concat_ws(
                              ' ',
                              NULLIF(profile.first_name, ''),
                              NULLIF(profile.middle_name, ''),
                              NULLIF(profile.last_name, '')
                          ),
                          ''
                      ),
                      profile.username,
                      ''
                  ),
                  452
              )
        ) INTO actor_valid;
    END IF;

    IF NOT actor_valid THEN
        RAISE EXCEPTION 'branch transfer actor attribution is invalid'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION org_reject_structure_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.org_history_maintenance', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'organization structure is historical; deactivate or archive it'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION org_reject_transfer_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.org_history_maintenance', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'branch transfer history is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS org_branch_reject_delete ON org_branch;
CREATE TRIGGER org_branch_reject_delete
BEFORE DELETE ON org_branch
FOR EACH ROW EXECUTE FUNCTION org_reject_structure_delete();

DROP TRIGGER IF EXISTS org_department_reject_delete ON org_department;
CREATE TRIGGER org_department_reject_delete
BEFORE DELETE ON org_department
FOR EACH ROW EXECUTE FUNCTION org_reject_structure_delete();

DROP TRIGGER IF EXISTS org_room_reject_delete ON org_room;
CREATE TRIGGER org_room_reject_delete
BEFORE DELETE ON org_room
FOR EACH ROW EXECUTE FUNCTION org_reject_structure_delete();

DROP TRIGGER IF EXISTS org_transfer_reject_mutation ON org_branchtransfer;
CREATE TRIGGER org_transfer_reject_mutation
BEFORE UPDATE OR DELETE ON org_branchtransfer
FOR EACH ROW EXECUTE FUNCTION org_reject_transfer_mutation();

DROP TRIGGER IF EXISTS org_transfer_validate_insert ON org_branchtransfer;
CREATE TRIGGER org_transfer_validate_insert
BEFORE INSERT ON org_branchtransfer
FOR EACH ROW EXECUTE FUNCTION org_validate_transfer_insert();
"""


DROP_HISTORY_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS org_transfer_validate_insert ON org_branchtransfer;
DROP TRIGGER IF EXISTS org_transfer_reject_mutation ON org_branchtransfer;
DROP TRIGGER IF EXISTS org_room_reject_delete ON org_room;
DROP TRIGGER IF EXISTS org_department_reject_delete ON org_department;
DROP TRIGGER IF EXISTS org_branch_reject_delete ON org_branch;
DROP FUNCTION IF EXISTS org_reject_transfer_mutation();
DROP FUNCTION IF EXISTS org_reject_structure_delete();
DROP FUNCTION IF EXISTS org_validate_transfer_insert();
"""


def preflight_org_integrity(apps, schema_editor):
    """Reject unsafe organization data before changing the schema."""

    from django_tenants.utils import get_public_schema_name

    connection = schema_editor.connection
    if connection.schema_name == get_public_schema_name():
        return
    database = connection.alias
    Branch = apps.get_model("org", "Branch")
    Department = apps.get_model("org", "Department")
    Room = apps.get_model("org", "Room")

    def remember(sample: list[int], value: int) -> None:
        if len(sample) < _PREFLIGHT_SAMPLE_LIMIT:
            sample.append(value)

    invalid_timezones: list[int] = []
    for branch_id, timezone_name in Branch.objects.using(database).values_list("pk", "timezone").iterator():
        try:
            ZoneInfo(timezone_name)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            remember(invalid_timezones, branch_id)

    negative_budgets = list(
        Department.objects.using(database).filter(budget__lt=0).values_list("pk", flat=True)[:20]
    )
    oversized_departments: list[int] = []
    for department_id, description in (
        Department.objects.using(database).values_list("pk", "description").iterator()
    ):
        if len(description or "") > 4_000:
            remember(oversized_departments, department_id)
    invalid_rooms: list[int] = []
    oversized_room_notes: list[int] = []
    for room_id, equipment, notes in (
        Room.objects.using(database).values_list("pk", "equipment", "notes").iterator()
    ):
        if len(notes or "") > 4_000:
            remember(oversized_room_notes, room_id)
        valid_equipment = isinstance(equipment, list) and len(equipment) <= 64
        normalized: list[str] = []
        if valid_equipment:
            for item in equipment:
                if not isinstance(item, str) or not item.strip() or len(item.strip()) > 100:
                    valid_equipment = False
                    break
                normalized.append(item.strip())
            valid_equipment = valid_equipment and len(normalized) == len(set(normalized))
        if not valid_equipment:
            remember(invalid_rooms, room_id)

    if (
        invalid_timezones
        or negative_budgets
        or oversized_departments
        or invalid_rooms
        or oversized_room_notes
    ):
        raise RuntimeError(
            "organization integrity preflight failed; no values were guessed: "
            f"invalid_timezone_ids={invalid_timezones[:20]}, "
            f"negative_budget_ids={negative_budgets}, "
            f"oversized_department_ids={oversized_departments}, "
            f"invalid_equipment_ids={invalid_rooms[:20]}, "
            f"oversized_room_note_ids={oversized_room_notes[:20]}"
        )


def backfill_transfer_attribution(apps, schema_editor):
    """Backfill only exact role-native identities after the fields exist.

    Existing transfer rows that cannot be attributed without guessing remain
    explicitly unresolved: the student fields retain their unresolved default,
    while the actor principal pair and display snapshot remain blank.
    """

    from django_tenants.utils import get_public_schema_name

    connection = schema_editor.connection
    if connection.schema_name == get_public_schema_name():
        return
    database = connection.alias
    BranchTransfer = apps.get_model("org", "BranchTransfer")
    StudentProfile = apps.get_model("students", "StudentProfile")

    resolved = 0
    cursor = 0
    while True:
        transfers = list(
            BranchTransfer.objects.using(database)
            .filter(pk__gt=cursor, student__isnull=True)
            .order_by("pk")[:_BATCH_SIZE]
        )
        if not transfers:
            break
        cursor = transfers[-1].pk
        profiles = {
            profile.user_id: profile
            for profile in StudentProfile.objects.using(database)
            .filter(user_id__in={row.user_id for row in transfers})
            .only("pk", "user_id", "student_id", "first_name", "middle_name", "last_name")
        }
        updates = []
        for transfer in transfers:
            profile = profiles.get(transfer.user_id)
            if profile is None:
                continue
            transfer.student_id = profile.pk
            transfer.student_public_id = profile.student_id
            transfer.student_name = " ".join(
                part for part in (profile.first_name, profile.middle_name, profile.last_name) if part
            )
            transfer.student_attribution_status = "resolved"
            updates.append(transfer)
        if updates:
            BranchTransfer.objects.using(database).bulk_update(
                updates,
                ("student", "student_public_id", "student_name", "student_attribution_status"),
                batch_size=_BATCH_SIZE,
            )
            resolved += len(updates)

    unresolved = BranchTransfer.objects.using(database).filter(student__isnull=True).count()

    # A compatibility User can back several role accounts, so an actor is
    # attributed only when exactly one role-native profile exists. Historical
    # inactive profiles remain valid evidence; active status today must not
    # rewrite who performed an old transfer.
    actor_resolved = 0
    actor_cursor = 0
    while True:
        transfers = list(
            BranchTransfer.objects.using(database)
            .filter(pk__gt=actor_cursor, actor__isnull=False)
            .order_by("pk")[:_BATCH_SIZE]
        )
        if not transfers:
            break
        actor_cursor = transfers[-1].pk
        actor_user_ids = {row.actor_id for row in transfers}
        matches: dict[int, list[tuple[str, int, str]]] = {user_id: [] for user_id in actor_user_ids}
        for kind, model_label in (
            ("parent", "parents.ParentProfile"),
            ("staff", "org.StaffProfile"),
            ("student", "students.StudentProfile"),
            ("teacher", "teachers.TeacherProfile"),
        ):
            Profile = apps.get_model(*model_label.split(".", 1))
            rows = (
                Profile.objects.using(database)
                .filter(user_id__in=actor_user_ids)
                .values_list(
                    "pk",
                    "user_id",
                    "first_name",
                    "middle_name",
                    "last_name",
                    "username",
                )
            )
            for principal_id, user_id, first_name, middle_name, last_name, username in rows:
                display_name = " ".join(part for part in (first_name, middle_name, last_name) if part) or (
                    username or ""
                )
                matches[user_id].append((kind, principal_id, display_name[:452]))

        updates = []
        for transfer in transfers:
            candidates = matches.get(transfer.actor_id, [])
            if len(candidates) != 1:
                continue
            kind, principal_id, display_name = candidates[0]
            transfer.actor_principal_kind = kind
            transfer.actor_principal_id = principal_id
            transfer.actor_name = display_name
            updates.append(transfer)
        if updates:
            BranchTransfer.objects.using(database).bulk_update(
                updates,
                ("actor_principal_kind", "actor_principal_id", "actor_name"),
                batch_size=_BATCH_SIZE,
            )
            actor_resolved += len(updates)

    unresolved_actors = (
        BranchTransfer.objects.using(database)
        .filter(actor__isnull=False, actor_principal_id__isnull=True)
        .count()
    )
    logger.warning(
        "organization transfer attribution backfill: schema=%s "
        "students_resolved=%s students_unresolved=%s "
        "actors_resolved=%s actors_unresolved=%s",
        connection.schema_name,
        resolved,
        unresolved,
        actor_resolved,
        unresolved_actors,
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("org", "0019_centersettings_organization_timezone"),
        ("parents", "0010_preserve_family_lifecycle_history"),
        ("students", "0011_protect_identity_history"),
        ("teachers", "0010_alter_payoutpolicy_method"),
    ]

    operations = [
        # A failed preflight rolls back before any schema operation, so legacy
        # data can be repaired and the migration can be retried safely.
        migrations.RunPython(preflight_org_integrity, migrations.RunPython.noop),
        migrations.AddField(
            model_name="branchtransfer",
            name="actor_name",
            field=models.CharField(blank=True, editable=False, max_length=452),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="actor_principal_id",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="actor_principal_kind",
            field=models.CharField(blank=True, editable=False, max_length=16),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="student",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="branch_transfers",
                to="students.studentprofile",
            ),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="student_attribution_status",
            field=models.CharField(
                choices=[("resolved", "Resolved"), ("unresolved", "Unresolved")],
                default="unresolved",
                editable=False,
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="student_name",
            field=models.CharField(blank=True, editable=False, max_length=452),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="student_public_id",
            field=models.CharField(blank=True, editable=False, max_length=32),
        ),
        migrations.RunPython(backfill_transfer_attribution, migrations.RunPython.noop),
        # PostgreSQL defers Django foreign-key checks until transaction end.
        # Flush the student-attribution FK events before ALTER TABLE adds the
        # remaining constraints in this same atomic migration.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="branch",
            name="timezone",
            field=models.CharField(
                default="Asia/Tashkent",
                max_length=64,
                validators=[core.validators.validate_iana_timezone],
            ),
        ),
        migrations.AlterField(
            model_name="branchtransfer",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transfers_made",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="branchtransfer",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="branch_transfers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="department",
            name="branch",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="departments",
                to="org.branch",
            ),
        ),
        migrations.AlterField(
            model_name="department",
            name="description",
            field=models.TextField(
                blank=True,
                validators=[django.core.validators.MaxLengthValidator(4_000)],
            ),
        ),
        migrations.AlterField(
            model_name="room",
            name="branch",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rooms",
                to="org.branch",
            ),
        ),
        migrations.AlterField(
            model_name="room",
            name="notes",
            field=models.TextField(
                blank=True,
                validators=[django.core.validators.MaxLengthValidator(4_000)],
            ),
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.CheckConstraint(
                condition=models.Q(budget__isnull=True) | models.Q(budget__gte=0),
                name="department_budget_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="branchtransfer",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(student__isnull=False, student_attribution_status="resolved")
                        & ~models.Q(student_public_id="")
                    )
                    | models.Q(
                        student__isnull=True,
                        student_attribution_status="unresolved",
                        student_name="",
                        student_public_id="",
                    )
                ),
                name="branch_transfer_student_attribution_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="branchtransfer",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_name="",
                        actor_principal_id__isnull=True,
                        actor_principal_kind="",
                    )
                    | models.Q(
                        actor__isnull=False,
                        actor_principal_id__isnull=False,
                        actor_principal_kind__in=("staff", "teacher", "student", "parent"),
                    )
                ),
                name="branch_transfer_actor_principal_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="branchtransfer",
            constraint=models.CheckConstraint(
                condition=~models.Q(from_branch=models.F("to_branch")),
                name="branch_transfer_branches_differ",
            ),
        ),
        migrations.RunSQL(CREATE_HISTORY_GUARDS_SQL, DROP_HISTORY_GUARDS_SQL),
    ]
