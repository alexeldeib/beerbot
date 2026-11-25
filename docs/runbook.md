# Beerbot runbook

How the pieces fit, where every credential lives and how to replace it, and what to do when
something looks wrong. Commands assume `gh` and `fly` are logged in as the owner.

## Map

| Piece | Runs on | What it does |
|---|---|---|
| **Beerius** (GroupMe bot `beerius`, group `beerbot` 112172155) | Fly app `beerbot-groupme` | Receives every message at `/callback?token=…`, logs drinks, answers questions |
| **beerdev scan** | GitHub Actions `beerdev.yml`, every 10 min | Files chat asks as issues, reacts 💡 on the ask |
| **beerdev build** | GitHub Actions `beerdev-build.yml`, started by scans | Claude Code implements the oldest `beerdev` issue, tests, merges, deploys, reacts ✅ |
| **CI** | GitHub Actions `ci.yml`, on push to `main` or dispatch | Tests, then blue-green deploy to Fly, then verifies health, version, and DB |
| **ops** | GitHub Actions `ops.yml`, hourly and on failures | Probes health, redeploys a stuck app, files one issue per failure class; never posts in the chat |
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
| `GROUPME_ACCESS_TOKEN` | GitHub; local `.env.local` | beerdev reads chat and reacts | The owner's personal token: sign in at https://dev.groupme.com/account/access-token. It grants full account access, so it is only given to jobs that never run agent-written code |
| `CLAUDE_CODE_OAUTH_TOKEN` | GitHub | beerdev's coding agent | `claude setup-token` (valid one year), then `gh secret set CLAUDE_CODE_OAUTH_TOKEN` |
| `BEERDEV_BOT_ID` | GitHub | beerdev posts announcements | `bot_id` of the `beerdev` bot from `GET https://api.groupme.com/v3/bots` |
| `BEERDEV_GROUP_ID` (variable) | GitHub | Which group beerdev watches | `112172155`. Deleting it pauses scans |

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
- **ops:** issues labeled `ops`. "Production is unhealthy" opens with the action taken (a CI
  redeploy, at most once per 30 minutes) and closes itself on recovery. "Messages failing with
  `<Error>`" lists each failed message with its exception type and stack frames; code-level classes
  are also labeled `beerdev`, so beerdev fixes them. If a class recurs within 7 days of closing,
  ops appends to the closed issue instead of starting another build. Ops reads the database only
  after a failure, or once a day at 12:17 UTC, so Neon can sleep.

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
