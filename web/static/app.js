const state = {
  origin: null,
  destination: null,
  profile: "balanced",
  routeFocus: 0,
  picking: "origin",
  routes: [],
  selectedRouteId: null,
  routeLayers: [],
  originMarker: null,
  destinationMarker: null,
};

const $ = (id) => document.getElementById(id);

const map = new maplibregl.Map({
  container: "map",
  center: [37.6173, 55.7558],
  zoom: 11.2,
  attributionControl: false,
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  },
});

map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");

function initMoscowTime() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Moscow",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date());

  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]));
  $("departure").value = `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

function coordsText(point) {
  return point ? `${point.lat.toFixed(6)}, ${point.lon.toFixed(6)}` : "Не выбрано";
}

function updateLocationUI(target) {
  $(`${target}-coords`).textContent = coordsText(state[target]);
}

function setPicking(target) {
  state.picking = target;
  document.querySelectorAll(".map-pick").forEach((button) => {
    button.classList.toggle("active", button.dataset.target === target);
  });
  $("map-hint").textContent =
    target === "origin"
      ? "Нажмите на карту, чтобы поставить старт"
      : "Нажмите на карту, чтобы поставить финиш";
}

function createMarker(target, point) {
  const existing = target === "origin" ? state.originMarker : state.destinationMarker;
  if (existing) existing.remove();

  const marker = new maplibregl.Marker({
    color: target === "origin" ? "#13a56f" : "#181a1f",
    draggable: true,
  })
    .setLngLat([point.lon, point.lat])
    .addTo(map);

  marker.on("dragend", () => {
    const pos = marker.getLngLat();
    state[target] = { lat: pos.lat, lon: pos.lng };
    const input = $(`${target}-search`);
    input.value = coordsText(state[target]);
    input.dataset.auto = "1";
    updateLocationUI(target);
  });

  if (target === "origin") state.originMarker = marker;
  else state.destinationMarker = marker;
}

function setPoint(target, point, label = null, fly = true) {
  state[target] = point;
  createMarker(target, point);

  const input = $(`${target}-search`);
  input.value = label || coordsText(point);
  input.dataset.auto = label ? "0" : "1";

  updateLocationUI(target);

  if (fly) {
    map.flyTo({
      center: [point.lon, point.lat],
      zoom: Math.max(map.getZoom(), 13),
      essential: true,
    });
  }

  if (target === "origin" && !state.destination) setPicking("destination");
  updateSummary();
}

map.on("click", (event) => {
  const target =
    state.picking ||
    (!state.origin ? "origin" : !state.destination ? "destination" : "destination");

  setPoint(
    target,
    { lat: event.lngLat.lat, lon: event.lngLat.lng },
    null,
    false
  );
});

document.querySelectorAll(".map-pick").forEach((button) => {
  button.addEventListener("click", () => setPicking(button.dataset.target));
});

const PROFILE_UI = {
  fast: { label: "Быстро", speed: 14 },
  balanced: { label: "Баланс", speed: 11 },
  calm: { label: "Спокойно", speed: 8.5 },
};

const ROUTE_FOCUS_UI = {
  "-2": {
    name: "Больше транспорта",
    short: "больше ОТ",
    description: "Стараться основную дистанцию проезжать на ОТ. Велосипед — преимущественно для подхода и последней части пути.",
  },
  "-1": {
    name: "Скорее транспорт",
    short: "скорее ОТ",
    description: "Небольшой приоритет общественного транспорта, но быстрые велосипедные связки сохраняются.",
  },
  "0": {
    name: "Баланс",
    short: "баланс",
    description: "Время остаётся главным фактором, но алгоритм старается сохранять разумную долю велосипеда.",
  },
  "1": {
    name: "Больше велосипеда",
    short: "больше вело",
    description: "Допускается умеренный проигрыш по времени ради более длинных и цельных велосипедных участков.",
  },
  "2": {
    name: "Велопрогулка",
    short: "велопрогулка",
    description: "Велосипед становится частью цели поездки. Алгоритм готов принять заметный, но ограниченный проигрыш по времени.",
  },
};

function updateProfileSummary() {
  const profile = PROFILE_UI[state.profile] || PROFILE_UI.balanced;
  const focus = ROUTE_FOCUS_UI[String(state.routeFocus)] || ROUTE_FOCUS_UI["0"];
  $("profile-summary").textContent =
    `${profile.label} · ~${profile.speed} км/ч · маршрут: ${focus.short}`;
}

function updateRouteFocusUI() {
  const focus = ROUTE_FOCUS_UI[String(state.routeFocus)] || ROUTE_FOCUS_UI["0"];
  $("route-focus-name").textContent = focus.name;
  $("route-focus-description").textContent = focus.description;
  updateProfileSummary();
}

document.querySelectorAll("#profile-selector button").forEach((button) => {
  button.addEventListener("click", () => {
    state.profile = button.dataset.profile;
    document.querySelectorAll("#profile-selector button").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    updateProfileSummary();
  });
});

$("route-focus").addEventListener("input", (event) => {
  state.routeFocus = Number(event.target.value);
  updateRouteFocusUI();
});

$("settings-button").addEventListener("click", () => {
  const panel = $("settings-panel");
  const opening = panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !opening);
  $("settings-button").classList.toggle("active", opening);
  $("settings-button").setAttribute("aria-expanded", String(opening));
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".settings-wrap")) {
    $("settings-panel").classList.add("hidden");
    $("settings-button").classList.remove("active");
    $("settings-button").setAttribute("aria-expanded", "false");
  }
});

document.querySelectorAll("[data-search]").forEach((button) => {
  button.addEventListener("click", () => searchPlace(button.dataset.search));
});

["origin", "destination"].forEach((target) => {
  $(`${target}-search`).addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      searchPlace(target);
    }
  });
});

async function searchPlace(target) {
  const input = $(`${target}-search`);
  const query = input.value.trim();
  const box = $(`${target}-results`);
  if (query.length < 3) return;

  box.classList.remove("hidden");
  box.innerHTML = `<div class="search-result">Ищу…</div>`;

  try {
    const response = await fetch(`/api/geocode?q=${encodeURIComponent(query)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Ошибка поиска");

    if (!data.results.length) {
      box.innerHTML = `<div class="search-result">Ничего не найдено</div>`;
      return;
    }

    box.innerHTML = "";
    data.results.forEach((result) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "search-result";
      button.textContent = result.name;
      button.addEventListener("click", () => {
        setPoint(target, { lat: result.lat, lon: result.lon }, result.name, true);
        box.classList.add("hidden");
      });
      box.appendChild(button);
    });
  } catch (error) {
    box.innerHTML = `<div class="search-result">${escapeHtml(error.message)}</div>`;
  }
}

document.addEventListener("click", (event) => {
  if (!event.target.closest(".location-field")) {
    document.querySelectorAll(".search-results").forEach((box) => box.classList.add("hidden"));
  }
});

$("route-button").addEventListener("click", calculateRoutes);
$("clear-button").addEventListener("click", clearAll);

function updateSummary() {
  $("results-summary").textContent =
    state.origin && state.destination ? "Готово к расчёту" : "Выберите две точки";
}

async function calculateRoutes() {
  hideError();

  if (!state.origin || !state.destination) {
    showError("Сначала выберите старт и финиш.");
    return;
  }

  setLoading(true);
  clearRouteLayers();
  state.routes = [];
  state.selectedRouteId = null;
  $("route-list").innerHTML = "";
  $("warnings").classList.add("hidden");

  try {
    const response = await fetch("/api/routes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin: state.origin,
        destination: state.destination,
        departureTime: $("departure").value,
        profile: state.profile,
        routeFocus: state.routeFocus,
        maxTransfers: Number($("max-transfers").value),
        deepSearch: true,
      }),
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Ошибка маршрутизации.");

    state.routes = data.routes || [];
    renderWarnings(data.warnings || []);
    renderRoutes();

    if (state.routes.length) {
      selectRoute(state.routes[0].id);
      const stats = data.stats || {};
      const candidates = stats.candidatesTotal ?? state.routes.length;
      const boardingAnchors = stats.boardingAnchorCandidates ?? 0;
      const egressAnchors = stats.egressAnchorCandidates ?? stats.anchorCandidates ?? 0;
      const optimized = stats.transitOptimizedCandidates ?? 0;
      const pareto = stats.paretoCandidates ?? candidates;
      const transferFiltered = stats.transferFiltered ?? 0;
      const focusName = stats.routeFocusName || "Баланс";
      const elapsed = stats.elapsedMs ? ` · ${(stats.elapsedMs / 1000).toFixed(1)} с` : "";
      const transferInfo = transferFiltered ? ` · отсечено по пересадкам ${transferFiltered}` : "";
      $("results-summary").textContent =
        `${state.routes.length} из ${candidates} · ${focusName} · Pareto ${pareto} · ОТ→вело ${optimized} · вход ${boardingAnchors} / выход ${egressAnchors}${transferInfo}${elapsed}`;
      if (stats.deepSearchError) {
        showDeepSearchWarning(stats.deepSearchError);
      }
    } else {
      $("results-summary").textContent = "Маршруты не найдены";
      $("route-list").innerHTML =
        `<div class="error-message">OTP не вернул подходящих маршрутов.</div>`;
    }
  } catch (error) {
    showError(error.message);
    $("results-summary").textContent = "Ошибка";
  } finally {
    setLoading(false);
  }
}

function renderWarnings(warnings) {
  const ignored = new Set([
    "WALKING_BETTER_THAN_TRANSIT",
    "NO_STOPS_IN_RANGE",
    "NO_TRANSIT_CONNECTION",
    "NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW",
  ]);
  const meaningful = warnings.filter(
    (item) => item && item.code && !ignored.has(item.code)
  );

  if (!meaningful.length) {
    $("warnings").classList.add("hidden");
    return;
  }

  $("warnings").innerHTML = meaningful
    .map((item) => `<div><strong>${escapeHtml(item.code)}</strong>: ${escapeHtml(item.description || "")}</div>`)
    .join("");
  $("warnings").classList.remove("hidden");
}

function showDeepSearchWarning(message) {
  const box = $("warnings");
  const previous = box.classList.contains("hidden") ? "" : box.innerHTML;
  box.innerHTML = `${previous}<div><strong>Deep search:</strong> ${escapeHtml(message)}</div>`;
  box.classList.remove("hidden");
}

function renderRoutes() {
  const list = $("route-list");
  list.innerHTML = "";

  state.routes.forEach((route, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "route-card";
    card.dataset.routeId = route.id;

    const label = route.recommendation ||
      (route.kind === "bike" ? "Только велосипед" : "Смешанный маршрут");

    const waitBadge =
      route.initialWait > 60
        ? `<span class="badge wait">выезд через ${formatDuration(route.initialWait)}</span>`
        : "";

    const transitNames = route.legs
      .filter((leg) => leg.transitLeg)
      .map((leg) => (leg.route && (leg.route.shortName || leg.route.longName)) || modeName(leg.mode))
      .filter(Boolean);

    const transitBadge = transitNames.length
      ? `<span class="badge">${escapeHtml(transitNames.join(" · "))}</span>`
      : `<span class="badge">🚲 ${(route.bikeDistance / 1000).toFixed(1)} км</span>`;

    let anchorBadge = "";
    if (route.anchor?.type === "boarding") {
      const km = (Number(route.anchor.bikeAccessDistance) || 0) / 1000;
      anchorBadge = `<span class="badge anchor">🚲 ${km.toFixed(1)} км → посадка: ${escapeHtml(route.anchor.name)}</span>`;
    } else if (route.anchor) {
      const km = (Number(route.anchor.bikeEgressDistance) || 0) / 1000;
      anchorBadge = `<span class="badge anchor">выход: ${escapeHtml(route.anchor.name)} → 🚲 ${km.toFixed(1)} км</span>`;
    }

    const explanations = Array.isArray(route.explanations)
      ? route.explanations.slice(0, 3)
      : [];
    const explanationHtml = explanations.length
      ? `<div class="route-explanations">${explanations
          .map((text) => `<span class="explanation-chip">${escapeHtml(text)}</span>`)
          .join("")}</div>`
      : "";

    const optimization = route.optimization || null;
    const optimizationBadge = optimization && (optimization.replacedWalkLegs || optimization.replacedTransitCount)
      ? `<span class="badge optimized">🚲 заменено: ${optimization.replacedWalkLegs || 0} пеш. + ${optimization.replacedTransitCount || 0} ОТ</span>`
      : "";

    const bikeShare = Math.round((Number(route.bikeShare) || 0) * 100);
    const shareBadge = route.kind === "mixed"
      ? `<span class="badge">🚲 ${bikeShare}% пути</span>`
      : "";

    card.innerHTML = `
      <div class="route-top">
        <div>
          <div class="route-time">${formatDuration(route.doorToDoor)}</div>
          <div class="route-label">${label} · в пути ${formatDuration(route.duration)}</div>
        </div>
        <div class="route-arrival">до ${formatClock(route.end)}</div>
      </div>

      <div class="route-badges">
        ${waitBadge}
        ${transitBadge}
        ${anchorBadge}
        ${optimizationBadge}
        ${shareBadge}
        ${route.transfers ? `<span class="badge">${route.transfers} перес.</span>` : ""}
      </div>

      ${explanationHtml}
      <div class="legs">${route.legs.map(renderLeg).join("")}</div>
    `;

    card.addEventListener("click", () => selectRoute(route.id));
    list.appendChild(card);
  });
}

function renderLeg(leg) {
  const routeName =
    leg.route && (leg.route.shortName || leg.route.longName)
      ? ` ${escapeHtml(leg.route.shortName || leg.route.longName)}`
      : "";

  const distance =
    leg.distance >= 1000
      ? `${(leg.distance / 1000).toFixed(1)} км`
      : `${Math.round(leg.distance)} м`;

  return `
    <div class="leg">
      <span class="leg-color" style="background:${modeColor(leg.mode)}"></span>
      <span class="leg-main">
        <strong>${modeName(leg.mode)}${routeName}</strong> · ${distance}
      </span>
      <span class="leg-duration">${formatDuration(leg.duration)}</span>
    </div>
  `;
}

function selectRoute(routeId) {
  const route = state.routes.find((item) => item.id === routeId);
  if (!route) return;

  state.selectedRouteId = routeId;
  document.querySelectorAll(".route-card").forEach((card) => {
    card.classList.toggle("active", card.dataset.routeId === routeId);
  });
  drawRoute(route);
}

function clearRouteLayers() {
  for (const item of state.routeLayers) {
    if (map.getLayer(item.layerId)) map.removeLayer(item.layerId);
    if (map.getSource(item.sourceId)) map.removeSource(item.sourceId);
  }
  state.routeLayers = [];
}

function drawRoute(route) {
  clearRouteLayers();
  const bounds = new maplibregl.LngLatBounds();

  route.legs.forEach((leg, index) => {
    const coordinates = leg.geometry && leg.geometry.coordinates;
    if (!coordinates || coordinates.length < 2) return;

    coordinates.forEach((coord) => bounds.extend(coord));
    const sourceId = `route-source-${index}`;
    const layerId = `route-layer-${index}`;

    map.addSource(sourceId, {
      type: "geojson",
      data: {
        type: "Feature",
        properties: { mode: leg.mode },
        geometry: { type: "LineString", coordinates },
      },
    });

    map.addLayer({
      id: layerId,
      type: "line",
      source: sourceId,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": modeColor(leg.mode),
        "line-width": leg.transitLeg ? 6 : 5,
        "line-opacity": 0.92,
      },
    });

    state.routeLayers.push({ sourceId, layerId });
  });

  if (!bounds.isEmpty()) {
    map.fitBounds(bounds, {
      padding: { top: 70, right: 70, bottom: 70, left: 70 },
      maxZoom: 15,
      duration: 650,
    });
  }
}

function clearAll() {
  state.routes = [];
  state.selectedRouteId = null;
  state.origin = null;
  state.destination = null;

  if (state.originMarker) state.originMarker.remove();
  if (state.destinationMarker) state.destinationMarker.remove();
  state.originMarker = null;
  state.destinationMarker = null;

  ["origin", "destination"].forEach((target) => {
    $(`${target}-search`).value = "";
    $(`${target}-coords`).textContent = "Не выбрано";
    $(`${target}-results`).classList.add("hidden");
  });

  $("route-list").innerHTML = "";
  $("warnings").classList.add("hidden");
  clearRouteLayers();
  hideError();
  setPicking("origin");
  updateSummary();

  map.flyTo({ center: [37.6173, 55.7558], zoom: 11.2, essential: true });
}

function setLoading(enabled) {
  $("loading").classList.toggle("hidden", !enabled);
  $("route-button").disabled = enabled;
  $("route-button").textContent = enabled ? "Считаю…" : "Построить маршрут";
}

function showError(message) {
  $("planner-error").textContent = message;
  $("planner-error").classList.remove("hidden");
}

function hideError() {
  $("planner-error").classList.add("hidden");
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours} ч ${minutes} мин`;
  if (minutes) return `${minutes} мин`;
  return `${total} с`;
}

function formatClock(value) {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    hour: "2-digit",
    minute: "2-digit",
  }).format(dt);
}

function modeName(mode) {
  return {
    BICYCLE: "Велосипед",
    WALK: "Пешком",
    BUS: "Автобус",
    TRAM: "Трамвай",
    TROLLEYBUS: "Троллейбус",
    RAIL: "Поезд",
    SUBWAY: "Метро",
  }[mode] || mode || "Участок";
}

function modeColor(mode) {
  return {
    BICYCLE: "#13a56f",
    WALK: "#858b95",
    BUS: "#3277e3",
    TRAM: "#e35b55",
    TROLLEYBUS: "#3277e3",
    RAIL: "#8c5bd7",
    SUBWAY: "#d34141",
  }[mode] || "#5b626c";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function checkOtp() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    $("otp-status").classList.toggle("ok", Boolean(data.ok));
    $("otp-status").classList.toggle("bad", !data.ok);
    $("otp-status").title = data.ok ? "OTP доступен" : "OTP недоступен";
  } catch {
    $("otp-status").classList.add("bad");
    $("otp-status").title = "OTP недоступен";
  }
}

initMoscowTime();
updateRouteFocusUI();
setPicking("origin");
updateSummary();
checkOtp();
