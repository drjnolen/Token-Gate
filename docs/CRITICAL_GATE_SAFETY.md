# Gate safety fixes and rollout

This change fixes the critical findings from the October 3 code review, along
with the related wallet-deletion race and delivered-alert cooldown query.

## Behavior

- NFT checks accept an exact, case-sensitive Move type, including any type
  arguments, or an explicitly configured package address. Package gates match
  only the object's outer package. A wrapper containing the required NFT type
  as a generic argument cannot satisfy that type's gate.
- Personal Kiosks require an explicitly trusted capability type. The default is
  Mysten's mainnet `PersonalKioskCap`, as published in the
  [official SDK constants](https://github.com/MystenLabs/ts-sdks/blob/main/packages/kiosk/src/constants.ts).
  Operators can add reviewed deployments with comma-separated full types in
  `SUI_PERSONAL_KIOSK_CAP_TYPES`. This setting grants ownership authority: do not
  add an arbitrary same-named package. Malformed entries prevent startup.
- Empty, non-exempt wallet registrations stay subject to the existing grace
  and alert/removal policy. NFT-only groups still use their existing alert-only
  policy; this change does not introduce a new automatic-removal policy.
- Configuration input requires a reply to the current private prompt within
  15 minutes. The bot rechecks the sender's admin role and active subscription
  when processing the reply. `/cancel` and selecting another menu action close
  the old prompt. A restart invalidates unfinished prompts.
- Wallet deletion has a confirmation showing the exact address and explaining
  access consequences. Its 15-minute action is bound to the group, user, and
  address. Deletion locks and reads the current registration before removing
  only that address, preserving concurrent additions and exemption state.
  Old index-based buttons ask the member to reopen `/mywallets`.
- The alert cooldown batch now supplies its timestamp and delivery version
  correctly, and is written only after an administrator notification succeeds.

## Final removal and recovery

The scanner loads group settings from PostgreSQL on each pass. Before removal,
it reads a current registration/configuration snapshot and checks the complete
gate with fresh provider data, including the NFT alternative in Token OR NFT
mode. An unavailable result defers removal.

After the provider read, the bot locks the configuration, registration,
subscription, scheduler lease, and grace state, and compares them with the
checked snapshot. It rechecks Telegram membership and clock-based permissions
immediately before the ban. Concurrent database writers serialize with this
decision, including writers on other bot instances. Admins and owners are not
removed. Blockchain requests run outside transactions; the final membership
request and single ban request hold row locks. Competing lock acquisition has
a three-second timeout, which defers removal. The lease and subscription must
have at least two minutes remaining for this action.

A `removal_recovery` row is committed **before** attempting the ban and has no
foreign key to the registration. Membership cleanup or a transaction rollback
cannot erase it. The ban has a ten-minute expiry as an independent fallback.
After successful removal the bot confirms unban; failures retain a retry job.
The recovery worker starts with the application, claims jobs across instances,
and retries without issuing another ban. An ambiguous ban timeout also retains
the recovery intent. `/cwstatus` reports pending recovery jobs. The worker
preserves a later administrator ban with a different expiry.

Automatic removal now defers for legacy basic groups: Telegram supports ban
expiry for supergroups and channels, while this bot manages group membership.
Use a supergroup for automatic removal. See the
[Telegram ban/unban API contract](https://core.telegram.org/bots/api#banchatmember).

## Deployment

1. Stop old bot workers before starting the updated release. Running the old
   enforcement or input handlers alongside the new release retains the old
   unsafe behavior.
2. The existing startup migration creates `removal_recovery` and
   `wallet_delete_actions` and their indexes. These changes are additive and
   idempotent; existing registrations and settings are retained.
3. Review existing NFT collection settings. Partial names now produce an
   unavailable check instead of a permissive substring match. Replace them
   with the full Move type or the intended package address. Review any personal
   Kiosk deployment beyond the default before explicitly trusting its cap type.
4. Verify `/cwstatus`, a fresh wallet registration, and a private config reply
   in a staging supergroup. Exercise removal with a test member before enabling
   production auto-removal. No production Telegram or blockchain mutation is
   performed by the automated tests.
5. Keep a compatible recovery worker running until `removal_recovery` is empty
   when rolling back. The finite Telegram expiry still provides a fallback.

## Validation

The unit suite covers forged cap packages, nested collection types, case
sensitivity, empty-wallet scans, prompt ownership/expiry/permission changes,
fresh NFT recovery, stale decisions, lost leases, ambiguous bans, failed unbans,
and restart recovery. PostgreSQL integration tests run the real startup
migration twice and verify row locking, concurrent wallet addition/deletion,
durable recovery, changed authorization state, and the alert batch SQL.

CI provides a disposable PostgreSQL 17 service. Locally, set
`TEST_DATABASE_URL` to a dedicated test database and run
`python -m unittest discover -s tests -v`. The integration tests create and drop
their own random schema. Without that setting they are explicitly skipped.
