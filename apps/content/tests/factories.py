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
    s3_key = factory.Sequence(lambda n: f"tenant_a/content/{n}/file.pdf")
    content_type = "application/pdf"
    size_bytes = 1000
    status = LessonFile.Status.CLEAN
    # A factory file represents a real, published file — dual-approved by default
    # so visibility fixtures stay green (F4-5). Real uploads default to unapproved.
    is_approved_teacher = True
    is_approved_manager = True
