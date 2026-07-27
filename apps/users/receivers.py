"""Invalidate live tokens when a user's roles change (D1-LC-7, TD-1).

Granting or revoking a RoleMembership bumps the user's ``token_version`` so the
next request with an old access token is rejected 401 ``token_stale``. Bumping
is idempotent by intent — a stale token is the goal even on double-fire.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.users.models import RoleMembership
from apps.users.services import bump_token_version


@receiver(post_save, sender=RoleMembership, dispatch_uid="users.rolemembership_saved_bump_tv")
def on_role_membership_saved(sender, instance: RoleMembership, **kwargs) -> None:
    bump_token_version(instance.user_id)


@receiver(post_delete, sender=RoleMembership, dispatch_uid="users.rolemembership_deleted_bump_tv")
def on_role_membership_deleted(sender, instance: RoleMembership, **kwargs) -> None:
    bump_token_version(instance.user_id)
