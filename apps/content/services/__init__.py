"""Content storage services (TASKS §13, §23, TD-13/16).

The canonical signed-URL flow: `request_upload` (validate vs knobs → pending
`LessonFile` + presigned PUT) → client PUTs → `confirm_upload` (enqueue) →
`validate_uploaded_file` (libmagic sniff → clean/rejected, move tmp→content) →
`generate_thumbnail`. Downloads are presigned + counter-tracked. No S3 HTTP runs
in a request handler — only local URL signing (DoD #9).
"""

from __future__ import annotations

import io
import uuid
from pathlib import PurePosixPath

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.content.models import FileView, LessonFile, LibraryMaterial
from apps.content.signals import file_upload_confirmed
from apps.org.selectors import get_center_settings
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
)
from core.permissions import Role
from core.utils import current_schema
from infrastructure.storage.s3_client import (
    copy_object,
    delete_object,
    download_bytes,
    get_object_range,
    head_object,
    presign_download,
    presign_upload,
    upload_bytes,
)

# Declared content-type must be consistent with the extension for known types.
_EXT_MIME: dict[str, set[str]] = {
    "pdf": {"application/pdf"},
    "mp4": {"video/mp4"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "mp3": {"audio/mpeg"},
    "jpg": {"image/jpeg"},
    "jpeg": {"image/jpeg"},
    "png": {"image/png"},
    "webp": {"image/webp"},
}
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_THUMB_MAX_EDGE = 320


# ---------------------------------------------------------------------------
# Upload request (validate + presign)
# ---------------------------------------------------------------------------


def _safe_basename(filename: str) -> str:
    """Reduce a filename to a safe basename for S3-key interpolation.

    The serializers (ContentUploadUrlSerializer / NewVersionSerializer) already
    reject path separators / '..' / leading-dot for the API path; this is the
    defense-in-depth chokepoint so any direct caller (seed, version chain,
    future imports) cannot interpolate a traversal segment into the tmp key —
    and, via _filename_of, into the later final_key.
    """
    name = PurePosixPath((filename or "").replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise UnprocessableEntity(
            _("That filename is not allowed."),
            code="invalid_filename",
            fields={"filename": ["Filename must be a non-empty basename."]},
        )
    return name


def _validate_upload_inputs(*, filename: str, content_type: str, size_bytes: int, settings) -> None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in {e.lower() for e in settings.allowed_file_types}:
        raise UnprocessableEntity(
            _("That file type is not allowed."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{ext}' is not allowed."]},
        )
    expected = _EXT_MIME.get(ext)
    if expected is not None and content_type not in expected:
        raise UnprocessableEntity(
            _("The declared content type does not match the file extension."),
            code="file_type_not_allowed",
            fields={"content_type": [f"'{content_type}' is not valid for '.{ext}'."]},
        )
    if size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise UnprocessableEntity(
            _("That file is too large."),
            code="file_too_large",
            fields={"size_bytes": [f"Exceeds the {settings.max_upload_mb} MB limit."]},
        )
    if settings.storage_quota_gb is not None:
        from apps.content.selectors import storage_used_bytes

        quota_bytes = settings.storage_quota_gb * 1024 * 1024 * 1024
        if storage_used_bytes() + size_bytes > quota_bytes:
            raise UnprocessableEntity(
                _("This center has reached its storage quota."), code="storage_quota_exceeded"
            )


@transaction.atomic
def request_upload(
    *, filename, content_type, size_bytes, user=None, lesson=None, folder=None, title=None, previous=None
) -> dict:
    """Validate against the knobs and create a `pending` LessonFile with a tmp
    key + presigned PUT URL. `previous` links a new version."""
    settings = get_center_settings()
    # Sanitize to a basename before the key is built (defense in depth behind the
    # serializer): a name with '/', '\' or '..' would otherwise escape the
    # per-upload {uuid}/ isolation and taint the later {schema}/content key too.
    filename = _safe_basename(filename)
    _validate_upload_inputs(
        filename=filename, content_type=content_type, size_bytes=size_bytes, settings=settings
    )
    if lesson is None and folder is None and previous is not None:
        lesson, folder = previous.lesson, previous.folder

    schema = current_schema()
    s3_key = f"{schema}/tmp/{uuid.uuid4().hex}/{filename}"
    lesson_file = LessonFile.objects.create(
        lesson=lesson,
        folder=folder,
        title=title or filename,
        s3_key=s3_key,
        content_type=content_type,
        size_bytes=size_bytes,
        status=LessonFile.Status.PENDING,
        version=(previous.version + 1) if previous else 1,
        previous_version=previous,
        uploaded_by=user,
    )
    expires_in = 600
    url = presign_upload(s3_key, expires_in=expires_in, content_type=content_type)
    return {"file": lesson_file, "url": url, "key": s3_key, "expires_in": expires_in}


@transaction.atomic
def confirm_upload(*, file: LessonFile) -> LessonFile:
    """Mark a pending upload ready and enqueue async validation. 409 if not
    pending. No S3 call here — just enqueue. Emits ``file_upload_confirmed`` on
    commit (D4-A AI content summary consumes it)."""
    if file.status != LessonFile.Status.PENDING:
        raise ConflictException(_("This file has already been processed."), code="file_not_pending")
    schema = current_schema()
    file_id = file.pk
    requested_by = file.uploaded_by_id
    transaction.on_commit(lambda: _enqueue_validate(file_id, schema))
    transaction.on_commit(
        lambda: file_upload_confirmed.send(
            sender=LessonFile,
            file_id=file_id,
            requested_by=requested_by,
            schema_name=schema,
        )
    )
    return file


def _enqueue_validate(file_id: int, schema: str) -> None:
    from celery_tasks.content_tasks import validate_uploaded_file

    validate_uploaded_file.delay(file_id, _schema_name=schema)


# ---------------------------------------------------------------------------
# Async validation + thumbnailing (task bodies)
# ---------------------------------------------------------------------------


def _sniff_mime(buffer: bytes) -> str:
    """libmagic MIME sniff. Imported lazily so the app loads where libmagic's
    native lib is absent (e.g. a Windows dev box); CI/Linux runs it for real and
    unit tests monkeypatch this function."""
    import magic

    return magic.from_buffer(buffer, mime=True)


def _filename_of(s3_key: str) -> str:
    return s3_key.rsplit("/", 1)[-1]


def _ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _sniff_matches(*, sniffed: str, declared: str, ext: str) -> bool:
    """The libmagic sniff must match the exact MIME(s) allowed for the file's
    extension (D2-E-4); fall back to the declared family for extensions not in
    `_EXT_MIME` (no exact map to enforce)."""
    expected = _EXT_MIME.get(ext)
    if expected is not None:
        return sniffed in expected
    return sniffed.split("/")[0] == declared.split("/")[0]


def validate_uploaded_file(file_id: int) -> str:
    """Task body: sniff the uploaded object, reject on mismatch/oversize, else
    move tmp→content and mark clean (enqueuing a thumbnail for images).
    Idempotent: a non-pending file short-circuits. Runs under the tenant schema."""
    file = LessonFile.objects.get(pk=file_id)
    if file.status != LessonFile.Status.PENDING:
        return file.status

    head = head_object(file.s3_key)
    actual_size = int(head.get("ContentLength", file.size_bytes))
    settings = get_center_settings()
    if actual_size > settings.max_upload_mb * 1024 * 1024:
        return _reject(file, "Uploaded object exceeds the size limit.")
    file.size_bytes = actual_size

    # Re-validate the quota at the authoritative chokepoint: `file` is still
    # PENDING so storage_used_bytes() (CLEAN only) excludes it, making
    # `current_clean + actual_size` the correct prospective total. This closes
    # the sequential-batch / concurrent bypass that request_upload alone misses.
    if settings.storage_quota_gb is not None:
        from apps.content.selectors import storage_used_bytes

        quota_bytes = settings.storage_quota_gb * 1024 * 1024 * 1024
        if storage_used_bytes() + actual_size > quota_bytes:
            return _reject(file, "Uploaded object would exceed the storage quota.")

    filename = _filename_of(file.s3_key)
    sniffed = _sniff_mime(get_object_range(file.s3_key, start=0, end=8191))
    if not _sniff_matches(sniffed=sniffed, declared=file.content_type, ext=_ext_of(filename)):
        return _reject(file, f"Content sniff '{sniffed}' does not match declared '{file.content_type}'.")

    final_key = f"{current_schema()}/content/{file.pk}/{filename}"
    copy_object(src_key=file.s3_key, dest_key=final_key)
    delete_object(file.s3_key)

    file.s3_key = final_key
    file.status = LessonFile.Status.CLEAN
    file.save(update_fields=["s3_key", "size_bytes", "status", "updated_at"])

    if file.content_type in _IMAGE_TYPES:
        schema = current_schema()
        transaction.on_commit(lambda: _enqueue_thumbnail(file.pk, schema))
    return file.status


def _reject(file: LessonFile, reason: str) -> str:
    file.status = LessonFile.Status.REJECTED
    file.reject_reason = reason[:255]
    file.save(update_fields=["status", "reject_reason", "size_bytes", "updated_at"])
    # Mirror the happy path: drop the orphaned tmp object so rejected blobs do
    # not accumulate in the shared bucket (the lifecycle rule is a placeholder).
    delete_object(file.s3_key)
    return file.status


def _enqueue_thumbnail(file_id: int, schema: str) -> None:
    from celery_tasks.content_tasks import generate_thumbnail

    generate_thumbnail.delay(file_id, _schema_name=schema)


def generate_thumbnail(file_id: int) -> str | None:
    """Task body: render a ≤320px JPEG thumbnail for a clean image file.
    Idempotent: re-run short-circuits once `thumbnail_key` is set."""
    from PIL import Image

    file = LessonFile.objects.get(pk=file_id)
    if file.status != LessonFile.Status.CLEAN or file.content_type not in _IMAGE_TYPES:
        return None
    if file.thumbnail_key:
        return file.thumbnail_key

    raw = download_bytes(file.s3_key)
    image = Image.open(io.BytesIO(raw))
    image.thumbnail((_THUMB_MAX_EDGE, _THUMB_MAX_EDGE))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG")

    thumb_key = f"{current_schema()}/content/{file.pk}/thumb.jpg"
    upload_bytes(thumb_key, buffer.getvalue(), content_type="image/jpeg")
    file.thumbnail_key = thumb_key
    file.save(update_fields=["thumbnail_key", "updated_at"])
    return thumb_key


# ---------------------------------------------------------------------------
# Download + view tracking
# ---------------------------------------------------------------------------


def download_url(*, file: LessonFile, user, actor_is_staff: bool = False) -> dict:
    """Signed GET (TTL 300) for a CLEAN file; F()-increments download_count and
    records a FileView. Visibility is already enforced by the scoped queryset.
    F4-5: a view-only file (``is_downloadable=False``) yields no download URL to
    learners — only content staff may still pull the raw bytes to manage it."""
    if file.status != LessonFile.Status.CLEAN:
        raise ConflictException(_("This file is not available for download."), code="file_not_clean")
    if not file.is_downloadable and not actor_is_staff:
        raise ConflictException(_("This file is view-only and cannot be downloaded."), code="file_view_only")
    LessonFile.objects.filter(pk=file.pk).update(download_count=F("download_count") + 1)
    FileView.objects.create(file=file, user=user, action=FileView.Action.DOWNLOAD)
    return {"url": presign_download(file.s3_key, expires_in=300), "expires_in": 300}


def track_view(*, file: LessonFile, user) -> None:
    LessonFile.objects.filter(pk=file.pk).update(view_count=F("view_count") + 1)
    FileView.objects.create(file=file, user=user, action=FileView.Action.VIEW)


# ---------------------------------------------------------------------------
# Dual publication approval (F4-5)
# ---------------------------------------------------------------------------

# Roles allowed to give the elevated manager (second) approval. The teacher's
# ``content:*`` wildcard would otherwise also satisfy ``content:publish``, so the
# manager leg is gated on a manager ROLE in addition to the permission code.
_MANAGER_APPROVAL_ROLES = (Role.DIRECTOR, Role.HEAD_OF_DEPT)


@transaction.atomic
def approve_teacher_leg(*, file: LessonFile, actor) -> LessonFile:
    """First of two sign-offs. Records the teacher who vouches for the file. The
    row is locked so the already-approved guard and the recorded signer are
    race-free (matches the maker-checker discipline of apps/approvals)."""
    file = LessonFile.objects.select_for_update().get(pk=file.pk)
    if file.status != LessonFile.Status.CLEAN:
        raise ConflictException(_("Only a clean file can be approved."), code="file_not_clean")
    if file.is_approved_teacher:
        raise ConflictException(_("This file already has teacher approval."), code="teacher_already_approved")
    file.is_approved_teacher = True
    file.approved_teacher_by = actor
    file.approved_teacher_at = timezone.now()
    file.save(
        update_fields=[
            "is_approved_teacher",
            "approved_teacher_by",
            "approved_teacher_at",
            "updated_at",
        ]
    )
    return file


@transaction.atomic
def approve_manager_leg(
    *, file: LessonFile, actor, actor_roles, is_downloadable: bool | None = None
) -> LessonFile:
    """Second sign-off — publishes the file to learners. Maker-checker: requires
    the teacher leg first AND a different person who holds a manager role. The
    manager may also set the view-only / downloadable toggle at publish time.
    The row is locked so concurrent approvals can't clobber the recorded signer
    or the view-only toggle (last-writer-wins) and the 409 guard stays authoritative."""
    file = LessonFile.objects.select_for_update().get(pk=file.pk)
    if file.status != LessonFile.Status.CLEAN:
        raise ConflictException(_("Only a clean file can be approved."), code="file_not_clean")
    if not file.is_approved_teacher:
        raise UnprocessableEntity(
            _("A teacher must approve this file first."), code="teacher_approval_required"
        )
    if file.is_approved_manager:
        raise ConflictException(_("This file already has manager approval."), code="manager_already_approved")
    if not actor.is_superuser and not (set(actor_roles) & set(_MANAGER_APPROVAL_ROLES)):
        raise PermissionException(_("Only a manager can give the second approval."), code="not_a_manager")
    if file.approved_teacher_by_id == actor.id:
        raise PermissionException(
            _("The manager approval must come from a different person than the teacher approval."),
            code="dual_control_self",
        )
    file.is_approved_manager = True
    file.approved_manager_by = actor
    file.approved_manager_at = timezone.now()
    fields = ["is_approved_manager", "approved_manager_by", "approved_manager_at", "updated_at"]
    if is_downloadable is not None:
        file.is_downloadable = is_downloadable
        fields.append("is_downloadable")
    file.save(update_fields=fields)
    return file


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def create_new_version(*, previous: LessonFile, filename, content_type, size_bytes, user=None) -> dict:
    return request_upload(
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        user=user,
        previous=previous,
    )


# ---------------------------------------------------------------------------
# Library materials (F9-1) — AI-drafted teaching text, human-published
# ---------------------------------------------------------------------------

_MAX_MATERIAL_CHARS = 20000  # bound the AI body to the model's output + a sane cap


@transaction.atomic
def create_material(*, library, title, topic="", created_by=None) -> LibraryMaterial:
    """Create a DRAFT material in a library; the body starts empty (hand-written or
    AI-drafted via request_material_generation)."""
    return LibraryMaterial.objects.create(
        library=library, title=title, topic=topic or "", created_by=created_by
    )


def request_material_generation(*, material: LibraryMaterial, requested_by=None):
    """Ask the AI to draft the material's body from its topic. Budget-reserved and
    enqueued on commit; the task fills the body, which the manager then reviews +
    publishes. Only a DRAFT can be (re)drafted — a published material is frozen."""
    from apps.ai.models import AIFeature
    from apps.ai.services import active_prompt, check_and_reserve_budget
    from core.utils import current_schema

    if material.status != LibraryMaterial.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft material can be AI-drafted."), code="material_not_draft")
    prompt = active_prompt(AIFeature.MATERIAL_GENERATION)
    ai_request = check_and_reserve_budget(
        feature=AIFeature.MATERIAL_GENERATION,
        estimated_tokens=prompt.token_cost_cap,
        requested_by=requested_by,
        source_app="content",
        source_id=material.id,
    )
    if getattr(ai_request, "_should_enqueue", False):
        schema = current_schema()
        params = {"material_id": material.id, "title": material.title, "topic": material.topic}
        transaction.on_commit(lambda: _enqueue_material_generation(ai_request.pk, params, schema))
    return ai_request


def _enqueue_material_generation(ai_request_id: int, params: dict, schema: str) -> None:
    from celery_tasks.ai_tasks import run_material_generation

    run_material_generation.delay(ai_request_id, params=params, _schema_name=schema)


@transaction.atomic
def apply_generated_material(*, material_id: int, output_text: str) -> bool:
    """Write the AI's drafted text onto the material's body (F9-1). Idempotent +
    non-destructive: only a still-DRAFT material is updated (a published or vanished
    one is left untouched), and the body is bounded. Returns whether it was applied."""
    material = LibraryMaterial.objects.select_for_update().filter(pk=material_id).first()
    if material is None or material.status != LibraryMaterial.Status.DRAFT:
        return False
    material.body = (output_text or "").strip()[:_MAX_MATERIAL_CHARS]
    material.save(update_fields=["body", "updated_at"])
    return True


@transaction.atomic
def update_material(*, material_id: int, fields: dict) -> LibraryMaterial:
    """Hand-edit a DRAFT material's title / topic / body. Re-fetched + status-checked
    UNDER a row lock and saved with explicit update_fields, so a concurrent publish can't
    be clobbered by a stale full-row save (a lost update that would silently un-publish)."""
    material = LibraryMaterial.objects.select_for_update().filter(pk=material_id).first()
    if material is None:
        raise NotFoundException(_("Material not found."), code="material_not_found")
    if material.status != LibraryMaterial.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft material can be edited."), code="material_not_draft")
    editable = {k: v for k, v in fields.items() if k in ("title", "topic", "body")}
    if editable:
        for key, value in editable.items():
            setattr(material, key, value)
        material.save(update_fields=[*editable.keys(), "updated_at"])
    return material


@transaction.atomic
def publish_material(*, material: LibraryMaterial) -> LibraryMaterial:
    """Publish a drafted material so learners with access to the library can read it.
    A human sign-off step (the AI drafts; a person still decides to publish). Requires a
    non-empty body and locks the row so it can't be double-published."""
    material = LibraryMaterial.objects.select_for_update().get(pk=material.pk)
    if material.status == LibraryMaterial.Status.PUBLISHED:
        raise UnprocessableEntity(_("This material is already published."), code="already_published")
    if not material.body.strip():
        raise UnprocessableEntity(
            _("A material needs a body before it can be published."), code="material_empty"
        )
    material.status = LibraryMaterial.Status.PUBLISHED
    material.published_at = timezone.now()
    material.save(update_fields=["status", "published_at", "updated_at"])
    return material
