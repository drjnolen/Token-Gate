# Sui GraphQL migration and operations

## Architecture

All authoritative Sui reads now go through `SuiGraphQLGateway`:

- fungible balances use `Address.balance`;
- directly owned objects and staking receipts use `Address.objects`;
- Kiosk inventory uses `Object.dynamicFields`;
- ownership signatures use `verifySignature` with `PERSONAL_MESSAGE`.

Browser code only connects a wallet and requests a personal-message signature.
It never decides whether a user satisfies a gate.

## Required deployment settings

- `SUI_GRAPHQL_URLS`: comma-separated production GraphQL endpoints. Put the
  preferred provider first and a separately operated provider second.
- `WALLET_CONNECT_URL`: `https://alphacity.tech/verify/`
- `WALLET_CONNECT_ALLOWED_HOSTS`: `alphacity.tech,www.alphacity.tech`
- `CORS_ALLOWED_ORIGINS`: `https://alphacity.tech,https://www.alphacity.tech`
- `PUBLIC_WEBAPP_BASE_URL`: the public HTTPS origin of this bot, when the
  platform does not provide `RENDER_EXTERNAL_URL`.
- `SUI_OPERATION_TIMEOUT_SECONDS`: overall deadline for one holdings
  operation (default `90`).
- `SUI_MAX_PAGES` and `SUI_MAX_OBJECTS`: traversal safety budgets (defaults
  `200` and `10000`).
- `AUTO_REMOVE_GRACE_SECONDS`: default grace period for newly configured
  groups (default `86400`; admins can override it in `/cwconfig`).
- `VERIFY_SESSION_MAX_ATTEMPTS`: database-enforced submission limit per
  verification link (default `10`).
- `METRICS_TOKEN`: optional bearer token that enables the otherwise hidden
  JSON `/metrics` endpoint.

The public Mysten GraphQL endpoint remains a development fallback only.

Optional Telegram webhook mode requires both:

- `TELEGRAM_WEBHOOK_URL`: the bot's public HTTPS origin (the app appends
  `/telegram/webhook` when needed);
- `TELEGRAM_WEBHOOK_SECRET`: 1-256 letters, digits, underscores, or hyphens.

Without these variables, the bot uses long polling protected by a PostgreSQL
single-active-instance lease.

## Verification outcomes

- `pass`: requirements were conclusively met.
- `fail`: requirements were conclusively not met.
- `indeterminate`: at least one provider result needed for the decision was
  unavailable.

New admissions return HTTP 503 for an indeterminate result and restore the
verification session to `pending`, so the same link can be retried. Existing
members are not alerted or removed on an indeterminate result.

Wallet persistence and session completion share one database transaction.
`user_wallet_addresses` enforces one owner per wallet per group while the
existing JSON wallet list remains as a compatibility projection.

Externally hosted verification links carry their single-use session in the URL
fragment, so it is not sent to the static host or CDN. The page converts legacy
query-string links into fragments immediately and removes the fragment after a
terminal result.

## Enforcement and voting safety

- Auto-removal begins with a configurable grace period.
- When the grace period expires, the bot performs a final uncached balance
  read. Provider failure defers removal.
- Removal is implemented as Telegram ban + immediate unban, so the member can
  re-register and rejoin after restoring holdings.
- Warnings, deferrals, recoveries, failures, and removals are retained in
  `enforcement_events` for 90 days.
- Weighted polls use the first authoritative cast-time holdings result as an
  immutable per-poll snapshot. Changing an option does not re-weight the vote.
- Provider failure never records a zero-weight vote.

## Verification UI synchronization

`templates/verify.html`, `static/verify.js`, and `static/verify.css` are the
canonical source. Export them to an Alphacity checkout with:

```text
python scripts/sync_verify_page.py /path/to/Alphacity
python scripts/sync_verify_page.py --check /path/to/Alphacity
```

The exporter preserves Alphacity telemetry tags and generates its verification
page regression test.

## Deployment order

1. Deploy the Token-Gate backend with `WALLET_CONNECT_URL` temporarily unset.
2. Verify `/health` and `/ready`; `/ready` must report PostgreSQL and Sui as
   healthy.
3. Deploy the Alphacity verification page.
4. Set `WALLET_CONNECT_URL` and its allowlist/CORS settings.
5. Run a real wallet verification for token, NFT, Kiosk NFT, and any configured
   trait gate.

Rollback is safe: unset `WALLET_CONNECT_URL` to return users to the backend's
bundled page. The additive session and normalized-wallet schema can remain in
place.

## Monitoring

Alert on:

- `/ready` returning 503 for more than two cached probe windows;
- repeated `All Sui GraphQL providers failed` messages;
- sessions repeatedly recovered from stale `processing` state;
- Telegram polling lease loss or webhook authentication failures.

Group administrators can run `/cwstatus` for the deployed revision, latest
wallet-scan state, active verification count, members in grace, last
enforcement event, and credential-free GraphQL circuit status. The optional
`/metrics` endpoint exposes aggregate counters and timings without wallet or
Telegram identifiers.

The liveness `/health` endpoint intentionally performs no third-party calls.
