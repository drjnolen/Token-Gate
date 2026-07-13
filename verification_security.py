"""Small, dependency-light primitives for Sui wallet ownership verification.

The Sui wallet standard serializes an Ed25519 personal-message signature as
``base64(flag || signature || public_key)``.  The signed bytes are the Sui
``PersonalMessage`` intent followed by BCS-encoded message bytes, hashed with
Blake2b-256.  Keeping this code separate from the Telegram/Flask application
makes the security-critical format directly testable.
"""

import base64
import hashlib
import hmac

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


SUI_ED25519_SCHEME_FLAG = 0
_SUI_PERSONAL_MESSAGE_INTENT = b"\x03\x00\x00"


def canonical_sui_address(address: str) -> str | None:
    """Return a normalized 32-byte Sui address, or ``None`` when invalid."""
    if not isinstance(address, str):
        return None
    value = address.strip().lower()
    if not value.startswith("0x"):
        return None
    body = value[2:]
    if not body or len(body) > 64:
        return None
    try:
        int(body, 16)
    except ValueError:
        return None
    return "0x" + body.zfill(64)


def _uleb128(value: int) -> bytes:
    """Encode a non-negative integer using the BCS ULEB128 representation."""
    if value < 0:
        raise ValueError("ULEB128 values must be non-negative")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def sui_personal_message_digest(message: str) -> bytes:
    """Return the exact digest Sui wallets sign for ``signPersonalMessage``."""
    encoded = message.encode("utf-8")
    signed_bytes = _SUI_PERSONAL_MESSAGE_INTENT + _uleb128(len(encoded)) + encoded
    return hashlib.blake2b(signed_bytes, digest_size=32).digest()


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


def verify_sui_personal_message_signature(address: str, message: str, serialized_signature: str) -> bool:
    """Verify an Ed25519 Sui ``signPersonalMessage`` signature.

    The public key embedded in the serialized signature is also used to derive
    the Sui address.  Therefore a valid signature for a different wallet is
    not sufficient to register the submitted address.
    """
    expected_address = canonical_sui_address(address)
    if not expected_address or not isinstance(message, str) or not isinstance(serialized_signature, str):
        return False
    try:
        raw = base64.b64decode(serialized_signature, validate=True)
    except (ValueError, TypeError):
        return False

    # Sui Ed25519: 1-byte scheme flag, 64-byte signature, 32-byte public key.
    if len(raw) != 97 or raw[0] != SUI_ED25519_SCHEME_FLAG:
        return False
    signature = raw[1:65]
    public_key = raw[65:]
    actual_address = "0x" + hashlib.blake2b(raw[:1] + public_key, digest_size=32).hexdigest()
    if not hmac.compare_digest(expected_address, actual_address):
        return False
    try:
        VerifyKey(public_key).verify(sui_personal_message_digest(message), signature)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False
