#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

action="${1:-}"
case "$action" in
  enable|disable|assert-enabled|assert-disabled) ;;
  *)
    echo "usage: $0 enable|disable|assert-enabled|assert-disabled" >&2
    exit 2
    ;;
esac

DEPLOY_DIR="${STARFORGE_DEPLOY_DIR:-/root/starforge-deploy}"
REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
MAINTENANCE_ENV="${DEPLOY_DIR}/maintenance.env"
ENV_HELPER="${REPO_DIR}/scripts/lib/production_env.sh"

die() {
  echo "$1" >&2
  exit "${2:-1}"
}

[[ "$EUID" -eq 0 ]] || die "Production maintenance control must run as root"
[[ -f "$ENV_HELPER" && ! -L "$ENV_HELPER" ]] || die "Production environment reader is unavailable"
# shellcheck source=scripts/lib/production_env.sh
source "$ENV_HELPER"
sf_require_private_root_file "$MAINTENANCE_ENV" || exit 1
sf_read_env_values "$MAINTENANCE_ENV" maintenance_values \
  STARFORGE_CADDY_CONTAINER \
  CADDY_IMAGE \
  STARFORGE_CADDY_CONFIG_DIR \
  STARFORGE_API_ORIGIN \
  STARFORGE_MEDIA_ORIGIN || die "Maintenance environment is invalid"

CADDY_CONTAINER="${maintenance_values[0]}"
CADDY_IMAGE="${maintenance_values[1]}"
CADDY_CONFIG_DIR="${maintenance_values[2]}"
API_ORIGIN="${maintenance_values[3]}"
MEDIA_ORIGIN="${maintenance_values[4]}"

[[ "$CADDY_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || {
  die "STARFORGE_CADDY_CONTAINER is invalid"
}
sf_require_digest_image CADDY_IMAGE "$CADDY_IMAGE" || exit 1
[[ "$CADDY_CONFIG_DIR" == /* && "$CADDY_CONFIG_DIR" != "/" && ! -L "$CADDY_CONFIG_DIR" ]] || {
  die "STARFORGE_CADDY_CONFIG_DIR must be an absolute non-root directory without symlinks"
}
[[ -d "$CADDY_CONFIG_DIR" && "$(stat -c '%u' "$CADDY_CONFIG_DIR")" == "0" ]] || {
  die "Caddy configuration directory must be root-owned"
}
config_mode="$(stat -c '%a' "$CADDY_CONFIG_DIR")"
(( (8#$config_mode & 0022) == 0 )) || die "Caddy configuration directory must not be group/world writable"

API_ORIGIN_VALUE="$API_ORIGIN" MEDIA_ORIGIN_VALUE="$MEDIA_ORIGIN" python3 - <<'PY'
import os
from urllib.parse import urlsplit

for name in ("API_ORIGIN_VALUE", "MEDIA_ORIGIN_VALUE"):
    value = os.environ[name]
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(f"{name} must be one credential-free HTTPS origin")
PY
API_ORIGIN="${API_ORIGIN%/}"
MEDIA_ORIGIN="${MEDIA_ORIGIN%/}"

caddyfile="${CADDY_CONFIG_DIR}/Caddyfile"
state_dir="${CADDY_CONFIG_DIR}/maintenance.d"
state_file="${state_dir}/state.caddy"
[[ -f "$caddyfile" && ! -L "$caddyfile" && "$(stat -c '%u' "$caddyfile")" == "0" ]] || {
  die "Root-owned Caddyfile is unavailable"
}
[[ -d "$state_dir" && ! -L "$state_dir" && "$(stat -c '%u' "$state_dir")" == "0" ]] || {
  die "Root-owned Caddy maintenance directory is unavailable"
}
state_dir_mode="$(stat -c '%a' "$state_dir")"
(( (8#$state_dir_mode & 0022) == 0 )) || die "Caddy maintenance directory must not be group/world writable"

python3 - "$caddyfile" <<'PY'
import sys
from pathlib import Path

lines = {
    line.strip()
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
required = {
    "import maintenance.d/state.caddy",
    "import starforge_api_maintenance",
    "import starforge_storage_maintenance",
}
missing = required - lines
if missing:
    raise SystemExit(f"Caddyfile is missing maintenance imports: {sorted(missing)}")
PY

container_name="$(docker inspect "$CADDY_CONTAINER" --format '{{.Name}}')" || {
  die "Configured Caddy container is unavailable"
}
[[ "$container_name" == "/${CADDY_CONTAINER}" ]] || die "Caddy container name did not resolve exactly"
[[ "$(docker inspect "$CADDY_CONTAINER" --format '{{.State.Status}}')" == "running" ]] || {
  die "Caddy container is not running"
}
expected_image_id="$(docker image inspect "$CADDY_IMAGE" --format '{{.Id}}')" || {
  die "Reviewed Caddy image is unavailable locally"
}
[[ "$(docker inspect "$CADDY_CONTAINER" --format '{{.Image}}')" == "$expected_image_id" ]] || {
  die "Caddy container does not use the reviewed digest-pinned image"
}
mount_manifest="$(docker inspect "$CADDY_CONTAINER" --format \
  '{{range .Mounts}}{{if eq .Destination "/etc/caddy"}}{{println .Type .Source .RW}}{{end}}{{end}}')"
[[ "$mount_manifest" == "bind ${CADDY_CONFIG_DIR} false" ]] || {
  die "Caddy must mount the exact reviewed configuration directory read-only at /etc/caddy"
}

tmp_dir="$(mktemp -d /tmp/starforge-maintenance.XXXXXX)"
cleanup() {
  if [[ -n "${tmp_dir:-}" && "$tmp_dir" == /tmp/starforge-maintenance.* ]]; then
    rm -rf -- "$tmp_dir"
  else
    echo "Refusing to remove unexpected maintenance path: ${tmp_dir:-<unset>}" >&2
  fi
}
trap cleanup EXIT

render_enabled() {
  cat <<'CADDY'
(starforge_api_maintenance) {
	header {
		Cache-Control "no-store"
		Retry-After "60"
		Content-Type "application/json"
		X-StarForge-Maintenance "active"
	}
	respond "{\"success\":false,\"code\":\"maintenance\",\"message\":\"Scheduled maintenance is in progress.\"}" 503 {
		close
	}
}

(starforge_storage_maintenance) {
	@starforgeStorageMutation {
		not method GET HEAD OPTIONS
	}
	header @starforgeStorageMutation {
		Cache-Control "no-store"
		Retry-After "60"
		X-StarForge-Maintenance "active"
	}
	respond @starforgeStorageMutation "Storage writes are temporarily unavailable." 503 {
		close
	}
}
CADDY
}

render_disabled() {
  cat <<'CADDY'
(starforge_api_maintenance) {
	@starforgeMaintenanceDisabled path /.well-known/starforge-maintenance-disabled
	respond @starforgeMaintenanceDisabled 404
}

(starforge_storage_maintenance) {
	@starforgeStorageMaintenanceDisabled path /.well-known/starforge-storage-maintenance-disabled
	respond @starforgeStorageMaintenanceDisabled 404
}
CADDY
}

render_enabled >"$tmp_dir/enabled.caddy"
render_disabled >"$tmp_dir/disabled.caddy"
chmod 0644 "$tmp_dir/enabled.caddy" "$tmp_dir/disabled.caddy"

previous_state="missing"
if [[ -e "$state_file" ]]; then
  [[ -f "$state_file" && ! -L "$state_file" && "$(stat -c '%u' "$state_file")" == "0" ]] || {
    die "Caddy maintenance state must be a root-owned regular file"
  }
  if cmp -s "$state_file" "$tmp_dir/enabled.caddy"; then
    previous_state="enabled"
  elif cmp -s "$state_file" "$tmp_dir/disabled.caddy"; then
    previous_state="disabled"
  else
    die "Caddy maintenance state differs from both reviewed canonical states"
  fi
fi

desired_state="${action#assert-}"
if [[ "$action" == "enable" || "$action" == "disable" ]]; then
  install -o root -g root -m 0644 "$tmp_dir/${desired_state}.caddy" "$tmp_dir/state.caddy"
  mv -f -- "$tmp_dir/state.caddy" "$state_file"
  sync -d "$state_file"
  if ! docker exec "$CADDY_CONTAINER" caddy validate \
      --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null || \
     ! docker exec "$CADDY_CONTAINER" caddy reload \
      --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null; then
    if [[ "$previous_state" == "enabled" || "$previous_state" == "disabled" ]]; then
      install -o root -g root -m 0644 "$tmp_dir/${previous_state}.caddy" "$state_file"
      sync -d "$state_file"
      docker exec "$CADDY_CONTAINER" caddy reload \
        --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || true
    else
      rm -f -- "$state_file"
    fi
    die "Caddy rejected the requested maintenance state; previous state was restored"
  fi
fi

cmp -s "$state_file" "$tmp_dir/${desired_state}.caddy" || {
  die "On-disk Caddy maintenance state does not match the requested state"
}

request_status() {
  local method="$1" url="$2" headers="$3"
  curl -sS --proto '=https' --tlsv1.2 --connect-timeout 5 --max-time 15 \
    -X "$method" -D "$headers" -o /dev/null -w '%{http_code}' "$url"
}

api_headers="$tmp_dir/api.headers"
ws_headers="$tmp_dir/ws.headers"
health_headers="$tmp_dir/health.headers"
storage_write_headers="$tmp_dir/storage-write.headers"
storage_read_headers="$tmp_dir/storage-read.headers"
api_status="$(request_status GET "${API_ORIGIN}/api/schema/" "$api_headers")"
ws_status="$(request_status GET "${API_ORIGIN}/ws/ping/" "$ws_headers")"
health_status="$(request_status GET "${API_ORIGIN}/healthz/ready" "$health_headers")"
storage_write_status="$(request_status PUT \
  "${MEDIA_ORIGIN}/.starforge-maintenance-probe" "$storage_write_headers")"
storage_read_status="$(request_status GET \
  "${MEDIA_ORIGIN}/.starforge-maintenance-probe" "$storage_read_headers")"

has_maintenance_header() {
  tr -d '\r' <"$1" | grep -Fqi 'X-StarForge-Maintenance: active'
}

if [[ "$desired_state" == "enabled" ]]; then
  [[ "$api_status" == "503" && "$ws_status" == "503" && "$storage_write_status" == "503" ]] || {
    die "Maintenance edge did not block API, WebSocket, and storage mutation traffic"
  }
  has_maintenance_header "$api_headers" && \
    has_maintenance_header "$ws_headers" && \
    has_maintenance_header "$storage_write_headers" || {
      die "Maintenance response marker is missing"
    }
  [[ "$health_status" == "200" ]] || die "Readiness must remain observable during maintenance"
  ! has_maintenance_header "$health_headers" || die "Readiness was incorrectly maintenance-blocked"
  [[ "$storage_read_status" != "503" ]] || die "Maintenance must not block storage reads"
else
  ! has_maintenance_header "$api_headers" && \
    ! has_maintenance_header "$ws_headers" && \
    ! has_maintenance_header "$storage_write_headers" || {
      die "Maintenance marker remained active after disable"
    }
fi

echo "Production maintenance state is ${desired_state} and externally verified."
