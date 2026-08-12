"""Stable user-facing summaries for completed wallet verification."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


VERIFICATION_SUCCESS_COPY = "Wallet verified and registered successfully."


def _decimal_string(value: Any) -> str | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def qualifying_holdings_summary(
    config: Mapping[str, Any],
    requirement_eval: Mapping[str, Any],
) -> dict[str, str | int]:
    """Return only holdings that participated in a successful configured gate."""
    if not requirement_eval.get("requirements_met"):
        return {}

    mode = config.get("registration_mode", "token")
    summary: dict[str, str | int] = {}
    if mode in {"token", "both"} and config.get("token"):
        token_balance = _decimal_string(requirement_eval.get("token_balance"))
        if token_balance is not None:
            summary["token_balance"] = token_balance

    if mode in {"nft", "both"} and config.get("nft_collection_id"):
        count_key = "trait_count" if config.get("nft_trait_name") else "nft_count"
        nft_count = requirement_eval.get(count_key)
        if isinstance(nft_count, int) and not isinstance(nft_count, bool) and nft_count >= 0:
            summary["nft_count"] = nft_count

    return summary


def _format_token_amount(value: Any) -> str | None:
    text = _decimal_string(value)
    if text is None:
        return None
    whole, separator, fraction = text.partition(".")
    grouped = f"{int(whole):,}"
    return grouped + (separator + fraction if separator else "")


def verification_success_message(summary: Mapping[str, Any] | None) -> str:
    """Build stable success copy from a durable qualifying-holdings summary."""
    summary = summary or {}
    parts: list[str] = []

    token_amount = _format_token_amount(summary.get("token_balance"))
    if token_amount is not None:
        token_noun = "token" if token_amount == "1" else "tokens"
        parts.append(f"{token_amount} qualifying {token_noun}")

    nft_count = summary.get("nft_count")
    if isinstance(nft_count, int) and not isinstance(nft_count, bool) and nft_count >= 0:
        nft_noun = "NFT" if nft_count == 1 else "NFTs"
        parts.append(f"{nft_count:,} qualifying {nft_noun}")

    if not parts:
        return VERIFICATION_SUCCESS_COPY
    if len(parts) == 1:
        holdings_copy = parts[0]
    else:
        holdings_copy = ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{VERIFICATION_SUCCESS_COPY} {holdings_copy} found."
