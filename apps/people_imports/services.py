"""Draft validation and bounded finalization for people imports."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.people_imports.models import PeopleImportDraft, PeopleImportRow
from apps.people_imports.parser import parse_people_file
from core.exceptions import PermissionException, StarforgeError, ValidationException
from core.permissions import get_user_roles, has_permission_code
from core.scoping import request_permission_membership_allows
from core.validators import normalize_phone, validate_phone

logger = logging.getLogger("starforge.people_imports")
User = get_user_model()

FINALIZE_CHUNK_SIZE = 25
MAX_PATCH_ROWS = 100

COMMON_FIELDS = (
    "username",
    "first_name",
    "last_name",
    "middle_name",
    "phone",
    "email",
    "birthdate",
    "gender",
    "branch",
)
STUDENT_FIELDS = (
    *COMMON_FIELDS,
    "status",
    "cohort",
    "academic_level",
    "location",
    "previous_school",
)
TEACHER_FIELDS = (
    *COMMON_FIELDS,
    "department",
    "hire_date",
    "subjects",
    "qualifications",
    "is_substitute",
)
FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    PeopleImportDraft.Kind.STUDENT: frozenset(STUDENT_FIELDS),
    PeopleImportDraft.Kind.TEACHER: frozenset(TEACHER_FIELDS),
}

_HEADER_ALIASES = {
    "username": {"username", "user name", "login", "login name"},
    "first_name": {"first name", "firstname", "given name", "name", "ism"},
    "last_name": {"last name", "lastname", "surname", "family name", "familiya"},
    "middle_name": {"middle name", "middlename", "patronymic", "fathers name", "otasining ismi"},
    "full_name": {"full name", "fullname", "student name", "teacher name", "person"},
    "phone": {"phone", "phone number", "mobile", "mobile number", "telephone", "tel", "telefon"},
    "email": {"email", "email address", "e mail"},
    "birthdate": {"birthdate", "birth date", "date of birth", "dob", "birthday", "tugilgan sana"},
    "gender": {"gender", "sex", "jins"},
    "branch": {"branch", "branch name", "campus", "campus name", "filial"},
    "status": {"status", "student status", "enrollment status"},
    "cohort": {"cohort", "cohort name", "group", "group name", "class"},
    "academic_level": {"academic level", "level", "grade", "student level"},
    "location": {"location", "address", "city", "area", "district"},
    "previous_school": {"previous school", "prior school", "school"},
    "department": {"department", "department name", "team", "faculty"},
    "hire_date": {"hire date", "hired on", "start date", "employment date"},
    "subjects": {"subjects", "subject", "teaching subjects", "specialisms"},
    "qualifications": {"qualifications", "qualification", "credentials", "education"},
    "is_substitute": {"is substitute", "substitute", "arrangement", "teacher type"},
}

_TEXT_LIMITS = {
    "username": 150,
    "first_name": 150,
    "last_name": 150,
    "middle_name": 150,
    "phone": 32,
    "email": 254,
    "academic_level": 64,
    "location": 200,
    "previous_school": 200,
    "qualifications": 4_000,
}
_STUDENT_STATUSES = frozenset(
    {"lead", "application", "accepted", "enrolled", "active", "graduated", "withdrawn"}
)
_GENDERS = {
    "": "",
    "f": "f",
    "female": "f",
    "woman": "f",
    "ayol": "f",
    "m": "m",
    "male": "m",
    "man": "m",
    "erkak": "m",
}
_TRUE_VALUES = frozenset({"true", "yes", "y", "1", "substitute", "temporary"})
_FALSE_VALUES = frozenset({"false", "no", "n", "0", "regular", "permanent", ""})


def _normalized_header(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


_ALIAS_TO_FIELD = {
    _normalized_header(alias): field
    for field, aliases in _HEADER_ALIASES.items()
    for alias in aliases | {field}
}


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    return str(value).strip()


def _source_to_data(kind: str, source: dict[str, Any], default_branch_id: int | None) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    full_name = ""
    for header, value in source.items():
        if header == "__source_row__":
            continue
        field = _ALIAS_TO_FIELD.get(_normalized_header(header))
        if field == "full_name":
            full_name = _string(value)
        elif field in FIELDS_BY_KIND[kind] and not _string(mapped.get(field)):
            mapped[field] = value
    if full_name and not (_string(mapped.get("first_name")) or _string(mapped.get("last_name"))):
        parts = full_name.split()
        if parts:
            mapped["first_name"] = parts[0]
        if len(parts) > 1:
            mapped["last_name"] = parts[-1]
        if len(parts) > 2:
            mapped["middle_name"] = " ".join(parts[1:-1])
    mapped.setdefault("branch", default_branch_id or "")
    if kind == PeopleImportDraft.Kind.STUDENT:
        mapped.setdefault("status", "lead")
        mapped.setdefault("cohort", "")
    else:
        mapped.setdefault("department", "")
        mapped.setdefault("is_substitute", False)
        mapped.setdefault("subjects", [])
    return {field: mapped.get(field, "") for field in FIELDS_BY_KIND[kind]}


def _error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)


def _parse_date(value: Any, field: str, errors: dict[str, list[str]]) -> str:
    raw = _string(value)
    if not raw:
        return ""
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    _error(errors, field, "Use a date such as 2026-08-20 or 20.08.2026.")
    return raw[:40]


def _parse_boolean(value: Any, field: str, errors: dict[str, list[str]]) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _string(value).lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    _error(errors, field, "Use Yes or No.")
    return False


def _parse_subjects(value: Any, errors: dict[str, list[str]]) -> list[str]:
    raw = value if isinstance(value, list) else re.split(r"[,;|\n]+", _string(value))
    subjects: list[str] = []
    for item in raw:
        subject = _string(item)
        if not subject:
            continue
        if len(subject) > 100:
            _error(errors, "subjects", "Each subject must be 100 characters or fewer.")
            subject = subject[:100]
        if subject.casefold() not in {existing.casefold() for existing in subjects}:
            subjects.append(subject)
    if len(subjects) > 20:
        _error(errors, "subjects", "Use no more than 20 subjects.")
        subjects = subjects[:20]
    return subjects


@dataclass(frozen=True)
class ImportLookups:
    branches: dict[int, Any]
    branch_labels: dict[str, set[int]]
    departments: dict[int, Any]
    department_labels: dict[tuple[int, str], set[int]]
    cohorts: dict[int, Any]
    cohort_labels: dict[tuple[int, str], set[int]]


def _label_keys(value: Any) -> set[str]:
    keys = {_normalized_header(getattr(value, "name", "")), _normalized_header(getattr(value, "slug", ""))}
    return {key for key in keys if key}


def _lookups() -> ImportLookups:
    from apps.cohorts.models import Cohort
    from apps.org.models import Branch, Department

    branches = {
        branch.id: branch for branch in Branch.objects.filter(is_active=True, archived_at__isnull=True)
    }
    branch_labels: dict[str, set[int]] = defaultdict(set)
    for branch in branches.values():
        for key in _label_keys(branch):
            branch_labels[key].add(branch.id)

    departments = {
        department.id: department
        for department in Department.objects.filter(is_active=True, branch_id__in=branches).select_related(
            "branch"
        )
    }
    department_labels: dict[tuple[int, str], set[int]] = defaultdict(set)
    for department in departments.values():
        for key in _label_keys(department):
            department_labels[(department.branch_id, key)].add(department.id)

    cohorts = {
        cohort.id: cohort
        for cohort in Cohort.objects.filter(is_archived=False, branch_id__in=branches).select_related(
            "department"
        )
    }
    cohort_labels: dict[tuple[int, str], set[int]] = defaultdict(set)
    for cohort in cohorts.values():
        for key in _label_keys(cohort):
            cohort_labels[(cohort.branch_id, key)].add(cohort.id)
    return ImportLookups(
        branches=branches,
        branch_labels=dict(branch_labels),
        departments=departments,
        department_labels=dict(department_labels),
        cohorts=cohorts,
        cohort_labels=dict(cohort_labels),
    )


def _resolve_id(
    value: Any,
    *,
    records: dict[int, Any],
    labels: dict[Any, set[int]],
    label_prefix: tuple[Any, ...] = (),
) -> tuple[int | None, str]:
    raw = _string(value)
    if not raw:
        return None, ""
    if re.fullmatch(r"[1-9]\d*", raw):
        candidate = int(raw)
        return (candidate, "") if candidate in records else (None, raw)
    key: Any = (*label_prefix, _normalized_header(raw)) if label_prefix else _normalized_header(raw)
    matches = labels.get(key, set())
    return (next(iter(matches)), "") if len(matches) == 1 else (None, raw)


def _authorization_request(draft: PeopleImportDraft):
    user = User.objects.filter(pk=draft.created_by_id, is_active=True).first()
    if user is None:
        return None
    return SimpleNamespace(
        user=user,
        principal_kind=draft.principal_kind,
        principal_id=draft.principal_id,
        principal_validated=False,
        method="POST",
    )


def _has_write_permission(authorization, kind: str) -> bool:
    if authorization is None:
        return False
    if getattr(authorization.user, "is_superuser", False):
        return True
    return has_permission_code(get_user_roles(authorization), f"{kind}s:write")


def _normalize_row(
    *,
    kind: str,
    raw_data: dict[str, Any],
    lookups: ImportLookups,
    authorization,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    allowed = FIELDS_BY_KIND[kind]
    data = {field: raw_data.get(field, "") for field in allowed}
    errors: dict[str, list[str]] = {}

    for field in COMMON_FIELDS:
        if field in {"birthdate", "gender", "branch"}:
            continue
        value = _string(data.get(field))
        limit = _TEXT_LIMITS.get(field)
        if limit and len(value) > limit:
            _error(errors, field, f"Use {limit} characters or fewer.")
            value = value[:limit]
        data[field] = value

    if not data["first_name"] and not data["last_name"]:
        _error(errors, "first_name", "Provide at least one name.")

    if data["phone"]:
        try:
            normalized_phone = normalize_phone(data["phone"])
            validate_phone(normalized_phone)
            data["phone"] = normalized_phone
        except (ValidationException, DjangoValidationError):
            _error(errors, "phone", "Enter a valid phone number.")
    if data["email"]:
        data["email"] = data["email"].lower()
        try:
            validate_email(data["email"])
        except DjangoValidationError:
            _error(errors, "email", "Enter a valid email address.")
    if not data["phone"] and not data["email"]:
        _error(errors, "phone", "Provide a phone or an email.")

    if data["username"]:
        try:
            UnicodeUsernameValidator()(data["username"])
        except DjangoValidationError:
            _error(errors, "username", "Use letters, numbers, and @/./+/-/_ only.")

    data["birthdate"] = _parse_date(data.get("birthdate"), "birthdate", errors)
    raw_gender = _string(data.get("gender")).lower()
    if raw_gender not in _GENDERS:
        _error(errors, "gender", "Choose Female, Male, or leave it blank.")
        data["gender"] = ""
    else:
        data["gender"] = _GENDERS[raw_gender]

    branch_id, unresolved_branch = _resolve_id(
        data.get("branch"), records=lookups.branches, labels=lookups.branch_labels
    )
    data["branch"] = branch_id or ""
    if unresolved_branch:
        _error(errors, "branch", f'No active branch matches "{unresolved_branch[:100]}".')
    elif branch_id is None:
        _error(errors, "branch", "Choose a branch.")

    if kind == PeopleImportDraft.Kind.STUDENT:
        data["status"] = _string(data.get("status") or "lead").lower().replace(" ", "_")
        if data["status"] not in _STUDENT_STATUSES:
            _error(errors, "status", "Choose a valid enrollment status.")
            data["status"] = "lead"
        for field in ("academic_level", "location", "previous_school"):
            value = _string(data.get(field))
            limit = _TEXT_LIMITS[field]
            if len(value) > limit:
                _error(errors, field, f"Use {limit} characters or fewer.")
                value = value[:limit]
            data[field] = value
        cohort_id = None
        unresolved_cohort = ""
        if _string(data.get("cohort")):
            if branch_id is None:
                unresolved_cohort = _string(data.get("cohort"))
            else:
                cohort_id, unresolved_cohort = _resolve_id(
                    data.get("cohort"),
                    records=lookups.cohorts,
                    labels=lookups.cohort_labels,
                    label_prefix=(branch_id,),
                )
                if cohort_id is not None and lookups.cohorts[cohort_id].branch_id != branch_id:
                    cohort_id, unresolved_cohort = None, _string(data.get("cohort"))
        data["cohort"] = cohort_id or ""
        if unresolved_cohort:
            _error(errors, "cohort", f'No active group in this branch matches "{unresolved_cohort[:100]}".')
        if branch_id is not None and not request_permission_membership_allows(
            authorization,
            permission="students:write",
            branch_id=branch_id,
            department_id=None,
            enforce_department=False,
            account_kinds={"staff", "teacher"},
        ):
            _error(errors, "branch", "Your student permission does not cover this branch.")
        if cohort_id is not None:
            cohort = lookups.cohorts[cohort_id]
            if not request_permission_membership_allows(
                authorization,
                permission="cohorts:write",
                branch_id=cohort.branch_id,
                department_id=cohort.department_id,
                account_kinds={"staff", "teacher"},
            ):
                _error(errors, "cohort", "Your group permission does not cover this group.")
    else:
        department_id = None
        unresolved_department = ""
        if _string(data.get("department")):
            if branch_id is None:
                unresolved_department = _string(data.get("department"))
            else:
                department_id, unresolved_department = _resolve_id(
                    data.get("department"),
                    records=lookups.departments,
                    labels=lookups.department_labels,
                    label_prefix=(branch_id,),
                )
                if department_id is not None and lookups.departments[department_id].branch_id != branch_id:
                    department_id, unresolved_department = None, _string(data.get("department"))
        data["department"] = department_id or ""
        if unresolved_department:
            _error(
                errors,
                "department",
                f'No active department in this branch matches "{unresolved_department[:100]}".',
            )
        data["hire_date"] = _parse_date(data.get("hire_date"), "hire_date", errors)
        data["subjects"] = _parse_subjects(data.get("subjects"), errors)
        qualifications = _string(data.get("qualifications"))
        if len(qualifications) > _TEXT_LIMITS["qualifications"]:
            _error(errors, "qualifications", "Use 4,000 characters or fewer.")
            qualifications = qualifications[: _TEXT_LIMITS["qualifications"]]
        data["qualifications"] = qualifications
        data["is_substitute"] = _parse_boolean(data.get("is_substitute"), "is_substitute", errors)
        if branch_id is not None and not request_permission_membership_allows(
            authorization,
            permission="teachers:write",
            branch_id=branch_id,
            department_id=department_id,
            account_kinds={"staff"},
        ):
            _error(errors, "branch", "Your teacher permission does not cover this assignment.")
    return data, errors


def _taken_usernames(values: set[str]) -> set[str]:
    if not values:
        return set()
    from django.apps import apps as django_apps

    taken = set(User.objects.filter(username__in=values).values_list("username", flat=True))
    for label in (
        "students.StudentProfile",
        "teachers.TeacherProfile",
        "parents.ParentProfile",
        "org.StaffProfile",
    ):
        model = django_apps.get_model(label)
        taken.update(model.objects.filter(username__in=values).values_list("username", flat=True))
    return taken


def _add_duplicate_errors(kind: str, rows: list[PeopleImportRow]) -> None:
    included = [row for row in rows if row.is_included and row.state != PeopleImportRow.State.IMPORTED]
    phones = [row.data.get("phone") for row in included if row.data.get("phone")]
    emails = [str(row.data.get("email") or "").casefold() for row in included if row.data.get("email")]
    usernames = [row.data.get("username") for row in included if row.data.get("username")]
    phone_counts, email_counts, username_counts = Counter(phones), Counter(emails), Counter(usernames)

    from apps.students.models import StudentProfile
    from apps.teachers.models import TeacherProfile

    model = StudentProfile if kind == PeopleImportDraft.Kind.STUDENT else TeacherProfile
    existing_phones = (
        set(model.objects.filter(phone__in=set(phones)).values_list("phone", flat=True)) if phones else set()
    )
    existing_emails = (
        set(
            model.objects.annotate(email_lower=Lower("email"))
            .filter(email_lower__in=set(emails))
            .values_list("email_lower", flat=True)
        )
        if emails
        else set()
    )
    existing_usernames = _taken_usernames(set(usernames))

    for row in included:
        errors = dict(row.errors or {})
        phone = row.data.get("phone")
        email = str(row.data.get("email") or "").casefold()
        username = row.data.get("username")
        if phone and phone_counts[phone] > 1:
            _error(errors, "phone", "This phone appears more than once in the draft.")
        if phone and phone in existing_phones:
            _error(errors, "phone", f"A {kind} already uses this phone.")
        if email and email_counts[email] > 1:
            _error(errors, "email", "This email appears more than once in the draft.")
        if email and email in existing_emails:
            _error(errors, "email", f"A {kind} already uses this email.")
        if username and username_counts[username] > 1:
            _error(errors, "username", "This username appears more than once in the draft.")
        if username and username in existing_usernames:
            _error(errors, "username", "This username is already in use.")
        row.errors = errors
        row.state = PeopleImportRow.State.INVALID if errors else PeopleImportRow.State.READY


def refresh_counts(draft: PeopleImportDraft) -> PeopleImportDraft:
    counts = draft.rows.aggregate(
        row_count=Count("id"),
        ready_count=Count("id", filter=Q(is_included=True, state=PeopleImportRow.State.READY)),
        error_count=Count("id", filter=Q(is_included=True, state=PeopleImportRow.State.INVALID)),
        excluded_count=Count("id", filter=Q(is_included=False)),
        imported_count=Count("id", filter=Q(state=PeopleImportRow.State.IMPORTED)),
    )
    for field, value in counts.items():
        setattr(draft, field, int(value or 0))
    draft.save(update_fields=[*counts.keys(), "updated_at"])
    return draft


def validate_draft(draft: PeopleImportDraft, *, authorization=None) -> PeopleImportDraft:
    authorization = authorization or _authorization_request(draft)
    if not _has_write_permission(authorization, draft.kind):
        raise PermissionException(
            _("Your permission to create these accounts is no longer active."), code="forbidden"
        )
    lookups = _lookups()
    rows = list(draft.rows.order_by("position", "id"))
    for row in rows:
        if row.state == PeopleImportRow.State.IMPORTED:
            continue
        row.data, row.errors = _normalize_row(
            kind=draft.kind,
            raw_data=row.data if isinstance(row.data, dict) else {},
            lookups=lookups,
            authorization=authorization,
        )
        row.state = PeopleImportRow.State.INVALID if row.errors else PeopleImportRow.State.READY
    _add_duplicate_errors(draft.kind, rows)
    pending = [row for row in rows if row.state != PeopleImportRow.State.IMPORTED]
    if pending:
        now = timezone.now()
        for row in pending:
            row.updated_at = now
        PeopleImportRow.objects.bulk_update(
            pending,
            ("data", "errors", "state", "updated_at"),
            batch_size=250,
        )
    return refresh_counts(draft)


def _principal_snapshot(request) -> tuple[str, int]:
    kind = str(getattr(request, "principal_kind", "") or "")
    principal_id = getattr(request, "principal_id", None)
    if kind not in {"staff", "teacher"} or not isinstance(principal_id, int) or principal_id <= 0:
        raise PermissionException(
            _("This import needs an authenticated staff or teacher profile."),
            code="forbidden",
        )
    return kind, principal_id


@transaction.atomic
def create_draft(
    *,
    request,
    kind: str,
    file_obj,
    default_branch_id: int | None = None,
) -> PeopleImportDraft:
    if kind not in FIELDS_BY_KIND:
        raise ValidationException(
            _("Choose students or teachers."),
            code="validation_error",
            fields={"kind": [_("Choose students or teachers.")]},
        )
    parsed = parse_people_file(file_obj)
    principal_kind, principal_id = _principal_snapshot(request)
    draft = PeopleImportDraft.objects.create(
        kind=kind,
        source_file_name=Path(str(getattr(file_obj, "name", "") or "import")).name[:255],
        source_sheet=parsed.sheet_name,
        created_by=request.user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        default_branch_id=default_branch_id,
    )
    import_rows = []
    for index, source in enumerate(parsed.rows, start=1):
        source_row = source.get("__source_row__")
        position = int(source_row) if source_row and source_row.isdigit() else index + 1
        stored_source = {
            str(key)[:120]: _string(value)[:500] for key, value in source.items() if key != "__source_row__"
        }
        import_rows.append(
            PeopleImportRow(
                draft=draft,
                position=position,
                source_data=stored_source,
                data=_source_to_data(kind, source, default_branch_id),
            )
        )
    PeopleImportRow.objects.bulk_create(import_rows, batch_size=250)
    return validate_draft(draft, authorization=request)


def owned_drafts(request):
    principal_kind, principal_id = _principal_snapshot(request)
    return PeopleImportDraft.objects.filter(
        created_by=request.user,
        principal_kind=principal_kind,
        principal_id=principal_id,
    )


def get_owned_draft(request, draft_id: int, *, lock: bool = False) -> PeopleImportDraft:
    query = owned_drafts(request)
    if lock:
        query = query.select_for_update()
    draft = query.filter(pk=draft_id).first()
    if draft is None:
        from core.exceptions import NotFoundException

        raise NotFoundException(code="not_found")
    return draft


@transaction.atomic
def update_rows(
    *,
    request,
    draft_id: int,
    changes: Any,
) -> PeopleImportDraft:
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_PATCH_ROWS:
        raise ValidationException(
            _("Save between 1 and 100 changed rows at a time."),
            code="validation_error",
            fields={"rows": [_("Send between 1 and 100 changed rows.")]},
        )
    draft = get_owned_draft(request, draft_id, lock=True)
    if draft.status not in {
        PeopleImportDraft.Status.DRAFT,
        PeopleImportDraft.Status.NEEDS_ATTENTION,
        PeopleImportDraft.Status.FAILED,
    }:
        raise ValidationException(
            _("This import cannot be edited in its current state."),
            code="import_locked",
        )
    allowed = FIELDS_BY_KIND[draft.kind]
    row_ids: list[int] = []
    parsed_changes: dict[int, dict[str, Any]] = {}
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("id"), int):
            raise ValidationException(_("Every changed row needs a valid id."), code="validation_error")
        row_id = change["id"]
        if row_id in parsed_changes:
            raise ValidationException(
                _("Each row may be changed only once per save."), code="validation_error"
            )
        supplied_data = change.get("data")
        if supplied_data is not None:
            if not isinstance(supplied_data, dict):
                raise ValidationException(_("Row data must be an object."), code="validation_error")
            unknown = sorted(set(supplied_data) - allowed)
            if unknown:
                raise ValidationException(
                    _("The row contains unsupported fields."),
                    code="validation_error",
                    fields={"rows": [f"Unsupported: {', '.join(unknown)}"]},
                )
        included = change.get("is_included")
        if included is not None and not isinstance(included, bool):
            raise ValidationException(_("is_included must be true or false."), code="validation_error")
        parsed_changes[row_id] = {"data": supplied_data, "is_included": included}
        row_ids.append(row_id)
    rows = list(PeopleImportRow.objects.select_for_update().filter(draft=draft, id__in=row_ids))
    if len(rows) != len(row_ids):
        raise ValidationException(
            _("One or more rows do not belong to this import."), code="validation_error"
        )
    now = timezone.now()
    for row in rows:
        if row.state == PeopleImportRow.State.IMPORTED:
            raise ValidationException(_("Imported rows cannot be changed."), code="import_row_locked")
        change = parsed_changes[row.id]
        if change["data"] is not None:
            row.data = {field: change["data"].get(field, "") for field in allowed}
        if change["is_included"] is not None:
            row.is_included = change["is_included"]
        row.updated_at = now
    PeopleImportRow.objects.bulk_update(rows, ("data", "is_included", "updated_at"), batch_size=100)
    draft.status = PeopleImportDraft.Status.DRAFT
    draft.error_message = ""
    draft.completed_at = None
    draft.save(update_fields=("status", "error_message", "completed_at", "updated_at"))
    return validate_draft(draft, authorization=request)


@transaction.atomic
def prepare_confirmation(*, request, draft_id: int) -> PeopleImportDraft:
    draft = get_owned_draft(request, draft_id, lock=True)
    if draft.status not in {
        PeopleImportDraft.Status.DRAFT,
        PeopleImportDraft.Status.NEEDS_ATTENTION,
        PeopleImportDraft.Status.FAILED,
    }:
        raise ValidationException(_("This import is already being processed."), code="import_locked")
    validate_draft(draft, authorization=request)
    draft.refresh_from_db()
    if draft.error_count:
        raise ValidationException(
            _("Fix every included row before confirming the import."),
            code="import_has_errors",
            fields={"rows": [_("Included rows still need attention.")]},
        )
    if draft.ready_count == 0:
        raise ValidationException(
            _("There are no ready accounts to import."),
            code="empty_import",
            fields={"rows": [_("Include at least one valid row.")]},
        )
    draft.status = PeopleImportDraft.Status.QUEUED
    draft.error_message = ""
    draft.started_at = None
    draft.completed_at = None
    draft.save(update_fields=("status", "error_message", "started_at", "completed_at", "updated_at"))
    return draft


def mark_dispatch_failed(draft_id: int) -> None:
    PeopleImportDraft.objects.filter(pk=draft_id, status=PeopleImportDraft.Status.QUEUED).update(
        status=PeopleImportDraft.Status.FAILED,
        error_message="The background import could not be started. Try confirming again.",
    )


def _create_student(data: dict[str, Any]):
    from apps.cohorts.models import Cohort
    from apps.cohorts.services import enroll_student_in_cohort
    from apps.org.models import Branch
    from apps.students.services import create_student

    branch = Branch.objects.get(pk=data["branch"], is_active=True, archived_at__isnull=True)
    student = create_student(
        branch=branch,
        username=data.get("username", ""),
        phone=data.get("phone", ""),
        email=data.get("email", ""),
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        middle_name=data.get("middle_name", ""),
        birthdate=date.fromisoformat(data["birthdate"]) if data.get("birthdate") else None,
        gender=data.get("gender", ""),
        status=data.get("status", "lead"),
        academic_level=data.get("academic_level", ""),
        location=data.get("location", ""),
        previous_school=data.get("previous_school", ""),
    )
    if data.get("cohort"):
        cohort = Cohort.objects.get(pk=data["cohort"], branch=branch, is_archived=False)
        enroll_student_in_cohort(cohort=cohort, student=student)
    return student


def _create_teacher(data: dict[str, Any]):
    from apps.org.models import Branch, Department
    from apps.teachers.services import create_teacher

    branch = Branch.objects.get(pk=data["branch"], is_active=True, archived_at__isnull=True)
    department = None
    if data.get("department"):
        department = Department.objects.get(pk=data["department"], branch=branch, is_active=True)
    return create_teacher(
        branch=branch,
        department=department,
        username=data.get("username", ""),
        phone=data.get("phone", ""),
        email=data.get("email", ""),
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        middle_name=data.get("middle_name", ""),
        birthdate=date.fromisoformat(data["birthdate"]) if data.get("birthdate") else None,
        gender=data.get("gender", ""),
        hire_date=date.fromisoformat(data["hire_date"]) if data.get("hire_date") else None,
        subjects=data.get("subjects") or [],
        qualifications=data.get("qualifications", ""),
        is_substitute=bool(data.get("is_substitute")),
    )


def _safe_row_error(exc: Exception) -> dict[str, list[str]]:
    if isinstance(exc, StarforgeError):
        fields = getattr(exc, "fields", None)
        if fields:
            return {
                str(field): [str(item) for item in (messages if isinstance(messages, list) else [messages])]
                for field, messages in fields.items()
            }
        return {"row": [str(exc.detail)]}
    if isinstance(exc, IntegrityError):
        return {
            "row": [
                "A contact or username was claimed while this import was running. Review this row and try again."
            ]
        }
    return {"row": ["This account could not be created. Review the row and try again."]}


def _process_row(row_id: int, kind: str) -> None:
    try:
        with transaction.atomic():
            row = PeopleImportRow.objects.select_for_update().get(pk=row_id)
            if not row.is_included or row.state != PeopleImportRow.State.READY:
                return
            created = (
                _create_student(row.data)
                if kind == PeopleImportDraft.Kind.STUDENT
                else _create_teacher(row.data)
            )
            row.created_object_id = created.pk
            row.state = PeopleImportRow.State.IMPORTED
            row.errors = {}
            row.save(update_fields=("created_object_id", "state", "errors", "updated_at"))
    except Exception as exc:  # one malformed/racing row must not abort the remaining draft
        if not isinstance(exc, (StarforgeError, IntegrityError)):
            logger.exception("Unexpected people-import row failure", extra={"row_id": row_id})
        PeopleImportRow.objects.filter(pk=row_id, state=PeopleImportRow.State.READY).update(
            state=PeopleImportRow.State.INVALID,
            errors=_safe_row_error(exc),
            updated_at=timezone.now(),
        )


def process_draft(draft_id: int) -> None:
    with transaction.atomic():
        draft = PeopleImportDraft.objects.select_for_update().filter(pk=draft_id).first()
        if draft is None or draft.status == PeopleImportDraft.Status.COMPLETED:
            return
        if draft.status not in {PeopleImportDraft.Status.QUEUED, PeopleImportDraft.Status.PROCESSING}:
            raise ValidationException(_("This import is not queued."), code="import_not_queued")
        draft.status = PeopleImportDraft.Status.PROCESSING
        draft.started_at = draft.started_at or timezone.now()
        draft.error_message = ""
        draft.save(update_fields=("status", "started_at", "error_message", "updated_at"))

    authorization = _authorization_request(draft)
    validate_draft(draft, authorization=authorization)
    draft.refresh_from_db()
    if draft.error_count:
        draft.status = PeopleImportDraft.Status.NEEDS_ATTENTION
        draft.save(update_fields=("status", "updated_at"))
        return

    while True:
        row_ids = list(
            PeopleImportRow.objects.filter(
                draft=draft,
                is_included=True,
                state=PeopleImportRow.State.READY,
            )
            .order_by("position", "id")
            .values_list("id", flat=True)[:FINALIZE_CHUNK_SIZE]
        )
        if not row_ids:
            break
        for row_id in row_ids:
            _process_row(row_id, draft.kind)
        refresh_counts(draft)

    refresh_counts(draft)
    draft.refresh_from_db()
    draft.completed_at = timezone.now()
    draft.status = (
        PeopleImportDraft.Status.NEEDS_ATTENTION if draft.error_count else PeopleImportDraft.Status.COMPLETED
    )
    draft.save(update_fields=("status", "completed_at", "updated_at"))


def mark_processing_failed(draft_id: int) -> None:
    PeopleImportDraft.objects.filter(
        pk=draft_id,
        status__in=(PeopleImportDraft.Status.QUEUED, PeopleImportDraft.Status.PROCESSING),
    ).update(
        status=PeopleImportDraft.Status.FAILED,
        error_message="The background import stopped before every row was processed. You can retry safely; completed rows will not be repeated.",
    )


@transaction.atomic
def discard_draft(*, request, draft_id: int) -> None:
    draft = get_owned_draft(request, draft_id, lock=True)
    if draft.imported_count or draft.status in {
        PeopleImportDraft.Status.QUEUED,
        PeopleImportDraft.Status.PROCESSING,
        PeopleImportDraft.Status.COMPLETED,
    }:
        raise ValidationException(
            _("This import must be retained because account creation has started."),
            code="import_locked",
        )
    draft.delete()
