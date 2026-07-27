"""Seed a dev environment with a demo tenant + admin user.

After running, open http://demo.localhost:8000/admin/ (or hit the API
on demo.localhost:8000). Idempotent.
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from datetime import date  # noqa: E402

from django_tenants.utils import schema_context  # noqa: E402

from apps.tenancy.models import Center  # noqa: E402
from apps.tenancy.services import provision_center  # noqa: E402
from apps.users.models import User  # noqa: E402


# Dev storage bootstrap (D2-E-8): expire abandoned tmp uploads, allow browser PUT.
def _tmp_lifecycle_config() -> dict:
    """One expire-tmp rule per Center schema, matching the schema-first key
    layout (`{schema}/tmp/...`, see services.request_upload). A bare `tmp/`
    prefix never matched the real keys, so abandoned/rejected tmp blobs never
    expired. Idempotent: re-running rebuilds the same rule set from the current
    Center list. A new Center created after bootstrap needs a re-run (or the
    per-schema sweep task) to be covered — acceptable for dev seeding."""
    schemas = sorted(Center.objects.values_list("schema_name", flat=True))
    return {
        "Rules": [
            {
                "ID": f"expire-tmp-uploads-{schema}",
                "Filter": {"Prefix": f"{schema}/tmp/"},
                "Status": "Enabled",
                "Expiration": {"Days": 7},
            }
            for schema in schemas
        ]
    }


_DEV_CORS = {
    "CORSRules": [
        {
            "AllowedMethods": ["PUT", "GET", "HEAD"],
            "AllowedOrigins": ["*"],  # dev/MinIO only; prod CORS is [OWNER:O-9]
            "AllowedHeaders": ["*"],
            "MaxAgeSeconds": 3000,
        }
    ]
}


def bootstrap_dev_storage(client=None, *, bucket: str | None = None) -> bool:
    """Create the dev bucket + per-schema tmp lifecycle + CORS if absent.
    Idempotent (a re-run is a no-op); returns False (with a warning) if storage
    is unreachable so `seed_dev` still succeeds without a running MinIO.

    Upload keys are schema-first (`{schema}/tmp/...`), so the lifecycle config
    carries one expire-tmp rule per Center schema (see `_tmp_lifecycle_config`);
    the validate task also deletes the tmp object on both the happy and reject
    paths. A per-schema sweep task is the future hardening for crashed validates.
    """
    from typing import Any, cast

    from django.conf import settings as dj_settings

    opts = cast(dict[str, Any], dj_settings.STORAGES["default"]).get("OPTIONS", {})
    bucket = bucket or opts.get("bucket_name")
    if not bucket:
        print("storage bootstrap skipped: no bucket configured (test/local FS storage)")
        return False
    if client is None:
        from infrastructure.storage.s3_client import get_s3_client

        client = get_s3_client()
    try:
        names = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
        if bucket not in names:
            client.create_bucket(Bucket=bucket)
            print(f"created bucket {bucket}")
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket, LifecycleConfiguration=_tmp_lifecycle_config()
        )
        client.put_bucket_cors(Bucket=bucket, CORSConfiguration=_DEV_CORS)
        print(f"storage bootstrap ok: bucket {bucket} (+ tmp lifecycle, CORS)")
        return True
    except Exception as exc:  # MinIO down / creds wrong — don't fail the whole seed
        print(f"storage bootstrap skipped (storage unreachable): {exc}")
        return False


def _seed_demo_domain(actor: User) -> None:
    """Seed a small, idempotent people domain for manual testing (D1-LD-10)."""
    from apps.cohorts.models import Cohort
    from apps.org.models import Branch, Department
    from apps.parents.services import create_parent, link_guardian
    from apps.students.models import StudentProfile
    from apps.students.services import create_student
    from apps.teachers.services import create_teacher
    from apps.users.models import RoleMembership
    from core.permissions import Role

    branch, _ = Branch.objects.get_or_create(slug="main", defaults={"name": "Main Branch"})
    department, _ = Department.objects.get_or_create(
        branch=branch, slug="general", defaults={"name": "General Studies"}
    )
    RoleMembership.objects.get_or_create(user=actor, branch=branch, department=None, role=Role.DIRECTOR)

    for i in (1, 2):
        phone = f"+99890111110{i}"
        if not User.objects.filter(phone=phone).exists():
            create_teacher(
                branch=branch,
                department=department,
                phone=phone,
                first_name=f"Teacher{i}",
                last_name="Demo",
            )

    cohort, _ = Cohort.objects.get_or_create(
        branch=branch,
        name="Group A",
        defaults={
            "department": department,
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 12, 31),
        },
    )

    from apps.cohorts.services import enroll_student_in_cohort

    for i in range(1, 6):
        phone = f"+99890222220{i}"
        if not User.objects.filter(phone=phone).exists():
            student = create_student(
                branch=branch,
                phone=phone,
                first_name=f"Student{i}",
                last_name="Demo",
                status=StudentProfile.Status.ACTIVE,
            )
            enroll_student_in_cohort(cohort=cohort, student=student)

    students = list(StudentProfile.objects.order_by("id")[:2])
    for i in (1, 2):
        phone = f"+99890333330{i}"
        if not User.objects.filter(phone=phone).exists():
            parent = create_parent(phone=phone, first_name=f"Parent{i}", last_name="Demo")
            if i - 1 < len(students):
                link_guardian(
                    parent=parent,
                    student=students[i - 1],
                    relationship="mother" if i == 1 else "father",
                    is_primary=True,
                )
    print("seeded demo domain: 1 branch, 1 department, 2 teachers, 1 cohort, 5 students, 2 parents")


def main() -> None:
    # Map the apex host to the public schema — without this Domain row,
    # django-tenants 404s http://localhost:8000/admin/ entirely.
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Domain

    public_center, _ = Center.objects.get_or_create(
        schema_name=get_public_schema_name(),
        defaults={"name": "Platform", "slug": "platform"},
    )
    Domain.objects.get_or_create(domain="localhost", tenant=public_center, defaults={"is_primary": True})

    bootstrap_dev_storage()

    # Platform staff live in the public schema (TD-3 / ADR-007) so the apex
    # /admin/ and the platform API work. Login is username+password.
    platform_admin, p_created = User.objects.get_or_create(
        username="admin",
        defaults={
            "phone": "+998900000000",
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )
    if p_created or not platform_admin.has_usable_password():
        platform_admin.set_password("starforge-platform")
        platform_admin.is_staff = True
        platform_admin.is_superuser = True
        platform_admin.save()
        print("created PLATFORM superuser admin / starforge-platform (apex /admin/)")
    else:
        print("platform superuser already exists")

    slug = "demo"
    hostname = "demo.localhost"
    if not Center.objects.filter(schema_name=slug).exists():
        provision_center(
            name="Demo Education Center",
            slug=slug,
            primary_domain=hostname,
            contact_name="Demo Admin",
            contact_phone="+998901234567",
            contact_email="admin@demo.localhost",
        )
        print(f"created Center {slug} @ {hostname}")
    else:
        print(f"Center {slug} already exists")

    with schema_context(slug):
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={
                "phone": "+998901234567",
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )
        if created or not admin.has_usable_password():
            admin.set_password("starforge-dev")
            admin.is_staff = True
            admin.is_superuser = True
            admin.save()
            print("created tenant superuser admin / starforge-dev (demo.localhost)")
        else:
            print("superuser already exists")

        _seed_demo_domain(admin)


if __name__ == "__main__":
    main()
