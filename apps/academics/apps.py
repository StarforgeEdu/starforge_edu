from django.apps import AppConfig


class AcademicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.academics"
    label = "academics"
    verbose_name = "Academics"

    def ready(self) -> None:
        from apps.academics.interfaces.repositories import (
            IExamRepository,
            IExamTypeRepository,
            IGradeRepository,
            ISubjectRepository,
            ITranscriptRepository,
        )
        from apps.academics.interfaces.services import (
            IExamService,
            IExamTypeService,
            IGradeService,
            ISubjectService,
            ITranscriptService,
        )
        from apps.academics.repositories.academics_repository import (
            ExamRepository,
            ExamTypeRepository,
            GradeRepository,
            SubjectRepository,
            TranscriptRepository,
        )
        from apps.academics.services.v1.academics_service import (
            ExamService,
            ExamTypeService,
            GradeService,
            SubjectService,
            TranscriptService,
        )
        from core.container import container

        container.register(ISubjectRepository, SubjectRepository)
        container.register(IExamTypeRepository, ExamTypeRepository)
        container.register(IExamRepository, ExamRepository)
        container.register(IGradeRepository, GradeRepository)
        container.register(ITranscriptRepository, TranscriptRepository)
        container.register(ISubjectService, SubjectService)
        container.register(IExamTypeService, ExamTypeService)
        container.register(IExamService, ExamService)
        container.register(IGradeService, GradeService)
        container.register(ITranscriptService, TranscriptService)
