# Adding a new app

```bash
mkdir -p apps/<name>/{migrations,tests,dto,interfaces,repositories,services/v1,views/v1}
touch apps/<name>/{__init__,apps,models,admin,urls,presenters,selectors}.py
touch apps/<name>/{migrations,tests}/__init__.py
```

1. **`apps.py`** — set `name = "apps.<name>"` and a unique `label`. If `<name>` collides with a Django contrib label, pick a different label (e.g. `auth_app`, `ai_app`).
2. **Register in settings** — add `"apps.<name>.apps.<Name>Config"` to `TENANT_APPS` (or `SHARED_APPS` if it's truly platform-level data).
3. **Wire URLs** — add `path("<name>/", include("apps.<name>.urls"))` to `config/urls.py`.
4. **Layering** — define request DTOs, service/repository interfaces, ORM repositories, one application service, thin views, and presenters. Bind interfaces to implementations in `AppConfig.ready()` through `core.container`.
5. **Permissions** — add grants to `core.permissions.ROLE_PERMISSION_MATRIX`; decorate each view with `@require_auth`, call `check_perm(request, "<resource>:read|write|approve")`, and resolve rows only through a scope-aware selector/repository.
6. **HTTP contract** — use strict `core.http` parsers and `core.responses` so malformed input never becomes a 500 and every response uses the flat envelope. Support HEAD only where GET has no write side effect.
7. **Migrations** — `python manage.py makemigrations <name>` then `migrate_schemas`.
8. **Tests** — cover every route/method, permission branch, cross-tenant and cross-branch ID, malformed/null body, query budget, and service invariant.
9. **Schema** — run `python scripts/export_openapi.py --validate` and verify the new paths and methods are present.

Avoid:
- Cross-app FKs from one role app to another (`students` ↔ `teachers`). Route through `cohorts`, `attendance`, or `academics`.
- Calling channel adapters (sms/email/push) directly from a domain app — emit a signal and let `apps.notifications` route.
- Adding sync HTTP calls to external services in request handlers — push to Celery.
