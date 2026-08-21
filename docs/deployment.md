# Deployment

v1 ships with a working compose stack for dev. Real production deployment
is out of scope for v1, but the shape is:

| Service | Container | Ports |
|---|---|---|
| Web (HTTP) | gunicorn + `config.wsgi` | 8000 |
| Realtime (ASGI) | daphne + `config.asgi` | 8001 |
| Worker | celery worker | — |
| Beat | celery beat | — |
| Postgres | 16+ (managed) | 5432 |
| Redis | 7+ (managed) | 6379 |
| Object storage | AWS S3 (prod) / MinIO (self-host) | — |
| Reverse proxy | nginx / traefik / caddy | 80/443 |

## Subdomain TLS
For `*.starforge.uz`, use a wildcard cert via Let's Encrypt DNS-01
(certbot with the relevant DNS plugin, or the cert-manager DNS01
solver on Kubernetes). HTTP-01 cannot issue wildcards.

## Secret management
`.env` for dev only. In prod use the platform's secret manager (AWS SSM,
Vault, k8s secrets) and inject as env vars matching the `env(...)` keys
in `config/settings/base.py`.

Production object-storage authority is intentionally split across two
root-owned files below `STARFORGE_DEPLOY_DIR`:

- `app.env` contains one non-root media-runtime credential. Its reviewed MinIO
  policy permits object I/O only in the private media bucket and explicitly
  denies the public static bucket. It also sets
  `AWS_EC2_METADATA_DISABLED=true` and `STATIC_STORAGE_WRITE_ENABLED=False`.
- `static-storage.env` contains a different non-root static-writer credential.
  Compose exposes it only to the one-shot `collectstatic` service. Long-running
  web, ASGI, worker, and Beat processes use a URL-only static backend with no
  S3 client or credential-provider fallback. Compose also removes the media
  credential from the final `collectstatic` environment, and production
  settings reject any process that receives both authorities.

Install both files as root-owned regular files with mode `0400` or `0600`.
Create/update buckets and the exact versioned policies only through
`scripts/configure_production_storage.sh`, then run the read-only
`scripts/verify_production_storage.sh`. The verifier proves both positive paths
and denial of bucket discovery, bucket administration, cross-bucket reads and
writes, additional direct policies, and inherited group policy. MinIO root
credentials are confined to a digest-pinned operator container and never enter
an application container. Both credential files are included in the same
encrypted deployment-configuration backup and restore drill.

Credential-bearing provider endpoints also require an exact hostname
allowlist. Configure `SOLIQ_API_URL` and `SOLIQ_API_ALLOWED_HOSTS` as one
reviewed pair; for example, the deliberately non-routable validation pair is
`https://soliq.example.invalid` and `soliq.example.invalid`. Never use a
wildcard, URL/path, or IP address in the hostname list. Follow the
[provider endpoint allowlist runbook](runbooks/provider-endpoint-allowlists.md)
before enabling an integration.

## Migrations

Do not run tenant migrations manually against a serving production release.
The current hardening release is one mechanically enforced non-rolling
boundary: it includes safeguarding, privacy, exact-principal attribution,
immutable finance/audit/organization history, assessment integrity, and the
other migrations declared by the cutover command. Follow
[`runbooks/production-release-cutover.md`](runbooks/production-release-cutover.md).

The immutable deployment script requires an exact 40-character approved
revision, stops all old application processes, restore-tests the database,
broker, object, and configuration snapshot, and pauses for a human-reviewed
backfill evidence digest before candidate startup.

```bash
python manage.py migrate_schemas --shared           # public schema
python manage.py migrate_schemas                    # all tenant schemas
```

Adding a tenant runs all `TENANT_APPS` migrations on the new schema
automatically (Center.auto_create_schema=True).

## First tenant director

The role-native staff API cannot create a tenant's first owner because that API
correctly requires an existing owner-authorized session. After provisioning at
least one branch, an operator can bootstrap exactly one first director from an
application container:

```bash
python manage.py bootstrap_tenant_director \
  --schema center_schema \
  --branch central \
  --username admin \
  --first-name Amina \
  --last-name Director \
  --email amina@example.com
```

The command refuses the public schema, unknown/inactive tenants, inactive
branches, missing recovery contacts, and any tenant that already has an active
director. It generates a strong one-time password, prints it once, stores only
its Django hash, and forces a password change at first login. Never run the
development CEO seed in production and never install a shared default such as
`root`.

## Backup

Do not use a per-tenant dump as the release recovery point. The production
script creates one encrypted Restic snapshot containing a PostgreSQL custom
dump, point-in-time Redis broker RDB, MinIO mirror, checksums, and deployment
configuration, then validates PostgreSQL and Redis restoreability before any
migration starts. Database-only rollback can lose or replay queued financial
and notification work.

## Out of scope for v1
- Container orchestration (k8s manifests, Helm charts)
- Observability (Sentry, Prometheus, Grafana)
- Rate-limiting at the edge
- Branch print agent deployment (separate repo)
