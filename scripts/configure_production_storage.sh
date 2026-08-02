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

for helper in "$ENV_HELPER" "$POLICY_HELPER"; do
  [[ -f "$helper" && ! -L "$helper" ]] || {
    echo "Production storage helper is unavailable" >&2
    exit 1
  }
done
# shellcheck source=scripts/lib/production_env.sh
source "$ENV_HELPER"

[[ "$EUID" -eq 0 ]] || {
  echo "Production storage configuration must run as root" >&2
  exit 1
}
[[ -d "$DEPLOY_DIR" && ! -L "$DEPLOY_DIR" ]] || {
  echo "Production deployment directory must be an existing non-symlink directory" >&2
  exit 1
}
[[ -f "$compose_file" && ! -L "$compose_file" ]] || {
  echo "Production Compose file is unavailable" >&2
  exit 1
}
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
  AWS_S3_SECRET_ACCESS_KEY || exit 1
media_bucket="${app_values[0]}"
static_bucket="${app_values[1]}"
media_public_endpoint="${app_values[2]}"
static_public_endpoint="${app_values[3]}"
storage_cors_origins="${app_values[4]}"
media_access_key="${app_values[5]}"
media_secret_key="${app_values[6]}"

sf_read_env_values "$STATIC_STORAGE_ENV" static_values \
  AWS_STATIC_ACCESS_KEY_ID AWS_STATIC_SECRET_ACCESS_KEY || exit 1
static_access_key="${static_values[0]}"
static_secret_key="${static_values[1]}"

sf_read_env_values "$MINIO_ENV" minio_values \
  MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_API_CORS_ALLOW_ORIGIN || exit 1
minio_root_user="${minio_values[0]}"
minio_root_password="${minio_values[1]}"
minio_cors_origins="${minio_values[2]}"

sf_read_env_values "$BACKUP_ENV" backup_values MINIO_MC_IMAGE || exit 1
minio_mc_image="${backup_values[0]}"
sf_require_digest_image MINIO_MC_IMAGE "$minio_mc_image" || exit 1

access_key_pattern='^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$'
for service_access_key in "$media_access_key" "$static_access_key"; do
  [[ "$service_access_key" =~ $access_key_pattern ]] || {
    echo "Storage service access keys must use a safe 3-128 character identifier" >&2
    exit 1
  }
  [[ "$service_access_key" != REPLACE_* && "$service_access_key" != GENERATE_* ]] || {
    echo "Storage service access-key placeholders must be replaced" >&2
    exit 1
  }
done
for service_secret_key in "$media_secret_key" "$static_secret_key"; do
  [[ "${#service_secret_key}" -ge 32 ]] || {
    echo "Storage service secret keys must contain at least 32 characters" >&2
    exit 1
  }
  [[ "$service_secret_key" != REPLACE_* && "$service_secret_key" != GENERATE_* ]] || {
    echo "Storage service secret-key placeholders must be replaced" >&2
    exit 1
  }
done
[[ "$media_access_key" != "$static_access_key" && \
   "$media_secret_key" != "$static_secret_key" && \
   "$media_access_key" != "$minio_root_user" && \
   "$static_access_key" != "$minio_root_user" && \
   "$media_secret_key" != "$minio_root_password" && \
   "$static_secret_key" != "$minio_root_password" ]] || {
  echo "Media, static, and MinIO root credentials must all be distinct" >&2
  exit 1
}

tmp_dir="$(mktemp -d /tmp/starforge-storage-configure.XXXXXX)"
cleanup() {
  if [[ -n "${tmp_dir:-}" && "$tmp_dir" == /tmp/starforge-storage-configure.* ]]; then
    rm -rf -- "$tmp_dir"
  else
    echo "Refusing to remove unexpected storage configuration path: ${tmp_dir:-<unset>}" >&2
  fi
}
trap cleanup EXIT
install -d -m 0700 -- "$tmp_dir/expected"

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
  printf 'MEDIA_SECRET_KEY=%s\n' "$media_secret_key"
  printf 'STATIC_ACCESS_KEY=%s\n' "$static_access_key"
  printf 'STATIC_SECRET_KEY=%s\n' "$static_secret_key"
  printf 'MEDIA_BUCKET=%s\n' "$media_bucket"
  printf 'STATIC_BUCKET=%s\n' "$static_bucket"
} >"$tmp_dir/service-accounts.env"
chmod 0600 "$tmp_dir/service-accounts.env"

compose=(docker compose --env-file "$COMPOSE_ENV" -f "$compose_file")
export STARFORGE_DEPLOY_DIR="$DEPLOY_DIR"
project_name="$("${compose[@]}" config --format json | python3 -c '
import json
import sys

print(json.load(sys.stdin).get("name", ""))
')"
[[ "$project_name" == "starforge" ]] || {
  echo "Production storage configuration must target the exact starforge Compose project" >&2
  exit 1
}
minio_containers="$("${compose[@]}" ps -q minio)"
mapfile -t minio_ids < <(printf '%s\n' "$minio_containers" | sed '/^$/d')
[[ "${#minio_ids[@]}" == "1" ]] || {
  echo "Exactly one running MinIO container is required" >&2
  exit 1
}
minio_container="${minio_ids[0]}"

# MinIO root authority exists only inside this digest-pinned, read-only operator
# container. Application and collectstatic containers receive non-root service
# credentials through different root-only files.
docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --tmpfs /root/.mc:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 64 --memory 128m --cpus 0.25 \
  --network "container:${minio_container}" \
  --env-file "$MINIO_ENV" \
  --env-file "$tmp_dir/service-accounts.env" \
  -v "$tmp_dir/expected:/policies:ro" \
  --entrypoint /bin/sh "$minio_mc_image" -ceu '
    mc alias set source http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc mb --ignore-existing "source/$MEDIA_BUCKET" "source/$STATIC_BUCKET" >/dev/null
    mc anonymous set none "source/$MEDIA_BUCKET" >/dev/null
    mc anonymous set-json /policies/public-static-policy.json "source/$STATIC_BUCKET" >/dev/null
    mc admin policy create source starforge-media-runtime-v1 /policies/media-runtime-policy.json >/dev/null
    mc admin policy create source starforge-static-writer-v1 /policies/static-writer-policy.json >/dev/null
    if ! mc admin user info source "$MEDIA_ACCESS_KEY" >/dev/null 2>&1; then
      mc admin user add source "$MEDIA_ACCESS_KEY" "$MEDIA_SECRET_KEY" >/dev/null
    fi
    if ! mc admin user info source "$STATIC_ACCESS_KEY" >/dev/null 2>&1; then
      mc admin user add source "$STATIC_ACCESS_KEY" "$STATIC_SECRET_KEY" >/dev/null
    fi
    mc admin policy attach source starforge-media-runtime-v1 --user "$MEDIA_ACCESS_KEY" >/dev/null
    mc admin policy attach source starforge-static-writer-v1 --user "$STATIC_ACCESS_KEY" >/dev/null
  '

# Reuse the read-only verifier rather than trusting successful mutation calls.
STARFORGE_REPO_DIR="$REPO_DIR" "$REPO_DIR/scripts/verify_production_storage.sh"
echo "Production storage buckets and exact least-privilege service identities are configured."
