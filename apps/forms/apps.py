from django.apps import AppConfig


class FormsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.forms"
    label = "forms_app"  # avoid clashing with django.forms in app-label space
    verbose_name = "Forms & surveys"

    def ready(self) -> None:
        from apps.forms.interfaces.repositories import IFormRepository
        from apps.forms.interfaces.services import IFormService
        from apps.forms.repositories.form_repository import FormRepository
        from apps.forms.services.v1.form_service import FormService
        from core.container import container

        container.register(IFormRepository, FormRepository)
        container.register(IFormService, FormService)
