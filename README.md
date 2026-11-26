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

Tests that need PostgreSQL skip unless `BEERBOT_TEST_DATABASE_URL` points at a disposable
database; each test creates and drops its own schema there. They never use `DATABASE_URL`.
`BEERBOT_LIVE_LLM=1` also runs the real-model chat scenarios in `tests/test_live_chat.py`.

---

## ⚙️ Configuration

Settings live in `src/beerbot/config.py` (Pydantic Settings, read from the environment or
`.env`). The required ones are `BEERBOT_BOT_ID`, `DATABASE_URL`, and `LLM_API_KEY` (or
`GEMINI_API_KEY`). Production secrets and where each one lives are in
[docs/runbook.md](docs/runbook.md#credentials).

---

## 🚢 Deployment (Fly.io)

Every push to `main` runs CI, deploys blue-green, and verifies `/health`, `/ready`, `/version`
(the new git SHA), and `/ready/db`; a failed deploy rolls itself back. You normally never run
`fly deploy` yourself. Deploys, rollbacks, and incidents: [docs/runbook.md](docs/runbook.md).

For a new deployment:

```bash
fly launch --no-deploy
fly secrets set BEERBOT_BOT_ID=your_bot_id DATABASE_URL="postgresql://..." \
  GROUPME_WEBHOOK_SECRET=your_random_webhook_secret LLM_API_KEY=your_model_key \
  ADMIN_TOKEN=your_admin_token
fly deploy --build-arg GIT_SHA="$(git rev-parse HEAD)"
curl https://your-app.fly.dev/ready/db  # after every manual deploy
```

Register the group through the admin API (below), then set the GroupMe bot callback URL to
`https://your-app.fly.dev/callback?token=YOUR_WEBHOOK_SECRET`.

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

## 🛠 beerdev: asks from the chat, shipped

`beerdev` is a second GroupMe bot in the same app. It answers questions about the bot, the
request queue, recent errors, deploys, and costs, and it turns asks into shipped changes.

| In the chat | Meaning |
|---|---|
| 💡 Queued: … | Accepted; beerdev will build it |
| 🤓 … | An answer ("beerdev any errors lately?") |
| 👀 Filed for a human: … | Needs setup a person must do (it is in the inbox) |
| 🛠️ New: … / ✅ Shipped: … | Deployed and verified |
| 👀 Needs the owner's review: … | Held for the owner as an open PR |
| 🤷 Skipping: … | beerdev declined it, with the reason |
| 🚫 Vetoed: … | The owner stopped it, or it is being removed |
| 📜 beerdev this week | Monday digest: shipped, declined, and waiting on a human |

1. **Talk to beerdev:** start a message with "beerdev", or reply to one of beerdev's posts.
   Everything else goes to Beerius, which never sees beerdev's messages. beerdev answers from
   live status, the request queue, deploys, and the docs, using only its own narrow tools. It
   files requests as issues with no names or chat text, since this repository is public.
2. **Build** (`beerdev-build.yml`): Claude Code builds the next `beerdev` issue, incidents first,
   then runs lint, the full suite (including the pipeline policy in `tests/test_policy.py`), and
   the live chat e2e with read-only credentials. Changes beyond routine app code (workflows,
   scripts, Docker, Fly, and security-sensitive files such as auth, config, and migrations) need
   an independent reviewer's approval, or they wait for the owner as an open PR. Dependency
   changes always wait for the owner. Ship refuses changes to beerdev's kernel, merges, deploys
   through CI, appends `CHANGELOG.md`, and tells the chat.
3. **Lead** (`beerdev-lead.yml`, Mondays) proposes up to three improvements. Kernel changes, new
   secrets or paid services, data deletion, and product calls wait in the inbox as a `proposal`.
4. **ops** (`ops.yml`) is the SRE: every 5 minutes it checks what users would feel, rolls back a
   deploy that broke production, pages the owner in a private GroupMe group, and files tickets
   that start a beerdev build. See the runbook's "Incidents".

**Steering (owner only):**
- In the chat, reply "veto" or "approve" to one of beerdev's messages, or say "beerdev veto the
  gym tracker". beerdev checks the sender's GroupMe id and acts on the owner's own words, never
  the model's reading. A veto closes queued work, stops a build at merge, or removes shipped work
  through a new build. Approve makes a proposal or waiting ask buildable, merges a held PR, and
  retries a failed build.
- From a terminal: `gh issue close <n> --reason "not planned"` vetoes; `gh issue edit <n>
  --add-label beerdev --remove-label inbox` then `gh workflow run beerdev-build.yml` approves.
- The inbox lists everything waiting on you:
  https://github.com/alexeldeib/beerbot/issues?q=is%3Aopen+label%3Ainbox

---

## 🏗 Architecture

```
src/beerbot/
├── main.py           # FastAPI app, webhook router (Beerius or beerdev), admin endpoints
├── agent.py          # Beerius: prompt, multimodal input, rate limiting, recaps
├── beerdev.py        # beerdev, the peer agent: router rule, prompt, narrow tools
├── tools.py          # Beerius's request-scoped, validated tools
├── model_runtime.py  # Bounded tool loop; Google and OpenAI-compatible sessions
├── delivery.py       # Durable inbox, atomic execution, outbox delivery, retention, status
├── usage.py          # Token counts and the spend report
├── dispatch.py       # Starts GitHub workflows (ops, builds) from the app
├── repositories.py   # Database operations
├── models.py         # Pydantic models and enums
├── llm.py            # Provider-neutral model profile and capabilities
├── groupme_client.py # GroupMe API with multi-group support
├── database.py       # asyncpg pool and versioned migrations
├── config.py         # Environment configuration
├── web.py            # /app: invite-only accounts and personal history
├── activity.py       # /app native Log/Edit/Undo (feature-gated)
└── app_groups.py     # /app app-only groups and invitations (feature-gated)
```

**Key Design Decisions:**
- **Idempotency**: Inbound messages deduplicate by `(group_id, message_id)`; drink rows also retain their existing uniqueness constraint
- **Atomic transactions**: A message's database effects, tool results, completion, and queued reply commit together
- **Eastern timezone**: Consistent "today"/"this week" calculations
- **Glass-based detection**: Vision identifies drinks by container, not color
- **Registered groups only**: Inbound and outbound GroupMe traffic must map to a configured group
- **No chat corpus in Git**: Production messages, media, and local evaluation data belong in private storage

More detail:
- [docs/architecture.md](docs/architecture.md): message execution, delivery, retention, and the model loop
- [docs/idle-compute.md](docs/idle-compute.md): wakeups, health checks, and keeping Neon idle
- [docs/runbook.md](docs/runbook.md): credentials, access, deploys, incidents, spend
- [docs/app.md](docs/app.md): the web app, accounts, native logging, and app groups
- [docs/production-launch.md](docs/production-launch.md): what's left before a wider rollout

---

## 📄 License

MIT

---

*Cheers! 🍻*
