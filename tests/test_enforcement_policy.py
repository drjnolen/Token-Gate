import datetime
import unittest

from enforcement_policy import (
    EnforcementDecision,
    GateStatus,
    decide_auto_removal,
    evaluate_gate,
)


class GateEvaluationTests(unittest.TestCase):
    def test_token_provider_failure_is_never_treated_as_a_failed_balance(self):
        self.assertEqual(
            evaluate_gate(
                "token",
                token_valid=False,
                nft_valid=False,
                trait_valid=True,
                token_indeterminate=True,
            ),
            GateStatus.INDETERMINATE,
        )

    def test_both_mode_passes_when_either_authoritative_branch_passes(self):
        self.assertEqual(
            evaluate_gate(
                "both",
                token_valid=True,
                nft_valid=False,
                trait_valid=False,
                nft_indeterminate=True,
            ),
            GateStatus.PASS,
        )

    def test_both_mode_defers_when_no_branch_passes_and_one_is_unknown(self):
        self.assertEqual(
            evaluate_gate(
                "both",
                token_valid=False,
                nft_valid=False,
                trait_valid=True,
                nft_indeterminate=True,
            ),
            GateStatus.INDETERMINATE,
        )


class AutoRemovalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 8, 4, 12, 0, 0)

    def test_first_failure_starts_a_grace_period(self):
        decision = decide_auto_removal(
            first_failed_at=None,
            now=self.now,
            grace_seconds=86_400,
        )
        self.assertEqual(decision.action, EnforcementDecision.WARN)
        self.assertEqual(decision.remaining_seconds, 86_400)

    def test_member_is_not_removed_before_grace_expires(self):
        decision = decide_auto_removal(
            first_failed_at=self.now - datetime.timedelta(hours=23),
            now=self.now,
            grace_seconds=86_400,
        )
        self.assertEqual(decision.action, EnforcementDecision.WAIT)
        self.assertEqual(decision.remaining_seconds, 3_600)

    def test_member_is_eligible_for_final_recheck_after_grace(self):
        decision = decide_auto_removal(
            first_failed_at=self.now - datetime.timedelta(days=2),
            now=self.now,
            grace_seconds=86_400,
        )
        self.assertEqual(decision.action, EnforcementDecision.RECHECK)
        self.assertEqual(decision.remaining_seconds, 0)


if __name__ == "__main__":
    unittest.main()
