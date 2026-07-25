from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .models import PROFILE_CONFIG


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
    routingErrors { code description inputField }
    edges {
      node {
        duration start end generalizedCost numberOfTransfers waitingTime walkDistance
        legs {
          mode transitLeg duration distance startTime endTime realTime
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


class OTPClient:
    def __init__(self, endpoint: str, timeout: int = 190) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def post_json(self, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "OTPTimeout": "180000",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OTP HTTP {exc.code}: {details[:1500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Не удалось подключиться к OTP: {exc.reason}") from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OTP вернул не JSON: {raw[:1500]}") from exc

        if result.get("errors"):
            messages = [e.get("message", "GraphQL error") for e in result["errors"]]
            raise RuntimeError("\n".join(messages))
        return result

    def health(self) -> bool:
        result = self.post_json(
            {"query": "query { feeds { feedId } }", "variables": {}}, timeout=10
        )
        return not bool(result.get("errors"))

    def plan(
        self,
        *,
        origin: dict[str, Any],
        destination: dict[str, Any],
        departure: datetime,
        profile: str,
        transit_modes: list[str] | None = None,
        max_transfers: int = 2,
        direct_bike: bool = False,
        transit_only: bool = False,
        direct_only: bool = False,
        first: int = 8,
        egress_mode: str = "BICYCLE",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cfg = PROFILE_CONFIG[profile]

        modes: dict[str, Any] = {}
        if direct_bike or direct_only:
            modes["direct"] = ["BICYCLE"]
        if direct_only:
            modes["directOnly"] = True
        elif transit_modes:
            modes["transit"] = {
                "access": ["BICYCLE"],
                "egress": [egress_mode],
                "transfer": ["BICYCLE"],
                "transit": [{"mode": mode} for mode in transit_modes],
            }
            if transit_only:
                modes["transitOnly"] = True

        variables = {
            "origin": origin,
            "destination": destination,
            "dateTime": {"earliestDeparture": departure.isoformat(timespec="seconds")},
            "modes": modes,
            "preferences": {
                "street": {
                    "bicycle": {"optimization": {"triangle": cfg["triangle"]}}
                },
                "transit": {"transfer": {"maximumTransfers": max_transfers}},
            },
            "first": first,
        }

        result = self.post_json(
            {"query": PLAN_QUERY, "operationName": "Plan", "variables": variables}
        )
        connection = (result.get("data") or {}).get("planConnection")
        if connection is None:
            raise RuntimeError("OTP не вернул planConnection.")

        nodes = [
            edge.get("node")
            for edge in (connection.get("edges") or [])
            if edge.get("node")
        ]
        return nodes, connection.get("routingErrors") or []


def coordinate_location(lat: float, lon: float, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "location": {"coordinate": {"latitude": lat, "longitude": lon}},
    }


def stop_location(stop_gtfs_id: str, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "location": {"stopLocation": {"stopLocationId": stop_gtfs_id}},
    }
