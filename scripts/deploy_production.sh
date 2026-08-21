#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

revision="${1:-}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || {
  echo "usage: $0 <exact-40-character-commit-sha>" >&2
  exit 2
}

REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
DEPLOY_DIR="${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}"
RELEASE_ROOT="${STARFORGE_RELEASE_ROOT:-/root/starforge-releases}"
COMPOSE_ENV="${DEPLOY_DIR}/compose.env"
LOCK_FILE="${DEPLOY_DIR}/deploy.lock"
LEADERSHIP_SMOKE_CONFIG="${DEPLOY_DIR}/leadership-smoke.json"
HEALTH_URL="${STARFORGE_HEALTH_URL:-https://starforge.78.111.91.113.nip.io/healthz/ready}"
APPROVED_REMOTE_REF="${STARFORGE_APPROVED_REMOTE_REF:-refs/remotes/origin/codex/permission-audit-release}"
MAINTENANCE_CUTOVER_ACK="${STARFORGE_MAINTENANCE_CUTOVER:-}"
REVIEWED_BACKFILL_SHA256="${STARFORGE_REVIEWED_BACKFILL_SHA256:-}"
RESUME_FAILED_CUTOVER_ACK="${STARFORGE_RESUME_FAILED_CUTOVER:-}"
MIGRATION_EVIDENCE="${DEPLOY_DIR}/migration-cutover.evidence"

[[ "${STARFORGE_BOOTSTRAP_REVISION:-}" == "$revision" && \
   "${STARFORGE_BOOTSTRAP_DEPLOY_BLOB:-}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Production deploy must be launched through launch_production_deploy.sh" >&2
  exit 78
}
expected_deploy_blob="$(git -C "$REPO_DIR" rev-parse "${revision}:scripts/deploy_production.sh")"
actual_deploy_blob="$(git -C "$REPO_DIR" hash-object "${BASH_SOURCE[0]}")"
[[ "$expected_deploy_blob" == "$actual_deploy_blob" && \
   "$expected_deploy_blob" == "$STARFORGE_BOOTSTRAP_DEPLOY_BLOB" ]] || {
  echo "Running deploy orchestration does not match the exact approved revision" >&2
  exit 78
}

app_services=(web daphne worker-critical worker-default worker-long beat)
stateful_services=(postgres redis minio)
declare -A stateful_volume_destinations=(
  [postgres]="/var/lib/postgresql/data"
  [redis]="/data"
  [minio]="/data"
)
declare -A stateful_volume_names=(
  [postgres]="sf_pg"
  [redis]="sf_redis"
  [minio]="sf_minio"
)
apps_quiesced=0
schema_change_started=0
candidate_healthy=0
maintenance_enabled=0
intentional_review_pause=0
resume_review=0
resume_migration=0
resume_phase=""
cutover_required=0
previous_image=""
previous_image_id=""
candidate_image_id=""
verified_backup_snapshot=""
review_digest=""
compose=()
previous_app_containers=()

die() {
  echo "$1" >&2
  exit "${2:-1}"
}

require_private_root_file() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || die "Required deployment file is unavailable: $path"
  [[ "$(stat -c '%u' "$path")" == "0" ]] || die "Deployment file must be owned by root: $path"
  local mode
  mode="$(stat -c '%a' "$path")"
  [[ "$mode" == "600" || "$mode" == "400" ]] || {
    die "Deployment file must use mode 0600 or 0400: $path"
  }
}

marker_value() {
  local marker="$1" key="$2"
  sed -n "s/^${key}=//p" "$marker" | tail -n 1
}

atomic_marker() {
  local destination="$1"
  shift
  local temporary
  temporary="$(mktemp "${DEPLOY_DIR}/.$(basename "$destination").XXXXXX")"
  printf '%s\n' "$@" >"$temporary"
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$destination"
}

atomic_migration_evidence() {
  local temporary
  temporary="$(mktemp "${DEPLOY_DIR}/.migration-cutover.evidence.XXXXXX")"
  printf '%s\n' "$@" >"$temporary"
  chmod 0644 "$temporary"
  mv -f -- "$temporary" "$MIGRATION_EVIDENCE"
}

[[ "$EUID" -eq 0 ]] || die "Production deployment must run as root"
[[ -d "$REPO_DIR/.git" ]] || die "Deployment repository is unavailable"
require_private_root_file "$COMPOSE_ENV"
require_private_root_file "${DEPLOY_DIR}/static-storage.env"
[[ ! -L "$DEPLOY_DIR" && ! -L "$RELEASE_ROOT" ]] || die "Deployment roots must not be symbolic links"
install -d -o root -g root -m 0700 -- "$DEPLOY_DIR" "$RELEASE_ROOT"
if [[ ! -e "$MIGRATION_EVIDENCE" ]]; then
  atomic_migration_evidence "status=disabled"
fi
[[ -f "$MIGRATION_EVIDENCE" && ! -L "$MIGRATION_EVIDENCE" && \
   "$(stat -c '%u:%a' "$MIGRATION_EVIDENCE")" == "0:644" ]] || {
  die "Migration cutover evidence must be a root-owned mode-0644 regular file"
}
[[ "$APPROVED_REMOTE_REF" =~ ^refs/remotes/origin/[A-Za-z0-9._/-]+$ ]] && \
  [[ "$APPROVED_REMOTE_REF" != *".."* && "$APPROVED_REMOTE_REF" != *"//"* ]] || {
  die "STARFORGE_APPROVED_REMOTE_REF must be one normalized origin remote-tracking ref"
}
HEALTH_URL_VALUE="$HEALTH_URL" python3 -c '
import os
import sys
from urllib.parse import urlsplit

value = os.environ["HEALTH_URL_VALUE"]
parsed = urlsplit(value)
valid = (
    parsed.scheme == "https"
    and bool(parsed.hostname)
    and parsed.username is None
    and parsed.password is None
    and not parsed.fragment
    and parsed.path.startswith("/")
)
raise SystemExit(0 if valid else 1)
' || die "STARFORGE_HEALTH_URL must be one credential-free HTTPS URL"

# Compose needs one stable root-only Firebase JSON path even when push is
# disabled. Production settings reject the inert empty object if push is ever
# enabled by mistake.
firebase_credentials="${DEPLOY_DIR}/firebase.json"
[[ ! -L "$firebase_credentials" ]] || die "Firebase credentials must not be a symbolic link"
if [[ ! -e "$firebase_credentials" ]]; then
  printf '{}\n' >"$firebase_credentials"
  chmod 0600 "$firebase_credentials"
fi
require_private_root_file "$firebase_credentials"
python3 -c 'import json, sys; value = json.load(open(sys.argv[1])); raise SystemExit(0 if isinstance(value, dict) else 1)' \
  "$firebase_credentials" || die "Firebase credentials must contain one JSON object"

[[ ! -L "$LOCK_FILE" ]] || die "Deployment lock must not be a symbolic link"
exec 9>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
flock -n 9 || die "Another deployment is already running"

git -C "$REPO_DIR" fetch --prune origin
sha="$(git -C "$REPO_DIR" rev-parse --verify "${revision}^{commit}")"
[[ "$sha" == "$revision" ]] || die "Revision did not resolve to the exact requested commit"
git -C "$REPO_DIR" show-ref --verify --quiet "$APPROVED_REMOTE_REF" || {
  die "Approved remote release ref is unavailable: $APPROVED_REMOTE_REF"
}
git -C "$REPO_DIR" merge-base --is-ancestor "$sha" "$APPROVED_REMOTE_REF" || {
  die "Requested revision is not reachable from the approved remote release ref"
}

short_sha="${sha:0:12}"
release_dir="${RELEASE_ROOT}/${sha}"
image="starforge:${sha}"
evidence_dir="${DEPLOY_DIR}/release-evidence/${sha}"
review_marker="${DEPLOY_DIR}/cutover_review_pending"
failure_marker="${DEPLOY_DIR}/cutover_failed"
storage_marker="${evidence_dir}/storage-verified.manifest"
[[ ! -L "${DEPLOY_DIR}/release-evidence" && ! -L "$evidence_dir" ]] || {
  die "Release evidence paths must not be symbolic links"
}
install -d -o root -g root -m 0700 -- "${DEPLOY_DIR}/release-evidence" "$evidence_dir"

if [[ -e "$failure_marker" ]]; then
  require_private_root_file "$failure_marker"
  marker_revision="$(marker_value "$failure_marker" candidate_revision)"
  [[ "$marker_revision" == "$sha" ]] || {
    die "A different release has an unfinished failed cutover; recover it first" 78
  }
  [[ "$RESUME_FAILED_CUTOVER_ACK" == "$sha" ]] || {
    die "Forward recovery requires STARFORGE_RESUME_FAILED_CUTOVER=${sha}" 78
  }
  resume_migration=1
  apps_quiesced=1
  schema_change_started=1
  cutover_required=1
  previous_image="$(marker_value "$failure_marker" previous_image)"
  previous_image_id="$(marker_value "$failure_marker" previous_image_id)"
  candidate_image_id="$(marker_value "$failure_marker" candidate_image_id)"
  verified_backup_snapshot="$(marker_value "$failure_marker" verified_backup_snapshot)"
  [[ "$candidate_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid candidate image in failure marker" 78
  [[ "$verified_backup_snapshot" =~ ^[0-9a-f]{64}$ ]] || die "Invalid backup in failure marker" 78
elif [[ -e "$review_marker" ]]; then
  require_private_root_file "$review_marker"
  marker_revision="$(marker_value "$review_marker" revision)"
  [[ "$marker_revision" == "$sha" ]] || {
    die "A different release has an unfinished post-migration review; recover it first" 78
  }
  resume_review=1
  resume_phase="$(marker_value "$review_marker" phase)"
  [[ "$resume_phase" == "review_pending" || "$resume_phase" == "apply_started" || "$resume_phase" == "backfills_applied" ]] || {
    die "Invalid phase in review marker" 78
  }
  apps_quiesced=1
  schema_change_started=1
  cutover_required=1
  previous_image="$(marker_value "$review_marker" previous_image)"
  previous_image_id="$(marker_value "$review_marker" previous_image_id)"
  candidate_image_id="$(marker_value "$review_marker" candidate_image_id)"
  verified_backup_snapshot="$(marker_value "$review_marker" verified_backup_snapshot)"
  review_digest="$(marker_value "$review_marker" review_sha256)"
  [[ "$candidate_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid candidate image in review marker" 78
  [[ "$verified_backup_snapshot" =~ ^[0-9a-f]{64}$ ]] || die "Invalid backup in review marker" 78
  [[ "$review_digest" =~ ^[0-9a-f]{64}$ ]] || die "Invalid evidence digest in review marker" 78
fi

check_ci() {
  : "${GITHUB_TOKEN:?GITHUB_TOKEN is required to verify CI}"
  [[ "$GITHUB_TOKEN" =~ ^[A-Za-z0-9_]{20,255}$ ]] || {
    die "GITHUB_TOKEN has an invalid format"
  }
  # Keep the bearer out of argv and persistent storage: /proc/<pid>/cmdline is
  # commonly world-readable, and a temporary file can survive SIGKILL and then
  # enter a configuration backup. Bash exposes this anonymous pipe as /dev/fd;
  # only the curl process receives the read descriptor.
  local status=0
  curl -fsS --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 30 \
    --config <(printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN") \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/StarforgeEdu/starforge_edu/commits/${sha}/check-runs?per_page=100" \
    | python3 -c '
import json
import sys

runs = json.load(sys.stdin).get("check_runs", [])
expected_sha = sys.argv[1]
required = {
    "secret-scan",
    "lint",
    "typecheck",
    "test",
    "schema",
    "dependency-audit",
    "container-smoke",
}
latest = {}
for run in runs:
    if run.get("head_sha") != expected_sha or (run.get("app") or {}).get("slug") != "github-actions":
        continue
    name = run.get("name")
    current = latest.get(name)
    if current is None or (run.get("completed_at") or "") > (current.get("completed_at") or ""):
        latest[name] = run
missing = required - latest.keys()
failed = {
    name: (latest[name].get("status"), latest[name].get("conclusion"))
    for name in required & latest.keys()
    if latest[name].get("status") != "completed" or latest[name].get("conclusion") != "success"
}
if missing or failed:
    print(f"CI gate failed; missing={sorted(missing)} failed={failed}", file=sys.stderr)
    raise SystemExit(1)
' "$sha" || status="$?"
  return "$status"
}

cleanup_worktree() {
  if [[ -d "$release_dir" ]]; then
    git -C "$REPO_DIR" worktree remove --force "$release_dir" >/dev/null 2>&1 || true
  fi
}

write_cutover_failure_marker() {
  atomic_marker "${DEPLOY_DIR}/cutover_failed" \
    "candidate_revision=${sha}" \
    "candidate_image=${image}" \
    "candidate_image_id=${candidate_image_id}" \
    "release_helpers_sha256=${helper_manifest_sha256}" \
    "previous_image=${previous_image}" \
    "previous_image_id=${previous_image_id}" \
    "verified_backup_snapshot=${verified_backup_snapshot}"
}

on_exit() {
  local status="$?"
  trap - EXIT
  set +e
  if [[ "$status" -ne 0 && "$apps_quiesced" == "1" && "${#compose[@]}" -gt 0 ]]; then
    if [[ "$schema_change_started" == "0" ]]; then
      if [[ "${#previous_app_containers[@]}" -gt 0 ]]; then
        echo "Cutover stopped before migrations; restarting the unchanged previous containers." >&2
        docker start "${previous_app_containers[@]}" >&2
        if [[ "$maintenance_enabled" == "1" ]]; then
          previous_ready=0
          for _ in $(seq 1 36); do
            if curl -fsS --proto '=https' --tlsv1.2 --connect-timeout 5 --max-time 15 \
              "$HEALTH_URL" >/dev/null; then
              previous_ready=1
              break
            fi
            sleep 5
          done
          if [[ "$previous_ready" == "1" ]] && \
             STARFORGE_REPO_DIR="$release_dir" \
               "$release_dir/scripts/set_production_maintenance.sh" disable >&2; then
            maintenance_enabled=0
          else
            echo "Previous release did not prove readiness; maintenance remains enabled." >&2
          fi
        fi
      else
        echo "Cutover stopped before migrations; there were no prior application containers to restart." >&2
      fi
    else
      quiesce_project_applications 0 >/dev/null 2>&1 || true
      if [[ "$intentional_review_pause" == "0" ]]; then
        write_cutover_failure_marker
        echo "Cutover failed after migration started; application services remain stopped." >&2
      fi
      echo "Do not start the old image against the migrated schema." >&2
    fi
  fi
  cleanup_worktree
  exit "$status"
}
trap on_exit EXIT

check_ci
[[ ! -e "$release_dir" ]] || die "Disposable release worktree path already exists: $release_dir"
git -C "$REPO_DIR" worktree add --detach "$release_dir" "$sha"

helper_manifest="${evidence_dir}/release-helpers.sha256"
helper_manifest_tmp="${helper_manifest}.tmp"
helper_paths=(
  docker/Dockerfile
  docker/docker-compose.production.yml
  docker/entrypoint.sh
  scripts/backup_production.sh
  scripts/capture_broker_depth.py
  scripts/deploy_production.sh
  scripts/drain_celery_for_release.py
  scripts/launch_production_deploy.sh
  scripts/run_leadership_release_smoke.py
  scripts/set_production_maintenance.sh
  scripts/storage_iam_contract.py
  scripts/verify_minio_restore.py
  scripts/verify_restore.sh
  scripts/verify_production_storage.sh
  scripts/lib/production_env.sh
)
for helper_path in "${helper_paths[@]}"; do
  [[ -f "${release_dir}/${helper_path}" && ! -L "${release_dir}/${helper_path}" ]] || {
    die "Release helper is unavailable: $helper_path"
  }
done
(cd "$release_dir" && sha256sum -- "${helper_paths[@]}") >"$helper_manifest_tmp"
chmod 0600 "$helper_manifest_tmp"
mv -f -- "$helper_manifest_tmp" "$helper_manifest"
helper_manifest_sha256="$(sha256sum "$helper_manifest" | awk '{print $1}')"
[[ "$helper_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Release helper manifest is invalid"

release_env_helper="${release_dir}/scripts/lib/production_env.sh"
[[ -f "$release_env_helper" && ! -L "$release_env_helper" ]] || {
  die "Approved release environment reader is unavailable"
}
# shellcheck source=scripts/lib/production_env.sh
source "$release_env_helper"
sf_clear_compose_process_overrides
sf_export_compose_infrastructure_images "$COMPOSE_ENV" || {
  die "Reviewed stateful image configuration is invalid"
}
require_private_root_file "$LEADERSHIP_SMOKE_CONFIG"
python3 "$release_dir/scripts/run_leadership_release_smoke.py" \
  --config "$LEADERSHIP_SMOKE_CONFIG" \
  --expected-revision "$sha" \
  --validate-only >"${evidence_dir}/leadership-smoke-preflight.json"
chmod 0600 "${evidence_dir}/leadership-smoke-preflight.json"

if [[ "$resume_review" == "1" || "$resume_migration" == "1" ]]; then
  built_revision="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
  current_image_id="$(docker image inspect "$image" --format '{{.Id}}')"
  [[ "$built_revision" == "$sha" && "$current_image_id" == "$candidate_image_id" ]] || {
    die "The reviewed candidate image is unavailable or was replaced" 78
  }
else
  echo "Building immutable application image $image..."
  docker build --pull \
    --build-arg "VCS_REF=$sha" \
    -f "$release_dir/docker/Dockerfile" \
    -t "$image" \
    "$release_dir"
  built_revision="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
  candidate_image_id="$(docker image inspect "$image" --format '{{.Id}}')"
  built_env_revision="$(docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sed -n 's/^STARFORGE_IMAGE_REVISION=//p')"
  [[ "$built_revision" == "$sha" && "$built_env_revision" == "$sha" ]] || {
    die "Image revision provenance does not match the approved commit"
  }
  [[ "$candidate_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Docker returned an invalid image ID"
fi

export APP_IMAGE="$image"
export STARFORGE_RELEASE_REVISION="$sha"
export STARFORGE_CANDIDATE_IMAGE_ID="$candidate_image_id"
export STARFORGE_RELEASE_HELPERS_SHA256="$helper_manifest_sha256"
export STARFORGE_DEPLOY_DIR="$DEPLOY_DIR"
compose=(docker compose --env-file "$COMPOSE_ENV" -f "$release_dir/docker/docker-compose.production.yml")

mapfile -t configured_images < <("${compose[@]}" config --images | sort -u)
for configured_image in "${configured_images[@]}"; do
  if [[ "$configured_image" == "$image" ]]; then
    continue
  fi
  [[ "$configured_image" =~ @sha256:[0-9a-f]{64}$ ]] || {
    die "Infrastructure image is not pinned by digest: $configured_image"
  }
done

project_name="$("${compose[@]}" config --format json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
[[ "$project_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || die "Invalid Compose project name"
[[ "$project_name" == "starforge" ]] || die "Production Compose project name must remain starforge"

declare -A configured_stateful_images=()
while IFS=$'\t' read -r service configured_image; do
  [[ -n "$service" && -n "$configured_image" ]] || die "Stateful Compose image configuration is incomplete"
  configured_stateful_images["$service"]="$configured_image"
done < <(
  "${compose[@]}" config --format json | python3 -c '
import json
import sys

document = json.load(sys.stdin)
for service in ("postgres", "redis", "minio"):
    image = (document.get("services", {}).get(service, {}) or {}).get("image")
    if not isinstance(image, str) or not image:
        raise SystemExit(f"Missing image for stateful service {service}")
    print(f"{service}\t{image}")
'
)
[[ "${#configured_stateful_images[@]}" == "${#stateful_services[@]}" ]] || {
  die "Every stateful service must have one configured image"
}

is_stateful_service() {
  local candidate="$1" service
  for service in "${stateful_services[@]}"; do
    [[ "$candidate" != "$service" ]] || return 0
  done
  return 1
}

load_running_project_applications() {
  running_project_applications=()
  local container_ids container_id service
  container_ids="$(docker ps -q --filter "label=com.docker.compose.project=${project_name}")" || return 1
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    service="$(docker inspect "$container_id" \
      --format '{{ index .Config.Labels "com.docker.compose.service" }}')" || return 1
    if ! is_stateful_service "$service"; then
      running_project_applications+=("$container_id")
    fi
  done <<<"$container_ids"
}

record_running_project_applications() {
  local container_id
  load_running_project_applications || return 1
  for container_id in "${running_project_applications[@]}"; do
    append_previous_container_once "$container_id"
  done
}

classify_running_project_applications() {
  producer_containers=()
  worker_containers=()
  local container_id command_json
  load_running_project_applications || return 1
  for container_id in "${running_project_applications[@]}"; do
    command_json="$(docker inspect "$container_id" --format '{{json .Config.Cmd}}')" || return 1
    if [[ "$command_json" == '["worker"]' ]]; then
      worker_containers+=("$container_id")
    else
      producer_containers+=("$container_id")
    fi
  done
}

append_previous_container_once() {
  local candidate="$1" recorded
  for recorded in "${previous_app_containers[@]}"; do
    [[ "$candidate" != "$recorded" ]] || return 0
  done
  previous_app_containers+=("$candidate")
}

quiesce_project_applications() {
  local capture_previous="$1" attempt container_id
  for attempt in 1 2 3; do
    load_running_project_applications || return 1
    [[ "${#running_project_applications[@]}" -gt 0 ]] || return 0
    if [[ "$capture_previous" == "1" ]]; then
      for container_id in "${running_project_applications[@]}"; do
        append_previous_container_once "$container_id"
      done
    fi
    docker stop --time 120 "${running_project_applications[@]}" >/dev/null || return 1
  done
  load_running_project_applications || return 1
  [[ "${#running_project_applications[@]}" == "0" ]]
}

container_environment_digest() {
  docker inspect "$1" --format '{{json .Config.Env}}' | python3 -c '
import hashlib
import json
import sys

values = json.load(sys.stdin) or []
payload = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
'
}

stateful_reference_contract() (
  set -Eeuo pipefail
  local service="$1"
  local reference_project="starforge-verify-${service}-$$-${RANDOM}"
  local reference_container reference_config_hash reference_environment_digest

  cleanup_reference() {
    local status="$?" cleanup_status
    trap - EXIT INT TERM
    set +e
    "${compose[@]}" --project-name "$reference_project" \
      down --volumes --remove-orphans >/dev/null 2>&1
    cleanup_status="$?"
    if [[ "$status" == "0" && "$cleanup_status" != "0" ]]; then
      exit 1
    fi
    exit "$status"
  }
  trap cleanup_reference EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  "${compose[@]}" --project-name "$reference_project" \
    create --no-build "$service" >/dev/null
  reference_container="$(
    "${compose[@]}" --project-name "$reference_project" ps -aq "$service"
  )"
  [[ -n "$reference_container" ]] || return 1
  reference_config_hash="$(docker inspect "$reference_container" \
    --format '{{ index .Config.Labels "com.docker.compose.config-hash" }}')"
  reference_environment_digest="$(container_environment_digest "$reference_container")"
  [[ "$reference_config_hash" =~ ^[0-9a-f]{64}$ && \
     "$reference_environment_digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\t%s\n' "$reference_config_hash" "$reference_environment_digest"
)

verify_stateful_infrastructure() {
  local service expected_image expected_image_id container_ids container_id actual_image_id state health
  local expected_config_hash config_hash_line actual_config_hash published_ports mount_manifest
  local actual_environment_digest reference_contract reference_config_hash reference_environment_digest
  local expected_volume_name expected_volume_destination
  local -a service_containers
  for service in "${stateful_services[@]}"; do
    expected_image="${configured_stateful_images[$service]}"
    [[ "$expected_image" =~ @sha256:[0-9a-f]{64}$ ]] || {
      die "Stateful service $service is not configured with an immutable image digest"
    }
    expected_image_id="$(docker image inspect "$expected_image" --format '{{.Id}}')" || {
      die "Configured stateful image is unavailable locally: $service"
    }
    service_containers=()
    container_ids="$(docker ps -q \
      --filter "label=com.docker.compose.project=${project_name}" \
      --filter "label=com.docker.compose.service=${service}")" || {
      die "Cannot inspect running stateful service $service"
    }
    while IFS= read -r container_id; do
      [[ -z "$container_id" ]] || service_containers+=("$container_id")
    done <<<"$container_ids"
    [[ "${#service_containers[@]}" == "1" ]] || {
      die "Stateful service $service must already have exactly one running container; provision it separately"
    }
    container_id="${service_containers[0]}"
    actual_image_id="$(docker inspect "$container_id" --format '{{.Image}}')"
    [[ "$actual_image_id" == "$expected_image_id" ]] || {
      die "Stateful service $service does not use the approved configured image; use the infrastructure change runbook"
    }
    state="$(docker inspect "$container_id" --format '{{.State.Status}}')"
    health="$(docker inspect "$container_id" --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')"
    [[ "$state" == "running" && "$health" == "healthy" ]] || {
      die "Stateful service $service must be running and healthy before an application release"
    }
    config_hash_line="$("${compose[@]}" config --hash "$service")"
    expected_config_hash="${config_hash_line#${service} }"
    [[ "$expected_config_hash" =~ ^[0-9a-f]{64}$ ]] || {
      die "Cannot resolve the reviewed Compose configuration hash for $service"
    }
    actual_config_hash="$(docker inspect "$container_id" \
      --format '{{ index .Config.Labels "com.docker.compose.config-hash" }}')"
    if [[ "$actual_config_hash" != "$expected_config_hash" ]]; then
      # Some Compose releases calculate `config --hash` from the fully
      # resolved env_file model but label containers from the pre-resolution
      # service model. Ask the same Compose binary to create a stopped,
      # isolated reference container and compare the label it would actually
      # apply. The distinct project gets empty disposable volumes and cannot
      # attach to production state.
      if ! reference_contract="$(stateful_reference_contract "$service")"; then
        die "Cannot verify an isolated Compose reference for stateful service $service"
      fi
      IFS=$'\t' read -r reference_config_hash reference_environment_digest <<<"$reference_contract"
      actual_environment_digest="$(container_environment_digest "$container_id")"
      [[ "$actual_config_hash" == "$reference_config_hash" && \
         "$actual_environment_digest" == "$reference_environment_digest" ]] || {
        die "Stateful service $service configuration differs from the reviewed Compose definition"
      }
    fi
    published_ports="$(docker inspect "$container_id" --format '{{json .HostConfig.PortBindings}}')"
    [[ "$published_ports" == "{}" || "$published_ports" == "null" ]] || {
      die "Stateful service $service unexpectedly publishes a host port"
    }
    expected_volume_name="${project_name}_${stateful_volume_names[$service]}"
    expected_volume_destination="${stateful_volume_destinations[$service]}"
    mount_manifest="$(docker inspect "$container_id" --format \
      '{{range .Mounts}}{{println .Type .Name .Destination .RW}}{{end}}')"
    [[ "$mount_manifest" == "volume ${expected_volume_name} ${expected_volume_destination} true" ]] || {
      die "Stateful service $service does not use its one reviewed writable named-volume mount"
    }
  done
}

capture_web_command() {
  local destination="$1"
  shift
  local temporary="${destination}.tmp"
  rm -f -- "$temporary"
  if ! "${compose[@]}" run --rm --no-deps -T \
    -e "STARFORGE_RELEASE_REVISION=${sha}" web "$@" >"$temporary"; then
    chmod 0600 "$temporary"
    mv -f -- "$temporary" "${destination}.failed"
    return 1
  fi
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$destination"
}

capture_preflight() {
  local destination="$1"
  local temporary="${destination}.tmp"
  rm -f -- "$temporary"
  if ! "${compose[@]}" --profile tools run --rm --no-deps -T release-preflight >"$temporary"; then
    chmod 0600 "$temporary"
    mv -f -- "$temporary" "${destination}.failed"
    return 1
  fi
  chmod 0600 "$temporary"
  mv -f -- "$temporary" "$destination"
}

capture_notification_command() {
  local report_destination="$1" summary_destination="$2"
  shift 2
  local report_name summary_temporary
  report_name="$(basename "$report_destination")"
  [[ "$report_name" =~ ^[a-z0-9][a-z0-9.-]+\.json$ ]] || die "Invalid notification evidence name"
  summary_temporary="${summary_destination}.tmp"
  rm -f -- "$summary_temporary"
  if ! "${compose[@]}" run --rm --no-deps -T --user 0:0 \
    -v "${evidence_dir}:/release-evidence" \
    -e "STARFORGE_RELEASE_REVISION=${sha}" web \
    python manage.py backfill_notification_principals \
    --report "/release-evidence/${report_name}" --no-color "$@" >"$summary_temporary"; then
    chmod 0600 "$summary_temporary"
    mv -f -- "$summary_temporary" "${summary_destination}.failed"
    return 1
  fi
  [[ -f "$report_destination" && ! -L "$report_destination" ]] || {
    die "Notification command did not create its private evidence report"
  }
  chown root:root "$report_destination"
  chmod 0600 "$report_destination" "$summary_temporary"
  mv -f -- "$summary_temporary" "$summary_destination"
}

capture_broker_depth() {
  local destination="$1"
  shift
  capture_web_command "$destination" \
    python scripts/capture_broker_depth.py "$@"
}

drain_project_applications() {
  local capture_previous="$1" evidence_prefix="$2" worker_count
  local expected_workers actual_workers worker_container
  local -a drained_worker_containers
  if [[ "$capture_previous" == "1" ]]; then
    record_running_project_applications || return 1
  fi
  classify_running_project_applications || return 1

  # Stop every non-worker producer by its inspected container command. This
  # catches renamed/orphan web, ASGI, beat, and one-off services without
  # trusting a hard-coded Compose service list. Workers stay alive to finish
  # active/reserved/ETA work under the configured hard time limit.
  if [[ "${#producer_containers[@]}" -gt 0 ]]; then
    docker stop --time 120 "${producer_containers[@]}" >/dev/null || return 1
  fi
  classify_running_project_applications || return 1
  [[ "${#producer_containers[@]}" == "0" ]] || return 1
  worker_count="${#worker_containers[@]}"

  if (( worker_count > 0 )); then
    drained_worker_containers=("${worker_containers[@]}")
    capture_web_command "${evidence_prefix}-celery-drain.json" \
      python scripts/drain_celery_for_release.py --expected-workers "$worker_count" || return 1
    # A newly appeared worker could have early-acknowledged work after the
    # stable observation. Re-discover by command and require the exact same
    # running container set before stopping the workers proven idle.
    classify_running_project_applications || return 1
    [[ "${#producer_containers[@]}" == "0" ]] || return 1
    expected_workers="$(printf '%s\n' "${drained_worker_containers[@]}" | LC_ALL=C sort)"
    actual_workers="$(printf '%s\n' "${worker_containers[@]}" | LC_ALL=C sort)"
    [[ "$actual_workers" == "$expected_workers" ]] || return 1
    for worker_container in "${worker_containers[@]}"; do
      [[ "$(docker inspect "$worker_container" --format '{{.State.Status}}')" == "running" ]] || {
        return 1
      }
    done
    # The workers have proved stable-idle; stopping them cannot terminate work.
    docker stop --time 120 "${worker_containers[@]}" >/dev/null || return 1
  fi

  # Catch any project process that appeared during the drain, then prove the
  # broker has no ready or unacknowledged task before backup/migration.
  quiesce_project_applications 0 || return 1
  capture_broker_depth "${evidence_prefix}-broker-empty.json" --require-empty
}

pin_persistent_candidate_image() {
  COMPOSE_ENV_PATH="$COMPOSE_ENV" CANDIDATE_IMAGE="$image" python3 - <<'PY'
import os
import stat
import tempfile
from pathlib import Path

path = Path(os.environ["COMPOSE_ENV_PATH"])
candidate = os.environ["CANDIDATE_IMAGE"]
metadata = path.lstat()
if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
    raise SystemExit("compose.env is not a root-owned regular file")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
matches = [index for index, line in enumerate(lines) if line.startswith("APP_IMAGE=")]
if len(matches) != 1:
    raise SystemExit("compose.env must contain exactly one APP_IMAGE assignment")
line_ending = "\r\n" if lines[matches[0]].endswith("\r\n") else "\n"
lines[matches[0]] = f"APP_IMAGE={candidate}{line_ending}"
descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".compose.env.")
try:
    os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        descriptor = -1
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_name, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
PY
  require_private_root_file "$COMPOSE_ENV"
}

apply_candidate_migrations() {
  export STARFORGE_MAINTENANCE_CUTOVER="$sha"
  STARFORGE_REPO_DIR="$release_dir" \
    "$release_dir/scripts/set_production_maintenance.sh" assert-enabled
  [[ -f "$migration_broker_evidence" && ! -L "$migration_broker_evidence" ]] || {
    die "Quiesced broker evidence is unavailable" 78
  }
  atomic_migration_evidence \
    "status=authorized" \
    "revision=${sha}" \
    "candidate_image_id=${candidate_image_id}" \
    "helpers_sha256=${helper_manifest_sha256}" \
    "verified_backup_snapshot=${verified_backup_snapshot}" \
    "broker_evidence_sha256=$(sha256sum "$migration_broker_evidence" | awk '{print $1}')"
  # Persist the candidate before the first schema write. A generic future
  # `docker compose up` must resolve to the forward-compatible image, never the
  # stopped pre-cutover tag. The verified backup contains the previous file.
  pin_persistent_candidate_image
  # From this point forward, even an apparently reversible migration may have
  # committed in one tenant. Never start the preceding image automatically.
  schema_change_started=1
  "${compose[@]}" --profile tools run --rm --no-deps -T migrate
  local state
  state="$(
    "${compose[@]}" --profile tools run --rm --no-deps -T cutover-check | tail -n 1 | tr -d '\r'
  )"
  [[ "$state" == "clear" ]] || die "Maintenance migrations did not complete for every tenant schema" 78
  atomic_migration_evidence "status=disabled"
}

review_set_digest() {
  local notification_file="$1" finance_file="$2" audit_file="$3" workflow_file="$4" ai_file="$5"
  {
    printf 'notification %s\n' "$(sha256sum "$notification_file" | awk '{print $1}')"
    printf 'finance %s\n' "$(sha256sum "$finance_file" | awk '{print $1}')"
    printf 'audit %s\n' "$(sha256sum "$audit_file" | awk '{print $1}')"
    printf 'workflow %s\n' "$(sha256sum "$workflow_file" | awk '{print $1}')"
    printf 'ai %s\n' "$(sha256sum "$ai_file" | awk '{print $1}')"
  } | sha256sum | awk '{print $1}'
}

generate_backfill_review() {
  local phase="$1"
  local notification_file="${evidence_dir}/${phase}-notification-principals.json"
  local notification_summary="${evidence_dir}/${phase}-notification-principals-summary.jsonl"
  local finance_file="${evidence_dir}/${phase}-finance-attribution.json"
  local audit_file="${evidence_dir}/${phase}-audit-scopes.jsonl"
  local workflow_file="${evidence_dir}/${phase}-workflow-attribution.jsonl"
  local ai_file="${evidence_dir}/${phase}-ai-attribution.jsonl"

  capture_notification_command "$notification_file" "$notification_summary"
  capture_web_command "$finance_file" \
    python manage.py backfill_finance_attribution --no-color
  capture_web_command "$audit_file" \
    python manage.py backfill_audit_scopes --no-color
  capture_web_command "$workflow_file" \
    python manage.py check_workflow_principal_attribution --no-color
  capture_web_command "$ai_file" \
    python manage.py check_ai_attribution --fail-on-expired-content --no-color

  review_digest="$(review_set_digest \
    "$notification_file" "$finance_file" "$audit_file" "$workflow_file" "$ai_file")"
  atomic_marker "${evidence_dir}/${phase}-review.manifest" \
    "revision=${sha}" \
    "candidate_image_id=${candidate_image_id}" \
    "review_sha256=${review_digest}" \
    "notification_sha256=$(sha256sum "$notification_file" | awk '{print $1}')" \
    "finance_sha256=$(sha256sum "$finance_file" | awk '{print $1}')" \
    "audit_sha256=$(sha256sum "$audit_file" | awk '{print $1}')" \
    "workflow_sha256=$(sha256sum "$workflow_file" | awk '{print $1}')" \
    "ai_sha256=$(sha256sum "$ai_file" | awk '{print $1}')"
}

echo "Verifying separately managed stateful infrastructure..."
verify_stateful_infrastructure

if [[ "$resume_review" == "1" || "$resume_migration" == "1" ]]; then
  echo "Reasserting external maintenance before any forward-recovery command..."
  STARFORGE_REPO_DIR="$release_dir" \
    "$release_dir/scripts/set_production_maintenance.sh" enable
  maintenance_enabled=1
  echo "Reasserting application quiescence before forward recovery..."
  quiesce_project_applications 0 || {
    die "A project application process could not be stopped during forward recovery" 78
  }
fi

echo "Running production configuration checks..."
"${compose[@]}" run --rm --no-deps -T web python manage.py check --deploy --fail-level WARNING

if [[ "$resume_review" == "0" && "$resume_migration" == "0" ]]; then
  echo "Verifying pre-provisioned object storage with a bounded reserved-object probe..."
  STARFORGE_REPO_DIR="$release_dir" "$release_dir/scripts/verify_production_storage.sh"
  atomic_marker "$storage_marker" \
    "revision=${sha}" \
    "candidate_image_id=${candidate_image_id}" \
    "verified_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
else
  require_private_root_file "$storage_marker"
  [[ "$(marker_value "$storage_marker" revision)" == "$sha" && \
     "$(marker_value "$storage_marker" candidate_image_id)" == "$candidate_image_id" ]] || {
    die "Forward recovery has no matching pre-maintenance storage verification" 78
  }
fi

if [[ "$resume_review" == "0" && "$resume_migration" == "0" ]]; then
  echo "Verifying backup repository identity and staging capacity before maintenance..."
  STARFORGE_REPO_DIR="$release_dir" \
    STARFORGE_COMPOSE_FILE="$release_dir/docker/docker-compose.production.yml" \
    "$release_dir/scripts/backup_production.sh" --preflight
fi

if [[ "$resume_review" == "0" && "$resume_migration" == "0" ]]; then
  echo "Running serving-state all-tenant release preflight..."
  capture_preflight "${evidence_dir}/preflight-serving.jsonl"
  cutover_state="$(
    "${compose[@]}" --profile tools run --rm --no-deps -T cutover-check | tail -n 1 | tr -d '\r'
  )"
  case "$cutover_state" in
    clear)
      ;;
    required)
      cutover_required=1
      [[ "$MAINTENANCE_CUTOVER_ACK" == "$sha" ]] || {
        die "This revision requires STARFORGE_MAINTENANCE_CUTOVER=${sha} for its scheduled window" 78
      }
      ;;
    *)
      die "Unexpected maintenance-cutover state; refusing deployment" 78
      ;;
  esac

  previous_web_container="$("${compose[@]}" ps -q web | head -n 1)"
  if [[ -n "$previous_web_container" ]]; then
    previous_image="$(docker inspect "$previous_web_container" --format '{{.Config.Image}}')"
    previous_image_id="$(docker inspect "$previous_web_container" --format '{{.Image}}')"
  fi
elif [[ "$resume_review" == "1" ]]; then
  [[ "$("${compose[@]}" --profile tools run --rm --no-deps -T cutover-check | tail -n 1 | tr -d '\r')" == "clear" ]] || {
    die "Cannot resume: maintenance migrations are not clear in every tenant" 78
  }
else
  recovery_state="$(
    "${compose[@]}" --profile tools run --rm --no-deps -T cutover-check | tail -n 1 | tr -d '\r'
  )"
  [[ "$recovery_state" == "required" || "$recovery_state" == "clear" ]] || {
    die "Cannot determine tenant migration state for forward recovery" 78
  }
fi

if [[ "$maintenance_enabled" == "0" ]]; then
  echo "Enabling and externally verifying API/WebSocket/storage-write maintenance..."
  STARFORGE_REPO_DIR="$release_dir" \
    "$release_dir/scripts/set_production_maintenance.sh" enable
  maintenance_enabled=1
fi

broker_phase="initial"
if [[ "$resume_review" == "1" ]]; then
  broker_phase="resume-${resume_phase}"
elif [[ "$resume_migration" == "1" ]]; then
  broker_phase="resume-migration"
fi
echo "Recording broker depth before application shutdown..."
capture_broker_depth "${evidence_dir}/${broker_phase}-broker-before-stop.json"

echo "Stopping producers, draining durable Celery work, and proving an empty broker..."
apps_quiesced=1
capture_previous=0
if [[ "$resume_review" == "0" && "$resume_migration" == "0" ]]; then
  capture_previous=1
fi
drain_project_applications \
  "$capture_previous" "${evidence_dir}/${broker_phase}" || {
  die "Application work did not reach a safe empty boundary; refusing migration" 78
}

capture_broker_depth \
  "${evidence_dir}/${broker_phase}-broker-after-stop.json" --require-empty
migration_broker_evidence="${evidence_dir}/${broker_phase}-broker-after-stop.json"

if [[ "$resume_review" == "0" && "$resume_migration" == "0" ]]; then
  echo "Repeating migration preflight against the quiesced database..."
  capture_preflight "${evidence_dir}/preflight-quiesced.jsonl"

  [[ "${SKIP_BACKUP:-0}" != "1" ]] || {
    die "Production deployments cannot skip their quiesced backup and restore verification" 78
  }
  backup_started_epoch="$(date +%s)"
  STARFORGE_REPO_DIR="$release_dir" \
    STARFORGE_COMPOSE_FILE="$release_dir/docker/docker-compose.production.yml" \
    "$release_dir/scripts/backup_production.sh"
  require_private_root_file "${DEPLOY_DIR}/last_created_backup"
  created_backup_snapshot="$(<"${DEPLOY_DIR}/last_created_backup")"
  [[ "$created_backup_snapshot" =~ ^[0-9a-f]{64}$ ]] || die "Backup returned an invalid snapshot ID"
  [[ "$(stat -c '%Y' "${DEPLOY_DIR}/last_created_backup")" -ge "$backup_started_epoch" ]] || {
    die "Created backup marker is stale"
  }
  STARFORGE_RESTORE_SNAPSHOT="$created_backup_snapshot" \
    STARFORGE_REPO_DIR="$release_dir" \
    "$release_dir/scripts/verify_restore.sh"
  verified_backup_snapshot="$created_backup_snapshot"
  atomic_marker "${DEPLOY_DIR}/last_verified_backup" "$verified_backup_snapshot"
  atomic_marker "${DEPLOY_DIR}/last_verified_backup.meta" \
    "snapshot_id=${verified_backup_snapshot}" \
    "verified_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
    "revision=${sha}" \
    "verification=isolated-postgres-restore,isolated-redis-load-cache-flush,minio-manifest-check,restic-integrity"

  atomic_marker "${evidence_dir}/quiesced-backup.manifest" \
    "revision=${sha}" \
    "release_helpers_sha256=${helper_manifest_sha256}" \
    "candidate_image_id=${candidate_image_id}" \
    "previous_image=${previous_image}" \
    "previous_image_id=${previous_image_id}" \
    "verified_backup_snapshot=${verified_backup_snapshot}" \
    "broker_before_sha256=$(sha256sum "${evidence_dir}/initial-broker-before-stop.json" | awk '{print $1}')" \
    "broker_after_sha256=$(sha256sum "${evidence_dir}/initial-broker-after-stop.json" | awk '{print $1}')"

  echo "Applying public and tenant migrations..."
  apply_candidate_migrations
elif [[ "$resume_review" == "1" ]]; then
  STARFORGE_RESTORE_SNAPSHOT="$verified_backup_snapshot" \
    STARFORGE_REPO_DIR="$release_dir" \
    "$release_dir/scripts/verify_restore.sh"
else
  STARFORGE_RESTORE_SNAPSHOT="$verified_backup_snapshot" \
    STARFORGE_REPO_DIR="$release_dir" \
    "$release_dir/scripts/verify_restore.sh"
  echo "Retrying the same candidate's migrations from the preserved recovery boundary..."
  apply_candidate_migrations
fi

if [[ "$cutover_required" == "1" && "$resume_phase" != "backfills_applied" ]]; then
  marker_digest=""
  if [[ "$resume_review" == "1" ]]; then
    marker_digest="$(marker_value "$review_marker" review_sha256)"
  fi

  if [[ "$resume_phase" != "apply_started" ]]; then
    phase="initial"
    [[ "$resume_review" == "0" ]] || phase="resume"
    [[ "$resume_migration" == "0" ]] || phase="recovery"
    echo "Generating read-only legacy-attribution review evidence..."
    generate_backfill_review "$phase"

    if [[ "$resume_review" == "1" && "$review_digest" != "$marker_digest" ]]; then
      die "Quiesced review evidence changed since the operator review" 78
    fi
  else
    review_digest="$marker_digest"
  fi

  if [[ "$REVIEWED_BACKFILL_SHA256" != "$review_digest" ]]; then
    if [[ "$resume_phase" == "apply_started" ]]; then
      die "Resume the idempotent apply with the originally reviewed evidence digest" 78
    fi
    atomic_marker "$review_marker" \
      "phase=review_pending" \
      "revision=${sha}" \
      "candidate_image=${image}" \
      "candidate_image_id=${candidate_image_id}" \
      "release_helpers_sha256=${helper_manifest_sha256}" \
      "previous_image=${previous_image}" \
      "previous_image_id=${previous_image_id}" \
      "verified_backup_snapshot=${verified_backup_snapshot}" \
      "review_sha256=${review_digest}"
    intentional_review_pause=1
    echo "Post-migration review is pending. Application services remain stopped." >&2
    echo "Review root-only evidence in $evidence_dir, then rerun the exact revision with:" >&2
    echo "STARFORGE_REVIEWED_BACKFILL_SHA256=$review_digest" >&2
    exit 79
  fi

  approved_review_digest="$review_digest"
  atomic_marker "$review_marker" \
    "phase=apply_started" \
    "revision=${sha}" \
    "candidate_image=${image}" \
    "candidate_image_id=${candidate_image_id}" \
    "release_helpers_sha256=${helper_manifest_sha256}" \
    "previous_image=${previous_image}" \
    "previous_image_id=${previous_image_id}" \
    "verified_backup_snapshot=${verified_backup_snapshot}" \
    "review_sha256=${approved_review_digest}"
  echo "Applying only human-reviewed, deterministic attribution resolutions..."
  capture_notification_command \
    "${evidence_dir}/applied-notification-principals.json" \
    "${evidence_dir}/applied-notification-principals-summary.jsonl" \
    --apply
  capture_web_command "${evidence_dir}/applied-finance-attribution.json" \
    python manage.py backfill_finance_attribution --apply --no-color
  capture_web_command "${evidence_dir}/applied-audit-scopes.jsonl" \
    python manage.py backfill_audit_scopes --apply --no-color
  capture_web_command "${evidence_dir}/post-apply-workflow-attribution.jsonl" \
    python manage.py check_workflow_principal_attribution --no-color
  capture_web_command "${evidence_dir}/post-apply-ai-attribution.jsonl" \
    python manage.py check_ai_attribution --fail-on-expired-content --no-color
  generate_backfill_review "post-apply"
  atomic_marker "$review_marker" \
    "phase=backfills_applied" \
    "revision=${sha}" \
    "candidate_image=${image}" \
    "candidate_image_id=${candidate_image_id}" \
    "release_helpers_sha256=${helper_manifest_sha256}" \
    "previous_image=${previous_image}" \
    "previous_image_id=${previous_image_id}" \
    "verified_backup_snapshot=${verified_backup_snapshot}" \
    "review_sha256=${approved_review_digest}" \
    "post_review_sha256=${review_digest}"
fi

"${compose[@]}" --profile tools run --rm --no-deps -T collectstatic
capture_broker_depth "${evidence_dir}/broker-before-candidate-start.json"

echo "Starting release $short_sha..."
"${compose[@]}" up -d --remove-orphans --no-deps "${app_services[@]}"
verify_stateful_infrastructure

for service in "${app_services[@]}"; do
  service_container_count=0
  while IFS= read -r container_id; do
    [[ -z "$container_id" ]] && continue
    service_container_count=$((service_container_count + 1))
    actual_image_id="$(docker inspect "$container_id" --format '{{.Image}}')"
    [[ "$actual_image_id" == "$candidate_image_id" ]] || {
      die "Service $service did not start from the reviewed candidate image" 78
    }
  done < <("${compose[@]}" ps -q "$service")
  [[ "$service_container_count" -gt 0 ]] || die "Service $service has no candidate container" 78
done

containers_ready=0
for _ in $(seq 1 36); do
  containers_ready=1
  for service in postgres redis minio "${app_services[@]}"; do
    service_container_count=0
    while IFS= read -r container_id; do
      [[ -z "$container_id" ]] && continue
      service_container_count=$((service_container_count + 1))
      container_state="$(docker inspect "$container_id" --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
      [[ "$container_state" == "healthy" || "$container_state" == "running" ]] || containers_ready=0
    done < <("${compose[@]}" ps -q "$service")
    [[ "$service_container_count" -gt 0 ]] || containers_ready=0
  done
  [[ "$containers_ready" == "1" ]] && break
  sleep 5
done
[[ "$containers_ready" == "1" ]] || die "Candidate containers did not become internally healthy"

healthy=0
for _ in $(seq 1 36); do
  if curl -fsS --proto '=https' --tlsv1.2 --connect-timeout 5 --max-time 15 \
    "$HEALTH_URL" >/dev/null; then
    healthy=1
    break
  fi
  sleep 5
done

if [[ "$healthy" != "1" ]]; then
  echo "Release failed readiness checks" >&2
  "${compose[@]}" ps >&2 || true
  exit 1
fi
candidate_healthy=1

echo "Hashing legacy session credentials after release readiness..."
"${compose[@]}" run --rm --no-deps -T web python manage.py hash_session_keys
capture_broker_depth "${evidence_dir}/broker-after-candidate-start.json"

echo "Running authenticated director/manager catalog smoke behind maintenance..."
candidate_web_container="$("${compose[@]}" ps -q web)"
[[ -n "$candidate_web_container" && \
   "$(printf '%s\n' "$candidate_web_container" | sed '/^$/d' | wc -l)" == "1" ]] || {
  die "Leadership smoke requires exactly one candidate web container" 78
}
smoke_temporary="${evidence_dir}/leadership-smoke.json.tmp"
rm -f -- "$smoke_temporary"
if ! docker run --rm --pull=never --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --user 0:0 --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges \
  --pids-limit 32 --memory 128m --cpus 0.25 \
  --network "container:${candidate_web_container}" \
  --mount "type=bind,src=${LEADERSHIP_SMOKE_CONFIG},dst=/run/secrets/leadership-smoke.json,ro" \
  --entrypoint python "$image" \
  scripts/run_leadership_release_smoke.py \
  --config /run/secrets/leadership-smoke.json \
  --expected-revision "$sha" >"$smoke_temporary"; then
  chmod 0600 "$smoke_temporary"
  mv -f -- "$smoke_temporary" "${evidence_dir}/leadership-smoke.failed.json"
  die "Authenticated leadership release smoke failed; maintenance remains enabled" 78
fi
SMOKE_EVIDENCE="$smoke_temporary" EXPECTED_REVISION="$sha" python3 - <<'PY'
import json
import os
from pathlib import Path

rows = Path(os.environ["SMOKE_EVIDENCE"]).read_text(encoding="utf-8").splitlines()
if len(rows) != 1:
    raise SystemExit("Leadership smoke emitted an unexpected evidence stream")
payload = json.loads(rows[0])
if (
    payload.get("revision") != os.environ["EXPECTED_REVISION"]
    or payload.get("passed") is not True
    or not isinstance(payload.get("operation_count"), int)
    or payload["operation_count"] < 108
):
    raise SystemExit("Leadership smoke evidence is incomplete")
PY
chmod 0600 "$smoke_temporary"
mv -f -- "$smoke_temporary" "${evidence_dir}/leadership-smoke.json"
smoke_sha256="$(sha256sum "${evidence_dir}/leadership-smoke.json" | awk '{print $1}')"

echo "Disabling maintenance only after authenticated smoke passed..."
STARFORGE_REPO_DIR="$release_dir" \
  "$release_dir/scripts/set_production_maintenance.sh" disable
maintenance_enabled=0
STARFORGE_REPO_DIR="$release_dir" \
  "$release_dir/scripts/set_production_maintenance.sh" assert-disabled
curl -fsS --proto '=https' --tlsv1.2 --connect-timeout 5 --max-time 15 \
  "$HEALTH_URL" >/dev/null

atomic_marker "${DEPLOY_DIR}/current_release" \
  "revision=${sha}" \
  "image=${image}" \
  "image_id=${candidate_image_id}" \
  "release_helpers_sha256=${helper_manifest_sha256}" \
  "verified_backup_snapshot=${verified_backup_snapshot}" \
  "leadership_smoke_sha256=${smoke_sha256}"
if [[ -f "$review_marker" ]]; then
  unlink -- "$review_marker"
fi
if [[ -f "${DEPLOY_DIR}/cutover_failed" ]]; then
  unlink -- "${DEPLOY_DIR}/cutover_failed"
fi
echo "Deployment $short_sha is healthy."
