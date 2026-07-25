from __future__ import annotations

import concurrent.futures
import itertools
import math
import time
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .gtfs_index import Anchor, GtfsIndex
from .models import (
    PROFILE_CONFIG,
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
            max_transfers = min(5, max(0, int(payload.get("maxTransfers", 2))))
        except (TypeError, ValueError):
            max_transfers = 2

        generic_routes, warnings, query_stats = self._generic_candidates(
            origin, destination, departure, profile, max_transfers
        )

        anchor_routes: list[dict[str, Any]] = []
        anchors: list[Anchor] = []
        anchor_error = None
        if payload.get("deepSearch", True):
            try:
                anchors = self.gtfs.egress_anchors(origin, destination, limit=8)
                anchor_routes = self._egress_anchor_candidates(
                    anchors, origin, destination, departure, profile, max_transfers
                )
            except Exception as exc:
                # Deep search must never break basic routing.
                anchor_error = str(exc)

        all_candidates = self._dedupe_exact(generic_routes + anchor_routes)
        all_candidates = [self._score_route(r, profile) for r in all_candidates]
        selected = self._select_diverse(all_candidates, limit=8)
        self._assign_recommendation_labels(selected)

        return {
            "routes": selected,
            "warnings": warnings,
            "stats": {
                "genericQueries": query_stats["queries"],
                "genericCandidates": len(generic_routes),
                "anchorsConsidered": len(anchors),
                "anchorCandidates": len(anchor_routes),
                "candidatesTotal": len(all_candidates),
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
                "name": "no_transfers",
                "profile": selected_profile,
                "modes": ALL_TRANSIT,
                "max_transfers": 0,
                "strategy": "no_transfers",
            },
        ]
        if max_transfers >= 1:
            specs.append(
                {
                    "name": "one_transfer",
                    "profile": selected_profile,
                    "modes": ALL_TRANSIT,
                    "max_transfers": 1,
                    "strategy": "one_transfer",
                }
            )

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
                first=10,
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

    def _score_route(self, route: dict[str, Any], profile_key: str) -> dict[str, Any]:
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

    def _select_diverse(self, routes: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if not routes:
            return []

        routes = sorted(routes, key=lambda r: (r["score"], r["doorToDoor"]))
        best_time = min(r["doorToDoor"] for r in routes)
        soft_limit = max(best_time * 1.50, best_time + 15 * 60)

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

        # Explicit diversity quotas. This is intentionally not "top N by score".
        add(min(routes, key=lambda r: r["doorToDoor"]))
        add(min(routes, key=lambda r: r["score"]))
        add(self._best_matching(routes, lambda r: r.get("strategy") == "egress_anchor"))
        add(self._best_matching(routes, lambda r: "BUS" in r.get("transitModes", []) or "TROLLEYBUS" in r.get("transitModes", [])))
        add(self._best_matching(routes, lambda r: "TRAM" in r.get("transitModes", [])))
        add(self._best_matching(routes, lambda r: "RAIL" in r.get("transitModes", [])))
        add(self._best_matching(routes, lambda r: r.get("kind") == "bike"), force=True)

        for route in routes:
            add(route)
            if len(selected) >= limit:
                break

        # Stable display: best recommendation first, then ascending score.
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
