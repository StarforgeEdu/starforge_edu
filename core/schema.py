"""drf-spectacular extensions.

Registering an `OpenApiAuthenticationExtension` for the custom
`SessionAuthentication` teaches the schema generator to emit a `bearer` security
scheme (the opaque session key) instead of warning "could not resolve
authenticator" on every view. Importing this module is enough to register it
(drf-spectacular auto-registers extension subclasses on definition); `config.urls`
imports it.
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class SessionKeyScheme(OpenApiAuthenticationExtension):
    target_class = "core.session_auth.SessionAuthentication"
    name = "sessionAuth"

    def get_security_definition(self, auto_schema):
        # Opaque session key presented as `Authorization: Bearer <key>`.
        return {"type": "http", "scheme": "bearer"}
