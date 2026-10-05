# Simplified verification links

Production bot links now use:

```text
https://alphacity.tech/verify/#verification_session=<unique-token>
```

The page's existing trusted default backend resolves the session to its Telegram
user and group, then returns that group's requirements. The session creation,
expiry, signature binding, group configuration, wallet registration, and
admission logic are unchanged. The session remains in the fragment, so it is
not sent to the static website host or CDN.

The redundant `api_verify_url` is omitted only when the validated backend URL
matches the page's default. Render and local-development deployments retain
their explicit endpoint because their sessions may live in different databases.
The Python and JavaScript default endpoints are checked for agreement by tests.

## Compatibility

- Existing fragment and query-string links remain supported under the existing
  endpoint validation rules. Unknown or malformed endpoints still fail closed.
- Migrating a legacy query-string link preserves a rejected endpoint in the
  fragment, so reloading does not silently route it to a different service.
- New default-backend links work with the already-deployed website controller,
  which already falls back to that backend when no endpoint is provided.
- The updated controller also keeps **Add another wallet** links short and
  preserves alternate routing, fresh signatures, and reload/retry recovery.
- Backend-rendered pages retain their existing server configuration precedence.

## Rollout and rollback

The Token-Gate and Alpha City PRs can be deployed in either order. Deploying the
bot creates short initial links; deploying the website keeps subsequent wallet
links short too. No database migration or configuration change is required.

The canonical template uses a new JavaScript asset version. Synchronize the
website checkout with `python scripts/sync_verify_page.py <Alphacity checkout>`
and check it with `--check`. The synchronized changes affect only the verification
controller, its asset reference, and regression tests.

Rolling back either side remains compatible: the older controller understands
session-only links, and the newer controller accepts valid older links.

## Validation

Tests cover distinct groups and users with token versus NFT/trait requirements,
server-owned group identity in signed messages, default and alternate routing,
legacy fragment/query links, multi-wallet continuation/reload, missing/expired
sessions, and rejection of invalid endpoints before network or wallet activity.
The existing Python and JavaScript suites continue to cover holdings, signatures,
admission, enforcement, and interrupted submissions.

This reduces opportunities for copy/paste or wallet-handoff damage; it cannot
repair a truncated session token. Members should still tap a fresh bot link and
use **Copy link** if their wallet browser requires pasting it.
