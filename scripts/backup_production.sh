#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

DEPLOY_DIR="${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}"
REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
COMPOSE_FILE="${REPO_DIR}/docker/docker-compose.production.yml"
COMPOSE_ENV="${DEPLOY_DIR}/compose.env"
DB_ENV="${DEPLOY_DIR}/postgres.env"
MINIO_ENV="${DEPLOY_DIR}/minio.env"
BACKUP_ENV="${DEPLOY_DIR}/backup.env"
LOCK_FILE="${DEPLOY_DIR}/backup.lock"
ENV_HELPER="${REPO_DIR}/scripts/lib/production_env.sh"
mode="${1:-}"
[[ -z "$mode" || "$mode" == "--preflight" ]] || {
  echo "Usage: $0 [--preflight]" >&2
  exit 2
}

[[ -f "$ENV_HELPER" && ! -L "$ENV_HELPER" ]] || {
  echo "Production environment reader is unavailable" >&2
  exit 1
}
# shellcheck source=scripts/lib/production_env.sh
source "$ENV_HELPER"

[[ "$EUID" -eq 0 ]] || { echo "Production backup must run as root" >&2; exit 1; }
[[ ! -L "$DEPLOY_DIR" ]] || { echo "Deployment directory must not be a symbolic link" >&2; exit 1; }

[[ -f "$COMPOSE_FILE" && ! -L "$COMPOSE_FILE" ]] || {
  echo "Production Compose file is unavailable" >&2
  exit 1
}
for required in "$COMPOSE_ENV" "$DB_ENV" "$MINIO_ENV" "$BACKUP_ENV"; do
  sf_require_private_root_file "$required" || exit 1
done

[[ ! -L "$LOCK_FILE" ]] || { echo "Backup lock must not be a symbolic link" >&2; exit 1; }
exec 8>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
flock -n 8 || { echo "Another backup is already running" >&2; exit 1; }

sf_clear_compose_process_overrides
sf_export_compose_infrastructure_images "$COMPOSE_ENV" || exit 1
sf_read_env_values "$COMPOSE_ENV" compose_app_values APP_IMAGE || exit 1
APP_IMAGE="${compose_app_values[0]}"
export APP_IMAGE
sf_read_env_values "$DB_ENV" db_values POSTGRES_USER POSTGRES_DB || exit 1
POSTGRES_USER="${db_values[0]}"
POSTGRES_DB="${db_values[1]}"
sf_read_env_values "$MINIO_ENV" minio_values MINIO_ROOT_USER MINIO_ROOT_PASSWORD || exit 1
MINIO_ROOT_USER="${minio_values[0]}"
MINIO_ROOT_PASSWORD="${minio_values[1]}"
sf_read_env_values "$BACKUP_ENV" backup_values \
  BACKUP_MODE RESTIC_HOST RESTIC_REPOSITORY RESTIC_REPOSITORY_ID RESTIC_PASSWORD \
  RESTIC_IMAGE MINIO_MC_IMAGE BACKUP_STAGING_ROOT BACKUP_MIN_FREE_BYTES || exit 1
BACKUP_MODE="${backup_values[0]}"
RESTIC_HOST="${backup_values[1]}"
RESTIC_REPOSITORY="${backup_values[2]}"
RESTIC_REPOSITORY_ID="${backup_values[3]}"
RESTIC_PASSWORD="${backup_values[4]}"
RESTIC_IMAGE="${backup_values[5]}"
MINIO_MC_IMAGE="${backup_values[6]}"
BACKUP_STAGING_ROOT="${backup_values[7]}"
BACKUP_MIN_FREE_BYTES="${backup_values[8]}"

for image_spec in \
  "POSTGRES_IMAGE:$POSTGRES_IMAGE" \
  "REDIS_IMAGE:$REDIS_IMAGE" \
  "MINIO_IMAGE:$MINIO_IMAGE" \
  "RESTIC_IMAGE:$RESTIC_IMAGE" \
  "MINIO_MC_IMAGE:$MINIO_MC_IMAGE"; do
  sf_require_digest_image "${image_spec%%:*}" "${image_spec#*:}" || exit 1
done
[[ "$RESTIC_REPOSITORY_ID" =~ ^[0-9a-f]{64}$ ]] || {
  echo "RESTIC_REPOSITORY_ID must be the exact 64-character repository ID" >&2
  exit 1
}
[[ "$BACKUP_MIN_FREE_BYTES" =~ ^[0-9]+$ ]] || {
  echo "BACKUP_MIN_FREE_BYTES must be a non-negative integer" >&2
  exit 1
}

restic_repository_args=()
local_backup_root=""

[[ "$BACKUP_STAGING_ROOT" == /* && "$BACKUP_STAGING_ROOT" != "/" ]] || {
  echo "BACKUP_STAGING_ROOT must be an absolute non-root path" >&2
  exit 1
}
[[ -d "$BACKUP_STAGING_ROOT" && ! -L "$BACKUP_STAGING_ROOT" ]] || {
  echo "BACKUP_STAGING_ROOT must be a pre-provisioned directory" >&2
  exit 1
}
staging_root="$(realpath -e -- "$BACKUP_STAGING_ROOT")"
[[ "$staging_root" == "${BACKUP_STAGING_ROOT%/}" ]] || {
  echo "BACKUP_STAGING_ROOT must not traverse symbolic links" >&2
  exit 1
}
[[ "$(stat -c '%u:%g:%a' "$staging_root")" == "0:0:700" ]] || {
  echo "BACKUP_STAGING_ROOT must be owned by root:root with mode 0700" >&2
  exit 1
}
mountpoint -q "$staging_root" || {
  echo "BACKUP_STAGING_ROOT must be a dedicated mounted filesystem" >&2
  exit 1
}
tmp_parent="$staging_root"

path_contains() {
  local parent="${1%/}"
  local child="${2%/}"
  [[ "$child" == "$parent" || "$child" == "$parent/"* ]]
}

case "$BACKUP_MODE" in
  offsite)
    ;;
  local)
    sf_read_env_values "$BACKUP_ENV" local_values LOCAL_BACKUP_ROOT || exit 1
    LOCAL_BACKUP_ROOT="${local_values[0]}"
    [[ "$EUID" -eq 0 ]] || { echo "Local backups must run as root" >&2; exit 1; }
    : "${LOCAL_BACKUP_ROOT:?LOCAL_BACKUP_ROOT is required for local backups}"
    [[ "$LOCAL_BACKUP_ROOT" == /* && "$LOCAL_BACKUP_ROOT" != "/" ]] || {
      echo "LOCAL_BACKUP_ROOT must be an absolute non-root path" >&2
      exit 1
    }
    [[ ! -L "$LOCAL_BACKUP_ROOT" ]] || {
      echo "LOCAL_BACKUP_ROOT must not be a symbolic link" >&2
      exit 1
    }
    if [[ ! -e "$LOCAL_BACKUP_ROOT" ]]; then
      install -d -o root -g root -m 0700 -- "$LOCAL_BACKUP_ROOT"
    fi
    [[ -d "$LOCAL_BACKUP_ROOT" && ! -L "$LOCAL_BACKUP_ROOT" ]] || {
      echo "LOCAL_BACKUP_ROOT must be a directory" >&2
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

    canonical_deploy="$(realpath -e -- "$DEPLOY_DIR")"
    canonical_repo="$(realpath -e -- "$REPO_DIR")"
    if path_contains "$canonical_deploy" "$local_backup_root" || \
       path_contains "$local_backup_root" "$canonical_deploy" || \
       path_contains "$canonical_repo" "$local_backup_root" || \
       path_contains "$local_backup_root" "$canonical_repo" || \
       path_contains "$staging_root" "$local_backup_root" || \
       path_contains "$local_backup_root" "$staging_root"; then
      echo "LOCAL_BACKUP_ROOT must be separate from deploy, repository, and staging paths" >&2
      exit 1
    fi
    case "$RESTIC_REPOSITORY" in
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

compose=(docker compose --env-file "$COMPOSE_ENV" -f "$COMPOSE_FILE")
export STARFORGE_DEPLOY_DIR="$DEPLOY_DIR"
project_name="$("${compose[@]}" config --format json | python3 -c \
  'import json, sys; print(json.load(sys.stdin).get("name", ""))')"
[[ "$project_name" == "starforge" ]] || {
  echo "Production backup Compose project must resolve exactly to starforge" >&2
  exit 1
}

available_bytes() {
  df -PB1 "$1" | awk 'NR == 2 { print $4 }'
}

local_source_bytes() {
  local postgres_mount redis_mount minio_mount postgres_bytes redis_bytes minio_bytes deployment_bytes
  postgres_mount="$(docker volume inspect "${project_name}_sf_pg" --format '{{.Mountpoint}}')"
  redis_mount="$(docker volume inspect "${project_name}_sf_redis" --format '{{.Mountpoint}}')"
  minio_mount="$(docker volume inspect "${project_name}_sf_minio" --format '{{.Mountpoint}}')"
  postgres_bytes="$(du -sb "$postgres_mount" | awk '{ print $1 }')"
  redis_bytes="$(du -sb "$redis_mount" | awk '{ print $1 }')"
  minio_bytes="$(du -sb "$minio_mount" | awk '{ print $1 }')"
  deployment_bytes="$(du -sb "$DEPLOY_DIR" | awk '{ print $1 }')"
  printf '%s\n' "$((postgres_bytes + redis_bytes + minio_bytes + deployment_bytes))"
}

require_backup_capacity() {
  local phase="$1"
  local staging_free staging_required repository_free repository_required
  staging_free="$(available_bytes "$staging_root")"
  staging_required="$BACKUP_MIN_FREE_BYTES"
  if [[ "$phase" == "preflight" ]]; then
    # Logical staging can approach the live database, broker, object, and
    # configuration footprint. Keep another full-footprint margin so a growth
    # race fails before the application enters maintenance.
    staging_required="$((staging_required + (2 * $(local_source_bytes))))"
  fi
  if (( staging_free < staging_required )); then
    echo "Insufficient staging capacity during $phase: free=$staging_free required=$staging_required" >&2
    exit 1
  fi
  if [[ "$BACKUP_MODE" == "local" ]]; then
    repository_free="$(available_bytes "$local_backup_root")"
    repository_required="$BACKUP_MIN_FREE_BYTES"
    if [[ "$phase" == "preflight" ]]; then
      repository_required="$((repository_required + $(local_source_bytes)))"
    fi
    if (( repository_free < repository_required )); then
      echo "Insufficient local repository capacity during $phase: free=$repository_free required=$repository_required" >&2
      exit 1
    fi
  fi
}

require_backup_capacity preflight

restic_container_args=(
  --rm
  --pull=never
  --read-only
  --tmpfs /tmp:rw,noexec,nosuid,size=128m
  --tmpfs /root/.cache:rw,noexec,nosuid,size=128m
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit 100
  --memory 512m
  --cpus 0.5
  --env-file "$BACKUP_ENV"
)

restic_run() {
  docker run "${restic_container_args[@]}" \
    "${restic_repository_args[@]}" "$RESTIC_IMAGE" "$@"
}

repository_config="$(restic_run cat config)" || {
  echo "The configured Restic repository is unavailable; initialize it only through the backup bootstrap runbook" >&2
  exit 1
}
actual_repository_id="$(RESTIC_CONFIG="$repository_config" python3 - <<'PY'
import json
import os

value = json.loads(os.environ["RESTIC_CONFIG"]).get("id", "")
if not isinstance(value, str):
    raise SystemExit("Restic repository config has no stable ID")
print(value)
PY
)"
[[ "$actual_repository_id" == "$RESTIC_REPOSITORY_ID" ]] || {
  echo "Restic repository identity does not match the reviewed production repository" >&2
  exit 1
}

if [[ "$mode" == "--preflight" ]]; then
  echo "Backup repository identity and staging capacity are ready."
  exit 0
fi

tmp_dir="$(mktemp -d "${tmp_parent%/}/starforge-backup.XXXXXX")"
restic_output=""
cleanup() {
  if [[ -n "${restic_output:-}" && "$restic_output" == "${tmp_parent%/}"/.starforge-restic-output.* ]]; then
    rm -f -- "$restic_output"
  elif [[ -n "${restic_output:-}" ]]; then
    echo "Refusing to remove unexpected Restic output path: $restic_output" >&2
  fi
  if [[ -n "${tmp_dir:-}" && "$tmp_dir" == "${tmp_parent%/}"/starforge-backup.* ]]; then
    rm -rf -- "$tmp_dir"
  else
    echo "Refusing to remove unexpected backup path: ${tmp_dir:-<unset>}" >&2
  fi
}
trap cleanup EXIT

echo "Creating consistent PostgreSQL logical dump..."
"${compose[@]}" exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=9 \
  >"$tmp_dir/postgres.dump"
test -s "$tmp_dir/postgres.dump"
"${compose[@]}" exec -T postgres pg_restore --list \
  <"$tmp_dir/postgres.dump" >/dev/null

echo "Creating a point-in-time Redis broker snapshot..."
mkdir -p "$tmp_dir/broker"
redis_container="$("${compose[@]}" ps -q redis)"
[[ -n "$redis_container" ]] || { echo "Redis container is unavailable" >&2; exit 1; }
docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --user 0:0 --cap-drop ALL --security-opt no-new-privileges \
  --network "container:${redis_container}" \
  -v "$tmp_dir/broker:/backup" \
  --entrypoint redis-cli "$REDIS_IMAGE" \
  -h 127.0.0.1 --rdb /backup/broker.rdb >/dev/null
test -s "$tmp_dir/broker/broker.rdb"
docker run --rm --pull=never --read-only --network none \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$tmp_dir/broker:/backup:ro" \
  --entrypoint redis-check-rdb "$REDIS_IMAGE" /backup/broker.rdb >/dev/null

echo "Creating an object-level MinIO mirror..."
mkdir -p "$tmp_dir/minio"
mkdir -p "$tmp_dir/minio-cluster"
minio_container="$("${compose[@]}" ps -q minio)"
[[ -n "$minio_container" ]] || { echo "MinIO container is unavailable" >&2; exit 1; }
docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --tmpfs /root/.mc:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --network "container:${minio_container}" \
  --env-file "$MINIO_ENV" \
  -v "$tmp_dir/minio:/backup" \
  -v "$tmp_dir/minio-cluster:/cluster" \
  --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ceu \
  'mc alias set source http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
   # This logical backup mirrors current object bytes, not version history.
   # Capture the same current-only view so deleted and superseded versions can
   # never be mistaken for restorable objects.
   mc ls --recursive --json source/ > /cluster/object-inventory.jsonl
   cd /cluster
   mc admin cluster bucket export source >/dev/null
   set -- ./*-bucket-metadata.zip
   [ "$#" -eq 1 ] && [ -f "$1" ]
   mv -- "$1" bucket-metadata.zip
   mc admin cluster iam export source --output /cluster/iam-metadata.zip >/dev/null
   mc mirror --overwrite --remove source/ /backup/'

echo "Staging root-only deployment configuration..."
mkdir -p "$tmp_dir/deployment"
tar -C "$DEPLOY_DIR" --exclude='*.log' --exclude='*.lock' -cf - . \
  | tar -C "$tmp_dir/deployment" -xf -

(
  cd "$tmp_dir"
  find postgres.dump broker minio minio-cluster deployment -type f -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 -r sha256sum
) >"$tmp_dir/SHA256SUMS"

require_backup_capacity staged

echo "Creating one atomic PostgreSQL, MinIO, and configuration snapshot..."
restic_output="$(mktemp "${tmp_parent%/}/.starforge-restic-output.XXXXXX")"
chmod 0600 "$restic_output"
docker run "${restic_container_args[@]}" \
  "${restic_repository_args[@]}" \
  -v "$tmp_dir:/backup:ro" "$RESTIC_IMAGE" \
  backup /backup --host "$RESTIC_HOST" --tag starforge --tag production --json \
  >"$restic_output"
snapshot_id="$(python3 - "$restic_output" <<'PY'
import json
import sys
from pathlib import Path

snapshot_ids = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    if payload.get("message_type") == "summary":
        snapshot_ids.append(payload.get("snapshot_id", ""))
if len(snapshot_ids) != 1:
    raise SystemExit("Restic did not report exactly one final snapshot ID")
print(snapshot_ids[0])
PY
)"
[[ "$snapshot_id" =~ ^[0-9a-f]{64}$ ]] || {
  echo "Restic returned an invalid snapshot ID" >&2
  exit 1
}

if [[ "$BACKUP_MODE" == "local" ]]; then
  restic_run check --read-data
else
  restic_run check --read-data-subset=5%
fi

require_backup_capacity created
marker_tmp="$(mktemp "${DEPLOY_DIR}/.last_created_backup.XXXXXX")"
printf '%s\n' "$snapshot_id" >"$marker_tmp"
chmod 0600 "$marker_tmp"
mv -f -- "$marker_tmp" "${DEPLOY_DIR}/last_created_backup"
metadata_tmp="$(mktemp "${DEPLOY_DIR}/.last_created_backup.meta.XXXXXX")"
{
  printf 'snapshot_id=%s\n' "$snapshot_id"
  printf 'created_at=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  printf 'repository_id=%s\n' "$RESTIC_REPOSITORY_ID"
  printf 'includes=postgres,redis-all-databases,minio-current-objects,minio-policy-iam,deployment\n'
} >"$metadata_tmp"
chmod 0600 "$metadata_tmp"
mv -f -- "$metadata_tmp" "${DEPLOY_DIR}/last_created_backup.meta"
echo "Starforge snapshot $snapshot_id was created and repository-checked; an isolated restore is still required."
