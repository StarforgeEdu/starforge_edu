# Notification principal-attribution cutover

This migration is deliberately fail-closed: legacy notifications and
preferences become non-deliverable until a reviewed backfill proves one exact
role-native owner. It must not be deployed as a mixed-version rolling release.

Old workers do not understand `attribution_status`. For SMS, email, and push,
the old implementation can call an external provider before it attempts to
insert a `NotificationDelivery` row. The new database delivery trigger therefore
cannot make an already-running old worker safe. A rollback to the old image after
the migration is equally unsafe.

## Required release sequence

1. Build and record one immutable candidate image and revision. Run the
   notification, authorization, WebSocket, migration, and schema suites from it.
2. Put notification-producing tenant traffic into maintenance mode. Stop Beat,
   all old API/ASGI processes, and all old Celery producers and workers.
3. Record broker queue counts for release evidence. Do not purge the shared
   queues and do not restart an old worker to drain them after migration. The
   new worker accepts the legacy task argument shapes and quarantines any queued
   notification whose exact role recipient cannot be proven.
4. Take the reviewed atomic PostgreSQL, Redis broker, MinIO, and configuration
   backup and record broker queue counts on both sides of worker shutdown.
5. Apply `notifications.0012_recipient_principal_attribution` from the candidate
   image. Do not start any old revision after this point.
6. The production release script produces an owner-readable dry-run report
   before changing rows. For a separate rehearsal, use:

   ```sh
   python manage.py backfill_notification_principals \
     --report /secure/release-evidence/notification-principals-review.json
   ```

7. Review `resolvable`, `unresolved`, `conflicting`, and `quarantined` counts for
   every tenant. An ambiguous bridge user is not a resolvable recipient.
8. Supply the exact combined review SHA-256 to the release script. It verifies
   the quiesced evidence has not changed, records an `apply_started` marker, and
   runs the equivalent idempotent apply below while saving a second report:

   ```sh
   python manage.py backfill_notification_principals \
     --apply \
     --report /secure/release-evidence/notification-principals-applied.json
   ```

9. Start only the candidate Celery/Beat/API/ASGI image. Restore traffic after
   exact student, parent, teacher, and staff feed/WebSocket smoke tests pass.
10. Monitor quarantined creation counts, external delivery failures, WebSocket
    4401/4403/4429/1011 closes, and broker depth.

## Rollback boundary

After step 5, application rollback means a roll-forward fix on the new schema.
Never point an old worker at the migrated database. A true rollback requires the
pre-cutover database backup, the corresponding broker state, and maintenance
mode so no post-cutover notification or domain write is lost or replayed.
