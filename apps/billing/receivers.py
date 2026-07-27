"""Billing receivers (D3-E-3).

Auto-create a trialing Subscription when a tenancy.Center is provisioned. The
Center post_save fires in the PUBLIC schema (Center lives only there), so this
receiver runs in the right context to write a public-schema Subscription row.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.tenancy.models import Center

logger = logging.getLogger("starforge.billing")


@receiver(post_save, sender=Center, dispatch_uid="billing.center_auto_subscribe")
def auto_create_subscription(sender, instance: Center, created: bool, **kwargs) -> None:
    """On Center creation, open a trialing subscription (idempotent).

    Guarded so a missing Plan catalog (data migration not yet applied in a bare
    test DB) never aborts Center provisioning — provisioning is load-bearing for
    every other lane's tests.
    """
    if not created:
        return
    from apps.billing.services import create_trial_subscription

    try:
        create_trial_subscription(center=instance)
    except Exception:  # never block tenant provisioning on billing
        logger.exception("auto subscription creation failed", extra={"center_id": instance.pk})
