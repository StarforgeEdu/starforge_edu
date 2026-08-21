# Intelligence latency incident evidence ledger

Snapshot time: 2026-08-10 22:52 Asia/Tashkent (17:52 UTC)

Release commit under review: `9a4e281c7b52f5bf12b4f37c722211cc8415326a`

## Reporting job

- Question: what caused minute-long management-dashboard loads, how large was the remediation, and what capacity is appropriate for production?
- Audience: technical operators and backend maintainers.
- Scope: production StarForge intelligence endpoints, the production-size seeded tenant, and the 2026-08-10 remediation.
- Baseline: the incident state before the set-based selector rewrite, Redis private-snapshot cache, response compression, and the one-to-two-vCPU resize.
- Decision criterion: preserve authorization correctness while returning repeated management snapshots in well below one second on the current tenant.

## Observed production evidence

### Pre-fix request distribution

The production backend-duration sample covered 20 minutes during the incident.

| Statistic | Backend duration (seconds) |
|---|---:|
| Median (p50) | 0.121 |
| p90 | 0.923 |
| p95 | 2.400 |
| p99 | 49.863 |
| Maximum | 58.141 |

Approximately 98.6% of requests completed in under one second. The slow tail was concentrated in intelligence reads rather than being a uniform slowdown.

### Post-deploy request distribution

The eight-minute production web sample after deployment contained 137 requests.

| Statistic | Backend duration (seconds) |
|---|---:|
| Median (p50) | 0.0291 |
| p95 | 0.3322 |
| p99 | 0.7776 |
| Maximum | 1.4249 |

Only one request exceeded one second and none exceeded five seconds. A separate authenticated smoke suite completed 108 operations successfully. This short observation confirms immediate recovery but is not a long-running service-level distribution.

### Intelligence endpoint and selector observations

| Measurement | Before | After |
|---|---:|---:|
| Student-risk computation | 30–58 seconds | 0.228 seconds, selector benchmark |
| Student-risk endpoint, cold private snapshot | 30–58 seconds | 0.1086 seconds |
| Student-risk endpoint, warm private snapshot | 30–58 seconds | 0.0228 seconds |
| Executive-summary computation | More than 60 seconds / worker timeout | 0.0887 seconds, selector benchmark |
| Executive-summary endpoint, Redis hit A | More than 60 seconds / worker timeout | 0.0540 seconds |
| Executive-summary endpoint, Redis hit B | More than 60 seconds / worker timeout | 0.1273 seconds |

The risk-selector output was checked against the previous result for the first 100 rows and the full 1,200-student total; it matched exactly. Both rewritten selector paths executed four database queries in the recorded benchmark.

### Query-plan evidence

| Plan measure | Before | After |
|---|---:|---:|
| PostgreSQL planner cost | 5,925,831.91 | approximately 6,209 summed across the set-based signal queries |
| Correlated subplans | 20 | 0 |
| Attendance relation scans in the pathological plan | 70,389 | not repeated as correlated subplans |
| Attendance tuples read in the pathological plan | 664,020,804 | removed as a correlated-repeat pattern |

The old Django query expanded attendance, grade, and overdue-invoice annotations every time score, filter, ordering, or aggregation referenced them. The generated statement was about 11,000 characters and contained 13 attendance, four grade, and three invoice correlated subqueries. Pagination count and page retrieval expanded the expensive logic twice. The replacement performs one grouped query per signal over the already-authorized student subquery, combines risk flags in memory, globally ranks the small candidate set, and loads only the selected page's student records.

### Transport evidence

The OpenAPI schema response measured 1,386,127 bytes before content encoding and 65,266 wire bytes with gzip. That is a 95.29% reduction, or 21.24× fewer transferred bytes. The observed compressed request had 3.65 seconds to first byte and 4.37 seconds total from a variable 2.8 Mbps / high-latency client connection, so compression reduces transfer cost but does not remove network latency.

### Capacity and incident behavior

- Before resize: one vCPU, 2.9 GiB RAM, no swap.
- After resize: two vCPUs, 2.9 GiB RAM.
- Web tier before remediation: two synchronous Gunicorn workers, a 384 MiB web memory limit, 0.65 CPU allocation, and a 60-second request timeout.
- During the incident: six 60-second Gunicorn worker timeouts and two web-container cgroup OOM kills were observed.
- When both synchronous workers were occupied by intelligence SQL, even the local liveness endpoint queued or timed out.
- PostgreSQL showed no blocking lock or deadlock condition; Redis showed no eviction or rejected-connection condition; statistics had been analyzed.

### Production-size tenant used for verification

The deterministic production seed contained 1,200 operational students, 60 teachers, 60 cohorts, 9,600 lessons (9,540 completed), 190,800 attendance records, 14,400 invoices, 12,072 payments, 4,800 exam results, 4,200 assignment submissions, and 19,200 messages. All academic subject data was English-only. This establishes realistic single-tenant row volume, not a concurrency or multi-tenant capacity ceiling.

## Implemented controls inspected in source

- `apps/intelligence/selectors.py`: set-based attendance, published-exam, and overdue-invoice aggregation; deterministic global ranking; bounded page hydration.
- `apps/intelligence/cache.py`: versioned private keys, fresh/stale windows, a distributed refresh lease, stale-while-refresh, a bounded one-second cold-miss poll, fail-open-to-authoritative-compute behavior on Redis errors, and non-sensitive freshness headers.
- `apps/intelligence/views/v1/intelligence_views.py`: live authentication and permission resolution before cache access; exact principal identity; private/no-cache browser headers; `Authorization` and `Accept-Language` variance; organization-wide staff-only risk-list caching; live scoped-student authorization before risk-detail cache reads.
- `config/settings/base.py`: executive 300-second fresh / 1,800-second stale window; risk 120-second fresh / 600-second stale window; 60-second refresh lease.
- `config/settings/production.py`: bounded production validation for every intelligence-cache TTL.

The cache is an optimization, not an authorization source. The key includes tenant, `(user_id, principal kind, principal id)`, effective permissions, effective permission scopes, selected resource scope, query selectors, locale, timezone, and date. Scoped staff and teacher risk lists bypass caching because row access can change through mutable department, cohort, or teaching assignments. Redis failures execute the authoritative loader instead of turning into an availability or authorization failure.

## Derived comparisons

Conservative speedup uses the lower bound of the pre-fix risk interval (30 seconds) and the executive timeout floor (60 seconds):

- Risk selector: at least 131.6× (`30 / 0.228`).
- Risk live cold: at least 276.2× (`30 / 0.1086`).
- Risk live warm: at least 1,315.8× (`30 / 0.0228`).
- Executive selector: more than 676.4× (`60 / 0.0887`).
- Executive cache hit, using the slower of the two observed hits: more than 471.3× (`60 / 0.1273`).
- Planner-cost reduction: approximately 954.4× (`5,925,831.91 / 6,209`).

These are descriptive lower bounds, not confidence intervals or causal estimates.

## Chart map

| Report segment | Question | Form | Fields | Supported claim | Palette | Delivery |
|---|---|---|---|---|---|---|
| Remediation magnitude | How large is the conservative improvement at each measured layer? | Vertical bar | measurement, minimum_speedup_x | Every recorded optimized path improved by at least two orders of magnitude | Single-root blue with neutral scaffolding | Native report chart in `artifact.json` |

The report uses one chart because the evidence consists of discrete before/after anchors, not a sufficiently granular time series. Exact durations, planner values, and transfer sizes remain in tables; no trend chart was produced because fewer than eight comparable temporal observations would imply unsupported continuity.

## Required-structure mapping

- Title → artifact title block.
- Technical summary → “The pathological tail is removed; capacity is now headroom.”
- Key findings with visual evidence → latency distribution table, conservative-speedup chart, live verification table, and query-plan table.
- Scope, data, and metric definitions → “What was measured and what each number means.”
- Methodology → “Four checks isolate code, cache, transport, and capacity.”
- Limitations, uncertainty, and robustness → “The evidence is strong for this incident, not a universal capacity proof.”
- Recommended next steps → “Use 4 CPU / 8 GB as production headroom and operate the fix.”
- Further questions → “What to measure next.”

## Limitations and robustness notes

- The live before/after change included a CPU resize near the software remediation, so the live endpoint difference cannot be attributed entirely to one intervention. The isolated selector timings and elimination of correlated subplans independently identify the query-shape improvement.
- The pre-fix distribution is a 20-minute incident sample, not a long-running service-level baseline.
- The post-deploy distribution is an eight-minute, 137-request recovery sample, not a sustained-load percentile estimate.
- The two executive cache-hit observations and one risk cold/warm pair are smoke measurements, not percentile estimates.
- The large seeded tenant validates row-volume behavior and exact output equivalence; it does not simulate many concurrent tenants or refresh stampedes.
- Redis stale windows deliberately trade bounded freshness for availability. Stronger freshness would require event-driven invalidation or materialized projections.
- Compression evidence applies to a large JSON schema response; ordinary endpoint payloads will save fewer absolute bytes.
- ClickHouse, DuckDB, and Polars were assessed architecturally but were not installed or benchmarked in this incident.

## Official architecture references

- Redis cache-aside and stampede-control guidance: https://redis.io/docs/latest/develop/use-cases/cache-aside/ruby/
- Redis client-side caching reference: https://redis.io/docs/latest/develop/reference/client-side-caching/
- DuckDB concurrency model: https://duckdb.org/docs/current/connect/concurrency
- DuckDB workload-tuning guidance: https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads
- Polars lazy API and predicate/projection pushdown: https://docs.pola.rs/user-guide/concepts/lazy-api/
- ClickHouse analytical use cases: https://clickhouse.com/use-cases
