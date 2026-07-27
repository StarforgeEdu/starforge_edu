"""Security contracts enforced by the container launch configuration."""

from pathlib import Path


def test_gunicorn_access_log_omits_credential_bearing_request_target():
    docker_dir = Path(__file__).resolve().parents[1] / "docker"
    launch_configs = (
        (docker_dir / "entrypoint.sh").read_text(),
        (docker_dir / "docker-compose.yml").read_text(),
    )

    for config in launch_configs:
        assert "--access-logformat" in config
        assert "%(r)s" not in config  # full request line
        assert "%(U)s" not in config  # URL path (contains the signed iCal token)
        assert "%(q)s" not in config  # query string (legacy token fallback)
