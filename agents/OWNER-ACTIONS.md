# OWNER-ACTIONS — what only Adrian can provide

Audience: **the owner**. Agents: when blocked, cite the gate ID (`BLOCKED(O-x)`) in WORKLOG and build the mock path — per **TD-2 nothing in the 5-day build waits on this file**. Every integration runs against a deterministic mock (`*_USE_MOCK=True`) until you deliver the real credential and flip the flag.

Rules for you (the owner):
1. **Never put secrets in git.** Secrets go into `.env` on the server (or a password manager) — never into `.env.example`, never into a commit, never into chat logs you paste elsewhere.
2. Env variable names below match `config/settings/base.py` and `.env.example` exactly. Vars marked **(new)** are added by the builder agents during the 5 days — they will appear in `.env.example` with empty values; you fill them in `.env` / `.env.production` only.
3. When you complete a gate, tick it in the checklist at the bottom and tell the agents (WORKLOG or chat) so they can flip the mock flag in staging.

## Gate summary — what, by when

| Gate | What | Needed by (soft) | Blocks launch? |
|---|---|---|---|
| O-1 | Eskiz SMS account + sender ID | Day 3 (real SMS test) | Yes — OTP login by SMS |
| O-2 | Anthropic API key | Day 4 (AI lane) | No — AI features stay mocked |
| O-3 | Click platform merchant + sandbox | Day 3 / launch | Launch only |
| O-4 | Payme platform merchant + sandbox | Day 3 / launch | Launch only |
| O-5 | Soliq/OFD fiscalization credentials | Launch | Yes — legally required for real payments |
| O-6 | Uzum merchant | Launch (can lag) | No — optional provider |
| O-7 | Firebase project (push) | Day 4 | No — push stays mocked |
| O-8 | Domain + wildcard DNS + TLS | Staging/launch | Yes — subdomains are the product |
| O-9 | Production hosting (Postgres/Redis/S3) | Launch | Yes |
| O-10 | Sentry DSN | Launch (recommended) | No |
| O-11 | FIELD_ENCRYPTION_KEY | Day 1 staging | Yes — encrypted fields need it |
| O-12 | Billing plan prices | Day 3 | No — defaults proposed below |
| O-13 | Legal: oferta, privacy, SMS consent | Launch | Yes — minors' data |

---

## O-1 — Eskiz SMS (eskiz.uz)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Eskiz account + approved sender ID (nick) | OTP login, absence/payment SMS — TASKS §3, §10, §17; TD-2; Day 1-C, Day 3-C | `ESKIZ_EMAIL`, `ESKIZ_PASSWORD`, `ESKIZ_API_URL` (exists, default ok), `ESKIZ_FROM` **(new, Day 1-A per TD-17)**, `ESKIZ_USE_MOCK` | Yes — `MockEskizClient` logs the SMS to stdout | Set `ESKIZ_USE_MOCK=False` in staging `.env`, request an OTP to your own phone via `POST /api/v1/auth/otp/request/` |

Do this:
1. Register at <https://eskiz.uz> (cabinet: <https://my.eskiz.uz>). You get a login **email + password** — these are the API credentials. Put them in `ESKIZ_EMAIL` / `ESKIZ_PASSWORD`.
2. Until your own sender ID is approved, Eskiz only sends from test sender `4546` and **only pre-approved test texts**. The code currently hardcodes `"from": "4546"` in `infrastructure/sms/eskiz_client.py`; Day 1 Lane A (TD-17) moves it to `ESKIZ_FROM` env — leave `ESKIZ_FROM=4546` until step 3 completes.
3. In the Eskiz cabinet, apply for your own sender ID / nick (e.g. `STARFORGE`): requires your legal-entity documents (guvohnoma/INN) and takes days — **start this now**. Also submit your SMS templates (OTP text, absence notice) for moderation; Eskiz rejects unregistered message texts on a real sender.
4. When approved: set `ESKIZ_FROM=STARFORGE`, `ESKIZ_USE_MOCK=False` in staging, send yourself an OTP, confirm delivery and that the sender shows your nick.
5. Top up the Eskiz SMS balance (per-SMS billing); set a calendar reminder to monitor balance.

## O-2 — Anthropic API key

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| API key from console.anthropic.com | AI feedback / exam-gen / summaries — TASKS §18; TD-2; Day 4-A | `ANTHROPIC_API_KEY` (exists) | Yes — Day 4-A adds `ANTHROPIC_USE_MOCK` **(new)** per TD-2 | Set the key + `ANTHROPIC_USE_MOCK=False` in staging; trigger one assignment-feedback task; check `AIRequest` row records token usage |

Do this:
1. Sign up at <https://console.anthropic.com> → Settings → API keys → Create key. Add a payment method and set a **monthly spend limit** (start: $50).
2. Paste into `ANTHROPIC_API_KEY` on the server. Never commit it.
3. Model: settings default is `ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"` (`config/settings/base.py`). Upgrading to an Opus-class model multiplies per-token cost ~5x — only do it if feedback quality is provably insufficient. (TD-17: the README/client docstring mentioning `claude-opus-4-7` gets aligned to the settings value Day 1.)
4. Per-tenant budgets already exist as settings defaults: `AI_DEFAULT_DAILY_TOKENS = 100_000`, `AI_DEFAULT_MONTHLY_TOKENS = 2_000_000` — Day 4-A enforces them. You do nothing here unless you want different defaults.

## O-3 — Click merchant (merchant.click.uz)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Platform Click merchant account + a test/sandbox merchant | Tuition payments (TD-6) + platform subscription billing (TD-8) — TASKS §16; Day 3-B/3-E | `CLICK_SERVICE_ID`, `CLICK_MERCHANT_ID`, `CLICK_SECRET_KEY`, `CLICK_MERCHANT_USER_ID`, `CLICK_USE_MOCK` — all **(new, Day 3-B)** | Yes — mock Click client + simulated webhooks | `CLICK_USE_MOCK=False` in staging with the **test merchant**; pay a 1,000-UZS test invoice; webhook completes the Payment |

Important distinction (TD-6): **each school brings its own Click merchant account** — school admins enter their credentials into the encrypted `ProviderConfig` (Day 3-B builds the form/API), not into env. The env vars above are for **your platform account only**, used for TD-8 subscription billing (schools paying *you*).

Do this:
1. Onboard at <https://merchant.click.uz> as your legal entity (requires INN, bank account, contract with Click). Ask the Click manager for **test/sandbox credentials** as well — you need both.
2. From the merchant cabinet collect: **SERVICE_ID**, **MERCHANT_ID**, **SECRET_KEY**, **MERCHANT_USER_ID** → the four env vars above.
3. In the Click cabinet, register the webhook (Prepare/Complete) URL. Pattern (TD-6, public schema): `https://starforge.uz/api/v1/webhooks/click/<center-slug>/` — for your platform billing account use the platform slug the Day 3-E agent gives you (e.g. `.../webhooks/click/platform/`). Each school registers their own slug URL in *their* Click cabinet (you'll give them instructions; the runbook in `docs/` covers it, TASKS §30).
4. Pass the sandbox credentials to the agents for staging; keep production credentials in the production `.env` only.

## O-4 — Payme merchant (business.payme.uz / Paycom)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Platform Payme merchant + sandbox cabinet | Tuition payments (TD-6) + subscription billing (TD-8) — TASKS §16; Day 3-B/3-E | `PAYME_MERCHANT_ID`, `PAYME_KEY`, `PAYME_TEST_KEY`, `PAYME_USE_MOCK` — all **(new, Day 3-B)** | Yes — mock JSON-RPC handlers, simulated transactions | `PAYME_USE_MOCK=False` in staging using `PAYME_TEST_KEY`; run the Paycom sandbox test suite against the endpoint |

Same per-center vs platform split as O-3: schools enter their own Payme credentials into `ProviderConfig`; env vars are your **platform** account.

Do this:
1. Onboard at <https://business.payme.uz> (operator: Paycom). Sign the merchant contract; you receive a **merchant ID** and a **production key** plus a **test key**.
2. In the Paycom merchant cabinet, register the JSON-RPC endpoint URL: `https://starforge.uz/api/v1/webhooks/payme/<center-slug>/` (platform slug for your billing account; each school registers theirs).
3. Use the Paycom **sandbox/test cabinet** (test.paycom.uz, linked from developer docs at developer.help.paycom.uz) to run their automated merchant-API test against staging — the Day 3-B agent implements all six JSON-RPC methods (TASKS §16) and will tell you when staging is ready for the sandbox run.
4. Deliver `PAYME_TEST_KEY` early (staging) and `PAYME_KEY` at launch.

## O-5 — Soliq fiscalization (e-receipts)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Fiscal credentials from soliq.uz / your OFD provider | Legal e-receipt per payment — TD-7; Day 3-B | `SOLIQ_API_URL`, `SOLIQ_TOKEN`, `SOLIQ_USE_MOCK` — all **(new, Day 3-B; names may be refined by the implementing agent)** | Yes — `MockSoliqClient` returns deterministic fiscal sign + QR URL | `SOLIQ_USE_MOCK=False` in staging; complete a test payment; `FiscalReceipt` row stores a real fiscal sign/QR that validates on the tax portal |

Honesty note: the exact API surface (virtual fiscal module vs OFD aggregator REST API) will be confirmed by the agent implementing TD-7 — Uzbekistan's online-KKM landscape has several OFD intermediaries. **Your action is to obtain the credentials and the receipt requirements; the agent adapts the client.**

Do this:
1. Talk to your accountant: every online tuition payment legally needs a fiscal receipt (onlayn-KKM/virtual kassa). Register a **virtual cash register** via <https://soliq.uz> / my.soliq.uz or contract an OFD aggregator (e.g. the provider your bank or Payme/Click manager recommends — they bundle fiscalization).
2. Obtain: the fiscal module token / API credentials, your **INN**, and the **IKPU/SPIC codes + package codes** for "education services" line items — agents need these to build valid receipts. Send them to the Day 3-B agent via WORKLOG/chat (not git).
3. Provide one **sample receipt requirement** from the provider (fields they mandate) if available.
4. Mock works for the whole build; flipping to real is a launch-gate, not a build-gate.

## O-6 — Uzum merchant

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Uzum (Apelsin/Uzum Bank) merchant account | Third payment provider — TASKS §16; TD-6; Day 3-B | `UZUM_MERCHANT_ID`, `UZUM_SECRET_KEY`, `UZUM_USE_MOCK` — all **(new, Day 3-B)** | Yes | Same pattern: flip in staging, pay a test invoice, webhook lands at `/api/v1/webhooks/uzum/<center-slug>/` |

Do this: onboard via Uzum's merchant/business channel (<https://uzum.uz> business / Uzum Bank acquiring). Lower priority than Click/Payme — Uzum can launch later; the code ships mock-first either way. Same per-center vs platform distinction as O-3.

## O-7 — Push notifications (Firebase / APNs)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Firebase project + service-account JSON | Mobile push — TASKS §3 (device push tokens), §17; TD-2; Day 3-C | `FCM_CREDENTIALS_FILE` (path to JSON), `FCM_USE_MOCK` — both **(new, Day 3-C)** | Yes — mock push logs payloads | `FCM_USE_MOCK=False` in staging; register a device push token; trigger an absence notification; phone buzzes |

Do this:
1. Create a Firebase project at <https://console.firebase.google.com> (free tier is fine).
2. Project settings → Service accounts → **Generate new private key** → download the JSON. Place it on the server (e.g. `/srv/starforge/secrets/fcm.json`), set `FCM_CREDENTIALS_FILE=/srv/starforge/secrets/fcm.json`. Never commit the JSON.
3. **APNs (iOS): deferred.** FCM relays to APNs once the iOS app exists — when it ships, you'll add an Apple Developer account ($99/yr) and upload the APNs key into the same Firebase project. No backend env change expected.

## O-8 — Domain, DNS, TLS

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Own `starforge.uz` + wildcard DNS + wildcard TLS | Subdomain-per-tenant is the architecture — TASKS §2; TD-19; Day 5-E deploy prep | `ALLOWED_HOSTS=.starforge.uz`, `CSRF_TRUSTED_ORIGINS=https://*.starforge.uz` (exist in `.env.example`) | Yes — local dev uses `demo.localhost`, needs **nothing** from you | `https://demo.starforge.uz/api/schema/swagger-ui/` loads with a valid padlock on staging |

Do this:
1. Register `starforge.uz` through a cctld.uz-accredited registrar (e.g. ahost.uz, webspace.uz) if you don't already own it.
2. DNS records: `A starforge.uz → <server IP>` and **wildcard** `A *.starforge.uz → <same IP>`. Every new school subdomain then works with zero DNS changes.
3. TLS: wildcard certs require **DNS-01** validation (HTTP-01 cannot issue wildcards) — see `docs/deployment.md` "Subdomain TLS". Easiest: use a DNS provider with an API (Cloudflare free tier — point your registrar's nameservers at it) so Caddy/certbot can auto-renew `*.starforge.uz`. Give the agents the DNS API token at deploy time (O-9).
4. Local development needs none of this: `*.localhost` resolves automatically.

## O-9 — Production hosting

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Server(s) + Postgres + Redis + S3 bucket | Going live — TASKS §29; Day 5-E ships compose-prod + runbooks; live hosting is yours | Full block below: `DATABASE_URL`, `REDIS_URL`, `AWS_*`, `SECRET_KEY`, … | Yes — docker compose dev stack runs everything locally | Run the 12-step end-state acceptance (ROADMAP §7) against the production URL |

Recommendation: **Hetzner** (hetzner.com). Reasoning: best price/performance in Europe, Falkenstein/Helsinki DCs give ~80–110 ms latency to Tashkent (acceptable; no major cloud has an Uzbekistan region), pay-per-month, simple console for a non-DevOps founder. Alternative: DigitalOcean (nicer managed DBs, ~1.5–2x price). Avoid AWS for v1 — cost/complexity overkill.

Do this:
1. Create a Hetzner account → one **CPX31** (4 vCPU / 8 GB, ~€16/mo) to start; the Day 5-E compose-prod file runs web+asgi+worker+beat+caddy on it.
2. Database: Hetzner has no managed Postgres — either run the compose Postgres with the backup runbook (Day 5-E writes it, `pg_dump` nightly to S3), or pay for managed Postgres elsewhere (Aiven/Neon, ~$30+/mo) if you want zero DB ops. Decide and tell the Day 5-E agent.
3. Redis: runs in the compose stack (fine at this scale).
4. S3: Hetzner Object Storage or Backblaze B2 (both S3-compatible). Create a bucket `starforge-media`, generate access keys → `AWS_S3_*` vars.
5. Fill `.env.production` from the template at the bottom of this file; hand the server IP to O-8 DNS.

## O-10 — Sentry (recommended)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Sentry project DSN | Error tracking — TASKS §1 ("config-only; defer real DSN") | `SENTRY_DSN` **(new, Day 1-A, config-only)** | Yes — empty DSN = disabled, zero effect | Set DSN in prod; trigger a test error; event appears in Sentry |

Do this: sign up at <https://sentry.io> (free tier OK) → create a Django project → copy the DSN → `SENTRY_DSN=` in production env. Optional but strongly recommended before real users.

## O-11 — FIELD_ENCRYPTION_KEY

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| One Fernet key you generate | Encrypts `national_id`, `medical_notes`, provider credentials, Soliq tokens — TD-11; Day 1 | `FIELD_ENCRYPTION_KEY` **(new, Day 1)** | Dev/test auto-generate a throwaway key; **staging/prod need a real one** | App boots with encrypted fields readable; restart keeps data readable |

Do this:
1. Generate once: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Put the output in staging/production `.env` as `FIELD_ENCRYPTION_KEY=...` **and store a copy in your password manager**. If this key is lost, every encrypted field (passport numbers, medical notes, merchant credentials) is unrecoverable. If it leaks, rotate per the rotation runbook (`docs/`, written Day 5-E per TD-11).
3. NEVER commit it. It must be different from `SECRET_KEY`.

## O-12 — Billing plans (your pricing decision)

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Plan names, prices, limits | Paywall — TD-8; Day 3-E seeds `Plan` rows | None — data, not env (seeded via `apps/billing`) | Yes — Day 3-E seeds the proposed defaults below if you say nothing | Suspend a test center's subscription → tenant API returns 402 `subscription_required` |

Proposed defaults (Day 3-E seeds these unless you reply with edits — **approving as-is is a valid answer**). All plans start with a 14-day free trial:

| Plan | UZS / month | Max students | Max branches | AI tokens / month | Storage GB |
|---|---|---|---|---|---|
| Start | 490,000 | 200 | 1 | 200,000 | 20 |
| Standard | 990,000 | 600 | 3 | 1,000,000 | 100 |
| Pro | 1,990,000 | 2,000 | 10 | 3,000,000 | 500 |

Do this: reply in WORKLOG/chat with "approved" or your edited table before end of Day 3. Prices are editable later via the platform admin (TD-10) — this just sets launch defaults.

## O-13 — Legal documents

| What | Why / feature | Exact env vars | Mock meanwhile? | How to flip + verify |
|---|---|---|---|---|
| Public offer (oferta), privacy policy, SMS consent wording | Legal operation; you process **minors' personal data** | None — static documents, hosted on the marketing site / linked from apps | Build proceeds without them | Lawyer-reviewed documents published before first paying school |

Do this:
1. Commission a **public offer (publichnaya oferta)** for the SaaS subscription (you ↔ schools) and a **data-processing clause** for schools entering students' data.
2. Commission a **privacy policy** covering: parents' and **children's** personal data, encrypted fields (passport/national ID, medical notes), data location (EU servers per O-9 — verify this is acceptable under O'zbekiston "Shaxsga doir ma'lumotlar to'g'risida"gi qonun / law No. ZRU-547, which has data-localization provisions — **this needs a real UZ lawyer, not a template**).
3. Approve the **SMS consent wording** — parents must have consented (via the school's enrollment contract) to receive attendance/payment SMS; give schools a template clause.
4. Flag: minors' data + cross-border hosting is the single highest legal risk here. Budget for a proper legal review before launch; the build does not wait for it.

---

## `.env.production` template

Copy, fill every `<...>`, keep on the server only. Vars marked `(new)` land in `.env.example` as the build progresses — sync this block with `.env.example` at Day 5.

```bash
# --- Core ---
DEBUG=False
SECRET_KEY=<python -c "import secrets;print(secrets.token_urlsafe(64))">
ALLOWED_HOSTS=.starforge.uz
DJANGO_SETTINGS_MODULE=config.settings.production

# --- Data stores (O-9) ---
DATABASE_URL=postgres://<user>:<password>@<host>:5432/starforge
REDIS_URL=redis://<host>:6379/0
CHANNEL_REDIS_URL=
CELERY_BROKER_URL=
CELERY_RESULT_BACKEND=

# --- CORS / CSRF (O-8) ---
CORS_ALLOWED_ORIGINS=https://starforge.uz,https://app.starforge.uz
CSRF_TRUSTED_ORIGINS=https://*.starforge.uz

# --- S3 storage (O-9) ---
AWS_STORAGE_BUCKET_NAME=starforge-media
AWS_S3_ENDPOINT_URL=<https://...object-storage-endpoint>
AWS_S3_ACCESS_KEY_ID=<key>
AWS_S3_SECRET_ACCESS_KEY=<secret>
AWS_S3_REGION_NAME=<region>

# --- Eskiz SMS (O-1) ---
ESKIZ_API_URL=https://notify.eskiz.uz/api
ESKIZ_EMAIL=<eskiz-login-email>
ESKIZ_PASSWORD=<eskiz-password>
ESKIZ_FROM=<approved-nick, 4546 until approved>   # (new)
ESKIZ_USE_MOCK=False

# --- Anthropic (O-2) ---
ANTHROPIC_API_KEY=<sk-ant-...>
ANTHROPIC_USE_MOCK=False                          # (new)

# --- Payments: PLATFORM accounts for TD-8 billing (O-3/O-4/O-6) (all new) ---
CLICK_SERVICE_ID=<...>
CLICK_MERCHANT_ID=<...>
CLICK_SECRET_KEY=<...>
CLICK_MERCHANT_USER_ID=<...>
CLICK_USE_MOCK=False
PAYME_MERCHANT_ID=<...>
PAYME_KEY=<production-key>
PAYME_TEST_KEY=<test-key>
PAYME_USE_MOCK=False
UZUM_MERCHANT_ID=<...>
UZUM_SECRET_KEY=<...>
UZUM_USE_MOCK=True                                # flip when O-6 done

# --- Fiscalization (O-5) (new; names confirmed by TD-7 agent) ---
SOLIQ_API_URL=<provider-endpoint>
SOLIQ_TOKEN=<fiscal-module-token>
SOLIQ_USE_MOCK=False

# --- Push (O-7) (new) ---
FCM_CREDENTIALS_FILE=/srv/starforge/secrets/fcm.json
FCM_USE_MOCK=False

# --- Encryption (O-11) (new) ---
FIELD_ENCRYPTION_KEY=<Fernet key — also in password manager>

# --- Observability (O-10) (new) ---
SENTRY_DSN=<https://...@sentry.io/...>

# --- Email ---
DEFAULT_FROM_EMAIL=noreply@starforge.uz
EMAIL_HOST=<smtp host>
EMAIL_PORT=587
EMAIL_HOST_USER=<smtp user>
EMAIL_HOST_PASSWORD=<smtp password>
EMAIL_USE_TLS=True
```

## Definition of unblocked — tick as you go

- [ ] **O-1** Eskiz account created; `ESKIZ_EMAIL`/`ESKIZ_PASSWORD` in staging `.env`; sender-ID application submitted; real SMS received on own phone with `ESKIZ_USE_MOCK=False`
- [ ] **O-2** Anthropic key in staging; spend limit set; one real AI task completed under budget accounting
- [ ] **O-3** Click platform + sandbox credentials delivered; webhook URL registered in Click cabinet; sandbox payment completes in staging
- [ ] **O-4** Payme merchant + test key delivered; JSON-RPC URL registered; Paycom sandbox suite passes against staging
- [ ] **O-5** Fiscal/OFD credentials + IKPU codes delivered to Day 3-B agent; real receipt validated (launch gate)
- [ ] **O-6** Uzum credentials delivered (or explicitly deferred past launch)
- [ ] **O-7** Firebase service-account JSON on server; real push received on a test device
- [ ] **O-8** `starforge.uz` owned; wildcard A record live; wildcard TLS auto-renewing; `https://demo.starforge.uz` green padlock
- [ ] **O-9** Server provisioned; Postgres backup strategy chosen; S3 bucket + keys created; `.env.production` fully filled
- [ ] **O-10** Sentry DSN set; test event visible
- [ ] **O-11** `FIELD_ENCRYPTION_KEY` generated, in staging+prod env AND password manager
- [ ] **O-12** Plan table approved (or edited) and confirmed to Day 3-E
- [ ] **O-13** Oferta + privacy policy + SMS consent wording at a UZ lawyer; published before first paying school
