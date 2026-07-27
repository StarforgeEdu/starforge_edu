import pytest

from apps.tenancy.services import provision_center
from core.exceptions import ValidationException

pytestmark = pytest.mark.django_db


def test_reserved_slug_rejected(db):
    with pytest.raises(ValidationException) as exc:
        provision_center(name="X", slug="admin", primary_domain="admin.localhost")
    assert exc.value.code == "slug_reserved"


def test_invalid_slug_rejected(db):
    with pytest.raises(ValidationException) as exc:
        provision_center(name="X", slug="Bad-Slug", primary_domain="bad.localhost")
    assert exc.value.code == "slug_invalid"


def test_duplicate_slug_rejected(tenant_a):
    with pytest.raises(ValidationException) as exc:
        provision_center(name="Dup", slug=tenant_a.schema_name, primary_domain="dup.localhost")
    assert exc.value.code == "slug_taken"


def test_provisioning_creates_center_settings(tenant_a):
    from django_tenants.utils import schema_context

    from apps.org.models import CenterSettings

    with schema_context(tenant_a.schema_name):
        assert CenterSettings.objects.filter(pk=1).exists()
