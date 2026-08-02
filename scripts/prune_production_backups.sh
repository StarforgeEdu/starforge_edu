#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

DEPLOY_DIR="${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}"
REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
BACKUP_ENV="${DEPLOY_DIR}/backup.env"
ENV_HELPER="${REPO_DIR}/scripts/lib/production_env.sh"
PLAN_HELPER="${REPO_DIR}/scripts/validate_restic_forget_plan.py"
LOCK_FILE="${DEPLOY_DIR}/backup.lock"

[[ "$EUID" -eq 0 ]] || { echo "Production backup retention must run as root" >&2; exit 1; }
[[ -f "$ENV_HELPER" && ! -L "$ENV_HELPER" ]] || {
  echo "Production environment reader is unavailable" >&2
  exit 1
}
# shellcheck source=scripts/lib/production_env.sh
source "$ENV_HELPER"
[[ -f "$PLAN_HELPER" && ! -L "$PLAN_HELPER" ]] || {
  echo "Restic retention plan validator is unavailable" >&2
  exit 1
}
sf_require_private_root_file "$BACKUP_ENV" || exit 1
for marker in "${DEPLOY_DIR}/last_created_backup" "${DEPLOY_DIR}/last_verified_backup"; do
  sf_require_private_root_file "$marker" || exit 1
done
created_snapshot="$(<"${DEPLOY_DIR}/last_created_backup")"
verified_snapshot="$(<"${DEPLOY_DIR}/last_verified_backup")"
[[ "$created_snapshot" =~ ^[0-9a-f]{64}$ && "$verified_snapshot" =~ ^[0-9a-f]{64}$ ]] || {
  echo "Backup markers must contain exact 64-character snapshot IDs" >&2
  exit 1
}
[[ "$created_snapshot" == "$verified_snapshot" ]] || {
  echo "Retention is refused because the newest created snapshot has not passed restore verification" >&2
  exit 1
}
[[ ! -L "$LOCK_FILE" ]] || { echo "Backup lock must not be a symbolic link" >&2; exit 1; }
exec 8>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
flock -n 8 || { echo "Backup creation or another retention run is active" >&2; exit 1; }
sf_read_env_values "$BACKUP_ENV" values \
  BACKUP_MODE RESTIC_HOST RESTIC_REPOSITORY RESTIC_REPOSITORY_ID RESTIC_IMAGE || exit 1
BACKUP_MODE="${values[0]}"
RESTIC_HOST="${values[1]}"
RESTIC_REPOSITORY="${values[2]}"
RESTIC_REPOSITORY_ID="${values[3]}"
RESTIC_IMAGE="${values[4]}"
sf_require_digest_image RESTIC_IMAGE "$RESTIC_IMAGE" || exit 1
[[ "$RESTIC_REPOSITORY_ID" =~ ^[0-9a-f]{64}$ ]] || {
  echo "RESTIC_REPOSITORY_ID must be the exact 64-character repository ID" >&2
  exit 1
}

repository_args=()
case "$BACKUP_MODE" in
  offsite)
    ;;
  local)
    sf_read_env_values "$BACKUP_ENV" local_values LOCAL_BACKUP_ROOT || exit 1
    local_root="$(realpath -e -- "${local_values[0]}")"
    [[ "$local_root" == "${local_values[0]%/}" && \
       "$(stat -c '%u:%g:%a' "$local_root")" == "0:0:700" ]] || {
      echo "LOCAL_BACKUP_ROOT must be canonical and owned by root:root with mode 0700" >&2
      exit 1
    }
    case "$RESTIC_REPOSITORY" in
      /repository|/repository/*) ;;
      *) echo "Local RESTIC_REPOSITORY must be below /repository" >&2; exit 1 ;;
    esac
    repository_args+=(--network none --mount "type=bind,src=${local_root},dst=/repository")
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
    --env-file "$BACKUP_ENV" "${repository_args[@]}" "$RESTIC_IMAGE" "$@"
}

repository_config="$(restic_run cat config)"
actual_id="$(RESTIC_CONFIG="$repository_config" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["RESTIC_CONFIG"]).get("id", ""))
PY
)"
[[ "$actual_id" == "$RESTIC_REPOSITORY_ID" ]] || {
  echo "Restic repository identity does not match the reviewed production repository" >&2
  exit 1
}

verified_membership="$(restic_run snapshots --json "$verified_snapshot")"
VERIFIED_MEMBERSHIP="$verified_membership" VERIFIED_SNAPSHOT="$verified_snapshot" python3 - <<'PY'
import json
import os

rows = json.loads(os.environ["VERIFIED_MEMBERSHIP"])
if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("id") != os.environ["VERIFIED_SNAPSHOT"]:
    raise SystemExit("The verified snapshot marker is not an exact member of this Restic repository")
PY

plan_file="$(mktemp "${DEPLOY_DIR}/restic-forget-plan.XXXXXX")"
remove_file="$(mktemp "${DEPLOY_DIR}/restic-forget-remove.XXXXXX")"
cleanup_retention_plan() {
  rm -f -- "$plan_file" "$remove_file"
}
trap cleanup_retention_plan EXIT

retention_policy=(
  --host "$RESTIC_HOST"
  --tag starforge
  --group-by host,paths
  --keep-last 5
  --keep-daily 14
  --keep-weekly 8
  --keep-monthly 12
)
restic_run forget "${retention_policy[@]}" --dry-run --json >"$plan_file"
python3 "$PLAN_HELPER" \
  --plan "$plan_file" \
  --verified-snapshot "$verified_snapshot" >"$remove_file"

mapfile -t snapshots_to_remove <"$remove_file"
for snapshot_to_remove in "${snapshots_to_remove[@]}"; do
  [[ "$snapshot_to_remove" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Retention plan validator returned an invalid snapshot ID" >&2
    exit 1
  }
done
for ((offset = 0; offset < ${#snapshots_to_remove[@]}; offset += 100)); do
  batch=("${snapshots_to_remove[@]:offset:100}")
  restic_run forget "${batch[@]}"
done
restic_run prune

post_prune_membership="$(restic_run snapshots --json "$verified_snapshot")"
VERIFIED_MEMBERSHIP="$post_prune_membership" VERIFIED_SNAPSHOT="$verified_snapshot" python3 - <<'PY'
import json
import os

rows = json.loads(os.environ["VERIFIED_MEMBERSHIP"])
if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("id") != os.environ["VERIFIED_SNAPSHOT"]:
    raise SystemExit("The verified snapshot did not survive production backup retention")
PY

restic_run check --read-data-subset=5%
echo "Production backup retention completed after verified-snapshot promotion."
