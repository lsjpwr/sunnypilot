# 내비 경로 기반 감속·과속카메라 대응 설계

**베이스:** `korea-dev` @ `8148ecfe7`

**Goal:** 폰 내비 앱이 안내를 시작할 때 목적지를 콤마 기기로 넘기고, 기기가 스스로 경로탐색 API를 호출해 앞으로 갈 경로를 안다. 그 경로로 (1) 과속카메라·방지턱 판정에서 옆 도로 오검출을 없애고 (2) 전방 커브를 미리 감속한다.

**핵심 판단:** 경로 API에서 필요한 것은 **폴리라인뿐이다.** 제한속도와 과속카메라는 이미 `korea_links.sqlite`와 `korea_cameras.sqlite`에 있다. 경로가 하는 일은 "분기에서 어느 쪽인지" 하나다. 이 판단이 설계 전체를 작게 만든다 — 응답 파싱이 좌표열 하나로 끝나고, API 제공자를 바꿔도 파서만 갈면 된다.

---

## 배경: 이미 깔려 있는 것

이 기능은 맨바닥에서 시작하지 않는다. 외부 내비 입력 경로가 이미 절반 이상 완성돼 있다.

| 있는 것 | 위치 |
|---|---|
| UDP 5555 JSON 수신 스레드 | `openpilot/sunnypilot/mapd/korea/external_source.py:68` |
| 토글 `KoreaExternalNavEnabled` | `openpilot/common/params_keys.h:274`, UI는 `speed_limit_settings.py:98` |
| mapd 기동 시 자동 start/stop | `openpilot/sunnypilot/mapd/mapd_manager.py:196` |
| 외부 데이터가 로컬 DB보다 우선 | `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py:159` |
| API 키 파라미터 `KoreaMapApiKey` | `openpilot/common/params_keys.h:275` |
| 주입 가능한 HTTP opener 패턴 | `openpilot/sunnypilot/mapd/korea/camera_refresh.py:46` |
| 곡률·거리 계산 함수 | `openpilot/sunnypilot/mapd/korea/geo.py:43`, `openpilot/sunnypilot/navd/helpers.py:86` |
| 목표속도 점 전달 경로 | `korea_map_data.py:216` → `MapTargetVelocities` → SCC-Map |

`external_source.py:7` 주석이 상황을 정확히 적어두었다: "a UDP JSON sink a phone-side navigation app can push into. Nothing ships that app yet."

### 폰 쪽 근거

[NaviToTesla](https://github.com/zipizigi/NaviToTesla)는 알림 접근 권한으로 T맵·카카오내비의 안내 시작 알림을 읽어 목적지를 뽑고, Tesla `navigation_request`로 **목적지 문자열 하나**를 차에 넘긴다. 차는 그것을 받아 자기 내비로 경로를 다시 계산한다. 이 구조를 그대로 빌린다 — 넘기는 것은 경로가 아니라 목적지이고, 경로는 받는 쪽이 만든다.

차이 둘: Tesla는 주소 문자열을 받지만 우리는 위경도를 받는다(앱 쪽에서 지오코딩). 그리고 그 앱이 콤마로도 쏘게 하려면 포크가 필요하다.

---

## 아키텍처

```
폰 (NaviToTesla 포크)              콤마 기기 (mapd 프로세스)
  안내 시작 알림
    → 목적지 좌표    ──UDP 5555──▶  ExternalNavSource        (있음, 키 2개 추가)
                                        │ 목적지
                                        ▼
                                    NavDestination 파라미터   (시동 단위, 휘발)
                                        │
                                        ▼
                                    RouteSource 스레드        (신규)
                                    경로탐색 API → 폴리라인
                                        │
                                        ▼
                                    KoreaMapData.tick() 1 Hz
                                        ├─ 경로 회랑으로 카메라·방지턱 필터
                                        └─ 전방 300 m 곡률 → 목표속도 점
                                        ▼
                                    MapTargetVelocities (방지턱 + 커브 병합)
```

경계는 셋이고 각각 혼자 이해되고 혼자 테스트된다:

- **`external_source.py`** — 와이어 포맷과 신뢰 경계. 바깥에서 온 숫자를 걸러 `ExternalNav`로 만드는 일만 한다.
- **`korea/route.py` (신규)** — 목적지를 폴리라인으로 바꾸는 일만 한다. HTTP, 재탐색 상태기계, 곡률 계산. openpilot 스택을 모른다.
- **`korea_map_data.py`** — 둘을 받아 `liveMapDataSP`와 `MapTargetVelocities`로 내보낸다. 기존 역할 그대로.

---

## 컴포넌트

### 1. 목적지 수신 — `external_source.py` (~10줄)

와이어 포맷에 키 둘을 더한다. 나머지는 그대로다.

```json
{"destination_lat": 37.5665, "destination_lon": 126.9780}
```

`_bounded`와 같은 방식으로 한국 bbox(위도 33–39, 경도 124–132) 밖이면 버린다. 안내 종료는 두 값을 `0`으로 보내는 것으로 표현한다.

**목적지는 TTL 밖에 둔다.** `ExternalNavSource.latest()`는 5초가 지나면 `None`을 돌려주는데(`external_source.py:101`), 목적지에 그 규칙을 그대로 쓰면 앱이 한 번만 보낸 목적지가 5초 뒤 사라진다. 목적지는 받는 즉시 `NavDestination` 파라미터에 쓰고, 이후로는 파라미터가 진실이다.

**`NavDestination`의 JSON 형식은 `athenad.py:390`이 이미 쓰는 것을 그대로 따른다** — `{"latitude", "longitude", "place_name", "place_details"}`. 그 RPC(`setNavDestination`)는 지금도 살아 있고 아무도 읽지 않는다. 형식을 맞추면 UDP 말고 comma connect의 목적지 전송도 같은 입력으로 공짜로 동작한다. 형식이 갈리면 두 writer가 서로 다른 모양을 쓰게 되므로, 이건 선택이 아니라 제약이다.

키는 `params_keys.h`에 `{PERSISTENT 아님, STRING}`으로 등록한다. 해제 조건 셋: 앱이 `0,0`을 보냄 / 목적지 100 m 이내 도달 / 시동 재시작(휘발).

### 2. 경로 획득 — `korea/route.py` (신규, ~130줄)

```python
def fetch_route(api_key, start, dest, opener=urllib.request.urlopen) -> list[tuple[float, float]]
```

`camera_refresh.fetch_all`과 같은 시그니처 규약이다 — `opener`를 주입받아 테스트가 네트워크 없이 돈다. `urllib.request`만 쓴다(신규 의존성 0).

응답에서 좌표열만 뽑는다. 검증 셋:

- 한국 bbox 밖 좌표가 하나라도 있으면 응답 전체를 버린다
- 점 5000개 초과면 버린다
- 점 2개 미만이면 버린다

**반드시 별도 스레드다.** mapd는 1 Hz 루프이고 `camera_refresh.py:40`의 타임아웃은 30초다. 제어 루프 안에서 부르면 감속 판단이 최대 30초 멈춘다. `CameraRefresher`(`camera_refresh.py:147`)의 스레드 구조를 그대로 복제한다 — `korea/` 하위 모듈이므로 `cereal`과 `Params`는 스레드 함수 안에서만 import한다.

### 3. 재탐색 상태기계 — `korea/route.py`

| 항목 | 값 | 근거 |
|---|---|---|
| 이탈 판정 | 폴리라인에서 50 m 초과가 3틱 연속 | 1틱 튐(GPS 노이즈)으로 재탐색하지 않는다 |
| 재탐색 쿨다운 | 5초 | 이탈 확정 3초 + 5초 = 8초면 새 경로. 체감되지 않는다 |
| 연속 실패 백오프 | 5s → 15s → 60s → 300s | 새 경로도 곧바로 이탈 판정되는 폭주를 막는 진짜 방어 |
| 백오프 리셋 | 경로 복귀(50 m 이내) 시 | |
| 일일 호출 상한 | 200회, 초과 시 그날 경로 기능만 off | |

쿨다운을 30초로 잡았다가 5초로 내렸다. 쿨다운은 폭주 방어 도구로 잘못 쓰인 것이었다 — 폭주는 연속 실패 백오프가 막고, 쿨다운은 정상 재탐색의 반응 속도만 정한다. 무료 쿼터 숫자는 TMAP·카카오모빌리티 모두 공개 페이지에 없어 확인하지 못했고(로그인 필요), 그래서 쿼터 숫자에 설계를 매달지 않았다. 재탐색은 1주행에 0–5회 수준이라 어떤 무료 티어든 여유이며, 일일 상한 200회는 쿼터를 몰라도 안전한 하드캡이다.

### 4. 경로 회랑 필터 — `korea/db.py` (~30줄)

`db.py:316 next_camera`는 지금 방위각 콘(`CAMERA_AHEAD_TOLERANCE`)만 본다. 교차로와 분기에서 옆 도로 카메라를 집을 수 있다. 경로가 있으면 판정을 바꾼다:

- 폴리라인에서 30 m 이내
- 경로 진행 방향 기준 앞쪽

`next_camera`와 `next_bump` 양쪽에 같은 필터가 들어간다. 둘 다 같은 오검출을 공유하므로 한쪽만 고치면 나머지가 남는다. 필요한 함수는 이미 있다 — `geo.py:43 point_segment_distance`, `navd/helpers.py:86 distance_along_geometry`.

경로가 없을 때는 기존 방위각 콘 동작 그대로다. 이 기능이 꺼지거나 실패해도 오늘의 동작이 남는다.

### 5. 커브 감속 — `korea/route.py` + `korea_map_data.py` (~50줄)

전방 300 m 폴리라인에서 세 점씩 묶어 곡률을 구하고, 목표속도를 낸다. 하한은 SCC-Map의 `MIN_V`(20 km/h)다. 상한은 설정속도가 아니라 도로의 게시 제한속도이고, 일치하는 링크가 없으면 `MAX_SPEED_LIMIT`로 대체한다 -- mapd는 애초에 설정속도를 모르고, SmartCruiseControlMap이 어차피 그 위의 목표는 스스로 거부하므로 게시 제한속도보다 높은 커브 목표는 어느 쪽으로 계산해도 잡음일 뿐이다.

**`MapTargetVelocities` 병합이 필수다.** `korea_map_data.py:216 publish_bump_target`은 지금 이 파라미터를 매 틱 통째로 덮어쓴다. 커브 점을 따로 쓰면 방지턱이 죽는다. 한 함수에서 두 소스를 합쳐 거리순으로 내보내도록 바꾼다.

---

## 신뢰 경계

`external_source.py:19`가 세운 원칙을 그대로 승계한다: **이 데이터는 longitudinal control에 도달하므로 전부 untrusted다.**

- 목적지 좌표: 한국 bbox 검증
- 경로 폴리라인: bbox + 점 개수 + 최소 길이 검증, 하나라도 어긋나면 응답 전체 폐기
- 곡률 목표속도: `MIN_V` 하한, 설정속도 상한
- 어떤 검증 실패도 예외가 아니라 "경로 없음"으로 떨어진다

---

## 실패 처리 — 전부 기존 동작으로 degrade

| 상황 | 처리 |
|---|---|
| API 키 없음/무효 | 경로 기능만 off. DB 기반 카메라·제한속도는 그대로 |
| 인터넷 끊김 | 마지막 폴리라인 유지. 없으면 off |
| API 타임아웃·쿼터 초과 | 스레드에서 백오프 재시도. 제어 루프 영향 0 |
| 응답 좌표 이상 | 폐기, 이전 경로 유지 |
| 일일 상한 초과 | 그날 경로 기능 off, UTC 타임스탬프를 86400초로 나눠 날짜를 판정하므로 KST 09:00에 리셋 |
| 목적지 미설정 | 경로 관련 코드 전부 비활성 |

`korea_map_data.py:145`의 기존 방침과 같다 — 경로가 없는 것은 안전한 답이고, 죽은 mapd는 나쁜 답이다.

---

## 테스트 전략

테스트는 `unittest`다(이 리포 규약, pytest 아님). 신규 프레임워크 0개.

### `korea/tests/test_route.py` (신규)

`test_camera_refresh.py:30 fake_opener` 패턴을 그대로 쓴다. 실제 API 응답 한 덩어리를 픽스처로 고정한다.

- 폴리라인 파싱이 좌표열을 정확히 뽑는다
- 한국 bbox 밖 좌표 → 응답 폐기
- 점 5000개 초과 → 폐기
- HTTP 예외 → 이전 경로 유지
- 일일 상한 초과 → 호출 자체가 안 나간다

재탐색 상태기계는 가짜 시계로 검증한다:

- 3틱 연속 이탈 → 재탐색 1회
- 2틱 이탈 후 복귀 → 재탐색 0회
- 연속 실패 4회 → 간격이 5/15/60/300
- 경로 복귀 → 백오프 리셋

### `korea/tests/test_db.py` (추가)

- 분기 케이스: 카메라 2개(내 경로 / 옆 도로), 경로를 주면 내 것만 나온다
- 경로 없음 → 기존 방위각 콘 동작 그대로 (회귀 방어)
- 경로 뒤쪽 카메라 → 제외
- 방지턱도 같은 셋

### `mapd/tests/test_korea_map_data.py` (추가) — 회귀 위험 1순위

`MapTargetVelocities` 병합이 기존 방지턱 기능을 깨기 가장 쉬운 자리다.

- 방지턱만 → 기존과 동일한 리스트
- 커브만 → 커브 점만
- 둘 다 → 둘 다, 거리순
- 목적지 해제 → 방지턱만 남는다

### `korea/tests/test_external_source.py` (추가)

- `test_destination_lands`
- `test_destination_outside_korea_rejected`

### 실차 전 검증

`mapd/live_map_data/standalone.py`는 `OsmMapData`를 하드코딩하고 경로 기능을 지원하지 않으므로 이 기능을 실행해볼 수 없다. 이 기능의 실차 전 검증은 디바이스에서 직접 해야 한다.

---

## 작업량

| 항목 | 위치 | 줄수 |
|---|---|---|
| 목적지 키 2개 + bbox 검증 | `external_source.py` | ~10 |
| 경로 API 클라이언트 | `korea/route.py` (신규) | ~80 |
| 재탐색 상태기계 | `korea/route.py` | ~50 |
| 곡률 → 목표속도 | `korea/route.py` | ~50 |
| 경로 회랑 필터 | `korea/db.py` | ~30 |
| 병합 + 배선 | `korea_map_data.py` | ~40 |
| 파라미터 등록 + 설정 UI 3곳 | `params_keys.h` 외 | ~30 |
| 앱 포크 (Kotlin, 별도 리포) | — | ~50 |

---

## 제약

- 신규 런타임 의존성 0. `urllib.request`만 쓴다 — `requests` 금지.
- `openpilot/sunnypilot/mapd/korea/` 하위는 모듈 최상단에서 openpilot 스택(`cereal`, `common.params`, `cloudlog`)을 import하지 않는다. 스레드 함수 안에서만 허용한다(`camera_refresh.py`가 쓰는 방식).
- Python 들여쓰기 2칸.
- 신규 파일은 sunnypilot MIT 헤더로 시작한다.
- 새 파라미터는 `params_keys.h`에 등록해야 `Params().get()`이 동작하고, `params.cc`로 컴파일되므로 재빌드가 필요하다.
- 사용자가 만질 파라미터는 설정 화면 셋(raylib UI, mici, sunnylink YAML) 모두에 노출한다. `settings_ui.json`은 생성물이므로 손대지 않는다.

---

## 범위 밖

- **모델이 경로를 먹는 진짜 E2E.** `log.capnp:2694-2709`에서 `navModel`·`navInstruction`·`navRoute`가 전부 DEPRECATED이고, 현재 supercombo에 `nav_features` 입력이 없다. 넣으려면 모델 재학습이라 개인 수준에서 불가능하다.
- **경로 기반 자동 차선변경·분기 진입.** 모델이 경로를 모르므로 룰 기반으로만 가능한데, 위험도에 비해 품질이 낮다.
- **실시간 교통·단속 정보.** 경로 폴리라인만 쓴다는 핵심 판단의 범위 밖이다.
- **API 제공자 비교.** TMAP으로 먼저 구현한다 — 응답에서 좌표열만 뽑으므로 파서가 얇고, 제공자를 바꿔도 `korea/route.py`의 파싱 함수 하나만 갈면 된다. 무료 한도는 TMAP·카카오모빌리티 모두 공개 페이지에 없어(로그인 필요) 구현 시점에 콘솔에서 확인한다. 일일 상한 200회가 그때까지의 방어다.
