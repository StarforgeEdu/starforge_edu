from django.apps import AppConfig


class AuthAppConfig(AppConfig):
    """label='auth_app' avoids collision with django.contrib.auth's 'auth' label."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.auth"
    label = "auth_app"
    verbose_name = "Auth (custom session login + OTP reset)"

    def ready(self) -> None:
        from . import receivers  # noqa: F401

        self._register_container()

    @staticmethod
    def _register_container() -> None:
        """Bind this app's ports to their implementations (the layered architecture's
        wiring). Each app registers its own bindings in ready() — modular + explicit."""
        from apps.auth.interfaces.auth_service import IAuthService
        from apps.auth.interfaces.repositories import ISessionRepository, IUserRepository
        from apps.auth.repositories.session_repository import SessionRepository
        from apps.auth.repositories.user_repository import UserRepository
        from apps.auth.services.v1.auth_service import AuthService
        from core.container import container

        container.register(IUserRepository, UserRepository)
        container.register(ISessionRepository, SessionRepository)
        container.register(IAuthService, AuthService)
