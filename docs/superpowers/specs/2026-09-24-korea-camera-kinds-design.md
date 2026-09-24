# 과속카메라 종류별 감속·도달 여유 거리·써니링크 재배치 설계

**베이스:** `korea-dev` @ `59b5c25a5`

**Goal:** 과속카메라 감속을 카메라 종류별로 켜고 끌 수 있게 하고, 카메라 몇 m 앞에서 제한속도에 도달할지 고를 수 있게 한다. 둘 다 써니링크에서 바꿀 수 있어야 한다. 써니링크 프론트엔드가 보여 주지 못하는 `korea` 페이지는 없애고, 그 안팎의 한국 지도 설정을 Cruise 페이지에 기능별로 다시 놓는다.

**핵심 판단 둘:**

1. **"종류"는 고정식/이동식이 아니라 단속 종류다.** 공공데이터(전국무인교통단속카메라표준데이터)는 정의부터 "고정식 무인교통단속카메라, 이동식 제외"다. DB에 이동식이 없으니 이동식 때문에 감속하는 일은 지금도 없다. 데이터가 실제로 구분해 주는 것은 과속 / 신호·과속 / 구간단속 / 보호구역이고, 토글은 이 넷으로 만든다.
2. **카메라 감속은 SLA가 아니라 SCC-Map으로 보낸다.** 카메라 감속은 "지점 앞에서 줄였다가 지나면 원래대로"인 일시 감속이다. 방지턱·경로 커브와 같은 성격이고, 그 둘은 이미 `MapTargetVelocities` → SCC-Map으로 간다. SLA는 계속 유지되는 법정 제한속도를 따라가도록 설계돼 있어서, 카메라 하나를 제한속도 변경 두 번(내려감·올라감)으로 보고 둘 다 운전자 확인 절차에 건다(아래 "배경" 참고).

---

## 배경: 지금 카메라 감속이 동작하는 방식

| 단계 | 위치 |
|---|---|
| CSV/API → `korea_cameras.sqlite` (`lat, lon, limit_kph, section_m`만 저장, 제한속도 > 0인 행만) | `openpilot/sunnypilot/mapd/korea/build_db.py:152`, `:182`, `:87` |
| 주 1회 API 재생성 (`KoreaMapApiKey`, 비종량제 망) | `openpilot/sunnypilot/mapd/korea/camera_refresh.py:95`, `:150` |
| 2 km·±60°·경로 30 m 안의 가장 가까운 카메라 | `openpilot/sunnypilot/mapd/korea/db.py:324` |
| 카메라 제한속도·거리를 `liveMapDataSP.speedLimitAhead/Distance`로 발행 | `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py:229` |
| −1.0 m/s² 등감속 가정 거리 안이면 목표를 카메라 제한속도로 교체 | `openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_resolver.py:135` |
| SLA(Assist 모드)가 크루즈 목표를 낮춤 | `openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py:135` |
| 플래너가 cruise·SCC-V·SCC-M·SLA 중 최솟값 채택 | `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py:76` |

이 경로의 문제 셋:

- **80 km/h 미만 제한속도는 운전자 확인이 필요하다.** `apply_confirm_speed_threshold`(`speed_limit_assist.py:198`)가 새 제한속도가 `CONFIRM_SPEED_THRESHOLD`(80 km/h, `speed_limit/__init__.py:47`) 미만이면 preActive로 보낸다. non-PCM 롱컨트롤 차는 preActive 동안 제한을 풀고(`get_v_target_from_control`은 active 상태에서만 제한속도를 낸다), 5초 안에 −/SET 버튼을 누르지 않으면 inactive가 된다. PCM 롱컨트롤 차(테슬라 알파 롱 포함 — `pcmCruise` 기본값 True)는 15초 안에 크루즈 설정속도를 맞추지 않으면 inactive가 되고, PCM 경로의 inactive는 재인게이지 전까지 빠져나오지 않는다. 도심 카메라(30~70 km/h)는 거의 전부 여기에 걸린다.
- **속도 오프셋이 카메라에도 붙는다.** SLA 목표는 `speed_limit_final = 제한속도 + 오프셋`이다.
- **감속 시작점이 카메라 위치에서 딱 도달하도록 잡혀 있다.** −1.0 m/s² 가정 거리(100→60 km/h면 약 247 m)에서 시작하고, 실제 감속은 플래너가 최대 −1.2 m/s²·저크 제한으로 한다. 목표 근처에서 감속이 느슨해지고 mapd가 1 Hz라서 카메라 지점에서 몇 km/h 초과할 여지가 있다.

구간단속에는 전용 로직이 없다. 시점·종점 카메라가 점 카메라로만 처리되고, 그 사이는 링크 제한속도(+오프셋)를 따른다. 구간 유지는 이 설계 범위 밖이다("범위 밖" 참고).

---

## 아키텍처

변경 전:

```
korea_cameras.sqlite ─ next_camera ─▶ liveMapDataSP.speedLimitAhead ─┬─▶ Resolver ─▶ SLA ─▶ planner min()
                                                                      └─▶ HUD 앞쪽 표지
방지턱 + 경로 커브 ─────────────────▶ MapTargetVelocities ─▶ SCC-Map ─▶ planner min()
```

변경 후:

```
korea_cameras.sqlite (+kind) ─ next_camera(kinds) ─┬─▶ liveMapDataSP.speedLimitAhead ─▶ HUD 앞쪽 표지
                                                   │        (resolver는 한국 모드에서 ahead 무시)
                                                   └─▶ 카메라 점 (여유 거리만큼 앞) ─┐
방지턱 + 경로 커브 ────────────────────────────────────────────────────────────────┴─▶ MapTargetVelocities ─▶ SCC-Map ─▶ planner min()
링크 max_spd ─▶ liveMapDataSP.speedLimit ─▶ Resolver ─▶ SLA (도로 제한속도만)
```

경계:

- **`build_db.py`** — 원본 행을 종류로 분류해 `kind`를 기록한다. 분류 규칙은 여기 한 곳에만 있다.
- **`db.py`** — 켜진 종류만 후보로 삼아 다음 카메라를 고른다. `kind`가 없는 구 DB도 연다.
- **`korea_map_data.py`** — 파라미터를 읽고, 고른 카메라를 표시(`speedLimitAhead`)와 감속(`MapTargetVelocities`) 두 곳으로 내보낸다.
- **`speed_limit_resolver.py`** — 한국 모드에서 ahead 값을 무시하는 두 줄. 업스트림 파일에 대는 유일한 로직 변경이다.
- **`map_controller.py`** — 한국 모드 활성 조건에 카메라 토글을 더한다.

---

## 컴포넌트

### 1. 카메라 분류 — `build_db.py`, `db.py`

`cameras` 테이블에 컬럼 하나를 더한다.

```sql
kind INTEGER NOT NULL  -- CAMERA_SPEED 0, CAMERA_SIGNAL 1, CAMERA_SECTION 2, CAMERA_ZONE 3
```

상수는 `BUMP_*`처럼 `db.py`에 둔다(`build_db.py`가 이미 `db.py`에서 `BUMP_*`를 가져온다 — 빌더 → 런타임 방향 유지).

**분류: 카메라 하나는 종류 하나, 구체적인 쪽이 이긴다.** 보호구역 > 구간단속 > 신호·과속 > 과속.

| 종류 | 조건 |
|---|---|
| 보호구역 | `보호구역구분`(API `prtcareaType`)이 1(노인) 또는 2(어린이) |
| 구간단속 | `단속구간위치구분`(API `regltSctnLcSe`)이 1(시점) 또는 2(종점) |
| 신호·과속 | `단속구분`(API `regltSe`)에 2(신호) 포함 |
| 과속 | 나머지 전부 (모르는 코드 포함) |

세 필드 모두 `'2'`, `'02'`, `'01+02'`, `'1+2'`처럼 0 채움과 조합이 섞여 있다(`build_db.py:160` 주석). `+`로 나누고 각 조각을 정수로 읽어 집합으로 비교한다. 읽을 수 없는 조각은 버린다.

**코드 집합은 실데이터로 확정했다 (2026-09-24, 계획 작성 중).** data.go.kr 표준 페이지에는 세 필드의 코드표가 없다. 그래서 로컬에 받아 둔 API 전체 덤프(`E:/dev/korea_map_data/cameras_raw.json`, 2026-08-19 수집, 43,347행)에서 `keep_camera`를 통과하는 33,415행의 교차표와 `설치장소` 문구로 판정했다. API 키는 쓰지 않았고, 구현에도 필요 없다.

| 필드 | 값: 행 수 | 판정 근거 |
|---|---|---|
| `단속구분` | 2: 17,735 · 1: 11,497 · 99: 1,404 · 4: 906 · 02: 570 · 04: 503 · 01: 458 · 01+02: 321 · 1+2: 12 · 3: 7 · 03: 2 | 설치장소에 교차로·사거리·삼거리가 든 비율이 2는 45%, 1은 20%이고, 1은 학교 앞이 많다. 그래서 1은 과속, 2는 신호다. 2도 전부 제한속도가 있으므로 신호·과속 겸용 카메라다. 3·4는 제한속도가 있는 행이 10~18%뿐인 다른 위반이다. 99의 74%(1,059행)는 구간단속 시점·종점이다 |
| `단속구간위치구분` | 빈칸: 32,314 · 2: 542 · 1: 530 · 01: 22 · 02: 7 | 설치장소에 "시점"이 든 비율이 1은 69%, "종점"이 든 비율이 2는 67%다. 1은 시점, 2는 종점이다 |
| `보호구역구분` | 99: 15,641 · 2: 13,445 · 빈칸: 3,393 · 02: 503 · 1: 418 · 01: 15 | 2는 91%가 30 km/h이고 51%의 설치장소에 "학교"가 들어 있어 어린이보호구역이다. 1은 32%에 "노인"이 들어 있어 노인보호구역이다. 99는 해당없음이다. 3 이상의 코드는 없다 |

우선순위를 적용한 종류별 행 수: 보호구역 14,381 · 신호·과속 10,659 · 과속 7,287 · 구간단속 1,088. 겹치는 행은 보호구역∩신호 7,977행, 보호구역∩구간 13행이고, 둘 다 보호구역으로 분류된다.

**`과속단속구간길이` > 0 보조 규칙은 뺐다.** 위치구분은 빈칸인데 길이가 있는 116행은 모두 경기도 안성시청이 제출했다. 단속구분은 모두 1, 길이는 모두 45이고, 설치장소는 학교 앞과 교차로다. 이 116행은 구간단속이 아니라 한 기관의 입력 관행이다. 그래서 구간단속은 `단속구간위치구분`만으로 판정한다.

`load_cameras`와 `load_cameras_api`는 `(lat, lon, limit_kph, section_m, kind)`를 낸다. CSV 경로는 `CAMERA_COLUMNS`에 세 컬럼(`단속구분`, `단속구간위치구분`, `보호구역구분`)을 더하고, API 경로는 `regltSe`, `regltSctnLcSe`, `prtcareaType`를 읽는다. 분류 함수 `classify_camera`는 두 경로가 함께 쓴다 — `keep_camera`와 같은 이유(두 벌은 어긋난다).

**이름이 바뀐 컬럼은 조용히 넘기지 않는다.** 분류 컬럼 하나가 사라지면 `row.get()`이 `None`을 돌려주고 모든 행이 "과속"이 된다 — 방지턱 로더가 컬럼 셋을 전부 검사하는 것(`build_db.py:220`)과 같은 위험이다. CSV 경로는 헤더 검사를 위도 하나에서 분류 컬럼까지 넓혀 `KeyError`로 실패한다. 그래서 4컬럼으로 잘라 둔 CSV(`E:/dev/korea_map_data/`에 있는 것)는 더 이상 빌드되지 않는다. data.go.kr의 원본 CSV가 필요하다. API 경로는 분류 필드 셋(`regltSe`, `regltSctnLcSe`, `prtcareaType`) 중 하나라도 받은 항목 어디에도 없으면 `refresh()`가 새 DB를 쓰지 않고 기존 DB를 유지한다(경고 로그). `refresh()`의 "never raises" 약속(`camera_refresh.py:95`)은 그대로 지킨다.

**`SCHEMA_VERSION`은 올리지 않는다.** 세 DB가 같은 값(`db.py:23`)을 공유하므로, 올리면 220 MB 링크 DB와 방지턱 DB까지 거부된다. 대신 카메라 연결을 열 때 `PRAGMA table_info(cameras)`로 `kind` 유무를 보고 기억한다. `_reload_one`(`db.py:228`)이 카메라 파일을 새로 열 때도 다시 본다. `kind`가 없는 DB의 카메라는 종류 모름으로 취급해 **필터를 항상 통과**시킨다 — 즉 지금 동작 그대로다.

### 2. 카메라 선택 — `db.next_camera`

```python
def next_camera(self, lat, lon, heading_deg, route=None, kinds=None) -> Camera | None:
```

`kinds`가 주어지면 그 집합에 없는 종류는 거리 비교 전에 건너뛴다. 그래서 가까운 꺼진 카메라가 더 먼 켜진 카메라를 가리지 않는다. `kinds=None`은 전부 허용(기존 호출과 테스트 호환). 종류 모름(구 DB)은 항상 허용. 넷을 모두 끄면 카메라가 없는 것과 같다.

`Camera`에 `lat`, `lon`, `kind: int | None`을 더한다(`None` = 구 DB라 종류 모름). 감속 점을 만들려면 좌표가 필요하다.

### 3. 파라미터와 발행 — `korea_map_data.py`

새 파라미터 다섯(`params_keys.h`, 모두 `PERSISTENT | BACKUP`):

| 키 | 타입 | 기본값 |
|---|---|---|
| `KoreaCameraSpeedEnabled` | BOOL | `"1"` |
| `KoreaCameraSignalEnabled` | BOOL | `"1"` |
| `KoreaCameraSectionEnabled` | BOOL | `"1"` |
| `KoreaCameraZoneEnabled` | BOOL | `"1"` |
| `KoreaCameraMargin` | INT (m) | `"50"` |

`read_bump_params`(`korea_map_data.py:243`)와 같은 자리에서 1 Hz로 읽는다. 여유 거리는 `get_sanitize_int_param`으로 `CAMERA_MARGIN_RANGE = (0, 300)`에 가둔다(방지턱 속도와 같은 방식 — 경계는 코드, 기본값은 `params_keys.h`). 기본 50 m인 이유: 단속 검지기(루프 센서)가 카메라 기둥보다 수십 m 앞에 있는 경우가 많고, `LastGPSPosition`이 1 Hz라서 SCC-Map이 보는 거리가 최대 1초 분량 늦다.

`update_location`은 켜진 종류 집합을 `next_camera(..., kinds=...)`에 넘긴다.

**표시:** `get_next_speed_limit_and_distance`는 그대로 카메라 제한속도와 **실제 거리**를 낸다. HUD 앞쪽 표지와 거리 표시는 변하지 않는다. 꺼진 종류는 후보에서 빠졌으므로 표시에도 나오지 않는다("완전 무시").

**감속:** `publish_targets`(`korea_map_data.py:253`)가 카메라 점을 방지턱·커브 점과 같은 리스트에 넣는다. 조건은 방지턱과 같다 — `localizer_valid`이고 위치가 있을 때만.

- 점 위치: 차에서 카메라로 가는 직선 위, 차에서 `max(0, 거리 − 여유)` 떨어진 곳. 위경도 선형 보간이면 충분하다(2 km 이내).
- 이미 여유 구간 안이면(거리 ≤ 여유) 점은 차 위치다. SCC-Map은 거리 0짜리 점을 계속 유효로 보므로 카메라를 지날 때까지 제한속도 부근을 유지한다. 정확히는 SCC-Map이 `v_ego`가 목표 아래로 내려가면 점을 놓고(`map_controller.py:161`) 다시 넘으면 잡는다 — 방지턱·커브와 같은 동작이고, 속도는 목표 근처에서 머문다. 카메라가 `next_camera`에서 빠지는 순간(진행방향 ±60° 밖) 점도 사라진다.
- 속도: 카메라 제한속도(m/s) 그대로. 오프셋 없음.

SCC-Map은 `TARGET_JERK`/`TARGET_ACCEL`(`map_controller.py:22-23`)로 필요한 거리를 구하고 목표속도 × `TARGET_OFFSET`(1초)을 더해 시작한다. 그래서 실제 도달 지점은 "여유 거리 + 약 1초 분량" 앞이다. 설정 설명에는 "약 N m 앞에서 제한속도 도달"로 적는다.

모듈 머리 주석(`korea_map_data.py:7-13`)의 "SpeedLimitAssist already slows for speedLimitAhead" 문단은 새 경로에 맞게 고친다.

### 4. SLA에서 카메라 떼기 — `speed_limit_resolver.py`

`_process_map_data`(`speed_limit_resolver.py:122`)에서 한국 모드면 `next_speed_limit`을 0으로 둔다. `MapDataSource`는 `update_params`(`:92`)의 기존 주기로 읽는다.

결과: SLA는 한국 모드에서 도로 제한속도(`speedLimit`)만 따른다. 카메라 확인 팝업이 사라지고, 오프셋이 카메라에 붙지 않는다. OSM 모드는 한 글자도 달라지지 않는다. DEC(`dec.py:331`)는 현재 제한속도만 읽으므로 영향이 없다.

### 5. SCC-Map 활성 조건 — `map_controller.py`

`_get_enabled`(`map_controller.py:100`)의 한국 분기에 "카메라 종류 토글 중 하나라도 켜짐"을 더한다. 빈 목록은 지금도 no-op이므로 켜 두는 비용은 없다. 독스트링의 "one toggle per source" 설명도 함께 고친다.

### 6. 설정 화면 세 곳

**활성 조건:**

- 종류 토글 넷: 맵 소스가 Korea. 롱컨트롤 게이트는 두지 않는다 — 토글이 HUD 표시도 거르므로 롱컨트롤이 없는 차에도 의미가 있다.
- 여유 거리: 맵 소스가 Korea이고 롱컨트롤 또는 ICBM. 감속에만 영향을 준다(방지턱 속도와 같은 규칙).
- 오프로드 제한 없음. 1 Hz로 다시 읽는다.

**써니링크 — `settings_ui_src/pages/cruise.yaml`, `korea.yaml` 삭제.** Speed Limits와 Smart Cruise Control 사이에 섹션 셋을 새로 둔다.

```
Speed Limits › Speed Limit Settings
    Mode / Source / Map Data Source / Offset Type / Offset Value
Korea Speed Cameras
    Speed Cameras / Signal + Speed Cameras / Section Enforcement / Protected Zones   (toggle)
    Camera Arrival Margin 0–300 m, step 10, unit "m"                                (option)
    Speed Camera API Key                                                            (text)
Korea Speed Bumps
    Speed Bump Slowdown / Arch Bump Target Speed / Flat-Top Bump Target Speed       (규칙 그대로 이동)
Korea Route & Map Data
    External Navigation Input / Route API Key / Download Map Data Automatically     (규칙 그대로 이동)
Smart Cruise Control
```

- 옮기는 항목은 `enablement`/`visibility` 규칙을 그대로 가져간다. `test_settings_changes.py`의 게이트 검사가 항목 단위이므로 섹션 단위 규칙으로 바꾸지 않는다.
- API 키 두 칸(`widget: text`)은 현재 프론트엔드(`sunnylink-frontend` @ `7edbc84`)에서 빈 행이 된다. `SchemaItemRenderer.svelte`의 위젯 분기에 `text`가 없어 입력 요소가 그려지지 않고, 값을 쓰는 경로도 없다. 프론트엔드가 `text`를 지원하면 이 자리에서 바로 보인다.
- `settings_ui.json`은 `compile_settings_ui.py`로 다시 만든다. 손으로 고치지 않는다.
- `test_compile_settings_ui.py:130`의 패널 기대 집합에서 `korea`를 뺀다.

**tici — `speed_limit_settings.py`.** 종류 토글 넷과 여유 거리 옵션(`option_item_sp`, `value_change_step=10`, 라벨 "50 m")을 방지턱 항목(`:119`) 위에 둔다. 활성 조건은 `_update_state`(`:241`)에 위 규칙대로 더한다. 기기 화면 구조는 그대로다.

**mici — `toggles.py`.** 종류 토글 넷만 더한다. 여유 거리는 방지턱 속도처럼 mici에 두지 않는다.

**설명 문구(영문, 세 곳 공통):**

| 항목 | 설명 |
|---|---|
| Speed Cameras | Slow down for fixed cameras that enforce speed only. |
| Signal + Speed Cameras | Slow down for intersection cameras that enforce both red lights and speed. |
| Section Enforcement | Slow down at the start and end cameras of average-speed sections. The speed between them is not held. |
| Protected Zones | Slow down for cameras in school and senior protection zones. A camera inside a zone follows this toggle whatever else it enforces. |
| Camera Arrival Margin | Reach the camera's limit about this far before the camera. Speed detectors often sit tens of metres ahead of the pole. |

종류 토글 넷에는 공통으로 "Turning a type off also hides its cameras from the speed limit ahead sign."를 붙인다.

### 7. 구 DB 재생성 — `camera_refresh.py`

`CameraRefresher._due`(`camera_refresh.py:150`)는 카메라 DB에 `kind`가 없으면 `True`를 돌려준다. 업데이트 후 첫 비종량제 연결에서 주 1회 주기를 기다리지 않고 새 스키마로 다시 만든다. API 키가 없으면 지금처럼 로그만 남기고 1시간 뒤 다시 본다 — 그런 사용자는 PC에서 `build_db --cameras`로 다시 빌드해 복사한다. `kind` 유무 확인은 `db.py`의 함수 하나를 두 곳(`KoreaMapDB`, `CameraRefresher`)이 같이 쓴다.

---

## 기존 대비 달라지는 동작

- 카메라 감속이 SLA 모드와 무관해진다. 롱컨트롤 또는 ICBM이 있고 종류 토글이 켜져 있으면 동작한다. 기본값이 전부 ON이므로 SLA를 Information 모드로 쓰던 경우에도 업데이트 후 카메라 감속이 시작된다.
- 80 km/h 미만 카메라에서 확인 버튼이 필요 없다. PCM 차에서 카메라 하나를 놓쳐 SLA가 꺼지는 일도 없다.
- 카메라 목표는 오프셋 없이 제한속도 그대로다.
- SLA 경고 모드의 "카메라 제한속도 초과" 경고가 없어진다. HUD 앞쪽 표지는 남는다.
- 외부 내비 앱이 보내는 `next_speed_limit`도 한국 모드에서는 표시 전용이 된다(resolver가 ahead를 무시). 지금은 보내는 앱이 없어 영향이 없다.

## 실패 처리

- `kind` 없는 구 DB: 종류 모름 = 항상 통과. 토글은 DB가 다시 만들어질 때까지 효과가 없고, 감속 자체는 지금처럼 된다.
- 파라미터 이상값: 여유 거리는 `(0, 300)`으로 잘린다. BOOL은 `get_bool`의 기존 동작.
- 카메라 조회 예외: `update_location`의 기존 광역 예외 처리(`korea_map_data.py:209`)가 그대로 덮는다.
- 위치 무효: `publish_targets`의 기존 `localizer_valid` 조건 안에서만 카메라 점을 넣으므로, 위치가 멈추면 점도 빠진다.

## 테스트

단위 테스트는 `unittest`다. 환경은 `docs/superpowers/plans/2026-09-18-korea-route-nav.md`의 "테스트 환경" 절을 따른다.

- **호스트 Python (`korea/tests`)**
  - `test_build_db`: 조합 표기(`01+02`, `1+2`, `02`), 우선순위(보호구역 안 신호·과속 → 보호구역), 모르는 코드 → 과속, CSV와 API 두 경로가 같은 `kind`를 기록, 분류 컬럼이 빠진 CSV → `KeyError`.
  - `test_db`: `kinds` 필터, 가까운 꺼진 카메라가 먼 켜진 카메라를 가리지 않음, `kind` 없는 구 DB는 전부 통과, 재적재 후 `kind` 유무 재판정.
  - `test_camera_refresh`: `kind` 없는 DB → `_due()`가 `True`, 분류 필드 하나가 빠진 API 응답 → 기존 DB 유지하고 예외 없음.
- **`sp-build` 컨테이너 (cereal 필요)**
  - `test_korea_map_data`: 파라미터 → 종류 집합과 여유 거리(범위 밖 값 잘림), 카메라 점 위치(거리 > 여유, 거리 ≤ 여유), 방지턱·커브와 병합 후 거리순 정렬, `speedLimitAhead`는 실제 거리.
  - `test_map_controller`: 한국 분기가 카메라 토글로도 켜짐.
  - `test_speed_limit_resolver`: 한국 모드는 ahead 무시, OSM 모드는 그대로.
  - 써니링크: `korea` 패널 없음, 새 키 다섯의 존재와 게이트, 옮긴 항목의 규칙 보존.
  - `params_keys.h`를 바꾼 뒤에는 scons로 `openpilot/common`을 다시 빌드한다.
- **회귀 기준:** OSM 모드 동작 100% 동일. 한국 모드의 방지턱·경로 커브 감속 동일.
- **실차 확인(사용자):** 아는 카메라에서 HUD 앞쪽 표지, `SCC-M` 배지, 약 50 m 앞 도달, SLA 확인 팝업 없음. 써니링크 Cruise 페이지에 새 섹션 셋.

## 범위 밖

- **구간 유지** — 시점 통과부터 종점 통과까지 구간 제한속도를 유지하는 것. 시점(1)·종점(2) 코드는 실데이터로 확인됐다(1절의 검증 결과). 별도 스펙으로 한다.
- **이동식 카메라** — 공공데이터에 없다. 외부 내비 앱 와이어 포맷에 카메라 종류 키를 더하는 일은 보내는 앱이 생길 때 한다.
- **써니링크 프론트엔드의 `text` 위젯** — 이 저장소 밖(`sunnypilot/sunnylink-frontend`)의 일이다.
- **기기 UI 재배치** — 요청 범위는 써니링크뿐이다.
