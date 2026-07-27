# Adding a new app

```bash
mkdir -p apps/<name>/migrations apps/<name>/tests
touch apps/<name>/{__init__,apps,models,admin,serializers,views,urls,services,selectors}.py
touch apps/<name>/{migrations,tests}/__init__.py
```

1. **`apps.py`** — set `name = "apps.<name>"` and a unique `label`. If `<name>` collides with a Django contrib label, pick a different label (e.g. `auth_app`, `ai_app`).
2. **Register in settings** — add `"apps.<name>.apps.<Name>Config"` to `TENANT_APPS` (or `SHARED_APPS` if it's truly platform-level data).
3. **Wire URLs** — add `path("<name>/", include("apps.<name>.urls"))` to `config/urls.py`.
4. **Permissions** — add the resource to `core.permissions.ROLE_PERMISSION_MATRIX` and set `resource = "<name>"` on each ViewSet (per-action verbs are derived; custom `@action`s need an entry in `required_perms`). Views with no mapping are fail-closed.
5. **Migrations** — `python manage.py makemigrations <name>` then `migrate_schemas`.
6. **Tests** — `apps/<name>/tests/test_<feature>.py` (pytest-django picks them up automatically).

Avoid:
- Cross-app FKs from one role app to another (`students` ↔ `teachers`). Route through `cohorts`, `attendance`, or `academics`.
- Calling channel adapters (sms/email/push) directly from a domain app — emit a signal and let `apps.notifications` route.
- Adding sync HTTP calls to external services in request handlers — push to Celery.
