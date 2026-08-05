"""Pure policy decisions shared by admission, enforcement, and tests."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class EnforcementDecision(str, Enum):
    WARN = "warn"
    WAIT = "wait"
    RECHECK = "recheck"


@dataclass(frozen=True)
class AutoRemovalDecision:
    action: EnforcementDecision
    remaining_seconds: int


def evaluate_gate(
    registration_mode: str,
    *,
    token_valid: bool,
    nft_valid: bool,
    trait_valid: bool,
    token_indeterminate: bool = False,
    nft_indeterminate: bool = False,
    trait_indeterminate: bool = False,
) -> GateStatus:
    """Return a fail-safe gate result.

    Provider failures remain ``INDETERMINATE`` and can never become a failed
    holdings result. In ``both`` mode, either authoritative branch can pass.
    """
    nft_branch_valid = nft_valid and trait_valid
    nft_branch_indeterminate = nft_indeterminate or trait_indeterminate

    if registration_mode == "token":
        if token_indeterminate:
            return GateStatus.INDETERMINATE
        return GateStatus.PASS if token_valid else GateStatus.FAIL
    if registration_mode == "nft":
        if nft_branch_indeterminate:
            return GateStatus.INDETERMINATE
        return GateStatus.PASS if nft_branch_valid else GateStatus.FAIL
    if registration_mode == "both":
        if token_valid or nft_branch_valid:
            return GateStatus.PASS
        if token_indeterminate or nft_branch_indeterminate:
            return GateStatus.INDETERMINATE
        return GateStatus.FAIL
    return GateStatus.FAIL


def decide_auto_removal(
    *,
    first_failed_at: datetime.datetime | None,
    now: datetime.datetime,
    grace_seconds: int,
) -> AutoRemovalDecision:
    """Choose the next non-destructive enforcement step.

    A missing failure timestamp starts the grace period. Once it expires, the
    caller must run a final uncached holdings check before removing anyone.
    """
    grace_seconds = max(0, int(grace_seconds))
    if first_failed_at is None:
        return AutoRemovalDecision(EnforcementDecision.WARN, grace_seconds)

    elapsed = max(0, int((now - first_failed_at).total_seconds()))
    remaining = max(0, grace_seconds - elapsed)
    if remaining:
        return AutoRemovalDecision(EnforcementDecision.WAIT, remaining)
    return AutoRemovalDecision(EnforcementDecision.RECHECK, 0)
