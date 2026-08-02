#!/usr/bin/env bash

# Shared fail-closed readers for root-only production environment files. These
# files use a deliberately smaller grammar than a shell or Docker Compose: one
# literal KEY=VALUE per line, comments only at column zero, no interpolation,
# quoting, escapes, or whitespace. That keeps secrets data-only and guarantees
# that reading a deployment file can never execute code as root.

sf_require_private_root_file() {
  local path="$1" mode
  [[ -f "$path" && ! -L "$path" ]] || {
    echo "Required production input is unavailable: $path" >&2
    return 1
  }
  [[ "$(stat -c '%u' "$path")" == "0" ]] || {
    echo "Production input must be owned by root: $path" >&2
    return 1
  }
  mode="$(stat -c '%a' "$path")"
  [[ "$mode" == "600" || "$mode" == "400" ]] || {
    echo "Production input must use mode 0600 or 0400: $path" >&2
    return 1
  }
}

sf_read_env_values() {
  local path="$1" output_name="$2" encoded value completed=0
  shift 2
  local -n output="$output_name"
  output=()
  while IFS= read -r encoded; do
    if [[ "$encoded" == "STARFORGE_ENV_STREAM_COMPLETE" ]]; then
      completed=1
      continue
    fi
    value="$(printf '%s' "$encoded" | base64 --decode)" || {
      output=()
      return 1
    }
    output+=("$value")
  done < <(python3 - "$path" "$@" <<'PY'
import base64
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
requested = sys.argv[2:]
values: dict[str, str] = {}
key_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
literal_pattern = re.compile(r"[^\s'\"\\$`]*\Z")
for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    if not raw_line or raw_line.startswith("#"):
        continue
    if "=" not in raw_line:
        raise SystemExit(f"Malformed environment entry in {path.name}:{line_number}")
    key, value = raw_line.split("=", 1)
    if not key_pattern.fullmatch(key):
        raise SystemExit(f"Invalid environment key in {path.name}:{line_number}")
    if key in values:
        raise SystemExit(f"Duplicate environment key in {path.name}:{line_number}: {key}")
    if not literal_pattern.fullmatch(value):
        raise SystemExit(
            f"Environment values must be unquoted literal data without whitespace or interpolation: "
            f"{path.name}:{line_number}"
        )
    values[key] = value

for key in requested:
    value = values.get(key)
    if value is None or value == "":
        raise SystemExit(f"Required environment key is missing: {path.name}:{key}")
    print(base64.b64encode(value.encode("utf-8")).decode("ascii"))
print("STARFORGE_ENV_STREAM_COMPLETE")
PY
  )
  [[ "$completed" == "1" && "${#output[@]}" == "$#" ]] || {
    output=()
    return 1
  }
}

sf_require_digest_image() {
  local name="$1" value="$2"
  [[ "$value" =~ @sha256:[0-9a-f]{64}$ ]] || {
    echo "$name must use an immutable sha256 image digest" >&2
    return 1
  }
}

sf_clear_compose_process_overrides() {
  # Docker Compose gives inherited process variables precedence over an
  # explicit --env-file.  Release automation must resolve only the reviewed
  # file plus the candidate APP_IMAGE that it sets itself.
  unset \
    COMPOSE_FILE \
    COMPOSE_PROJECT_NAME \
    COMPOSE_PROFILES \
    COMPOSE_ENV_FILES \
    COMPOSE_PATH_SEPARATOR \
    COMPOSE_PARALLEL_LIMIT \
    COMPOSE_IGNORE_ORPHANS \
    COMPOSE_REMOVE_ORPHANS \
    COMPOSE_ANSI \
    COMPOSE_STATUS_STDOUT \
    COMPOSE_PROGRESS \
    COMPOSE_MENU \
    DOCKER_DEFAULT_PLATFORM
}

sf_export_compose_infrastructure_images() {
  local path="$1"
  local -a images
  sf_read_env_values "$path" images POSTGRES_IMAGE REDIS_IMAGE MINIO_IMAGE || return 1
  sf_require_digest_image POSTGRES_IMAGE "${images[0]}" || return 1
  sf_require_digest_image REDIS_IMAGE "${images[1]}" || return 1
  sf_require_digest_image MINIO_IMAGE "${images[2]}" || return 1
  POSTGRES_IMAGE="${images[0]}"
  REDIS_IMAGE="${images[1]}"
  MINIO_IMAGE="${images[2]}"
  export POSTGRES_IMAGE REDIS_IMAGE MINIO_IMAGE
}
