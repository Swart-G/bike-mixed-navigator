from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from routing.diagnostics import RoutingDiagnostics
from routing.gtfs_index import Anchor, LineMetrics
from routing.models import MOSCOW_TZ, ROUTE_FOCUS_CONFIG
from routing.planner import RouteEditConflict, RoutePlanner


def geometry(offset: float = 0.0) -> dict:
    return {
        "type": "LineString",
        "coordinates": [
            [37.60 + offset, 55.70 + offset],
            [37.66 + offset, 55.74 + offset],
        ],
    }


def bike_leg(duration: int, distance: float, offset: float = 0.0) -> dict:
    return {
        "mode": "BICYCLE",
        "transitLeg": False,
        "duration": duration,
        "distance": distance,
        "from": {"name": "Bike A", "lat": 55.70, "lon": 37.60},
        "to": {"name": "Bike B", "lat": 55.74, "lon": 37.66},
        "route": None,
        "geometry": geometry(offset),
    }


def transit_leg(
    mode: str,
    duration: int,
    distance: float,
    route_name: str,
    trunk: float,
    offset: float = 0.0,
) -> dict:
    return {
        "mode": mode,
        "transitLeg": True,
        "duration": duration,
        "distance": distance,
        "tripId": f"trip-{route_name}",
        "from": {"name": f"{route_name} start", "lat": 55.70, "lon": 37.60},
        "to": {"name": f"{route_name} end", "lat": 55.74, "lon": 37.66},
        "route": {"shortName": route_name, "longName": None, "mode": mode},
        "lineMetrics": {
            "routeId": route_name,
            "mode": mode,
            "tripCount": 100,
            "medianHeadway": 420,
            "commercialSpeedKmh": 35,
            "bikesAllowedRatio": 1.0,
            "trunkScore": trunk,
        },
        "geometry": geometry(offset),
    }


def route(
    planner: RoutePlanner,
    *,
    door_to_door: int,
    legs: list[dict],
    strategy: str = "transit_optimized",
    profile: str = "fast",
) -> dict:
    has_transit = any(leg.get("transitLeg") for leg in legs)
    raw = {
        "kind": "mixed" if has_transit else "bike",
        "strategy": strategy,
        "sourceQuery": strategy,
        "duration": door_to_door,
        "initialWait": 0,
        "doorToDoor": door_to_door,
        "waitingTime": 0,
        "score": 0,
        "legs": legs,
    }
    return planner._score_route(raw, profile)


class TransitLegUtilityRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = RoutePlanner("http://invalid", "/does/not/exist.zip")
        self.weak_bus = LineMetrics(
            route_id="local",
            mode="BUS",
            trip_count=20,
            median_headway_s=12 * 60,
            commercial_speed_kmh=10,
            bikes_allowed_ratio=1.0,
            trunk_score=0.28,
        )

    def test_useless_short_bus_loses_on_complete_door_to_door_utility(self):
        decision = self.planner._transit_leg_replacement_decision(
            leg={"mode": "BUS", "distance": 900, "duration": 3 * 60},
            bike={"duration": 6 * 60, "bikeDistance": 1100},
            wait_before=5 * 60,
            line=self.weak_bus,
            downstream_trunk_score=0.0,
            route_focus=0,
        )
        self.assertTrue(decision["replace"])
        self.assertLess(decision["utilitySeconds"], decision["requiredUtilitySeconds"])
        self.assertGreater(decision["effectiveTransitSeconds"], decision["bikeEquivalentSeconds"])

    def test_bike_short_bus_bike_candidate_is_removed_by_segment_optimizer(self):
        requested = datetime(2026, 7, 27, 12, 0, tzinfo=MOSCOW_TZ)
        bus_start = requested + timedelta(minutes=10)
        bus_end = bus_start + timedelta(minutes=3)
        first_bike = {
            **bike_leg(5 * 60, 1500),
            "startTime": requested.isoformat(),
            "endTime": (requested + timedelta(minutes=5)).isoformat(),
        }
        bus = {
            **transit_leg("BUS", 3 * 60, 900, "local", 0.28),
            "tripId": "weak-trip",
            "startTime": bus_start.isoformat(),
            "endTime": bus_end.isoformat(),
        }
        last_bike = {
            **bike_leg(10 * 60, 3000),
            "startTime": bus_end.isoformat(),
            "endTime": (bus_end + timedelta(minutes=10)).isoformat(),
        }
        self.planner.gtfs.line_metrics_for_trip = lambda _trip_id: self.weak_bus
        self.planner._bike_between_leg_endpoints = lambda *_args, **_kwargs: route(
            self.planner,
            door_to_door=6 * 60,
            legs=[bike_leg(6 * 60, 1100)],
        )
        optimized, comparisons = self.planner._optimize_transit_skeleton(
            {
                "kind": "mixed",
                "strategy": "transit_skeleton",
                "sourceQuery": "test",
                "doorToDoor": 23 * 60,
                "start": requested.isoformat(),
                "end": (bus_end + timedelta(minutes=10)).isoformat(),
                "legs": [first_bike, bus, last_bike],
            },
            requested_departure=requested,
            profile="balanced",
            route_focus=0,
        )
        self.assertEqual(comparisons, 1)
        self.assertIsNone(optimized)

    def test_short_feeder_is_kept_when_bicycle_would_miss_train(self):
        train_departure = datetime(2026, 7, 27, 12, 3, tzinfo=MOSCOW_TZ)
        decision = self.planner._transit_leg_replacement_decision(
            leg={"mode": "BUS", "distance": 1000, "duration": 2 * 60},
            bike={"duration": 9 * 60, "bikeDistance": 2200},
            wait_before=5 * 60,
            line=self.weak_bus,
            downstream_trunk_score=0.92,
            route_focus=1,
            next_transit_start=train_departure,
            replacement_arrival=datetime(2026, 7, 27, 12, 4, tzinfo=MOSCOW_TZ),
            downstream_mode="RAIL",
            downstream_headway_s=20 * 60,
        )
        self.assertFalse(decision["replace"])
        self.assertTrue(decision["missesDownstreamDeparture"])
        self.assertEqual(decision["reason"], "scheduled_connection_protected")

    def test_rebuild_schedule_rejects_uncatchable_replacement(self):
        requested = datetime(2026, 7, 27, 12, 0, tzinfo=MOSCOW_TZ)
        train_start = requested + timedelta(minutes=3)
        legs = [
            bike_leg(4 * 60, 1200),
            {
                **transit_leg("RAIL", 20 * 60, 20_000, "D2", 0.92),
                "_fixedTransitStart": train_start.isoformat(),
                "_fixedTransitEnd": (train_start + timedelta(minutes=20)).isoformat(),
            },
        ]
        self.assertIsNone(
            self.planner._rebuild_schedule(legs, requested_departure=requested)
        )


class ManualBicycleReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = RoutePlanner("http://invalid", "/does/not/exist.zip")
        self.requested = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW_TZ)

    def _bike_result(self, duration: int = 8 * 60, distance: float = 2200) -> dict:
        return {
            "kind": "bike",
            "duration": duration,
            "doorToDoor": duration,
            "legs": [bike_leg(duration, distance)],
        }

    def test_manual_range_is_recalculated_as_a_bicycle_route(self):
        bus = {
            **transit_leg("BUS", 12 * 60, 4200, "m7", 0.64),
            "startTime": self.requested.isoformat(),
            "endTime": (self.requested + timedelta(minutes=12)).isoformat(),
            "from": {"name": "Старт", "lat": 55.70, "lon": 37.60},
            "to": {"name": "Финиш", "lat": 55.74, "lon": 37.66},
        }
        self.planner._bike_between_points = (
            lambda *_args, **_kwargs: self._bike_result()
        )
        result = self.planner.replace_with_bicycle(
            {
                "route": {
                    "id": "route-2",
                    "kind": "mixed",
                    "start": self.requested.isoformat(),
                    "end": (self.requested + timedelta(minutes=12)).isoformat(),
                    "doorToDoor": 12 * 60,
                    "legs": [bus],
                },
                "startBoundary": 0,
                "endBoundary": 1,
                "departureTime": "2026-07-27T08:00",
            }
        )
        edited = result["route"]
        self.assertEqual(edited["id"], "route-2")
        self.assertEqual(edited["kind"], "bike")
        self.assertTrue(all(leg["mode"] == "BICYCLE" for leg in edited["legs"]))
        self.assertEqual(edited["doorToDoor"], 8 * 60)
        self.assertEqual(edited["manualEdits"][0]["replacedLegs"], 1)
        self.assertEqual(result["edit"]["from"], "Старт")

    def test_manual_replacement_rejects_a_missed_fixed_train(self):
        feeder_end = self.requested + timedelta(minutes=2)
        train_start = self.requested + timedelta(minutes=5)
        feeder = {
            **transit_leg("BUS", 2 * 60, 1000, "local", 0.30),
            "startTime": self.requested.isoformat(),
            "endTime": feeder_end.isoformat(),
            "from": {"name": "Старт", "lat": 55.70, "lon": 37.60},
            "to": {"name": "Станция", "lat": 55.71, "lon": 37.62},
        }
        train = {
            **transit_leg("RAIL", 20 * 60, 20_000, "D2", 0.92),
            "startTime": train_start.isoformat(),
            "endTime": (train_start + timedelta(minutes=20)).isoformat(),
            "from": {"name": "Станция", "lat": 55.71, "lon": 37.62},
            "to": {"name": "Финиш", "lat": 55.82, "lon": 37.82},
        }
        self.planner._bike_between_points = (
            lambda *_args, **_kwargs: self._bike_result(duration=10 * 60)
        )
        with self.assertRaises(RouteEditConflict):
            self.planner.replace_with_bicycle(
                {
                    "route": {
                        "id": "route-1",
                        "kind": "mixed",
                        "start": self.requested.isoformat(),
                        "end": (train_start + timedelta(minutes=20)).isoformat(),
                        "doorToDoor": 25 * 60,
                        "legs": [feeder, train],
                    },
                    "startBoundary": 0,
                    "endBoundary": 1,
                    "departureTime": "2026-07-27T08:00",
                }
            )

    def test_manual_replacement_validates_boundary_order(self):
        with self.assertRaisesRegex(ValueError, "раньше"):
            self.planner.replace_with_bicycle(
                {
                    "route": {
                        "id": "route-1",
                        "doorToDoor": 300,
                        "legs": [bike_leg(300, 1000)],
                    },
                    "startBoundary": 1,
                    "endBoundary": 1,
                    "departureTime": "2026-07-27T08:00",
                }
            )

    def test_manual_replacement_never_accepts_an_automobile_leg(self):
        car = {
            **bike_leg(300, 1000),
            "mode": "CAR",
        }
        with self.assertRaisesRegex(ValueError, "Автомобильные"):
            self.planner.replace_with_bicycle(
                {
                    "route": {
                        "id": "route-1",
                        "doorToDoor": 300,
                        "legs": [car],
                    },
                    "startBoundary": 0,
                    "endBoundary": 1,
                    "departureTime": "2026-07-27T08:00",
                }
            )


class CandidateGenerationAndFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = RoutePlanner("http://invalid", "/does/not/exist.zip")

    def test_strong_trunk_access_candidate_is_generated_for_far_rail_anchor(self):
        strong_rail = LineMetrics(
            route_id="D2",
            mode="RAIL",
            trip_count=120,
            median_headway_s=6 * 60,
            commercial_speed_kmh=48,
            bikes_allowed_ratio=1.0,
            trunk_score=0.92,
        )
        self.planner.gtfs.line_metrics_for_trip = lambda _trip_id: strong_rail
        self.planner._bike_between_points = lambda *_args, **_kwargs: route(
            self.planner,
            door_to_door=11 * 60,
            legs=[bike_leg(11 * 60, 3000)],
        )

        class RailOTP:
            def plan(inner_self, *, departure, **_kwargs):
                start = departure + timedelta(minutes=2)
                end = start + timedelta(minutes=22)
                return [
                    {
                        "duration": 22 * 60,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "generalizedCost": 22 * 60,
                        "waitingTime": 2 * 60,
                        "legs": [
                            {
                                "mode": "RAIL",
                                "transitLeg": True,
                                "duration": 22 * 60,
                                "distance": 23_000,
                                "startTime": start.isoformat(),
                                "endTime": end.isoformat(),
                                "from": {
                                    "name": "Far rail",
                                    "lat": 55.72,
                                    "lon": 37.64,
                                },
                                "to": {"name": "Destination", "lat": 55.82, "lon": 37.82},
                                "route": {
                                    "shortName": "D2",
                                    "longName": None,
                                    "mode": "RAIL",
                                },
                                "trip": {"gtfsId": "trip-D2"},
                                "legGeometry": None,
                            }
                        ],
                    }
                ], []

        self.planner.otp = RailOTP()
        anchor = Anchor(
            stop_id="rail",
            name="Far rail",
            lat=55.72,
            lon=37.64,
            distance_from_origin_m=2500,
            distance_to_destination_m=20_000,
            corridor_distance_m=100,
            projection=0.12,
            route_count=1,
            modes=("RAIL",),
            best_trunk_score=0.92,
            trunk_routes=("D2",),
            score=9.0,
        )
        departure = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW_TZ)

        transit_heavy = self.planner._boarding_anchor_routes(
            anchor,
            (55.70, 37.60),
            (55.82, 37.82),
            departure,
            "balanced",
            2,
            -2,
        )
        bike_heavy = self.planner._boarding_anchor_routes(
            anchor,
            (55.70, 37.60),
            (55.82, 37.82),
            departure,
            "balanced",
            2,
            2,
        )

        # Actual street distance (3 km) is outside transit-heavy access policy,
        # while bike-heavy generation explicitly reaches this strong rail anchor.
        self.assertEqual(transit_heavy, [])
        self.assertTrue(bike_heavy)
        self.assertTrue(any(r["strategy"] == "trunk_access" for r in bike_heavy))

    def test_extreme_focus_values_select_structurally_different_strategies(self):
        direct = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[bike_leg(30 * 60, 12_000)],
            strategy="direct_bike",
        )
        transit_heavy = route(
            self.planner,
            door_to_door=34 * 60,
            legs=[
                bike_leg(3 * 60, 900),
                transit_leg("RAIL", 24 * 60, 24_000, "D2", 0.92),
                bike_leg(3 * 60, 900),
            ],
            strategy="trunk_access",
        )
        bike_heavy = route(
            self.planner,
            door_to_door=33 * 60,
            legs=[
                bike_leg(24 * 60, 14_000, 0.01),
                transit_leg("RAIL", 6 * 60, 5000, "D1", 0.80, 0.01),
                bike_leg(3 * 60, 1500, 0.01),
            ],
        )
        candidates = [direct, transit_heavy, bike_heavy]

        def selected(focus: int) -> list[dict]:
            focused = self.planner._apply_route_focus(
                [dict(r) for r in candidates],
                profile_key="fast",
                route_focus=focus,
            )
            focused = self.planner._classify_strategies(focused, route_focus=focus)
            focused = self.planner._pareto_prune(focused, route_focus=focus)
            return self.planner._select_diverse(focused, limit=2, route_focus=focus)

        transit_result = selected(-2)
        bike_result = selected(2)
        self.assertTrue(
            any("TRANSIT_HEAVY" in r["archetypes"] for r in transit_result)
        )
        self.assertTrue(any("BIKE_HEAVY" in r["archetypes"] for r in bike_result))
        self.assertNotEqual(
            {r["strategyArchetype"] for r in transit_result},
            {r["strategyArchetype"] for r in bike_result},
        )
        self.assertGreater(
            sum(r["bikeShare"] for r in bike_result),
            sum(r["bikeShare"] for r in transit_result),
        )

    def test_multiple_bicycle_street_strategies_survive_single_route_clustering(self):
        candidates = []
        for preference, strategy, offset, duration in (
            ("direct", "bike_direct", 0.000, 30 * 60),
            ("cycleway", "bike_cycleway", 0.004, 32 * 60),
            ("quiet", "bike_quiet", -0.004, 34 * 60),
        ):
            candidate = route(
                self.planner,
                door_to_door=duration,
                legs=[bike_leg(duration, 12_000, offset)],
                strategy=strategy,
            )
            candidate["streetPreference"] = preference
            candidates.append(candidate)

        focused = self.planner._apply_route_focus(
            candidates,
            profile_key="balanced",
            route_focus=0,
        )
        classified = self.planner._classify_strategies(focused, route_focus=0)
        pareto = self.planner._pareto_prune(classified, route_focus=0)
        selected = self.planner._select_diverse(pareto, limit=8, route_focus=0)

        self.assertGreaterEqual(len(selected), 2)
        self.assertGreaterEqual(
            len({r.get("streetPreference") for r in selected}),
            2,
        )

    def test_automobile_street_leg_is_never_a_valid_candidate(self):
        car_route = {
            "doorToDoor": 600,
            "legs": [{"mode": "CAR", "duration": 600, "distance": 5000}],
        }
        bicycle_route = {
            "doorToDoor": 900,
            "legs": [{"mode": "BICYCLE", "duration": 900, "distance": 4000}],
        }
        self.assertFalse(self.planner._candidate_is_valid(car_route))
        self.assertTrue(self.planner._candidate_is_valid(bicycle_route))

    def test_bike_heavy_generation_keeps_full_skeletons_before_optimization(self):
        calls = []

        class CaptureOTP:
            def plan(inner_self, **kwargs):
                calls.append(kwargs)
                return [], []

        self.planner.otp = CaptureOTP()
        departure = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW_TZ)
        _routes, _warnings, stats = self.planner._transit_first_candidates(
            (55.70, 37.60),
            (55.82, 37.82),
            departure,
            "balanced",
            4,
            2,
        )

        self.assertEqual(stats["queries"], len(calls))
        self.assertIn(4, {call["max_transfers"] for call in calls})
        self.assertIn(0, {call["max_transfers"] for call in calls})
        self.assertTrue(
            any(
                int((call["departure"] - departure).total_seconds()) == 12 * 60
                for call in calls
            )
        )

    def test_generic_multimodal_search_uses_all_bicycle_street_hypotheses(self):
        calls = []

        class CaptureOTP:
            def plan(inner_self, **kwargs):
                calls.append(kwargs)
                return [], []

        self.planner.otp = CaptureOTP()
        departure = datetime(2026, 7, 27, 8, 0, tzinfo=MOSCOW_TZ)
        self.planner._generic_candidates(
            (55.70, 37.60),
            (55.82, 37.82),
            departure,
            "balanced",
            4,
            0,
        )
        multimodal_all = [
            call
            for call in calls
            if call.get("transit_modes")
            and set(call["transit_modes"]) == {
                "BUS",
                "TRAM",
                "TROLLEYBUS",
                "RAIL",
                "SUBWAY",
            }
        ]

        self.assertEqual(
            {call["profile"] for call in multimodal_all},
            {"fast", "balanced", "calm"},
        )

    def test_extreme_focus_configs_have_disjoint_optimizer_families(self):
        transit = ROUTE_FOCUS_CONFIG[-2]
        bike = ROUTE_FOCUS_CONFIG[2]

        self.assertLess(transit.max_bike_access_m, bike.max_bike_access_m)
        self.assertLess(transit.max_bike_egress_m, bike.max_bike_egress_m)
        self.assertTrue(
            set(transit.optimizer_focus_variants).isdisjoint(
                bike.optimizer_focus_variants
            )
        )
        self.assertIn(4, bike.transit_transfer_caps)

    def test_strategy_group_distinguishes_skipping_an_intermediate_line(self):
        with_local = route(
            self.planner,
            door_to_door=35 * 60,
            legs=[
                transit_leg("BUS", 8 * 60, 3000, "A", 0.50),
                transit_leg("TRAM", 12 * 60, 6000, "T", 0.72),
                transit_leg("BUS", 10 * 60, 9000, "C", 0.66),
            ],
        )
        without_local = route(
            self.planner,
            door_to_door=32 * 60,
            legs=[
                transit_leg("BUS", 8 * 60, 3000, "A", 0.50),
                transit_leg("BUS", 18 * 60, 9000, "C", 0.66),
            ],
        )

        self.assertNotEqual(
            self.planner._strategy_group(with_local),
            self.planner._strategy_group(without_local),
        )

    def test_route_card_counts_every_visible_segment_transition(self):
        legs = [
            bike_leg(5 * 60, 3700),
            transit_leg("BUS", 32 * 60, 8200, "c977", 0.62),
            bike_leg(12 * 60, 1600, 0.002),
            transit_leg("BUS", 9 * 60, 2800, "912", 0.48, 0.003),
            transit_leg("BUS", 29 * 60, 18_700, "e132", 0.75, 0.004),
            bike_leg(9 * 60, 1400, 0.005),
        ]
        candidate = route(
            self.planner,
            door_to_door=2 * 60 * 60 + 27 * 60,
            legs=legs,
        )

        self.assertEqual(candidate["transfers"], 5)
        self.assertEqual(candidate["routeTransitions"], 5)
        self.assertEqual(candidate["transitTransfers"], 2)
        self.assertEqual(len(candidate["transferPoints"]), 5)
        self.assertEqual(
            [point["index"] for point in candidate["transferPoints"]],
            [1, 2, 3, 4, 5],
        )

    def test_transition_count_does_not_depend_on_map_coordinates(self):
        legs = [
            {
                "mode": "BICYCLE",
                "transitLeg": False,
                "duration": 300,
                "distance": 1200,
            },
            {
                "mode": "BUS",
                "transitLeg": True,
                "duration": 600,
                "distance": 4000,
                "tripId": "trip-1",
                "route": {"shortName": "1"},
            },
        ]
        candidate = route(self.planner, door_to_door=900, legs=legs)

        self.assertEqual(candidate["transfers"], 1)
        self.assertEqual(candidate["transferPoints"][0]["lat"], None)
        self.assertEqual(candidate["transferPoints"][0]["lon"], None)

    def test_diversity_fills_ten_slots_when_ten_strategies_are_available(self):
        candidates = [
            route(
                self.planner,
                door_to_door=(30 + index) * 60,
                legs=[
                    transit_leg(
                        "BUS",
                        (25 + index) * 60,
                        12_000 + index * 500,
                        f"M{index}",
                        0.55 + index * 0.01,
                        index * 0.008,
                    )
                ],
                strategy=f"corridor_{index}",
            )
            for index in range(12)
        ]
        classified = self.planner._classify_strategies(candidates, route_focus=0)
        selected = self.planner._select_diverse(
            classified,
            limit=10,
            route_focus=0,
        )

        self.assertEqual(len(selected), 10)
        self.assertEqual(len({tuple(r["transitRoutes"]) for r in selected}), 10)

    def test_diversity_does_not_restore_a_duplicate_just_to_inflate_count(self):
        direct = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[bike_leg(30 * 60, 12_000)],
            strategy="bike_direct",
        )
        direct["streetPreference"] = "direct"
        cycleway = route(
            self.planner,
            door_to_door=31 * 60,
            legs=[bike_leg(31 * 60, 12_100)],
            strategy="bike_cycleway",
        )
        cycleway["streetPreference"] = "cycleway"
        classified = self.planner._classify_strategies(
            [direct, cycleway],
            route_focus=0,
        )

        selected = self.planner._select_diverse(
            classified,
            limit=10,
            route_focus=0,
        )

        self.assertEqual(len(selected), 1)


class StrategyPreservationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = RoutePlanner("http://invalid", "/does/not/exist.zip")
        self.direct = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[bike_leg(30 * 60, 11_000)],
            strategy="direct_bike",
        )
        self.rail = route(
            self.planner,
            door_to_door=34 * 60,
            legs=[
                bike_leg(3 * 60, 800),
                transit_leg("RAIL", 25 * 60, 22_000, "D2", 0.92),
                bike_leg(3 * 60, 800),
            ],
            strategy="trunk_access",
        )

    def _pipeline(self, routes: list[dict], focus: int, limit: int = 8) -> list[dict]:
        focused = self.planner._apply_route_focus(
            routes,
            profile_key="fast",
            route_focus=focus,
        )
        classified = self.planner._classify_strategies(focused, route_focus=focus)
        pareto = self.planner._pareto_prune(classified, route_focus=focus)
        return self.planner._select_diverse(pareto, limit=limit, route_focus=focus)

    def test_fast_transit_focus_keeps_transit_when_direct_bike_is_faster(self):
        selected = self._pipeline([self.direct, self.rail], -2)
        self.assertIn("bike", {r["kind"] for r in selected})
        self.assertIn("mixed", {r["kind"] for r in selected})

    def test_full_generation_pipeline_keeps_reasonable_transit_alternative(self):
        strong = LineMetrics(
            route_id="D2",
            mode="RAIL",
            trip_count=120,
            median_headway_s=6 * 60,
            commercial_speed_kmh=48,
            bikes_allowed_ratio=1.0,
            trunk_score=0.92,
        )
        self.planner.gtfs.line_metrics_for_trip = lambda _trip_id: strong

        class StrongTransitOTP:
            def plan(inner_self, *, departure, direct_only=False, **_kwargs):
                if direct_only:
                    end = departure + timedelta(minutes=30)
                    return [
                        {
                            "duration": 30 * 60,
                            "start": departure.isoformat(),
                            "end": end.isoformat(),
                            "generalizedCost": 30 * 60,
                            "waitingTime": 0,
                            "legs": [
                                {
                                    "mode": "BICYCLE",
                                    "transitLeg": False,
                                    "duration": 30 * 60,
                                    "distance": 12_000,
                                    "startTime": departure.isoformat(),
                                    "endTime": end.isoformat(),
                                    "from": {"name": "A", "lat": 55.70, "lon": 37.60},
                                    "to": {"name": "B", "lat": 55.82, "lon": 37.82},
                                    "route": None,
                                    "trip": None,
                                    "legGeometry": None,
                                }
                            ],
                        }
                    ], []
                start = departure + timedelta(minutes=2)
                end = start + timedelta(minutes=32)
                return [
                    {
                        "duration": 32 * 60,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "generalizedCost": 32 * 60,
                        "waitingTime": 2 * 60,
                        "legs": [
                            {
                                "mode": "RAIL",
                                "transitLeg": True,
                                "duration": 32 * 60,
                                "distance": 24_000,
                                "startTime": start.isoformat(),
                                "endTime": end.isoformat(),
                                "from": {"name": "Rail A", "lat": 55.70, "lon": 37.60},
                                "to": {"name": "Rail B", "lat": 55.82, "lon": 37.82},
                                "route": {
                                    "shortName": "D2",
                                    "longName": None,
                                    "mode": "RAIL",
                                },
                                "trip": {"gtfsId": "trip-D2"},
                                "legGeometry": None,
                            }
                        ],
                    }
                ], []

        self.planner.otp = StrongTransitOTP()
        result = self.planner.plan(
            {
                "origin": {"lat": 55.70, "lon": 37.60},
                "destination": {"lat": 55.82, "lon": 37.82},
                "departureTime": datetime(
                    2026,
                    7,
                    27,
                    8,
                    0,
                    tzinfo=MOSCOW_TZ,
                ).isoformat(),
                "profile": "fast",
                "routeFocus": -2,
                "maxTransfers": 2,
                "deepSearch": False,
            }
        )
        self.assertTrue(any(r["kind"] == "bike" for r in result["routes"]))
        self.assertTrue(any("RAIL" in r["archetypes"] for r in result["routes"]))

    def test_balanced_transit_heavy_result_is_not_bicycle_only(self):
        direct = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[bike_leg(30 * 60, 11_000)],
            strategy="direct_bike",
            profile="balanced",
        )
        rail = route(
            self.planner,
            door_to_door=36 * 60,
            legs=[
                bike_leg(4 * 60, 1000),
                transit_leg("RAIL", 25 * 60, 22_000, "D2", 0.92),
                bike_leg(4 * 60, 1000),
            ],
            strategy="trunk_access",
            profile="balanced",
        )
        focused = self.planner._apply_route_focus(
            [direct, rail],
            profile_key="balanced",
            route_focus=-2,
        )
        classified = self.planner._classify_strategies(focused, route_focus=-2)
        selected = self.planner._select_diverse(
            self.planner._pareto_prune(classified, route_focus=-2),
            limit=8,
            route_focus=-2,
        )
        self.assertTrue(any(r["kind"] == "mixed" for r in selected))

    def test_pareto_preserves_rail_archetype_even_if_direct_bike_dominates(self):
        weak_bus = route(
            self.planner,
            door_to_door=39 * 60,
            legs=[transit_leg("BUS", 30 * 60, 10_000, "123", 0.30)],
        )
        focused = self.planner._apply_route_focus(
            [self.direct, self.rail, weak_bus],
            profile_key="fast",
            route_focus=-2,
        )
        classified = self.planner._classify_strategies(focused, route_focus=-2)
        diagnostics = RoutingDiagnostics(enabled=True)
        kept = self.planner._pareto_prune(
            classified,
            route_focus=-2,
            diagnostics=diagnostics,
        )
        rail = next(r for r in kept if "RAIL" in r["archetypes"])
        self.assertEqual(rail["paretoStatus"], "strategy_preserved")
        self.assertEqual(diagnostics.pareto_before, 3)
        self.assertGreaterEqual(diagnostics.pareto_after, 2)

    def test_public_transport_and_direct_bicycle_extremes_are_both_preserved(self):
        public_transport = route(
            self.planner,
            door_to_door=38 * 60,
            legs=[transit_leg("BUS", 34 * 60, 16_000, "м1", 0.82)],
            strategy="public_transport",
        )
        classified = self.planner._classify_strategies(
            [self.direct, public_transport],
            route_focus=0,
        )
        selected = self.planner._select_diverse(
            self.planner._pareto_prune(classified, route_focus=0),
            limit=8,
            route_focus=0,
        )

        self.assertTrue(any(r["kind"] == "bike" for r in selected))
        self.assertTrue(
            any("PUBLIC_TRANSPORT" in r["archetypes"] for r in selected)
        )

    def test_express_bus_gets_priority_over_equally_fast_local_bus(self):
        local_leg = transit_leg("BUS", 24 * 60, 14_000, "123", 0.68)
        express_leg = transit_leg("BUS", 24 * 60, 14_000, "е10", 0.82, 0.02)
        express_leg["lineMetrics"].update(
            {
                "busServiceClass": "express",
                "busPriorityScore": 1.0,
            }
        )
        local = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[local_leg],
            strategy="public_transport",
        )
        express = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[express_leg],
            strategy="public_transport",
        )
        classified = self.planner._classify_strategies(
            [local, express],
            route_focus=0,
        )

        self.assertLess(express["score"], local["score"])
        self.assertIn("RAPID_BUS", express["archetypes"])

    def test_pareto_preserves_distinct_surface_corridors_with_same_archetype(self):
        corridor_a = route(
            self.planner,
            door_to_door=30 * 60,
            legs=[transit_leg("BUS", 25 * 60, 14_000, "A", 0.58)],
        )
        corridor_b = route(
            self.planner,
            door_to_door=34 * 60,
            legs=[transit_leg("BUS", 28 * 60, 15_000, "B", 0.58, 0.02)],
        )
        slower_a = route(
            self.planner,
            door_to_door=38 * 60,
            legs=[transit_leg("BUS", 32 * 60, 14_000, "A", 0.58)],
        )
        focused = self.planner._apply_route_focus(
            [corridor_a, corridor_b, slower_a],
            profile_key="balanced",
            route_focus=0,
        )
        classified = self.planner._classify_strategies(focused, route_focus=0)
        kept = self.planner._pareto_prune(classified, route_focus=0)

        kept_lines = {
            tuple(candidate.get("transitRoutes") or [])
            for candidate in kept
        }
        self.assertIn(("A",), kept_lines)
        self.assertIn(("B",), kept_lines)

    def test_same_corridor_rail_and_bus_are_not_collapsed_as_same_strategy(self):
        rail = route(
            self.planner,
            door_to_door=34 * 60,
            legs=[bike_leg(4 * 60, 1000), transit_leg("RAIL", 25 * 60, 22_000, "D2", 0.9)],
            strategy="trunk_access",
        )
        bus = route(
            self.planner,
            door_to_door=35 * 60,
            legs=[bike_leg(4 * 60, 1000), transit_leg("BUS", 26 * 60, 21_000, "M1", 0.75)],
        )
        classified = self.planner._classify_strategies([rail, bus], route_focus=0)
        self.assertLess(self.planner._route_similarity(*classified), 0.80)


if __name__ == "__main__":
    unittest.main()
