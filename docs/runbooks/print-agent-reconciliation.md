# Branch print-agent lease and reconciliation runbook

Physical printing is not replay-safe. A daemon can download a document or send
it to a printer and then disappear before reporting success. The API therefore
never treats silence as proof that nothing printed.

## Release cutover

This is a non-rolling protocol migration. Stop old API, worker, beat, and branch
agent processes before applying `printing.0005_print_job_delivery_lease`. The
migration moves every legacy `picked` or `printing` row to
`reconciliation_required`; it does not requeue one.

Deploy the API/workers, upgrade every branch agent to the lease-aware protocol,
then start agents. An upgraded agent must:

1. retain the `lease_id` returned by claim only for that local attempt;
2. include it in every heartbeat and status report;
3. heartbeat well inside the configured lease (recommended every one third of
   `PRINT_AGENT_LEASE_SECONDS`, with jitter);
4. receive a successful `printing` status transition before submitting any
   bytes to a printer/spooler; a `failed` report while still `picked` is an
   assertion that no physical output was submitted and page progress is zero;
5. stop printing immediately after `print_reconciliation_required`, `404`, or
   authentication revocation;
6. never claim the same local spool entry again after an ambiguous response.

The signed download URL expires no later than the original claim lease. A
heartbeat extends processing time but does not mint another download URL.
An agent-reported failure is automatically retryable only while the job is
still `picked` with zero page progress. A failure after entering `printing` or
reporting any page progress is ambiguous/partial output and is quarantined for
the same human review as an expired lease.

## Monitoring and bounded quarantine

Beat runs `quarantine-stale-print-leases` every minute. It fans out to active
tenants and locks/quarantines at most
`PRINT_STALE_LEASE_SWEEP_BATCH_SIZE` rows per tenant invocation. It never
requeues, deletes, or signs a document.

The sweep never requeues a job.

Inspect one tenant without printing identifiers:

```bash
python manage.py check_print_reconciliation --schema TENANT
```

Use the release/readiness gate when no unresolved physical outcome is allowed:

```bash
python manage.py check_print_reconciliation \
  --schema TENANT \
  --fail-on-open-reconciliation
```

Alert when either count is non-zero for two sweep intervals. Also alert when
the count grows, when Beat/worker heartbeat is unhealthy, or when one branch
agent repeatedly expires leases. Never automate a retry from those alerts.

## Investigate one quarantined job

Use the branch-scoped print register. `printing:read` may view the state and its
append-only reconciliation history; `printing:write` in the exact job branch is
required to resolve it. Out-of-scope IDs return 404.

Collect positive evidence under an incident/reference number:

- inspect the physical output tray and printer queue/history;
- inspect the branch daemon's local spool and process journal;
- establish whether the document was downloaded and submitted to the printer;
- compare the recorded `pages_printed`, printer, agent, heartbeat, and expiry;
- revoke a lost or compromised branch-agent token before further work.

Absence of an API success response is not evidence that nothing printed.

## Record the reviewed outcome

Submit one exact outcome to
`POST /api/v1/printing/jobs/{id}/reconcile/` with a unique 16–128 character
`Idempotency-Key`, the evidence reference, an authenticated staff session, and
cookie CSRF when applicable.

- `confirmed_printed`: positive evidence proves the complete requested output;
  the job closes as `done` and is never replayed.
- `confirmed_not_printed`: positive evidence proves no physical output; only
  this outcome can return the job to `queued`, and the normal maximum-attempt
  bound still applies.
- `abandoned_unknown`: the outcome remains unknowable; the job closes as
  `failed` and is never replayed.

Exact retries with the same idempotency key, job, outcome, and evidence
reference return the recorded result. Reusing a key for different intent returns
409. Reconciliation rows and audits omit the raw idempotency key and lease
capability.

If only partial output is proven, use `abandoned_unknown`. Do not label it
`confirmed_not_printed`; any replacement/partial-page request must be a separate,
explicitly approved workflow outside automatic recovery.

## Queue-request idempotency

An open job is keyed by branch, source, source ID, and server-derived object key.
An exact retry with identical pages, copies, color, duplex, and cohort returns
that job. Reusing the same key/source with different physical options returns
409 `print_idempotency_conflict`; the existing request is not modified.
