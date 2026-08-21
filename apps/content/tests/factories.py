"""Content-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.academics.tests.factories import SubjectFactory
from apps.content.models import (
    ContentLesson,
    ContentLibrary,
    Course,
    Folder,
    LessonFile,
    Module,
)
from core.utils import current_schema


class ContentLibraryFactory(factory.django.DjangoModelFactory[ContentLibrary]):
    class Meta:
        model = ContentLibrary

    name = factory.Sequence(lambda n: f"Library {n}")
    visibility = ContentLibrary.Visibility.TENANT


class CourseFactory(factory.django.DjangoModelFactory[Course]):
    class Meta:
        model = Course

    library = factory.SubFactory(ContentLibraryFactory)
    subject = factory.SubFactory(SubjectFactory)
    title = factory.Sequence(lambda n: f"Course {n}")


class ModuleFactory(factory.django.DjangoModelFactory[Module]):
    class Meta:
        model = Module

    course = factory.SubFactory(CourseFactory)
    title = factory.Sequence(lambda n: f"Module {n}")
    order = factory.Sequence(lambda n: n)


class ContentLessonFactory(factory.django.DjangoModelFactory[ContentLesson]):
    class Meta:
        model = ContentLesson

    module = factory.SubFactory(ModuleFactory)
    title = factory.Sequence(lambda n: f"Content Lesson {n}")


class FolderFactory(factory.django.DjangoModelFactory[Folder]):
    class Meta:
        model = Folder

    library = factory.SubFactory(ContentLibraryFactory)
    name = factory.Sequence(lambda n: f"Folder {n}")


class LessonFileFactory(factory.django.DjangoModelFactory[LessonFile]):
    class Meta:
        model = LessonFile

    folder = factory.SubFactory(FolderFactory)
    title = factory.Sequence(lambda n: f"File {n}")
    # The final storage path is record-bound, so replace this collision-free
    # creation placeholder with the row's canonical path after INSERT.
    s3_key = factory.Sequence(lambda n: f"tenant_a/content/factory-{n}/file.pdf")
    content_type = "application/pdf"
    size_bytes = 1000
    status = LessonFile.Status.CLEAN
    # A factory file represents a real, published file — dual-approved by default
    # so visibility fixtures stay green (F4-5). Real uploads default to unapproved.
    is_approved_teacher = True
    is_approved_manager = True

    @factory.post_generation
    def canonical_storage_key(self, create, extracted, **_kwargs):
        if not create or extracted is False:
            return
        self.s3_key = f"{current_schema()}/content/{self.pk}/file.pdf"
        self.save(update_fields=["s3_key"])
