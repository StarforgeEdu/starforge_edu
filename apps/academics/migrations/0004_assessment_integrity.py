from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Lower
from django.utils import timezone


def reject_case_insensitive_catalog_duplicates(apps, schema_editor):
    """Fail closed instead of silently merging catalogue rows during deploy."""
    from django.db.models import Count

    database = schema_editor.connection.alias
    for model_name, label in (("Subject", "subject"), ("ExamType", "exam type")):
        model = apps.get_model("academics", model_name)
        duplicates = list(
            model.objects.using(database)
            .annotate(normalized=Lower("name"))
            .values("normalized")
            .annotate(total=Count("id"))
            .filter(total__gt=1)
            .values_list("normalized", flat=True)[:10]
        )
        if duplicates:
            values = ", ".join(repr(value) for value in duplicates)
            raise RuntimeError(
                f"Resolve duplicate {label} names before applying academics.0004: {values}"
            )


def invalidate_unverified_legacy_grades(apps, schema_editor):
    """Never certify a legacy computed grade without recomputing its evidence."""

    Grade = apps.get_model("academics", "Grade")
    Grade.objects.using(schema_editor.connection.alias).update(
        is_valid=False,
        invalidated_at=timezone.now(),
        invalidation_reason="legacy_unverified",
    )


def reject_invalid_legacy_assessment_values(apps, schema_editor):
    """Stop before installing guards around already-invalid score evidence."""

    Exam = apps.get_model("academics", "Exam")
    ExamResult = apps.get_model("academics", "ExamResult")
    database = schema_editor.connection.alias
    invalid_exam_ids: list[int] = []
    invalid_exam_count = 0
    for exam_id, max_score, weight in (
        Exam.objects.using(database)
        .values_list("pk", "max_score", "weight")
        .iterator(chunk_size=2_000)
    ):
        if (
            max_score is None
            or not max_score.is_finite()
            or max_score <= 0
            or weight is None
            or not weight.is_finite()
            or weight <= 0
        ):
            invalid_exam_count += 1
            if len(invalid_exam_ids) < 20:
                invalid_exam_ids.append(exam_id)

    invalid_result_ids: list[int] = []
    invalid_result_count = 0
    for result_id, score, max_score in (
        ExamResult.objects.using(database)
        .values_list("pk", "score", "exam__max_score")
        .iterator(chunk_size=2_000)
    ):
        if (
            score is None
            or not score.is_finite()
            or max_score is None
            or not max_score.is_finite()
            or score < 0
            or score > max_score
        ):
            invalid_result_count += 1
            if len(invalid_result_ids) < 20:
                invalid_result_ids.append(result_id)

    if invalid_exam_count or invalid_result_count:
        raise RuntimeError(
            "Assessment integrity preflight failed; repair invalid numeric evidence before retrying: "
            f"invalid_exams={invalid_exam_count}, exam_ids={invalid_exam_ids}, "
            f"invalid_results={invalid_result_count}, result_ids={invalid_result_ids}"
        )


ACADEMIC_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION academics_guard_exam_integrity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    trusted_write boolean := COALESCE(
        current_setting('starforge.academic_integrity_write', true) = 'on',
        false
    );
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_published
           OR OLD.requires_republish
           OR EXISTS (SELECT 1 FROM academics_examresult WHERE exam_id = OLD.id)
           OR EXISTS (SELECT 1 FROM academics_examlifecycleevent WHERE exam_id = OLD.id) THEN
            RAISE EXCEPTION 'assessment evidence cannot be deleted'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;

    IF (OLD.subject_id IS DISTINCT FROM NEW.subject_id
        OR OLD.cohort_id IS DISTINCT FROM NEW.cohort_id
        OR OLD.term_id IS DISTINCT FROM NEW.term_id)
       AND EXISTS (SELECT 1 FROM academics_examresult WHERE exam_id = OLD.id) THEN
        RAISE EXCEPTION 'an exam with recorded results cannot be moved'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.max_score IS DISTINCT FROM NEW.max_score
       AND EXISTS (
           SELECT 1
           FROM academics_examresult
           WHERE exam_id = OLD.id AND score > NEW.max_score
       ) THEN
        RAISE EXCEPTION 'maximum score is below an existing result'
            USING ERRCODE = '23514';
    END IF;

    IF (OLD.is_published OR OLD.requires_republish)
       AND NOT trusted_write
       AND (
           OLD.subject_id IS DISTINCT FROM NEW.subject_id
           OR OLD.cohort_id IS DISTINCT FROM NEW.cohort_id
           OR OLD.term_id IS DISTINCT FROM NEW.term_id
           OR OLD.exam_type_id IS DISTINCT FROM NEW.exam_type_id
           OR OLD.title IS DISTINCT FROM NEW.title
           OR OLD.exam_date IS DISTINCT FROM NEW.exam_date
           OR OLD.max_score IS DISTINCT FROM NEW.max_score
           OR OLD.weight IS DISTINCT FROM NEW.weight
           OR OLD.is_published IS DISTINCT FROM NEW.is_published
           OR OLD.published_at IS DISTINCT FROM NEW.published_at
           OR OLD.version IS DISTINCT FROM NEW.version
           OR OLD.requires_republish IS DISTINCT FROM NEW.requires_republish
       ) THEN
        RAISE EXCEPTION 'published assessments require the correction workflow'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS academics_exam_integrity_guard ON academics_exam;
CREATE TRIGGER academics_exam_integrity_guard
BEFORE UPDATE OR DELETE ON academics_exam
FOR EACH ROW EXECUTE FUNCTION academics_guard_exam_integrity();

CREATE OR REPLACE FUNCTION academics_guard_result_integrity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_exam_id bigint;
    exam_locked boolean;
    exam_max numeric;
    trusted_write boolean := COALESCE(
        current_setting('starforge.academic_integrity_write', true) = 'on',
        false
    );
BEGIN
    target_exam_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.exam_id ELSE NEW.exam_id END;
    SELECT (is_published OR requires_republish), max_score
      INTO exam_locked, exam_max
      FROM academics_exam
     WHERE id = target_exam_id;

    IF TG_OP <> 'DELETE' THEN
        IF NEW.score::text IN ('NaN', 'Infinity', '-Infinity')
           OR NEW.score < 0
           OR NEW.score > exam_max THEN
            RAISE EXCEPTION 'result score is outside the exam range'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE'
       AND (OLD.exam_id IS DISTINCT FROM NEW.exam_id
            OR OLD.student_id IS DISTINCT FROM NEW.student_id) THEN
        RAISE EXCEPTION 'result identity is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF exam_locked AND NOT trusted_write THEN
        RAISE EXCEPTION 'published results require the correction workflow'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS academics_result_integrity_guard ON academics_examresult;
CREATE TRIGGER academics_result_integrity_guard
BEFORE INSERT OR UPDATE OR DELETE ON academics_examresult
FOR EACH ROW EXECUTE FUNCTION academics_guard_result_integrity();

CREATE OR REPLACE FUNCTION academics_reject_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Preserve history while permitting the actor FK's SET NULL maintenance.
    IF TG_OP = 'UPDATE'
       AND OLD.actor_id IS NOT NULL
       AND NEW.actor_id IS NULL
       AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'exam lifecycle history is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS academics_exam_history_immutable ON academics_examlifecycleevent;
CREATE TRIGGER academics_exam_history_immutable
BEFORE UPDATE OR DELETE ON academics_examlifecycleevent
FOR EACH ROW EXECUTE FUNCTION academics_reject_history_mutation();
"""


REVERSE_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS academics_exam_history_immutable ON academics_examlifecycleevent;
DROP FUNCTION IF EXISTS academics_reject_history_mutation();
DROP TRIGGER IF EXISTS academics_result_integrity_guard ON academics_examresult;
DROP FUNCTION IF EXISTS academics_guard_result_integrity();
DROP TRIGGER IF EXISTS academics_exam_integrity_guard ON academics_exam;
DROP FUNCTION IF EXISTS academics_guard_exam_integrity();
"""


class Migration(migrations.Migration):
    dependencies = [("academics", "0003_examtype_remove_exam_type_exam_exam_type")]

    operations = [
        migrations.RunPython(
            reject_invalid_legacy_assessment_values,
            migrations.RunPython.noop,
        ),
        migrations.AddField(
            model_name="exam",
            name="requires_republish",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="exam",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="grade",
            name="invalidated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="grade",
            name="invalidation_reason",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="grade",
            name="is_valid",
            field=models.BooleanField(db_index=True, default=False),
            preserve_default=False,
        ),
        migrations.RunPython(
            invalidate_unverified_legacy_grades,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="grade",
            name="is_valid",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AlterField(
            model_name="exam",
            name="exam_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="exams",
                to="academics.examtype",
            ),
        ),
        migrations.AlterField(
            model_name="examresult",
            name="exam",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="results",
                to="academics.exam",
            ),
        ),
        migrations.CreateModel(
            name="ExamLifecycleEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[("published", "Published"), ("corrected", "Corrected")], max_length=16)),
                ("exam_version", models.PositiveIntegerField()),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("details", models.JSONField(default=dict)),
                ("actor_repr", models.CharField(blank=True, max_length=255)),
                ("branch_id_snapshot", models.PositiveBigIntegerField()),
                ("department_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="users.user")),
                ("exam", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lifecycle_events", to="academics.exam")),
            ],
            options={
                "ordering": ("-created_at", "-id"),
                "indexes": [
                    models.Index(fields=["exam", "-created_at", "-id"], name="exam_history_time_idx"),
                    models.Index(fields=["branch_id_snapshot", "department_id_snapshot", "-created_at"], name="exam_history_scope_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("exam", "event_type", "exam_version"), name="exam_lifecycle_unique_version_event"),
                    models.CheckConstraint(
                        condition=(
                            models.Q(event_type="published")
                            | (models.Q(event_type="corrected") & ~models.Q(reason=""))
                        ),
                        name="exam_correction_reason_required",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            reject_case_insensitive_catalog_duplicates,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(Lower("name"), name="subject_name_unique_ci"),
        ),
        migrations.AddConstraint(
            model_name="examtype",
            constraint=models.UniqueConstraint(Lower("name"), name="exam_type_name_unique_ci"),
        ),
        migrations.AddIndex(
            model_name="exam",
            index=models.Index(
                fields=["cohort", "is_published", "exam_date"],
                name="exam_cohort_pub_date_idx",
            ),
        ),
        migrations.RunSQL(ACADEMIC_GUARDS_SQL, REVERSE_GUARDS_SQL),
    ]
