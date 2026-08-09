"""Contracts for production object-storage topology and least privilege."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker" / "docker-compose.production.yml").read_text(encoding="utf-8")
CADDY = (ROOT / "docker" / "Caddyfile.starforge.example").read_text(encoding="utf-8")
DEPLOY = (ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
CONFIGURE = (ROOT / "scripts" / "configure_production_storage.sh").read_text(encoding="utf-8")
VERIFY = (ROOT / "scripts" / "verify_production_storage.sh").read_text(encoding="utf-8")
ENV_HELPER = (ROOT / "scripts" / "lib" / "production_env.sh").read_text(encoding="utf-8")
POLICY_HELPER = ROOT / "scripts" / "storage_iam_contract.py"
APP_ENV_EXAMPLE = (ROOT / "docker" / "app.env.production.example").read_text(encoding="utf-8")
STATIC_ENV_EXAMPLE = (ROOT / "docker" / "static-storage.env.example").read_text(encoding="utf-8")
RESTORE = (ROOT / "scripts" / "verify_restore.sh").read_text(encoding="utf-8")


def test_minio_is_reachable_only_through_caddy_s3_alias():
    assert "aliases: [starforge-minio]" in COMPOSE
    assert "reverse_proxy starforge-minio:9000" in CADDY
    assert "9001" in CADDY
    assert "must remain private" in CADDY
    assert "ports:" not in COMPOSE


def test_static_writer_secret_is_isolated_to_collectstatic():
    app_anchor, services = COMPOSE.split("services:", 1)
    collectstatic = services.split("  collectstatic:", 1)[1].split("\nnetworks:", 1)[0]

    assert "static-storage.env" not in app_anchor
    assert "static-storage.env" in collectstatic
    assert 'STATIC_STORAGE_WRITE_ENABLED: "True"' in collectstatic
    assert 'AWS_S3_ACCESS_KEY_ID: ""' in collectstatic
    assert 'AWS_S3_SECRET_ACCESS_KEY: ""' in collectstatic
    assert "networks:\n      - internal\n      - egress" in collectstatic
    assert "AWS_STATIC_ACCESS_KEY_ID" not in APP_ENV_EXAMPLE
    assert "AWS_STATIC_SECRET_ACCESS_KEY" not in APP_ENV_EXAMPLE
    assert "AWS_STATIC_ACCESS_KEY_ID" in STATIC_ENV_EXAMPLE
    assert "AWS_STATIC_SECRET_ACCESS_KEY" in STATIC_ENV_EXAMPLE
    assert "STATIC_STORAGE_WRITE_ENABLED=False" in APP_ENV_EXAMPLE
    assert "AWS_EC2_METADATA_DISABLED=true" in APP_ENV_EXAMPLE
    assert 'require_private_root_file "${DEPLOY_DIR}/static-storage.env"' in DEPLOY
    assert '"$deployment_path/static-storage.env"' in RESTORE
    assert "AWS_STATIC_ACCESS_KEY_ID AWS_STATIC_SECRET_ACCESS_KEY" in RESTORE
    assert "Restored media and static service credentials are not isolated" in RESTORE
    assert "overlap MinIO root authority" in RESTORE


def test_storage_bootstrap_creates_exact_non_root_identities_and_policies():
    assert 'mc mb --ignore-existing "source/$MEDIA_BUCKET" "source/$STATIC_BUCKET"' in CONFIGURE
    assert 'mc anonymous set none "source/$MEDIA_BUCKET"' in CONFIGURE
    assert "public-static-policy.json" in CONFIGURE
    assert "starforge-media-runtime-v1" in CONFIGURE
    assert "starforge-static-writer-v1" in CONFIGURE
    assert "mc admin user add" in CONFIGURE
    assert "mc admin policy attach" in CONFIGURE
    assert '"$media_access_key" != "$minio_root_user"' in CONFIGURE
    assert '"$static_access_key" != "$minio_root_user"' in CONFIGURE
    assert '"$REPO_DIR/scripts/verify_production_storage.sh"' in CONFIGURE


def test_policy_renderer_scopes_each_identity_and_explicitly_denies_the_other_bucket(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(POLICY_HELPER),
            "render",
            str(tmp_path),
            "starforge-media",
            "starforge-static",
            "https://app.example.test",
            "https://app.example.test",
            "https://media.example.test",
            "https://static.example.test",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    media = json.loads((tmp_path / "media-runtime-policy.json").read_text(encoding="utf-8"))
    static = json.loads((tmp_path / "static-writer-policy.json").read_text(encoding="utf-8"))
    public = json.loads((tmp_path / "public-static-policy.json").read_text(encoding="utf-8"))

    assert any(
        statement["Effect"] == "Allow" and statement["Resource"] == ["arn:aws:s3:::starforge-media/*"]
        for statement in media["Statement"]
    )
    assert any(
        statement["Effect"] == "Deny" and "arn:aws:s3:::starforge-static/*" in statement["Resource"]
        for statement in media["Statement"]
    )
    assert any(
        statement["Effect"] == "Deny" and "arn:aws:s3:::starforge-media/*" in statement["Resource"]
        for statement in static["Statement"]
    )
    assert public["Statement"] == [
        {
            "Action": ["s3:GetObject"],
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Resource": ["arn:aws:s3:::starforge-static/*"],
        }
    ]
    assert "s3:ListAllMyBuckets" not in json.dumps(media)
    assert "s3:ListAllMyBuckets" not in json.dumps(static)


def _render_and_write_valid_iam_evidence(root: Path) -> tuple[Path, Path]:
    expected = root / "expected"
    evidence = root / "evidence"
    expected.mkdir(parents=True)
    evidence.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(POLICY_HELPER),
            "render",
            str(expected),
            "starforge-media",
            "starforge-static",
            "https://app.example.test",
            "https://app.example.test",
            "https://media.example.test",
            "https://static.example.test",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    media_policy = json.loads((expected / "media-runtime-policy.json").read_text(encoding="utf-8"))
    static_policy = json.loads((expected / "static-writer-policy.json").read_text(encoding="utf-8"))

    artifacts = {
        "media-policy-info.json": {
            "status": "success",
            "policyInfo": {
                "PolicyName": "starforge-media-runtime-v1",
                "Policy": media_policy,
            },
        },
        "static-policy-info.json": {
            "status": "success",
            "policyInfo": {
                "PolicyName": "starforge-static-writer-v1",
                "Policy": static_policy,
            },
        },
        "media-user.json": {
            "status": "success",
            "accessKey": "media-runtime",
            "policyName": "starforge-media-runtime-v1",
            "userStatus": "enabled",
        },
        "static-user.json": {
            "status": "success",
            "accessKey": "static-writer",
            "policyName": "starforge-static-writer-v1",
            "userStatus": "enabled",
        },
        "iam-mappings.json": {
            "status": "success",
            "result": {
                "userMappings": [
                    {"user": "media-runtime", "policies": ["starforge-media-runtime-v1"]},
                    {"user": "static-writer", "policies": ["starforge-static-writer-v1"]},
                ]
            },
        },
    }
    for name, payload in artifacts.items():
        (evidence / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    (evidence / "groups.jsonl").write_text("", encoding="utf-8")
    return expected, evidence


def _verify_iam(expected: Path, evidence: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(POLICY_HELPER),
            "verify",
            str(expected),
            str(evidence),
            "media-runtime",
            "static-writer",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_iam_verifier_accepts_only_the_exact_policy_and_direct_mapping(tmp_path):
    expected, evidence = _render_and_write_valid_iam_evidence(tmp_path)

    result = _verify_iam(expected, evidence)

    assert result.returncode == 0, result.stderr


def test_iam_verifier_rejects_additional_policy_or_group_inheritance(tmp_path):
    expected, evidence = _render_and_write_valid_iam_evidence(tmp_path)
    mappings = json.loads((evidence / "iam-mappings.json").read_text(encoding="utf-8"))
    mappings["result"]["userMappings"][0]["policies"].append("consoleAdmin")
    (evidence / "iam-mappings.json").write_text(json.dumps(mappings) + "\n", encoding="utf-8")

    additional_policy = _verify_iam(expected, evidence)

    assert additional_policy.returncode != 0
    assert "additional direct policies" in additional_policy.stderr

    expected, evidence = _render_and_write_valid_iam_evidence(tmp_path / "second")
    (evidence / "groups.jsonl").write_text(
        json.dumps(
            {
                "status": "success",
                "groupName": "admins",
                "members": ["static-writer"],
                "groupStatus": "enabled",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    inherited_policy = _verify_iam(expected, evidence)

    assert inherited_policy.returncode != 0
    assert "must not inherit policy" in inherited_policy.stderr


def test_read_only_release_verifier_checks_exact_iam_and_behavioral_denials():
    verify = DEPLOY.index("scripts/verify_production_storage.sh")
    backup = DEPLOY.index("scripts/backup_production.sh")
    migrations = DEPLOY.index('echo "Applying public and tenant migrations..."')
    assert verify < backup < migrations
    assert "scripts/configure_production_storage.sh" not in DEPLOY
    assert "mc mb" not in VERIFY
    assert "mc anonymous set" not in VERIFY
    assert "mc admin policy info" in VERIFY
    assert "mc admin policy entities" in VERIFY
    assert "mc admin group info" in VERIFY
    assert 'python3 "$POLICY_HELPER" verify' in VERIFY
    assert VERIFY.count("can discover buckets outside its exact scope") == 2
    for denial in (
        '"static PutObject"',
        '"static DeleteObject"',
        '"static bucket location"',
        '"media PutObject"',
        '"media bucket administration"',
        '"media bucket policy mutation"',
    ):
        assert denial in VERIFY
    assert "Runtime process received static-writer credentials" in VERIFY
    assert "PublicStaticFilesStorage" in VERIFY
    assert "presign_upload" in VERIFY
    assert "presign_post_upload" in VERIFY
    assert "Private media object was anonymously readable" in VERIFY
    assert "--proto '=https'" in VERIFY
    assert "--connect-timeout" in VERIFY
    assert "--max-time" in VERIFY


def test_storage_operator_tools_are_digest_pinned_hardened_and_data_only():
    for script in (CONFIGURE, VERIFY):
        assert "sf_require_private_root_file" in script
        assert "sf_read_env_values" in script
        assert 'source "$MINIO_ENV"' not in script
        assert 'source "$BACKUP_ENV"' not in script
        assert "sf_require_digest_image MINIO_MC_IMAGE" in script
        assert "--pull=never" in script
        assert "--read-only" in script
        assert "--cap-drop ALL" in script
        assert "--security-opt no-new-privileges" in script
        assert "static-storage.env" in script
        assert "STARFORGE_COMPOSE_FILE" not in script
        assert 'compose_file="${REPO_DIR}/docker/docker-compose.production.yml"' in script
        assert "sf_clear_compose_process_overrides" in script
        assert "sf_export_compose_infrastructure_images" in script
        assert '[[ "$project_name" == "starforge" ]]' in script
    assert "Duplicate environment key" in ENV_HELPER
    assert "Invalid environment key" in ENV_HELPER
    assert "unquoted literal data" in ENV_HELPER


def test_storage_bootstrap_cannot_replace_candidate_image_from_compose_env():
    assert 'candidate_image="$APP_IMAGE"' in CONFIGURE
    assert 'export APP_IMAGE="$candidate_image"' in CONFIGURE
    assert 'source "$COMPOSE_ENV"' not in CONFIGURE
