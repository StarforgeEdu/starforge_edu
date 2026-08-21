"""User-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.org.tests.factories import BranchFactory
from apps.users.models import RoleMembership, User


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)

    username = factory.Sequence(lambda n: f"user{n:05d}")
    # E.164, must pass core.validators.normalize_phone; never Faker (no uz_UZ).
    phone = factory.Sequence(lambda n: f"+99890{n:07d}")
    first_name = factory.Faker("first_name", locale="ru_RU")
    last_name = factory.Faker("last_name", locale="ru_RU")


class RoleMembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RoleMembership

    user = factory.SubFactory(UserFactory)
    branch = factory.SubFactory(BranchFactory)
    role = "teacher"
