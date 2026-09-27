# 테슬라 목적지 수신 — owner API 모드 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 테슬라 개발자 등록 없이, Termius로 넣은 owner API 토큰과 VIN만으로 기기가 차의 내비 목적지를 읽는다. Fleet 설정이 있으면 Fleet을 쓴다.

**Architecture:** 스레드는 그대로 `TeslaDestinationSource` 하나다. `korea/tesla.py`에 owner API용 요청 함수(갱신, 차량 id 찾기, 목적지 조회)를 더하고, `step()`이 조회 주기마다 파라미터로 모드를 고른다. 쓰기 규칙, 하루 상한, 알림, 실패 처리는 두 모드가 같이 쓴다. 새 파라미터 `KoreaTeslaOwnerRefreshToken`은 백업에서 빠지고 sunnylink 원격 조회에서 차단된다.

**Tech Stack:** Python 3.12 표준 라이브러리(`urllib`, `json`, `ssl`), `unittest`, C++ `params_keys.h`, Tesla owner API(비공식)와 Fleet API.

**Spec:** `docs/superpowers/specs/2026-09-27-korea-tesla-owner-api-design.md` (기존 Fleet 설계: `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`)

## Global Constraints

- 신규 런타임 의존성 0. HTTP는 `urllib.request`, TLS 설정은 `ssl`만 쓴다.
- `openpilot/sunnypilot/mapd/korea/` 하위는 모듈 최상단에서 openpilot 스택(`openpilot.cereal`, `openpilot.common.params`, `cloudlog`, `alertmanager`)을 import하지 않는다. `_loop` 안에서만 허용한다.
- Python 들여쓰기 2칸, ruff line-length 160.
- 실제 토큰, `client_id`, VIN은 저장소·문서·로그·커밋 어디에도 넣지 않는다. 테스트는 `OWNER-1`, `OWNER-2`, `OWNER-ACCESS-2`, `CLIENT`, `REFRESH-1`, `ACCESS-2`, `VIN123` 같은 눈에 띄는 가짜 값만 쓴다.
- 비밀값은 HTTP 헤더와 POST 본문에만 싣고 URL에는 넣지 않는다. 테슬라 요청이 실패하면 로그에는 HTTP 상태코드, 예외 타입, 고정 문구만 남긴다.
- owner 상수: 토큰 `https://auth.tesla.com/oauth2/v3/token`, API `https://owner-api.teslamotors.com`, `client_id` `ownerapi`, `scope` `openid email offline_access`. 갱신 본문은 JSON이다.
- owner 갱신 요청은 `minimum_version = ssl.TLSVersion.TLSv1_3`인 SSL 컨텍스트(`OWNER_TLS`)로 보낸다. 토큰 종류는 갱신 때 정해지므로 `products`와 `vehicle_data`는 기본 컨텍스트를 쓴다.
- 모드: `KoreaTeslaClientId`·`KoreaTeslaRefreshToken`·`KoreaTeslaVin`이 모두 있으면 Fleet, 아니면 `KoreaTeslaOwnerRefreshToken`과 `KoreaTeslaVin`이 있으면 owner, 아니면 쉰다. 실패해도 다른 모드로 넘어가지 않는다.
- 새 갱신 토큰은 모드에 맞는 키(`KoreaTeslaRefreshToken` 또는 `KoreaTeslaOwnerRefreshToken`)에 `block=True`로, 다른 어떤 요청보다 먼저 저장한다.
- 파라미터: `KoreaTeslaOwnerRefreshToken`은 `{PERSISTENT, STRING, ""}`(`BACKUP` 없음)이고 `sunnylinkd.py`의 `REMOTE_READ_DENYLIST`에 들어간다.
- 알림 문구(`Offroad_KoreaTeslaAuth`, `severity` 0): `Tesla connection lost. Destinations set in the car's navigation are not received until the Tesla login is set up again.`
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
- **컨테이너 `sp-build` (실제 `Params`가 필요한 테스트, 린트):** bind-mount가 깨져 있으니 소스를 `docker cp`로 밀어 넣는다. Git Bash에서는 `docker cp`마다 `MSYS_NO_PATHCONV=1`을 붙인다. 폴더는 항상 **부모 경로로** 복사한다(같은 이름 폴더 안으로 복사하면 `korea/korea`처럼 중첩된다).
  ```bash
  cd /e/dev/sunnypilot
  MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
  MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
  MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
  MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/selfdrived/alerts_offroad.json sp-build:/work/openpilot/selfdrive/selfdrived/alerts_offroad.json
  MSYS_NO_PATHCONV=1 docker cp pyproject.toml sp-build:/work/pyproject.toml
  docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
  docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest <dotted.module> -v'
  docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd openpilot/sunnypilot/sunnylink'
  ```
  `params_keys.h`를 바꾸면 scons로 다시 빌드해야 `Params()`가 새 키를 안다.
- 컨테이너에는 pytest가 없다. `unittest`만 돈다.

## File Structure

| 파일 | 역할 | Task |
|---|---|---|
| `openpilot/sunnypilot/mapd/korea/tesla.py` | owner 요청 함수(1), 모드 선택(2) | 1, 2 |
| `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py` | 위 두 가지 테스트 | 1, 2 |
| `openpilot/common/params_keys.h` | `KoreaTeslaOwnerRefreshToken` 등록 | 3 |
| `openpilot/selfdrive/selfdrived/alerts_offroad.json` | 알림 문구 | 3 |
| `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py` | `REMOTE_READ_DENYLIST`에 owner 토큰 | 3 |
| `openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py` | 원격 조회 차단 테스트 | 3 |
| `docs/korea_tesla_destination.md` | owner 모드 안내 | 4 |
| `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md` | 옛 알림 문구 한 줄 | 4 |

새 파일은 없다.

---

### Task 1: owner API 요청 함수

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/tesla.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py`

**Interfaces:**
- Consumes: 기존 `parse_destination`, `token_request`, `VEHICLE_DATA_QUERY`, `AuthRejected`, `HTTP_TIMEOUT_S`, `FLEET_API_URL`
- Produces:
  - 상수 `OWNER_API_URL`, `OWNER_TOKEN_URL`, `OWNER_CLIENT_ID`, `OWNER_SCOPE`, `OWNER_TLS: ssl.SSLContext`
  - `class VehicleNotFound(Exception)`
  - `api_request(access_token: str, path: str, base: str = FLEET_API_URL) -> urllib.request.Request`
  - `_read_json(request, opener, context: ssl.SSLContext | None = None) -> dict` — opener를 항상 `opener(request, timeout=HTTP_TIMEOUT_S, context=context)`로 부른다
  - `owner_token_request(refresh_token: str) -> urllib.request.Request`
  - `owner_refresh_access_token(refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]`
  - `owner_vehicle_id(access_token: str, vin: str, opener=urllib.request.urlopen) -> str`
  - `owner_fetch_destination(access_token: str, vehicle_id: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None`
  - `refresh_access_token`, `fetch_destination`, `vehicle_data_request`의 시그니처와 동작은 그대로다(`tesla_setup.py`가 쓴다)

- [ ] **Step 1: 테스트 도우미를 고치고 실패하는 테스트를 쓴다**

`test_tesla.py`의 import 블록을 아래로 바꾼다:

```python
import json
import ssl
import sys
import types
import unittest
import urllib.error
from unittest import mock

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.route import RequestBudget
from openpilot.sunnypilot.mapd.korea.tesla import (OWNER_API_URL, OWNER_TOKEN_URL, POLL_INTERVAL_S, TOKEN_URL, AuthRejected, TeslaDestinationSource,
                                                   VehicleNotFound, fetch_destination, owner_fetch_destination, owner_refresh_access_token,
                                                   owner_vehicle_id, parse_destination, refresh_access_token)
from openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh import OneShotStop
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeClock, FakeResponse
```

`TOKENS = {...}` 줄 바로 아래에 더한다:

```python
OWNER_TOKENS = {"access_token": "OWNER-ACCESS-2", "refresh_token": "OWNER-2", "expires_in": 28800, "token_type": "Bearer"}
OWNER_ID = 1492931520123456
# /api/1/products the way the owner API answers it: energy products carry no VIN.
PRODUCTS = {"response": [{"energy_site_id": 7, "resource_type": "battery"},
                         {"id": OWNER_ID, "vehicle_id": 99, "vin": "VIN123", "display_name": "Y"}], "count": 2}
```

`class ScriptedOpener` 전체를 아래로 바꾼다:

```python
class ScriptedOpener:
  """urlopen stand-in. A token request (either API) gets `token`; /api/1/products gets
  `products`; a vehicle_data request gets the next of `answers`. An exception in any place is
  raised instead of answered. `events` can be shared with a FakeParams so a test can tell
  which happened first; `contexts` records the TLS context each request asked for."""

  def __init__(self, answers=(), token=TOKENS, events=None, products=PRODUCTS):
    self.answers = list(answers)
    self.token = token
    self.products = products
    self.events = [] if events is None else events
    self.requests = []
    self.contexts = []

  def __call__(self, request, timeout=None, context=None):
    self.requests.append(request)
    self.contexts.append(context)
    if request.full_url in (TOKEN_URL, OWNER_TOKEN_URL):
      kind, answer = "token", self.token
    elif request.full_url.endswith("/api/1/products"):
      kind, answer = "products", self.products
    else:
      kind, answer = "vehicle_data", self.answers.pop(0)
    self.events.append(kind)
    if isinstance(answer, Exception):
      raise answer
    return FakeResponse(answer)

  def count(self, kind):
    return self.events.count(kind)
```

`class TestFetchDestination` 바로 아래에 더한다:

```python
class TestOwnerRefresh(unittest.TestCase):
  def test_refreshes_the_way_the_tesla_app_does(self):
    """NaviToTesla's refresh (2026-09): JSON to auth.tesla.com as the ownerapi client."""
    opener = ScriptedOpener(token=OWNER_TOKENS)
    self.assertEqual(owner_refresh_access_token("OWNER-1", opener), ("OWNER-ACCESS-2", "OWNER-2"))
    request = opener.requests[0]
    self.assertEqual(request.full_url, OWNER_TOKEN_URL)
    self.assertEqual(request.get_method(), "POST")
    self.assertEqual(request.get_header("Content-type"), "application/json")
    self.assertEqual(json.loads(request.data), {"grant_type": "refresh_token", "client_id": "ownerapi",
                                                "refresh_token": "OWNER-1", "scope": "openid email offline_access"})
    self.assertNotIn("OWNER-1", request.full_url)

  def test_the_refresh_goes_over_tls_1_3(self):
    """Below 1.3 auth.tesla.com mints a Fleet token, which owner-api refuses with 403."""
    opener = ScriptedOpener(token=OWNER_TOKENS)
    owner_refresh_access_token("OWNER-1", opener)
    self.assertEqual(opener.contexts[0].minimum_version, ssl.TLSVersion.TLSv1_3)

  def test_a_refused_owner_token_is_auth_rejected(self):
    """Used already -- by NaviToTesla, if the two share one -- or revoked."""
    for code in (400, 401):
      with self.subTest(code=code), self.assertRaises(AuthRejected):
        owner_refresh_access_token("OWNER-1", ScriptedOpener(token=http_error(code)))


class TestOwnerVehicleId(unittest.TestCase):
  def test_finds_the_car_by_vin(self):
    opener = ScriptedOpener()
    self.assertEqual(owner_vehicle_id("OWNER-ACCESS-2", "VIN123", opener), str(OWNER_ID))
    request = opener.requests[0]
    self.assertEqual(request.full_url, OWNER_API_URL + "/api/1/products")
    self.assertEqual(request.get_header("Authorization"), "Bearer OWNER-ACCESS-2")

  def test_a_vin_not_on_the_account_is_vehicle_not_found(self):
    with self.assertRaises(VehicleNotFound):
      owner_vehicle_id("OWNER-ACCESS-2", "VIN999", ScriptedOpener())

  def test_an_id_that_is_not_an_integer_is_never_used(self):
    """The id goes into a URL path, and the answer is untrusted."""
    for bad in ("1/../2", 1.5, True, None):
      with self.subTest(id=bad), self.assertRaises(VehicleNotFound):
        owner_vehicle_id("OWNER-ACCESS-2", "VIN123", ScriptedOpener(products={"response": [{"id": bad, "vin": "VIN123"}]}))

  def test_an_answer_of_the_wrong_shape_is_vehicle_not_found(self):
    for products in ({"response": None}, {"response": ["x"]}, {}):
      with self.subTest(products=products), self.assertRaises(VehicleNotFound):
        owner_vehicle_id("OWNER-ACCESS-2", "VIN123", ScriptedOpener(products=products))


class TestOwnerFetchDestination(unittest.TestCase):
  def test_reads_the_destination_from_owner_api_by_id(self):
    opener = ScriptedOpener([car()])
    self.assertEqual(owner_fetch_destination("OWNER-ACCESS-2", str(OWNER_ID), opener), (GANGNAM[0], GANGNAM[1], "강남역"))
    request = opener.requests[0]
    self.assertEqual(request.full_url, f"{OWNER_API_URL}/api/1/vehicles/{OWNER_ID}/vehicle_data?endpoints=drive_state%3Blocation_data")
    self.assertEqual(request.get_header("Authorization"), "Bearer OWNER-ACCESS-2")
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla`
Expected: `ImportError: cannot import name 'OWNER_API_URL' from 'openpilot.sunnypilot.mapd.korea.tesla'`

- [ ] **Step 3: owner 요청 함수를 구현한다**

`tesla.py` import 블록에 `import ssl`을 더한다(`import logging` 아래, `import threading` 위).

`MAX_PLACE_NAME = 64 ...` 줄 바로 아래에 더한다:

```python
# The owner API is the Tesla app's own, unofficial one -- NaviToTesla uses it. It needs no
# developer app and bills nothing, but its token is the whole account, which is why Fleet
# wins when both are set (TeslaDestinationSource.step).
OWNER_API_URL = "https://owner-api.teslamotors.com"
OWNER_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
OWNER_CLIENT_ID = "ownerapi"
OWNER_SCOPE = "openid email offline_access"
# auth.tesla.com picks the kind of token by the TLS version of the refresh: below 1.3 it mints
# a Fleet token, which owner-api refuses with 403 (snowake.dev, 2026-06). Only the refresh
# needs it -- the token's kind is settled there -- so the API calls keep the default context.
OWNER_TLS = ssl.create_default_context()
OWNER_TLS.minimum_version = ssl.TLSVersion.TLSv1_3
```

`class AuthRejected` 바로 아래에 더한다:

```python
class VehicleNotFound(Exception):
  """The owner account has no car with KoreaTeslaVin. Only fixing the VIN helps."""
```

`_read_json`을 아래로 바꾼다:

```python
def _read_json(request: urllib.request.Request, opener, context: ssl.SSLContext | None = None) -> dict:
  with opener(request, timeout=HTTP_TIMEOUT_S, context=context) as response:
    payload = json.loads(response.read())
  if not isinstance(payload, dict):
    raise ValueError("answer is not a JSON object")
  return payload
```

`api_request`를 아래로 바꾼다:

```python
def api_request(access_token: str, path: str, base: str = FLEET_API_URL) -> urllib.request.Request:
  return urllib.request.Request(base + path, headers={"Authorization": f"Bearer {access_token}"})
```

`refresh_access_token` 함수 전체를 아래 두 함수로 바꾼다:

```python
def _refresh(request: urllib.request.Request, opener, context: ssl.SSLContext | None = None) -> tuple[str, str]:
  """(access token, the refresh token that replaces the one in `request`).

  Tesla's refresh tokens are single use, so the caller must save the second value before
  anything else can fail. AuthRejected when Tesla refuses the token; any other failure is
  raised as it is -- a 5xx says nothing about the token.
  """
  try:
    payload = _read_json(request, opener, context)
  except urllib.error.HTTPError as e:
    if e.code in (400, 401):
      raise AuthRejected(f"HTTP {e.code}") from None
    raise
  access, replacement = payload.get("access_token"), payload.get("refresh_token")
  if not (isinstance(access, str) and access and isinstance(replacement, str) and replacement):
    raise ValueError("token answer without tokens")
  return access, replacement


def refresh_access_token(client_id: str, refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]:
  """Fleet: a refresh needs only the client id, never the secret."""
  return _refresh(token_request({"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token}), opener)
```

`fetch_destination` 함수 바로 아래에 더한다:

```python
def owner_token_request(refresh_token: str) -> urllib.request.Request:
  """The refresh the way the Tesla app's own client makes it; NaviToTesla sends this same JSON
  (2026-09). The token rides the body, never the URL -- same promise as token_request."""
  body = {"grant_type": "refresh_token", "client_id": OWNER_CLIENT_ID, "refresh_token": refresh_token, "scope": OWNER_SCOPE}
  return urllib.request.Request(OWNER_TOKEN_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                method="POST")


def owner_refresh_access_token(refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]:
  """refresh_access_token for an owner token, over TLS 1.3. Single use as well."""
  return _refresh(owner_token_request(refresh_token), opener, OWNER_TLS)


def owner_vehicle_id(access_token: str, vin: str, opener=urllib.request.urlopen) -> str:
  """The owner API addresses a car by its id in /api/1/products, not by VIN -- NaviToTesla
  does the same. The answer is untrusted and the id goes into a path, so only an int is
  taken (bool, an int subclass, is not)."""
  products = _read_json(api_request(access_token, "/api/1/products", OWNER_API_URL), opener).get("response")
  for product in products if isinstance(products, list) else []:
    if isinstance(product, dict) and product.get("vin") == vin and type(product.get("id")) is int:
      return str(product["id"])
  raise VehicleNotFound("no car with this VIN on the account")


def owner_fetch_destination(access_token: str, vehicle_id: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None:
  """fetch_destination through the owner API."""
  path = f"/api/1/vehicles/{urllib.parse.quote(vehicle_id, safe='')}/vehicle_data?{VEHICLE_DATA_QUERY}"
  return parse_destination(_read_json(api_request(access_token, path, OWNER_API_URL), opener))
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup`
Expected: `Ran 69 tests` … `OK` (test_tesla 45 → 53, test_tesla_setup 16)

- [ ] **Step 5: 린트**

Run:
```bash
cd /e/dev/sunnypilot && MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/ && MSYS_NO_PATHCONV=1 docker cp pyproject.toml sp-build:/work/pyproject.toml && docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd'
```
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
git add openpilot/sunnypilot/mapd/korea/tesla.py openpilot/sunnypilot/mapd/korea/tests/test_tesla.py
git commit -m "feat: add owner API requests for reading the Tesla destination" -m "The refresh is NaviToTesla's JSON as the ownerapi client, over TLS 1.3 so auth.tesla.com mints an owner token rather than a Fleet one. The car is found by VIN in /api/1/products, and only an integer id goes into a path." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY"
```

---

### Task 2: 폴링 스레드의 모드 선택

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/tesla.py`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_tesla.py`

**Interfaces:**
- Consumes (Task 1): `owner_refresh_access_token`, `owner_vehicle_id`, `owner_fetch_destination`, `VehicleNotFound`, `OWNER_API_URL`, `OWNER_TOKEN_URL`; 테스트 쪽 `ScriptedOpener(answers, token, events, products)`, `OWNER_TOKENS`, `OWNER_ID`, `PRODUCTS`
- Produces:
  - 상수 `FLEET = "fleet"`, `OWNER = "owner"`, `REFRESH_TOKEN_KEYS = {FLEET: "KoreaTeslaRefreshToken", OWNER: "KoreaTeslaOwnerRefreshToken"}`
  - `TeslaDestinationSource._fetch(mode: str, vin: str) -> tuple[float, float, str] | None`
  - `TeslaDestinationSource._reject(credentials: tuple, set_alert) -> None` — `credentials`는 `(mode, client_id, refresh_token, vin)`
  - `step()`의 시그니처는 그대로다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_tesla.py`의 `CREDENTIALS = {...}` 바로 아래에 더한다:

```python
OWNER_CREDENTIALS = {"KoreaTeslaOwnerRefreshToken": "OWNER-1", "KoreaTeslaVin": "VIN123", "KoreaRouteApiKey": "TMAP"}
```

`SourceTestCase.source`를 아래로 바꾼다:

```python
  def source(self, answers=(), token=TOKENS, products=PRODUCTS):
    self.opener = ScriptedOpener(answers, token, self.events, products)
    return TeslaDestinationSource(opener=self.opener, clock=self.clock)
```

`class TestAuth` 바로 아래(`class TestLoop` 위)에 더한다:

```python
class TestOwnerMode(SourceTestCase):
  def setUp(self):
    super().setUp()
    self.params = FakeParams(OWNER_CREDENTIALS, self.events)

  def test_an_owner_token_and_vin_read_the_car_through_the_owner_api(self):
    source = self.source([car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual([r.full_url for r in self.opener.requests],
                     [OWNER_TOKEN_URL, OWNER_API_URL + "/api/1/products",
                      f"{OWNER_API_URL}/api/1/vehicles/{OWNER_ID}/vehicle_data?endpoints=drive_state%3Blocation_data"])
    self.assertEqual(self.nav()["place_name"], "강남역")

  def test_the_new_owner_refresh_token_is_saved_before_anything_else_is_asked(self):
    source = self.source([car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual(self.params.values["KoreaTeslaOwnerRefreshToken"], "OWNER-2")
    self.assertIn("KoreaTeslaOwnerRefreshToken", self.params.blocking)
    self.assertLess(self.events.index("put KoreaTeslaOwnerRefreshToken"), self.events.index("products"))

  def test_the_car_is_looked_up_once_per_access_token(self):
    source = self.source([car(), car()], token=OWNER_TOKENS)
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("products"), 1)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_no_vin_asks_nothing(self):
    self.params.values.pop("KoreaTeslaVin")
    source = self.source([car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual(self.opener.requests, [])

  def test_a_vin_not_on_the_account_raises_the_alert_and_stops_asking(self):
    self.params.values["KoreaTeslaVin"] = "VIN999"
    source = self.source([car()], token=OWNER_TOKENS)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIs(self.alerts[-1], True)
    self.assertEqual(self.opener.count("products"), 1)
    self.assertEqual(self.opener.count("vehicle_data"), 0)

  def test_fixing_only_the_vin_asks_again(self):
    """Refused credentials are remembered as a whole, so a VIN fixed by hand over ssh is tried
    on the next poll -- no restart, no new token."""
    self.params.values["KoreaTeslaVin"] = "VIN999"
    source = self.source([car()], token=OWNER_TOKENS)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.params.values["KoreaTeslaVin"] = "VIN123"
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNotNone(self.nav())
    self.assertIs(self.alerts[-1], False)

  def test_owner_api_403_raises_the_alert(self):
    """The account is off the owner API, or auth.tesla.com minted a Fleet token anyway."""
    source = self.source([http_error(403), car()], token=OWNER_TOKENS)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIs(self.alerts[-1], True)
    self.assertEqual(self.opener.count("vehicle_data"), 1)


class TestModeChoice(SourceTestCase):
  def test_fleet_wins_when_both_are_set(self):
    """Official, and its grant cannot command the car. Passes before Task 2 too: it pins the
    order once the owner mode exists."""
    self.params.values["KoreaTeslaOwnerRefreshToken"] = "OWNER-1"
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.opener.requests[0].full_url, TOKEN_URL)
    self.assertTrue(self.opener.requests[-1].full_url.startswith(tesla.FLEET_API_URL))
    self.assertEqual(self.params.values["KoreaTeslaOwnerRefreshToken"], "OWNER-1")

  def test_setting_up_fleet_later_drops_the_owner_access_token(self):
    """The two APIs do not take each other's tokens."""
    self.params = FakeParams(OWNER_CREDENTIALS, self.events)
    source = self.source([car(), car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual(self.opener.count("products"), 1)
    self.params.values.update({"KoreaTeslaClientId": "CLIENT", "KoreaTeslaRefreshToken": "REFRESH-1"})  # tesla_setup ran
    self.opener.token = TOKENS
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.requests[-2].full_url, TOKEN_URL)
    self.assertEqual(self.opener.requests[-1].get_header("Authorization"), "Bearer ACCESS-2")
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla`
Expected: `FAILED`. 지금 `step()`은 Fleet 인증값이 없으면 아무것도 묻지 않으므로 `TestOwnerMode`의 6개(`test_no_vin_asks_nothing` 제외)와 `test_setting_up_fleet_later_drops_the_owner_access_token`이 실패한다(`[] != [...]`, `no logs of level WARNING`, `0 != 1` 등). `test_no_vin_asks_nothing`과 `test_fleet_wins_when_both_are_set`은 지금도 통과한다 — 모드가 생긴 뒤의 규칙을 고정하는 테스트다.

- [ ] **Step 3: 모드 선택을 구현한다**

`tesla.py` 모듈 docstring의 첫 두 문단을 아래로 바꾼다(마지막 "Deliberately free of openpilot imports …" 문단은 그대로 둔다):

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla: reads the destination the car's own navigation is guiding to, through Tesla's
servers, and hands it to RouteSource through NavDestination.

The driver already sends every destination to the car (NaviToTesla on the phone), so the
car is the one place that always has it -- reading it back beats a patched phone app
sending it a second time. Nothing here commands the car.

Two ways in. The Fleet API is official and its grant is read-only; tesla_setup sets it up
from a PC. The owner API is the Tesla app's own, unofficial one: an owner token put on the
device by hand over ssh, no developer app, but the token is the whole account. Fleet wins
when both are set.

Deliberately free of openpilot imports at module level so it runs under a bare Python
interpreter -- same rule as route.py. Only _loop imports the device stack.
"""
```

`SAME_DESTINATION_M = 50.` 줄 바로 아래에 더한다:

```python
FLEET, OWNER = "fleet", "owner"
# Where each mode keeps its single-use refresh token.
REFRESH_TOKEN_KEYS = {FLEET: "KoreaTeslaRefreshToken", OWNER: "KoreaTeslaOwnerRefreshToken"}
```

`TeslaDestinationSource.__init__`에서 `self._access_token` 줄부터 `self._rejected` 줄까지를 아래로 바꾼다:

```python
    self._access_token: str | None = None
    # Which API the access token belongs to. The two do not take each other's tokens.
    self._mode: str | None = None
    # (VIN, id) of the car the owner API answered for, looked up once per access token.
    self._owner_vehicle: tuple[str, str] | None = None
    # What this thread last wrote, so the car repeating it is not another write.
    self._written: tuple[float, float] | None = None
    # The credentials Tesla refused: (mode, client id, refresh token, VIN). Polling waits for
    # any of them to change -- tesla_setup writing new ones, or a VIN fixed by hand over ssh.
    self._rejected: tuple | None = None
```

`step()`에서 `client_id = params.get("KoreaTeslaClientId")` 줄부터 `self._apply(params, destination)` 바로 앞까지를 아래로 바꾼다:

```python
    # Without the TMAP key a destination buys no route, only a bill.
    if not params.get("KoreaRouteApiKey"):
      return
    client_id, vin = params.get("KoreaTeslaClientId"), params.get("KoreaTeslaVin")
    fleet_token, owner_token = params.get("KoreaTeslaRefreshToken"), params.get("KoreaTeslaOwnerRefreshToken")
    # Fleet first when both are set: it is official, and its grant cannot command the car. The
    # owner API is for owners without a Tesla developer app. No fallback either way.
    if client_id and fleet_token and vin:
      mode, refresh_token = FLEET, fleet_token
    elif owner_token and vin:
      mode, refresh_token = OWNER, owner_token
    else:
      return
    if mode != self._mode:
      self._mode, self._access_token = mode, None
    credentials = (mode, client_id, refresh_token, vin)
    if credentials == self._rejected:
      return
    if not self.budget.allow():
      LOG.warning("tesla: daily request cap reached")
      return

    fresh = self._access_token is None
    try:
      if fresh:
        if mode == FLEET:
          self._access_token, refresh_token = refresh_access_token(client_id, refresh_token, self._opener)
        else:
          self._access_token, refresh_token = owner_refresh_access_token(refresh_token, self._opener)
        credentials = (mode, client_id, refresh_token, vin)
        self._owner_vehicle = None
        # Single use: Tesla retired the old token the moment it answered. Save the new one
        # before anything else can fail, or nothing that works is left on the device. Params.put
        # only queues the write here unless block=True, and this is the one write that must be
        # on flash before the token is used.
        params.put(REFRESH_TOKEN_KEYS[mode], refresh_token, block=True)
        set_alert(False)
      destination = self._fetch(mode, vin)
    except AuthRejected:
      LOG.warning("tesla: refresh token rejected, set up the Tesla login again")
      self._reject(credentials, set_alert)
      return
    except VehicleNotFound:
      LOG.warning("tesla: no car with KoreaTeslaVin on the owner account")
      self._reject(credentials, set_alert)
      return
    except urllib.error.HTTPError as e:
      LOG.warning("tesla: request failed: HTTP %d", e.code)
      if e.code == 401 and not fresh:
        # The access token outlived its hours. Refresh on the next tick rather than a minute
        # from now -- this is usually the first poll of a drive.
        self._access_token = None
        self._next_poll = now
      elif e.code == 429:
        self._next_poll = now + RATE_LIMIT_PAUSE_S
      elif 400 <= e.code < 500 and e.code != 408:
        # A token refused right after its refresh, a grant without vehicle_location, a VIN the
        # account no longer has, a missing partner registration, the wrong region, unpaid
        # billing, an account the owner API no longer serves: none of it changes by asking
        # again, and on Fleet every ask is billed. 408 (the car is asleep) and 5xx stay "ask
        # again next minute".
        self._reject(credentials, set_alert)
      return
    except Exception as e:
      # The network, a timeout, a body that is not JSON: all "ask again next minute". Only the
      # type is logged -- a message can carry the URL, and a body the car's position.
      LOG.warning("tesla: request failed: %s", type(e).__name__)
      return
```

`_reject`를 아래 두 메서드로 바꾼다:

```python
  def _fetch(self, mode: str, vin: str) -> tuple[float, float, str] | None:
    if mode == OWNER and (self._owner_vehicle is None or self._owner_vehicle[0] != vin):
      self._owner_vehicle = (vin, owner_vehicle_id(self._access_token, vin, self._opener))
    self.budget.spend()
    if mode == FLEET:
      return fetch_destination(self._access_token, vin, self._opener)
    return owner_fetch_destination(self._access_token, self._owner_vehicle[1], self._opener)

  def _reject(self, credentials: tuple, set_alert) -> None:
    self._rejected = credentials
    self._access_token = None
    set_alert(True)
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_tesla openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup`
Expected: `Ran 78 tests` … `OK` (test_tesla 53 → 62, test_tesla_setup 16)

- [ ] **Step 5: 린트**

Run: Task 1 Step 5와 같은 명령.
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
git add openpilot/sunnypilot/mapd/korea/tesla.py openpilot/sunnypilot/mapd/korea/tests/test_tesla.py
git commit -m "feat: read the Tesla destination through the owner API when Fleet is not set up" -m "Each poll picks Fleet when its three credentials are set, else the owner token with the VIN. The access token is dropped when the mode changes, the rotated refresh token goes to the mode's own key, and refused credentials are remembered as a whole so a VIN fixed by hand is tried again." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY"
```

---

### Task 3: 파라미터 등록, 원격 조회 차단, 알림 문구

**Files:**
- Modify: `openpilot/common/params_keys.h`
- Modify: `openpilot/selfdrive/selfdrived/alerts_offroad.json`
- Modify: `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py`
- Test: `openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py`

**Interfaces:**
- Consumes (Task 2): 파라미터 이름 `KoreaTeslaOwnerRefreshToken`
- Produces: 기기에 등록된 `KoreaTeslaOwnerRefreshToken`, 원격 조회에서 빠지는 owner 토큰, 새 알림 문구

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_sunnylinkd.py`의 `class TestGetParams` 안의 테스트 메서드 전체를 아래로 바꾼다:

```python
  def test_tesla_refresh_tokens_never_leave_the_device(self):
    """The Fleet token reads the car's location for three months; the owner token is the whole
    Tesla account (korea/tesla.py)."""
    Params().put("KoreaTeslaRefreshToken", "REFRESH-1", block=True)
    Params().put("KoreaTeslaOwnerRefreshToken", "OWNER-1", block=True)
    Params().put("KoreaRouteApiKey", "TMAP", block=True)

    response = sunnylinkd.getParams(["KoreaTeslaRefreshToken", "KoreaTeslaOwnerRefreshToken", "KoreaRouteApiKey"])

    self.assertNotIn("KoreaTeslaRefreshToken", response)
    self.assertNotIn("KoreaTeslaOwnerRefreshToken", response)
    self.assertEqual([p["key"] for p in json.loads(response["params"])], ["KoreaRouteApiKey"])
```

- [ ] **Step 2: 파라미터를 등록하고 알림 문구를 바꾼다**

`params_keys.h`에서 테슬라 키 세 줄과 그 위 주석을 아래로 바꾼다:

```cpp
    // Written by tesla_setup from the PC (Fleet), or by hand over ssh (the owner token), read
    // by korea/tesla.py. Not BACKUP: a refresh token changes on every use, so a restored copy
    // is dead, and a backup only ships a credential off the device. For the same reason
    // sunnylink's getParams refuses to send either refresh token (REMOTE_READ_DENYLIST in
    // sunnylinkd.py).
    {"KoreaTeslaClientId", {PERSISTENT, STRING, ""}},
    {"KoreaTeslaOwnerRefreshToken", {PERSISTENT, STRING, ""}},
    {"KoreaTeslaRefreshToken", {PERSISTENT, STRING, ""}},
    {"KoreaTeslaVin", {PERSISTENT, STRING, ""}},
```

`alerts_offroad.json`의 `Offroad_KoreaTeslaAuth` `text`를 아래로 바꾼다:

```json
    "text": "Tesla connection lost. Destinations set in the car's navigation are not received until the Tesla login is set up again.",
```

- [ ] **Step 3: 컨테이너에서 테스트를 돌려 실패를 확인한다**

Run:
```bash
cd /e/dev/sunnypilot && MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/ && MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h && MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/selfdrived/alerts_offroad.json sp-build:/work/openpilot/selfdrive/selfdrived/alerts_offroad.json && docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common > /tmp/scons.log 2>&1; tail -1 /tmp/scons.log; ./.venv/bin/python -m unittest openpilot.sunnypilot.sunnylink.athena.tests.test_sunnylinkd 2>&1 | tail -4'
```
Expected: `scons: done building targets.` 그리고 `AssertionError: 'KoreaTeslaOwnerRefreshToken' unexpectedly found in {...}`, `FAILED (failures=1)`

- [ ] **Step 4: 차단 목록에 넣는다**

`sunnylinkd.py`의 `REMOTE_READ_DENYLIST`를 아래로 바꾼다:

```python
REMOTE_READ_DENYLIST = {
  "KoreaTeslaRefreshToken",  # reads the car's location for three months (korea/tesla.py)
  "KoreaTeslaOwnerRefreshToken",  # the Tesla app's own login: the whole account (korea/tesla.py)
}
```

- [ ] **Step 5: 컨테이너에서 통과를 확인한다**

Run:
```bash
cd /e/dev/sunnypilot && MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/ && docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.sunnylink.athena.tests.test_sunnylinkd openpilot.selfdrive.selfdrived.tests.test_alerts.TestAlerts.test_offroad_alerts 2>&1 | tail -3 && ./.venv/bin/ruff check openpilot/sunnypilot/sunnylink'
```
Expected: `Ran 11 tests` … `OK`, `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
git add openpilot/common/params_keys.h openpilot/selfdrive/selfdrived/alerts_offroad.json openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py
git commit -m "feat: register the Tesla owner token and keep it off sunnylink" -m "KoreaTeslaOwnerRefreshToken is PERSISTENT without BACKUP and on sunnylink's REMOTE_READ_DENYLIST, like the Fleet token. The lost-login alert no longer names tesla_setup, which only covers Fleet." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY"
```

---

### Task 4: 안내 문서와 전체 검증

**Files:**
- Modify: `docs/korea_tesla_destination.md`
- Modify: `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`

**Interfaces:**
- Consumes: Task 1~3의 동작(모드 규칙, 파라미터 이름, 알림 문구)
- Produces: 운전자가 따라 할 owner 모드 설정 절차

- [ ] **Step 1: 안내 문서를 고친다**

`docs/korea_tesla_destination.md`에서:

(a) 3번째 줄(설계 문서를 가리키는 문단) 바로 아래에 빈 줄을 두고 더한다:

````markdown
**방법은 두 가지다.** 둘 다 설정돼 있으면 기기는 Fleet을 쓴다.

| | Fleet (추천) | owner (비공식) |
|---|---|---|
| 설정 | PC에서 아래 1~4단계, 약 1시간 | 폰 Termius로 명령 두 줄, 약 10분 (아래 "owner 모드" 절) |
| 토큰 권한 | 읽기 전용. 새도 차를 움직일 수 없다 | 테슬라 앱 로그인과 같은 계정 전체 권한 |
| 요금 | 월 $10 공제 안에서 0원(결제 정보 등록 필요) | 없음 |
| 수명 | 테슬라 공식 API | 테슬라가 2026년에 단계적으로 닫는 중이라 언제 막힐지 모른다 |
````

(b) `**필요한 것**`을 `**필요한 것 (Fleet 방법)**`으로 바꾼다.

(c) `## 확인 (다음 주행)` 바로 위에 더한다:

````markdown
## owner 모드 (PC 없이, 비공식)

1~4단계 대신 폰만으로 쓰는 방법이다. 위 표의 권한·수명 차이를 알고 쓴다. 기기 SSH 접속(Termius), `KoreaExternalNavEnabled` 켜짐, `KoreaRouteApiKey`, 이 기능이 들어간 빌드는 Fleet과 똑같이 필요하다.

**주의**
- **NaviToTesla에 넣은 토큰을 복사하지 않는다.** 갱신 토큰은 한 번 쓰면 무효가 되어, 기기와 NaviToTesla가 서로를 끊는다. 기기용은 새로 로그인해 따로 받는다.
- 2026-04에 테슬라가 로그인 방식을 바꿔 예전 토큰 발급 도구 대부분이 `redirect_uri` 오류로 막혔다. 그 뒤에 갱신된 도구(예: tesla_auth 포크)를 쓴다.
- 이 토큰은 계정 전체 권한이다. 채팅·메모·스크린샷에 남기지 않는다.

1. 토큰 발급 도구로 테슬라에 새로 로그인해 **Refresh Token**을 받고 폰에 복사해 둔다.
2. Termius로 기기에 접속해 토큰을 넣는다. 붙여넣은 입력이 화면에 안 보이는 게 정상이다:
   ```bash
   read -rs V && printf '%s' "$V" > /data/params/d/.KoreaTeslaOwnerRefreshToken.tmp && mv -f /data/params/d/.KoreaTeslaOwnerRefreshToken.tmp /data/params/d/KoreaTeslaOwnerRefreshToken && unset V
   ```
3. VIN(17자리, 차 화면 컨트롤 → 소프트웨어)을 넣는다:
   ```bash
   read -r V && printf '%s' "$V" > /data/params/d/KoreaTeslaVin && unset V
   ```
4. 두 값이 들어갔는지 본다. 값 대신 글자 수만 나오고, 둘 다 0이 아니면 된다. 재부팅은 필요 없다:
   ```bash
   wc -c /data/params/d/KoreaTeslaOwnerRefreshToken /data/params/d/KoreaTeslaVin
   ```

두 명령은 Termius **Snippets**에 저장해 두면 탭 한 번으로 실행된다. 명령 안에 토큰이 없어서 저장해도 안전하다.

**나중에 Fleet으로 옮기기:** 1~4단계를 하면 기기가 다음 조회부터 Fleet을 쓴다. 그다음 owner 토큰을 지운다:
```bash
ssh comma@<기기IP> rm /data/params/d/KoreaTeslaOwnerRefreshToken
```
````

(d) `## 확인 (다음 주행)`의 4번 항목 아래에 더한다:

````markdown
5. owner 모드라면 NaviToTesla로 목적지를 보낸 뒤에도 NaviToTesla가 계속 동작하는지 본다. 끊기면 두 곳이 같은 토큰을 쓰고 있는 것이다.
````

(e) `## 문제 해결` 표의 마지막 줄 아래에 두 줄을 더한다:

```markdown
| owner 모드에서 "Tesla connection lost" | 토큰이 이미 쓰였거나(NaviToTesla와 같은 토큰) 폐기됐다. 또는 VIN이 그 계정에 없다 | VIN을 확인하고, 토큰을 새로 발급해 owner 모드 2번을 다시 한다 |
| owner 모드에서 새 토큰을 넣어도 곧바로 다시 알림 | 이 계정에서 owner API가 막혔다(403) | Fleet 설정(1~4단계)으로 옮긴다 |
```

(f) `## 끄기`의 코드 블록 아래에 더한다:

````markdown
- owner 모드 끄기: 기기의 owner 토큰을 지운다. 테슬라 쪽 토큰까지 끊으려면 계정 비밀번호를 바꾸는데, 그러면 NaviToTesla 같은 다른 앱도 다시 로그인해야 한다.
  ```bash
  ssh comma@<기기IP> rm /data/params/d/KoreaTeslaOwnerRefreshToken
  ```
````

- [ ] **Step 2: 옛 설계 문서의 알림 문구를 맞춘다**

`docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`에서 코드 블록 안의 줄

```
Tesla connection lost. Destinations set in the car's navigation are not received until tesla_setup is run again.
```

을 아래로 바꾸고, 코드 블록을 닫는 줄 바로 아래에 `(2026-09-27 owner 모드 설계에서 문구를 바꿨다. owner 모드는 tesla_setup을 쓰지 않는다.)`를 한 줄 더한다:

```
Tesla connection lost. Destinations set in the car's navigation are not received until the Tesla login is set up again.
```

- [ ] **Step 3: 전체 검증**

1. 호스트:
   ```bash
   cd /e/dev/sunnypilot && PYTHONUTF8=1 python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh openpilot.sunnypilot.mapd.korea.tests.test_map_download openpilot.sunnypilot.mapd.korea.tests.test_route openpilot.sunnypilot.mapd.korea.tests.test_external_source openpilot.sunnypilot.mapd.korea.tests.test_geo openpilot.sunnypilot.mapd.korea.tests.test_tesla openpilot.sunnypilot.mapd.korea.tests.test_tesla_setup openpilot.sunnypilot.mapd.korea.tests.test_device_imports
   ```
   Expected: `Ran 349 tests`, 실패는 알려진 윈도우 `PermissionError` 1건뿐(332 + Task 1의 8 + Task 2의 9).
2. 컨테이너: 테스트 환경 절의 `docker cp` 다섯 줄과 scons를 실행한 뒤:
   ```bash
   docker exec sp-build bash -lc 'cd /work && K=openpilot.sunnypilot.mapd.korea.tests; ./.venv/bin/python -m unittest $K.test_build_db $K.test_db $K.test_deploy $K.test_camera_refresh $K.test_map_download $K.test_route $K.test_external_source $K.test_geo $K.test_tesla $K.test_tesla_setup $K.test_device_imports openpilot.sunnypilot.mapd.tests.test_korea_map_data openpilot.sunnypilot.mapd.tests.test_mapd_source openpilot.sunnypilot.mapd.tests.test_mapd_process openpilot.sunnypilot.sunnylink.athena.tests.test_sunnylinkd openpilot.sunnypilot.sunnylink.tests.test_settings_schema openpilot.selfdrive.selfdrived.tests.test_alerts.TestAlerts.test_offroad_alerts > /tmp/final.log 2>&1; grep -E "^(ERROR|FAIL):" /tmp/final.log; grep -E "^Ran |^OK|^FAILED" /tmp/final.log; echo "cereal-missing: $(grep -c "No module named .cereal." /tmp/final.log)"'
   ```
   Expected: `Ran 464 tests`(446 + 17 + `test_offroad_alerts` 1), `OK (skipped=3)`, `cereal-missing: 0`
3. 린트: `docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd openpilot/sunnypilot/sunnylink'` → `All checks passed!`
4. 키 스캔: `cd /e/dev/sunnypilot && bash .superpowers/sdd/scan-api-key.sh` → 모든 개수 `0`

테스트 개수가 다르면 개수 차이의 원인을 보고서에 적는다(다른 커밋이 테스트를 더했을 수 있다). 실패가 새로 생기면 멈추고 보고한다.

- [ ] **Step 4: 커밋**

```bash
git add docs/korea_tesla_destination.md docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md
git commit -m "docs: explain the owner API mode for the Tesla destination" -m "The guide compares the two ways in, walks through the Termius setup with a token separate from NaviToTesla's, and covers moving to Fleet later and the owner-mode failures. The Fleet spec now shows the new alert text." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY"
```
