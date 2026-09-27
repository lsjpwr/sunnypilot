# 테슬라 목적지 수신 — owner API 모드 설계

**베이스:** `korea-dev` @ `23d378b97`

**Goal:** 테슬라 개발자 등록 없이, 운전자가 따로 발급한 owner API 토큰과 VIN만 Termius로 넣으면 기기가 차의 내비 목적지를 읽게 한다. 기존 Fleet API 경로(`2026-09-25-korea-tesla-destination-design.md`)는 그대로 두고, Fleet 설정이 있으면 Fleet을 쓴다.

**핵심 판단:** owner API는 비공식이고 2026년에 단계적으로 닫히는 중이다. 그래도 PC·개발자 등록·결제 등록 없이 폰만으로 설정할 수 있어서 둘 다 지원한다. 둘이 같이 있으면 공식이고 권한이 좁은 Fleet이 이긴다(2026-09-27 사용자 결정). 실패했을 때 다른 쪽으로 자동 전환하지는 않는다.

---

## 배경

- NaviToTesla는 owner API를 쓴다: 토큰 `https://auth.tesla.com/oauth2/v3/token`, `client_id=ownerapi`, `scope=openid email offline_access`(JSON 본문), API `https://owner-api.teslamotors.com`, 차량은 `/api/1/products`의 `id`로 부른다(`/api/1/vehicles/{id}/command/share`).
- 갱신 토큰은 한 번 쓰면 무효가 된다. NaviToTesla는 갱신할 때마다 새 토큰을 저장한다(`NaviToTeslaService.refreshToken`). 그래서 **기기는 NaviToTesla의 토큰을 같이 쓸 수 없다.** 같이 쓰면 먼저 갱신한 쪽이 다른 쪽을 끊는다. 기기용 토큰은 따로 발급한다.
- 2026년 owner API 상황:
  - 2026-04: 테슬라가 `ownerapi` 로그인의 redirect_uri를 바꿔 기존 토큰 발급 도구 대부분이 `redirect_uri not registered`로 막혔다. 갱신된 도구(tesla_auth 포크 등)는 된다.
  - 2026-05: owner API의 에너지 기기 주소가 401을 준다. 차량 주소는 NaviToTesla가 여전히 쓴다.
  - 2026-06: auth.tesla.com은 갱신 요청의 TLS 버전으로 토큰 종류를 정한다. TLS 1.3 미만이면 Fleet용 토큰을 주고, owner API는 그 토큰을 403으로 거부한다.
- Fleet API는 `ownerapi` 토큰을 받지 않는다(JWT audience가 다르다). 그래서 두 모드의 접근 토큰은 섞이지 않는다.

## 아키텍처

스레드는 그대로 하나(`TeslaDestinationSource`)다. 요청 부분만 모드별로 갈린다.

```
조회할 때마다 파라미터로 모드를 고른다
  Fleet 인증값 3개 (ClientId, RefreshToken, Vin) 있음 → Fleet   (기존 경로, 무수정)
  아니면 OwnerRefreshToken + Vin 있음                 → owner
  아니면                                              → 쉼

owner:
  POST auth.tesla.com/oauth2/v3/token (JSON, TLS 1.3)  → 접근 토큰 + 새 갱신 토큰(먼저 저장)
  GET  owner-api/api/1/products                        → VIN이 같은 항목의 id (접근 토큰마다 한 번)
  GET  owner-api/api/1/vehicles/{id}/vehicle_data?endpoints=drive_state;location_data
                                   │
                                   ▼
  parse_destination → _apply → NavDestination   (기존, 무수정)
```

쓰기 규칙, 하루 상한, 알림, 주행 중에만 묻는 조건, TMap 키 조건은 두 모드가 같이 쓴다.

---

## 컴포넌트

### 1. 모드 선택과 파라미터

| 키 | 플래그 | 이유 |
|---|---|---|
| `KoreaTeslaOwnerRefreshToken` | `PERSISTENT, STRING, ""` | 신규. `BACKUP` 제외, sunnylink `getParams` 차단(`REMOTE_READ_DENYLIST`). 계정 전체 권한 토큰이라 Fleet 토큰보다 더 조심한다 |
| `KoreaTeslaVin` | 기존 | 두 모드가 같이 쓴다 |

- 모드는 폴링 주기마다 다시 고른다. 운전자가 나중에 `tesla_setup`을 돌리면 재시작 없이 다음 주기부터 Fleet이 된다.
- 모드가 바뀌면 들고 있던 접근 토큰과 차량 id를 버린다.
- 거부 기억(`_rejected`)을 토큰 값 하나에서 **인증값 묶음**(모드, 토큰, VIN, Fleet이면 ClientId까지)으로 바꾼다. 묶음 중 하나라도 바뀌면 다시 시도한다. Termius로 VIN만 고치는 경우를 위해서이고, Fleet 모드에도 똑같이 적용된다.
- 새 갱신 토큰은 모드에 맞는 키에 `block=True`로 저장한다(Fleet: `KoreaTeslaRefreshToken`, owner: `KoreaTeslaOwnerRefreshToken`).

### 2. owner 요청

- 갱신: `POST https://auth.tesla.com/oauth2/v3/token`, `Content-Type: application/json`, 본문 `{"grant_type": "refresh_token", "client_id": "ownerapi", "refresh_token": ..., "scope": "openid email offline_access"}`. NaviToTesla가 2026-09에 쓰는 형식 그대로다. 400·401은 `AuthRejected`다.
- owner 요청은 전부 `minimum_version = TLSv1_3`인 SSL 컨텍스트로 보낸다. 갱신 요청이 TLS 1.2로 가면 owner API가 거부하는 토큰이 나온다.
- 차량 id: 새 접근 토큰마다 `GET /api/1/products`를 한 번 부른다. `vin`이 `KoreaTeslaVin`과 같은 항목의 `id`를 쓴다. `id`는 정수여야 하고, 경로에 넣을 때 `quote`한다(응답은 untrusted). 그 VIN이 없으면 오래가는 실패다.
- 목적지: `GET /api/1/vehicles/{id}/vehicle_data?endpoints=drive_state;location_data`, `Authorization: Bearer`. 응답 해석은 `parse_destination` 그대로다.
- 하루 상한(`DAILY_REQUEST_CAP` = 300)은 `vehicle_data`에만 적용한다. owner API는 요금이 없지만 상한은 폭주 방지로 남긴다. `products`는 접근 토큰마다(약 8시간) 한 번이라 세지 않는다.

### 3. 알림

`Offroad_KoreaTeslaAuth` 하나를 두 모드가 같이 쓴다. 문구에서 `tesla_setup`을 뺀다:

```
Tesla connection lost. Destinations set in the car's navigation are not received until the Tesla login is set up again.
```

### 4. 설정 — Termius (PC 불필요)

1. 기기용 owner 토큰을 **NaviToTesla와 따로** 발급한다(갱신된 발급 도구).
2. Termius에서 토큰을 넣는다. 명령 줄에 토큰이 없어 셸 기록에 남지 않고, 임시 파일에 쓴 뒤 `mv`로 바꿔 끼운다:
   ```bash
   read -rs V && printf '%s' "$V" > /data/params/d/.KoreaTeslaOwnerRefreshToken.tmp && mv -f /data/params/d/.KoreaTeslaOwnerRefreshToken.tmp /data/params/d/KoreaTeslaOwnerRefreshToken && unset V
   ```
3. VIN을 넣는다(비밀값 아님):
   ```bash
   read -r V && printf '%s' "$V" > /data/params/d/KoreaTeslaVin && unset V
   ```
4. 다음 주행에서 목적지가 1분 안에 `NavDestination`에 들어오면 끝이다.

`docs/korea_tesla_destination.md`에 "owner 모드 (PC 없이, 비공식)" 절을 더한다. 발급, 위 명령, 확인, 문제 해결, 나중에 Fleet으로 옮기는 법(`tesla_setup` 실행 → Fleet이 이김 → owner 토큰 파일 삭제), 끄는 법을 적는다.

---

## 신뢰 경계와 보안

- owner 토큰은 테슬라 앱 로그인과 같은 계정 권한이다. 새면 위치 추적과, 차종에 따라 명령까지 가능하다. 기존 설계의 "읽기 전용이라 새도 차를 못 움직인다"는 owner 모드에서는 성립하지 않는다. 안내 문서 첫머리에 적는다.
- 그래서 Fleet 토큰과 같은 규칙을 모두 지킨다: 로그·URL에 싣지 않는다, `BACKUP` 제외, sunnylink `getParams` 차단, 실패 로그는 HTTP 상태나 예외 타입만.
- `products` 응답의 `id`와 `vehicle_data` 응답은 untrusted다. `id`는 정수만 받고 `quote`해서 경로에 넣는다.

---

## 실패 처리

Fleet 표(`2026-09-25-korea-tesla-destination-design.md` 실패 처리 절)를 그대로 따른다. owner 모드에서 새로 생기는 경우:

| 상황 | 처리 |
|---|---|
| owner 갱신 400·401 | 갱신 거부. 조회 중단 + 알림. 토큰을 다른 앱(NaviToTesla 등)이 이미 썼거나 폐기됐다 |
| owner API 403 | 조회 중단 + 알림. 이 계정에서 owner API가 막혔거나 Fleet용 토큰이 나왔다. 안내 문서는 Fleet 설정을 권한다 |
| `products`에 그 VIN이 없음 | 조회 중단 + 알림. VIN을 고치면 인증값 묶음이 바뀌어 다시 시도한다 |
| 그 밖의 경우 | Fleet 표와 같다(408·5xx·네트워크: 다음 주기, 429: 5분, 오래된 접근 토큰의 401: 다음 틱에 갱신) |

---

## 비용

owner API는 요금이 없다. Fleet 모드의 비용은 그대로다.

---

## 테스트 전략

`korea/tests/test_tesla.py`에 더한다. 가짜 opener는 `context` 인자를 받아 기록한다.

- 모드 선택: Fleet 3개만 → Fleet / owner 토큰 + VIN만 → owner / 둘 다 → Fleet / VIN 없음 → 요청 0회
- owner 갱신: auth.tesla.com에 JSON 본문, `client_id=ownerapi`, `scope`, 토큰이 URL에 없음, TLS 1.3 컨텍스트가 넘어감
- 새 owner 갱신 토큰이 `KoreaTeslaOwnerRefreshToken`에 `block=True`로 먼저 저장됨
- 차량 id: VIN으로 찾음 / 정수가 아닌 id는 거부 / VIN 없음 → 알림 + 조회 중단
- owner `vehicle_data` 경로가 owner-api 호스트와 id를 씀
- 모드 전환(owner → Fleet)이 접근 토큰을 버림
- 거부 뒤 VIN만 바뀌어도 다시 시도함
- sunnylink `getParams`가 `KoreaTeslaOwnerRefreshToken`을 돌려주지 않음(컨테이너, `athena/tests/test_sunnylinkd.py`)

### 실차 확인

1. 따로 발급한 owner 토큰과 VIN을 Termius로 넣는다.
2. NaviToTesla로 목적지를 보내고 1분 안에 `NavDestination`이 채워진다.
3. NaviToTesla도 계속 동작한다(토큰을 나눠 쓰지 않았다는 확인).

---

## 제약

기존 설계의 제약을 모두 따른다. 추가로:

- 새 의존성 0. TLS 설정은 표준 라이브러리 `ssl`만 쓴다.
- owner 모드 상수: 토큰 `https://auth.tesla.com/oauth2/v3/token`, API `https://owner-api.teslamotors.com`, `client_id` `ownerapi`, `scope` `openid email offline_access`.
- 테스트는 `OWNER-1`, `OWNER-2` 같은 눈에 띄는 가짜 토큰만 쓴다.

---

## 범위 밖

- **실패 시 다른 모드로 자동 전환.** 사용자가 "Fleet 우선, 없으면 owner"를 골랐다(2026-09-27).
- **기기에서 owner 토큰 발급.** 새 로그인은 `tesla://auth/callback` 같은 앱 스킴으로 돌아와 브라우저만으로는 받기 어렵다. 발급은 외부 도구에 맡긴다.
- **설정 직후 바로 확인하는 기기용 스크립트.** 첫 주행 확인으로 부족하면 그때 추가한다.
- **owner API 명령.** 읽기만 한다.

---

## 근거

- NaviToTesla 토큰 요청(`client_id`, `scope`): https://github.com/zipizigi/NaviToTesla/blob/HEAD/app/src/main/kotlin/me/zipi/navitotesla/model/TeslaRefreshTokenRequest.kt
- NaviToTesla 호스트: https://github.com/zipizigi/NaviToTesla/blob/HEAD/app/src/main/kotlin/me/zipi/navitotesla/AppRepository.kt
- NaviToTesla 차량 주소와 `products`: https://github.com/zipizigi/NaviToTesla/blob/HEAD/app/src/main/kotlin/me/zipi/navitotesla/api/TeslaApi.kt
- NaviToTesla가 갱신 때 새 토큰 저장: https://github.com/zipizigi/NaviToTesla/blob/HEAD/app/src/main/kotlin/me/zipi/navitotesla/service/NaviToTeslaService.kt
- redirect_uri 변경(2026-04): https://github.com/teslamate-org/teslamate/issues/5296 , https://github.com/teslamate-org/teslamate/discussions/5295
- 에너지 주소 401, `ownerapi` 토큰의 Fleet 거부(2026-05): https://github.com/springfall2008/batpred/issues/3965
- TLS 1.3과 토큰 종류(2026-06): https://snowake.dev/posts/tesla-owner-api-tls-version/
- 갱신 토큰 1회용: https://teslamotorsclub.com/tmc/threads/refreshing-the-api-token.266016/
