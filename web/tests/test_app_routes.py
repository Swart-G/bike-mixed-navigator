from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import app as web_app


class RoutesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = web_app.app.test_client()

    def test_returns_503_without_running_planner_when_otp_is_unavailable(self) -> None:
        planner = Mock()
        planner.otp.health.side_effect = RuntimeError("connection refused")

        with patch.object(web_app, "planner", planner):
            response = self.client.post("/api/routes", json={})

        self.assertEqual(503, response.status_code)
        self.assertEqual("OTP_UNAVAILABLE", response.get_json()["code"])
        planner.plan.assert_not_called()

    def test_returns_503_when_otp_is_running_but_not_ready(self) -> None:
        planner = Mock()
        planner.otp.health.return_value = False

        with patch.object(web_app, "planner", planner):
            response = self.client.post("/api/routes", json={})

        self.assertEqual(503, response.status_code)
        self.assertEqual("OTP_UNAVAILABLE", response.get_json()["code"])
        planner.plan.assert_not_called()

    def test_returns_503_if_otp_fails_while_building_an_empty_result(self) -> None:
        planner = Mock()
        planner.otp.health.side_effect = [True, RuntimeError("connection reset")]
        planner.plan.return_value = {"routes": [], "warnings": [], "stats": {}}

        with patch.object(web_app, "planner", planner):
            response = self.client.post("/api/routes", json={})

        self.assertEqual(503, response.status_code)
        self.assertEqual("OTP_UNAVAILABLE", response.get_json()["code"])

    def test_keeps_a_valid_empty_result_when_otp_is_still_ready(self) -> None:
        planner = Mock()
        planner.otp.health.return_value = True
        planner.plan.return_value = {"routes": [], "warnings": [], "stats": {}}

        with patch.object(web_app, "planner", planner):
            response = self.client.post("/api/routes", json={})

        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.get_json()["routes"])
        self.assertEqual(2, planner.otp.health.call_count)


if __name__ == "__main__":
    unittest.main()
