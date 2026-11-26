# The web app (/app)

GroupMe is the production transport and its legacy `users`/`beers`/`group_id` rows stay
authoritative. `/app` is the first-party direction: invite-only accounts, a personal history across
groups, and (feature-gated) native logging and app-only groups. Messaging providers are adapters;
they do not own users or activity.

## Personal dashboard (invite-only)

Only registered production groups contribute to the personal dashboard. Classify
a workspace with `PATCH /admin/workspaces/{workspace_id}/environment` (admin bearer
token) and `{"environment":"test"}` to exclude every GroupMe group mapped to it
from totals, charts, recent activity, and the group picker. Explicit requests for
an excluded group return 404. The setting is stored in `workspaces.settings`,
preserves other settings, and can be reversed with `{"environment":"production"}`.
Unclassified legacy workspaces retain production visibility. This changes only
personal-dashboard visibility: no identities, drink rows, group registrations,
bot replies, or group-specific leaderboards/recaps are modified. It is not process,
database, or credential isolation; use a separate deployment/database for risky tests.

`/app` is the first-party personal view: this week and last week,
all-time drinks, Split-G total, eight-week trend, drink breakdown, and the latest
30 entries. It combines authoritative legacy `beers` with native app activity,
without double counting mirrored entries. It does not read group chats/photos,
send notifications, or change the GroupMe agent. Group filters expose personal
history and explicitly granted app groups; GroupMe participation alone grants no app access.

## Native logging (feature-gated)

Migration 6 adds native activity, explicit app workspace access, durable command
receipts, and a `personal_activity` read view. It does not backfill, rewrite, or
replace legacy drink rows, and does not modify the GroupMe agent/tools/repositories.

- `APP_ACTIVITY_ENABLED=true` exposes Log / Edit / Undo and app-only group creation.
  It defaults to false as an independent kill switch; reads still work when off.
- `PUT /admin/workspaces/{workspace_id}/app-access`, with admin bearer authorization
  and `{"account_id":"<confirmed-account>","role":"member","active":true}`, grants
  self-logging access. Set `active:false` to revoke it. Inferred historical
  memberships are never sufficient. Production group access must be explicitly
  confirmed by an operator; the app cannot claim existing groups by ID or name.
- Admin invitations take exactly one of: `groupme_user_id` (links that GroupMe user's
  history in the same transaction), an existing `person_id`, or a `name` for a new native
  person with no GroupMe history. Normal email proof is still required.
- App-only groups create an internal workspace and an explicit owner grant. They
  do not register a GroupMe bot, create fake users, or send chat messages. Owners
  can manage these groups and create email-specific invitation links in the app.
- In a connected workspace, app writes require exactly one real legacy group and
  one real mapped legacy user. A log inserts both the authoritative legacy row
  and its app record in one transaction. No provider message ID is fabricated.
  Ambiguous/missing mappings fail closed. GroupMe stats and recaps therefore count
  app-created drinks, but logging in the app sends no GroupMe message.
- Edits and undo apply only to the account's own app-created entries. Old GroupMe
  entries remain managed through GroupMe. Opaque revisions reject stale edits,
  including concurrent changes made by GroupMe. GroupMe deletions cascade to the
  app reference; its durable create receipt prevents a retry from resurrecting it.
- Every mutation has an account-scoped UUID request ID and payload fingerprint.
  The receipt commits with the mutation, survives undo, and is retained. The UI
  retains an unconfirmed request in tab session storage for safe retry; the
  database, never browser storage, is authoritative. A tab refresh preserves the
  pending request, and sign-out clears the local draft.
- Writes are limited to 120 commands/account/hour and 20 owned groups/account.
  These controls supplement, not replace, future public-launch abuse protection.

Run `tests/test_activity.py` with a disposable PostgreSQL database before release.
It covers native users, mirrored statistics, GroupMe edit/undo compatibility,
atomic failure rollback, concurrent retries, test-workspace exclusion, revoked
access, mapping drift, and CSRF. Roll back by disabling the feature or redeploying
the prior application; leave additive schema/data intact. Old app code may omit
native-only history until rolled forward, but GroupMe remains on its legacy rows.

## App group invitations and management

Migration 7 adds `app_group_invitations`. Owners can rename
an app-only group, create/revoke invitations, remove members, or transfer ownership
to a current verified member. Members can leave after transferring ownership if
they are the owner. Existing GroupMe groups cannot use these management endpoints,
even if an operator grants an app owner role. Test workspaces remain excluded.

Invitation flow:

1. The owner chooses **Manage app groups**, enters a friend's email, and creates
   a link. The owner shares it manually; creation sends no invitation email.
2. The link contains a random UUID locator in the fragment. It is not an auth
   credential: no membership is granted without proof of the exact invited email.
   Public previews expose only the group and inviter names, never recipient emails.
3. An unregistered recipient can prepare a pending native account through the
   valid invitation, then complete the existing email-code flow. A display name
   is set only after email proof. Existing accounts/person mappings are reused
   without name-based matching, reassignment, or renaming.
4. The recipient explicitly chooses **Join group**. Acceptance rechecks expiry,
   revocation, matching verified email, current inviter ownership/account status,
   and native production workspace eligibility. An accepted invitation cannot
   reactivate a removed member. Re-inviting an inactive former owner grants only
   member access, not their former privileges.

Links expire after seven days. Creating another invitation for the same email
revokes earlier pending links. Ownership transfer revokes pending invitations;
revoking the inviter's access also makes their links unavailable. The UI preserves
the locator through sign-in in tab session storage. Neither a missing invitation
nor an unverified email can create group membership.

Joining shares display names and membership roles with that group's members.
Recipient emails on pending invitations are visible only to owners. Personal drink
histories remain person-scoped: group membership does not expose another person's
stats or records. Leaving/removal revokes group management and logging/edit access,
but does not delete personal history or accounts. Cross-group notifications and
group-shared activity feeds are not part of this increment.

Mutations use durable account-scoped request IDs.
Creation is bounded to 50 invitations/group/day in addition to account command
limits. These are pilot limits, not a substitute for public-launch abuse controls.
Keep `APP_ACTIVITY_ENABLED=false` as the shared app-write kill switch. Rollbacks
leave additive tables intact and do not affect the GroupMe agent.

## Email configuration and operator onboarding

Before enabling real sign-in, configure `WEB_ORIGIN` to the exact HTTPS app origin,
plus `SMTP_HOST`, `SMTP_PORT` (465 for implicit TLS, otherwise mandatory STARTTLS),
`SMTP_FROM`, and the provider's `SMTP_USERNAME` / `SMTP_PASSWORD` as Fly secrets.
No plaintext SMTP or certificate-validation fallback is supported. An empty email
configuration fails closed, without affecting GroupMe readiness. Set
`WEB_ORIGIN=http://127.0.0.1:8089` and `ENVIRONMENT=development` only for local testing.

Account rollout:

1. An administrator independently confirms the existing person and the email they
   want to use. Never match by display name alone or infer account ownership from
   a group membership, incoming message, or unverified email.
2. `POST /admin/accounts/invite` with the existing admin bearer token and JSON
   `{"groupme_user_id":"<their GroupMe user id>","email":"<confirmed-email>"}` creates a
   pending account with a seven-day invitation and links that user's GroupMe history (use
   `person_id` or `name` instead for people without one). A verified binding is never
   overwritten (409). Until the email is verified, the latest admin invitation wins: it
   renews an expired invite, corrects a mistyped email, and takes over an account bound to
   that email through a group invitation. The endpoint itself sends no email. Invite
   GroupMe members this way before a friend's group invitation reaches them: once they
   verify a native account there, their GroupMe history can't be linked to it.
3. The invited person opens `/app`, requests a code, and types the emailed eight
   digits in the **same browser** within ten minutes. Email delivery must be tested
   with the real mailbox before declaring this flow ready. Unknown, expired, and
   delivery-failed requests have the same generic response; a throttled one gets 429. The
   email is sent after the response, so its timing doesn't reveal whether an email is invited.
4. Successful proof activates the account, consumes its outstanding challenges,
   and creates a 30-day HttpOnly, Secure, SameSite=Strict session. The database
   stores only hashes of session/browser tokens and codes. Sign-out revokes the
   session server-side. Disabled accounts and merged/disabled people fail closed.

Limits: five attempts per challenge. Per source IP (`Fly-Client-IP`), ten sign-in
requests per 15 minutes, and per source and email one a minute and three per 15 minutes,
checked in memory before the database (one machine; move them to Postgres before adding a
second). 100 codes per 15 minutes globally, in Postgres. Challenges are removed after a day
and expired sessions removed on login requests. SMTP failure logs omit recipients,
codes, and provider error details. This initial flow deliberately does not include
self-service identity claiming, email changes, account merging, or password recovery.
To revoke an account, `POST /admin/accounts/{account_id}/disable` (admin token): it
disables the account and ends its sessions. There is no API to re-enable one.
Re-invitation/reassignment requires explicit operator review, not a public reset endpoint.

Migration 5 adds only new tables; old releases ignore them. Keep the existing
blue-green deploy strategy and `/ready` checks. An application rollback can leave
these additive tables in place. Run the real PostgreSQL suite (including
`tests/test_web.py`) using `BEERBOT_TEST_DATABASE_URL` before release. Do not test
against the application's production `DATABASE_URL`.
