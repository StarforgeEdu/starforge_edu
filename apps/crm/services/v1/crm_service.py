from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, time
from typing import Any, TypeVar

from django.apps import apps as django_apps
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Model, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.scopes import organization_audit_scope, scoped_audit_scope
from apps.audit.services import audit_log
from apps.crm.dto import (
    AttributionCreateDTO,
    CampaignCreateDTO,
    CRMScope,
    DuplicateReviewDTO,
    FollowUpCreateDTO,
    LeadCreateDTO,
    LeadFilterDTO,
    LeadOwnerDTO,
    PipelineStageDTO,
    StageTransitionDTO,
    TouchCreateDTO,
)
from apps.crm.identity import lead_identity_fingerprints
from apps.crm.interfaces.repositories import ICRMRepository
from apps.crm.interfaces.services import ICRMService
from apps.crm.models import (
    AcquisitionCampaign,
    CRMIdempotencyRecord,
    CRMLead,
    LeadAttribution,
    LeadDuplicateCandidate,
    LeadFollowUp,
    LeadMerge,
    LeadSource,
    LeadStageHistory,
    LeadTouch,
    PipelineStage,
)
from apps.students.models import StudentProfile
from core.exceptions import ConflictException, NotFoundException, ValidationException
from core.permissions import get_user_roles_for_user
from core.role_principals import PRINCIPAL_MODELS, STAFF_PRINCIPAL_KINDS, RolePrincipal
from core.scoping import permission_membership_scopes
from core.utils import current_schema, stable_hash

T = TypeVar("T")


def _canonical_fingerprint(value: Any) -> str:
    return stable_hash(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def _validate_idempotency_key(raw: str) -> str:
    if not isinstance(raw, str) or raw != raw.strip() or not 8 <= len(raw) <= 128:
        raise ValidationException(
            _("Idempotency-Key must contain 8 to 128 unpadded characters."),
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": [_("Provide a valid idempotency key.")]},
        )
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in raw):
        raise ValidationException(
            _("Idempotency-Key contains unsupported characters."),
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": [_("Use visible ASCII characters only.")]},
        )
    return raw


def _grant_covers(grant, *, branch_id: int, department_id: int | None) -> bool:
    return grant.is_organization_wide or (
        grant.branch_id == branch_id and (grant.department_id is None or grant.department_id == department_id)
    )


def _selected_principal(owner: LeadOwnerDTO, *, branch_id: int, department_id: int | None) -> RolePrincipal:
    if owner.principal_kind not in STAFF_PRINCIPAL_KINDS or owner.principal_id < 1:
        raise ValidationException(
            _("Choose an active staff or teacher role account."),
            code="validation_error",
            fields={"owner": [_("Choose an active CRM role account.")]},
        )
    model = django_apps.get_model(PRINCIPAL_MODELS[owner.principal_kind])
    profile = (
        model.objects.select_related("user")
        .filter(pk=owner.principal_id, is_active=True, user__is_active=True)
        .first()
    )
    if profile is None:
        raise ValidationException(
            _("Choose an active staff or teacher role account."),
            code="validation_error",
            fields={"owner": [_("Choose an active CRM role account.")]},
        )
    roles = get_user_roles_for_user(
        profile.user,
        principal_kind=owner.principal_kind,
        principal_id=owner.principal_id,
        principal_validated=True,
    )
    grants = permission_membership_scopes(
        roles=roles,
        permission="crm:read",
        account_kinds=STAFF_PRINCIPAL_KINDS,
    )
    if not any(_grant_covers(grant, branch_id=branch_id, department_id=department_id) for grant in grants):
        # Generic selection denial avoids exposing whether a guessed profile exists.
        raise ValidationException(
            _("Choose an owner who can access this lead."),
            code="validation_error",
            fields={"owner": [_("Choose an active CRM role account in this scope.")]},
        )
    return RolePrincipal(
        kind=owner.principal_kind,
        principal_id=owner.principal_id,
        user_id=profile.user_id,
    )


def _state_for_stage(stage: PipelineStage) -> str:
    if stage.category == PipelineStage.Category.OPEN:
        return CRMLead.State.OPEN
    if stage.category == PipelineStage.Category.WON:
        return CRMLead.State.WON
    if stage.category == PipelineStage.Category.LOST:
        return CRMLead.State.LOST
    raise RuntimeError("Unsupported CRM pipeline-stage category")


class CRMService(ICRMService):
    def __init__(self, repository: ICRMRepository) -> None:
        self._repository = repository

    def leads(self, *, scope: CRMScope, filters: LeadFilterDTO) -> QuerySet[CRMLead]:
        return self._repository.scoped_leads(scope=scope, filters=filters)

    def get_lead(self, *, scope: CRMScope, pk: int) -> CRMLead | None:
        return self._repository.get_scoped_lead(scope=scope, pk=pk)

    def stages(self, *, active_only: bool = False) -> QuerySet[PipelineStage]:
        return self._repository.stages(active_only=active_only)

    def sources(self, *, active_only: bool = False) -> QuerySet[LeadSource]:
        return self._repository.sources(active_only=active_only)

    def campaigns(self, *, scope: CRMScope, active_only: bool = False) -> QuerySet[AcquisitionCampaign]:
        return self._repository.campaigns(scope=scope, active_only=active_only)

    def stage_history(self, lead: CRMLead) -> QuerySet[LeadStageHistory]:
        return self._repository.stage_history(lead)

    def touches(self, lead: CRMLead) -> QuerySet[LeadTouch]:
        return self._repository.touches(lead)

    def follow_ups(self, lead: CRMLead) -> QuerySet[LeadFollowUp]:
        return self._repository.follow_ups(lead)

    def follow_up_register(self, *, scope: CRMScope) -> QuerySet[LeadFollowUp]:
        return self._repository.scoped_follow_ups(scope=scope)

    def attributions(self, lead: CRMLead) -> QuerySet[LeadAttribution]:
        return self._repository.attributions(lead)

    def duplicates(self, *, scope: CRMScope) -> QuerySet[LeadDuplicateCandidate]:
        return self._repository.duplicate_candidates(scope=scope)

    @transaction.atomic
    def create_stage(
        self,
        data: PipelineStageDTO,
        *,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[PipelineStage, bool]:
        def operation() -> PipelineStage:
            if PipelineStage.objects.filter(Q(slug=data.slug) | Q(position=data.position)).exists():
                raise ConflictException(
                    _("A stage already uses this slug or position."), code="stage_conflict"
                )
            try:
                with transaction.atomic():
                    stage = PipelineStage.objects.create(
                        slug=data.slug,
                        name=data.name,
                        category=data.category,
                        position=data.position,
                    )
            except IntegrityError as exc:
                raise ConflictException(
                    _("A stage already uses this slug or position."), code="stage_conflict"
                ) from exc
            audit_log(
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.PipelineStage",
                resource_id=stage.pk,
                after={"slug": stage.slug, "category": stage.category, "position": stage.position},
                scope=organization_audit_scope(),
            )
            return stage

        return self._idempotent(
            operation_name="catalog.stage.create",
            payload=asdict(data),
            scope=CRMScope(organization_wide=True),
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="stage",
            operation=operation,
        )

    @transaction.atomic
    def update_stage(
        self,
        stage_id: int,
        changes: dict[str, Any],
        *,
        actor,
        actor_principal: RolePrincipal,
    ) -> PipelineStage:
        stage = PipelineStage.objects.select_for_update().filter(pk=stage_id).first()
        if stage is None:
            raise NotFoundException(code="not_found")
        if {"slug", "position"} & changes.keys():
            conflict = PipelineStage.objects.exclude(pk=stage.pk).filter(
                Q(slug=changes.get("slug", stage.slug)) | Q(position=changes.get("position", stage.position))
            )
            if conflict.exists():
                raise ConflictException(
                    _("A stage already uses this slug or position."), code="stage_conflict"
                )
        if "category" in changes and changes["category"] != stage.category and stage.leads.exists():
            raise ConflictException(
                _("A stage in use cannot change lifecycle category."), code="stage_in_use"
            )
        before = {field: getattr(stage, field) for field in changes}
        for field, value in changes.items():
            setattr(stage, field, value)
        try:
            with transaction.atomic():
                stage.save(update_fields=[*changes, "updated_at"])
        except IntegrityError as exc:
            raise ConflictException(
                _("A stage already uses this slug or position."), code="stage_conflict"
            ) from exc
        audit_log(
            actor=actor,
            actor_principal=actor_principal,
            action="update",
            resource_type="crm.PipelineStage",
            resource_id=stage.pk,
            before=before,
            after={field: getattr(stage, field) for field in changes},
            scope=organization_audit_scope(),
        )
        return stage

    @transaction.atomic
    def create_source(
        self,
        *,
        slug: str,
        name: str,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadSource, bool]:
        def operation() -> LeadSource:
            if LeadSource.objects.filter(slug=slug).exists():
                raise ConflictException(_("A source already uses this slug."), code="source_conflict")
            try:
                with transaction.atomic():
                    source = LeadSource.objects.create(slug=slug, name=name)
            except IntegrityError as exc:
                raise ConflictException(
                    _("A source already uses this slug."), code="source_conflict"
                ) from exc
            audit_log(
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.LeadSource",
                resource_id=source.pk,
                after={"slug": source.slug, "name": source.name},
                scope=organization_audit_scope(),
            )
            return source

        return self._idempotent(
            operation_name="catalog.source.create",
            payload={"slug": slug, "name": name},
            scope=CRMScope(organization_wide=True),
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="source",
            operation=operation,
        )

    @transaction.atomic
    def create_campaign(
        self,
        data: CampaignCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[AcquisitionCampaign, bool]:
        def operation() -> AcquisitionCampaign:
            source = LeadSource.objects.filter(pk=data.source_id, is_active=True).first()
            if source is None:
                raise ValidationException(
                    _("Choose an active lead source."),
                    fields={"source": [_("Choose an active lead source.")]},
                )
            branch, department = self._campaign_boundary(data, scope=scope)
            if AcquisitionCampaign.objects.filter(code=data.code).exists():
                raise ConflictException(_("A campaign already uses this code."), code="campaign_conflict")
            try:
                with transaction.atomic():
                    campaign = AcquisitionCampaign.objects.create(
                        code=data.code,
                        name=data.name,
                        source=source,
                        branch=branch,
                        department=department,
                        starts_on=data.starts_on,
                        ends_on=data.ends_on,
                    )
            except IntegrityError as exc:
                raise ConflictException(
                    _("A campaign already uses this code."), code="campaign_conflict"
                ) from exc
            audit_log(
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.AcquisitionCampaign",
                resource_id=campaign.pk,
                after={
                    "code": campaign.code,
                    "source_id": source.pk,
                    "branch_id": campaign.branch_id,
                    "department_id": campaign.department_id,
                },
                scope=(
                    scoped_audit_scope(campaign.branch_id, campaign.department_id)
                    if campaign.branch_id is not None
                    else organization_audit_scope()
                ),
            )
            return campaign

        return self._idempotent(
            operation_name="catalog.campaign.create",
            payload=asdict(data),
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="campaign",
            operation=operation,
        )

    def _campaign_boundary(self, data: CampaignCreateDTO, *, scope: CRMScope):
        from apps.org.models import Branch, Department

        if data.branch_id is None:
            if not scope.organization_wide or data.department_id is not None:
                raise ValidationException(
                    _("An organization-wide campaign requires organization-wide CRM scope."),
                    fields={"branch": [_("Choose a branch in your CRM scope.")]},
                )
            return None, None
        branch = Branch.objects.filter(pk=data.branch_id, is_active=True, archived_at__isnull=True).first()
        department = (
            Department.objects.filter(pk=data.department_id, branch_id=data.branch_id, is_active=True).first()
            if data.department_id is not None
            else None
        )
        if (
            branch is None
            or (data.department_id is not None and department is None)
            or not scope.allows(branch_id=data.branch_id, department_id=data.department_id)
        ):
            raise ValidationException(
                _("Choose an active campaign boundary in your CRM scope."),
                fields={"branch": [_("Choose an accessible branch and department.")]},
            )
        return branch, department

    @transaction.atomic
    def create_lead(
        self,
        data: LeadCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[CRMLead, bool]:
        payload = {
            "student": data.student_id,
            "stage": data.stage_id,
            "department": data.department_id,
            "owner": asdict(data.owner) if data.owner else None,
            "source": data.source_id,
            "campaign": data.campaign_id,
            "medium": data.medium,
            "content": data.content,
            "occurred_at": data.attribution_occurred_at,
        }

        def operation() -> CRMLead:
            student = (
                StudentProfile.objects.select_for_update(of=("self",))
                .select_related("branch", "current_cohort")
                .filter(pk=data.student_id)
                .first()
            )
            department_id = data.department_id
            if student is None or not scope.allows(
                branch_id=student.branch_id,
                department_id=department_id,
            ):
                raise NotFoundException(code="not_found")
            if student.status != StudentProfile.Status.LEAD or not student.is_active:
                raise ConflictException(_("Only an active student lead can enter CRM."), code="not_a_lead")
            if CRMLead.objects.filter(student=student).exists():
                raise ConflictException(_("This student already has a CRM lead."), code="lead_exists")
            department = self._department(student.branch_id, department_id)
            stage = PipelineStage.objects.filter(pk=data.stage_id, is_active=True).first()
            if stage is None or stage.category != PipelineStage.Category.OPEN:
                raise ValidationException(
                    _("Choose an active open pipeline stage."),
                    fields={"stage": [_("Choose an active open stage.")]},
                )
            owner_principal = (
                _selected_principal(
                    data.owner,
                    branch_id=student.branch_id,
                    department_id=department_id,
                )
                if data.owner
                else None
            )
            source, campaign = self._attribution_targets(
                source_id=data.source_id,
                campaign_id=data.campaign_id,
                branch_id=student.branch_id,
                department_id=department_id,
            )
            assert source is not None
            lead = CRMLead.objects.create(
                student=student,
                branch=student.branch,
                department=department,
                stage=stage,
                owner_id=owner_principal.user_id if owner_principal else None,
                owner_principal_kind=owner_principal.kind if owner_principal else "",
                owner_principal_id=owner_principal.principal_id if owner_principal else None,
                initial_source=source,
                initial_campaign=campaign,
                created_by=actor,
                created_by_principal_kind=actor_principal.kind,
                created_by_principal_id=actor_principal.principal_id,
                **lead_identity_fingerprints(student),
            )
            history = LeadStageHistory.objects.create(
                lead=lead,
                from_stage=None,
                to_stage=stage,
                from_state=CRMLead.State.OPEN,
                to_state=CRMLead.State.OPEN,
                note="Lead entered CRM.",
                actor=actor,
                actor_principal_kind=actor_principal.kind,
                actor_principal_id=actor_principal.principal_id,
            )
            LeadAttribution.objects.create(
                lead=lead,
                source=source,
                campaign=campaign,
                medium=data.medium,
                content=data.content,
                occurred_at=data.attribution_occurred_at or timezone.now(),
                actor=actor,
                actor_principal_kind=actor_principal.kind,
                actor_principal_id=actor_principal.principal_id,
            )
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.CRMLead",
                resource_id=lead.pk,
                after={
                    "student_id": student.pk,
                    "stage_id": stage.pk,
                    "state": lead.state,
                    "owner_principal_kind": lead.owner_principal_kind,
                    "owner_principal_id": lead.owner_principal_id,
                    "stage_history_id": history.pk,
                },
            )
            return lead

        return self._idempotent(
            operation_name="lead.create",
            payload=payload,
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="lead",
            operation=operation,
        )

    @transaction.atomic
    def assign_owner(
        self,
        lead_id: int,
        owner: LeadOwnerDTO | None,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[CRMLead, bool]:
        def operation() -> CRMLead:
            lead = self._locked_lead(scope, lead_id)
            if lead.state == CRMLead.State.MERGED:
                raise ConflictException(_("A merged lead cannot be reassigned."), code="lead_merged")
            selected = (
                _selected_principal(owner, branch_id=lead.branch_id, department_id=lead.department_id)
                if owner
                else None
            )
            before = {
                "owner_principal_kind": lead.owner_principal_kind,
                "owner_principal_id": lead.owner_principal_id,
            }
            lead.owner_id = selected.user_id if selected else None
            lead.owner_principal_kind = selected.kind if selected else ""
            lead.owner_principal_id = selected.principal_id if selected else None
            lead.version += 1
            lead.save(
                update_fields=(
                    "owner",
                    "owner_principal_kind",
                    "owner_principal_id",
                    "version",
                    "updated_at",
                )
            )
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="update",
                resource_type="crm.CRMLeadOwner",
                resource_id=lead.pk,
                before=before,
                after={
                    "owner_principal_kind": lead.owner_principal_kind,
                    "owner_principal_id": lead.owner_principal_id,
                },
            )
            return lead

        return self._idempotent(
            operation_name=f"lead.owner:{lead_id}",
            payload=asdict(owner) if owner else None,
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="lead",
            operation=operation,
        )

    @transaction.atomic
    def transition(
        self,
        lead_id: int,
        data: StageTransitionDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadStageHistory, bool]:
        def operation() -> LeadStageHistory:
            lead = self._locked_lead(scope, lead_id)
            if lead.state in {CRMLead.State.WON, CRMLead.State.MERGED}:
                raise ConflictException(_("A converted or merged lead is terminal."), code="lead_closed")
            if lead.version != data.expected_version:
                raise ConflictException(_("The lead changed; refresh and retry."), code="version_conflict")
            stage = PipelineStage.objects.filter(pk=data.stage_id, is_active=True).first()
            if stage is None:
                raise ValidationException(
                    _("Choose an active pipeline stage."), fields={"stage": [_("Choose an active stage.")]}
                )
            if stage.pk == lead.stage_id:
                raise ConflictException(_("The lead is already in this stage."), code="same_stage")
            next_state = _state_for_stage(stage)
            if lead.state == CRMLead.State.LOST and next_state != CRMLead.State.OPEN:
                raise ConflictException(
                    _("A lost lead may only be reopened into an open stage."),
                    code="invalid_transition",
                )
            if lead.state == CRMLead.State.LOST and not data.note.strip():
                raise ValidationException(
                    _("A reopening reason is required."),
                    fields={"note": [_("Explain why this lost lead is being reopened.")]},
                )
            loss_reason = data.loss_reason.strip()
            if next_state == CRMLead.State.LOST and not loss_reason:
                raise ValidationException(
                    _("A loss reason is required."),
                    fields={"loss_reason": [_("Explain why this lead was lost.")]},
                )
            if next_state != CRMLead.State.LOST and loss_reason:
                raise ValidationException(
                    _("Loss reason is only valid for a lost stage."),
                    fields={"loss_reason": [_("Remove this field for a non-lost stage.")]},
                )
            old_stage_id = lead.stage_id
            old_state = lead.state
            student = StudentProfile.objects.select_for_update(of=("self",)).get(pk=lead.student_id)
            lead.student = student
            lead.stage = stage
            lead.state = next_state
            lead.loss_reason = loss_reason
            lead.version += 1
            history = LeadStageHistory.objects.create(
                lead=lead,
                from_stage_id=old_stage_id,
                to_stage=stage,
                from_state=old_state,
                to_state=next_state,
                loss_reason=loss_reason,
                note=data.note,
                actor=actor,
                actor_principal_kind=actor_principal.kind,
                actor_principal_id=actor_principal.principal_id,
            )
            # Evidence is appended before the lifecycle row changes. The
            # database trigger therefore rejects bulk or raw updates that try
            # to bypass history, while this entire unit remains transactional.
            lead.save(update_fields=("stage", "state", "loss_reason", "version", "updated_at"))
            if next_state == CRMLead.State.WON:
                if lead.student.status != StudentProfile.Status.LEAD:
                    raise ConflictException(
                        _("The student lifecycle no longer matches this lead."),
                        code="student_state_conflict",
                    )
                from apps.students.services import transition_enrollment

                transition_enrollment(
                    student=lead.student,
                    to_status=StudentProfile.Status.APPLICATION,
                    note=f"CRM conversion from lead #{lead.pk}",
                    actor=actor,
                )
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="update",
                resource_type="crm.LeadStageHistory",
                resource_id=history.pk,
                before={"lead_id": lead.pk, "stage_id": old_stage_id, "state": old_state},
                after={
                    "lead_id": lead.pk,
                    "stage_id": stage.pk,
                    "state": next_state,
                    "loss_reason": loss_reason,
                    "actor_principal_kind": actor_principal.kind,
                    "actor_principal_id": actor_principal.principal_id,
                },
            )
            return history

        return self._idempotent(
            operation_name=f"lead.transition:{lead_id}",
            payload=asdict(data),
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="stage_history",
            operation=operation,
        )

    @transaction.atomic
    def add_touch(
        self,
        lead_id: int,
        data: TouchCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadTouch, bool]:
        def operation() -> LeadTouch:
            lead = self._locked_lead(scope, lead_id)
            if lead.state == CRMLead.State.MERGED:
                raise ConflictException(_("Record touches on the canonical lead."), code="lead_merged")
            touch = LeadTouch.objects.create(
                lead=lead,
                channel=data.channel,
                direction=data.direction,
                outcome=data.outcome,
                summary=data.summary,
                occurred_at=data.occurred_at or timezone.now(),
                actor=actor,
                actor_principal_kind=actor_principal.kind,
                actor_principal_id=actor_principal.principal_id,
            )
            # Never copy free-form communication text into the general audit feed.
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.LeadTouch",
                resource_id=touch.pk,
                after={
                    "lead_id": lead.pk,
                    "channel": touch.channel,
                    "direction": touch.direction,
                    "outcome": touch.outcome,
                    "occurred_at": touch.occurred_at.isoformat(),
                },
            )
            return touch

        return self._idempotent(
            operation_name=f"lead.touch:{lead_id}",
            payload=asdict(data),
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="touch",
            operation=operation,
        )

    @transaction.atomic
    def add_follow_up(
        self,
        lead_id: int,
        data: FollowUpCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadFollowUp, bool]:
        def operation() -> LeadFollowUp:
            lead = self._locked_lead(scope, lead_id)
            if lead.state != CRMLead.State.OPEN:
                raise ConflictException(_("Follow-ups require an open lead."), code="lead_closed")
            if data.due_at <= timezone.now():
                raise ValidationException(
                    _("Follow-up time must be in the future."),
                    fields={"due_at": [_("Choose a future time.")]},
                )
            owner = data.assignee or (
                LeadOwnerDTO(lead.owner_principal_kind, int(lead.owner_principal_id))
                if lead.owner_principal_id is not None
                else LeadOwnerDTO(actor_principal.kind, actor_principal.principal_id)
            )
            assignee = _selected_principal(owner, branch_id=lead.branch_id, department_id=lead.department_id)
            follow_up = LeadFollowUp.objects.create(
                lead=lead,
                due_at=data.due_at,
                purpose=data.purpose,
                assignee_id=assignee.user_id,
                assignee_principal_kind=assignee.kind,
                assignee_principal_id=assignee.principal_id,
                created_by=actor,
                created_by_principal_kind=actor_principal.kind,
                created_by_principal_id=actor_principal.principal_id,
            )
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.LeadFollowUp",
                resource_id=follow_up.pk,
                after={
                    "lead_id": lead.pk,
                    "due_at": follow_up.due_at.isoformat(),
                    "assignee_principal_kind": assignee.kind,
                    "assignee_principal_id": assignee.principal_id,
                },
            )
            return follow_up

        return self._idempotent(
            operation_name=f"lead.follow_up:{lead_id}",
            payload=asdict(data),
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="follow_up",
            operation=operation,
        )

    @transaction.atomic
    def resolve_follow_up(
        self,
        follow_up_id: int,
        *,
        status: str,
        note: str,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadFollowUp, bool]:
        def operation() -> LeadFollowUp:
            visible = (
                LeadFollowUp.objects.filter(
                    pk=follow_up_id, lead__in=self.leads(scope=scope, filters=LeadFilterDTO())
                )
                .values_list("lead_id", flat=True)
                .first()
            )
            if visible is None:
                raise NotFoundException(code="not_found")
            self._locked_lead(scope, int(visible))
            follow_up = LeadFollowUp.objects.select_for_update().filter(pk=follow_up_id).first()
            if follow_up is None:
                raise NotFoundException(code="not_found")
            if follow_up.status != LeadFollowUp.Status.PENDING:
                raise ConflictException(_("This follow-up is already resolved."), code="follow_up_closed")
            follow_up.status = status
            follow_up.resolution_note = note
            follow_up.resolved_by = actor
            follow_up.resolved_by_principal_kind = actor_principal.kind
            follow_up.resolved_by_principal_id = actor_principal.principal_id
            follow_up.resolved_at = timezone.now()
            follow_up.save(
                update_fields=(
                    "status",
                    "resolution_note",
                    "resolved_by",
                    "resolved_by_principal_kind",
                    "resolved_by_principal_id",
                    "resolved_at",
                    "updated_at",
                )
            )
            self._audit(
                follow_up.lead,
                actor=actor,
                actor_principal=actor_principal,
                action="update",
                resource_type="crm.LeadFollowUp",
                resource_id=follow_up.pk,
                before={"status": LeadFollowUp.Status.PENDING},
                after={"status": status, "resolved_at": follow_up.resolved_at.isoformat()},
            )
            return follow_up

        return self._idempotent(
            operation_name=f"follow_up.resolve:{follow_up_id}",
            payload={"status": status, "note": note},
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="follow_up",
            operation=operation,
        )

    @transaction.atomic
    def add_attribution(
        self,
        lead_id: int,
        data: AttributionCreateDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadAttribution, bool]:
        def operation() -> LeadAttribution:
            lead = self._locked_lead(scope, lead_id)
            if lead.state == CRMLead.State.MERGED:
                raise ConflictException(_("Attribute the canonical lead."), code="lead_merged")
            source, campaign = self._attribution_targets(
                source_id=data.source_id,
                campaign_id=data.campaign_id,
                branch_id=lead.branch_id,
                department_id=lead.department_id,
            )
            assert source is not None
            attribution = LeadAttribution.objects.create(
                lead=lead,
                source=source,
                campaign=campaign,
                medium=data.medium,
                content=data.content,
                occurred_at=data.occurred_at or timezone.now(),
                actor=actor,
                actor_principal_kind=actor_principal.kind,
                actor_principal_id=actor_principal.principal_id,
            )
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.LeadAttribution",
                resource_id=attribution.pk,
                after={
                    "lead_id": lead.pk,
                    "source_id": source.pk,
                    "campaign_id": attribution.campaign_id,
                    "occurred_at": attribution.occurred_at.isoformat(),
                },
            )
            return attribution

        return self._idempotent(
            operation_name=f"lead.attribution:{lead_id}",
            payload=asdict(data),
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="attribution",
            operation=operation,
        )

    @transaction.atomic
    def detect_duplicates(
        self,
        lead_id: int,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[list[LeadDuplicateCandidate], bool]:
        def operation() -> CRMLead:
            lead = self._locked_lead(scope, lead_id)
            if lead.state == CRMLead.State.MERGED:
                raise ConflictException(_("Use the canonical lead."), code="lead_merged")
            student = StudentProfile.objects.select_for_update(of=("self",)).get(pk=lead.student_id)
            lead.student = student
            fingerprints = lead_identity_fingerprints(student)
            CRMLead.objects.filter(pk=lead.pk).update(**fingerprints)
            for field, value in fingerprints.items():
                setattr(lead, field, value)
            signal_q = Q(pk__in=[])
            for field, value in fingerprints.items():
                if value:
                    signal_q |= Q(**{field: value})
            if not signal_q:
                return lead
            candidates = list(
                self._repository.scoped_leads(scope=scope)
                .filter(signal_q)
                .exclude(pk=lead.pk)
                .exclude(state=CRMLead.State.MERGED)
                .order_by("pk")[:100]
            )
            for other in candidates:
                signals: list[str] = []
                if lead.phone_fingerprint and lead.phone_fingerprint == other.phone_fingerprint:
                    signals.append("phone")
                if lead.email_fingerprint and lead.email_fingerprint == other.email_fingerprint:
                    signals.append("email")
                if lead.identity_fingerprint and lead.identity_fingerprint == other.identity_fingerprint:
                    signals.append("name_birthdate")
                score = 100 if {"phone", "email"} & set(signals) else 80
                left_id, right_id = sorted((lead.pk, other.pk))
                candidate, created = LeadDuplicateCandidate.objects.get_or_create(
                    left_id=left_id,
                    right_id=right_id,
                    defaults={"score": score, "signals": signals},
                )
                if not created and candidate.status == LeadDuplicateCandidate.Status.PENDING:
                    LeadDuplicateCandidate.objects.filter(pk=candidate.pk).update(
                        score=max(candidate.score, score),
                        signals=sorted(set(candidate.signals) | set(signals)),
                    )
            self._audit(
                lead,
                actor=actor,
                actor_principal=actor_principal,
                action="create",
                resource_type="crm.DuplicateScan",
                resource_id=lead.pk,
                after={"lead_id": lead.pk, "candidate_count": len(candidates)},
            )
            return lead

        _lead, replayed = self._idempotent(
            operation_name=f"lead.duplicate_scan:{lead_id}",
            payload={"lead": lead_id},
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="duplicate_scan",
            operation=operation,
        )
        candidates = list(
            self._repository.duplicate_candidates(scope=scope).filter(
                Q(left_id=lead_id) | Q(right_id=lead_id)
            )
        )
        return candidates, replayed

    @transaction.atomic
    def dismiss_duplicate(
        self,
        candidate_id: int,
        data: DuplicateReviewDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadDuplicateCandidate, bool]:
        def operation() -> LeadDuplicateCandidate:
            candidate = self._locked_candidate(scope, candidate_id)
            if candidate.status != LeadDuplicateCandidate.Status.PENDING:
                raise ConflictException(
                    _("This duplicate candidate was already reviewed."), code="duplicate_reviewed"
                )
            now = timezone.now()
            candidate.status = LeadDuplicateCandidate.Status.DISMISSED
            candidate.reviewed_by = actor
            candidate.reviewed_by_principal_kind = actor_principal.kind
            candidate.reviewed_by_principal_id = actor_principal.principal_id
            candidate.reviewed_at = now
            candidate.rationale = data.rationale
            candidate.save(
                update_fields=(
                    "status",
                    "reviewed_by",
                    "reviewed_by_principal_kind",
                    "reviewed_by_principal_id",
                    "reviewed_at",
                    "rationale",
                )
            )
            self._audit(
                candidate.left,
                actor=actor,
                actor_principal=actor_principal,
                action="update",
                resource_type="crm.LeadDuplicateCandidate",
                resource_id=candidate.pk,
                before={"status": "pending"},
                after={"status": "dismissed", "right_id": candidate.right_id},
            )
            return candidate

        return self._idempotent(
            operation_name=f"duplicate.dismiss:{candidate_id}",
            payload={"rationale": data.rationale},
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="duplicate_candidate",
            operation=operation,
        )

    @transaction.atomic
    def merge_duplicate(
        self,
        candidate_id: int,
        data: DuplicateReviewDTO,
        *,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        idempotency_key: str,
    ) -> tuple[LeadMerge, bool]:
        def operation() -> LeadMerge:
            visible = self._repository.get_duplicate_candidate(scope=scope, pk=candidate_id)
            if visible is None:
                raise NotFoundException(code="not_found")
            if data.canonical_lead_id not in {visible.left_id, visible.right_id}:
                raise ValidationException(
                    _("Canonical lead must be one member of the reviewed pair."),
                    fields={"canonical_lead": [_("Choose one lead from this candidate pair.")]},
                )
            lead_ids = sorted((visible.left_id, visible.right_id))
            locked = {
                lead.pk: lead
                for lead in self._repository.scoped_leads(scope=scope)
                .select_for_update(of=("self",))
                .filter(pk__in=lead_ids)
                .order_by("pk")
            }
            if len(locked) != 2:
                raise NotFoundException(code="not_found")
            students = {
                student.pk: student
                for student in StudentProfile.objects.select_for_update(of=("self",))
                .filter(pk__in=[lead.student_id for lead in locked.values()])
                .order_by("pk")
            }
            if len(students) != 2:
                raise NotFoundException(code="not_found")
            for lead in locked.values():
                lead.student = students[lead.student_id]
            # All workflows lock leads and students before duplicate-review
            # rows. The stable order prevents scan/merge lock inversion.
            candidate = (
                LeadDuplicateCandidate.objects.select_for_update(of=("self",))
                .filter(pk=candidate_id, left_id=lead_ids[0], right_id=lead_ids[1])
                .first()
            )
            if candidate is None:
                raise NotFoundException(code="not_found")
            if candidate.status != LeadDuplicateCandidate.Status.PENDING:
                raise ConflictException(
                    _("This duplicate candidate was already reviewed."), code="duplicate_reviewed"
                )
            canonical = locked[int(data.canonical_lead_id)]
            duplicate = locked[candidate.right_id if canonical.pk == candidate.left_id else candidate.left_id]
            if any(lead.state == CRMLead.State.MERGED for lead in locked.values()):
                raise ConflictException(_("A lead in this pair was already merged."), code="lead_merged")
            if any(
                lead.student.status != StudentProfile.Status.LEAD or not lead.student.is_active
                for lead in locked.values()
            ):
                raise ConflictException(
                    _("Only active, unresolved student leads can be canonicalized."),
                    code="student_state_conflict",
                )
            previous_duplicate_state = duplicate.state
            now = timezone.now()
            candidate.status = LeadDuplicateCandidate.Status.MERGED
            candidate.reviewed_by = actor
            candidate.reviewed_by_principal_kind = actor_principal.kind
            candidate.reviewed_by_principal_id = actor_principal.principal_id
            candidate.reviewed_at = now
            candidate.rationale = data.rationale
            candidate.save(
                update_fields=(
                    "status",
                    "reviewed_by",
                    "reviewed_by_principal_kind",
                    "reviewed_by_principal_id",
                    "reviewed_at",
                    "rationale",
                )
            )
            merge = LeadMerge.objects.create(
                candidate=candidate,
                canonical=canonical,
                duplicate=duplicate,
                rationale=data.rationale,
                reviewed_by=actor,
                reviewed_by_principal_kind=actor_principal.kind,
                reviewed_by_principal_id=actor_principal.principal_id,
            )
            duplicate.state = CRMLead.State.MERGED
            duplicate.canonical_lead = canonical
            duplicate.loss_reason = ""
            duplicate.version += 1
            duplicate.save(update_fields=("state", "canonical_lead", "loss_reason", "version", "updated_at"))
            # Keep the role-native identity and every immutable CRM event for
            # audit/reconciliation, while preventing the duplicate identity
            # from continuing through an independent admissions workflow.
            duplicate.student.is_active = False
            duplicate.student.save(update_fields=("is_active", "updated_at"))
            self._audit(
                duplicate,
                actor=actor,
                actor_principal=actor_principal,
                action="update",
                resource_type="crm.LeadMerge",
                resource_id=merge.pk,
                before={"duplicate_id": duplicate.pk, "state": previous_duplicate_state},
                after={
                    "duplicate_id": duplicate.pk,
                    "canonical_id": canonical.pk,
                    "state": "merged",
                    "duplicate_student_deactivated": True,
                },
            )
            return merge

        return self._idempotent(
            operation_name=f"duplicate.merge:{candidate_id}",
            payload={"canonical": data.canonical_lead_id, "rationale": data.rationale},
            scope=scope,
            actor=actor,
            actor_principal=actor_principal,
            key=idempotency_key,
            result_type="merge",
            operation=operation,
        )

    def funnel(
        self,
        *,
        scope: CRMScope,
        date_from: date,
        date_to: date,
        branch_id: int | None,
        department_id: int | None,
        source_id: int | None,
        campaign_id: int | None,
    ) -> dict[str, Any]:
        if branch_id is not None and not scope.allows(branch_id=branch_id, department_id=department_id):
            raise ValidationException(
                _("Choose a scope you can access."),
                fields={"branch": [_("Choose a branch and department in your CRM scope.")]},
            )
        if department_id is not None and branch_id is None:
            raise ValidationException(
                _("Department requires branch."), fields={"department": [_("Select branch as well.")]}
            )
        tz = timezone.get_current_timezone()
        lower = timezone.make_aware(datetime.combine(date_from, time.min), tz)
        upper = timezone.make_aware(datetime.combine(date_to, time.max), tz)
        filters = LeadFilterDTO(
            branch_id=branch_id,
            department_id=department_id,
            source_id=source_id,
            campaign_id=campaign_id,
            created_from=lower,
            created_to=upper,
        )
        base_qs = self._repository.scoped_leads(scope=scope, filters=filters)
        totals = base_qs.aggregate(
            sample_size=Count(
                "id",
                filter=~Q(state=CRMLead.State.MERGED),
                distinct=True,
            ),
            open=Count("id", filter=Q(state=CRMLead.State.OPEN), distinct=True),
            won=Count("id", filter=Q(state=CRMLead.State.WON), distinct=True),
            lost=Count("id", filter=Q(state=CRMLead.State.LOST), distinct=True),
            excluded_merged=Count(
                "id",
                filter=Q(state=CRMLead.State.MERGED),
                distinct=True,
            ),
        )
        qs = base_qs.exclude(state=CRMLead.State.MERGED)
        stages = list(
            qs.values("stage_id", "stage__slug", "stage__name", "stage__position")
            .annotate(count=Count("id", distinct=True))
            .order_by("stage__position", "stage_id")
        )
        sample = int(totals["sample_size"] or 0)
        won = int(totals["won"] or 0)
        lost = int(totals["lost"] or 0)
        return {
            "generated_at": timezone.now().isoformat(),
            "window": {
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "timezone": str(tz),
                "basis": "lead_created_at",
                "inclusive": True,
            },
            "scope": {
                "authorization": {
                    "organization_wide": scope.organization_wide,
                    "branch_wide": sorted(scope.branch_wide_ids),
                    "departments": [
                        {"branch": scoped_branch, "department": scoped_department}
                        for scoped_branch, scoped_department in sorted(scope.department_scopes)
                    ],
                },
                "filters": {
                    "branch": branch_id,
                    "department": department_id,
                    "source": source_id,
                    "campaign": campaign_id,
                },
            },
            "sample_size": sample,
            "excluded_merged_count": int(totals["excluded_merged"] or 0),
            "states": {
                "open": int(totals["open"] or 0),
                "won": won,
                "lost": lost,
            },
            "conversion_fraction": (won / sample if sample else None),
            "loss_fraction": (lost / sample if sample else None),
            "stages": [
                {
                    "id": row["stage_id"],
                    "slug": row["stage__slug"],
                    "name": row["stage__name"],
                    "position": row["stage__position"],
                    "count": row["count"],
                    "fraction": (row["count"] / sample if sample else None),
                }
                for row in stages
            ],
            "definitions": {
                "sample_size": (
                    "Distinct non-merged CRM leads created inside the inclusive window and exact scope."
                ),
                "conversion_fraction": (
                    "Leads whose current state is won divided by sample_size; null when sample_size is zero."
                ),
                "loss_fraction": (
                    "Leads whose current state is lost divided by sample_size; null when sample_size is zero."
                ),
            },
        }

    def _department(self, branch_id: int, department_id: int | None):
        if department_id is None:
            return None
        from apps.org.models import Department

        department = Department.objects.filter(
            pk=department_id,
            branch_id=branch_id,
            is_active=True,
        ).first()
        if department is None:
            raise ValidationException(
                _("Choose an active department in the lead branch."),
                fields={"department": [_("Choose a department in the selected branch.")]},
            )
        return department

    def _attribution_targets(
        self,
        *,
        source_id: int | None,
        campaign_id: int | None,
        branch_id: int,
        department_id: int | None,
    ) -> tuple[LeadSource | None, AcquisitionCampaign | None]:
        if source_id is None:
            if campaign_id is not None:
                raise ValidationException(
                    _("Campaign requires source."), fields={"source": [_("Select a source as well.")]}
                )
            return None, None
        source = LeadSource.objects.filter(pk=source_id, is_active=True).first()
        if source is None:
            raise ValidationException(
                _("Choose an active lead source."), fields={"source": [_("Choose an active source.")]}
            )
        campaign = None
        if campaign_id is not None:
            campaign = AcquisitionCampaign.objects.filter(
                pk=campaign_id,
                source=source,
                is_active=True,
            ).first()
            if campaign is None or (
                campaign.branch_id is not None
                and (
                    campaign.branch_id != branch_id
                    or (campaign.department_id is not None and campaign.department_id != department_id)
                )
            ):
                raise ValidationException(
                    _("Choose an active campaign in this lead scope and source."),
                    fields={"campaign": [_("Choose a matching campaign.")]},
                )
        return source, campaign

    def _locked_lead(self, scope: CRMScope, lead_id: int) -> CRMLead:
        lead = self._repository.get_scoped_lead(scope=scope, pk=lead_id, lock=True)
        if lead is None:
            raise NotFoundException(code="not_found")
        return lead

    def _locked_candidate(self, scope: CRMScope, candidate_id: int) -> LeadDuplicateCandidate:
        candidate = self._repository.get_duplicate_candidate(scope=scope, pk=candidate_id, lock=True)
        if candidate is None:
            raise NotFoundException(code="not_found")
        return candidate

    def _audit(
        self,
        lead: CRMLead,
        *,
        actor,
        actor_principal: RolePrincipal,
        action: str,
        resource_type: str,
        resource_id: int,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        audit_log(
            actor=actor,
            actor_principal=actor_principal,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=before,
            after=after,
            scope=scoped_audit_scope(lead.branch_id, lead.department_id),
        )

    def _idempotent(
        self,
        *,
        operation_name: str,
        payload: Any,
        scope: CRMScope,
        actor,
        actor_principal: RolePrincipal,
        key: str,
        result_type: str,
        operation: Callable[[], T],
    ) -> tuple[T, bool]:
        normalized_key = _validate_idempotency_key(key)
        key_hash = stable_hash(normalized_key)
        fingerprint = _canonical_fingerprint(
            {"operation": operation_name, "payload": payload, "actor": asdict(actor_principal)}
        )
        # PostgreSQL transaction-scoped advisory lock serializes the check/write
        # before any domain side effect, so a concurrent retry cannot execute twice.
        lock_hash = stable_hash(
            f"{current_schema()}:{actor_principal.kind}:{actor_principal.principal_id}:{key_hash}"
        )
        lock_id = int(lock_hash[:15], 16)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])
        existing = CRMIdempotencyRecord.objects.filter(
            actor_principal_kind=actor_principal.kind,
            actor_principal_id=actor_principal.principal_id,
            key_hash=key_hash,
        ).first()
        if existing is not None:
            if existing.operation != operation_name or existing.request_fingerprint != fingerprint:
                raise ConflictException(
                    _("This idempotency key was already used for another request."),
                    code="idempotency_mismatch",
                )
            return self._resolve_result(existing, scope=scope), True
        result = operation()
        result_id = getattr(result, "pk", None)
        if not isinstance(result_id, int):
            raise RuntimeError("An idempotent CRM operation must return a saved model")
        CRMIdempotencyRecord.objects.create(
            actor=actor,
            actor_principal_kind=actor_principal.kind,
            actor_principal_id=actor_principal.principal_id,
            key_hash=key_hash,
            operation=operation_name,
            request_fingerprint=fingerprint,
            result_type=result_type,
            result_id=result_id,
        )
        return result, False

    def _resolve_result(self, record: CRMIdempotencyRecord, *, scope: CRMScope):
        """Resolve a replay only through the caller's *current* effective scope.

        An idempotency record proves that this role principal executed an earlier
        request. It is not a durable authorization grant: membership revocation
        or reassignment must make the stored result disappear immediately.
        """

        lead_qs = self._repository.scoped_leads(scope=scope)
        result: Model | None
        if record.result_type == "stage":
            result = PipelineStage.objects.filter(pk=record.result_id).first()
        elif record.result_type == "source":
            result = LeadSource.objects.filter(pk=record.result_id).first()
        elif record.result_type == "campaign":
            result = self._repository.campaigns(scope=scope).filter(pk=record.result_id).first()
        elif record.result_type in {"lead", "duplicate_scan"}:
            result = lead_qs.filter(pk=record.result_id).first()
        elif record.result_type == "stage_history":
            result = (
                LeadStageHistory.objects.select_related("lead", "from_stage", "to_stage")
                .filter(pk=record.result_id, lead__in=lead_qs)
                .first()
            )
        elif record.result_type == "touch":
            result = (
                LeadTouch.objects.select_related("lead").filter(pk=record.result_id, lead__in=lead_qs).first()
            )
        elif record.result_type == "follow_up":
            result = self._repository.scoped_follow_ups(scope=scope).filter(pk=record.result_id).first()
        elif record.result_type == "attribution":
            result = (
                LeadAttribution.objects.select_related("lead", "source", "campaign", "actor")
                .filter(pk=record.result_id, lead__in=lead_qs)
                .first()
            )
        elif record.result_type == "duplicate_candidate":
            result = self._repository.get_duplicate_candidate(
                scope=scope,
                pk=record.result_id,
            )
        elif record.result_type == "merge":
            result = (
                LeadMerge.objects.select_related("candidate", "canonical", "duplicate")
                .filter(
                    pk=record.result_id,
                    candidate__in=self._repository.duplicate_candidates(scope=scope),
                )
                .first()
            )
        else:
            raise ConflictException(
                _("The earlier operation result is no longer available."),
                code="idempotency_result_missing",
            )
        if result is None:
            # A result that moved outside this principal's current boundary must
            # look exactly like any other out-of-scope identifier.
            raise NotFoundException(code="not_found")
        return result
