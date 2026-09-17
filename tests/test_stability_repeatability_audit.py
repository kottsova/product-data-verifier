import unittest

from diagnostics.stability_repeatability_audit import (
    _jump_over_half,
    _repeatability_score,
)


class StabilityRepeatabilityAuditTests(unittest.TestCase):
    def test_confirmed_jump_treats_zero_to_positive_as_unstable(self) -> None:
        self.assertTrue(_jump_over_half([0, 2, 2]))
        self.assertTrue(_jump_over_half([2, 4, 3]))
        self.assertFalse(_jump_over_half([4, 5, 6]))
        self.assertFalse(_jump_over_half([0, 0, 0]))

    def test_repeatability_score_is_100_for_identical_runs(self) -> None:
        self.assertEqual(_repeatability_score(0.0, 0.0, 0.0, 0.0), 100.0)

    def test_repeatability_score_penalizes_each_variance_dimension(self) -> None:
        score = _repeatability_score(0.2, 20.0, 0.2, 0.2)
        self.assertEqual(score, 80.0)


if __name__ == "__main__":
    unittest.main()
