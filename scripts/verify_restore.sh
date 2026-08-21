#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

DEPLOY_DIR="${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}"
REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
BACKUP_ENV="${DEPLOY_DIR}/backup.env"
COMPOSE_ENV="${DEPLOY_DIR}/compose.env"
ENV_HELPER="${REPO_DIR}/scripts/lib/production_env.sh"

[[ -f "$ENV_HELPER" && ! -L "$ENV_HELPER" ]] || {
  echo "Production environment reader is unavailable" >&2
  exit 1
}
# shellcheck source=scripts/lib/production_env.sh
source "$ENV_HELPER"

[[ "$EUID" -eq 0 ]] || { echo "Production restore verification must run as root" >&2; exit 1; }
[[ ! -L "$DEPLOY_DIR" ]] || { echo "Deployment directory must not be a symbolic link" >&2; exit 1; }

sf_require_private_root_file "$BACKUP_ENV" || exit 1
sf_require_private_root_file "$COMPOSE_ENV" || exit 1

sf_read_env_values "$BACKUP_ENV" backup_values \
  BACKUP_MODE RESTIC_HOST RESTIC_REPOSITORY RESTIC_REPOSITORY_ID RESTIC_IMAGE MINIO_MC_IMAGE \
  BACKUP_STAGING_ROOT || exit 1
BACKUP_MODE="${backup_values[0]}"
RESTIC_HOST="${backup_values[1]}"
RESTIC_REPOSITORY="${backup_values[2]}"
RESTIC_REPOSITORY_ID="${backup_values[3]}"
RESTIC_IMAGE="${backup_values[4]}"
MINIO_MC_IMAGE="${backup_values[5]}"
BACKUP_STAGING_ROOT="${backup_values[6]}"
sf_read_env_values "$COMPOSE_ENV" compose_values POSTGRES_IMAGE REDIS_IMAGE MINIO_IMAGE || exit 1
POSTGRES_IMAGE="${compose_values[0]}"
REDIS_IMAGE="${compose_values[1]}"
MINIO_IMAGE="${compose_values[2]}"
for image_spec in \
  "RESTIC_IMAGE:$RESTIC_IMAGE" \
  "POSTGRES_IMAGE:$POSTGRES_IMAGE" \
  "REDIS_IMAGE:$REDIS_IMAGE" \
  "MINIO_IMAGE:$MINIO_IMAGE" \
  "MINIO_MC_IMAGE:$MINIO_MC_IMAGE"; do
  sf_require_digest_image "${image_spec%%:*}" "${image_spec#*:}" || exit 1
done
[[ "$RESTIC_REPOSITORY_ID" =~ ^[0-9a-f]{64}$ ]] || {
  echo "RESTIC_REPOSITORY_ID must be the exact 64-character repository ID" >&2
  exit 1
}
[[ "$BACKUP_STAGING_ROOT" == /* && "$BACKUP_STAGING_ROOT" != "/" ]] || {
  echo "BACKUP_STAGING_ROOT must be an absolute non-root path" >&2
  exit 1
}
staging_root="$(realpath -e -- "$BACKUP_STAGING_ROOT")"
[[ "$staging_root" == "${BACKUP_STAGING_ROOT%/}" && \
   "$(stat -c '%u:%g:%a' "$staging_root")" == "0:0:700" ]] || {
  echo "BACKUP_STAGING_ROOT must be a canonical root-owned mode-0700 directory" >&2
  exit 1
}
mountpoint -q "$staging_root" || {
  echo "BACKUP_STAGING_ROOT must be a dedicated mounted filesystem" >&2
  exit 1
}

restic_repository_args=()
case "$BACKUP_MODE" in
  offsite)
    ;;
  local)
    sf_read_env_values "$BACKUP_ENV" local_values LOCAL_BACKUP_ROOT || exit 1
    LOCAL_BACKUP_ROOT="${local_values[0]}"
    [[ "$LOCAL_BACKUP_ROOT" == /* && "$LOCAL_BACKUP_ROOT" != "/" ]] || {
      echo "LOCAL_BACKUP_ROOT must be an absolute non-root path" >&2
      exit 1
    }
    local_backup_root="$(realpath -e -- "$LOCAL_BACKUP_ROOT")"
    [[ "$local_backup_root" == "${LOCAL_BACKUP_ROOT%/}" ]] || {
      echo "LOCAL_BACKUP_ROOT must not traverse symbolic links" >&2
      exit 1
    }
    [[ "$(stat -c '%u:%g:%a' "$local_backup_root")" == "0:0:700" ]] || {
      echo "LOCAL_BACKUP_ROOT must be owned by root:root with mode 0700" >&2
      exit 1
    }
    case "${RESTIC_REPOSITORY:-}" in
      /repository|/repository/*) ;;
      *)
        echo "Local RESTIC_REPOSITORY must be /repository or a child of it" >&2
        exit 1
        ;;
    esac
    restic_repository_args+=(
      --network none
      --mount "type=bind,src=${local_backup_root},dst=/repository"
    )
    ;;
  *)
    echo "BACKUP_MODE must be either local or offsite" >&2
    exit 1
    ;;
esac

restic_run() {
  docker run --rm --pull=never --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=128m \
    --tmpfs /root/.cache:rw,noexec,nosuid,size=128m \
    --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 100 --memory 512m --cpus 0.5 \
    --env-file "$BACKUP_ENV" \
    "${restic_repository_args[@]}" "$RESTIC_IMAGE" "$@"
}

repository_config="$(restic_run cat config)" || {
  echo "The configured Restic repository is unavailable" >&2
  exit 1
}
actual_repository_id="$(RESTIC_CONFIG="$repository_config" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["RESTIC_CONFIG"]).get("id", ""))
PY
)"
[[ "$actual_repository_id" == "$RESTIC_REPOSITORY_ID" ]] || {
  echo "Restic repository identity does not match the reviewed production repository" >&2
  exit 1
}

tmp_dir="$(mktemp -d "${staging_root%/}/starforge-restore.XXXXXX")"
container="starforge-restore-verify-$$"
redis_container="starforge-redis-restore-verify-$$"
minio_container="starforge-minio-restore-verify-$$"
password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker rm -f "$redis_container" >/dev/null 2>&1 || true
  docker rm -f "$minio_container" >/dev/null 2>&1 || true
  case "$tmp_dir" in
    "${staging_root%/}"/starforge-restore.*) rm -rf -- "$tmp_dir" ;;
    *) echo "Refusing to remove unexpected restore path: $tmp_dir" >&2 ;;
  esac
}
trap cleanup EXIT

snapshot="${STARFORGE_RESTORE_SNAPSHOT:-}"
if [[ -z "$snapshot" && -r "${DEPLOY_DIR}/last_verified_backup" ]]; then
  snapshot="$(<"${DEPLOY_DIR}/last_verified_backup")"
fi
[[ "$snapshot" =~ ^[0-9a-f]{64}$ ]] || {
  echo "An exact hexadecimal STARFORGE_RESTORE_SNAPSHOT or verified marker is required" >&2
  exit 1
}

echo "Restoring atomic snapshot $snapshot into an isolated directory..."
docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --tmpfs /root/.cache:rw,noexec,nosuid,size=128m \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 100 --memory 512m --cpus 0.5 \
  --env-file "$BACKUP_ENV" \
  "${restic_repository_args[@]}" \
  -v "$tmp_dir:/restore" "$RESTIC_IMAGE" \
  restore "$snapshot" --host "$RESTIC_HOST" --tag starforge --target /restore

snapshot_root="$tmp_dir/backup"
[[ -d "$snapshot_root" && ! -L "$snapshot_root" ]] || {
  echo "Restored snapshot does not have the exact /backup layout" >&2
  exit 1
}
dump_path="$snapshot_root/postgres.dump"
checksum_path="$snapshot_root/SHA256SUMS"
broker_path="$snapshot_root/broker/broker.rdb"
minio_path="$snapshot_root/minio"
minio_cluster_path="$snapshot_root/minio-cluster"
deployment_path="$snapshot_root/deployment"
[[ -f "$dump_path" && -s "$dump_path" ]] || { echo "Restored dump is missing" >&2; exit 1; }
[[ -f "$checksum_path" && -s "$checksum_path" ]] || { echo "Restored checksum manifest is missing" >&2; exit 1; }
[[ -f "$broker_path" && -s "$broker_path" ]] || { echo "Restored Redis snapshot is missing" >&2; exit 1; }
[[ -d "$minio_path" && -d "$minio_cluster_path" && -d "$deployment_path" ]] || {
  echo "Restored object or deployment snapshot is missing" >&2
  exit 1
}
for artifact in \
  "$minio_cluster_path/object-inventory.jsonl" \
  "$minio_cluster_path/bucket-metadata.zip" \
  "$minio_cluster_path/iam-metadata.zip" \
  "$deployment_path/app.env" \
  "$deployment_path/static-storage.env" \
  "$deployment_path/minio.env"; do
  [[ -f "$artifact" && ! -L "$artifact" ]] || {
    echo "Required restored artifact is missing: $(basename "$artifact")" >&2
    exit 1
  }
done
(cd "$snapshot_root" && sha256sum --check SHA256SUMS)
docker run --rm --pull=never --read-only --network none \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$(dirname "$dump_path"):/restore:ro" "$POSTGRES_IMAGE" \
  pg_restore --list /restore/"$(basename "$dump_path")" >/dev/null

docker run --rm --pull=never --read-only --network none \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$(dirname "$broker_path"):/restore:ro" \
  --entrypoint redis-check-rdb "$REDIS_IMAGE" \
  /restore/"$(basename "$broker_path")" >/dev/null

redis_data_dir="$tmp_dir/redis-data"
install -d -m 0700 -- "$redis_data_dir"
install -m 0600 -- "$broker_path" "$redis_data_dir/dump.rdb"
docker run -d --pull=never --name "$redis_container" --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --user 0:0 --cap-drop ALL --security-opt no-new-privileges \
  --memory=192m --cpus=0.25 --pids-limit=80 \
  -v "$redis_data_dir:/data" \
  --entrypoint redis-server "$REDIS_IMAGE" \
  --appendonly no --save "" --dir /data --dbfilename dump.rdb >/dev/null
for _ in $(seq 1 30); do
  if docker exec "$redis_container" redis-cli ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$redis_container" redis-cli ping >/dev/null
broker_keys_before="$(docker exec "$redis_container" redis-cli -n 2 dbsize | tr -d '\r')"
[[ "$broker_keys_before" =~ ^[0-9]+$ ]]
docker exec "$redis_container" redis-cli -n 0 flushdb >/dev/null
docker exec "$redis_container" redis-cli -n 1 flushdb >/dev/null
[[ "$(docker exec "$redis_container" redis-cli -n 0 dbsize | tr -d '\r')" == "0" ]]
[[ "$(docker exec "$redis_container" redis-cli -n 1 dbsize | tr -d '\r')" == "0" ]]
[[ "$(docker exec "$redis_container" redis-cli -n 2 dbsize | tr -d '\r')" == "$broker_keys_before" ]]

pgdata_dir="$tmp_dir/postgres-data"
install -d -m 0700 -- "$pgdata_dir"
docker run -d --pull=never --name "$container" --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /var/run/postgresql:rw,noexec,nosuid,size=16m \
  --security-opt no-new-privileges \
  --memory=384m --cpus=0.5 --pids-limit=100 \
  -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=restore \
  -v "$pgdata_dir:/var/lib/postgresql/data" "$POSTGRES_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$container" pg_isready -U postgres -d restore >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$container" pg_isready -U postgres -d restore >/dev/null

docker exec -i "$container" pg_restore -U postgres -d restore \
  --exit-on-error --no-owner --no-acl <"$dump_path"
migration_count="$(docker exec "$container" psql -U postgres -d restore -v ON_ERROR_STOP=1 -Atc \
  "SELECT count(*) FROM django_migrations;")"
schema_count="$(docker exec "$container" psql -U postgres -d restore -v ON_ERROR_STOP=1 -Atc \
  "SELECT count(*) FROM information_schema.schemata;")"
[[ "$migration_count" =~ ^[0-9]+$ && "$migration_count" -gt 0 ]]
[[ "$schema_count" =~ ^[0-9]+$ && "$schema_count" -gt 0 ]]

sf_read_env_values "$deployment_path/app.env" storage_values \
  AWS_STORAGE_BUCKET_NAME AWS_STATIC_BUCKET_NAME \
  AWS_S3_ACCESS_KEY_ID AWS_S3_SECRET_ACCESS_KEY || exit 1
media_bucket="${storage_values[0]}"
static_bucket="${storage_values[1]}"
media_access_key="${storage_values[2]}"
media_secret_key="${storage_values[3]}"
sf_read_env_values "$deployment_path/static-storage.env" static_storage_values \
  AWS_STATIC_ACCESS_KEY_ID AWS_STATIC_SECRET_ACCESS_KEY || exit 1
static_access_key="${static_storage_values[0]}"
static_secret_key="${static_storage_values[1]}"
[[ "$media_access_key" != "$static_access_key" && \
   "$media_secret_key" != "$static_secret_key" ]] || {
  echo "Restored media and static service credentials are not isolated" >&2
  exit 1
}
bucket_pattern='^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
[[ "$media_bucket" != "$static_bucket" && \
   "$media_bucket" =~ $bucket_pattern && "$static_bucket" =~ $bucket_pattern ]] || {
  echo "Restored application bucket names are invalid" >&2
  exit 1
}
[[ -d "$minio_path/$media_bucket" && -d "$minio_path/$static_bucket" ]] || {
  echo "Restored object mirror does not contain both configured buckets" >&2
  exit 1
}
sf_read_env_values "$deployment_path/minio.env" restored_minio_values \
  MINIO_ROOT_USER MINIO_ROOT_PASSWORD || exit 1
[[ "$media_access_key" != "${restored_minio_values[0]}" && \
   "$static_access_key" != "${restored_minio_values[0]}" && \
   "$media_secret_key" != "${restored_minio_values[1]}" && \
   "$static_secret_key" != "${restored_minio_values[1]}" ]] || {
  echo "Restored storage service credentials overlap MinIO root authority" >&2
  exit 1
}

minio_data_dir="$tmp_dir/minio-data"
minio_verification_dir="$tmp_dir/minio-verification"
minio_roundtrip_dir="$tmp_dir/minio-roundtrip"
install -d -m 0700 -- "$minio_data_dir" "$minio_verification_dir" "$minio_roundtrip_dir"
docker run -d --pull=never --name "$minio_container" --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory=384m --cpus=0.4 --pids-limit=100 \
  --env-file "$deployment_path/minio.env" \
  -v "$minio_data_dir:/data" \
  --entrypoint minio "$MINIO_IMAGE" server /data >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$minio_container" curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$minio_container" curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null

docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --tmpfs /root/.mc:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --network "container:${minio_container}" \
  --env-file "$deployment_path/minio.env" \
  -v "$minio_path:/objects:ro" \
  -v "$minio_cluster_path:/cluster:ro" \
  -v "$minio_verification_dir:/verification" \
  -v "$minio_roundtrip_dir:/roundtrip" \
  --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ceu '
    mc alias set restored http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    for directory in /objects/*; do
      [ -d "$directory" ]
      bucket="${directory##*/}"
      case "$bucket" in
        *[!a-z0-9.-]*|.*|*.) echo "Invalid restored bucket path" >&2; exit 1 ;;
      esac
      mc mb --ignore-existing "restored/$bucket" >/dev/null
    done
    mc admin cluster bucket import restored /cluster/bucket-metadata.zip >/dev/null
    mc admin cluster iam import restored /cluster/iam-metadata.zip >/dev/null
    for directory in /objects/*; do
      bucket="${directory##*/}"
      mc mirror --overwrite --remove "$directory/" "restored/$bucket/" >/dev/null
    done
    mc stat --json "restored/'"$media_bucket"'" > /verification/media-stat.json
    mc stat --json "restored/'"$static_bucket"'" > /verification/static-stat.json
    mc anonymous get-json "restored/'"$media_bucket"'" > /verification/media-policy.json
    mc anonymous get-json "restored/'"$static_bucket"'" > /verification/static-policy.json
    mc ls --recursive --json restored/ > /verification/restored-inventory.jsonl
    mc mirror --overwrite --remove restored/ /roundtrip/ >/dev/null
  '

python3 "$REPO_DIR/scripts/verify_minio_restore.py" \
  --original-inventory "$minio_cluster_path/object-inventory.jsonl" \
  --restored-inventory "$minio_verification_dir/restored-inventory.jsonl" \
  --original-objects "$minio_path" \
  --restored-objects "$minio_roundtrip_dir" \
  --media-policy "$minio_verification_dir/media-policy.json" \
  --static-policy "$minio_verification_dir/static-policy.json" \
  --static-bucket "$static_bucket"

if [[ "$BACKUP_MODE" == "local" ]]; then
  restic_run check --read-data
else
  restic_run check --read-data-subset=5%
fi

echo "Restore verification completed successfully."
