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

# Deploy: merging to main deploys through CI (docs/runbook.md); to redeploy without a change:
gh workflow run ci.yml --ref main
```

## Architecture

Two peer agents in one app. A deterministic router picks Beerius or beerdev for each message, and
the chosen agent runs one bounded tool loop:

```
GroupMe Webhook → main.py (validate, route, persist inbox, acknowledge)
    → delivery.py worker → claim (attempt committed) → transactional BeerAgent.answer()
    → History: the group's last ten stored messages, both bots' posts included
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
├── agent.py          # BeerAgent: system prompt, media, rate limiting, the tool loop both bots use
├── model_runtime.py  # Bounded tool loop, Google and OpenAI-compatible sessions
├── delivery.py       # Durable message execution, outbox, retries, retention and status
├── usage.py          # Model token counts, list prices, spend report against MODEL_BUDGET_USD
├── beerdev.py        # beerdev, the peer agent: router rule, prompt, narrow tools
├── tools.py          # Tool factory: async closures with validation
├── llm.py            # Provider-neutral model profile and capability metadata
├── config.py         # Pydantic Settings for env vars
├── models.py         # Pydantic models (GroupMeMessage, User, DrinkType enum)
├── database.py       # asyncpg pool and schema management
├── repositories.py   # Data access layer
├── groupme_client.py # GroupMe API wrapper
├── dispatch.py       # Starts GitHub workflows (ops, builds) from the app
├── web.py            # /app: invite-only accounts, sign-in, personal history
├── activity.py       # /app native Log/Edit/Undo and the shared account rules
└── app_groups.py     # /app app-only groups and invitations
```

## Key Patterns

- **One agent per message**: the router picks Beerius or beerdev; each runs one bounded tool loop, with no regex command routing
- **Closure-based tools**: Tools bind group_id/message_id/sender via closure — AI never sees security-sensitive IDs
- **Async-first**: All I/O is async; the loop executes validated tool calls sequentially
- **Idempotency**: Message deduplication via `(message_id, user_id, drink_type)`
- **Durability**: Inbox `(group_id, message_id)` deduplication; message DB writes/results/outbox commit atomically
- **Idle compute**: Queue workers wake on committed receipts, replies and retries; empty queues use aligned hourly recovery, not second-level polling. `/ready` must stay database-free. After every deploy, verify the new version and call `/ready/db` to recover handoff work. Keep one Fly machine running unless external scheduling is explicitly implemented. See `docs/idle-compute.md`.
- **Delivery**: Only connect failures and rate limits automatically retry; uncertain sends need explicit admin review
- **Retention**: Inbox content, successful tool traces, and reply text expire after three days; IDs remain for deduplication
- **Timezone**: Eastern time (America/New_York) for "today"/"week" calculations
- **Rate limiting**: TokenBucket per group — tool-call replies always sent, personality replies rate-limited
- **Multi-group**: Single bot instance serves registered GroupMe groups
- **First-party product**: Web/iOS will own accounts and global personal history; messaging integrations are adapters. GroupMe's legacy `users`/`beers`/`group_id` rows stay authoritative; a workspace is the tenant, and each GroupMe group maps to one (`groupme:<id>`)
- **Web accounts**: `/app` uses explicit admin-approved invitations plus email proof. The admin invites by `groupme_user_id`, which links that user's history (`users.person_id`) in the same transaction, or by `person_id`/`name`. A verified binding is never reassigned; an unverified one yields to the admin's latest invite. GroupMe participation alone never grants app access. Never auto-claim by name or email alone. `POST /admin/accounts/{id}/disable` revokes. Sign-in limits are per source IP in memory (one machine) before any database work
- **Native activity**: `activity.py` owns explicit app access and idempotent Log/Edit/Undo. A connected app log writes the real legacy row and app reference atomically. `personal_activity` reads legacy rows plus native-only entries, without double counting. Keep GroupMe repositories and tool behavior unchanged; never invent provider identities. Test with PostgreSQL and feature-gate app writes.
- **App groups**: `app_groups.py` manages only native workspaces with no legacy group mapping. Invitations require a verified matching email and a live owner grant. Group membership shares names/roles, never another person's drink history. Keep workspace-before-membership and user-before-beer lock ordering when extending commands.
- **Migrations**: never `SELECT *` (or `RETURNING *`) from a table a migration alters. Neon's pooler caches server-side plans by query text, so the old plan fails with `InvalidCachedStatementError` until the query text changes (2026-11-25 outage)
- **Private data**: Never add production messages, media, or local evaluation corpora to Git
- **beerdev kernel**: beerdev may change workflows, scripts, Docker, Fly, and dependencies, but never `.github/workflows/beerdev-build.yml`, `beerdev-lead.yml`, `.github/beerdev-*.md`, `.claude/`, `CLAUDE.md`/`CLAUDE.local.md` in any directory, or `tests/test_policy.py`. The policy test pins secrets, write permissions, triggers, hosts, deploy verification and rollback, and owner-only steering: update it by hand when changing those on purpose. Changes beyond `src/`, `docs/`, `README.md`, and `tests/test_*.py`, and to `src/beerbot/{beerdev,main,dispatch,config,database,web}.py`, need the independent reviewer (`ROUTINE` in `beerdev-build.yml`), or they wait as a PR in the inbox; the owner's "approve" merges a held PR. Each build starts the next, and each new ops ticket starts one
- **beerdev**: a peer agent in the same app (`beerdev.py`). The router in the callback (`beerdev.addressed`) sends messages that start with "beerdev" or reply to beerdev's own posts to beerdev, recorded as `message_inbox.agent`. Beerius never sees those messages, and its prompt points capability asks to beerdev. beerdev's only tools are `app_status`, `request_queue`, `deploys`, `read_doc` (an allowlist), `file_request`, `steer` (owner by sender id, bound to the owner's words), and `reply`. Its replies post as `BEERDEV_BOT_ID`, threaded. `beerdev.yml` is only the Monday digest; `beerdev-build.yml` ships `beerdev` issues (incidents first) with Claude Code (`.github/beerdev-prompt.md`), appending `CHANGELOG.md`; `beerdev-lead.yml` proposes up to three improvements weekly. Each chat feature adds a real-model scenario to `tests/test_live_chat.py` (`BEERBOT_LIVE_LLM=1`). Operations: `docs/runbook.md`
- **Spend**: every model call goes through a session that calls `usage.record`, and counts are written only while the database is already awake (after each message, hourly sweep). `/admin/usage` reports month to date against `MODEL_BUDGET_USD`; ops files one `inbox` issue a month when over. New model call sites must record usage; new models need `usage.PRICES`. Runbook: "Spend"
- **ops (the SRE)**: `ops.yml` + `scripts/ops.py` run every 5 minutes, after CI on main, and when the app reports a failure. Symptoms (`/health`, `/ready`, the memory-only `oldest_unprocessed_seconds`/`oldest_undelivered_seconds`) never wake the database. The decision table has no model: wait out a failing dependency (checked first, from `last_error`); roll back a clean deploy that broke production within an hour, once (`rollback.yml`, which refuses to roll onto a release a rollback left); page for anything else. `OPS_TOKEN` only reads. **Never restart.** An incident is one `incident` issue assigned to the owner, paged via the private "beerbot alerts" GroupMe group (`OPS_ALERT_BOT_ID`) on open, on a failed rollback, and on resolve (an `ack` label stops pages), with a **Root cause** ticket for beerdev (the SWE) when code was involved. Failure classes, budget, and credential expiry are tickets. A daily run (12:23 UTC) sweeps what needs the database. Terminal failures store a publishable `error_detail` (module-qualified exception type and HTTP status plus this repo's frames, never message text) and bump the memory-only `TERMINAL_FAILURES`; `dispatch.py` starts ops right away when `BEERDEV_GITHUB_TOKEN` is set, and the app re-enables ops if GitHub disabled it for inactivity

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
- `ADMIN_TOKEN` - Bearer token for admin endpoints (Fly only, never GitHub)
- `OPS_TOKEN` - Read-only token for the status and usage endpoints (ops)
- `BEERDEV_GITHUB_TOKEN` - Fine-grained (this repo: Issues, Actions) for beerdev and workflow starts
- `BEERDEV_BOT_ID` - beerdev's replies post as this bot

## Coding Conventions

- Line length: 100 characters (ruff)
- Type hints on all functions
- Conventional commits: `feat:`, `fix:`, `refactor:`, `chore:`
- Tests in `tests/` mirroring src structure
- Logger per module: `logger = logging.getLogger(__name__)`
