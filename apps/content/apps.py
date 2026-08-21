from django.apps import AppConfig


class ContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.content"
    label = "content"
    verbose_name = "Lesson content"

    def ready(self) -> None:
        from apps.content.interfaces.repositories import (
            IContentLessonRepository,
            IContentLibraryRepository,
            ICourseRepository,
            IFolderRepository,
            ILessonFileRepository,
            ILibraryMaterialRepository,
            IModuleRepository,
        )
        from apps.content.interfaces.services import (
            IContentLessonService,
            IContentLibraryService,
            ICourseService,
            IFolderService,
            ILessonFileService,
            ILibraryMaterialService,
            IModuleService,
        )
        from apps.content.repositories.content_repository import (
            ContentLessonRepository,
            ContentLibraryRepository,
            CourseRepository,
            FolderRepository,
            LessonFileRepository,
            LibraryMaterialRepository,
            ModuleRepository,
        )
        from apps.content.services.v1.content_service import (
            ContentLessonService,
            ContentLibraryService,
            CourseService,
            FolderService,
            LessonFileService,
            LibraryMaterialService,
            ModuleService,
        )
        from core.container import container

        container.register(IContentLibraryRepository, ContentLibraryRepository)
        container.register(ICourseRepository, CourseRepository)
        container.register(IModuleRepository, ModuleRepository)
        container.register(IContentLessonRepository, ContentLessonRepository)
        container.register(IFolderRepository, FolderRepository)
        container.register(ILessonFileRepository, LessonFileRepository)
        container.register(ILibraryMaterialRepository, LibraryMaterialRepository)
        container.register(IContentLibraryService, ContentLibraryService)
        container.register(ICourseService, CourseService)
        container.register(IModuleService, ModuleService)
        container.register(IContentLessonService, ContentLessonService)
        container.register(IFolderService, FolderService)
        container.register(ILessonFileService, LessonFileService)
        container.register(ILibraryMaterialService, LibraryMaterialService)

        # Register storage lifecycle receivers after the models and task plumbing
        # are ready.  Importing for side effects is intentional.
        from apps.content import receivers  # noqa: F401
