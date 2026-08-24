# vtb-sla 베이스 전환과 OSM 복원

작성: 2026-08-24
브랜치: `korea-dev`

## 배경

`korea-dev`는 지금까지 `upstream/master`(sunnypilot) 위에서 한국 공공 지도 데이터 파이프라인과
운전자 감시 모드를 구현해왔다(31커밋). 그 과정의 Task 9가 OSM 스택을 통째로 삭제했는데,
이는 요구사항을 잘못 읽은 것이다. 사용자가 원한 것은 **"지도 데이터의 출처를 OSM에서 한국
공공데이터로 대체"**이지 **"OSM 기능을 제거"**가 아니었다.

동시에 별도 조사에서 두 가지가 확인됐다.

1. Tesla 안전 수정 — 차량이 스스로 조향할 때(Emergency Lane Departure Avoidance, Autopark)
   openpilot이 이를 인식하지 못하는 결함 — 이 **opendbc 서브모듈**에 있다. openpilot 본체만
   체리픽해서는 가져올 수 없다.
2. DEC Map 모드는 `liveMapDataSP.speedLimit`만 읽으므로, 이 브랜치가 만든 한국 DB를 코드
   수정 없이 소비한다.

두 가지 모두 `bfayers/vtb-sla-sunnylink-testing` 브랜치를 베이스로 삼아야 얻을 수 있다.

## 목표

1. 베이스를 `upstream/master` → `bfayers/vtb-sla-sunnylink-testing`으로 전환한다
2. Task 9가 삭제한 OSM 스택을 복원한다
3. 두 지도 소스가 충돌 없이 공존하게 한다
4. 한국 설정 2개에 온디바이스 UI를 준다

## 비목표

- 한국 링크 DB에서 커브 목표속도(`MapTargetVelocities`) 계산 — 커브는 SCC-Vision과 osm 모드가 덮는다
- `vtb-sla`의 나머지 기능을 개별 검증 — 베이스로 통째 수용하고, 검증은 디바이스 단계에서 한다
- 안드로이드 내비 릴레이 앱 — 범위 밖(UDP 입력구만 제공)
- Tesla Model Y Juniper 2026 핑거프린트 확정 — 실차 확인이 선행되어야 한다

---

## 1. 베이스 전환

### 고정된 SHA

bfayers는 force-push를 한다(2026-08-24 fetch에서 3개 브랜치 강제 갱신, 4개 삭제 관측).
따라서 브랜치 이름이 아니라 SHA를 로컬 태그로 고정하고 그 위에서 작업한다.

| 저장소 | 태그 | SHA |
|---|---|---|
| sunnypilot | `vtb-base-20260824` | `07b2ed06d` |
| opendbc | `vtb-opendbc-20260824` | `8bd5af67f` |
| panda | `vtb-panda-20260824` | `f6887ad012` |

### 서브모듈 URL

`vtb-base`의 `.gitmodules`는 상대경로를 쓴다.

```
[submodule "panda"]     url = ../panda.git
[submodule "opendbc"]   url = ../opendbc.git
```

상대경로는 부모 저장소 URL 기준으로 해석되므로, 디바이스가 `lsjpwr/sunnypilot`에서 받으면
`lsjpwr/opendbc`와 `lsjpwr/panda`를 찾는다. 이 둘을 `bfayers/*`에서 포크해 생성했고, 고정
SHA가 각 포크에서 ref로 도달 가능함을 확인했다.

이 방식을 택한 이유는 bfayers가 브랜치를 삭제해도(실제로 `-rl` 계열 3개가 삭제됐다) 사본이
남기 때문이다. 절대 URL로 `bfayers/*`를 직접 가리키는 대안은 그 위험에 계속 노출된다.

### 전환 방법: 리플레이 후 복원

31커밋을 `vtb-base` 위로 그대로 replay한다. 워크트리에서 실측한 결과 **충돌 0**이다.

대안으로 리베이스 중에 Task 9를 "OSM 유지 + 한국 소스 추가"로 고쳐 쓰는 방법이 있으나
채택하지 않는다. 그 커밋 하나가 22개 파일을 건드리고 이후 6개 커밋이 그 결과 위에 쌓여
있어, 중간 수술의 위험이 복원 커밋을 따로 두는 비용보다 훨씬 크다. 히스토리에 "삭제 후
복원"이 남지만 그것이 실제로 일어난 일이며, 각 단계가 독립적으로 검증된다.

### 베이스에서 물려받는 결함

`vtb-base`는 자체적으로 ruff를 통과하지 못한다. 세 건 모두 리베이스 이전부터 존재하며
반드시 고쳐야 한다.

| 위치 | 오류 | 출처 커밋 | 영향 |
|---|---|---|---|
| `cruise.py:85` | F821 `multiple_button_item_sp` 미정의 | `fc8b7fd74` | **런타임 크래시** |
| `vision_controller.py:15` | F401 미사용 import | `b5b4a0388` | CI 실패 |
| `hud_renderer.py:284` | E501 178자 | `bffaeddf9` | CI 실패 |

첫 번째가 특히 중요하다. `cruise.py`는 12행에서 `toggle_item_sp, option_item_sp,
simple_button_item_sp`만 import하는데 85행이 `multiple_button_item_sp`를 호출한다. 모듈 내
정의도, 와일드카드 import도 없고, `_initialize_items()`는 `__init__` 40행에서 무조건 불린다.
**Settings → Cruise 패널을 여는 즉시 `NameError`가 난다.** DEC Map을 넣은 커밋이 헬퍼 이름만
바꾸고 import 줄을 고치지 않았다.

---

## 2. OSM 복원

Task 9(`a7ea951a4`)의 역연산이되, 한국 코드는 건드리지 않는다.

### 통째 복구 (9개, 충돌 불가)

`git checkout a7ea951a4^ -- <path>`로 복구한다.

```
openpilot/selfdrive/ui/sunnypilot/layouts/settings/osm.py      (232줄, 설정 패널)
openpilot/sunnypilot/mapd/live_map_data/osm_map_data.py        (OsmMapData 소스)
openpilot/sunnypilot/mapd/live_map_data/debug.py
openpilot/sunnypilot/mapd/live_map_data/standalone.py
openpilot/sunnypilot/mapd/mapd_installer.py                    (바이너리 다운로더)
openpilot/sunnypilot/mapd/update_version.py
openpilot/sunnypilot/mapd/version.py
openpilot/sunnypilot/mapd/tests/test_mapd_version.py
openpilot/sunnypilot/mapd/tests/mapd_hash
```

### 바이너리 복구 (2개)

```
openpilot/third_party/mapd_pfeiferj/mapd        (9.4MB aarch64 ELF, upstream에 100755로 tracked)
openpilot/third_party/mapd_pfeiferj/README.md
```

이 둘은 `e82853efb`에서 삭제했는데, upstream이 실행 권한과 함께 추적하고 있음을 확인했으므로
삭제 자체가 upstream과의 이탈이었다.

### 추가 병합 (10개, 삭제 없이 추가만)

| 파일 | 되돌릴 내용 |
|---|---|
| `common/params_keys.h` | OSM 파라미터 16개를 한국 블록 옆에 재추가 |
| `common/hardware/hw.py` | `Paths.mapd_root()` |
| `selfdrived/alerts_offroad.json` | `Offroad_OSMUpdateRequired` (`Offroad_KoreaMapMissing`은 유지) |
| `selfdrived/selfdrived.py` | `ignored_processes = {'mapd', }` |
| `system/manager/process_config.py` | `mapd_ready()` + `NativeProcess("mapd", ...)` |
| `sunnypilot/mapd/__init__.py` | `MAPD_BIN_DIR`, `MAPD_PATH` |
| `sunnypilot/mapd/live_map_data/__init__.py` | OSM 상수와 `get_debug()` |
| `ui/.../settings/settings.py` | OSM 패널 진입점 |
| `ui/.../settings/visuals.py` | 도로명 표시 안내 문구 |
| `ui/.../cruise_sub_layouts/speed_limit_policy.py` | 정책 설명 4줄 |

### SCC-Map 토글 복원

`14cc23164`를 되돌린다. 큰 UI의 `scc_m_toggle`, `cruise.yaml`의 `SmartCruiseControlMap` 블록,
`settings_ui.json` 재컴파일, `map_controller.py`의 "inert" docstring 원복.

복원 후 SCC-Map은 osm 모드에서 실제로 동작한다. 데이터 생산자(mapd 바이너리)가 돌아오기
때문이다.

---

## 3. 두 소스의 공존: `MapDataSource`

### 왜 단일 활성 소스인가

`OsmMapData`와 `KoreaMapData`는 둘 다 `BaseMapData`를 상속하고 같은 `liveMapDataSP` 메시지를
발행한다. 동시에 돌리면 서로 덮어쓴다. `BaseMapData` 추상화가 애초에 "한 번에 하나"를
전제로 설계돼 있으므로, 그 전제를 따른다.

### 파라미터

```c
{"MapDataSource", {PERSISTENT | BACKUP, INT, "1"}},
```

INT인 이유는 설정 위젯이 `int(params.get(key))`로 읽고 버튼 인덱스를 그대로 쓰기 때문이다
(`MultipleButtonAction`). STRING을 쓰면 위젯이 동작하지 않는다.

값은 `SpeedLimitPolicy`의 `Policy`와 같은 방식으로 `IntEnumBase`에 정의하며, 위치는
`openpilot/sunnypilot/mapd/__init__.py`다.

```python
class MapSource(IntEnumBase):
  osm = 0
  korea = 1
```

버튼 순서는 `[OpenStreetMap, 한국 공공데이터]`로 인덱스와 일치시킨다. 기본값이 `korea`(1)인
이유는 이 포크의 목적이 한국 주행이고, DB가 없을 때 오프로드 경고가 무엇을 복사해야 하는지
정확히 알려주기 때문이다.

### 런타임 분기

```
MapDataSource = "korea"                    MapDataSource = "osm"
──────────────────────────                 ──────────────────────────
mapd_manager                               mapd_manager
  KoreaMapData ──► liveMapDataSP             OsmMapData ──► liveMapDataSP
  CameraRefresher (주 1회 갱신)                 └ LastGPSPosition 기록
  ExternalNavSource (토글 시)
                                           NativeProcess("mapd")  ← 이때만 실행
  Offroad_KoreaMapMissing 감시                 MapSpeedLimit / RoadName
                                             MapTargetVelocities ──► SCC-Map
                                           Offroad_OSMUpdateRequired 감시
```

프로세스 게이트는 기존 함수를 확장한다.

```python
def mapd_ready(started, params, CP) -> bool:
  source = params.get("MapDataSource", return_default=True)
  return source == MapSource.osm and os.path.exists(Paths.mapd_root())
```

`mapd_manager`는 시작 시 한 번 소스를 읽는다. 온로드 중 전환은 지원하지 않는다 — 링크 DB가
프로세스 시작 시 한 번만 열리고, mapd 바이너리 프로세스도 manager가 관리하기 때문이다.
설정 UI는 오프로드에서만 변경 가능하게 한다.

### 기능별 가용성

| 기능 | korea | osm | 근거 |
|---|:---:|:---:|---|
| 제한속도 / 도로명 | O | O | 양쪽 다 `liveMapDataSP` 발행 |
| 단속카메라 | O | X | 한국 DB에만 있는 데이터 |
| SCC-Map (커브) | X | O | `MapTargetVelocities`는 mapd 바이너리만 생산 |
| SCC-Vision (커브) | O | O | `modelV2`만 읽음, 지도 무관 |
| DEC Map | O | O | `liveMapDataSP.speedLimit`만 읽음 |
| 외부 내비 입력 | O | X | 한국 앱 전용 입력구 |

korea 모드에서 커브 감속이 사라지지 않는다는 점이 중요하다 — SCC-Vision이 덮는다.

### korea 모드에서의 SCC-Map 토글

비활성화(회색)하고 설명 문구로 이유를 밝힌다. 숨기지 않는다. `cruise.py`가 이미 종방향
제어 불가 시 `set_enabled(False)`를 쓰는 관용을 따르며, 사용자가 "왜 없지"가 아니라
"왜 못 쓰지"를 알 수 있다.

---

## 4. 한국 설정 온디바이스 UI

두 파라미터 모두 sunnylink 원격에서만 접근 가능했다. 기존 위젯을 쓰며 새 위젯은 만들지 않는다.

### `KoreaExternalNavEnabled`

`speed_limit_settings.py`에 `toggle_item_sp`로 추가한다. 이 위치를 고른 이유는 sunnylink
`cruise.yaml`이 이미 같은 서브패널에 넣어두었기 때문이다 — 두 표면의 구조가 일치한다.

### `KoreaMapApiKey`

`simple_button_item_sp` 행을 두고 탭하면 `InputDialogSP`를 띄운다.

```python
InputDialogSP(title=..., current_text=..., param="KoreaMapApiKey", password_mode=True).show()
```

`param`을 넘기면 확인 시 알아서 저장하므로 별도 저장 로직이 필요 없다. `password_mode`로
어깨너머 노출을 막는다. 키 자체는 화면에 마스킹되어 표시된다.

### mici

mici에는 cruise 패널이 없으므로 `KoreaExternalNavEnabled`는 `toggles.py`에 둔다.
API 키는 `BigInputDialog`를 쓴다(`mici/layouts/settings/developer.py:57`의 GitHub 사용자명
입력과 동일한 패턴).

---

## 5. 작업 순서

네 단계 모두 `cruise.py` / `cruise.yaml` / `settings_ui.json`을 건드리므로 병렬 불가다.

```
1. 베이스 전환      리베이스 + 물려받은 lint 3건 수정
2. OSM 복원         파일 복구 + 추가 병합 + SCC-Map 토글
3. MapDataSource    파라미터 + 분기 + 프로세스 게이트 + UI
4. 한국 설정 UI     최종 구조 위에 2개 추가
```

1이 먼저인 이유는 베이스가 `cruise.py`를 이미 바꿔놓기 때문이고, 2가 3보다 먼저인 이유는
복원된 `osm.py` 패널이 설정 화면 구조를 결정하기 때문이다.

---

## 6. 테스트 전략

| 단계 | 검증 |
|---|---|
| 1 | 전체 ruff 통과(물려받은 3건 포함), 기존 216 테스트 유지, 서브모듈 체크아웃이 `lsjpwr/*`에서 성공 |
| 2 | 복원된 `test_mapd_version.py` 부활, OSM 파라미터 접근이 `UnknownKeyName`을 내지 않음 |
| 3 | `mapd_ready`가 osm에서만 True, 소스별로 올바른 `mapd_manager` 시작 경로와 오프로드 경고 |
| 4 | sunnylink 스키마 회귀 테스트에 두 항목 추가(`test_settings_changes.py`의 기존 패턴) |

베이스에서 물려받은 `cruise.py` F821은 정적 검사로 잡히지만 **실제 렌더는 디바이스에서만
확인 가능하다**(raylib가 GPU 없는 headless에서 segfault). 따라서 "설정 패널이 열린다"는
디바이스 검증 항목으로 남는다.

## 7. 위험과 완화

| 위험 | 완화 |
|---|---|
| bfayers force-push로 베이스 소실 | 로컬 태그 3개로 SHA 고정, opendbc/panda는 자체 포크 |
| 베이스가 조향 거동을 바꾸는 커밋 포함(협조 조향, 5Nm 토크 해제) | 디바이스 실주행 검증 항목으로 명시. PC에서 보증 불가 |
| 서브모듈 URL이 디바이스에서만 실패 | 포크 생성 완료, 고정 SHA의 ref 도달성 확인 완료 |
| 지도 소스 전환이 온로드 중 일어남 | 오프로드 전용 설정으로 제한 |
| DEC Map이 잘못된 제한속도로 모드 오판 | 실패 방향이 안전(모르면 blended). FCW·정차·고긴급 감속 오버라이드가 그 위에 존재 |
