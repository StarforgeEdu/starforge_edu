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


def test_production_beat_healthcheck_uses_runtime_available_in_slim_image():
    compose = (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )

    assert "ps -o args=" not in compose
    assert "['1', *Path('/proc/1/task/1/children').read_text().split()]" in compose
    assert "(Path('/proc') / pid / 'cmdline').read_bytes()" in compose
    assert "pid != str(os.getpid())" in compose
    assert '"CMD",' in compose


def test_production_beat_has_scheduler_memory_headroom():
    compose = (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    beat = compose.split("\n  beat:\n", maxsplit=1)[1].split("\n  migrate:\n", maxsplit=1)[0]

    assert "mem_limit: 256m" in beat
