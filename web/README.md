# Смешанный навигатор — Web prototype

Web UI для локального OpenTripPlanner.

## Уже работает

- карта MapLibre;
- старт и финиш кликом по карте;
- перетаскиваемые маркеры;
- поиск адреса по кнопке/Enter;
- велосипед + BUS/TRAM/TROLLEYBUS/RAIL + велосипед;
- direct bicycle;
- метро исключено;
- цветная геометрия каждого участка из `legGeometry`;
- несколько вариантов;
- честное `initialWait`: общее время считается от момента, когда пользователь готов выехать;
- backend-прокси к OTP.

## Вариант 1: запустить Python рядом с OTP

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Открыть:

```text
http://localhost:5000
```

По умолчанию backend обращается к:

```text
http://localhost:8080/otp/gtfs/v1
```

## Вариант 2: добавить в существующий Docker Compose

Рекомендуемая структура:

```text
otp-moscow/
├── compose.yaml
├── data/
└── web/
    ├── app.py
    ├── Dockerfile
    ├── requirements.txt
    ├── templates/
    │   └── index.html
    └── static/
        ├── app.js
        └── style.css
```

В `compose.yaml` добавить:

```yaml
  web:
    build: ./web
    container_name: mixed-navigator-web
    ports:
      - "5000:5000"
    environment:
      OTP_URL: "http://otp:8080/otp/gtfs/v1"
      NOMINATIM_USER_AGENT: "MixedNavigatorPrototype/0.1"
    depends_on:
      - otp
    restart: unless-stopped
```

Запуск:

```bash
docker compose up -d --build
docker compose logs -f web
```

## Геокодинг

Публичный Nominatim используется только по явному действию пользователя:
кнопка поиска или Enter. Autocomplete отсутствует. Backend кэширует результаты
и выдерживает минимум 1.05 секунды между запросами.

Для production лучше заменить его на собственный geocoder или отдельного провайдера.

## Переменные окружения

- `OTP_URL`
- `NOMINATIM_URL`
- `NOMINATIM_USER_AGENT`
- `PORT`

## Ограничения прототипа

- realtime транспорта пока нет;
- корректность bike-on-transit зависит от подготовленного GTFS;
- нет elevation/DEM;
- road-stress пока остаётся на уровне базовой bicycle-модели OTP;
- нет аккаунтов, истории и сохранённых мест.
