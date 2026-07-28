const state = {
  origin: null,
  destination: null,
  picking: null,
  routes: [],
  selectedRouteId: null,
  selectedLegIndex: -1,
  routeLayers: [],
  transferMarkers: [],
  originMarker: null,
  destinationMarker: null,
  routeHistory: new Map(),
  plannedDeparture: null,
  loadingMessageTimer: null,
  carouselScrollTimer: null,
  carouselPointerMoved: false,
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
  document.body.classList.toggle("map-picking", Boolean(target));
  if (target && isMobileLayout()) {
    document.body.classList.remove("mobile-planner-open");
  }
  document.querySelectorAll(".map-pick").forEach((button) => {
    button.classList.toggle("active", button.dataset.target === target);
  });
  $("map-hint").classList.toggle("hidden", !target);
  map.getCanvas().style.cursor = target ? "crosshair" : "";
  if (target) {
    $("map-hint").textContent =
      target === "origin"
        ? "Нажмите на карту, чтобы поставить старт"
        : "Нажмите на карту, чтобы поставить финиш";
  }
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

  updateSummary();
}

map.on("click", (event) => {
  const target = state.picking;
  if (!target) return;

  setPoint(
    target,
    { lat: event.lngLat.lat, lon: event.lngLat.lng },
    null,
    false
  );
  setPicking(null);
  if (isMobileLayout()) {
    document.body.classList.add("mobile-planner-open");
  }
});

document.querySelectorAll(".map-pick").forEach((button) => {
  button.addEventListener("click", () => {
    setPicking(state.picking === button.dataset.target ? null : button.dataset.target);
  });
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
$("mobile-open-planner").addEventListener("click", () => {
  document.body.classList.add("mobile-planner-open");
});
$("detail-overview").addEventListener("click", showRouteOverview);
$("detail-close").addEventListener("click", closeRouteDetail);
$("route-edit-button").addEventListener("click", openRouteEditor);
$("route-edit-cancel").addEventListener("click", closeRouteEditor);
$("route-edit-apply").addEventListener("click", applyBicycleReplacement);
$("route-edit-undo").addEventListener("click", undoBicycleReplacement);
$("route-edit-start").addEventListener("change", syncRouteEditRange);
$("route-edit-end").addEventListener("change", syncRouteEditRange);
$("mobile-edit-search").addEventListener("click", () => {
  closeRouteDetail();
  document.body.classList.add("mobile-planner-open");
});
$("mobile-close-search").addEventListener("click", () => {
  document.body.classList.remove("mobile-planner-open");
});
$("detail-leg-prev").addEventListener("click", () => {
  const route = getSelectedRoute();
  if (!route || state.selectedLegIndex <= 0) return;
  focusRouteLeg(state.selectedLegIndex - 1);
});
$("detail-leg-next").addEventListener("click", () => {
  const route = getSelectedRoute();
  if (!route || state.selectedLegIndex >= route.legs.length - 1) return;
  focusRouteLeg(state.selectedLegIndex + 1);
});

function updateSummary() {
  $("route-button").disabled = !(state.origin && state.destination);
}

async function calculateRoutes() {
  hideError();

  if (!state.origin || !state.destination) {
    showError("Сначала выберите старт и финиш.");
    return;
  }

  setLoading(true);
  const plannedDeparture = $("departure").value;
  clearRouteLayers();
  state.routes = [];
  state.selectedRouteId = null;
  state.selectedLegIndex = -1;
  state.routeHistory.clear();
  document.body.classList.remove("has-routes", "detail-open");
  $("route-list").innerHTML = "";
  $("warnings").classList.add("hidden");
  $("route-detail-panel").classList.add("hidden");

  try {
    const response = await fetch("/api/routes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin: state.origin,
        destination: state.destination,
        departureTime: plannedDeparture,
        deepSearch: true,
      }),
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Ошибка маршрутизации.");

    state.routes = data.routes || [];
    state.plannedDeparture = plannedDeparture;
    renderWarnings(data.warnings || []);
    renderRoutes();

    if (state.routes.length) {
      document.body.classList.add("has-routes");
      document.body.classList.remove("mobile-planner-open");
      selectRoute(state.routes[0].id, { showDetails: !isMobileLayout() });
      const stats = data.stats || {};
      if (stats.deepSearchError) {
        showDeepSearchWarning(stats.deepSearchError);
      }
    } else {
      showError("Не удалось найти подходящие маршруты. Попробуйте изменить точки.");
      if (isMobileLayout()) {
        document.body.classList.add("mobile-planner-open");
      }
    }
  } catch (error) {
    showError(error.message);
    if (isMobileLayout()) {
      document.body.classList.add("mobile-planner-open");
    }
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

  state.routes.forEach((route) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "route-card";
    card.dataset.routeId = route.id;

    const transitNames = transitChain(route);
    const bikeDistance = Number(route.bikeDistance || 0);
    const transitDistance = route.legs
      .filter((leg) => leg.transitLeg)
      .reduce((total, leg) => total + Number(leg.distance || 0), 0);
    const transitModes = route.legs
      .filter((leg) => leg.transitLeg)
      .map((leg) => leg.mode)
      .filter((mode, index, values) => index === 0 || mode !== values[index - 1])
      .map(modeIcon)
      .join("");
    const tags = compactRouteTags(route)
      .map((tag) => `<span class="compact-route-tag">${escapeHtml(tag)}</span>`)
      .join("");

    card.innerHTML = `
      <div class="compact-route-top">
        <strong>${formatDuration(route.doorToDoor)}</strong>
        <span>до ${formatClock(route.end)}</span>
      </div>
      <div class="compact-route-metrics">
        <span class="bike-metric">🚲 ${formatDistance(bikeDistance)}</span>
        ${
          transitDistance > 0
            ? `<span class="transit-metric">${transitModes || "🚌"} ${formatDistance(transitDistance)}</span>`
            : ""
        }
      </div>
      <div class="compact-route-footer">
        <span class="compact-route-lines">${
          transitNames.length
            ? escapeHtml(transitNames.join(" · "))
            : "целиком на велосипеде"
        }</span>
        <span class="compact-route-tags">${tags}</span>
      </div>
    `;

    card.addEventListener("click", () => {
      if (state.carouselPointerMoved) {
        state.carouselPointerMoved = false;
        return;
      }
      if (isMobileLayout()) centerRouteCard(card);
      selectRoute(route.id);
    });
    list.appendChild(card);
  });
}

function centerRouteCard(card, behavior = "smooth") {
  card.scrollIntoView({
    behavior,
    block: "nearest",
    inline: "center",
  });
}

function selectCenteredCarouselRoute() {
  if (!isMobileLayout() || document.body.classList.contains("detail-open")) return;
  const list = $("route-list");
  const cards = Array.from(list.querySelectorAll(".route-card"));
  if (!cards.length) return;
  const listRect = list.getBoundingClientRect();
  const center = listRect.left + listRect.width / 2;
  const nearest = cards.reduce((best, card) => {
    const rect = card.getBoundingClientRect();
    const distance = Math.abs(rect.left + rect.width / 2 - center);
    return !best || distance < best.distance ? { card, distance } : best;
  }, null);
  const routeId = nearest?.card?.dataset.routeId;
  if (routeId && routeId !== state.selectedRouteId) {
    selectRoute(routeId, { showDetails: false });
  }
}

function openCenteredRouteDetail() {
  if (!state.selectedRouteId) return;
  selectRoute(state.selectedRouteId);
}

function setupCarouselInteractions() {
  const list = $("route-list");
  const shelf = document.querySelector(".results-section");
  let cardGesture = null;
  let shelfGesture = null;

  list.addEventListener("scroll", () => {
    if (!isMobileLayout()) return;
    window.clearTimeout(state.carouselScrollTimer);
    state.carouselScrollTimer = window.setTimeout(
      selectCenteredCarouselRoute,
      90
    );
  }, { passive: true });

  list.addEventListener("pointerdown", (event) => {
    cardGesture = { x: event.clientX, y: event.clientY };
    state.carouselPointerMoved = false;
  });
  list.addEventListener("pointermove", (event) => {
    if (!cardGesture) return;
    if (
      Math.abs(event.clientX - cardGesture.x) > 9 ||
      Math.abs(event.clientY - cardGesture.y) > 9
    ) {
      state.carouselPointerMoved = true;
    }
  });
  list.addEventListener("pointerup", () => {
    window.setTimeout(() => {
      state.carouselPointerMoved = false;
    }, 0);
    cardGesture = null;
  });
  list.addEventListener("pointercancel", () => {
    cardGesture = null;
    state.carouselPointerMoved = false;
  });

  shelf.addEventListener("pointerdown", (event) => {
    if (!isMobileLayout()) return;
    shelfGesture = { x: event.clientX, y: event.clientY };
  });
  window.addEventListener("pointermove", (event) => {
    if (!shelfGesture || !isMobileLayout()) return;
    const dx = event.clientX - shelfGesture.x;
    const dy = event.clientY - shelfGesture.y;
    if (dy < 0 && Math.abs(dy) > Math.abs(dx)) {
      shelf.style.setProperty("--sheet-lift", `${Math.max(-20, dy * 0.14)}px`);
    }
  });
  window.addEventListener("pointerup", (event) => {
    if (!shelfGesture || !isMobileLayout()) return;
    const dx = event.clientX - shelfGesture.x;
    const dy = event.clientY - shelfGesture.y;
    shelf.style.removeProperty("--sheet-lift");
    shelfGesture = null;
    if (dy < -52 && Math.abs(dy) > Math.abs(dx) * 1.15) {
      openCenteredRouteDetail();
    }
  });
  window.addEventListener("pointercancel", () => {
    shelfGesture = null;
    shelf.style.removeProperty("--sheet-lift");
  });
}

function compactRouteTags(route) {
  if (Array.isArray(route.manualEdits) && route.manualEdits.length) {
    return ["изменён вручную"];
  }
  if (route.kind === "bike") return ["без транспорта"];
  if (route.streetPreference === "cycleway") return ["по велодорожкам"];
  return [];
}

function formatDistance(meters) {
  const distance = Math.max(0, Number(meters) || 0);
  if (distance >= 1000) {
    return `${(distance / 1000).toFixed(distance >= 10_000 ? 0 : 1)} км`;
  }
  return `${Math.round(distance)} м`;
}

function transitChain(route) {
  const names = [];
  route.legs
    .filter((leg) => leg.transitLeg)
    .forEach((leg) => {
      const name =
        (leg.route && (leg.route.shortName || leg.route.longName)) ||
        modeName(leg.mode);
      if (name && names[names.length - 1] !== name) names.push(name);
    });
  return names;
}

function selectRoute(routeId, { showDetails = true } = {}) {
  const route = state.routes.find((item) => item.id === routeId);
  if (!route) return;

  state.selectedRouteId = routeId;
  document.querySelectorAll(".route-card").forEach((card) => {
    card.classList.toggle("active", card.dataset.routeId === routeId);
  });
  if (showDetails) {
    renderRouteDetail(route);
  } else {
    closeRouteDetail({ keepOverview: false });
  }
  drawRoute(route);
}

function renderRouteDetail(route) {
  state.selectedLegIndex = -1;
  $("route-detail-panel").classList.remove("hidden");
  document.body.classList.add("detail-open");
  closeRouteEditor({ restoreOverview: false });
  const detailChain = transitChain(route);
  $("detail-label").textContent =
    route.kind === "bike"
      ? "Велосипедный маршрут"
      : `Велосипед + ${detailChain.join(" + ") || "транспорт"}`;
  $("detail-duration").textContent = formatDuration(route.doorToDoor);
  $("detail-arrival").textContent = `до ${formatClock(route.end)}`;

  const bikeShare = Math.round((Number(route.bikeShare) || 0) * 100);
  const chain = transitChain(route);
  const badges = [
    `🚲 ${(Number(route.bikeDistance || 0) / 1000).toFixed(1)} км`,
    route.kind === "mixed" ? `велосипед ${bikeShare}% пути` : "только велосипед",
    chain.length ? chain.join(" · ") : null,
    route.transfers ? formatTransitionCount(route.transfers) : "без смен участков",
    route.initialWait > 60 ? `выезд через ${formatDuration(route.initialWait)}` : null,
  ].filter(Boolean);
  $("detail-badges").innerHTML = badges
    .map((text) => `<span>${escapeHtml(text)}</span>`)
    .join("");

  const explanations = Array.isArray(route.explanations)
    ? route.explanations.slice(0, 3)
    : [];
  $("detail-explanations").innerHTML = explanations
    .map((text) => `<span>${escapeHtml(text)}</span>`)
    .join("");
  $("detail-explanations").classList.toggle("hidden", !explanations.length);

  $("detail-leg-steps").innerHTML = route.legs
    .map((leg, index) => {
      const routeName =
        leg.route && (leg.route.shortName || leg.route.longName)
          ? ` ${leg.route.shortName || leg.route.longName}`
          : "";
      return `
        <button class="detail-leg-step" data-leg-index="${index}" type="button">
          <i style="background:${modeColor(leg.mode)}"></i>
          <span>${modeIcon(leg.mode)} ${escapeHtml(modeName(leg.mode) + routeName)}</span>
          <small>${formatDuration(leg.duration)}</small>
        </button>
      `;
    })
    .join("");
  document.querySelectorAll(".detail-leg-step").forEach((button) => {
    button.addEventListener("click", () => focusRouteLeg(Number(button.dataset.legIndex)));
  });
  const history = state.routeHistory.get(route.id) || [];
  $("route-edit-undo").classList.toggle("hidden", !history.length);
  updateDetailLeg(route, -1);
}

function closeRouteDetail({ keepOverview = true } = {}) {
  $("route-detail-panel").classList.add("hidden");
  document.body.classList.remove("detail-open");
  closeRouteEditor({ restoreOverview: false });
  if (keepOverview && getSelectedRoute()) showRouteOverview();
}

function openRouteEditor() {
  const route = getSelectedRoute();
  if (!route || !route.legs.length) return;

  const preferredLeg =
    state.selectedLegIndex >= 0
      ? state.selectedLegIndex
      : Math.max(0, route.legs.findIndex((leg) => leg.transitLeg));
  const start = Math.min(preferredLeg, route.legs.length - 1);
  const end = start + 1;
  populateBoundarySelects(route, start, end);
  $("route-edit-error").classList.add("hidden");
  $("route-edit-panel").classList.remove("hidden");
  $("route-edit-button").classList.add("active");
  syncRouteEditRange();
  $("route-edit-panel").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function closeRouteEditor({ restoreOverview = true } = {}) {
  $("route-edit-panel").classList.add("hidden");
  $("route-edit-button").classList.remove("active");
  $("route-edit-error").classList.add("hidden");
  if (restoreOverview && getSelectedRoute()) showRouteOverview();
}

function populateBoundarySelects(route, selectedStart, selectedEnd) {
  const options = [];
  for (let index = 0; index <= route.legs.length; index += 1) {
    options.push(
      `<option value="${index}">${escapeHtml(routeBoundaryLabel(route, index))}</option>`
    );
  }
  $("route-edit-start").innerHTML = options.slice(0, -1).join("");
  $("route-edit-end").innerHTML = options.slice(1).join("");
  $("route-edit-start").value = String(selectedStart);
  $("route-edit-end").value = String(selectedEnd);
}

function routeBoundaryLabel(route, index) {
  if (index === 0) {
    return `Старт · ${route.legs[0]?.from?.name || "начало маршрута"}`;
  }
  if (index === route.legs.length) {
    return `Финиш · ${route.legs.at(-1)?.to?.name || "конец маршрута"}`;
  }
  const name =
    route.legs[index - 1]?.to?.name ||
    route.legs[index]?.from?.name ||
    `точка ${index}`;
  return `${index}. ${name}`;
}

function syncRouteEditRange() {
  const route = getSelectedRoute();
  if (!route) return;
  const startSelect = $("route-edit-start");
  const endSelect = $("route-edit-end");
  let start = Number(startSelect.value);
  let end = Number(endSelect.value);
  if (start >= end) {
    end = Math.min(route.legs.length, start + 1);
    endSelect.value = String(end);
  }
  Array.from(startSelect.options).forEach((option) => {
    option.disabled = Number(option.value) >= end;
  });
  Array.from(endSelect.options).forEach((option) => {
    option.disabled = Number(option.value) <= start;
  });
  setRouteRangeEmphasis(start, end);
  fitRouteLegRange(route, start, end);
}

function setRouteRangeEmphasis(startBoundary, endBoundary) {
  state.routeLayers.forEach((item) => {
    if (!map.getLayer(item.layerId)) return;
    const selected =
      item.legIndex >= startBoundary && item.legIndex < endBoundary;
    map.setPaintProperty(item.layerId, "line-opacity", selected ? 1 : 0.14);
    map.setPaintProperty(
      item.layerId,
      "line-width",
      selected ? item.baseWidth + 2 : 3
    );
  });
  state.transferMarkers.forEach((marker) => {
    marker.getElement().style.opacity = "0.28";
  });
}

function fitRouteLegRange(route, startBoundary, endBoundary) {
  const bounds = new maplibregl.LngLatBounds();
  route.legs.slice(startBoundary, endBoundary).forEach((leg) => {
    const coordinates = leg.geometry && leg.geometry.coordinates;
    (coordinates || []).forEach((coordinate) => bounds.extend(coordinate));
  });
  if (!bounds.isEmpty()) fitMapBounds(bounds, 16);
}

async function applyBicycleReplacement() {
  const route = getSelectedRoute();
  if (!route) return;
  const startBoundary = Number($("route-edit-start").value);
  const endBoundary = Number($("route-edit-end").value);
  const button = $("route-edit-apply");
  const errorBox = $("route-edit-error");
  errorBox.classList.add("hidden");
  button.disabled = true;
  button.textContent = "Строю веломаршрут…";

  try {
    const response = await fetch("/api/routes/replace-with-bicycle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        route,
        startBoundary,
        endBoundary,
        departureTime: state.plannedDeparture || $("departure").value,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Не удалось заменить часть маршрута.");
    }

    const history = state.routeHistory.get(route.id) || [];
    history.push(cloneRoute(route));
    state.routeHistory.set(route.id, history);
    const edited = data.route;
    edited.id = route.id;
    const routeIndex = state.routes.findIndex((item) => item.id === route.id);
    state.routes[routeIndex] = edited;
    renderRoutes();
    selectRoute(edited.id);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = "Пересчитать на велосипеде";
  }
}

function undoBicycleReplacement() {
  const route = getSelectedRoute();
  if (!route) return;
  const history = state.routeHistory.get(route.id) || [];
  const previous = history.pop();
  if (!previous) return;
  state.routeHistory.set(route.id, history);
  const routeIndex = state.routes.findIndex((item) => item.id === route.id);
  state.routes[routeIndex] = previous;
  renderRoutes();
  selectRoute(previous.id);
}

function cloneRoute(route) {
  if (typeof structuredClone === "function") return structuredClone(route);
  return JSON.parse(JSON.stringify(route));
}

function clearRouteLayers() {
  state.transferMarkers.forEach((marker) => marker.remove());
  state.transferMarkers = [];
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

    state.routeLayers.push({
      sourceId,
      layerId,
      legIndex: index,
      baseWidth: leg.transitLeg ? 6 : 5,
    });
  });

  drawTransferMarkers(route);

  if (!bounds.isEmpty()) {
    fitMapBounds(bounds, 15);
  }
}

function getSelectedRoute() {
  return state.routes.find((route) => route.id === state.selectedRouteId) || null;
}

function showRouteOverview() {
  const route = getSelectedRoute();
  if (!route) return;
  state.selectedLegIndex = -1;
  updateDetailLeg(route, -1);
  setRouteLayerEmphasis(-1);

  const bounds = new maplibregl.LngLatBounds();
  route.legs.forEach((leg) => {
    const coordinates = leg.geometry && leg.geometry.coordinates;
    (coordinates || []).forEach((coordinate) => bounds.extend(coordinate));
  });
  if (!bounds.isEmpty()) fitMapBounds(bounds, 15);
}

function focusRouteLeg(index) {
  const route = getSelectedRoute();
  if (!route || index < 0 || index >= route.legs.length) return;
  const leg = route.legs[index];
  state.selectedLegIndex = index;
  updateDetailLeg(route, index);
  setRouteLayerEmphasis(index);

  const coordinates = leg.geometry && leg.geometry.coordinates;
  if (!coordinates || coordinates.length < 2) return;
  const bounds = new maplibregl.LngLatBounds();
  coordinates.forEach((coordinate) => bounds.extend(coordinate));
  if (!bounds.isEmpty()) fitMapBounds(bounds, 16);
}

function updateDetailLeg(route, index) {
  const overview = index < 0;
  $("detail-overview").classList.toggle("active", overview);
  $("detail-leg-prev").disabled = overview || index === 0;
  $("detail-leg-next").disabled =
    !route.legs.length || index >= route.legs.length - 1;
  document.querySelectorAll(".detail-leg-step").forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.legIndex) === index);
  });

  if (overview) {
    $("detail-leg-position").textContent = formatLegCount(route.legs.length);
    $("detail-leg-title").textContent = "Весь маршрут";
    $("detail-leg-subtitle").textContent =
      "Нажмите на участок или используйте стрелку вправо";
    return;
  }

  const leg = route.legs[index];
  const routeName =
    leg.route && (leg.route.shortName || leg.route.longName)
      ? ` ${leg.route.shortName || leg.route.longName}`
      : "";
  const distance =
    Number(leg.distance || 0) >= 1000
      ? `${(Number(leg.distance) / 1000).toFixed(1)} км`
      : `${Math.round(Number(leg.distance || 0))} м`;
  const from = leg.from && leg.from.name ? leg.from.name : "начало участка";
  const to = leg.to && leg.to.name ? leg.to.name : "конец участка";
  $("detail-leg-position").textContent =
    `Участок ${index + 1} из ${route.legs.length}`;
  $("detail-leg-title").textContent = `${modeName(leg.mode)}${routeName}`;
  $("detail-leg-subtitle").textContent =
    `${distance} · ${formatDuration(leg.duration)} · ${from} → ${to}`;
}

function setRouteLayerEmphasis(activeIndex) {
  state.routeLayers.forEach((item) => {
    if (!map.getLayer(item.layerId)) return;
    const active = activeIndex < 0 || item.legIndex === activeIndex;
    map.setPaintProperty(item.layerId, "line-opacity", active ? 0.96 : 0.18);
    map.setPaintProperty(
      item.layerId,
      "line-width",
      activeIndex < 0 ? item.baseWidth : active ? item.baseWidth + 2 : 3
    );
  });
  state.transferMarkers.forEach((marker) => {
    marker.getElement().style.opacity = activeIndex < 0 ? "1" : "0.32";
  });
}

function fitMapBounds(bounds, maxZoom) {
  const container = map.getContainer();
  const wideMap = container.clientWidth >= 760;
  const mobile = isMobileLayout();
  const detailPanel = $("route-detail-panel");
  const detailHeight =
    mobile && !detailPanel.classList.contains("hidden")
      ? Math.min(detailPanel.offsetHeight + 28, window.innerHeight * 0.68)
      : 0;
  const detailWidth =
    !mobile && !detailPanel.classList.contains("hidden")
      ? detailPanel.offsetWidth
      : 0;
  const resultShelf = document.querySelector(".results-section");
  const resultHeight =
    mobile && document.body.classList.contains("has-routes")
      ? Math.min(resultShelf?.offsetHeight || 0, window.innerHeight * 0.36)
      : 0;
  map.fitBounds(bounds, {
    padding: mobile
      ? {
          top: 76,
          right: 34,
          bottom: Math.max(56, detailHeight || resultHeight) + 18,
          left: 34,
        }
      : {
          top: 75,
          right: wideMap ? Math.max(70, detailWidth + 42) : 70,
          bottom: 75,
          left: 70,
        },
    maxZoom,
    duration: 600,
  });
}

function drawTransferMarkers(route) {
  const points = Array.isArray(route.transferPoints) ? route.transferPoints : [];
  points.forEach((point, index) => {
    if (point.lat == null || point.lon == null) return;
    const lat = Number(point.lat);
    const lon = Number(point.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;

    const element = document.createElement("button");
    element.type = "button";
    element.className = "route-transfer-marker";
    element.textContent = String(point.index || index + 1);

    const from = point.fromRoute || modeName(point.fromMode);
    const to = point.toRoute || modeName(point.toMode);
    const place = point.name ? ` · ${point.name}` : "";
    const description = `${from || "Участок"} → ${to || "следующий участок"}${place}`;
    element.setAttribute("aria-label", `Пересадка ${index + 1}: ${description}`);

    const popup = new maplibregl.Popup({ offset: 14, closeButton: false })
      .setText(description);
    const marker = new maplibregl.Marker({ element, anchor: "center" })
      .setLngLat([lon, lat])
      .setPopup(popup)
      .addTo(map);
    state.transferMarkers.push(marker);
  });
}

function clearAll() {
  state.routes = [];
  state.selectedRouteId = null;
  state.selectedLegIndex = -1;
  state.routeHistory.clear();
  state.plannedDeparture = null;
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
  $("route-detail-panel").classList.add("hidden");
  clearRouteLayers();
  hideError();
  setPicking(null);
  document.body.classList.remove("has-routes", "detail-open", "mobile-planner-open");
  updateSummary();

  map.flyTo({ center: [37.6173, 55.7558], zoom: 11.2, essential: true });
}

const ROUTING_LOADING_MESSAGES = [
  "Сравниваем велосипед и транспорт",
  "Ищем сильные линии впереди",
  "Проверяем пересадки и расписание",
  "Убираем похожие варианты",
];

function setLoading(enabled) {
  $("loading").classList.toggle("hidden", !enabled);
  document.body.classList.toggle("routing-loading", enabled);
  if (enabled && isMobileLayout()) {
    document.body.classList.remove("mobile-planner-open");
    $("route-detail-panel").classList.add("hidden");
    document.body.classList.remove("detail-open");
  }
  window.clearInterval(state.loadingMessageTimer);
  state.loadingMessageTimer = null;
  if (enabled) {
    let messageIndex = 0;
    $("loading-message").textContent = ROUTING_LOADING_MESSAGES[messageIndex];
    state.loadingMessageTimer = window.setInterval(() => {
      messageIndex = (messageIndex + 1) % ROUTING_LOADING_MESSAGES.length;
      $("loading-message").animate(
        [
          { opacity: 0, transform: "translateY(3px)" },
          { opacity: 1, transform: "translateY(0)" },
        ],
        { duration: 260, easing: "ease-out" }
      );
      $("loading-message").textContent = ROUTING_LOADING_MESSAGES[messageIndex];
    }, 1250);
  }
  $("route-button").disabled = enabled || !(state.origin && state.destination);
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

function formatLegCount(value) {
  return formatRussianCount(value, ["участок", "участка", "участков"]);
}

function formatTransitionCount(value) {
  return formatRussianCount(value, ["смена участка", "смены участков", "смен участков"]);
}

function formatRussianCount(value, forms) {
  const count = Math.max(0, Math.round(Number(value) || 0));
  const mod100 = count % 100;
  const mod10 = count % 10;
  const form =
    mod100 >= 11 && mod100 <= 14
      ? forms[2]
      : mod10 === 1
        ? forms[0]
        : mod10 >= 2 && mod10 <= 4
          ? forms[1]
          : forms[2];
  return `${count} ${form}`;
}

function isMobileLayout() {
  return window.matchMedia("(max-width: 820px)").matches;
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

function modeIcon(mode) {
  return {
    BICYCLE: "🚲",
    WALK: "🚶",
    BUS: "🚌",
    TRAM: "🚋",
    TROLLEYBUS: "🚎",
    RAIL: "🚆",
    SUBWAY: "Ⓜ",
  }[mode] || "•";
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
setPicking(null);
updateSummary();
setupCarouselInteractions();
checkOtp();
