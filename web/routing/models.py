from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

PROFILE_CONFIG = {
    "fast": {
        "name": "Прямее",
        "speed_kmh": 11.0,
        "speed_mps": 11.0 / 3.6,
        "transit_replace_max_bike_distance": 1500,
        "transit_replace_max_bike_duration": 420,
        "transit_replace_min_saving": 150,
        # "Direct" is only relative to the other bicycle variants: safety still
        # dominates, and the OTP street mode remains BICYCLE.
        "triangle": {"time": 0.35, "safety": 0.60, "flatness": 0.05},
        "transfer_penalty": 240,
        "bike_boarding_penalty": 120,
        "wait_factor": 0.35,
    },
    "balanced": {
        "name": "По велодорожкам",
        "speed_kmh": 11.0,
        "speed_mps": 11.0 / 3.6,
        "transit_replace_max_bike_distance": 1500,
        "transit_replace_max_bike_duration": 420,
        "transit_replace_min_saving": 150,
        "triangle": {"time": 0.13, "safety": 0.82, "flatness": 0.05},
        "transfer_penalty": 240,
        "bike_boarding_penalty": 120,
        "wait_factor": 0.35,
    },
    "calm": {
        "name": "Тихий маршрут",
        "speed_kmh": 11.0,
        "speed_mps": 11.0 / 3.6,
        "transit_replace_max_bike_distance": 1500,
        "transit_replace_max_bike_duration": 420,
        "transit_replace_min_saving": 150,
        "triangle": {"time": 0.05, "safety": 0.90, "flatness": 0.05},
        "transfer_penalty": 240,
        "bike_boarding_penalty": 120,
        "wait_factor": 0.35,
    },
}

DEFAULT_PROFILE = "balanced"

# Internal street-routing hypotheses.  They are generated together; this is no
# longer a user-selected journey style.
BICYCLE_ROUTE_VARIANTS = (
    {
        "key": "direct",
        "name": "Прямой веломаршрут",
        "otp_profile": "fast",
        "strategy": "bike_direct",
    },
    {
        "key": "cycleway",
        "name": "По велодорожкам",
        "otp_profile": "balanced",
        "strategy": "bike_cycleway",
    },
    {
        "key": "quiet",
        "name": "Тихий веломаршрут",
        "otp_profile": "calm",
        "strategy": "bike_quiet",
    },
)


@dataclass(frozen=True)
class RouteFocusConfig:
    """One coherent set of behavioural knobs for the route-focus axis.

    These values are deliberately expressed as distances, perceived-time
    multipliers and candidate budgets.  The planner can therefore use the same
    preference during generation, segment optimisation, scoring and selection
    instead of accumulating unrelated ``if route_focus == ...`` branches.
    """

    key: str
    name: str
    bike_cost_factor: float
    transit_cost_factor: float
    max_bike_access_m: float
    max_bike_egress_m: float
    access_target_m: float
    egress_target_m: float
    target_bike_share_shift: float
    share_penalty_seconds: float
    short_transit_penalty_factor: float
    transfer_penalty_factor: float
    feeder_protection: float
    trunk_access_bonus_factor: float
    min_transit_utility_seconds: float
    max_replacement_bike_seconds: int
    time_tolerance_ratio: float
    time_tolerance_seconds: int
    anchor_limit: int
    otp_candidates_per_query: int
    transit_skeleton_limit: int
    transfer_reduction: int
    generic_mode_families: tuple[str, ...]
    transit_departure_offsets_min: tuple[int, ...]
    transit_transfer_caps: tuple[int, ...]
    optimizer_focus_variants: tuple[int, ...]
    minimum_result_strategies: int


ROUTE_FOCUS_CONFIG: dict[int, RouteFocusConfig] = {
    -2: RouteFocusConfig(
        key="transit",
        name="Больше транспорта",
        bike_cost_factor=1.32,
        transit_cost_factor=0.76,
        max_bike_access_m=1_800,
        max_bike_egress_m=4_500,
        access_target_m=600,
        egress_target_m=1_300,
        target_bike_share_shift=-0.24,
        share_penalty_seconds=2_400,
        short_transit_penalty_factor=0.60,
        transfer_penalty_factor=0.72,
        feeder_protection=1.45,
        trunk_access_bonus_factor=1.45,
        min_transit_utility_seconds=-120,
        max_replacement_bike_seconds=9 * 60,
        time_tolerance_ratio=0.32,
        time_tolerance_seconds=12 * 60,
        anchor_limit=10,
        otp_candidates_per_query=24,
        transit_skeleton_limit=50,
        transfer_reduction=0,
        generic_mode_families=(
            "all",
            "bus",
            "tram",
            "rail",
            "bus_tram",
            "bus_rail",
            "tram_rail",
        ),
        transit_departure_offsets_min=(0, 8),
        transit_transfer_caps=(4, 2),
        optimizer_focus_variants=(-2, -1),
        minimum_result_strategies=6,
    ),
    -1: RouteFocusConfig(
        key="transit_lean",
        name="Скорее транспорт",
        bike_cost_factor=1.08,
        transit_cost_factor=0.94,
        max_bike_access_m=3_000,
        max_bike_egress_m=8_000,
        access_target_m=1_250,
        egress_target_m=2_800,
        target_bike_share_shift=-0.12,
        share_penalty_seconds=1_300,
        short_transit_penalty_factor=0.80,
        transfer_penalty_factor=0.86,
        feeder_protection=1.20,
        trunk_access_bonus_factor=1.22,
        min_transit_utility_seconds=-55,
        max_replacement_bike_seconds=12 * 60,
        time_tolerance_ratio=0.27,
        time_tolerance_seconds=12 * 60,
        anchor_limit=11,
        otp_candidates_per_query=22,
        transit_skeleton_limit=46,
        transfer_reduction=0,
        generic_mode_families=(
            "all",
            "bus",
            "tram",
            "rail",
            "bus_tram",
            "bus_rail",
            "tram_rail",
        ),
        transit_departure_offsets_min=(0, 10),
        transit_transfer_caps=(4, 2),
        optimizer_focus_variants=(-2, -1, 0),
        minimum_result_strategies=6,
    ),
    0: RouteFocusConfig(
        key="auto",
        name="Все стратегии",
        bike_cost_factor=1.0,
        transit_cost_factor=1.0,
        max_bike_access_m=8_000,
        max_bike_egress_m=16_000,
        access_target_m=2_800,
        egress_target_m=6_000,
        target_bike_share_shift=0.0,
        share_penalty_seconds=700,
        short_transit_penalty_factor=1.0,
        transfer_penalty_factor=1.0,
        feeder_protection=1.0,
        trunk_access_bonus_factor=1.0,
        min_transit_utility_seconds=20,
        max_replacement_bike_seconds=16 * 60,
        time_tolerance_ratio=0.55,
        time_tolerance_seconds=35 * 60,
        anchor_limit=24,
        otp_candidates_per_query=24,
        transit_skeleton_limit=64,
        transfer_reduction=0,
        generic_mode_families=(
            "all",
            "bus",
            "tram",
            "rail",
            "bus_tram",
            "bus_rail",
            "tram_rail",
        ),
        transit_departure_offsets_min=(0, 8, 16),
        transit_transfer_caps=(4, 2, 1, 0),
        optimizer_focus_variants=(-2, -1, 0, 1, 2),
        minimum_result_strategies=10,
    ),
    1: RouteFocusConfig(
        key="bike_lean",
        name="Больше велосипеда",
        bike_cost_factor=0.82,
        transit_cost_factor=1.12,
        max_bike_access_m=5_200,
        max_bike_egress_m=12_000,
        access_target_m=2_800,
        egress_target_m=6_000,
        target_bike_share_shift=0.22,
        share_penalty_seconds=1_700,
        short_transit_penalty_factor=1.35,
        transfer_penalty_factor=1.20,
        feeder_protection=0.90,
        trunk_access_bonus_factor=1.12,
        min_transit_utility_seconds=80,
        max_replacement_bike_seconds=22 * 60,
        time_tolerance_ratio=0.40,
        time_tolerance_seconds=23 * 60,
        anchor_limit=15,
        otp_candidates_per_query=18,
        transit_skeleton_limit=38,
        transfer_reduction=1,
        generic_mode_families=(
            "all",
            "bus",
            "tram",
            "rail",
            "bus_rail",
            "tram_rail",
        ),
        transit_departure_offsets_min=(0, 12),
        transit_transfer_caps=(4, 1, 0),
        optimizer_focus_variants=(0, 1, 2),
        minimum_result_strategies=6,
    ),
    2: RouteFocusConfig(
        key="ride",
        name="Велопрогулка",
        bike_cost_factor=0.62,
        transit_cost_factor=1.30,
        max_bike_access_m=8_000,
        max_bike_egress_m=16_000,
        access_target_m=4_800,
        egress_target_m=9_500,
        target_bike_share_shift=0.42,
        share_penalty_seconds=2_500,
        short_transit_penalty_factor=1.75,
        transfer_penalty_factor=1.42,
        feeder_protection=0.82,
        trunk_access_bonus_factor=1.20,
        min_transit_utility_seconds=200,
        max_replacement_bike_seconds=40 * 60,
        time_tolerance_ratio=0.58,
        time_tolerance_seconds=32 * 60,
        anchor_limit=18,
        otp_candidates_per_query=16,
        transit_skeleton_limit=46,
        transfer_reduction=1,
        generic_mode_families=(
            "all",
            "bus",
            "tram",
            "rail",
            "bus_rail",
            "tram_rail",
        ),
        transit_departure_offsets_min=(0, 12),
        transit_transfer_caps=(4, 1, 0),
        optimizer_focus_variants=(1, 2),
        minimum_result_strategies=6,
    ),
}


def parse_coordinate(obj: Any, name: str) -> tuple[float, float]:
    if not isinstance(obj, dict):
        raise ValueError(f"{name}: ожидается объект lat/lon.")
    try:
        lat = float(obj["lat"])
        lon = float(obj["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: некорректные координаты.") from exc
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError(f"{name}: координаты вне допустимого диапазона.")
    return lat, lon


def parse_departure(value: str | None) -> datetime:
    if not value:
        return datetime.now(MOSCOW_TZ).replace(second=0, microsecond=0)
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("Некорректная дата/время отправления.") from exc
    return dt.replace(tzinfo=MOSCOW_TZ) if dt.tzinfo is None else dt.astimezone(MOSCOW_TZ)


def parse_otp_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(MOSCOW_TZ)
    except ValueError:
        return None


def decode_polyline(encoded: str | None, precision: int = 5) -> list[list[float]]:
    """Decode Google encoded polyline to GeoJSON [lon, lat] coordinates."""
    if not encoded:
        return []

    coords: list[list[float]] = []
    index = 0
    lat = 0
    lon = 0
    factor = 10**precision

    while index < len(encoded):
        for is_lon in (False, True):
            result = 0
            shift = 0
            while True:
                if index >= len(encoded):
                    return coords
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append([lon / factor, lat / factor])

    return coords


def transit_modes(route: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for leg in route.get("legs") or []:
        if leg.get("transitLeg") and leg.get("mode") and leg["mode"] not in result:
            result.append(leg["mode"])
    return result


def transit_route_names(route: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for leg in route.get("legs") or []:
        if not leg.get("transitLeg"):
            continue
        r = leg.get("route") or {}
        name = r.get("shortName") or r.get("longName") or leg.get("mode")
        if name and name not in result:
            result.append(str(name))
    return result


def normalized_signature(route: dict[str, Any]) -> tuple:
    """Signature intentionally ignores bicycle street geometry.

    This makes two candidates using exactly the same transit chain part of the same
    diversity cluster even when OTP chose a slightly different bicycle approach.
    """
    transit = []
    for leg in route.get("legs") or []:
        if not leg.get("transitLeg"):
            continue
        r = leg.get("route") or {}
        transit.append(
            (
                leg.get("mode"),
                r.get("shortName") or r.get("longName"),
                (leg.get("from") or {}).get("name"),
                (leg.get("to") or {}).get("name"),
            )
        )
    if transit:
        return tuple(transit)
    return ("DIRECT_BIKE",)
