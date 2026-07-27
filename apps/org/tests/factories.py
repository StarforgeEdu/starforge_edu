"""Org-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

from datetime import time

import factory

from apps.org.models import Branch, BranchWorkingHours, Department, Room


class BranchFactory(factory.django.DjangoModelFactory[Branch]):
    class Meta:
        model = Branch
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"Branch {n}")
    slug = factory.Sequence(lambda n: f"branch-{n}")


class RoomFactory(factory.django.DjangoModelFactory[Room]):
    class Meta:
        model = Room
        django_get_or_create = ("branch", "name")

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Room {n}")
    capacity = 20


class BranchWorkingHoursFactory(factory.django.DjangoModelFactory[BranchWorkingHours]):
    class Meta:
        model = BranchWorkingHours
        django_get_or_create = ("branch", "weekday")

    branch = factory.SubFactory(BranchFactory)
    weekday = factory.Sequence(lambda n: n % 7)
    opens_at = time(8, 0)
    closes_at = time(18, 0)


class DepartmentFactory(factory.django.DjangoModelFactory[Department]):
    class Meta:
        model = Department
        django_get_or_create = ("branch", "slug")

    branch = factory.SubFactory(BranchFactory)
    name = factory.Sequence(lambda n: f"Department {n}")
    slug = factory.Sequence(lambda n: f"dept-{n}")
