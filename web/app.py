#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any

from flask import Flask, jsonify, render_template, request

from routing.planner import RoutePlanner

app = Flask(__name__)

OTP_URL = os.environ.get("OTP_URL", "http://localhost:8080/otp/gtfs/v1")
GTFS_PATH = os.environ.get("GTFS_PATH", "/otp-data/moscow-gtfs.zip")
OTP_FEED_ID = os.environ.get("OTP_FEED_ID", "1")
NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
)
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT", "MixedNavigatorPrototype/0.2"
)

planner = RoutePlanner(
    otp_url=OTP_URL,
    gtfs_path=GTFS_PATH,
    feed_id=OTP_FEED_ID,
)


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


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def nominatim_search(query: str) -> list[dict[str, Any]]:
    global _last_nominatim_request

    key = query.strip().lower()
    cached = _geocode_cache.get(key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 0,
            "countrycodes": "ru",
            "accept-language": "ru",
        }
    )

    with _nominatim_lock:
        elapsed = time.monotonic() - _last_nominatim_request
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)

        data = get_json(
            f"{NOMINATIM_URL}?{params}",
            headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept": "application/json"},
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
        status = planner.health()
        return jsonify({"ok": bool(status["otp"]), "otp": OTP_URL, **status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "otp": OTP_URL}), 503


@app.post("/api/routes")
def routes():
    try:
        body = request.get_json(force=True, silent=False) or {}
        result = planner.plan(body)
        return jsonify({**result, "otpUrl": OTP_URL})
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
