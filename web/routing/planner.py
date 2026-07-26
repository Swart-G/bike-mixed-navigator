from __future__ import annotations

import concurrent.futures
import itertools
import math
import time
import threading
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .gtfs_index import Anchor, GtfsIndex
from .models import (
    PROFILE_CONFIG,
    ROUTE_FOCUS_CONFIG,
    MOSCOW_TZ,
    decode_polyline,
    normalized_signature,
    parse_coordinate,
    parse_departure,
    parse_otp_time,
    transit_modes,
    transit_route_names,
)
from .otp_client import OTPClient, coordinate_location, stop_location


ALL_TRANSIT = ["BUS", "TRAM", "TROLLEYBUS", "RAIL"]
MODE_QUERIES = {
    "bus": ["BUS", "TROLLEYBUS"],
    "tram": ["TRAM"],
    "rail": ["RAIL"],
    "bus_tram": ["BUS", "TROLLEYBUS", "TRAM"],
    "bus_rail": ["BUS", "TROLLEYBUS", "RAIL"],
    "tram_rail": ["TRAM", "RAIL"],
}

# Normal outcomes for exploratory candidate queries. A BUS/TRAM/RAIL-only
# search can legitimately fail while the overall multimodal planner succeeds.
SOFT_ROUTING_ERRORS = {
    "NO_STOPS_IN_RANGE",
    "WALKING_BETTER_THAN_TRANSIT",
    "NO_TRANSIT_CONNECTION",
    "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW",
}


class RoutePlanner:
    def __init__(
        self,
        otp_url: str,
        gtfs_path: str,
        feed_id: str = "1",
        generic_workers: int = 6,
        anchor_workers: int = 4,
    ) -> None:
        self.otp = OTPClient(otp_url)
        self.gtfs = GtfsIndex(gtfs_path)
        self.feed_id = feed_id
        self.generic_workers = generic_workers
        self.anchor_workers = anchor_workers
        self._bike_cache: dict[tuple, dict[str, Any] | None] = {}
        self._bike_cache_lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        otp_ok = self.otp.health()
        # GTFS index is lazy but health is a convenient place to warm it up.
        self.gtfs.ensure_loaded()
        return {
            "otp": otp_ok,
            "gtfsIndex": {
                "loaded": self.gtfs.loaded,
                "stopCount": self.gtfs.stop_count if self.gtfs.loaded else 0,
                "error": self.gtfs.error,
            },
        }

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        origin = parse_coordinate(payload.get("origin"), "Старт")
        destination = parse_coordinate(payload.get("destination"), "Финиш")
        departure = parse_departure(payload.get("departureTime"))
        profile = payload.get("profile", "balanced")
        if profile not in PROFILE_CONFIG:
            profile = "balanced"
        try:
            max_transfers = min(5, max(2, int(payload.get("maxTransfers", 2))))
        except (TypeError, ValueError):
            max_transfers = 2

        try:
            route_focus = min(2, max(-2, int(payload.get("routeFocus", 0))))
        except (TypeError, ValueError):
            route_focus = 0
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]

        generic_routes, warnings, query_stats = self._generic_candidates(
            origin, destination, departure, profile, max_transfers
        )

        transit_first_routes, transit_first_warnings, transit_first_stats = (
            self._transit_first_candidates(
                origin,
                destination,
                departure,
                profile,
                max_transfers,
                route_focus,
            )
        )
        warnings.extend(transit_first_warnings)

        anchor_routes: list[dict[str, Any]] = []
        anchors: list[Anchor] = []
        anchor_error = None
        if payload.get("deepSearch", True):
            try:
                anchors = self.gtfs.egress_anchors(
                    origin,
                    destination,
                    limit=focus_cfg["anchor_limit"],
                    route_focus=route_focus,
                )
                anchor_routes = self._egress_anchor_candidates(
                    anchors, origin, destination, departure, profile, max_transfers
                )
            except Exception as exc:
                # Deep search must never break basic routing.
                anchor_error = str(exc)

        # When transit-first optimization works, raw OTP BIKE+TRANSIT candidates are only
        # useful for the direct bicycle baseline. Otherwise they can re-introduce exactly the
        # short, inefficient transit legs that the optimizer removed.
        generic_for_merge = [r for r in generic_routes if r.get("kind") == "bike"]
        if not transit_first_routes:
            generic_for_merge = generic_routes

        all_candidates = self._dedupe_exact(
            generic_for_merge + transit_first_routes + anchor_routes
        )

        # Recompute transfer counts after every transformation/composition and
        # enforce the user's setting locally as a hard constraint. This is
        # intentionally independent of OTP's own maximumTransfers request.
        before_transfer_filter = len(all_candidates)
        all_candidates = [
            self._refresh_route_metrics(route)
            for route in all_candidates
        ]
        all_candidates = [
            route
            for route in all_candidates
            if int(route.get("transfers") or 0) <= max_transfers
        ]
        transfer_filtered = before_transfer_filter - len(all_candidates)

        all_candidates = [self._score_route(r, profile) for r in all_candidates]
        all_candidates = self._apply_route_focus(
            all_candidates,
            profile_key=profile,
            route_focus=route_focus,
        )
        selected = self._select_diverse(
            all_candidates,
            limit=10,
            route_focus=route_focus,
        )
        self._assign_recommendation_labels(selected)

        # We intentionally run several narrow exploratory searches. A failure
        # in one of them is not a route-planning failure. Suppress these soft
        # diagnostics whenever the combined search produced usable routes.
        if selected:
            warnings = [
                warning
                for warning in warnings
                if warning.get("code") not in SOFT_ROUTING_ERRORS
            ]

        return {
            "routes": selected,
            "warnings": warnings,
            "stats": {
                "genericQueries": query_stats["queries"],
                "genericCandidates": len(generic_routes),
                "transitSkeletonQueries": transit_first_stats["queries"],
                "transitSkeletons": transit_first_stats["skeletons"],
                "transitOptimizedCandidates": len(transit_first_routes),
                "bikeComparisons": transit_first_stats["bikeComparisons"],
                "anchorsConsidered": len(anchors),
                "anchorCandidates": len(anchor_routes),
                "candidatesTotal": len(all_candidates),
                "transferFiltered": transfer_filtered,
                "maxTransfers": max_transfers,
                "routeFocus": route_focus,
                "routeFocusName": focus_cfg["name"],
                "returned": len(selected),
                "deepSearchAvailable": self.gtfs.loaded,
                "deepSearchError": anchor_error or self.gtfs.error,
                "elapsedMs": round((time.monotonic() - started) * 1000),
            },
        }

    def _generic_candidates(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        selected_profile: str,
        max_transfers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        specs: list[dict[str, Any]] = [
            {
                "name": "main",
                "profile": selected_profile,
                "modes": ALL_TRANSIT,
                "max_transfers": max_transfers,
                "direct_bike": True,
                "strategy": "generic",
            },
            {
                "name": "fast",
                "profile": "fast",
                "modes": ALL_TRANSIT,
                "max_transfers": max_transfers,
                "strategy": "profile_fast",
            },
            {
                "name": "calm",
                "profile": "calm",
                "modes": ALL_TRANSIT,
                "max_transfers": max_transfers,
                "strategy": "profile_calm",
            },
            {
                "name": "bus",
                "profile": selected_profile,
                "modes": MODE_QUERIES["bus"],
                "max_transfers": max_transfers,
                "strategy": "mode_bus",
            },
            {
                "name": "tram",
                "profile": selected_profile,
                "modes": MODE_QUERIES["tram"],
                "max_transfers": max_transfers,
                "strategy": "mode_tram",
            },
            {
                "name": "rail",
                "profile": selected_profile,
                "modes": MODE_QUERIES["rail"],
                "max_transfers": max_transfers,
                "strategy": "mode_rail",
            },
            {
                "name": "bus_tram",
                "profile": selected_profile,
                "modes": MODE_QUERIES["bus_tram"],
                "max_transfers": max_transfers,
                "strategy": "mode_bus_tram",
            },
            {
                "name": "bus_rail",
                "profile": selected_profile,
                "modes": MODE_QUERIES["bus_rail"],
                "max_transfers": max_transfers,
                "strategy": "mode_bus_rail",
            },
            {
                "name": "tram_rail",
                "profile": selected_profile,
                "modes": MODE_QUERIES["tram_rail"],
                "max_transfers": max_transfers,
                "strategy": "mode_tram_rail",
            },
            {
                "name": "simple",
                "profile": selected_profile,
                "modes": ALL_TRANSIT,
                "max_transfers": min(1, max_transfers),
                "strategy": "simple_transit",
            },
        ]

        origin_loc = coordinate_location(*origin, "Старт")
        destination_loc = coordinate_location(*destination, "Финиш")
        routes: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def run(spec: dict[str, Any]):
            nodes, local_warnings = self.otp.plan(
                origin=origin_loc,
                destination=destination_loc,
                departure=departure,
                profile=spec["profile"],
                transit_modes=spec["modes"],
                max_transfers=spec["max_transfers"],
                direct_bike=spec.get("direct_bike", False),
                transit_only=not spec.get("direct_bike", False),
                first=14,
            )
            local_routes = []
            for node in nodes:
                route = self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=selected_profile,
                    strategy=spec["strategy"],
                    source_query=spec["name"],
                )
                if route["kind"] != "other":
                    local_routes.append(route)
            return local_routes, local_warnings

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(run, spec) for spec in specs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    local_routes, local_warnings = future.result()
                    routes.extend(local_routes)
                    warnings.extend(local_warnings)
                except Exception as exc:
                    warnings.append({"code": "CANDIDATE_QUERY_FAILED", "description": str(exc)})

        return self._dedupe_exact(routes), self._dedupe_warnings(warnings), {"queries": len(specs)}

    def _transit_first_candidates(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
        route_focus: int = 0,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        """Build public-transport skeletons first, then optimize every segment with bicycle.

        This is deliberately the inverse of the generic OTP BIKE+TRANSIT request. OTP first
        thinks like a normal public transport planner (WALK access/transfer/egress). Then we
        independently ask whether each walking or transit leg is better done by bicycle.
        """
        specs: list[dict[str, Any]] = [
            {"name": "pt_all", "modes": ALL_TRANSIT, "max_transfers": max_transfers},
            {"name": "pt_bus", "modes": MODE_QUERIES["bus"], "max_transfers": max_transfers},
            {"name": "pt_tram", "modes": MODE_QUERIES["tram"], "max_transfers": max_transfers},
            {"name": "pt_rail", "modes": MODE_QUERIES["rail"], "max_transfers": max_transfers},
            {"name": "pt_bus_tram", "modes": MODE_QUERIES["bus_tram"], "max_transfers": max_transfers},
            {"name": "pt_bus_rail", "modes": MODE_QUERIES["bus_rail"], "max_transfers": max_transfers},
            {"name": "pt_tram_rail", "modes": MODE_QUERIES["tram_rail"], "max_transfers": max_transfers},
            {"name": "pt_simple", "modes": ALL_TRANSIT, "max_transfers": min(1, max_transfers)},
        ]

        origin_loc = coordinate_location(*origin, "Старт")
        destination_loc = coordinate_location(*destination, "Финиш")
        skeletons: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def run(spec: dict[str, Any]):
            nodes, local_warnings = self.otp.plan(
                origin=origin_loc,
                destination=destination_loc,
                departure=departure,
                profile=profile,
                transit_modes=spec["modes"],
                max_transfers=spec["max_transfers"],
                transit_only=True,
                access_mode="WALK",
                egress_mode="WALK",
                transfer_mode="WALK",
                first=12,
            )
            local = []
            for node in nodes:
                route = self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="transit_skeleton",
                    source_query=spec["name"],
                )
                if route["kind"] == "mixed":
                    local.append(route)
            return local, local_warnings

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(run, spec) for spec in specs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    local, local_warnings = future.result()
                    skeletons.extend(local)
                    warnings.extend(local_warnings)
                except Exception as exc:
                    warnings.append(
                        {"code": "TRANSIT_SKELETON_QUERY_FAILED", "description": str(exc)}
                    )

        skeletons = self._dedupe_exact(skeletons)
        # Keep transit diversity before the expensive bicycle comparison stage.
        preselected: list[dict[str, Any]] = []
        seen_signatures: set[tuple] = set()
        for route in sorted(skeletons, key=lambda r: (r["doorToDoor"], r["score"])):
            signature = normalized_signature(route)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            preselected.append(route)
            if len(preselected) >= 24:
                break

        comparison_counter = [0]
        counter_lock = threading.Lock()

        def optimize(route: dict[str, Any]):
            optimized, comparisons = self._optimize_transit_skeleton(
                route,
                departure,
                profile,
                route_focus,
            )
            with counter_lock:
                comparison_counter[0] += comparisons
            return optimized

        optimized_routes: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(optimize, route) for route in preselected]
            for future in concurrent.futures.as_completed(futures):
                try:
                    route = future.result()
                    if route is not None:
                        optimized_routes.append(route)
                except Exception as exc:
                    warnings.append(
                        {"code": "TRANSIT_OPTIMIZER_FAILED", "description": str(exc)}
                    )

        return (
            self._dedupe_exact(optimized_routes),
            self._dedupe_warnings(warnings),
            {
                "queries": len(specs),
                "skeletons": len(preselected),
                "bikeComparisons": comparison_counter[0],
            },
        )

    def _optimize_transit_skeleton(
        self,
        skeleton: dict[str, Any],
        requested_departure: datetime,
        profile: str,
        route_focus: int = 0,
    ) -> tuple[dict[str, Any] | None, int]:
        """Replace WALK and inefficient transit legs with direct bicycle legs.

        Transit legs keep their original scheduled departure/arrival times. A replacement is
        accepted only when bicycle reaches the same endpoint earlier than the original segment
        (including the wait before boarding). This means downstream scheduled legs remain valid.
        """
        original_legs = deepcopy(skeleton.get("legs") or [])
        if not original_legs:
            return None, 0

        cfg = PROFILE_CONFIG[profile]
        route_focus = min(2, max(-2, int(route_focus)))

        distance_factor = {
            -2: 0.45,
            -1: 0.70,
            0: 1.00,
            1: 1.45,
            2: 2.10,
        }[route_focus]
        duration_factor = {
            -2: 0.65,
            -1: 0.80,
            0: 1.00,
            1: 1.30,
            2: 1.65,
        }[route_focus]
        saving_factor = {
            -2: 1.50,
            -1: 1.20,
            0: 1.00,
            1: 0.80,
            2: 0.60,
        }[route_focus]

        transit_replace_max_distance = (
            float(cfg["transit_replace_max_bike_distance"]) * distance_factor
        )
        transit_replace_max_duration = (
            float(cfg["transit_replace_max_bike_duration"]) * duration_factor
        )
        transit_replace_min_saving = (
            float(cfg["transit_replace_min_saving"]) * saving_factor
        )

        comparisons = 0
        choices: list[dict[str, Any]] = []
        replaced_walk = 0
        replaced_transit: list[dict[str, Any]] = []
        previous_original_end = parse_otp_time(skeleton.get("start")) or requested_departure

        for index, leg in enumerate(original_legs):
            mode = leg.get("mode") or "UNKNOWN"
            is_transit = bool(leg.get("transitLeg"))
            duration = int(leg.get("duration") or 0)
            distance = float(leg.get("distance") or 0)
            leg_start = self._parse_leg_timestamp(leg.get("startTime"))
            leg_end = self._parse_leg_timestamp(leg.get("endTime"))
            wait_before = 0
            if is_transit and leg_start is not None:
                wait_before = max(0, int((leg_start - previous_original_end).total_seconds()))

            should_compare = False
            if not is_transit and mode == "WALK" and distance >= 80:
                should_compare = True
            elif is_transit:
                # Cheap heuristic prevents pointless OTP calls for obviously fast, long transit.
                estimated_bike = distance / 4.4 if distance > 0 else duration
                effective_transit = wait_before + duration + 45
                if distance <= 6500 or estimated_bike <= effective_transit * 1.35:
                    should_compare = True

            bike = None
            if should_compare:
                bike = self._bike_between_leg_endpoints(leg, requested_departure, profile)
                comparisons += 1

            replace = False
            saved_seconds = 0
            if bike is not None:
                bike_duration = int(bike.get("duration") or 0)
                if not is_transit and mode == "WALK":
                    # Small margin accounts for mounting/dismounting the bicycle.
                    replace = bike_duration + 20 < duration
                    saved_seconds = duration - bike_duration
                elif is_transit:
                    effective_transit = wait_before + duration + 45
                    bike_distance = float(bike.get("bikeDistance") or 0)

                    # Public transport is the skeleton of the route. We only remove a transit
                    # leg when it is a genuinely short "micro-leg" that is quicker to cycle.
                    # Long BUS/TRAM/RAIL legs are preserved even when a straight bicycle path
                    # between their endpoints is mathematically faster. Long bicycle egress is
                    # handled by the dedicated egress-anchor search instead.
                    short_enough = (
                        bike_distance <= transit_replace_max_distance
                        and bike_duration <= transit_replace_max_duration
                    )
                    enough_saving = (
                        effective_transit - bike_duration
                        >= transit_replace_min_saving
                    )
                    replace = short_enough and enough_saving
                    saved_seconds = effective_transit - bike_duration

            if replace and bike is not None:
                replacement_legs = deepcopy(bike.get("legs") or [])
                for repl in replacement_legs:
                    repl["replaces"] = {
                        "mode": mode,
                        "route": (leg.get("route") or {}).get("shortName")
                        or (leg.get("route") or {}).get("longName"),
                        "savedSeconds": max(0, int(saved_seconds)),
                    }
                    repl["transitLeg"] = False
                choices.extend(replacement_legs)
                if is_transit:
                    replaced_transit.append(
                        {
                            "mode": mode,
                            "route": (leg.get("route") or {}).get("shortName")
                            or (leg.get("route") or {}).get("longName"),
                            "distance": round(distance, 1),
                            "savedSeconds": max(0, int(saved_seconds)),
                        }
                    )
                else:
                    replaced_walk += 1
            else:
                kept = deepcopy(leg)
                kept["_fixedTransitStart"] = leg_start.isoformat() if is_transit and leg_start else None
                kept["_fixedTransitEnd"] = leg_end.isoformat() if is_transit and leg_end else None
                choices.append(kept)

            if leg_end is not None:
                previous_original_end = leg_end
            elif leg_start is not None:
                previous_original_end = leg_start + timedelta(seconds=duration)
            else:
                previous_original_end += timedelta(seconds=duration)

        choices = self._merge_adjacent_bicycle_legs(choices)
        kept_transit = [leg for leg in choices if leg.get("transitLeg")]
        if not kept_transit:
            # Direct bicycle is already generated separately and is a better representation.
            return None, comparisons

        # Start as late as possible while still catching the first remaining scheduled vehicle.
        first_transit_index = next(i for i, leg in enumerate(choices) if leg.get("transitLeg"))
        first_transit_start = self._parse_leg_timestamp(
            choices[first_transit_index].get("_fixedTransitStart")
        )
        prefix_duration = sum(
            int(leg.get("duration") or 0) for leg in choices[:first_transit_index]
        )
        if first_transit_start is not None:
            route_start = max(
                requested_departure,
                first_transit_start - timedelta(seconds=prefix_duration),
            )
        else:
            route_start = requested_departure

        current = route_start
        waiting_total = 0
        scheduled_legs: list[dict[str, Any]] = []

        for leg in choices:
            leg = deepcopy(leg)
            if leg.get("transitLeg"):
                fixed_start = self._parse_leg_timestamp(leg.pop("_fixedTransitStart", None))
                fixed_end = self._parse_leg_timestamp(leg.pop("_fixedTransitEnd", None))
                if fixed_start is not None:
                    # Replacements are only useful if the original downstream trip remains catchable.
                    if current > fixed_start + timedelta(seconds=20):
                        return None, comparisons
                    wait = max(0, int((fixed_start - current).total_seconds()))
                    waiting_total += wait
                    current = fixed_start
                leg["startTime"] = current.isoformat()
                if fixed_end is not None and fixed_end >= current:
                    current = fixed_end
                else:
                    current += timedelta(seconds=int(leg.get("duration") or 0))
                leg["endTime"] = current.isoformat()
            else:
                leg.pop("_fixedTransitStart", None)
                leg.pop("_fixedTransitEnd", None)
                leg["startTime"] = current.isoformat()
                current += timedelta(seconds=int(leg.get("duration") or 0))
                leg["endTime"] = current.isoformat()
            scheduled_legs.append(leg)

        scheduled_legs = self._merge_adjacent_bicycle_legs(scheduled_legs)
        transit_legs = [leg for leg in scheduled_legs if leg.get("transitLeg")]
        bike_distance = sum(
            float(leg.get("distance") or 0)
            for leg in scheduled_legs
            if leg.get("mode") == "BICYCLE"
        )
        walk_distance = sum(
            float(leg.get("distance") or 0)
            for leg in scheduled_legs
            if leg.get("mode") == "WALK"
        )
        transit_distance = sum(
            float(leg.get("distance") or 0) for leg in transit_legs
        )

        initial_wait = max(0, int((route_start - requested_departure).total_seconds()))
        duration = max(0, int((current - route_start).total_seconds()))
        route = {
            "id": "",
            "kind": "mixed",
            "strategy": "transit_optimized",
            "sourceQuery": skeleton.get("sourceQuery", "transit_skeleton"),
            "duration": duration,
            "initialWait": initial_wait,
            "doorToDoor": max(0, int((current - requested_departure).total_seconds())),
            "start": route_start.isoformat(),
            "end": current.isoformat(),
            "generalizedCost": None,
            "score": 0,
            "transfers": max(0, len(transit_legs) - 1),
            "waitingTime": waiting_total,
            "walkDistance": round(walk_distance, 1),
            "bikeDistance": round(bike_distance, 1),
            "transitDistance": round(transit_distance, 1),
            "bikeBoardings": len(transit_legs),
            "legs": scheduled_legs,
            "optimization": {
                "baseTransitRoutes": skeleton.get("transitRoutes", []),
                "replacedWalkLegs": replaced_walk,
                "replacedTransitLegs": replaced_transit,
                "replacedTransitCount": len(replaced_transit),
                "savedSecondsEstimate": sum(
                    item.get("savedSeconds", 0) for item in replaced_transit
                ),
            },
        }
        return self._score_route(route, profile), comparisons

    def _bike_between_leg_endpoints(
        self,
        leg: dict[str, Any],
        departure: datetime,
        profile: str,
    ) -> dict[str, Any] | None:
        from_obj = leg.get("from") or {}
        to_obj = leg.get("to") or {}
        try:
            a = (float(from_obj["lat"]), float(from_obj["lon"]))
            b = (float(to_obj["lat"]), float(to_obj["lon"]))
        except (KeyError, TypeError, ValueError):
            return None
        return self._bike_between_points(a, b, departure, profile)

    def _bike_between_points(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
    ) -> dict[str, Any] | None:
        key = (
            round(origin[0], 5),
            round(origin[1], 5),
            round(destination[0], 5),
            round(destination[1], 5),
            profile,
        )
        with self._bike_cache_lock:
            if key in self._bike_cache:
                cached = self._bike_cache[key]
                return deepcopy(cached) if cached is not None else None

        try:
            nodes, _ = self.otp.plan(
                origin=coordinate_location(*origin, "Bike start"),
                destination=coordinate_location(*destination, "Bike end"),
                departure=departure,
                profile=profile,
                max_transfers=0,
                direct_bike=True,
                direct_only=True,
                first=1,
            )
            routes = [
                self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="bike_segment",
                    source_query="bike_segment",
                )
                for node in nodes
            ]
            routes = [route for route in routes if route.get("kind") == "bike"]
            result = min(routes, key=lambda r: r["duration"]) if routes else None
        except Exception:
            result = None

        with self._bike_cache_lock:
            self._bike_cache[key] = deepcopy(result) if result is not None else None
        return deepcopy(result) if result is not None else None

    @staticmethod
    def _parse_leg_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(MOSCOW_TZ)
        if isinstance(value, (int, float)):
            number = float(value)
            # Legacy OTP GraphQL startTime/endTime are epoch milliseconds.
            if abs(number) > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=MOSCOW_TZ)
        text = str(value).strip()
        if not text:
            return None
        try:
            if text.replace(".", "", 1).isdigit():
                return RoutePlanner._parse_leg_timestamp(float(text))
            return parse_otp_time(text)
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _merge_adjacent_bicycle_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for leg in legs:
            if (
                result
                and result[-1].get("mode") == "BICYCLE"
                and not result[-1].get("transitLeg")
                and leg.get("mode") == "BICYCLE"
                and not leg.get("transitLeg")
            ):
                previous = result[-1]
                previous["duration"] = int(previous.get("duration") or 0) + int(
                    leg.get("duration") or 0
                )
                previous["distance"] = round(
                    float(previous.get("distance") or 0) + float(leg.get("distance") or 0),
                    1,
                )
                previous["to"] = deepcopy(leg.get("to"))
                if leg.get("endTime") is not None:
                    previous["endTime"] = leg.get("endTime")
                a = ((previous.get("geometry") or {}).get("coordinates") or [])
                b = ((leg.get("geometry") or {}).get("coordinates") or [])
                if a and b:
                    previous["geometry"] = {
                        "type": "LineString",
                        "coordinates": a + b[1:] if a[-1] == b[0] else a + b,
                    }
                replaced = previous.setdefault("replacedSegments", [])
                if previous.get("replaces"):
                    replaced.append(previous.pop("replaces"))
                if leg.get("replaces"):
                    replaced.append(leg.get("replaces"))
            else:
                result.append(deepcopy(leg))
        return result

    def _egress_anchor_candidates(
        self,
        anchors: list[Anchor],
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
    ) -> list[dict[str, Any]]:
        if not anchors:
            return []

        origin_loc = coordinate_location(*origin, "Старт")
        destination_loc = coordinate_location(*destination, "Финиш")

        def run_anchor(anchor: Anchor) -> list[dict[str, Any]]:
            anchor_gtfs_id = f"{self.feed_id}:{anchor.stop_id}"
            first_nodes, _ = self.otp.plan(
                origin=origin_loc,
                destination=stop_location(anchor_gtfs_id, anchor.name),
                departure=departure,
                profile=profile,
                transit_modes=ALL_TRANSIT,
                max_transfers=max_transfers,
                transit_only=True,
                egress_mode="BICYCLE",
                first=4,
            )

            first_routes = []
            for node in first_nodes:
                route = self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="egress_anchor",
                    source_query="egress_anchor",
                )
                if route["kind"] == "mixed" and route["transitDistance"] >= 800:
                    first_routes.append(route)
            if not first_routes:
                return []

            first_routes.sort(key=lambda r: (r["doorToDoor"], r["score"]))
            first_routes = first_routes[:2]

            # Direct bicycle path from anchor to destination is schedule-independent.
            bike_nodes, _ = self.otp.plan(
                origin=coordinate_location(anchor.lat, anchor.lon, anchor.name),
                destination=destination_loc,
                departure=departure,
                profile=profile,
                max_transfers=0,
                direct_bike=True,
                direct_only=True,
                first=2,
            )
            bike_routes = [
                self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="anchor_bike_egress",
                    source_query="anchor_bike_egress",
                )
                for node in bike_nodes
            ]
            bike_routes = [r for r in bike_routes if r["kind"] == "bike"]
            if not bike_routes:
                return []
            bike = min(bike_routes, key=lambda r: r["duration"])

            combined = []
            for first in first_routes:
                route = self._combine_anchor_route(first, bike, anchor, profile)
                combined.append(route)
            return combined

        result: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.anchor_workers) as pool:
            futures = [pool.submit(run_anchor, anchor) for anchor in anchors]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result.extend(future.result())
                except Exception:
                    # One bad anchor should not poison the whole deep search.
                    continue
        return self._dedupe_exact(result)

    def _normalize_route(
        self,
        itinerary: dict[str, Any],
        *,
        requested_departure: datetime,
        profile_key: str,
        strategy: str,
        source_query: str,
    ) -> dict[str, Any]:
        start_dt = parse_otp_time(itinerary.get("start"))
        end_dt = parse_otp_time(itinerary.get("end"))
        duration = int(float(itinerary.get("duration") or 0))
        initial_wait = max(0, int((start_dt - requested_departure).total_seconds())) if start_dt else 0
        transfers = int(itinerary.get("numberOfTransfers") or 0)
        waiting_time = int(float(itinerary.get("waitingTime") or 0))
        walk_distance = float(itinerary.get("walkDistance") or 0)

        legs_out = []
        bike_distance = 0.0
        transit_distance = 0.0
        bike_boardings = 0

        for leg in itinerary.get("legs") or []:
            mode = leg.get("mode") or "UNKNOWN"
            distance = float(leg.get("distance") or 0)
            is_transit = bool(leg.get("transitLeg"))
            if mode == "BICYCLE":
                bike_distance += distance
            if is_transit:
                transit_distance += distance
                bike_boardings += 1

            from_obj = leg.get("from") or {}
            to_obj = leg.get("to") or {}
            geometry = decode_polyline((leg.get("legGeometry") or {}).get("points"))
            if len(geometry) < 2:
                try:
                    geometry = [
                        [float(from_obj["lon"]), float(from_obj["lat"])],
                        [float(to_obj["lon"]), float(to_obj["lat"])],
                    ]
                except (KeyError, TypeError, ValueError):
                    geometry = []

            route_obj = leg.get("route") or {}
            legs_out.append(
                {
                    "mode": mode,
                    "transitLeg": is_transit,
                    "duration": int(float(leg.get("duration") or 0)),
                    "distance": round(distance, 1),
                    "startTime": leg.get("startTime"),
                    "endTime": leg.get("endTime"),
                    "realTime": bool(leg.get("realTime")),
                    "interlineWithPreviousLeg": bool(leg.get("interlineWithPreviousLeg")),
                    "tripId": (leg.get("trip") or {}).get("gtfsId"),
                    "from": {
                        "name": from_obj.get("name") or "Старт",
                        "lat": from_obj.get("lat"),
                        "lon": from_obj.get("lon"),
                    },
                    "to": {
                        "name": to_obj.get("name") or "Финиш",
                        "lat": to_obj.get("lat"),
                        "lon": to_obj.get("lon"),
                    },
                    "route": {
                        "shortName": route_obj.get("shortName"),
                        "longName": route_obj.get("longName"),
                        "mode": route_obj.get("mode"),
                    }
                    if route_obj
                    else None,
                    "geometry": {"type": "LineString", "coordinates": geometry},
                }
            )

        has_transit = any(leg["transitLeg"] for leg in legs_out)
        has_bike = any(leg["mode"] == "BICYCLE" for leg in legs_out)
        route = {
            "id": "",
            "kind": "mixed" if has_transit else "bike" if has_bike else "other",
            "strategy": strategy,
            "sourceQuery": source_query,
            "duration": duration,
            "initialWait": initial_wait,
            "doorToDoor": initial_wait + duration,
            "start": start_dt.isoformat() if start_dt else itinerary.get("start"),
            "end": end_dt.isoformat() if end_dt else itinerary.get("end"),
            "generalizedCost": itinerary.get("generalizedCost"),
            "score": 0,
            "transfers": transfers,
            "waitingTime": waiting_time,
            "walkDistance": round(walk_distance, 1),
            "bikeDistance": round(bike_distance, 1),
            "transitDistance": round(transit_distance, 1),
            "bikeBoardings": bike_boardings,
            "legs": legs_out,
        }
        return self._score_route(route, profile_key)

    def _combine_anchor_route(
        self,
        first: dict[str, Any],
        bike: dict[str, Any],
        anchor: Anchor,
        profile: str,
    ) -> dict[str, Any]:
        first_end = parse_otp_time(first.get("end"))
        if first_end is None:
            return first

        bike_legs = deepcopy(bike["legs"])
        cursor = first_end
        for leg in bike_legs:
            leg["startTime"] = cursor.isoformat()
            cursor += timedelta(seconds=int(leg.get("duration") or 0))
            leg["endTime"] = cursor.isoformat()

        duration = first["duration"] + bike["duration"]
        route = {
            "id": "",
            "kind": "mixed",
            "strategy": "egress_anchor",
            "sourceQuery": "egress_anchor",
            "duration": duration,
            "initialWait": first["initialWait"],
            "doorToDoor": first["initialWait"] + duration,
            "start": first["start"],
            "end": cursor.isoformat(),
            "generalizedCost": None,
            "score": 0,
            "transfers": first["transfers"],
            "waitingTime": first["waitingTime"],
            "walkDistance": first["walkDistance"],
            "bikeDistance": round(first["bikeDistance"] + bike["bikeDistance"], 1),
            "transitDistance": first["transitDistance"],
            "bikeBoardings": first.get("bikeBoardings", 0),
            "legs": first["legs"] + bike_legs,
            "anchor": {
                "stopId": anchor.stop_id,
                "name": anchor.name,
                "lat": anchor.lat,
                "lon": anchor.lon,
                "bikeEgressDistance": round(bike["bikeDistance"], 1),
                "routeCount": anchor.route_count,
                "modes": list(anchor.modes),
            },
        }
        return self._score_route(route, profile)

    @staticmethod
    def _actual_transfer_count(route: dict[str, Any]) -> int:
        """Count actual changes between public-transport vehicles.

        OTP's numberOfTransfers is useful, but routes are later transformed and
        composed by our own planner. We therefore recompute the number from the
        final leg list. Interlined legs do not count as a new boarding.
        """
        boardings = 0
        previous_trip: str | None = None

        for leg in route.get("legs") or []:
            if not leg.get("transitLeg"):
                # Leaving transit means a later boarding counts again, even if
                # a pathological feed happens to reuse the same trip id.
                previous_trip = None
                continue

            trip_id = leg.get("tripId")
            interline = bool(leg.get("interlineWithPreviousLeg"))

            if interline:
                previous_trip = trip_id or previous_trip
                continue

            # A split leg belonging to the exact same trip is not a transfer.
            if trip_id and previous_trip and trip_id == previous_trip:
                continue

            boardings += 1
            previous_trip = trip_id

        return max(0, boardings - 1)

    def _refresh_route_metrics(self, route: dict[str, Any]) -> dict[str, Any]:
        """Recompute metrics from the final leg list after all transformations."""
        route = deepcopy(route)
        legs = route.get("legs") or []

        bike_distance = sum(
            float(leg.get("distance") or 0)
            for leg in legs
            if leg.get("mode") == "BICYCLE" and not leg.get("transitLeg")
        )
        walk_distance = sum(
            float(leg.get("distance") or 0)
            for leg in legs
            if leg.get("mode") == "WALK" and not leg.get("transitLeg")
        )
        transit_distance = sum(
            float(leg.get("distance") or 0)
            for leg in legs
            if leg.get("transitLeg")
        )

        boardings = self._actual_transfer_count(route) + (
            1 if any(leg.get("transitLeg") for leg in legs) else 0
        )

        route["bikeDistance"] = round(bike_distance, 1)
        route["walkDistance"] = round(walk_distance, 1)
        route["transitDistance"] = round(transit_distance, 1)
        route["bikeBoardings"] = boardings
        route["transfers"] = max(0, boardings - 1)

        movement = bike_distance + transit_distance
        route["bikeShare"] = round(bike_distance / movement, 4) if movement > 0 else 0.0
        route["transitShare"] = round(transit_distance / movement, 4) if movement > 0 else 0.0
        return route

    @staticmethod
    def _base_target_bike_share(reference_distance_m: float) -> float:
        """Adaptive bicycle share before applying the user's preference.

        Short trips naturally favour the bicycle; long trips naturally favour
        transit. The focus setting moves this baseline instead of replacing it
        with a fixed percentage.
        """
        if reference_distance_m <= 4_000:
            return 0.88
        if reference_distance_m <= 8_000:
            return 0.68
        if reference_distance_m <= 15_000:
            return 0.48
        if reference_distance_m <= 25_000:
            return 0.32
        return 0.20

    def _apply_route_focus(
        self,
        routes: list[dict[str, Any]],
        *,
        profile_key: str,
        route_focus: int,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []

        route_focus = min(2, max(-2, int(route_focus)))
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]
        profile_cfg = PROFILE_CONFIG[profile_key]

        direct_bikes = [
            route
            for route in routes
            if route.get("kind") == "bike" and float(route.get("bikeDistance") or 0) > 0
        ]
        if direct_bikes:
            reference_distance = min(
                float(route.get("bikeDistance") or 0)
                for route in direct_bikes
            )
        else:
            reference_distance = max(
                (
                    float(route.get("bikeDistance") or 0)
                    + float(route.get("transitDistance") or 0)
                )
                for route in routes
            )

        base_target = self._base_target_bike_share(reference_distance)
        target = min(
            1.0,
            max(0.05, base_target + float(focus_cfg["bike_share_shift"])),
        )

        best_time = max(1, min(int(route.get("doorToDoor") or 0) for route in routes))
        allowed_ratio = 1.0 + float(focus_cfg["time_tolerance_ratio"])

        result: list[dict[str, Any]] = []
        for route in routes:
            route = self._refresh_route_metrics(route)
            bike_share = float(route.get("bikeShare") or 0)
            base_score = float(route.get("baseScore", route.get("score", 0)) or 0)

            # The preference is directional. A transport-heavy preference does
            # not punish "too much transit", and a cycling-heavy preference does
            # not punish "too much bicycle". Balance aims near the adaptive target.
            if route_focus < 0:
                share_gap = max(0.0, bike_share - target)
            elif route_focus > 0:
                share_gap = max(0.0, target - bike_share)
            else:
                share_gap = abs(bike_share - target)

            share_penalty = share_gap * float(focus_cfg["share_penalty_seconds"])

            # Transfer aversion is still present, but a transport-oriented user
            # tolerates transfers somewhat more than a bicycle-oriented user.
            transfer_adjustment = (
                int(route.get("transfers") or 0)
                * float(profile_cfg["transfer_penalty"])
                * (float(focus_cfg["transfer_penalty_factor"]) - 1.0)
            )

            time_ratio = float(route.get("doorToDoor") or best_time) / best_time
            detour_penalty = 0.0
            if time_ratio > allowed_ratio:
                detour_penalty = (
                    (time_ratio - allowed_ratio) * best_time * 3.0
                    + 240.0
                )

            preference_score = (
                base_score
                + share_penalty
                + transfer_adjustment
                + detour_penalty
            )

            route["score"] = round(preference_score)
            route["preference"] = {
                "focus": route_focus,
                "focusName": focus_cfg["name"],
                "targetBikeShare": round(target, 3),
                "actualBikeShare": round(bike_share, 3),
                "referenceDistance": round(reference_distance, 1),
                "timeRatio": round(time_ratio, 3),
                "allowedTimeRatio": round(allowed_ratio, 3),
            }
            result.append(route)

        return result

    def _score_route(self, route: dict[str, Any], profile_key: str) -> dict[str, Any]:
        route = self._refresh_route_metrics(route)
        cfg = PROFILE_CONFIG[profile_key]
        score = float(route.get("doorToDoor") or 0)
        score += float(route.get("waitingTime") or 0) * cfg["wait_factor"]
        score += int(route.get("transfers") or 0) * cfg["transfer_penalty"]
        score += int(route.get("bikeBoardings") or 0) * cfg["bike_boarding_penalty"]
        score += (float(route.get("walkDistance") or 0) / 1000.0) * 180

        if route.get("kind") == "mixed":
            transit_distance = float(route.get("transitDistance") or 0)
            total_movement = transit_distance + float(route.get("bikeDistance") or 0)
            # Penalise cases like 5.4 km bike + 330 m bus + 1.3 km bike.
            if transit_distance < 800:
                score += 300
            if total_movement > 0 and transit_distance / total_movement < 0.08:
                score += 240

        route["baseScore"] = round(score)
        route["score"] = round(score)
        route["transitModes"] = transit_modes(route)
        route["transitRoutes"] = transit_route_names(route)
        return route

    def _dedupe_exact(self, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple, dict[str, Any]] = {}
        for route in routes:
            key = self._exact_signature(route)
            previous = best.get(key)
            if previous is None or (route.get("score", math.inf), route.get("doorToDoor", math.inf)) < (
                previous.get("score", math.inf),
                previous.get("doorToDoor", math.inf),
            ):
                best[key] = route
        return list(best.values())

    @staticmethod
    def _exact_signature(route: dict[str, Any]) -> tuple:
        return tuple(
            (
                leg.get("mode"),
                (leg.get("route") or {}).get("shortName") or (leg.get("route") or {}).get("longName"),
                (leg.get("from") or {}).get("name"),
                (leg.get("to") or {}).get("name"),
                round(float(leg.get("distance") or 0) / 100.0),
            )
            for leg in route.get("legs") or []
        )

    @staticmethod
    def _dedupe_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = set()
        result = []
        for warning in warnings:
            key = (warning.get("code"), warning.get("description"))
            if key in seen:
                continue
            seen.add(key)
            result.append(warning)
        return result

    def _select_diverse(
        self,
        routes: list[dict[str, Any]],
        limit: int,
        route_focus: int = 0,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []

        route_focus = min(2, max(-2, int(route_focus)))
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]

        routes = sorted(routes, key=lambda r: (r["score"], r["doorToDoor"]))
        best_time = min(r["doorToDoor"] for r in routes)

        min_extra = {
            -2: 12 * 60,
            -1: 12 * 60,
            0: 15 * 60,
            1: 22 * 60,
            2: 30 * 60,
        }[route_focus]
        soft_limit = max(
            best_time * (1.0 + float(focus_cfg["time_tolerance_ratio"])),
            best_time + min_extra,
        )

        selected: list[dict[str, Any]] = []
        selected_ids: set[int] = set()
        cluster_counts: dict[tuple, int] = {}

        def add(route: dict[str, Any] | None, force: bool = False) -> None:
            if route is None or id(route) in selected_ids or len(selected) >= limit:
                return
            if not force and route["doorToDoor"] > soft_limit:
                return

            cluster = normalized_signature(route)
            if not force and cluster_counts.get(cluster, 0) >= 1:
                return

            selected.append(route)
            selected_ids.add(id(route))
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1

        eligible = [route for route in routes if route["doorToDoor"] <= soft_limit]
        if not eligible:
            eligible = routes

        fastest = min(routes, key=lambda r: r["doorToDoor"])
        best_score = min(routes, key=lambda r: (r["score"], r["doorToDoor"]))

        # Candidate closest to the adaptive bicycle-share target.
        focus_match = min(
            eligible,
            key=lambda r: (
                abs(
                    float(r.get("bikeShare") or 0)
                    - float((r.get("preference") or {}).get("targetBikeShare", 0))
                ),
                r["score"],
            ),
        )

        transit_heavy = min(
            eligible,
            key=lambda r: (
                float(r.get("bikeShare") or 0),
                r["score"],
            ),
        )
        bike_heavy = max(
            eligible,
            key=lambda r: (
                float(r.get("bikeShare") or 0),
                -r["score"],
            ),
        )

        # First slots depend on user intent. This changes actual output diversity,
        # not just sorting.
        add(fastest)
        add(best_score)
        add(focus_match)

        if route_focus < 0:
            add(transit_heavy)
            add(self._best_matching(eligible, lambda r: r.get("strategy") == "transit_optimized"))
            add(bike_heavy)
        elif route_focus > 0:
            add(bike_heavy)
            add(self._best_matching(eligible, lambda r: r.get("strategy") == "egress_anchor"))
            add(transit_heavy)
        else:
            add(self._best_matching(eligible, lambda r: r.get("strategy") == "transit_optimized"))
            add(self._best_matching(eligible, lambda r: r.get("strategy") == "egress_anchor"))

        # Mode diversity.
        add(self._best_matching(
            eligible,
            lambda r: "BUS" in r.get("transitModes", [])
            or "TROLLEYBUS" in r.get("transitModes", []),
        ))
        add(self._best_matching(eligible, lambda r: "TRAM" in r.get("transitModes", [])))
        add(self._best_matching(eligible, lambda r: "RAIL" in r.get("transitModes", [])))

        # Direct bicycle remains visible as a useful baseline even when it is
        # outside the normal soft time window.
        add(self._best_matching(routes, lambda r: r.get("kind") == "bike"), force=True)

        for route in routes:
            add(route)
            if len(selected) >= limit:
                break

        selected.sort(key=lambda r: (r["score"], r["doorToDoor"]))
        for index, route in enumerate(selected):
            route["id"] = f"route-{index}"
        return selected

    @staticmethod
    def _best_matching(routes, predicate):
        matches = [r for r in routes if predicate(r)]
        return min(matches, key=lambda r: (r["score"], r["doorToDoor"])) if matches else None

    def _assign_recommendation_labels(self, routes: list[dict[str, Any]]) -> None:
        if not routes:
            return
        fastest = min(routes, key=lambda r: r["doorToDoor"])
        best_score = min(routes, key=lambda r: r["score"])

        for route in routes:
            if route is fastest:
                route["recommendation"] = "Самый быстрый"
            elif route is best_score:
                route["recommendation"] = "Оптимальный"
            elif route.get("strategy") == "transit_optimized":
                route["recommendation"] = "ОТ, оптимизированный велосипедом"
            elif route.get("strategy") == "egress_anchor":
                route["recommendation"] = "Транспорт → велосипед"
            elif route.get("kind") == "bike":
                route["recommendation"] = "Только велосипед"
            elif "RAIL" in route.get("transitModes", []):
                route["recommendation"] = "Поезд + велосипед"
            elif "TRAM" in route.get("transitModes", []):
                route["recommendation"] = "Трамвай + велосипед"
            elif "BUS" in route.get("transitModes", []) or "TROLLEYBUS" in route.get("transitModes", []):
                route["recommendation"] = "Автобус + велосипед"
            else:
                route["recommendation"] = "Альтернатива"
