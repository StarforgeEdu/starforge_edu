"""Fail-closed environment checks shared by local demonstration seed scripts."""

from __future__ import annotations

import os
import socket
from ipaddress import ip_address
from pathlib import Path
from typing import Any

LOCAL_SEED_CONFIRMATION_ENV = "STARFORGE_ALLOW_LOCAL_DEMO_SEED"
DEVELOPMENT_SETTINGS_MODULE = "config.settings.development"


def _database_target_is_local_or_private(settings_obj: Any, *, project_root: Path) -> bool:
    database = settings_obj.DATABASES.get("default", {})
    engine = str(database.get("ENGINE", ""))
    if engine.endswith("sqlite3"):
        name = str(database.get("NAME", ""))
        if name == ":memory:":
            return True
        if not name:
            return False
        try:
            Path(name).resolve().relative_to(project_root.resolve())
        except ValueError:
            return False
        return True

    host = str(database.get("HOST", "")).strip()
    if not host or host.startswith("/"):
        # Empty host / filesystem path means a local Unix-domain socket.
        return True

    port = str(database.get("PORT", "") or "5432")
    try:
        addresses = {
            ip_address(str(sockaddr[0]).split("%", 1)[0])
            for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_loopback or address.is_private for address in addresses)


def assert_local_seed_environment(settings_obj: Any, *, project_root: Path) -> None:
    """Reject every seed run that is not explicitly local development."""

    settings_module = str(getattr(settings_obj, "SETTINGS_MODULE", ""))
    if settings_module != DEVELOPMENT_SETTINGS_MODULE or not bool(settings_obj.DEBUG):
        raise RuntimeError("Local demo seeding requires config.settings.development with DEBUG enabled.")
    if os.getenv(LOCAL_SEED_CONFIRMATION_ENV, "").strip() != "1":
        raise RuntimeError(f"Set {LOCAL_SEED_CONFIRMATION_ENV}=1 for this one local seed invocation.")
    if not _database_target_is_local_or_private(settings_obj, project_root=project_root):
        raise RuntimeError("Local demo seeding requires a loopback or private database target.")
