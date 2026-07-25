#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
OTP_URL = os.environ.get("OTP_URL", "http://localhost:8080/otp/gtfs/v1")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_USER_AGENT = os.environ.get("NOMINATIM_USER_AGENT", "MixedNavigatorPrototype/0.1")

PROFILE_CONFIG = {
    "fast": {
        "triangle": {"time": 0.75, "safety": 0.20, "flatness": 0.05},
        "transfer_penalty": 150,
        "bike_boarding_penalty": 90,
    },
    "balanced": {
        "triangle": {"time": 0.50, "safety": 0.40, "flatness": 0.10},
        "transfer_penalty": 240,
        "bike_boarding_penalty": 120,
    },
    "calm": {
        "triangle": {"time": 0.30, "safety": 0.60, "flatness": 0.10},
        "transfer_penalty": 300,
        "bike_boarding_penalty": 150,
    },
}

PLAN_QUERY = """
query Plan(
  $origin: PlanLabeledLocationInput!
  $destination: PlanLabeledLocationInput!
  $dateTime: PlanDateTimeInput!
  $modes: PlanModesInput!
  $preferences: PlanPreferencesInput
  $first: Int!
) {
  planConnection(
    origin: $origin
    destination: $destination
    dateTime: $dateTime
    modes: $modes
    preferences: $preferences
    itineraryFilter: { itineraryFilterDebugProfile: LIST_ALL }
    first: $first
  ) {
    searchDateTime
    routingErrors { code description inputField }
    edges {
      node {
        duration
        start
        end
        generalizedCost
        numberOfTransfers
        waitingTime
        walkDistance
        legs {
          mode
          transitLeg
          duration
          distance
          startTime
          endTime
          realTime
          from { name lat lon }
          to { name lat lon }
          route { shortName longName mode }
          legGeometry { length points }
        }
      }
    }
  }
}
"""


class LruCache:
    def __init__(self, max_size: int = 256) -> None:
        self.max_size = max_size
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)


_geocode_cache = LruCache()
_nominatim_lock = threading.Lock()
_last_nominatim_request = 0.0


def post_json(url: str, payload: dict[str, Any], timeout: int = 190) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OTPTimeout": "180000",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OTP HTTP {exc.code}: {details[:1500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Не удалось подключиться к OTP: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OTP вернул не JSON: {raw[:1500]}") from exc


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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


def otp_location(lat: float, lon: float, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "location": {
            "coordinate": {
                "latitude": lat,
                "longitude": lon,
            }
        },
    }


def decode_polyline(encoded: str | None, precision: int = 5) -> list[list[float]]:
    if not encoded:
        return []
    coords: list[list[float]] = []
    index = 0
    lat = 0
    lon = 0
    factor = 10 ** precision

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


def parse_otp_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(MOSCOW_TZ)
    except ValueError:
        return None


def route_signature(itinerary: dict[str, Any]) -> tuple:
    return tuple(
        (
            leg.get("mode"),
            (leg.get("route") or {}).get("shortName") or (leg.get("route") or {}).get("longName"),
            (leg.get("from") or {}).get("name"),
            (leg.get("to") or {}).get("name"),
        )
        for leg in (itinerary.get("legs") or [])
    )


def normalize_route(
    itinerary: dict[str, Any],
    requested_departure: datetime,
    profile_key: str,
    route_index: int,
) -> dict[str, Any]:
    profile = PROFILE_CONFIG[profile_key]
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
        transit_leg = bool(leg.get("transitLeg"))

        if mode == "BICYCLE":
            bike_distance += distance
        if transit_leg:
            transit_distance += distance
            bike_boardings += 1

        geometry = decode_polyline((leg.get("legGeometry") or {}).get("points"))
        from_obj = leg.get("from") or {}
        to_obj = leg.get("to") or {}

        if len(geometry) < 2:
            try:
                geometry = [
                    [float(from_obj["lon"]), float(from_obj["lat"])],
                    [float(to_obj["lon"]), float(to_obj["lat"])],
                ]
            except (KeyError, TypeError, ValueError):
                geometry = []

        route_obj = leg.get("route") or {}

        legs_out.append({
            "mode": mode,
            "transitLeg": transit_leg,
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
            } if route_obj else None,
            "geometry": {"type": "LineString", "coordinates": geometry},
        })

    has_transit = any(leg["transitLeg"] for leg in legs_out)
    has_bicycle = any(leg["mode"] == "BICYCLE" for leg in legs_out)
    door_to_door = initial_wait + duration

    score = (
        door_to_door
        + waiting_time * 0.35
        + transfers * profile["transfer_penalty"]
        + bike_boardings * profile["bike_boarding_penalty"]
        + (walk_distance / 1000.0) * 180
    )

    return {
        "id": f"route-{route_index}",
        "kind": "mixed" if has_transit else "bike" if has_bicycle else "other",
        "duration": duration,
        "initialWait": initial_wait,
        "doorToDoor": door_to_door,
        "start": start_dt.isoformat() if start_dt else itinerary.get("start"),
        "end": end_dt.isoformat() if end_dt else itinerary.get("end"),
        "generalizedCost": itinerary.get("generalizedCost"),
        "score": round(score),
        "transfers": transfers,
        "waitingTime": waiting_time,
        "walkDistance": round(walk_distance, 1),
        "bikeDistance": round(bike_distance, 1),
        "transitDistance": round(transit_distance, 1),
        "legs": legs_out,
    }


def plan_routes(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    origin_lat, origin_lon = parse_coordinate(payload.get("origin"), "Старт")
    dest_lat, dest_lon = parse_coordinate(payload.get("destination"), "Финиш")
    departure = parse_departure(payload.get("departureTime"))

    profile_key = payload.get("profile", "balanced")
    if profile_key not in PROFILE_CONFIG:
        profile_key = "balanced"

    try:
        max_transfers = int(payload.get("maxTransfers", 2))
    except (TypeError, ValueError):
        max_transfers = 2
    max_transfers = min(5, max(0, max_transfers))
    cfg = PROFILE_CONFIG[profile_key]

    variables = {
        "origin": otp_location(origin_lat, origin_lon, "Старт"),
        "destination": otp_location(dest_lat, dest_lon, "Финиш"),
        "dateTime": {"earliestDeparture": departure.isoformat(timespec="seconds")},
        "first": 18,
        "modes": {
            "direct": ["BICYCLE"],
            "transit": {
                "access": ["BICYCLE"],
                "egress": ["BICYCLE"],
                "transfer": ["BICYCLE"],
                "transit": [
                    {"mode": "BUS"},
                    {"mode": "TRAM"},
                    {"mode": "TROLLEYBUS"},
                    {"mode": "RAIL"},
                ],
            },
        },
        "preferences": {
            "street": {
                "bicycle": {
                    "optimization": {
                        "triangle": cfg["triangle"],
                    }
                }
            },
            "transit": {
                "transfer": {
                    "maximumTransfers": max_transfers,
                }
            },
        },
    }

    result = post_json(OTP_URL, {
        "query": PLAN_QUERY,
        "operationName": "Plan",
        "variables": variables,
    })

    if result.get("errors"):
        raise RuntimeError("\n".join(e.get("message", "GraphQL error") for e in result["errors"]))

    connection = (result.get("data") or {}).get("planConnection")
    if connection is None:
        raise RuntimeError("OTP не вернул planConnection.")

    warnings = connection.get("routingErrors") or []
    nodes = [edge.get("node") for edge in (connection.get("edges") or []) if edge.get("node")]

    normalized = []
    seen = set()
    for index, node in enumerate(nodes):
        signature = route_signature(node)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        route = normalize_route(node, departure, profile_key, index)
        if route["kind"] != "other":
            normalized.append(route)

    normalized.sort(key=lambda r: (r["score"], r["doorToDoor"], r["duration"]))
    return normalized[:8], warnings


def nominatim_search(query: str) -> list[dict[str, Any]]:
    global _last_nominatim_request

    key = query.strip().lower()
    cached = _geocode_cache.get(key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 0,
        "countrycodes": "ru",
        "accept-language": "ru",
    })

    with _nominatim_lock:
        elapsed = time.monotonic() - _last_nominatim_request
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)

        data = get_json(
            f"{NOMINATIM_URL}?{params}",
            headers={
                "User-Agent": NOMINATIM_USER_AGENT,
                "Accept": "application/json",
            },
        )
        _last_nominatim_request = time.monotonic()

    results = [
        {
            "name": item.get("display_name"),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "type": item.get("type"),
        }
        for item in data
        if item.get("lat") and item.get("lon")
    ]
    _geocode_cache.put(key, results)
    return results


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    try:
        result = post_json(
            OTP_URL,
            {"query": "query { feeds { feedId } }", "variables": {}},
            timeout=10,
        )
        if result.get("errors"):
            raise RuntimeError(result["errors"][0].get("message", "OTP error"))
        return jsonify({"ok": True, "otp": OTP_URL})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "otp": OTP_URL}), 503


@app.post("/api/routes")
def routes():
    try:
        body = request.get_json(force=True, silent=False) or {}
        result, warnings = plan_routes(body)
        return jsonify({"routes": result, "warnings": warnings, "otpUrl": OTP_URL})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/geocode")
def geocode():
    query = (request.args.get("q") or "").strip()
    if len(query) < 3:
        return jsonify({"error": "Введите хотя бы 3 символа."}), 400
    try:
        return jsonify({"results": nominatim_search(query)})
    except Exception as exc:
        return jsonify({"error": f"Ошибка геокодинга: {exc}"}), 502


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
        threaded=True,
    )
