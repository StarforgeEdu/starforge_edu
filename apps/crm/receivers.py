from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.crm.identity import lead_identity_fingerprints
from apps.crm.models import CRMLead
from apps.students.models import StudentProfile
from core.exceptions import ConflictException


@receiver(post_save, sender=StudentProfile, dispatch_uid="crm.sync_identity_fingerprints")
def sync_lead_identity_fingerprints(sender, instance: StudentProfile, **kwargs) -> None:
    """Keep indexed duplicate keys synchronized without copying identity values."""

    CRMLead.objects.filter(student_id=instance.pk).update(**lead_identity_fingerprints(instance))


@receiver(pre_save, sender=StudentProfile, dispatch_uid="crm.guard_open_lead_transition")
def guard_open_lead_transition(sender, instance: StudentProfile, **kwargs) -> None:
    """Prevent bypassing CRM conversion/loss evidence through the student endpoint.

    CRM transition writes the lead state first in the same transaction, so its
    lead→application enrolment move is allowed. Any direct move while the CRM
    workflow has not recorded conversion is rejected transactionally. Lost
    leads must be explicitly reopened; merged duplicates must use the canonical
    identity.
    """

    if instance.pk is None:
        return
    prior_status = StudentProfile.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    if (
        prior_status == StudentProfile.Status.LEAD
        and instance.status != StudentProfile.Status.LEAD
        and CRMLead.objects.filter(student_id=instance.pk).exclude(state=CRMLead.State.WON).exists()
    ):
        raise ConflictException(
            "Move this lead through the CRM pipeline so conversion evidence is retained.",
            code="crm_transition_required",
        )
