"""Student-domain factories (TESTING.md §4). Call inside schema_context(tenant).

Creates rows directly for test fixtures; the enrollment state machine and
generated IDs are exercised via the service in the API tests."""

from __future__ import annotations

import factory

from apps.org.tests.factories import BranchFactory
from apps.students.models import EnrollmentReason, StudentProfile
from apps.users.tests.factories import UserFactory


class StudentProfileFactory(factory.django.DjangoModelFactory[StudentProfile]):
    class Meta:
        model = StudentProfile

    user = factory.SubFactory(UserFactory)
    username = factory.LazyAttribute(lambda o: o.user.username)
    password = factory.LazyAttribute(lambda o: o.user.password)
    # Identity is owned by the student model now; mirror it off the user (as create_student
    # does) so a test that sets user.first_name / user.birthdate flows through.
    first_name = factory.LazyAttribute(lambda o: o.user.first_name)
    last_name = factory.LazyAttribute(lambda o: o.user.last_name)
    middle_name = factory.LazyAttribute(lambda o: o.user.middle_name)
    phone = factory.LazyAttribute(lambda o: o.user.phone or "")
    email = factory.LazyAttribute(lambda o: o.user.email or "")
    birthdate = factory.LazyAttribute(lambda o: o.user.birthdate)
    gender = factory.LazyAttribute(lambda o: o.user.gender)
    branch = factory.SubFactory(BranchFactory)
    student_id = factory.Sequence(lambda n: f"STU-{n:05d}")
    status = StudentProfile.Status.ACTIVE


class EnrollmentReasonFactory(factory.django.DjangoModelFactory[EnrollmentReason]):
    class Meta:
        model = EnrollmentReason

    name = factory.Sequence(lambda n: f"Reason {n}")
    slug = factory.Sequence(lambda n: f"reason-{n}")
