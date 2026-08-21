"""Teacher-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.org.tests.factories import BranchFactory
from apps.teachers.models import TeacherProfile, TeacherType
from apps.users.tests.factories import UserFactory


class TeacherProfileFactory(factory.django.DjangoModelFactory[TeacherProfile]):
    class Meta:
        model = TeacherProfile

    user = factory.SubFactory(UserFactory)
    username = factory.LazyAttribute(lambda o: o.user.username)
    password = factory.LazyAttribute(lambda o: o.user.password)
    # Identity is owned by the teacher model now; mirror it off the user (as create_teacher
    # does) so a test that sets user.first_name / user.birthdate flows through.
    first_name = factory.LazyAttribute(lambda o: o.user.first_name)
    last_name = factory.LazyAttribute(lambda o: o.user.last_name)
    middle_name = factory.LazyAttribute(lambda o: o.user.middle_name)
    phone = factory.LazyAttribute(lambda o: o.user.phone or "")
    email = factory.LazyAttribute(lambda o: o.user.email or "")
    birthdate = factory.LazyAttribute(lambda o: o.user.birthdate)
    gender = factory.LazyAttribute(lambda o: o.user.gender)
    branch = factory.SubFactory(BranchFactory)


class TeacherTypeFactory(factory.django.DjangoModelFactory[TeacherType]):
    class Meta:
        model = TeacherType

    name = factory.Sequence(lambda n: f"Teacher Type {n}")
    slug = factory.Sequence(lambda n: f"teacher-type-{n}")
    is_active = True
    is_system = False
    is_default = False
    sort_order = 100
