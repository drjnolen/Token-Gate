# Multi-wallet registration

Interactive verification now checks the union of the newly signed wallet and
the user's wallets already registered **in this group**. Token balances and NFT
counts (including configured traits and Kiosk items) combine. Re-registering
the same canonical address never increases its contribution. The existing
"NFT or Token" policy is unchanged; tokens and NFTs are not converted into one
another.

## Website flow

1. Open the private `/register` link at `alphacity.tech/verify/`.
2. Choose a wallet and sign its session-bound ownership message. Holdings are
   checked automatically against the group's server-owned requirements.
3. If the combined holdings are below the threshold, the wallet is saved but
   no access is granted. The page reports the totals and thresholds and offers
   **Add another wallet**.
4. That button reloads the same verification page with a new session fragment.
   Choose the additional wallet/account and sign a new message. Existing
   registered wallets are included automatically, without signing them again.
5. Once the combined holdings qualify, the success message shows their total
   and wallet count. The existing Telegram confirmation/invite flow is used.

Previously admin-approved wallets participate just as they do during ongoing
membership enforcement. Admin flows, exemptions, `/mywallets`, wallet removal,
and uniqueness within a group retain their existing behavior.

## Reliability and resource bounds

- Wallet save, completed result, and at most one child session are committed in
  one database transaction. Replaying a request or refreshing restores the same
  child, not another one. The child is bound to the same Telegram user/group.
- Child sessions inherit the original expiry, never extend it. Once it expires,
  the page offers the existing Telegram new-link action. Saved wallets persist.
- Every additional wallet requires its own fresh session-bound signature;
  browser-supplied wallet lists, counts, identities, and thresholds are ignored.
- Provider failures remain retryable/indeterminate, never a fabricated zero
  balance. Fresh checks share the existing bounded operation deadline and
  concurrency slots; no transaction is held open during blockchain requests.
- The registered-wallet snapshot is rechecked under a row lock before saving.
  Concurrent additions/removals require a fresh retry instead of admitting a
  user using holdings from a removed wallet.
- A below-threshold result keeps its session only in the URL fragment for
  refresh recovery. Successful results clear it. Continuation retains the
  validated API endpoint and cannot redirect to a server-supplied URL.

## Rollout

Deploy the bot PR first, then the paired AlphaCity website PR. Bot startup adds
the nullable `verification_sessions.next_session_id` column using the existing
idempotent migration path. No wallet backfill or destructive migration is needed.
Old clients can still register; new clients gracefully fall back to requesting a
new Telegram link if an older backend does not supply a continuation session.
Existing completed sessions without a child keep their previous recovery path.

The website assets and behavioral test are exported using
`python scripts/sync_verify_page.py <Alphacity checkout>`; `--check` detects drift.
Shared wallet-connector code and visual styles are unchanged.

## Validation

Automated tests exercise the real evaluator, API handler and database functions
with mocked external boundaries, plus the actual JavaScript controller with a
mock DOM/wallet provider. Cases cover two 500k wallets reaching a 1m threshold,
duplicates, group/user scope, NFT/trait and OR gates, provider failures, signature
rejection, concurrent edits, expiry, durable replay and browser refresh/retry.

Before production rollout, smoke-test with a real Telegram test group, database,
and two real wallets: first below threshold, second bringing the total above;
refresh the first result; confirm `/mywallets` lists both; confirm the existing
invite and join-time membership checks succeed. These external integrations are
not exercised by the isolated automated tests.
