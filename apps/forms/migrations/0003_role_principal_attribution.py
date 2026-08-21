from __future__ import annotations

from django.db import migrations, models

_PROFILE_MODELS = (
    ("student", "students", "StudentProfile"),
    ("teacher", "teachers", "TeacherProfile"),
    ("parent", "parents", "ParentProfile"),
    ("staff", "org", "StaffProfile"),
)


def _principal_evidence(apps, user_ids, *, database):
    matches: dict[int, list[tuple[str, int]]] = {int(user_id): [] for user_id in user_ids}
    for kind, app_label, model_name in _PROFILE_MODELS:
        model = apps.get_model(app_label, model_name)
        for user_id, principal_id in model.objects.using(database).filter(
            user_id__in=matches,
            user__is_active=True,
            is_active=True,
        ).values_list("user_id", "pk"):
            matches[int(user_id)].append((kind, int(principal_id)))
    return matches


def _principal_map(apps, user_ids, *, database):
    return {
        user_id: rows[0]
        for user_id, rows in _principal_evidence(apps, user_ids, database=database).items()
        if len(rows) == 1
    }


def _staff_principal_evidence(apps, user_ids, *, database):
    matches: dict[int, list[tuple[str, int]]] = {int(user_id): [] for user_id in user_ids}
    for kind, app_label, model_name in (
        ("staff", "org", "StaffProfile"),
        ("teacher", "teachers", "TeacherProfile"),
    ):
        model = apps.get_model(app_label, model_name)
        for user_id, principal_id in model.objects.using(database).filter(
            user_id__in=matches,
            user__is_active=True,
            is_active=True,
        ).values_list("user_id", "pk"):
            matches[int(user_id)].append((kind, int(principal_id)))
    return matches


def backfill_principals(apps, schema_editor):
    Form = apps.get_model("forms_app", "Form")
    FormResponse = apps.get_model("forms_app", "FormResponse")
    database = schema_editor.connection.alias

    last_pk = 0
    while True:
        forms = list(
            Form.objects.using(database)
            .filter(pk__gt=last_pk)
            .only("pk", "created_by_id")
            .order_by("pk")[:500]
        )
        if not forms:
            break
        last_pk = forms[-1].pk
        creator_user_ids = {form.created_by_id for form in forms if form.created_by_id is not None}
        evidence_by_user = _staff_principal_evidence(
            apps,
            creator_user_ids,
            database=database,
        )
        for form in forms:
            evidence = evidence_by_user.get(form.created_by_id, [])
            if len(evidence) == 1:
                form.created_by_principal_kind, form.created_by_principal_id = evidence[0]
                form.created_by_attribution_status = "resolved"
            else:
                form.created_by_principal_kind = ""
                form.created_by_principal_id = None
                form.created_by_attribution_status = "quarantined"
        Form.objects.using(database).bulk_update(
            forms,
            [
                "created_by_principal_kind",
                "created_by_principal_id",
                "created_by_attribution_status",
            ],
            batch_size=500,
        )

    last_pk = 0
    while True:
        forms = list(
            Form.objects.using(database)
            .filter(pk__gt=last_pk)
            .exclude(audience_user_ids=[])
            .only("pk", "audience_user_ids")
            .order_by("pk")[:500]
        )
        if not forms:
            break
        last_pk = forms[-1].pk
        audience_user_ids = {
            raw_user_id
            for form in forms
            for raw_user_id in form.audience_user_ids
            if isinstance(raw_user_id, int) and not isinstance(raw_user_id, bool) and raw_user_id > 0
        }
        principals_by_user = _principal_map(apps, audience_user_ids, database=database)
        changed_forms = []
        for form in forms:
            principals = []
            seen = set()
            for raw_user_id in form.audience_user_ids:
                if isinstance(raw_user_id, bool) or not isinstance(raw_user_id, int) or raw_user_id <= 0:
                    continue
                resolved = principals_by_user.get(raw_user_id)
                if resolved is None or resolved in seen:
                    continue
                seen.add(resolved)
                kind, principal_id = resolved
                principals.append({"kind": kind, "id": principal_id, "user_id": raw_user_id})
            if principals:
                form.audience_principals = principals
                changed_forms.append(form)
        if changed_forms:
            Form.objects.using(database).bulk_update(
                changed_forms,
                ["audience_principals"],
                batch_size=500,
            )

    last_pk = 0
    while True:
        responses = list(
            FormResponse.objects.using(database).filter(pk__gt=last_pk).order_by("pk")[:1000]
        )
        if not responses:
            break
        last_pk = responses[-1].pk
        user_ids = {response.respondent_id for response in responses if response.respondent_id is not None}
        evidence_by_user = _principal_evidence(apps, user_ids, database=database)
        changed_responses = []
        for response in responses:
            if response.respondent_id is None:
                response.respondent_attribution_status = "anonymous"
                changed_responses.append(response)
                continue
            evidence = evidence_by_user.get(response.respondent_id, [])
            if len(evidence) != 1:
                response.respondent_attribution_status = "conflicting" if len(evidence) > 1 else "unresolved"
                changed_responses.append(response)
                continue
            kind, principal_id = evidence[0]
            response.respondent_principal_kind = kind
            response.respondent_principal_id = principal_id
            response.respondent_attribution_status = "resolved"
            if response.dedupe_token:
                response.dedupe_token = f"{kind}:{principal_id}"
            changed_responses.append(response)
        if changed_responses:
            FormResponse.objects.using(database).bulk_update(
                changed_responses,
                [
                    "respondent_principal_kind",
                    "respondent_principal_id",
                    "respondent_attribution_status",
                    "dedupe_token",
                ],
                batch_size=500,
            )


FORM_RESPONSE_PRINCIPAL_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION forms_staff_principal_is_owned(
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

CREATE OR REPLACE FUNCTION forms_validate_creator_principal()
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
        RAISE EXCEPTION 'form creator principal attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
       AND NOT (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL) THEN
        RAISE EXCEPTION 'form creator bridge attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF creator_changed
       AND NEW.created_by_attribution_status IN ('captured', 'resolved')
       AND NEW.created_by_id IS NOT NULL
       AND NOT forms_staff_principal_is_owned(
           NEW.created_by_principal_kind, NEW.created_by_principal_id, NEW.created_by_id
       ) THEN
        RAISE EXCEPTION 'form creator principal is not owned by user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION forms_validate_response_principal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_is_live boolean := false;
    attribution_changed boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        attribution_changed := true;
    ELSE
        attribution_changed := (
            NEW.respondent_id IS DISTINCT FROM OLD.respondent_id
            OR NEW.respondent_principal_kind IS DISTINCT FROM OLD.respondent_principal_kind
            OR NEW.respondent_principal_id IS DISTINCT FROM OLD.respondent_principal_id
            OR NEW.respondent_attribution_status IS DISTINCT FROM OLD.respondent_attribution_status
        );
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.respondent_principal_kind IS DISTINCT FROM OLD.respondent_principal_kind
        OR NEW.respondent_principal_id IS DISTINCT FROM OLD.respondent_principal_id
        OR NEW.respondent_attribution_status IS DISTINCT FROM OLD.respondent_attribution_status
    ) THEN
        RAISE EXCEPTION 'form response attribution is immutable' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.respondent_id IS DISTINCT FROM OLD.respondent_id
       AND NOT (OLD.respondent_id IS NOT NULL AND NEW.respondent_id IS NULL) THEN
        RAISE EXCEPTION 'form response bridge attribution is immutable' USING ERRCODE = '23514';
    END IF;

    IF NOT attribution_changed
       OR NEW.respondent_attribution_status NOT IN ('captured', 'resolved')
       OR NEW.respondent_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.respondent_principal_kind = 'student' THEN
        SELECT EXISTS (
            SELECT 1 FROM students_studentprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.respondent_principal_id
              AND profile.user_id = NEW.respondent_id
              AND profile.is_active AND bridge.is_active
        ) INTO principal_is_live;
    ELSIF NEW.respondent_principal_kind = 'teacher' THEN
        SELECT EXISTS (
            SELECT 1 FROM teachers_teacherprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.respondent_principal_id
              AND profile.user_id = NEW.respondent_id
              AND profile.is_active AND bridge.is_active
        ) INTO principal_is_live;
    ELSIF NEW.respondent_principal_kind = 'parent' THEN
        SELECT EXISTS (
            SELECT 1 FROM parents_parentprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.respondent_principal_id
              AND profile.user_id = NEW.respondent_id
              AND profile.is_active AND bridge.is_active
        ) INTO principal_is_live;
    ELSIF NEW.respondent_principal_kind = 'staff' THEN
        SELECT EXISTS (
            SELECT 1 FROM org_staffprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = NEW.respondent_principal_id
              AND profile.user_id = NEW.respondent_id
              AND profile.is_active AND bridge.is_active
        ) INTO principal_is_live;
    END IF;
    IF NOT principal_is_live THEN
        RAISE EXCEPTION 'form response principal is not active or owned by respondent'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER forms_response_principal_guard
BEFORE INSERT OR UPDATE OF respondent_id, respondent_principal_kind,
    respondent_principal_id, respondent_attribution_status
ON forms_app_formresponse
FOR EACH ROW EXECUTE FUNCTION forms_validate_response_principal();

CREATE TRIGGER forms_creator_principal_guard
BEFORE INSERT OR UPDATE OF created_by_id, created_by_principal_kind,
    created_by_principal_id, created_by_attribution_status
ON forms_app_form
FOR EACH ROW EXECUTE FUNCTION forms_validate_creator_principal();
"""


DROP_FORM_RESPONSE_PRINCIPAL_GUARD_SQL = r"""
DROP TRIGGER IF EXISTS forms_creator_principal_guard ON forms_app_form;
DROP TRIGGER IF EXISTS forms_response_principal_guard ON forms_app_formresponse;
DROP FUNCTION IF EXISTS forms_validate_creator_principal();
DROP FUNCTION IF EXISTS forms_validate_response_principal();
DROP FUNCTION IF EXISTS forms_staff_principal_is_owned(text, bigint, bigint);
"""


def reject_invalid_windows(apps, schema_editor):
    Form = apps.get_model("forms_app", "Form")
    invalid = Form.objects.using(schema_editor.connection.alias).filter(
        opens_at__isnull=False,
        closes_at__isnull=False,
        closes_at__lte=models.F("opens_at"),
    ).count()
    if invalid:
        raise RuntimeError(
            f"Cannot enforce form_close_after_open: {invalid} forms have an invalid response window. "
            "Review and repair those rows before retrying this migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("forms_app", "0002_form_audience_roles_form_audience_user_ids"),
        ("org", "0021_durable_center_settings"),
        ("parents", "0010_preserve_family_lifecycle_history"),
        ("students", "0011_protect_identity_history"),
        ("teachers", "0010_alter_payoutpolicy_method"),
    ]

    operations = [
        migrations.AddField(
            model_name="form",
            name="audience_principals",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="form",
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
            model_name="form",
            name="created_by_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="form",
            name="created_by_principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="formresponse",
            name="respondent_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="formresponse",
            name="respondent_principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="formresponse",
            name="respondent_attribution_status",
            field=models.CharField(
                choices=(
                    ("captured", "Captured"),
                    ("resolved", "Resolved from legacy data"),
                    ("anonymous", "Anonymous"),
                    ("unresolved", "Needs review"),
                    ("conflicting", "Conflicting legacy identity"),
                    ("quarantined", "Quarantined"),
                ),
                default="unresolved",
                max_length=16,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_principals, migrations.RunPython.noop),
        migrations.RunPython(reject_invalid_windows, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="formresponse",
            name="respondent_attribution_status",
            field=models.CharField(
                choices=(
                    ("captured", "Captured"),
                    ("resolved", "Resolved from legacy data"),
                    ("anonymous", "Anonymous"),
                    ("unresolved", "Needs review"),
                    ("conflicting", "Conflicting legacy identity"),
                    ("quarantined", "Quarantined"),
                ),
                default="quarantined",
                max_length=16,
            ),
        ),
        migrations.AddIndex(
            model_name="formresponse",
            index=models.Index(
                fields=["form", "respondent_principal_kind", "respondent_principal_id"],
                name="form_response_principal_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="form",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(opens_at__isnull=True)
                    | models.Q(closes_at__isnull=True)
                    | models.Q(closes_at__gt=models.F("opens_at"))
                ),
                name="form_close_after_open",
            ),
        ),
        migrations.AddConstraint(
            model_name="form",
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
                name="form_creator_principal_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="formresponse",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        respondent__isnull=True,
                        respondent_principal_kind="",
                        respondent_principal_id__isnull=True,
                        respondent_attribution_status="anonymous",
                    )
                    | (
                        models.Q(respondent_principal_kind__in=("student", "teacher", "parent", "staff"))
                        & models.Q(respondent_principal_id__isnull=False)
                        & models.Q(respondent_attribution_status__in=("captured", "resolved"))
                    )
                    | (
                        models.Q(respondent__isnull=False)
                        & models.Q(respondent_principal_kind="")
                        & models.Q(respondent_principal_id__isnull=True)
                        & models.Q(
                            respondent_attribution_status__in=(
                                "unresolved",
                                "conflicting",
                                "quarantined",
                            )
                        )
                    )
                ),
                name="form_response_principal_pair",
            ),
        ),
        migrations.RunSQL(
            FORM_RESPONSE_PRINCIPAL_GUARD_SQL,
            DROP_FORM_RESPONSE_PRINCIPAL_GUARD_SQL,
        ),
    ]
