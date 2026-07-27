# Starforge Edu — Product Vision & Idea Backlog

> Source: founder brain-dump (2026-06-01). The founder worked as a teacher at an
> Uzbek education center and was a student before that — **most ideas are born
> from real pain and joy.** Captured faithfully; lightly organized. Items marked
> **[challenge]** are Claude's notes/questions to validate via deep research on
> how Uzbek education centers actually operate — the founder explicitly asked to
> be challenged with better ideas.
>
> This is the canonical product vision. `FEATURE_LIST.md` is the raw idea inbox;
> `agents/FEATURE_BACKLOG.md` is the engineering breakdown that sequences this.

## 0. Positioning (correction)
- This is for **education centers** (tutoring centers, language schools, exam-prep
  centers), **NOT K-12 schools.** Update all framing accordingly.
- Frontend design is going well (separate track).
- **North star: kill paper.** Going paperless is the single biggest problem in
  Uzbek education centers today. Even centers that *have* a system still run
  paper attendance sheets because the system is bad. Every feature should remove
  a sheet of paper or a manual ritual.

## 1. Printing service — the wedge (almost no competitor)
- **Built-in content library shipped with the app.** Based on the center's focus,
  pre-load a large default library. Example: an English-only center gets tons of
  English books by default — usable by printers (to print), students (to read),
  teachers (to teach from), and anyone studying at that center.
- **Custom print layouts to save paper** — e.g. 2-up, n-up, duplex. Paper-saving is
  a first-class feature, not an option buried in a driver.
- **Paper-usage accounting.** The operator records pages used per job; the center
  sees total paper spend over time. A toggleable setting can *require* per-job logging.
- The branch print agent (separate repo) does the CUPS work; this app owns the
  queue, library, layouts, quotas, and accounting.
- **[challenge]** Library licensing/copyright for bundled books in UZ — biggest risk *and* moat.

## 2. Extreme notifications — "don't let anyone miss anything"
- No teacher, auditor, manager, or CEO should ever miss a notification. Persistent,
  multi-channel until acknowledged.
- **[challenge]** Persistent-until-ack + escalation (in-app → push → SMS → call) with
  read receipts + audit trail. Risk: notification fatigue. Use per-event severity tiers;
  mandatory-ack only for the few that truly matter.

## 3. Departments + task management
- Permission-holder creates a department, adds people, assigns tasks.
- **Whole-department task → distributed evenly** so everyone does an equal share.
- Also assign to a **single person** (private tasks). Department lifecycle: **open/closed/pending.**
- **[challenge]** Define the fairness algorithm (round-robin / by load / by skill) and make it visible.

## 4. Dynamic permission system — CRITICAL (security)
- **Permission-locked, not just role-locked.** Fully dynamic per center; create custom
  roles, define permissions, assign to users.
- **HARD REQUIREMENT — backend checks live permissions on every request.** A center the
  founder saw stored auth in **localStorage**, checked only on the frontend; logging in
  as a normal teacher gave full admin in **under a minute** — the backend never verified.
  Never here: the **server is the sole authority**, checks permissions live (no
  client-trusted state), revocation is immediate.
- Extends the static `ROLE_PERMISSION_MATRIX` toward center-configurable roles + granular
  permissions, still enforced server-side.

## 5. Payments (born from personal pain as a student)
- Connect **Click, Payme**, other UZ rails (Uzum, etc.).
- **Payment-delay request** to teacher/manager/CEO/payments-handler; on approval moves to
  a student-chosen date within bounds (so shy students don't ask in person).
- **Discount requests** by a teacher for a student → manager approves → applies (percent or fixed).
- **Partial / "pay what you can" online** with a **note explaining the situation** →
  reviewer accepts + assigns a pay-by date.
- **Daily reminders** until paid. Whole flow removes shame + friction.

## 6. Branch management
- CEO/manager (with permission) **create branches, view details, assign workers/teachers.**

## 7. Fair salary / revenue-share engine (FULL fairness)
- **Configurable payout rules in-app** (e.g. teacher earns X% of their own students' payments).
- **Salary-prep workflow:** teacher asks an assigned **cashier to prepare their salary**;
  cashier can **accept or reject** (sometimes the till is empty — must be representable).
- **[challenge]** Small center-editable rule DSL (per-student %, base+bonus, caps, per-cohort
  overrides) with dry-run preview + full audit.

## 8. Book selling (centers sell books)
- Buy in-app + **pay online** → **QR/short code** → show seller → confirm + receive money.
- **Cash option:** seller (often the printer operator) **records the cash sale in-app** so
  money can't silently disappear. Accountability is the point.

## 9. Assistant role (concrete example of dynamic roles)
- Create an "assistant" role; assign to teacher(s). Work mostly during lessons + **private
  messages/assignments** from teachers ("start the lesson", "print this").
- **Calling absent students:** setting to **auto-message the parent from the assistant's
  phone**, or a **call button** using stored numbers.

## 10. In-app chat
- Telegram-like messaging, nicer — DMs and likely groups, inside the app.

## 11. HR & contracts
- An **HR department**. Hire on **contracts** (e.g. 4-year) with **equity or salary**;
  salary can **increase/decrease** over time.
- **[challenge]** "Equity" is unusual — clarify profit-share vs bonus pool vs literal ownership.

## 12. Rule book / policy acknowledgment
- Each center has a **rule book**; **everyone** must be **forced to read and accept** it.
- **Role/permission-filtered content** (teachers' rules ≠ printers' rules). Versioning +
  re-acknowledgment on change, with audit trail.

## 13. "Keep an eye" / monitoring
- Activity-monitoring/oversight (under-specified). Possibly activity dashboard / audit feed /
  live ops view. (See Batch 3/4 — this becomes the AI camera differentiator.)

## 14. Team events + transparent cost-splitting
- Handle money for **team events**. **Configurable who pays** (split evenly / center-covered /
  mix). **Event announcement + RSVP** (one-button join). **Full fair detail up front**
  (your charge, what to bring, food, leave/return times). Accept or reject.

## 15. Procurement / purchase requests
- Replace "wait for the manager to release cash": submit **item request** → manager approves
  → **cashier auto-notified to ready money** (can delay if till empty) → done.
- **[challenge — key insight]** Same shape as payment-delay, discounts, salary-prep, book-cash,
  event-split: **request → approve(s) → cashier disburses → notify → ledger entry.** Build ONE
  generic **Approvals + Money-movement (ledger) engine**; each becomes a configured instance.
  Single biggest architectural simplification in the product.

## 16. HR / hiring pipeline + Telegram bots
- HR bots on **Telegram** + in-app. **Custom application questions.** Candidates accepted,
  scheduled for interview, talk with manager/HR in-app (calling supported). Telegram dominant in UZ.

## 17. Call recording + AI call analysis
- Route staff calls through recording/analysis; report call QA to the manager.
- **[challenge — highest legal/technical risk]** Recording implicates consent + UZ personal-data
  localization law; needs telephony + UZ/RU transcription. MVP likely **call logging + manual
  notes + opt-in recording with disclosure**, not blanket recording.
- "Meta AI" = genuinely Meta's AI (WhatsApp/Meta Business AI). Integration + legality need research.

## Batch 3 — business model + camera differentiator
- **Not a commodity shared-SaaS CRM** — a **premium, managed, AI-heavy, dedicated-infrastructure**
  product. **3 centers ready (5,000+ students, 10+ branches).** Incumbents are slow/buggy.
- **Dedicated server per center** (paid from subscription) — premium justification; answers the
  "price too high" finding (different category, not commodity CRM).
- **Premium AI tiering:** **Opus 4.8 high-effort** for heavy reasoning, **Sonnet** basic,
  **Haiku** fast. Meter per-center (TASKS §18 budgets).
- **AI camera built in** ("looks into cameras") — headline differentiator. Biometric → **UZ law
  requires domestic servers** → the dedicated in-country server-per-center model fits perfectly.
  Consent + signage; keep biometric/video on the center's own server. django-tenants deploys
  onto a per-center box with no rework.

## Batch 4 — camera design, exams, engagement
- **AI camera:** edge box / center server; **data never leaves the building.** Replaces the
  weekly 2h manager+teacher footage review. Stack = CV presence + **ASR (Whisper) on lesson
  audio** → local LLM analysis. **Lead with audio→transcript** (on-time/taught/covered/tone) +
  light presence; deep video later. Batch at lesson end; VRAM is the floor (used RTX 3060 12GB).
  **Visibility DYNAMIC** — teachers vote, then configure; teacher sees own report (fairness, not
  surveillance). Consent + signage + short retention.
- **Mock exams:** **AI-generated (Opus)** + government-licensed official sets (recurring promise).
  **Gold = AI auto-scoring + feedback** on writing/speaking (band + criterion feedback in seconds;
  teacher overrides). AI content dodges copyright. Human glance before students see a mock.
- **Typing + CBT simulator** (real timer, listening-once, highlighter, word counter) + typing
  practice. Nobody in center-software does this.
- **Voting/polls primitive** (visibility, rule changes, events). Consent, not decree.
- **Student engagement layer:** in-app **podcasts** (TTS/licensed), **focus/study board** (Pomodoro),
  **gamified competition** (Blooket/Kahoot-style vocab games, leaderboards, prizes — unadopted in
  UZ → gap). AI generates game content from the lesson. Digitize the "chocolate prize" into
  **points/badges**.
- **Theme: TWO surfaces, one app** — (a) the ops platform the **owner** pays for, (b) the
  daily-attention surface the **student** opens daily. Ops wins the sale; engagement wins retention.

## Batch 5 — AI speaking partner + lesson media
- **AI speaking partner / IELTS speaking simulator ⭐** — talk to AI, it runs a real 3-part IELTS
  speaking test + scores instantly. Stack: Whisper → Opus examiner → TTS. 24/7, no anxiety.
  Honesty: fluency/lexical/grammar score well from transcript; **pronunciation is genuinely hard**
  — approximate + label, don't overpromise.
- **Progress tracking:** save voice recordings; **improvement graph + band trajectory** (secure/local,
  consent — minors).
- **YouTube lesson-linked clips:** **search + EMBED** official player (never rip). Match the lesson's
  target phrases via **caption search**; AI suggests search terms.

## Batch 6 — the joy layer
- **Student rewards (company-funded):** gift digital subs (Spotify/Netflix-capped/Crunchyroll) +
  physical (CMF Buds — cheap for us, life-changing for them). Marketing + retention + word-of-mouth.
- **⚠️ Guardrail:** unbounded cost that grows with success → make PREDICTABLE: capped reward pool
  per branch/term, points→catalog with monthly cap, co-fund with the center, sponsor deals, bulk-buy.
  **Every reward = a money-movement → ledger entry.**
- **Living yearbook:** teachers attach the **group photo** periodically → cohort sees its journey.
  Cheap, high emotion. Consent for minors; cohort-scoped.
- **Theme:** the JOY layer is the un-copyable soul; pair with budget discipline.

## Batch 7 — points economy + redemption store
- **Known brands only** (AirPods, Nothing Ear/CMF, Samsung Buds). Quality/trust over cheap.
- **Points economy:** earn from studying (attendance, scores, improvement, game wins, maybe on-time
  payment) → spend in a **tiered in-app store** (cheap pens/notebooks ↔ aspirational earbuds at top).
- Control **earn rate** + **catalog prices** → cost predictable/capped by design. Balance earn vs burn.
  Points = issued currency → **track issuance + redemption in the ledger.**
- **Unify with the in-app book/material market:** ONE marketplace, TWO currencies — **UZS** for
  books/materials, **points** for rewards. Branch operator fulfills + marks in-app (inventory + audit).

## Batch 8 — "watch together" co-watch
- Co-presence English co-watch + chat. **Never host/stream** copyrighted video — **Teleparty model:
  sync timeline + chat over each user's own legal playback**, or embed YouTube/public-domain.
- **Child safety (minors):** cohort/center-scoped only (no strangers), center-approved content,
  chat moderated + logged, parent/center-visible. Frame as **"study together."**
- **Verdict:** KEEP as a later engagement feature, safe-by-design.

## Batch 9 — supervised in-app communication (governed Telegram alternative)
- Move group chats + comms **into the app, fully supervised** → the safety risk becomes the value
  prop: control for the owner, safety for the parent.
- **Center-provisioned logins (no open signup) → no strangers.** Matches the existing auth model.
- **AI moderation** (Haiku) scans for profanity/bullying/concern → escalate to a human. Wordlist +
  context AI, covering **Uzbek + Russian + English.**
- **Co-watch refined:** only short, approved English LEARNING clips embedded from YouTube.
- **Notify via Telegram (reach), keep supervised group chat in-app (control).**

## Batch 10 — the Intelligence layer + growth/ops
**KEY INSIGHT:** most of this is ONE **Intelligence/analytics layer** computed from data already
collected (attendance, grades, submissions, payments). Risk flags, family health, branch ranking,
teacher leaderboards, reputation are *views/scores* on the same pipeline. Build it once.
- **Student risk prediction ⭐** — flag at-risk students (attendance dropping, HW missing, scores
  declining, tuition delayed). **Start as transparent RULES**, not black-box AI. Dropout = #1 revenue leak.
- **Family health scores** — frame as **"needs attention"** (neutral), internal/permissioned.
- **Teacher performance intelligence ⚠️** — measure **value-added/improvement**, not raw scores;
  teacher sees own first; visibility voted (like camera). Avoid toxic leaderboards.
- **Branch ranking**, **Classroom-IQ QR dashboard** (scan on entry → attendance/HW/plan/materials),
  **smart group formation** (reuse schedule conflict/availability engine), **student journey timeline**
  (parent-facing portfolio), **referrals** (→ points), **teacher/substitute marketplace**,
  **equipment tracking** (asset registry), **reputation dashboard** (internal health + public reviews
  with anti-gaming/moderation).

## Themes (cross-cutting)
1. **Accountability & anti-fraud** runs through everything (cash logging, live permission checks,
   paper accounting, fair payouts, audit). Arguably the real product.
2. **Dignity / shame-reduction** (partial pay, delay/discount requests — all in-app, no asking in person).
3. **Paper elimination** — the measurable promise. A per-center "paper/money saved" dashboard.
4. **One Approvals + Ledger engine underlies most "money" features.** `request → N approvals →
   cashier disburses → notify → immutable ledger entry`. Build once; the rest is configuration.
   Biggest leverage point. The ledger makes "money can't disappear" literally true.

## Claude's own ideas (founder asked to hear them)
**Top 3:** (1) **Make the Ledger the product** — every som a row, live "where is the money" view,
auto-reconciled vs Click/Payme/Uzum payouts. (2) **Telegram-first for parents/students; app is for
staff** — deliver pings/reminders/pay-links/RSVPs via a Telegram bot; the polished app is the staff
surface. (3) **Lead → trial → enrolled CRM funnel** — highest commercial ROI module.
**Supporting:** payment trust-score (auto delays for reliable payers), one-tap/QR attendance →
auto-absence Telegram → feeds payroll + fairness, "paper & money saved" renewal dashboard, print-cost
attribution + quotas, offline-first attendance/cash logging, substitute auto-coverage, onboarding
templates by center type, consent-first call QA.

## Next step the founder requested
Deep research on how Uzbek education centers actually operate (payments, payroll norms, Click/Payme
flows, book-selling, staffing, regulations), then **challenge these ideas and propose better ones.**
More ideas are coming — this file + `FEATURE_LIST.md` are append-only.
