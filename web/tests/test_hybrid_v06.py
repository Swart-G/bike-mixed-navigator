from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from routing.gtfs_index import GtfsIndex, LineMetrics
from routing.planner import RoutePlanner


def line_coords(points):
    return {"type": "LineString", "coordinates": points}


def bike_leg(points, distance=1000):
    return {
        "mode": "BICYCLE",
        "transitLeg": False,
        "duration": 300,
        "distance": distance,
        "geometry": line_coords(points),
        "from": {"name": "A"},
        "to": {"name": "B"},
        "route": None,
    }


def transit_leg(points, route="m1", mode="BUS", distance=5000):
    return {
        "mode": mode,
        "transitLeg": True,
        "duration": 900,
        "distance": distance,
        "geometry": line_coords(points),
        "from": {"name": "S1"},
        "to": {"name": "S2"},
        "route": {"shortName": route, "longName": None, "mode": mode},
    }


class HybridPlannerLogicTests(unittest.TestCase):
    def setUp(self):
        self.planner = RoutePlanner("http://invalid.local/otp/gtfs/v1", "/does/not/exist.zip")

    def test_target_bike_share_changes_with_trip_length(self):
        self.assertEqual(self.planner._base_target_bike_share(3500), 0.90)
        self.assertEqual(self.planner._base_target_bike_share(7000), 0.70)
        self.assertEqual(self.planner._base_target_bike_share(12000), 0.45)
        self.assertEqual(self.planner._base_target_bike_share(20000), 0.22)
        self.assertEqual(self.planner._base_target_bike_share(30000), 0.10)

    def test_weak_short_bus_can_be_replaced_by_bike(self):
        weak = LineMetrics(
            route_id="local",
            mode="BUS",
            trip_count=30,
            median_headway_s=900,
            commercial_speed_kmh=10,
            bikes_allowed_ratio=1.0,
            trunk_score=0.30,
        )
        leg = {"mode": "BUS", "distance": 900, "duration": 240}
        bike = {"duration": 185, "bikeDistance": 900}
        decision = self.planner._transit_leg_replacement_decision(
            leg=leg,
            bike=bike,
            wait_before=240,
            line=weak,
            downstream_trunk_score=0.0,
            route_focus=0,
        )
        self.assertTrue(decision["replace"])
        self.assertGreater(decision["savedSeconds"], 90)

    def test_strong_rail_is_not_removed_for_small_gain(self):
        strong = LineMetrics(
            route_id="d2",
            mode="RAIL",
            trip_count=100,
            median_headway_s=420,
            commercial_speed_kmh=42,
            bikes_allowed_ratio=1.0,
            trunk_score=0.90,
        )
        leg = {"mode": "RAIL", "distance": 1300, "duration": 180}
        bike = {"duration": 260, "bikeDistance": 1250}
        decision = self.planner._transit_leg_replacement_decision(
            leg=leg,
            bike=bike,
            wait_before=180,
            line=strong,
            downstream_trunk_score=0.0,
            route_focus=0,
        )
        self.assertFalse(decision["replace"])

    def test_pareto_removes_clearly_dominated_route(self):
        a = {"kind": "mixed", "doorToDoor": 1800, "transfers": 1, "discomfort": 300, "score": 2100}
        b = {"kind": "mixed", "doorToDoor": 2200, "transfers": 2, "discomfort": 650, "score": 2850}
        c = {"kind": "mixed", "doorToDoor": 1950, "transfers": 0, "discomfort": 420, "score": 2370}
        kept = self.planner._pareto_prune([a, b, c])
        self.assertIn(a, kept)
        self.assertIn(c, kept)
        self.assertNotIn(b, kept)

    def test_similarity_collapses_same_transit_corridor(self):
        r1 = {
            "legs": [
                bike_leg([[37.60, 55.70], [37.61, 55.705]]),
                transit_leg([[37.61, 55.705], [37.65, 55.72], [37.70, 55.74]], "m1"),
            ]
        }
        r2 = {
            "legs": [
                bike_leg([[37.601, 55.701], [37.611, 55.706]]),
                transit_leg([[37.611, 55.706], [37.651, 55.721], [37.701, 55.741]], "m1"),
            ]
        }
        r3 = {
            "legs": [
                bike_leg([[37.60, 55.70], [37.58, 55.72]]),
                transit_leg([[37.58, 55.72], [37.56, 55.75], [37.54, 55.78]], "77"),
            ]
        }
        self.assertGreater(self.planner._route_similarity(r1, r2), 0.65)
        self.assertLess(self.planner._route_similarity(r1, r3), 0.35)


class GtfsIndexTests(unittest.TestCase):
    def _make_gtfs(self, root: Path) -> Path:
        path = root / "mini.zip"
        files = {
            "stops.txt": """stop_id,stop_name,stop_lat,stop_lon\nS1,Start,55.7000,37.6000\nS2,Local,55.7100,37.6200\nS3,Trunk,55.7200,37.6400\nS4,End,55.7600,37.7000\n""",
            "routes.txt": """route_id,route_short_name,route_type\nR1,L1,3\nR2,M1,3\n""",
            "trips.txt": """route_id,service_id,trip_id,bikes_allowed\nR1,WD,T1,2\nR2,WD,T2,1\nR2,WD,T3,1\nR2,WD,T4,1\n""",
            "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\nT2,08:00:00,08:00:00,S3,1\nT2,08:20:00,08:20:00,S4,2\nT3,08:06:00,08:06:00,S3,1\nT3,08:26:00,08:26:00,S4,2\nT4,08:12:00,08:12:00,S3,1\nT4,08:32:00,08:32:00,S4,2\n""",
        }
        with zipfile.ZipFile(path, "w") as zf:
            for name, body in files.items():
                zf.writestr(name, body)
        return path

    def test_trip_policy_and_line_metrics_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_gtfs(Path(tmp))
            index = GtfsIndex(str(path))
            index.ensure_loaded()
            self.assertTrue(index.loaded)
            self.assertEqual(index.bike_allowed_for_trip("1:T1"), 2)
            self.assertEqual(index.bike_allowed_for_trip("T2"), 1)
            metrics = index.line_metrics_for_trip("T2")
            self.assertIsNotNone(metrics)
            assert metrics is not None
            self.assertEqual(metrics.route_id, "R2")
            self.assertAlmostEqual(metrics.median_headway_s or 0, 360, delta=1)
            self.assertGreater(metrics.trunk_score, 0.45)
            self.assertEqual(metrics.route_name, "M1")
            self.assertEqual(metrics.bus_service_class, "trunk")
            self.assertGreaterEqual(metrics.bus_priority_score, 0.85)

    def test_boarding_anchor_can_prefer_farther_stronger_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_gtfs(Path(tmp))
            index = GtfsIndex(str(path))
            anchors = index.boarding_anchors(
                (55.705, 37.605),
                (55.780, 37.750),
                limit=3,
                route_focus=1,
            )
            self.assertTrue(anchors)
            trunk = next((a for a in anchors if a.name == "Trunk"), None)
            self.assertIsNotNone(trunk)
            assert trunk is not None
            self.assertGreater(trunk.distance_from_origin_m, 2000)
            self.assertGreater(trunk.score, anchors[0].score)

    def test_legality_gate_rejects_explicit_bike_ban(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_gtfs(Path(tmp))
            planner = RoutePlanner("http://invalid.local/otp/gtfs/v1", str(path))
            banned = {"legs": [{"transitLeg": True, "tripId": "T1"}]}
            allowed = {"legs": [{"transitLeg": True, "tripId": "T2"}]}
            unknown = {"legs": [{"transitLeg": True, "tripId": "UNKNOWN"}]}
            self.assertFalse(planner._bike_carriage_is_legal(banned))
            self.assertTrue(planner._bike_carriage_is_legal(allowed))
            self.assertTrue(planner._bike_carriage_is_legal(unknown))


class FakeOTP:
    def health(self):
        return True

    def plan(self, *, departure, direct_bike=False, direct_only=False, transit_only=False, **kwargs):
        start = departure
        if direct_only:
            end = start + __import__("datetime").timedelta(seconds=900)
            return [self._bike_itinerary(start, end)], []

        nodes = []
        if direct_bike and not transit_only:
            end = start + __import__("datetime").timedelta(seconds=2700)
            nodes.append(self._bike_itinerary(start, end, distance=9000))

        transit_start = start + __import__("datetime").timedelta(seconds=120)
        transit_end = transit_start + __import__("datetime").timedelta(seconds=1200)
        nodes.append(self._transit_itinerary(transit_start, transit_end))
        return nodes, []

    @staticmethod
    def _bike_itinerary(start, end, distance=2500):
        duration = int((end - start).total_seconds())
        return {
            "duration": duration,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "generalizedCost": duration,
            "numberOfTransfers": 0,
            "waitingTime": 0,
            "walkDistance": 0,
            "legs": [{
                "mode": "BICYCLE", "transitLeg": False, "duration": duration,
                "distance": distance, "startTime": start.isoformat(), "endTime": end.isoformat(),
                "from": {"name": "A", "lat": 55.705, "lon": 37.605},
                "to": {"name": "B", "lat": 55.780, "lon": 37.750},
                "legGeometry": None, "route": None, "trip": None,
            }],
        }

    @staticmethod
    def _transit_itinerary(start, end):
        duration = int((end - start).total_seconds())
        return {
            "duration": duration,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "generalizedCost": duration,
            "numberOfTransfers": 0,
            "waitingTime": 120,
            "walkDistance": 0,
            "legs": [{
                "mode": "BUS", "transitLeg": True, "duration": duration,
                "distance": 7200, "startTime": start.isoformat(), "endTime": end.isoformat(),
                "realTime": False, "interlineWithPreviousLeg": False,
                "from": {"name": "Trunk", "lat": 55.720, "lon": 37.640},
                "to": {"name": "End", "lat": 55.760, "lon": 37.700},
                "legGeometry": None,
                "route": {"shortName": "M1", "longName": "Main", "mode": "BUS"},
                "trip": {"gtfsId": "1:T2"},
            }],
        }


class PlannerSmokeTests(GtfsIndexTests):
    def test_full_plan_pipeline_with_fake_otp(self):
        from datetime import datetime
        from routing.models import MOSCOW_TZ

        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_gtfs(Path(tmp))
            planner = RoutePlanner(
                "http://fake/otp/gtfs/v1", str(path), generic_workers=2, anchor_workers=2
            )
            planner.otp = FakeOTP()
            payload = {
                "origin": {"lat": 55.705, "lon": 37.605},
                "destination": {"lat": 55.780, "lon": 37.750},
                "departureTime": datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW_TZ).isoformat(),
                "profile": "balanced",
                "routeFocus": 0,
                "maxTransfers": 2,
                "deepSearch": True,
                "debugRouting": True,
            }
            result = planner.plan(payload)
            self.assertEqual(result["stats"]["algorithm"], "hybrid-strategy-v0.6")
            self.assertEqual(result["stats"]["routingPipelineVersion"], 7)
            self.assertTrue(result["routes"])
            self.assertGreaterEqual(len(result["routes"]), 2)
            self.assertLessEqual(len(result["routes"]), 20)
            self.assertTrue(result["stats"]["profileIgnored"])
            self.assertTrue(result["stats"]["transferSettingIgnored"])
            self.assertTrue(result["stats"]["routeFocusIgnored"])
            self.assertEqual(
                result["stats"]["optimizerFocusVariants"],
                [-2, -1, 0, 1, 2],
            )
            self.assertEqual(
                result["stats"]["bicycleStrategies"],
                ["direct", "cycleway", "quiet"],
            )
            self.assertIn("generated", result["stats"]["pipeline"])
            self.assertIn("rejected", result["stats"]["pipeline"])
            self.assertIn("pareto", result["stats"]["pipeline"])
            self.assertIn("strategies", result["stats"]["pipeline"])
            self.assertTrue(result["debugTrace"])
            for route in result["routes"]:
                self.assertIn("recommendation", route)
                self.assertIn("explanations", route)
                self.assertEqual(
                    route.get("transfers", 0),
                    len(route.get("transferPoints") or []),
                )
                self.assertLessEqual(route.get("transitTransfers", 0), 4)

            legacy_style_result = planner.plan({**payload, "profile": "calm"})
            def fingerprint(response):
                return {
                    (
                        route.get("kind"),
                        route.get("strategyArchetype"),
                        route.get("streetPreference"),
                        tuple(route.get("transitRoutes") or []),
                    )
                    for route in response["routes"]
                }

            self.assertEqual(fingerprint(result), fingerprint(legacy_style_result))
            legacy_focus_result = planner.plan({**payload, "routeFocus": 2})
            self.assertEqual(fingerprint(result), fingerprint(legacy_focus_result))


if __name__ == "__main__":
    unittest.main()
