# Messaging thread realtime cutover

This runbook promotes `starforge.messaging.thread.v1`. The WebSocket is an
authorized delivery accelerator; `Message`, `ThreadParticipant`, and
`ThreadRealtimeEvent` rows are the durable truth.

## Safety invariants

- Resolve the tenant from the validated Host before touching tenant data.
- Authenticate with the host-only secure cookie or `bearer.<opaque-session>`
  WebSocket subprotocol. Never put a credential in a URL.
- A socket must match one live role-native session principal, an active
  `messaging:read` grant, and that exact principal's deliverable thread seat.
  Recheck all three before every push and on every heartbeat.
- Redis group names contain only tenant schema and numeric thread ID. Channel
  payloads contain event pointers only—never subject, body, attachment key, or
  participant display name.
- `ThreadRealtimeEvent.sequence` is allocated while the Thread row is locked.
  Live channel hints are best-effort and can be missing, duplicated, or out of
  order across producers. Durable recovery pages are ascending; detect gaps,
  recover them, and deduplicate by sequence.
- `last_read_message_id` is inclusive and monotonic. Prefer an explicit
  `through_message_id`; an omitted value is legacy behavior that snapshots the
  current final message under the same lock.
- Typing, delivery receipts, and presence are not provided. The contact
  directory's `recently_active` value is only a five-minute request heuristic.

## Pre-deployment review

1. Build and test the immutable candidate image. Confirm:

   ```text
   python manage.py check --deploy
   python manage.py makemigrations messaging --check --dry-run
   python manage.py migrate_schemas --plan
   pytest apps/messaging/tests/test_realtime_thread.py
   pytest apps/messaging/tests tests/test_realtime_ws.py tests/unit/test_websocket_security.py
   ```

   After migrations on the restored snapshot, run the privacy-safe cutover
   report and keep its JSON with the release evidence:

   ```text
   python manage.py review_messaging_realtime_cutover --fail-on-blocked
   python manage.py review_messaging_realtime_cutover --fail-on-unresolved
   ```

2. On a restored production snapshot, record per-tenant counts before applying
   migration `messaging.0007_thread_realtime_protocol`:

   ```sql
   SELECT count(*) AS messages,
          count(*) FILTER (WHERE sender_id IS NULL) AS deleted_sender_rows
   FROM messaging_message;

   SELECT attribution_status, count(*)
   FROM messaging_threadparticipant
   GROUP BY attribution_status ORDER BY attribution_status;
   ```

3. The migration resolves a historical message sender only from the unique,
   deliverable seat with the same `(thread_id, user_id)`. Unresolved or
   conflicting rows remain explicitly unattributed; never guess them from a
   different role profile. The migration deterministically maps each legacy
   `last_read_at` to the final message at or before that timestamp.

4. Confirm the ASGI edge preserves `Sec-WebSocket-Protocol`, routes
   `/ws/messaging/threads/` to Daphne, enforces TLS, and does not log headers or
   frames. Do not add `WEBSOCKET_ALLOW_QUERY_TOKEN`; runtime rejects URL tokens
   regardless of configuration.

## Client cutover

For an existing local cursor `C`:

1. Open `/ws/messaging/threads/{thread_id}/` and wait for `thread.ready`.
2. Send `{"type":"thread.sync","after":C,"limit":50}` repeatedly while
   `has_more` is true. The HTTP fallback is
   `/api/v1/messaging/threads/{id}/events/?after=C&limit=100`.
3. Persist a cursor only after all effects for that sequence have committed in
   the client. Ignore duplicates and buffer/recover gaps.
4. For `message.created`, fetch content from the scoped message list using the
   last processed message ID. For `read.updated`, update only that actor's
   inclusive `message_id` boundary.
5. If `reset_required` is true, discard derived thread state, fetch thread and
   messages fully, then persist `high_watermark` as the new baseline.
6. Reply to each server ping with pong. Back off reconnects with jitter from one
   to thirty seconds; 4401 requires reauthentication and 4403 requires an
   authorization/scope change rather than blind retries.

## Post-migration verification

For each tenant under review:

```sql
SELECT sender_attribution_status, count(*)
FROM messaging_message
GROUP BY sender_attribution_status ORDER BY sender_attribution_status;

SELECT count(*) AS invalid_read_cursor
FROM messaging_threadparticipant participant
JOIN messaging_message message ON message.id = participant.last_read_message_id
WHERE message.thread_id <> participant.thread_id;

SELECT thread_id, min(sequence), max(sequence), count(*)
FROM messaging_threadrealtimeevent
GROUP BY thread_id
HAVING min(sequence) <> 1 OR max(sequence) <> count(*);
```

Exercise one teacher/student thread and one authorized staff/parent thread:

- connect each participant and prove an outsider receives 4403;
- post a body containing a unique canary and prove the canary appears in REST
  but not the Redis/group event or infrastructure logs;
- disconnect, post two messages, reconnect from the prior cursor, and recover
  both sequences once after deduplication;
- revoke the role membership or exact thread seat and prove the open socket
  closes 4403 before another pointer is delivered;
- revoke the session and prove the socket closes 4401;
- mark read through an older message while a newer message arrives and prove
  the newer message stays unread.

## Rollback

Do not run an old application against the migrated schema. The migration adds
columns, constraints, validation triggers, and an append-only event table.
Before traffic, rollback means restore the reviewed database snapshot and the
matching prior image together. After traffic, prefer a forward fix: restoring
the database would discard committed messages, read states, and event cursors.
