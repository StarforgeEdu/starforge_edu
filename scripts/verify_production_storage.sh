#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

DEPLOY_DIR="${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}"
REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
compose_file="${REPO_DIR}/docker/docker-compose.production.yml"
COMPOSE_ENV="${DEPLOY_DIR}/compose.env"
APP_ENV="${DEPLOY_DIR}/app.env"
STATIC_STORAGE_ENV="${DEPLOY_DIR}/static-storage.env"
MINIO_ENV="${DEPLOY_DIR}/minio.env"
BACKUP_ENV="${DEPLOY_DIR}/backup.env"
ENV_HELPER="${REPO_DIR}/scripts/lib/production_env.sh"
POLICY_HELPER="${REPO_DIR}/scripts/storage_iam_contract.py"

die() {
  echo "$1" >&2
  exit "${2:-1}"
}

for helper in "$ENV_HELPER" "$POLICY_HELPER"; do
  [[ -f "$helper" && ! -L "$helper" ]] || die "Production storage helper is unavailable"
done
# shellcheck source=scripts/lib/production_env.sh
source "$ENV_HELPER"

[[ "$EUID" -eq 0 ]] || die "Production storage verification must run as root"
[[ -d "$DEPLOY_DIR" && ! -L "$DEPLOY_DIR" ]] || {
  die "Production deployment directory must be an existing non-symlink directory"
}
[[ -f "$compose_file" && ! -L "$compose_file" ]] || die "Compose file is unavailable"
for required in "$COMPOSE_ENV" "$APP_ENV" "$STATIC_STORAGE_ENV" "$MINIO_ENV" "$BACKUP_ENV"; do
  sf_require_private_root_file "$required" || exit 1
done
sf_clear_compose_process_overrides
sf_export_compose_infrastructure_images "$COMPOSE_ENV" || exit 1

: "${APP_IMAGE:?APP_IMAGE must name the candidate image}"
candidate_image="$APP_IMAGE"
export APP_IMAGE="$candidate_image"

sf_read_env_values "$APP_ENV" app_values \
  AWS_STORAGE_BUCKET_NAME \
  AWS_STATIC_BUCKET_NAME \
  AWS_S3_PUBLIC_ENDPOINT_URL \
  AWS_STATIC_PUBLIC_ENDPOINT_URL \
  STORAGE_CORS_ALLOWED_ORIGINS \
  AWS_S3_ACCESS_KEY_ID \
  AWS_S3_SECRET_ACCESS_KEY \
  STATIC_STORAGE_WRITE_ENABLED \
  AWS_EC2_METADATA_DISABLED || die "Application storage environment is invalid"
media_bucket="${app_values[0]}"
static_bucket="${app_values[1]}"
media_public_endpoint="${app_values[2]}"
static_public_endpoint="${app_values[3]}"
storage_cors_origins="${app_values[4]}"
media_access_key="${app_values[5]}"
media_secret_key="${app_values[6]}"
runtime_static_write="${app_values[7]}"
metadata_disabled="${app_values[8]}"

[[ "$runtime_static_write" == "False" || "$runtime_static_write" == "false" ]] || {
  die "app.env must keep STATIC_STORAGE_WRITE_ENABLED=False"
}
[[ "$metadata_disabled" == "true" || "$metadata_disabled" == "True" ]] || {
  die "app.env must disable the AWS instance-metadata credential provider"
}

sf_read_env_values "$STATIC_STORAGE_ENV" static_values \
  AWS_STATIC_ACCESS_KEY_ID AWS_STATIC_SECRET_ACCESS_KEY || {
  die "Static-writer environment is invalid"
}
static_access_key="${static_values[0]}"
static_secret_key="${static_values[1]}"

sf_read_env_values "$MINIO_ENV" minio_values \
  MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_API_CORS_ALLOW_ORIGIN || {
  die "MinIO environment is invalid"
}
minio_root_user="${minio_values[0]}"
minio_root_password="${minio_values[1]}"
minio_cors_origins="${minio_values[2]}"

sf_read_env_values "$BACKUP_ENV" backup_values MINIO_MC_IMAGE || {
  die "Backup tool image configuration is invalid"
}
minio_mc_image="${backup_values[0]}"
sf_require_digest_image MINIO_MC_IMAGE "$minio_mc_image" || exit 1

access_key_pattern='^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$'
for service_access_key in "$media_access_key" "$static_access_key"; do
  [[ "$service_access_key" =~ $access_key_pattern ]] || {
    die "Storage service access keys must use a safe 3-128 character identifier"
  }
  [[ "$service_access_key" != REPLACE_* && "$service_access_key" != GENERATE_* ]] || {
    die "Storage service access-key placeholders must be replaced"
  }
done
for service_secret_key in "$media_secret_key" "$static_secret_key"; do
  [[ "${#service_secret_key}" -ge 32 ]] || {
    die "Storage service secret keys must contain at least 32 characters"
  }
  [[ "$service_secret_key" != REPLACE_* && "$service_secret_key" != GENERATE_* ]] || {
    die "Storage service secret-key placeholders must be replaced"
  }
done
[[ "$media_access_key" != "$static_access_key" && \
   "$media_secret_key" != "$static_secret_key" && \
   "$media_access_key" != "$minio_root_user" && \
   "$static_access_key" != "$minio_root_user" && \
   "$media_secret_key" != "$minio_root_password" && \
   "$static_secret_key" != "$minio_root_password" ]] || {
  die "Media, static, and MinIO root credentials must all be distinct"
}

tmp_dir="$(mktemp -d /tmp/starforge-storage-verify.XXXXXX)"
cleanup() {
  if [[ -n "${tmp_dir:-}" && "$tmp_dir" == /tmp/starforge-storage-verify.* ]]; then
    rm -rf -- "$tmp_dir"
  else
    echo "Refusing to remove unexpected storage verification path: ${tmp_dir:-<unset>}" >&2
  fi
}
trap cleanup EXIT
install -d -m 0700 -- "$tmp_dir/expected" "$tmp_dir/evidence"

python3 "$POLICY_HELPER" render \
  "$tmp_dir/expected" \
  "$media_bucket" \
  "$static_bucket" \
  "$storage_cors_origins" \
  "$minio_cors_origins" \
  "$media_public_endpoint" \
  "$static_public_endpoint"

{
  printf 'MEDIA_ACCESS_KEY=%s\n' "$media_access_key"
  printf 'STATIC_ACCESS_KEY=%s\n' "$static_access_key"
  printf 'MEDIA_BUCKET=%s\n' "$media_bucket"
  printf 'STATIC_BUCKET=%s\n' "$static_bucket"
} >"$tmp_dir/service-identities.env"
chmod 0600 "$tmp_dir/service-identities.env"

compose=(docker compose --env-file "$COMPOSE_ENV" -f "$compose_file")
export STARFORGE_DEPLOY_DIR="$DEPLOY_DIR"
project_name="$("${compose[@]}" config --format json | python3 -c '
import json
import sys

print(json.load(sys.stdin).get("name", ""))
')"
[[ "$project_name" == "starforge" ]] || die \
  "Production storage verification must target the exact starforge Compose project"
minio_containers="$("${compose[@]}" ps -q minio)"
mapfile -t minio_ids < <(printf '%s\n' "$minio_containers" | sed '/^$/d')
[[ "${#minio_ids[@]}" == "1" ]] || die "Exactly one running MinIO container is required"
minio_container="${minio_ids[0]}"

# Root is used only for read-only IAM/policy introspection inside this pinned
# operator image. Neither application service-account secret is present here.
docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --tmpfs /root/.mc:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 64 --memory 128m --cpus 0.25 \
  --network "container:${minio_container}" \
  --env-file "$MINIO_ENV" \
  --env-file "$tmp_dir/service-identities.env" \
  -v "$tmp_dir/evidence:/verification" \
  --entrypoint /bin/sh "$minio_mc_image" -ceu '
    mc alias set source http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc stat --json "source/$MEDIA_BUCKET" > /verification/media-stat.json
    mc stat --json "source/$STATIC_BUCKET" > /verification/static-stat.json
    mc anonymous get-json "source/$MEDIA_BUCKET" > /verification/media-public-policy.json
    mc anonymous get-json "source/$STATIC_BUCKET" > /verification/static-public-policy.json
    mc admin policy info --json source starforge-media-runtime-v1 > /verification/media-policy-info.json
    mc admin policy info --json source starforge-static-writer-v1 > /verification/static-policy-info.json
    mc admin user info --json source "$MEDIA_ACCESS_KEY" > /verification/media-user.json
    mc admin user info --json source "$STATIC_ACCESS_KEY" > /verification/static-user.json
    mc admin policy entities --json source \
      --user "$MEDIA_ACCESS_KEY" --user "$STATIC_ACCESS_KEY" > /verification/iam-mappings.json
    : > /verification/groups.jsonl
    mc admin group list source | while IFS= read -r group; do
      [ -n "$group" ] || continue
      case "$group" in
        *[!A-Za-z0-9._-]*) echo "Unsafe MinIO group name" >&2; exit 1 ;;
      esac
      mc admin group info --json source "$group" >> /verification/groups.jsonl
    done
  '

python3 "$POLICY_HELPER" verify \
  "$tmp_dir/expected" "$tmp_dir/evidence" "$media_access_key" "$static_access_key"

python3 - "$tmp_dir" <<'PY'
import json
import sys
from pathlib import Path

directory = Path(sys.argv[1])
for name in ("media-stat.json", "static-stat.json"):
    rows = [
        json.loads(line)
        for line in (directory / "evidence" / name).read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not rows or any(row.get("status") == "error" for row in rows):
        raise SystemExit(f"Storage bucket check failed: {name}")

media_policy = json.loads(
    (directory / "evidence" / "media-public-policy.json").read_text(encoding="utf-8")
)
if media_policy != {}:
    raise SystemExit("Private media bucket unexpectedly has an anonymous access policy")

expected = json.loads(
    (directory / "expected" / "public-static-policy.json").read_text(encoding="utf-8")
)
actual = json.loads(
    (directory / "evidence" / "static-public-policy.json").read_text(encoding="utf-8")
)
if actual != expected:
    raise SystemExit("Public static bucket policy differs from the reviewed GetObject-only policy")
PY

curl -fsS --proto '=https' --tlsv1.2 --connect-timeout 5 --max-time 15 \
  "${media_public_endpoint%/}/minio/health/live" >/dev/null
curl -fsS --proto '=https' --tlsv1.2 --connect-timeout 5 --max-time 15 \
  "${static_public_endpoint%/}/minio/health/live" >/dev/null

# The isolated writer proves its own positive path and every negative boundary.
# Its fixed probe is always removed in-process and never collides with an
# application asset.
"${compose[@]}" --profile tools run --rm --no-deps -T collectstatic python - <<'PY'
import json
import os
from urllib.request import Request, urlopen

from botocore.exceptions import ClientError
from django.conf import settings
from django.core.files.storage import storages

key = ".starforge-release-probe/static-current"
media_bucket = os.environ["AWS_STORAGE_BUCKET_NAME"]
static_bucket = settings.STORAGES["staticfiles"]["OPTIONS"]["bucket_name"]
static_storage = storages["staticfiles"]
if not settings.STATIC_STORAGE_WRITE_ENABLED:
    raise SystemExit("collectstatic did not select the isolated writable backend")
if any(os.environ.get(name) for name in ("AWS_S3_ACCESS_KEY_ID", "AWS_S3_SECRET_ACCESS_KEY")):
    raise SystemExit("collectstatic received media-runtime credentials")
if settings.STORAGES["default"].get("BACKEND") != (
    "infrastructure.storage.backends.DisabledObjectStorage"
):
    raise SystemExit("collectstatic did not disable the media backend")
client = static_storage.connection.meta.client
credentials = client._request_signer._credentials  # noqa: SLF001 - release-boundary assertion
if credentials.access_key != os.environ["AWS_STATIC_ACCESS_KEY_ID"]:
    raise SystemExit("Static backend did not use the explicit static-writer identity")


def expect_denied(label, operation, cleanup=None):
    try:
        result = operation()
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in {401, 403}:
            raise SystemExit(f"{label} failed with an unexpected status") from exc
        return
    if cleanup is not None:
        cleanup()
    body = result.get("Body") if isinstance(result, dict) else None
    if body is not None:
        body.close()
    raise SystemExit(f"Static-writer identity unexpectedly gained {label}")


def verify_bucket_discovery():
    # MinIO implements ListBuckets as a filtered view for identities that have
    # ListBucket on one bucket. Either a 403 or an exact own-bucket-only result
    # proves that ListAllMyBuckets authority is absent.
    try:
        response = client.list_buckets()
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in {401, 403}:
            raise SystemExit("Bucket discovery failed with an unexpected status") from exc
        return
    visible = {item.get("Name") for item in response.get("Buckets", [])}
    if visible != {static_bucket}:
        raise SystemExit("Static-writer identity can discover buckets outside its exact scope")


try:
    client.put_object(Bucket=static_bucket, Key=key, Body=b"static-ok", ContentType="text/plain")
    response = client.get_object(Bucket=static_bucket, Key=key)
    try:
        if response["Body"].read() != b"static-ok":
            raise SystemExit("Static-writer read returned unexpected bytes")
    finally:
        response["Body"].close()
    listed = client.list_objects_v2(Bucket=static_bucket, Prefix=key, MaxKeys=1)
    if not any(item.get("Key") == key for item in listed.get("Contents", [])):
        raise SystemExit("Static-writer could not list its exact probe")
    public_url = static_storage.url(key)
    request = Request(
        public_url,
        method="OPTIONS",
        headers={
            "Origin": os.environ["STORAGE_CORS_ALLOWED_ORIGINS"].split(",", 1)[0],
            "Access-Control-Request-Method": "GET",
        },
    )
    with urlopen(request, timeout=15) as preflight:  # noqa: S310
        if preflight.status not in {200, 204}:
            raise SystemExit("Static CORS preflight returned an unexpected status")
        expected_origin = os.environ["STORAGE_CORS_ALLOWED_ORIGINS"].split(",", 1)[0]
        if preflight.headers.get("Access-Control-Allow-Origin") != expected_origin:
            raise SystemExit("Static CORS preflight did not authorize the configured origin")
        allowed_methods = ",".join(
            preflight.headers.get_all("Access-Control-Allow-Methods", [])
        ).upper()
        if "GET" not in {value.strip() for value in allowed_methods.split(",")}:
            raise SystemExit("Static CORS preflight did not authorize GET")
    with urlopen(public_url, timeout=15) as public:  # noqa: S310
        if public.status != 200 or public.read() != b"static-ok":
            raise SystemExit("Public static endpoint returned unexpected probe bytes")

    verify_bucket_discovery()
    expect_denied(
        "media ListBucket",
        lambda: client.list_objects_v2(Bucket=media_bucket, MaxKeys=1),
    )
    expect_denied(
        "media GetObject",
        lambda: client.get_object(Bucket=media_bucket, Key=key),
    )
    expect_denied(
        "media PutObject",
        lambda: client.put_object(Bucket=media_bucket, Key=key, Body=b"forbidden"),
        cleanup=lambda: client.delete_object(Bucket=media_bucket, Key=key),
    )
    expect_denied(
        "static bucket administration",
        lambda: client.get_bucket_policy(Bucket=static_bucket),
    )
    expect_denied(
        "static bucket policy mutation",
        lambda: client.put_bucket_policy(
            Bucket=static_bucket,
            Policy=json.dumps(
                {
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
            ),
        ),
    )
finally:
    client.delete_object(Bucket=static_bucket, Key=key)
PY

# A candidate runtime gets only the media account. It must have no static
# credential variables, no writable static backend, no bucket discovery or
# administration, and an explicit deny on every static-bucket S3 action.
"${compose[@]}" run --rm --no-deps -T web python - <<'PY'
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import requests
from botocore.exceptions import ClientError
from django.conf import settings

from infrastructure.storage.s3_client import (
    get_s3_client,
    presign_download,
    presign_post_upload,
    presign_upload,
)

for forbidden_name in ("AWS_STATIC_ACCESS_KEY_ID", "AWS_STATIC_SECRET_ACCESS_KEY"):
    if forbidden_name in os.environ:
        raise SystemExit("Runtime process received static-writer credentials")
if settings.STATIC_STORAGE_WRITE_ENABLED:
    raise SystemExit("Runtime process enabled static writes")
static_config = settings.STORAGES["staticfiles"]
if static_config.get("BACKEND") != "infrastructure.storage.backends.PublicStaticFilesStorage":
    raise SystemExit("Runtime process did not select the URL-only static backend")
if {"access_key", "secret_key", "endpoint_url"}.intersection(static_config.get("OPTIONS", {})):
    raise SystemExit("Runtime static backend contains object-I/O configuration")

client = get_s3_client()
credentials = client._request_signer._credentials  # noqa: SLF001 - release-boundary assertion
if credentials.access_key != os.environ["AWS_S3_ACCESS_KEY_ID"]:
    raise SystemExit("Media backend did not use the explicit media-runtime identity")

key = ".starforge-release-probe/media-current"
post_key = f"{key}.post"
media_bucket = os.environ["AWS_STORAGE_BUCKET_NAME"]
static_bucket = settings.STORAGES["staticfiles"]["OPTIONS"]["bucket_name"]
media_endpoint = settings.AWS_S3_PUBLIC_ENDPOINT_URL.rstrip("/")
probe_origin = os.environ["STORAGE_CORS_ALLOWED_ORIGINS"].split(",", 1)[0]


def expect_denied(label, operation, cleanup=None):
    try:
        result = operation()
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in {401, 403}:
            raise SystemExit(f"{label} failed with an unexpected status") from exc
        return
    if cleanup is not None:
        cleanup()
    body = result.get("Body") if isinstance(result, dict) else None
    if body is not None:
        body.close()
    raise SystemExit(f"Media-runtime identity unexpectedly gained {label}")


def verify_bucket_discovery():
    # The pinned MinIO release may return a permission-filtered ListBuckets
    # response rather than 403. Never accept visibility beyond the media bucket.
    try:
        response = client.list_buckets()
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in {401, 403}:
            raise SystemExit("Bucket discovery failed with an unexpected status") from exc
        return
    visible = {item.get("Name") for item in response.get("Buckets", [])}
    if visible != {media_bucket}:
        raise SystemExit("Media-runtime identity can discover buckets outside its exact scope")


def verify_preflight(url, *, method, request_headers=()):
    headers = {"Origin": probe_origin, "Access-Control-Request-Method": method}
    if request_headers:
        headers["Access-Control-Request-Headers"] = ",".join(request_headers)
    request = Request(url, method="OPTIONS", headers=headers)
    with urlopen(request, timeout=15) as response:  # noqa: S310
        if response.status not in {200, 204}:
            raise SystemExit("Storage CORS preflight returned an unexpected status")
        if response.headers.get("Access-Control-Allow-Origin") != probe_origin:
            raise SystemExit("Storage CORS preflight did not authorize the configured origin")
        methods = ",".join(response.headers.get_all("Access-Control-Allow-Methods", [])).upper()
        if method not in {allowed.strip() for allowed in methods.split(",")}:
            raise SystemExit(f"Storage CORS preflight did not authorize {method}")
        if request_headers:
            values = ",".join(response.headers.get_all("Access-Control-Allow-Headers", [])).lower()
            allowed_headers = {value.strip() for value in values.split(",")}
            if "*" not in allowed_headers and not set(request_headers).issubset(allowed_headers):
                raise SystemExit("Storage CORS preflight did not authorize required headers")


verify_bucket_discovery()
expect_denied(
    "static ListBucket",
    lambda: client.list_objects_v2(Bucket=static_bucket, MaxKeys=1),
)
expect_denied(
    "static GetObject",
    lambda: client.get_object(Bucket=static_bucket, Key=key),
)
expect_denied(
    "static PutObject",
    lambda: client.put_object(Bucket=static_bucket, Key=key, Body=b"forbidden"),
    cleanup=lambda: client.delete_object(Bucket=static_bucket, Key=key),
)
expect_denied(
    "static DeleteObject",
    lambda: client.delete_object(Bucket=static_bucket, Key=key),
)
expect_denied(
    "static bucket location",
    lambda: client.get_bucket_location(Bucket=static_bucket),
)
expect_denied(
    "media bucket administration",
    lambda: client.get_bucket_policy(Bucket=media_bucket),
)
expect_denied(
    "media bucket policy mutation",
    lambda: client.put_bucket_policy(
        Bucket=media_bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{media_bucket}/*"],
                    }
                ],
            }
        ),
    ),
)

try:
    put_url = presign_upload(key, expires_in=60, content_type="text/plain", size_bytes=7)
    verify_preflight(put_url, method="PUT", request_headers=("content-type",))
    request = Request(
        put_url,
        data=b"private",
        method="PUT",
        headers={"Content-Type": "text/plain", "Content-Length": "7"},
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310
        if response.status not in {200, 204}:
            raise SystemExit("Presigned media PUT returned an unexpected status")
    listed = client.list_objects_v2(Bucket=media_bucket, Prefix=key, MaxKeys=1)
    if not any(item.get("Key") == key for item in listed.get("Contents", [])):
        raise SystemExit("Media-runtime identity could not list its exact probe")
    media_url = f"{media_endpoint}/{media_bucket}/{key}"
    verify_preflight(media_url, method="GET")
    with urlopen(presign_download(key, expires_in=60), timeout=15) as response:  # noqa: S310
        if response.status != 200 or response.read() != b"private":
            raise SystemExit("Signed private media verification returned unexpected content")
    try:
        urlopen(media_url, timeout=15)  # noqa: S310
    except HTTPError as exc:
        if exc.code not in {401, 403}:
            raise SystemExit("Private media endpoint returned an unexpected status") from exc
    else:
        raise SystemExit("Private media object was anonymously readable")
finally:
    client.delete_object(Bucket=media_bucket, Key=key)

try:
    post = presign_post_upload(post_key, size_bytes=4, expires_in=60, content_type="text/plain")
    verify_preflight(post["url"], method="POST", request_headers=("content-type",))
    response = requests.post(
        post["url"],
        data=post["fields"],
        files={"file": ("probe.txt", b"post", "text/plain")},
        timeout=(5, 15),
    )
    if response.status_code not in {200, 201, 204}:
        raise SystemExit("Presigned media POST returned an unexpected status")
    with urlopen(presign_download(post_key, expires_in=60), timeout=15) as download:  # noqa: S310
        if download.status != 200 or download.read() != b"post":
            raise SystemExit("Presigned media POST did not persist the exact probe bytes")
finally:
    client.delete_object(Bucket=media_bucket, Key=post_key)
PY

echo "Production storage identities, exact policies, public delivery, CORS, and denials are verified."
