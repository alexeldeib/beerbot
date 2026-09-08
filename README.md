# 🍺 Beerbot

**The GroupMe bot that never forgets a round.**

Track drinks with your friends, compete on leaderboards, and let AI detect what you're drinking from photos. Whether it's a casual beer, a glass of wine, or a perfectly-poured Guinness with the G split just right — Beerbot's got you covered.

---

## ✨ Features

🍻 **Multi-Drink Tracking** — Not just beer! Track beers, wines, cocktails, and hard seltzers (claws). Each drink type has its own emoji, stats, and leaderboard filtering.

📸 **AI-Powered Image Analysis** — Post a photo and Beerbot uses Google Gemini Vision to detect drinks by glass type. Pint glass? Beer. Wine glass? Wine. Martini glass? Cocktail. It even detects when you've nailed the perfect "Split the G" on a Guinness!

🍀 **Split the G Detection** — Special recognition for the Irish art of pouring a Guinness with the beer level exactly at the G. Comes with its own leaderboard.

🌐 **Multi-Group Support** — Run Beerbot in multiple GroupMe groups, each with its own bot_id mapping and independent statistics.

📊 **Comprehensive Stats** — Daily, weekly, and all-time leaderboards. Personal stats with drink-type breakdown. The legendary "Road to 1 Million" countdown that projects when your group will hit a million drinks.

💸 **Debt Tracking** — Someone owes the group a round? Track it with `!owe` and watch their debt decrease as they drink.

🥂 **AI-Generated Toasts** — Need inspiration? Ask Beerbot for a creative drinking toast in styles ranging from medieval knight to nature documentary narrator.

🔒 **Idempotent Processing** — Beerbot won't double-count if GroupMe sends the same webhook twice.

---

## 📖 Commands

### Logging Drinks

| Method | Example | Notes |
|--------|---------|-------|
| Beer emoji | 🍺 or 🍺🍺🍺 | Count matches emoji count |
| Wine emoji | 🍷 | Logs as wine |
| Cocktail emojis | 🍸 🍹 🥃 | Logs as cocktail |
| +N drinks | `+3 beers` `+2 wines` `+1 cocktail` | Explicit quantity |
| Word triggers | `beer me` `cheers` `wine me` `claw me` | Logs 1 drink |
| Generic drinks | `+3 mimosas` `+2 shots` | Alcoholic words → cocktails |
| Photo | *Post any image* | AI detects drink type by glass |
| @mentions | `+2 beers @Alice @Bob` | Logs for mentioned users |
| Remove drinks | `-3 beers` `-2 wines` | Removes from your count |

### Stats Commands

| Command | Description |
|---------|-------------|
| `!beers` | Group drink count with type breakdown |
| `!mystats` | Your personal stats by drink type |
| `!leaderboard [type]` | Top drinkers (filter by `beer`/`wine`/`cocktail`/`claw`) |
| `!today [type]` | Today's stats (filterable) |
| `!week [type]` | This week's stats (filterable) |
| `!million [type]` | Road to 1 Million countdown |

### Split the G

| Command | Description |
|---------|-------------|
| *Post a Guinness photo* | Auto-detected when beer is at the G level |
| `!splitg` | Split the G leaderboard |
| `!unsplit [N] [@user]` | Remove split(s) |

### Debt Tracking

| Command | Description |
|---------|-------------|
| `!owe @user` | Add 1 beer debt |
| `!owe 5 @user` | Add N beers debt |
| `!debts` | Show who owes the most |

*Debts auto-reduce as users drink!*

### Other

| Command | Description |
|---------|-------------|
| `!undo` | Remove your last drink entry |
| `!unbeer N [@user]` | Remove N beers |
| `!toast` | Get an AI-generated drinking toast |
| `!help` | Show command reference |

---

## 🛠 Tech Stack

- **Python 3.11+** with type hints
- **FastAPI** for async webhook handling
- **asyncpg** for PostgreSQL with connection pooling
- **Configurable LLM endpoint** (Google Gemini 3.6 Flash by default)
- **GroupMe Bot API** for messaging
- **Pydantic v2** for validation
- **Fly.io** for hosting
- **Neon PostgreSQL** for database

---

## 🚀 Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- GroupMe bot token ([create one here](https://dev.groupme.com/bots))
- PostgreSQL database (Neon recommended)
- Google Gemini API key (optional, for image analysis)

### Local Development

```bash
# Clone and install
git clone https://github.com/yourusername/beerbot.git
cd beerbot
uv sync

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run
uv run uvicorn src.beerbot.main:app --reload --port 8080

# Expose for webhooks (use ngrok or similar)
ngrok http 8080
```

### Running Tests

```bash
uv run pytest
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BEERBOT_BOT_ID` | Yes | Your GroupMe bot ID |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `GROUPME_WEBHOOK_SECRET` | Production | Random bearer token included in the callback URL |
| `REQUIRE_REGISTERED_GROUPS` | No | Reject unknown GroupMe groups (default: `true`) |
| `LLM_PROVIDER` | No | Runtime model adapter; currently `google` |
| `LLM_MODEL` | No | Pinned model name (default: `gemini-3.6-flash`) |
| `LLM_API_KEY` | No | Model endpoint credential; falls back to `GEMINI_API_KEY` |
| `LLM_BASE_URL` | No | Reserved for an OpenAI-compatible/self-hosted adapter |
| `ENABLE_IMAGE_ANALYSIS` | No | Set `false` to disable (default: `true`) |
| `ENVIRONMENT` | No | `development` or `production` |
| `ADMIN_TOKEN` | No | Bearer token for admin endpoints |

---

## 🚢 Deployment (Fly.io)

```bash
# Install Fly CLI
curl -L https://fly.io/install.sh | sh
fly auth login

# Create app
fly launch --no-deploy

# Set secrets
fly secrets set BEERBOT_BOT_ID=your_bot_id
fly secrets set DATABASE_URL="postgresql://..."
fly secrets set GROUPME_WEBHOOK_SECRET=your_random_webhook_secret
fly secrets set LLM_API_KEY=your_model_key
fly secrets set ADMIN_TOKEN=your_admin_token

# Deploy
fly deploy --build-arg GIT_SHA="$(git rev-parse HEAD)"
```

Set a long random `GROUPME_WEBHOOK_SECRET`, register the group through the admin
API, and set the GroupMe bot callback URL to:
```
https://your-app.fly.dev/callback?token=YOUR_WEBHOOK_SECRET
```

---

## 🌐 Multi-Group Setup

Register groups via the admin API:

```bash
# Register a new group
curl -X POST https://your-app.fly.dev/admin/groups \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"group_id": "12345", "bot_id": "abc123", "name": "My Group"}'

# List all groups
curl https://your-app.fly.dev/admin/groups \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

---

## 🏗 Architecture

```
src/beerbot/
├── main.py           # FastAPI app, webhook handler, routing
├── agent.py          # Beerius prompt, multimodal input, model orchestration
├── tools.py          # Request-scoped, validated read/write tools
├── delivery.py       # Durable inbox, atomic execution, outbox delivery and retention
├── repositories.py   # Database operations
├── models.py         # Pydantic models and enums
├── llm.py            # Provider-neutral model profile and capabilities
├── gateways/         # Canonical transport contracts and shadow adapters
├── routing.py        # Stable provider route identifiers
├── groupme_client.py # GroupMe API with multi-group support
├── database.py       # asyncpg pool and versioned migrations
└── config.py         # Environment configuration
```

**Key Design Decisions:**
- **Idempotency**: Inbound messages deduplicate by `(group_id, message_id)`; drink rows also retain their existing uniqueness constraint
- **Atomic transactions**: A message's database effects, tool results, completion, and queued reply commit together
- **Eastern timezone**: Consistent "today"/"this week" calculations
- **Glass-based detection**: Vision identifies drinks by container, not color
- **Registered groups only**: Inbound and outbound GroupMe traffic must map to a configured group
- **No chat corpus in Git**: Production messages, media, and local evaluation data belong in private storage

---

## Direction of Travel

GroupMe is the only production transport today. Its callbacks now enter a durable
inbox; its existing prompts, tools, and statistics remain authoritative. Workspace, gateway connection,
and gateway route records are maintained as a shadow model so additional
functionality can be built and verified without changing existing user behavior.

The first-party web/iOS application is intended to become the primary product
surface for accounts, global personal history, settings, and rich activity UI.
Messaging providers remain transport adapters: they normalize provider events
into a common envelope and deliver replies or notifications, but do not own
users, agent sessions, or activity semantics.

The target tenant boundary is a workspace rather than a messaging provider. A
workspace may have multiple gateway routes—GroupMe, SMS, WhatsApp, Discord, or
other channels—and each route uses provider-owned identifiers such as a GroupMe
group ID or receiving number plus sender/thread identity. Global people,
external identities, and workspace memberships are maintained as shadow state:
each existing GroupMe user maps to one provisional global person, while
memberships are inferred only from observed group-scoped activity or debt. The
legacy `users`, `beers`, and `group_id` paths remain authoritative; no global
stats or account behavior is exposed until shadow parity and the future activity
model are verified.

## Message execution and delivery

`POST /callback` validates the existing GroupMe registration and stores the event
before returning HTTP 200 with action `queued` or `duplicate`. A worker executes
pending messages in receipt order within each group. PostgreSQL claims and a
transaction-scoped group lock protect concurrent workers. The existing agent and
tools execute inside a single database transaction, bounded to 90 seconds. All
repository acquisitions share that connection; related writes and the stored
reply either commit together or roll back together. Model failures retry up to
three attempts with backoff; cancellation rolls back and leaves the inbox pending.
The recorded tool arguments/results describe successful committed executions.

A separate worker sends stored replies. Connect failures and HTTP 429 responses
retry up to five attempts with backoff; Retry-After seconds are honored. Read/write
timeouts, HTTP 408/5xx, and interrupted sends become `uncertain` rather than being
automatically repeated. GroupMe has no bot-post idempotency key, so uncertain
delivery cannot be resolved into exactly-once sending automatically.

Authenticated admins can inspect `GET /admin/messages/status` for counts, oldest
pending age, and the last 50 failures/uncertain deliveries (IDs and error codes,
without message content). `POST /admin/messages/outbox/{id}/retry` retries only a
stored failed reply. For uncertain delivery, pass `acknowledge_uncertain=true`
only after accepting the risk of a duplicate chat reply. This never reruns tools.
Failed executions require investigation; the worker does not retry them forever.

Completed message history survives restarts. Inbox payloads, tool results, and
outbound text expire after three days, with bounded cleanup roughly every minute.
Message IDs and state remain as deduplication tombstones. Media is fetched for
analysis, not stored as binary data. Expired content is not retryable.

`/health` remains process liveness. `/ready` checks database connectivity, model
client configuration, and worker tasks; Fly gates blue/green traffic on `/ready`.
Shutdown cancels workers, rolls back incomplete execution, and closes the pool.
Migration 4 is additive. Deduplication protects messages first accepted by this
release; it cannot retrospectively identify every command processed by earlier
releases. Weekly recaps still use their existing independent scheduler and are
not part of this message outbox. Rolling back to code predating migration 4 leaves
pending inbox/outbox records retained; deploy compatible worker code to drain them.

Identity maintenance is explicit and outside the message path. Authenticated
admins can inspect `GET /admin/identities/parity` and repair missing records with
`POST /admin/identities/reconcile`. Both accept `after_id` (default 0) and `limit`
(default 100, maximum 500). Follow `next_after_id` until null for a complete pass.
Reports contain counts and cursors only. Apply reports describe gaps repaired in
that page; preview again to verify the result. Conflicting links and blocked
identities are reported without modification. Existing names, roles, lifecycle
states, and identity links are preserved. New historical memberships have status
`observed`, which must never grant app access. Legacy migration-created active
memberships remain shadow data and also require explicit authorization at app cutover.

Repairs are atomic per page, serialized using a PostgreSQL advisory lock, and
use short statement/lock timeouts. A busy repair returns HTTP 409; a database
failure rolls the page back. Retry the same cursor after a failure. Reconciliation
is intentionally not scheduled yet. Future linking writers must share its locking
and transaction discipline. The old unconditional single-user observer was removed.

CI runs real PostgreSQL migration and reconciliation regressions. To run these
locally, set `BEERBOT_TEST_DATABASE_URL` to a disposable test database and run
`uv run --extra dev pytest`. Each test creates and drops a unique schema there;
the tests never use the application's `DATABASE_URL` for integration testing.

The model loop is explicit and provider-independent. `model_runtime.py` validates
each batch of tool arguments, executes tools sequentially, and feeds results back
through the selected adapter. `reply` completes the turn; raw model text does not
publish chat messages. Silence remains a successful turn with no reply call.
`AGENT_MAX_TOOL_CALLS` bounds model rounds (default 5); `AGENT_MAX_EXECUTED_TOOLS`
bounds actual tool executions (default 20). Invalid calls, incomplete responses,
or exhausted budgets raise and roll back the enclosing message transaction.

`LLM_PROVIDER=google` remains the default. Its adapter disables SDK automatic tool
execution and preserves the full native model content, including thought
signatures, between calls. Images and video remain supported through Gemini.

For a hosted or self-hosted compatible server, set `LLM_PROVIDER=openai_compatible`,
`LLM_BASE_URL` to its API prefix (for example `https://server.example/v1`), and
`LLM_MODEL` to its model identifier. Set `LLM_API_KEY` if required; the adapter never
sends `GEMINI_API_KEY` to a compatible server. It uses the OpenAI SDK Chat
Completions interface with automatic SDK retries disabled, preserves call IDs and
returned reasoning fields, and supports text, images, and function tools. Recaps
use the same configured adapter. A keyless local server may omit `LLM_API_KEY`.

Compatible video input is opt-in: `LLM_VIDEO_FORMAT=video_url` enables that
server-specific content format; `disabled` rejects video input if video analysis
is enabled. Set `LLM_SUPPORTS_VIDEO=false` to disable video analysis instead.
This is transport support, not a claim that every Kimi or compatible deployment
accepts video or meets Beerius's quality requirements. Run an isolated canary
against the actual endpoint before changing production away from Gemini.

General CI lives in `.github/workflows/ci.yml` and runs lint, formatting, tests,
package build, and container build. A successful push to `main` is deployed to
Fly with a health-checked blue-green replacement; production deploys are
serialized, superseded revisions are skipped, and `/health` plus `/version` are
verified before the workflow succeeds. Production-derived evaluation data must
not be committed; a future replay suite should use sanitized fixtures or an
access-controlled external store.

---

## Personal dashboard (invite-only)

Only registered production groups contribute to the personal dashboard. Classify
a workspace with `PATCH /admin/workspaces/{workspace_id}/environment` (admin bearer
token) and `{"environment":"test"}` to exclude every gateway group mapped to it
from totals, charts, recent activity, and the group picker. Explicit requests for
an excluded group return 404. The setting is stored in `workspaces.settings`,
preserves other settings, and can be reversed with `{"environment":"production"}`.
Unclassified legacy workspaces retain production visibility. This changes only
personal-dashboard visibility: no identities, drink rows, group registrations,
bot replies, or group-specific leaderboards/recaps are modified. It is not process,
database, or credential isolation; use a separate deployment/database for risky tests.

`/app` is the first-party personal view: this week and last week,
all-time drinks, Split-G total, eight-week trend, drink breakdown, and the latest
30 entries. It combines authoritative legacy `beers` with native app activity,
without double counting mirrored entries. It does not read group chats/photos,
send notifications, or change the GroupMe agent. Group filters expose personal
history and explicitly granted app groups; shadow memberships grant no app access.

### Native logging (feature-gated)

Migration 6 adds native activity, explicit app workspace access, durable command
receipts, and a `personal_activity` read view. It does not backfill, rewrite, or
replace legacy drink rows, and does not modify the GroupMe agent/tools/repositories.

- `APP_ACTIVITY_ENABLED=true` exposes Log / Edit / Undo and app-only group creation.
  It defaults to false as an independent kill switch; reads still work when off.
- `PUT /admin/workspaces/{workspace_id}/app-access`, with admin bearer authorization
  and `{"account_id":"<confirmed-account>","role":"member","active":true}`, grants
  self-logging access. Set `active:false` to revoke it. Inferred historical
  memberships are never sufficient. Production group access must be explicitly
  confirmed by an operator; the app cannot claim existing groups by ID or name.
- Admin invitations accept either an existing `person_id`, or a `name` to create
  a new native person with no GroupMe identity. Normal email proof is still required.
- App-only groups create an internal workspace and an explicit owner grant. They
  do not register a GroupMe bot, create fake users, or send chat messages. Owners
  can manage these groups and create email-specific invitation links in the app.
- In a connected workspace, app writes require exactly one real legacy group and
  one real mapped legacy user. A log inserts both the authoritative legacy row
  and its app record in one transaction. No provider message ID is fabricated.
  Ambiguous/missing mappings fail closed. GroupMe stats and recaps therefore count
  app-created drinks, but logging in the app sends no GroupMe message.
- Edits and undo apply only to the account's own app-created entries. Old GroupMe
  entries remain managed through GroupMe. Opaque revisions reject stale edits,
  including concurrent changes made by GroupMe. GroupMe deletions cascade to the
  app reference; its durable create receipt prevents a retry from resurrecting it.
- Every mutation has an account-scoped UUID request ID and payload fingerprint.
  The receipt commits with the mutation, survives undo, and is retained. The UI
  retains an unconfirmed request in tab session storage for safe retry; the
  database, never browser storage, is authoritative. A tab refresh preserves the
  pending request, and sign-out clears the local draft.
- Writes are limited to 120 commands/account/hour and 20 owned groups/account.
  These controls supplement, not replace, future public-launch abuse protection.

Run `tests/test_activity.py` with a disposable PostgreSQL database before release.
It covers native users, mirrored statistics, GroupMe edit/undo compatibility,
atomic failure rollback, concurrent retries, test-workspace exclusion, revoked
access, mapping drift, and CSRF. Roll back by disabling the feature or redeploying
the prior application; leave additive schema/data intact. Old app code may omit
native-only history until rolled forward, but GroupMe remains on its legacy rows.

### App group invitations and management

Migration 7 adds `app_group_invitations` and `app_group_events`. Owners can rename
an app-only group, create/revoke invitations, remove members, or transfer ownership
to a current verified member. Members can leave after transferring ownership if
they are the owner. Existing GroupMe groups cannot use these management endpoints,
even if an operator grants an app owner role. Test workspaces remain excluded.

Invitation flow:

1. The owner chooses **Manage app groups**, enters a friend's email, and creates
   a link. The owner shares it manually; creation sends no invitation email.
2. The link contains a random UUID locator in the fragment. It is not an auth
   credential: no membership is granted without proof of the exact invited email.
   Public previews expose only the group and inviter names, never recipient emails.
3. An unregistered recipient can prepare a pending native account through the
   valid invitation, then complete the existing email-code flow. A display name
   is set only after email proof. Existing accounts/person mappings are reused
   without name-based matching, reassignment, or renaming.
4. The recipient explicitly chooses **Join group**. Acceptance rechecks expiry,
   revocation, matching verified email, current inviter ownership/account status,
   and native production workspace eligibility. An accepted invitation cannot
   reactivate a removed member. Re-inviting an inactive former owner grants only
   member access, not their former privileges.

Links expire after seven days. Creating another invitation for the same email
revokes earlier pending links. Ownership transfer revokes pending invitations;
revoking the inviter's access also makes their links unavailable. The UI preserves
the locator through sign-in in tab session storage. Neither a missing invitation
nor an unverified email can create group membership.

Joining shares display names and membership roles with that group's members.
Recipient emails on pending invitations are visible only to owners. Personal drink
histories remain person-scoped: group membership does not expose another person's
stats or records. Leaving/removal revokes group management and logging/edit access,
but does not delete personal history or accounts. Cross-group notifications and
group-shared activity feeds are not part of this increment.

Mutations use durable account-scoped request IDs and retain group action events.
Creation is bounded to 50 invitations/group/day in addition to account command
limits. These are pilot limits, not a substitute for public-launch abuse controls.
Keep `APP_ACTIVITY_ENABLED=false` as the shared app-write kill switch. Rollbacks
leave additive tables intact and do not affect the GroupMe agent.

### Email configuration and operator onboarding

Before enabling real sign-in, configure `WEB_ORIGIN` to the exact HTTPS app origin,
plus `SMTP_HOST`, `SMTP_PORT` (465 for implicit TLS, otherwise mandatory STARTTLS),
`SMTP_FROM`, and the provider's `SMTP_USERNAME` / `SMTP_PASSWORD` as Fly secrets.
No plaintext SMTP or certificate-validation fallback is supported. An empty email
configuration fails closed, without affecting GroupMe readiness. Set
`WEB_ORIGIN=http://127.0.0.1:8089` and `ENVIRONMENT=development` only for local testing.

Account rollout:

1. An administrator independently confirms the existing person and the email they
   want to use. Never match by display name alone or infer account ownership from
   a group membership, incoming message, or unverified email.
2. `POST /admin/accounts/invite` with the existing admin bearer token and JSON
   `{"person_id":"<confirmed-person-id>","email":"<confirmed-email>"}` creates a
   pending account with a seven-day invitation. Existing person/email bindings
   conflict; they are never overwritten. The endpoint itself sends no email.
3. The invited person opens `/app`, requests a code, and types the emailed eight
   digits in the **same browser** within ten minutes. Email delivery must be tested
   with the real mailbox before declaring this flow ready. Unknown, throttled,
   expired, and delivery-failed requests have the same generic response.
4. Successful proof activates the account, consumes its outstanding challenges,
   and creates a 30-day HttpOnly, Secure, SameSite=Strict session. The database
   stores only hashes of session/browser tokens and codes. Sign-out revokes the
   session server-side. Disabled accounts and merged/disabled people fail closed.

Limits: five attempts per challenge; one code/minute, three/15 minutes/account,
and 100/15 minutes globally across machines. Challenges are removed after a day
and expired sessions removed on login requests. SMTP failure logs omit recipients,
codes, and provider error details. This initial flow deliberately does not include
self-service identity claiming, email changes, account merging, or password recovery.
For emergency revocation, set the affected account's `status` to `disabled` through
an authenticated operator database session; all its sessions immediately fail their
next authorization check. Re-invitation/reassignment requires explicit operator
review, not a public reset endpoint.

Migration 5 adds only new tables; old releases ignore them. Keep the existing
blue-green deploy strategy and `/ready` checks. An application rollback can leave
these additive tables in place. Run the real PostgreSQL suite (including
`tests/test_web.py`) using `BEERBOT_TEST_DATABASE_URL` before release. Do not test
against the application's production `DATABASE_URL`.

## 📄 License

MIT

---

*Cheers! 🍻*
