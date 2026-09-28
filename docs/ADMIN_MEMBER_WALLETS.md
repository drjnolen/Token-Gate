# Add a member wallet from private configuration

1. A group administrator runs `/cwconfig` in the group and opens the private
   configuration link.
2. Select **Add member wallet**, then **Choose member** on Telegram's keyboard.
3. Select one person. The picker can show people outside the group; the bot
   checks membership before continuing. Bot accounts and nonmembers are rejected.
4. Reply to the bot's wallet-address prompt with the member's Sui address.
5. Check the group, member name, numeric Telegram ID, and full wallet address.
   Press **Confirm and add wallet** to save.
6. Use **Back to configuration** after the success message.

This is an administrator-approved registration. It does not prove wallet
ownership or perform an immediate holdings check. Members who need ownership
verification should use the existing registration/signature flow.

Existing wallets and exemption status are preserved. The same address cannot
be assigned to another member of the same group. The existing `/addwallet`
reply and numeric-ID commands remain available without changes.

## Cancellation, authorization, and recovery

- Send `/cancel` or use **Cancel** to discard the form.
- Each form expires after 15 minutes. Starting a new form replaces the old one;
  choosing another private configuration action closes it. Old selections and
  confirmation buttons cannot apply to a replacement form or a different admin.
- Admin status, active subscription, and target membership are checked before
  saving. Restricted users are accepted only when Telegram confirms they are
  still members. The bot must be an administrator in the group for reliable
  membership lookup.
- Entering this flow clears a previous unfinished configuration input prompt.
  Address input must be a reply to this form's own prompt; unrelated commands
  are not captured by the form.
- Invalid addresses can be corrected by replying to the original address
  prompt again. Temporary lookup/database failures leave confirmation retryable.
  If a save response is lost, retrying adds the same canonical address through
  the existing duplicate-safe, transactional writer.
- Drafts are bounded, process-local, and require no schema migration. A restart
  loses unfinished forms; reopen the menu and start again. If multiple bot
  processes handle updates, drafts are not shared between them.
- Telegram clients must support the native user-sharing keyboard. If the picker
  is unavailable, update the Telegram client or use `/addwallet <user_id> <address>`.

## Validation

Tests exercise the complete controller with mocked Telegram/DB dependencies,
real Telegram message dispatch for command coexistence, and the existing main
configuration callback and append-only save adapter. They cover permission
revocation, departed members, subscription expiry, wrong-user/group callbacks,
expired/replaced forms, concurrent confirmation, duplicate-address races,
database failures, and lost success-message delivery.

A real Telegram client/picker smoke test remains a deployment check; local tests
do not contact real group members or write to the production database.
