from __future__ import annotations

import csv
import io
import math
import os
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path


ROUTE_TYPE_TO_MODE = {
    "0": "TRAM",
    "1": "SUBWAY",
    "2": "RAIL",
    "3": "BUS",
    "11": "TROLLEYBUS",
}


@dataclass(frozen=True)
class StopInfo:
    stop_id: str
    name: str
    lat: float
    lon: float
    route_count: int
    modes: tuple[str, ...]


@dataclass(frozen=True)
class Anchor:
    stop_id: str
    name: str
    lat: float
    lon: float
    distance_to_destination_m: float
    corridor_distance_m: float
    route_count: int
    modes: tuple[str, ...]
    score: float


class GtfsIndex:
    """Small in-memory index used only to generate routing hypotheses.

    OTP remains the source of truth for schedules and route validity. This index
    only answers: "which stops are interesting enough to ask OTP about?"
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._error: str | None = None
        self._stops: list[StopInfo] = []

    @property
    def loaded(self) -> bool:
        self.ensure_loaded()
        return self._loaded

    @property
    def error(self) -> str | None:
        self.ensure_loaded()
        return self._error

    @property
    def stop_count(self) -> int:
        self.ensure_loaded()
        return len(self._stops)

    def ensure_loaded(self) -> None:
        if self._loaded or self._error is not None:
            return
        with self._lock:
            if self._loaded or self._error is not None:
                return
            try:
                self._load()
                self._loaded = True
            except Exception as exc:  # anchor search is optional; routing must still work
                self._error = str(exc)

    def _load(self) -> None:
        path = Path(self.path)
        if not path.is_file():
            raise FileNotFoundError(f"GTFS для anchor-поиска не найден: {path}")

        with zipfile.ZipFile(path) as z:
            stops = {}
            with z.open("stops.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    try:
                        stops[row["stop_id"]] = (
                            row.get("stop_name") or row["stop_id"],
                            float(row["stop_lat"]),
                            float(row["stop_lon"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue

            routes = {}
            with z.open("routes.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    route_id = row.get("route_id")
                    if not route_id:
                        continue
                    routes[route_id] = ROUTE_TYPE_TO_MODE.get(str(row.get("route_type") or ""), "OTHER")

            trip_route = {}
            with z.open("trips.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    trip_id = row.get("trip_id")
                    route_id = row.get("route_id")
                    if trip_id and route_id:
                        trip_route[trip_id] = route_id

            stop_routes: dict[str, set[str]] = {stop_id: set() for stop_id in stops}
            with z.open("stop_times.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    stop_id = row.get("stop_id")
                    trip_id = row.get("trip_id")
                    if not stop_id or stop_id not in stop_routes or not trip_id:
                        continue
                    route_id = trip_route.get(trip_id)
                    if route_id:
                        stop_routes[stop_id].add(route_id)

        result = []
        for stop_id, (name, lat, lon) in stops.items():
            route_ids = stop_routes.get(stop_id) or set()
            modes = tuple(sorted({routes.get(route_id, "OTHER") for route_id in route_ids}))
            result.append(
                StopInfo(
                    stop_id=stop_id,
                    name=name,
                    lat=lat,
                    lon=lon,
                    route_count=len(route_ids),
                    modes=modes,
                )
            )
        self._stops = result

    def egress_anchors(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        limit: int = 8,
        route_focus: int = 0,
    ) -> list[Anchor]:
        self.ensure_loaded()
        if not self._loaded:
            return []

        direct_m = haversine_m(*origin, *destination)
        if direct_m < 4_000:
            return []

        route_focus = min(2, max(-2, int(route_focus)))
        max_fraction = {
            -2: 0.48,
            -1: 0.58,
            0: 0.68,
            1: 0.78,
            2: 0.88,
        }[route_focus]
        max_cap = {
            -2: 7_000.0,
            -1: 8_500.0,
            0: 10_000.0,
            1: 12_000.0,
            2: 14_000.0,
        }[route_focus]
        max_egress_m = min(max_cap, max(4_000.0, direct_m * max_fraction))
        candidates: list[Anchor] = []

        for stop in self._stops:
            if stop.route_count == 0:
                continue
            if not set(stop.modes).intersection({"BUS", "TRAM", "TROLLEYBUS", "RAIL"}):
                continue

            d_dest = haversine_m(stop.lat, stop.lon, *destination)
            if d_dest < 1_000 or d_dest > max_egress_m:
                continue

            projection, corridor_m = project_to_segment(
                origin[0], origin[1], destination[0], destination[1], stop.lat, stop.lon
            )
            # Route focus changes how early we are willing to leave transit.
            # "Велопрогулка" can exit much earlier, while a transport-heavy route
            # keeps the anchor closer to the destination.
            min_projection = {
                -2: 0.52,
                -1: 0.44,
                0: 0.36,
                1: 0.29,
                2: 0.22,
            }[route_focus]
            if projection < min_projection or projection > 1.15:
                continue
            if corridor_m > 5_000:
                continue

            transport_value = math.log1p(stop.route_count) * 2.1
            mode_bonus = 0.7 * len(set(stop.modes).intersection({"BUS", "TRAM", "RAIL"}))
            corridor_bonus = max(0.0, 2.5 * (1.0 - corridor_m / 5_000.0))
            # The preferred egress length moves with route focus. This affects
            # candidate generation itself, not just the final score.
            target = {
                -2: 2_000.0,
                -1: 3_000.0,
                0: 4_500.0,
                1: 6_000.0,
                2: 8_000.0,
            }[route_focus]
            egress_bonus = max(0.0, 2.4 - abs(d_dest - target) / 2_800.0)

            candidates.append(
                Anchor(
                    stop_id=stop.stop_id,
                    name=stop.name,
                    lat=stop.lat,
                    lon=stop.lon,
                    distance_to_destination_m=d_dest,
                    corridor_distance_m=corridor_m,
                    route_count=stop.route_count,
                    modes=stop.modes,
                    score=transport_value + mode_bonus + corridor_bonus + egress_bonus,
                )
            )

        # Preserve several egress-distance scales instead of taking only the globally
        # highest scoring central interchange stops.
        bands = (
            (1_000, 2_500),
            (2_500, 4_500),
            (4_500, 7_000),
            (7_000, 10_000),
            (10_000, 14_001),
        )
        selected: list[Anchor] = []
        per_band = max(1, math.ceil(limit / len(bands)))

        for low, high in bands:
            band = [a for a in candidates if low <= a.distance_to_destination_m < high]
            band.sort(key=lambda a: a.score, reverse=True)
            for anchor in band:
                if len([x for x in selected if low <= x.distance_to_destination_m < high]) >= per_band:
                    break
                if any(haversine_m(anchor.lat, anchor.lon, x.lat, x.lon) < 600 for x in selected):
                    continue
                selected.append(anchor)

        if len(selected) < limit:
            for anchor in sorted(candidates, key=lambda a: a.score, reverse=True):
                if anchor in selected:
                    continue
                if any(haversine_m(anchor.lat, anchor.lon, x.lat, x.lon) < 600 for x in selected):
                    continue
                selected.append(anchor)
                if len(selected) >= limit:
                    break

        return selected[:limit]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def project_to_segment(
    a_lat: float,
    a_lon: float,
    b_lat: float,
    b_lon: float,
    p_lat: float,
    p_lon: float,
) -> tuple[float, float]:
    """Return projection ratio and approximate perpendicular distance in meters."""
    ref_lat = math.radians((a_lat + b_lat + p_lat) / 3.0)
    scale_x = 111_320.0 * math.cos(ref_lat)
    scale_y = 110_540.0

    ax, ay = a_lon * scale_x, a_lat * scale_y
    bx, by = b_lon * scale_x, b_lat * scale_y
    px, py = p_lon * scale_x, p_lat * scale_y

    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    if denom == 0:
        return 0.0, math.hypot(px - ax, py - ay)

    t = ((px - ax) * vx + (py - ay) * vy) / denom
    cx, cy = ax + t * vx, ay + t * vy
    return t, math.hypot(px - cx, py - cy)
