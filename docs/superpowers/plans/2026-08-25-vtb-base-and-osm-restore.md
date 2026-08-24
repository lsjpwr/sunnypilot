# vtb-sla 베이스 전환과 OSM 복원 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `korea-dev`의 베이스를 `bfayers/vtb-sla-sunnylink-testing`으로 옮기고, Task 9가 삭제한 OSM 스택을 복원해 한국 공공 지도 데이터와 `MapDataSource` 파라미터로 선택 가능하게 공존시킨다.

**Architecture:** 32커밋을 고정 SHA `vtb-base-20260824`(`07b2ed06d`) 위로 replay한 뒤, 삭제됐던 OSM 파일을 `a7ea951a4^`에서 복구하고 추가 병합으로 되살린다. 두 지도 소스(`OsmMapData`, `KoreaMapData`)는 같은 `liveMapDataSP`를 발행하므로 동시 실행이 불가능하다. 따라서 `MapDataSource` INT 파라미터 하나가 `mapd_manager`의 시작 경로와 `mapd` NativeProcess 게이트를 함께 결정한다.

**Tech Stack:** Python 3.12, raylib UI(`openpilot/system/ui`), capnp(`liveMapDataSP`), SQLite(한국 DB), ruff, pytest, git submodule(opendbc/panda), CodeGraph(코드 탐색 인덱스)

---

## Global Constraints

이 절의 규칙은 **모든 태스크의 요구사항에 암묵적으로 포함된다.**

- **API 키 절대 금지.** `E:\dev\korea_map_data\data_go_kr.key`의 data.go.kr 키는 저장소 밖에 있다. 어떤 파일·커밋·테스트 픽스처·로그에도 들어가서는 안 된다. 이 포크는 **공개 저장소**다. 푸시 전 인코딩 형태와 `urllib.parse.unquote` 디코딩 형태 **양쪽**을 워킹트리·diff·전 커밋에 대해 재확인한다.
- **베이스 SHA는 태그로 고정.** 브랜치 이름(`bfayers/vtb-sla-sunnylink-testing`)을 절대 참조하지 않는다. bfayers는 force-push를 하며 2026-08-24 fetch에서 브랜치 3개 강제 갱신·4개 삭제가 관측됐다.

  | 저장소 | 태그 | SHA |
  |---|---|---|
  | sunnypilot | `vtb-base-20260824` | `07b2ed06d` |
  | opendbc (`opendbc_repo/`) | `vtb-opendbc-20260824` | `8bd5af67f3e718923c57eae58032336ed6b2e75d` |
  | panda (`panda/`) | `vtb-panda-20260824` | `f6887ad012f1924f75983b09b435cd8ac57eb03d` |

- **`.gitmodules`는 베이스 것을 그대로 쓴다.** 베이스는 상대경로(`url = ../opendbc.git`, `url = ../panda.git`)를 쓰고, 이는 부모 저장소 URL 기준으로 해석되므로 `lsjpwr/opendbc`·`lsjpwr/panda`를 가리킨다. 두 포크는 이미 생성했고 고정 SHA의 ref 도달성을 확인했다. **절대 URL로 되돌리지 말 것.**
- **ruff 전체 통과가 완료 조건이다.** `lint.select`에 `"F"`가 포함돼 F401/F821 모두 CI 실패다. E501 한도는 160자.
- **한국 코드를 되돌리지 않는다.** `korea_map_data.py`, `mapd_manager.py`의 한국 로직, `test_korea_map_data.py`, `openpilot/sunnypilot/mapd/korea/**`는 이 계획에서 **삭제·되돌림 대상이 아니다.** 복원은 OSM 쪽에만 적용된다.
- **커밋 메시지 트레일러**(이 저장소 관례):
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
  ```
- **테스트 실행 환경.** ruff/pytest는 Docker 컨테이너 `friendly_thompson`에서 돌린다(`/w` = `E:\dev\sunnypilot`). Git Bash에서는 경로 훼손을 막기 위해 `MSYS_NO_PATHCONV=1`을 붙인다.
  ```bash
  MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check .
  MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest <경로> -q
  ```
  `ModuleNotFoundError: No module named 'shapefile'`(pyshp)는 **실패가 아니다.** pyshp는 PC 전용 빌드 도구이고 컨테이너에 없다. 해당 3개 테스트는 원래 스킵된다.
- **디바이스 렌더는 PC에서 검증 불가.** raylib은 GPU 없는 headless에서 segfault한다. "설정 패널이 실제로 그려진다"는 항상 디바이스 검증 항목으로 남는다.

---

## CodeGraph 운용 규칙

이 저장소는 CodeGraph로 인덱싱돼 있다(`.codegraph/`, 약 357MB, git 추적 안 함). 파일 탐색·심볼 추적은 grep/find보다 CodeGraph를 **먼저** 쓴다.

**태스크마다 지켜야 할 3단계:**

1. **편집 전 — 탐색.** 건드릴 심볼의 현재 소스와 호출 경로를 한 번에 받는다.
   ```bash
   codegraph explore "<심볼 또는 질문>"
   ```
2. **편집 후 — 동기화.** 백그라운드 데몬이 약 1초 뒤 자동 반영하지만, 다음 태스크의 탐색 정확도를 보장하려면 명시적으로 돌린다.
   ```bash
   codegraph sync && codegraph status
   ```
3. **테스트 선택 — 영향 분석.** 바꾼 파일이 어떤 테스트에 걸리는지 인덱스가 알려준다.
   ```bash
   codegraph affected <바꾼 파일들>
   ```

**Task 1 전용 규칙 — 리베이스는 전체 재인덱싱을 요구한다.** 32커밋 replay는 워킹트리 수천 개 파일을 한꺼번에 바꾼다. 증분 `sync`로는 신뢰할 수 없으므로 `codegraph index`(처음부터 재구축)를 돌린다.

- `codegraph daemon`은 **대화형**이다("pick one and press enter to stop it"). 스크립트에서 쓰지 말 것.
- 인덱싱이 잠금에 막히면 `codegraph unlock`으로 stale lock을 제거한 뒤 재시도한다.
- `.codegraph/`를 **절대 커밋하지 않는다.** 자체 `.gitignore`가 내용물을 무시하지만 디렉터리 자체는 untracked로 보인다. `git add -A`를 쓰지 말고 항상 경로를 명시해 add한다.

---

## 파일 구조

### 복구되는 파일 (Task 2, `a7ea951a4^` / `e82853efb^`에서 그대로)

| 파일 | 책임 |
|---|---|
| `openpilot/sunnypilot/mapd/live_map_data/osm_map_data.py` | `BaseMapData` 구현체. `/dev/shm/params`의 `MapSpeedLimit`/`RoadName`을 읽어 `liveMapDataSP` 발행, `LastGPSPosition` 기록 |
| `openpilot/sunnypilot/mapd/live_map_data/debug.py` | OSM 데이터 디버그 출력 |
| `openpilot/sunnypilot/mapd/live_map_data/standalone.py` | 디바이스 밖 단독 실행 진입점 |
| `openpilot/sunnypilot/mapd/mapd_installer.py` | pfeiferj mapd 바이너리 다운로더 (VERSION = v1.12.0) |
| `openpilot/sunnypilot/mapd/version.py` | 설치된 바이너리 버전 |
| `openpilot/sunnypilot/mapd/update_version.py` | `MAPD_HASH_PATH` 정의 + 해시 갱신 도구 |
| `openpilot/sunnypilot/mapd/tests/test_mapd_version.py` | 바이너리 해시가 `mapd_hash`와 일치하는지 검사 |
| `openpilot/sunnypilot/mapd/tests/mapd_hash` | 기대 해시 (텍스트) |
| `openpilot/selfdrive/ui/sunnypilot/layouts/settings/osm.py` | OSM 설정 패널 (232줄): 지역 선택, 다운로드 진행률, 삭제 |
| `openpilot/third_party/mapd_pfeiferj/mapd` | aarch64 ELF 바이너리 9,371,832 B, 모드 100755 |
| `openpilot/third_party/mapd_pfeiferj/README.md` | 바이너리 출처 표기 |

### 추가 병합되는 파일 (Task 2·3, 삭제 없이 추가만)

| 파일 | 되살릴 것 | 태스크 |
|---|---|---|
| `openpilot/common/params_keys.h` | OSM 파라미터 18개 | 2 |
| `openpilot/common/hardware/hw.py` | `Paths.mapd_root()` | 2 |
| `openpilot/sunnypilot/mapd/__init__.py` | `MAPD_BIN_DIR`, `MAPD_PATH` | 2 |
| `openpilot/sunnypilot/mapd/live_map_data/__init__.py` | OSM 상수 8개 + `get_debug()` | 2 |
| `openpilot/selfdrive/selfdrived/selfdrived.py` | `ignored_processes` 에 mapd | 3 |
| `openpilot/system/manager/process_config.py` | `mapd_ready()` + `NativeProcess("mapd", ...)` | 3 |
| `openpilot/selfdrive/selfdrived/alerts_offroad.json` | `Offroad_OSMUpdateRequired` | 3 |
| `openpilot/selfdrive/ui/sunnypilot/layouts/settings/settings.py` | OSM 패널 진입점 | 3 |

### 되돌리지 **않는** 두 파일 (의도적 편차)

스펙 2절은 이 둘을 "추가 병합 10개"에 넣었으나, 실제로는 **되돌리면 안 된다.**

| 파일 | Task 9가 한 일 | 판단 |
|---|---|---|
| `.../settings/visuals.py` | 도로명 안내문을 "OpenStreetMap 데이터베이스를 OSM 패널에서 다운로드해야 함" → "지도 데이터베이스가 디바이스에 설치돼 있어야 함"으로 중립화 | **유지.** 두 소스가 공존하는 지금은 중립 문구가 정확하고, OSM 문구로 되돌리면 korea 모드에서 거짓이 된다 |
| `.../cruise_sub_layouts/speed_limit_policy.py` | 정책 설명 4줄의 "OpenStreetMaps" → "the map database" | **유지.** 같은 이유 |

구현자는 이 두 파일을 건드리지 않는다. 이는 누락이 아니라 결정이다.

### 새로 만들거나 크게 바뀌는 파일

| 파일 | 변경 | 태스크 |
|---|---|---|
| `openpilot/sunnypilot/mapd/__init__.py` | `MapSource(IntEnumBase)` 추가 | 5 |
| `openpilot/sunnypilot/mapd/mapd_manager.py` | `main_thread()`를 소스별 두 경로로 분기 | 5 |
| `openpilot/sunnypilot/mapd/tests/test_mapd_source.py` | 신규 — 분기와 게이트 테스트 | 5 |
| `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py` | lint 수정 → SCC-Map 토글 복원 → 소스별 활성/비활성 | 1·4·5 |
| `.../cruise_sub_layouts/speed_limit_settings.py` | 한국 설정 2개 추가 | 6 |
| `openpilot/selfdrive/ui/mici/layouts/settings/toggles.py` | mici용 한국 설정 2개 | 6 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` | SCC-Map 복원 → `MapDataSource` | 4·5 |
| `openpilot/sunnypilot/sunnylink/settings_ui.json` | 위 yaml에서 매번 재컴파일 | 4·5 |

### 왜 순차 실행인가

Task 4·5·6이 모두 `cruise.py` / `cruise.yaml` / `settings_ui.json`을 건드린다. `settings_ui.json`은 생성물이라 병렬 편집 시 충돌이 아니라 **조용한 덮어쓰기**가 난다. 병렬 불가.

---

## Task 1: 베이스 전환과 물려받은 lint 수정

**Files:**
- Rebase: `korea-dev` 32커밋 → `vtb-base-20260824`
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py:12`
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py:15`
- Modify: `openpilot/selfdrive/ui/mici/onroad/hud_renderer.py:284`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces: `vtb-base` 위에 올라간 `korea-dev`. 이후 모든 태스크가 이 트리를 전제한다. `multiple_button_item_sp`가 `cruise.py` 스코프에 존재하게 되며, Task 5가 이를 `MapDataSource` 위젯에 재사용한다.

- [ ] **Step 1: 안전망 브랜치를 만든다**

리베이스는 되돌리기 어렵다. 현재 상태를 이름 붙여 남긴다.

```bash
cd E:/dev/sunnypilot
git branch korea-dev-pre-vtb-20260825
git rev-parse korea-dev-pre-vtb-20260825
```

기대: `cc42609bb`로 시작하는 SHA 출력.

- [ ] **Step 2: 고정 태그와 포크 도달성을 재확인한다**

```bash
cd E:/dev/sunnypilot
git rev-parse vtb-base-20260824
git ls-tree 07b2ed06d opendbc_repo panda
git ls-remote https://github.com/lsjpwr/opendbc.git | grep 8bd5af67f
git ls-remote https://github.com/lsjpwr/panda.git | grep f6887ad012
```

기대:

```
07b2ed06d...
160000 commit 8bd5af67f3e718923c57eae58032336ed6b2e75d	opendbc_repo
160000 commit f6887ad012f1924f75983b09b435cd8ac57eb03d	panda
8bd5af67f3e718923c57eae58032336ed6b2e75d	refs/...
f6887ad012f1924f75983b09b435cd8ac57eb03d	refs/...
```

`ls-remote`가 아무것도 못 찾으면 **여기서 멈춘다.** 포크에 SHA가 없으면 디바이스가 서브모듈 체크아웃에 실패한다.

- [ ] **Step 3: 리베이스한다**

워크트리 실측에서 충돌 0으로 확인됐다. 그래도 충돌이 나면 멈추고 보고한다.

```bash
cd E:/dev/sunnypilot
git rebase --onto vtb-base-20260824 $(git merge-base korea-dev vtb-base-20260824) korea-dev
```

기대: `Successfully rebased and updated refs/heads/korea-dev.`

충돌 시: `git rebase --abort` 후 중단하고 보고한다. 임의 해결 금지.

- [ ] **Step 4: 리베이스 결과를 검증한다**

```bash
cd E:/dev/sunnypilot
git log --oneline -3
git rev-list --count vtb-base-20260824..korea-dev
grep -A2 'submodule "opendbc"' .gitmodules
git ls-tree HEAD opendbc_repo panda
```

기대:
- 커밋 수 `32`
- `.gitmodules`에 `url = ../opendbc.git` (상대경로 유지)
- 서브모듈 핀이 Step 2와 동일한 두 SHA

`.gitmodules`가 절대 URL이면 리베이스가 잘못된 것이다. 중단하고 보고한다.

- [ ] **Step 5: 서브모듈을 새 핀으로 체크아웃한다**

```bash
cd E:/dev/sunnypilot
git submodule update --init --recursive opendbc_repo panda
git submodule status opendbc_repo panda
```

기대: 두 줄 모두 앞에 공백(= 핀과 일치), SHA가 `8bd5af67f`/`f6887ad012`.

- [ ] **Step 6: CodeGraph를 전체 재구축한다**

리베이스가 워킹트리를 통째로 바꿨으므로 증분 sync로는 부족하다.

```bash
cd E:/dev/sunnypilot
codegraph index
codegraph status
```

기대: `status`가 최신 파일 수/심볼 수를 보고하고 에러 없음.

잠금 오류(lock file)가 나면:

```bash
codegraph unlock && codegraph index
```

- [ ] **Step 7: 베이스가 물려준 lint 3건을 확인한다 (수정 전)**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
```

기대 (정확히 이 3건):

```
openpilot/selfdrive/ui/mici/onroad/hud_renderer.py:284:161: E501 Line too long (178 > 160)
openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py:85:23: F821 Undefined name `multiple_button_item_sp`
openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py:15:60: F401 [*] `openpilot.selfdrive.controls.lib.drive_helpers.MAX_LATERAL_ACCEL_NO_ROLL` imported but unused
Found 3 errors.
```

3건보다 많으면 리베이스가 새 오류를 만든 것이다. 멈추고 보고한다.

- [ ] **Step 8: F821을 고친다 — `cruise.py`의 import 한 줄**

이것은 CI 실패가 아니라 **런타임 크래시**다. `_initialize_items()`가 `__init__` 40행에서 무조건 호출되므로 Settings → Cruise를 여는 즉시 `NameError`가 난다. 헬퍼는 `openpilot/system/ui/sunnypilot/widgets/list_view.py:369`에 실제로 존재하며, DEC Map을 넣은 커밋 `fc8b7fd74`가 import 줄만 빠뜨렸다.

`openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py:12`

변경 전:

```python
from openpilot.system.ui.sunnypilot.widgets.list_view import toggle_item_sp, option_item_sp, simple_button_item_sp
```

변경 후:

```python
from openpilot.system.ui.sunnypilot.widgets.list_view import (toggle_item_sp, option_item_sp, simple_button_item_sp,
                                                              multiple_button_item_sp)
```

(한 줄로 이으면 160자를 넘어 E501이 난다. 괄호로 감싸 나눈다.)

- [ ] **Step 9: F401을 고친다 — 미사용 import 삭제**

`MAX_LATERAL_ACCEL_NO_ROLL`은 `vision_controller.py` 안에서 단 한 번도 쓰이지 않는다(import 줄이 파일 내 유일한 출현). 상수 자체는 `openpilot/selfdrive/controls/lib/drive_helpers.py:14`에 살아 있고 그 파일 안에서만 쓰인다. 삭제가 맞다.

`openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py`에서 15행을 통째로 지운다:

```python
from openpilot.selfdrive.controls.lib.drive_helpers import MAX_LATERAL_ACCEL_NO_ROLL
```

- [ ] **Step 10: E501을 고친다 — 178자 줄 나누기**

`openpilot/selfdrive/ui/mici/onroad/hud_renderer.py:284`

변경 전 (178자):

```python
      experimental_mode = experimental_mode and sm['longitudinalPlanSP'].dec.state == custom.LongitudinalPlanSP.DynamicExperimentalControl.DynamicExperimentalControlState.blended
```

변경 후 — 이 저장소가 같은 식을 이미 이렇게 나눈다(`openpilot/selfdrive/ui/onroad/exp_button.py:35-39`). 그 관용을 그대로 따른다:

```python
      experimental_mode = (
          experimental_mode
          and sm['longitudinalPlanSP'].dec.state
          == custom.LongitudinalPlanSP.DynamicExperimentalControl.DynamicExperimentalControlState.blended
      )
```

- [ ] **Step 11: ruff가 깨끗한지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
```

기대: `All checks passed!`

- [ ] **Step 12: 기존 테스트가 유지되는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd openpilot/sunnypilot/sunnylink -q
```

기대: mapd/korea 테스트 88개 통과. pyshp(`No module named 'shapefile'`) 관련 3건은 정상 스킵이다.

- [ ] **Step 13: CodeGraph를 동기화한다**

```bash
cd E:/dev/sunnypilot && codegraph sync && codegraph status
```

- [ ] **Step 14: 커밋한다**

`.codegraph/`, `dev/`가 untracked로 남아 있으므로 `git add -A`를 쓰지 않는다.

```bash
cd E:/dev/sunnypilot
git add openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py \
        openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py \
        openpilot/selfdrive/ui/mici/onroad/hud_renderer.py
git commit -F- <<'MSG'
fix: repair the three lint failures inherited from the vtb-sla base

cruise.py:85 calls multiple_button_item_sp but line 12 never imported it, so
opening Settings -> Cruise raised NameError on the base branch. The helper is
real (system/ui/sunnypilot/widgets/list_view.py:369); the DEC Map commit renamed
the call and left the import line behind.

The other two are CI-only: an unused MAX_LATERAL_ACCEL_NO_ROLL import in
vision_controller.py, and a 178-char line in mici's hud_renderer.py wrapped the
way exp_button.py already wraps the same expression.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

## Task 2: OSM 파일 복구와 import 가능하게 만들기

Task 9(`a7ea951a4`)가 지운 파일을 되살리고, 그 파일들이 **import되는 데 필요한 최소 병합**을 함께 한다. 파일만 복구하면 `mapd_installer.py`가 `from openpilot.sunnypilot.mapd import MAPD_PATH, MAPD_BIN_DIR`에서 즉시 ImportError를 낸다. 그래서 이 둘은 한 태스크다.

**Files:**
- Restore: 위 "복구되는 파일" 표의 9개 + 바이너리 2개
- Modify: `openpilot/common/params_keys.h` (`// korea map` 블록 위)
- Modify: `openpilot/common/hardware/hw.py` (`korea_map_root` 위)
- Modify: `openpilot/sunnypilot/mapd/__init__.py`
- Modify: `openpilot/sunnypilot/mapd/live_map_data/__init__.py`
- Test: `openpilot/sunnypilot/mapd/tests/test_mapd_version.py` (복구되면서 되살아남)

**Interfaces:**
- Consumes: Task 1의 리베이스된 트리
- Produces:
  - `openpilot.sunnypilot.mapd.MAPD_BIN_DIR: str`, `openpilot.sunnypilot.mapd.MAPD_PATH: str`
  - `openpilot.common.hardware.hw.Paths.mapd_root() -> str` (staticmethod)
  - `openpilot.sunnypilot.mapd.live_map_data.get_debug(msg, log_to_cloud=True) -> None` 및 상수 `LOOK_AHEAD_HORIZON_TIME`, `ROAD_NAME_TIMEOUT`, `R`, `QUERY_RADIUS`, `QUERY_RADIUS_OFFLINE`
  - `openpilot.sunnypilot.mapd.live_map_data.osm_map_data.OsmMapData` (`BaseMapData` 서브클래스, `.tick()` 보유)
  - `openpilot.selfdrive.ui.sunnypilot.layouts.settings.osm.OSMLayout` (Task 3이 설정 패널에 등록)
  - `openpilot.sunnypilot.mapd.mapd_installer.VERSION: str`, `update_installed_version(version, params)`
  - 파라미터 18개가 `params_keys.h`에 존재 (Task 3의 오프로드 경고와 Task 5의 분기가 의존)

- [ ] **Step 1: CodeGraph로 현재 mapd 패키지 구조를 확인한다**

편집 전에 무엇이 남아 있고 무엇이 사라졌는지 인덱스에서 확인한다.

```bash
cd E:/dev/sunnypilot
codegraph explore "BaseMapData KoreaMapData mapd_manager live_map_data package"
```

기대: `BaseMapData`, `KoreaMapData`만 나오고 `OsmMapData`는 없다. 이것이 복구 전 상태다.

- [ ] **Step 2: 9개 파일을 복구한다**

```bash
cd E:/dev/sunnypilot
git checkout a7ea951a4^ -- \
  openpilot/selfdrive/ui/sunnypilot/layouts/settings/osm.py \
  openpilot/sunnypilot/mapd/live_map_data/osm_map_data.py \
  openpilot/sunnypilot/mapd/live_map_data/debug.py \
  openpilot/sunnypilot/mapd/live_map_data/standalone.py \
  openpilot/sunnypilot/mapd/mapd_installer.py \
  openpilot/sunnypilot/mapd/update_version.py \
  openpilot/sunnypilot/mapd/version.py \
  openpilot/sunnypilot/mapd/tests/test_mapd_version.py \
  openpilot/sunnypilot/mapd/tests/mapd_hash
git status --short
```

기대: 9개 파일이 `A `(added)로 나온다.

- [ ] **Step 3: 바이너리 2개를 복구하고 실행 권한을 확인한다**

이 둘은 `e82853efb`에서 지웠으므로 그 부모에서 가져온다. upstream이 `100755`로 추적하고 있으므로 삭제 자체가 upstream과의 이탈이었다.

```bash
cd E:/dev/sunnypilot
git checkout e82853efb^ -- \
  openpilot/third_party/mapd_pfeiferj/mapd \
  openpilot/third_party/mapd_pfeiferj/README.md
git ls-files -s openpilot/third_party/mapd_pfeiferj/
```

기대:

```
100755 <sha> 0	openpilot/third_party/mapd_pfeiferj/mapd
100644 <sha> 0	openpilot/third_party/mapd_pfeiferj/README.md
```

`100755`가 아니면 실행 권한이 빠진 것이다. `git update-index --chmod=+x openpilot/third_party/mapd_pfeiferj/mapd`로 고친다.

- [ ] **Step 4: `Paths.mapd_root()`를 되살린다**

`openpilot/common/hardware/hw.py` — `korea_map_root` 바로 위에 넣는다(Task 9가 지운 자리 그대로).

```python
  @staticmethod
  def mapd_root() -> str:
    if PC:
      return str(Path(Paths.comma_home()) / "media" / "0" / "osm")
    else:
      return "/data/media/0/osm"

  @staticmethod
  def korea_map_root() -> str:
```

- [ ] **Step 5: `mapd/__init__.py`에 경로 상수를 되살린다**

Task 9는 이 파일의 내용을 라이선스 헤더로 바꿔치웠다. 헤더는 남기고 상수를 되돌린다.

`openpilot/sunnypilot/mapd/__init__.py` 전체:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

from openpilot.common.basedir import BASEDIR

MAPD_BIN_DIR = os.path.join(BASEDIR, 'openpilot', 'third_party/mapd_pfeiferj')
MAPD_PATH = os.path.join(MAPD_BIN_DIR, 'mapd')
```

- [ ] **Step 6: `live_map_data/__init__.py`에 OSM 상수를 되살린다**

`openpilot/sunnypilot/mapd/live_map_data/__init__.py` 전체:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.swaglog import cloudlog

LOOK_AHEAD_HORIZON_TIME = 15.  # s. Time horizon for look ahead of turn speed sections to provide on liveMapDataSP msg.
_DEBUG = False
_CLOUDLOG_DEBUG = False
ROAD_NAME_TIMEOUT = 30  # secs
R = 6373000.0  # approximate radius of earth in mts
QUERY_RADIUS = 3000  # mts. Radius to use on OSM data queries.
QUERY_RADIUS_OFFLINE = 2250  # mts. Radius to use on offline OSM data queries.


def get_debug(msg, log_to_cloud=True):
  if _CLOUDLOG_DEBUG and log_to_cloud:
    cloudlog.debug(msg)
  if _DEBUG:
    print(msg)
```

- [ ] **Step 7: OSM 파라미터 18개를 되살린다**

`openpilot/common/params_keys.h` — `// korea map` 주석 블록 바로 위에 별도 `// mapd` 블록으로 넣는다. 한국 블록은 그대로 둔다.

변경 전:

```c
    {"PlanplusControl", {PERSISTENT | BACKUP, FLOAT, "1.0"}},

    // korea map
    {"KoreaExternalNavEnabled", {PERSISTENT | BACKUP, BOOL}},
```

변경 후:

```c
    {"PlanplusControl", {PERSISTENT | BACKUP, FLOAT, "1.0"}},

    // mapd
    {"MapAdvisorySpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT}},
    {"MapdVersion", {PERSISTENT, STRING}},
    {"MapSpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT, "0.0"}},
    {"NextMapSpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_OSMUpdateRequired", {CLEAR_ON_MANAGER_START, JSON}},
    {"OsmDbUpdatesCheck", {CLEAR_ON_MANAGER_START, BOOL}},  // mapd database update happens with device ON, reset on boot
    {"OSMDownloadBounds", {PERSISTENT, STRING}},
    {"OsmDownloadedDate", {PERSISTENT, STRING, "0.0"}},
    {"OSMDownloadLocations", {PERSISTENT, JSON}},
    {"OSMDownloadProgress", {CLEAR_ON_MANAGER_START, JSON}},
    {"OsmLocal", {PERSISTENT, BOOL}},
    {"OsmLocationName", {PERSISTENT, STRING}},
    {"OsmLocationTitle", {PERSISTENT, STRING}},
    {"OsmLocationUrl", {PERSISTENT, STRING}},
    {"OsmStateName", {PERSISTENT, STRING, "All"}},
    {"OsmStateTitle", {PERSISTENT, STRING}},
    {"OsmWayTest", {PERSISTENT, STRING}},
    {"RoadName", {CLEAR_ON_ONROAD_TRANSITION, STRING}},

    // korea map
    {"KoreaExternalNavEnabled", {PERSISTENT | BACKUP, BOOL}},
```

- [ ] **Step 8: 복구된 파일이 import되는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -c "
from openpilot.sunnypilot.mapd import MAPD_PATH, MAPD_BIN_DIR
from openpilot.sunnypilot.mapd.live_map_data import get_debug, LOOK_AHEAD_HORIZON_TIME
from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
from openpilot.common.hardware.hw import Paths
print('MAPD_PATH', MAPD_PATH)
print('mapd_root', Paths.mapd_root())
print('OsmMapData', OsmMapData.__mro__[1].__name__)
"
```

기대:

```
MAPD_PATH /w/openpilot/third_party/mapd_pfeiferj/mapd
mapd_root .../media/0/osm
OsmMapData BaseMapData
```

- [ ] **Step 9: 되살아난 해시 테스트를 돌린다**

`test_mapd_version.py`는 `get_file_hash(MAPD_PATH)`를 `mapd_hash` 파일과 대조한다. 둘 다 같은 커밋에서 복구했으므로 일치해야 한다. 불일치하면 바이너리가 손상된 것이다.

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd/tests/test_mapd_version.py -v
```

기대: PASS.

- [ ] **Step 10: OSM 파라미터가 실제로 등록됐는지 확인한다**

`params_keys.h`에 없는 키를 읽으면 `UnknownKeyName`이 난다. 이 검사는 Step 7이 실제로 반영됐음을 증명한다.

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -c "
from openpilot.common.params import Params
p = Params()
for k in ('OsmLocationName', 'OsmStateName', 'MapdVersion', 'RoadName', 'Offroad_OSMUpdateRequired'):
    p.get(k, return_default=True)
print('all 5 OSM params resolve')
"
```

기대: `all 5 OSM params resolve` (예외 없음).

- [ ] **Step 11: ruff와 전체 mapd 테스트**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd -q
```

기대: `All checks passed!` / 한국 테스트 88개 + `test_mapd_version` 통과.

- [ ] **Step 12: CodeGraph 동기화하고 복구를 확인한다**

```bash
cd E:/dev/sunnypilot
codegraph sync
codegraph explore "OsmMapData"
```

기대: `OsmMapData`의 소스와 `BaseMapData` 상속 관계가 인덱스에 나타난다.

- [ ] **Step 13: 커밋한다**

```bash
cd E:/dev/sunnypilot
git add openpilot/selfdrive/ui/sunnypilot/layouts/settings/osm.py \
        openpilot/sunnypilot/mapd/live_map_data/osm_map_data.py \
        openpilot/sunnypilot/mapd/live_map_data/debug.py \
        openpilot/sunnypilot/mapd/live_map_data/standalone.py \
        openpilot/sunnypilot/mapd/live_map_data/__init__.py \
        openpilot/sunnypilot/mapd/mapd_installer.py \
        openpilot/sunnypilot/mapd/update_version.py \
        openpilot/sunnypilot/mapd/version.py \
        openpilot/sunnypilot/mapd/__init__.py \
        openpilot/sunnypilot/mapd/tests/test_mapd_version.py \
        openpilot/sunnypilot/mapd/tests/mapd_hash \
        openpilot/third_party/mapd_pfeiferj/mapd \
        openpilot/third_party/mapd_pfeiferj/README.md \
        openpilot/common/hardware/hw.py \
        openpilot/common/params_keys.h
git commit -F- <<'MSG'
feat: restore the OSM map stack that the korean source replaced

The requirement was to change where map data comes from, not to remove
OpenStreetMap. Task 9 read it as the latter and deleted the whole stack; this
brings back every file it removed, plus the pfeiferj binary a later commit
dropped -- upstream tracks that binary at 100755, so deleting it was a
divergence in itself.

Files come back verbatim from the commit before the deletion. The four merged
files gain only what OSM needs to import: Paths.mapd_root, MAPD_PATH /
MAPD_BIN_DIR, the live_map_data constants, and the 18 OSM params. Nothing on the
korean side is touched.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

## Task 3: OSM 프로세스와 설정 패널 다시 연결

파일은 살아났지만 아직 아무도 부르지 않는다. manager가 mapd 바이너리를 띄우고, 설정 화면에 OSM 패널이 보이게 한다.

**Files:**
- Modify: `openpilot/selfdrive/selfdrived/selfdrived.py`
- Modify: `openpilot/system/manager/process_config.py`
- Modify: `openpilot/selfdrive/selfdrived/alerts_offroad.json`
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/settings.py`

**Interfaces:**
- Consumes: Task 2의 `Paths.mapd_root()`, `MAPD_PATH`, `OSMLayout`, `Offroad_OSMUpdateRequired` 파라미터
- Produces:
  - `openpilot.system.manager.process_config.mapd_ready(started: bool, params: Params, CP: car.CarParams) -> bool` — Task 5가 이 함수 본문에 `MapDataSource` 조건을 더한다
  - `OP.PanelType.OSM` 열거값 — 설정 화면 진입점
  - `procs`에 `NativeProcess("mapd", ...)` 등록

- [ ] **Step 1: CodeGraph로 프로세스 등록 지점을 확인한다**

```bash
cd E:/dev/sunnypilot
codegraph explore "process_config procs NativeProcess mapd_ready uploader_ready"
```

기대: `procs` 리스트와 `*_ready` 게이트 함수들의 현재 소스. `mapd_ready`는 아직 없다.

- [ ] **Step 2: `selfdrived.py`에서 mapd를 다시 무시 목록에 넣는다**

mapd는 `ignored_processes`에 있어야 한다. korea 모드에서는 이 프로세스가 아예 돌지 않으므로, 없으면 `processNotRunning`이 떠서 **인게이지가 막힌다.**

`openpilot/selfdrive/selfdrived/selfdrived.py`

변경 전:

```python
    self.ignored_processes: set[str] = set()
```

변경 후:

```python
    self.ignored_processes = {'mapd', }
```

- [ ] **Step 3: `process_config.py`에 게이트와 프로세스를 되살린다**

세 곳을 고친다.

(a) import — 원본은 `from openpilot.sunnypilot.mapd.mapd_manager import MAPD_PATH`였다. **그대로 되돌리지 않는다.** 그 경로는 manager 시작 시 `mapd_manager` 전체를 import하게 만들고, 이제 그 모듈은 한국 sqlite/카메라 스택까지 끌고 온다. 상수는 `openpilot.sunnypilot.mapd`에 있으므로 거기서 직접 가져온다.

기존 import 블록(파일 상단, `from openpilot.common.hardware.hw import Paths` 아래)에 추가:

```python
from openpilot.sunnypilot.mapd import MAPD_PATH
```

(b) 게이트 함수 — `is_stock_model` 아래, `uploader_ready` 위에 넣는다:

```python
def mapd_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
  return bool(os.path.exists(Paths.mapd_root()))
```

(c) 프로세스 등록 — `procs` 리스트의 `# mapd` 주석 아래, `mapd_manager` 줄 **위**에 넣는다:

```python
  # mapd
  NativeProcess("mapd", Paths.mapd_root(), ["bash", "-c", f"{MAPD_PATH} > /dev/null 2>&1"], mapd_ready),
  PythonProcess("mapd_manager", "openpilot.sunnypilot.mapd.mapd_manager", always_run),
```

- [ ] **Step 4: 오프로드 경고를 되살린다**

`openpilot/selfdrive/selfdrived/alerts_offroad.json` — `Offroad_KoreaMapMissing`은 **그대로 두고** 그 옆에 추가한다. 둘 다 필요하다(Task 5에서 소스별로 하나씩 쓴다).

`Offroad_KoreaMapMissing` 블록 바로 앞에 넣는다:

```json
  "Offroad_OSMUpdateRequired": {
    "text": "OpenStreetMap database is out of date. New maps must be downloaded if you wish to continue using OpenStreetMap data for Enhanced Speed Control and road name display.\n\n%1",
    "severity": 0
  },
```

- [ ] **Step 5: 설정 화면에 OSM 패널을 되살린다**

`openpilot/selfdrive/ui/sunnypilot/layouts/settings/settings.py` — 세 곳.

(a) import — `network` 다음, `software` 앞(알파벳 순서 유지):

```python
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.osm import OSMLayout
```

(b) `OP.PanelType` 열거 — `"DISPLAY"` 다음, `"NAVIGATION"` 앞:

```python
    "DISPLAY",
    "OSM",
    "NAVIGATION",
```

(c) 패널 등록 — `DISPLAY` 줄 다음:

```python
      OP.PanelType.OSM: PanelInfo(tr_noop("OSM"), OSMLayout(), icon="../../sunnypilot/selfdrive/assets/offroad/icon_map.png"),
```

- [ ] **Step 6: 게이트가 실제로 작동하는지 테스트를 쓴다**

새 파일 `openpilot/sunnypilot/mapd/tests/test_mapd_process.py`:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

from openpilot.system.manager.process_config import mapd_ready, procs


def test_mapd_is_registered_as_a_native_process():
  """Without this the binary never starts and MapTargetVelocities stays empty,
  which is exactly the silent failure that made SCC-Map inert."""
  assert any(p.name == "mapd" for p in procs)


def test_mapd_ready_follows_the_map_directory(tmp_path, monkeypatch):
  monkeypatch.setattr("openpilot.system.manager.process_config.Paths.mapd_root",
                      staticmethod(lambda: str(tmp_path / "osm")))
  assert mapd_ready(False, None, None) is False
  os.makedirs(tmp_path / "osm")
  assert mapd_ready(False, None, None) is True
```

- [ ] **Step 7: 테스트가 통과하는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd/tests/test_mapd_process.py -v
```

기대: 2 passed.

- [ ] **Step 8: 뮤테이션으로 테스트가 진짜인지 확인한다**

`process_config.py`의 `NativeProcess("mapd", ...)` 줄을 잠시 주석 처리하고 다시 돌린다.

기대: `test_mapd_is_registered_as_a_native_process`가 FAIL. 확인 후 주석을 되돌린다.

- [ ] **Step 9: alerts JSON이 유효한지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -c "
import json
d = json.load(open('openpilot/selfdrive/selfdrived/alerts_offroad.json'))
assert 'Offroad_OSMUpdateRequired' in d, 'OSM alert missing'
assert 'Offroad_KoreaMapMissing' in d, 'korea alert lost'
print('both offroad alerts present')
"
```

기대: `both offroad alerts present`.

- [ ] **Step 10: ruff와 전체 테스트**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd openpilot/selfdrive/selfdrived -q
```

기대: `All checks passed!`, 실패 0.

- [ ] **Step 11: CodeGraph 동기화 + 영향 확인**

```bash
cd E:/dev/sunnypilot
codegraph sync
codegraph affected openpilot/system/manager/process_config.py openpilot/selfdrive/selfdrived/selfdrived.py
```

`affected`가 Step 10에서 안 돌린 테스트를 지목하면 그것도 돌린다.

- [ ] **Step 12: 커밋한다**

```bash
cd E:/dev/sunnypilot
git add openpilot/selfdrive/selfdrived/selfdrived.py \
        openpilot/system/manager/process_config.py \
        openpilot/selfdrive/selfdrived/alerts_offroad.json \
        openpilot/selfdrive/ui/sunnypilot/layouts/settings/settings.py \
        openpilot/sunnypilot/mapd/tests/test_mapd_process.py
git commit -F- <<'MSG'
feat: run the mapd binary again and put the OSM panel back in settings

Restoring the files was not enough -- nothing started the process or reached the
panel. manager registers the NativeProcess again behind mapd_ready, selfdrived
puts mapd back in ignored_processes (without it, a source that does not run mapd
raises processNotRunning and blocks engagement), and the settings screen gets its
OSM entry back.

MAPD_PATH now comes straight from openpilot.sunnypilot.mapd rather than through
mapd_manager. The old import pulled the whole manager module -- and with it the
korean sqlite and camera stack -- into manager startup for one string constant.

Offroad_KoreaMapMissing stays; both alerts are needed once the source is
selectable.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

## Task 4: SCC-Map 토글 복원

`14cc23164`("stop shipping a toggle for a feature this branch removed")를 되돌린다. 그 커밋의 전제 — "생산자가 없다" — 는 Task 3에서 mapd 바이너리가 돌아오면서 더 이상 참이 아니다.

**Files:**
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py` (5곳)
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py` (docstring 제거)
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` (파일 끝에 블록 추가)
- Generate: `openpilot/sunnypilot/sunnylink/settings_ui.json` (재컴파일, 직접 편집 금지)

**Interfaces:**
- Consumes: Task 1이 고친 `cruise.py`의 `multiple_button_item_sp` import, Task 3의 mapd NativeProcess
- Produces: `CruiseLayout.scc_m_toggle` 속성 — Task 5가 여기에 소스별 활성/비활성 로직을 건다

- [ ] **Step 1: CodeGraph로 `SmartCruiseControlMap` 소비 지점을 확인한다**

```bash
cd E:/dev/sunnypilot
codegraph explore "SmartCruiseControlMap map_controller target_velocities"
```

기대: `SmartCruiseControlMap`의 소스와, `MapTargetVelocities`/`LastGPSPosition`를 읽는 지점.

- [ ] **Step 2: `map_controller.py`의 "inert" docstring을 제거한다**

`openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py`

`class SmartCruiseControlMap:` 바로 아래에 붙은 12줄 docstring("Curve speed from map geometry. Inert on this branch...")을 통째로 지운다. 클래스 본문은 다시 `v_target: float = 0`으로 시작해야 한다.

변경 후:

```python
class SmartCruiseControlMap:
  v_target: float = 0
  a_target: float = 0.
  v_ego: float = 0.
```

- [ ] **Step 3: `cruise.py`에 토글 정의를 되살린다**

`scc_v_toggle` 정의 다음, `custom_acc_toggle` 정의 앞:

```python
    self.scc_v_toggle = toggle_item_sp(
      title=tr("Smart Cruise Control - Vision"),
      description=tr("Use vision path predictions to estimate the appropriate speed to drive through turns ahead."),
      param="SmartCruiseControlVision")

    self.scc_m_toggle = toggle_item_sp(
      title=tr("Smart Cruise Control - Map"),
      description=tr("Use map data to estimate the appropriate speed to drive through turns ahead."),
      param="SmartCruiseControlMap")

    self.custom_acc_toggle = toggle_item_sp(
```

- [ ] **Step 4: `cruise.py`의 items 리스트에 되살린다**

```python
    items = [
      self.icbm_toggle,
      self.dec_option,
      self.dec_map_max_speed_option,
      self.scc_v_toggle,
      self.scc_m_toggle,
      self.custom_acc_toggle,
      self.custom_acc_short_increment,
      self.custom_acc_long_increment,
      self.sla_settings_button,
    ]
```

- [ ] **Step 5: `cruise.py`의 `_update_state`에 3줄을 되살린다**

`if has_long or has_icbm:` 블록 안:

```python
        self.scc_v_toggle.action_item.set_enabled(True)
        self.scc_m_toggle.action_item.set_enabled(True)
```

`else:` 블록 안 — `params.remove` 한 줄과 `set_enabled(False)` 한 줄:

```python
        ui_state.params.remove("SmartCruiseControlVision")
        ui_state.params.remove("SmartCruiseControlMap")
        self.custom_acc_toggle.action_item.set_enabled(False)
        self.dec_option.action_item.set_enabled(False)
        self.dec_map_max_speed_option.action_item.set_enabled(False)
        self.scc_v_toggle.action_item.set_enabled(False)
        self.scc_m_toggle.action_item.set_enabled(False)
```

- [ ] **Step 6: sunnylink yaml에 블록을 되살린다**

`openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` **맨 끝**에 추가한다(원래 마지막 항목이었다). 들여쓰기는 형제 항목과 정확히 같아야 한다.

```yaml
  - key: SmartCruiseControlMap
    widget: toggle
    title: Map
    description: Use map data to estimate the appropriate speed to drive through turns ahead.
    visibility:
    - type: any
      conditions:
      - type: capability
        field: has_longitudinal_control
        equals: true
      - type: capability
        field: has_icbm
        equals: true
    enablement:
    - type: any
      conditions:
      - type: capability
        field: has_longitudinal_control
        equals: true
      - type: capability
        field: has_icbm
        equals: true
```

- [ ] **Step 7: `settings_ui.json`을 재컴파일한다**

**손으로 편집하지 않는다.** 생성물이다.

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m openpilot.sunnypilot.sunnylink.tools.compile_settings_ui
```

- [ ] **Step 8: 컴파일 결과가 소스와 일치하는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m openpilot.sunnypilot.sunnylink.tools.compile_settings_ui --check
```

기대: 종료 코드 0. 0이 아니면 Step 7을 다시 돌린다.

- [ ] **Step 9: 두 표면 모두에 토글이 있는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -c "
import json
s = open('openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py').read()
assert 'scc_m_toggle' in s, 'raylib toggle missing'
j = json.load(open('openpilot/sunnypilot/sunnylink/settings_ui.json'))
assert 'SmartCruiseControlMap' in json.dumps(j), 'sunnylink toggle missing'
print('SCC-Map present on both surfaces')
"
```

기대: `SCC-Map present on both surfaces`.

- [ ] **Step 10: ruff와 sunnylink 스키마 테스트**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/sunnylink -q
```

기대: `All checks passed!`, 실패 0.

- [ ] **Step 11: CodeGraph 동기화**

```bash
cd E:/dev/sunnypilot && codegraph sync
```

- [ ] **Step 12: 커밋한다**

```bash
cd E:/dev/sunnypilot
git add openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py \
        openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py \
        openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml \
        openpilot/sunnypilot/sunnylink/settings_ui.json
git commit -F- <<'MSG'
feat: ship the Smart Cruise Control - Map toggle again

The toggle was pulled because nothing wrote MapTargetVelocities once the
pfeiferj mapd process was gone. That process is back, so the premise no longer
holds and the feature works again in osm mode.

Restores the toggle on both surfaces and drops the "inert" docstring that
described the removed state.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

## Task 5: `MapDataSource` — 두 소스를 하나만 고르게 한다

`OsmMapData`와 `KoreaMapData`는 둘 다 `BaseMapData`를 상속하고 **같은 `liveMapDataSP`를 발행한다.** 동시에 돌면 서로 덮어쓴다. 파라미터 하나가 어느 쪽이 도는지 결정한다.

**Files:**
- Modify: `openpilot/common/params_keys.h`
- Modify: `openpilot/sunnypilot/mapd/__init__.py` (`MapSource` 추가)
- Modify: `openpilot/sunnypilot/mapd/mapd_manager.py` (분기)
- Modify: `openpilot/system/manager/process_config.py` (`mapd_ready` 확장)
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py` (SCC-Map 소스별 비활성)
- Modify: `.../cruise_sub_layouts/speed_limit_settings.py` (소스 선택 위젯)
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml`
- Create: `openpilot/sunnypilot/mapd/tests/test_mapd_source.py`

**Interfaces:**
- Consumes: Task 2의 `OsmMapData`/`MAPD_PATH`, Task 3의 `mapd_ready`, Task 4의 `scc_m_toggle`
- Produces:
  - `openpilot.sunnypilot.mapd.MapSource(IntEnumBase)` — 멤버 `osm = 0`, `korea = 1`
  - `openpilot.sunnypilot.mapd.mapd_manager.osm_main() -> None`
  - `openpilot.sunnypilot.mapd.mapd_manager.korea_main() -> None`
  - `mapd_ready`가 `MapDataSource == MapSource.osm`일 때만 True
  - 파라미터 `MapDataSource` (INT, 기본 `"1"`)

### 결정 사항 두 가지 (스펙에서 확정되지 않아 여기서 정한다)

1. **기본값 = `korea`(1).** 이 포크의 목적이 한국 주행이고, DB가 없을 때 오프로드 경고가 "무엇을 복사해야 하는지"를 정확히 알려준다.
2. **소스 선택 위젯의 위치 = Speed Limit 서브패널 최상단.** 지도 데이터의 유일한 실사용처가 이 패널(제한속도/도로명)이고, Task 6의 한국 설정 2개도 같은 패널에 들어가므로 지도 관련 설정이 한 곳에 모인다. Cruise 패널 본체가 아닌 이유는 SCC-Map 토글이 이미 거기 있어 "지도 관련 설정"이 두 층으로 갈라지기 때문이다.

둘 다 사용자가 뒤집을 수 있는 판단이다.

- [ ] **Step 1: 실패하는 테스트를 먼저 쓴다**

새 파일 `openpilot/sunnypilot/mapd/tests/test_mapd_source.py`:

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

import pytest

from openpilot.sunnypilot.mapd import MapSource
from openpilot.system.manager.process_config import mapd_ready


class FakeParams:
  """Params stand-in. get() returns an int the way an INT param key does."""

  def __init__(self, source):
    self.source = source

  def get(self, key, return_default=False):
    assert key == "MapDataSource", f"unexpected param read: {key}"
    return int(self.source)


def test_the_enum_values_match_the_button_indices():
  """MultipleButtonAction stores the button index straight into the param, so the
  enum values ARE the button order. Buttons are [OpenStreetMap, korean public data]."""
  assert int(MapSource.osm) == 0
  assert int(MapSource.korea) == 1


@pytest.mark.parametrize("source,dir_exists,expected", [
  (MapSource.osm, True, True),
  (MapSource.osm, False, False),    # binary has nowhere to run
  (MapSource.korea, True, False),   # dir may linger from a previous osm run
  (MapSource.korea, False, False),
])
def test_the_mapd_binary_runs_only_for_osm(tmp_path, monkeypatch, source, dir_exists, expected):
  """Two publishers on liveMapDataSP overwrite each other. The binary is the osm
  producer, so korea mode must leave it stopped even when /data/media/0/osm exists."""
  root = tmp_path / "osm"
  if dir_exists:
    os.makedirs(root)
  monkeypatch.setattr("openpilot.system.manager.process_config.Paths.mapd_root",
                      staticmethod(lambda: str(root)))
  assert mapd_ready(False, FakeParams(source), None) is expected
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd/tests/test_mapd_source.py -q
```

기대: `ImportError: cannot import name 'MapSource'` — 전부 collection 에러.

- [ ] **Step 3: 파라미터를 등록한다**

`openpilot/common/params_keys.h` — Task 2에서 만든 `// mapd` 블록 첫 줄에 넣는다(알파벳 순서상 `MapAdvisorySpeedLimit` 앞).

```c
    // mapd
    {"MapDataSource", {PERSISTENT | BACKUP, INT, "1"}},
    {"MapAdvisorySpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT}},
```

**INT여야 한다.** 설정 위젯 `MultipleButtonActionSP`가 `int(self.params.get(self.param_key, return_default=True))`로 읽고 버튼 인덱스를 그대로 쓴다. STRING이면 위젯이 동작하지 않는다.

- [ ] **Step 4: `MapSource` 열거형을 정의한다**

`openpilot/sunnypilot/mapd/__init__.py` — Task 2에서 만든 내용에 추가한다. `IntEnumBase`는 `openpilot/sunnypilot/__init__.py:22`에 있고, `speed_limit/common.py`의 `Policy`가 같은 방식을 쓴다.

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

from openpilot.common.basedir import BASEDIR
from openpilot.sunnypilot import IntEnumBase

MAPD_BIN_DIR = os.path.join(BASEDIR, 'openpilot', 'third_party/mapd_pfeiferj')
MAPD_PATH = os.path.join(MAPD_BIN_DIR, 'mapd')


class MapSource(IntEnumBase):
  """Which database feeds liveMapDataSP. Only one can run: both sources publish the
  same message and would overwrite each other. Values are the settings button indices."""
  osm = 0
  korea = 1
```

- [ ] **Step 5: `mapd_ready`를 소스로 게이트한다**

`openpilot/system/manager/process_config.py`

import 줄에 `MapSource`를 더한다:

```python
from openpilot.sunnypilot.mapd import MAPD_PATH, MapSource
```

함수 본문:

```python
def mapd_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
  # The binary is the osm producer. In korea mode it must stay stopped even if the
  # osm directory is still on disk from an earlier run -- two publishers on
  # liveMapDataSP overwrite each other.
  source = params.get("MapDataSource", return_default=True)
  return source == MapSource.osm and os.path.exists(Paths.mapd_root())
```

- [ ] **Step 6: Task 3의 낡은 게이트 테스트를 제거한다**

`mapd_ready`가 이제 `params`를 실제로 읽으므로, Task 3이 쓴 `test_mapd_ready_follows_the_map_directory`는 `None`을 넘겨 `AttributeError`로 깨진다. 그 테스트가 검사하던 "디렉터리 존재" 조건은 Step 1의 `test_the_mapd_binary_runs_only_for_osm`이 네 조합으로 모두 덮으므로, 대체가 아니라 흡수다.

`openpilot/sunnypilot/mapd/tests/test_mapd_process.py`에서 `test_mapd_ready_follows_the_map_directory` 함수 전체와 이제 쓰이지 않는 `import os`를 삭제한다. `test_mapd_is_registered_as_a_native_process`와 `mapd_ready` import는 남긴다 — 남은 import는 `procs`만이므로 import 줄도 다음으로 줄인다:

```python
from openpilot.system.manager.process_config import procs
```

- [ ] **Step 7: 테스트가 모두 통과하는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd/tests/test_mapd_source.py openpilot/sunnypilot/mapd/tests/test_mapd_process.py -v
```

기대: `test_mapd_source.py` 5 passed + `test_mapd_process.py` 1 passed.

- [ ] **Step 8: 커밋한다 (분기 전 중간 지점)**

```bash
cd E:/dev/sunnypilot
git add openpilot/common/params_keys.h \
        openpilot/sunnypilot/mapd/__init__.py \
        openpilot/system/manager/process_config.py \
        openpilot/sunnypilot/mapd/tests/test_mapd_source.py \
        openpilot/sunnypilot/mapd/tests/test_mapd_process.py
git commit -F- <<'MSG'
feat: add MapDataSource and gate the mapd binary on it

Both map sources subclass BaseMapData and publish the same liveMapDataSP, so
running them together means they overwrite each other. One INT param picks the
winner; INT because MultipleButtonActionSP reads it with int() and uses the value
as a button index directly.

The binary produces the osm data, so korea mode leaves it stopped even when
/data/media/0/osm still exists from an earlier run.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

### Task 5 계속: `mapd_manager` 분기

- [ ] **Step 9: 분기 테스트를 먼저 쓴다**

`openpilot/sunnypilot/mapd/tests/test_mapd_source.py` 끝에 추가한다. 새 import는 없다 — `mapd_manager`는 함수 안에서 import한다(모듈 상단에 두면 이 파일의 다른 테스트까지 디바이스 스택을 끌어온다).

```python
def test_main_dispatches_on_the_param(monkeypatch):
  """One read at startup decides the whole process. Getting this backwards means the
  wrong database feeds every speed limit for the entire drive."""
  from openpilot.sunnypilot.mapd import mapd_manager

  called = []
  monkeypatch.setattr(mapd_manager, "osm_main", lambda: called.append("osm"))
  monkeypatch.setattr(mapd_manager, "korea_main", lambda: called.append("korea"))

  for source, expected in ((MapSource.osm, "osm"), (MapSource.korea, "korea")):
    called.clear()
    monkeypatch.setattr(mapd_manager, "Params", lambda s=source: FakeParams(s))
    mapd_manager.main()
    assert called == [expected], f"source {source!r} started {called}"
```

- [ ] **Step 10: 테스트가 실패하는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd/tests/test_mapd_source.py::test_main_dispatches_on_the_param -q
```

기대: `AttributeError: ... has no attribute 'osm_main'`.

- [ ] **Step 11: `mapd_manager.py`에 OSM 경로를 되살린다**

`a7ea951a4^`의 OSM 헬퍼를 되살리되, **모듈 레벨 `Params()` 인스턴스화는 되살리지 않는다.** 원본은 import 시점에 `params = Params()`와 `Params("/dev/shm/params")`를 만들었는데, 그러면 이 모듈을 import하는 것만으로 부작용이 생긴다. 필요한 두 함수에 인자로 넘긴다.

`openpilot/sunnypilot/mapd/mapd_manager.py` 상단 import에 추가:

```python
import glob
import json
import platform
import shutil
from datetime import datetime

from openpilot.common.hardware.hw import Paths
from openpilot.sunnypilot.mapd import MAPD_PATH, MapSource
from openpilot.sunnypilot.mapd.mapd_installer import VERSION, update_installed_version
from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
```

기존 한국 import(`camera_refresh`, `external_source`, `korea_map_data`)와 `_korea_log` 로깅 브리지는 **그대로 둔다.**

OSM 헬퍼 5개를 파일에 추가한다:

```python
def get_files_for_cleanup() -> list[str]:
  paths = [
    f"{Paths.mapd_root()}/db",
    f"{Paths.mapd_root()}/v*"
  ]
  files_to_remove = []
  for path in paths:
    if os.path.exists(path):
      files = glob.glob(path + '/**', recursive=True)
      files_to_remove.extend(files)
  # check for version and mapd files
  if not os.path.isfile(MAPD_PATH):
    files_to_remove.append(MAPD_PATH)
  return files_to_remove


def cleanup_old_osm_data(files_to_remove: list[str]) -> None:
  for file in files_to_remove:
    # Remove trailing slash if path is file
    if file.endswith('/') and os.path.isfile(file[:-1]):
      file = file[:-1]
    # Try to remove as file or symbolic link first
    if os.path.islink(file) or os.path.isfile(file):
      os.remove(file)
    elif os.path.isdir(file):  # If it's a directory
      shutil.rmtree(file, ignore_errors=False)


def request_refresh_osm_location_data(params, mem_params, nations: list[str], states: list[str] | None = None) -> None:
  params.put("OsmDownloadedDate", str(datetime.now().timestamp()), block=True)
  params.put_bool("OsmDbUpdatesCheck", False, block=True)

  osm_download_locations = {
    "nations": nations,
    "states": states or []
  }

  cloudlog.info("mapd: downloading maps for %s", json.dumps(osm_download_locations))
  mem_params.put("OSMDownloadLocations", osm_download_locations, block=True)


def filter_nations_and_states(nations: list[str], states: list[str] | None = None) -> tuple[list[str], list[str]]:
  """Filters and prepares nation and state data for OSM map download.

  If the nation is 'US' and a specific state is provided, the nation 'US' is removed from the list.
  If the nation is 'US' and the state is 'All', the 'All' is removed from the list.
  The idea behind these filters is that if a specific state in the US is provided,
  there's no need to download map data for the entire US. Conversely,
  if the state is unspecified (i.e., 'All'), we intend to download map data for the whole US,
  and 'All' isn't a valid state name, so it's removed.
  """
  if "US" in nations and states and not any(x.lower() == "all" for x in states):
    # If a specific state in the US is provided, remove 'US' from nations
    nations.remove("US")
  elif "US" in nations and states and any(x.lower() == "all" for x in states):
    # If 'All' is provided as a state (case invariant), remove those instances from states
    states = [x for x in states if x.lower() != "all"]
  elif "US" not in nations and states and any(x.lower() == "all" for x in states):
    states.remove("All")
  return nations, states or []


def update_osm_db(params, mem_params) -> None:
  if params.get_bool("OsmDbUpdatesCheck"):
    cleanup_old_osm_data(get_files_for_cleanup())
    country = params.get("OsmLocationName", return_default=True)
    state = params.get("OsmStateName", return_default=True)
    filtered_nations, filtered_states = filter_nations_and_states([country], [state])
    request_refresh_osm_location_data(params, mem_params, filtered_nations, filtered_states)

  if not mem_params.get("OSMDownloadBounds"):
    mem_params.put("OSMDownloadBounds", "", block=True)

  if not mem_params.get("LastGPSPosition"):
    mem_params.put("LastGPSPosition", "{}", block=True)


def osm_main() -> None:
  params = Params()
  mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else params

  update_installed_version(VERSION, params)
  config_realtime_process([0, 1, 2, 3], 5)

  rk = Ratekeeper(1, print_delay_threshold=None)
  live_map_sp = OsmMapData()

  try:
    os.makedirs(Paths.mapd_root(), exist_ok=True)
  except OSError:
    cloudlog.exception("mapd: failed to make %s", Paths.mapd_root())

  while True:
    show_alert = bool(get_files_for_cleanup() and params.get_bool("OsmLocal"))
    set_offroad_alert("Offroad_OSMUpdateRequired", show_alert, "This alert will be cleared when new maps are downloaded.")

    update_osm_db(params, mem_params)
    live_map_sp.tick()
    rk.keep_time()
```

- [ ] **Step 12: 기존 `main_thread`를 `korea_main`으로 이름만 바꾼다**

본문은 한 줄도 바꾸지 않는다.

```python
def korea_main() -> None:
```

- [ ] **Step 13: `main()`을 분기로 바꾼다**

```python
def main() -> None:
  # Read once at startup. Switching sources mid-drive is not supported: the link database
  # opens once when the process starts, and the mapd binary is managed by manager, not by
  # this loop. The settings UI restricts the change to offroad for the same reason.
  source = Params().get("MapDataSource", return_default=True)
  if source == MapSource.osm:
    osm_main()
  else:
    korea_main()


if __name__ == "__main__":
  main()
```

기존의 `def main() -> None: main_thread()`는 제거된다.

- [ ] **Step 14: 분기 테스트가 통과하는지 확인한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd/tests/test_mapd_source.py -v
```

기대: 6 passed.

- [ ] **Step 15: 뮤테이션으로 분기 테스트가 진짜인지 확인한다**

`main()`의 조건을 `if source == MapSource.korea:`로 뒤집고 다시 돌린다.

기대: `test_main_dispatches_on_the_param` FAIL. 확인 후 되돌린다.

- [ ] **Step 16: 소스 선택 위젯을 추가한다**

`openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py`

파일 상단 상수 블록에 추가:

```python
MAP_SOURCE_BUTTONS = [tr("OpenStreetMap"), tr("Korea Public Data")]

MAP_SOURCE_DESCRIPTIONS = [
  tr("OpenStreetMap: worldwide coverage. Enables curve slowdown from map geometry."),
  tr("Korea Public Data: speed limits and speed cameras from the Korean public datasets. "
     "No map curve data -- Smart Cruise Control - Vision covers curves."),
]
```

`_initialize_items()`에 위젯을 만든다(`self._speed_limit_mode` 정의 **앞**):

```python
    self._map_source = multiple_button_item_sp(
      title=lambda: tr("Map Data Source"),
      description=self._get_map_source_description,
      buttons=MAP_SOURCE_BUTTONS,
      param="MapDataSource",
      button_width=380,
    )
```

items 리스트 맨 앞에 넣는다:

```python
    items = [
      self._map_source,
      LineSeparatorSP(40),
      self._speed_limit_mode,
      LineSeparatorSP(40),
      self._source_button,
      LineSeparatorSP(40),
      self._speed_limit_offset_type,
      self._speed_limit_value_offset
    ]
```

설명 콜백을 다른 `_get_*_description`과 나란히 추가한다:

```python
  @staticmethod
  def _get_map_source_description():
    return get_highlighted_description(ui_state.params, "MapDataSource", MAP_SOURCE_DESCRIPTIONS)
```

`_update_state`에 오프로드 제한을 건다(메서드 본문 `super()._update_state()` 바로 뒤):

```python
    # Switching sources onroad would leave the running mapd_manager on the old database
    # and the mapd binary in the wrong state until the next restart.
    self._map_source.action_item.set_enabled(ui_state.is_offroad())
```

- [ ] **Step 17: SCC-Map을 korea 모드에서 비활성화한다**

`openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py`

import에 추가:

```python
from openpilot.sunnypilot.mapd import MapSource
```

파일 상단 상수에 추가:

```python
SCC_MAP_KOREA_DESCRIPTION = tr_noop("Requires the OpenStreetMap data source. "
                                    "The Korean public database has no curve geometry; "
                                    "Smart Cruise Control - Vision covers curves instead.")
SCC_MAP_DESCRIPTION = tr_noop("Use map data to estimate the appropriate speed to drive through turns ahead.")
```

Task 4에서 만든 토글 정의의 description을 상수로 바꾼다:

```python
    self.scc_m_toggle = toggle_item_sp(
      title=tr("Smart Cruise Control - Map"),
      description=tr(SCC_MAP_DESCRIPTION),
      param="SmartCruiseControlMap")
```

`_update_state`의 `if has_long or has_icbm:` 블록에서 `scc_m_toggle` 줄을 바꾼다 — 숨기지 않고 이유를 밝혀 비활성화한다:

```python
        self.scc_v_toggle.action_item.set_enabled(True)
        is_osm = ui_state.params.get("MapDataSource", return_default=True) == MapSource.osm
        self.scc_m_toggle.action_item.set_enabled(is_osm)
        new_scc_m_desc = tr(SCC_MAP_DESCRIPTION if is_osm else SCC_MAP_KOREA_DESCRIPTION)
        if self.scc_m_toggle.description != new_scc_m_desc:
          self.scc_m_toggle.set_description(new_scc_m_desc)
```

`else:` 블록은 Task 4의 상태 그대로 둔다(`set_enabled(False)`).

- [ ] **Step 18: sunnylink에도 소스 선택을 추가한다**

`openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` — `KoreaExternalNavEnabled` 항목 **앞**(같은 서브패널, `SpeedLimitPolicy` 다음)에 넣는다. 들여쓰기는 형제 항목과 동일하게 맞춘다.

```yaml
    - key: MapDataSource
      widget: multiple_button
      needs_onroad_cycle: true
      title: Map Data Source
      description: Which map database feeds speed limits and road names. OpenStreetMap also
        provides curve geometry for Smart Cruise Control - Map; the Korean public database
        provides speed cameras instead.
      options:
      - value: 0
        label: OpenStreetMap
      - value: 1
        label: Korea Public Data
      enablement:
      - $ref: '#/macros/offroad'
```

- [ ] **Step 19: `settings_ui.json`을 재컴파일하고 검증한다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m openpilot.sunnypilot.sunnylink.tools.compile_settings_ui
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m openpilot.sunnypilot.sunnylink.tools.compile_settings_ui --check
```

기대: 두 번째 명령 종료 코드 0.

- [ ] **Step 20: ruff + 전체 테스트**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/mapd openpilot/sunnypilot/sunnylink -q
```

기대: `All checks passed!`, 실패 0.

- [ ] **Step 21: CodeGraph 동기화 + 영향 확인**

```bash
cd E:/dev/sunnypilot
codegraph sync
codegraph impact mapd_ready
codegraph affected openpilot/sunnypilot/mapd/mapd_manager.py
```

`impact`가 예상 못 한 소비자를 보여주면 그 경로도 확인한다.

- [ ] **Step 22: 커밋한다**

```bash
cd E:/dev/sunnypilot
git add openpilot/sunnypilot/mapd/mapd_manager.py \
        openpilot/sunnypilot/mapd/tests/test_mapd_source.py \
        openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py \
        openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py \
        openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml \
        openpilot/sunnypilot/sunnylink/settings_ui.json
git commit -F- <<'MSG'
feat: let the user pick which map database feeds liveMapDataSP

mapd_manager reads MapDataSource once at startup and runs one of two loops. Both
of them publish the same message, so there is no configuration where they
coexist; switching is offroad-only because the link database opens once at
process start and the binary is managed by manager, not by this loop.

The OSM helpers come back without the module-level Params() the original had --
importing this module should not touch /dev/shm.

Smart Cruise Control - Map is greyed out in korea mode with a description saying
why, rather than hidden: the Korean database has no curve geometry, and a user
who cannot find the toggle learns less than one who reads that Vision covers it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

## Task 6: 한국 설정 온디바이스 UI

`KoreaExternalNavEnabled`는 sunnylink 원격에서만 바꿀 수 있었고, `KoreaMapApiKey`는 **어느 UI에도 없어서** 파라미터를 직접 쓰는 방법밖에 없었다. 둘 다 디바이스에서 바꾸게 한다. 새 위젯은 만들지 않는다.

**Files:**
- Modify: `.../settings/cruise_sub_layouts/speed_limit_settings.py`
- Modify: `openpilot/selfdrive/ui/mici/layouts/settings/toggles.py`
- Test: `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`
- **Not modified:** `cruise.yaml` / `settings_ui.json` — 아래 Step 8 참조

**Interfaces:**
- Consumes: Task 5의 `MapSource`와 `self._map_source` 위젯
- Produces: 없음 (최종 태스크, 소비자 없음)

- [ ] **Step 1: CodeGraph로 기존 위젯 사용 패턴을 확인한다**

```bash
cd E:/dev/sunnypilot
codegraph explore "InputDialogSP SpeedLimitSettingsLayout simple_button_item_sp"
```

기대: `InputDialogSP.__init__`이 `param`을 받아 CONFIRM 시 `self._params.put(self.param, text)`를 하는 것을 확인.

- [ ] **Step 2: `speed_limit_settings.py`에 import를 추가한다**

```python
from openpilot.system.ui.sunnypilot.widgets.input_dialog import InputDialogSP
from openpilot.system.ui.sunnypilot.widgets.list_view import (multiple_button_item_sp, option_item_sp,
                                                              simple_button_item_sp, toggle_item_sp,
                                                              LineSeparatorSP, ListItemSP, SimpleButtonActionSP)
```

기존 `list_view` import 줄을 위 형태로 교체한다(`toggle_item_sp`, `ListItemSP`, `SimpleButtonActionSP`가 추가됨).

- [ ] **Step 3: 외부 내비 토글을 추가한다**

`_initialize_items()`에서 `self._speed_limit_offset_type` 정의 **앞**에 넣는다. 문구는 sunnylink `cruise.yaml`의 기존 항목과 동일하게 맞춘다 — 두 표면이 같은 설정을 다르게 설명하면 안 된다.

```python
    self._external_nav = toggle_item_sp(
      title=lambda: tr("External Navigation Input"),
      description=tr("Accept speed limit and speed camera data from a companion navigation app on the "
                     "local network. Leave this off unless you are running one -- the built-in offline "
                     "map database works without it."),
      param="KoreaExternalNavEnabled")
```

- [ ] **Step 4: API 키 입력 행을 추가한다**

`InputDialogSP`에 `param`을 넘기면 확인 시 알아서 저장한다. 별도 저장 로직이 필요 없다. `password_mode=True`로 어깨너머 노출을 막는다.

`_external_nav` 정의 다음에 넣는다:

```python
    self._api_key = ListItemSP(
      title=lambda: tr("Speed Camera API Key"),
      description=tr("data.go.kr key used to refresh the speed camera database over Wi-Fi. "
                     "Without it the cameras shipped with the database are used as-is."),
      action_item=SimpleButtonActionSP(
        button_text=lambda: tr("Change") if ui_state.params.get("KoreaMapApiKey") else tr("Set"),
        callback=self._edit_api_key,
      ))
```

메서드를 클래스에 추가한다(`_get_map_source_description` 옆):

```python
  @staticmethod
  def _edit_api_key():
    InputDialogSP(
      title=tr("Speed Camera API Key"),
      sub_title=tr("data.go.kr service key"),
      current_text=ui_state.params.get("KoreaMapApiKey") or "",
      param="KoreaMapApiKey",
      password_mode=True,
    ).show()
```

- [ ] **Step 5: items 리스트에 두 항목을 넣는다**

sunnylink의 순서(`SpeedLimitPolicy` → `KoreaExternalNavEnabled` → `SpeedLimitOffsetType`)와 맞춘다.

```python
    items = [
      self._map_source,
      LineSeparatorSP(40),
      self._speed_limit_mode,
      LineSeparatorSP(40),
      self._source_button,
      LineSeparatorSP(40),
      self._external_nav,
      self._api_key,
      LineSeparatorSP(40),
      self._speed_limit_offset_type,
      self._speed_limit_value_offset
    ]
```

- [ ] **Step 6: 두 항목을 korea 모드에서만 활성화한다**

osm 모드에서는 한국 DB가 돌지 않으므로 두 설정 모두 무의미하다. Task 5에서 만든 `_update_state`의 소스 판정을 재사용한다.

```python
    is_offroad = ui_state.is_offroad()
    self._map_source.action_item.set_enabled(is_offroad)

    is_korea = ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea
    self._external_nav.action_item.set_enabled(is_korea)
    self._api_key.action_item.set_enabled(is_korea)
```

`MapSource` import를 파일 상단에 추가한다:

```python
from openpilot.sunnypilot.mapd import MapSource
```

- [ ] **Step 7: mici에 같은 두 설정을 추가한다**

mici에는 cruise 패널이 없다. `toggles.py`에 둔다. API 키는 `BigInputDialog`를 쓰며, 이는 `developer.py:57`의 GitHub 사용자명 입력과 같은 패턴이다.

`openpilot/selfdrive/ui/mici/layouts/settings/toggles.py`

import에 추가:

```python
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigInputDialog
```

(`BigConfirmationCircleButton`는 이미 dialog에서 import 중이므로 같은 줄에 합쳐도 된다.)

`TogglesLayoutMici.__init__`에서 `enable_openpilot` 정의 다음에 추가:

```python
    korea_nav_toggle = BigParamControl("korean external navigation input", "KoreaExternalNavEnabled")

    def api_key_callback(text):
      ui_state.params.put("KoreaMapApiKey", text)
      self._api_key_btn.set_value("Set" if text else "Not set")

    def edit_api_key():
      gui_app.push_widget(BigInputDialog("enter data.go.kr service key...",
                                         ui_state.params.get("KoreaMapApiKey") or "",
                                         minimum_length=0,
                                         confirm_callback=api_key_callback))

    has_key = bool(ui_state.params.get("KoreaMapApiKey"))
    self._api_key_btn = BigButton("speed camera API key", "Set" if has_key else "Not set")
    self._api_key_btn.set_click_callback(edit_api_key)
```

`_scroller.add_widgets([...])` 리스트에 `enable_openpilot` 다음으로 두 항목을 넣는다:

```python
      enable_openpilot,
      korea_nav_toggle,
      self._api_key_btn,
    ])
```

`_refresh_toggles` 튜플에도 토글을 추가한다:

```python
      ("OpenpilotEnabledToggle", enable_openpilot),
      ("KoreaExternalNavEnabled", korea_nav_toggle),
    )
```

- [ ] **Step 8: API 키를 sunnylink에 노출하지 않는다 (확인만)**

`settings_ui.schema.json:184`의 `widget` enum은 `["toggle", "option", "multiple_button", "button", "info"]`뿐이다. **자유 텍스트 입력 위젯이 없다.** 따라서 `KoreaMapApiKey`는 sunnylink 원격에 올릴 수 없고, 올려서도 안 된다 — API 키를 원격 표면에 두면 노출면만 넓어진다.

`KoreaMapApiKey`는 **디바이스 전용**으로 남는다(Step 4의 `password_mode` 입력). `KoreaExternalNavEnabled`는 이미 `cruise.yaml`에 있으므로 이 태스크에서 yaml 변경은 없다.

확인만 한다:

```bash
cd E:/dev/sunnypilot
grep -n "KoreaExternalNavEnabled" openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml
grep -c "KoreaMapApiKey" openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml
```

기대: 첫 명령은 한 줄을 찾고, 두 번째는 `0`.

- [ ] **Step 9: 컴파일 결과가 여전히 소스와 일치하는지 확인한다**

이 태스크는 yaml을 바꾸지 않으므로 재컴파일이 필요 없다. Task 5의 결과가 그대로 유효한지만 확인한다.

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m openpilot.sunnypilot.sunnylink.tools.compile_settings_ui --check
```

기대: 종료 코드 0. 0이 아니면 Task 5 Step 19의 재컴파일 결과가 커밋되지 않은 것이다.

- [ ] **Step 10: 스키마 회귀 테스트를 쓴다**

`openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py` 끝에 추가한다. 기존 헬퍼 `_find_item`과 `generate_schema`를 그대로 쓴다.

```python
class TestKoreaMapSettings(OpenpilotTestCase):
  """The source selector and the external-nav toggle must exist on the remote surface.
  A raylib-only setting is invisible to sunnylink users."""

  def test_the_remote_settings_are_in_the_schema(self):
    schema = generate_schema()
    for key in ("MapDataSource", "KoreaExternalNavEnabled"):
      self.assertIsNotNone(_find_item(schema, key), f"{key} missing from the sunnylink schema")

  def test_the_api_key_stays_off_the_remote_surface(self):
    """The schema has no free-text widget (settings_ui.schema.json enumerates
    toggle/option/multiple_button/button/info), and an API key does not belong on a
    remote surface anyway. It is device-only, entered through InputDialogSP."""
    self.assertIsNone(_find_item(generate_schema(), "KoreaMapApiKey"))

  def test_the_source_selector_offers_exactly_two_sources(self):
    """The option values are the button indices the raylib widget writes to the param.
    A third option here without a MapSource member would write a value nothing handles."""
    item = _find_item(generate_schema(), "MapDataSource")
    self.assertEqual([o["value"] for o in item["options"]], [0, 1])
```

- [ ] **Step 11: 테스트를 돌린다**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py -v -k Korea
```

기대: 3 passed.

- [ ] **Step 12: 뮤테이션으로 테스트가 진짜인지 확인한다**

`cruise.yaml`에서 `MapDataSource` 블록(Task 5 Step 18에서 추가)을 잠시 지우고 재컴파일한 뒤 다시 돌린다.

기대: `test_the_remote_settings_are_in_the_schema`와 `test_the_source_selector_offers_exactly_two_sources` 둘 다 FAIL. 확인 후 되돌리고 재컴파일한다.

- [ ] **Step 13: 파라미터 이름 오타를 잡는다**

UI가 잘못된 키를 쓰면 런타임에 `UnknownKeyName`이 난다. 정적으로 확인한다.

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -c "
from openpilot.common.params import Params
p = Params()
for k in ('MapDataSource', 'KoreaExternalNavEnabled', 'KoreaMapApiKey', 'SmartCruiseControlMap'):
    p.get(k, return_default=True)
print('all UI params resolve')
"
```

기대: `all UI params resolve`.

- [ ] **Step 14: ruff + 전체 테스트**

```bash
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/ruff check . --output-format concise
MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -m pytest openpilot/sunnypilot -q
```

기대: `All checks passed!`, 실패 0.

- [ ] **Step 15: CodeGraph 최종 동기화**

```bash
cd E:/dev/sunnypilot && codegraph sync && codegraph status
```

- [ ] **Step 16: API 키 유출 검사 — 푸시 전 필수**

이 포크는 공개 저장소다. 실제 키(`E:\dev\korea_map_data\data_go_kr.key`)가 인코딩 형태와 디코딩 형태 **양쪽 모두** 어디에도 없어야 한다.

```bash
cd E:/dev/sunnypilot
KEY_ENC=$(cat E:/dev/korea_map_data/data_go_kr.key)
KEY_DEC=$(MSYS_NO_PATHCONV=1 docker exec -w /w friendly_thompson /w/.venv/bin/python -c "import urllib.parse,sys; print(urllib.parse.unquote(sys.argv[1]))" "$KEY_ENC")
for K in "$KEY_ENC" "$KEY_DEC"; do
  echo "--- checking a key form (not printed)"
  git grep -I -n -F "$K" -- . && echo "!!! FOUND IN WORKING TREE" || echo "working tree clean"
  git log -p --all -S "$K" --oneline | head -5
done
```

기대: 두 형태 모두 "working tree clean"이고 `git log -S` 출력이 비어 있다. **하나라도 걸리면 푸시하지 않고 즉시 보고한다.**

- [ ] **Step 17: 커밋한다**

```bash
cd E:/dev/sunnypilot
git add openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py \
        openpilot/selfdrive/ui/mici/layouts/settings/toggles.py \
        openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py
git commit -F- <<'MSG'
feat: expose the korean map settings on the device itself

KoreaExternalNavEnabled reached only the sunnylink remote and KoreaMapApiKey
reached nothing at all -- it was settable only by writing the param directly. Both
now sit in the Speed Limit sub-panel next to the map source selector, and the
external-nav wording matches the remote so the two surfaces cannot drift.

The API key stays device-only. The sunnylink schema has no free-text widget
(settings_ui.schema.json enumerates toggle/option/multiple_button/button/info),
and a key does not belong on a remote surface regardless; InputDialogSP takes it
in password_mode and saves through the dialog's own param handling.

mici has no cruise panel, so both settings go in toggles.py, with BigInputDialog
for the key the way developer.py takes a GitHub username.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
MSG
```

---

## 디바이스 검증 항목 (PC에서 확인 불가)

raylib은 GPU 없는 headless에서 segfault한다. 아래는 **반드시 실차/실기기에서** 확인해야 한다.

1. **Settings → Cruise가 열린다.** 베이스가 물려준 F821은 정적으로 고쳤지만, 실제 렌더는 여기서만 확인된다.
2. **Settings → Speed Limit에 소스 선택·외부 내비·API 키 3개 행이 그려진다.** 오프로드에서만 소스 변경이 가능한지 함께 본다.
3. **Settings에 OSM 패널이 나타나고 지역 다운로드가 동작한다.**
4. **korea 모드에서 SCC-Map이 회색이고 설명이 이유를 말한다.** osm 모드로 바꾸고 재부팅하면 활성화된다.
5. **소스별로 올바른 오프로드 경고가 뜬다** — korea에서 DB 없으면 `Offroad_KoreaMapMissing`, osm에서 지도 오래되면 `Offroad_OSMUpdateRequired`.
6. **mici UI에서 두 한국 설정이 보인다** (mici 기기를 쓰는 경우).
7. **서브모듈 체크아웃이 디바이스에서 성공한다** — `lsjpwr/opendbc`, `lsjpwr/panda`에서 상대경로로 해석된다.
8. **조향 거동 검증(vtb-sla 베이스가 바꾼 것).** 협조 조향과 5Nm 토크 해제는 PC에서 보증할 수 없다. 안전한 장소에서 낮은 속도로 먼저 확인한다.
9. **Emergency Lane Departure Avoidance / Autopark 중 openpilot이 조향을 양보한다** (opendbc 서브모듈의 Tesla 안전 수정).

---

## 자체 검토 결과

**스펙 커버리지** — 스펙 각 절이 어느 태스크에 대응하는지:

| 스펙 절 | 태스크 |
|---|---|
| 1 베이스 전환 (고정 SHA, 서브모듈 URL, 리플레이) | Task 1 Step 1-5 |
| 1 물려받는 결함 3건 | Task 1 Step 7-11 |
| 2 통째 복구 9개 | Task 2 Step 2 |
| 2 바이너리 복구 2개 | Task 2 Step 3 |
| 2 추가 병합 10개 | Task 2 Step 4-7 (4개) + Task 3 Step 2-5 (4개) + **2개는 의도적 미실행**(위 "되돌리지 않는 두 파일") |
| 2 SCC-Map 토글 복원 | Task 4 |
| 3 `MapDataSource` 파라미터 / 런타임 분기 / 프로세스 게이트 | Task 5 |
| 3 korea 모드에서의 SCC-Map 토글 | Task 5 Step 17 |
| 4 `KoreaExternalNavEnabled` / `KoreaMapApiKey` / mici | Task 6 |
| 5 작업 순서 | Task 1→6 순차 |
| 6 테스트 전략 | 각 태스크의 검증 스텝 + 디바이스 검증 항목 |
| 7 위험과 완화 | Global Constraints + Task 1 Step 1(안전망 브랜치) |

**스펙 대비 편차 3건** (모두 근거와 함께 위에 명시):

1. `visuals.py`와 `speed_limit_policy.py`는 되돌리지 않는다 — 중립 문구가 두 소스 공존 상황에서 정확하다.
2. `process_config.py`의 `MAPD_PATH` import 경로를 `mapd_manager` → `openpilot.sunnypilot.mapd`로 바꾼다 — 원본 경로는 manager 시작 시 한국 sqlite 스택까지 끌어온다.
3. **스펙 4절의 사실 오류를 바로잡았다.** 스펙은 "두 파라미터 모두 sunnylink 원격에서만 접근 가능했다"고 썼으나, `KoreaMapApiKey`는 `cruise.yaml`에 없었다 — 어떤 UI에도 없었다. 게다가 `settings_ui.schema.json:184`의 `widget` enum에 자유 텍스트 입력이 없어 sunnylink에 올릴 수도 없다. 디바이스 전용으로 남기며, 이는 API 키의 노출면을 좁힌다는 점에서도 옳다.

**스펙이 정하지 않아 이 계획에서 정한 것 2건** (사용자가 뒤집을 수 있음):

1. `MapDataSource` 기본값 = `korea`(1)
2. 소스 선택 위젯 위치 = Speed Limit 서브패널 최상단

**타입 일관성** — 태스크 간 참조 이름 확인:

- `MapSource.osm` / `MapSource.korea`: Task 5 정의 → Task 5 Step 5·13·17, Task 6 Step 6에서 동일하게 사용
- `mapd_ready(started, params, CP)`: Task 3 정의 → Task 5 Step 5에서 본문만 확장, 시그니처 불변
- `MAPD_PATH` / `MAPD_BIN_DIR`: Task 2 정의 → Task 3 Step 3, Task 5 Step 11에서 사용
- `Paths.mapd_root()`: Task 2 정의 → Task 3 Step 3, Task 5 Step 11에서 사용
- `self.scc_m_toggle`: Task 4 정의 → Task 5 Step 17에서 확장
- `self._map_source`: Task 5 정의 → Task 6 Step 5·6에서 사용
- `osm_main()` / `korea_main()`: Task 5 Step 11·12 정의 → Step 13의 `main()`과 Step 9 테스트에서 사용
