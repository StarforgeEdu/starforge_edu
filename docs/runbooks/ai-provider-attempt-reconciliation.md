# AI provider attempt reconciliation

An AI request enters `uncertain` only when the durable pre-call marker exists but
no provider receipt was committed. The worker cannot know whether the remote
service accepted or billed that call. It deliberately does not retry: temporary
loss of that generation is safer than duplicate spend or duplicate domain writes.

## Find the quarantine

Run the read-only inventory and fail a release while any outcome is unresolved:

```bash
python manage.py check_ai_attribution --schema TENANT \
  --fail-on-uncertain-provider-outcome
```

Use the request's provider attempt timestamp and the restricted provider billing
console/support case to determine the outcome. Never infer “not charged” merely
because the generated response is absent locally.

## Reconcile a proven uncharged attempt

```bash
python manage.py reconcile_ai_provider_attempt \
  --schema TENANT \
  --request-id 123 \
  --outcome not_charged \
  --reference INC-2026-001 \
  --confirm-ambiguous-provider-outcome
```

This releases the conservative reservation and closes the request as failed. It
does not enqueue another completion. A product user may explicitly request a new
generation only through a separately reviewed re-drive policy; the same request
is never replayed automatically.

## Reconcile a proven charged attempt

Record the exact provider receipt and every token class shown by the provider:

```bash
python manage.py reconcile_ai_provider_attempt \
  --schema TENANT \
  --request-id 123 \
  --outcome charged \
  --reference ANTHROPIC-CASE-123 \
  --provider-request-id msg_... \
  --provider-stop-reason end_turn \
  --input-tokens 1200 \
  --output-tokens 240 \
  --cache-read-tokens 0 \
  --cache-creation-tokens 800 \
  --confirm-ambiguous-provider-outcome
```

The command replaces the reservation with audited actual usage and cost, then
closes the request as failed because its generated body was never durably
captured. Reconciliation outcome, reference, timestamp, provider receipt, and
cost evidence become immutable.

After either path, rerun `check_ai_attribution`. Keep the incident/provider case
referenced by the command under the organization's normal financial evidence
retention policy.
