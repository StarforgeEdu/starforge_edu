import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

CREATE_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION parents_reject_history_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'family identity history cannot be deleted'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION parents_guard_profile_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.branch_at_creation_id IS DISTINCT FROM OLD.branch_at_creation_id
       OR NEW.department_at_creation_id IS DISTINCT FROM OLD.department_at_creation_id
       OR NEW.attribution_status IS DISTINCT FROM OLD.attribution_status
       OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'parent identity attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION parents_guard_guardian_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'revoked guardian history is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.parent_id IS DISTINCT FROM OLD.parent_id
       OR NEW.student_id IS DISTINCT FROM OLD.student_id
       OR NEW.relationship IS DISTINCT FROM OLD.relationship
       OR NEW.is_primary IS DISTINCT FROM OLD.is_primary
       OR NEW.custody_notes IS DISTINCT FROM OLD.custody_notes THEN
        RAISE EXCEPTION 'guardian relationship history is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revoked_at IS NULL AND NEW.revoked_by_id IS NOT NULL THEN
        RAISE EXCEPTION 'guardian revocation timestamp is required'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION parents_guard_pickup_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF OLD.is_active = FALSE THEN
        RAISE EXCEPTION 'deactivated pickup history is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.student_id IS DISTINCT FROM OLD.student_id THEN
        RAISE EXCEPTION 'pickup student attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.is_active = FALSE AND NEW.deactivated_at IS NULL THEN
        RAISE EXCEPTION 'pickup deactivation timestamp is required'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.is_active = TRUE
       AND (NEW.deactivated_at IS NOT NULL OR NEW.deactivated_by_id IS NOT NULL) THEN
        RAISE EXCEPTION 'active pickup authorization has deactivation evidence'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS parents_profile_reject_delete ON parents_parentprofile;
CREATE TRIGGER parents_profile_reject_delete
BEFORE DELETE ON parents_parentprofile
FOR EACH ROW EXECUTE FUNCTION parents_reject_history_delete();

DROP TRIGGER IF EXISTS parents_profile_guard_identity ON parents_parentprofile;
CREATE TRIGGER parents_profile_guard_identity
BEFORE UPDATE ON parents_parentprofile
FOR EACH ROW EXECUTE FUNCTION parents_guard_profile_identity();

DROP TRIGGER IF EXISTS parents_guardian_reject_delete ON parents_guardian;
CREATE TRIGGER parents_guardian_reject_delete
BEFORE DELETE ON parents_guardian
FOR EACH ROW EXECUTE FUNCTION parents_reject_history_delete();

DROP TRIGGER IF EXISTS parents_guardian_guard_history ON parents_guardian;
CREATE TRIGGER parents_guardian_guard_history
BEFORE UPDATE ON parents_guardian
FOR EACH ROW EXECUTE FUNCTION parents_guard_guardian_history();

DROP TRIGGER IF EXISTS parents_pickup_reject_delete ON parents_pickupauthorization;
CREATE TRIGGER parents_pickup_reject_delete
BEFORE DELETE ON parents_pickupauthorization
FOR EACH ROW EXECUTE FUNCTION parents_reject_history_delete();

DROP TRIGGER IF EXISTS parents_pickup_guard_history ON parents_pickupauthorization;
CREATE TRIGGER parents_pickup_guard_history
BEFORE UPDATE ON parents_pickupauthorization
FOR EACH ROW EXECUTE FUNCTION parents_guard_pickup_history();
"""


DROP_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS parents_pickup_guard_history ON parents_pickupauthorization;
DROP TRIGGER IF EXISTS parents_pickup_reject_delete ON parents_pickupauthorization;
DROP TRIGGER IF EXISTS parents_guardian_guard_history ON parents_guardian;
DROP TRIGGER IF EXISTS parents_guardian_reject_delete ON parents_guardian;
DROP TRIGGER IF EXISTS parents_profile_guard_identity ON parents_parentprofile;
DROP TRIGGER IF EXISTS parents_profile_reject_delete ON parents_parentprofile;
DROP FUNCTION IF EXISTS parents_guard_pickup_history();
DROP FUNCTION IF EXISTS parents_guard_guardian_history();
DROP FUNCTION IF EXISTS parents_guard_profile_identity();
DROP FUNCTION IF EXISTS parents_reject_history_delete();
"""


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("parents", "0009_encrypt_safeguarding_text"),
        ("students", "0011_protect_identity_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="guardian",
            name="revoked_at",
            field=models.DateTimeField(blank=True, db_index=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="guardian",
            name="revoked_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pickupauthorization",
            name="deactivated_at",
            field=models.DateTimeField(blank=True, db_index=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="pickupauthorization",
            name="deactivated_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterUniqueTogether(name="guardian", unique_together=set()),
        migrations.AlterField(
            model_name="parentprofile",
            name="user",
            field=models.OneToOneField(
                editable=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="parent_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="parentprofile",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="created_parent_profiles",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="guardian",
            name="one_primary_guardian_per_student",
        ),
        migrations.AlterField(
            model_name="guardian",
            name="parent",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="guardianships",
                to="parents.parentprofile",
            ),
        ),
        migrations.AlterField(
            model_name="guardian",
            name="student",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="guardians",
                to="students.studentprofile",
            ),
        ),
        migrations.AlterField(
            model_name="pickupauthorization",
            name="student",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pickup_authorizations",
                to="students.studentprofile",
            ),
        ),
        migrations.AddConstraint(
            model_name="guardian",
            constraint=models.UniqueConstraint(
                condition=models.Q(("revoked_at__isnull", True)),
                fields=("parent", "student"),
                name="one_active_guardian_link",
            ),
        ),
        migrations.AddConstraint(
            model_name="guardian",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True), ("revoked_at__isnull", True)),
                fields=("student",),
                name="one_primary_guardian_per_student",
            ),
        ),
        migrations.AddIndex(
            model_name="guardian",
            index=models.Index(
                fields=["student", "revoked_at", "is_primary"],
                name="guardian_active_student_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="guardian",
            index=models.Index(
                fields=["parent", "revoked_at"],
                name="guardian_active_parent_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="pickupauthorization",
            index=models.Index(
                fields=["student", "is_active", "-created_at"],
                name="pickup_student_active_idx",
            ),
        ),
        migrations.RunSQL(CREATE_GUARDS_SQL, reverse_sql=DROP_GUARDS_SQL),
    ]
