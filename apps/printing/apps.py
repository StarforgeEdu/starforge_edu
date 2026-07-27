from django.apps import AppConfig


class PrintingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.printing"
    label = "printing"
    verbose_name = "Printing (server side)"

    def ready(self) -> None:
        from apps.printing.interfaces.repositories import (
            IBranchAgentRepository,
            IPrinterRepository,
            IPrintJobRepository,
        )
        from apps.printing.interfaces.services import (
            IBranchAgentService,
            IPrinterService,
            IPrintJobService,
        )
        from apps.printing.repositories.printing_repository import (
            BranchAgentRepository,
            PrinterRepository,
            PrintJobRepository,
        )
        from apps.printing.services.v1.printing_service import (
            BranchAgentService,
            PrinterService,
            PrintJobService,
        )
        from core.container import container

        container.register(IPrintJobRepository, PrintJobRepository)
        container.register(IPrinterRepository, PrinterRepository)
        container.register(IBranchAgentRepository, BranchAgentRepository)
        container.register(IPrintJobService, PrintJobService)
        container.register(IPrinterService, PrinterService)
        container.register(IBranchAgentService, BranchAgentService)
