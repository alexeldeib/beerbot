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
- **Idempotency**: Deduplicated by `(message_id, user_id, drink_type)`
- **Atomic transactions**: Batch logging uses PostgreSQL transactions
- **Eastern timezone**: Consistent "today"/"this week" calculations
- **Glass-based detection**: Vision identifies drinks by container, not color
- **Registered groups only**: Inbound and outbound GroupMe traffic must map to a configured group
- **No chat corpus in Git**: Production messages, media, and local evaluation data belong in private storage

---

## Direction of Travel

GroupMe is the only production transport today and remains on its unchanged
legacy callback, tool, and statistics path. New workspace, gateway connection,
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

The model profile is also configuration-driven. Google is the implemented
runtime today; the next explicit agent-loop iteration will add adapters for
OpenAI-compatible endpoints, including self-hosted multimodal models, and will
advertise image, video, and tool-calling capabilities independently.

General CI lives in `.github/workflows/ci.yml` and runs lint, formatting, tests,
package build, and container build. A successful push to `main` is deployed to
Fly with a health-checked blue-green replacement; production deploys are
serialized, superseded revisions are skipped, and `/health` plus `/version` are
verified before the workflow succeeds. Production-derived evaluation data must
not be committed; a future replay suite should use sanitized fixtures or an
access-controlled external store.

---

## 📄 License

MIT

---

*Cheers! 🍻*
