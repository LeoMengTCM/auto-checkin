"""Unit tests for pure functions in server.py (no browser / network needed).

Run:  python3 -m unittest discover tests -v
"""

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import server  # noqa: E402


class TestMaskToken(unittest.TestCase):
    def test_normal_token(self):
        self.assertEqual(server._mask_token("sk-abc1234567890xyz"), "sk-abc****0xyz")

    def test_short_token(self):
        self.assertEqual(server._mask_token("sk-123"), "sk-****")

    def test_empty_and_none(self):
        self.assertEqual(server._mask_token(""), "")
        self.assertEqual(server._mask_token(None), "")
        self.assertEqual(server._mask_token(123), "")


class TestSafeName(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(server._safe_name("alice"))
        self.assertTrue(server._safe_name("用户_01"))

    def test_traversal(self):
        self.assertFalse(server._safe_name("../etc"))
        self.assertFalse(server._safe_name("a/b"))
        self.assertFalse(server._safe_name("a\\b"))
        self.assertFalse(server._safe_name(".."))
        self.assertFalse(server._safe_name(".hidden"))

    def test_empty_and_long(self):
        self.assertFalse(server._safe_name(""))
        self.assertFalse(server._safe_name(None))
        self.assertFalse(server._safe_name("x" * 101))


class TestParseBalance(unittest.TestCase):
    def test_values(self):
        self.assertEqual(server._parse_balance_value("$25.50"), 25.5)
        self.assertEqual(server._parse_balance_value("$1,200.00"), 1200.0)
        self.assertIsNone(server._parse_balance_value(None))
        self.assertIsNone(server._parse_balance_value("abc"))


class TestSessionExpiry(unittest.TestCase):
    class LoginPage:
        def evaluate(self, script):
            if script == server.CHECK_LOGIN_PAGE_JS:
                return True
            return {}

    def test_login_page_is_classified_as_session_expired(self):
        result = server.detect_checkin_status(self.LoginPage(), server.DEFAULT_URL)
        self.assertEqual(result["state"], "session_expired")
        self.assertIn("会话已过期", result["message"])


class TestDetectSite(unittest.TestCase):
    def test_known_hosts(self):
        self.assertEqual(server.detect_site("https://anyrouter.top"), server.SITE_ANYROUTER)
        self.assertEqual(server.detect_site("https://agentrouter.org"), server.SITE_AGENTROUTER)

    def test_backup_domain_and_subdomain(self):
        self.assertEqual(server.detect_site("https://ps.air-outer.com"), server.SITE_AGENTROUTER)
        self.assertEqual(server.detect_site("https://www.agentrouter.org"), server.SITE_AGENTROUTER)

    def test_unknown_and_invalid_fall_back_to_anyrouter(self):
        self.assertEqual(server.detect_site("https://example.com"), server.SITE_ANYROUTER)
        self.assertEqual(server.detect_site(""), server.SITE_ANYROUTER)
        self.assertEqual(server.detect_site(None), server.SITE_ANYROUTER)

    def test_health_model_defaults(self):
        self.assertEqual(server.health_model_for("https://agentrouter.org"), "deepseek-v4-flash")
        self.assertIsNone(server.health_model_for("https://anyrouter.top"))


class TestCheckinConcurrency(unittest.TestCase):
    def test_worker_count_is_bounded(self):
        self.assertEqual(server._checkin_worker_count(0), 0)
        self.assertEqual(server._checkin_worker_count(1), 1)
        self.assertEqual(server._checkin_worker_count(10), 2)
        self.assertEqual(server._checkin_worker_count(10, 3), 3)


class TestClassifyProbeResponse(unittest.TestCase):
    def _ok_body(self, text="OK"):
        return json.dumps({
            "type": "message",
            "content": [{"type": "text", "text": text}],
        })

    def test_success(self):
        r = server._classify_probe_response(200, self._ok_body())
        self.assertTrue(r["ok"])
        self.assertEqual(r["category"], "ok")
        self.assertIn("OK", r["message"])

    def test_auth(self):
        body = json.dumps({"error": {"message": "invalid api key"}})
        r = server._classify_probe_response(401, body)
        self.assertEqual(r["category"], "auth")
        r = server._classify_probe_response(403, body)
        self.assertEqual(r["category"], "auth")

    def test_quota_by_status(self):
        r = server._classify_probe_response(402, "{}")
        self.assertEqual(r["category"], "quota")

    def test_quota_by_keyword_beats_status(self):
        body = json.dumps({"error": {"message": "余额不足，请充值"}})
        r = server._classify_probe_response(403, body)
        self.assertEqual(r["category"], "quota")

    def test_rate_limit(self):
        r = server._classify_probe_response(429, "{}")
        self.assertEqual(r["category"], "rate_limit")

    def test_model_unavailable(self):
        body = json.dumps({"error": {"message": "model not found"}})
        r = server._classify_probe_response(404, body)
        self.assertEqual(r["category"], "model")

    def test_server_error(self):
        r = server._classify_probe_response(503, "upstream error")
        self.assertEqual(r["category"], "server")

    def test_malformed_200(self):
        r = server._classify_probe_response(200, "not-json")
        self.assertFalse(r["ok"])
        self.assertEqual(r["category"], "unknown")


class TestParseHHMM(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(server._parse_hhmm("09:00"), (9, 0))
        self.assertEqual(server._parse_hhmm("23:59"), (23, 59))
        self.assertEqual(server._parse_hhmm("8:05"), (8, 5))

    def test_invalid(self):
        for bad in ("24:00", "12:60", "abc", "9", "", None, "09:0"):
            self.assertIsNone(server._parse_hhmm(bad), bad)


class TestScheduleConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = server.get_schedule_config({})
        self.assertEqual(cfg, server.SCHEDULE_DEFAULTS)

    def test_merge_and_clamp(self):
        cfg = server.get_schedule_config({
            "schedule": {"enabled": 1, "time": "07:30", "jitter_minutes": 999},
        })
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["time"], "07:30")
        self.assertEqual(cfg["jitter_minutes"], 120)

    def test_invalid_values_fall_back(self):
        cfg = server.get_schedule_config({
            "schedule": {"time": "25:99", "jitter_minutes": "abc"},
        })
        self.assertEqual(cfg["time"], server.SCHEDULE_DEFAULTS["time"])
        self.assertEqual(cfg["jitter_minutes"], server.SCHEDULE_DEFAULTS["jitter_minutes"])


class TestComputeNextRun(unittest.TestCase):
    NOW = datetime(2026, 7, 9, 10, 0, 0)

    def test_future_slot_today(self):
        nr = server.compute_next_run(self.NOW, "11:00", 0)
        self.assertEqual(nr, self.NOW.replace(hour=11, minute=0))

    def test_past_slot_rolls_to_tomorrow(self):
        nr = server.compute_next_run(self.NOW, "09:00", 0)
        self.assertEqual(nr, self.NOW.replace(hour=9) + timedelta(days=1))

    def test_catch_up_runs_soon(self):
        nr = server.compute_next_run(self.NOW, "09:00", 30, catch_up=True)
        self.assertTrue(self.NOW < nr <= self.NOW + timedelta(minutes=3))

    def test_jitter_bounds(self):
        for _ in range(20):
            nr = server.compute_next_run(self.NOW, "11:00", 10)
            base = self.NOW.replace(hour=11, minute=0)
            self.assertTrue(base <= nr <= base + timedelta(minutes=10))


if __name__ == "__main__":
    unittest.main()
