from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

PROFILE_CONFIG = {
    "fast": {
        "name": "Быстро",
        "speed_kmh": 14.0,
        "speed_mps": 14.0 / 3.6,
        "transit_replace_max_bike_distance": 2000,
        "transit_replace_max_bike_duration": 480,
        "transit_replace_min_saving": 120,
        "triangle": {"time": 0.75, "safety": 0.20, "flatness": 0.05},
        "transfer_penalty": 150,
        "bike_boarding_penalty": 75,
        "wait_factor": 0.25,
    },
    "balanced": {
        "name": "Баланс",
        "speed_kmh": 11.0,
        "speed_mps": 11.0 / 3.6,
        "transit_replace_max_bike_distance": 1500,
        "transit_replace_max_bike_duration": 420,
        "transit_replace_min_saving": 150,
        "triangle": {"time": 0.50, "safety": 0.40, "flatness": 0.10},
        "transfer_penalty": 240,
        "bike_boarding_penalty": 120,
        "wait_factor": 0.35,
    },
    "calm": {
        "name": "Спокойно",
        "speed_kmh": 8.5,
        "speed_mps": 8.5 / 3.6,
        "transit_replace_max_bike_distance": 1200,
        "transit_replace_max_bike_duration": 360,
        "transit_replace_min_saving": 180,
        "triangle": {"time": 0.30, "safety": 0.60, "flatness": 0.10},
        "transfer_penalty": 300,
        "bike_boarding_penalty": 150,
        "wait_factor": 0.40,
    },
}


ROUTE_FOCUS_CONFIG = {
    -2: {
        "key": "transit",
        "name": "Больше транспорта",
        "bike_share_shift": -0.32,
        "share_penalty_seconds": 1500,
        "transfer_penalty_factor": 0.70,
        "time_tolerance_ratio": 0.30,
        "anchor_limit": 9,
    },
    -1: {
        "key": "transit_lean",
        "name": "Скорее транспорт",
        "bike_share_shift": -0.16,
        "share_penalty_seconds": 1200,
        "transfer_penalty_factor": 0.85,
        "time_tolerance_ratio": 0.25,
        "anchor_limit": 10,
    },
    0: {
        "key": "balanced",
        "name": "Баланс",
        "bike_share_shift": 0.0,
        "share_penalty_seconds": 900,
        "transfer_penalty_factor": 1.0,
        "time_tolerance_ratio": 0.25,
        "anchor_limit": 12,
    },
    1: {
        "key": "bike_lean",
        "name": "Больше велосипеда",
        "bike_share_shift": 0.20,
        "share_penalty_seconds": 1200,
        "transfer_penalty_factor": 1.15,
        "time_tolerance_ratio": 0.38,
        "anchor_limit": 14,
    },
    2: {
        "key": "ride",
        "name": "Велопрогулка",
        "bike_share_shift": 0.42,
        "share_penalty_seconds": 1500,
        "transfer_penalty_factor": 1.30,
        "time_tolerance_ratio": 0.55,
        "anchor_limit": 16,
    },
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
