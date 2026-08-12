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
import warnings
from pathlib import PurePosixPath

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _

from apps.content.models import FileView, LessonFile, LibraryMaterial
from apps.content.selectors import can_publish_content
from apps.content.signals import file_upload_confirmed
from apps.content.storage_keys import (
    is_safe_storage_filename,
    pending_key,
    primary_key,
    thumbnail_key,
    trusted_pending_key,
    trusted_primary_key,
    trusted_thumbnail_key,
)
from apps.org.models import CenterSettings
from apps.org.selectors import get_center_settings
from core.attachment_storage import allowed_attachment_mime_types, attachment_content_matches
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
)
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

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_THUMB_MAX_EDGE = 320
_IMAGE_MAX_PIXELS = 25_000_000


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
    if not is_safe_storage_filename(name):
        raise UnprocessableEntity(
            _("That filename is not allowed."),
            code="invalid_filename",
            fields={
                "filename": [
                    "Filename must be a non-empty ASCII basename containing only letters, "
                    "digits, dots, underscores, or hyphens."
                ]
            },
        )
    return name


def _validate_upload_inputs(*, filename: str, content_type: str, size_bytes: int, settings) -> None:
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
        raise UnprocessableEntity(
            _("The file size is invalid."),
            code="invalid_file_size",
            fields={"size_bytes": ["File size must be a positive integer."]},
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in {e.lower() for e in settings.allowed_file_types}:
        raise UnprocessableEntity(
            _("That file type is not allowed."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{ext}' is not allowed."]},
        )
    expected = allowed_attachment_mime_types(filename)
    if not expected:
        raise UnprocessableEntity(
            _("That file type is not supported for uploads."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{ext}' has no reviewed file signature."]},
        )
    if content_type not in expected:
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
    *,
    filename,
    content_type,
    size_bytes,
    user=None,
    lesson=None,
    folder=None,
    title=None,
    previous=None,
    is_downloadable=True,
    submitted_by_teacher=None,
    submission_audience="",
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
    if (lesson is None) == (folder is None):
        raise UnprocessableEntity(
            _("Choose exactly one content location."),
            code="invalid_file_location",
            fields={"lesson": ["Choose either a lesson or a folder, but not both."]},
        )

    schema = current_schema()
    s3_key = pending_key(schema=schema, upload_id=uuid.uuid4().hex, filename=filename)
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
        is_downloadable=is_downloadable,
        submitted_by_teacher=submitted_by_teacher,
        submission_audience=submission_audience,
    )
    expires_in = 600
    url = presign_upload(
        s3_key,
        expires_in=expires_in,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    return {"file": lesson_file, "url": url, "key": s3_key, "expires_in": expires_in}


@transaction.atomic
def confirm_upload(*, file: LessonFile, requested_by=None, requested_principal=None) -> LessonFile:
    """Mark a pending upload ready and enqueue async validation. 409 if not
    pending. No S3 call here — just enqueue. The content-summary signal is sent
    only after validation commits a CLEAN file, so rejected/missing objects do
    not consume AI budget."""
    if file.status != LessonFile.Status.PENDING:
        raise ConflictException(_("This file has already been processed."), code="file_not_pending")
    schema = current_schema()
    file_id = file.pk
    requested_by_id = getattr(requested_by, "pk", None)
    principal_kind = getattr(requested_principal, "kind", None)
    principal_id = getattr(requested_principal, "principal_id", None)
    transaction.on_commit(
        lambda: _enqueue_validate(
            file_id,
            schema,
            requested_by_id=requested_by_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )
    )
    return file


def _enqueue_validate(
    file_id: int,
    schema: str,
    *,
    requested_by_id: int | None = None,
    principal_kind: str | None = None,
    principal_id: int | None = None,
) -> None:
    from celery_tasks.content_tasks import validate_uploaded_file

    validate_uploaded_file.delay(
        file_id,
        requested_by=requested_by_id,
        requested_principal_kind=principal_kind,
        requested_principal_id=principal_id,
        _schema_name=schema,
    )


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
    extension (D2-E-4). Unknown organization-configured extensions fail closed
    instead of relying on a broad top-level MIME family."""
    return attachment_content_matches(
        filename=f"upload.{ext}",
        declared=declared,
        sniffed=sniffed,
    )


def _bounded_image_payload(key: str, *, max_bytes: int) -> bytes:
    """Download and structurally verify an image within byte and pixel bounds."""

    from PIL import Image, UnidentifiedImageError

    raw = download_bytes(key, max_bytes=max_bytes)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > _IMAGE_MAX_PIXELS:
                    raise ValueError("Image dimensions exceed the permitted limit")
                image.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ) as exc:
        raise ValueError("Image payload is invalid or exceeds the permitted dimensions") from exc
    return raw


def _head_or_none(key: str) -> dict | None:
    try:
        return head_object(key)
    except FileNotFoundError:
        return None
    except Exception as exc:
        # S3 reports a missing key as ClientError; transient/network errors must
        # still bubble so Celery retries instead of permanently rejecting a file.
        from botocore.exceptions import ClientError

        if isinstance(exc, ClientError) and str(exc.response.get("Error", {}).get("Code")) in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return None
        raise


@transaction.atomic
def validate_uploaded_file(
    file_id: int,
    *,
    requested_by: int | None = None,
    requested_principal_kind: str | None = None,
    requested_principal_id: int | None = None,
) -> str:
    """Task body: sniff the uploaded object, reject on mismatch/oversize, else
    move tmp→content and mark clean (enqueuing a thumbnail for images).
    Idempotent: a non-pending file short-circuits. Runs under the tenant schema."""
    file = LessonFile.objects.select_for_update().get(pk=file_id)
    if file.status != LessonFile.Status.PENDING:
        return file.status

    schema = current_schema()
    source_key = trusted_pending_key(file.s3_key, schema=schema)
    if source_key is None:
        return _reject(file, "The upload storage reference is invalid.", delete_source=False)
    filename = _filename_of(source_key)
    final_key = primary_key(schema=schema, file_id=file.pk, filename=filename)

    # A previous attempt may have copied and deleted the tmp object immediately
    # before its DB transaction failed.  The deterministic record-bound final
    # path lets the retry recover without rejecting a valid upload.
    head = _head_or_none(source_key)
    already_copied = False
    if head is None:
        head = _head_or_none(final_key)
        if head is None:
            return _reject(file, "Uploaded object was not found.")
        already_copied = True
    initial_size = int(head.get("ContentLength", file.size_bytes))
    settings = get_center_settings()
    max_upload_bytes = settings.max_upload_mb * 1024 * 1024
    if initial_size < 1:
        return _reject(file, "Uploaded object is empty.")
    if initial_size > max_upload_bytes:
        return _reject(file, "Uploaded object exceeds the size limit.")

    # Serialize quota admission on the tenant singleton before the final copy.
    # Without this lock, two validation workers can each observe capacity and
    # jointly exceed it.
    if settings.storage_quota_gb is not None:
        settings = CenterSettings.objects.select_for_update().get(pk=settings.pk)

    if not already_copied:
        initial_sniff = _sniff_mime(get_object_range(source_key, start=0, end=8191))
        if not _sniff_matches(
            sniffed=initial_sniff,
            declared=file.content_type,
            ext=_ext_of(filename),
        ):
            return _reject(file, "Uploaded content does not match its declared file type.")
        copy_object(src_key=source_key, dest_key=final_key)

    # Validate the immutable, record-bound destination, not merely the tmp
    # object observed before copy. A still-valid upload URL can race metadata
    # inspection; rechecking the destination prevents a swapped payload from
    # becoming downloadable.
    final_head = _head_or_none(final_key)
    if final_head is None:
        raise RuntimeError("Content copy completed without a readable destination object")
    actual_size = int(final_head.get("ContentLength", initial_size))
    if actual_size < 1 or actual_size > max_upload_bytes:
        delete_object(final_key)
        reason = "Uploaded object is empty." if actual_size < 1 else "Uploaded object exceeds the size limit."
        return _reject(file, reason)

    sniffed = _sniff_mime(get_object_range(final_key, start=0, end=8191))
    if not _sniff_matches(sniffed=sniffed, declared=file.content_type, ext=_ext_of(filename)):
        delete_object(final_key)
        return _reject(file, "Uploaded content does not match its declared file type.")

    if file.content_type in _IMAGE_TYPES:
        try:
            _bounded_image_payload(final_key, max_bytes=max_upload_bytes)
        except ValueError:
            delete_object(final_key)
            return _reject(file, "Uploaded image is invalid or exceeds the dimension limit.")

    # `file` is still PENDING so storage_used_bytes() (CLEAN only) excludes it,
    # making current_clean + actual_size the authoritative prospective total.
    if settings.storage_quota_gb is not None:
        from apps.content.selectors import storage_used_bytes

        quota_bytes = settings.storage_quota_gb * 1024 * 1024 * 1024
        if storage_used_bytes() + actual_size > quota_bytes:
            delete_object(final_key)
            return _reject(file, "Uploaded object would exceed the storage quota.")

    file.s3_key = final_key
    file.size_bytes = actual_size
    file.status = LessonFile.Status.CLEAN
    file.save(update_fields=["s3_key", "size_bytes", "status", "updated_at"])
    if not already_copied:
        # Deletion is deliberately after the durable row update. If this call
        # fails the transaction rolls back and a retry can use the tmp object;
        # if a commit later fails, the deterministic final object is recoverable.
        delete_object(source_key)

    if file.content_type in _IMAGE_TYPES:
        transaction.on_commit(lambda: _enqueue_thumbnail(file.pk, schema))
    # HTTP confirmation supplies the exact session principal. Compatibility
    # callers may omit it; in that case the AI layer still resolves the uploader
    # only when the bridge identity is unambiguous and otherwise fails closed.
    requested_by = requested_by or file.uploaded_by_id
    transaction.on_commit(
        lambda: file_upload_confirmed.send(
            sender=LessonFile,
            file_id=file.pk,
            requested_by=requested_by,
            requested_principal_kind=requested_principal_kind,
            requested_principal_id=requested_principal_id,
            schema_name=schema,
        )
    )
    return file.status


def _reject(file: LessonFile, reason: str, *, delete_source: bool = True) -> str:
    file.status = LessonFile.Status.REJECTED
    file.reject_reason = reason[:255]
    file.save(update_fields=["status", "reject_reason", "size_bytes", "updated_at"])
    # Mirror the happy path: drop the orphaned tmp object so rejected blobs do
    # not accumulate in the shared bucket (the lifecycle rule is a placeholder).
    source_key = trusted_pending_key(file.s3_key, schema=current_schema())
    if delete_source and source_key:
        delete_object(source_key)
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
    schema = current_schema()
    primary = trusted_primary_key(file, schema=schema)
    if primary is None:
        return None
    trusted_thumbnail = trusted_thumbnail_key(file, schema=schema)
    if trusted_thumbnail:
        return trusted_thumbnail
    if file.thumbnail_key:
        # A poisoned reference must never be signed or used as an idempotency
        # marker. Clear it and regenerate only from the row-bound primary object.
        LessonFile.objects.filter(pk=file.pk).update(thumbnail_key="")
        file.thumbnail_key = ""

    settings = get_center_settings()
    raw = _bounded_image_payload(primary, max_bytes=settings.max_upload_mb * 1024 * 1024)
    image = Image.open(io.BytesIO(raw))
    image.thumbnail((_THUMB_MAX_EDGE, _THUMB_MAX_EDGE))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG")

    thumb_key = thumbnail_key(schema=schema, file_id=file.pk)
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
    storage_key = trusted_primary_key(file, schema=current_schema())
    if storage_key is None:
        raise ConflictException(_("This file is temporarily unavailable."), code="file_unavailable")
    LessonFile.objects.filter(pk=file.pk).update(download_count=F("download_count") + 1)
    FileView.objects.create(file=file, user=user, action=FileView.Action.DOWNLOAD)
    return {"url": presign_download(storage_key, expires_in=300), "expires_in": 300}


def track_view(*, file: LessonFile, user) -> None:
    if file.status != LessonFile.Status.CLEAN:
        raise ConflictException(_("This file is not available to view."), code="file_not_clean")
    LessonFile.objects.filter(pk=file.pk).update(view_count=F("view_count") + 1)
    FileView.objects.create(file=file, user=user, action=FileView.Action.VIEW)


# ---------------------------------------------------------------------------
# Dual publication approval (F4-5)
# ---------------------------------------------------------------------------


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
    the teacher leg first AND a different person with an explicit publisher
    grant. The
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
    if not actor.is_superuser and not can_publish_content(actor_roles):
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


def request_material_generation(*, material: LibraryMaterial, requested_by=None, requested_principal=None):
    """Ask the AI to draft the material's body from its topic. Budget-reserved and
    enqueued on commit; the task fills the body, which the manager then reviews +
    publishes. Only a DRAFT can be (re)drafted — a published material is frozen."""
    from apps.ai.models import AIFeature
    from apps.ai.services import check_and_reserve_budget
    from core.utils import current_schema

    if material.status != LibraryMaterial.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft material can be AI-drafted."), code="material_not_draft")
    ai_request = check_and_reserve_budget(
        feature=AIFeature.MATERIAL_GENERATION,
        requested_by=requested_by,
        requested_principal=requested_principal,
        source_app="content",
        source_id=material.id,
        params={"title": material.title, "topic": material.topic},
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
    # Raw provider output is untrusted. Preserve Markdown structure while
    # neutralizing embedded HTML; a renderer must still apply its own safe-link
    # policy, but scripts/iframes never enter the stored material as active tags.
    material.body = str(escape((output_text or "").strip()[:_MAX_MATERIAL_CHARS]))
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
