# Safeguarding, privacy, and notification-identity cutover

This release changes three tenant columns from plaintext to authenticated
Fernet ciphertext:

- `parents_parentprofile.notes`;
- `parents_guardian.custody_notes`;
- `students_studentprofile.emergency_contacts`.

The migrations use encrypted shadow columns, bounded keyset backfill, an exact
authenticated readback, and only then drop and rename the legacy columns. They
are deliberately atomic. On PostgreSQL, the schema locks are retained until
commit, so this is a maintenance-window migration, not an online migration.

The same mechanical maintenance gate also covers payments migration
`0007_webhook_privacy_and_txn_integrity`. That migration removes legacy raw
webhook and provider-attempt columns after privacy scrubbing; preceding web and
worker images still reference those columns. It must therefore run only after
all application processes are drained. The payment migration does not change
the field-encryption-key requirements below.

It also covers notifications migration
`0012_recipient_principal_attribution`. That migration replaces shared bridge-
user ownership with immutable student/teacher/parent/staff attribution. An old
SMS, email, or push worker can contact a provider before it writes its delivery
row, so a database trigger alone cannot make a mixed-version deployment safe.
Queued task payloads remain readable by the new worker and fail closed when the
recipient cannot be proven; do not restart or drain them with an old worker after
the schema change. The detailed classification procedure is in
[`notification-principal-cutover.md`](notification-principal-cutover.md).

## Preconditions

1. Take and restore-test a database backup. Do not waive this gate.
2. Verify the production `FIELD_ENCRYPTION_KEY` is present in the secrets
   manager and in every web, ASGI, Celery, and migration workload. Compare a
   one-way fingerprint of the decoded key; never print the key.
3. Confirm the key is the same durable key that encrypted all existing
   `EncryptedTextField` and `EncryptedCharField` values. Do not generate a new
   key and do not rotate it during this deployment.
4. Run the exact migration and safeguarding test manifest from the candidate
   image against a disposable production-like database.
5. Estimate the maintenance window from a restored production snapshot. Both
   affected tables are locked while the backfill and verification run.

### Payment evidence retention decision

Before approving payments migration `0007`, the privacy/compliance owner must
record whether any legacy raw webhook body, source IP, or payment-attempt
request/response body has a legal retention requirement. The default decision is
deletion: these fields can contain personal and payment data and are not needed
for replay protection or normal reconciliation.

If counsel or a regulator requires retention, export only the minimum required
records before the maintenance backup. The export must be encrypted, integrity-
checked, access-restricted to the named compliance custodians, assigned a
documented expiry/deletion date, and stored outside application logs and normal
operator-accessible object storage. Record the export identifier and approval in
the release evidence; never record payload contents or encryption keys there.

Migration `0007` first overwrites every legacy `WebhookEvent.payload` and
`remote_ip`, and every `PaymentAttempt.request_payload` and `response_payload`,
then removes the obsolete columns. This privacy scrub is intentionally
irreversible. Reversing the Django migration or rolling back application code
cannot reconstruct those values; only restoring the verified pre-migration
database backup (under maintenance isolation, with the corresponding application
and broker boundary) can recover them.

## Cutover

1. Follow
   [`production-release-cutover.md`](production-release-cutover.md) and invoke
   the reviewed deploy script with the exact candidate SHA as both its argument
   and one-shot acknowledgment:
   `STARFORGE_MAINTENANCE_CUTOVER=<40-character-sha>`. Do not store that
   acknowledgment in a long-lived environment file. The image embeds the same
   revision and the migration entrypoint requires all three values to match.
2. The script stops web, ASGI, every Celery worker, and beat, then proves none is
   still running. PostgreSQL, Redis, and object storage remain online. It takes
   and restore-tests a new atomic database, Redis broker, object, and
   configuration backup only after writes are quiesced; `SKIP_BACKUP=1` is
   rejected.
3. The migration container independently requires the acknowledgment before it
   runs. It executes `migrate_schemas --shared` and `migrate_schemas --tenant`
   from the immutable candidate image. A failed tenant transaction preserves
   that tenant's plaintext columns; never force or fake the migration as applied.
4. Confirm every tenant reports the complete migration set declared in
   `REQUIRED_MIGRATIONS` as applied. This includes parents `0009`, payments
   `0007`/`0008`, notifications `0012`, students `0010`, org `0020`/`0021`, and
   audit/report/role-principal cutovers. Investigate any tenant independently
   before proceeding.
5. Using a restricted database session, verify non-empty values in the three
   columns are Fernet tokens and that known plaintext samples are absent. Do
   not export field contents into logs or release evidence.
6. Start only the candidate web/ASGI/workers. A full restart is mandatory
   because `core.fields._fernet` caches its key for the process lifetime.
7. Exercise an authorized safeguarding reader and a directory-only reader in
   two branches. The authorized reader sees the decoded values; the directory
   reader receives no collection field and `null` on detail. Exercise one
   safeguarding update and confirm raw storage remains ciphertext.
8. Remove maintenance mode only after readiness and the smoke checks pass.

## Failure and rollback

- A failure after services stop but before migration begins automatically
  restarts the previous immutable image.
- Once migration begins, the deploy script never starts the previous image
  automatically. If migration or readiness fails it stops all application
  services and writes a root-only `${STARFORGE_DEPLOY_DIR}/cutover_failed`
  marker containing only candidate/previous image identifiers and the verified
  backup snapshot ID. PostgreSQL, Redis, and object storage remain available
  for recovery.
- Before a tenant migration commits, fix the cause and retry; atomic DDL and
  data changes roll back together. Because `migrate_schemas` processes tenants
  independently, a later failure can leave earlier tenants committed. Keep the
  whole application offline until every tenant reports `clear`.
- Reverting migration `0009` or `0010` is data-preserving but intentionally
  restores plaintext. It is an emergency action requiring the same maintenance
  isolation, a fresh backup, and security-owner approval.
- Reversing payments migration `0007` can recreate column shape but cannot
  restore scrubbed webhook/IP/provider-attempt values. Recovery of that evidence
  requires the verified pre-migration backup or the separately approved encrypted
  compliance export described above.
- Prefer a forward fix with the same dual-shape-aware candidate. A universal
  rollback restores the verified quiesced database/object/configuration snapshot
  before starting the recorded previous image. A hand-written partial rollback
  is not universal because the candidate may contain other migrations.
- Do not roll application containers back to an old image while encrypted
  columns remain deployed. A reviewed targeted reversal of migration `0009`
  and `0010` is data-preserving but restores plaintext and is acceptable only
  when migration planning proves no other schema change depends on them.
- A decryption error is an integrity incident (wrong key, corrupt token, or
  tampering). Preserve the database backup and logs, but never copy the raw
  value into tickets or chat.
