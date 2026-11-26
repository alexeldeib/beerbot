# How a message is handled

The wake and idle-compute design is in [idle-compute.md](idle-compute.md); operations are in
[runbook.md](runbook.md).

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
outbound text expire after three days, removed in bounded batches by worker maintenance.
Message IDs and state remain as deduplication tombstones. Media is fetched for
analysis, not stored as binary data. Expired content is not retryable.


Health checks, the post-deploy `/ready/db` call, worker wakes, and the hourly recovery sweep
are in [idle-compute.md](idle-compute.md). Weekly recaps use their own scheduler, not this outbox.

## The model loop

The model loop is explicit and provider-independent. `model_runtime.py` validates
each batch of tool arguments, executes tools sequentially, and feeds results back
through the selected adapter. `reply` completes the turn; raw model text does not
publish chat messages. Silence remains a successful turn with no reply call.
`AGENT_MAX_TOOL_CALLS` bounds model rounds (default 5); `AGENT_MAX_EXECUTED_TOOLS`
bounds actual tool executions (default 20). Invalid calls, incomplete responses,
or exhausted budgets raise and roll back the enclosing message transaction.

`LLM_PROVIDER=google` is the default. Its adapter disables SDK automatic tool
execution and preserves the full native model content, including thought
signatures, between calls. Images and video are supported through Gemini.

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
This is transport support, not a claim that every compatible deployment
accepts video or meets Beerius's quality requirements. Run an isolated canary
against the actual endpoint before changing production away from Gemini.

Every model call records its token counts for the spend report (`usage.py`; runbook "Spend").
Production-derived evaluation data must not be committed; a future replay suite should use
sanitized fixtures or an access-controlled external store.
