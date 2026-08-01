from __future__ import annotations

import threading
from collections import Counter
from typing import Any


class RoutingDiagnostics:
    """Thread-safe, bounded diagnostics for one planning request.

    Aggregate counters are always available for the normal API stats.  Detailed
    candidate events are retained only when ``debugRouting`` is requested, so
    production requests neither print nor accumulate a large trace.
    """

    def __init__(self, enabled: bool = False, max_events: int = 240) -> None:
        self.enabled = enabled
        self.max_events = max_events
        self.generated: Counter[str] = Counter()
        self.rejected: Counter[str] = Counter()
        self.strategy_counts: Counter[str] = Counter()
        self.clustered = 0
        self.selected = 0
        self.pareto_before = 0
        self.pareto_after = 0
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def generated_candidates(
        self,
        family: str,
        count: int,
        routes: list[dict[str, Any]] | None = None,
    ) -> None:
        if count <= 0:
            return
        with self._lock:
            self.generated[family] += count
        if routes:
            for route in routes:
                self.event("candidate_generated", route, family=family)

    def reject(self, reason: str, route: dict[str, Any] | None = None, **details: Any) -> None:
        with self._lock:
            self.rejected[reason] += 1
        self.event("candidate_rejected", route, reason=reason, **details)

    def event(
        self,
        event: str,
        route: dict[str, Any] | None = None,
        **details: Any,
    ) -> None:
        if not self.enabled:
            return
        item: dict[str, Any] = {"event": event, **details}
        if route is not None:
            item["candidate"] = self._route_ref(route)
        with self._lock:
            if len(self._events) < self.max_events:
                self._events.append(item)

    def count_strategies(self, routes: list[dict[str, Any]]) -> None:
        counts: Counter[str] = Counter()
        for route in routes:
            for archetype in route.get("archetypes") or ["UNCLASSIFIED"]:
                counts[str(archetype)] += 1
        with self._lock:
            self.strategy_counts = counts

    def stats(self) -> dict[str, Any]:
        return {
            "generated": dict(sorted(self.generated.items())),
            "rejected": dict(sorted(self.rejected.items())),
            "pareto": {"before": self.pareto_before, "after": self.pareto_after},
            "strategies": dict(sorted(self.strategy_counts.items())),
            "clustered": self.clustered,
            "returned": self.selected,
        }

    def trace(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    @staticmethod
    def _route_ref(route: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": route.get("kind"),
            "strategy": route.get("strategy"),
            "archetypes": route.get("archetypes") or [],
            "sourceQuery": route.get("sourceQuery"),
            "doorToDoor": route.get("doorToDoor"),
            "bikeShare": route.get("bikeShare"),
            "transitRoutes": route.get("transitRoutes") or [],
        }
