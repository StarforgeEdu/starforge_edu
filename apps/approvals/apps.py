from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.approvals"
    label = "approvals"
    verbose_name = "Approvals & Ledger"

    def ready(self) -> None:
        from apps.approvals.interfaces.repositories import (
            IApprovalRequestRepository,
            ILedgerEntryRepository,
        )
        from apps.approvals.interfaces.services import IApprovalService, ILedgerService
        from apps.approvals.repositories.approval_repository import (
            ApprovalRequestRepository,
            LedgerEntryRepository,
        )
        from apps.approvals.services.v1.approval_service import ApprovalService, LedgerService
        from core.container import container

        container.register(IApprovalRequestRepository, ApprovalRequestRepository)
        container.register(ILedgerEntryRepository, LedgerEntryRepository)
        container.register(IApprovalService, ApprovalService)
        container.register(ILedgerService, LedgerService)
