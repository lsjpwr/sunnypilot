# 테슬라 내비 목적지 수신 설계

**베이스:** `korea-dev` @ `0a8530cff`

**Goal:** 운전자가 폰에서 NaviToTesla로 테슬라 내비에 넣은 목적지를, 콤마 기기가 테슬라 Fleet API로 읽어 `NavDestination`에 넣는다. 그러면 기존 `RouteSource`가 TMap 경로를 받고, 경로 기반 카메라 필터와 커브 감속이 돈다.

**핵심 판단:** 목적지는 이미 차에 있다. 폰 앱을 고쳐 기기로 한 번 더 쏘게 하는 대신 차에서 읽는다. 운전자가 주행마다 할 일은 0이고, 유지할 폰 앱도 0이다. 대가는 1회 설정(약 1시간)과 사용량 과금이다. 과금은 월 $10 공제 안에 들어간다(비용 절).

---

## 배경

**수신 쪽은 완성돼 있다.** `NavDestination` 파라미터를 쓰면 `RouteSource`(`openpilot/sunnypilot/mapd/korea/route.py:371`)가 읽어 TMap 경로를 받는다. 이 파라미터에 쓰는 쪽이 없었을 뿐이다 — `external_source.py:7`: "Nothing ships that app yet."

**운전자의 실제 흐름(2026-09-25 확인):** 폰 T맵·카카오내비에서 목적지를 찾고, NaviToTesla로 테슬라 내비에 보낸다. 즉 주행 중 목적지의 원본은 차다.

### 검토한 대안

| 대안 | 판단 |
|---|---|
| **차에서 읽기 (Fleet API `vehicle_data`)** | **채택.** 목적지가 어떤 경로로 차에 들어갔든(NaviToTesla, 테슬라 앱 공유, 화면 입력, 음성) 같은 방식으로 읽힌다. 차에서 바꾸거나 취소한 것도 따라간다 |
| 폰에서 보내기 (NaviToTesla 포크 → UDP 5555) | 기기 쪽은 이미 있으나, 원본 저장소에 라이선스가 없고(`license: null`) 2026-09-25에도 푸시가 있을 만큼 활발해 포크를 계속 따라가야 한다. 폰과 기기가 같은 Wi-Fi여야 한다 |
| 기기 웹페이지에서 직접 입력 | 목적지를 폰 앱과 여기에 두 번 넣어야 한다 |
| 대행 서비스 (Teslemetry) | 월 ₩44,689. 개발자 등록을 대신해 줄 뿐 같은 API다 |
| CAN에서 읽기 | Tesla DBC의 내비 관련 신호는 `UI_navRouteActive` 한 비트뿐이다. 목적지 좌표는 CAN에 없다 |
| Fleet Telemetry `RouteLine` | 테슬라가 계산한 경로선을 직접 받을 수 있지만, 인증서를 단 공개 서버를 운영해야 한다 |

---

## 아키텍처

```
폰: T맵/카카오내비 → NaviToTesla → 테슬라 내비
                                     │ (테슬라 서버)
                                     ▼
콤마 기기 (mapd 프로세스)
  TeslaDestinationSource 스레드 (신규, korea/tesla.py)
    주행 중에만 60초마다 GET vehicle_data?endpoints=drive_state;location_data
    drive_state.active_route_latitude / active_route_longitude
                                     │
                                     ▼
  NavDestination 파라미터 (athenad 형식 그대로)      ← 기존
                                     │
                                     ▼
  RouteSource → TMap 경로 → 카메라 회랑·커브 감속     ← 기존, 무수정
```

경계는 셋이다:

- **`korea/tesla.py`** — 테슬라와의 HTTP, 토큰 갱신, 응답 검증, 폴링 스레드. openpilot 스택은 스레드 함수 안에서만 import한다(`route.py`, `camera_refresh.py`와 같은 규칙).
- **`korea/tesla_setup.py`** — PC에서 한 번 실행하는 설정 도구. 런타임 경로에 없다.
- **`mapd_manager.py`** — 스레드를 `RouteSource` 옆에서 시작하고 멈춘다. 그 외 기존 코드는 손대지 않는다.

---

## 컴포넌트

### 1. 폴링 조건

아래가 모두 참일 때만 요청을 보낸다. 하나라도 거짓이면 그 주기는 건너뛴다(요청 0회, 비용 0).

| 조건 | 이유 |
|---|---|
| 주행 중 (`deviceState.started`) | 주차 중 조회는 돈만 든다. 스레드 안에서 `SubMaster(['deviceState'])`로 본다 — `MapDownloader._loop`(`map_download.py:290`)와 같은 방식 |
| `KoreaTeslaClientId`·`KoreaTeslaRefreshToken`·`KoreaTeslaVin` 모두 있음 | 설정 전에는 조용히 쉰다. 설정 스크립트가 값을 쓰면 재시작 없이 다음 주기부터 돈다 |
| `KoreaRouteApiKey` 있음 | TMap 키가 없으면 목적지를 받아도 경로를 못 받는다 |
| 일일 상한 안 | 하루 300회. `route.py`의 `RequestBudget`을 재사용한다 |
| 429 휴지 중 아님, 인증 거부 상태 아님 | 실패 처리 절 |

스레드 자체는 `KoreaExternalNavEnabled`가 켜졌을 때만 `RouteSource`와 함께 시작한다. 새 토글은 없다 — `mapd_manager.py:210`이 적어 둔 대로, 토글을 둘로 나누면 둘 다 켜야 하는 함정만 생긴다.

주기는 60초다. 주행이 시작되면 첫 요청은 기다리지 않고 바로 보낸다.

### 2. 목적지 쓰기 규칙

| 상황 | 동작 |
|---|---|
| 차 목적지가 새로 생기거나 바뀜 | `NavDestination`에 쓴다. 형식은 `athenad.py:389`와 같은 `{"latitude", "longitude", "place_name", "place_details"}`이고, `place_name`에 `active_route_destination`을 넣는다 |
| 지난번에 쓴 것과 같은 목적지 (`SAME_DESTINATION_M` = 50 m 이내) | 쓰지 않는다. 도착(100 m)으로 `RouteSource`가 지운 뒤에도 되살리지 않는다 — 되살리면 도착 지점에서 60초마다 쓰기와 지우기가 반복된다. 정확한 좌표 일치가 아니라 거리로 비교하는 이유: 좌표가 조금이라도 흔들려 다시 쓰면 `RouteSource`가 목적지 변경으로 보고(`route.py:402`) 경로를 버리고 TMap을 다시 부른다 |
| 차 목적지가 사라짐 (안내 종료·취소) | `NavDestination`이 이 스레드가 쓴 값일 때만 지운다. UDP나 athenad RPC가 쓴 값은 건드리지 않는다 |
| 주행 시작 (`started`의 상승 에지) | "지난번에 쓴 목적지" 기억을 비운다. 주행이 끝날 때 `NavDestination`이 이미 지워졌으므로(`CLEAR_ON_OFFROAD_TRANSITION`), 같은 목적지를 다음 주행에 다시 써야 한다 |

### 3. 토큰

- 요청 권한은 `openid offline_access vehicle_device_data vehicle_location`이다. 차에 명령하는 권한(`vehicle_cmds` 등)은 요청하지 않는다. 토큰이 새도 차를 움직일 수 없다.
- 접근 토큰은 메모리에만 둔다. 없거나 `vehicle_data`가 401을 주면 갱신한다.
- 갱신 토큰은 1회용이다(테슬라 문서: "The refresh token is single use only and expires after 3 months"). 갱신 응답의 새 갱신 토큰을 **접근 토큰을 쓰기 전에** `KoreaTeslaRefreshToken`에 저장한다. 저장이 늦어져 그 사이 프로세스가 죽으면 토큰 사슬이 끊긴다.
- 갱신에는 `client_id`와 갱신 토큰만 필요하다. `client_secret`은 기기에 두지 않는다.
- 비밀값은 헤더와 POST 본문에만 싣고 URL에는 넣지 않는다. urllib은 URL을 예외 메시지에 넣고 그것이 cloudlog로 간다(`route.py:125`와 같은 약속). 토큰, 응답 본문, 위치는 로그에 남기지 않는다.

### 4. 설정 스크립트 — `korea/tesla_setup.py`

PC에서 한 번 실행한다. 기존 `deploy`처럼 PC에서 돌고 SSH로 기기에 쓴다. `--host`는 필수다 — 토큰을 화면에 출력하는 대체 경로를 두지 않기 위해서다.

```
python -m openpilot.sunnypilot.mapd.korea.tesla_setup --host comma@<기기IP>
```

1. `client_id`(입력), `client_secret`(`getpass`, 화면에 안 보임), 도메인(예: `lsjpwr.github.io`)을 묻는다. 리디렉트 URI 기본값은 `https://<도메인>/callback`이다.
2. 파트너 토큰(`client_credentials`)을 받아 도메인을 등록한다(`POST /api/1/partner_accounts`). 지역당 한 번이면 되고, 다시 해도 해롭지 않다.
3. 로그인 주소를 출력하고 브라우저로 연다. `state`는 난수다. 운전자가 로그인하고 동의하면, 넘어간 페이지의 주소창 주소를 복사해 붙여넣는다(없는 페이지라 404가 떠도 주소에 코드가 들어 있다). `state`가 다르면 중단한다.
4. 코드를 토큰으로 바꾼다. 차량 목록에서 VIN을 고른다(한 대면 자동).
5. 받은 접근 토큰으로 `vehicle_data`를 한 번 불러 확인한다. 위치 권한이 빠져 좌표 필드가 없으면, 동의 화면에서 위치를 켜고 다시 하라고 알린다.
6. 인증값 3개를 SSH 표준입력으로 기기에 쓴다. 임시 파일에 쓴 뒤 `mv`로 바꿔 끼워, 스레드가 반쯤 쓰인 토큰을 읽지 않게 한다.

`client_secret`과 토큰은 명령줄 인자, PC 디스크, 로그 어디에도 남기지 않는다. 스크립트는 토큰을 화면에 출력하지 않는다.

### 5. 파라미터와 알림

`params_keys.h`:

| 키 | 플래그 | 이유 |
|---|---|---|
| `KoreaTeslaClientId` | `PERSISTENT, STRING, ""` | |
| `KoreaTeslaRefreshToken` | `PERSISTENT, STRING, ""` | `BACKUP` 제외. 토큰이 쓸 때마다 바뀌어 백업본은 복원해도 무효이고, 인증값을 클라우드로 내보내기만 한다 |
| `KoreaTeslaVin` | `PERSISTENT, STRING, ""` | 셋은 함께 쓰일 때만 의미가 있으므로 셋 다 `BACKUP` 제외 |
| `Offroad_KoreaTeslaAuth` | `CLEAR_ON_MANAGER_START, JSON` | 연결 끊김 알림. `Offroad_KoreaMapMissing`(`params_keys.h:290`)과 같은 모양 |

`alerts_offroad.json`에 `Offroad_KoreaTeslaAuth`를 더한다(`severity` 0):

```
Tesla connection lost. Destinations set in the car's navigation are not received until tesla_setup is run again.
```

설정 화면(raylib UI, mici, sunnylink YAML)에는 넣지 않는다. 경로 설계(`2026-09-18-korea-route-nav-design.md`)의 "사용자가 만질 파라미터는 설정 화면 셋 모두에 노출한다"는 규칙의 예외다 — 이 값들은 사람이 아니라 설정 스크립트가 쓰고, 갱신 토큰은 쓸 때마다 바뀌어 화면 입력이 의미가 없다.

---

## 신뢰 경계

응답은 종방향 제어까지 닿는 목적지가 되므로 `external_source.py:22`의 원칙을 그대로 따른다 — 전부 untrusted다.

- `active_route_latitude`/`longitude`: 숫자여야 하고(`bool` 제외), `route.in_korea`를 통과해야 한다. 아니면 "목적지 없음"이다.
- `active_route_destination`: 문자열이 아니면 버리고, 64자로 자른다(`external_source.MAX_ROAD_NAME`과 같은 길이).
- 어떤 검증 실패도 예외가 아니라 "목적지 없음"으로 떨어진다.

---

## 실패 처리

모든 실패는 "목적지 없음" 또는 "직전 목적지 유지"로 끝난다. 제어 루프(1 Hz `KoreaMapData.tick`)는 이 스레드를 기다리지 않는다.

| 상황 | 처리 |
|---|---|
| `vehicle_data` 401 | 접근 토큰 갱신 후 다음 주기에 다시 |
| 갱신 거부 (토큰 엔드포인트 400·401) 또는 `vehicle_data` 403 | 조회 중단, `Offroad_KoreaTeslaAuth` 켬. 기기의 갱신 토큰 값이 바뀌면(설정 재실행) 재시작 없이 다시 시도하고, 갱신이 성공하면 알림을 끈다 |
| 408 (차 응답 없음) | 다음 주기에 다시 |
| 429 | 5분 쉰다 |
| 5xx, 네트워크 오류, 깨진 JSON | 다음 주기에 다시 |
| 일일 상한 300회 초과 | 그날 조회 중단. 이미 쓴 목적지와 경로는 그대로 간다. 날짜는 `RequestBudget`대로 UTC 기준이라 KST 09:00에 풀린다 |
| 스레드 안의 예상 못 한 예외 | 로그만 남기고 다음 주기 — `RouteSource._loop`(`route.py:424`)와 같은 이유 |

---

## 비용

테슬라 요금: 데이터 요청 500회당 $1, 계정당 월 $10 공제. 500 미만 상태코드는 전부 과금된다(408 포함).

| 항목 | 값 |
|---|---|
| 1시간 주행 | 60회 = $0.12 |
| 공제로 덮이는 주행 | 월 83시간 |
| 하루 상한 300회 | 주행 5시간 분량. 매일 상한까지 써도 월 9,000회 = $18 − $10 = $8 |

---

## 테스트 전략

`unittest`, 가짜 opener(`test_camera_refresh.py`의 `fake_opener` 패턴), 가짜 시계, 가짜 params. 네트워크 0.

### `korea/tests/test_tesla.py` (신규)

- 응답 읽기: 목적지 있음 / 필드 없음 / `null` / 한국 밖 / `bool` 좌표 / 긴 이름 자르기
- 쓰기 규칙: 새 목적지는 쓴다 / 같은 목적지는 도착으로 지워진 뒤에도 안 쓴다 / 50 m 이내 흔들림은 같은 목적지다 / 바뀐 목적지는 쓴다 / 차 안내가 끝나면 내 값만 지운다 / 남이 쓴 값은 남긴다 / 주행 시작마다 기억을 비운다
- 토큰: 401 → 갱신 → 새 갱신 토큰이 접근 토큰보다 먼저 저장된다 / 갱신 거부 → 알림 켜짐, 조회 중단 / 토큰 값이 바뀌면 재시도, 성공하면 알림 꺼짐
- 408·429·5xx 처리, 일일 상한, 주차 중 요청 0회, 인증값·TMap 키 없으면 요청 0회
- 비밀값이 URL에 없다 (요청 객체 검사)

### `tesla_setup.py`

로그인 주소 만들기와, 붙여넣은 주소에서 코드 뽑기·`state` 검증은 순수 함수로 떼어 테스트한다. 대화형 흐름 자체는 실제 설정으로 확인한다.

### 실차 확인

1. 설정 스크립트의 연결 확인이 통과한다.
2. NaviToTesla로 목적지를 보내고 1분 안에 `/data/params/d/NavDestination`이 채워진다.
3. 차에서 안내를 취소하면 1분 안에 비워진다.

---

## 작업량

| 항목 | 위치 | 줄수 |
|---|---|---|
| HTTP·토큰·응답 검증·폴링 스레드 | `korea/tesla.py` (신규) | ~150 |
| 1회 설정 도구 | `korea/tesla_setup.py` (신규) | ~100 |
| 스레드 시작·정지 | `mapd_manager.py` | ~8 |
| 파라미터 4개 | `params_keys.h` | 4 |
| 연결 끊김 알림 | `alerts_offroad.json` | 4 |
| 테스트 | `korea/tests/test_tesla.py` (신규) | ~200 |
| 설정 안내 (GitHub Pages 공개키, 개발자 앱 등록, 스크립트 실행) | `docs/korea_tesla_destination.md` (신규) | ~80 |

---

## 제약

- 신규 런타임 의존성 0. `urllib.request`만 쓴다.
- `openpilot/sunnypilot/mapd/korea/` 하위는 모듈 최상단에서 openpilot 스택(`cereal`, `common.params`, `cloudlog`, `alertmanager`)을 import하지 않는다. 스레드 함수와 스크립트의 `main()` 안에서만 허용한다.
- Python 들여쓰기 2칸. 신규 파일은 sunnypilot MIT 헤더로 시작한다.
- 새 파라미터는 `params_keys.h`에 등록해야 `Params().get()`이 동작하고, `params.cc`로 컴파일되므로 기기 재빌드가 필요하다.
- 실제 `client_id`, `client_secret`, 토큰, VIN은 저장소·문서·로그·커밋에 들어가지 않는다. 설정할 때 운전자가 직접 입력한다.
- 테슬라 엔드포인트는 한국이 속한 지역 값으로 고정한다: API `https://fleet-api.prd.na.vn.cloud.tesla.com`, 토큰 `https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token`, 로그인 `https://auth.tesla.com/oauth2/v3/authorize`.

---

## 범위 밖

- **테슬라가 계산한 경로선 자체.** Fleet Telemetry `RouteLine`은 공개 서버가 필요하다. 지금은 목적지만 받고 경로는 TMap이 만든다. 테슬라 경로와 TMap 경로가 갈리면 기존 이탈·재탐색(`route.py`)이 처리한다.
- **CAN `UI_navRouteActive`로 안내 종료를 즉시 감지.** 60초 지연이 문제로 드러나면 그때.
- **차량 여러 대.** 설정 때 고른 VIN 하나만 본다.
- **설정 화면의 연결 상태 표시.** 실패는 오프로드 알림으로만 알린다.
- **목적지를 받은 뒤 폴링 간격 늘리기.** 월 83시간까지 비용 0이라 지금은 이득이 없다.

---

## 근거

- 토큰 수명·1회용·갱신 필드: https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens
- 파트너 등록과 공개키 경로: https://developer.tesla.com/docs/fleet-api/endpoints/partner-endpoints
- 요금 공제·속도 제한: https://developer.tesla.com/docs/fleet-api/billing-and-limits , 단가: https://www.notateslaapp.com/news/2415/tesla-announces-api-pricing-third-party-service-costs-expected-to-rise
- `location_data` 없이는 `drive_state`에 좌표가 빠지는 사례: https://github.com/teslamotors/fleet-telemetry/issues/392
- `active_route_*` 필드: https://tesla-api.timdorr.com/vehicle/state/drivestate
- NaviToTesla 인증 방식·라이선스: https://github.com/zipizigi/NaviToTesla
