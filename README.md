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
- **Google Gemini** (gemini-2.0-flash) for vision
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
| `GEMINI_API_KEY` | No | Google Gemini API key |
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
fly secrets set GEMINI_API_KEY=your_gemini_key
fly secrets set ADMIN_TOKEN=your_admin_token

# Deploy
fly deploy
```

Set your GroupMe bot callback URL to:
```
https://your-app.fly.dev/callback
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
├── services.py       # Message parsing, stats, business logic
├── repositories.py   # Database operations
├── models.py         # Pydantic models and enums
├── vision.py         # Gemini Vision integration
├── groupme_client.py # GroupMe API with multi-group support
├── database.py       # asyncpg pool and migrations
└── config.py         # Environment configuration
```

**Key Design Decisions:**
- **Idempotency**: Deduplicated by `(message_id, user_id)`
- **Atomic transactions**: Batch logging uses PostgreSQL transactions
- **Eastern timezone**: Consistent "today"/"this week" calculations
- **Glass-based detection**: Vision identifies drinks by container, not color

---

## 🔬 Vision Pipeline (for Development)

Beerbot includes a vision prompt testing and optimization pipeline for improving drink detection accuracy.

### Directory Structure

```
data/
├── images/          # Scraped GroupMe images
├── labels.json      # Ground truth labels (reviewed: true/false)
├── metadata.json    # Image metadata from scraping
├── prompts/         # Vision prompt versions
│   └── v12.txt      # Current production prompt
└── results/
    ├── baseline_latest.json  # Current best results
    └── *.json                 # Historical evaluation results
```

### Local Workflow

```bash
# 1. Scrape new images from GroupMe (requires GROUPME_ACCESS_TOKEN)
uv run scripts/scrape_images.py --scrape --incremental

# 2. Auto-label with Gemini (requires GEMINI_API_KEY)
uv run scripts/scrape_images.py --label

# 3. Review labels interactively
uv run scripts/review_labels.py

# 4. Evaluate prompt accuracy
uv run scripts/eval_prompt.py

# 5. Evaluate specific prompt version
uv run scripts/eval_prompt.py --prompt data/prompts/v12.txt

# 6. CI-style evaluation with baseline comparison
uv run scripts/eval_prompt.py --reviewed-only --ci --baseline data/results/baseline_latest.json
```

### GitHub Actions

Two automated workflows help maintain vision accuracy:

**1. Scrape Workflow** (`.github/workflows/scrape.yml`)
- Runs weekly (or manually)
- Scrapes new images from GroupMe
- Auto-labels with Gemini
- Creates PR with new images for review

**2. Evaluate Workflow** (`.github/workflows/evaluate.yml`)
- Triggers on changes to labels, prompts, or vision.py
- Compares against baseline accuracy
- Posts results as PR comment
- Fails if accuracy drops below threshold

### GitHub Secrets Required

| Secret | Description |
|--------|-------------|
| `GROUPME_ACCESS_TOKEN` | GroupMe API token for scraping images |
| `GEMINI_API_KEY` | Google Gemini API key for labeling and evaluation |

To get these tokens:
- **GroupMe**: Visit [dev.groupme.com](https://dev.groupme.com), click "Access Token"
- **Gemini**: Visit [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

### Updating the Baseline

After improving the prompt and verifying accuracy:

```bash
# Run full evaluation
uv run scripts/eval_prompt.py --output data/results/new_baseline.json

# If accuracy improved, update baseline
cp data/results/new_baseline.json data/results/baseline_latest.json
```

---

## 📄 License

MIT

---

*Cheers! 🍻*
