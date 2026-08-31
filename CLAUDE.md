# Beerbot

GroupMe bot that tracks alcoholic drink consumption (beer, wine, cocktails, hard seltzers) with AI-powered image analysis, leaderboards, and witty responses.

## Tech Stack

- **Python 3.11+** with FastAPI, asyncpg, Pydantic v2
- **Configurable LLM profile** (Google Gemini 3.6 Flash by default) with AFC
- **PostgreSQL** with async connection pooling
- **uv** for dependency management
- **Fly.io** for deployment

## Commands

```bash
# Install dependencies
uv sync

# Run local server
uv run uvicorn src.beerbot.main:app --reload --port 8080

# Run tests
uv run pytest

# Run specific test with verbose output
uv run pytest tests/test_tools.py -v

# Lint and format
uv run ruff check src tests
uv run ruff format src tests

# Deploy
fly deploy
```

## Architecture

Single AI agent processes every message via Gemini function calling:

```
GroupMe Webhook → main.py (parse, skip bots) → BeerAgent.process_message()
    → Build system prompt (personality + context)
    → Build contents (text + images as multimodal parts)
    → Create tool closures (group_id/sender bound via closure)
    → configured model endpoint with automatic function calling
    → Rate limit check → send reply if any
```

## Project Structure

```
src/beerbot/
├── main.py           # FastAPI app, webhook handler, admin endpoints
├── agent.py          # BeerAgent: system prompt, AFC, rate limiting, conversation history
├── tools.py          # Tool factory: 15 async closures (7 write, 8 read) with validation
├── llm.py            # Provider-neutral model profile and capability metadata
├── gateways/         # Canonical inbound transport contracts and adapters
├── routing.py        # Stable gateway route identifiers
├── config.py         # Pydantic Settings for env vars
├── models.py         # Pydantic models (GroupMeMessage, User, DrinkType enum)
├── database.py       # asyncpg pool and schema management
├── repositories.py   # Data access layer (unchanged)
└── groupme_client.py # GroupMe API wrapper (unchanged)
```

## Key Patterns

- **Single agent**: One Gemini call with function calling replaces regex + command routing + separate AI
- **Closure-based tools**: Tools bind group_id/message_id/sender via closure — AI never sees security-sensitive IDs
- **Async-first**: All I/O uses asyncpg and httpx; AFC uses native async tool callables
- **Idempotency**: Message deduplication via `(message_id, user_id, drink_type)`
- **Timezone**: Eastern time (America/New_York) for "today"/"week" calculations
- **Rate limiting**: TokenBucket per group — tool-call replies always sent, personality replies rate-limited
- **Multi-group**: Single bot instance serves registered GroupMe groups
- **Future gateways**: Multiple gateway conversations may map to one workspace; gateways do not define the tenant
- **Compatibility first**: GroupMe remains the live source of truth while workspace/gateway records are shadow state
- **First-party product**: Web/iOS will own accounts and global personal history; messaging integrations are adapters
- **Shadow identities**: People, external identities, and memberships are not used for live authorization or stats
- **Private data**: Never add production messages, media, or local evaluation corpora to Git

No repository-local issue tracker is configured.

## Environment Variables

Required:
- `BEERBOT_BOT_ID` - GroupMe bot ID
- `DATABASE_URL` - PostgreSQL connection string
- `LLM_API_KEY` or `GEMINI_API_KEY` - model endpoint credential

Optional:
- `GROUPME_WEBHOOK_SECRET` - callback bearer token in the callback URL
- `REQUIRE_REGISTERED_GROUPS` - reject unknown GroupMe group IDs (default: true)
- `LLM_PROVIDER` - model adapter name (currently `google`)
- `LLM_MODEL` - pinned model name (default: `gemini-3.6-flash`)
- `LLM_BASE_URL` - reserved for an OpenAI-compatible/self-hosted endpoint adapter
- `LLM_SUPPORTS_IMAGES`, `LLM_SUPPORTS_VIDEO`, `LLM_SUPPORTS_TOOLS` - endpoint capabilities
- `ENABLE_IMAGE_ANALYSIS` - Enable/disable image analysis (default: true)
- `AGENT_MAX_TOOL_CALLS` - Max AFC tool calls per message (default: 5)
- `WEEKLY_RECAP_ENABLED` - Enable weekly recap generation (default: true)
- `ADMIN_TOKEN` - Bearer token for admin endpoints

## Coding Conventions

- Line length: 100 characters (ruff)
- Type hints on all functions
- Conventional commits: `feat:`, `fix:`, `refactor:`, `chore:`
- Tests in `tests/` mirroring src structure
- Logger per module: `logger = logging.getLogger(__name__)`
