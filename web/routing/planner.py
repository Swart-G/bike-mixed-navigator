from __future__ import annotations

import concurrent.futures
import math
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Callable

from .gtfs_index import Anchor, GtfsIndex, LineMetrics
from .models import (
    MOSCOW_TZ,
    PROFILE_CONFIG,
    ROUTE_FOCUS_CONFIG,
    decode_polyline,
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
SOFT_ROUTING_ERRORS = {
    "NO_STOPS_IN_RANGE",
    "WALKING_BETTER_THAN_TRANSIT",
    "NO_TRANSIT_CONNECTION",
    "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW",
}


class RoutePlanner:
    """Hybrid bicycle + public transport planner layered on top of OTP.

    OTP is treated as the schedule oracle. The custom layer is responsible for
    generating strategically different hypotheses, replacing weak micro-transit
    with bicycle legs, finding strong boarding/egress anchors, and selecting a
    small set of genuinely different routes.
    """

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
        self.generic_workers = max(1, generic_workers)
        self.anchor_workers = max(1, anchor_workers)
        self._bike_cache: dict[tuple, dict[str, Any] | None] = {}
        self._bike_cache_lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def health(self) -> dict[str, Any]:
        otp_ok = self.otp.health()
        self.gtfs.ensure_loaded()
        return {
            "otp": otp_ok,
            "gtfsIndex": {
                "loaded": self.gtfs.loaded,
                "stopCount": self.gtfs.stop_count if self.gtfs.loaded else 0,
                "lineCount": self.gtfs.line_count if self.gtfs.loaded else 0,
                "error": self.gtfs.error,
            },
        }

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        origin = parse_coordinate(payload.get("origin"), "Старт")
        destination = parse_coordinate(payload.get("destination"), "Финиш")
        departure = parse_departure(payload.get("departureTime"))

        profile = str(payload.get("profile") or "balanced")
        if profile not in PROFILE_CONFIG:
            profile = "balanced"
        max_transfers = self._clamp_int(payload.get("maxTransfers", 2), 2, 5, 2)
        route_focus = self._clamp_int(payload.get("routeFocus", 0), -2, 2, 0)
        deep_search = bool(payload.get("deepSearch", True))
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]

        # Warm the line/stop metrics once. If GTFS indexing fails, all routing
        # still works; it simply falls back to neutral line-quality assumptions.
        self.gtfs.ensure_loaded()

        generic_routes, warnings, generic_stats = self._generic_candidates(
            origin, destination, departure, profile, max_transfers
        )
        direct_bike = self._best_matching(generic_routes, lambda r: r.get("kind") == "bike")

        transit_routes, transit_warnings, transit_stats = self._transit_first_candidates(
            origin,
            destination,
            departure,
            profile,
            max_transfers,
            route_focus,
        )
        warnings.extend(transit_warnings)

        boarding_anchors: list[Anchor] = []
        egress_anchors: list[Anchor] = []
        boarding_routes: list[dict[str, Any]] = []
        egress_routes: list[dict[str, Any]] = []
        deep_error: str | None = None

        if deep_search and self.gtfs.loaded:
            try:
                anchor_limit = int(focus_cfg["anchor_limit"])
                boarding_anchors = self.gtfs.boarding_anchors(
                    origin,
                    destination,
                    limit=max(6, anchor_limit),
                    route_focus=route_focus,
                )
                egress_anchors = self.gtfs.egress_anchors(
                    origin,
                    destination,
                    limit=max(6, anchor_limit - 2),
                    route_focus=route_focus,
                )
                boarding_routes, egress_routes = self._anchor_candidates(
                    boarding_anchors=boarding_anchors,
                    egress_anchors=egress_anchors,
                    origin=origin,
                    destination=destination,
                    departure=departure,
                    profile=profile,
                    max_transfers=max_transfers,
                )
            except Exception as exc:
                deep_error = str(exc)

        # Raw BIKE+TRANSIT OTP candidates are a fallback only. Once our
        # transit-first/anchor pipelines produce routes, keeping raw candidates
        # tends to reintroduce weak local buses and near-clones.
        fallback_mixed = []
        if not transit_routes and not boarding_routes and not egress_routes:
            fallback_mixed = [r for r in generic_routes if r.get("kind") == "mixed"]

        pool = [r for r in [direct_bike] if r is not None]
        pool.extend(transit_routes)
        pool.extend(boarding_routes)
        pool.extend(egress_routes)
        pool.extend(fallback_mixed)
        pool = self._dedupe_exact(pool)

        before_policy_filter = len(pool)
        policy_legal_pool = [route for route in pool if self._bike_carriage_is_legal(route)]
        bike_policy_filtered = before_policy_filter - len(policy_legal_pool)

        before_transfer_filter = len(policy_legal_pool)
        normalized_pool: list[dict[str, Any]] = []
        for route in policy_legal_pool:
            route = self._score_route(route, profile)
            if int(route.get("transfers") or 0) <= max_transfers:
                normalized_pool.append(route)
        transfer_filtered = before_transfer_filter - len(normalized_pool)

        focused = self._apply_route_focus(
            normalized_pool,
            profile_key=profile,
            route_focus=route_focus,
        )
        pareto = self._pareto_prune(focused)
        selected = self._select_diverse(
            pareto,
            limit=6,
            route_focus=route_focus,
        )
        self._assign_recommendation_labels(selected)
        self._add_explanations(selected)

        if selected:
            warnings = [w for w in warnings if w.get("code") not in SOFT_ROUTING_ERRORS]

        return {
            "routes": selected,
            "warnings": self._dedupe_warnings(warnings),
            "stats": {
                "algorithm": "hybrid-strategy-v0.6",
                "genericQueries": generic_stats["queries"],
                "genericCandidates": len(generic_routes),
                "transitSkeletonQueries": transit_stats["queries"],
                "transitSkeletons": transit_stats["skeletons"],
                "transitOptimizedCandidates": len(transit_routes),
                "bikeComparisons": transit_stats["bikeComparisons"],
                "boardingAnchorsConsidered": len(boarding_anchors),
                "boardingAnchorCandidates": len(boarding_routes),
                "egressAnchorsConsidered": len(egress_anchors),
                "anchorCandidates": len(egress_routes),  # backwards-compatible UI field
                "egressAnchorCandidates": len(egress_routes),
                "candidatesBeforePareto": len(focused),
                "paretoCandidates": len(pareto),
                "candidatesTotal": len(focused),
                "transferFiltered": transfer_filtered,
                "bikePolicyFiltered": bike_policy_filtered,
                "maxTransfers": max_transfers,
                "routeFocus": route_focus,
                "routeFocusName": focus_cfg["name"],
                "returned": len(selected),
                "deepSearchAvailable": self.gtfs.loaded,
                "deepSearchError": deep_error or self.gtfs.error,
                "elapsedMs": round((time.monotonic() - started) * 1000),
            },
        }

    # ------------------------------------------------------ candidate generation

    def _generic_candidates(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        selected_profile: str,
        max_transfers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        specs = [
            {
                "name": "main",
                "profile": selected_profile,
                "modes": ALL_TRANSIT,
                "direct_bike": True,
                "strategy": "generic",
            },
            {
                "name": "profile_fast",
                "profile": "fast",
                "modes": ALL_TRANSIT,
                "strategy": "generic_fast",
            },
            {
                "name": "rail",
                "profile": selected_profile,
                "modes": MODE_QUERIES["rail"],
                "strategy": "generic_rail",
            },
            {
                "name": "bus_tram",
                "profile": selected_profile,
                "modes": MODE_QUERIES["bus_tram"],
                "strategy": "generic_surface",
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
                max_transfers=max_transfers,
                direct_bike=bool(spec.get("direct_bike")),
                transit_only=not bool(spec.get("direct_bike")),
                first=14,
            )
            local_routes = [
                self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=selected_profile,
                    strategy=spec["strategy"],
                    source_query=spec["name"],
                )
                for node in nodes
            ]
            return [r for r in local_routes if r.get("kind") != "other"], local_warnings

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(run, spec) for spec in specs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    local, local_warnings = future.result()
                    routes.extend(local)
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
        route_focus: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        specs = [
            ("pt_all", ALL_TRANSIT, max_transfers),
            ("pt_bus", MODE_QUERIES["bus"], max_transfers),
            ("pt_tram", MODE_QUERIES["tram"], max_transfers),
            ("pt_rail", MODE_QUERIES["rail"], max_transfers),
            ("pt_bus_tram", MODE_QUERIES["bus_tram"], max_transfers),
            ("pt_bus_rail", MODE_QUERIES["bus_rail"], max_transfers),
            ("pt_tram_rail", MODE_QUERIES["tram_rail"], max_transfers),
            ("pt_simple", ALL_TRANSIT, 1),
        ]
        origin_loc = coordinate_location(*origin, "Старт")
        destination_loc = coordinate_location(*destination, "Финиш")
        skeletons: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def run(spec: tuple[str, list[str], int]):
            name, modes, transfers = spec
            nodes, local_warnings = self.otp.plan(
                origin=origin_loc,
                destination=destination_loc,
                departure=departure,
                profile=profile,
                transit_modes=modes,
                max_transfers=min(max_transfers, transfers),
                transit_only=True,
                access_mode="WALK",
                egress_mode="WALK",
                transfer_mode="WALK",
                first=14,
            )
            local: list[dict[str, Any]] = []
            for node in nodes:
                route = self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="transit_skeleton",
                    source_query=name,
                )
                if route.get("kind") == "mixed":
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

        # Exact dedupe first, then retain different transport chains before the
        # expensive bicycle comparisons. Similarity is intentionally not used yet:
        # two skeletons on the same corridor can optimize into different strategies.
        skeletons = self._dedupe_exact(skeletons)
        preselected: list[dict[str, Any]] = []
        chain_seen: set[tuple] = set()
        for route in sorted(skeletons, key=lambda r: (r["doorToDoor"], r["score"])):
            chain = self._transit_chain_signature(route)
            if chain in chain_seen:
                continue
            chain_seen.add(chain)
            preselected.append(route)
            if len(preselected) >= 28:
                break

        comparisons = 0
        counter_lock = threading.Lock()
        optimized: list[dict[str, Any]] = []

        def optimize(route: dict[str, Any]):
            out, count = self._optimize_transit_skeleton(
                route,
                requested_departure=departure,
                profile=profile,
                route_focus=route_focus,
            )
            nonlocal comparisons
            with counter_lock:
                comparisons += count
            return out

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(optimize, route) for route in preselected]
            for future in concurrent.futures.as_completed(futures):
                try:
                    route = future.result()
                    if route is not None:
                        optimized.append(route)
                except Exception as exc:
                    warnings.append({"code": "TRANSIT_OPTIMIZER_FAILED", "description": str(exc)})

        return (
            self._dedupe_exact(optimized),
            self._dedupe_warnings(warnings),
            {"queries": len(specs), "skeletons": len(preselected), "bikeComparisons": comparisons},
        )

    # ---------------------------------------------------------- segment optimizer

    def _optimize_transit_skeleton(
        self,
        skeleton: dict[str, Any],
        requested_departure: datetime,
        profile: str,
        route_focus: int,
    ) -> tuple[dict[str, Any] | None, int]:
        original = deepcopy(skeleton.get("legs") or [])
        if not original:
            return None, 0

        comparisons = 0
        choices: list[dict[str, Any]] = []
        replaced_walk = 0
        replaced_transit: list[dict[str, Any]] = []
        previous_original_end = parse_otp_time(skeleton.get("start")) or requested_departure

        for index, leg in enumerate(original):
            mode = leg.get("mode") or "UNKNOWN"
            is_transit = bool(leg.get("transitLeg"))
            duration = int(leg.get("duration") or 0)
            distance = float(leg.get("distance") or 0)
            leg_start = self._parse_leg_timestamp(leg.get("startTime"))
            leg_end = self._parse_leg_timestamp(leg.get("endTime"))
            wait_before = (
                max(0, int((leg_start - previous_original_end).total_seconds()))
                if is_transit and leg_start is not None
                else 0
            )

            line = self.gtfs.line_metrics_for_trip(leg.get("tripId")) if is_transit else None
            downstream_trunk = self._downstream_trunk_score(original, index + 1)
            should_compare = (
                mode == "WALK" and not is_transit and distance >= 70
            ) or (
                is_transit
                and self._should_compare_transit_leg(
                    leg=leg,
                    wait_before=wait_before,
                    line=line,
                    route_focus=route_focus,
                )
            )

            bike = None
            if should_compare:
                bike = self._bike_between_leg_endpoints(leg, requested_departure, profile)
                comparisons += 1

            replace = False
            decision: dict[str, Any] = {}
            if bike is not None:
                if not is_transit and mode == "WALK":
                    bike_duration = int(bike.get("duration") or 0)
                    replace = bike_duration + 20 < duration
                    decision = {
                        "reason": "walk_to_bike",
                        "savedSeconds": max(0, duration - bike_duration),
                    }
                elif is_transit:
                    decision = self._transit_leg_replacement_decision(
                        leg=leg,
                        bike=bike,
                        wait_before=wait_before,
                        line=line,
                        downstream_trunk_score=downstream_trunk,
                        route_focus=route_focus,
                    )
                    replace = bool(decision.get("replace"))

            if replace and bike is not None:
                replacement_legs = deepcopy(bike.get("legs") or [])
                replacement_note = {
                    "mode": mode,
                    "route": (leg.get("route") or {}).get("shortName")
                    or (leg.get("route") or {}).get("longName"),
                    "savedSeconds": int(decision.get("savedSeconds") or 0),
                    "reason": decision.get("reason") or "bike_faster",
                }
                for repl in replacement_legs:
                    repl["transitLeg"] = False
                    repl["replaces"] = replacement_note
                choices.extend(replacement_legs)
                if is_transit:
                    replaced_transit.append(
                        {
                            **replacement_note,
                            "distance": round(distance, 1),
                            "lineTrunkScore": round(line.trunk_score, 3) if line else None,
                        }
                    )
                else:
                    replaced_walk += 1
            else:
                kept = deepcopy(leg)
                if is_transit:
                    kept["_fixedTransitStart"] = leg_start.isoformat() if leg_start else None
                    kept["_fixedTransitEnd"] = leg_end.isoformat() if leg_end else None
                choices.append(kept)

            if leg_end is not None:
                previous_original_end = leg_end
            elif leg_start is not None:
                previous_original_end = leg_start + timedelta(seconds=duration)
            else:
                previous_original_end += timedelta(seconds=duration)

        choices = self._merge_adjacent_bicycle_legs(choices)
        if not any(leg.get("transitLeg") for leg in choices):
            return None, comparisons

        rebuilt = self._rebuild_schedule(
            choices,
            requested_departure=requested_departure,
        )
        if rebuilt is None:
            return None, comparisons
        route_start, route_end, waiting_total, scheduled_legs = rebuilt

        route = {
            "id": "",
            "kind": "mixed",
            "strategy": "transit_optimized",
            "sourceQuery": skeleton.get("sourceQuery", "transit_skeleton"),
            "duration": max(0, int((route_end - route_start).total_seconds())),
            "initialWait": max(0, int((route_start - requested_departure).total_seconds())),
            "doorToDoor": max(0, int((route_end - requested_departure).total_seconds())),
            "start": route_start.isoformat(),
            "end": route_end.isoformat(),
            "generalizedCost": None,
            "score": 0,
            "waitingTime": waiting_total,
            "legs": scheduled_legs,
            "optimization": {
                "baseTransitRoutes": skeleton.get("transitRoutes", []),
                "replacedWalkLegs": replaced_walk,
                "replacedTransitLegs": replaced_transit,
                "replacedTransitCount": len(replaced_transit),
                "savedSecondsEstimate": sum(x.get("savedSeconds", 0) for x in replaced_transit),
            },
        }
        return self._score_route(route, profile), comparisons

    def _should_compare_transit_leg(
        self,
        *,
        leg: dict[str, Any],
        wait_before: int,
        line: LineMetrics | None,
        route_focus: int,
    ) -> bool:
        distance = float(leg.get("distance") or 0)
        duration = max(1, int(leg.get("duration") or 0))
        mode = str(leg.get("mode") or "")
        speed = distance / duration * 3.6
        trunk = line.trunk_score if line else 0.50
        headway = line.median_headway_s if line else None

        max_distance = {
            "BUS": 4_800,
            "TROLLEYBUS": 4_800,
            "TRAM": 3_800,
            "RAIL": 2_200,
        }.get(mode, 3_500)
        max_distance *= { -2: 0.65, -1: 0.82, 0: 1.0, 1: 1.22, 2: 1.45 }[route_focus]

        suspicious = sum(
            [
                distance < 1_500,
                duration < 5 * 60,
                speed < 14.0,
                wait_before > 150,
                headway is not None and headway > 10 * 60,
                trunk < 0.55,
            ]
        )
        return distance <= max_distance and suspicious >= 1

    def _transit_leg_replacement_decision(
        self,
        *,
        leg: dict[str, Any],
        bike: dict[str, Any],
        wait_before: int,
        line: LineMetrics | None,
        downstream_trunk_score: float,
        route_focus: int,
    ) -> dict[str, Any]:
        distance = float(leg.get("distance") or 0)
        duration = max(1, int(leg.get("duration") or 0))
        bike_duration = max(1, int(bike.get("duration") or 0))
        bike_distance = float(bike.get("bikeDistance") or 0)
        mode = str(leg.get("mode") or "")
        speed = distance / duration * 3.6
        trunk = line.trunk_score if line else 0.50
        headway = line.median_headway_s if line else None

        board_alight_slack = 60 if mode in {"BUS", "TROLLEYBUS"} else 75
        effective_transit = wait_before + duration + board_alight_slack
        saving = effective_transit - bike_duration

        evidence = 0
        evidence += distance < 1_200
        evidence += duration < 4 * 60
        evidence += speed < 12.0
        evidence += headway is not None and headway > 10 * 60
        evidence += trunk < 0.55

        # A weak local bus can be replaced over a somewhat longer bicycle leg,
        # but rail and strong trunk lines are deliberately much harder to remove.
        max_bike_distance = {
            "BUS": 3_200,
            "TROLLEYBUS": 3_200,
            "TRAM": 2_500,
            "RAIL": 1_600,
        }.get(mode, 2_500)
        max_bike_distance *= { -2: 0.60, -1: 0.80, 0: 1.0, 1: 1.35, 2: 1.70 }[route_focus]

        required_saving = max(90.0, effective_transit * 0.15)
        required_saving *= { -2: 1.55, -1: 1.25, 0: 1.0, 1: 0.82, 2: 0.68 }[route_focus]
        if mode == "TRAM":
            required_saving += 30
        elif mode == "RAIL":
            required_saving += 100
        if trunk >= 0.72:
            required_saving += 150
        elif trunk >= 0.62:
            required_saving += 75
        if downstream_trunk_score >= 0.72:
            # Preserve useful feeders unless the bicycle wins decisively.
            required_saving += 60

        required_evidence = 2
        if trunk >= 0.72 or mode == "RAIL":
            required_evidence = 3
        elif trunk < 0.45 and mode in {"BUS", "TROLLEYBUS"}:
            required_evidence = 1

        replace = (
            evidence >= required_evidence
            and bike_distance <= max_bike_distance
            and saving >= required_saving
        )
        return {
            "replace": replace,
            "reason": "weak_micro_transit" if trunk < 0.55 else "bike_materially_faster",
            "savedSeconds": max(0, int(saving)),
            "effectiveTransitSeconds": int(effective_transit),
            "requiredSavingSeconds": round(required_saving),
            "evidence": int(evidence),
            "trunkScore": round(trunk, 3),
        }

    def _downstream_trunk_score(self, legs: list[dict[str, Any]], start: int) -> float:
        for leg in legs[start:]:
            if not leg.get("transitLeg"):
                continue
            metrics = self.gtfs.line_metrics_for_trip(leg.get("tripId"))
            return metrics.trunk_score if metrics else 0.0
        return 0.0

    def _rebuild_schedule(
        self,
        legs: list[dict[str, Any]],
        *,
        requested_departure: datetime,
    ) -> tuple[datetime, datetime, int, list[dict[str, Any]]] | None:
        first_transit_index = next((i for i, leg in enumerate(legs) if leg.get("transitLeg")), None)
        if first_transit_index is None:
            return None
        first_start = self._parse_leg_timestamp(legs[first_transit_index].get("_fixedTransitStart"))
        prefix_duration = sum(int(leg.get("duration") or 0) for leg in legs[:first_transit_index])
        route_start = (
            max(requested_departure, first_start - timedelta(seconds=prefix_duration))
            if first_start is not None
            else requested_departure
        )

        current = route_start
        waiting_total = 0
        scheduled: list[dict[str, Any]] = []
        for raw in legs:
            leg = deepcopy(raw)
            if leg.get("transitLeg"):
                fixed_start = self._parse_leg_timestamp(leg.pop("_fixedTransitStart", None))
                fixed_end = self._parse_leg_timestamp(leg.pop("_fixedTransitEnd", None))
                if fixed_start is not None:
                    if current > fixed_start + timedelta(seconds=20):
                        return None
                    waiting_total += max(0, int((fixed_start - current).total_seconds()))
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
            scheduled.append(leg)

        return route_start, current, waiting_total, self._merge_adjacent_bicycle_legs(scheduled)

    # --------------------------------------------------------------- anchor search

    def _anchor_candidates(
        self,
        *,
        boarding_anchors: list[Anchor],
        egress_anchors: list[Anchor],
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        boarding: list[dict[str, Any]] = []
        egress: list[dict[str, Any]] = []

        jobs: list[tuple[str, Anchor]] = [*(('boarding', a) for a in boarding_anchors), *(('egress', a) for a in egress_anchors)]

        def run(job: tuple[str, Anchor]):
            role, anchor = job
            if role == "boarding":
                return role, self._boarding_anchor_routes(
                    anchor, origin, destination, departure, profile, max_transfers
                )
            return role, self._egress_anchor_routes(
                anchor, origin, destination, departure, profile, max_transfers
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.anchor_workers) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    role, routes = future.result()
                    (boarding if role == "boarding" else egress).extend(routes)
                except Exception:
                    continue

        return self._dedupe_exact(boarding), self._dedupe_exact(egress)

    def _boarding_anchor_routes(
        self,
        anchor: Anchor,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
    ) -> list[dict[str, Any]]:
        bike = self._bike_between_points(origin, (anchor.lat, anchor.lon), departure, profile)
        if bike is None or float(bike.get("bikeDistance") or 0) < 180:
            return []

        bike_duration = int(bike.get("duration") or 0)
        transit_departure = departure + timedelta(seconds=bike_duration + 35)
        gtfs_id = f"{self.feed_id}:{anchor.stop_id}"
        nodes, _ = self.otp.plan(
            origin=stop_location(gtfs_id, anchor.name),
            destination=coordinate_location(*destination, "Финиш"),
            departure=transit_departure,
            profile=profile,
            transit_modes=ALL_TRANSIT,
            max_transfers=max_transfers,
            transit_only=True,
            access_mode="WALK",
            egress_mode="BICYCLE",
            transfer_mode="BICYCLE",
            first=6,
        )

        result: list[dict[str, Any]] = []
        for node in nodes:
            second = self._normalize_route(
                node,
                requested_departure=transit_departure,
                profile_key=profile,
                strategy="boarding_anchor_tail",
                source_query="boarding_anchor",
            )
            if second.get("kind") != "mixed" or float(second.get("transitDistance") or 0) < 800:
                continue
            result.append(self._combine_boarding_route(bike, second, anchor, departure, profile))
        result.sort(key=lambda r: (r["score"], r["doorToDoor"]))
        return result[:2]

    def _egress_anchor_routes(
        self,
        anchor: Anchor,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
    ) -> list[dict[str, Any]]:
        gtfs_id = f"{self.feed_id}:{anchor.stop_id}"
        nodes, _ = self.otp.plan(
            origin=coordinate_location(*origin, "Старт"),
            destination=stop_location(gtfs_id, anchor.name),
            departure=departure,
            profile=profile,
            transit_modes=ALL_TRANSIT,
            max_transfers=max_transfers,
            transit_only=True,
            access_mode="BICYCLE",
            egress_mode="WALK",
            transfer_mode="BICYCLE",
            first=5,
        )
        bike = self._bike_between_points(
            (anchor.lat, anchor.lon), destination, departure, profile
        )
        if bike is None:
            return []

        result: list[dict[str, Any]] = []
        for node in nodes:
            first = self._normalize_route(
                node,
                requested_departure=departure,
                profile_key=profile,
                strategy="egress_anchor_head",
                source_query="egress_anchor",
            )
            if first.get("kind") != "mixed" or float(first.get("transitDistance") or 0) < 800:
                continue
            result.append(self._combine_egress_route(first, bike, anchor, profile))
        result.sort(key=lambda r: (r["score"], r["doorToDoor"]))
        return result[:2]

    def _combine_boarding_route(
        self,
        bike: dict[str, Any],
        tail: dict[str, Any],
        anchor: Anchor,
        departure: datetime,
        profile: str,
    ) -> dict[str, Any]:
        bike_legs = deepcopy(bike.get("legs") or [])
        cursor = departure
        for leg in bike_legs:
            leg["startTime"] = cursor.isoformat()
            cursor += timedelta(seconds=int(leg.get("duration") or 0))
            leg["endTime"] = cursor.isoformat()

        tail_start = parse_otp_time(tail.get("start")) or cursor
        wait_gap = max(0, int((tail_start - cursor).total_seconds()))
        tail_end = parse_otp_time(tail.get("end")) or tail_start + timedelta(seconds=int(tail.get("duration") or 0))
        route = {
            "id": "",
            "kind": "mixed",
            "strategy": "boarding_anchor",
            "sourceQuery": "boarding_anchor",
            "duration": max(0, int((tail_end - departure).total_seconds())),
            "initialWait": 0,
            "doorToDoor": max(0, int((tail_end - departure).total_seconds())),
            "start": departure.isoformat(),
            "end": tail_end.isoformat(),
            "generalizedCost": None,
            "waitingTime": max(int(tail.get("waitingTime") or 0), wait_gap),
            "legs": bike_legs + deepcopy(tail.get("legs") or []),
            "anchor": self._anchor_json(anchor, "boarding", bike_distance=float(bike.get("bikeDistance") or 0)),
        }
        return self._score_route(route, profile)

    def _combine_egress_route(
        self,
        head: dict[str, Any],
        bike: dict[str, Any],
        anchor: Anchor,
        profile: str,
    ) -> dict[str, Any]:
        first_end = parse_otp_time(head.get("end"))
        if first_end is None:
            return head
        bike_legs = deepcopy(bike.get("legs") or [])
        cursor = first_end
        for leg in bike_legs:
            leg["startTime"] = cursor.isoformat()
            cursor += timedelta(seconds=int(leg.get("duration") or 0))
            leg["endTime"] = cursor.isoformat()

        start = parse_otp_time(head.get("start")) or first_end - timedelta(seconds=int(head.get("duration") or 0))
        route = {
            "id": "",
            "kind": "mixed",
            "strategy": "egress_anchor",
            "sourceQuery": "egress_anchor",
            "duration": max(0, int((cursor - start).total_seconds())),
            "initialWait": int(head.get("initialWait") or 0),
            "doorToDoor": int(head.get("initialWait") or 0) + max(0, int((cursor - start).total_seconds())),
            "start": start.isoformat(),
            "end": cursor.isoformat(),
            "generalizedCost": None,
            "waitingTime": int(head.get("waitingTime") or 0),
            "legs": deepcopy(head.get("legs") or []) + bike_legs,
            "anchor": self._anchor_json(anchor, "egress", bike_distance=float(bike.get("bikeDistance") or 0)),
        }
        return self._score_route(route, profile)

    @staticmethod
    def _anchor_json(anchor: Anchor, role: str, bike_distance: float) -> dict[str, Any]:
        data = {
            "type": role,
            "stopId": anchor.stop_id,
            "name": anchor.name,
            "lat": anchor.lat,
            "lon": anchor.lon,
            "routeCount": anchor.route_count,
            "modes": list(anchor.modes),
            "bestTrunkScore": round(anchor.best_trunk_score, 3),
            "trunkRoutes": list(anchor.trunk_routes),
        }
        if role == "boarding":
            data["bikeAccessDistance"] = round(bike_distance, 1)
        else:
            data["bikeEgressDistance"] = round(bike_distance, 1)
        return data

    # ------------------------------------------------------------ OTP bike helper

    def _bike_between_leg_endpoints(
        self,
        leg: dict[str, Any],
        departure: datetime,
        profile: str,
    ) -> dict[str, Any] | None:
        a = leg.get("from") or {}
        b = leg.get("to") or {}
        try:
            origin = (float(a["lat"]), float(a["lon"]))
            destination = (float(b["lat"]), float(b["lon"]))
        except (KeyError, TypeError, ValueError):
            return None
        return self._bike_between_points(origin, destination, departure, profile)

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
            candidates = [
                self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="bike_segment",
                    source_query="bike_segment",
                )
                for node in nodes
            ]
            candidates = [r for r in candidates if r.get("kind") == "bike"]
            result = min(candidates, key=lambda r: r["duration"]) if candidates else None
        except Exception:
            result = None

        with self._bike_cache_lock:
            self._bike_cache[key] = deepcopy(result) if result is not None else None
        return deepcopy(result) if result is not None else None

    # --------------------------------------------------------- normalize / metrics

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
        initial_wait = (
            max(0, int((start_dt - requested_departure).total_seconds())) if start_dt else 0
        )

        legs_out: list[dict[str, Any]] = []
        for leg in itinerary.get("legs") or []:
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
            trip_id = (leg.get("trip") or {}).get("gtfsId")
            metrics = self.gtfs.line_metrics_for_trip(trip_id) if leg.get("transitLeg") else None
            legs_out.append(
                {
                    "mode": leg.get("mode") or "UNKNOWN",
                    "transitLeg": bool(leg.get("transitLeg")),
                    "duration": int(float(leg.get("duration") or 0)),
                    "distance": round(float(leg.get("distance") or 0), 1),
                    "startTime": leg.get("startTime"),
                    "endTime": leg.get("endTime"),
                    "realTime": bool(leg.get("realTime")),
                    "interlineWithPreviousLeg": bool(leg.get("interlineWithPreviousLeg")),
                    "tripId": trip_id,
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
                    "lineMetrics": self._line_metrics_json(metrics) if metrics else None,
                    "geometry": {"type": "LineString", "coordinates": geometry},
                }
            )

        has_transit = any(leg.get("transitLeg") for leg in legs_out)
        has_bike = any(leg.get("mode") == "BICYCLE" for leg in legs_out)
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
            "waitingTime": int(float(itinerary.get("waitingTime") or 0)),
            "legs": legs_out,
        }
        return self._score_route(route, profile_key)

    @staticmethod
    def _line_metrics_json(metrics: LineMetrics) -> dict[str, Any]:
        return {
            "routeId": metrics.route_id,
            "mode": metrics.mode,
            "tripCount": metrics.trip_count,
            "medianHeadway": round(metrics.median_headway_s) if metrics.median_headway_s else None,
            "commercialSpeedKmh": round(metrics.commercial_speed_kmh, 1) if metrics.commercial_speed_kmh else None,
            "bikesAllowedRatio": round(metrics.bikes_allowed_ratio, 3) if metrics.bikes_allowed_ratio is not None else None,
            "trunkScore": round(metrics.trunk_score, 3),
        }

    @staticmethod
    def _actual_transfer_count(route: dict[str, Any]) -> int:
        boardings = 0
        previous_trip: str | None = None
        for leg in route.get("legs") or []:
            if not leg.get("transitLeg"):
                previous_trip = None
                continue
            trip_id = leg.get("tripId")
            if leg.get("interlineWithPreviousLeg"):
                previous_trip = trip_id or previous_trip
                continue
            if trip_id and previous_trip and trip_id == previous_trip:
                continue
            boardings += 1
            previous_trip = trip_id
        return max(0, boardings - 1)

    def _refresh_route_metrics(self, route: dict[str, Any]) -> dict[str, Any]:
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
            float(leg.get("distance") or 0) for leg in legs if leg.get("transitLeg")
        )
        transit_legs = [leg for leg in legs if leg.get("transitLeg")]
        transfers = self._actual_transfer_count(route)
        boardings = transfers + (1 if transit_legs else 0)

        movement = bike_distance + transit_distance
        route["bikeDistance"] = round(bike_distance, 1)
        route["walkDistance"] = round(walk_distance, 1)
        route["transitDistance"] = round(transit_distance, 1)
        route["transfers"] = transfers
        route["bikeBoardings"] = boardings
        route["bikeShare"] = round(bike_distance / movement, 4) if movement else 0.0
        route["transitShare"] = round(transit_distance / movement, 4) if movement else 0.0

        trunk_weight = 0.0
        trunk_duration = 0.0
        weak_micro_penalty = 0.0
        best_trunk = 0.0
        best_trunk_name: str | None = None
        for leg in transit_legs:
            lm = leg.get("lineMetrics") or {}
            trunk = float(lm.get("trunkScore") or 0.50)
            duration = float(leg.get("duration") or 0)
            distance = float(leg.get("distance") or 0)
            trunk_weight += trunk * duration
            trunk_duration += duration
            route_name = (leg.get("route") or {}).get("shortName") or (leg.get("route") or {}).get("longName")
            if trunk > best_trunk:
                best_trunk = trunk
                best_trunk_name = route_name
            if distance < 1_400 and trunk < 0.48:
                weak_micro_penalty += 120
            if duration < 4 * 60 and trunk < 0.42:
                weak_micro_penalty += 60

        route["avgTrunkScore"] = round(trunk_weight / trunk_duration, 3) if trunk_duration else 0.0
        route["bestTrunkScore"] = round(best_trunk, 3)
        route["bestTrunkRoute"] = best_trunk_name
        route["microTransitPenalty"] = round(weak_micro_penalty)
        return route

    def _score_route(self, route: dict[str, Any], profile_key: str) -> dict[str, Any]:
        route = self._refresh_route_metrics(route)
        cfg = PROFILE_CONFIG[profile_key]

        wait_cost = float(route.get("waitingTime") or 0) * float(cfg["wait_factor"])
        transfer_cost = int(route.get("transfers") or 0) * float(cfg["transfer_penalty"])
        boarding_cost = int(route.get("bikeBoardings") or 0) * float(cfg["bike_boarding_penalty"])
        walk_cost = (float(route.get("walkDistance") or 0) / 1000.0) * 180.0
        micro_penalty = float(route.get("microTransitPenalty") or 0)

        avg_trunk = float(route.get("avgTrunkScore") or 0)
        transit_seconds = sum(
            int(leg.get("duration") or 0)
            for leg in route.get("legs") or []
            if leg.get("transitLeg")
        )
        trunk_bonus = min(260.0, max(0.0, avg_trunk - 0.55) * transit_seconds * 0.45)

        discomfort = max(
            0.0,
            wait_cost + transfer_cost + boarding_cost + walk_cost + micro_penalty - trunk_bonus,
        )
        score = float(route.get("doorToDoor") or 0) + discomfort

        # A mixed route whose transit contribution is almost cosmetic should not
        # outrank a clean direct bicycle route merely because of schedule noise.
        if route.get("kind") == "mixed":
            transit_distance = float(route.get("transitDistance") or 0)
            movement = transit_distance + float(route.get("bikeDistance") or 0)
            if transit_distance < 650:
                score += 360
            if movement and transit_distance / movement < 0.07:
                score += 260

        route["discomfort"] = round(discomfort)
        route["trunkBonus"] = round(trunk_bonus)
        route["baseScore"] = round(score)
        route["score"] = round(score)
        route["transitModes"] = transit_modes(route)
        route["transitRoutes"] = transit_route_names(route)
        return route

    # ------------------------------------------------------------ focus / Pareto

    @staticmethod
    def _base_target_bike_share(reference_distance_m: float) -> float:
        if reference_distance_m <= 4_000:
            return 0.90
        if reference_distance_m <= 8_000:
            return 0.70
        if reference_distance_m <= 15_000:
            return 0.45
        if reference_distance_m <= 25_000:
            return 0.22
        return 0.10

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

        direct_bikes = [r for r in routes if r.get("kind") == "bike" and r.get("bikeDistance")]
        if direct_bikes:
            reference_distance = min(float(r["bikeDistance"]) for r in direct_bikes)
        else:
            reference_distance = max(
                float(r.get("bikeDistance") or 0) + float(r.get("transitDistance") or 0)
                for r in routes
            )

        target = self._base_target_bike_share(reference_distance)
        target += { -2: -0.20, -1: -0.10, 0: 0.0, 1: 0.15, 2: 0.30 }[route_focus]
        target = max(0.05, min(0.95, target))

        best_time = max(1, min(int(r.get("doorToDoor") or 0) for r in routes))
        allowed_ratio = 1.0 + float(focus_cfg["time_tolerance_ratio"])
        result: list[dict[str, Any]] = []

        for raw in routes:
            route = self._refresh_route_metrics(raw)
            bike_share = float(route.get("bikeShare") or 0)
            if route_focus < 0:
                share_gap = max(0.0, bike_share - target)
            elif route_focus > 0:
                share_gap = max(0.0, target - bike_share)
            else:
                share_gap = abs(bike_share - target)

            share_penalty = share_gap * float(focus_cfg["share_penalty_seconds"])
            transfer_adjustment = (
                int(route.get("transfers") or 0)
                * float(profile_cfg["transfer_penalty"])
                * (float(focus_cfg["transfer_penalty_factor"]) - 1.0)
            )
            time_ratio = float(route.get("doorToDoor") or best_time) / best_time
            detour_penalty = 0.0
            if time_ratio > allowed_ratio:
                detour_penalty = (time_ratio - allowed_ratio) * best_time * 3.0 + 240.0

            route["score"] = round(
                float(route.get("baseScore") or 0)
                + share_penalty
                + transfer_adjustment
                + detour_penalty
            )
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

    def _pareto_prune(self, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep routes not clearly dominated on time, transfers and discomfort.

        Small epsilons avoid retaining dozens of routes that differ by seconds,
        while still preserving meaningful trade-offs such as no-transfer vs faster.
        """
        if len(routes) <= 2:
            return routes

        kept: list[dict[str, Any]] = []
        for candidate in routes:
            dominated = False
            c_time = float(candidate.get("doorToDoor") or math.inf)
            c_transfers = int(candidate.get("transfers") or 0)
            c_discomfort = float(candidate.get("discomfort") or 0)
            for other in routes:
                if other is candidate:
                    continue
                o_time = float(other.get("doorToDoor") or math.inf)
                o_transfers = int(other.get("transfers") or 0)
                o_discomfort = float(other.get("discomfort") or 0)

                no_worse = (
                    o_time <= c_time + 45
                    and o_transfers <= c_transfers
                    and o_discomfort <= c_discomfort + 45
                )
                materially_better = (
                    o_time < c_time - 90
                    or o_transfers < c_transfers
                    or o_discomfort < c_discomfort - 90
                )
                if no_worse and materially_better:
                    # Do not let a mixed route erase the direct-bike baseline; it
                    # is an important user-visible strategy even when slower.
                    if candidate.get("kind") == "bike" and other.get("kind") != "bike":
                        continue
                    dominated = True
                    break
            if not dominated:
                kept.append(candidate)

        if not kept:
            kept = sorted(routes, key=lambda r: (r["score"], r["doorToDoor"]))[:12]
        return sorted(kept, key=lambda r: (r["score"], r["doorToDoor"]))[:40]

    # ------------------------------------------------------- similarity / diversity

    def _select_diverse(
        self,
        routes: list[dict[str, Any]],
        *,
        limit: int,
        route_focus: int,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []
        route_focus = min(2, max(-2, int(route_focus)))
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]
        routes = sorted(routes, key=lambda r: (r["score"], r["doorToDoor"]))
        best_time = min(float(r["doorToDoor"]) for r in routes)
        soft_limit = max(
            best_time * (1.0 + float(focus_cfg["time_tolerance_ratio"])),
            best_time + { -2: 10*60, -1: 12*60, 0: 15*60, 1: 22*60, 2: 30*60 }[route_focus],
        )
        eligible = [r for r in routes if float(r["doorToDoor"]) <= soft_limit]
        if not eligible:
            eligible = routes

        # First collapse high-overlap clusters. This specifically prevents
        # "same bus + same transfer + 300 m different bicycle approach" clones.
        representatives: list[dict[str, Any]] = []
        for route in eligible:
            cluster_index = next(
                (
                    i
                    for i, rep in enumerate(representatives)
                    if self._route_similarity(route, rep) >= 0.80
                ),
                None,
            )
            if cluster_index is None:
                representatives.append(route)
            else:
                rep = representatives[cluster_index]
                if (route["score"], route["doorToDoor"]) < (rep["score"], rep["doorToDoor"]):
                    representatives[cluster_index] = route

        direct_bike = self._best_matching(routes, lambda r: r.get("kind") == "bike")
        if direct_bike is not None and all(id(x) != id(direct_bike) for x in representatives):
            representatives.append(direct_bike)

        selected: list[dict[str, Any]] = []

        def add(route: dict[str, Any] | None, force: bool = False) -> None:
            if route is None or len(selected) >= limit or route in selected:
                return
            if not force and route.get("kind") != "bike" and float(route["doorToDoor"]) > soft_limit:
                return
            max_sim = max((self._route_similarity(route, x) for x in selected), default=0.0)
            if not force and max_sim >= 0.84:
                return
            selected.append(route)

        # Archetypes are filled before generic MMR. Each slot still passes the
        # similarity gate, so labels correspond to genuinely different strategies.
        add(min(representatives, key=lambda r: r["doorToDoor"]))
        add(min(representatives, key=lambda r: (r["score"], r["doorToDoor"])))
        add(self._best_matching(representatives, lambda r: r.get("strategy") == "boarding_anchor"))
        add(self._best_matching(representatives, lambda r: r.get("strategy") == "egress_anchor"))
        add(self._best_matching(representatives, lambda r: "RAIL" in r.get("transitModes", [])))
        add(
            min(
                representatives,
                key=lambda r: (int(r.get("transfers") or 0), r["score"]),
            )
            if representatives
            else None
        )
        add(direct_bike, force=True)

        lambda_similarity = { -2: 300.0, -1: 330.0, 0: 360.0, 1: 390.0, 2: 420.0 }[route_focus]
        while len(selected) < limit:
            remaining = [r for r in representatives if r not in selected]
            if not remaining:
                break
            scored = []
            for route in remaining:
                max_sim = max((self._route_similarity(route, x) for x in selected), default=0.0)
                diversity_cost = float(route["score"]) + lambda_similarity * max_sim
                scored.append((diversity_cost, max_sim, float(route["doorToDoor"]), route))
            scored.sort(key=lambda item: (item[0], item[1], item[2]))
            chosen = next((item[3] for item in scored if item[1] < 0.86), None)
            if chosen is None:
                break
            add(chosen)

        selected.sort(key=lambda r: (r["score"], r["doorToDoor"]))
        for index, route in enumerate(selected):
            route["id"] = f"route-{index}"
            route["diversity"] = {
                "maxSimilarityToOtherShown": round(
                    max(
                        (self._route_similarity(route, other) for other in selected if other is not route),
                        default=0.0,
                    ),
                    3,
                )
            }
        return selected

    def _route_similarity(self, a: dict[str, Any], b: dict[str, Any]) -> float:
        a_transit = self._corridor_cells(a, transit=True)
        b_transit = self._corridor_cells(b, transit=True)
        a_bike = self._corridor_cells(a, transit=False)
        b_bike = self._corridor_cells(b, transit=False)

        if not a_transit and not b_transit:
            return self._set_overlap(a_bike, b_bike)
        if not a_transit or not b_transit:
            return 0.20 * self._set_overlap(a_bike, b_bike)

        transit_corridor = self._set_overlap(a_transit, b_transit)
        line_overlap = self._set_overlap(
            set(self._transit_chain_signature(a)),
            set(self._transit_chain_signature(b)),
        )
        transit_overlap = 0.75 * transit_corridor + 0.25 * line_overlap
        bike_overlap = self._set_overlap(a_bike, b_bike)
        transfer_overlap = self._set_overlap(self._transfer_stops(a), self._transfer_stops(b))
        return max(0.0, min(1.0, 0.60 * transit_overlap + 0.25 * bike_overlap + 0.15 * transfer_overlap))

    @staticmethod
    def _set_overlap(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / max(1, min(len(a), len(b)))

    def _corridor_cells(self, route: dict[str, Any], *, transit: bool) -> set[tuple[int, int]]:
        cells: set[tuple[int, int]] = set()
        for leg in route.get("legs") or []:
            is_transit = bool(leg.get("transitLeg"))
            if is_transit != transit:
                if transit or leg.get("mode") != "BICYCLE":
                    continue
            elif not transit and leg.get("mode") != "BICYCLE":
                continue
            coords = ((leg.get("geometry") or {}).get("coordinates") or [])
            for lon, lat in coords[:: max(1, len(coords) // 80 or 1)]:
                # ~350-400 m grid around Moscow. A slightly coarser corridor
                # cell is deliberate: access legs shifted by one parallel street
                # must not masquerade as a genuinely different trip strategy.
                cells.add((round(float(lat) * 300), round(float(lon) * 180)))
        return cells

    @staticmethod
    def _transfer_stops(route: dict[str, Any]) -> set[str]:
        transit = [leg for leg in route.get("legs") or [] if leg.get("transitLeg")]
        stops: set[str] = set()
        for left, right in zip(transit, transit[1:]):
            stops.add(str((left.get("to") or {}).get("name") or ""))
            stops.add(str((right.get("from") or {}).get("name") or ""))
        return {s for s in stops if s}

    @staticmethod
    def _transit_chain_signature(route: dict[str, Any]) -> tuple[str, ...]:
        result: list[str] = []
        for leg in route.get("legs") or []:
            if not leg.get("transitLeg"):
                continue
            r = leg.get("route") or {}
            name = r.get("shortName") or r.get("longName") or leg.get("mode") or "?"
            token = f"{leg.get('mode')}:{name}"
            if not result or result[-1] != token:
                result.append(token)
        return tuple(result)

    # ------------------------------------------------------------- explanations

    def _assign_recommendation_labels(self, routes: list[dict[str, Any]]) -> None:
        if not routes:
            return
        fastest = min(routes, key=lambda r: r["doorToDoor"])
        optimal = min(routes, key=lambda r: r["score"])
        min_transfers = min(routes, key=lambda r: (r.get("transfers", 0), r["doorToDoor"]))

        used: set[str] = set()
        for route in routes:
            if route is fastest:
                label = "Самый быстрый"
            elif route is optimal:
                label = "Оптимальный баланс"
            elif route.get("strategy") == "boarding_anchor":
                label = "Велосипед к сильной линии"
            elif route.get("strategy") == "egress_anchor":
                label = "Ранний выход → велосипед"
            elif route is min_transfers and route.get("kind") == "mixed":
                label = "Меньше пересадок"
            elif route.get("kind") == "bike":
                label = "Только велосипед"
            elif "RAIL" in route.get("transitModes", []):
                label = "Поезд + велосипед"
            elif float(route.get("bestTrunkScore") or 0) >= 0.68:
                label = "Сильный транспортный коридор"
            else:
                label = "Альтернатива"

            # Repeated labels are unhelpful; fall back to the strategy name.
            if label in used and label not in {"Самый быстрый", "Только велосипед"}:
                label = "Альтернатива"
            used.add(label)
            route["recommendation"] = label

    def _add_explanations(self, routes: list[dict[str, Any]]) -> None:
        for route in routes:
            notes: list[str] = []
            anchor = route.get("anchor") or {}
            if anchor.get("type") == "boarding":
                km = float(anchor.get("bikeAccessDistance") or 0) / 1000.0
                notes.append(f"Велоподъезд {km:.1f} км к более сильной точке посадки")
            elif anchor.get("type") == "egress":
                km = float(anchor.get("bikeEgressDistance") or 0) / 1000.0
                notes.append(f"Ранний выход из транспорта и {km:.1f} км на велосипеде")

            optimization = route.get("optimization") or {}
            removed = int(optimization.get("replacedTransitCount") or 0)
            saved = int(optimization.get("savedSecondsEstimate") or 0)
            if removed:
                notes.append(
                    f"Пропущено слабых коротких участков ОТ: {removed}"
                    + (f" · ≈{round(saved / 60)} мин экономии" if saved >= 60 else "")
                )

            if float(route.get("bestTrunkScore") or 0) >= 0.68 and route.get("bestTrunkRoute"):
                notes.append(f"Используется сильная линия {route['bestTrunkRoute']}")
            if route.get("kind") == "mixed" and int(route.get("transfers") or 0) == 0:
                notes.append("Без пересадок между маршрутами ОТ")
            route["explanations"] = notes[:3]

    # --------------------------------------------------------------- utilities

    def _bike_carriage_is_legal(self, route: dict[str, Any]) -> bool:
        """Reject only trips that explicitly forbid bicycles in GTFS.

        Blank/unknown values remain allowed for compatibility with incomplete
        feeds. The Moscow prototype currently relies on a temporary policy patch,
        so making unknown values fatal here would incorrectly erase most routes.
        """
        for leg in route.get("legs") or []:
            if not leg.get("transitLeg"):
                continue
            if self.gtfs.bike_allowed_for_trip(leg.get("tripId")) == 2:
                return False
        return True

    @staticmethod
    def _parse_leg_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(MOSCOW_TZ)
        if isinstance(value, (int, float)):
            number = float(value)
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
        for raw in legs:
            leg = deepcopy(raw)
            if (
                result
                and result[-1].get("mode") == "BICYCLE"
                and not result[-1].get("transitLeg")
                and leg.get("mode") == "BICYCLE"
                and not leg.get("transitLeg")
            ):
                previous = result[-1]
                previous["duration"] = int(previous.get("duration") or 0) + int(leg.get("duration") or 0)
                previous["distance"] = round(float(previous.get("distance") or 0) + float(leg.get("distance") or 0), 1)
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
                result.append(leg)
        return result

    def _dedupe_exact(self, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple, dict[str, Any]] = {}
        for route in routes:
            key = self._exact_signature(route)
            previous = best.get(key)
            if previous is None or (
                float(route.get("score") or math.inf),
                float(route.get("doorToDoor") or math.inf),
            ) < (
                float(previous.get("score") or math.inf),
                float(previous.get("doorToDoor") or math.inf),
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
        result: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for warning in warnings:
            key = (warning.get("code"), warning.get("description"))
            if key in seen:
                continue
            seen.add(key)
            result.append(warning)
        return result

    @staticmethod
    def _best_matching(
        routes: list[dict[str, Any]],
        predicate: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any] | None:
        matches = [route for route in routes if predicate(route)]
        return min(matches, key=lambda r: (r.get("score", math.inf), r.get("doorToDoor", math.inf))) if matches else None

    @staticmethod
    def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
        try:
            return min(high, max(low, int(value)))
        except (TypeError, ValueError):
            return default
