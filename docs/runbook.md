# Beerbot runbook

How the pieces fit, where every credential lives and how to replace it, and what to do when
something looks wrong. Commands assume `gh` and `fly` are logged in as the owner.

## Map

| Piece | Runs on | What it does |
|---|---|---|
| **Beerius** (GroupMe bot `beerius`, group `beerbot` 112172155) | Fly app `beerbot-groupme` | Receives every message at `/callback?token=…`, logs drinks, answers questions |
| **beerdev scan** | GitHub Actions `beerdev.yml`: instantly on mentions, else every 10 min | Files chat asks as issues and acks them in the chat as the beerdev bot; Monday digest; `steer` (veto/approve) |
| **beerdev build** | GitHub Actions `beerdev-build.yml`, started by scans | Claude Code implements the oldest `beerdev` issue (incidents first), tests, merges, deploys, updates `CHANGELOG.md`, tells the chat |
| **beerdev lead** | GitHub Actions `beerdev-lead.yml`, Mondays | Read-only review of code, incidents, and backlog; proposes up to three improvements |
| **CI** | GitHub Actions `ci.yml`, on push to `main` or dispatch | Tests, then blue-green deploy to Fly, then verifies health, version, and DB |
| **ops** | GitHub Actions `ops.yml`: hourly, on app failures, and on CI failures on `main` | Probes health, redeploys a stuck app, and files incidents with their diagnosis for beerdev to fix; never posts in the chat |
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
| `GEMINI_API_KEY` | Fly (Beerius); GitHub (scan, live e2e) | Model calls | Google AI Studio. Prefer a separate low-quota key for GitHub, because agent-written tests can read it |
| `ADMIN_TOKEN` | Fly; GitHub (ops); local `.env.local` | `/admin/*` endpoints | `openssl rand -hex 32`, then set the same value in all three places |
| `GITHUB_DISPATCH_TOKEN` (optional) | Fly | Instant beerdev scans on mentions, and instant ops checks on failures | See "Enable instant triggers". Without it, schedules catch up (scan every 10 min, ops hourly) |
| `BEERBOT_BOT_ID` | Fly | Legacy default bot. Per-group bot IDs live in the `groups` table (`POST /admin/groups`) | GroupMe bot list |
| `SMTP_*` | Fly | Email sign-in for `/app` | Mail provider |
| `FLY_API_TOKEN` | GitHub | CI deploys | Deploy token `github-actions-beerbot`, expires 2027-08-30: `fly tokens create deploy -a beerbot-groupme -n github-actions-beerbot -x 8760h \| gh secret set FLY_API_TOKEN` |
| `GROUPME_ACCESS_TOKEN` | GitHub (scan only); local `.env.local` | beerdev's scan reads the chat; nothing acts as the owner | The owner's personal token: sign in at https://dev.groupme.com/account/access-token. It grants full account access, so it is only given to jobs that never run agent-written code |
| `CLAUDE_CODE_OAUTH_TOKEN` | GitHub | beerdev's coding agent | `claude setup-token` (valid one year), then `gh secret set CLAUDE_CODE_OAUTH_TOKEN` |
| `BEERDEV_BOT_ID` | GitHub | beerdev posts announcements | `bot_id` of the `beerdev` bot from `GET https://api.groupme.com/v3/bots` |
| `BEERDEV_GROUP_ID` (variable) | GitHub | Which group beerdev watches | `112172155`. Deleting it pauses scans |
| `OWNER_USER_ID` | Fly (secret); GitHub (variable) | The owner's GroupMe id: Beerius follows their commands; beerdev takes their vetoes and approvals | `13419729`; not a secret, kept out of the public code |

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

### Enable instant triggers

GitHub creates fine-grained tokens only in its web UI. This link prefills everything except the
repository:
https://github.com/settings/personal-access-tokens/new?name=beerbot-dispatch&description=Fly+app+starts+beerdev+and+ops+workflows&target_name=alexeldeib&expires_in=366&actions=write

Set **Repository access → Only select repositories → beerbot**, generate the token, and then:
```sh
printf 'GITHUB_DISPATCH_TOKEN=%s\n' "$TOKEN" | fly secrets import --stage -a beerbot-groupme
gh workflow run ci.yml --ref main
```
The token can only start and inspect workflows in this repository. Without it, nothing breaks.
It expires after a year; a lapsed token only slows acks back down to the schedules.

### Fly tokens

CI deploys with an app-scoped deploy token, and nothing in the automation reads Fly directly:
failures carry their own stack frames in the database. For ad-hoc debugging from another
machine, mint a read-only token instead of reusing the deploy one:
`fly tokens create readonly -o personal -n debugging -x 720h`. List or revoke tokens with
`fly tokens list -a beerbot-groupme` and `fly tokens revoke <id>`.

## Access map

Who can read and write what, as narrowly as each system allows. Agent-written code runs only in
beerdev's `implement` job.

| Actor | Reads | Writes | Credentials |
|---|---|---|---|
| Fly app | Webhooks, database, model | Database, Beerius's posts, workflow starts | `DATABASE_URL`, `GEMINI_API_KEY`, `GROUPME_WEBHOOK_SECRET`, `ADMIN_TOKEN`, `SMTP_*`, `GITHUB_DISPATCH_TOKEN` (Actions only, this repo, until 2027-11-24) |
| CI `test` | Repository | Nothing | `GITHUB_TOKEN` (contents: read) |
| CI `deploy` | Repository | The Fly app | `FLY_API_TOKEN` (deploy token scoped to the app) |
| beerdev scan | Chat, issues | Issues, beerdev's chat posts, build starts | `GROUPME_ACCESS_TOKEN` (read use only), `BEERDEV_BOT_ID`, `GEMINI_API_KEY`, `GITHUB_TOKEN` (issues, actions) |
| beerdev `implement` (agent code) | Repository, its issue | A patch artifact only | `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY` (live e2e); read-only `GITHUB_TOKEN`, used before the agent starts |
| beerdev `ship` | GitHub's copy of the issue, the patch | Branch, PR, merge, CI start, beerdev's chat post | `GITHUB_TOKEN` (contents, PRs, issues, actions), `BEERDEV_BOT_ID` |
| beerdev lead `plan` (agent) | Repository, issues, CI runs | A plan file only | `CLAUDE_CODE_OAUTH_TOKEN`, read-only `GITHUB_TOKEN` |
| beerdev lead `file` | The plan (schema-checked, at most three items) | Issues, build starts | `GITHUB_TOKEN` (issues, actions) |
| ops | Health and admin status, CI runs and logs, issues | Issues, CI redeploys | `ADMIN_TOKEN`, `GITHUB_TOKEN` (issues, actions) |
| Owner's machine | Everything | Everything | `gh` (repo scope), `fly` (org), `.env` (production database), `.env.local` |

Known gaps, broadest first:
- **`GROUPME_ACCESS_TOKEN` is the owner's whole GroupMe account,** although beerdev now uses it
  only to read the chat: it posts as its own bot, and nothing acts as the owner. GroupMe bots
  can't read history, so the planned fix is to move intake into the app, which already receives
  every message. GitHub would then get only paraphrased requests, and the token would leave
  GitHub entirely.
- **`ADMIN_TOKEN` opens every admin endpoint,** but ops only reads `/admin/*/status`. A separate
  read-only ops token would remove its ability to invite, register, or retry.
- **`DATABASE_URL` is the database owner,** because migrations run at startup. A runtime role
  without DDL would need a separate migration step.
- **Agent-written tests can read the Claude and Gemini keys in `implement`.** Use dedicated,
  low-quota keys for CI.

## Deploys and rollbacks

- Merging to `main` deploys automatically. To redeploy without a change, run `gh workflow run ci.yml --ref main`.
- CI deploys blue-green, then verifies `/health`, `/ready`, the git SHA in `/version`, and
  `/ready/db`. That last call recovers messages stranded during the handoff.
- Run `fly deploy` by hand only if CI itself is broken, then call `/ready/db`.
- To roll back, run `gh pr revert <number>` and merge the revert PR.

## Watching it

- **beerdev:** issues labeled `beerdev`, `beerdev-infra`, and `beerdev-failed`; the Actions tabs for
  **beerdev** and **beerdev build**. Each scan logs one line, for example `Scanned 12 messages … filed: bug`.
- **Message queue:**
  `curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://beerbot-groupme.fly.dev/admin/messages/status | jq`
  shows failed executions (messages Beerius never answered) and deliveries needing attention.
  `/admin/workers/status` is memory-only and safe to poll.
- **Logs:** `fly logs -a beerbot-groupme`.
- **ops:** issues labeled `ops`. Incidents carry their own diagnosis: the workers' last errors
  for an unhealthy app, or the failing step's log for a failed deploy. They are labeled
  `beerdev` + `bug`, so beerdev builds a fix **before** any feature. If the cause isn't in the code
  (database, network, provider, credentials), beerdev writes what a person must do and moves the
  issue to the inbox. "Production is unhealthy" also redeploys once (at most every 30 minutes)
  and closes itself on recovery. A failure class that recurs within 7 days of closing is appended
  to the closed issue instead of starting another build. Ops reads the database only after a
  failure or once a day at 12:17 UTC, so Neon can sleep.
- **Inbox:** https://github.com/alexeldeib/beerbot/issues?q=is%3Aopen+label%3Ainbox lists everything
  waiting on the owner: infra asks with their human setup, the weekly lead's `proposal`s,
  incidents beerdev couldn't fix in code, and stuck replies. Everything else runs without you.
- **Steering:** in the chat, the owner (GroupMe id in the `OWNER_USER_ID` variable) replies "veto"
  or "approve" to one of beerdev's messages, or names the request ("beerdev veto the gym
  tracker"). Anyone else's commands are ignored. The same from a terminal:
  `gh workflow run beerdev.yml -f action=approve -f issue=<n>` makes a proposal or waiting issue
  buildable. `action=veto` closes queued work, stops a build at merge, or removes
  shipped work through a new build, and posts 🚫 in the chat.
- **History:** `CHANGELOG.md` gets one line per shipped issue. Mondays at 9am Eastern, beerdev posts
  a digest (shipped, declined, waiting on a human), and skips quiet weeks.

## Troubleshooting

**Beerius didn't answer.** Ops files these on its own. By hand, check `failed_executions` in the
queue status: `error_detail` has the exception type and stack frames. Payloads are kept for 3
days, so to see the message itself:
```sh
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -c "BEGIN READ ONLY;" -c \
  "SELECT id, created_at, attempts, error_code, left(payload->>'text', 80) FROM message_inbox
   WHERE state='failed' ORDER BY id DESC LIMIT 10;" -c "ROLLBACK;"
```
A completed message with no reply means the model chose silence, which is the normal case for banter.

**A reply is stuck as `uncertain`.** First check in the chat that it never arrived. Then
`curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" "https://beerbot-groupme.fly.dev/admin/messages/outbox/<id>/retry?acknowledge_uncertain=true"`.

**beerdev ignored an ask.** Read that scan's log line. Asks that mention "beerdev" are the most
reliable. To rescan a window, run `gh workflow run beerdev.yml -f since_hours=6`; already-filed asks
are skipped.

**A beerdev build failed.** The issue has `beerdev-failed` and a link to the run. Remove the label to
retry.

**Scans fail with 401.** The GroupMe token changed. Get the new one from
https://dev.groupme.com/account/access-token, then run `gh secret set GROUPME_ACCESS_TOKEN` and update `.env.local`.
