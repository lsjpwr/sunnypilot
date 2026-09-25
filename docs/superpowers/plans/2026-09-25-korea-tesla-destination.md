# 테슬라 내비 목적지 수신 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 콤마 기기가 테슬라 Fleet API로 차 내비의 목적지를 읽어 `NavDestination`에 넣는다. 기존 `RouteSource`가 그걸 받아 TMap 경로를 만든다.

**Architecture:** 새 모듈 `korea/tesla.py`가 HTTP·토큰·응답 검증과 폴링 스레드(`TeslaDestinationSource`)를 가진다. 스레드는 `mapd_manager.korea_main`에서 `RouteSource`와 같은 토글로 시작·정지한다. 인증값 3개는 PC에서 한 번 돌리는 `korea/tesla_setup.py`가 SSH로 기기 파라미터에 쓴다. 경로·카메라 코드는 손대지 않는다.

**Tech Stack:** Python 3.12 표준 라이브러리(`urllib`, `json`, `threading`), `unittest`, openpilot `Params`·`cereal.messaging`·`set_offroad_alert`(스레드 안에서만 import), C++ `params_keys.h`, Tesla Fleet API.

**Spec:** `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`

## Global Constraints

- 신규 런타임 의존성 0. HTTP는 `urllib.request`만 쓴다.
- `openpilot/sunnypilot/mapd/korea/` 하위는 모듈 최상단에서 openpilot 스택(`cereal`, `openpilot.common.params`, `cloudlog`, `openpilot.selfdrive.selfdrived.alertmanager`)을 import하지 않는다. 스레드 함수(`_loop`)와 스크립트의 `main()` 안에서만 허용한다.
- Python 들여쓰기 2칸, ruff line-length 160. 신규 파일은 sunnypilot MIT 헤더로 시작한다.
- 새 파라미터는 `params_keys.h`에 등록하고 컨테이너에서 `scons -j8 openpilot/common`으로 재빌드해야 `Params().get()`이 동작한다.
- 실제 `client_id`, `client_secret`, 토큰, VIN은 저장소·문서·로그·커밋 어디에도 넣지 않는다. 테스트는 `CLIENT`, `REFRESH-1`, `ACCESS-2`, `VIN123` 같은 눈에 띄는 가짜 값만 쓴다.
- 비밀값은 HTTP 헤더와 POST 본문에만 싣고 URL에는 넣지 않는다. 토큰, 응답 본문, 위치는 로그에 남기지 않는다. 테슬라 요청이 실패하면 HTTP 상태코드나 예외 타입만 남긴다(`_loop`의 최후 방어 `LOG.exception`은 `RouteSource._loop`과 같은 패턴이고, 요청 데이터를 담지 않는다).
- 테슬라 엔드포인트(한국이 속한 지역): API `https://fleet-api.prd.na.vn.cloud.tesla.com`, 토큰 `https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token`, 로그인 `https://auth.tesla.com/oauth2/v3/authorize`.
- 권한: `openid offline_access vehicle_device_data vehicle_location`. 명령 권한은 요청하지 않는다.
- 값: `POLL_INTERVAL_S = 60.`, `RATE_LIMIT_PAUSE_S = 300.`, `DAILY_REQUEST_CAP = 300`, `SAME_DESTINATION_M = 50.`, `MAX_PLACE_NAME = 64`.
- 파라미터: `KoreaTeslaClientId`, `KoreaTeslaRefreshToken`, `KoreaTeslaVin`은 `{PERSISTENT, STRING, ""}`(`BACKUP` 없음). `Offroad_KoreaTeslaAuth`는 `{CLEAR_ON_MANAGER_START, JSON}`.
- 알림 문구(`severity` 0): `Tesla connection lost. Destinations set in the car's navigation are not received until tesla_setup is run again.`
- `NavDestination` 형식은 athenad와 같다: `{"latitude", "longitude", "place_name", "place_details"}`.
- 테스트는 `unittest`(pytest 아님).
- 커밋 메시지는 영어이고, 끝에 아래 두 줄을 붙인다:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
  ```
- 푸시는 이 계획의 범위가 아니다. 푸시하기 전에는 반드시 `bash .superpowers/sdd/scan-api-key.sh`의 모든 개수가 0이어야 한다. 키 파일 `E:/dev/korea_map_data/data_go_kr.key`는 열거나 출력하지 않는다.

## 테스트 환경 (이 호스트)

- **호스트 (stdlib 테스트, `korea/tests/`):**
  ```bash
  cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v
  ```
  알려진 윈도우 전용 실패 1건(`test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road`의 `PermissionError`)은 이 계획과 무관하다.
- **컨테이너 `sp-build` (cereal이 필요한 테스트, 린트):** bind-mount가 깨져 있으니 소스를 `docker cp`로 밀어 넣는다. Git Bash에서는 `MSYS_NO_PATHCONV=1`을 붙인다.
  ```bash
  cd /e/dev/sunnypilot
  MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
  MSYS_NO_PATHCONV=1 docker cp pyproject.toml sp-build:/work/pyproject.toml
  docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd'
  ```
- 컨테이너에는 pytest가 없다. `unittest`만 돈다.

## File Structure

| 파일 | 책임 |
|---|---|
| `openpilot/sunnypilot/mapd/korea/tesla.py` (신규) | 테슬라 HTTP, 토큰 갱신, 응답 검증, 폴링 스레드 `TeslaDestinationSource` |
| `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py` (신규) | 위 모듈 테스트. 가짜 opener, 시계, params |
| `openpilot/sunnypilot/mapd/korea/tesla_setup.py` (신규) | PC에서 한 번 실행하는 설정 CLI |
| `openpilot/sunnypilot/mapd/korea/tests/test_tesla_setup.py` (신규) | 설정 CLI의 순수 함수 테스트 |
| `openpilot/sunnypilot/mapd/mapd_manager.py` | 스레드 시작·정지 배선 |
| `openpilot/sunnypilot/mapd/tests/test_mapd_source.py` | 배선 테스트 |
| `openpilot/common/params_keys.h` | 파라미터 4개 |
| `openpilot/selfdrive/selfdrived/alerts_offroad.json` | 연결 끊김 알림 |
| `openpilot/sunnypilot/mapd/korea/route.py`, `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py` | `NavDestination`을 쓰는 쪽 목록을 설명하는 주석만 |
| `docs/korea_tesla_destination.md` (신규) | 1회 설정 안내 |

---

### Task 1: vehicle_data 응답 읽기와 토큰·조회 HTTP

**Files:**
- Create: `openpilot/sunnypilot/mapd/korea/tesla.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py`

**Interfaces:**
- Consumes: `openpilot.sunnypilot.mapd.korea.route.in_korea(lat, lon) -> bool`, 테스트용 `openpilot.sunnypilot.mapd.korea.tests.test_route.FakeResponse(payload)`
- Produces:
  - 상수 `FLEET_API_URL: str`, `TOKEN_URL: str`, `HTTP_TIMEOUT_S: float`, `VEHICLE_DATA_QUERY: str`, `MAX_PLACE_NAME: int`
  - `class AuthRejected(Exception)`
  - `parse_destination(payload) -> tuple[float, float, str] | None`
  - `token_request(fields: dict[str, str]) -> urllib.request.Request`
  - `api_request(access_token: str, path: str) -> urllib.request.Request`
  - `refresh_access_token(client_id: str, refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]`: (접근 토큰, 새 갱신 토큰)
  - `fetch_destination(access_token: str, vin: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None`
  - 테스트 헬퍼 `GANGNAM`, `CITY_HALL`, `car()`, `NO_ROUTE`, `TOKENS`, `http_error()`, `ScriptedOpener`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`openpilot/sunnypilot/mapd/korea/tests/test_tesla.py`:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import unittest
import urllib.error

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.tesla import TOKEN_URL, AuthRejected, fetch_destination, parse_destination, refresh_access_token
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeResponse

GANGNAM = (37.4979, 127.0276)
CITY_HALL = (37.5665, 126.9780)


def car(lat=GANGNAM[0], lon=GANGNAM[1], name="강남역"):
  """vehicle_data the way Tesla answers it with a route active, trimmed to what matters."""
  return {"response": {"drive_state": {"active_route_destination": name, "active_route_latitude": lat,
                                       "active_route_longitude": lon, "shift_state": "D"}}}


NO_ROUTE = {"response": {"drive_state": {"shift_state": "D"}}}
TOKENS = {"access_token": "ACCESS-2", "refresh_token": "REFRESH-2", "expires_in": 28800, "token_type": "Bearer"}


def http_error(code):
  return urllib.error.HTTPError(tesla.FLEET_API_URL, code, "error", None, None)


class ScriptedOpener:
  """urlopen stand-in. A token request gets `token`; a vehicle_data request gets the next of
  `answers`. An exception in either place is raised instead of answered. `events` can be
  shared with a FakeParams so a test can tell which happened first."""

  def __init__(self, answers=(), token=TOKENS, events=None):
    self.answers = list(answers)
    self.token = token
    self.events = [] if events is None else events
    self.requests = []

  def __call__(self, request, timeout=None):
    self.requests.append(request)
    is_token = request.full_url == TOKEN_URL
    self.events.append("token" if is_token else "vehicle_data")
    answer = self.token if is_token else self.answers.pop(0)
    if isinstance(answer, Exception):
      raise answer
    return FakeResponse(answer)

  def count(self, kind):
    return self.events.count(kind)


class TestParseDestination(unittest.TestCase):
  def test_an_active_route_is_the_destination(self):
    self.assertEqual(parse_destination(car()), (GANGNAM[0], GANGNAM[1], "강남역"))

  def test_no_active_route_is_no_destination(self):
    self.assertIsNone(parse_destination(NO_ROUTE))

  def test_null_coordinates_are_no_destination(self):
    self.assertIsNone(parse_destination(car(lat=None, lon=None)))

  def test_coordinates_as_text_are_no_destination(self):
    self.assertIsNone(parse_destination(car(lat="37.4979", lon="127.0276")))

  def test_a_destination_outside_korea_is_no_destination(self):
    self.assertIsNone(parse_destination(car(lat=35.6762, lon=139.6503)))

  def test_a_long_name_is_cut(self):
    self.assertEqual(len(parse_destination(car(name="가" * 100))[2]), tesla.MAX_PLACE_NAME)

  def test_a_name_that_is_not_text_is_dropped(self):
    self.assertEqual(parse_destination(car(name=12))[2], "")

  def test_an_answer_of_the_wrong_shape_is_no_destination(self):
    for payload in ([], None, {"response": []}, {"response": {"drive_state": "x"}}):
      with self.subTest(payload=payload):
        self.assertIsNone(parse_destination(payload))


class TestRefreshAccessToken(unittest.TestCase):
  def test_returns_the_access_token_and_its_replacement(self):
    self.assertEqual(refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener()), ("ACCESS-2", "REFRESH-2"))

  def test_the_refresh_token_travels_in_the_body_only(self):
    opener = ScriptedOpener()
    refresh_access_token("CLIENT", "REFRESH-1", opener)
    request = opener.requests[0]
    self.assertNotIn("REFRESH-1", request.full_url)
    self.assertIn("grant_type=refresh_token", request.data.decode())
    self.assertIn("refresh_token=REFRESH-1", request.data.decode())

  def test_a_refused_token_is_auth_rejected(self):
    for code in (400, 401):
      with self.subTest(code=code), self.assertRaises(AuthRejected):
        refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener(token=http_error(code)))

  def test_a_server_error_is_not_a_rejection(self):
    """A 5xx says nothing about the token. Calling it a rejection would stop polling until the
    owner re-ran the setup, for an outage on Tesla's side."""
    with self.assertRaises(urllib.error.HTTPError):
      refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener(token=http_error(503)))

  def test_an_answer_without_tokens_is_an_error(self):
    with self.assertRaises(ValueError):
      refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener(token={"access_token": "ACCESS-2"}))


class TestFetchDestination(unittest.TestCase):
  def test_reads_the_destination_with_a_bearer_header(self):
    opener = ScriptedOpener([car()])
    self.assertEqual(fetch_destination("ACCESS-2", "VIN123", opener), (GANGNAM[0], GANGNAM[1], "강남역"))
    request = opener.requests[0]
    self.assertEqual(request.get_header("Authorization"), "Bearer ACCESS-2")
    self.assertNotIn("ACCESS-2", request.full_url)

  def test_asks_for_location_data(self):
    """Without location_data Tesla leaves the coordinates out of drive_state (fleet-telemetry#392)."""
    opener = ScriptedOpener([car()])
    fetch_destination("ACCESS-2", "VIN123", opener)
    self.assertIn("/api/1/vehicles/VIN123/vehicle_data?endpoints=drive_state%3Blocation_data", opener.requests[0].full_url)

  def test_an_http_error_reaches_the_caller(self):
    with self.assertRaises(urllib.error.HTTPError):
      fetch_destination("ACCESS-2", "VIN123", ScriptedOpener([http_error(408)]))


if __name__ == "__main__":
  unittest.main()
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v`
Expected: `ModuleNotFoundError: No module named 'openpilot.sunnypilot.mapd.korea.tesla'`

- [ ] **Step 3: 최소 구현을 쓴다**

`openpilot/sunnypilot/mapd/korea/tesla.py`:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla: reads the destination the car's own navigation is guiding to, through the Tesla
Fleet API, and hands it to RouteSource through NavDestination.

The driver already sends every destination to the car (NaviToTesla on the phone), so the
car is the one place that always has it -- reading it back beats a patched phone app
sending it a second time. The grant is read-only: nothing here can command the car.

Deliberately free of openpilot imports at module level so it runs under a bare Python
interpreter -- same rule as route.py. Only _loop imports the device stack.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from openpilot.sunnypilot.mapd.korea.route import in_korea

LOG = logging.getLogger(__name__)

# Korea is served from the North America / Asia-Pacific region.
FLEET_API_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
HTTP_TIMEOUT_S = 10.
# location_data is what puts coordinates in drive_state: without it Tesla answers with
# active_route_destination alone (teslamotors/fleet-telemetry#392).
VEHICLE_DATA_QUERY = urllib.parse.urlencode({"endpoints": "drive_state;location_data"})
MAX_PLACE_NAME = 64  # same cut as external_source.MAX_ROAD_NAME


class AuthRejected(Exception):
  """Tesla refused the refresh token. Only tesla_setup can get a new one."""


def parse_destination(payload) -> tuple[float, float, str] | None:
  """(lat, lon, name) of the car's active route; None for no route and for anything that
  does not look like one. It becomes a route that reaches longitudinal control, so it is
  untrusted the same way external_source.py treats a datagram."""
  response = payload.get("response") if isinstance(payload, dict) else None
  drive_state = response.get("drive_state") if isinstance(response, dict) else None
  if not isinstance(drive_state, dict):
    return None
  lat, lon = drive_state.get("active_route_latitude"), drive_state.get("active_route_longitude")
  if not all(isinstance(v, int | float) for v in (lat, lon)) or not in_korea(lat, lon):
    return None
  name = drive_state.get("active_route_destination")
  return float(lat), float(lon), name[:MAX_PLACE_NAME] if isinstance(name, str) else ""


def _read_json(request: urllib.request.Request, opener) -> dict:
  with opener(request, timeout=HTTP_TIMEOUT_S) as response:
    payload = json.loads(response.read())
  if not isinstance(payload, dict):
    raise ValueError("answer is not a JSON object")
  return payload


def token_request(fields: dict[str, str]) -> urllib.request.Request:
  """Secrets travel in the form body, never the URL: urllib puts the URL in exception
  messages, and those reach cloudlog -- route.py:125 makes the same promise."""
  return urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(fields).encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")


def api_request(access_token: str, path: str) -> urllib.request.Request:
  return urllib.request.Request(FLEET_API_URL + path, headers={"Authorization": f"Bearer {access_token}"})


def refresh_access_token(client_id: str, refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]:
  """(access token, the refresh token that replaces this one).

  Tesla's refresh tokens are single use, so the caller must save the second value before
  anything else can fail. AuthRejected when Tesla refuses the token; any other failure is
  raised as it is -- a 5xx says nothing about the token.
  """
  request = token_request({"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token})
  try:
    payload = _read_json(request, opener)
  except urllib.error.HTTPError as e:
    if e.code in (400, 401):
      raise AuthRejected(f"HTTP {e.code}") from None
    raise
  access, replacement = payload.get("access_token"), payload.get("refresh_token")
  if not (isinstance(access, str) and access and isinstance(replacement, str) and replacement):
    raise ValueError("token answer without tokens")
  return access, replacement


def fetch_destination(access_token: str, vin: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None:
  """One billed vehicle_data request. An HTTPError is raised as it is: its status decides
  what the caller does next."""
  path = f"/api/1/vehicles/{urllib.parse.quote(vin, safe='')}/vehicle_data?{VEHICLE_DATA_QUERY}"
  return parse_destination(_read_json(api_request(access_token, path), opener))
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v`
Expected: `Ran 16 tests ... OK`

- [ ] **Step 5: 린트**

Run:
```bash
cd /e/dev/sunnypilot
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
MSYS_NO_PATHCONV=1 docker cp pyproject.toml sp-build:/work/pyproject.toml
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd'
```
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
cd /e/dev/sunnypilot
git add openpilot/sunnypilot/mapd/korea/tesla.py openpilot/sunnypilot/mapd/korea/tests/test_tesla.py
git commit -F - <<'EOF'
feat: read the car's navigation destination from a Tesla vehicle_data answer

parse_destination turns drive_state's active_route_* fields into a checked
(lat, lon, name), and the two HTTP helpers refresh the single-use token and
ask for vehicle_data with location_data. Secrets ride in headers and form
bodies only, never the URL.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 2: 폴링 조건과 NavDestination 쓰기 규칙

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/tesla.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py`

**Interfaces:**
- Consumes: Task 1의 `refresh_access_token`, `fetch_destination`, `LOG`. `route.RequestBudget(cap, clock=time.time)`의 `.allow() -> bool`, `.spend()`, `.cap`. `geo.haversine(lat1, lon1, lat2, lon2) -> float`(미터). 테스트용 `test_route.FakeClock(now)`(`.now`를 바꿀 수 있는 호출형 시계).
- Produces:
  - 상수 `POLL_INTERVAL_S = 60.`, `DAILY_REQUEST_CAP = 300`, `SAME_DESTINATION_M = 50.`
  - `TeslaDestinationSource(opener=urllib.request.urlopen, clock=time.monotonic)`: 속성 `.budget`(`RequestBudget`), 메서드 `.step(params, started: bool, set_alert) -> None`. `params`는 `get(key)`, `put(key, value)`, `remove(key)`를 가진 객체이고, `set_alert`는 `bool` 하나를 받는 함수다(Task 3부터 쓴다).
  - 테스트 헬퍼 `CREDENTIALS`, `FakeParams`, `SourceTestCase`

이 태스크의 `step`에는 예외 처리가 없다. 실패 처리는 Task 3에서 넣는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_tesla.py`의 import 블록을 아래로 바꾼다:

```python
import json
import unittest
import urllib.error

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.route import RequestBudget
from openpilot.sunnypilot.mapd.korea.tesla import (POLL_INTERVAL_S, TOKEN_URL, AuthRejected, TeslaDestinationSource, fetch_destination,
                                                   parse_destination, refresh_access_token)
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeClock, FakeResponse
```

`ScriptedOpener` 클래스 바로 뒤에 넣는다:

```python
CREDENTIALS = {"KoreaTeslaClientId": "CLIENT", "KoreaTeslaRefreshToken": "REFRESH-1", "KoreaTeslaVin": "VIN123",
               "KoreaRouteApiKey": "TMAP"}


class FakeParams:
  """Params stand-in over a plain dict. Writes and removals land in `events`, next to the
  opener's requests, so a test can check what happened in which order."""

  def __init__(self, values=None, events=None):
    self.values = dict(CREDENTIALS if values is None else values)
    self.events = [] if events is None else events

  def get(self, key, return_default=False):
    return self.values.get(key)

  def put(self, key, value):
    self.events.append(f"put {key}")
    self.values[key] = value

  def remove(self, key):
    self.events.append(f"remove {key}")
    self.values.pop(key, None)
```

파일 끝의 `if __name__ == "__main__":` 앞에 넣는다:

```python
class SourceTestCase(unittest.TestCase):
  """Drives TeslaDestinationSource.step() one tick at a time on a fake clock."""

  def setUp(self):
    super().setUp()
    self.events = []
    self.clock = FakeClock(1000.)
    self.params = FakeParams(events=self.events)
    self.alerts = []

  def source(self, answers=(), token=TOKENS):
    self.opener = ScriptedOpener(answers, token, self.events)
    return TeslaDestinationSource(opener=self.opener, clock=self.clock)

  def tick(self, source, started=True, seconds=0.):
    self.clock.now += seconds
    source.step(self.params, started, self.alerts.append)

  def nav(self):
    raw = self.params.values.get("NavDestination")
    return None if raw is None else json.loads(raw)


class TestPollingConditions(SourceTestCase):
  def test_parked_asks_nothing(self):
    source = self.source([car()])
    self.tick(source, started=False)
    self.assertEqual(self.opener.requests, [])

  def test_missing_setup_asks_nothing(self):
    for key in ("KoreaTeslaClientId", "KoreaTeslaRefreshToken", "KoreaTeslaVin"):
      with self.subTest(missing=key):
        self.params = FakeParams({k: v for k, v in CREDENTIALS.items() if k != key}, self.events)
        source = self.source([car()])
        self.tick(source)
        self.assertEqual(self.opener.requests, [])

  def test_no_tmap_key_asks_nothing(self):
    """Without it a destination buys no route, only a bill."""
    self.params.values.pop("KoreaRouteApiKey")
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.opener.requests, [])

  def test_asks_once_a_minute(self):
    source = self.source([car(), car()])
    self.tick(source)
    for _ in range(int(POLL_INTERVAL_S) - 1):
      self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_a_new_drive_asks_at_once(self):
    """The first poll of a drive does not wait out the last drive's minute."""
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, started=False, seconds=10.)
    self.tick(source, seconds=10.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_the_daily_cap_stops_asking(self):
    source = self.source([car(), car()])
    source.budget = RequestBudget(cap=1, clock=FakeClock())
    self.tick(source)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_the_daily_cap_is_300(self):
    """Five hours of driving at one request a minute (spec, 비용 절)."""
    self.assertEqual(TeslaDestinationSource().budget.cap, 300)


class TestWriteRules(SourceTestCase):
  def test_the_cars_destination_is_written(self):
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.nav(), {"latitude": GANGNAM[0], "longitude": GANGNAM[1], "place_name": "강남역", "place_details": None})

  def test_the_same_destination_is_not_written_back_after_arrival(self):
    """RouteSource clears it on arrival while the car still shows the route. Writing it back
    would only have RouteSource clear it again, every minute, for as long as the car is parked."""
    source = self.source([car(), car()])
    self.tick(source)
    self.params.remove("NavDestination")  # RouteSource: arrived
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(self.nav())

  def test_a_jitter_under_50_m_is_the_same_destination(self):
    source = self.source([car(), car(lat=GANGNAM[0] + 0.0002)])  # ~22 m north
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.events.count("put NavDestination"), 1)

  def test_a_changed_destination_is_written(self):
    source = self.source([car(), car(*CITY_HALL, name="서울시청")])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual((self.nav()["latitude"], self.nav()["longitude"]), CITY_HALL)

  def test_guidance_ended_in_the_car_clears_our_destination(self):
    source = self.source([car(), NO_ROUTE])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(self.nav())

  def test_guidance_ended_in_the_car_keeps_a_destination_it_did_not_write(self):
    """Something else -- the UDP socket, athenad -- wrote after us. Not the car's to cancel."""
    source = self.source([car(), NO_ROUTE])
    self.tick(source)
    other = json.dumps({"latitude": CITY_HALL[0], "longitude": CITY_HALL[1], "place_name": None, "place_details": None})
    self.params.values["NavDestination"] = other
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.params.values["NavDestination"], other)

  def test_a_new_drive_writes_the_same_destination_again(self):
    """The offroad transition cleared NavDestination. The car still guiding there means the
    route thread should have it again."""
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, started=False, seconds=10.)
    self.params.remove("NavDestination")  # manager: CLEAR_ON_OFFROAD_TRANSITION
    self.tick(source, seconds=10.)
    self.assertIsNotNone(self.nav())


class TestTokens(SourceTestCase):
  def test_the_new_refresh_token_is_saved_before_the_car_is_asked(self):
    """Single use: once Tesla has answered, only the new token works. Had the car's answer
    raised before the save, the device would be left holding a dead one."""
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.params.values["KoreaTeslaRefreshToken"], "REFRESH-2")
    self.assertLess(self.events.index("put KoreaTeslaRefreshToken"), self.events.index("vehicle_data"))

  def test_the_access_token_is_kept_between_polls(self):
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("token"), 1)
    self.assertEqual(self.opener.requests[-1].get_header("Authorization"), "Bearer ACCESS-2")
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v`
Expected: `ImportError: cannot import name 'POLL_INTERVAL_S' from 'openpilot.sunnypilot.mapd.korea.tesla'`

- [ ] **Step 3: 구현한다**

`tesla.py`의 import 블록을 아래로 바꾼다:

```python
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from openpilot.sunnypilot.mapd.korea.geo import haversine
from openpilot.sunnypilot.mapd.korea.route import RequestBudget, in_korea
```

`MAX_PLACE_NAME = 64 ...` 줄 바로 뒤에 넣는다:

```python

POLL_INTERVAL_S = 60.
# Five hours of driving at one request a minute. Tesla bills 500 data requests per $1 and
# takes $10 a month off, so this guards against a runaway, not against normal use.
DAILY_REQUEST_CAP = 300
# Closer than this is the same destination. Rewriting one that only jittered would have
# RouteSource drop its route and ask TMAP again (route.py:402).
SAME_DESTINATION_M = 50.
```

파일 끝에 넣는다:

```python


def _is_ours(raw, written: tuple[float, float]) -> bool:
  """Does NavDestination still hold exactly what this thread wrote? json round-trips a
  float exactly, so equality is right here -- unlike between two readings of the car."""
  try:
    dest = json.loads(raw)
    return (float(dest["latitude"]), float(dest["longitude"])) == written
  except (TypeError, ValueError, KeyError):
    return False


class TeslaDestinationSource:
  """Copies the car's navigation destination into NavDestination while the car is on.

  step() makes every decision and gets the device state as arguments, so the tests drive
  it with no device stack.
  """

  def __init__(self, opener=urllib.request.urlopen, clock=time.monotonic):
    self._opener = opener
    self._clock = clock
    self.budget = RequestBudget(cap=DAILY_REQUEST_CAP)
    self._access_token: str | None = None
    # What this thread last wrote, so the car repeating it is not another write.
    self._written: tuple[float, float] | None = None
    self._next_poll = 0.
    self._was_started = False

  def step(self, params, started: bool, set_alert) -> None:
    """One tick. set_alert(bool) shows or clears Offroad_KoreaTeslaAuth."""
    now = self._clock()
    if started and not self._was_started:
      # A new drive. The offroad transition cleared NavDestination, so what this thread wrote
      # last drive is no reason to skip it now -- and the first poll of a drive does not wait
      # out the last drive's minute.
      self._written = None
      self._next_poll = now
    self._was_started = started
    if not started or now < self._next_poll:
      return
    self._next_poll = now + POLL_INTERVAL_S

    client_id = params.get("KoreaTeslaClientId")
    refresh_token = params.get("KoreaTeslaRefreshToken")
    vin = params.get("KoreaTeslaVin")
    # Without the TMAP key a destination buys no route, only a bill.
    if not (client_id and refresh_token and vin and params.get("KoreaRouteApiKey")):
      return
    if not self.budget.allow():
      LOG.warning("tesla: daily request cap reached")
      return

    if self._access_token is None:
      self._access_token, refresh_token = refresh_access_token(client_id, refresh_token, self._opener)
      # Single use: Tesla retired the old token the moment it answered. Save the new one
      # before anything else can fail, or nothing that works is left on the device.
      params.put("KoreaTeslaRefreshToken", refresh_token)
    self.budget.spend()
    self._apply(params, fetch_destination(self._access_token, vin, self._opener))

  def _apply(self, params, destination: tuple[float, float, str] | None) -> None:
    if destination is None:
      # Guidance ended in the car. Clear only what this thread wrote: a destination from the
      # UDP socket or athenad is not the car's to cancel.
      if self._written is not None and _is_ours(params.get("NavDestination"), self._written):
        params.remove("NavDestination")
      self._written = None
      return
    lat, lon, name = destination
    if self._written is not None and haversine(lat, lon, *self._written) <= SAME_DESTINATION_M:
      # Also the case after RouteSource cleared it on arrival: writing it back would only
      # have RouteSource clear it again, every minute, for as long as the car is parked there.
      return
    params.put("NavDestination", json.dumps({"latitude": lat, "longitude": lon,
                                             "place_name": name or None, "place_details": None}))
    self._written = (lat, lon)
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v`
Expected: `Ran 32 tests ... OK`

- [ ] **Step 5: 린트**

Task 1 Step 5와 같은 명령. Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
cd /e/dev/sunnypilot
git add openpilot/sunnypilot/mapd/korea/tesla.py openpilot/sunnypilot/mapd/korea/tests/test_tesla.py
git commit -F - <<'EOF'
feat: poll the Tesla for its destination and hand it to the route thread

TeslaDestinationSource.step asks once a minute while the car is on, only
once setup and the TMAP key are in place, under a 300-a-day cap. It writes
NavDestination when the car's destination is new, leaves a 50 m jitter or
an arrival clear alone, and clears only what it wrote when guidance ends.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 3: 실패 처리, 연결 끊김 알림, 스레드

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/tesla.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py`

**Interfaces:**
- Consumes: Task 2의 `TeslaDestinationSource`, `SourceTestCase`, `FakeParams`. 테스트용 `test_camera_refresh.OneShotStop`(`wait()`가 한 번 만에 루프를 끝내는 `threading.Event`).
- Produces:
  - 상수 `RATE_LIMIT_PAUSE_S = 300.`
  - `TeslaDestinationSource.start()`, `.stop()`, `._loop()`. `_loop`은 1초마다 `step(params, started, set_alert)`를 부르고, `set_alert(show)`는 `set_offroad_alert("Offroad_KoreaTeslaAuth", show)`다.
  - `step`은 이제 어떤 예외도 밖으로 내보내지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_tesla.py`의 import 블록을 아래로 바꾼다:

```python
import json
import sys
import types
import unittest
import urllib.error
from unittest import mock

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.route import RequestBudget
from openpilot.sunnypilot.mapd.korea.tesla import (POLL_INTERVAL_S, TOKEN_URL, AuthRejected, TeslaDestinationSource, fetch_destination,
                                                   parse_destination, refresh_access_token)
from openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh import OneShotStop
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeClock, FakeResponse
```

파일 끝의 `if __name__ == "__main__":` 앞에 넣는다(`RATE_LIMIT_PAUSE_S`는 `tesla.`로 부른다. 그래야 이 테스트들만 따로 실패하는 게 보인다):

```python
class TestFailures(SourceTestCase):
  def assert_asks_again_after(self, error, seconds):
    source = self.source([error, car()])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=seconds - 1.)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)
    self.assertIsNotNone(self.nav())

  def test_an_unavailable_car_is_asked_again_next_minute(self):
    self.assert_asks_again_after(http_error(408), POLL_INTERVAL_S)

  def test_a_server_error_is_asked_again_next_minute(self):
    self.assert_asks_again_after(http_error(503), POLL_INTERVAL_S)

  def test_a_network_error_is_asked_again_next_minute(self):
    self.assert_asks_again_after(urllib.error.URLError("no route to host"), POLL_INTERVAL_S)

  def test_too_many_requests_waits_five_minutes(self):
    self.assert_asks_again_after(http_error(429), tesla.RATE_LIMIT_PAUSE_S)


class TestAuth(SourceTestCase):
  def test_a_refused_refresh_token_raises_the_alert_and_stops_asking(self):
    source = self.source([car()], token=http_error(400))
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts, [True])
    self.assertEqual(self.opener.count("token"), 1)
    self.assertEqual(self.opener.count("vehicle_data"), 0)

  def test_a_new_token_from_setup_is_tried_and_clears_the_alert(self):
    source = self.source([car()], token=http_error(400))
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.opener.token = TOKENS
    self.params.values["KoreaTeslaRefreshToken"] = "REFRESH-FROM-SETUP"
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts, [True, False])
    self.assertIsNotNone(self.nav())

  def test_a_grant_without_location_raises_the_alert(self):
    """403: the owner took vehicle_location away, or never gave it. Asking again changes nothing."""
    source = self.source([http_error(403), car()])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts[-1], True)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_an_expired_access_token_is_refreshed_on_the_next_tick(self):
    """Usually the first poll of a drive: waiting a minute to retry would delay the route."""
    source = self.source([car(), http_error(401), car(*CITY_HALL)])
    self.tick(source)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("token"), 2)
    self.assertEqual((self.nav()["latitude"], self.nav()["longitude"]), CITY_HALL)
    self.assertNotIn(True, self.alerts)

  def test_a_token_refused_right_after_refresh_raises_the_alert(self):
    """A token minted this tick and refused anyway will not be accepted on a retry -- and
    retrying every tick would spend the day's cap in five minutes."""
    source = self.source([http_error(401), car()])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts[-1], True)
    self.assertEqual(self.opener.count("vehicle_data"), 1)


class TestLoop(unittest.TestCase):
  def run_loop(self, step, started=True, recv_frame=1, alerts=None):
    """_loop for one iteration, with the device stack faked through sys.modules -- the
    technique test_camera_refresh.TestRefresherLoop uses for CameraRefresher._loop."""
    class FakeSubMaster:
      def __init__(self, services):
        self.recv_frame = {'deviceState': recv_frame}

      def update(self, timeout):
        pass

      def __getitem__(self, service):
        return types.SimpleNamespace(started=started)

    sink = [] if alerts is None else alerts
    messaging = types.ModuleType("cereal.messaging")
    messaging.SubMaster = FakeSubMaster
    params_mod = types.ModuleType("openpilot.common.params")
    params_mod.Params = FakeParams
    alertmanager = types.ModuleType("openpilot.selfdrive.selfdrived.alertmanager")
    alertmanager.set_offroad_alert = lambda *a: sink.append(a)
    self.enterContext(mock.patch.dict(sys.modules, {
      "cereal": types.ModuleType("cereal"),
      "cereal.messaging": messaging,
      "openpilot.common.params": params_mod,
      "openpilot.selfdrive.selfdrived.alertmanager": alertmanager,
    }))
    source = TeslaDestinationSource()
    source.step = step
    source._stop = OneShotStop()
    source._loop()

  def test_started_comes_from_deviceState(self):
    seen = []
    self.run_loop(lambda params, started, set_alert: seen.append(started))
    self.assertEqual(seen, [True])

  def test_nothing_received_yet_is_not_a_drive(self):
    """A SubMaster that has received nothing reads the capnp default. Here that must mean
    "wait", never a drive that has started."""
    seen = []
    self.run_loop(lambda params, started, set_alert: seen.append(started), recv_frame=0)
    self.assertEqual(seen, [False])

  def test_the_alert_is_offroad_korea_tesla_auth(self):
    alerts = []
    self.run_loop(lambda params, started, set_alert: set_alert(True), alerts=alerts)
    self.assertEqual(alerts, [("Offroad_KoreaTeslaAuth", True)])

  def test_the_thread_survives_an_unexpected_error(self):
    """No supervisor: anything escaping _loop ends destinations from the car until the
    process restarts."""
    def boom(params, started, set_alert):
      raise RuntimeError("boom")
    with self.assertLogs(tesla.LOG, level="ERROR"):
      self.run_loop(boom)
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v`
Expected: 새 테스트 13개만 실패하거나 ERROR가 난다. `HTTPError`·`URLError`·`AuthRejected`가 `step` 밖으로 새고, `RATE_LIMIT_PAUSE_S`와 `_loop`은 `AttributeError`가 난다. Task 1·2의 테스트 32개는 그대로 통과한다.

- [ ] **Step 3: 구현한다**

`tesla.py`의 import 블록에 `import threading`을 넣는다(`import logging` 다음 줄):

```python
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
```

`POLL_INTERVAL_S = 60.` 바로 뒤에 한 줄을 넣는다:

```python
RATE_LIMIT_PAUSE_S = 300.
```

`class TeslaDestinationSource` 전체(Task 2에서 쓴 `__init__`, `step`, `_apply`)를 아래로 바꾼다. `_apply`는 그대로다:

```python
class TeslaDestinationSource:
  """Background thread that copies the car's navigation destination into NavDestination.

  Same start/stop shape as RouteSource. step() makes every decision and gets the device
  state as arguments, so the tests drive it with no device stack; _loop only feeds it.
  """

  def __init__(self, opener=urllib.request.urlopen, clock=time.monotonic):
    self._opener = opener
    self._clock = clock
    self.budget = RequestBudget(cap=DAILY_REQUEST_CAP)
    self._access_token: str | None = None
    # What this thread last wrote, so the car repeating it is not another write.
    self._written: tuple[float, float] | None = None
    # The refresh token Tesla refused. Polling waits for tesla_setup to write another one.
    self._rejected: str | None = None
    self._next_poll = 0.
    self._was_started = False
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

  def step(self, params, started: bool, set_alert) -> None:
    """One tick. set_alert(bool) shows or clears Offroad_KoreaTeslaAuth."""
    now = self._clock()
    if started and not self._was_started:
      # A new drive. The offroad transition cleared NavDestination, so what this thread wrote
      # last drive is no reason to skip it now -- and the first poll of a drive does not wait
      # out the last drive's minute.
      self._written = None
      self._next_poll = now
    self._was_started = started
    if not started or now < self._next_poll:
      return
    self._next_poll = now + POLL_INTERVAL_S

    client_id = params.get("KoreaTeslaClientId")
    refresh_token = params.get("KoreaTeslaRefreshToken")
    vin = params.get("KoreaTeslaVin")
    # Without the TMAP key a destination buys no route, only a bill.
    if not (client_id and refresh_token and vin and params.get("KoreaRouteApiKey")):
      return
    if refresh_token == self._rejected:
      return
    if not self.budget.allow():
      LOG.warning("tesla: daily request cap reached")
      return

    fresh = self._access_token is None
    try:
      if fresh:
        self._access_token, refresh_token = refresh_access_token(client_id, refresh_token, self._opener)
        # Single use: Tesla retired the old token the moment it answered. Save the new one
        # before anything else can fail, or nothing that works is left on the device.
        params.put("KoreaTeslaRefreshToken", refresh_token)
        set_alert(False)
      self.budget.spend()
      destination = fetch_destination(self._access_token, vin, self._opener)
    except AuthRejected:
      LOG.warning("tesla: refresh token rejected, run tesla_setup again")
      self._reject(refresh_token, set_alert)
      return
    except urllib.error.HTTPError as e:
      LOG.warning("tesla: request failed: HTTP %d", e.code)
      if e.code == 401 and not fresh:
        # The access token outlived its hours. Refresh on the next tick rather than a minute
        # from now -- this is usually the first poll of a drive.
        self._access_token = None
        self._next_poll = now
      elif e.code in (401, 403):
        # A token minted this tick and refused anyway, or a grant without vehicle_location:
        # asking again changes neither, and retrying every tick would spend the day's cap.
        self._reject(refresh_token, set_alert)
      elif e.code == 429:
        self._next_poll = now + RATE_LIMIT_PAUSE_S
      return
    except Exception as e:
      # The network, a timeout, a body that is not JSON: all "ask again next minute". Only the
      # type is logged -- a message can carry the URL, and a body the car's position.
      LOG.warning("tesla: request failed: %s", type(e).__name__)
      return
    self._apply(params, destination)

  def _reject(self, refresh_token: str, set_alert) -> None:
    self._rejected = refresh_token
    self._access_token = None
    set_alert(True)

  def _apply(self, params, destination: tuple[float, float, str] | None) -> None:
    if destination is None:
      # Guidance ended in the car. Clear only what this thread wrote: a destination from the
      # UDP socket or athenad is not the car's to cancel.
      if self._written is not None and _is_ours(params.get("NavDestination"), self._written):
        params.remove("NavDestination")
      self._written = None
      return
    lat, lon, name = destination
    if self._written is not None and haversine(lat, lon, *self._written) <= SAME_DESTINATION_M:
      # Also the case after RouteSource cleared it on arrival: writing it back would only
      # have RouteSource clear it again, every minute, for as long as the car is parked there.
      return
    params.put("NavDestination", json.dumps({"latitude": lat, "longitude": lon,
                                             "place_name": name or None, "place_details": None}))
    self._written = (lat, lon)

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is what
    # lets the tests run under a bare interpreter -- same as RouteSource._loop
    import cereal.messaging as messaging
    from openpilot.common.params import Params
    from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert

    params = Params()
    sm = messaging.SubMaster(['deviceState'])

    def set_alert(show: bool) -> None:
      set_offroad_alert("Offroad_KoreaTeslaAuth", show)

    while not self._stop.is_set():
      try:
        sm.update(0)
        # A SubMaster that has received nothing reads the capnp default, started=False --
        # "not yet", which is the safe answer here.
        self.step(params, bool(sm.recv_frame['deviceState']) and sm['deviceState'].started, set_alert)
      except Exception:
        # No supervisor: dying here ends destinations from the car until the process
        # restarts. Keep going and try again next second.
        LOG.exception("tesla: source loop error")
      self._stop.wait(1.)
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla -v`
Expected: `Ran 45 tests ... OK`

- [ ] **Step 5: 린트**

Task 1 Step 5와 같은 명령. Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
cd /e/dev/sunnypilot
git add openpilot/sunnypilot/mapd/korea/tesla.py openpilot/sunnypilot/mapd/korea/tests/test_tesla.py
git commit -F - <<'EOF'
feat: survive Tesla API failures and flag a lost Tesla login offroad

A refused refresh token or a 403 raises Offroad_KoreaTeslaAuth and stops
polling until tesla_setup writes a new token. An expired access token is
refreshed on the next tick, 429 waits five minutes, and anything else is
retried next minute. The thread runs step() once a second, reading started
from deviceState, and survives anything step() raises.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 4: 파라미터·알림 등록과 mapd 배선

**Files:**
- Modify: `openpilot/common/params_keys.h` (`NavDestination` 주석 257–259행, Korea 블록 289–290행)
- Modify: `openpilot/selfdrive/selfdrived/alerts_offroad.json` (`Offroad_KoreaMapMissing` 뒤)
- Modify: `openpilot/sunnypilot/mapd/mapd_manager.py` (import 26행, 변수 195행, 시작 214–216행, 정지 258–259행)
- Modify: `openpilot/sunnypilot/mapd/korea/route.py:372` (docstring 한 줄)
- Modify: `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py` (`update_destination` docstring)
- Test: `openpilot/sunnypilot/mapd/tests/test_mapd_source.py`

**Interfaces:**
- Consumes: Task 3의 `TeslaDestinationSource()`, `.start()`, `.stop()`. 테스트 하네스의 `_record(built, name, factory)`, `FakeRouteSource`(`started`/`stopped` 플래그가 있는 시작·정지 가짜).
- Produces: `KoreaExternalNavEnabled`가 켜져 있으면 `korea_main`이 `RouteSource` 바로 뒤에 `TeslaDestinationSource`를 시작하고, `finally`에서 멈춘다. 파라미터 4개와 알림 1개가 등록된다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`openpilot/sunnypilot/mapd/tests/test_mapd_source.py`의 `run_source_main` 안 `patches` 딕셔너리에서 `"RouteSource": ...` 줄 바로 뒤에 넣는다:

```python
      "TeslaDestinationSource": _record(built, "tesla_source", FakeRouteSource),  # same start/stop shape
```

`test_the_korea_loop_releases_its_resources_before_returning` 메서드 바로 뒤에 넣는다:

```python
  def test_the_korea_loop_polls_the_car_only_with_external_nav(self):
    """The Tesla thread feeds the route thread, so it follows the same toggle -- and is
    released with everything else when the loop ends, or it keeps polling (and billing)
    for a source that is gone."""
    params = FakeParams(MapSource.korea, KoreaExternalNavEnabled=True)
    built = self.run_source_main(params, "korea_main", lambda: setattr(params, "source", MapSource.osm))
    self.assertTrue(built["tesla_source"].started)
    self.assertTrue(built["tesla_source"].stopped, "the Tesla thread outlives the source that started it")

    params = FakeParams(MapSource.korea)
    built = self.run_source_main(params, "korea_main", lambda: setattr(params, "source", MapSource.osm))
    self.assertNotIn("tesla_source", built)
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run:
```bash
cd /e/dev/sunnypilot
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_mapd_source -v'
```
Expected: `run_source_main`을 쓰는 테스트가 전부 `AttributeError: <module 'openpilot.sunnypilot.mapd.mapd_manager' ...> does not have the attribute 'TeslaDestinationSource'`로 ERROR.

- [ ] **Step 3: 파라미터와 알림을 등록한다**

`openpilot/common/params_keys.h` — `NavDestination` 주석:

old:
```
    // Written by athenad's setNavDestination RPC and by the Korean external nav socket;
    // read by mapd. Cleared on manager start and when the drive ends, so a destination
    // never carries into a later drive.
```
new:
```
    // Written by athenad's setNavDestination RPC, the Korean external nav socket and the
    // Tesla destination thread (korea/tesla.py); read by mapd. Cleared on manager start and
    // when the drive ends, so a destination never carries into a later drive.
```

같은 파일의 Korea 블록:

old:
```
    {"KoreaSpeedBumpTrapezoidSpeed", {PERSISTENT | BACKUP, INT, "35"}},   // km/h, 사다리꼴형
    {"Offroad_KoreaMapMissing", {CLEAR_ON_MANAGER_START, JSON}},
```
new:
```
    {"KoreaSpeedBumpTrapezoidSpeed", {PERSISTENT | BACKUP, INT, "35"}},   // km/h, 사다리꼴형
    // Written by tesla_setup from the PC, read by korea/tesla.py. Not BACKUP: the refresh
    // token changes on every use, so a restored copy is dead, and a backup only ships a
    // credential off the device.
    {"KoreaTeslaClientId", {PERSISTENT, STRING, ""}},
    {"KoreaTeslaRefreshToken", {PERSISTENT, STRING, ""}},
    {"KoreaTeslaVin", {PERSISTENT, STRING, ""}},
    {"Offroad_KoreaMapMissing", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_KoreaTeslaAuth", {CLEAR_ON_MANAGER_START, JSON}},
```

`openpilot/selfdrive/selfdrived/alerts_offroad.json`:

old:
```json
  "Offroad_KoreaMapMissing": {
    "text": "Korean map database not found. Speed limit assist and road name display are unavailable until it is copied to the device.\n\n%1",
    "severity": 0
  },
```
new:
```json
  "Offroad_KoreaMapMissing": {
    "text": "Korean map database not found. Speed limit assist and road name display are unavailable until it is copied to the device.\n\n%1",
    "severity": 0
  },
  "Offroad_KoreaTeslaAuth": {
    "text": "Tesla connection lost. Destinations set in the car's navigation are not received until tesla_setup is run again.",
    "severity": 0
  },
```

- [ ] **Step 4: mapd에 배선한다**

`openpilot/sunnypilot/mapd/mapd_manager.py`:

old:
```python
from openpilot.sunnypilot.mapd.korea.route import RouteSource
```
new:
```python
from openpilot.sunnypilot.mapd.korea.route import RouteSource
from openpilot.sunnypilot.mapd.korea.tesla import TeslaDestinationSource
```

old:
```python
  live_map_sp = None
  route_source = None
```
new:
```python
  live_map_sp = None
  route_source = None
  tesla_source = None
```

old:
```python
    if external_nav:
      route_source = RouteSource()
      route_source.start()
```
new:
```python
    if external_nav:
      route_source = RouteSource()
      route_source.start()
      # It feeds the route thread above, so it follows the same toggle. Until tesla_setup has
      # written its credentials it asks Tesla nothing.
      tesla_source = TeslaDestinationSource()
      tesla_source.start()
```

old:
```python
    if route_source is not None:
      route_source.stop()
```
new:
```python
    if route_source is not None:
      route_source.stop()
    if tesla_source is not None:
      tesla_source.stop()
```

`NavDestination`을 쓰는 쪽 목록을 설명하는 주석 두 곳:

`openpilot/sunnypilot/mapd/korea/route.py` —
old: `    """The destination athenad's setNavDestination RPC and the UDP socket both write."""`
new: `    """The destination athenad's setNavDestination RPC, the UDP socket and korea/tesla.py write."""`

`openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py` —
old:
```
    sent once, so it cannot live there. The param is the one place both writers -- this
    socket and athenad's setNavDestination RPC -- agree on, which is why the JSON shape is
    athenad's.
```
new:
```
    sent once, so it cannot live there. The param is the one place every writer -- this
    socket, athenad's setNavDestination RPC and korea/tesla.py -- agrees on, which is why the
    JSON shape is athenad's.
```

- [ ] **Step 5: 재빌드하고 테스트가 통과하는지 확인한다**

Run:
```bash
cd /e/dev/sunnypilot
MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/selfdrived/alerts_offroad.json sp-build:/work/openpilot/selfdrive/selfdrived/alerts_offroad.json
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_mapd_source -v'
```
Expected: scons는 `scons: done building targets.`, 테스트는 `OK`.

키 등록과 알림을 확인한다(컨테이너에 pytest가 없어 `test_alerts.py` 대신 쓴다):

```bash
docker exec -i sp-build bash -lc 'cd /work && ./.venv/bin/python -' <<'PY'
from openpilot.common.params import Params
from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert
p = Params()
for key in ("KoreaTeslaClientId", "KoreaTeslaRefreshToken", "KoreaTeslaVin"):
  p.get(key)  # UnknownKeyName until params_keys.h is rebuilt
set_offroad_alert("Offroad_KoreaTeslaAuth", True)
print(p.get("Offroad_KoreaTeslaAuth")["text"])
set_offroad_alert("Offroad_KoreaTeslaAuth", False)
PY
```
Expected: `Tesla connection lost. Destinations set in the car's navigation are not received until tesla_setup is run again.`

- [ ] **Step 6: 린트**

Task 1 Step 5와 같은 명령. Expected: `All checks passed!`

- [ ] **Step 7: 커밋**

```bash
cd /e/dev/sunnypilot
git add openpilot/common/params_keys.h openpilot/selfdrive/selfdrived/alerts_offroad.json openpilot/sunnypilot/mapd/mapd_manager.py openpilot/sunnypilot/mapd/tests/test_mapd_source.py openpilot/sunnypilot/mapd/korea/route.py openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py
git commit -F - <<'EOF'
feat: start the Tesla destination thread with external nav

korea_main starts TeslaDestinationSource next to RouteSource under
KoreaExternalNavEnabled and stops it on every exit path. Registers
KoreaTeslaClientId, KoreaTeslaRefreshToken and KoreaTeslaVin without
BACKUP, since a rotated refresh token is dead once restored, and adds the
Offroad_KoreaTeslaAuth alert.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 5: 1회 설정 스크립트와 안내 문서

**Files:**
- Create: `openpilot/sunnypilot/mapd/korea/tesla_setup.py`
- Create: `docs/korea_tesla_destination.md`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_tesla_setup.py`

**Interfaces:**
- Consumes: Task 1의 `FLEET_API_URL`, `HTTP_TIMEOUT_S`, `VEHICLE_DATA_QUERY`, `api_request`, `parse_destination`, `token_request`
- Produces:
  - `authorize_url(client_id: str, redirect_uri: str, state: str) -> str`
  - `code_from_redirect(pasted: str, state: str) -> str`: 맞지 않으면 `ValueError`
  - `choose_vin(vehicles: list) -> str`: 차가 없으면 `ValueError`
  - `push_param(host: str, key: str, value: str) -> None`
  - `main()`: `--host` 필수

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`openpilot/sunnypilot/mapd/korea/tests/test_tesla_setup.py`:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import contextlib
import io
import sys
import unittest
import urllib.parse
from unittest import mock

from openpilot.sunnypilot.mapd.korea import tesla_setup
from openpilot.sunnypilot.mapd.korea.tesla_setup import authorize_url, choose_vin, code_from_redirect, push_param


class TestAuthorizeUrl(unittest.TestCase):
  def test_asks_for_every_read_only_scope_and_nothing_else(self):
    query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url("CLIENT", "https://me.github.io/callback", "S")).query)
    self.assertEqual(query["scope"], ["openid offline_access vehicle_device_data vehicle_location"])
    self.assertEqual(query["require_requested_scopes"], ["true"])
    self.assertEqual(query["response_type"], ["code"])
    self.assertEqual(query["client_id"], ["CLIENT"])
    self.assertEqual(query["redirect_uri"], ["https://me.github.io/callback"])
    self.assertEqual(query["state"], ["S"])


class TestCodeFromRedirect(unittest.TestCase):
  def test_reads_the_code_from_the_pasted_address(self):
    pasted = " https://me.github.io/callback?locale=en-US&code=C0DE&state=S&issuer=https%3A%2F%2Fauth.tesla.com \n"
    self.assertEqual(code_from_redirect(pasted, "S"), "C0DE")

  def test_an_address_from_another_login_is_refused(self):
    with self.assertRaisesRegex(ValueError, "state"):
      code_from_redirect("https://me.github.io/callback?code=C0DE&state=OTHER", "S")

  def test_a_refused_login_says_why(self):
    with self.assertRaisesRegex(ValueError, "access_denied"):
      code_from_redirect("https://me.github.io/callback?error=access_denied&state=S", "S")

  def test_an_address_without_a_code_is_refused(self):
    with self.assertRaisesRegex(ValueError, "no code"):
      code_from_redirect("https://me.github.io/callback?state=S", "S")


class TestChooseVin(unittest.TestCase):
  def test_the_only_car_is_chosen_without_asking(self):
    self.assertEqual(choose_vin([{"vin": "VIN123", "display_name": "Y"}]), "VIN123")

  def test_an_account_without_a_car_stops_the_setup(self):
    with self.assertRaises(ValueError):
      choose_vin([])

  def test_several_cars_are_asked_about(self):
    cars = [{"vin": "VIN1", "display_name": "A"}, {"vin": "VIN2", "display_name": "B"}]
    with mock.patch("builtins.input", return_value="2"), contextlib.redirect_stdout(io.StringIO()):
      self.assertEqual(choose_vin(cars), "VIN2")


class TestPushParam(unittest.TestCase):
  def push(self, key, value):
    with mock.patch.object(tesla_setup.subprocess, "run") as run:
      push_param("comma@10.0.0.2", key, value)
    return run.call_args

  def test_the_value_goes_over_stdin_only(self):
    call = self.push("KoreaTeslaRefreshToken", "REFRESH-1")
    self.assertNotIn("REFRESH-1", " ".join(call.args[0]))
    self.assertEqual(call.kwargs["input"], b"REFRESH-1")
    self.assertTrue(call.kwargs["check"])

  def test_the_param_lands_by_rename(self):
    call = self.push("KoreaTeslaVin", "VIN123")
    self.assertEqual(call.args[0][:2], ["ssh", "comma@10.0.0.2"])
    self.assertEqual(call.args[0][2],
                     "cat > /data/params/d/.KoreaTeslaVin.tmp && mv -f /data/params/d/.KoreaTeslaVin.tmp /data/params/d/KoreaTeslaVin")


class TestMain(unittest.TestCase):
  def test_host_is_required(self):
    """No --host, no run: there is deliberately no mode that prints the token instead."""
    with (mock.patch.object(sys, "argv", ["tesla_setup"]), contextlib.redirect_stderr(io.StringIO()),
          self.assertRaises(SystemExit)):
      tesla_setup.main()


if __name__ == "__main__":
  unittest.main()
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup -v`
Expected: `ModuleNotFoundError: No module named 'openpilot.sunnypilot.mapd.korea.tesla_setup'`

- [ ] **Step 3: 스크립트를 쓴다**

`openpilot/sunnypilot/mapd/korea/tesla_setup.py`:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla_setup: the one-time setup behind korea/tesla.py. Runs on the PC:

  python -m openpilot.sunnypilot.mapd.korea.tesla_setup --host comma@<device-ip>

Registers the app's domain with Tesla, walks the owner through Tesla's login, and writes
KoreaTeslaClientId, KoreaTeslaVin and KoreaTeslaRefreshToken to the device over ssh. The
client secret is asked for, used and dropped. No secret or token is printed, put on a
command line or written to this machine's disk -- which is why --host is required instead
of a fallback that prints the values. docs/korea_tesla_destination.md has the steps
before this one.
"""
import argparse
import getpass
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from openpilot.sunnypilot.mapd.korea.tesla import (FLEET_API_URL, HTTP_TIMEOUT_S, VEHICLE_DATA_QUERY, api_request,
                                                   parse_destination, token_request)

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
# Read-only on purpose: nothing on the device may command the car.
SCOPES = "openid offline_access vehicle_device_data vehicle_location"
DEVICE_PARAMS_DIR = "/data/params/d"


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
  return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
    "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES, "state": state,
    # Without it the consent page lets the owner untick vehicle_location, and vehicle_data
    # then answers without the destination's coordinates -- silently, on every drive.
    "require_requested_scopes": "true",
  })


def code_from_redirect(pasted: str, state: str) -> str:
  """The authorization code in the address Tesla sent the browser to. ValueError when that
  address is not the answer to this login."""
  query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
  if query.get("state") != [state]:
    raise ValueError("that address is not from this login (state does not match)")
  if "error" in query:
    raise ValueError(f"Tesla refused the login: {query['error'][0]}")
  if not query.get("code"):
    raise ValueError("that address has no code in it")
  return query["code"][0]


def choose_vin(vehicles: list) -> str:
  if not vehicles:
    raise ValueError("this Tesla account has no vehicle")
  if len(vehicles) == 1:
    return vehicles[0]["vin"]
  for i, vehicle in enumerate(vehicles, 1):
    print(f"  {i}. {vehicle.get('display_name') or '(no name)'}  {vehicle['vin']}")
  return vehicles[int(input("Which car? ")) - 1]["vin"]


def push_param(host: str, key: str, value: str) -> None:
  """One param on the device. The value rides ssh's stdin, never the command line, and
  lands by rename so the running device never reads half a token."""
  tmp = f"{DEVICE_PARAMS_DIR}/.{key}.tmp"
  subprocess.run(["ssh", host, f"cat > {tmp} && mv -f {tmp} {DEVICE_PARAMS_DIR}/{key}"], input=value.encode(), check=True)


def _send(request: urllib.request.Request) -> dict:
  with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
    return json.loads(response.read() or b"{}")


def _register_domain(partner_token: str, domain: str) -> None:
  request = urllib.request.Request(FLEET_API_URL + "/api/1/partner_accounts", data=json.dumps({"domain": domain}).encode(),
                                   headers={"Authorization": f"Bearer {partner_token}", "Content-Type": "application/json"},
                                   method="POST")
  try:
    _send(request)
  except urllib.error.HTTPError as e:
    # A second run may be refused as already registered. A real failure surfaces at the
    # vehicle list below, with Tesla's reason in it.
    print(f"Domain registration answered HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def _check(access_token: str, vin: str) -> None:
  """One real read, so a missing location grant shows up now instead of on the road."""
  try:
    data = _send(api_request(access_token, f"/api/1/vehicles/{urllib.parse.quote(vin, safe='')}/vehicle_data?{VEHICLE_DATA_QUERY}"))
  except urllib.error.HTTPError as e:
    if e.code != 408:
      raise
    print("The car is asleep, so the connection check was skipped.")
    return
  drive_state = (data.get("response") or {}).get("drive_state") or {}
  if "latitude" not in drive_state:
    print("WARNING: Tesla answered without the car's location. Run this again and allow Vehicle Location.")
  destination = parse_destination(data)
  print(f"Connection OK. The car's destination right now: {(destination[2] or '(unnamed)') if destination else 'none'}")


def main() -> None:
  parser = argparse.ArgumentParser(description="One-time Tesla setup so the device can read the car's navigation destination.")
  parser.add_argument("--host", required=True, help="ssh target, e.g. comma@192.168.1.50")
  args = parser.parse_args()

  client_id = input("Tesla app Client ID: ").strip()
  client_secret = getpass.getpass("Tesla app Client Secret (not shown): ").strip()
  domain = input("App domain, e.g. yourname.github.io: ").strip()
  redirect_uri = input(f"Redirect URI [https://{domain}/callback]: ").strip() or f"https://{domain}/callback"

  try:
    partner = _send(token_request({"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                                   "scope": "openid vehicle_device_data vehicle_location", "audience": FLEET_API_URL}))
    _register_domain(partner["access_token"], domain)

    state = secrets.token_urlsafe(16)
    url = authorize_url(client_id, redirect_uri, state)
    print(f"\nLog in to Tesla in the browser. If none opens, open this address:\n{url}\n")
    webbrowser.open(url)
    code = code_from_redirect(input("Paste the address of the page you land on (a 404 page is fine): "), state)
    tokens = _send(token_request({"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
                                  "code": code, "audience": FLEET_API_URL, "redirect_uri": redirect_uri}))
    refresh_token = tokens["refresh_token"]

    vin = choose_vin(_send(api_request(tokens["access_token"], "/api/1/vehicles")).get("response") or [])
    _check(tokens["access_token"], vin)
  except urllib.error.HTTPError as e:
    sys.exit(f"Tesla answered HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
  except urllib.error.URLError as e:
    sys.exit(f"Could not reach Tesla: {e.reason}")
  except (KeyError, IndexError, ValueError) as e:
    sys.exit(f"Setup stopped: {e}")

  push_param(args.host, "KoreaTeslaClientId", client_id)
  push_param(args.host, "KoreaTeslaVin", vin)
  # Last, so the device never holds a token without the rest of the setup.
  push_param(args.host, "KoreaTeslaRefreshToken", refresh_token)
  print("Saved to the device. It reads the car's destination from the next drive on.")


if __name__ == "__main__":
  main()
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup -v`
Expected: `Ran 11 tests ... OK`

- [ ] **Step 5: 안내 문서를 쓴다**

`docs/korea_tesla_destination.md`:

````markdown
# 테슬라 내비 목적지 연결 (1회 설정)

폰에서 NaviToTesla로 테슬라에 보낸 목적지를 콤마 기기가 테슬라 서버에서 읽어 온다. 한 번 설정하면 운전할 때마다 할 일은 없다. 설계: `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`.

**걸리는 시간:** 약 1시간. 대부분은 테슬라 개발자 사이트 입력이다.

**필요한 것**
- GitHub 계정
- 차량 소유자의 테슬라 계정
- 이 저장소가 있는 PC(Git Bash의 `openssl`, `ssh`)
- 기기 SSH 접속(`ssh comma@<기기IP>`가 되는 상태)
- 기기 설정: `KoreaExternalNavEnabled` 켜짐, `KoreaRouteApiKey` 입력됨

## 1. 공개키 만들기 (2분)

테슬라는 앱을 등록할 때, 도메인에 올린 공개키를 확인한다. 이 기능은 차에 명령하지 않아 개인키를 쓰지 않지만, 버리지 말고 저장소 밖에 보관한다.

```bash
openssl ecparam -name prime256v1 -genkey -noout -out tesla-private.pem
openssl ec -in tesla-private.pem -pubout -out com.tesla.3p.public-key.pem
```

## 2. GitHub Pages에 올리기 (10분)

1. GitHub에서 `<아이디>.github.io` 이름으로 공개 저장소를 만든다.
2. "Add file → Create new file"로 빈 파일 `.nojekyll`을 만든다. 이 파일이 없으면 `.well-known` 폴더가 공개되지 않는다.
3. 같은 방법으로 `.well-known/appspecific/com.tesla.3p.public-key.pem`을 만들고, 1단계에서 만든 `com.tesla.3p.public-key.pem`의 내용을 붙여넣는다.
4. 몇 분 뒤 브라우저에서 `https://<아이디>.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem`이 열리면 된다.

## 3. 테슬라 개발자 앱 등록 (20분)

1. https://developer.tesla.com 에 테슬라 계정으로 로그인하고 앱을 새로 만든다.
2. 이름과 설명은 자유롭게 적는다. 용도에는 "개인 차량의 내비 목적지 읽기"라고 적는다.
3. OAuth 방식은 "Authorization Code and Machine-to-Machine"을 고른다.
4. Allowed Origin: `https://<아이디>.github.io`
5. Allowed Redirect URI: `https://<아이디>.github.io/callback`
6. 권한(Scopes)은 **Vehicle Information**과 **Vehicle Location** 두 개만 고른다. 명령 권한은 고르지 않는다.
7. 결제 정보를 요구하면 등록한다. 월 $10 공제 안에서는 청구액이 0원이다(아래 비용 절).
8. 만들어진 앱의 **Client ID**와 **Client Secret**을 확인한다. 어디에도 저장하거나 붙여 두지 않는다. 4단계에서 한 번 입력하면 끝이다.

## 4. 설정 스크립트 실행 (5분)

먼저 테슬라 앱을 열어 차를 깨워 둔다(연결 확인용). 그다음 PC의 저장소 루트에서:

```bash
python -m openpilot.sunnypilot.mapd.korea.tesla_setup --host comma@<기기IP>
```

1. Client ID, Client Secret(입력이 화면에 보이지 않는다), 도메인(`<아이디>.github.io`)을 입력한다. Redirect URI는 Enter로 기본값을 쓴다.
2. 브라우저가 열리면 테슬라에 로그인하고 동의한다.
3. 넘어간 페이지가 404여도 된다. 주소창의 주소 전체를 복사해 스크립트에 붙여넣는다.
4. 차가 여러 대면 번호를 고른다.
5. `Connection OK`와 `Saved to the device`가 나오면 끝이다.

스크립트는 토큰을 화면에 출력하지 않고, PC에 저장하지도 않는다.

## 확인 (다음 주행)

1. 주행을 시작하고 NaviToTesla로 목적지를 보낸다.
2. 1분 안에 목적지가 기기에 들어온다:
   ```bash
   ssh comma@<기기IP> cat /data/params/d/NavDestination
   ```
3. 테슬라에서 안내를 취소하면 1분 안에 비워진다.

## 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| 주차 화면에 "Tesla connection lost" | 갱신 토큰이 거부됐다. 3개월 넘게 운전하지 않았거나, 테슬라 계정에서 앱 접근을 끊었다 | 4단계를 다시 실행 |
| 스크립트가 `WARNING: Tesla answered without the car's location` | 동의 화면에서 위치 권한이 빠졌다 | 4단계를 다시 실행하고 위치를 허용 |
| 스크립트가 `Tesla answered HTTP 4xx`로 멈춤 | 2단계 공개키가 안 보이거나, 도메인이 앱 설정과 다르다 | 2단계 4번의 주소가 열리는지 본다 |
| 목적지가 안 들어오고 알림도 없음 | `KoreaExternalNavEnabled`가 꺼졌거나 `KoreaRouteApiKey`가 없다 | 설정에서 켜고 키를 넣는다 |

## 비용

테슬라 데이터 요청은 500회당 $1이고, 계정마다 매달 $10를 깎아 준다. 기기는 주행 중에만 1분에 한 번 묻는다.

| 주행 | 요청 | 금액 |
|---|---|---|
| 1시간 | 60회 | $0.12 |
| 한 달 83시간까지 | 5,000회 | 공제로 0원 |

하루 300회(주행 5시간 분량)를 넘으면 그날은 더 묻지 않는다.

## 끄기

- 잠시 끄기: 설정에서 `KoreaExternalNavEnabled`를 끈다. 경로 기능 전체가 함께 꺼진다.
- 완전히 끄기: 기기의 토큰을 지우고, 테슬라 계정 설정의 서드파티 앱 관리에서 이 앱의 접근도 끊는다.
  ```bash
  ssh comma@<기기IP> rm /data/params/d/KoreaTeslaRefreshToken
  ```
````

- [ ] **Step 6: 린트**

Task 1 Step 5와 같은 명령. Expected: `All checks passed!`

- [ ] **Step 7: 커밋**

```bash
cd /e/dev/sunnypilot
git add openpilot/sunnypilot/mapd/korea/tesla_setup.py openpilot/sunnypilot/mapd/korea/tests/test_tesla_setup.py docs/korea_tesla_destination.md
git commit -F - <<'EOF'
feat: add the one-time Tesla setup script and its guide

tesla_setup registers the app's domain, runs Tesla's login with every
read-only scope required, picks the VIN, checks one real vehicle_data read,
and writes the three params over ssh stdin by rename. The client secret is
never stored, and no token is printed or put on a command line. The guide
covers the public key on GitHub Pages and the developer app.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 6: 전체 검증

**Files:** 없음(검증만). 문제가 나오면 해당 태스크로 돌아가 고치고, 그 태스크의 커밋 규칙대로 새 커밋을 만든다.

- [ ] **Step 1: 호스트 korea 테스트 전체**

Run:
```bash
cd /e/dev/sunnypilot
PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh openpilot.sunnypilot.mapd.korea.tests.test_map_download openpilot.sunnypilot.mapd.korea.tests.test_route openpilot.sunnypilot.mapd.korea.tests.test_external_source openpilot.sunnypilot.mapd.korea.tests.test_geo openpilot.sunnypilot.mapd.korea.tests.test_tesla openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup
```
Expected: 알려진 `PermissionError` 1건 외 통과.

- [ ] **Step 2: 컨테이너 전체 (리눅스)**

Run:
```bash
cd /e/dev/sunnypilot
MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/selfdrived/alerts_offroad.json sp-build:/work/openpilot/selfdrive/selfdrived/alerts_offroad.json
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest \
  openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db \
  openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh \
  openpilot.sunnypilot.mapd.korea.tests.test_map_download openpilot.sunnypilot.mapd.korea.tests.test_route \
  openpilot.sunnypilot.mapd.korea.tests.test_external_source openpilot.sunnypilot.mapd.korea.tests.test_geo \
  openpilot.sunnypilot.mapd.korea.tests.test_tesla openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup \
  openpilot.sunnypilot.mapd.tests.test_korea_map_data openpilot.sunnypilot.mapd.tests.test_mapd_source \
  openpilot.sunnypilot.mapd.tests.test_mapd_process'
```
Expected: `OK`. skip은 `TestLoadLinks` 3개뿐이다.

- [ ] **Step 3: 린트**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp pyproject.toml sp-build:/work/pyproject.toml
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd'
```
Expected: `All checks passed!`

- [ ] **Step 4: API 키가 저장소에 없는지 확인한다**

Run: `cd /e/dev/sunnypilot && bash .superpowers/sdd/scan-api-key.sh`
Expected: 출력되는 개수가 전부 `0`이고, 마지막 줄이 `SCAN COMPLETE (all counts must be 0)`이다. 스크립트는 키 값을 출력하지 않는다.

- [ ] **Step 5: 작업 트리를 확인한다**

Run: `cd /e/dev/sunnypilot && git status --short && git log --oneline -8`
Expected: 이 계획 전부터 있던 untracked 파일(`dev/`, `.codegraph/`, 다른 계획서 등) 외에 변경이 없다. 이 계획의 커밋은 5개(Task 1–5)이고, 그 아래에 설계 문서와 이 계획서 커밋이 있다.
