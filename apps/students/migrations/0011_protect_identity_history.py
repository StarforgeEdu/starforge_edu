import django.db.models.deletion
from django.db import migrations, models

CREATE_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION students_reject_profile_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'student identity history cannot be deleted'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION students_guard_profile_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.student_id IS DISTINCT FROM OLD.student_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'student identity attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION students_reject_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.identity_history_maintenance', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'student enrollment history is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS students_profile_reject_delete ON students_studentprofile;
CREATE TRIGGER students_profile_reject_delete
BEFORE DELETE ON students_studentprofile
FOR EACH ROW EXECUTE FUNCTION students_reject_profile_delete();

DROP TRIGGER IF EXISTS students_profile_guard_identity ON students_studentprofile;
CREATE TRIGGER students_profile_guard_identity
BEFORE UPDATE ON students_studentprofile
FOR EACH ROW EXECUTE FUNCTION students_guard_profile_identity();

DROP TRIGGER IF EXISTS students_reason_reject_delete ON students_enrollmentreason;
CREATE TRIGGER students_reason_reject_delete
BEFORE DELETE ON students_enrollmentreason
FOR EACH ROW EXECUTE FUNCTION students_reject_profile_delete();

DROP TRIGGER IF EXISTS students_event_reject_mutation ON students_enrollmentevent;
CREATE TRIGGER students_event_reject_mutation
BEFORE UPDATE OR DELETE ON students_enrollmentevent
FOR EACH ROW EXECUTE FUNCTION students_reject_event_mutation();
"""


DROP_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS students_event_reject_mutation ON students_enrollmentevent;
DROP TRIGGER IF EXISTS students_reason_reject_delete ON students_enrollmentreason;
DROP TRIGGER IF EXISTS students_profile_guard_identity ON students_studentprofile;
DROP TRIGGER IF EXISTS students_profile_reject_delete ON students_studentprofile;
DROP FUNCTION IF EXISTS students_reject_event_mutation();
DROP FUNCTION IF EXISTS students_guard_profile_identity();
DROP FUNCTION IF EXISTS students_reject_profile_delete();
"""


class Migration(migrations.Migration):
    dependencies = [("students", "0010_encrypt_emergency_contacts")]

    operations = [
        migrations.AlterField(
            model_name="studentprofile",
            name="user",
            field=models.OneToOneField(
                editable=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="student_profile",
                to="users.user",
            ),
        ),
        migrations.AlterField(
            model_name="enrollmentevent",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="users.user",
            ),
        ),
        migrations.AlterField(
            model_name="enrollmentevent",
            name="student",
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="enrollment_events",
                to="students.studentprofile",
            ),
        ),
        migrations.RunSQL(CREATE_GUARDS_SQL, reverse_sql=DROP_GUARDS_SQL),
    ]
