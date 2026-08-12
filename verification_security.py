"""Canonical inputs for provider-verified Sui wallet ownership."""

import re


_VERIFICATION_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def is_valid_verification_session_id(value: str) -> bool:
    """Return whether a value can be one of our URL-safe session secrets."""
    return isinstance(value, str) and bool(_VERIFICATION_SESSION_PATTERN.fullmatch(value))


def canonical_sui_address(address: str) -> str | None:
    """Return a normalized 32-byte Sui address, or ``None`` when invalid."""
    if not isinstance(address, str):
        return None
    value = address.strip().lower()
    if not value.startswith("0x"):
        return None
    body = value[2:]
    if not body or len(body) > 64 or re.fullmatch(r"[0-9a-f]+", body) is None:
        return None
    return "0x" + body.zfill(64)


def build_wallet_ownership_message(session_id: str, group_id: int, user_id: int, address: str) -> str:
    """Build the canonical, session-bound message a wallet must sign."""
    canonical_address = canonical_sui_address(address)
    if not canonical_address:
        raise ValueError("Invalid Sui address")
    return (
        "Token Gate wallet ownership verification\n"
        f"Session: {session_id}\n"
        f"Telegram user: {int(user_id)}\n"
        f"Group: {int(group_id)}\n"
        f"Wallet: {canonical_address}"
    )
