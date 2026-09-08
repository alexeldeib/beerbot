# Beerbot

GroupMe bot that tracks alcoholic drink consumption (beer, wine, cocktails, hard seltzers) with AI-powered image analysis, leaderboards, and witty responses.

## Tech Stack

- **Python 3.11+** with FastAPI, asyncpg, Pydantic v2
- **Explicit model loop** with Google Gemini 3.6 Flash by default and OpenAI-compatible adapters
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
GroupMe Webhook → main.py (validate, persist inbox, acknowledge)
    → delivery.py worker → transactional BeerAgent.process_message()
    → Build system prompt (personality + context)
    → Build contents (text + images as multimodal parts)
    → Create tool closures (group_id/sender bound via closure)
    → explicit validated tool loop through the configured model adapter
    → Rate limit check → commit tools/results/outbox together
    → delivery.py sender → deliver stored reply independently
```

## Project Structure

```
src/beerbot/
├── main.py           # FastAPI app, webhook handler, admin endpoints
├── agent.py          # BeerAgent: system prompt, rate limiting, conversation history
├── model_runtime.py  # Bounded tool loop, Google and OpenAI-compatible sessions
├── delivery.py       # Durable message execution, outbox, retries, retention and status
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
- **Async-first**: All I/O is async; the loop executes validated tool calls sequentially
- **Idempotency**: Message deduplication via `(message_id, user_id, drink_type)`
- **Durability**: Inbox `(group_id, message_id)` deduplication; message DB writes/results/outbox commit atomically
- **Delivery**: Only connect failures and rate limits automatically retry; uncertain sends need explicit admin review
- **Retention**: Inbox content, successful tool traces, and reply text expire after three days; IDs remain for deduplication
- **Timezone**: Eastern time (America/New_York) for "today"/"week" calculations
- **Rate limiting**: TokenBucket per group — tool-call replies always sent, personality replies rate-limited
- **Multi-group**: Single bot instance serves registered GroupMe groups
- **Future gateways**: Multiple gateway conversations may map to one workspace; gateways do not define the tenant
- **Compatibility first**: GroupMe remains the live source of truth while workspace/gateway records are shadow state
- **First-party product**: Web/iOS will own accounts and global personal history; messaging integrations are adapters
- **Shadow identities**: The GroupMe path does not use people, external identities, or memberships for live authorization or stats
- **Web accounts**: `/app` uses explicit admin-approved person/email invitations plus email proof, never shadow membership as authorization. Read-only personal stats use the linked `users.person_id`; the GroupMe path is unchanged. Never auto-claim by name or email alone.
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
- `LLM_PROVIDER` - `google` or `openai_compatible`
- `LLM_MODEL` - pinned model name (default: `gemini-3.6-flash`)
- `LLM_BASE_URL` - required API prefix for an OpenAI-compatible/self-hosted endpoint
- `LLM_SUPPORTS_IMAGES`, `LLM_SUPPORTS_VIDEO`, `LLM_SUPPORTS_TOOLS` - endpoint capabilities
- `ENABLE_IMAGE_ANALYSIS` - Enable/disable image analysis (default: true)
- `AGENT_MAX_TOOL_CALLS` - Max model rounds per message (default: 5)
- `AGENT_MAX_EXECUTED_TOOLS` - Max individual tools per message (default: 20)
- `LLM_VIDEO_FORMAT` - `disabled` or explicit compatible-server `video_url` format
- `WEEKLY_RECAP_ENABLED` - Enable weekly recap generation (default: true)
- `ADMIN_TOKEN` - Bearer token for admin endpoints

## Coding Conventions

- Line length: 100 characters (ruff)
- Type hints on all functions
- Conventional commits: `feat:`, `fix:`, `refactor:`, `chore:`
- Tests in `tests/` mirroring src structure
- Logger per module: `logger = logging.getLogger(__name__)`
