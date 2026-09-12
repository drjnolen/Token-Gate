# Reliability and resource review — 2026-09-11

Reviewed Token-Gate main at `5e4da6a`, including database lifecycle, background
wallet scans, verification, voting, caches, GraphQL retries/pagination, delayed
tasks, health endpoints, and CI. This is a source review with regression tests;
it is not a production database profile, live-wallet acceptance test, or review
of the entire Alphacity website. No production settings or data were changed.

## Findings fixed in this PR

| Priority | Location | Finding and change |
| --- | --- | --- |
| P1 | `main.py:get_connection_pool`, `get_db_cursor` | A periodic pool probe caught every error, including pool exhaustion, and called `closeall()`. This could terminate unrelated in-flight transactions. Reusing an existing pool now requires no extra checkout/query. A failed rollback discards only that connection; failed returns no longer abandon the shared pool. A newly created pool is published only after its startup probe succeeds. |
| P1 | `runtime_support.py:SlidingWindowRateLimiter` | `max_keys` was not a real bound: live identities were never removed, and every new key beyond capacity triggered a whole-map scan. Ordered expiry now removes oldest expired entries, caps live identities, and preserves existing quotas. New identities receive the existing rate-limit response at capacity until a slot expires. |
| P2 | `main.py:fetch_wallet_balances`, `runtime_support.py:bounded_executor_map` | `Executor.map` eagerly submitted entire wallet lists. A large scan could create thousands of queued futures ahead of interactive work. Each batch now has at most twice `SUI_FETCH_WORKERS` outstanding futures (16 by default), preserves result ordering, and cancels queued futures when iteration stops or a worker raises. Duplicate addresses within a call are fetched once. This is a per-batch bound, not a global admission limit across callers. Running futures cannot be cancelled by this helper. |
| P2 | `main.py:fetch_wallet_balances` | Cache keys lowercased case-sensitive Move type names and omitted decimals even though values were already scaled. Keys now preserve token-type case and include decimals, preventing incorrect reuse after configuration changes. Fresh verification requests still bypass cached results. |
| P2 | `main.py:check_user_wallets` | A shallow copy of the group map left each group's configuration mutable during a long scan. Each group dictionary is now copied under the configuration lock. This supplies a consistent per-scan snapshot; it does not make a running scan immediately adopt subsequent admin edits. |

## Remaining findings and recommended follow-up

1. **P2 — Database activity continues even when there are no user requests.**
   `keep_alive` calls `cleanup_expired_data` every minute, issuing four cleanup
   statements. Scheduler and Telegram poller lease renewal also write every
   60 and 45 seconds respectively. These operations may prevent an otherwise
   idle serverless database from suspending. At one instance, cleanup alone is
   approximately 5,760 statements/day; this is a code-derived estimate, not a
   production measurement. Measure cleanup duration, expired-row volume, and
   lease contention before separating fast session cleanup from less frequent
   audit/orphan cleanup. Preserve lease renewal deadlines: reducing heartbeats
   without changing lease policy risks duplicate pollers or enforcement.

2. **P2 — Interactive trait verification repeats fresh NFT discovery.**
   `evaluate_wallet_requirements(force_fresh=True)` calls a fresh collection
   count and then a fresh trait/category count. Both can traverse owned objects
   and Kiosks. Periodic enforcement already reuses one fetched object list.
   A future refactor should share one request-local fresh snapshot for the
   interactive path, retaining its retry and indeterminate-result semantics.
   Do not substitute the 12-hour display cache for admission or removal checks.

3. **P2 — Readiness checks can duplicate provider work.**
   `readiness_check` releases its cache lock before database/GraphQL checks.
   Concurrent requests after expiry can all probe independently, and the cache
   timestamp precedes the potentially slow probe. Coalesce refreshes and apply
   a small dedicated provider deadline if monitoring traffic is material.
   Continue using the cheap `/health` endpoint for liveness. The current PR
   removes only the redundant pool-manager probes, not `/ready` diagnostics.

4. **P2 — Synchronous work and queue limits need workload measurements.**
   The immediate background executor and delayed-task heap have no global
   backlog cap. Delayed callbacks run on one scheduler thread, so slow network
   calls can postpone other callbacks. Measure queue depth and callback latency
   before introducing rejection or worker handoff: silently dropping callbacks
   could lose user notifications. The new balance window reduces one source of
   queue pressure but does not establish a process-wide resource budget.

5. **P2 — Pool sizing and write reduction need production evidence.**
   The default pool retains five connections and permits fifteen. Cached
   holdings updates write a timestamp even when values remain unchanged.
   Inspect peak borrowed connections, pool exhaustion, query latency, row
   updates, and database compute billing before lowering pool limits or
   batching/skipping writes. Skipping writes must preserve the meaning of
   `holdings_updated_at`. No unmeasured monthly savings are claimed here.

6. **P2 — Integration coverage remains limited.**
   The application performs service initialization at import time. New tests
   execute the actual selected function bodies with fake pools/gateways,
   exercising lifecycle behavior without connecting to production. They do not
   prove PostgreSQL socket recovery, database locking under load, or live wallet
   compatibility. A disposable PostgreSQL integration harness and staging
   concurrent-verification smoke test remain useful follow-up work.

## Validation and rollout

- 72 Python tests pass, including fault injection for pool exhaustion, rollback
  failure, failed returns, cache isolation, duplicate lookup suppression, bounded
  submission/cancellation, and limiter capacity/expiry.
- Ruff and whitespace checks pass. Existing admission, voting, verification,
  and GraphQL regression suites are included in the test run.
- No schema migration, dependency change, registration UI change, or production
  configuration change is required. CI validates the PR before merge.
- After deployment, compare database connection errors, verification latency,
  wallet-scan duration, and process memory against the previous release. Pool
  saturation will still fail a checkout; it should no longer destroy unrelated
  transactions. New limiter identities are rejected at capacity by design.

The demonstrated reductions are structural: no recurring pool-manager probe,
no full-map limiter scan per new identity, and a bounded number of outstanding
balance futures per batch. Dollar and CPU savings depend on actual traffic.
