from __future__ import annotations

import concurrent.futures
import math
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Callable

from .diagnostics import RoutingDiagnostics
from .gtfs_index import Anchor, GtfsIndex, LineMetrics
from .models import (
    BICYCLE_ROUTE_VARIANTS,
    DEFAULT_PROFILE,
    MOSCOW_TZ,
    PROFILE_CONFIG,
    ROUTE_FOCUS_CONFIG,
    RouteFocusConfig,
    decode_polyline,
    parse_coordinate,
    parse_departure,
    parse_otp_time,
    transit_modes,
    transit_route_names,
)
from .otp_client import OTPClient, coordinate_location, stop_location


ALL_TRANSIT = ["BUS", "TRAM", "TROLLEYBUS", "RAIL", "SUBWAY"]
MODE_QUERIES = {
    "bus": ["BUS", "TROLLEYBUS"],
    "tram": ["TRAM"],
    "rail": ["RAIL", "SUBWAY"],
    "bus_tram": ["BUS", "TROLLEYBUS", "TRAM"],
    "bus_rail": ["BUS", "TROLLEYBUS", "RAIL", "SUBWAY"],
    "tram_rail": ["TRAM", "RAIL", "SUBWAY"],
}
SOFT_ROUTING_ERRORS = {
    "NO_STOPS_IN_RANGE",
    "WALKING_BETTER_THAN_TRANSIT",
    "NO_TRANSIT_CONNECTION",
    "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW",
}
INTERNAL_MAX_TRANSIT_TRANSFERS = 4
FINAL_ROUTE_LIMIT = 20


class RouteEditConflict(ValueError):
    """A manual edit is valid geometrically but breaks a scheduled connection."""


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

        # `profile` remains accepted for API compatibility but no longer changes
        # routing behaviour.  Several bicycle street strategies are generated
        # together below.
        requested_profile = payload.get("profile")
        profile = DEFAULT_PROFILE
        requested_max_transfers = payload.get("maxTransfers")
        requested_route_focus = payload.get("routeFocus")
        max_transfers = INTERNAL_MAX_TRANSIT_TRANSFERS
        # Journey settings were removed from the product. Focus remains an
        # internal candidate-family axis and AUTO (0) deliberately generates
        # conservative, balanced and bicycle-heavy optimization variants.
        route_focus = 0
        deep_search = bool(payload.get("deepSearch", True))
        diagnostics = RoutingDiagnostics(enabled=bool(payload.get("debugRouting", False)))
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]

        # Warm the line/stop metrics once. If GTFS indexing fails, all routing
        # still works; it simply falls back to neutral line-quality assumptions.
        self.gtfs.ensure_loaded()

        generic_routes, warnings, generic_stats = self._generic_candidates(
            origin, destination, departure, profile, max_transfers, route_focus
        )
        direct_bikes = [r for r in generic_routes if r.get("kind") == "bike"]
        for family, count in generic_stats["families"].items():
            diagnostics.generated_candidates(family, count)
        for route in generic_routes:
            diagnostics.event(
                "candidate_generated",
                route,
                family=str(route.get("sourceQuery") or "generic"),
            )

        transit_routes, transit_warnings, transit_stats = self._transit_first_candidates(
            origin,
            destination,
            departure,
            profile,
            max_transfers,
            route_focus,
            seed_skeletons=[r for r in generic_routes if r.get("kind") == "mixed"],
            diagnostics=diagnostics,
        )
        warnings.extend(transit_warnings)
        diagnostics.generated_candidates("transitOptimized", len(transit_routes), transit_routes)

        boarding_anchors: list[Anchor] = []
        egress_anchors: list[Anchor] = []
        boarding_routes: list[dict[str, Any]] = []
        egress_routes: list[dict[str, Any]] = []
        anchor_bike_comparisons = 0
        trunk_searches = 0
        deep_error: str | None = None

        if deep_search and self.gtfs.loaded:
            try:
                anchor_limit = int(focus_cfg.anchor_limit)
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
                trunk_searches = sum(
                    1
                    for anchor in boarding_anchors + egress_anchors
                    if anchor.best_trunk_score >= 0.58
                    or set(anchor.modes).intersection({"RAIL", "SUBWAY", "TRAM"})
                )
                diagnostics.generated_candidates("trunkSearches", trunk_searches)
                boarding_routes, egress_routes = self._anchor_candidates(
                    boarding_anchors=boarding_anchors,
                    egress_anchors=egress_anchors,
                    origin=origin,
                    destination=destination,
                    departure=departure,
                    profile=profile,
                    max_transfers=max_transfers,
                    route_focus=route_focus,
                )
                diagnostics.generated_candidates(
                    "boardingAnchorRaw",
                    len(boarding_routes),
                    boarding_routes,
                )
                diagnostics.generated_candidates(
                    "egressAnchorRaw",
                    len(egress_routes),
                    egress_routes,
                )
                boarding_routes, boarding_comparisons = self._optimize_candidate_set(
                    boarding_routes,
                    requested_departure=departure,
                    profile=profile,
                    route_focus=route_focus,
                    diagnostics=diagnostics,
                )
                egress_routes, egress_comparisons = self._optimize_candidate_set(
                    egress_routes,
                    requested_departure=departure,
                    profile=profile,
                    route_focus=route_focus,
                    diagnostics=diagnostics,
                )
                anchor_bike_comparisons = boarding_comparisons + egress_comparisons
            except Exception as exc:
                deep_error = str(exc)
        diagnostics.generated_candidates("boardingAnchors", len(boarding_routes), boarding_routes)
        diagnostics.generated_candidates("egressAnchors", len(egress_routes), egress_routes)

        pool = list(direct_bikes)
        pool.extend(transit_routes)
        pool.extend(boarding_routes)
        pool.extend(egress_routes)
        raw_pool_count = len(pool)
        pool = self._dedupe_exact(pool)
        deduped_pool_count = len(pool)

        normalized_pool: list[dict[str, Any]] = []
        bike_policy_filtered = 0
        transfer_filtered = 0
        for route in pool:
            if not self._candidate_is_valid(route):
                diagnostics.reject("invalid", route)
                continue
            if not self._bike_carriage_is_legal(route):
                bike_policy_filtered += 1
                diagnostics.reject("bikePolicy", route)
                continue
            route = self._score_route(route, profile)
            normalized_pool.append(route)

        focused = self._apply_route_focus(
            normalized_pool,
            profile_key=profile,
            route_focus=route_focus,
        )
        focused = self._classify_strategies(focused, route_focus=route_focus)
        diagnostics.count_strategies(focused)
        pareto = self._pareto_prune(
            focused,
            route_focus=route_focus,
            diagnostics=diagnostics,
        )
        selected = self._select_diverse(
            pareto,
            limit=FINAL_ROUTE_LIMIT,
            route_focus=route_focus,
            diagnostics=diagnostics,
        )
        self._assign_recommendation_labels(selected)
        self._add_explanations(selected)

        if selected:
            warnings = [w for w in warnings if w.get("code") not in SOFT_ROUTING_ERRORS]

        stats = {
            "algorithm": "hybrid-strategy-v0.6",
            "routingPipelineVersion": 7,
            "genericQueries": generic_stats["queries"],
            "genericCandidates": len(generic_routes),
            "transitSkeletonQueries": transit_stats["queries"],
            "transitSkeletons": transit_stats["skeletons"],
            "transitOptimizedCandidates": transit_stats["optimizedCandidates"],
            "publicTransportCandidates": transit_stats["publicTransportCandidates"],
            "optimizerFocusVariants": list(focus_cfg.optimizer_focus_variants),
            "bikeComparisons": transit_stats["bikeComparisons"],
            "anchorBikeComparisons": anchor_bike_comparisons,
            "boardingAnchorsConsidered": len(boarding_anchors),
            "boardingAnchorCandidates": len(boarding_routes),
            "egressAnchorsConsidered": len(egress_anchors),
            "anchorCandidates": len(egress_routes),  # backwards-compatible UI field
            "egressAnchorCandidates": len(egress_routes),
            "trunkSearches": trunk_searches,
            "candidatesBeforePareto": len(focused),
            "paretoCandidates": len(pareto),
            "candidatesTotal": len(focused),
            "candidateStages": {
                "rawPool": raw_pool_count,
                "afterExactDedupe": deduped_pool_count,
                "scored": len(normalized_pool),
                "afterPareto": len(pareto),
                "returned": len(selected),
            },
            "transferFiltered": transfer_filtered,
            "bikePolicyFiltered": bike_policy_filtered,
            "maxTransfers": max_transfers,
            "transferSettingIgnored": requested_max_transfers is not None,
            "routeFocus": route_focus,
            "routeFocusName": focus_cfg.name,
            "routeFocusIgnored": requested_route_focus is not None,
            "routeFocusGeneration": {
                "maxBikeAccessM": focus_cfg.max_bike_access_m,
                "maxBikeEgressM": focus_cfg.max_bike_egress_m,
                "anchorLimit": focus_cfg.anchor_limit,
                "modeFamilies": list(focus_cfg.generic_mode_families),
                "departureOffsetsMin": list(
                    focus_cfg.transit_departure_offsets_min
                ),
                "transferCaps": list(focus_cfg.transit_transfer_caps),
                "optimizerFocusVariants": list(
                    focus_cfg.optimizer_focus_variants
                ),
            },
            "profileIgnored": requested_profile is not None,
            "bicycleStrategies": [item["key"] for item in BICYCLE_ROUTE_VARIANTS],
            "returned": len(selected),
            "deepSearchAvailable": self.gtfs.loaded,
            "deepSearchError": deep_error or self.gtfs.error,
            "pipeline": diagnostics.stats(),
            "elapsedMs": round((time.monotonic() - started) * 1000),
        }
        result = {
            "routes": selected,
            "warnings": self._dedupe_warnings(warnings),
            "stats": stats,
        }
        if diagnostics.enabled:
            result["debugTrace"] = diagnostics.trace()
        return result

    def replace_with_bicycle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace a contiguous range of route legs with one OTP bicycle route.

        Boundaries are indexes between legs: ``0`` is the route origin and
        ``len(legs)`` is the destination. Remaining scheduled transit keeps its
        original departure. The edit is rejected when the new bicycle segment
        can no longer catch one of those departures.
        """

        source = payload.get("route")
        if not isinstance(source, dict):
            raise ValueError("Для изменения нужен исходный маршрут.")
        route = deepcopy(source)
        legs = route.get("legs")
        if not isinstance(legs, list) or not legs:
            raise ValueError("В исходном маршруте нет участков.")
        if len(legs) > 80:
            raise ValueError("В маршруте слишком много участков для ручного изменения.")
        if any(
            str(leg.get("mode") or "").upper() in {"CAR", "MOTORCYCLE"}
            for leg in legs
            if isinstance(leg, dict)
        ):
            raise ValueError("Автомобильные участки нельзя использовать в веломаршруте.")

        try:
            start_boundary = int(payload.get("startBoundary"))
            end_boundary = int(payload.get("endBoundary"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректные границы велосипедной замены.") from exc
        if not 0 <= start_boundary < end_boundary <= len(legs):
            raise ValueError("Начало замены должно находиться раньше её конца.")

        requested_departure = parse_departure(
            payload.get("departureTime") or route.get("start")
        )
        start_point = self._route_boundary(legs, start_boundary)
        end_point = self._route_boundary(legs, end_boundary)
        if start_point is None or end_point is None:
            raise ValueError("Не удалось определить координаты выбранных точек.")

        segment_departure = (
            self._parse_leg_timestamp((legs[start_boundary] or {}).get("startTime"))
            or requested_departure
        )
        bicycle = self._bike_between_points(
            (start_point["lat"], start_point["lon"]),
            (end_point["lat"], end_point["lon"]),
            segment_departure,
            DEFAULT_PROFILE,
        )
        if bicycle is None:
            raise ValueError("OTP не смог построить велосипедный путь между этими точками.")
        bicycle_legs = [
            deepcopy(leg)
            for leg in bicycle.get("legs") or []
            if leg.get("mode") == "BICYCLE" and not leg.get("transitLeg")
        ]
        if not bicycle_legs or len(bicycle_legs) != len(bicycle.get("legs") or []):
            raise ValueError("Велосипедная замена содержит недопустимый тип дороги.")

        bicycle_legs[0]["from"] = deepcopy(start_point["place"])
        bicycle_legs[-1]["to"] = deepcopy(end_point["place"])
        edit_record = {
            "startBoundary": start_boundary,
            "endBoundary": end_boundary,
            "from": start_point["place"].get("name") or "Начало",
            "to": end_point["place"].get("name") or "Конец",
            "replacedLegs": end_boundary - start_boundary,
            "bikeDistance": round(
                sum(float(leg.get("distance") or 0) for leg in bicycle_legs),
                1,
            ),
            "bikeDuration": sum(int(leg.get("duration") or 0) for leg in bicycle_legs),
        }
        for leg in bicycle_legs:
            leg["manualReplacement"] = deepcopy(edit_record)

        combined = (
            deepcopy(legs[:start_boundary])
            + bicycle_legs
            + deepcopy(legs[end_boundary:])
        )
        for leg in combined:
            if not leg.get("transitLeg"):
                continue
            fixed_start = leg.get("_fixedTransitStart") or leg.get("startTime")
            fixed_end = leg.get("_fixedTransitEnd") or leg.get("endTime")
            if fixed_start:
                leg["_fixedTransitStart"] = fixed_start
            if fixed_end:
                leg["_fixedTransitEnd"] = fixed_end

        if any(leg.get("transitLeg") for leg in combined):
            rebuilt = self._rebuild_schedule(
                combined,
                requested_departure=requested_departure,
            )
            if rebuilt is None:
                raise RouteEditConflict(
                    "Велосипедная замена не успевает на следующий рейс. "
                    "Выберите конец после этой пересадки или другой диапазон."
                )
            route_start, route_end, waiting_time, scheduled = rebuilt
        else:
            route_start = requested_departure
            route_end, scheduled = self._schedule_street_legs(
                combined,
                route_start=route_start,
            )
            waiting_time = 0

        route["id"] = str(route.get("id") or "")
        route["kind"] = (
            "mixed"
            if any(leg.get("transitLeg") for leg in scheduled)
            else "bike"
        )
        route["strategy"] = "manual_bicycle_edit"
        route["sourceQuery"] = "manual_bicycle_edit"
        route["streetPreference"] = "cycleway"
        route["start"] = route_start.isoformat()
        route["end"] = route_end.isoformat()
        route["initialWait"] = max(
            0,
            int((route_start - requested_departure).total_seconds()),
        )
        route["duration"] = max(0, int((route_end - route_start).total_seconds()))
        route["doorToDoor"] = max(
            0,
            int((route_end - requested_departure).total_seconds()),
        )
        route["waitingTime"] = waiting_time
        route["legs"] = scheduled
        route["manualEdits"] = (
            list(route.get("manualEdits") or [])[-19:] + [edit_record]
        )
        route["anchor"] = None
        route["generalizedCost"] = None

        route = self._score_route(route, DEFAULT_PROFILE)
        route = self._classify_strategies([route], route_focus=0)[0]
        self._assign_recommendation_labels([route])
        self._add_explanations([route])
        route["explanations"] = [
            f"Участок {edit_record['from']} → {edit_record['to']} заменён велосипедом"
        ] + list(route.get("explanations") or [])[:2]
        return {
            "route": route,
            "edit": edit_record,
        }

    # ------------------------------------------------------ candidate generation

    def _generic_candidates(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        selected_profile: str,
        max_transfers: int,
        route_focus: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        focus = ROUTE_FOCUS_CONFIG[route_focus]
        transfer_cap = max(0, max_transfers - focus.transfer_reduction)
        specs: list[dict[str, Any]] = [
            {
                "name": f"direct_{variant['key']}",
                "profile": variant["otp_profile"],
                "direct_bike": True,
                "direct_only": True,
                "strategy": variant["strategy"],
                "street_preference": variant["key"],
            }
            for variant in BICYCLE_ROUTE_VARIANTS
        ]
        # Generate multimodal access with every bicycle street hypothesis. The
        # old implementation varied only direct-bike routes, so moving focus
        # could not change the actual access corridor found by OTP.
        specs.extend(
            {
                "name": f"generic_{variant['key']}",
                "profile": variant["otp_profile"],
                "modes": ALL_TRANSIT,
                "strategy": f"generic_multimodal_{variant['key']}",
                "street_preference": variant["key"],
            }
            for variant in BICYCLE_ROUTE_VARIANTS
        )
        specs.extend(
            {
                "name": family,
                "profile": selected_profile,
                "modes": MODE_QUERIES[family],
                "strategy": f"generic_{family}",
                "street_preference": "cycleway",
            }
            for family in focus.generic_mode_families
            if family != "all"
        )
        origin_loc = coordinate_location(*origin, "Старт")
        destination_loc = coordinate_location(*destination, "Финиш")
        routes: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        family_counts: dict[str, int] = {}

        def run(spec: dict[str, Any]):
            nodes, local_warnings = self.otp.plan(
                origin=origin_loc,
                destination=destination_loc,
                departure=departure,
                profile=spec["profile"],
                transit_modes=spec.get("modes"),
                max_transfers=transfer_cap,
                direct_bike=bool(spec.get("direct_bike")),
                direct_only=bool(spec.get("direct_only")),
                transit_only=False,
                first=focus.otp_candidates_per_query,
            )
            local_routes = [
                self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=selected_profile,
                    strategy=spec["strategy"],
                    source_query=spec["name"],
                    street_preference=spec.get("street_preference"),
                )
                for node in nodes
            ]
            return spec["name"], [r for r in local_routes if r.get("kind") != "other"], local_warnings

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(run, spec) for spec in specs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    family, local, local_warnings = future.result()
                    routes.extend(local)
                    warnings.extend(local_warnings)
                    family_counts[family] = family_counts.get(family, 0) + len(local)
                except Exception as exc:
                    warnings.append({"code": "CANDIDATE_QUERY_FAILED", "description": str(exc)})

        return (
            self._dedupe_exact(routes),
            self._dedupe_warnings(warnings),
            {"queries": len(specs), "families": family_counts},
        )

    def _transit_first_candidates(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
        route_focus: int,
        seed_skeletons: list[dict[str, Any]] | None = None,
        diagnostics: RoutingDiagnostics | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        focus = ROUTE_FOCUS_CONFIG[route_focus]
        # Always include full-transfer skeletons. Bike-heavy routing needs to
        # see them before it can replace weak feeder/intermediate legs; reducing
        # the transfer cap at this stage used to erase those opportunities.
        specs: list[dict[str, Any]] = []
        seen_specs: set[tuple[tuple[str, ...], int, int]] = set()

        def add_spec(
            name: str,
            modes: list[str],
            transfers: int,
            offset_min: int = 0,
        ) -> None:
            key = (tuple(modes), min(max_transfers, transfers), offset_min)
            if key in seen_specs:
                return
            seen_specs.add(key)
            specs.append(
                {
                    "name": name,
                    "modes": modes,
                    "transfers": key[1],
                    "offsetMin": offset_min,
                }
            )

        for family in focus.generic_mode_families:
            modes = ALL_TRANSIT if family == "all" else MODE_QUERIES[family]
            add_spec(f"pt_{family}", modes, max_transfers)
        for offset_min in focus.transit_departure_offsets_min:
            for transfers in focus.transit_transfer_caps:
                suffix = f"{offset_min}m_t{transfers}"
                add_spec(f"pt_all_{suffix}", ALL_TRANSIT, transfers, offset_min)
        origin_loc = coordinate_location(*origin, "Старт")
        destination_loc = coordinate_location(*destination, "Финиш")
        skeletons: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        def run(spec: dict[str, Any]):
            name = str(spec["name"])
            modes = list(spec["modes"])
            transfers = int(spec["transfers"])
            query_departure = departure + timedelta(minutes=int(spec["offsetMin"]))
            nodes, local_warnings = self.otp.plan(
                origin=origin_loc,
                destination=destination_loc,
                departure=query_departure,
                profile=profile,
                transit_modes=modes,
                max_transfers=transfers,
                transit_only=True,
                access_mode="WALK",
                egress_mode="WALK",
                transfer_mode="WALK",
                first=focus.otp_candidates_per_query,
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

        skeletons.extend(deepcopy(seed_skeletons or []))

        # Exact dedupe first, then retain different transport chains before the
        # expensive bicycle comparisons. Similarity is intentionally not used yet:
        # two skeletons on the same corridor can optimize into different strategies.
        skeletons = self._dedupe_exact(skeletons)
        preselected: list[dict[str, Any]] = []
        hypothesis_seen: set[tuple] = set()
        chain_counts: dict[tuple, int] = {}
        for route in sorted(
            skeletons,
            key=lambda r: (
                self._candidate_generation_priority(r, route_focus),
                r["doorToDoor"],
            ),
        ):
            chain = self._transit_chain_signature(route)
            transit_legs = [leg for leg in route.get("legs") or [] if leg.get("transitLeg")]
            hypothesis = (
                chain,
                (transit_legs[0].get("from") or {}).get("name") if transit_legs else None,
                (transit_legs[-1].get("to") or {}).get("name") if transit_legs else None,
            )
            if hypothesis in hypothesis_seen or chain_counts.get(chain, 0) >= 8:
                continue
            hypothesis_seen.add(hypothesis)
            chain_counts[chain] = chain_counts.get(chain, 0) + 1
            preselected.append(route)
            if len(preselected) >= focus.transit_skeleton_limit:
                break

        comparisons = 0
        counter_lock = threading.Lock()
        optimized: list[dict[str, Any]] = []
        if diagnostics:
            diagnostics.generated_candidates("transitSkeletons", len(preselected), preselected)

        def optimize(route: dict[str, Any]):
            local_routes: list[dict[str, Any]] = []
            count = 0
            for optimizer_focus in focus.optimizer_focus_variants:
                out, local_count = self._optimize_transit_skeleton(
                    route,
                    requested_departure=departure,
                    profile=profile,
                    route_focus=optimizer_focus,
                )
                count += local_count
                if out is not None:
                    out["optimizationFocus"] = optimizer_focus
                    out.setdefault("optimization", {})["focusVariant"] = optimizer_focus
                    local_routes.append(out)
            nonlocal comparisons
            with counter_lock:
                comparisons += count
            if not local_routes and diagnostics:
                diagnostics.reject("segmentOptimization", route)
            return local_routes

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(optimize, route) for route in preselected]
            for future in concurrent.futures.as_completed(futures):
                try:
                    optimized.extend(future.result())
                except Exception as exc:
                    warnings.append({"code": "TRANSIT_OPTIMIZER_FAILED", "description": str(exc)})

        # Transit-first itineraries are useful results in their own right.  In
        # the previous pipeline they were only optimizer input, which meant a
        # valid all-/mostly-transit option could disappear after bicycle segment
        # replacement. Keep one original candidate per real transit hypothesis.
        public_transport: list[dict[str, Any]] = []
        for skeleton in preselected:
            candidate = deepcopy(skeleton)
            candidate["strategy"] = "public_transport"
            candidate.setdefault("candidateFamilies", []).append("public_transport")
            candidate["optimization"] = {
                "focusVariant": None,
                "replacedWalkCount": 0,
                "replacedTransitCount": 0,
                "preservedTransitSkeleton": True,
            }
            public_transport.append(candidate)

        return (
            self._dedupe_exact(public_transport + optimized),
            self._dedupe_warnings(warnings),
            {
                "queries": len(specs),
                "skeletons": len(preselected),
                "bikeComparisons": comparisons,
                "optimizerRejected": max(0, len(preselected) - len(optimized)),
                "optimizedCandidates": len(self._dedupe_exact(optimized)),
                "publicTransportCandidates": len(self._dedupe_exact(public_transport)),
            },
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
        transit_decisions: list[dict[str, Any]] = []
        # The first boarding wait is part of door-to-door utility.  Initialising
        # this cursor from itinerary.start used to make that wait disappear.
        previous_original_end = requested_departure

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
                    profile=profile,
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
                    next_context = self._next_transit_context(
                        original,
                        index=index,
                        previous_arrival=previous_original_end,
                        replacement_duration=int(bike.get("duration") or 0),
                    )
                    decision = self._transit_leg_replacement_decision(
                        leg=leg,
                        bike=bike,
                        wait_before=wait_before,
                        line=line,
                        downstream_trunk_score=downstream_trunk,
                        route_focus=route_focus,
                        **next_context,
                    )
                    replace = bool(decision.get("replace"))
                    transit_decisions.append(
                        {
                            "mode": mode,
                            "route": (leg.get("route") or {}).get("shortName")
                            or (leg.get("route") or {}).get("longName"),
                            **decision,
                        }
                    )

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
                    if decision:
                        kept["transitUtility"] = {
                            key: value for key, value in decision.items() if key != "replace"
                        }
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
        rebuilt_door_to_door = max(
            0,
            int((route_end - requested_departure).total_seconds()),
        )
        actual_saved = max(
            0,
            int(skeleton.get("doorToDoor") or rebuilt_door_to_door)
            - rebuilt_door_to_door,
        )

        route = {
            "id": "",
            "kind": "mixed",
            "strategy": (
                skeleton.get("strategy")
                if skeleton.get("strategy")
                in {"boarding_anchor", "trunk_access", "egress_anchor"}
                else "transit_optimized"
            ),
            "sourceQuery": skeleton.get("sourceQuery", "transit_skeleton"),
            "duration": max(0, int((route_end - route_start).total_seconds())),
            "initialWait": max(0, int((route_start - requested_departure).total_seconds())),
            "doorToDoor": rebuilt_door_to_door,
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
                "savedSecondsEstimate": actual_saved,
                "localUtilitySavingsSeconds": sum(
                    x.get("savedSeconds", 0) for x in replaced_transit
                ),
                "transitLegDecisions": transit_decisions,
            },
        }
        if skeleton.get("anchor"):
            route["anchor"] = deepcopy(skeleton["anchor"])
        if skeleton.get("candidateFamilies"):
            route["candidateFamilies"] = list(skeleton["candidateFamilies"])
        if skeleton.get("streetPreference"):
            route["streetPreference"] = skeleton["streetPreference"]
        return self._score_route(route, profile), comparisons

    def _optimize_candidate_set(
        self,
        routes: list[dict[str, Any]],
        *,
        requested_departure: datetime,
        profile: str,
        route_focus: int,
        diagnostics: RoutingDiagnostics | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if not routes:
            return [], 0
        result: list[dict[str, Any]] = []
        comparisons = 0
        lock = threading.Lock()

        def optimize(candidate: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal comparisons
            optimized, local_comparisons = self._optimize_transit_skeleton(
                candidate,
                requested_departure=requested_departure,
                profile=profile,
                route_focus=route_focus,
            )
            with lock:
                comparisons += local_comparisons
            if optimized is None and diagnostics:
                diagnostics.reject("segmentOptimization", candidate)
            return optimized

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.generic_workers) as pool:
            futures = [pool.submit(optimize, route) for route in routes]
            for future in concurrent.futures.as_completed(futures):
                try:
                    optimized = future.result()
                    if optimized is not None:
                        result.append(optimized)
                except Exception as exc:
                    if diagnostics:
                        diagnostics.reject(
                            "segmentOptimizationError",
                            details=str(exc),
                        )
        return self._dedupe_exact(result), comparisons

    def _should_compare_transit_leg(
        self,
        *,
        leg: dict[str, Any],
        wait_before: int,
        line: LineMetrics | None,
        route_focus: int,
        profile: str = "balanced",
    ) -> bool:
        distance = float(leg.get("distance") or 0)
        duration = max(1, int(leg.get("duration") or 0))
        mode = str(leg.get("mode") or "")
        speed = distance / duration * 3.6
        trunk = line.trunk_score if line else 0.50
        headway = line.median_headway_s if line else None
        focus = ROUTE_FOCUS_CONFIG[route_focus]
        bike_speed = float(PROFILE_CONFIG[profile]["speed_mps"])
        estimated_bike = distance / max(0.5, bike_speed) * 1.18
        board_alight = 75 if mode in {"BUS", "TROLLEYBUS"} else 95
        estimated_transit = wait_before + duration + board_alight
        relative_advantage = estimated_bike - estimated_transit

        # This gate controls only whether an exact OTP bicycle comparison is
        # worth paying for.  The eventual decision is made by full relative
        # utility, never by a fixed transit-distance cutoff.
        uncertain_or_weak = (
            relative_advantage < 4 * 60
            or speed < float(PROFILE_CONFIG[profile]["speed_kmh"]) * 1.35
            or wait_before > 90
            or (headway is not None and headway > 8 * 60)
            or trunk < 0.62
        )
        if mode in {"BUS", "TROLLEYBUS"}:
            return uncertain_or_weak or route_focus > 0
        if mode == "TRAM":
            return uncertain_or_weak and (trunk < 0.74 or route_focus > 0)
        if mode in {"RAIL", "SUBWAY"}:
            return trunk < 0.62 or (
                route_focus > 0
                and relative_advantage < focus.min_transit_utility_seconds + 3 * 60
            )
        return uncertain_or_weak

    def _transit_leg_replacement_decision(
        self,
        *,
        leg: dict[str, Any],
        bike: dict[str, Any],
        wait_before: int,
        line: LineMetrics | None,
        downstream_trunk_score: float,
        route_focus: int,
        next_transit_start: datetime | None = None,
        replacement_arrival: datetime | None = None,
        downstream_mode: str | None = None,
        downstream_headway_s: float | None = None,
    ) -> dict[str, Any]:
        duration = max(1, int(leg.get("duration") or 0))
        bike_duration = max(1, int(bike.get("duration") or 0))
        mode = str(leg.get("mode") or "")
        trunk = line.trunk_score if line else 0.50
        headway = line.median_headway_s if line else None
        focus = ROUTE_FOCUS_CONFIG[route_focus]

        utility = self._transit_leg_utility(
            mode=mode,
            ride_seconds=duration,
            wait_seconds=wait_before,
            bike_seconds=bike_duration,
            trunk_score=trunk,
            headway_s=headway,
            downstream_trunk_score=downstream_trunk_score,
            downstream_mode=downstream_mode,
            downstream_headway_s=downstream_headway_s,
            focus=focus,
        )
        catch_margin: int | None = None
        misses_connection = False
        if next_transit_start is not None and replacement_arrival is not None:
            catch_margin = int((next_transit_start - replacement_arrival).total_seconds())
            transfer_buffer = 45 if downstream_mode in {"RAIL", "SUBWAY"} else 30
            misses_connection = catch_margin < transfer_buffer

        replace = (
            not misses_connection
            and bike_duration <= focus.max_replacement_bike_seconds
            and float(utility["utilitySeconds"]) < focus.min_transit_utility_seconds
        )
        reason = "scheduled_connection_protected" if misses_connection else (
            "weak_relative_utility" if replace else "useful_transit_leg"
        )
        return {
            "replace": replace,
            "reason": reason,
            "savedSeconds": max(
                0,
                int(utility["effectiveTransitSeconds"] - utility["bikeEquivalentSeconds"]),
            ),
            **utility,
            "requiredUtilitySeconds": round(focus.min_transit_utility_seconds),
            "catchMarginSeconds": catch_margin,
            "missesDownstreamDeparture": misses_connection,
            "trunkScore": round(trunk, 3),
        }

    @staticmethod
    def _transit_leg_utility(
        *,
        mode: str,
        ride_seconds: int,
        wait_seconds: int,
        bike_seconds: int,
        trunk_score: float,
        headway_s: float | None,
        downstream_trunk_score: float,
        downstream_mode: str | None,
        downstream_headway_s: float | None,
        focus: RouteFocusConfig,
    ) -> dict[str, Any]:
        """Return the explainable relative value of using one transit leg.

        Positive utility means transit has a real function; negative utility
        means cycling between the same points is preferable.  Waiting, boarding,
        detour, line quality, long-bike avoidance and downstream connection value
        are represented once each to avoid double charging.
        """

        board_alight = {
            "BUS": 85,
            "TROLLEYBUS": 85,
            "TRAM": 95,
            "RAIL": 120,
            "SUBWAY": 115,
        }.get(mode, 90)
        effective_transit = (wait_seconds + ride_seconds + board_alight) * focus.transit_cost_factor
        bike_equivalent = bike_seconds * focus.bike_cost_factor
        direct_advantage = bike_equivalent - effective_transit

        mode_strength = {
            "BUS": 0.05,
            "TROLLEYBUS": 0.06,
            "TRAM": 0.22,
            "RAIL": 0.48,
            "SUBWAY": 0.46,
        }.get(mode, 0.0)
        quality_value = ride_seconds * (
            mode_strength + max(0.0, trunk_score - 0.45) * 1.15
        )
        quality_value += max(0.0, trunk_score - 0.65) * 300.0
        quality_value *= focus.trunk_access_bonus_factor

        # A long or topologically difficult bike alternative is useful evidence
        # that transit is crossing a barrier or serving a genuine corridor.
        long_bike_value = max(0.0, bike_seconds - 9 * 60) * 0.32
        barrier_value = max(0.0, bike_seconds - ride_seconds * 1.45) * 0.38

        downstream_value = 0.0
        if downstream_mode:
            downstream_value = (
                170.0
                * downstream_trunk_score**2
                * focus.feeder_protection
            )
            if downstream_mode in {"RAIL", "SUBWAY"}:
                downstream_value += 70.0 * focus.feeder_protection
            # Sparse downstream departures make a catchable feeder more valuable.
            if downstream_headway_s:
                downstream_value += min(100.0, downstream_headway_s * 0.08)

        reliability_cost = 0.0
        if headway_s:
            reliability_cost = min(90.0, max(0.0, headway_s - 8 * 60) * 0.10)

        value = (
            direct_advantage
            + quality_value
            + long_bike_value
            + barrier_value
            + downstream_value
            - reliability_cost
        )
        return {
            "utilitySeconds": round(value),
            "effectiveTransitSeconds": round(effective_transit),
            "bikeEquivalentSeconds": round(bike_equivalent),
            "doorTimeAdvantageSeconds": round(direct_advantage),
            "qualityValueSeconds": round(quality_value),
            "longBikeValueSeconds": round(long_bike_value + barrier_value),
            "downstreamValueSeconds": round(downstream_value),
            "reliabilityCostSeconds": round(reliability_cost),
        }

    def _next_transit_context(
        self,
        legs: list[dict[str, Any]],
        *,
        index: int,
        previous_arrival: datetime,
        replacement_duration: int,
    ) -> dict[str, Any]:
        between = 0
        for next_leg in legs[index + 1 :]:
            if next_leg.get("transitLeg"):
                metrics = self.gtfs.line_metrics_for_trip(next_leg.get("tripId"))
                return {
                    "next_transit_start": self._parse_leg_timestamp(next_leg.get("startTime")),
                    "replacement_arrival": previous_arrival
                    + timedelta(seconds=replacement_duration + between),
                    "downstream_mode": str(next_leg.get("mode") or ""),
                    "downstream_headway_s": metrics.median_headway_s if metrics else None,
                }
            between += int(next_leg.get("duration") or 0)
        return {
            "next_transit_start": None,
            "replacement_arrival": None,
            "downstream_mode": None,
            "downstream_headway_s": None,
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

    @classmethod
    def _route_boundary(
        cls,
        legs: list[dict[str, Any]],
        boundary: int,
    ) -> dict[str, Any] | None:
        """Return a reliable coordinate and place object between two legs."""

        candidates: list[tuple[dict[str, Any], list[float] | None]] = []
        if boundary > 0:
            previous = legs[boundary - 1]
            geometry = ((previous.get("geometry") or {}).get("coordinates") or [])
            candidates.append(
                (
                    previous.get("to") or {},
                    geometry[-1] if geometry else None,
                )
            )
        if boundary < len(legs):
            following = legs[boundary]
            geometry = ((following.get("geometry") or {}).get("coordinates") or [])
            candidates.append(
                (
                    following.get("from") or {},
                    geometry[0] if geometry else None,
                )
            )

        for place, coordinate in candidates:
            try:
                lat = float(place["lat"])
                lon = float(place["lon"])
            except (KeyError, TypeError, ValueError):
                if not coordinate or len(coordinate) < 2:
                    continue
                try:
                    lon = float(coordinate[0])
                    lat = float(coordinate[1])
                except (TypeError, ValueError):
                    continue
            return {
                "lat": lat,
                "lon": lon,
                "place": {
                    "name": place.get("name")
                    or ("Старт" if boundary == 0 else "Финиш" if boundary == len(legs) else "Точка смены"),
                    "lat": lat,
                    "lon": lon,
                },
            }
        return None

    @staticmethod
    def _schedule_street_legs(
        legs: list[dict[str, Any]],
        *,
        route_start: datetime,
    ) -> tuple[datetime, list[dict[str, Any]]]:
        current = route_start
        scheduled: list[dict[str, Any]] = []
        for raw in legs:
            leg = deepcopy(raw)
            leg.pop("_fixedTransitStart", None)
            leg.pop("_fixedTransitEnd", None)
            leg["startTime"] = current.isoformat()
            current += timedelta(seconds=int(leg.get("duration") or 0))
            leg["endTime"] = current.isoformat()
            scheduled.append(leg)
        return current, RoutePlanner._merge_adjacent_bicycle_legs(scheduled)

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
        route_focus: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        boarding: list[dict[str, Any]] = []
        egress: list[dict[str, Any]] = []

        jobs: list[tuple[str, Anchor]] = [*(('boarding', a) for a in boarding_anchors), *(('egress', a) for a in egress_anchors)]

        def run(job: tuple[str, Anchor]):
            role, anchor = job
            if role == "boarding":
                return role, self._boarding_anchor_routes(
                    anchor,
                    origin,
                    destination,
                    departure,
                    profile,
                    max_transfers,
                    route_focus,
                )
            return role, self._egress_anchor_routes(
                anchor,
                origin,
                destination,
                departure,
                profile,
                max_transfers,
                route_focus,
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
        route_focus: int,
    ) -> list[dict[str, Any]]:
        focus = ROUTE_FOCUS_CONFIG[route_focus]
        bike = self._bike_between_points(origin, (anchor.lat, anchor.lon), departure, profile)
        bike_distance = float((bike or {}).get("bikeDistance") or 0)
        if bike is None or bike_distance < 180 or bike_distance > focus.max_bike_access_m * 1.18:
            return []

        bike_duration = int(bike.get("duration") or 0)
        transit_departure = departure + timedelta(seconds=bike_duration + 35)
        gtfs_id = f"{self.feed_id}:{anchor.stop_id}"

        result: list[dict[str, Any]] = []
        query_specs = self._anchor_mode_queries(anchor)
        transfer_cap = max(0, max_transfers - focus.transfer_reduction)
        for query_name, modes in query_specs:
            nodes, _ = self.otp.plan(
                origin=stop_location(gtfs_id, anchor.name),
                destination=coordinate_location(*destination, "Финиш"),
                departure=transit_departure,
                profile=profile,
                transit_modes=modes,
                max_transfers=transfer_cap,
                transit_only=True,
                access_mode="WALK",
                egress_mode="BICYCLE",
                transfer_mode="BICYCLE",
                first=max(6, focus.otp_candidates_per_query // 2),
            )
            for node in nodes:
                second = self._normalize_route(
                    node,
                    requested_departure=transit_departure,
                    profile_key=profile,
                    strategy="boarding_anchor_tail",
                    source_query=f"boarding_anchor_{query_name}",
                )
                if second.get("kind") != "mixed":
                    continue
                actual_trunk = float(second.get("bestTrunkScore") or 0)
                strategy = (
                    "trunk_access"
                    if query_name == "trunk"
                    and anchor.best_trunk_score >= 0.58
                    and actual_trunk >= 0.50
                    else "boarding_anchor"
                )
                result.append(
                    self._combine_boarding_route(
                        bike,
                        second,
                        anchor,
                        departure,
                        profile,
                        strategy=strategy,
                    )
                )
        result = self._dedupe_exact(result)
        result.sort(key=lambda r: (self._candidate_generation_priority(r, route_focus), r["doorToDoor"]))
        return result[:4]

    def _egress_anchor_routes(
        self,
        anchor: Anchor,
        origin: tuple[float, float],
        destination: tuple[float, float],
        departure: datetime,
        profile: str,
        max_transfers: int,
        route_focus: int,
    ) -> list[dict[str, Any]]:
        focus = ROUTE_FOCUS_CONFIG[route_focus]
        gtfs_id = f"{self.feed_id}:{anchor.stop_id}"
        bike = self._bike_between_points(
            (anchor.lat, anchor.lon), destination, departure, profile
        )
        bike_distance = float((bike or {}).get("bikeDistance") or 0)
        if bike is None or bike_distance > focus.max_bike_egress_m * 1.18:
            return []

        result: list[dict[str, Any]] = []
        transfer_cap = max(0, max_transfers - focus.transfer_reduction)
        for query_name, modes in self._anchor_mode_queries(anchor):
            nodes, _ = self.otp.plan(
                origin=coordinate_location(*origin, "Старт"),
                destination=stop_location(gtfs_id, anchor.name),
                departure=departure,
                profile=profile,
                transit_modes=modes,
                max_transfers=transfer_cap,
                transit_only=True,
                access_mode="BICYCLE",
                egress_mode="WALK",
                transfer_mode="BICYCLE",
                first=max(6, focus.otp_candidates_per_query // 2),
            )
            for node in nodes:
                first = self._normalize_route(
                    node,
                    requested_departure=departure,
                    profile_key=profile,
                    strategy="egress_anchor_head",
                    source_query=f"egress_anchor_{query_name}",
                )
                if first.get("kind") != "mixed":
                    continue
                result.append(self._combine_egress_route(first, bike, anchor, profile))
        result = self._dedupe_exact(result)
        result.sort(key=lambda r: (self._candidate_generation_priority(r, route_focus), r["doorToDoor"]))
        return result[:4]

    @staticmethod
    def _anchor_mode_queries(anchor: Anchor) -> list[tuple[str, list[str]]]:
        modes = set(anchor.modes)
        if anchor.best_trunk_score < 0.58 and not modes.intersection(
            {"RAIL", "SUBWAY", "TRAM"}
        ):
            return [("all", ALL_TRANSIT)]
        if "RAIL" in modes:
            trunk_modes = ["RAIL"]
        elif "SUBWAY" in modes:
            trunk_modes = ["SUBWAY"]
        elif "TRAM" in modes:
            trunk_modes = ["TRAM"]
        else:
            trunk_modes = [m for m in ("BUS", "TROLLEYBUS") if m in modes] or [
                "BUS",
                "TROLLEYBUS",
            ]
        result = [("trunk", trunk_modes)]
        if set(trunk_modes) != set(ALL_TRANSIT):
            result.append(("all", ALL_TRANSIT))
        return result

    def _combine_boarding_route(
        self,
        bike: dict[str, Any],
        tail: dict[str, Any],
        anchor: Anchor,
        departure: datetime,
        profile: str,
        strategy: str = "boarding_anchor",
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
            "strategy": strategy,
            "sourceQuery": tail.get("sourceQuery") or "boarding_anchor",
            "duration": max(0, int((tail_end - departure).total_seconds())),
            "initialWait": 0,
            "doorToDoor": max(0, int((tail_end - departure).total_seconds())),
            "start": departure.isoformat(),
            "end": tail_end.isoformat(),
            "generalizedCost": None,
            "waitingTime": max(int(tail.get("waitingTime") or 0), wait_gap),
            "legs": bike_legs + deepcopy(tail.get("legs") or []),
            "anchor": self._anchor_json(anchor, "boarding", bike_distance=float(bike.get("bikeDistance") or 0)),
            "streetPreference": bike.get("streetPreference") or "cycleway",
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
            "streetPreference": bike.get("streetPreference") or "cycleway",
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
                    street_preference={
                        "fast": "direct",
                        "balanced": "cycleway",
                        "calm": "quiet",
                    }.get(profile, "cycleway"),
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
        street_preference: str | None = None,
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
            "streetPreference": street_preference,
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
            "routeName": metrics.route_name,
            "busServiceClass": metrics.bus_service_class,
            "busPriorityScore": round(metrics.bus_priority_score, 3),
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

    @staticmethod
    def _is_user_transition(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if left.get("transitLeg") and right.get("transitLeg"):
            if right.get("interlineWithPreviousLeg"):
                return False
            left_trip = left.get("tripId")
            right_trip = right.get("tripId")
            if left_trip and right_trip and left_trip == right_trip:
                return False
        if (
            not left.get("transitLeg")
            and not right.get("transitLeg")
            and left.get("mode") == right.get("mode")
        ):
            return False
        return True

    @classmethod
    def _route_transition_points(cls, legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        for left, right in zip(legs, legs[1:]):
            if not cls._is_user_transition(left, right):
                continue
            left_to = left.get("to") or {}
            right_from = right.get("from") or {}
            lat = left_to.get("lat")
            lon = left_to.get("lon")
            if lat is None:
                lat = right_from.get("lat")
            if lon is None:
                lon = right_from.get("lon")
            if lat is None or lon is None:
                left_geometry = ((left.get("geometry") or {}).get("coordinates") or [])
                right_geometry = ((right.get("geometry") or {}).get("coordinates") or [])
                coordinate = left_geometry[-1] if left_geometry else (
                    right_geometry[0] if right_geometry else None
                )
                if coordinate:
                    lon, lat = coordinate
            left_route = left.get("route") or {}
            right_route = right.get("route") or {}
            points.append(
                {
                    "index": len(points) + 1,
                    # Keep transitions without coordinates in the count. The
                    # frontend simply skips their map marker, while the route
                    # card still reflects every visible change of segment.
                    "lat": float(lat) if lat is not None else None,
                    "lon": float(lon) if lon is not None else None,
                    "name": left_to.get("name") or right_from.get("name") or "Пересадка",
                    "fromMode": left.get("mode"),
                    "toMode": right.get("mode"),
                    "fromRoute": left_route.get("shortName") or left_route.get("longName"),
                    "toRoute": right_route.get("shortName") or right_route.get("longName"),
                }
            )
        return points

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
        bike_duration = sum(
            int(leg.get("duration") or 0)
            for leg in legs
            if leg.get("mode") == "BICYCLE" and not leg.get("transitLeg")
        )
        walk_duration = sum(
            int(leg.get("duration") or 0)
            for leg in legs
            if leg.get("mode") == "WALK" and not leg.get("transitLeg")
        )
        transit_duration = sum(
            int(leg.get("duration") or 0) for leg in legs if leg.get("transitLeg")
        )
        transit_legs = [leg for leg in legs if leg.get("transitLeg")]
        transit_transfers = self._actual_transfer_count(route)
        boardings = transit_transfers + (1 if transit_legs else 0)
        transfer_points = self._route_transition_points(legs)
        transfers = len(transfer_points)

        movement = bike_distance + transit_distance
        route["bikeDistance"] = round(bike_distance, 1)
        route["walkDistance"] = round(walk_distance, 1)
        route["transitDistance"] = round(transit_distance, 1)
        route["bikeDuration"] = bike_duration
        route["walkDuration"] = walk_duration
        route["transitDuration"] = transit_duration
        route["transfers"] = transfers
        route["routeTransitions"] = transfers
        route["transitTransfers"] = transit_transfers
        route["transferPoints"] = transfer_points
        route["bikeBoardings"] = boardings
        route["transitBoardings"] = boardings
        route["bikeShare"] = round(bike_distance / movement, 4) if movement else 0.0
        route["transitShare"] = round(transit_distance / movement, 4) if movement else 0.0

        trunk_weight = 0.0
        trunk_duration = 0.0
        best_trunk = 0.0
        best_trunk_name: str | None = None
        rapid_bus_weight = 0.0
        rapid_bus_duration = 0.0
        best_rapid_bus_route: str | None = None
        best_rapid_bus_class: str | None = None
        best_rapid_bus_priority = 0.0
        for leg in transit_legs:
            lm = leg.get("lineMetrics") or {}
            trunk = float(lm.get("trunkScore") or 0.50)
            duration = float(leg.get("duration") or 0)
            trunk_weight += trunk * duration
            trunk_duration += duration
            route_name = (leg.get("route") or {}).get("shortName") or (leg.get("route") or {}).get("longName")
            if trunk > best_trunk:
                best_trunk = trunk
                best_trunk_name = route_name
            if leg.get("mode") in {"BUS", "TROLLEYBUS"}:
                priority = float(lm.get("busPriorityScore") or 0)
                rapid_bus_weight += priority * duration
                rapid_bus_duration += duration
                if priority > best_rapid_bus_priority:
                    best_rapid_bus_priority = priority
                    best_rapid_bus_route = route_name
                    best_rapid_bus_class = lm.get("busServiceClass")
        route["avgTrunkScore"] = round(trunk_weight / trunk_duration, 3) if trunk_duration else 0.0
        route["bestTrunkScore"] = round(best_trunk, 3)
        route["bestTrunkRoute"] = best_trunk_name
        route["avgRapidBusPriority"] = (
            round(rapid_bus_weight / rapid_bus_duration, 3)
            if rapid_bus_duration
            else 0.0
        )
        route["bestRapidBusPriority"] = round(best_rapid_bus_priority, 3)
        route["bestRapidBusRoute"] = best_rapid_bus_route
        route["bestRapidBusClass"] = best_rapid_bus_class
        return route

    def _score_route(self, route: dict[str, Any], profile_key: str) -> dict[str, Any]:
        route = self._refresh_route_metrics(route)
        cfg = PROFILE_CONFIG[profile_key]

        wait_cost = float(route.get("waitingTime") or 0) * float(cfg["wait_factor"])
        transfer_cost = int(route.get("transitTransfers") or 0) * float(cfg["transfer_penalty"])
        boarding_cost = int(route.get("bikeBoardings") or 0) * float(cfg["bike_boarding_penalty"])
        walk_cost = (float(route.get("walkDistance") or 0) / 1000.0) * 180.0
        micro_penalty, leg_utilities = self._estimated_micro_transit_penalty(route, profile_key)
        complexity_cost = int(route.get("routeTransitions") or 0) * 45.0

        avg_trunk = float(route.get("avgTrunkScore") or 0)
        transit_seconds = sum(
            int(leg.get("duration") or 0)
            for leg in route.get("legs") or []
            if leg.get("transitLeg")
        )
        trunk_bonus = min(260.0, max(0.0, avg_trunk - 0.55) * transit_seconds * 0.45)
        rapid_bus_seconds = sum(
            int(leg.get("duration") or 0)
            for leg in route.get("legs") or []
            if leg.get("transitLeg") and leg.get("mode") in {"BUS", "TROLLEYBUS"}
        )
        rapid_bus_priority = float(route.get("avgRapidBusPriority") or 0)
        rapid_bus_bonus = min(360.0, rapid_bus_priority * rapid_bus_seconds * 0.22)

        discomfort = max(
            0.0,
            wait_cost
            + transfer_cost
            + boarding_cost
            + walk_cost
            + micro_penalty
            + complexity_cost
            - trunk_bonus
            - rapid_bus_bonus,
        )
        score = float(route.get("doorToDoor") or 0) + discomfort

        route["discomfort"] = round(discomfort)
        route["microTransitPenalty"] = round(micro_penalty)
        route["complexityCost"] = round(complexity_cost)
        route["transitLegUtilities"] = leg_utilities
        route["trunkBonus"] = round(trunk_bonus)
        route["rapidBusBonus"] = round(rapid_bus_bonus)
        route["baseScore"] = round(score)
        route["score"] = round(score)
        route["transitModes"] = transit_modes(route)
        route["transitRoutes"] = transit_route_names(route)
        return route

    def _estimated_micro_transit_penalty(
        self,
        route: dict[str, Any],
        profile_key: str,
    ) -> tuple[float, list[dict[str, Any]]]:
        transit_legs = [leg for leg in route.get("legs") or [] if leg.get("transitLeg")]
        if not transit_legs:
            return 0.0, []
        profile = PROFILE_CONFIG[profile_key]
        allocated_wait = float(route.get("waitingTime") or 0) / len(transit_legs)
        details: list[dict[str, Any]] = []
        penalty = 0.0
        for leg in transit_legs:
            existing = leg.get("transitUtility") or {}
            if existing.get("utilitySeconds") is not None:
                utility = float(existing["utilitySeconds"])
                source = "otp_bike_comparison"
            else:
                distance = float(leg.get("distance") or 0)
                estimated_bike = max(
                    1,
                    round(distance / max(0.5, float(profile["speed_mps"])) * 1.18),
                )
                lm = leg.get("lineMetrics") or {}
                estimate = self._transit_leg_utility(
                    mode=str(leg.get("mode") or ""),
                    ride_seconds=max(1, int(leg.get("duration") or 0)),
                    wait_seconds=round(allocated_wait),
                    bike_seconds=estimated_bike,
                    trunk_score=float(lm.get("trunkScore") or 0.50),
                    headway_s=lm.get("medianHeadway"),
                    downstream_trunk_score=0.0,
                    downstream_mode=None,
                    downstream_headway_s=None,
                    focus=ROUTE_FOCUS_CONFIG[0],
                )
                utility = float(estimate["utilitySeconds"])
                source = "estimated"

            # This is a soft route-level guard for candidates which did not pass
            # through the exact segment optimiser.  The penalty is relative to
            # utility and capped; no distance threshold decides the outcome.
            leg_penalty = min(420.0, max(0.0, 20.0 - utility) * 0.65)
            penalty += leg_penalty
            details.append(
                {
                    "mode": leg.get("mode"),
                    "route": (leg.get("route") or {}).get("shortName")
                    or (leg.get("route") or {}).get("longName"),
                    "utilitySeconds": round(utility),
                    "penaltySeconds": round(leg_penalty),
                    "source": source,
                }
            )
        return penalty, details

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
        target += focus_cfg.target_bike_share_shift
        target = max(0.05, min(0.95, target))

        best_time = max(1, min(int(r.get("doorToDoor") or 0) for r in routes))
        allowed_ratio = 1.0 + focus_cfg.time_tolerance_ratio
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

            share_penalty = share_gap * focus_cfg.share_penalty_seconds
            transfer_adjustment = (
                int(route.get("transitTransfers") or 0)
                * float(profile_cfg["transfer_penalty"])
                * (focus_cfg.transfer_penalty_factor - 1.0)
            )
            modality_adjustment = (
                float(route.get("bikeDuration") or 0) * (focus_cfg.bike_cost_factor - 1.0)
                + float(route.get("transitDuration") or 0)
                * (focus_cfg.transit_cost_factor - 1.0)
            )
            short_transit_adjustment = float(route.get("microTransitPenalty") or 0) * (
                focus_cfg.short_transit_penalty_factor - 1.0
            )
            trunk_adjustment = -float(route.get("trunkBonus") or 0) * (
                focus_cfg.trunk_access_bonus_factor - 1.0
            )
            time_ratio = float(route.get("doorToDoor") or best_time) / best_time
            detour_penalty = 0.0
            if time_ratio > allowed_ratio:
                detour_penalty = (time_ratio - allowed_ratio) * best_time * 3.0 + 240.0

            route["score"] = round(
                float(route.get("baseScore") or 0)
                + share_penalty
                + transfer_adjustment
                + modality_adjustment
                + short_transit_adjustment
                + trunk_adjustment
                + detour_penalty
            )
            route["preference"] = {
                "focus": route_focus,
                "focusName": focus_cfg.name,
                "targetBikeShare": round(target, 3),
                "actualBikeShare": round(bike_share, 3),
                "referenceDistance": round(reference_distance, 1),
                "timeRatio": round(time_ratio, 3),
                "allowedTimeRatio": round(allowed_ratio, 3),
                "bikeCostFactor": focus_cfg.bike_cost_factor,
                "transitCostFactor": focus_cfg.transit_cost_factor,
                "shortTransitPenaltyFactor": focus_cfg.short_transit_penalty_factor,
                "transferPenaltyFactor": focus_cfg.transfer_penalty_factor,
            }
            result.append(route)
        return result

    def _candidate_generation_priority(self, route: dict[str, Any], route_focus: int) -> float:
        """Focus-aware priority used before expensive candidate optimisation."""
        focus = ROUTE_FOCUS_CONFIG[min(2, max(-2, int(route_focus)))]
        route = self._refresh_route_metrics(route)
        return (
            float(route.get("baseScore") or route.get("score") or route.get("doorToDoor") or 0)
            + float(route.get("bikeDuration") or 0) * (focus.bike_cost_factor - 1.0)
            + float(route.get("transitDuration") or 0) * (focus.transit_cost_factor - 1.0)
            + int(route.get("transitTransfers") or 0)
            * 180.0
            * (focus.transfer_penalty_factor - 1.0)
            - float(route.get("trunkBonus") or 0)
            * (focus.trunk_access_bonus_factor - 1.0)
        )

    def _classify_strategies(
        self,
        routes: list[dict[str, Any]],
        *,
        route_focus: int,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []
        best_time = min(float(r.get("doorToDoor") or math.inf) for r in routes)
        best_score = min(float(r.get("score") or math.inf) for r in routes)
        mixed = [r for r in routes if r.get("kind") == "mixed"]
        best_bike_heavy = (
            max(
                mixed,
                key=lambda r: (
                    float(r.get("bikeShare") or 0),
                    -float(r.get("score") or math.inf),
                ),
            )
            if mixed
            else None
        )
        best_transit_heavy = (
            max(
                mixed,
                key=lambda r: (
                    float(r.get("transitShare") or 0),
                    -float(r.get("score") or math.inf),
                ),
            )
            if mixed
            else None
        )
        min_mixed_transfers = min(
            (int(r.get("transfers") or 0) for r in mixed),
            default=0,
        )
        classified: list[dict[str, Any]] = []
        for raw in routes:
            route = raw
            archetypes: list[str] = []
            kind = route.get("kind")
            bike_share = float(route.get("bikeShare") or 0)
            transit_share = float(route.get("transitShare") or 0)
            strategy = route.get("strategy")
            anchor = route.get("anchor") or {}

            if kind == "bike":
                archetypes.append("DIRECT_BIKE")
            if float(route.get("bikeDistance") or 0) >= 300:
                bicycle_archetype = {
                    "direct": "BIKE_DIRECT",
                    "cycleway": "BIKE_CYCLEWAY",
                    "quiet": "BIKE_QUIET",
                }.get(str(route.get("streetPreference") or ""))
                if bicycle_archetype:
                    archetypes.append(bicycle_archetype)
            if float(route.get("doorToDoor") or math.inf) <= best_time + 30:
                archetypes.append("FASTEST")
            if float(route.get("score") or math.inf) <= best_score + 45:
                archetypes.append("BALANCED")
            if kind == "mixed" and (
                transit_share >= 0.52 or route is best_transit_heavy
            ):
                archetypes.append("TRANSIT_HEAVY")
            if kind == "mixed" and (
                transit_share >= 0.80
                or (
                    strategy == "public_transport"
                    and float(route.get("bikeDistance") or 0) <= 300
                )
            ):
                archetypes.append("PUBLIC_TRANSPORT")
            if (
                kind == "mixed"
                and float(route.get("bestRapidBusPriority") or 0) >= 0.72
            ):
                archetypes.append("RAPID_BUS")
            if kind == "mixed" and (
                bike_share >= 0.52 or route is best_bike_heavy
            ):
                archetypes.append("BIKE_HEAVY")
            if (
                strategy == "trunk_access"
                or (
                    anchor.get("type") == "boarding"
                    and float(route.get("bestTrunkScore") or 0) >= 0.58
                )
            ):
                archetypes.append("TRUNK_ACCESS")
            if set(route.get("transitModes", [])).intersection({"RAIL", "SUBWAY"}):
                archetypes.append("RAIL")
            if strategy == "egress_anchor":
                archetypes.append("EARLY_EGRESS")
            if kind == "mixed" and int(route.get("transfers") or 0) == min_mixed_transfers:
                archetypes.append("LOW_TRANSFER")

            if not archetypes:
                archetypes.append("BALANCED")
            if route_focus >= 1:
                primary_order = (
                    "BIKE_CYCLEWAY",
                    "BIKE_DIRECT",
                    "BIKE_QUIET",
                    "DIRECT_BIKE",
                    "BIKE_HEAVY",
                    "TRUNK_ACCESS",
                    "RAPID_BUS",
                    "PUBLIC_TRANSPORT",
                    "RAIL",
                    "EARLY_EGRESS",
                    "TRANSIT_HEAVY",
                    "LOW_TRANSFER",
                    "BALANCED",
                    "FASTEST",
                )
            else:
                primary_order = (
                    "BIKE_CYCLEWAY",
                    "BIKE_DIRECT",
                    "BIKE_QUIET",
                    "DIRECT_BIKE",
                    "TRUNK_ACCESS",
                    "RAPID_BUS",
                    "PUBLIC_TRANSPORT",
                    "RAIL",
                    "EARLY_EGRESS",
                    "TRANSIT_HEAVY",
                    "BIKE_HEAVY",
                    "LOW_TRANSFER",
                    "BALANCED",
                    "FASTEST",
                )
            route["archetypes"] = list(dict.fromkeys(archetypes))
            route["strategyArchetype"] = next(
                item for item in primary_order if item in route["archetypes"]
            )
            route["strategyGroup"] = self._strategy_group(route)
            classified.append(route)
        return classified

    @staticmethod
    def _strategy_group(route: dict[str, Any]) -> str:
        """Return a user-visible strategy bucket, coarser than an itinerary.

        Pareto is allowed to remove another variant inside this bucket, but the
        best route to a different trunk/surface corridor is preserved. This is
        the missing layer between broad archetypes such as TRANSIT_HEAVY and an
        exact stop-by-stop signature.
        """

        if route.get("kind") == "bike":
            return f"bike:{route.get('streetPreference') or 'default'}"

        transit_legs = [leg for leg in route.get("legs") or [] if leg.get("transitLeg")]
        if not transit_legs:
            return f"other:{route.get('strategy') or 'unknown'}"

        def route_token(leg: dict[str, Any]) -> str:
            route_obj = leg.get("route") or {}
            name = (
                route_obj.get("shortName")
                or route_obj.get("longName")
                or leg.get("mode")
                or "?"
            )
            return f"{leg.get('mode') or '?'}:{name}"

        dominant = max(
            transit_legs,
            key=lambda leg: (
                float(leg.get("distance") or 0),
                float((leg.get("lineMetrics") or {}).get("trunkScore") or 0),
                int(leg.get("duration") or 0),
            ),
        )
        modes: list[str] = []
        chain: list[str] = []
        for leg in transit_legs:
            mode = str(leg.get("mode") or "?")
            if not modes or modes[-1] != mode:
                modes.append(mode)
            token = route_token(leg)
            if not chain or chain[-1] != token:
                chain.append(token)

        legs = route.get("legs") or []
        first_transit = next(
            (index for index, leg in enumerate(legs) if leg.get("transitLeg")),
            0,
        )
        last_transit = max(
            (index for index, leg in enumerate(legs) if leg.get("transitLeg")),
            default=len(legs) - 1,
        )
        access_m = sum(
            float(leg.get("distance") or 0)
            for leg in legs[:first_transit]
            if leg.get("mode") == "BICYCLE"
        )
        egress_m = sum(
            float(leg.get("distance") or 0)
            for leg in legs[last_transit + 1 :]
            if leg.get("mode") == "BICYCLE"
        )

        def distance_band(distance: float) -> str:
            if distance < 1_200:
                return "near"
            if distance < 3_500:
                return "medium"
            return "far"

        anchor = route.get("anchor") or {}
        role = str(anchor.get("type") or route.get("strategy") or "mixed")
        return ":".join(
            (
                role,
                "-".join(modes),
                route_token(dominant),
                chain[0],
                f"legs{len(chain)}",
                distance_band(access_m),
                distance_band(egress_m),
                str(route.get("streetPreference") or "default"),
            )
        )

    def _pareto_prune(
        self,
        routes: list[dict[str, Any]],
        *,
        route_focus: int = 0,
        diagnostics: RoutingDiagnostics | None = None,
    ) -> list[dict[str, Any]]:
        """Keep routes not clearly dominated on time, transfers and discomfort.

        Small epsilons avoid retaining dozens of routes that differ by seconds,
        while still preserving meaningful trade-offs such as no-transfer vs faster.
        """
        if diagnostics:
            diagnostics.pareto_before = len(routes)
        if len(routes) <= 2:
            if diagnostics:
                diagnostics.pareto_after = len(routes)
            return routes

        routes = self._classify_strategies(routes, route_focus=route_focus)
        protected: set[int] = set()
        archetypes = {a for route in routes for a in route.get("archetypes") or []}
        for archetype in archetypes:
            best = self._best_for_archetype(routes, archetype)
            if best is not None:
                protected.add(id(best))
        strategy_groups: dict[str, list[dict[str, Any]]] = {}
        for route in routes:
            strategy_groups.setdefault(
                str(route.get("strategyGroup") or self._strategy_group(route)),
                [],
            ).append(route)
        for grouped_routes in strategy_groups.values():
            protected.add(id(min(grouped_routes, key=self._stable_route_key)))

        kept: list[dict[str, Any]] = []
        for candidate in routes:
            dominated = False
            if id(candidate) in protected:
                candidate["paretoStatus"] = "strategy_preserved"
                kept.append(candidate)
                continue
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
                    if diagnostics:
                        diagnostics.reject(
                            "dominated",
                            candidate,
                            dominatedBy={
                                "strategy": other.get("strategy"),
                                "archetypes": other.get("archetypes") or [],
                                "doorToDoor": other.get("doorToDoor"),
                                "score": other.get("score"),
                            },
                        )
                    break
            if not dominated:
                candidate["paretoStatus"] = "non_dominated"
                kept.append(candidate)

        if not kept:
            kept = sorted(routes, key=self._stable_route_key)[:12]
        kept = sorted(kept, key=self._stable_route_key)[:60]
        if diagnostics:
            diagnostics.pareto_after = len(kept)
        return kept

    # ------------------------------------------------------- similarity / diversity

    def _select_diverse(
        self,
        routes: list[dict[str, Any]],
        *,
        limit: int,
        route_focus: int,
        diagnostics: RoutingDiagnostics | None = None,
    ) -> list[dict[str, Any]]:
        if not routes:
            return []
        route_focus = min(2, max(-2, int(route_focus)))
        focus_cfg = ROUTE_FOCUS_CONFIG[route_focus]
        routes = sorted(routes, key=self._stable_route_key)
        best_time = min(float(r["doorToDoor"]) for r in routes)
        soft_limit = max(
            best_time * (1.0 + focus_cfg.time_tolerance_ratio),
            best_time + focus_cfg.time_tolerance_seconds,
        )
        hard_limit = max(best_time * 2.0, best_time + 90 * 60)
        eligible = [r for r in routes if float(r["doorToDoor"]) <= soft_limit]
        if not eligible:
            eligible = routes

        # The best representative of a strategy remains eligible even when an
        # unusually fast direct bicycle route sits outside its normal time band.
        for archetype in {a for r in routes for a in r.get("archetypes") or []}:
            best = self._best_for_archetype(routes, archetype)
            if (
                best is not None
                and float(best.get("doorToDoor") or math.inf) <= hard_limit
                and best not in eligible
            ):
                eligible.append(best)
        groups: dict[str, list[dict[str, Any]]] = {}
        for route in routes:
            groups.setdefault(
                str(route.get("strategyGroup") or self._strategy_group(route)),
                [],
            ).append(route)
        for grouped_routes in groups.values():
            best = min(grouped_routes, key=self._stable_route_key)
            if (
                float(best.get("doorToDoor") or math.inf) <= hard_limit
                and best not in eligible
            ):
                eligible.append(best)

        # First collapse high-overlap clusters. This specifically prevents
        # "same bus + same transfer + 300 m different bicycle approach" clones.
        representatives: list[dict[str, Any]] = []
        for route in eligible:
            cluster_index = next(
                (
                    i
                    for i, rep in enumerate(representatives)
                    if self._route_similarity(route, rep)
                    >= (
                        0.88
                        if route.get("streetPreference")
                        and rep.get("streetPreference")
                        and route.get("streetPreference") != rep.get("streetPreference")
                        else 0.80
                        if route.get("strategyGroup") == rep.get("strategyGroup")
                        else 0.88
                    )
                ),
                None,
            )
            if cluster_index is None:
                representatives.append(route)
            else:
                rep = representatives[cluster_index]
                if (route["score"], route["doorToDoor"]) < (rep["score"], rep["doorToDoor"]):
                    route["archetypes"] = list(
                        dict.fromkeys(
                            (route.get("archetypes") or []) + (rep.get("archetypes") or [])
                        )
                    )
                    representatives[cluster_index] = route
                else:
                    rep["archetypes"] = list(
                        dict.fromkeys(
                            (rep.get("archetypes") or []) + (route.get("archetypes") or [])
                        )
                    )
                if diagnostics:
                    diagnostics.clustered += 1
                    diagnostics.event(
                        "candidate_clustered",
                        route,
                        representative=RoutingDiagnostics._route_ref(
                            representatives[cluster_index]
                        ),
                    )

        direct_bike = self._best_matching(routes, lambda r: r.get("kind") == "bike")
        if direct_bike is not None and all(id(x) != id(direct_bike) for x in representatives):
            representatives.append(direct_bike)

        selected: list[dict[str, Any]] = []

        def add(
            route: dict[str, Any] | None,
            force: bool = False,
            preserve_strategy: bool = False,
        ) -> None:
            if route is None or len(selected) >= limit or route in selected:
                return
            if (
                not force
                and not preserve_strategy
                and route.get("kind") != "bike"
                and float(route["doorToDoor"]) > soft_limit
            ):
                return
            max_sim = max((self._route_similarity(route, x) for x in selected), default=0.0)
            if not force and max_sim >= (0.91 if preserve_strategy else 0.88):
                return
            selected.append(route)
            if diagnostics:
                diagnostics.event(
                    "candidate_selected",
                    route,
                    reason="archetype" if preserve_strategy else "rank_or_diversity",
                )

        # Archetypes are reserved before MMR.  Focus changes the reservation
        # order, so extreme slider values remain structurally different.
        if route_focus <= -1:
            archetype_order = (
                "FASTEST",
                "TRANSIT_HEAVY",
                "PUBLIC_TRANSPORT",
                "RAPID_BUS",
                "TRUNK_ACCESS",
                "RAIL",
                "LOW_TRANSFER",
                "BALANCED",
                "DIRECT_BIKE",
                "BIKE_CYCLEWAY",
                "BIKE_DIRECT",
                "BIKE_QUIET",
                "EARLY_EGRESS",
            )
        elif route_focus >= 1:
            archetype_order = (
                "FASTEST",
                "BIKE_HEAVY",
                "DIRECT_BIKE",
                "BIKE_CYCLEWAY",
                "BIKE_DIRECT",
                "BIKE_QUIET",
                "TRUNK_ACCESS",
                "RAPID_BUS",
                "PUBLIC_TRANSPORT",
                "RAIL",
                "EARLY_EGRESS",
                "BALANCED",
                "LOW_TRANSFER",
            )
        else:
            archetype_order = (
                "FASTEST",
                "BALANCED",
                "TRUNK_ACCESS",
                "RAPID_BUS",
                "PUBLIC_TRANSPORT",
                "RAIL",
                "LOW_TRANSFER",
                "EARLY_EGRESS",
                "BIKE_HEAVY",
                "TRANSIT_HEAVY",
                "DIRECT_BIKE",
                "BIKE_CYCLEWAY",
                "BIKE_DIRECT",
                "BIKE_QUIET",
            )
        mandatory = {
            "DIRECT_BIKE",
            "BIKE_DIRECT",
            "BIKE_CYCLEWAY",
            "PUBLIC_TRANSPORT",
            "RAPID_BUS",
        }
        if route_focus <= -1:
            mandatory.update({"TRANSIT_HEAVY", "TRUNK_ACCESS", "RAIL"})
        elif route_focus >= 1:
            mandatory.add("BIKE_HEAVY")

        # Keep the stable direct-bike baseline independently of focus.  It may
        # still sort below another recommendation after all scores are applied.
        add(direct_bike, force=True, preserve_strategy=True)
        for archetype in archetype_order:
            route = self._best_for_archetype(representatives, archetype)
            add(route, force=archetype == "DIRECT_BIKE", preserve_strategy=archetype in mandatory)

        lambda_similarity = 360.0 * (0.90 + 0.10 * abs(route_focus))
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
            chosen = next((item[3] for item in scored if item[1] < 0.88), None)
            if chosen is None:
                break
            before_add = len(selected)
            add(chosen)
            if len(selected) == before_add:
                break

        # A single-card result is especially unhelpful for a mixed navigator.
        # If strategy clustering was too aggressive, restore the best remaining
        # real candidate (prefer another bicycle street hypothesis) rather than
        # returning only one all-bicycle answer.
        minimum_target = min(
            focus_cfg.minimum_result_strategies,
            limit,
            len(representatives),
        )
        while len(selected) < minimum_target:
            remaining = [route for route in representatives if route not in selected]
            if not remaining:
                break
            existing_preferences = {
                route.get("streetPreference") for route in selected if route.get("streetPreference")
            }
            remaining.sort(
                key=lambda route: (
                    route.get("strategyGroup")
                    in {item.get("strategyGroup") for item in selected},
                    route.get("streetPreference") in existing_preferences,
                    route.get("score", math.inf),
                    route.get("doorToDoor", math.inf),
                )
            )
            restored = remaining[0]
            selected.append(restored)
            if diagnostics:
                diagnostics.event(
                    "candidate_selected",
                    restored,
                    reason="minimum_strategy_count",
                )

        selected.sort(key=self._stable_route_key)
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
        if diagnostics:
            diagnostics.selected = len(selected)
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
        mode_overlap = self._set_overlap(
            set(a.get("transitModes") or [leg.get("mode") for leg in a.get("legs") or [] if leg.get("transitLeg")]),
            set(b.get("transitModes") or [leg.get("mode") for leg in b.get("legs") or [] if leg.get("transitLeg")]),
        )
        transit_overlap = 0.55 * transit_corridor + 0.25 * line_overlap + 0.20 * mode_overlap
        bike_overlap = self._set_overlap(a_bike, b_bike)
        transfer_overlap = self._set_overlap(self._transfer_stops(a), self._transfer_stops(b))
        endpoint_overlap = self._set_overlap(
            self._boarding_egress_stops(a),
            self._boarding_egress_stops(b),
        )
        strategy_overlap = (
            1.0
            if a.get("strategyArchetype")
            and a.get("strategyArchetype") == b.get("strategyArchetype")
            else 0.0
        )
        return max(
            0.0,
            min(
                1.0,
                0.48 * transit_overlap
                + 0.20 * bike_overlap
                + 0.12 * transfer_overlap
                + 0.10 * endpoint_overlap
                + 0.10 * strategy_overlap,
            ),
        )

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
    def _boarding_egress_stops(route: dict[str, Any]) -> set[str]:
        transit = [leg for leg in route.get("legs") or [] if leg.get("transitLeg")]
        if not transit:
            return set()
        return {
            str((transit[0].get("from") or {}).get("name") or ""),
            str((transit[-1].get("to") or {}).get("name") or ""),
        } - {""}

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
            elif "TRUNK_ACCESS" in (route.get("archetypes") or []):
                label = "Велосипед к сильной линии"
            elif "RAPID_BUS" in (route.get("archetypes") or []):
                label = "Магистральный / экспресс-автобус"
            elif "PUBLIC_TRANSPORT" in (route.get("archetypes") or []):
                label = "Максимум общественного транспорта"
            elif route.get("strategy") == "egress_anchor":
                label = "Ранний выход → велосипед"
            elif route is min_transfers and route.get("kind") == "mixed":
                label = "Меньше пересадок"
            elif "BIKE_CYCLEWAY" in (route.get("archetypes") or []):
                label = "По велодорожкам"
            elif "BIKE_DIRECT" in (route.get("archetypes") or []):
                label = "Более прямой веломаршрут"
            elif "BIKE_QUIET" in (route.get("archetypes") or []):
                label = "Тихий веломаршрут"
            elif route.get("kind") == "bike":
                label = "Только велосипед"
            elif set(route.get("transitModes", [])).intersection({"RAIL", "SUBWAY"}):
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

            protected_feeder = next(
                (
                    item
                    for item in optimization.get("transitLegDecisions") or []
                    if item.get("reason") == "scheduled_connection_protected"
                ),
                None,
            )
            if protected_feeder:
                route_name = protected_feeder.get("route") or protected_feeder.get("mode") or "ОТ"
                notes.append(f"{route_name} сохранён, чтобы успеть на следующий рейс")

            if float(route.get("bestTrunkScore") or 0) >= 0.68 and route.get("bestTrunkRoute"):
                notes.append(f"Используется сильная линия {route['bestTrunkRoute']}")
            rapid_class = route.get("bestRapidBusClass")
            rapid_route = route.get("bestRapidBusRoute")
            if rapid_class in {"express", "trunk", "rapid"} and rapid_route:
                kind = {
                    "express": "экспресс",
                    "trunk": "магистральный маршрут",
                    "rapid": "быстрый автобус",
                }[rapid_class]
                notes.append(f"{rapid_route} — {kind}")
            if "PUBLIC_TRANSPORT" in (route.get("archetypes") or []):
                notes.append("Почти весь путь проходит на общественном транспорте")
            if route.get("kind") == "mixed" and int(route.get("transitTransfers") or 0) == 0:
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
    def _candidate_is_valid(route: dict[str, Any]) -> bool:
        legs = route.get("legs") or []
        if not legs or float(route.get("doorToDoor") or 0) <= 0:
            return False
        if any(str(leg.get("mode") or "").upper() in {"CAR", "MOTORCYCLE"} for leg in legs):
            return False
        return all(
            int(leg.get("duration") or 0) >= 0
            and float(leg.get("distance") or 0) >= 0
            for leg in legs
        )

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
                manual_replacements = previous.setdefault("manualReplacements", [])
                if previous.get("manualReplacement"):
                    manual_replacements.append(previous.pop("manualReplacement"))
                if leg.get("manualReplacement"):
                    manual_replacements.append(deepcopy(leg.get("manualReplacement")))
            else:
                result.append(leg)
        return result

    def _dedupe_exact(self, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple, dict[str, Any]] = {}
        for route in routes:
            route.setdefault(
                "candidateFamilies",
                [str(route.get("sourceQuery") or route.get("strategy") or "unknown")],
            )
            key = self._exact_signature(route)
            previous = best.get(key)
            if previous is None or (
                float(route.get("score") or math.inf),
                float(route.get("doorToDoor") or math.inf),
            ) < (
                float(previous.get("score") or math.inf),
                float(previous.get("doorToDoor") or math.inf),
            ):
                if previous is not None:
                    route["candidateFamilies"] = list(
                        dict.fromkeys(
                            (route.get("candidateFamilies") or [])
                            + (previous.get("candidateFamilies") or [])
                        )
                    )
                best[key] = route
            elif previous is not None:
                previous["candidateFamilies"] = list(
                    dict.fromkeys(
                        (previous.get("candidateFamilies") or [])
                        + (route.get("candidateFamilies") or [])
                    )
                )
                special = {"trunk_access": 0, "boarding_anchor": 1, "egress_anchor": 1}
                if special.get(str(route.get("strategy")), 99) < special.get(
                    str(previous.get("strategy")), 99
                ):
                    previous["strategy"] = route.get("strategy")
                    previous["anchor"] = deepcopy(route.get("anchor"))
        return list(best.values())

    @staticmethod
    def _exact_signature(route: dict[str, Any]) -> tuple:
        legs_signature = tuple(
            (
                leg.get("mode"),
                (leg.get("route") or {}).get("shortName") or (leg.get("route") or {}).get("longName"),
                (leg.get("from") or {}).get("name"),
                (leg.get("to") or {}).get("name"),
                round(float(leg.get("distance") or 0) / 100.0),
            )
            for leg in route.get("legs") or []
        )
        if route.get("streetPreference"):
            return (("BIKE_STRATEGY", route.get("streetPreference")),) + legs_signature
        return legs_signature

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
        return min(matches, key=RoutePlanner._stable_route_key) if matches else None

    @staticmethod
    def _best_for_archetype(
        routes: list[dict[str, Any]],
        archetype: str,
    ) -> dict[str, Any] | None:
        matches = [r for r in routes if archetype in (r.get("archetypes") or [])]
        if not matches:
            return None
        if archetype == "FASTEST":
            return min(
                matches,
                key=lambda r: (
                    r.get("doorToDoor", math.inf),
                    RoutePlanner._stable_route_key(r),
                ),
            )
        if archetype == "LOW_TRANSFER":
            return min(
                matches,
                key=lambda r: (
                    r.get("transfers", math.inf),
                    r.get("score", math.inf),
                    r.get("doorToDoor", math.inf),
                ),
            )
        return min(matches, key=RoutePlanner._stable_route_key)

    @staticmethod
    def _stable_route_key(route: dict[str, Any]) -> tuple:
        preference_rank = {
            "cycleway": 0,
            "direct": 1,
            "quiet": 2,
        }.get(route.get("streetPreference"), 3)
        return (
            route.get("score", math.inf),
            route.get("doorToDoor", math.inf),
            preference_rank,
            str(route.get("strategy") or ""),
            str(route.get("sourceQuery") or ""),
            tuple(route.get("transitRoutes") or []),
        )

    @staticmethod
    def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
        try:
            return min(high, max(low, int(value)))
        except (TypeError, ValueError):
            return default
