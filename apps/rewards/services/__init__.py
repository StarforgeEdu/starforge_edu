"""Reward services (F17-1)."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.rewards.models import RewardGrant, RewardType
from core.exceptions import PermissionException, UnprocessableEntity, ValidationException


def _recipient_branch(recipient):
    membership = recipient.role_memberships.filter(revoked_at__isnull=True, branch__isnull=False).first()
    return membership.branch if membership else None


@transaction.atomic
def create_reward_type(
    *,
    creator,
    name: str,
    is_cash: bool = False,
    default_amount_uzs=None,
    description: str = "",
    is_active: bool = True,
) -> RewardType:
    return RewardType.objects.create(
        name=name,
        is_cash=is_cash,
        default_amount_uzs=default_amount_uzs,
        description=description,
        is_active=is_active,
        created_by=creator,
    )


@transaction.atomic
def grant_reward(
    *, reward_type: RewardType, recipient, granted_by=None, amount_uzs=None, reason: str = ""
) -> RewardGrant:
    """Grant a reward to a staff member. A CASH reward records the grant AND raises a
    `reward`-kind A-1 ApprovalRequest for the money (approve → cashier disburse →
    immutable ledger) — money is never paid out without sign-off. A NON-cash reward
    is simply recorded."""
    if not reward_type.is_active:
        raise UnprocessableEntity(_("This reward type is no longer active."), code="reward_type_inactive")
    # Maker-checker: you may not reward yourself (a manager rewards their staff, not
    # themselves) — a self-reward must be granted by another manager.
    if granted_by is not None and recipient.pk == granted_by.pk:
        raise PermissionException(_("You cannot grant a reward to yourself."), code="self_grant")

    if not reward_type.is_cash:
        return RewardGrant.objects.create(
            reward_type=reward_type,
            recipient=recipient,
            amount_uzs=None,
            reason=reason,
            granted_by=granted_by,
        )

    amount = amount_uzs if amount_uzs is not None else reward_type.default_amount_uzs
    if amount is None or amount <= Decimal("0"):
        raise ValidationException(_("A cash reward needs a positive amount."), code="amount_required")
    grant = RewardGrant.objects.create(
        reward_type=reward_type, recipient=recipient, amount_uzs=amount, reason=reason, granted_by=granted_by
    )
    from apps.approvals.services import create_request

    payee = recipient.get_full_name() or recipient.username
    request = create_request(
        kind="reward",
        title=f"{reward_type.name} — {payee}"[:200],  # ApprovalRequest.title is max 200
        requested_by=granted_by,
        amount_uzs=amount,
        description=reason,
        branch=_recipient_branch(recipient),  # attribute the payout to the recipient's branch
        payload={
            "reward_grant_id": grant.pk,
            "recipient_id": recipient.pk,
            "reward_type": reward_type.name,
            # The ledger payee is the recipient, NOT the requester (the granter).
            "party_label": payee,
        },
    )
    grant.approval_request = request
    grant.save(update_fields=["approval_request"])
    return grant
