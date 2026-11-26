# Beerbot runbook

How the pieces fit, where every credential lives and how to replace it, and what to do when
something looks wrong. Commands assume `gh` and `fly` are logged in as the owner.

## Map

| Piece | Runs on | What it does |
|---|---|---|
| **Beerius** (GroupMe bot `beerius`, group `beerbot` 112172155) | Fly app `beerbot-groupme` | Receives every message at `/callback?token=…`, logs drinks, answers questions |
| **beerdev** (bot `beerdev`) | The same Fly app and process as Beerius | A peer bot. The router gives it messages that start with "beerdev" and replies to its posts; Beerius never sees those. It answers from live status, the request queue, deploys, and the docs, files requests, and steers them (owner only). It replies as the beerdev bot |
| **beerdev digest** | GitHub Actions `beerdev.yml`, Mondays | Posts the week's history line: shipped, declined, waiting on a human |
| **beerdev build** | GitHub Actions `beerdev-build.yml`, started by chat asks, the weekly lead, ops tickets, and the previous build | Claude Code implements the next `beerdev` issue (incidents first) and tests it; an independent reviewer approves anything beyond routine app code; then it merges, deploys, updates `CHANGELOG.md`, and tells the chat |
| **beerdev lead** | GitHub Actions `beerdev-lead.yml`, Mondays | Read-only review of code, incidents, and backlog; proposes up to three improvements |
| **CI** | GitHub Actions `ci.yml`, on push to `main` or dispatch | Tests, then blue-green deploy to Fly, then verifies health, version, and DB |
| **ops** (the SRE) | GitHub Actions `ops.yml`: every 5 minutes, after CI on `main`, and when the app reports a failure | Watches symptoms from outside Fly, applies one safe fix (rolling back a fresh deploy), pages the owner in the private "beerbot alerts" GroupMe group, and opens tickets for beerdev (the SWE). See "Incidents". Never posts in the main chat |
| **Database** | Neon PostgreSQL | All durable state; sleeps when idle (see `idle-compute.md`) |

The `beerius` bot in the Beerbeta test group points at `/callback-fake` on purpose, which
keeps that group disconnected.

## Credentials

Secrets are never printed. Store a new value from a file or a pipe, for example
`gh secret set NAME < file` or `printf 'NAME=%s\n' "$value" | fly secrets import -a beerbot-groupme`.
After changing Fly secrets, deploy through CI (`gh workflow run ci.yml --ref main`) so the change
goes out blue-green.

| Name | Lives in | Used for | Mint or rotate |
|---|---|---|---|
| `GROUPME_WEBHOOK_SECRET` | Fly, and Beerius's callback URL `?token=` | Rejecting forged callbacks (forged → 401) | See "Rotate the webhook secret" |
| `DATABASE_URL` | Fly; also the local `.env` | Neon production database | Neon console. The local `.env` points at **production**: run read-only SQL only (`BEGIN READ ONLY`) |
| `GEMINI_API_KEY` | Fly (Beerius and beerdev); GitHub (live e2e) | Model calls | Google AI Studio. Prefer a separate low-quota key for GitHub, because agent-written tests can read it |
| `ADMIN_TOKEN` | Fly; local `.env.local`. **Never GitHub** | Every `/admin/*` endpoint | `openssl rand -hex 32`, then set the same value in both places |
| `OPS_TOKEN` | Fly; GitHub (ops) | Read-only: `GET /admin/workers/status`, `/admin/messages/status`, and `/admin/usage`. Nothing else | `openssl rand -hex 32`, then `gh secret set OPS_TOKEN` and `fly secrets import --stage` with the same value |
| `BEERDEV_GITHUB_TOKEN` | Fly | beerdev files and steers requests and reads deploys. The app starts builds and ops checks, and re-enables ops if it goes silent | Fine-grained, this repository only, Issues and Actions read and write. It can't push code or change workflows. Prefilled: https://github.com/settings/personal-access-tokens/new?name=beerdev-app&description=Fly+app%3A+beerdev+files+and+steers+requests%2C+starts+builds+and+ops&target_name=alexeldeib&expires_in=366&issues=write&actions=write. Without it, beerdev says it can't reach GitHub |
| `BEERBOT_BOT_ID` | Fly | Legacy default bot. Per-group bot IDs live in the `groups` table (`POST /admin/groups`) | GroupMe bot list |
| `SMTP_*` | Fly | Email sign-in for `/app` | Mail provider |
| `OPS_ALERT_BOT_ID` | GitHub (ops) | Pages: the `ops` bot can only post to the private "beerbot alerts" group | `bot_id` of the `ops` bot from `GET https://api.groupme.com/v3/bots` |
| `FLY_API_TOKEN` | GitHub, `production` environment only (deploys from `main` only) | CI deploys and `rollback.yml` | Deploy token `github-actions-beerbot-production`, expires 2027-11-25: `fly tokens create deploy -a beerbot-groupme -n github-actions-beerbot-production -x 8760h \| gh secret set FLY_API_TOKEN --env production` |
| `GROUPME_ACCESS_TOKEN` | Local `.env.local` only | Manual lookups (listing bots, reading history). No automation holds it | The owner's personal token, with full account access: https://dev.groupme.com/account/access-token |
| `BEERDEV_SHIP_TOKEN` | GitHub, `beerdev` environment only | The ship job pushes and merges beerdev's changes, including workflow files; its merges start CI. Acts as the owner on GitHub | Fine-grained, this repository only: Contents, Pull requests, and Workflows read and write. Prefilled: https://github.com/settings/personal-access-tokens/new?name=beerdev-ship&description=beerdev+ship+job+pushes+and+merges+changes+including+workflows&target_name=alexeldeib&expires_in=366&contents=write&pull_requests=write&workflows=write. Then `gh secret set BEERDEV_SHIP_TOKEN --env beerdev`. Without it, nothing ships |
| `CLAUDE_CODE_OAUTH_TOKEN` | GitHub | beerdev's coding agent | Set 2026-11-07, expires 2027-11-07. `claude setup-token` (valid one year), then `gh secret set CLAUDE_CODE_OAUTH_TOKEN` |
| `BEERDEV_BOT_ID` | Fly (beerdev's replies); GitHub (digest, ship announcements) | beerdev posts as its own bot | `bot_id` of the `beerdev` bot from `GET https://api.groupme.com/v3/bots` |
| `OWNER_USER_ID` | Fly (secret) | The owner's GroupMe id: Beerius follows their commands, and beerdev accepts steering only from it | `13419729`; not a secret, kept out of the public code |

### Rotate the webhook secret

GroupMe bots can only send a secret in the callback URL, and the app accepts one secret, so a
rotation drops messages for a few seconds. Do it when the chat is quiet.

1. Generate it with `openssl rand -hex 32 > secret` (keep the file private).
2. Open https://dev.groupme.com/bots, edit `beerius` (group beerbot), and set the callback to
   `https://beerbot-groupme.fly.dev/callback?token=<secret>`. Save it, then confirm with `GET /v3/bots`.
3. `printf 'GROUPME_WEBHOOK_SECRET=%s\n' "$(cat secret)" | fly secrets import --stage -a beerbot-groupme`,
   then `gh workflow run ci.yml --ref main`.
4. Check that `curl -X POST https://beerbot-groupme.fly.dev/callback -d '{}'` returns `401`, and
   that the next chat message shows `POST /callback?token=*** … 200` in `fly logs`. Access logs
   redact `token=`.

## Access map

Who can read and write what, as narrowly as each system allows. Agent-written code runs only in
beerdev's `implement` job.

| Actor | Reads | Writes | Credentials |
|---|---|---|---|
| Fly app (Beerius and beerdev) | Webhooks, database, model, this repo's issues and runs | Database, both bots' posts, beerdev's issues and labels, workflow starts | `DATABASE_URL`, `GEMINI_API_KEY`, `GROUPME_WEBHOOK_SECRET`, `ADMIN_TOKEN`, `OPS_TOKEN` (to check it), `SMTP_*`, `BEERDEV_BOT_ID`, `BEERDEV_GITHUB_TOKEN` (Issues and Actions, this repo). Chat text reaches only each agent's own tools |
| CI `test` | Repository | Nothing | `GITHUB_TOKEN` (contents: read) |
| CI `deploy` | Repository | The Fly app | `FLY_API_TOKEN` (deploy token scoped to the app) |
| beerdev digest | Issues | beerdev's Monday post | `BEERDEV_BOT_ID`, read-only `GITHUB_TOKEN` |
| beerdev `implement` (agent code) | Repository, its issue, PyPI (`uv add`) | A patch artifact only (refused if it holds anything credential-like) | `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY` (live e2e); read-only `GITHUB_TOKEN`, used before the agent starts |
| beerdev `review` (agent) | The patch as data, GitHub's copy of the issue, the unchanged repository | A verdict only | `CLAUDE_CODE_OAUTH_TOKEN`, read-only `GITHUB_TOKEN`; runs nothing the implement agent wrote |
| beerdev `ship` | GitHub's copy of the issue, the patch, the verdict | Branch, PR, merge (or a held PR), CI start, beerdev's chat post | `BEERDEV_SHIP_TOKEN` (pushes, PRs, merges), `GITHUB_TOKEN` (issues write; actions and contents read), `BEERDEV_BOT_ID` |
| beerdev lead `plan` (agent) | Repository, issues, CI runs | A plan file only | `CLAUDE_CODE_OAUTH_TOKEN`, read-only `GITHUB_TOKEN` |
| beerdev lead `file` | The plan (schema-checked, at most three items) | Issues, build starts | `GITHUB_TOKEN` (issues, actions) |
| ops | Health, status, and spend; CI runs and logs; issues | Incidents and tickets, rollback and build starts, pages to the alerts group | `OPS_TOKEN`, `OPS_ALERT_BOT_ID`, `GITHUB_TOKEN` (issues, actions) |
| `rollback.yml` | Fly releases | Production: the newest earlier release with a different image (or a named one), with its commit's `fly.toml`, verified | `FLY_API_TOKEN` (`production` environment, main only) |
| Owner's machine | Everything | Everything | `gh` (repo scope), `fly` (org), `.env` (production database), `.env.local` |

### beerdev's kernel and gates

beerdev may change anything, including workflows, scripts, Docker, Fly, and dependencies, except
its **kernel**: `.github/workflows/beerdev-build.yml`, `.github/workflows/beerdev-lead.yml`,
`.github/beerdev-*.md`, `.claude/`, `CLAUDE.md` and `CLAUDE.local.md` in any directory (every Claude
agent loads them as instructions), and `tests/test_policy.py`. Ship refuses a patch that touches
them, including by rename. Three gates guard everything else:
1. `tests/test_policy.py` checks that secrets stay in their jobs and only reach known hosts. It
   checks that write permissions and triggers are no broader than today, and that only `main` runs
   code holding credentials: branch pushes trigger nothing, and those jobs carry the exact non-PR
   guard. It also checks that jobs holding credentials never restore a cache that agent code could
   have saved, that agents hold no write access, and that deploys still verify and roll back. Two
   app checks confirm that only the owner can steer beerdev and that webhooks and admin endpoints
   require their secrets. The suite runs it, and the review job reruns the kernel's own copy
   against the patched workflows before anything is pushed: pinned packages, outside the tree, so
   no agent-written code runs there. To change an invariant on purpose, edit the test by hand in
   the same PR.
2. The independent reviewer must approve any change beyond `src/`, `docs/`, `README.md`, and
   `tests/test_*.py`, and any change to the app files that hold credentials or decide who may do
   what: `src/beerbot/{beerdev,main,dispatch,config,database,web}.py` (`ROUTINE` in
   `beerdev-build.yml`; the gate fails closed). If it doesn't approve, the change waits as an open
   PR, and its issue moves to the inbox. Merge or close that PR yourself, or reply "approve" in the
   chat: the next build brings it up to date with `main`, waits for its CI, and merges it. Dependency changes (`pyproject.toml`, `uv.lock`) always
   wait this way: a reviewer can't vet a package's provenance offline. For a human on every
   ship, add a required reviewer to the `beerdev` environment.
3. CI rolls production back if a deploy fails verification.

GitHub backs this up: the `production` (Fly token) and `beerdev` (ship token) environments accept
only `main`, so a workflow on a beerdev branch can't read either.

Known gaps, broadest first:
- **Code can defeat the tests it runs under.** The policy test catches mistakes; the reviewer
  judges intent. Neither proves a deliberately hostile change safe.
- **`BEERDEV_SHIP_TOKEN` acts as the owner on GitHub:** beerdev's PRs and merges show the owner. A
  GitHub App would give beerdev its own identity.
- **One app, one secret scope.** Fly secrets are app-wide, so Beerius's process also holds
  beerdev's GitHub token. Each agent reaches only its own narrow tools, and the token is limited to
  this repo's issues and actions. A second Fly app would isolate it.
- **`DATABASE_URL` is the database owner,** because migrations run at startup. A runtime role
  without DDL would need a separate migration step.
- **Agent-written tests can read the Claude and Gemini keys in `implement`.** Use dedicated,
  low-quota keys for CI.

## Deploys and rollbacks

- Merging to `main` deploys automatically. To redeploy without a change, run `gh workflow run ci.yml --ref main`.
- CI deploys blue-green, then verifies `/health`, `/ready`, the git SHA in `/version`, and
  `/ready/db`. That last call recovers messages stranded during the handoff.
- **Automatic rollback:** before deploying, CI records the running image and git SHA. If
  verification fails after the deploy, it redeploys that image with that commit's `fly.toml`
  (blue-green), checks `/ready` and `/ready/db`, and still fails the run, so ops files "Deploy failed"
  (noting the rollback) and beerdev fixes forward. Migrations are forward-only: code must keep
  working on the previous schema (add columns, never `SELECT *`).
- Run `fly deploy` by hand only if CI itself is broken, then call `/ready/db`.
- To roll back by hand, run `gh workflow run rollback.yml --ref main`. It redeploys the newest
  earlier release with a different image, with that commit's `fly.toml` (CI labels each image with
  its SHA), blue-green, and verifies. If production already runs an image it ran before (CI or a
  rollback already went back), the release before it is the broken one, so it refuses: name the
  release with `-f version=<n>` (from `flyctl releases --app beerbot-groupme --image`). Add
  `-f dry_run=true` to only print the target. To revert the code,
  run `gh pr revert <number>` and merge it.

## Incidents

ops is the SRE and beerdev is the SWE. ops follows standard practice:
- alert on what users feel
- page only when it's urgent and actionable
- automate only fixes that are safe and usually right
- hand the root cause to engineering

**Signals,** every 5 minutes from GitHub Actions. All come from public endpoints and memory-only
status, so checks never wake the database:
- `/health` and `/ready` must answer. Each probe retries 3 times over a minute, so blips don't
  page.
- No message may wait over 5 minutes to be answered (`oldest_unprocessed_seconds`).
- No reply may wait over 5 minutes to be posted (`oldest_undelivered_seconds`).

**Response,** a fixed decision table with no model, decided once as the incident opens:

| Situation | ops does | You get |
|---|---|---|
| Worker errors name the database or the model provider (checked first) | Nothing; there's nothing to undo or restart | 🚨 page: "waiting on …" |
| It broke within 60 minutes of a clean deploy | Runs `rollback.yml`, at most once per deploy | 🚨 page, then ✅ when resolved |
| The rollback failed | Nothing more is automatic | 🚨 page |
| Anything else | Nothing | 🚨 page: "needs you" |
| A deploy failed and CI rolled it back | Ticket for beerdev (fix forward) | No page |
| Messages failed with code errors | Ticket per failure class for beerdev | No page |
| Over budget, or a credential expiring within 30 days | Inbox ticket | No page |

**Lifecycle:**
- Each incident is one issue labeled `incident` and assigned to you. That's the record and the
  email backup.
- Pages go to the private **beerbot alerts** GroupMe group (only you and the `ops` bot): when the
  incident opens, when a rollback fails, and when it resolves.
- When the signals pass again, ops closes the issue. If code was involved (a rollback, or errors
  in this repo's frames), it opens a **Root cause** ticket for beerdev, which reproduces the
  problem with a failing test and fixes it.
- ops never restarts the app; Fly restarts crashed processes by itself.
- While a deploy or rollback is running, ops waits, unless it has been running for over 30
  minutes (then it's stuck, and ops acts as usual).
- To stop pages for an incident you're handling, add the `ack` label; ops still resolves it.
  Closing it while the signals still fail just opens a new one.
- Each new ticket ops files starts a beerdev build, and each build starts the next, so the queue
  drains. After two deploy-failure tickets in a day, the next one goes to the inbox instead, so a
  fix that keeps failing to deploy can't loop.
- Once a day (12:23 UTC, its own run) ops sweeps what needs the database: failure classes, spend,
  and expiring credentials. Every 5-minute check also files a CI failure that was missed.
- If ops stops checking for an hour (GitHub disables schedules after 60 days without commits),
  the app turns `ops.yml` back on and starts it. A workflow you disabled by hand stays off.

## Watching it

- **beerdev:** issues labeled `beerdev`, `beerdev-infra`, and `beerdev-failed`; the Actions tabs for
  **beerdev build**. beerdev's chat turns are inbox rows like Beerius's (`agent = 'beerdev'`),
  with their tool traces.
- **Message queue:**
  `curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://beerbot-groupme.fly.dev/admin/messages/status | jq`
  shows failed executions (messages Beerius never answered) and deliveries needing attention.
  `/admin/workers/status` is memory-only and safe to poll.
- **Logs:** `fly logs -a beerbot-groupme`.
- **ops:** issues labeled `ops`. Outages are `incident` issues (see "Incidents"). Tickets carry
  their own diagnosis (the failure class with its code location, or the failing step's log for a
  failed deploy), are labeled `beerdev` + `bug`, and start a build, so beerdev fixes them
  **before** any feature. If the cause isn't in the code (database, network, provider,
  credentials), beerdev writes what a person must do and moves the issue to the inbox. A failure
  class that recurs within 7 days of closing reopens its issue in the inbox: the fix didn't hold.
  Ops reads the database only when the app reports a failure, or once a day, so Neon can sleep.
- **Inbox:** https://github.com/alexeldeib/beerbot/issues?q=is%3Aopen+label%3Ainbox lists everything
  waiting on the owner: infra asks with their human setup, the weekly lead's `proposal`s,
  incidents beerdev couldn't fix in code, changes the reviewer held (each links its open PR), and
  stuck replies. Everything else runs without you.
- **Asking:** anyone can ask in the chat ("beerdev what's queued?", "beerdev how does the zyn
  tracker count?", "beerdev any errors lately?"). A message that starts with "beerdev", or replies
  to one of beerdev's posts, goes to beerdev, which answers in seconds, threaded. It uses tools for
  allowlisted docs and behavior code, the issue queue, deploys, and live status:
  - recent failures, with exception type and code location but never message text
  - workers' last errors
  - deploys and ops runs, and ops incidents
  - spend

  It can't read raw logs, which can contain chat text; use `fly logs -a beerbot-groupme` for those.
- **Steering:** in the chat, the owner (GroupMe id in Fly's `OWNER_USER_ID` secret) replies "veto"
  or "approve" to one of beerdev's messages, or names the request ("beerdev veto the gym
  tracker"). beerdev refuses anyone else's in code, by GroupMe id, and acts only on the owner's
  own words. A veto closes queued work, stops a build at merge, or removes shipped work through a
  new build. From a terminal, veto with `gh issue close <n> --reason "not planned"`, or approve
  with `gh issue edit <n> --add-label beerdev --remove-label inbox` and then
  `gh workflow run beerdev-build.yml`.
- **History:** `CHANGELOG.md` gets one line per shipped issue. Mondays at 9am Eastern, beerdev posts
  a digest (shipped, declined, waiting on a human), and skips quiet weeks.

## Spend

At about 350 Beerius messages a month (late 2026), list prices come to roughly:

| Item | Monthly | Notes |
|---|---|---|
| Gemini, Beerius | about $3 (about $6 from 2027-01-01) | About 9K input and 600 output tokens per message. The system prompt and tool definitions are about 4.2K tokens per model round, and most messages take two rounds. Thinking is billed as output |
| Gemini, beerdev answers | about $0.03 per question | 34K-token context: docs plus the behavior files |
| Gemini, build e2e tests | about $0.10 to $0.30 per build | Shown at the end of each live test run |
| Fly | $3.69, plus about $0.15 | shared-cpu-1x, 256MB plus 256MB swap, always on in `iad`, plus a stopped standby's disk. IPs are free |
| Neon | $0 expected | Free plan. The idle design keeps compute near 15 CU-hours a month |
| Claude Code | Subscription | implement, review, and lead. Each run's tokens are in its Actions summary. A run starts near 38K input tokens |
| GitHub Actions | $0 | Public repository |

- **Tracking:** `src/beerbot/usage.py` counts every model response, including thinking tokens. It
  writes the counts with each message and on the hourly sweep, so tracking adds no database wakes.
  To read spend:
  `curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://beerbot-groupme.fly.dev/admin/usage | jq`
  shows month to date by source, the projection, and the budget. `/admin/workers/status` shows
  tokens since boot without touching the database. Ask "beerdev, what are we spending?" in the
  chat and beerdev answers from the same report.
- **Budget:** set `MODEL_BUDGET_USD` (Fly secret, default `5`). Ops checks once a day. It files one
  `inbox` issue a month when spend passes the budget, or when the projection does after the 7th.
- **Prices:** `PRICES` in `src/beerbot/usage.py` holds list prices; update it when Google changes
  them. `gemini-3.6-flash` doubles on 2027-01-01. For the actual bill, open AI Studio → Spend for
  the project that owns the app's key (`play`).
- **Cheaper:** the model is the biggest lever. `gemini-3.1-flash-lite` costs $0.25 per million
  input tokens and $1.50 per million output, against $0.75 and $3.75. Check a model with the live
  suite before switching:
  `BEERBOT_LIVE_LLM=1 LLM_MODEL=<model> uv run pytest tests/test_live_chat.py`.
  Then run `fly secrets set LLM_MODEL=<model>`.

## Troubleshooting

**Beerius didn't answer.** Ops files these on its own. By hand, check `failed_executions` in the
queue status: `error_detail` has the module-qualified exception type, its HTTP status if any, and
this repo's stack frames. Payloads are kept for 3
days, so to see the message itself:
```sh
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -c "BEGIN READ ONLY;" -c \
  "SELECT id, created_at, attempts, error_code, left(payload->>'text', 80) FROM message_inbox
   WHERE state='failed' ORDER BY id DESC LIMIT 10;" -c "ROLLBACK;"
```
A completed message with no reply means the model chose silence, which is the normal case for banter.

**A reply is stuck as `uncertain`.** First check in the chat that it never arrived. Then
`curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" "https://beerbot-groupme.fly.dev/admin/messages/outbox/<id>/retry?acknowledge_uncertain=true"`.

**beerdev ignored a message.** beerdev only answers messages that start with "beerdev" or reply
to one of its posts. Anything else goes to Beerius, which points capability asks to beerdev.

**A beerdev build failed.** The issue has `beerdev-failed` and a link to the run. Reply "approve"
to beerdev's message about it, or remove the label and run `gh workflow run beerdev-build.yml`.

**ops fails with 401 or 403.** `OPS_TOKEN` differs between Fly and GitHub: set the same value
in both.

**beerdev says it can't reach GitHub.** `BEERDEV_GITHUB_TOKEN` is missing or expired. Mint a new
one (see Credentials) and set it on Fly.
