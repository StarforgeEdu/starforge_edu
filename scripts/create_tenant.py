"""Provision a tenant: `python scripts/create_tenant.py <slug> <hostname> "<Center Name>"`.

Run from the repo root with DJANGO_SETTINGS_MODULE set (manage.py defaults
it to config.settings.development).
"""

from __future__ import annotations

import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from apps.tenancy.services import provision_center  # noqa: E402
from core.exceptions import StarforgeError  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print("usage: create_tenant.py <slug> <hostname> <Center Name>")
        sys.exit(1)
    slug, hostname, name = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        center = provision_center(name=name, slug=slug, primary_domain=hostname)
    except StarforgeError as exc:
        print(f"error [{exc.code}]: {exc.detail}")
        sys.exit(1)
    print(f"created Center id={center.pk} schema={center.schema_name} domain={hostname}")


if __name__ == "__main__":
    main()
