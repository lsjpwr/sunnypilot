# 내비 경로 기반 감속·과속카메라 대응 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 폰 내비 앱이 안내를 시작할 때 목적지를 UDP로 넘기면, 디바이스가 스스로 경로탐색 API를 호출해 폴리라인을 얻고, 그 경로로 (1) 과속카메라·방지턱의 옆 도로 오검출을 없애고 (2) 전방 커브를 미리 감속한다.

**Architecture:** 경로 API에서 **폴리라인만** 뽑는다. 제한속도와 카메라는 이미 로컬 sqlite에 있고, 경로가 하는 일은 "분기에서 어느 쪽인지" 하나다. 신규 모듈은 `korea/route.py` 하나이고 openpilot 스택을 모르는 순수 모듈이다. HTTP는 `CameraRefresher`와 같은 백그라운드 스레드에서 돌아 1 Hz 제어 루프를 절대 막지 않는다. 출력은 기존 통로 둘을 그대로 쓴다 — 카메라는 `liveMapDataSP`, 커브는 `MapTargetVelocities`.

**Tech Stack:** Python 3.11+ (디바이스), stdlib만 — `urllib.request`, `json`, `math`, `threading`, `time`. 신규 런타임 의존성 없음. 경로 제공자는 TMAP (`apis.openapi.sk.com/tmap/routes`), 응답은 GeoJSON FeatureCollection.

**베이스:** `korea-dev` @ `aaf0feff2`
**스펙:** `docs/superpowers/specs/2026-09-18-korea-route-nav-design.md`

## Global Constraints

- 베이스: `korea-dev` @ `aaf0feff2`. 이 브랜치 위에서 작업한다.
- 소스 경로는 중첩 구조를 따른다: 모든 코드는 `openpilot/` 하위.
- **신규 런타임 의존성 0.** stdlib만 쓴다. `requests` 금지 — `urllib.request`를 쓴다.
- `openpilot/sunnypilot/mapd/korea/` 하위는 **모듈 최상단에서 openpilot 스택 import 금지** — `cereal`, `common.params`, `common.constants`, `cloudlog` 중 무엇도 쓰지 않는다. bare 인터프리터에서 import 가능해야 테스트가 디바이스 스택 없이 돈다. 로깅은 stdlib `logging`.
  - 예외는 `CameraRefresher._loop`(`camera_refresh.py:161`)가 쓰는 방식뿐이다: `cereal`과 `Params`를 **스레드 함수 안에서** import한다.
  - 같은 패키지 안끼리의 import는 허용되지만 방향은 항상 **빌더 → 런타임**이다. `route.py`가 `deploy.py`나 `build_db.py`를 가져오면 안 된다.
- Python 들여쓰기는 2칸 (openpilot 규약).
- `openpilot/sunnypilot/` 하위 신규 파일은 sunnypilot MIT 헤더로 시작한다:
  ```python
  """
  Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

  This file is part of sunnypilot and is licensed under the MIT License.
  See the LICENSE.md file in the root directory for more details.
  """
  ```
- 테스트는 `unittest`다 (pytest 아님). 어서션은 `self.assertEqual` 계열.
- 새 파라미터는 `openpilot/common/params_keys.h`에 등록해야 `Params().get()`이 동작한다. **헤더만 고치면 안 된다** — `params.cc`로 컴파일되므로 재빌드가 필요하다 (아래 테스트 환경 절).
- **설정 화면은 세 개다.** 디바이스 raylib UI(`speed_limit_settings.py`), mici 소형 UI(`toggles.py`), sunnylink 원격 YAML(`settings_ui_src/pages/korea.yaml`). 사용자가 만질 새 파라미터는 세 곳 모두에 노출한다. `settings_ui.json`은 생성물이므로 절대 손으로 고치지 않는다.
- **외부에서 온 값은 전부 untrusted다.** `external_source.py:19`가 세운 원칙을 승계한다 — 이상한 값은 예외가 아니라 0 또는 `None`으로 떨어진다.
- **실패는 전부 기존 동작으로 degrade한다.** 경로가 없을 때의 동작은 오늘의 동작과 100% 같아야 한다. 이것이 모든 태스크의 회귀 기준이다.

### 테스트 환경 (이 호스트 전용)

`.superpowers/sdd/test-env.md`에 전체 레시피가 있다. 요약:

- **Docker bind-mount(`docker run -v`)이 이 호스트에서 깨져 있다.** `docker cp`로 넣는다.
- **(A) `korea/tests/`** — 순수 stdlib. 호스트 Python에서 그냥 돈다:
  ```bash
  cd E:/dev/sunnypilot
  python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v
  ```
  기존 문제 하나: `test_db.py`의 `test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road`가 Windows에서 `PermissionError`로 실패한다. 이 계획 이전부터 있었고 리눅스에서는 안 난다. 무시한다.
- **(B) cereal 필요** — `mapd/tests/`는 `sp-build` 컨테이너에서 돈다:
  ```bash
  MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
  docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_korea_map_data -v'
  ```
- **`params_keys.h`를 고쳤으면 재빌드한다:**
  ```bash
  docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
  ```

---

## File Structure

| 파일 | 책임 | 상태 |
|---|---|---|
| `openpilot/sunnypilot/mapd/korea/route.py` | 목적지 → 폴리라인. 파싱·검증·HTTP·재탐색 상태기계·곡률. openpilot 스택 모름 | 신규 |
| `openpilot/sunnypilot/mapd/korea/tests/test_route.py` | 위 모듈 전부 | 신규 |
| `openpilot/sunnypilot/mapd/korea/external_source.py` | 와이어 포맷과 신뢰 경계. 목적지 키 2개 추가 | 수정 |
| `openpilot/sunnypilot/mapd/korea/db.py` | sqlite 조회. `next_camera`/`next_bump`에 경로 회랑 필터 추가 | 수정 |
| `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py` | 배선. 경로를 db에 넘기고, 커브+방지턱을 `MapTargetVelocities`로 병합 | 수정 |
| `openpilot/sunnypilot/mapd/mapd_manager.py` | `RouteSource` 스레드 기동·정지 | 수정 |
| `openpilot/common/params_keys.h` | `KoreaRouteApiKey`, `NavDestination` 등록 | 수정 |
| 설정 UI 3곳 | `KoreaRouteApiKey` 노출 | 수정 |

`route.py`가 커 보이지만 네 덩어리(파싱/HTTP/상태기계/곡률)가 전부 "목적지를 폴리라인과 목표속도로 바꾼다"는 한 책임이고 함께 변한다. 나누면 서로만 쓰는 모듈 넷이 생긴다.

**새 토글은 만들지 않는다.** 경로 기능은 목적지가 있어야 동작하고 목적지는 UDP로 오므로, 이미 있는 `KoreaExternalNavEnabled`가 정확한 게이트다. 여기에 `KoreaRouteApiKey`가 비어 있으면 자동으로 꺼진다. 토글을 더 만들면 사용자가 둘 다 켜야 하는 함정이 생긴다.

---

## Task 1: 경로 응답 파싱과 검증

**Files:**
- Create: `openpilot/sunnypilot/mapd/korea/route.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_route.py`

**Interfaces:**
- Consumes: 없음 (최초 태스크)
- Produces:
  - `KOREA_BBOX: tuple[float, float, float, float]` — `(min_lat, max_lat, min_lon, max_lon)`
  - `MAX_ROUTE_POINTS: int`, `MIN_ROUTE_POINTS: int`
  - `in_korea(lat: float, lon: float) -> bool`
  - `parse_route(payload: dict) -> list[tuple[float, float]]` — `(lat, lon)` 튜플 리스트. 검증 실패면 빈 리스트.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`openpilot/sunnypilot/mapd/korea/tests/test_route.py`를 만든다:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import unittest

from openpilot.sunnypilot.mapd.korea.route import MAX_ROUTE_POINTS, in_korea, parse_route


def feature(coords, kind="LineString"):
  return {"type": "Feature", "geometry": {"type": kind, "coordinates": coords}}


# TMAP answers GeoJSON: coordinates are [lon, lat], LineString features carry the road
# geometry and Point features carry the turn markers. Seoul city hall -> Gangnam, trimmed.
SAMPLE = {
  "type": "FeatureCollection",
  "features": [
    feature([126.9780, 37.5665], kind="Point"),
    feature([[126.9780, 37.5665], [126.9800, 37.5600], [126.9850, 37.5500]]),
    feature([[126.9850, 37.5500], [127.0276, 37.4979]]),
  ],
}


class TestInKorea(unittest.TestCase):
  def test_seoul_is_inside(self):
    self.assertTrue(in_korea(37.5665, 126.9780))

  def test_tokyo_is_outside(self):
    self.assertFalse(in_korea(35.6762, 139.6503))

  def test_null_island_is_outside(self):
    self.assertFalse(in_korea(0., 0.))


class TestParseRoute(unittest.TestCase):
  def test_linestrings_concatenate_in_order(self):
    self.assertEqual(parse_route(SAMPLE), [
      (37.5665, 126.9780), (37.5600, 126.9800), (37.5500, 126.9850), (37.4979, 127.0276),
    ])

  def test_point_features_are_ignored(self):
    only_points = {"features": [feature([126.9780, 37.5665], kind="Point")]}
    self.assertEqual(parse_route(only_points), [])

  def test_a_coordinate_outside_korea_discards_the_whole_route(self):
    payload = {"features": [feature([[126.9780, 37.5665], [139.6503, 35.6762]])]}
    self.assertEqual(parse_route(payload), [])

  def test_too_many_points_are_discarded(self):
    coords = [[126.9780 + i * 1e-5, 37.5665] for i in range(MAX_ROUTE_POINTS + 1)]
    self.assertEqual(parse_route({"features": [feature(coords)]}), [])

  def test_a_single_point_is_not_a_route(self):
    self.assertEqual(parse_route({"features": [feature([[126.9780, 37.5665]])]}), [])

  def test_malformed_payload_is_empty_not_an_exception(self):
    for bad in ({}, {"features": None}, {"features": [{}]}, {"features": [{"geometry": 7}]},
                {"features": [feature([["nope", "nope"]])]}):
      self.assertEqual(parse_route(bad), [])
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'openpilot.sunnypilot.mapd.korea.route'`

- [ ] **Step 3: 최소 구현을 쓴다**

`openpilot/sunnypilot/mapd/korea/route.py`를 만든다:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

route: turns a destination into a polyline, and the polyline into slowdown targets.

Only the geometry is taken from the routing provider. Speed limits and cameras already
live in the local sqlite databases, so the route's single job is telling the lookups
which way we go at a fork. That keeps the parser thin enough to swap providers by
editing parse_route alone.

Deliberately free of openpilot imports so it runs under a bare Python interpreter --
same rule as db.py and geo.py.
"""
import logging

# Everything this feature serves is inside South Korea, so a coordinate outside it is a
# provider bug or a hostile answer either way. Generous on purpose: the box covers Jeju
# and Ulleungdo, and it is a sanity check, not a service area.
KOREA_BBOX = (33., 39., 124., 132.)  # min_lat, max_lat, min_lon, max_lon

# A 300 km route at TMAP's resolution is a few thousand points. 5000 is a runaway guard,
# not a real limit -- a bigger answer means the request was not what we think it was.
MAX_ROUTE_POINTS = 5000
MIN_ROUTE_POINTS = 2

LOG = logging.getLogger(__name__)


def in_korea(lat: float, lon: float) -> bool:
  min_lat, max_lat, min_lon, max_lon = KOREA_BBOX
  return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def parse_route(payload: dict) -> list[tuple[float, float]]:
  """Concatenated LineString geometry as (lat, lon), or [] for anything we will not trust.

  [] rather than an exception: every caller is on a path that must keep running with no
  route, and 'no route' is already a state the rest of the feature handles.
  """
  points: list[tuple[float, float]] = []
  try:
    for feat in payload.get("features") or []:
      geometry = feat.get("geometry") or {}
      if geometry.get("type") != "LineString":
        continue
      for lon, lat in geometry.get("coordinates") or []:
        lat, lon = float(lat), float(lon)
        if not in_korea(lat, lon):
          LOG.warning("route: coordinate outside Korea, discarding the route")
          return []
        points.append((lat, lon))
        if len(points) > MAX_ROUTE_POINTS:
          LOG.warning("route: over %d points, discarding the route", MAX_ROUTE_POINTS)
          return []
  except (AttributeError, TypeError, ValueError):
    # A payload shaped differently from what we expect is the same as no route. Deliberately
    # broad over the shape errors only -- a real bug in this function should still raise.
    LOG.warning("route: malformed payload")
    return []

  return points if len(points) >= MIN_ROUTE_POINTS else []
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: PASS, 9 tests

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/route.py openpilot/sunnypilot/mapd/korea/tests/test_route.py
git commit -m "feat: parse a routing answer into a validated polyline"
```

---

## Task 2: 경로 요청 (HTTP) 과 일일 상한

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/route.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_route.py`

**Interfaces:**
- Consumes: `parse_route`, `in_korea` (Task 1)
- Produces:
  - `ROUTE_URL: str`, `HTTP_TIMEOUT_S: float`, `DAILY_REQUEST_CAP: int`
  - `build_request(api_key, start, dest) -> urllib.request.Request` — `start`/`dest`는 `(lat, lon)`
  - `RequestBudget` — `.allow() -> bool`, `.spend() -> None`, 생성자 `RequestBudget(cap=DAILY_REQUEST_CAP, clock=time.time)`
  - `fetch_route(api_key, start, dest, opener=urllib.request.urlopen, budget=None) -> list[tuple[float, float]]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_route.py` 끝에 붙인다. 상단 import를 다음으로 바꾼다:

```python
import json
import unittest

from openpilot.sunnypilot.mapd.korea.route import (DAILY_REQUEST_CAP, MAX_ROUTE_POINTS, RequestBudget,
                                                   build_request, fetch_route, in_korea, parse_route)
```

```python
class FakeResponse:
  def __init__(self, payload):
    self._body = json.dumps(payload).encode()

  def read(self):
    return self._body

  def __enter__(self):
    return self

  def __exit__(self, *args):
    return False


def fake_opener(payload, capture=None):
  def opener(request, timeout=None):
    if capture is not None:
      capture.append(request)
    return FakeResponse(payload)
  return opener


class FakeClock:
  def __init__(self, now=0.):
    self.now = now

  def __call__(self):
    return self.now


class TestBuildRequest(unittest.TestCase):
  def test_the_key_travels_in_the_header_not_the_url(self):
    request = build_request("SECRET", (37.5665, 126.9780), (37.4979, 127.0276))
    self.assertNotIn("SECRET", request.full_url)
    self.assertEqual(request.get_header("Appkey"), "SECRET")

  def test_the_body_carries_lon_lat_in_that_order(self):
    request = build_request("K", (37.5665, 126.9780), (37.4979, 127.0276))
    body = json.loads(request.data)
    self.assertEqual((body["startX"], body["startY"]), (126.9780, 37.5665))
    self.assertEqual((body["endX"], body["endY"]), (127.0276, 37.4979))
    self.assertEqual(body["reqCoordType"], "WGS84GEO")
    self.assertEqual(body["resCoordType"], "WGS84GEO")


class TestRequestBudget(unittest.TestCase):
  def test_spending_up_to_the_cap_is_allowed(self):
    budget = RequestBudget(cap=3, clock=FakeClock())
    for _ in range(3):
      self.assertTrue(budget.allow())
      budget.spend()
    self.assertFalse(budget.allow())

  def test_the_budget_resets_on_the_next_day(self):
    clock = FakeClock()
    budget = RequestBudget(cap=1, clock=clock)
    budget.spend()
    self.assertFalse(budget.allow())
    clock.now += 24 * 3600
    self.assertTrue(budget.allow())

  def test_the_default_cap_is_the_documented_one(self):
    self.assertEqual(RequestBudget().cap, DAILY_REQUEST_CAP)


class TestFetchRoute(unittest.TestCase):
  def test_a_good_answer_becomes_a_polyline(self):
    route = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276), opener=fake_opener(SAMPLE))
    self.assertEqual(len(route), 4)

  def test_an_http_failure_is_an_empty_route(self):
    def boom(request, timeout=None):
      raise OSError("no network")
    self.assertEqual(fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276), opener=boom), [])

  def test_a_destination_outside_korea_is_never_requested(self):
    capture = []
    route = fetch_route("K", (37.5665, 126.9780), (35.6762, 139.6503),
                        opener=fake_opener(SAMPLE, capture))
    self.assertEqual(route, [])
    self.assertEqual(capture, [])

  def test_an_empty_key_is_never_requested(self):
    capture = []
    route = fetch_route("", (37.5665, 126.9780), (37.4979, 127.0276),
                        opener=fake_opener(SAMPLE, capture))
    self.assertEqual(route, [])
    self.assertEqual(capture, [])

  def test_the_budget_stops_the_request_before_the_socket(self):
    capture = []
    budget = RequestBudget(cap=0, clock=FakeClock())
    route = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276),
                        opener=fake_opener(SAMPLE, capture), budget=budget)
    self.assertEqual(route, [])
    self.assertEqual(capture, [])
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: FAIL — `ImportError: cannot import name 'RequestBudget'`

- [ ] **Step 3: 최소 구현을 쓴다**

`route.py`의 import를 다음으로 바꾼다:

```python
import json
import logging
import time
import urllib.request
```

상수 블록 아래에 붙인다:

```python
ROUTE_URL = "https://apis.openapi.sk.com/tmap/routes?version=1&format=json"
# Much shorter than camera_refresh's 30 s: that one runs offroad with all day to finish,
# this one is a driver waiting for a reroute. A request that has not answered in 10 s has
# already lost to the backoff that follows it.
HTTP_TIMEOUT_S = 10.
# A hard ceiling that does not depend on knowing the provider's free tier, which is not
# published. Real use is a handful of requests per drive, so this only ever fires on a bug.
DAILY_REQUEST_CAP = 200
_DAY_S = 24 * 3600


class RequestBudget:
  """A per-day request ceiling. Not thread-safe -- one route thread owns it."""

  def __init__(self, cap: int = DAILY_REQUEST_CAP, clock=time.time):  # noqa: TID251
    # Wall clock on purpose: 'per day' is a calendar notion, and a monotonic clock has an
    # arbitrary epoch. Same reasoning as CameraRefresher._due.
    self.cap = cap
    self._clock = clock
    self._day = int(clock() // _DAY_S)
    self._spent = 0

  def _roll(self) -> None:
    day = int(self._clock() // _DAY_S)
    if day != self._day:
      self._day, self._spent = day, 0

  def allow(self) -> bool:
    self._roll()
    return self._spent < self.cap

  def spend(self) -> None:
    self._roll()
    self._spent += 1


def build_request(api_key: str, start: tuple[float, float],
                  dest: tuple[float, float]) -> urllib.request.Request:
  """The key goes in a header, never the URL: urllib puts the URL in exception messages
  and those reach cloudlog. camera_refresh.py:105 makes the same promise."""
  body = {
    "startX": start[1], "startY": start[0],
    "endX": dest[1], "endY": dest[0],
    "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
    "searchOption": "0",
  }
  return urllib.request.Request(
    ROUTE_URL,
    data=json.dumps(body).encode(),
    headers={"appKey": api_key, "Content-Type": "application/json"},
    method="POST",
  )


def fetch_route(api_key: str, start: tuple[float, float], dest: tuple[float, float],
                opener=urllib.request.urlopen, budget: RequestBudget | None = None
                ) -> list[tuple[float, float]]:
  """One routing request. [] for anything that does not produce a route we trust.

  Every reason to not make the request is checked before the socket is touched, so a
  missing key or a bad destination costs nothing.
  """
  if not api_key or not in_korea(*start) or not in_korea(*dest):
    return []
  if budget is not None and not budget.allow():
    LOG.warning("route: daily request cap reached")
    return []

  try:
    if budget is not None:
      budget.spend()
    with opener(build_request(api_key, start, dest), timeout=HTTP_TIMEOUT_S) as response:
      payload = json.loads(response.read())
  except Exception:
    # Deliberately broad. urllib raises OSError/HTTPError, json raises ValueError, and a
    # truncated body can raise almost anything -- all of them mean the same thing here,
    # and this runs on a thread whose death would silently disable the feature.
    LOG.warning("route: request failed")
    return []

  return parse_route(payload) if isinstance(payload, dict) else []
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: PASS, 19 tests

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/route.py openpilot/sunnypilot/mapd/korea/tests/test_route.py
git commit -m "feat: fetch a route, with the key in a header and a daily ceiling"
```

---

## Task 3: 이탈 판정과 재탐색 상태기계

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/route.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_route.py`

**Interfaces:**
- Consumes: Task 1–2 전부
- Produces:
  - `OFF_ROUTE_M`, `OFF_ROUTE_TICKS`, `REROUTE_BACKOFF_S: tuple[float, ...]`
  - `distance_to_route(route, lat, lon) -> float` — 빈 경로면 `float('inf')`
  - `RouteState(clock=time.monotonic)` — 속성 `.route`, 메서드:
    - `.set_route(route: list[tuple[float, float]]) -> None`
    - `.update(lat: float, lon: float) -> bool` — 지금 재탐색해야 하면 `True`
    - `.note_request() -> None` — 요청을 보냈다고 알림 (쿨다운 시작)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

import에 `OFF_ROUTE_TICKS`, `REROUTE_BACKOFF_S`, `RouteState`, `distance_to_route`를 더하고 붙인다:

```python
# A 1.1 km straight leg north-east of Seoul city hall. ~0.001 deg lat is ~111 m.
STRAIGHT = [(37.5665 + i * 0.001, 126.9780) for i in range(11)]


class TestDistanceToRoute(unittest.TestCase):
  def test_a_point_on_the_line_is_zero(self):
    self.assertLess(distance_to_route(STRAIGHT, 37.5675, 126.9780), 1.)

  def test_an_empty_route_is_infinitely_far(self):
    self.assertEqual(distance_to_route([], 37.5665, 126.9780), float("inf"))

  def test_the_offset_is_measured_perpendicular(self):
    # 0.001 deg of longitude at this latitude is about 88 m
    offset = distance_to_route(STRAIGHT, 37.5675, 126.9790)
    self.assertGreater(offset, 80.)
    self.assertLess(offset, 95.)


class TestRouteState(unittest.TestCase):
  def setUp(self):
    self.clock = FakeClock()
    self.state = RouteState(clock=self.clock)
    self.state.set_route(STRAIGHT)

  def drive(self, ticks, lat=37.5675, lon=126.9780):
    return [self.state.update(lat, lon) for _ in range(ticks)]

  def test_staying_on_route_never_reroutes(self):
    self.assertEqual(self.drive(10), [False] * 10)

  def test_three_consecutive_off_route_ticks_reroute(self):
    self.assertEqual(self.drive(3, lon=126.9800), [False, False, True])

  def test_two_off_route_ticks_then_back_on_does_not_reroute(self):
    self.drive(2, lon=126.9800)
    self.assertEqual(self.drive(3), [False] * 3)

  def test_an_empty_route_reroutes_immediately(self):
    self.state.set_route([])
    self.assertEqual(self.state.update(37.5675, 126.9780), True)

  def test_the_cooldown_holds_the_second_request(self):
    self.drive(3, lon=126.9800)
    self.state.note_request()
    self.assertEqual(self.drive(3, lon=126.9800), [False] * 3)
    self.clock.now += REROUTE_BACKOFF_S[0]
    self.assertEqual(self.state.update(37.5675, 126.9800), True)

  def test_consecutive_failures_walk_the_backoff(self):
    for expected_wait in REROUTE_BACKOFF_S:
      self.drive(OFF_ROUTE_TICKS, lon=126.9800)
      self.state.note_request()
      self.assertEqual(self.state.update(37.5675, 126.9800), False)
      self.clock.now += expected_wait

  def test_the_last_backoff_step_repeats_rather_than_overflowing(self):
    for _ in range(len(REROUTE_BACKOFF_S) + 3):
      self.drive(OFF_ROUTE_TICKS, lon=126.9800)
      self.state.note_request()
      self.clock.now += REROUTE_BACKOFF_S[-1]
    self.assertEqual(self.state.update(37.5675, 126.9800), True)

  def test_getting_back_on_route_resets_the_backoff(self):
    for _ in range(3):
      self.drive(OFF_ROUTE_TICKS, lon=126.9800)
      self.state.note_request()
      self.clock.now += REROUTE_BACKOFF_S[-1]
    self.drive(1)  # back on the line
    self.drive(OFF_ROUTE_TICKS, lon=126.9800)
    self.state.note_request()
    self.clock.now += REROUTE_BACKOFF_S[0]
    self.assertEqual(self.state.update(37.5675, 126.9800), True)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: FAIL — `ImportError: cannot import name 'RouteState'`

- [ ] **Step 3: 최소 구현을 쓴다**

`route.py`에 import를 더한다:

```python
from openpilot.sunnypilot.mapd.korea.geo import point_segment_distance
```

그리고 붙인다:

```python
# Wider than the 30 m corridor the lookups use: the corridor decides whether a camera is
# on our road, this decides whether the route is wrong. Being strict here buys nothing and
# costs a request every time the localizer drifts in a tunnel.
OFF_ROUTE_M = 50.
# One tick of GPS jitter is not a wrong turn. Three seconds at 1 Hz is.
OFF_ROUTE_TICKS = 3
# The first step is the normal reroute latency -- 3 s to confirm plus 5 s is not felt. The
# rest exist only for the runaway case where the new route is immediately off-route too,
# which is the failure the cooldown alone used to be asked to cover and could not.
REROUTE_BACKOFF_S = (5., 15., 60., 300.)


def distance_to_route(route: list[tuple[float, float]], lat: float, lon: float) -> float:
  """Metres from (lat, lon) to the nearest point of the polyline."""
  if len(route) < MIN_ROUTE_POINTS:
    return float("inf")
  return min(point_segment_distance(lat, lon, a[0], a[1], b[0], b[1])
             for a, b in zip(route, route[1:], strict=False))


class RouteState:
  """Decides when to ask for a new route. Owns no I/O -- the caller does the fetching.

  Kept apart from the fetching on purpose: this is the part with the interesting states,
  and a pure object is the only way to test a backoff without waiting five minutes.
  """

  def __init__(self, clock=time.monotonic):
    self.route: list[tuple[float, float]] = []
    self._clock = clock
    self._off_ticks = 0
    self._failures = 0
    self._last_request = None  # None means 'never asked', which must not be a cooldown

  def set_route(self, route: list[tuple[float, float]]) -> None:
    self.route = route
    self._off_ticks = 0

  def note_request(self) -> None:
    """Called after a request goes out, whatever it answered. A request that produced a
    good route is still followed by a reset -- set_route's caller does that by getting
    back on the line, not by this method, so a bad answer keeps walking the backoff."""
    self._last_request = self._clock()
    self._failures += 1

  def _wait(self) -> float:
    return REROUTE_BACKOFF_S[min(self._failures - 1, len(REROUTE_BACKOFF_S) - 1)]

  def update(self, lat: float, lon: float) -> bool:
    if distance_to_route(self.route, lat, lon) <= OFF_ROUTE_M:
      self._off_ticks = 0
      self._failures = 0  # back on the line: whatever went wrong is over
      return False

    self._off_ticks += 1
    if self._off_ticks < OFF_ROUTE_TICKS and self.route:
      return False
    if self._last_request is not None and self._clock() - self._last_request < self._wait():
      return False
    return True
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: PASS, 30 tests

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/route.py openpilot/sunnypilot/mapd/korea/tests/test_route.py
git commit -m "feat: reroute after three off-route ticks, with a runaway backoff"
```

---

## Task 4: 전방 곡률에서 목표 속도 점 만들기

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/route.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_route.py`

**Interfaces:**
- Consumes: Task 1–3 전부
- Produces:
  - `CURVE_HORIZON_M`, `A_LAT_MAX`, `MIN_V_MS`
  - `curve_targets(route, lat, lon, v_max_ms) -> list[tuple[float, float, float]]` — `(lat, lon, velocity_ms)`, 가까운 순

- [ ] **Step 1: 실패하는 테스트를 쓴다**

import에 `A_LAT_MAX`, `MIN_V_MS`, `curve_targets`를 더하고 붙인다:

```python
import math

from openpilot.sunnypilot.mapd.korea.geo import haversine


def arc(center_lat, center_lon, radius_m, start_deg, end_deg, step_deg=5.):
  """A circular arc as (lat, lon) points -- a curve with a known radius to check against."""
  m_per_deg_lat = 111195.
  m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(center_lat))
  points = []
  angle = start_deg
  while angle <= end_deg:
    rad = math.radians(angle)
    points.append((center_lat + radius_m * math.sin(rad) / m_per_deg_lat,
                   center_lon + radius_m * math.cos(rad) / m_per_deg_lon))
    angle += step_deg
  return points


class TestCurveTargets(unittest.TestCase):
  def test_a_straight_road_has_no_targets(self):
    self.assertEqual(curve_targets(STRAIGHT, 37.5665, 126.9780, 30.), [])

  def test_an_empty_route_has_no_targets(self):
    self.assertEqual(curve_targets([], 37.5665, 126.9780, 30.), [])

  def test_a_200_m_radius_curve_targets_the_physics_speed(self):
    route = arc(37.5665, 126.9780, 200., 0., 90.)
    targets = curve_targets(route, route[0][0], route[0][1], 30.)
    self.assertTrue(targets)
    expected = math.sqrt(A_LAT_MAX * 200.)  # ~20 m/s
    self.assertAlmostEqual(targets[0][2], expected, delta=3.)

  def test_a_tight_curve_is_clamped_to_the_floor(self):
    route = arc(37.5665, 126.9780, 15., 0., 180.)
    targets = curve_targets(route, route[0][0], route[0][1], 30.)
    self.assertTrue(targets)
    self.assertGreaterEqual(min(t[2] for t in targets), MIN_V_MS)

  def test_a_gentle_curve_is_not_reported_above_the_set_speed(self):
    route = arc(37.5665, 126.9780, 2000., 0., 90.)
    self.assertEqual(curve_targets(route, route[0][0], route[0][1], 10.), [])

  def test_targets_come_back_nearest_first(self):
    route = STRAIGHT[:3] + arc(37.5685, 126.9780, 100., 0., 180.)
    targets = curve_targets(route, STRAIGHT[0][0], STRAIGHT[0][1], 30.)
    self.assertGreater(len(targets), 1)
    distances = [haversine(STRAIGHT[0][0], STRAIGHT[0][1], lat, lon) for lat, lon, _ in targets]
    self.assertEqual(distances, sorted(distances))

  def test_a_curve_behind_us_is_ignored(self):
    route = arc(37.5665, 126.9780, 100., 0., 90.)
    beyond = route[-1]
    self.assertEqual(curve_targets(route, beyond[0], beyond[1], 30.), [])
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: FAIL — `ImportError: cannot import name 'curve_targets'`

- [ ] **Step 3: 최소 구현을 쓴다**

`route.py`의 import에 `math`와 `haversine`을 더한다:

```python
import math
...
from openpilot.sunnypilot.mapd.korea.geo import haversine, point_segment_distance
```

붙인다:

```python
# SCC-Map brakes with a jerk limit, so a point handed over at 300 m is already inside the
# comfortable window at highway speed and well outside it in town.
CURVE_HORIZON_M = 300.
# Matches _A_LAT_REG_MAX in smart_cruise_control/vision_controller.py:31. The two
# controllers feed the same longitudinal planner, so disagreeing here would show up as one
# of them fighting the other through a bend.
A_LAT_MAX = 2.
# smart_cruise_control/__init__.py: MIN_V = 20 * CV.KPH_TO_MS. Repeated rather than
# imported because korea/ must stay importable without the openpilot stack.
MIN_V_MS = 20. / 3.6


def _menger_curvature(a: tuple[float, float], b: tuple[float, float],
                      c: tuple[float, float]) -> float:
  """Curvature in 1/m of the circle through three points, 0 when they are collinear.

  Menger's formula rather than a derivative: the polyline's point spacing is uneven, and
  three points with a circumscribed circle need no differentiation to be stable.
  """
  ab = haversine(a[0], a[1], b[0], b[1])
  bc = haversine(b[0], b[1], c[0], c[1])
  ca = haversine(c[0], c[1], a[0], a[1])
  if ab == 0. or bc == 0. or ca == 0.:
    return 0.

  # twice the triangle area, by the cross product in a local flat frame
  m_lon = 111195. * math.cos(math.radians(b[0]))
  ax, ay = (a[1] - b[1]) * m_lon, (a[0] - b[0]) * 111195.
  cx, cy = (c[1] - b[1]) * m_lon, (c[0] - b[0]) * 111195.
  area2 = abs(ax * cy - ay * cx)
  return 2. * area2 / (ab * bc * ca) if area2 > 0. else 0.


def curve_targets(route: list[tuple[float, float]], lat: float, lon: float,
                  v_max_ms: float) -> list[tuple[float, float, float]]:
  """(lat, lon, velocity) points for the curves inside CURVE_HORIZON_M ahead, nearest first.

  Only curves that actually ask for a slowdown are returned: a target at or above the set
  speed is not a target, it is noise SCC-Map would have to filter itself.
  """
  if len(route) < 3:
    return []

  # start at the segment we are on, so a curve already behind us is never reported
  start = min(range(len(route) - 1),
              key=lambda i: point_segment_distance(lat, lon, route[i][0], route[i][1],
                                                   route[i + 1][0], route[i + 1][1]))

  targets: list[tuple[float, float, float]] = []
  travelled = haversine(lat, lon, route[start][0], route[start][1])
  for i in range(start + 1, len(route) - 1):
    travelled += haversine(route[i - 1][0], route[i - 1][1], route[i][0], route[i][1])
    if travelled > CURVE_HORIZON_M:
      break

    curvature = _menger_curvature(route[i - 1], route[i], route[i + 1])
    if curvature <= 0.:
      continue
    velocity = max(math.sqrt(A_LAT_MAX / curvature), MIN_V_MS)
    if velocity < v_max_ms:
      targets.append((route[i][0], route[i][1], velocity))

  return targets
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route -v`
Expected: PASS, 37 tests

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/route.py openpilot/sunnypilot/mapd/korea/tests/test_route.py
git commit -m "feat: turn route curvature into slowdown targets"
```

---

## Task 5: UDP로 목적지 받기

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/external_source.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_external_source.py`

**Interfaces:**
- Consumes: `in_korea` (Task 1)
- Produces: `ExternalNav.destination: tuple[float, float] | None` — 목적지가 없거나 믿을 수 없으면 `None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_external_source.py`의 `TestParsePayload` 클래스 끝에 붙인다:

```python
  def test_destination_lands(self):
    nav = parse_payload({**VALID, "destination_lat": 37.4979, "destination_lon": 127.0276})
    self.assertEqual(nav.destination, (37.4979, 127.0276))

  def test_no_destination_key_is_none(self):
    self.assertIsNone(parse_payload(VALID).destination)

  def test_destination_outside_korea_is_rejected(self):
    nav = parse_payload({**VALID, "destination_lat": 35.6762, "destination_lon": 139.6503})
    self.assertIsNone(nav.destination)

  def test_zero_destination_means_guidance_ended(self):
    nav = parse_payload({**VALID, "destination_lat": 0, "destination_lon": 0})
    self.assertIsNone(nav.destination)

  def test_non_numeric_destination_is_rejected(self):
    nav = parse_payload({**VALID, "destination_lat": "37.5", "destination_lon": None})
    self.assertIsNone(nav.destination)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_external_source -v`
Expected: FAIL — `AttributeError: 'ExternalNav' object has no attribute 'destination'`

- [ ] **Step 3: 최소 구현을 쓴다**

`external_source.py`의 모듈 docstring 와이어 포맷 블록을 바꾼다:

```python
  {"speed_limit_kph": 60, "next_speed_limit_kph": 50,
   "next_speed_limit_distance_m": 320, "road_name": "테헤란로",
   "destination_lat": 37.4979, "destination_lon": 127.0276}

destination_lat/lon start a route the device fetches itself; 0/0 means guidance ended.
```

import를 더한다:

```python
from openpilot.sunnypilot.mapd.korea.route import in_korea
```

`ExternalNav`에 필드를 더한다:

```python
@dataclass
class ExternalNav:
  speed_limit_kph: float = 0.
  next_speed_limit_kph: float = 0.
  next_speed_limit_distance_m: float = 0.
  road_name: str = ""
  destination: tuple[float, float] | None = None
  received_at: float = 0.
  raw: dict = field(default_factory=dict)
```

`_bounded` 아래에 붙인다:

```python
def _destination(payload: dict) -> tuple[float, float] | None:
  """The destination, or None for 'no destination' -- which 0/0 deliberately means.

  A phone that sends 0/0 is saying guidance ended. That is the same state as never having
  sent one, so both arrive here as None and the caller needs no third case.
  """
  lat, lon = payload.get("destination_lat"), payload.get("destination_lon")
  for value in (lat, lon):
    if isinstance(value, bool) or not isinstance(value, int | float):
      return None
  return (float(lat), float(lon)) if in_korea(float(lat), float(lon)) else None
```

`parse_payload`의 반환에 한 줄을 더한다:

```python
    road_name=name[:MAX_ROAD_NAME] if isinstance(name, str) else "",
    destination=_destination(payload),
    received_at=time.monotonic(),
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_external_source -v`
Expected: PASS, 기존 테스트 전부 + 새 5개

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/external_source.py openpilot/sunnypilot/mapd/korea/tests/test_external_source.py
git commit -m "feat: accept a destination over the external nav socket"
```

---

## Task 6: 경로 회랑으로 카메라·방지턱 거르기

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/db.py:316-363`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_db.py`

**Interfaces:**
- Consumes: `distance_to_route` (Task 3)
- Produces:
  - `ROUTE_CORRIDOR_M: float` (in `db.py`)
  - `KoreaMapDB.next_camera(lat, lon, heading_deg, route=None)` — `route`는 `list[tuple[float, float]] | None`
  - `KoreaMapDB.next_bump(lat, lon, heading_deg, route=None)` — 같은 규약

`route=None`일 때의 동작은 오늘과 **완전히 같아야 한다.** 이것이 이 태스크의 회귀 기준이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_db.py` 끝에 붙인다. 이 파일의 기존 픽스처 헬퍼(카메라·링크를 sqlite에 넣는 함수)를 그대로 쓴다 — 파일 상단에서 이름을 확인하고 같은 것을 부른다.

이 파일의 `KoreaMapDBTestCase`와 그 헬퍼(`open_db`, `open_db_with_bumps`)를 그대로 쓴다. 픽스처는 튜플이다 — 카메라는 `(lat, lon, limit_kph, section_m)`, 링크는 `(speed, name, [(lat, lon), ...])`.

```python
# ROAD_60 runs east from (37.5000, 127.0200). CAM_AHEAD sits on it 500 m east.
# A camera 33 m north of CAM_AHEAD is a parallel road: inside the 60 deg cone that
# next_camera has always used, outside the 30 m route corridor.
CAM_SIDE_ROAD = (37.5003, 127.0257, 30, 0)


class TestRouteCorridorCameras(KoreaMapDBTestCase):
  def setUp(self):
    super().setUp()
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_SIDE_ROAD]))
    write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60]))
    self.db = self.open_db(cams, links)
    self.route = [(37.5000, 127.0200), (37.5000, 127.0320)]

  def test_without_a_route_the_side_road_camera_is_accepted(self):
    """Today's behaviour, and the reason this task exists."""
    camera = self.db.next_camera(37.5000, 127.0200, 90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 30)

  def test_the_route_rejects_the_side_road_camera(self):
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., route=self.route))

  def test_a_camera_on_the_route_survives(self):
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD]))
    self.db.reload_if_changed()
    camera = self.db.next_camera(37.5000, 127.0200, 90., route=self.route)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 50)

  def test_an_empty_route_behaves_like_no_route(self):
    self.assertEqual(self.db.next_camera(37.5000, 127.0200, 90.),
                     self.db.next_camera(37.5000, 127.0200, 90., route=[]))

  def test_a_route_running_the_other_way_rejects_everything(self):
    behind = [(37.5000, 127.0200), (37.5000, 127.0080)]
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., route=behind))
```

이어서 방지턱도 확인한다. 경로 필터는 **좁히기만 해야 한다** — 오늘 거부되는 것이 경로 때문에 통과되면 안 된다:

```python
class TestRouteCorridorBumps(KoreaMapDBTestCase):
  def setUp(self):
    super().setUp()
    self.db = self.open_db_with_bumps([BUMP_AHEAD_150, BUMP_LATERAL_10M])
    # ROAD_60 itself: east from the query point, which is what every bump test drives
    self.route = [(37.5000, 127.0200), (37.5000, 127.0320)]
    # a route that turns north 50 m ahead -- the bumps stay east, off it
    self.turn = [(37.5000, 127.0200), (37.5000, 127.0206), (37.5030, 127.0206)]

  def test_without_a_route_the_nearest_bump_wins(self):
    bump = self.db.next_bump(37.5000, 127.0200, 90.)
    self.assertIsNotNone(bump)
    self.assertAlmostEqual(bump.lon, 127.0217, places=4)

  def test_a_route_along_the_road_keeps_the_same_bump(self):
    self.assertEqual(self.db.next_bump(37.5000, 127.0200, 90.),
                     self.db.next_bump(37.5000, 127.0200, 90., route=self.route))

  def test_a_route_that_turns_away_rejects_the_bumps(self):
    self.assertIsNone(self.db.next_bump(37.5000, 127.0200, 90., route=self.turn))

  def test_an_empty_route_behaves_like_no_route(self):
    self.assertEqual(self.db.next_bump(37.5000, 127.0200, 90.),
                     self.db.next_bump(37.5000, 127.0200, 90., route=[]))

  def test_the_route_never_admits_what_the_corridor_rejects(self):
    # BUMP_LATERAL_30M is ~30 m north: outside BUMP_CORRIDOR_M (20 m) but inside
    # ROUTE_CORRIDOR_M (30 m). The route must not promote it.
    db = self.open_db_with_bumps([BUMP_LATERAL_30M])
    self.assertIsNone(db.next_bump(37.5000, 127.0200, 90.))
    self.assertIsNone(db.next_bump(37.5000, 127.0200, 90., route=self.route))
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_db -v`
Expected: FAIL — `TypeError: next_camera() got an unexpected keyword argument 'route'`

- [ ] **Step 3: 최소 구현을 쓴다**

`db.py`의 import를 바꾼다:

```python
from openpilot.sunnypilot.mapd.korea.route import distance_to_route
```

상수 블록에 더한다:

```python
# Tighter than OFF_ROUTE_M (50 m, route.py): that one asks 'is the route still right', this
# asks 'is this camera on our road'. A parallel road is typically 20-40 m away, so the
# corridor has to be narrower than the gap it is meant to reject. 30 m still clears the
# localizer's lateral error in an urban canyon, which is what forced BUMP_CORRIDOR_M to 20
# rather than 10.
ROUTE_CORRIDOR_M = 30.
```

`next_camera` 시그니처와 루프를 바꾼다:

```python
  def next_camera(self, lat: float, lon: float, heading_deg: float | None,
                  route: list[tuple[float, float]] | None = None) -> Camera | None:
    """Nearest speed camera ahead of us, or None. Needs a heading to know what 'ahead' means.

    With a route, 'ahead' stops being a bearing cone and becomes the road we will actually
    drive: the cone alone accepts a camera on the far side of a fork, which is the single
    most visible wrong slowdown this database produces.
    """
    if heading_deg is None:
      return None

    rows = self.cam.execute(
      "SELECT c.lat, c.lon, c.limit_kph, c.section_m FROM cameras_idx i JOIN cameras c ON c.id = i.id " + _RTREE_OVERLAP,
      (lat - CAMERA_SEARCH_DEG, lat + CAMERA_SEARCH_DEG, lon - CAMERA_SEARCH_DEG, lon + CAMERA_SEARCH_DEG),
    ).fetchall()

    best: Camera | None = None

    for clat, clon, limit_kph, section_m in rows:
      distance = haversine(lat, lon, clat, clon)
      if distance > CAMERA_MAX_DISTANCE_M or (best is not None and distance >= best.distance_m):
        continue
      if not _on_path(lat, lon, clat, clon, heading_deg, route, CAMERA_AHEAD_TOLERANCE):
        continue
      best = Camera(limit_kph=limit_kph, distance_m=distance, section_m=section_m)

    return best
```

`next_bump`도 같은 자리를 바꾼다 — 회랑 검사는 `_on_path`에 맡기고, 기존 코드의 `BUMP_CORRIDOR_M` 검사는 경로가 없을 때만 의미가 있으므로 `_on_path` 안으로 옮긴다:

```python
  def next_bump(self, lat: float, lon: float, heading_deg: float | None,
                route: list[tuple[float, float]] | None = None) -> Bump | None:
    """Nearest physical speed bump ahead of us, or None. Same route rule as next_camera."""
    if self.bmp is None or heading_deg is None:
      return None

    rows = self.bmp.execute(
      "SELECT b.lat, b.lon, b.kind FROM bumps_idx i JOIN bumps b ON b.id = i.id " + _RTREE_OVERLAP,
      (lat - BUMP_SEARCH_DEG, lat + BUMP_SEARCH_DEG, lon - BUMP_SEARCH_DEG, lon + BUMP_SEARCH_DEG),
    ).fetchall()

    best: Bump | None = None

    for blat, blon, kind in rows:
      if kind == BUMP_VIRTUAL:
        continue
      distance = haversine(lat, lon, blat, blon)
      if distance > BUMP_MAX_DISTANCE_M or (best is not None and distance >= best.distance_m):
        continue
      if not _on_path(lat, lon, blat, blon, heading_deg, route, BUMP_AHEAD_TOLERANCE,
                      corridor_m=BUMP_CORRIDOR_M):
        continue
      best = Bump(lat=blat, lon=blon, kind=kind, distance_m=distance)

    return best
```

`_heading_matches` 옆에 붙인다:

```python
def _on_path(lat: float, lon: float, tlat: float, tlon: float, heading_deg: float,
             route: list[tuple[float, float]] | None, tolerance_deg: float,
             corridor_m: float | None = None) -> bool:
  """Is the target ahead of us on the road we are driving?

  The route NARROWS the existing test, never widens it. Every check that runs without a
  route still runs with one, and the corridor is added on top. Ordered that way on purpose:
  ROUTE_CORRIDOR_M (30 m) is looser than BUMP_CORRIDOR_M (20 m), so letting the route
  replace the corridor would start accepting bumps on parallel streets that are rejected
  today -- a regression dressed up as a feature.

  The bearing cone stays in the with-route case too, because a route that doubles back (a
  U-turn, a loop ramp) passes within the corridor of a point we already drove past.
  """
  delta = bearing_delta(heading_deg, bearing(lat, lon, tlat, tlon))
  if delta > tolerance_deg:
    return False

  if corridor_m is not None and \
     haversine(lat, lon, tlat, tlon) * math.sin(math.radians(delta)) > corridor_m:
    return False

  if route:
    return distance_to_route(route, tlat, tlon) <= ROUTE_CORRIDOR_M

  return True
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_db -v`
Expected: PASS. 기존 테스트가 하나도 깨지지 않아야 한다 — 깨졌다면 `route=None` 경로가 예전과 달라진 것이고, 그것이 이 태스크의 유일한 실패 조건이다. (Windows에서 `test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road` 하나는 이 계획 이전부터 `PermissionError`로 실패한다. 무시한다.)

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/db.py openpilot/sunnypilot/mapd/korea/tests/test_db.py
git commit -m "feat: filter cameras and bumps by the route corridor"
```

---

## Task 7: 파라미터 등록과 설정 UI 세 곳

**Files:**
- Modify: `openpilot/common/params_keys.h:273-280`
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py`
- Modify: `openpilot/selfdrive/ui/mici/layouts/settings/toggles.py`
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml`

**Interfaces:**
- Consumes: 없음
- Produces:
  - 파라미터 `KoreaRouteApiKey` (`PERSISTENT | BACKUP, STRING, ""`)
  - 파라미터 `NavDestination` (`CLEAR_ON_MANAGER_START, STRING`)

`NavDestination`은 `athenad.py:396`이 이미 쓰고 있지만 등록된 적이 없다. 등록하면 comma connect의 목적지 전송이 같은 입력으로 동작한다. JSON 모양은 `athenad.py:390`이 정한 것을 그대로 쓴다: `{"latitude", "longitude", "place_name", "place_details"}`.

- [ ] **Step 1: 파라미터 둘을 등록한다**

`openpilot/common/params_keys.h`의 `// korea map` 블록에 알파벳 순으로 끼워 넣는다:

```c
    // korea map
    {"KoreaExternalNavEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"KoreaMapApiKey", {PERSISTENT | BACKUP, STRING, ""}},
    {"KoreaMapAutoDownload", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"KoreaRouteApiKey", {PERSISTENT | BACKUP, STRING, ""}},
    {"KoreaSpeedBumpArchSpeed", {PERSISTENT | BACKUP, INT, "25"}},        // km/h, 원호형
```

`RoadName` 옆(내비 관련)에 더한다:

```c
    // Written by athenad's setNavDestination RPC and by the external nav socket; read by
    // mapd. Cleared on every manager start on purpose -- a destination is one drive's
    // business, and resuming yesterday's would route the car somewhere nobody asked for.
    {"NavDestination", {CLEAR_ON_MANAGER_START, STRING}},
```

- [ ] **Step 2: 재빌드하고 파라미터가 살아 있는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -c "
from openpilot.common.params import Params
p = Params()
p.put(\"NavDestination\", \"{}\")
print(\"KoreaRouteApiKey:\", repr(p.get(\"KoreaRouteApiKey\", return_default=True)))
print(\"NavDestination:\", repr(p.get(\"NavDestination\")))
"'
```

Expected: `KoreaRouteApiKey: ''` 와 `NavDestination: '{}'`. `UnknownKeyName` 예외가 나면 재빌드가 안 된 것이다.

- [ ] **Step 3: raylib UI에 노출한다**

`speed_limit_settings.py`의 `self._api_key` 블록 바로 아래에 더한다:

```python
    self._route_api_key = ListItemSP(
      title=lambda: tr("Route API Key"),
      description=tr("TMAP appKey used to fetch the route to a destination sent by a companion " +
                     "navigation app. Without it, speed cameras and bumps are matched by heading " +
                     "alone, which can pick the wrong road at a fork."),
      action_item=SimpleButtonActionSP(
        button_text=lambda: tr("Change") if ui_state.params.get("KoreaRouteApiKey") else tr("Set"),
        callback=self._edit_route_api_key,
      ))
```

`_edit_api_key`(`speed_limit_settings.py:178`, `@staticmethod`) 바로 아래에 더한다:

```python
  @staticmethod
  def _edit_route_api_key():
    InputDialogSP(
      title=tr("Route API Key"),
      sub_title=tr("TMAP appKey"),
      current_text=ui_state.params.get("KoreaRouteApiKey") or "",
      param="KoreaRouteApiKey",
      password_mode=True,
    ).show()
```

그리고 `self._api_key`가 들어 있는 아이템 리스트에서 바로 뒤에 `self._route_api_key`를 더한다.

- [ ] **Step 4: mici UI에 노출한다**

`toggles.py`의 `has_key = bool(...)` 블록(`toggles.py:83-85`) 바로 아래에 더한다. 빈 문자열 확인 콜백은 `api_key_callback`과 같은 이유로 같은 모양이다 — mici 다이얼로그에는 마스킹이 없어 빈 값을 저장하면 살아 있는 키가 조용히 지워진다:

```python
    def route_api_key_callback(text):
      # Same no-op-on-empty rule as api_key_callback above, for the same reason: this
      # dialog seeds empty, so a bare confirm would otherwise wipe a live credential.
      if not text and ui_state.params.get("KoreaRouteApiKey"):
        return
      ui_state.params.put("KoreaRouteApiKey", text)
      self._route_api_key_btn.set_value("Set" if text else "Not set")

    def edit_route_api_key():
      gui_app.push_widget(BigInputDialog("enter TMAP appKey...",
                                         "",
                                         minimum_length=0,
                                         confirm_callback=route_api_key_callback))

    has_route_key = bool(ui_state.params.get("KoreaRouteApiKey"))
    self._route_api_key_btn = BigButton("route API key", "Set" if has_route_key else "Not set")
    self._route_api_key_btn.set_click_callback(edit_route_api_key)
```

`self._scroller.add_widgets([...])` 리스트에서 `self._api_key_btn` 바로 뒤에 `self._route_api_key_btn`을 더한다.

`set_enabled` 게이트(`toggles.py:135`)를 `_api_key_btn`과 똑같이 건다:

```python
    self._route_api_key_btn.set_enabled(
      lambda: ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea)
```

`_refresh_toggles`에는 **넣지 않는다.** 그 튜플은 BOOL 토글만 담고 있고(`toggles.py:104-114`), `_api_key_btn`도 들어 있지 않다. 버튼의 라벨은 콜백이 직접 갱신한다.

- [ ] **Step 5: sunnylink YAML에 노출하고 재생성한다**

`openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml`의 `items` 끝에 더한다:

```yaml
  - key: KoreaRouteApiKey
    widget: text
    secret: true
    max_length: 255
    requires_attestation: true
    title: Route API Key
    description: TMAP appKey used to fetch the route to a destination sent by a companion
      navigation app. Without it, speed cameras and bumps are matched by heading alone,
      which can pick the wrong road at a fork.
```

`settings_ui.json`을 손으로 고치지 말고 생성한다:

```bash
python openpilot/sunnypilot/sunnylink/settings_ui_src/compile_settings_ui.py
```

- [ ] **Step 6: UI 테스트를 돌린다**

```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/ui sp-build:/work/openpilot/selfdrive/
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.selfdrive.ui.mici.tests.test_toggles_api_key openpilot.selfdrive.ui.tests.test_speed_limit_settings_korea_gating -v'
```

Expected: PASS

- [ ] **Step 7: 커밋한다**

```bash
git add openpilot/common/params_keys.h openpilot/selfdrive/ui openpilot/sunnypilot/sunnylink
git commit -m "feat: add the route API key param and expose it on all three settings surfaces"
```

---

## Task 8: 배선 — 경로 스레드, 회랑 적용, 목표속도 병합

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/route.py`
- Modify: `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py:120-219`
- Modify: `openpilot/sunnypilot/mapd/mapd_manager.py:163-250`
- Test: `openpilot/sunnypilot/mapd/tests/test_korea_map_data.py`

**Interfaces:**
- Consumes: Task 1–7 전부
- Produces:
  - `RouteSource(clock=time.monotonic)` (in `route.py`) — `.start()`, `.stop()`, `.latest() -> list[tuple[float, float]]`, `.set_position(lat, lon)`
  - `KoreaMapData.route` — 현재 폴리라인, 없으면 `[]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_korea_map_data.py`에 붙인다. 이 파일의 `make_bump_data` 헬퍼를 쓴다 — `publish_bump_target`을 겨냥해 만들어진 것이고, 이름이 바뀐 `publish_targets`가 정확히 같은 자리를 차지한다. `StubMemParams`에는 `get`이 없으므로 `.values[...]`로 읽는다.

파일 상단 import에 더한다:

```python
import math
from openpilot.cereal import log
```

```python
class TestMapTargetVelocitiesMerge(unittest.TestCase):
  """The bump feature owns this param today. Curves have to join it, not replace it."""

  def make(self, bump=None, curve_points=(), enabled=True, localizer_valid=True):
    data = make_bump_data(bump=bump, enabled=enabled, localizer_valid=localizer_valid,
                          position=Coordinate(37.5000, 127.0200))
    data.route = []
    data.curve_points = list(curve_points)
    return data

  def read(self, data):
    data.publish_targets()
    return json.loads(data.mem_params.values["MapTargetVelocities"])

  def test_a_bump_alone_is_unchanged(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0217, kind=BUMP_ARCH, distance_m=150.))
    self.assertEqual(self.read(data), [
      {"latitude": 37.5000, "longitude": 127.0217, "velocity": 25 * CV.KPH_TO_MS},
    ])

  def test_a_curve_alone_is_published(self):
    data = self.make(curve_points=[(37.5000, 127.0234, 12.)])
    self.assertEqual(self.read(data), [
      {"latitude": 37.5000, "longitude": 127.0234, "velocity": 12.},
    ])

  def test_both_are_published_nearest_first(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0234, kind=BUMP_ARCH, distance_m=300.),
                     curve_points=[(37.5000, 127.0217, 12.)])
    self.assertEqual([p["longitude"] for p in self.read(data)], [127.0217, 127.0234])

  def test_nothing_ahead_clears_the_param(self):
    self.assertEqual(self.read(self.make()), [])

  def test_a_disabled_bump_does_not_remove_the_curve(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0234, kind=BUMP_ARCH, distance_m=300.),
                     curve_points=[(37.5000, 127.0217, 12.)], enabled=False)
    self.assertEqual(self.read(data), [
      {"latitude": 37.5000, "longitude": 127.0217, "velocity": 12.},
    ])

  def test_an_invalid_localizer_publishes_nothing(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0217, kind=BUMP_ARCH, distance_m=150.),
                     curve_points=[(37.5000, 127.0234, 12.)], localizer_valid=False)
    self.assertEqual(self.read(data), [])


class StubSM:
  """SubMaster stand-in: update() is a no-op and every key is the same location."""

  def __init__(self, llk):
    self._llk = llk

  def __getitem__(self, key):
    return self._llk

  def update(self, timeout):
    pass


def valid_llk(lat=37.5000, lon=127.0200, heading_deg=90.):
  return SimpleNamespace(
    status=log.LiveLocationKalman.Status.valid,
    gpsOK=True,
    positionGeodetic=SimpleNamespace(valid=True, value=[lat, lon, 0.]),
    calibratedOrientationNED=SimpleNamespace(value=[0., 0., math.radians(heading_deg)]),
  )


class TestRouteReachesTheLookups(unittest.TestCase):
  def test_the_route_is_handed_to_both_lookups(self):
    data = make_data()
    data.sm = StubSM(valid_llk())
    data.last_position = Coordinate(37.5000, 127.0200)
    data.last_bearing = 90.
    data.curve_points = []
    route = [(37.5000, 127.0200), (37.5000, 127.0320)]
    data.route_source = SimpleNamespace(latest=lambda: route, set_position=lambda lat, lon: None)

    seen = {}
    data.db = SimpleNamespace(
      reload_if_changed=lambda: False,
      current_link=lambda *a, **k: None,
      next_camera=lambda *a, **k: seen.update(camera=k.get("route")),
      next_bump=lambda *a, **k: seen.update(bump=k.get("route")),
    )

    data.update_location()
    self.assertEqual(seen["camera"], route)
    self.assertEqual(seen["bump"], route)

  def test_no_route_source_means_an_empty_route(self):
    data = make_data()
    data.sm = StubSM(valid_llk())
    data.last_position = Coordinate(37.5000, 127.0200)
    data.route_source = None
    data.db = None
    data.update_location()
    self.assertEqual(data.route, [])
```

- [ ] **Step 2: 실패를 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_korea_map_data -v'
```

Expected: FAIL — `AttributeError: 'KoreaMapData' object has no attribute 'publish_targets'`

- [ ] **Step 3: `RouteSource` 스레드를 쓴다**

`route.py` 끝에 붙인다. import에 `threading`을 더하고, 상수를 하나 더한다:

```python
# Close enough to be there. Generous on purpose -- the destination the phone sends is a
# POI centroid, not the kerb, and a 20 m threshold would leave the route live in a car park.
ARRIVED_M = 100.


def arrived(position: tuple[float, float] | None, destination: tuple[float, float] | None) -> bool:
  """Are we there? False when either end is unknown -- 'no position' is not 'arrived'."""
  if position is None or destination is None:
    return False
  return haversine(position[0], position[1], destination[0], destination[1]) <= ARRIVED_M
```

`test_route.py`에 더한다:

```python
class TestArrived(unittest.TestCase):
  def test_the_destination_itself_counts(self):
    self.assertTrue(arrived((37.4979, 127.0276), (37.4979, 127.0276)))

  def test_inside_the_threshold_counts(self):
    self.assertTrue(arrived((37.4979 + 0.0005, 127.0276), (37.4979, 127.0276)))  # ~55 m

  def test_outside_the_threshold_does_not(self):
    self.assertFalse(arrived((37.4979 + 0.002, 127.0276), (37.4979, 127.0276)))  # ~220 m

  def test_an_unknown_position_is_not_arrival(self):
    self.assertFalse(arrived(None, (37.4979, 127.0276)))
    self.assertFalse(arrived((37.4979, 127.0276), None))
```

```python
class RouteSource:
  """Background thread that keeps a polyline for the current destination.

  Runs off the control loop for one reason: a routing request takes seconds, and
  KoreaMapData.tick() is the 1 Hz path that decides whether to brake. Same split, and the
  same start/stop shape, as CameraRefresher.
  """

  def __init__(self, clock=time.monotonic):
    self.state = RouteState(clock=clock)
    self.budget = RequestBudget()
    self._lock = threading.Lock()
    self._position: tuple[float, float] | None = None
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None:
      return
    self._thread = threading.Thread(target=self._loop, daemon=True)
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=2.)
      self._thread = None

  def set_position(self, lat: float, lon: float) -> None:
    with self._lock:
      self._position = (lat, lon)

  def latest(self) -> list[tuple[float, float]]:
    return self.state.route

  def _destination(self, params) -> tuple[float, float] | None:
    """The destination athenad's setNavDestination RPC and the UDP socket both write."""
    raw = params.get("NavDestination")
    if not raw:
      return None
    try:
      dest = json.loads(raw)
      lat, lon = float(dest["latitude"]), float(dest["longitude"])
    except (ValueError, TypeError, KeyError):
      return None
    return (lat, lon) if in_korea(lat, lon) else None

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is what
    # lets the tests run under a bare interpreter -- same as CameraRefresher._loop
    from openpilot.common.params import Params

    params = Params()
    while not self._stop.is_set():
      try:
        with self._lock:
          position = self._position
        destination = self._destination(params)

        if arrived(position, destination):
          # Arrived. Clearing here rather than waiting for the phone means a driver who
          # closes the nav app on the kerb does not keep a route that now points behind
          # them -- and the reroute machinery would otherwise fire on every tick.
          params.remove("NavDestination")
          destination = None

        if destination is None or position is None:
          self.state.set_route([])
        elif self.state.update(*position):
          api_key = params.get("KoreaRouteApiKey", return_default=True) or ""
          route = fetch_route(api_key, position, destination, budget=self.budget)
          self.state.note_request()
          if route:
            self.state.set_route(route)
      except Exception:
        # This thread dying disables the feature silently until the next reboot, and
        # nothing it does is worth that. Keep going and try again next second.
        LOG.exception("route: source loop error")

      self._stop.wait(1.)
```

- [ ] **Step 4: `korea_map_data.py`를 배선한다**

import에 더한다:

```python
from openpilot.sunnypilot.mapd.korea.route import CURVE_HORIZON_M, RouteSource, curve_targets
```

`__init__` 시그니처와 본문에 더한다:

```python
  def __init__(self, cameras_path: str = KOREA_CAMERAS_PATH, links_path: str = KOREA_LINKS_PATH,
               bumps_path: str = KOREA_BUMPS_PATH, external: ExternalNavSource | None = None,
               route_source: RouteSource | None = None):
    ...
    self.route_source = route_source
    self.route: list[tuple[float, float]] = []
    self.curve_points: list[tuple[float, float, float]] = []
```

`update_location`의 조회 블록을 바꾼다:

```python
    self.route = self.route_source.latest() if self.route_source is not None else []

    lat, lon = self.last_position.latitude, self.last_position.longitude
    if self.route_source is not None and self.localizer_valid:
      self.route_source.set_position(lat, lon)
    try:
      self.link = self.db.current_link(lat, lon, self.last_bearing)
      self.camera = self.db.next_camera(lat, lon, self.last_bearing, route=self.route)
      self.bump = self.db.next_bump(lat, lon, self.last_bearing, route=self.route)
    except Exception:
```

`nav()` 아래에 목적지 중계를 더한다:

```python
  def update_destination(self) -> None:
    """Copy a destination from the socket into the param the route thread reads.

    The socket's TTL is 5 s (external_source.py:101) and a destination is sent once, so it
    cannot live there. The param is the one place both writers -- this socket and athenad's
    setNavDestination RPC -- agree on, which is why the JSON shape is athenad's.
    """
    nav = self.nav()
    if nav is None:
      return
    if nav.destination is None:
      if self.params.get("NavDestination"):
        self.params.remove("NavDestination")
      return
    self.params.put("NavDestination", json.dumps({
      "latitude": nav.destination[0], "longitude": nav.destination[1],
      "place_name": nav.road_name or None, "place_details": None,
    }))
```

`publish_bump_target`을 `publish_targets`로 바꾼다:

```python
  def publish_targets(self) -> None:
    """Hand the next bump AND the curves ahead to SmartCruiseControlMap.

    One writer, not two: SCC-Map reads the whole list from this one param, so a second
    writer would delete the first one's points every tick. The bump feature shipped first
    and owned the param alone -- it now shares it.

    Requires localizer_valid for the same reason it always did: last_position only updates
    while the localizer is valid, so a stalled localizer would republish a frozen point
    forever and the slowdown would never release.
    """
    points: list[tuple[float, float, float]] = []
    if self.localizer_valid and self.last_position is not None:
      if self.bump_enabled and self.bump is not None:
        target = self.bump_targets.get(self.bump.kind, 0.)
        if target > 0.:
          points.append((self.bump.lat, self.bump.lon, target))
      points.extend(self.curve_points)
      points.sort(key=lambda p: self.last_position.distance_to(Coordinate(p[0], p[1])))

    self.mem_params.put("MapTargetVelocities", json.dumps(
      [{"latitude": lat, "longitude": lon, "velocity": velocity} for lat, lon, velocity in points]))
    if self.last_position is not None:
      self.mem_params.put("LastGPSPosition", json.dumps(self.last_position.as_dict()))
```

`tick`을 바꾼다:

```python
  def tick(self) -> None:
    self.read_bump_params()
    self.update_destination()
    super().tick()
    self.curve_points = []
    if self.route and self.last_position is not None and self.localizer_valid:
      # The road's own limit, not the set speed: mapd never sees the set speed, and
      # SmartCruiseControlMap already refuses to act on a target above it
      # (map_controller.py:234). A curve target above the posted limit is noise either way.
      v_max = self.get_current_speed_limit() or MAX_SPEED_LIMIT
      self.curve_points = curve_targets(self.route, self.last_position.latitude,
                                        self.last_position.longitude, v_max)
    self.publish_targets()
```

`MAX_SPEED_LIMIT`을 `base_map_data`에서 import한다.

`close()`의 `MapTargetVelocities` 초기화는 그대로 둔다.

- [ ] **Step 5: `mapd_manager.py`에서 스레드를 띄우고 내린다**

import에 더한다:

```python
from openpilot.sunnypilot.mapd.korea.route import RouteSource
```

`korea_main()`의 `live_map_sp = None` 옆에 `route_source = None`을 더하고, `external` 블록 바로 아래에 붙인다:

```python
    if external_nav:
      route_source = RouteSource()
      route_source.start()

    live_map_sp = KoreaMapData(external=external, route_source=route_source)
```

`finally` 블록에서 `external.stop()` 바로 뒤에 더한다:

```python
    if route_source is not None:
      route_source.stop()
```

경로 기능은 `KoreaExternalNavEnabled`를 따라간다 — 목적지가 그 소켓으로만 오므로 소켓이 꺼져 있으면 경로도 있을 수 없다. 별도 토글은 사용자가 둘 다 켜야 하는 함정만 만든다.

- [ ] **Step 6: 테스트를 돌린다**

```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_korea_map_data openpilot.sunnypilot.mapd.tests.test_mapd_process -v'
cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_route openpilot.sunnypilot.mapd.korea.tests.test_external_source openpilot.sunnypilot.mapd.korea.tests.test_db -v
```

Expected: 전부 PASS. 기존 방지턱 테스트가 `publish_bump_target`을 이름으로 부르고 있으면 `publish_targets`로 고친다 — 이름이 바뀐 것은 책임이 늘었기 때문이고, 옛 이름을 별칭으로 남기면 두 번째 writer가 생길 여지를 남긴다.

- [ ] **Step 7: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd
git commit -m "feat: fetch routes on a thread and merge curve targets with bumps"
```

- [ ] **Step 8: 실차 전 확인**

실제 TMAP 키로 응답 모양을 한 번 확인한다. 이 계획의 픽스처는 문서화된 GeoJSON 구조를 따르지만, 실제 응답의 feature 순서와 `properties`는 확인해본 적이 없다:

```bash
cd E:/dev/sunnypilot && python -c "
from openpilot.sunnypilot.mapd.korea.route import fetch_route
route = fetch_route('YOUR_APPKEY', (37.5665, 126.9780), (37.4979, 127.0276))
print(len(route), route[:3], route[-3:])
"
```

Expected: 수백 개의 점이 서울시청에서 강남역까지 이어진다. 0이 나오면 `parse_route`를 실제 응답에 맞춘다 — 이 태스크가 끝나기 전에 고친다.

`standalone.py`로 좌표를 먹여 차 없이 전체 경로를 돌린다:

```bash
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m openpilot.sunnypilot.mapd.live_map_data.standalone'
```

---

## 완료 기준

- [ ] `python -m unittest discover openpilot/sunnypilot/mapd/korea/tests` 가 통과한다 (Windows의 기존 `PermissionError` 하나 제외)
- [ ] `mapd/tests/` 가 `sp-build`에서 통과한다
- [ ] 목적지 없이 주행할 때 카메라·방지턱 동작이 이 계획 이전과 동일하다
- [ ] `KoreaRouteApiKey`가 설정 화면 세 곳에 모두 보인다
- [ ] 실제 키로 `fetch_route`가 서울시청 → 강남역 폴리라인을 돌려준다
