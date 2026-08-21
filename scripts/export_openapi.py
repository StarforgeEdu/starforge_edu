"""Export and validate the complete tenant and public OpenAPI documents."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import django
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-output", type=Path, default=Path("openapi.yaml"))
    parser.add_argument("--public-output", type=Path, default=Path("openapi-public.yaml"))
    parser.add_argument("--validate", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()

    from core.openapi import build_schema

    documents = (
        (args.tenant_output, build_schema("config.urls")),
        (args.public_output, build_schema("config.urls_public")),
    )
    if args.validate:
        from drf_spectacular.validation import validate_schema

        for _path, schema in documents:
            validate_schema(schema)
            operation_ids = [
                operation["operationId"]
                for path_item in schema["paths"].values()
                for method, operation in path_item.items()
                if method in {"get", "post", "put", "patch", "delete"}
            ]
            if len(operation_ids) != len(set(operation_ids)):
                raise ValueError("OpenAPI operationId values must be globally unique")

    for path, schema in documents:
        path.write_text(
            yaml.safe_dump(schema, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
            newline="\n",
        )
        print(f"Wrote {path} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
