import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_accounts.manager import describe_quota, quota_labels
from codex_accounts.state import AccountError


class QuotaTests(unittest.TestCase):
    def test_window_duration_identifies_weekly_only_and_reordered_limits(self):
        weekly = {"usedPercent": 60, "windowDurationMins": 10080}
        self.assertEqual(
            describe_quota({"rateLimits": {"primary": weekly, "secondary": None}}),
            "5h: unavailable | weekly: 40% left",
        )
        self.assertEqual(
            describe_quota(
                {
                    "rateLimits": {
                        "primary": weekly,
                        "secondary": {"usedPercent": 5, "windowDurationMins": 300},
                    }
                }
            ),
            "5h: 95% left | weekly: 40% left",
        )

    def test_codex_bucket_takes_precedence_over_other_model_limits(self):
        other = {
            "limitId": "codex_other",
            "primary": {"usedPercent": 10, "windowDurationMins": 300},
        }
        self.assertEqual(
            describe_quota(
                {
                    "rateLimits": other,
                    "rateLimitsByLimitId": {
                        "codex_other": other,
                        "codex": {
                            "primary": {"usedPercent": 80, "windowDurationMins": 300}
                        },
                    },
                }
            ),
            "5h: 20% left | weekly: unavailable",
        )
        self.assertEqual(
            describe_quota({"rateLimits": other}),
            "5h: unavailable | weekly: unavailable",
        )

    def test_missing_and_malformed_usage_never_claims_available_quota(self):
        unavailable = "5h: unavailable | weekly: unavailable"
        for result in (None, {}, {"rateLimits": None}, {"rateLimits": []}):
            with self.subTest(result=result):
                self.assertEqual(describe_quota(result), unavailable)
        for used in (None, True, "50", "\x1b[2J", [], {}, float("nan")):
            with self.subTest(used=used):
                self.assertEqual(
                    describe_quota(
                        {
                            "rateLimits": {
                                "primary": {
                                    "usedPercent": used,
                                    "windowDurationMins": 300,
                                }
                            }
                        }
                    ),
                    unavailable,
                )
        for duration in (None, True, "300", 15, [], {}):
            with self.subTest(duration=duration):
                self.assertEqual(
                    describe_quota(
                        {
                            "rateLimits": {
                                "primary": {
                                    "usedPercent": 0,
                                    "windowDurationMins": duration,
                                }
                            }
                        }
                    ),
                    unavailable,
                )

    def test_remaining_percent_is_clamped_at_empty_and_full(self):
        self.assertEqual(
            describe_quota(
                {
                    "rateLimits": {
                        "primary": {"usedPercent": 120, "windowDurationMins": 300},
                        "secondary": {"usedPercent": -5, "windowDurationMins": 10080},
                    }
                }
            ),
            "5h: 0% left | weekly: 100% left",
        )

    def test_parallel_lookups_are_bounded_and_failures_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = {}
            for number in range(9):
                home = Path(temporary) / str(number)
                home.mkdir()
                accounts[str(number)] = {"home": str(home), "kind": "chatgpt"}
            active = peak = 0

            async def lookup(binary, home):
                nonlocal active, peak
                self.assertEqual(binary, "codex-fixture")
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    if home.name == "0":
                        raise AccountError("Quota unavailable")
                    return {
                        "rateLimits": {
                            "primary": {
                                "usedPercent": int(home.name),
                                "windowDurationMins": 300,
                            }
                        }
                    }
                finally:
                    active -= 1

            with (
                patch(
                    "codex_accounts.manager.codex_binary", return_value="codex-fixture"
                ),
                patch("codex_accounts.manager.read_rate_limits", side_effect=lookup),
            ):
                labels = quota_labels(accounts)

            self.assertGreater(peak, 1)
            self.assertLessEqual(peak, 4)
            self.assertEqual(labels["0"], "5h: unavailable | weekly: unavailable")
            for number in range(1, 9):
                self.assertEqual(
                    labels[str(number)],
                    f"5h: {100 - number}% left | weekly: unavailable",
                )

    def test_unsupported_accounts_and_missing_homes_skip_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = {
                "signed-out": {"home": temporary, "kind": "signedOut"},
                "api-key": {"home": temporary, "kind": "apiKey"},
                "missing": {
                    "home": str(Path(temporary) / "missing"),
                    "kind": "chatgpt",
                },
            }
            with (
                patch(
                    "codex_accounts.manager.codex_binary", return_value="codex-fixture"
                ),
                patch(
                    "codex_accounts.manager.read_rate_limits", new_callable=AsyncMock
                ) as lookup,
            ):
                labels = quota_labels(accounts)
            lookup.assert_not_awaited()
            self.assertTrue(
                all(
                    label == "5h: unavailable | weekly: unavailable"
                    for label in labels.values()
                )
            )


if __name__ == "__main__":
    unittest.main()
