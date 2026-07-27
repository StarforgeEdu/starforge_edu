"""DoNotContactService — the SMS consent (do-not-contact) list, keyed by E.164 phone."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.campaigns.interfaces.repositories import IDoNotContactRepository
from apps.campaigns.interfaces.services import IDoNotContactService
from apps.campaigns.models import DoNotContact
from core.exceptions import ConflictException, ValidationException


class DoNotContactService(IDoNotContactService):
    def __init__(self, entries: IDoNotContactRepository) -> None:
        self._entries = entries

    def list(self) -> QuerySet[DoNotContact]:
        return self._entries.get_queryset()

    def get(self, pk: int) -> DoNotContact | None:
        return self._entries.get_by_id(pk)

    def create(self, *, phone: str, reason: str, actor) -> DoNotContact:
        from core.validators import normalize_phone

        phone = (phone or "").strip()
        if not phone:
            raise ValidationException(
                _("A phone number is required."),
                code="validation_error",
                fields={"phone": ["This field is required."]},
            )
        # Canonicalize to E.164 (the single chokepoint User.phone also uses) so a
        # differently-formatted opt-out still byte-matches the stored phone; junk -> 400
        # invalid_phone.
        phone = normalize_phone(phone)
        try:
            with transaction.atomic():
                return DoNotContact.objects.create(phone=phone, reason=reason, created_by=actor)
        except IntegrityError:
            # the unique(phone) constraint — already opted out is a clean 409, not a 500
            raise ConflictException(
                _("That phone is already on the do-not-contact list."), code="already_opted_out"
            ) from None

    def delete(self, entry: DoNotContact) -> None:
        entry.delete()
