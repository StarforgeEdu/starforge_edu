# Production release cutover

This runbook is the authoritative production path for the security-hardening
release. It is intentionally non-rolling. The old web, ASGI, Beat, and every
Celery worker are stopped before the quiesced backup and remain stopped once a
tenant migration can have committed.

Never deploy a branch name or a short SHA. `scripts/launch_production_deploy.sh`
accepts one exact 40-character commit, checks out a disposable detached tree,
and executes the privileged deploy orchestrator only from those verified commit
bytes. The orchestrator proves that the commit is reachable from the
configured approved remote ref, verifies every required GitHub check, builds
that detached tree, and checks both the OCI revision label and the revision
embedded inside the image.

## Release boundary

The mechanical maintenance gate covers the complete migration set declared in
`check_safeguarding_encryption_cutover.REQUIRED_MIGRATIONS`, including:

- safeguarding encryption and family/student history protection;
- AI request scope/role-principal capture, output encryption, retention purge,
  and quarantine of legacy requests whose historical authority cannot be
  proven;
- payment historical scope, irreversible webhook privacy scrub, and external
  provider transaction uniqueness;
- notification, messaging, forms, meeting, task, and branch-transfer exact
  role-principal attribution;
- audit and finance immutable historical scope;
- organization settings, structure, and transfer-history integrity;
- assessment publication integrity, owner authority, compensation permission,
  report indexes, upload-grant cleanup, and approval idempotency.

An old process may not share a tenant schema after any part of this boundary is
applied. The deployment script stops named Compose services and also searches
all running containers by Compose project/service labels, which catches
one-off workers that `docker compose stop` does not own.

## Before the window

1. Choose the exact reviewed commit and set
   `STARFORGE_APPROVED_REMOTE_REF` to the fully qualified remote-tracking ref if
   it differs from the release branch default.
2. Confirm the required GitHub checks are successful for that exact commit.
   There is no unverified-CI override.
3. Rehearse the migration and smoke manifest against a restored,
   production-sized snapshot. Record duration, table counts, query timing, and
   disk headroom.
4. Complete the payment-evidence retention decision in
   `safeguarding-encryption-cutover.md`. Migration `payments.0007` destroys raw
   legacy provider payloads and source IPs.
5. Verify the stable `FIELD_ENCRYPTION_KEY` through a one-way fingerprint in
   every workload. Never print or copy the key into release evidence.
6. Confirm the deployment, application, static-writer, database, MinIO, backup,
   and Firebase files are root-owned regular files with owner-only permissions.
   `app.env` must contain only the media-runtime storage credential;
   `static-storage.env` must contain a different static-writer credential and
   is consumed only by the one-shot `collectstatic` service. Do not use a
   symlink for a secret or evidence path. Run the explicit storage configurator
   after credential rotation, then require the read-only storage verifier to
   pass its exact-policy, no-group, cross-bucket, ListAllMyBuckets, and bucket
   administration denial probes before the release window.
7. Schedule enough maintenance time for two operator phases. The application
   deliberately remains unavailable between review and apply.

## Phase 1: quiesce, preserve, migrate, review

Run as root with the exact candidate revision as both the argument and the
one-shot maintenance acknowledgement:

```sh
candidate=0123456789abcdef0123456789abcdef01234567
STARFORGE_MAINTENANCE_CUTOVER="$candidate" \
  /root/starforge_edu/scripts/launch_production_deploy.sh "$candidate"
```

The script performs these gates in order:

1. exact revision, approved-ref, CI, OCI label, embedded revision, pinned
   infrastructure-image, and Django production checks;
2. a read-only migration preflight across every tenant while the old release is
   still serving;
3. privacy-safe broker-depth evidence;
4. graceful shutdown of every old application process and proof that none is
   still running;
5. a second read-only preflight against the quiesced database;
6. one encrypted Restic snapshot containing the PostgreSQL dump, a point-in-time
   Redis broker RDB, MinIO objects, and root-only deployment configuration;
7. checksum, Restic integrity, PostgreSQL restore, and Redis RDB verification;
8. shared and tenant migrations from the candidate image;
9. proof that the complete maintenance migration set is recorded in every
   tenant;
10. read-only notification, finance, audit, workflow, and AI attribution
    reports. The AI report also fails closed if expired sensitive output remains
    stored after the migration.

The expected first-phase result is exit status `79`. This is an intentional
review pause, not permission to restart an old service. The script writes a
root-only `cutover_review_pending` marker and prints the review SHA-256. All
application services remain stopped. Immediately before the first schema write,
the script atomically pins `APP_IMAGE` in the persistent Compose environment to
the candidate. A generic later `docker compose up` therefore cannot resolve to
the pre-cutover tag; the verified backup preserves the old environment for a
full restore.

Review every file below
`/root/starforge-deploy/release-evidence/<revision>/`. In particular:

- reconcile preflight issue counts (all blocking counts must already be zero);
- record the quiesced backup snapshot and before/after broker depths;
- review resolvable, unresolved, conflicting, and quarantined notification
  ownership;
- review every finance conflict/evidence row;
- review audit scoped/organization/unresolved/quarantined counts;
- review unresolved workflow and branch attribution as intentionally hidden,
  never guessed data.
- review unresolved legacy AI requests as intentionally non-replayable and
  non-authoritative. The migration never assigns current branch or principal
  state to historical requests. Preserved broker jobs resume only under the
  candidate workers, which re-authorize before any provider call and fail
  closed for unresolved legacy attribution.

The evidence directory and reports are mode `0700`/`0600`. They may contain
internal row identifiers; do not copy them into chat, tickets, or public CI
artifacts.

## Phase 2: reviewed apply and candidate startup

After an authorized reviewer accepts the exact report set, rerun the same
revision with its digest:

```sh
candidate=0123456789abcdef0123456789abcdef01234567
review_sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
STARFORGE_MAINTENANCE_CUTOVER="$candidate" \
STARFORGE_REVIEWED_BACKFILL_SHA256="$review_sha256" \
  /root/starforge_edu/scripts/launch_production_deploy.sh "$candidate"
```

The script verifies that the candidate image ID, backup snapshot, revision, and
freshly regenerated quiesced report digest are unchanged. It then records an
`apply_started` marker before doing any write. Notification, finance, and audit
commands apply only deterministic resolutions; ambiguous rows remain
non-deliverable, conflicting, quarantined, or unresolved. Each command is
idempotent, so a process interruption can safely continue with the same review
digest. AI and workflow reports remain read-only: unresolved AI history is
quarantined rather than guessed, while expired retained content is a hard gate.
A `backfills_applied` marker prevents the review from being replayed if
readiness fails later.

Only after post-apply evidence and broker depth are captured does the script
start the candidate services. It verifies every service's Docker image ID,
waits for external readiness, hashes legacy session credentials, captures final
broker depth, and atomically records revision, image ID, and backup snapshot in
`current_release`.

## Failed cutover recovery

A failure before migrations automatically restarts the prior immutable image.
Once migration execution begins, automatic application rollback is forbidden.
The script stops all application services and writes `cutover_failed` with the
candidate/previous image IDs and preserved backup snapshot.

After reviewing the failure, retry only the same candidate as a forward fix:

```sh
candidate=0123456789abcdef0123456789abcdef01234567
STARFORGE_RESUME_FAILED_CUTOVER="$candidate" \
STARFORGE_MAINTENANCE_CUTOVER="$candidate" \
  /root/starforge_edu/scripts/launch_production_deploy.sh "$candidate"
```

This path reuses and re-verifies the original pre-cutover backup; it never
overwrites that recovery point with a partially migrated database. It confirms
that application containers remain stopped, reruns idempotent migrations, and
enters the same human-review phase.

A full rollback after schema change is an incident recovery, not an application
redeploy. Keep ingress in maintenance, restore PostgreSQL, Redis broker, MinIO,
and deployment configuration from the same verified Restic snapshot, verify the
restored revision boundary, and only then start the recorded previous image.
Restoring only the database can lose or replay queued provider/notification
work. Never point an old worker at the migrated database.

## Post-release smoke

Before removing maintenance mode, exercise the exact candidate with a director
and scoped department head:

- identity bootstrap, effective permissions, branch/department isolation, and
  out-of-scope 404 behavior;
- executive summary permission pruning and freshness metadata;
- safeguarding redaction/decryption and one authorized ciphertext update;
- payment idempotency, duplicate provider transaction rejection, and fiscal
  outbox recovery;
- notification feed/WebSocket principal isolation and one delivery in each
  enabled channel;
- messaging thread exact-participant WebSocket isolation, pointer-only payload,
  disconnect recovery by sequence, and monotonic message-ID read state (follow
  `messaging-thread-realtime-cutover.md`);
- finance/audit historical-scope filters and quarantined-row exclusion;
- readiness, worker heartbeat, broker depth, and external provider failure
  monitoring.

Record the backend revision, image ID, backup snapshot, review digest, smoke
accounts' role/scope (never credentials), and result in the private release
evidence package.
