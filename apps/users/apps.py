from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    label = "users"
    verbose_name = "Users"

    def ready(self) -> None:
        from apps.users.interfaces.repositories import IDeviceRepository, IUserRepository
        from apps.users.interfaces.services import IUserService
        from apps.users.repositories.user_repository import DeviceRepository, UserRepository
        from apps.users.services.v1.user_service import UserService
        from core.container import container

        from . import receivers  # noqa: F401

        container.register(IUserRepository, UserRepository)
        container.register(IDeviceRepository, DeviceRepository)
        container.register(IUserService, UserService)
