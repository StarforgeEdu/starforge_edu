"""Seed a large, deterministic education-centre simulation into one tenant.

This command is intentionally separate from the local demo seed.  It is an
operator-only load-data tool for staging/development tenants that happen to run
with production settings.  It never calls provider-facing services and every
row it owns carries a deterministic seed identifier, making interrupted runs
safe to resume.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from datetime import time as wall_time
from decimal import Decimal
from itertools import batched, pairwise

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Max
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.tenancy.models import Center

_SEED_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
_BATCH_SIZE = 2_000
_MIN_STUDENTS = 1_001
_MIN_TEACHERS = 51
_MAX_STUDENTS = 1_320
_MAX_TEACHERS = 60

_FIRST_NAMES = (
    "Aziza",
    "Bekzod",
    "Dilnoza",
    "Diyor",
    "Farhod",
    "Gulnoza",
    "Humoyun",
    "Iroda",
    "Jasur",
    "Kamola",
    "Laylo",
    "Madina",
    "Malika",
    "Muhammad",
    "Nargiza",
    "Nodira",
    "Oybek",
    "Sabina",
    "Sardor",
    "Shahzod",
    "Timur",
    "Umida",
    "Zarina",
    "Ziyoda",
)
_LAST_NAMES = (
    "Abdullayev",
    "Aliyev",
    "Ergashev",
    "Ismoilov",
    "Karimov",
    "Khasanov",
    "Mamatov",
    "Nematov",
    "Rahimov",
    "Rasulov",
    "Saidov",
    "Toshpulatov",
    "Usmonov",
    "Yuldashev",
)
_MESSAGE_TEXT = (
    "Hello, could you please confirm tomorrow's English lesson time?",
    "Today's speaking practice was useful. I will review the new vocabulary.",
    "Please remember to complete the reading exercise before the next lesson.",
    "Thank you, the homework instructions are clear now.",
    "We worked on pronunciation, listening, and a short conversation today.",
    "Could you share one more example for the grammar topic?",
    "The group will start with a ten-minute vocabulary review next lesson.",
    "I submitted the exercise and noted the words I need to practise.",
    "Excellent progress this week. Keep reading aloud for fifteen minutes daily.",
    "I may arrive a few minutes late, but I will attend the lesson.",
)


@dataclass(frozen=True)
class SeedConfig:
    schema: str
    seed_id: str
    random_seed: int
    as_of: str
    students: int
    teachers: int
    history_days: int

    @property
    def as_of_date(self) -> date:
        return date.fromisoformat(self.as_of)

    @property
    def marker(self) -> str:
        return f"[simulation:{self.seed_id}]"

    @property
    def username_token(self) -> str:
        return self.seed_id.replace("-", ".")

    @property
    def confirmation(self) -> str:
        return f"{self.schema}:{self.seed_id}:{self.students}:{self.teachers}"


def _month_shift(value: date, offset: int) -> date:
    absolute = value.year * 12 + value.month - 1 + offset
    year, month_index = divmod(absolute, 12)
    return date(year, month_index + 1, 1)


def _aware(on: date, hour: int, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime.combine(on, wall_time(hour, minute)))


def _lesson_dates(config: SeedConfig) -> list[date]:
    start = config.as_of_date - timedelta(days=config.history_days)
    return [
        start + timedelta(days=offset)
        for offset in range(config.history_days + 1)
        if (start + timedelta(days=offset)).weekday() in (0, 2, 4)
    ]


def _plan(config: SeedConfig) -> dict[str, int | str]:
    lesson_count = len(_lesson_dates(config))
    parent_count = sum((count + 1) // 2 for count in _branch_student_counts(config))
    crm_prospects = min(120, max(12, config.students // 10))
    support_threads = config.students
    payload: dict[str, int | str] = {
        "schema": config.schema,
        "seed_id": config.seed_id,
        "branches": 3,
        "departments": 3,
        "rooms": 30,
        "teachers": config.teachers,
        "students": config.students,
        "crm_prospects": crm_prospects,
        "parents": parent_count,
        "guardians": config.students,
        "cohorts": config.teachers,
        "completed_lessons": config.teachers * lesson_count,
        "scheduled_lessons": config.teachers,
        "attendance_records": config.students * lesson_count,
        "invoices": config.students * 12,
        "invoice_lines": config.students * 12,
        "payments_estimate": sum(
            bool(
                _invoice_payment_fraction(
                    student_index,
                    month_index,
                    current=month_index == 12,
                )
            )
            for student_index in range(1, config.students + 1)
            for month_index in range(1, 13)
        ),
        "assignments": config.teachers * 4,
        "exam_results": config.students * 4,
        "threads": support_threads,
        "messages": support_threads * 16,
        "notifications": config.students * 2,
        "history_days": config.history_days,
    }
    return payload


def _plan_digest(config: SeedConfig) -> str:
    encoded = json.dumps(
        {"config": asdict(config), "plan": _plan(config)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _branch_student_counts(config: SeedConfig) -> list[int]:
    counts = [0, 0, 0]
    for index in range(config.students):
        counts[(index % config.teachers) % 3] += 1
    return counts


class Command(BaseCommand):
    help = "Seed a tagged 1,000+ learner English-centre simulation into one tenant."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", required=True)
        parser.add_argument("--seed-id", required=True)
        parser.add_argument("--random-seed", type=int, default=20260810)
        parser.add_argument("--as-of", default=date.today().isoformat())
        parser.add_argument("--students", type=int, default=1_200)
        parser.add_argument("--teachers", type=int, default=60)
        parser.add_argument("--history-days", type=int, default=370)
        parser.add_argument("--plan", action="store_true")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--confirm", default="")
        parser.add_argument("--plan-digest", default="")
        parser.add_argument("--json", action="store_true", dest="json_output")

    def handle(self, *args, **options) -> None:
        config = self._config(options)
        plan = _plan(config)
        digest = _plan_digest(config)
        response = {"config": asdict(config), "plan": plan, "plan_digest": digest}
        if options["plan"] and not options["execute"]:
            self._write(response, json_output=options["json_output"])
            return
        if not options["execute"]:
            raise CommandError("Choose --plan or explicitly choose --execute.")
        self._authorize_execution(config, options, digest)
        self._preflight(config)

        started = time.monotonic()
        lock_key = int.from_bytes(
            hashlib.sha256(f"starforge-peak-seed:{config.schema}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_key])
            if not cursor.fetchone()[0]:
                raise CommandError("Another process is already running this simulation seed.")
        try:
            with schema_context(config.schema):
                self._execute(config)
                verification = _verification(config)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_key])

        response["duration_seconds"] = round(time.monotonic() - started, 3)
        response["verification"] = verification
        response["verification_digest"] = hashlib.sha256(
            json.dumps(verification, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._write(response, json_output=options["json_output"])

    def _config(self, options) -> SeedConfig:
        schema = str(options["schema"]).strip()
        seed_id = str(options["seed_id"]).strip()
        if not schema or schema == get_public_schema_name():
            raise CommandError("--schema must name one non-public tenant.")
        if not _SEED_ID.fullmatch(seed_id):
            raise CommandError("--seed-id must match [a-z0-9][a-z0-9-]{2,31}.")
        try:
            as_of = date.fromisoformat(str(options["as_of"])).isoformat()
        except ValueError as exc:
            raise CommandError("--as-of must be an ISO date.") from exc
        students = int(options["students"])
        teachers = int(options["teachers"])
        history_days = int(options["history_days"])
        if not _MIN_STUDENTS <= students <= _MAX_STUDENTS:
            raise CommandError(f"--students must be {_MIN_STUDENTS}..{_MAX_STUDENTS}.")
        if not _MIN_TEACHERS <= teachers <= _MAX_TEACHERS:
            raise CommandError(f"--teachers must be {_MIN_TEACHERS}..{_MAX_TEACHERS}.")
        if teachers > students:
            raise CommandError("--teachers cannot exceed --students.")
        if not 370 <= history_days <= 730:
            raise CommandError("--history-days must be 370..730.")
        return SeedConfig(
            schema=schema,
            seed_id=seed_id,
            random_seed=int(options["random_seed"]),
            as_of=as_of,
            students=students,
            teachers=teachers,
            history_days=history_days,
        )

    def _authorize_execution(self, config: SeedConfig, options, digest: str) -> None:
        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if settings.DEBUG or not settings_module.endswith(".production"):
            raise CommandError("Execution is reserved for the production settings module.")
        if os.environ.get("STARFORGE_ALLOW_PRODUCTION_SEED") != config.seed_id:
            raise CommandError("STARFORGE_ALLOW_PRODUCTION_SEED must exactly equal --seed-id.")
        if options["confirm"] != config.confirmation:
            raise CommandError(f"--confirm must exactly equal {config.confirmation!r}.")
        if options["plan_digest"] != digest:
            raise CommandError("--plan-digest does not match this exact seed plan.")

    def _preflight(self, config: SeedConfig) -> None:
        with schema_context(get_public_schema_name()):
            center = Center.objects.filter(
                schema_name=config.schema,
                is_active=True,
                archived_at__isnull=True,
            ).first()
        if center is None:
            raise CommandError(f"No active tenant uses schema {config.schema!r}.")
        with schema_context(config.schema):
            executor = MigrationExecutor(connection)
            if executor.migration_plan(executor.loader.graph.leaf_nodes()):
                raise CommandError("Tenant migrations are pending; seed execution refused.")

    def _execute(self, config: SeedConfig) -> None:
        rng = random.Random(config.random_seed)
        actor = _director_actor()
        structure = _ensure_structure(config)
        teachers = _ensure_teachers(config, structure, rng)
        cohorts = _ensure_cohorts(config, structure, teachers)
        students = _ensure_students(config, structure, cohorts, rng)
        _ensure_parents(config, students, actor)
        term, subject = _ensure_learning_history(config, cohorts, students)
        _ensure_finance_history(config, cohorts, students, actor)
        _ensure_academics(config, cohorts, students, term, subject, actor)
        _ensure_messaging(config, cohorts, students)
        _ensure_operations(config, structure, teachers, students, actor)

    def _write(self, payload: dict, *, json_output: bool) -> None:
        if json_output:
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))


def _director_actor():
    from apps.org.models import StaffProfile
    from apps.users.models import RoleMembership

    membership = (
        RoleMembership.objects.filter(
            account_type__slug="director",
            account_type__is_active=True,
            revoked_at__isnull=True,
            user__is_active=True,
            user__staff_profile__is_active=True,
        )
        .select_related("user", "user__staff_profile")
        .order_by("id")
        .first()
    )
    if membership is None:
        raise CommandError("The tenant has no active role-native director for seed attribution.")
    actor = StaffProfile.objects.filter(user=membership.user, is_active=True).first()
    if actor is None:
        raise CommandError("The selected director principal is unavailable.")
    return actor


def _ensure_structure(config: SeedConfig) -> dict:
    from apps.org.models import Branch, BranchWorkingHours, Department, Room

    manifest = json.dumps(
        {"config": asdict(config), "plan_digest": _plan_digest(config)},
        sort_keys=True,
        separators=(",", ":"),
    )
    specs = (
        ("central", "Central English Campus", "Amir Temur Avenue"),
        ("yunusabad", "Yunusabad English Campus", "Yunusabad District"),
        ("chilanzar", "Chilanzar English Campus", "Bunyodkor Avenue"),
    )
    branches = []
    departments = []
    rooms = []
    with transaction.atomic():
        for short_slug, display_name, address in specs:
            slug = f"sim-{config.seed_id}-{short_slug}"[:100]
            branch, created = Branch.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": f"{config.marker} {display_name}",
                    "address": address,
                    "phone": "",
                    "timezone": "Asia/Tashkent",
                    "is_active": True,
                    "max_students": max(500, config.students // 3 + 100),
                    "max_teachers": max(30, config.teachers // 3 + 10),
                },
            )
            if not created and not branch.name.startswith(config.marker):
                raise CommandError(f"Seed branch slug collision: {slug}")
            branches.append(branch)
            department, department_created = Department.objects.get_or_create(
                branch=branch,
                slug=f"sim-{config.seed_id}-english"[:100],
                defaults={
                    "name": f"{config.marker} English Studies",
                    "description": manifest,
                    "is_active": True,
                    "budget": Decimal("750000000.00"),
                },
            )
            if not department_created and (
                not department.name.startswith(config.marker) or department.description != manifest
            ):
                raise CommandError(
                    f"Seed manifest/configuration mismatch in branch {branch.pk}; "
                    "use the original exact plan."
                )
            departments.append(department)
            for room_index in range(1, 11):
                room, _ = Room.objects.get_or_create(
                    branch=branch,
                    name=f"{config.marker} Room {room_index:02d}",
                    defaults={
                        "capacity": 24,
                        "equipment": ["Whiteboard", "Projector", "Audio system"],
                        "is_active": True,
                    },
                )
                rooms.append(room)
            BranchWorkingHours.objects.bulk_create(
                [
                    BranchWorkingHours(
                        branch=branch,
                        weekday=weekday,
                        opens_at=wall_time(8, 0),
                        closes_at=wall_time(21, 0),
                        is_closed=weekday == 6,
                    )
                    for weekday in range(7)
                    if not BranchWorkingHours.objects.filter(branch=branch, weekday=weekday).exists()
                ],
                batch_size=20,
            )
    return {
        "branches": branches,
        "departments": departments,
        "rooms": rooms,
    }


def _identity_name(index: int, rng: random.Random) -> tuple[str, str]:
    first = _FIRST_NAMES[(index + rng.randrange(len(_FIRST_NAMES))) % len(_FIRST_NAMES)]
    last = _LAST_NAMES[(index * 7 + rng.randrange(len(_LAST_NAMES))) % len(_LAST_NAMES)]
    return first, last


def _ensure_teachers(config: SeedConfig, structure: dict, rng: random.Random) -> list:
    from apps.access.models import AccountType
    from apps.teachers.models import PayoutPolicy, TeacherProfile
    from apps.users.models import RoleMembership, User

    token = config.username_token
    expected = [f"sim.{token}.teacher.{index:03d}" for index in range(1, config.teachers + 1)]
    existing_users = set(User.objects.filter(username__in=expected).values_list("username", flat=True))
    existing_profiles = set(
        TeacherProfile.objects.filter(username__in=expected).values_list("username", flat=True)
    )
    if existing_users != existing_profiles:
        raise CommandError("Partial teacher identity collision detected; bridge/profile sets differ.")
    unusable = make_password(None)
    missing = [username for username in expected if username not in existing_users]
    with transaction.atomic():
        user_rows = []
        for username in missing:
            index = expected.index(username) + 1
            first, last = _identity_name(index, rng)
            user_rows.append(
                User(
                    username=username,
                    password=unusable,
                    first_name=first,
                    last_name=last,
                    is_active=True,
                    is_staff=False,
                )
            )
        User.objects.bulk_create(user_rows, batch_size=_BATCH_SIZE)
        user_map = User.objects.in_bulk(expected, field_name="username")
        profile_rows = []
        for index, username in enumerate(expected, start=1):
            if username in existing_profiles:
                continue
            user = user_map[username]
            branch_index = (index - 1) % 3
            profile_rows.append(
                TeacherProfile(
                    user=user,
                    username=username,
                    password=unusable,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    # Synthetic identities must remain impossible to contact even
                    # after the provider-disabled runner has exited.
                    phone="",
                    email=f"teacher.{index:03d}.{config.seed_id}@example.invalid",
                    birthdate=date(1980 + index % 18, index % 12 + 1, index % 27 + 1),
                    gender=(TeacherProfile.Gender.FEMALE if index % 2 else TeacherProfile.Gender.MALE),
                    branch=structure["branches"][branch_index],
                    department=structure["departments"][branch_index],
                    hire_date=config.as_of_date - timedelta(days=730 + index * 3),
                    subjects=["English"],
                    qualifications="CELTA · IELTS · English language teaching",
                    salary_type=TeacherProfile.SalaryType.MONTHLY,
                    rate=Decimal(7_000_000 + (index % 8) * 500_000),
                )
            )
        TeacherProfile.objects.bulk_create(profile_rows, batch_size=_BATCH_SIZE)
        profiles = list(TeacherProfile.objects.filter(username__in=expected).select_related("user"))
        by_username = {profile.username: profile for profile in profiles}
        teacher_type = AccountType.objects.get(
            slug="teacher",
            is_system=True,
            is_active=True,
            account_kind=AccountType.AccountKind.TEACHER,
        )
        membership_rows = []
        for username in expected:
            teacher = by_username[username]
            if not RoleMembership.objects.filter(
                user=teacher.user,
                branch=teacher.branch,
                department=teacher.department,
                account_type=teacher_type,
                revoked_at__isnull=True,
            ).exists():
                membership_rows.append(
                    RoleMembership(
                        user=teacher.user,
                        branch=teacher.branch,
                        department=teacher.department,
                        account_type=teacher_type,
                        role="teacher",
                    )
                )
        RoleMembership.objects.bulk_create(membership_rows, batch_size=_BATCH_SIZE)
        PayoutPolicy.objects.bulk_create(
            [
                PayoutPolicy(
                    teacher=teacher,
                    method=PayoutPolicy.Method.FLAT_MONTHLY,
                    flat_amount_uzs=teacher.rate,
                    is_active=True,
                )
                for teacher in profiles
                if not PayoutPolicy.objects.filter(teacher=teacher).exists()
            ],
            batch_size=_BATCH_SIZE,
        )
    return [by_username[username] for username in expected]


def _ensure_cohorts(config: SeedConfig, structure: dict, teachers: list) -> list:
    from apps.cohorts.models import Cohort, CohortTeacher
    from apps.teachers.models import TeacherType

    names = [f"{config.marker} English Group {index:03d}" for index in range(1, len(teachers) + 1)]
    existing = {
        (row.branch_id, row.name): row
        for row in Cohort.objects.filter(name__in=names).select_related("branch")
    }
    with transaction.atomic():
        rows = []
        for index, (name, teacher) in enumerate(zip(names, teachers, strict=True), start=1):
            key = (teacher.branch_id, name)
            if key in existing:
                if existing[key].primary_teacher_id != teacher.pk:
                    raise CommandError(f"Cohort identity collision for {name!r}.")
                continue
            branch_index = (index - 1) % 3
            room_offset = branch_index * 10 + ((index - 1) // 3) % 10
            rows.append(
                Cohort(
                    name=name,
                    branch=teacher.branch,
                    department=teacher.department,
                    level=("Beginner", "Elementary", "Intermediate", "Upper Intermediate")[(index - 1) % 4],
                    start_date=config.as_of_date - timedelta(days=config.history_days + 30),
                    end_date=config.as_of_date + timedelta(days=180),
                    capacity=max(22, (config.students + config.teachers - 1) // config.teachers + 2),
                    primary_teacher=teacher,
                    default_room=structure["rooms"][room_offset],
                    is_archived=False,
                )
            )
        Cohort.objects.bulk_create(rows, batch_size=_BATCH_SIZE)
        cohorts = list(Cohort.objects.filter(name__in=names).select_related("branch", "department"))
        by_name = {cohort.name: cohort for cohort in cohorts}
        main_type = TeacherType.objects.get(slug="main-teacher", is_active=True)
        assignments = []
        for name, teacher in zip(names, teachers, strict=True):
            cohort = by_name[name]
            if not CohortTeacher.objects.filter(
                cohort=cohort,
                teacher=teacher,
                teacher_type=main_type,
            ).exists():
                assignments.append(
                    CohortTeacher(
                        cohort=cohort,
                        teacher=teacher,
                        teacher_type=main_type,
                    )
                )
        CohortTeacher.objects.bulk_create(assignments, batch_size=_BATCH_SIZE)
    return [by_name[name] for name in names]


def _student_status(index: int):
    from apps.students.models import StudentProfile

    if index % 23 == 0:
        return StudentProfile.Status.ENROLLED
    return StudentProfile.Status.ACTIVE


def _status_chain(status: str) -> tuple[str, ...]:
    from apps.students.models import StudentProfile

    path = (
        StudentProfile.Status.LEAD,
        StudentProfile.Status.APPLICATION,
        StudentProfile.Status.ACCEPTED,
        StudentProfile.Status.ENROLLED,
        StudentProfile.Status.ACTIVE,
    )
    if status == StudentProfile.Status.ENROLLED:
        return path[:4]
    if status == StudentProfile.Status.ACTIVE:
        return path
    if status in (StudentProfile.Status.GRADUATED, StudentProfile.Status.WITHDRAWN):
        return (*path, status)
    if status == StudentProfile.Status.LEAD:
        return (StudentProfile.Status.LEAD,)
    return (StudentProfile.Status.LEAD, status)


def _ensure_students(
    config: SeedConfig,
    structure: dict,
    cohorts: list,
    rng: random.Random,
) -> list:
    from apps.access.models import AccountType
    from apps.cohorts.models import CohortMembership
    from apps.students.models import EnrollmentEvent, StudentProfile
    from apps.users.models import RoleMembership, User

    token = config.username_token
    expected = [f"sim.{token}.student.{index:05d}" for index in range(1, config.students + 1)]
    existing_users = set(User.objects.filter(username__in=expected).values_list("username", flat=True))
    existing_profiles = set(
        StudentProfile.objects.filter(username__in=expected).values_list("username", flat=True)
    )
    if existing_users != existing_profiles:
        raise CommandError("Partial student identity collision detected; bridge/profile sets differ.")
    unusable = make_password(None)
    missing = [username for username in expected if username not in existing_users]
    with transaction.atomic():
        users = []
        for index, username in enumerate(expected, start=1):
            if username not in missing:
                continue
            first, last = _identity_name(index + 10_000, rng)
            users.append(
                User(
                    username=username,
                    password=unusable,
                    first_name=first,
                    last_name=last,
                    is_active=True,
                    is_staff=False,
                )
            )
        User.objects.bulk_create(users, batch_size=_BATCH_SIZE)
        user_map = User.objects.in_bulk(expected, field_name="username")
        profiles = []
        history_start = config.as_of_date - timedelta(days=config.history_days + 5)
        for index, username in enumerate(expected, start=1):
            if username in existing_profiles:
                continue
            user = user_map[username]
            cohort = cohorts[(index - 1) % len(cohorts)]
            status = _student_status(index)
            profiles.append(
                StudentProfile(
                    user=user,
                    username=username,
                    password=unusable,
                    student_id=f"SIM-{config.seed_id[:12].upper()}-{index:05d}",
                    first_name=user.first_name,
                    last_name=user.last_name,
                    phone="",
                    email=f"student.{index:05d}.{config.seed_id}@example.invalid",
                    birthdate=date(
                        2005 + index % 9,
                        index % 12 + 1,
                        index % 27 + 1,
                    ),
                    gender=(StudentProfile.Gender.FEMALE if index % 2 else StudentProfile.Gender.MALE),
                    status=status,
                    branch=cohort.branch,
                    current_cohort=(
                        cohort
                        if status in (StudentProfile.Status.ACTIVE, StudentProfile.Status.ENROLLED)
                        else None
                    ),
                    enrollment_date=history_start,
                    academic_level=cohort.level,
                    location=cohort.branch.name,
                    previous_school="Tashkent secondary school",
                    emergency_contacts=[],
                )
            )
        StudentProfile.objects.bulk_create(profiles, batch_size=_BATCH_SIZE)
        student_rows = list(
            StudentProfile.objects.filter(username__in=expected).select_related(
                "user", "branch", "current_cohort"
            )
        )
        by_username = {student.username: student for student in student_rows}
        student_type = AccountType.objects.get(
            slug="student",
            is_system=True,
            is_active=True,
            account_kind=AccountType.AccountKind.STUDENT,
        )
        memberships = []
        cohort_memberships = []
        enrollment_events = []
        for index, username in enumerate(expected, start=1):
            student = by_username[username]
            cohort = cohorts[(index - 1) % len(cohorts)]
            if not RoleMembership.objects.filter(
                user=student.user,
                branch=student.branch,
                account_type=student_type,
                department__isnull=True,
                revoked_at__isnull=True,
            ).exists():
                memberships.append(
                    RoleMembership(
                        user=student.user,
                        branch=student.branch,
                        department=None,
                        account_type=student_type,
                        role="student",
                    )
                )
            if not CohortMembership.objects.filter(cohort=cohort, student=student).exists():
                cohort_memberships.append(
                    CohortMembership(
                        cohort=cohort,
                        student=student,
                        start_date=history_start,
                        end_date=(
                            config.as_of_date
                            if student.status
                            in (StudentProfile.Status.GRADUATED, StudentProfile.Status.WITHDRAWN)
                            else None
                        ),
                        moved_reason=(
                            "completed"
                            if student.status == StudentProfile.Status.GRADUATED
                            else (
                                "schedule_conflict"
                                if student.status == StudentProfile.Status.WITHDRAWN
                                else ""
                            )
                        ),
                    )
                )
            if not EnrollmentEvent.objects.filter(student=student).exists():
                chain = _status_chain(student.status)
                enrollment_events.extend(
                    EnrollmentEvent(
                        student=student,
                        from_status=from_status,
                        to_status=to_status,
                        reason_code=(
                            "completed"
                            if to_status == StudentProfile.Status.GRADUATED
                            else ("schedule_conflict" if to_status == StudentProfile.Status.WITHDRAWN else "")
                        ),
                        note=f"{config.marker} deterministic enrollment history",
                    )
                    for from_status, to_status in pairwise(chain)
                )
        RoleMembership.objects.bulk_create(memberships, batch_size=_BATCH_SIZE)
        CohortMembership.objects.bulk_create(cohort_memberships, batch_size=_BATCH_SIZE)
        EnrollmentEvent.objects.bulk_create(enrollment_events, batch_size=_BATCH_SIZE)
    print(f"seed phase identities: {len(student_rows)} students", flush=True)
    return [by_username[username] for username in expected]


def _parent_assignments(students: list) -> list[tuple[int, list]]:
    by_branch: dict[int, list] = {}
    for student in students:
        by_branch.setdefault(student.branch_id, []).append(student)
    assignments: list[tuple[int, list]] = []
    for branch_id in sorted(by_branch):
        for sibling_pair in batched(by_branch[branch_id], 2, strict=False):
            assignments.append((branch_id, list(sibling_pair)))
    return assignments


def _ensure_parents(config: SeedConfig, students: list, actor) -> None:
    from apps.access.models import AccountType
    from apps.notifications.models import (
        Channel,
        EventType,
        NotificationPreference,
        RecipientAttributionStatus,
        RecipientPrincipalKind,
    )
    from apps.parents.models import Guardian, ParentProfile
    from apps.users.models import RoleMembership, User
    from core.historical_scope import ScopeAttributionStatus

    assignments = _parent_assignments(students)
    token = config.username_token
    expected = [f"sim.{token}.parent.{index:05d}" for index in range(1, len(assignments) + 1)]
    existing_users = set(User.objects.filter(username__in=expected).values_list("username", flat=True))
    existing_profiles = set(
        ParentProfile.objects.filter(username__in=expected).values_list("username", flat=True)
    )
    if existing_users != existing_profiles:
        raise CommandError("Partial parent identity collision detected; bridge/profile sets differ.")
    unusable = make_password(None)
    with transaction.atomic():
        User.objects.bulk_create(
            [
                User(
                    username=username,
                    password=unusable,
                    first_name=_FIRST_NAMES[(index * 5) % len(_FIRST_NAMES)],
                    last_name=_LAST_NAMES[(index * 3) % len(_LAST_NAMES)],
                    is_active=True,
                    is_staff=False,
                )
                for index, username in enumerate(expected, start=1)
                if username not in existing_users
            ],
            batch_size=_BATCH_SIZE,
        )
        user_map = User.objects.in_bulk(expected, field_name="username")
        ParentProfile.objects.bulk_create(
            [
                ParentProfile(
                    user=user_map[username],
                    username=username,
                    password=unusable,
                    first_name=user_map[username].first_name,
                    last_name=user_map[username].last_name,
                    phone="",
                    email=f"parent.{index:05d}.{config.seed_id}@example.invalid",
                    gender=(ParentProfile.Gender.FEMALE if index % 2 else ParentProfile.Gender.MALE),
                    workplace="Tashkent private enterprise",
                    branch_at_creation_id=assignments[index - 1][0],
                    department_at_creation=None,
                    attribution_status=ScopeAttributionStatus.CAPTURED,
                    created_by=actor.user,
                )
                for index, username in enumerate(expected, start=1)
                if username not in existing_profiles
            ],
            batch_size=_BATCH_SIZE,
        )
        parents = list(ParentProfile.objects.filter(username__in=expected).select_related("user"))
        by_username = {parent.username: parent for parent in parents}
        parent_type = AccountType.objects.get(
            slug="parent",
            is_system=True,
            is_active=True,
            account_kind=AccountType.AccountKind.PARENT,
        )
        membership_rows = []
        guardian_rows = []
        for index, username in enumerate(expected, start=1):
            parent = by_username[username]
            branch_id, children = assignments[index - 1]
            if not RoleMembership.objects.filter(
                user=parent.user,
                branch_id=branch_id,
                account_type=parent_type,
                department__isnull=True,
                revoked_at__isnull=True,
            ).exists():
                membership_rows.append(
                    RoleMembership(
                        user=parent.user,
                        branch_id=branch_id,
                        department=None,
                        account_type=parent_type,
                        role="parent",
                    )
                )
            for child in children:
                if not Guardian.objects.filter(parent=parent, student=child).exists():
                    guardian_rows.append(
                        Guardian(
                            parent=parent,
                            student=child,
                            relationship=(
                                Guardian.Relationship.MOTHER if index % 2 else Guardian.Relationship.FATHER
                            ),
                            is_primary=True,
                        )
                    )
        RoleMembership.objects.bulk_create(membership_rows, batch_size=_BATCH_SIZE)
        Guardian.objects.bulk_create(guardian_rows, batch_size=_BATCH_SIZE)
        existing_preferences = set(
            NotificationPreference.objects.filter(
                recipient_principal_kind=RecipientPrincipalKind.PARENT,
                recipient_principal_id__in=[parent.pk for parent in parents],
                event_type=EventType.FINANCE_PAYMENT_REMINDER,
            ).values_list("recipient_principal_id", "channel")
        )
        NotificationPreference.objects.bulk_create(
            [
                NotificationPreference(
                    user=parent.user,
                    recipient_principal_kind=RecipientPrincipalKind.PARENT,
                    recipient_principal_id=parent.pk,
                    attribution_status=RecipientAttributionStatus.CAPTURED,
                    event_type=EventType.FINANCE_PAYMENT_REMINDER,
                    channel=channel,
                    enabled=False,
                )
                for parent in parents
                for channel in Channel.values
                if (parent.pk, channel) not in existing_preferences
            ],
            batch_size=_BATCH_SIZE,
        )
    print(f"seed phase families: {len(assignments)} parents, {len(students)} guardians", flush=True)


def _ensure_learning_history(config: SeedConfig, cohorts: list, students: list):
    from apps.academics.models import Subject
    from apps.attendance.models import AttendanceRecord
    from apps.schedule.models import Lesson, LessonType, Term

    start = config.as_of_date - timedelta(days=config.history_days + 10)
    term, _ = Term.objects.get_or_create(
        academic_year=f"{start.year}-{config.as_of_date.year}",
        name=f"{config.marker} Full operating year",
        defaults={
            "start_date": start,
            "end_date": config.as_of_date + timedelta(days=90),
            "is_current": False,
        },
    )
    subject = Subject.objects.filter(name__iexact="English").first()
    if subject is None:
        subject = Subject.objects.create(
            name="English",
            code=f"sim-{config.seed_id}-english"[:50],
            department=None,
            description="English language, speaking, listening, reading, and writing.",
            is_active=True,
        )
    lesson_type = LessonType.objects.filter(slug="main", is_active=True).first()
    if lesson_type is None:
        lesson_type = LessonType.objects.filter(is_active=True).order_by("id").first()
    lesson_dates = _lesson_dates(config)
    expected_keys = {
        (cohort.pk, f"{config.marker} English {lesson_date:%Y%m%d}")
        for cohort in cohorts
        for lesson_date in lesson_dates
    }
    existing_keys = set(
        Lesson.objects.filter(
            cohort__in=cohorts,
            title__startswith=config.marker,
        ).values_list("cohort_id", "title")
    )
    lesson_rows = []
    processed_at = timezone.now()
    for cohort_index, cohort in enumerate(cohorts):
        teacher = cohort.primary_teacher
        # Each branch has ten rooms for twenty cohorts. The second cohort
        # reusing a room starts after the first rather than overlapping it.
        branch_cohort_index = cohort_index // 3
        room_slot = branch_cohort_index % 10
        room_reuse = branch_cohort_index // 10
        hour = 8 + (room_slot % 5) * 2 + room_reuse * 2
        for lesson_date in lesson_dates:
            title = f"{config.marker} English {lesson_date:%Y%m%d}"
            if (cohort.pk, title) in existing_keys:
                continue
            starts_at = _aware(lesson_date, hour)
            lesson_rows.append(
                Lesson(
                    term=term,
                    cohort=cohort,
                    teacher=teacher,
                    room=cohort.default_room,
                    lesson_type=lesson_type,
                    title=title,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=90),
                    status=Lesson.Status.COMPLETED,
                    detached_from_rule=False,
                    auto_absence_processed_at=processed_at,
                )
            )
    for batch in batched(lesson_rows, _BATCH_SIZE, strict=False):
        with transaction.atomic():
            Lesson.objects.bulk_create(list(batch), batch_size=_BATCH_SIZE)
    with transaction.atomic():
        for cohort_index, cohort in enumerate(cohorts):
            title = f"{config.marker} next English lesson"
            if Lesson.objects.filter(cohort=cohort, title=title).exists():
                continue
            starts_at = _aware(config.as_of_date + timedelta(days=2), 8 + cohort_index % 6 * 2)
            Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=cohort.primary_teacher,
                room=None,
                lesson_type=lesson_type,
                title=title,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=90),
                status=Lesson.Status.SCHEDULED,
                detached_from_rule=False,
            )
    lessons = list(
        Lesson.objects.filter(
            cohort__in=cohorts,
            title__startswith=config.marker,
            status=Lesson.Status.COMPLETED,
        ).select_related("teacher__user")
    )
    if {(lesson.cohort_id, lesson.title) for lesson in lessons} != expected_keys:
        raise CommandError("Completed lesson set does not match the deterministic seed plan.")
    students_by_cohort: dict[int, list] = {}
    for student_index, student in enumerate(students, start=1):
        cohort = cohorts[(student_index - 1) % len(cohorts)]
        students_by_cohort.setdefault(cohort.pk, []).append((student_index, student))
    lesson_ordinals: dict[int, int] = {}
    pending: list = []
    written = 0
    for lesson in sorted(lessons, key=lambda item: (item.cohort_id, item.starts_at)):
        ordinal = lesson_ordinals.get(lesson.cohort_id, 0)
        lesson_ordinals[lesson.cohort_id] = ordinal + 1
        for student_index, student in students_by_cohort[lesson.cohort_id]:
            score = (student_index * 31 + ordinal * 17 + config.random_seed) % 100
            if score < 84:
                status = AttendanceRecord.Status.PRESENT
            elif score < 91:
                status = AttendanceRecord.Status.LATE
            elif score < 97:
                status = AttendanceRecord.Status.ABSENT
            else:
                status = AttendanceRecord.Status.EXCUSED
            arrived_at = None
            if status == AttendanceRecord.Status.PRESENT:
                arrived_at = lesson.starts_at + timedelta(minutes=score % 7)
            elif status == AttendanceRecord.Status.LATE:
                arrived_at = lesson.starts_at + timedelta(minutes=12 + score % 9)
            pending.append(
                AttendanceRecord(
                    student=student,
                    lesson=lesson,
                    status=status,
                    arrived_at=arrived_at,
                    note=f"{config.marker} generated attendance",
                    marked_by=lesson.teacher.user,
                    auto_marked=False,
                )
            )
            if len(pending) >= _BATCH_SIZE:
                AttendanceRecord.objects.bulk_create(
                    pending,
                    batch_size=_BATCH_SIZE,
                    update_conflicts=True,
                    update_fields=("status", "arrived_at", "note", "marked_by", "auto_marked"),
                    unique_fields=("student", "lesson"),
                )
                written += len(pending)
                pending.clear()
    if pending:
        AttendanceRecord.objects.bulk_create(
            pending,
            batch_size=_BATCH_SIZE,
            update_conflicts=True,
            update_fields=("status", "arrived_at", "note", "marked_by", "auto_marked"),
            unique_fields=("student", "lesson"),
        )
        written += len(pending)
    print(
        f"seed phase attendance: {len(lessons)} completed lessons, {written} attendance rows",
        flush=True,
    )
    return term, subject


def _invoice_status(student_index: int, month_index: int, current: bool):
    from apps.finance.models import Invoice

    score = (student_index * 17 + month_index * 13) % 100
    if current:
        return Invoice.Status.PAID if score < 45 else Invoice.Status.OVERDUE
    return Invoice.Status.PAID if score < 75 else Invoice.Status.OVERDUE


def _invoice_payment_fraction(student_index: int, month_index: int, current: bool) -> Decimal:
    score = (student_index * 17 + month_index * 13) % 100
    if current:
        if score < 45:
            return Decimal("1")
        if score < 60:
            return Decimal("0.5")
        return Decimal("0")
    if score < 75:
        return Decimal("1")
    if score < 86:
        return Decimal("0.5")
    return Decimal("0")


def _ensure_finance_history(config: SeedConfig, cohorts: list, students: list, actor) -> None:
    from apps.finance.models import (
        Expense,
        FeeSchedule,
        Invoice,
        InvoiceLine,
        PaymentAllocation,
        Refund,
    )
    from apps.payments.models import Payment
    from core.historical_scope import ScopeAttributionStatus

    with transaction.atomic():
        schedules = []
        for cohort_index, cohort in enumerate(cohorts, start=1):
            schedule, _ = FeeSchedule.objects.get_or_create(
                cohort=cohort,
                name=f"{config.marker} Monthly English tuition",
                defaults={
                    "amount_uzs": Decimal(1_150_000 + (cohort_index % 6) * 50_000),
                    "billing_period": FeeSchedule.BillingPeriod.MONTHLY,
                    "due_day_of_month": 5,
                    "is_active": True,
                },
            )
            schedules.append(schedule)
    periods = [_month_shift(config.as_of_date.replace(day=1), offset) for offset in range(-11, 1)]
    code = hashlib.sha256(config.seed_id.encode()).hexdigest()[:8].upper()
    expected_numbers = {
        f"S{code}-{student_index:05d}-{month_index:02d}"
        for student_index in range(1, len(students) + 1)
        for month_index in range(1, 13)
    }
    existing_numbers = set(
        Invoice.objects.filter(number__in=expected_numbers).values_list("number", flat=True)
    )
    rows = []
    for student_index, student in enumerate(students, start=1):
        cohort_index = (student_index - 1) % len(cohorts)
        cohort = cohorts[cohort_index]
        schedule = schedules[cohort_index]
        for month_index, period_start in enumerate(periods, start=1):
            number = f"S{code}-{student_index:05d}-{month_index:02d}"
            if number in existing_numbers:
                continue
            status = _invoice_status(
                student_index,
                month_index,
                current=period_start == config.as_of_date.replace(day=1),
            )
            due_day = min(5, calendar.monthrange(period_start.year, period_start.month)[1])
            rows.append(
                Invoice(
                    number=number,
                    student=student,
                    cohort=cohort,
                    fee_schedule=schedule,
                    branch_at_issue=cohort.branch,
                    department_at_issue=cohort.department,
                    attribution_status=ScopeAttributionStatus.CAPTURED,
                    period=period_start.strftime("%Y-%m"),
                    status=status,
                    issue_date=period_start,
                    due_date=period_start.replace(day=due_day),
                    currency="UZS",
                    total_uzs=schedule.amount_uzs,
                    # Do not make a real director the synthetic payer/reminder
                    # recipient. Guardians own the simulated invoices.
                    created_by=None,
                )
            )
    for batch in batched(rows, _BATCH_SIZE, strict=False):
        with transaction.atomic():
            Invoice.objects.bulk_create(list(batch), batch_size=_BATCH_SIZE)
    invoices = list(
        Invoice.objects.filter(number__in=expected_numbers).select_related(
            "student__user", "cohort", "fee_schedule", "branch_at_issue", "department_at_issue"
        )
    )
    if len(invoices) != len(expected_numbers):
        raise CommandError("Invoice set is incomplete after finance seeding.")
    existing_line_invoice_ids = set(
        InvoiceLine.objects.filter(invoice__in=invoices, description__startswith=config.marker).values_list(
            "invoice_id", flat=True
        )
    )
    InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(
                invoice=invoice,
                description=f"{config.marker} Monthly English programme",
                line_type=InvoiceLine.LineType.TUITION,
                quantity=Decimal("1.00"),
                unit_price_uzs=invoice.total_uzs,
                amount_uzs=invoice.total_uzs,
            )
            for invoice in invoices
            if invoice.pk not in existing_line_invoice_ids
        ],
        batch_size=_BATCH_SIZE,
    )
    payment_keys = [f"sim:{config.seed_id}:{invoice.number}" for invoice in invoices]
    existing_keys = set(
        Payment.objects.filter(idempotency_key__in=payment_keys).values_list("idempotency_key", flat=True)
    )
    payment_rows = []
    payable_invoices = []
    current_period = config.as_of_date.strftime("%Y-%m")
    for invoice in invoices:
        _, student_token, month_token = invoice.number.rsplit("-", 2)
        fraction = _invoice_payment_fraction(
            int(student_token),
            int(month_token),
            current=invoice.period == current_period,
        )
        if not fraction:
            continue
        key = f"sim:{config.seed_id}:{invoice.number}"
        payable_invoices.append(invoice)
        if key in existing_keys:
            continue
        amount = (invoice.total_uzs * fraction).quantize(Decimal("0.01"))
        paid_at = _aware(invoice.due_date or invoice.issue_date or config.as_of_date, 14)
        payment_rows.append(
            Payment(
                # Historical bank transfers do not require a live cashier shift
                # and cannot be mistaken for unreconciled till cash.
                provider=Payment.Method.BANK_TRANSFER,
                amount_uzs=amount,
                currency="UZS",
                status=Payment.Status.COMPLETED,
                idempotency_key=key,
                provider_txn_id="",
                account_ref=invoice.number,
                allocation_status=Payment.Allocation.ALLOCATED,
                branch_at_payment=invoice.branch_at_issue,
                department_at_payment=invoice.department_at_issue,
                attribution_status=ScopeAttributionStatus.CAPTURED,
                payer=invoice.student.user,
                paid_at=paid_at,
                metadata={
                    "simulation_seed": config.seed_id,
                    "invoice_id": invoice.pk,
                    "student_id": invoice.student_id,
                },
            )
        )
    for batch in batched(payment_rows, _BATCH_SIZE, strict=False):
        with transaction.atomic():
            Payment.objects.bulk_create(list(batch), batch_size=_BATCH_SIZE)
    payments = list(Payment.objects.filter(idempotency_key__in=payment_keys))
    by_key = {payment.idempotency_key: payment for payment in payments}
    allocated_payment_ids = set(
        PaymentAllocation.objects.filter(payment_id__in=[row.pk for row in payments]).values_list(
            "payment_id", flat=True
        )
    )
    allocations = []
    for invoice in payable_invoices:
        payment = by_key[f"sim:{config.seed_id}:{invoice.number}"]
        if payment.pk in allocated_payment_ids:
            continue
        allocations.append(
            PaymentAllocation(
                invoice=invoice,
                payment_id=payment.pk,
                amount_uzs=payment.amount_uzs,
            )
        )
    PaymentAllocation.objects.bulk_create(allocations, batch_size=_BATCH_SIZE)
    with transaction.atomic():
        from apps.finance.services import create_expense

        categories = ("Rent", "Utilities", "Learning materials", "Technology")
        for branch in {cohort.branch for cohort in cohorts}:
            for month_index, period_start in enumerate(periods, start=1):
                for category_index in range(2):
                    description = (
                        f"{config.marker} {period_start:%Y-%m} "
                        f"{categories[(month_index + category_index) % len(categories)]}"
                    )
                    if Expense.objects.filter(branch=branch, description=description).exists():
                        continue
                    create_expense(
                        branch=branch,
                        category=categories[(month_index + category_index) % len(categories)],
                        description=description,
                        amount_uzs=Decimal(2_000_000 + month_index * 125_000 + category_index * 750_000),
                        created_by=actor.user,
                    )
        refundable = [invoice for invoice in payable_invoices if invoice.status == Invoice.Status.PAID][::120]
        refund_rows = []
        for invoice in refundable:
            payment = by_key[f"sim:{config.seed_id}:{invoice.number}"]
            reason = f"{config.marker} Schedule adjustment"
            if Refund.objects.filter(
                invoice=invoice,
                payment_id=payment.pk,
                reason=reason,
            ).exists():
                continue
            refund_rows.append(
                Refund(
                    invoice=invoice,
                    payment_id=payment.pk,
                    amount_uzs=min(Decimal("100000.00"), payment.amount_uzs),
                    reason=reason,
                    # Keep simulated refunds provider-free and pending. A
                    # completed refund must have real provider/ledger evidence.
                    state=Refund.State.REQUESTED,
                    requested_by=actor.user,
                    approved_by=None,
                )
            )
        Refund.objects.bulk_create(refund_rows, batch_size=_BATCH_SIZE)
    print(
        f"seed phase finance: {len(invoices)} invoices, {len(payments)} payments, "
        f"{PaymentAllocation.objects.filter(payment_id__in=[row.pk for row in payments]).count()} allocations",
        flush=True,
    )


def _ensure_academics(config: SeedConfig, cohorts: list, students: list, term, subject, actor) -> None:
    from apps.academics.grading import display_for
    from apps.academics.integrity import assessment_integrity_write, exam_readiness
    from apps.academics.models import Exam, ExamLifecycleEvent, ExamResult, ExamType, Grade
    from apps.assignments.models import Assignment, Submission, SubmissionGrade
    from apps.org.selectors import get_center_settings

    exam_types = list(ExamType.objects.filter(is_active=True).order_by("id"))
    if not exam_types:
        raise CommandError("No active exam types are available.")
    students_by_cohort: dict[int, list] = {}
    for student_index, student in enumerate(students, start=1):
        cohort = cohorts[(student_index - 1) % len(cohorts)]
        students_by_cohort.setdefault(cohort.pk, []).append((student_index, student))
    exam_dates = [
        config.as_of_date - timedelta(days=300),
        config.as_of_date - timedelta(days=210),
        config.as_of_date - timedelta(days=120),
        config.as_of_date - timedelta(days=30),
    ]
    expected_titles = {
        (cohort.pk, f"{config.marker} English assessment {index}")
        for cohort in cohorts
        for index in range(1, 5)
    }
    existing_titles = set(
        Exam.objects.filter(cohort__in=cohorts, title__startswith=config.marker).values_list(
            "cohort_id", "title"
        )
    )
    with transaction.atomic():
        Exam.objects.bulk_create(
            [
                Exam(
                    subject=subject,
                    cohort=cohort,
                    term=term,
                    exam_type=exam_types[(exam_index - 1) % len(exam_types)],
                    title=f"{config.marker} English assessment {exam_index}",
                    exam_date=exam_dates[exam_index - 1],
                    max_score=Decimal("100.00"),
                    weight=Decimal("1.000"),
                    is_published=False,
                    version=1,
                    requires_republish=False,
                    created_by=actor.user,
                )
                for cohort in cohorts
                for exam_index in range(1, 5)
                if (cohort.pk, f"{config.marker} English assessment {exam_index}") not in existing_titles
            ],
            batch_size=_BATCH_SIZE,
        )
    exams = list(
        Exam.objects.filter(cohort__in=cohorts, title__startswith=config.marker).select_related(
            "cohort__primary_teacher__user", "cohort__branch", "cohort__department"
        )
    )
    if {(exam.cohort_id, exam.title) for exam in exams} != expected_titles:
        raise CommandError("Exam set is incomplete after academic seeding.")
    result_rows = []
    for exam in exams:
        expected_students = {
            student.pk: (student_index, student)
            for student_index, student in students_by_cohort[exam.cohort_id]
        }
        existing_students = set(ExamResult.objects.filter(exam=exam).values_list("student_id", flat=True))
        if not existing_students <= expected_students.keys():
            raise CommandError(f"Unexpected student result collision for exam {exam.pk}.")
        missing_students = expected_students.keys() - existing_students
        if exam.is_published and missing_students:
            raise CommandError(f"Published exam {exam.pk} has incomplete result evidence.")
        assessment_index = int(exam.title.rsplit(" ", 1)[-1])
        for student_id in sorted(missing_students):
            student_index, student = expected_students[student_id]
            score = Decimal(48 + (student_index * 13 + assessment_index * 11) % 51)
            result_rows.append(
                ExamResult(
                    exam=exam,
                    student=student,
                    score=score,
                    note=f"{config.marker} English assessment result",
                    graded_by=exam.cohort.primary_teacher.user,
                )
            )
    for batch in batched(result_rows, _BATCH_SIZE, strict=False):
        with transaction.atomic():
            ExamResult.objects.bulk_create(list(batch), batch_size=_BATCH_SIZE)
    readiness = {exam.pk: exam_readiness(exam=exam) for exam in exams}
    unready = [exam_id for exam_id, snapshot in readiness.items() if not snapshot.ready]
    if unready:
        raise CommandError(f"Simulation exams are not publication-ready: {unready[:20]}")
    now = timezone.now()
    unpublished = [exam for exam in exams if not exam.is_published]
    existing_events = set(
        ExamLifecycleEvent.objects.filter(exam__in=exams, event_type="published").values_list(
            "exam_id", flat=True
        )
    )
    with transaction.atomic(), assessment_integrity_write():
        for exam in unpublished:
            exam.is_published = True
            exam.published_at = now
        Exam.objects.bulk_update(unpublished, ("is_published", "published_at"), batch_size=_BATCH_SIZE)
        ExamLifecycleEvent.objects.bulk_create(
            [
                ExamLifecycleEvent(
                    exam=exam,
                    event_type=ExamLifecycleEvent.EventType.PUBLISHED,
                    exam_version=exam.version,
                    reason="",
                    details={
                        "readiness": readiness[exam.pk].as_dict(),
                        "simulation_seed": config.seed_id,
                    },
                    actor=actor.user,
                    actor_repr=actor.get_full_name() or actor.username,
                    branch_id_snapshot=exam.cohort.branch_id,
                    department_id_snapshot=exam.cohort.department_id,
                )
                for exam in exams
                if exam.pk not in existing_events
            ],
            batch_size=_BATCH_SIZE,
        )
    results_by_student: dict[int, list] = {}
    for result in ExamResult.objects.filter(exam__in=exams).select_related("exam"):
        results_by_student.setdefault(result.student_id, []).append(result)
    existing_grade_students = set(
        Grade.objects.filter(student__in=students, subject=subject, term=term).values_list(
            "student_id", flat=True
        )
    )
    grading_scheme = get_center_settings().grading_scheme
    grade_rows = []
    for student in students:
        if student.pk in existing_grade_students:
            continue
        results = results_by_student.get(student.pk, [])
        if not results:
            continue
        raw = sum((result.score for result in results), Decimal("0")) / Decimal(len(results))
        grade_rows.append(
            Grade(
                student=student,
                subject=subject,
                term=term,
                value_raw=raw.quantize(Decimal("0.001")),
                value_display=display_for(raw, grading_scheme),
                components=[
                    {
                        "exam": result.exam_id,
                        "title": result.exam.title,
                        "score": str(result.score),
                        "max_score": str(result.exam.max_score),
                        "weight": str(result.exam.weight),
                        "exam_version": result.exam.version,
                    }
                    for result in results
                ],
                is_published=True,
                published_at=now,
                is_valid=True,
            )
        )
    Grade.objects.bulk_create(grade_rows, batch_size=_BATCH_SIZE)

    assignment_dates = [
        config.as_of_date - timedelta(days=280),
        config.as_of_date - timedelta(days=190),
        config.as_of_date - timedelta(days=100),
        config.as_of_date - timedelta(days=10),
    ]
    expected_assignment_titles = {
        (cohort.pk, f"{config.marker} English practice {index}")
        for cohort in cohorts
        for index in range(1, 5)
    }
    existing_assignment_titles = set(
        Assignment.objects.filter(cohort__in=cohorts, title__startswith=config.marker).values_list(
            "cohort_id", "title"
        )
    )
    Assignment.objects.bulk_create(
        [
            Assignment(
                cohort=cohort,
                created_by=cohort.primary_teacher.user,
                title=f"{config.marker} English practice {assignment_index}",
                description="Complete the English reading, writing, and speaking practice.",
                due_at=_aware(assignment_dates[assignment_index - 1], 20),
                attachments=[],
                rubric=[{"criterion": "English accuracy", "max_points": 100}],
                max_score=Decimal("100.00"),
                max_resubmits=2,
                status=Assignment.Status.CLOSED,
                published_at=_aware(assignment_dates[assignment_index - 1] - timedelta(days=7), 9),
            )
            for cohort in cohorts
            for assignment_index in range(1, 5)
            if (cohort.pk, f"{config.marker} English practice {assignment_index}")
            not in existing_assignment_titles
        ],
        batch_size=_BATCH_SIZE,
    )
    assignments = list(Assignment.objects.filter(cohort__in=cohorts, title__startswith=config.marker))
    if {(row.cohort_id, row.title) for row in assignments} != expected_assignment_titles:
        raise CommandError("Assignment set is incomplete after academic seeding.")
    existing_submission_pairs = set(
        Submission.objects.filter(assignment__in=assignments).values_list("assignment_id", "student_id")
    )
    submission_rows = []
    for assignment in assignments:
        for student_index, student in students_by_cohort[assignment.cohort_id]:
            if (student_index + assignment.pk) % 10 == 0:
                continue
            if (assignment.pk, student.pk) in existing_submission_pairs:
                continue
            submission_rows.append(
                Submission(
                    assignment=assignment,
                    student=student,
                    text=f"{config.marker} Completed English practice response.",
                    attachments=[],
                    is_late=(student_index + assignment.pk) % 17 == 0,
                    attempt_number=1,
                    status=Submission.Status.GRADED,
                )
            )
    Submission.objects.bulk_create(submission_rows, batch_size=_BATCH_SIZE)
    submissions = list(Submission.objects.filter(assignment__in=assignments).select_related("assignment"))
    graded_submission_ids = set(
        SubmissionGrade.objects.filter(submission__in=submissions).values_list("submission_id", flat=True)
    )
    SubmissionGrade.objects.bulk_create(
        [
            SubmissionGrade(
                submission=submission,
                score=Decimal(55 + (submission.student_id * 7 + submission.assignment_id * 3) % 45),
                rubric_scores=[],
                feedback="Good English progress. Review the corrections before the next lesson.",
                ai_feedback="",
                graded_by=submission.assignment.cohort.primary_teacher.user,
            )
            for submission in submissions
            if submission.pk not in graded_submission_ids
        ],
        batch_size=_BATCH_SIZE,
    )
    print(
        f"seed phase academics: {len(exams)} exams, {len(results_by_student)} graded students, "
        f"{len(assignments)} assignments, {len(submissions)} submissions",
        flush=True,
    )


def _ensure_messaging(config: SeedConfig, cohorts: list, students: list) -> None:
    from apps.messaging.models import (
        Message,
        ParticipantAttributionStatus,
        ParticipantPrincipalKind,
        Thread,
        ThreadParticipant,
    )
    from apps.parents.models import Guardian

    base_date = config.as_of_date - timedelta(days=180)
    history_started_at = _aware(base_date, 7)
    guardian_map = {
        row.student_id: row.parent
        for row in Guardian.objects.filter(student__in=students, revoked_at__isnull=True).select_related(
            "parent__user"
        )
    }
    subjects = [f"{config.marker} English support {student.student_id}" for student in students]
    existing_subjects = set(Thread.objects.filter(subject__in=subjects).values_list("subject", flat=True))
    thread_rows = []
    for student_index, (student, subject) in enumerate(zip(students, subjects, strict=True), start=1):
        if subject in existing_subjects:
            continue
        cohort = cohorts[(student_index - 1) % len(cohorts)]
        thread_rows.append(
            Thread(
                subject=subject,
                branch=student.branch,
                created_by=cohort.primary_teacher.user,
                realtime_sequence=0,
            )
        )
    Thread.objects.bulk_create(thread_rows, batch_size=_BATCH_SIZE)
    threads = list(Thread.objects.filter(subject__in=subjects).select_related("branch"))
    for thread in threads:
        thread.created_at = history_started_at
    Thread.objects.bulk_update(threads, ("created_at",), batch_size=_BATCH_SIZE)
    by_subject = {thread.subject: thread for thread in threads}
    existing_seats = set(
        ThreadParticipant.objects.filter(thread__in=threads).values_list(
            "thread_id", "principal_kind", "principal_id"
        )
    )
    seat_rows = []
    thread_context: dict[int, tuple] = {}
    for student_index, (student, subject) in enumerate(zip(students, subjects, strict=True), start=1):
        thread = by_subject[subject]
        teacher = cohorts[(student_index - 1) % len(cohorts)].primary_teacher
        parent = guardian_map.get(student.pk)
        participants = [
            (
                teacher.user,
                ParticipantPrincipalKind.TEACHER,
                teacher.pk,
            ),
            (
                student.user,
                ParticipantPrincipalKind.STUDENT,
                student.pk,
            ),
        ]
        if parent is not None:
            participants.append(
                (
                    parent.user,
                    ParticipantPrincipalKind.PARENT,
                    parent.pk,
                )
            )
        for user, principal_kind, principal_id in participants:
            if (thread.pk, principal_kind, principal_id) in existing_seats:
                continue
            seat_rows.append(
                ThreadParticipant(
                    thread=thread,
                    user=user,
                    principal_kind=principal_kind,
                    principal_id=principal_id,
                    attribution_status=ParticipantAttributionStatus.CAPTURED,
                    last_read_at=_aware(config.as_of_date, 20),
                    notifications_muted=False,
                )
            )
        thread_context[thread.pk] = (student_index, teacher, student, parent)
    ThreadParticipant.objects.bulk_create(seat_rows, batch_size=_BATCH_SIZE)
    ThreadParticipant.objects.filter(thread__in=threads).update(added_at=history_started_at)
    existing_message_keys = set(
        Message.objects.filter(thread__in=threads, body__startswith=config.marker).values_list(
            "thread_id", "body"
        )
    )
    message_rows: list = []
    message_times: dict[int, datetime] = {}
    for thread in threads:
        student_index, teacher, student, parent = thread_context[thread.pk]
        senders = [
            (teacher.user, ParticipantPrincipalKind.TEACHER, teacher.pk),
            (student.user, ParticipantPrincipalKind.STUDENT, student.pk),
        ]
        if parent is not None:
            senders.append((parent.user, ParticipantPrincipalKind.PARENT, parent.pk))
        for message_index in range(1, 17):
            body = (
                f"{config.marker} #{message_index:02d} "
                f"{_MESSAGE_TEXT[(student_index + message_index) % len(_MESSAGE_TEXT)]}"
            )
            if (thread.pk, body) in existing_message_keys:
                continue
            sender, principal_kind, principal_id = senders[(message_index - 1) % len(senders)]
            message = Message(
                thread=thread,
                sender=sender,
                sender_principal_kind=principal_kind,
                sender_principal_id=principal_id,
                sender_attribution_status=ParticipantAttributionStatus.CAPTURED,
                body=body,
                attachments=[],
            )
            message_rows.append(message)
            message_times[id(message)] = _aware(
                base_date + timedelta(days=(student_index * 3 + message_index * 11) % 181),
                8 + message_index % 12,
                (student_index + message_index * 7) % 60,
            )
    for batch_tuple in batched(message_rows, _BATCH_SIZE, strict=False):
        batch = list(batch_tuple)
        with transaction.atomic():
            Message.objects.bulk_create(batch, batch_size=_BATCH_SIZE)
            for message in batch:
                message.created_at = message_times[id(message)]
            Message.objects.bulk_update(batch, ("created_at",), batch_size=_BATCH_SIZE)
    # Repair timestamps deterministically on every resume, including a row from
    # an older interrupted command version that may have committed before its
    # historical timestamp update.
    tagged_messages = list(
        Message.objects.filter(thread__in=threads, body__startswith=config.marker).only(
            "id", "thread_id", "body", "created_at"
        )
    )
    prefix = f"{config.marker} #"
    for message in tagged_messages:
        message_index = int(message.body[len(prefix) : len(prefix) + 2])
        student_index = thread_context[message.thread_id][0]
        message.created_at = _aware(
            base_date + timedelta(days=(student_index * 3 + message_index * 11) % 181),
            8 + message_index % 12,
            (student_index + message_index * 7) % 60,
        )
    for batch_tuple in batched(tagged_messages, _BATCH_SIZE, strict=False):
        Message.objects.bulk_update(list(batch_tuple), ("created_at",), batch_size=_BATCH_SIZE)
    latest = {
        row["thread_id"]: row["latest"]
        for row in Message.objects.filter(thread__in=threads)
        .values("thread_id")
        .annotate(latest=Max("created_at"))
    }
    for thread in threads:
        thread.last_message_at = latest.get(thread.pk)
    Thread.objects.bulk_update(threads, ("last_message_at",), batch_size=_BATCH_SIZE)
    message_count = Message.objects.filter(thread__in=threads, body__startswith=config.marker).count()
    print(f"seed phase messaging: {len(threads)} threads, {message_count} messages", flush=True)


def _ensure_crm_prospects(config: SeedConfig, structure: dict) -> list:
    """Create a separate pre-enrolment pool instead of mislabelling learners.

    Operational students all have cohort, attendance, academic, and billing
    history. CRM prospects deliberately have none of those relationships until
    they convert through the real admissions workflow.
    """
    from apps.access.models import AccountType
    from apps.students.models import StudentProfile
    from apps.users.models import RoleMembership, User

    count = min(120, max(12, config.students // 10))
    token = config.username_token
    usernames = [f"sim.{token}.prospect.{index:04d}" for index in range(1, count + 1)]
    existing_users = set(User.objects.filter(username__in=usernames).values_list("username", flat=True))
    existing_profiles = set(
        StudentProfile.objects.filter(username__in=usernames).values_list("username", flat=True)
    )
    if existing_users != existing_profiles:
        raise CommandError("Partial CRM prospect identity collision detected.")
    unusable = make_password(None)
    with transaction.atomic():
        User.objects.bulk_create(
            [
                User(
                    username=username,
                    password=unusable,
                    first_name=_FIRST_NAMES[(index * 11) % len(_FIRST_NAMES)],
                    last_name=_LAST_NAMES[(index * 5) % len(_LAST_NAMES)],
                    is_active=True,
                    is_staff=False,
                )
                for index, username in enumerate(usernames, start=1)
                if username not in existing_users
            ],
            batch_size=_BATCH_SIZE,
        )
        users = User.objects.in_bulk(usernames, field_name="username")
        StudentProfile.objects.bulk_create(
            [
                StudentProfile(
                    user=users[username],
                    username=username,
                    password=unusable,
                    student_id=f"LEAD-{hashlib.sha256(config.seed_id.encode()).hexdigest()[:8].upper()}-{index:04d}",
                    first_name=users[username].first_name,
                    last_name=users[username].last_name,
                    phone="",
                    email=f"prospect.{index:04d}.{config.seed_id}@example.invalid",
                    birthdate=date(2006 + index % 8, index % 12 + 1, index % 27 + 1),
                    gender=(StudentProfile.Gender.FEMALE if index % 2 else StudentProfile.Gender.MALE),
                    status=StudentProfile.Status.LEAD,
                    branch=structure["branches"][(index - 1) % len(structure["branches"])],
                    current_cohort=None,
                    enrollment_date=None,
                    academic_level="",
                    location="Tashkent",
                    previous_school="",
                    emergency_contacts=[],
                )
                for index, username in enumerate(usernames, start=1)
                if username not in existing_profiles
            ],
            batch_size=_BATCH_SIZE,
        )
        prospects = list(
            StudentProfile.objects.filter(username__in=usernames).select_related("user", "branch")
        )
        student_type = AccountType.objects.get(
            slug="student",
            is_system=True,
            is_active=True,
            account_kind=AccountType.AccountKind.STUDENT,
        )
        existing_memberships = set(
            RoleMembership.objects.filter(
                user_id__in=[prospect.user_id for prospect in prospects],
                account_type=student_type,
                revoked_at__isnull=True,
            ).values_list("user_id", "branch_id")
        )
        RoleMembership.objects.bulk_create(
            [
                RoleMembership(
                    user=prospect.user,
                    branch=prospect.branch,
                    department=None,
                    account_type=student_type,
                    role="student",
                )
                for prospect in prospects
                if (prospect.user_id, prospect.branch_id) not in existing_memberships
            ],
            batch_size=_BATCH_SIZE,
        )
    by_username = {prospect.username: prospect for prospect in prospects}
    return [by_username[username] for username in usernames]


def _ensure_operations(config: SeedConfig, structure: dict, teachers: list, students: list, actor) -> None:
    from apps.crm.identity import lead_identity_fingerprints
    from apps.crm.models import (
        CRMLead,
        LeadAttribution,
        LeadSource,
        LeadStageHistory,
        LeadTouch,
        PipelineStage,
    )
    from apps.notifications.models import (
        EventType,
        Notification,
        RecipientAttributionStatus,
        RecipientPrincipalKind,
    )
    from apps.parents.models import PickupAuthorization
    from apps.tasks.models import Task

    # Safe in-app history only: no NotificationDelivery rows and therefore no
    # SMS, email, push, webhook, or provider interaction.
    notification_rows = []
    existing_dedupe = set(
        Notification.objects.filter(dedupe_key__startswith=f"sim:{config.seed_id}:").values_list(
            "dedupe_key", flat=True
        )
    )
    for student_index, student in enumerate(students, start=1):
        for event_index, (event_type, title) in enumerate(
            (
                (EventType.ATTENDANCE_LATE, "English attendance update"),
                (EventType.ASSIGNMENTS_GRADED, "English practice graded"),
            ),
            start=1,
        ):
            dedupe = f"sim:{config.seed_id}:{student_index}:{event_index}"
            if dedupe in existing_dedupe:
                continue
            notification_rows.append(
                Notification(
                    user=student.user,
                    event_type=event_type,
                    title=f"{config.marker} {title}",
                    body="Open your English workspace to review the latest update.",
                    data={"simulation_seed": config.seed_id},
                    recipient_principal_kind=RecipientPrincipalKind.STUDENT,
                    recipient_principal_id=student.pk,
                    attribution_status=RecipientAttributionStatus.CAPTURED,
                    dedupe_key=dedupe,
                    read_at=(timezone.now() if (student_index + event_index) % 3 else None),
                )
            )
    Notification.objects.bulk_create(notification_rows, batch_size=_BATCH_SIZE)

    existing_task_titles = set(
        Task.objects.filter(title__startswith=config.marker).values_list("assignee_id", "title")
    )
    task_rows = []
    for teacher_index, teacher in enumerate(teachers, start=1):
        for task_index in range(1, 4):
            title = f"{config.marker} Teacher workflow {task_index}"
            if (teacher.user_id, title) in existing_task_titles:
                continue
            status = (Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.DONE)[
                (teacher_index + task_index) % 3
            ]
            task_rows.append(
                Task(
                    title=title,
                    description="Review English learner progress, attendance, and the next lesson plan.",
                    status=status,
                    priority=(Task.Priority.NORMAL, Task.Priority.HIGH, Task.Priority.LOW)[task_index - 1],
                    assignee=teacher.user,
                    assignee_principal_kind="teacher",
                    assignee_principal_id=teacher.pk,
                    assignee_attribution_status="captured",
                    department=teacher.department,
                    branch=teacher.branch,
                    due_at=_aware(config.as_of_date + timedelta(days=task_index * 3), 18),
                    created_by=actor.user,
                    created_by_principal_kind="staff",
                    created_by_principal_id=actor.pk,
                    created_by_attribution_status="captured",
                    completed_at=(timezone.now() if status == Task.Status.DONE else None),
                )
            )
    Task.objects.bulk_create(task_rows, batch_size=_BATCH_SIZE)

    pickup_rows = []
    for student_index, student in enumerate(students, start=1):
        if (
            student_index % 4
            or PickupAuthorization.objects.filter(
                student=student, full_name__startswith=config.marker
            ).exists()
        ):
            continue
        pickup_rows.append(
            PickupAuthorization(
                student=student,
                full_name=f"{config.marker} Authorized family member",
                phone=f"+999000{student_index:06d}",
                relationship="Family",
                is_active=True,
            )
        )
    PickupAuthorization.objects.bulk_create(pickup_rows, batch_size=_BATCH_SIZE)

    # A bounded admissions funnel adds useful CRM cards without creating fake
    # outbound campaign deliveries.  These are initial states, not transitions.
    stage = PipelineStage.objects.filter(slug="new", is_active=True).first()
    source = LeadSource.objects.filter(slug="referral", is_active=True).first()
    if source is None or stage is None:
        raise CommandError("Canonical CRM stage/source rows are unavailable.")
    if source is not None and stage is not None:
        lead_students = _ensure_crm_prospects(config, structure)
        existing_lead_students = set(
            CRMLead.objects.filter(student__in=lead_students).values_list("student_id", flat=True)
        )
        lead_rows = []
        for student in lead_students:
            if student.pk in existing_lead_students:
                continue
            department = next(
                teacher.department for teacher in teachers if teacher.branch_id == student.branch_id
            )
            lead_rows.append(
                CRMLead(
                    student=student,
                    branch=student.branch,
                    department=department,
                    stage=stage,
                    state=CRMLead.State.OPEN,
                    owner=None,
                    owner_principal_kind="",
                    owner_principal_id=None,
                    initial_source=source,
                    loss_reason="",
                    created_by=actor.user,
                    created_by_principal_kind="staff",
                    created_by_principal_id=actor.pk,
                    version=1,
                    **lead_identity_fingerprints(student),
                )
            )
        CRMLead.objects.bulk_create(lead_rows, batch_size=_BATCH_SIZE)
        leads = list(CRMLead.objects.filter(student__in=lead_students))
        historical_leads = set(
            LeadStageHistory.objects.filter(lead__in=leads).values_list("lead_id", flat=True)
        )
        LeadStageHistory.objects.bulk_create(
            [
                LeadStageHistory(
                    lead=lead,
                    from_stage=None,
                    to_stage=stage,
                    from_state=CRMLead.State.OPEN,
                    to_state=CRMLead.State.OPEN,
                    note=f"{config.marker} Lead entered the English admissions pipeline.",
                    actor=actor.user,
                    actor_principal_kind="staff",
                    actor_principal_id=actor.pk,
                )
                for lead in leads
                if lead.pk not in historical_leads
            ],
            batch_size=_BATCH_SIZE,
        )
        attributed_leads = set(
            LeadAttribution.objects.filter(lead__in=leads).values_list("lead_id", flat=True)
        )
        crm_now = timezone.now()
        LeadAttribution.objects.bulk_create(
            [
                LeadAttribution(
                    lead=lead,
                    source=source,
                    medium="referral",
                    content=f"{config.marker} English programme",
                    occurred_at=crm_now,
                    actor=actor.user,
                    actor_principal_kind="staff",
                    actor_principal_id=actor.pk,
                )
                for lead in leads
                if lead.pk not in attributed_leads
            ],
            batch_size=_BATCH_SIZE,
        )
        existing_touches = set(
            LeadTouch.objects.filter(lead__in=leads, summary__startswith=config.marker).values_list(
                "lead_id", "summary"
            )
        )
        touch_rows = []
        for lead in leads:
            for touch_index in range(1, 4):
                summary = f"{config.marker} Admissions conversation {touch_index}"
                if (lead.pk, summary) in existing_touches:
                    continue
                touch_rows.append(
                    LeadTouch(
                        lead=lead,
                        channel=(
                            LeadTouch.Channel.PHONE,
                            LeadTouch.Channel.WHATSAPP,
                            LeadTouch.Channel.IN_PERSON,
                        )[touch_index - 1],
                        direction=(
                            LeadTouch.Direction.OUTBOUND if touch_index < 3 else LeadTouch.Direction.INBOUND
                        ),
                        outcome="English programme discussed",
                        summary=summary,
                        occurred_at=crm_now,
                        actor=actor.user,
                        actor_principal_kind="staff",
                        actor_principal_id=actor.pk,
                    )
                )
        LeadTouch.objects.bulk_create(touch_rows, batch_size=_BATCH_SIZE)
    print(
        f"seed phase operations: {Notification.objects.filter(dedupe_key__startswith=f'sim:{config.seed_id}:').count()} "
        f"notifications, {Task.objects.filter(title__startswith=config.marker).count()} tasks",
        flush=True,
    )


def _verification(config: SeedConfig) -> dict[str, int | str | bool | None]:
    from django.db.models import Count

    from apps.academics.models import Exam, ExamLifecycleEvent, ExamResult, Grade
    from apps.assignments.models import Assignment, Submission
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
    from apps.crm.models import CRMLead
    from apps.finance.models import Expense, Invoice, InvoiceLine, PaymentAllocation
    from apps.messaging.models import Message, Thread, ThreadParticipant
    from apps.notifications.models import Notification, NotificationDelivery
    from apps.parents.models import Guardian, ParentProfile
    from apps.payments.models import Payment
    from apps.schedule.models import Lesson
    from apps.students.models import StudentProfile
    from apps.tasks.models import Task
    from apps.teachers.models import TeacherProfile

    student_prefix = f"sim.{config.username_token}.student."
    teacher_prefix = f"sim.{config.username_token}.teacher."
    parent_prefix = f"sim.{config.username_token}.parent."
    prospect_prefix = f"sim.{config.username_token}.prospect."
    invoice_prefix = f"S{hashlib.sha256(config.seed_id.encode()).hexdigest()[:8].upper()}-"
    students = StudentProfile.objects.filter(username__startswith=student_prefix)
    teachers = TeacherProfile.objects.filter(username__startswith=teacher_prefix)
    cohorts = Cohort.objects.filter(name__startswith=config.marker)
    lessons = Lesson.objects.filter(cohort__in=cohorts, title__startswith=config.marker)
    completed_lessons = lessons.filter(status=Lesson.Status.COMPLETED)
    attendance = AttendanceRecord.objects.filter(
        lesson__in=completed_lessons,
        note__startswith=config.marker,
    )
    invoices = Invoice.objects.filter(student__in=students, number__startswith=invoice_prefix)
    payments = Payment.objects.filter(idempotency_key__startswith=f"sim:{config.seed_id}:")
    allocations = PaymentAllocation.objects.filter(invoice__in=invoices)
    threads = Thread.objects.filter(subject__startswith=config.marker)
    messages = Message.objects.filter(thread__in=threads, body__startswith=config.marker)
    duplicate_attendance = (
        attendance.values("student_id", "lesson_id").annotate(rows=Count("id")).filter(rows__gt=1).count()
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM (
                    SELECT invoice.id
                      FROM finance_invoice invoice
                      LEFT JOIN finance_invoiceline line ON line.invoice_id = invoice.id
                     WHERE invoice.number LIKE %s
                     GROUP BY invoice.id, invoice.total_uzs
                    HAVING COALESCE(SUM(line.amount_uzs), 0) <> invoice.total_uzs
              ) mismatch
            """,
            [f"{invoice_prefix}%"],
        )
        invoice_total_mismatches = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM finance_paymentallocation allocation
              JOIN finance_invoice invoice ON invoice.id = allocation.invoice_id
              LEFT JOIN payments_payment payment ON payment.id = allocation.payment_id
             WHERE invoice.number LIKE %s
               AND payment.id IS NULL
            """,
            [f"{invoice_prefix}%"],
        )
        allocation_orphans = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM (
                    SELECT payment.id
                      FROM payments_payment payment
                      LEFT JOIN finance_paymentallocation allocation
                        ON allocation.payment_id = payment.id
                     WHERE payment.idempotency_key LIKE %s
                     GROUP BY payment.id, payment.amount_uzs, payment.metadata
                    HAVING COUNT(allocation.id) <> 1
                       OR COALESCE(SUM(allocation.amount_uzs), 0) <> payment.amount_uzs
                       OR MIN(allocation.invoice_id)::text
                          IS DISTINCT FROM payment.metadata ->> 'invoice_id'
              ) mismatch
            """,
            [f"sim:{config.seed_id}:%"],
        )
        allocation_mismatches = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM (
                    SELECT invoice.id
                      FROM finance_invoice invoice
                      LEFT JOIN finance_paymentallocation allocation
                        ON allocation.invoice_id = invoice.id
                     WHERE invoice.number LIKE %s
                     GROUP BY invoice.id, invoice.total_uzs, invoice.status, invoice.due_date
                    HAVING COALESCE(SUM(allocation.amount_uzs), 0) > invoice.total_uzs
                       OR (
                            COALESCE(SUM(allocation.amount_uzs), 0) >= invoice.total_uzs
                            AND invoice.status <> 'paid'
                       )
                       OR (
                            COALESCE(SUM(allocation.amount_uzs), 0) < invoice.total_uzs
                            AND invoice.due_date < %s
                            AND invoice.status <> 'overdue'
                       )
              ) mismatch
            """,
            [f"{invoice_prefix}%", config.as_of_date],
        )
        invoice_state_mismatches = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM attendance_attendancerecord ar
              JOIN schedule_lesson l ON l.id = ar.lesson_id
             WHERE ar.note LIKE %s
               AND NOT EXISTS (
                   SELECT 1
                     FROM cohorts_cohortmembership cm
                    WHERE cm.student_id = ar.student_id
                      AND cm.cohort_id = l.cohort_id
                      AND cm.start_date <= (l.starts_at AT TIME ZONE 'Asia/Tashkent')::date
                      AND (cm.end_date IS NULL OR cm.end_date >= (l.starts_at AT TIME ZONE 'Asia/Tashkent')::date)
               )
            """,
            [f"{config.marker}%"],
        )
        attendance_outside_membership = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
              FROM messaging_message m
             WHERE m.body LIKE %s
               AND NOT EXISTS (
                   SELECT 1
                     FROM messaging_threadparticipant p
                    WHERE p.thread_id = m.thread_id
                      AND p.user_id = m.sender_id
                      AND p.principal_kind = m.sender_principal_kind
                      AND p.principal_id = m.sender_principal_id
                      AND p.attribution_status IN ('captured', 'resolved')
               )
            """,
            [f"{config.marker}%"],
        )
        unseated_message_senders = int(cursor.fetchone()[0])
    oldest_lesson = completed_lessons.order_by("starts_at").values_list("starts_at", flat=True).first()
    result = {
        "students": students.count(),
        "crm_prospects": StudentProfile.objects.filter(username__startswith=prospect_prefix).count(),
        "crm_leads": CRMLead.objects.filter(
            student__username__startswith=prospect_prefix,
            state=CRMLead.State.OPEN,
        ).count(),
        "teachers": teachers.count(),
        "parents": ParentProfile.objects.filter(username__startswith=parent_prefix).count(),
        "guardians": Guardian.objects.filter(student__in=students, revoked_at__isnull=True).count(),
        "cohorts": cohorts.count(),
        "student_memberships": CohortMembership.objects.filter(student__in=students).count(),
        "teacher_assignments": CohortTeacher.objects.filter(teacher__in=teachers).count(),
        "lessons": lessons.count(),
        "completed_lessons": completed_lessons.count(),
        "attendance_records": attendance.count(),
        "oldest_lesson": oldest_lesson.isoformat() if oldest_lesson else None,
        "invoices": invoices.count(),
        "invoice_lines": InvoiceLine.objects.filter(invoice__in=invoices).count(),
        "payments": payments.count(),
        "allocations": allocations.count(),
        "exams": Exam.objects.filter(cohort__in=cohorts, title__startswith=config.marker).count(),
        "exam_results": ExamResult.objects.filter(
            exam__cohort__in=cohorts, exam__title__startswith=config.marker
        ).count(),
        "exam_lifecycle_events": ExamLifecycleEvent.objects.filter(
            exam__cohort__in=cohorts,
            exam__title__startswith=config.marker,
            event_type=ExamLifecycleEvent.EventType.PUBLISHED,
        ).count(),
        "non_english_exams": Exam.objects.filter(
            cohort__in=cohorts,
            title__startswith=config.marker,
        )
        .exclude(subject__name__iexact="English")
        .count(),
        "grades": Grade.objects.filter(student__in=students).count(),
        "assignments": Assignment.objects.filter(cohort__in=cohorts, title__startswith=config.marker).count(),
        "submissions": Submission.objects.filter(
            assignment__cohort__in=cohorts, assignment__title__startswith=config.marker
        ).count(),
        "threads": threads.count(),
        "thread_participants": ThreadParticipant.objects.filter(thread__in=threads).count(),
        "messages": messages.count(),
        "notifications": Notification.objects.filter(dedupe_key__startswith=f"sim:{config.seed_id}:").count(),
        "notification_deliveries": NotificationDelivery.objects.filter(
            notification__dedupe_key__startswith=f"sim:{config.seed_id}:"
        ).count(),
        "tasks": Task.objects.filter(title__startswith=config.marker).count(),
        "expenses": Expense.objects.filter(description__startswith=config.marker).count(),
        "expenses_without_approval": Expense.objects.filter(
            description__startswith=config.marker,
            approval_request__isnull=True,
        ).count(),
        "prospect_memberships": CohortMembership.objects.filter(
            student__username__startswith=prospect_prefix
        ).count(),
        "english_only_teachers": teachers.filter(subjects=["English"]).count(),
        "invoice_total_mismatches": invoice_total_mismatches,
        "allocation_orphans": allocation_orphans,
        "allocation_mismatches": allocation_mismatches,
        "invoice_state_mismatches": invoice_state_mismatches,
        "duplicate_attendance": duplicate_attendance,
        "attendance_outside_membership": attendance_outside_membership,
        "unseated_message_senders": unseated_message_senders,
    }
    lesson_span_ok = bool(oldest_lesson and oldest_lesson.date() <= config.as_of_date - timedelta(days=365))
    lesson_days = len(_lesson_dates(config))
    expected_parents = int(_plan(config)["parents"])
    expected_prospects = int(_plan(config)["crm_prospects"])
    expected_payments = sum(
        bool(
            _invoice_payment_fraction(
                student_index,
                month_index,
                current=month_index == 12,
            )
        )
        for student_index in range(1, config.students + 1)
        for month_index in range(1, 13)
    )
    invariants = (
        result["students"] == config.students
        and result["crm_prospects"] == expected_prospects
        and result["crm_leads"] == expected_prospects
        and result["prospect_memberships"] == 0
        and result["teachers"] == config.teachers
        and result["english_only_teachers"] == config.teachers
        and result["parents"] == expected_parents
        and result["student_memberships"] == config.students
        and result["teacher_assignments"] == config.teachers
        and result["guardians"] == config.students
        and result["cohorts"] == config.teachers
        and result["completed_lessons"] == config.teachers * lesson_days
        and result["lessons"] == config.teachers * (lesson_days + 1)
        and result["attendance_records"] == config.students * lesson_days
        and result["invoices"] == config.students * 12
        and result["invoice_lines"] == config.students * 12
        and result["payments"] == expected_payments
        and result["allocations"] == expected_payments
        and result["exams"] == config.teachers * 4
        and result["exam_results"] == config.students * 4
        and result["exam_lifecycle_events"] == config.teachers * 4
        and result["grades"] == config.students
        and result["assignments"] == config.teachers * 4
        and config.students * 3 <= result["submissions"] <= config.students * 4
        and result["threads"] == config.students
        and result["thread_participants"] == config.students * 3
        and result["messages"] == config.students * 16
        and result["notifications"] == config.students * 2
        and result["notification_deliveries"] == 0
        and result["tasks"] == config.teachers * 3
        and result["expenses"] == 72
        and lesson_span_ok
        and not any(
            result[key]
            for key in (
                "invoice_total_mismatches",
                "allocation_orphans",
                "allocation_mismatches",
                "invoice_state_mismatches",
                "expenses_without_approval",
                "non_english_exams",
                "duplicate_attendance",
                "attendance_outside_membership",
                "unseated_message_senders",
            )
        )
    )
    result["lesson_span_at_least_365_days"] = lesson_span_ok
    result["passed"] = invariants
    if not invariants:
        raise CommandError(f"Simulation verification failed: {json.dumps(result, sort_keys=True)}")
    return result
