#!/usr/bin/env python3
"""Render and verify the exact production MinIO IAM contract.

This helper deliberately uses only the Python standard library.  Production
storage operator scripts run it on the host to keep policy generation and
policy verification identical without placing MinIO root credentials in an
application container.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MEDIA_POLICY_NAME = "starforge-media-runtime-v1"
STATIC_POLICY_NAME = "starforge-static-writer-v1"
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")


def _validate_bucket(name: str) -> str:
    if not BUCKET_PATTERN.fullmatch(name):
        raise SystemExit(f"Invalid storage bucket name: {name!r}")
    return name


def _parse_origins(name: str, raw_value: str) -> list[str]:
    origins = [value.strip() for value in raw_value.split(",") if value.strip()]
    if not origins:
        raise SystemExit(f"{name} must contain at least one exact HTTPS origin")
    normalized: set[str] = set()
    for origin in origins:
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise SystemExit(f"Invalid {name} origin: {origin}") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
            or "*" in origin
            or origin != origin.strip()
        ):
            raise SystemExit(f"Invalid {name} origin: {origin}")
        canonical = f"https://{parsed.hostname.casefold()}"
        if canonical in normalized:
            raise SystemExit(f"Duplicate {name} origin: {origin}")
        normalized.add(canonical)
    return origins


def _validate_endpoint(name: str, value: str) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SystemExit(f"{name} must be one credential-free HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise SystemExit(f"{name} must be one credential-free HTTPS origin")


def _bucket_actions(bucket: str) -> dict[str, Any]:
    return {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
        ],
        "Resource": [f"arn:aws:s3:::{bucket}"],
    }


def _object_actions(bucket: str) -> dict[str, Any]:
    return {
        "Effect": "Allow",
        "Action": [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:ListMultipartUploadParts",
            "s3:PutObject",
        ],
        "Resource": [f"arn:aws:s3:::{bucket}/*"],
    }


def _cross_bucket_deny(bucket: str) -> dict[str, Any]:
    # The static bucket has a public GetObject resource policy.  An explicit
    # identity-policy deny ensures that the authenticated media runtime cannot
    # use that public grant as authenticated cross-bucket authority.
    return {
        "Effect": "Deny",
        "Action": ["s3:*"],
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
    }


def _service_policy(own_bucket: str, denied_bucket: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            _bucket_actions(own_bucket),
            _object_actions(own_bucket),
            _cross_bucket_deny(denied_bucket),
        ],
    }


def _public_static_policy(static_bucket: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{static_bucket}/*"],
            }
        ],
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def render(args: argparse.Namespace) -> None:
    destination = Path(args.destination)
    if not destination.is_dir() or destination.is_symlink():
        raise SystemExit("Policy destination must be an existing non-symlink directory")
    media_bucket = _validate_bucket(args.media_bucket)
    static_bucket = _validate_bucket(args.static_bucket)
    if media_bucket == static_bucket:
        raise SystemExit("Media and static buckets must be distinct")
    app_origins = _parse_origins("STORAGE_CORS_ALLOWED_ORIGINS", args.app_origins)
    minio_origins = _parse_origins("MINIO_API_CORS_ALLOW_ORIGIN", args.minio_origins)
    if set(app_origins) != set(minio_origins):
        raise SystemExit("Application and MinIO storage CORS origins must match exactly")
    _validate_endpoint("AWS_S3_PUBLIC_ENDPOINT_URL", args.media_endpoint)
    _validate_endpoint("AWS_STATIC_PUBLIC_ENDPOINT_URL", args.static_endpoint)
    if urlsplit(args.media_endpoint).netloc.casefold() == urlsplit(args.static_endpoint).netloc.casefold():
        raise SystemExit("Media and static public origins must be distinct")

    _write_json(
        destination / "media-runtime-policy.json",
        _service_policy(media_bucket, static_bucket),
    )
    _write_json(
        destination / "static-writer-policy.json",
        _service_policy(static_bucket, media_bucket),
    )
    _write_json(destination / "public-static-policy.json", _public_static_policy(static_bucket))


def _read_json_lines(path: Path, *, exactly_one: bool = False) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"Missing IAM verification artifact: {path.name}")
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Malformed IAM artifact {path.name}:{line_number}") from exc
        if not isinstance(value, dict) or value.get("status") != "success":
            raise SystemExit(f"IAM command failed in {path.name}:{line_number}")
        values.append(value)
    if exactly_one and len(values) != 1:
        raise SystemExit(f"Expected exactly one IAM result in {path.name}")
    return values


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_canonical(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return value


def _verify_policy(evidence: Path, expected: Path, artifact: str, policy_name: str) -> None:
    payload = _read_json_lines(evidence / artifact, exactly_one=True)[0]
    policy_info = payload.get("policyInfo")
    if not isinstance(policy_info, dict) or policy_info.get("PolicyName") != policy_name:
        raise SystemExit(f"MinIO policy name differs from the reviewed contract: {policy_name}")
    actual_policy = policy_info.get("Policy")
    expected_policy = json.loads(expected.read_text(encoding="utf-8"))
    if _canonical(actual_policy) != _canonical(expected_policy):
        raise SystemExit(f"MinIO policy differs from the reviewed contract: {policy_name}")


def verify(args: argparse.Namespace) -> None:
    expected = Path(args.expected)
    evidence = Path(args.evidence)
    for directory, label in ((expected, "Expected policy"), (evidence, "IAM evidence")):
        if not directory.is_dir() or directory.is_symlink():
            raise SystemExit(f"{label} directory is unavailable")
    if args.media_user == args.static_user:
        raise SystemExit("Media and static service accounts must be distinct")

    _verify_policy(
        evidence,
        expected / "media-runtime-policy.json",
        "media-policy-info.json",
        MEDIA_POLICY_NAME,
    )
    _verify_policy(
        evidence,
        expected / "static-writer-policy.json",
        "static-policy-info.json",
        STATIC_POLICY_NAME,
    )

    expected_users = {
        args.media_user: MEDIA_POLICY_NAME,
        args.static_user: STATIC_POLICY_NAME,
    }
    for artifact, access_key in (
        ("media-user.json", args.media_user),
        ("static-user.json", args.static_user),
    ):
        payload = _read_json_lines(evidence / artifact, exactly_one=True)[0]
        if (
            payload.get("accessKey") != access_key
            or payload.get("userStatus") != "enabled"
            or payload.get("policyName") != expected_users[access_key]
        ):
            raise SystemExit(f"Service-account state is not exact for {access_key}")

    mapping_payload = _read_json_lines(evidence / "iam-mappings.json", exactly_one=True)[0]
    result = mapping_payload.get("result")
    mappings = result.get("userMappings", []) if isinstance(result, dict) else []
    actual_mappings: dict[str, list[str]] = {}
    if not isinstance(mappings, list):
        raise SystemExit("MinIO returned malformed service-account mappings")
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise SystemExit("MinIO returned malformed service-account mapping")
        user = mapping.get("user")
        policies = mapping.get("policies")
        if isinstance(user, str) and user in expected_users and isinstance(policies, list):
            if user in actual_mappings:
                raise SystemExit("MinIO returned duplicate service-account mappings")
            actual_mappings[user] = policies
    if {user: sorted(policies) for user, policies in actual_mappings.items()} != {
        user: [policy] for user, policy in expected_users.items()
    }:
        raise SystemExit("Service accounts have missing or additional direct policies")

    for group in _read_json_lines(evidence / "groups.jsonl"):
        members = group.get("members", [])
        if not isinstance(members, list):
            raise SystemExit("MinIO returned malformed group membership")
        if set(expected_users).intersection(members):
            raise SystemExit("Storage service accounts must not inherit policy through a group")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("destination")
    render_parser.add_argument("media_bucket")
    render_parser.add_argument("static_bucket")
    render_parser.add_argument("app_origins")
    render_parser.add_argument("minio_origins")
    render_parser.add_argument("media_endpoint")
    render_parser.add_argument("static_endpoint")
    render_parser.set_defaults(function=render)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("expected")
    verify_parser.add_argument("evidence")
    verify_parser.add_argument("media_user")
    verify_parser.add_argument("static_user")
    verify_parser.set_defaults(function=verify)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
