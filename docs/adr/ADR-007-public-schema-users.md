# ADR-007 — Public-schema users for platform staff (TD-3)

**Status:** Accepted (Day 1)
**Context:** ROADMAP TD-3; DAY-1 Lane B (D1-LB-1)

## Context

`apps.users` and `apps.auth` were originally in `TENANT_APPS` only, so the
`users_user` table existed exclusively inside tenant schemas. That left the
**public** schema with no user table, which broke two things on the apex
(non-tenant) host:

- `http://localhost:8000/admin/` — Django admin needs a `request.user` from
  `AUTH_USER_MODEL` resolvable in the active (public) schema.
- `IsAdminUser` on the platform API (`/api/v1/platform/...`) — there was no
  public user to be a platform staff member.

## Decision

Add `apps.users`, `apps.auth`, and `rest_framework_simplejwt.token_blacklist`
to `SHARED_APPS` **in addition to** keeping them in `TENANT_APPS`. django-tenants
then creates these tables in the public schema *and* in every tenant schema:

- **Public** `users_user` holds **platform staff only** (the owner/operators).
- **Tenant** `users_user` holds that Center's people, fully isolated as before.

`scripts/seed_dev.py` creates a public-schema platform superuser
(`+998900000000`) separate from each tenant's own superuser.

### Consequence: `RoleMembership` FK constraints

`RoleMembership` lives in the (now shared) `users` app but its `branch` /
`department` FKs point at `org` models, which are **tenant-only**. In the public
schema those `org_*` tables do not exist, so a real DB-level FK would make
`migrate_schemas --shared` fail. We set `db_constraint=False` on those two FKs:

- Public `users_rolemembership` is created without a dangling reference.
- Tenant schemas keep integrity at the ORM/service layer (platform staff never
  hold RoleMemberships, so no public rows reference `org`).

django-tenants' `TenantSyncRouter` skips the `org` table creation in public
while still satisfying the migration-graph dependency, so the graph stays
consistent across `--shared` and `--tenant` runs.

## Alternatives considered

- **Move `RoleMembership` to a tenant-only app.** Cleaner FK story, but a larger
  refactor that ripples through serializers/permissions; deferred.
- **A separate `PlatformUser` model on public.** Two user models complicate
  auth, JWT, and admin. Rejected.

## Status of related work

- TD-1 tenant-bound JWT (`core/authentication.py`) is unaffected: a public token
  carries `schema="public"` and only validates on the public host.
- Day 4 Lane E (control center, TD-10) builds the platform API surface on top of
  this foundation.
