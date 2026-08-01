# Hybrid Strategy Router v0.6.0

## Цель

Маршрутизатор должен выбирать **стратегию поездки**, а не несколько почти одинаковых
вариантов одной стратегии. Велосипед является полноценным режимом доступа, пересадки
и выхода: он может пропустить слабый локальный автобус и довезти пользователя к более
сильной линии общественного транспорта.

OpenTripPlanner остаётся schedule oracle: он отвечает за расписательно корректные
рейсы и street routing. Python backend создаёт гипотезы, оптимизирует их и выбирает
непохожие варианты.

## 1. GTFS Line Metrics

`routing/gtfs_index.py` один раз индексирует `stops.txt`, `routes.txt`, `trips.txt` и
`stop_times.txt`.

Для каждой линии считаются:

- `trip_count`;
- `median_headway_s`;
- приблизительная `commercial_speed_kmh`;
- `bikes_allowed_ratio`;
- `trunk_score`.

`trunk_score` объединяет частоту, скорость, плотность рейсов и mode bonus. Это
эвристическая характеристика, а не официальная классификация московской сети.
Она нужна, чтобы отличать сильный транспортный backbone от слабого local micro-leg.

## 2. Candidate generation

Одновременно формируются несколько классов кандидатов:

1. direct bicycle baseline;
2. transit-first skeletons: ALL, BUS, TRAM, RAIL и их комбинации;
3. boarding-anchor routes;
4. egress-anchor routes.

Raw `BICYCLE + TRANSIT + BICYCLE` результат OTP используется как fallback, когда
собственные pipeline не нашли смешанных вариантов.

## 3. Boarding anchors

Поиск точки посадки рассматривает остановки не только по расстоянию от старта.
Учитываются:

- удалённость от старта и цели;
- направление относительно OD-коридора;
- положение вдоль поездки;
- количество доступных маршрутов/modes;
- `best_trunk_score` остановки;
- пространственное разнообразие anchors.

Для каждого выбранного anchor строится:

```text
origin --BICYCLE--> anchor --TRANSIT(+BICYCLE egress)--> destination
```

Это позволяет получить сценарий «проехать 1–3 км на велосипеде и сесть на более
прямой/частый маршрут», которого не было в старой односторонней egress-логике.

## 4. Segment optimizer

Каждый transit skeleton проверяется по legs.

WALK может быть заменён велосипедом. Transit leg сравнивается с велосипедом только,
если он потенциально слабый: короткий, медленный, с заметным ожиданием, большим
headway или низким `trunk_score`.

Для transit-leg оценивается:

```text
effective transit = wait_before + in_vehicle + boarding/alighting overhead
saving = effective transit - bicycle_duration
```

Замена требует одновременно достаточного количества признаков слабого leg,
приемлемой длины велосипедной альтернативы и материального выигрыша времени.

BUS/TROLLEYBUS заменяются наиболее охотно, TRAM — осторожнее, RAIL — наиболее
консервативно. Для сильной trunk-line и для feeder-leg к downstream trunk требуемый
выигрыш увеличивается.

## 5. Catchability

После всех замен времена оставшегося транспорта **не сдвигаются**. Backend заново
строит абсолютный timeline. Если велосипедная замена приводит пользователя к следующей
посадке позже фиксированного времени рейса, кандидат отбрасывается.

## 6. Bike legality

Если `trips.txt` явно содержит `bikes_allowed=2` для конкретного используемого trip,
маршрут отбрасывается до ранжирования. `1` разрешает велосипед. Пустое/0 пока означает
«неизвестно» и не блокирует маршрут, поскольку московский prototype feed ещё требует
отдельного Bike Policy Compiler.

## 7. Scoring и bike share

Базовый score содержит:

- door-to-door time;
- waiting cost;
- transfer penalty;
- bicycle boarding penalty;
- walking penalty;
- penalty за слабые micro-transit legs;
- bonus за полезный trunk transit.

Целевая доля велосипеда зависит от длины прямого веломаршрута:

| Прямая велодистанция | Базовая доля велосипеда |
|---|---:|
| ≤ 4 км | 0.90 |
| ≤ 8 км | 0.70 |
| ≤ 15 км | 0.45 |
| ≤ 25 км | 0.22 |
| > 25 км | 0.10 |

`routeFocus` затем сдвигает цель: `-0.20 / -0.10 / 0 / +0.15 / +0.30`.
Это soft preference, а не жёсткая квота.

## 8. Pareto pruning

До diversity selector отсеиваются явно доминируемые варианты по трём измерениям:

- `doorToDoor`;
- `transfers`;
- `discomfort`.

Небольшие epsilon допускают секундные различия без размножения клонов. Direct bicycle
baseline защищён как отдельная пользовательская стратегия.

## 9. Similarity и diversity

Для каждого кандидата строятся corridor signatures:

- транспортная геометрия в ~350–400-метровых ячейках;
- chain используемых линий;
- велосипедная геометрия;
- transfer stops.

Итоговое сходство:

```text
0.60 * transit similarity
+ 0.25 * bicycle corridor overlap
+ 0.15 * transfer-stop overlap
```

Кандидаты с overlap около 0.80+ объединяются в один кластер. После этого selector
пытается сохранить archetypes: fastest, best score, boarding anchor, egress anchor,
rail, minimum transfers и direct bike. Оставшиеся места заполняются MMR-подобным
ранжированием `score + similarity_penalty`.

## 10. Что показывает UI

Карточка маршрута получает primary label и до трёх объяснений, например:

- «Велоподъезд 2.1 км к более сильной точке посадки»;
- «Пропущено слабых коротких участков ОТ: 1»;
- «Используется сильная линия m72»;
- «Без пересадок между маршрутами ОТ».

Debug summary показывает число boarding/egress anchors, кандидатов до Pareto,
Pareto-набор и число оптимизированных transport skeletons.

## Проверка

```bash
python -m unittest discover -s tests -v
python -m py_compile routing/*.py app.py
node --check static/app.js
```

Полный end-to-end тест требует запущенного московского OTP и реального GTFS/OSM graph.
