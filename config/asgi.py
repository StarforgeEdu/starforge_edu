"""ASGI entrypoint with Channels routing.

In dev: `daphne config.asgi:application` (or via docker compose).
"""

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

# Fail-safe default (matches wsgi.py): if DJANGO_SETTINGS_MODULE is unset, assume
# production rather than leaking DEBUG/wildcard-hosts/throwaway keys. Dev + Docker
# set it explicitly.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

# Initialize Django ASGI app first, then import everything else (the imports
# below depend on apps being loaded).
django_asgi_app = get_asgi_application()

from infrastructure.websocket.middleware import TenantAwareAuthMiddleware  # noqa: E402
from infrastructure.websocket.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": TenantAwareAuthMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
