# From production-hosted pilot to group rollout

## Current boundary

The app is deployed on Fly with CI-gated blue-green releases, readiness checks,
durable message execution/outbox, email proof, and read-only personal history.
It is still an operator-invited pilot, not an open-signup product. Keep GroupMe
logging and existing behavior authoritative during the rollout.

Beerbeta is a test workspace, not a second identity for the same human. Exclude
test workspaces from all personal app reads without rewriting historical users or
drink records. Workspace classification is reversible, but is not infrastructure
isolation. Later gateways map to the same workspace and inherit its classification.

## Before inviting the existing group

1. **Stable address:** use `beerbot.alexeldeib.xyz`, with a Fly certificate and a
   Cloudflare DNS-only record as the initial setup. Verify TLS before advertising
   the address. Update the explicit web-origin allowlist deliberately, preserve
   the working Fly URL during migration, and redirect only browser app traffic.
   Do not redirect or change the GroupMe callback. Host-only sessions require a
   new sign-in on the new hostname. Add a useful root route to `/app`.
2. **Account lifecycle:** native group owners can now invite/reinvite/revoke with
   an audit trail and matching-email proof. Keep linking existing GroupMe history
   operator-approved, with a documented correction path for mistaken person/email
   mappings. Never permit claiming by name, an unverified email, or inferred
   workspace membership. Review with a few volunteer users first.
3. **Webhook authenticity:** production has historically lacked the optional
   callback secret. Verify current configuration; stage a coordinated transition
   of callback URL and secret using an overlap/dual-acceptance period so valid
   callbacks never fail during a blue-green release. A user ID in an unsigned
   request is not proof of GroupMe ownership.
4. **Operational readiness:** confirm actual database backup/PITR retention and
   exercise a restore into an isolated environment. Add actionable alerts for
   readiness failures, queue failures/uncertain deliveries, and email failures.
   Existing health endpoints and logs alone are not an alerting service.
5. **Email reliability:** use a dedicated sending-only credential and an explicit
   sender domain; verify SPF/DKIM/DMARC and several recipients/providers. One Gmail
   inbox marking a message not spam does not establish general deliverability.
   Keep authentication emails free of private group activity.

## Before open signup or a broader public launch

- Add source-aware and global abuse controls before SMTP work, with correctly
  trusted proxy boundaries; current per-account and global code limits are not
  complete protection against request/connection exhaustion or email harassment.
- Decouple delivery from HTTP request timing, provide admin-visible delivery
  state, and make resends/recovery understandable without leaking account existence.
- Review session host-prefix protection, expiry/revocation, origin/CSRF behavior,
  cross-user authorization, and account recovery with adversarial tests.
- Define retention/deletion/export and who can see personal activity. Publishing
  summaries to groups, photos, write tools, and auto-linking accounts remain out
  of scope until explicit authorization semantics exist.
- Move risky tests to a separate Fly app and database with separate credentials;
  exclude or disable scheduled notifications there by default.
- Add consent-based or provider-verified gateway linking before open signup.
  SMS/WhatsApp/iOS do not need to be built to launch the existing-group web app.

## Release order

Test-workspace exclusion → hostname/certificate and origin migration → callback
authentication and operational checks → invitation lifecycle → small group pilot.
Expand only after actual sign-in, group isolation, revocation, delivery, and
restore checks pass. No rewrite or production database cutover is required.
