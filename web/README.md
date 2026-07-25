# Смешанный навигатор — Web prototype v0.2

Web UI + собственный слой генерации мультимодальных кандидатов поверх OpenTripPlanner.

## Что нового в v0.2

Маршрутизатор больше не полагается на один ответ OTP.

### Multi-query Candidate Generator

Один пользовательский запрос параллельно разворачивается в несколько поисков:

- выбранный профиль + все виды транспорта;
- fast + все виды транспорта;
- calm + все виды транспорта;
- BUS/TROLLEYBUS only;
- TRAM only;
- RAIL only;
- без пересадок;
- максимум одна пересадка.

Результаты объединяются и дедуплицируются.

### Egress Anchor Search

Backend читает `moscow-gtfs.zip` и строит в памяти индекс остановок. Для длинных поездок он выбирает до 8 перспективных точек выхода в 1–9 км от цели и проверяет отдельную стратегию:

```text
origin → bicycle/transit → anchor stop → bicycle → destination
```

Это позволяет находить варианты вроде «долго на автобусе к центру, затем несколько километров по велодорожкам», которые обычный top-N OTP может не показать.

### Diversity Selector

Финальный список — не просто top-N по одному score. Есть квоты на:

- самый быстрый;
- лучший по score;
- лучший egress-anchor;
- автобус + велосипед;
- трамвай + велосипед;
- поезд + велосипед;
- прямой велосипед;
- остальные непохожие варианты.

Одинаковые transit chains группируются, поэтому один и тот же трамвай не должен заполнять весь список.

### Новый penalty

Короткие бессмысленные посадки на транспорт штрафуются. Например:

```text
🚲 5.4 км → 🚌 0.3 км → 🚲 1.3 км
```

получает дополнительный penalty.

## Docker Compose

Web-контейнеру теперь нужен read-only доступ к тому же GTFS, который использует OTP.

Рекомендуемая структура:

```text
otp-moscow/
├── compose.yaml
├── data/
│   ├── graph.obj
│   ├── moscow.osm.pbf
│   └── moscow-gtfs.zip
└── web/
    ├── app.py
    ├── routing/
    ├── Dockerfile
    ├── requirements.txt
    ├── templates/
    └── static/
```

Добавьте/замените сервис `web`:

```yaml
  web:
    build: ./web
    container_name: mixed-navigator-web
    ports:
      - "5000:5000"
    environment:
      OTP_URL: "http://otp:8080/otp/gtfs/v1"
      GTFS_PATH: "/otp-data/moscow-gtfs.zip"
      OTP_FEED_ID: "1"
      NOMINATIM_USER_AGENT: "MixedNavigatorPrototype/0.2"
    volumes:
      - ./data:/otp-data:ro
    depends_on:
      - otp
    restart: unless-stopped
```

Пересборка:

```bash
docker compose up -d --build web
```

Проверка:

```bash
curl -s http://localhost:5000/api/health | jq
```

Ожидается примерно:

```json
{
  "ok": true,
  "gtfsIndex": {
    "loaded": true,
    "stopCount": 9902,
    "error": null
  }
}
```

## API

`POST /api/routes` дополнительно возвращает `stats`:

```json
{
  "genericQueries": 8,
  "genericCandidates": 24,
  "anchorsConsidered": 8,
  "anchorCandidates": 5,
  "candidatesTotal": 29,
  "returned": 8,
  "elapsedMs": 1820
}
```

У anchor-route появляется:

```json
{
  "strategy": "egress_anchor",
  "anchor": {
    "name": "...",
    "bikeEgressDistance": 4200
  }
}
```

## Ограничения текущего deep search

- Реализован только egress-anchor. Boarding anchors будут следующим этапом.
- Anchor generator использует GTFS topology и географическую эвристику, но ещё не знает качество велоинфраструктуры около остановки.
- OSM Bike Stress enrichment ещё не реализован.
- GTFS остаётся статическим, realtime Москвы не подключён.
