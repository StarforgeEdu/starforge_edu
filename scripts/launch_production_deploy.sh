#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

revision="${1:-}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: $0 <exact-40-character-commit-sha>" >&2; exit 2; }
REPO_DIR="${STARFORGE_REPO_DIR:-/root/starforge_edu}"
RELEASE_ROOT="${STARFORGE_RELEASE_ROOT:-/root/starforge-releases}"
APPROVED_REMOTE_REF="${STARFORGE_APPROVED_REMOTE_REF:-refs/remotes/origin/codex/permission-audit-release}"
[[ "$EUID" -eq 0 && -d "$REPO_DIR/.git" ]] || { echo "Root production repository is unavailable" >&2; exit 1; }
[[ "$RELEASE_ROOT" == /* && "$RELEASE_ROOT" != "/" && ! -L "$RELEASE_ROOT" ]] || {
  echo "Release root must be an absolute non-root directory without symlinks" >&2
  exit 1
}
[[ "$APPROVED_REMOTE_REF" =~ ^refs/remotes/origin/[A-Za-z0-9._/-]+$ && \
   "$APPROVED_REMOTE_REF" != *".."* && "$APPROVED_REMOTE_REF" != *"//"* ]] || {
  echo "Approved remote ref is invalid" >&2
  exit 1
}
install -d -o root -g root -m 0700 -- "$RELEASE_ROOT"
git -C "$REPO_DIR" fetch --prune origin
sha="$(git -C "$REPO_DIR" rev-parse --verify "${revision}^{commit}")"
[[ "$sha" == "$revision" ]] || { echo "Revision did not resolve exactly" >&2; exit 1; }
git -C "$REPO_DIR" show-ref --verify --quiet "$APPROVED_REMOTE_REF"
git -C "$REPO_DIR" merge-base --is-ancestor "$sha" "$APPROVED_REMOTE_REF"

launcher_root="$(mktemp -d "${RELEASE_ROOT}/.deploy-launch.${sha}.XXXXXX")"
release_tree="${launcher_root}/release"
cleanup() {
  git -C "$REPO_DIR" worktree remove --force "$release_tree" >/dev/null 2>&1 || true
  [[ "$launcher_root" == "${RELEASE_ROOT}/.deploy-launch.${sha}."* ]] && rm -rf -- "$launcher_root"
}
trap cleanup EXIT
git -C "$REPO_DIR" worktree add --detach "$release_tree" "$sha"
deploy_script="${release_tree}/scripts/deploy_production.sh"
expected_blob="$(git -C "$REPO_DIR" rev-parse "${sha}:scripts/deploy_production.sh")"
actual_blob="$(git -C "$REPO_DIR" hash-object "$deploy_script")"
[[ "$actual_blob" == "$expected_blob" ]] || { echo "Detached deploy script failed blob verification" >&2; exit 1; }

STARFORGE_BOOTSTRAP_REVISION="$sha" \
STARFORGE_BOOTSTRAP_DEPLOY_BLOB="$expected_blob" \
STARFORGE_REPO_DIR="$REPO_DIR" \
  "$deploy_script" "$sha"
