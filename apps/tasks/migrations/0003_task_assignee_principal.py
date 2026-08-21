from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_task_principals(apps, schema_editor):
    Task = apps.get_model("staff_tasks", "Task")
    StaffProfile = apps.get_model("org", "StaffProfile")
    TeacherProfile = apps.get_model("teachers", "TeacherProfile")
    database = schema_editor.connection.alias

    last_pk = 0
    while True:
        rows = list(
            Task.objects.using(database)
            .filter(pk__gt=last_pk)
            .exclude(assignee_id=None)
            .order_by("pk")[:500]
        )
        if not rows:
            break
        last_pk = rows[-1].pk
        user_ids = {task.assignee_id for task in rows}
        evidence: dict[int, list[tuple[str, int]]] = {user_id: [] for user_id in user_ids}
        for kind, model in (("staff", StaffProfile), ("teacher", TeacherProfile)):
            for user_id, principal_id in model.objects.using(database).filter(
                user_id__in=user_ids,
                user__is_active=True,
                is_active=True,
            ).values_list("user_id", "pk"):
                evidence[user_id].append((kind, principal_id))
        changed = []
        for task in rows:
            candidates = evidence[task.assignee_id]
            if len(candidates) != 1:
                # Retain but quarantine ambiguous legacy assignments. Exact-principal
                # reads intentionally do not surface them until an operator reviews them.
                continue
            task.assignee_principal_kind, task.assignee_principal_id = candidates[0]
            task.assignee_attribution_status = "captured"
            changed.append(task)
        if changed:
            Task.objects.using(database).bulk_update(
                changed,
                [
                    "assignee_principal_kind",
                    "assignee_principal_id",
                    "assignee_attribution_status",
                ],
                batch_size=500,
            )
    Task.objects.using(database).filter(assignee_id=None).update(
        assignee_attribution_status="captured"
    )

    last_pk = 0
    while True:
        rows = list(
            Task.objects.using(database)
            .filter(pk__gt=last_pk)
            .only("pk", "created_by_id")
            .order_by("pk")[:500]
        )
        if not rows:
            break
        last_pk = rows[-1].pk
        user_ids = {task.created_by_id for task in rows if task.created_by_id is not None}
        evidence = {user_id: [] for user_id in user_ids}
        for kind, model in (("staff", StaffProfile), ("teacher", TeacherProfile)):
            for user_id, principal_id in model.objects.using(database).filter(
                user_id__in=user_ids,
                user__is_active=True,
                is_active=True,
            ).values_list("user_id", "pk"):
                evidence[user_id].append((kind, principal_id))
        for task in rows:
            candidates = evidence.get(task.created_by_id, [])
            if len(candidates) == 1:
                task.created_by_principal_kind, task.created_by_principal_id = candidates[0]
                task.created_by_attribution_status = "resolved"
            else:
                task.created_by_principal_kind = ""
                task.created_by_principal_id = None
                task.created_by_attribution_status = "quarantined"
        Task.objects.using(database).bulk_update(
            rows,
            [
                "created_by_principal_kind",
                "created_by_principal_id",
                "created_by_attribution_status",
            ],
            batch_size=500,
        )


TASK_ASSIGNEE_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_staff_principal_is_owned(
    principal_kind text,
    principal_id bigint,
    bridge_user_id bigint
) RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    IF principal_kind = 'staff' THEN
        RETURN EXISTS (
            SELECT 1 FROM org_staffprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = principal_id
              AND profile.user_id = bridge_user_id
              AND profile.is_active AND bridge.is_active
        );
    ELSIF principal_kind = 'teacher' THEN
        RETURN EXISTS (
            SELECT 1 FROM teachers_teacherprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = principal_id
              AND profile.user_id = bridge_user_id
              AND profile.is_active AND bridge.is_active
        );
    END IF;
    RETURN false;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_validate_assignee_principal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_is_live boolean := false;
    assignee_changed boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        assignee_changed := true;
    ELSE
        assignee_changed := (
            NEW.assignee_id IS DISTINCT FROM OLD.assignee_id
            OR NEW.assignee_principal_kind IS DISTINCT FROM OLD.assignee_principal_kind
            OR NEW.assignee_principal_id IS DISTINCT FROM OLD.assignee_principal_id
            OR NEW.assignee_attribution_status IS DISTINCT FROM OLD.assignee_attribution_status
        );
    END IF;
    IF NOT assignee_changed
       OR NEW.assignee_attribution_status <> 'captured'
       OR NEW.assignee_id IS NULL THEN
        RETURN NEW;
    END IF;
    principal_is_live := tasks_staff_principal_is_owned(
        NEW.assignee_principal_kind, NEW.assignee_principal_id, NEW.assignee_id
    );
    IF NOT principal_is_live THEN
        RAISE EXCEPTION 'task assignee principal is not active or owned by user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_validate_creator_principal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    creator_changed boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        creator_changed := true;
    ELSE
        creator_changed := (
            NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
            OR NEW.created_by_principal_kind IS DISTINCT FROM OLD.created_by_principal_kind
            OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
            OR NEW.created_by_attribution_status IS DISTINCT FROM OLD.created_by_attribution_status
        );
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.created_by_principal_kind IS DISTINCT FROM OLD.created_by_principal_kind
        OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
        OR NEW.created_by_attribution_status IS DISTINCT FROM OLD.created_by_attribution_status
    ) THEN
        RAISE EXCEPTION 'task creator principal attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
       AND NOT (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL) THEN
        RAISE EXCEPTION 'task creator bridge attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF creator_changed
       AND NEW.created_by_attribution_status IN ('captured', 'resolved')
       AND NEW.created_by_id IS NOT NULL
       AND NOT tasks_staff_principal_is_owned(
           NEW.created_by_principal_kind, NEW.created_by_principal_id, NEW.created_by_id
       ) THEN
        RAISE EXCEPTION 'task creator principal is not owned by user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_assignee_principal_guard
BEFORE INSERT OR UPDATE OF assignee_id, assignee_principal_kind,
    assignee_principal_id, assignee_attribution_status
ON staff_tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_validate_assignee_principal();

CREATE TRIGGER tasks_creator_principal_guard
BEFORE INSERT OR UPDATE OF created_by_id, created_by_principal_kind,
    created_by_principal_id, created_by_attribution_status
ON staff_tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_validate_creator_principal();
"""


DROP_TASK_ASSIGNEE_GUARD_SQL = r"""
DROP TRIGGER IF EXISTS tasks_creator_principal_guard ON staff_tasks_task;
DROP TRIGGER IF EXISTS tasks_assignee_principal_guard ON staff_tasks_task;
DROP FUNCTION IF EXISTS tasks_validate_creator_principal();
DROP FUNCTION IF EXISTS tasks_validate_assignee_principal();
DROP FUNCTION IF EXISTS tasks_staff_principal_is_owned(text, bigint, bigint);
"""


class Migration(migrations.Migration):
    # The principal backfill updates rows guarded by deferred foreign-key
    # triggers. Commit those updates before PostgreSQL builds the following
    # index and constraints, otherwise CREATE INDEX sees pending trigger events.
    atomic = False

    dependencies = [
        ("staff_tasks", "0002_task_task_created_idx"),
        ("org", "0021_durable_center_settings"),
        ("teachers", "0010_alter_payoutpolicy_method"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="assignee_attribution_status",
            field=models.CharField(
                choices=(("captured", "Captured"), ("quarantined", "Quarantined")),
                default="quarantined",
                max_length=16,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="task",
            name="assignee_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="assignee_principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="task",
            name="created_by_attribution_status",
            field=models.CharField(
                choices=(
                    ("captured", "Captured"),
                    ("resolved", "Resolved from legacy data"),
                    ("quarantined", "Quarantined"),
                ),
                default="quarantined",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="created_by_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="created_by_principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.RunPython(backfill_task_principals, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="task",
            name="assignee_attribution_status",
            field=models.CharField(
                choices=(("captured", "Captured"), ("quarantined", "Quarantined")),
                default="captured",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="assignee",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assigned_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="task",
            index=models.Index(
                fields=["assignee_principal_kind", "assignee_principal_id", "status"],
                name="task_assignee_principal_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        assignee__isnull=True,
                        assignee_principal_kind="",
                        assignee_principal_id__isnull=True,
                        assignee_attribution_status="captured",
                    )
                    | (
                        models.Q(assignee__isnull=False)
                        & models.Q(assignee_principal_kind__in=("staff", "teacher"))
                        & models.Q(assignee_principal_id__isnull=False)
                        & models.Q(assignee_attribution_status="captured")
                    )
                    | models.Q(
                        assignee__isnull=False,
                        assignee_principal_kind="",
                        assignee_principal_id__isnull=True,
                        assignee_attribution_status="quarantined",
                    )
                ),
                name="task_assignee_principal_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(created_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(created_by_principal_id__isnull=False)
                        & models.Q(created_by_attribution_status__in=("captured", "resolved"))
                    )
                    | models.Q(
                        created_by_principal_kind="",
                        created_by_principal_id__isnull=True,
                        created_by_attribution_status="quarantined",
                    )
                ),
                name="task_creator_principal_pair",
            ),
        ),
        migrations.RunSQL(TASK_ASSIGNEE_GUARD_SQL, DROP_TASK_ASSIGNEE_GUARD_SQL),
    ]
