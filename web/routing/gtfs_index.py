from __future__ import annotations

import csv
import io
import math
import statistics
import threading
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .models import ROUTE_FOCUS_CONFIG


ROUTE_TYPE_TO_MODE = {
    "0": "TRAM",
    "1": "SUBWAY",
    "2": "RAIL",
    "3": "BUS",
    "11": "TROLLEYBUS",
}
SUPPORTED_MODES = {"BUS", "TRAM", "TROLLEYBUS", "RAIL", "SUBWAY"}


@dataclass(frozen=True)
class LineMetrics:
    route_id: str
    mode: str
    trip_count: int
    median_headway_s: float | None
    commercial_speed_kmh: float | None
    bikes_allowed_ratio: float | None
    trunk_score: float


@dataclass(frozen=True)
class StopInfo:
    stop_id: str
    name: str
    lat: float
    lon: float
    route_count: int
    modes: tuple[str, ...]
    route_ids: tuple[str, ...]
    best_trunk_score: float


@dataclass(frozen=True)
class Anchor:
    stop_id: str
    name: str
    lat: float
    lon: float
    distance_from_origin_m: float
    distance_to_destination_m: float
    corridor_distance_m: float
    projection: float
    route_count: int
    modes: tuple[str, ...]
    best_trunk_score: float
    trunk_routes: tuple[str, ...]
    score: float


class GtfsIndex:
    """Compact GTFS index for multimodal hypothesis generation.

    OpenTripPlanner remains the schedule oracle. This index deliberately does not
    attempt to route trips. It answers cheaper questions needed by the hybrid
    planner: which stops are useful anchors, and which lines look like strong
    transit backbones rather than slow local micro-legs.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._loaded = False
        self._error: str | None = None
        self._stops: list[StopInfo] = []
        self._stop_by_id: dict[str, StopInfo] = {}
        self._line_metrics: dict[str, LineMetrics] = {}
        self._trip_to_route: dict[str, str] = {}
        self._trip_bikes_allowed: dict[str, int | None] = {}

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

    @property
    def line_count(self) -> int:
        self.ensure_loaded()
        return len(self._line_metrics)

    def ensure_loaded(self) -> None:
        if self._loaded or self._error is not None:
            return
        with self._lock:
            if self._loaded or self._error is not None:
                return
            try:
                self._load()
                self._loaded = True
            except Exception as exc:  # deep search is optional; base routing must survive
                self._error = str(exc)

    @staticmethod
    def _gtfs_seconds(value: str | None) -> int | None:
        if not value:
            return None
        try:
            h, m, s = (int(part) for part in value.split(":"))
            return h * 3600 + m * 60 + s
        except (TypeError, ValueError):
            return None

    def _load(self) -> None:
        path = Path(self.path)
        if not path.is_file():
            raise FileNotFoundError(f"GTFS для deep-search не найден: {path}")

        with zipfile.ZipFile(path) as z:
            stops: dict[str, tuple[str, float, float]] = {}
            with z.open("stops.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    try:
                        stop_id = row["stop_id"]
                        stops[stop_id] = (
                            row.get("stop_name") or stop_id,
                            float(row["stop_lat"]),
                            float(row["stop_lon"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue

            routes: dict[str, dict[str, str]] = {}
            with z.open("routes.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    route_id = row.get("route_id")
                    if not route_id:
                        continue
                    routes[route_id] = {
                        "mode": ROUTE_TYPE_TO_MODE.get(str(row.get("route_type") or ""), "OTHER"),
                        "name": row.get("route_short_name") or row.get("route_long_name") or route_id,
                    }

            trip_route: dict[str, str] = {}
            trip_service: dict[str, str] = {}
            trip_bikes: dict[str, int | None] = {}
            route_trip_count: dict[str, int] = defaultdict(int)
            route_bike_allowed: dict[str, list[int]] = defaultdict(list)
            with z.open("trips.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    trip_id = row.get("trip_id")
                    route_id = row.get("route_id")
                    if not trip_id or not route_id:
                        continue
                    trip_route[trip_id] = route_id
                    trip_service[trip_id] = row.get("service_id") or ""
                    route_trip_count[route_id] += 1
                    bikes_raw = row.get("bikes_allowed")
                    bikes: int | None = None
                    if bikes_raw not in (None, ""):
                        try:
                            bikes = int(bikes_raw)
                        except ValueError:
                            bikes = None
                    trip_bikes[trip_id] = bikes
                    if bikes in (1, 2):
                        route_bike_allowed[route_id].append(1 if bikes == 1 else 0)

            stop_routes: dict[str, set[str]] = {stop_id: set() for stop_id in stops}
            route_speeds: dict[str, list[float]] = defaultdict(list)
            route_service_starts: dict[str, dict[str, list[int]]] = defaultdict(
                lambda: defaultdict(list)
            )

            current_trip: str | None = None
            first_departure: int | None = None
            last_arrival: int | None = None
            previous_coord: tuple[float, float] | None = None
            trip_distance_m = 0.0

            def flush_trip() -> None:
                nonlocal current_trip, first_departure, last_arrival, previous_coord, trip_distance_m
                if current_trip is None:
                    return
                route_id = trip_route.get(current_trip)
                if route_id:
                    if first_departure is not None:
                        route_service_starts[route_id][trip_service.get(current_trip, "")].append(
                            first_departure
                        )
                    if (
                        first_departure is not None
                        and last_arrival is not None
                        and last_arrival > first_departure
                        and trip_distance_m >= 500
                    ):
                        duration_h = (last_arrival - first_departure) / 3600.0
                        speed = (trip_distance_m / 1000.0) / duration_h
                        if 3.0 <= speed <= 90.0:
                            route_speeds[route_id].append(speed)
                current_trip = None
                first_departure = None
                last_arrival = None
                previous_coord = None
                trip_distance_m = 0.0

            with z.open("stop_times.txt") as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                for row in reader:
                    trip_id = row.get("trip_id")
                    stop_id = row.get("stop_id")
                    if not trip_id or not stop_id:
                        continue

                    if current_trip != trip_id:
                        flush_trip()
                        current_trip = trip_id

                    route_id = trip_route.get(trip_id)
                    if route_id and stop_id in stop_routes:
                        stop_routes[stop_id].add(route_id)

                    departure = self._gtfs_seconds(row.get("departure_time"))
                    arrival = self._gtfs_seconds(row.get("arrival_time"))
                    if first_departure is None and departure is not None:
                        first_departure = departure
                    if arrival is not None:
                        last_arrival = arrival
                    elif departure is not None:
                        last_arrival = departure

                    stop = stops.get(stop_id)
                    if stop is not None:
                        coord = (stop[1], stop[2])
                        if previous_coord is not None:
                            trip_distance_m += haversine_m(
                                previous_coord[0], previous_coord[1], coord[0], coord[1]
                            )
                        previous_coord = coord

                flush_trip()

        line_metrics: dict[str, LineMetrics] = {}
        for route_id, route in routes.items():
            mode = route["mode"]
            starts_by_service = route_service_starts.get(route_id, {})
            headway_samples: list[int] = []
            for starts in starts_by_service.values():
                starts.sort()
                for a, b in zip(starts, starts[1:]):
                    gap = b - a
                    # Ignore duplicate/simultaneous starts and night-size gaps.
                    if 120 <= gap <= 7_200:
                        headway_samples.append(gap)
            median_headway = (
                float(statistics.median(headway_samples)) if headway_samples else None
            )
            speed_samples = route_speeds.get(route_id) or []
            commercial_speed = (
                float(statistics.median(speed_samples)) if speed_samples else None
            )
            bike_values = route_bike_allowed.get(route_id) or []
            bikes_ratio = sum(bike_values) / len(bike_values) if bike_values else None
            trip_count = int(route_trip_count.get(route_id, 0))

            freq_score = (
                _clamp(1.0 - median_headway / 900.0, 0.0, 1.0)
                if median_headway is not None
                else _clamp(math.log1p(trip_count) / 6.0, 0.0, 0.55)
            )
            speed_score = (
                _clamp(commercial_speed / 25.0, 0.0, 1.0)
                if commercial_speed is not None
                else 0.45
            )
            density_score = _clamp(math.log1p(trip_count) / 7.0, 0.0, 1.0)
            mode_bonus = {
                "RAIL": 1.0,
                "TRAM": 0.82,
                "TROLLEYBUS": 0.56,
                "BUS": 0.50,
            }.get(mode, 0.35)
            trunk_score = _clamp(
                0.35 * freq_score
                + 0.25 * speed_score
                + 0.20 * density_score
                + 0.20 * mode_bonus,
                0.0,
                1.0,
            )

            line_metrics[route_id] = LineMetrics(
                route_id=route_id,
                mode=mode,
                trip_count=trip_count,
                median_headway_s=median_headway,
                commercial_speed_kmh=commercial_speed,
                bikes_allowed_ratio=bikes_ratio,
                trunk_score=trunk_score,
            )

        stop_infos: list[StopInfo] = []
        for stop_id, (name, lat, lon) in stops.items():
            route_ids = tuple(sorted(stop_routes.get(stop_id) or set()))
            modes = tuple(
                sorted(
                    {
                        routes.get(route_id, {}).get("mode", "OTHER")
                        for route_id in route_ids
                    }
                )
            )
            best_trunk = max(
                (line_metrics[r].trunk_score for r in route_ids if r in line_metrics),
                default=0.0,
            )
            stop_infos.append(
                StopInfo(
                    stop_id=stop_id,
                    name=name,
                    lat=lat,
                    lon=lon,
                    route_count=len(route_ids),
                    modes=modes,
                    route_ids=route_ids,
                    best_trunk_score=best_trunk,
                )
            )

        self._stops = stop_infos
        self._stop_by_id = {stop.stop_id: stop for stop in stop_infos}
        self._line_metrics = line_metrics
        self._trip_to_route = trip_route
        self._trip_bikes_allowed = trip_bikes

    @staticmethod
    def _strip_feed_id(gtfs_id: str | None) -> str | None:
        if not gtfs_id:
            return None
        text = str(gtfs_id)
        return text.split(":", 1)[1] if ":" in text else text

    def line_metrics_for_trip(self, trip_gtfs_id: str | None) -> LineMetrics | None:
        self.ensure_loaded()
        if not self._loaded:
            return None
        trip_id = self._strip_feed_id(trip_gtfs_id)
        if not trip_id:
            return None
        route_id = self._trip_to_route.get(trip_id)
        return self._line_metrics.get(route_id) if route_id else None

    def line_metrics_for_route(self, route_id: str | None) -> LineMetrics | None:
        self.ensure_loaded()
        if not self._loaded or not route_id:
            return None
        return self._line_metrics.get(self._strip_feed_id(route_id) or route_id)

    def bike_allowed_for_trip(self, trip_gtfs_id: str | None) -> int | None:
        """Return GTFS bikes_allowed for a concrete trip when explicitly known.

        GTFS values: 1 = allowed, 2 = forbidden, 0/blank = no information.
        Unknown is deliberately not treated as forbidden because many feeds omit
        this optional field; project-level Bike Policy Compiler can tighten this.
        """
        self.ensure_loaded()
        if not self._loaded:
            return None
        trip_id = self._strip_feed_id(trip_gtfs_id)
        if not trip_id:
            return None
        value = self._trip_bikes_allowed.get(trip_id)
        return value if value in (1, 2) else None

    def stop(self, stop_id: str) -> StopInfo | None:
        self.ensure_loaded()
        return self._stop_by_id.get(self._strip_feed_id(stop_id) or stop_id)

    def boarding_anchors(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        limit: int = 10,
        route_focus: int = 0,
    ) -> list[Anchor]:
        """Find useful places to *ride to* before boarding public transport.

        The nearest stop is intentionally not privileged. Strong trunk-like lines,
        direction of travel and spatial diversity matter more than raw distance.
        """
        return self._anchors(
            origin=origin,
            destination=destination,
            limit=limit,
            route_focus=route_focus,
            role="boarding",
        )

    def egress_anchors(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        limit: int = 8,
        route_focus: int = 0,
    ) -> list[Anchor]:
        return self._anchors(
            origin=origin,
            destination=destination,
            limit=limit,
            route_focus=route_focus,
            role="egress",
        )

    def _anchors(
        self,
        *,
        origin: tuple[float, float],
        destination: tuple[float, float],
        limit: int,
        route_focus: int,
        role: str,
    ) -> list[Anchor]:
        self.ensure_loaded()
        if not self._loaded:
            return []

        direct_m = haversine_m(*origin, *destination)
        if direct_m < 3_000:
            return []

        route_focus = min(2, max(-2, int(route_focus)))
        focus = ROUTE_FOCUS_CONFIG[route_focus]
        if role == "boarding":
            max_anchor_m = min(
                focus.max_bike_access_m,
                max(1_800.0, direct_m * 0.38),
            )
            target_distance = focus.access_target_m
        else:
            max_anchor_m = min(
                focus.max_bike_egress_m,
                max(
                    3_500.0,
                    direct_m
                    * min(0.90, focus.max_bike_egress_m / max(1.0, direct_m)),
                ),
            )
            target_distance = focus.egress_target_m

        candidates: list[Anchor] = []
        for stop in self._stops:
            if stop.route_count == 0 or not set(stop.modes).intersection(SUPPORTED_MODES):
                continue

            d_origin = haversine_m(stop.lat, stop.lon, *origin)
            d_dest = haversine_m(stop.lat, stop.lon, *destination)
            projection, corridor_m = project_to_segment(
                origin[0], origin[1], destination[0], destination[1], stop.lat, stop.lon
            )

            if role == "boarding":
                endpoint_distance = d_origin
                if endpoint_distance < 250 or endpoint_distance > max_anchor_m:
                    continue
                # A boarding anchor may be slightly behind the origin for a very strong line,
                # but normally it should make progress along the OD corridor.
                if projection < -0.08 or projection > 0.62:
                    continue
            else:
                endpoint_distance = d_dest
                if endpoint_distance < 800 or endpoint_distance > max_anchor_m:
                    continue
                if projection < 0.34 or projection > 1.12:
                    continue

            corridor_limit = min(4_800.0, max(1_700.0, direct_m * 0.22))
            if corridor_m > corridor_limit:
                continue

            metrics = [
                self._line_metrics[r]
                for r in stop.route_ids
                if r in self._line_metrics and self._line_metrics[r].mode in SUPPORTED_MODES
            ]
            trunk_routes = tuple(
                m.route_id
                for m in sorted(metrics, key=lambda item: item.trunk_score, reverse=True)[:3]
                if m.trunk_score >= 0.58
            )
            best_trunk = max((m.trunk_score for m in metrics), default=0.0)
            top2 = sorted((m.trunk_score for m in metrics), reverse=True)[:2]
            line_quality = sum(top2) / len(top2) if top2 else 0.0

            route_value = min(2.2, math.log1p(stop.route_count) * 0.62)
            mode_value = 0.30 * len(
                set(stop.modes).intersection({"BUS", "TRAM", "RAIL", "SUBWAY"})
            )
            trunk_value = 4.2 * line_quality
            corridor_value = max(0.0, 1.8 * (1.0 - corridor_m / corridor_limit))
            distance_value = max(0.0, 1.6 - abs(endpoint_distance - target_distance) / max(900.0, target_distance))

            if role == "boarding":
                # Reward useful progress, but do not over-favour the furthest stop.
                progress_value = max(0.0, min(1.5, projection * 2.7))
            else:
                progress_value = max(0.0, min(1.5, (1.0 - abs(1.0 - projection)) * 1.5))

            candidates.append(
                Anchor(
                    stop_id=stop.stop_id,
                    name=stop.name,
                    lat=stop.lat,
                    lon=stop.lon,
                    distance_from_origin_m=d_origin,
                    distance_to_destination_m=d_dest,
                    corridor_distance_m=corridor_m,
                    projection=projection,
                    route_count=stop.route_count,
                    modes=stop.modes,
                    best_trunk_score=best_trunk,
                    trunk_routes=trunk_routes,
                    score=(
                        trunk_value
                        + route_value
                        + mode_value
                        + corridor_value
                        + distance_value
                        + progress_value
                    ),
                )
            )

        return self._select_spatially_diverse(candidates, limit, role)

    @staticmethod
    def _select_spatially_diverse(
        candidates: list[Anchor], limit: int, role: str
    ) -> list[Anchor]:
        if not candidates or limit <= 0:
            return []

        if role == "boarding":
            bands = ((250, 1_000), (1_000, 2_000), (2_000, 3_500), (3_500, 6_501))
            distance = lambda a: a.distance_from_origin_m
        else:
            bands = ((800, 2_500), (2_500, 4_500), (4_500, 7_500), (7_500, 14_001))
            distance = lambda a: a.distance_to_destination_m

        selected: list[Anchor] = []
        per_band = max(1, math.ceil(limit / len(bands)))
        for low, high in bands:
            band = [a for a in candidates if low <= distance(a) < high]
            band.sort(key=lambda a: (a.score, a.best_trunk_score), reverse=True)
            used = 0
            for anchor in band:
                if used >= per_band:
                    break
                if any(haversine_m(anchor.lat, anchor.lon, x.lat, x.lon) < 550 for x in selected):
                    continue
                selected.append(anchor)
                used += 1

        for anchor in sorted(candidates, key=lambda a: (a.score, a.best_trunk_score), reverse=True):
            if len(selected) >= limit:
                break
            if anchor in selected:
                continue
            if any(haversine_m(anchor.lat, anchor.lon, x.lat, x.lon) < 550 for x in selected):
                continue
            selected.append(anchor)

        return selected[:limit]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
