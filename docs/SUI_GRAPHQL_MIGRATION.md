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

The liveness `/health` endpoint intentionally performs no third-party calls.
