import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.content.models import Folder
from apps.content.tests.factories import ContentLibraryFactory, FolderFactory

pytestmark = pytest.mark.django_db


def test_root_folder_name_is_unique_within_library(tenant_a):
    with schema_context(tenant_a.schema_name):
        library = ContentLibraryFactory()
        FolderFactory(library=library, parent=None, name="Exams")

        with pytest.raises(IntegrityError), transaction.atomic():
            Folder.objects.create(library=library, parent=None, name="Exams")


def test_same_folder_name_is_allowed_under_different_parents(tenant_a):
    with schema_context(tenant_a.schema_name):
        library = ContentLibraryFactory()
        first_parent = FolderFactory(library=library, name="First")
        second_parent = FolderFactory(library=library, name="Second")

        Folder.objects.create(library=library, parent=first_parent, name="Exams")
        Folder.objects.create(library=library, parent=second_parent, name="Exams")
