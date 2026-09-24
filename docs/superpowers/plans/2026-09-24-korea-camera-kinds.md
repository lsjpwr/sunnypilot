# 과속카메라 종류별 감속·도달 여유 거리·써니링크 재배치 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 과속카메라 감속을 단속 종류 넷(과속 / 신호·과속 / 구간단속 / 보호구역)별로 켜고 끄고, 카메라 몇 m 앞에서 제한속도에 도달할지 고르게 한다. 둘 다 써니링크 Cruise 페이지에서 바꿀 수 있어야 한다.

**Architecture:** 빌드할 때 카메라마다 `kind`를 기록하고(`build_db.py`), 런타임은 켜진 종류만 후보로 삼는다(`db.next_camera(kinds=...)`). 고른 카메라는 두 곳으로 나간다: 표시는 `liveMapDataSP.speedLimitAhead`(HUD 앞쪽 표지), 감속은 `MapTargetVelocities` → SCC-Map. 한국 모드에서 SLA resolver는 ahead 값을 무시한다. 써니링크는 `korea` 페이지를 없애고 Cruise 페이지에 기능별 섹션 셋을 둔다.

**Tech Stack:** Python 3.12(디바이스), stdlib `sqlite3`(R-tree), openpilot `Params`, capnp `cereal`, raylib UI, sunnylink YAML → JSON 컴파일러. 신규 의존성 없음.

**스펙:** `docs/superpowers/specs/2026-09-24-korea-camera-kinds-design.md` (2026-09-24 실데이터 검증 결과 반영본)
**베이스:** `korea-dev`, 이 계획이 커밋된 지점.

**순서:** Task 1 → 2 → 3은 DB 쪽이다. Task 4는 1·2가 필요하고, 5는 4가 필요하다. 6은 4·5 다음에 둔다 — 그래야 카메라 감속이 어느 커밋에서도 빠지지 않는다(4·5 사이에는 SLA와 SCC-Map이 둘 다 줄이지만 플래너가 최솟값을 쓰므로 무해하다). 7·8은 4의 파라미터가 필요하다. 9는 마지막이다.

## Global Constraints

- 모든 코드는 `openpilot/` 하위다. 들여쓰기 2칸, 줄 길이 160(ruff).
- `openpilot/sunnypilot/mapd/korea/` 모듈(`db.py`, `build_db.py`, `camera_refresh.py`)은 **최상단에서 openpilot 스택을 import하지 않는다** — bare 인터프리터에서 테스트가 돈다. 로깅은 stdlib `logging`. import 방향은 빌더 → 런타임(`build_db`·`camera_refresh` → `db`)이고, 반대는 금지다.
- 테스트는 `unittest`다(`OpenpilotTestCase`의 fixture 주입 포함). pytest를 쓰지 않는다.
- 신규 런타임 의존성 0.
- 새 파라미터 다섯, 모두 `PERSISTENT | BACKUP`:

  | 키 | 타입 | 기본값 |
  |---|---|---|
  | `KoreaCameraSpeedEnabled` | BOOL | `"1"` |
  | `KoreaCameraSignalEnabled` | BOOL | `"1"` |
  | `KoreaCameraSectionEnabled` | BOOL | `"1"` |
  | `KoreaCameraZoneEnabled` | BOOL | `"1"` |
  | `KoreaCameraMargin` | INT (m) | `"50"` |

- 여유 거리 경계는 `CAMERA_MARGIN_RANGE = (0, 300)`(`korea_map_data.py`), 두 UI의 증감 단위는 10 m다.
- 종류 상수: `CAMERA_SPEED = 0`, `CAMERA_SIGNAL = 1`, `CAMERA_SECTION = 2`, `CAMERA_ZONE = 3`. 우선순위 보호구역 > 구간단속 > 신호·과속 > 과속, 모르는 코드는 과속. 코드 집합: `SIGNAL_CODES = {2}`(단속구분), `SECTION_CODES = {1, 2}`(단속구간위치구분), `ZONE_CODES = {1, 2}`(보호구역구분). `과속단속구간길이`는 분류에 쓰지 않는다(스펙 1절 검증 결과).
- `SCHEMA_VERSION`은 `"1"` 그대로다. 세 DB가 공유하므로 올리면 링크·방지턱 DB까지 거부된다.
- 활성 조건: 종류 토글 넷은 맵 소스가 Korea일 때만. 여유 거리는 Korea이고 롱컨트롤 또는 ICBM일 때. 오프로드 제한은 없다.
- 설명 문구(영문, 세 UI 공통)는 그대로 쓴다:

  | 항목 | 설명 |
  |---|---|
  | Speed Cameras | Slow down for fixed cameras that enforce speed only. |
  | Signal + Speed Cameras | Slow down for intersection cameras that enforce both red lights and speed. |
  | Section Enforcement | Slow down at the start and end cameras of average-speed sections. The speed between them is not held. |
  | Protected Zones | Slow down for cameras in school and senior protection zones. A camera inside a zone follows this toggle whatever else it enforces. |
  | Camera Arrival Margin | Reach the camera's limit about this far before the camera. Speed detectors often sit tens of metres ahead of the pole. |

  종류 토글 넷에는 "Turning a type off also hides its cameras from the speed limit ahead sign."를 뒤에 붙인다.
- `settings_ui.json`은 생성물이다. 손으로 고치지 않는다.
- **회귀 기준:** OSM 모드 동작은 100% 같다. 한국 모드의 방지턱·경로 커브 감속도 같다.
- **API 키는 이 계획에 필요 없다.** 코드 집합은 로컬 덤프(`E:/dev/korea_map_data/cameras_raw.json`)로 확정했다. `E:/dev/korea_map_data/data_go_kr.key`를 열거나 출력하지 않는다(Task 9의 기존 검사 스크립트는 개수만 출력한다). 키 값을 파일·로그·커밋에 쓰지 않는다.
- 작업 트리에 관계없는 untracked 파일(`dev/`, `.codegraph/`, 다른 계획서)이 있다. `git add`는 파일 이름으로만 한다.
- 커밋 메시지는 영어, `feat:`/`fix:`/`test:`/`docs:` 접두어를 쓰고 끝에 다음 두 줄을 붙인다:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
  ```

### 테스트 환경 (이 호스트 전용)

전체 레시피는 `.superpowers/sdd/test-env.md`에 있다. 요약:

- **(A) `korea/tests/`** — 순수 stdlib. 호스트 Python에서 돈다:
  ```bash
  cd E:/dev/sunnypilot
  python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_db -v
  ```
  기존 문제 하나: `test_db.py`의 `test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road`가 Windows에서 `PermissionError`로 실패한다(열린 sqlite 파일 위로 `os.replace`). 리눅스에서는 통과한다. 무시한다. `os.replace`를 쓰는 재적재 테스트 몇 개는 Windows에서 skip되므로 **korea 테스트는 컨테이너에서도 한 번 돌린다.**
- **(B) cereal 필요** — `mapd/tests/`, `smart_cruise_control/tests/`, `speed_limit/tests/`, `sunnylink/tests/`는 `sp-build` 컨테이너에서 돈다. bind-mount가 깨져 있어서 `docker cp`로 소스를 넣는다:
  ```bash
  MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
  docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_korea_map_data -v'
  ```
  `docker cp`는 병합이라 **호스트에서 지운 파일이 컨테이너에는 남는다.** Task 8에서 지우는 `korea.yaml`은 컨테이너에서도 따로 지운다.
- **`params_keys.h`를 고쳤으면 재빌드한다** (안 하면 `Params().get_bool("새키")`가 런타임에 실패한다):
  ```bash
  MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
  docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
  ```
- **(C) raylib UI 테스트**는 `xvfb-run -a`로 감싼다. 안 감싸면 `skipIf(not DISPLAY)`로 조용히 건너뛰어 "통과"가 아니라 "실행 안 됨"이 된다.
- **써니링크 컴파일러는 호스트에서 `PYTHONUTF8=1`로 돌린다.** 없으면 cp949로 YAML을 읽다가 `UnicodeDecodeError`가 난다.
- **ruff는 컨테이너 것(0.16)을 쓴다.** 호스트 ruff(0.14)는 `pyproject.toml`의 `RUF103`을 몰라 실패한다.
- **계획 작성 시점 baseline(2026-09-24):** 호스트 korea 테스트는 알려진 `PermissionError` 1건 외에 통과. 컨테이너의 korea·mapd·SCC-Map·resolver 테스트는 전부 통과. 써니링크 테스트는 `test_api_key_stays_out_of_the_cruise_panel` 1건이 **이미 실패한다**(korea 페이지에 `KoreaRouteApiKey`가 추가된 뒤 갱신되지 않음). Task 8이 이 테스트를 다시 쓴다.

---

## File Structure

| 파일 | 책임 | 태스크 |
|---|---|---|
| `openpilot/sunnypilot/mapd/korea/build_db.py` | 원본 행 → `kind`. 분류 규칙이 있는 유일한 곳 | 1 |
| `openpilot/sunnypilot/mapd/korea/db.py` | `CAMERA_*`·`CAMERA_KIND_PARAMS` 상수, `next_camera(kinds)`, 구 DB 판별 `has_camera_kind` | 1, 2, 4 |
| `openpilot/sunnypilot/mapd/korea/camera_refresh.py` | 구 DB 조기 재생성, 분류 필드가 빠진 API 응답 거부 | 3 |
| `openpilot/common/params_keys.h` | 새 키 다섯 | 4 |
| `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py` | 파라미터 읽기, 켜진 종류 전달, 카메라 점 발행 | 4 |
| `openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py` | 한국 분기가 카메라 토글로도 켜짐 | 5 |
| `openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_resolver.py` | 한국 모드에서 ahead 무시 | 6 |
| `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py` | tici: 토글 넷 + 여유 거리 | 7 |
| `openpilot/selfdrive/ui/mici/layouts/settings/toggles.py` | mici: 토글 넷 | 7 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` | 써니링크 섹션 셋 | 8 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml` | 삭제 | 8 |
| `openpilot/sunnypilot/sunnylink/settings_ui.json` | 생성물(재컴파일) | 8 |

테스트: `korea/tests/test_build_db.py`(1), `korea/tests/test_db.py`(1, 2), `korea/tests/test_deploy.py`(1), `korea/tests/test_camera_refresh.py`(1, 3), `mapd/tests/test_korea_map_data.py`(1, 2, 4), `smart_cruise_control/tests/test_map_controller.py`(5), `speed_limit/tests/test_speed_limit_resolver.py`(6), `ui/tests/test_speed_limit_settings_korea_gating.py`·`ui/mici/tests/test_toggles_api_key.py`(7), `sunnylink/tests/test_settings_changes.py`·`test_compile_settings_ui.py`(8).

---

### Task 1: 카메라 분류와 `kind` 컬럼

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/db.py:37-43` (`BUMP_*` 아래에 `CAMERA_*`)
- Modify: `openpilot/sunnypilot/mapd/korea/build_db.py:33`, `:43-52`, `:87-100`, `:152-194`, `:244-253`
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_build_db.py`
- 4튜플 → 5튜플 기계적 수정: `korea/tests/test_db.py:16,23-25,139,501`, `korea/tests/test_deploy.py:19,32`, `korea/tests/test_camera_refresh.py:21-22,55,65`, `mapd/tests/test_korea_map_data.py:27,236,248,264`

**Interfaces:**
- Consumes: 없음.
- Produces:
  - `db.CAMERA_SPEED = 0`, `db.CAMERA_SIGNAL = 1`, `db.CAMERA_SECTION = 2`, `db.CAMERA_ZONE = 3`
  - `build_db.classify_camera(enforcement, section_position, zone) -> int` — 인자는 원본 문자열 또는 `None`
  - `build_db.CAMERA_KIND_COLUMNS = ("단속구분", "단속구간위치구분", "보호구역구분")`, `build_db.CAMERA_API_KIND_FIELDS = ("regltSe", "regltSctnLcSe", "prtcareaType")`
  - `load_cameras(path)`, `load_cameras_api(items)`는 `(lat, lon, limit_kph, section_m, kind)` 5튜플을 낸다
  - `insert_cameras(con, cameras)`는 5튜플을 받는다. `cameras` 테이블에 `kind INTEGER NOT NULL`

- [ ] **Step 1: `test_build_db.py`에 실패하는 테스트를 쓴다**

import 두 줄(15-21행)을 교체한다:

```python
from openpilot.sunnypilot.mapd.korea.build_db import LINK_COLUMNS, SCHEMA_VERSION, SCHEMA_CAMERAS, SCHEMA_LINKS, \
                                                     FALLBACK_PROJECTED_EPSG, WGS84_EPSG, CAMERA_KIND_COLUMNS, \
                                                     build_bumps, build_cameras, write_db, in_korea, \
                                                     classify_camera, classify_kind, insert_cameras, \
                                                     insert_links, load_bumps, load_cameras, load_cameras_api, \
                                                     load_links, pack_geom, to_float, to_int
from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH, BUMP_TRAPEZOID, BUMP_VIRTUAL, CAMERA_SECTION, \
                                               CAMERA_SIGNAL, CAMERA_SPEED, CAMERA_ZONE
```

`CSV_HEADER`/`CSV_ROWS`(30-38행)를 교체하고 `KIND_ROWS`를 더한다:

```python
CSV_HEADER = "무인교통단속카메라관리번호,위도,경도,단속구분,제한속도,과속단속구간길이,단속구간위치구분,보호구역구분\n"
CSV_ROWS = (
  "A-1,37.4979,127.0276,1,60,0,,99\n" +       # kept, speed
  "A-2,37.5000,127.0300,99,80,4200,1,99\n" +  # kept, section start
  "A-3,37.5100,127.0400,2,0,0,,99\n" +        # dropped: no speed limit
  "A-4,0,0,1,60,0,,99\n" +                    # dropped: outside Korea
  "A-5,37.5200,127.0500,1,999,0,,99\n" +      # dropped: implausible limit
  "A-6,,,,,,,\n"                              # dropped: empty row
)

# (lat, lon, 단속구분, 제한속도, 단속구간위치구분, 보호구역구분, kind). One row per kind, picked
# so that swapping any two of the three code columns changes at least one kind.
KIND_ROWS = (
  ("37.50", "127.00", "1", "60", "", "99", CAMERA_SPEED),
  ("37.51", "127.00", "99", "80", "1", "99", CAMERA_SECTION),
  ("37.52", "127.00", "2", "50", "", "99", CAMERA_SIGNAL),
  ("37.53", "127.00", "2", "30", "", "2", CAMERA_ZONE),
)
```

`test_load_cameras_keeps_only_speed_cameras`(98-102행)의 기대값을 5튜플로 바꾼다:

```python
  def test_load_cameras_keeps_only_speed_cameras(self):
    rows = list(load_cameras(self.write_csv()))
    self.assertEqual(len(rows), 2, rows)
    self.assertEqual(rows[0], (37.4979, 127.0276, 60, 0, CAMERA_SPEED))
    self.assertEqual(rows[1], (37.5000, 127.0300, 80, 4200, CAMERA_SECTION))
```

`test_write_db_leaves_the_old_file_untouched_when_fill_raises`의 `good`(201-202행):

```python
    def good(con):
      return insert_cameras(con, [(37.4979, 127.0276, 60, 0, CAMERA_SPEED)])
```

파일 끝에 두 클래스를 더한다:

```python
class TestClassifyCamera(unittest.TestCase):
  def test_speed_only(self):
    self.assertEqual(classify_camera("1", "", "99"), CAMERA_SPEED)
    self.assertEqual(classify_camera("01", "", ""), CAMERA_SPEED)

  def test_zero_padded_and_combined_codes_read_as_signal(self):
    """단속구분 mixes '2' and '02', and names both enforcements as '01+02' or '1+2'."""
    for code in ("2", "02", "01+02", "1+2"):
      with self.subTest(code=code):
        self.assertEqual(classify_camera(code, "", "99"), CAMERA_SIGNAL)

  def test_section_start_and_end(self):
    for position in ("1", "01", "2", "02"):
      with self.subTest(position=position):
        self.assertEqual(classify_camera("99", position, "99"), CAMERA_SECTION)

  def test_a_zone_wins_over_everything_else(self):
    """A school-zone signal camera answers to the zone toggle, not the signal one."""
    self.assertEqual(classify_camera("2", "", "2"), CAMERA_ZONE)
    self.assertEqual(classify_camera("01+02", "", "02"), CAMERA_ZONE)
    self.assertEqual(classify_camera("99", "1", "1"), CAMERA_ZONE)

  def test_a_section_wins_over_signal(self):
    self.assertEqual(classify_camera("2", "1", "99"), CAMERA_SECTION)

  def test_unknown_codes_fall_back_to_speed(self):
    """A code this build has never seen keeps the slowdown every camera had before kinds."""
    for codes in (("99", "", "99"), ("4", "", ""), ("", "", ""), (None, None, None), ("과속", "x", "3")):
      with self.subTest(codes=codes):
        self.assertEqual(classify_camera(*codes), CAMERA_SPEED)


class TestCameraKinds(BuildDBTestCase):
  def write_kind_csv(self, header=CSV_HEADER):
    path = self.tmp_path / "kinds.csv"
    rows = "".join(f"K-{i},{lat},{lon},{enforcement},{limit},0,{position},{zone}\n"
                   for i, (lat, lon, enforcement, limit, position, zone, _) in enumerate(KIND_ROWS))
    path.write_text(header + rows, encoding="cp949")
    return str(path)

  def test_the_csv_path_reads_all_three_code_columns(self):
    self.assertEqual([row[4] for row in load_cameras(self.write_kind_csv())],
                     [kind for *_, kind in KIND_ROWS])

  def test_the_api_path_classifies_like_the_csv_path(self):
    """Two loaders, one classify_camera: the same row must land in the same kind either way."""
    items = [{"latitude": lat, "longitude": lon, "regltSe": enforcement, "lmttVe": limit,
              "ovrspdRegltSctnLt": "0", "regltSctnLcSe": position, "prtcareaType": zone}
             for lat, lon, enforcement, limit, position, zone, _ in KIND_ROWS]
    self.assertEqual(list(load_cameras_api(items)), list(load_cameras(self.write_kind_csv())))

  def test_a_renamed_code_column_raises(self):
    """lat/lon/limit are unchanged, only one code column is renamed. The old lat-only guard
    missed this and filed every camera under CAMERA_SPEED."""
    for column in CAMERA_KIND_COLUMNS:
      with self.subTest(column=column):
        with self.assertRaises(KeyError):
          list(load_cameras(self.write_kind_csv(CSV_HEADER.replace(column, "renamed"))))

  def test_build_cameras_stores_the_kind(self):
    out = str(self.tmp_path / "korea_cameras.sqlite")
    build_cameras(out, self.write_csv())
    con = sqlite3.connect(out)
    try:
      self.assertEqual([r[0] for r in con.execute("SELECT kind FROM cameras ORDER BY id")],
                       [CAMERA_SPEED, CAMERA_SECTION])
    finally:
      con.close()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_build_db -v`
Expected: `ImportError: cannot import name 'CAMERA_KIND_COLUMNS' from 'openpilot.sunnypilot.mapd.korea.build_db'`

- [ ] **Step 3: `db.py`에 종류 상수를 더한다**

`BUMP_VIRTUAL = 2 ...`(43행) 바로 아래에 넣는다:

```python

# 단속 종류, as build_db.classify_camera maps it. Here rather than in build_db for the same
# builder -> runtime reason as BUMP_* above. A camera has exactly one kind; classify_camera
# says which one wins when it is several things at once.
CAMERA_SPEED = 0    # 과속 only -- and any code this build does not recognise
CAMERA_SIGNAL = 1   # 신호·과속, the multi-function intersection cameras
CAMERA_SECTION = 2  # the start or end camera of a 구간단속 section
CAMERA_ZONE = 3     # inside a 노인/어린이 보호구역, whatever else it enforces
```

- [ ] **Step 4: `build_db.py`를 고친다**

33행 import:

```python
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, BUMP_VIRTUAL, CAMERA_SECTION,
                                                 CAMERA_SIGNAL, CAMERA_SPEED, CAMERA_ZONE)
```

43-52행(`CAMERA_COLUMNS`와 그 위 주석)을 교체한다:

```python
# Column names of the CSV distribution on data.go.kr. The open API for the same dataset
# romanises them (latitude/longitude/lmttVe/ovrspdRegltSctnLt); load_cameras_api reads those.
# Verified against the 2026-08 release; re-check if a load returns 0 rows.
CAMERA_COLUMNS = {
  "lat": "위도",
  "lon": "경도",
  "limit": "제한속도",
  "section": "과속단속구간길이",
}

# 단속구분 / 단속구간위치구분 / 보호구역구분 in the order classify_camera takes them: the CSV's
# Korean headers, then the API's romanised keys for the same three fields.
CAMERA_KIND_COLUMNS = ("단속구분", "단속구간위치구분", "보호구역구분")
CAMERA_API_KIND_FIELDS = ("regltSe", "regltSctnLcSe", "prtcareaType")

# What those codes mean. data.go.kr publishes no codebook for them, so each set is read off
# the 2026-08 API snapshot; the evidence and per-kind counts are in
# docs/superpowers/specs/2026-09-24-korea-camera-kinds-design.md.
#   단속구분: 1 과속, 2 신호 -- every 신호 row carries a speed limit, so these are the
#     multi-function 신호·과속 cameras. '01+02' and '1+2' name both. 3/4/99 are other kinds.
#   단속구간위치구분: 1 시점, 2 종점 of a 구간단속 section, blank for a point camera.
#   보호구역구분: 1 노인, 2 어린이 보호구역, 99 none, blank unrecorded.
SIGNAL_CODES = frozenset({2})
SECTION_CODES = frozenset({1, 2})
ZONE_CODES = frozenset({1, 2})
```

`SCHEMA_CAMERAS`(87-100행)를 교체한다:

```python
SCHEMA_CAMERAS = _META + """
CREATE TABLE cameras(
  id        INTEGER PRIMARY KEY,
  lat       REAL    NOT NULL,
  lon       REAL    NOT NULL,
  limit_kph INTEGER NOT NULL,
  -- NOT A USABLE DISTANCE. Carried through verbatim from 과속단속구간길이 for provenance
  -- only. In the 2026-08 dataset just 1120 of 43347 rows populate it at all, and the unit
  -- is inconsistent between submitting agencies: values run 1, 2, 3 ... 45, 200, 18637,
  -- and 99999 as a sentinel. Never feed this to anything that computes a distance.
  section_m INTEGER NOT NULL,
  -- db.CAMERA_*, from classify_camera. A file built before this column still opens:
  -- KoreaMapDB treats its cameras as kind unknown (db.has_camera_kind).
  kind      INTEGER NOT NULL
);
CREATE VIRTUAL TABLE cameras_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
"""
```

`load_cameras`(152-174행)를 교체한다:

```python
def load_cameras(path: str) -> Iterator[tuple[float, float, int, int, int]]:
  """Yield (lat, lon, limit_kph, section_m, kind) for every usable speed-enforcement camera.

  Rows without a speed limit are red-light or parking cameras: they carry no target
  speed, so there is nothing for the longitudinal controller to do with them. That filter
  keeps 33415 of the 43347 rows in the 2026-08 dataset. A speed limit is still the only
  test for "enforces speed" -- the three CAMERA_KIND_COLUMNS only pick which toggle a kept
  camera answers to (classify_camera).

  section_m is passed through unvalidated -- see the schema comment. Treat it as a label,
  never as metres.
  """
  rows = read_csv_rows(path)
  expected = [*CAMERA_COLUMNS.values(), *CAMERA_KIND_COLUMNS]
  if rows and not set(expected) <= set(rows[0]):
    # Every column, not just lat: a renamed code column reads as None on every row and would
    # file every camera under CAMERA_SPEED instead of failing loudly -- the trap load_bumps
    # guards against for its kind column.
    raise KeyError(f"expected columns {expected!r}, got {list(rows[0])}")

  for row in rows:
    limit_kph = to_int(row.get(CAMERA_COLUMNS["limit"]))
    lat = to_float(row.get(CAMERA_COLUMNS["lat"]))
    lon = to_float(row.get(CAMERA_COLUMNS["lon"]))
    if keep_camera(lat, lon, limit_kph):
      yield (lat, lon, limit_kph, to_int(row.get(CAMERA_COLUMNS["section"])),
             classify_camera(*(row.get(column) for column in CAMERA_KIND_COLUMNS)))
```

`keep_camera`(177-179행)는 그대로 두고, 그 바로 아래에 두 함수를 넣는다:

```python
def to_codes(value) -> set[int]:
  """'2', '02', '01+02', '1+2' -> {2}, {2}, {1, 2}, {1, 2}. Pieces that are not integers drop out."""
  codes = set()
  for piece in str(value or "").split("+"):
    try:
      codes.add(int(piece))
    except ValueError:
      pass
  return codes


def classify_camera(enforcement, section_position, zone) -> int:
  """단속구분, 단속구간위치구분, 보호구역구분 -> one db.CAMERA_* kind.

  The most specific kind wins: zone > section > signal > speed. A signal camera in a school
  zone answers to the zone toggle, not the signal one. Anything unrecognised is
  CAMERA_SPEED, so a code this build has never seen keeps the slowdown every camera had
  before kinds existed.
  """
  if to_codes(zone) & ZONE_CODES:
    return CAMERA_ZONE
  if to_codes(section_position) & SECTION_CODES:
    return CAMERA_SECTION
  if to_codes(enforcement) & SIGNAL_CODES:
    return CAMERA_SIGNAL
  return CAMERA_SPEED
```

`load_cameras_api`(182-194행)를 교체한다:

```python
def load_cameras_api(items) -> Iterator[tuple[float, float, int, int, int]]:
  """Same rows as load_cameras, from the data.go.kr JSON API instead of the CSV.

  The API romanises every field name -- latitude/longitude/lmttVe/ovrspdRegltSctnLt and
  CAMERA_API_KIND_FIELDS -- where the CSV uses the Korean headers. Verified against the
  2026-08 snapshot: both paths yield the identical 33415 rows out of 43347.
  """
  for item in items:
    limit_kph = to_int(item.get("lmttVe"))
    lat = to_float(item.get("latitude"))
    lon = to_float(item.get("longitude"))
    if keep_camera(lat, lon, limit_kph):
      yield (lat, lon, limit_kph, to_int(item.get("ovrspdRegltSctnLt")),
             classify_camera(*(item.get(field) for field in CAMERA_API_KIND_FIELDS)))
```

`insert_cameras`(244-253행)를 교체한다:

```python
def insert_cameras(con: sqlite3.Connection, cameras) -> int:
  count = 0
  for lat, lon, limit_kph, section_m, kind in cameras:
    cur = con.execute("INSERT INTO cameras(lat, lon, limit_kph, section_m, kind) VALUES (?, ?, ?, ?, ?)",
                      (lat, lon, limit_kph, section_m, kind))
    con.execute("INSERT INTO cameras_idx VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, lat - POINT_BOX_DEG, lat + POINT_BOX_DEG,
                 lon - POINT_BOX_DEG, lon + POINT_BOX_DEG))
    count += 1
  return count
```

- [ ] **Step 5: 다른 테스트의 4튜플 호출을 5튜플로 바꾼다**

`korea/tests/test_db.py`:
- 16행: `from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH, BUMP_TRAPEZOID, CAMERA_SECTION, CAMERA_SPEED, KoreaMapDB, verify`
- 23-25행:
  ```python
  CAM_AHEAD = (37.5000, 127.0257, 50, 0, CAMERA_SPEED)
  CAM_BEHIND = (37.5000, 127.0143, 30, 0, CAMERA_SPEED)
  CAM_SECTION = (37.5000, 127.0280, 80, 4200, CAMERA_SECTION)
  ```
- 139행: `insert_cameras(con, [(37.4990, 127.0260, 40, 0, CAMERA_SPEED)])`
- 501행: `CAM_SIDE_ROAD = (37.5003, 127.0257, 30, 0, CAMERA_SPEED)`

`korea/tests/test_deploy.py`:
- 19행: `from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH, CAMERA_SPEED`
- 32행: `lambda con: insert_cameras(con, [(37.5 + i * 1e-4, 127.0, 60, 0, CAMERA_SPEED) for i in range(n)]))`

`korea/tests/test_camera_refresh.py`:
- 22행 아래에 `from openpilot.sunnypilot.mapd.korea.db import CAMERA_SPEED`
- 55행: `lambda con: insert_cameras(con, [(37.5 + i * 1e-4, 127.0, 60, 0, CAMERA_SPEED) for i in range(n)]))`
- 65행: `self.assertEqual(list(load_cameras_api(items)), [(37.5, 127.0, 60, 0, CAMERA_SPEED)])`

`mapd/tests/test_korea_map_data.py`:
- 27행: `from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH, BUMP_TRAPEZOID, BUMP_VIRTUAL, CAMERA_SPEED, Bump, Camera, Link`
- 236·248·264행의 `[(37.5000, 127.0257, 50, 0)]`를 모두 `[(37.5000, 127.0257, 50, 0, CAMERA_SPEED)]`로 바꾼다(replace_all).

- [ ] **Step 6: 호스트에서 korea 테스트를 돌린다**

Run:
```bash
cd E:/dev/sunnypilot
python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh
```
Expected: 새 테스트 전부 통과. 실패는 알려진 `PermissionError` 1건(`test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road`)뿐이다.

- [ ] **Step 7: 컨테이너에서 리눅스 전용 경로와 `test_korea_map_data`를 확인한다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh openpilot.sunnypilot.mapd.tests.test_korea_map_data'
```
Expected: `OK (skipped=3)` — skip 3개는 컨테이너에 pyshp/pyproj가 없는 `TestLoadLinks`다.

- [ ] **Step 8: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/db.py openpilot/sunnypilot/mapd/korea/build_db.py \
  openpilot/sunnypilot/mapd/korea/tests/test_build_db.py openpilot/sunnypilot/mapd/korea/tests/test_db.py \
  openpilot/sunnypilot/mapd/korea/tests/test_deploy.py openpilot/sunnypilot/mapd/korea/tests/test_camera_refresh.py \
  openpilot/sunnypilot/mapd/tests/test_korea_map_data.py
git commit -F - <<'EOF'
feat: classify each speed camera into one of four enforcement kinds

The camera database now records a kind per camera: speed, signal+speed,
section start/end, or protected zone. The codes come from the 2026-08 API
snapshot, since data.go.kr publishes no codebook for them. A CSV that lost
one of the three code columns now raises instead of filing every camera
under speed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 2: `next_camera(kinds=...)`와 구 DB 호환

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/db.py` — import(10-15행), `Camera`(83-94행), `has_camera_kind`(새 함수, `_check_schema_version` 아래), `KoreaMapDB.__init__`(185행), `_reload_one`(247-256행), `next_camera`(324-350행)
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_db.py`
- Modify: `openpilot/sunnypilot/mapd/tests/test_korea_map_data.py:120,130` (`Camera(...)` 생성자)

**Interfaces:**
- Consumes: Task 1의 `CAMERA_*`, 5컬럼 `cameras` 테이블.
- Produces:
  - `Camera(lat: float, lon: float, limit_kph: int, distance_m: float, section_m: int, kind: int | None)` — 전부 필수 인자. `kind=None`은 kind 컬럼이 없는 구 DB
  - `KoreaMapDB.next_camera(lat, lon, heading_deg, route=None, kinds: Collection[int] | None = None) -> Camera | None`
  - `db.has_camera_kind(con: sqlite3.Connection) -> bool`

- [ ] **Step 1: `test_db.py`에 실패하는 테스트를 쓴다**

16행 import를 교체한다:

```python
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, CAMERA_SECTION, CAMERA_SPEED,
                                                 CAMERA_ZONE, KoreaMapDB, has_camera_kind, verify)
```

`CAM_SECTION` 줄 아래에 픽스처를 더한다:

```python
# ~200 m east on ROAD_60: nearer than CAM_AHEAD (~500 m) on the same road
CAM_ZONE_NEAR = (37.5000, 127.0223, 30, 0, CAMERA_ZONE)
```

`_REPLACE_WHILE_OPEN_SKIP_REASON = ...` 줄 아래에 도우미를 더한다:

```python


def drop_kind(path):
  """Turn a fresh camera database into one built before cameras carried a kind."""
  con = sqlite3.connect(path)
  try:
    con.execute("ALTER TABLE cameras DROP COLUMN kind")
    con.commit()
  finally:
    con.close()
```

파일 끝에 두 클래스를 더한다:

```python
class TestCameraKindFilter(KoreaMapDBTestCase):
  """next_camera(kinds=...): the kind toggles decide which cameras exist at all."""

  def setUp(self):
    super().setUp()
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_ZONE_NEAR, CAM_AHEAD]))
    write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60]))
    self.db = self.open_db(cams, links)

  def test_no_kinds_means_every_kind(self):
    camera = self.db.next_camera(37.5000, 127.0200, 90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.kind, CAMERA_ZONE)

  def test_a_nearer_camera_that_is_off_does_not_hide_a_farther_one_that_is_on(self):
    camera = self.db.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED})
    self.assertIsNotNone(camera)
    self.assertEqual((camera.kind, camera.limit_kph), (CAMERA_SPEED, 50))

  def test_every_kind_off_means_no_camera(self):
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., kinds=frozenset()))

  def test_the_camera_carries_its_coordinate(self):
    camera = self.db.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED})
    self.assertEqual((camera.lat, camera.lon), CAM_AHEAD[:2])


class TestCameraDatabaseWithoutKind(KoreaMapDBTestCase):
  """A camera file built before the kind column. It must work exactly as it did before."""

  @staticmethod
  def _has_kind(path):
    con = sqlite3.connect(path)
    try:
      return has_camera_kind(con)
    finally:
      con.close()

  def test_has_camera_kind_tells_the_two_apart(self):
    cams, _ = self._make_pair()
    self.assertTrue(self._has_kind(cams))
    drop_kind(cams)
    self.assertFalse(self._has_kind(cams))

  def test_every_camera_passes_any_filter(self):
    cams, links = self._make_pair()
    drop_kind(cams)
    camera = self.open_db(cams, links).next_camera(37.5000, 127.0200, 90., kinds=frozenset())
    self.assertIsNotNone(camera, "an old database lost its cameras to a filter it cannot answer")
    self.assertIsNone(camera.kind)
    self.assertEqual(camera.limit_kph, 50)

  @unittest.skipIf(sys.platform == "win32", _REPLACE_WHILE_OPEN_SKIP_REASON)
  def test_a_reload_notices_the_new_column(self):
    """camera_refresh rebuilds an old file within the hour. The running process must start
    filtering then, not at the next reboot."""
    cams, links = self._make_pair()
    drop_kind(cams)
    database = self.open_db(cams, links)
    self.assertIsNotNone(database.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED}))

    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_ZONE_NEAR]))
    stamp = os.path.getmtime(cams) + 1  # an unambiguous change on any filesystem
    os.utime(cams, (stamp, stamp))

    self.assertTrue(database.reload_if_changed())
    self.assertIsNone(database.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED}))
```

`mapd/tests/test_korea_map_data.py`의 `Camera(...)` 두 곳(120·130행)을 새 필드로 바꾼다:

```python
    data = make_data(camera=Camera(lat=37.5029, lon=127.0276, limit_kph=50, distance_m=320., section_m=0,
                                   kind=CAMERA_SPEED))
```

```python
                     camera=Camera(lat=37.5029, lon=127.0276, limit_kph=50, distance_m=320., section_m=0,
                                   kind=CAMERA_SPEED),
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_db -v`
Expected: `ImportError: cannot import name 'has_camera_kind' from 'openpilot.sunnypilot.mapd.korea.db'`

- [ ] **Step 3: `db.py`를 고친다**

import 블록의 `import struct` 아래에 한 줄:

```python
from collections.abc import Collection
```

`Camera`(83-94행)를 교체한다:

```python
@dataclass(frozen=True)
class Camera:
  """A speed camera ahead of us.

  Carries the coordinate for the same reason Bump does: korea_map_data places the
  SmartCruiseControlMap target on the line to it.

  section_m is raw provenance carried through from 과속단속구간길이, not a usable
  distance: see the cameras table's schema comment in build_db.py (only ~2.6% of rows
  populate it, units are inconsistent between submitting agencies, and 99999 is a
  sentinel, not a real value). Never feed it to a distance calculation.

  kind is db.CAMERA_*, or None for a camera database built before the kind column --
  those cameras pass every kind filter, which is how every camera behaved before.
  """
  lat: float
  lon: float
  limit_kph: int
  distance_m: float
  section_m: int  # raw 과속단속구간길이 -- see docstring. kind, not this, says 구간단속
  kind: int | None
```

`_check_schema_version`(133-136행) 아래에 새 함수:

```python
def has_camera_kind(con: sqlite3.Connection) -> bool:
  """Was this camera database built with the kind column?

  SCHEMA_VERSION stays "1" for it on purpose: the version is shared with the link and bump
  files, and bumping it would reject a 220 MB link database that did not change. So an
  older camera file still opens, and its readers ask this instead. camera_refresh asks it
  too, to rebuild such a file without waiting out the week.
  """
  return any(row[1] == "kind" for row in con.execute("PRAGMA table_info(cameras)"))
```

`KoreaMapDB.__init__`의 `self.cam = self._open(cameras_path)`(185행) 바로 아래:

```python
    self._cam_has_kind = has_camera_kind(self.cam)
```

`_reload_one`의 `setattr(self, mtime_attr, mtime)`와 `if con_attr == "lnk":` 사이(251-252행 사이)에 넣는다:

```python
    if con_attr == "cam":
      # A refresh replaces a file from before the kind column with one that has it.
      self._cam_has_kind = has_camera_kind(con)
```

`next_camera`(324-350행)를 교체한다:

```python
  def next_camera(self, lat: float, lon: float, heading_deg: float | None,
                  route: list[tuple[float, float]] | None = None,
                  kinds: Collection[int] | None = None) -> Camera | None:
    """Nearest speed camera ahead of us, or None. Needs a heading to know what 'ahead' means.

    With a route, 'ahead' stops being a bearing cone and becomes the road we will actually
    drive: the cone alone accepts a camera on the far side of a fork, which is the single
    most visible wrong slowdown this database produces.

    kinds, when given, is the set of db.CAMERA_* the driver has on. Other kinds are skipped
    before the distance comparison, so a nearer camera of a kind that is off never hides a
    farther one that is on. A camera of unknown kind (a database built before the column)
    always passes. None means every kind.
    """
    if heading_deg is None:
      return None

    kind_column = "c.kind" if self._cam_has_kind else "NULL"
    rows = self.cam.execute(
      f"SELECT c.lat, c.lon, c.limit_kph, c.section_m, {kind_column} FROM cameras_idx i JOIN cameras c ON c.id = i.id " +
      _RTREE_OVERLAP,
      (lat - CAMERA_SEARCH_DEG, lat + CAMERA_SEARCH_DEG, lon - CAMERA_SEARCH_DEG, lon + CAMERA_SEARCH_DEG),
    ).fetchall()

    best: Camera | None = None

    for clat, clon, limit_kph, section_m, kind in rows:
      if kinds is not None and kind is not None and kind not in kinds:
        continue
      distance = haversine(lat, lon, clat, clon)
      if distance > CAMERA_MAX_DISTANCE_M or (best is not None and distance >= best.distance_m):
        continue
      if not _on_path(lat, lon, clat, clon, heading_deg, route, CAMERA_AHEAD_TOLERANCE):
        continue
      best = Camera(lat=clat, lon=clon, limit_kph=limit_kph, distance_m=distance, section_m=section_m, kind=kind)

    return best
```

- [ ] **Step 4: 호스트와 컨테이너에서 돌린다**

Run:
```bash
cd E:/dev/sunnypilot
python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.korea.tests.test_build_db
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.tests.test_korea_map_data'
```
Expected: 호스트는 알려진 `PermissionError` 1건 외 통과(`test_a_reload_notices_the_new_column`은 Windows에서 skip). 컨테이너는 `OK`.

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/db.py openpilot/sunnypilot/mapd/korea/tests/test_db.py \
  openpilot/sunnypilot/mapd/tests/test_korea_map_data.py
git commit -F - <<'EOF'
feat: let next_camera skip the camera kinds that are turned off

A camera of a kind that is off is dropped before the distance comparison,
so it never hides a farther camera that is on. A database built before
the kind column still opens, and its cameras pass every filter. The
camera now carries its coordinate for the SCC-Map target.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 3: 구 DB 조기 재생성, 분류 필드가 빠진 API 응답 거부

**Files:**
- Modify: `openpilot/sunnypilot/mapd/korea/camera_refresh.py` — import(29행), `current_has_kind`(새 함수, `current_row_count` 아래), `refresh`(102-110행 사이), `_due`(150-159행)
- Test: `openpilot/sunnypilot/mapd/korea/tests/test_camera_refresh.py`

**Interfaces:**
- Consumes: `db.has_camera_kind(con)`(Task 2), `build_db.CAMERA_API_KIND_FIELDS`(Task 1).
- Produces: `camera_refresh.current_has_kind(path: str) -> bool`. `CameraRefresher._due()`는 kind 컬럼이 없는 DB에서 `True`를 돌려준다. `refresh()`는 분류 필드 셋 중 하나라도 받은 항목 어디에도 없으면 0을 돌려주고 기존 DB를 둔다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

21-22행 import를 교체한다:

```python
from openpilot.sunnypilot.mapd.korea.build_db import (CAMERA_API_KIND_FIELDS, SCHEMA_CAMERAS, insert_cameras,
                                                      load_cameras_api, write_db)
```

`api_item`(25-27행)을 교체한다. 기본값은 과속 카메라다 — 새 거부 규칙이 기존 테스트의 항목을 막지 않게 한다:

```python
def api_item(lat, lon, limit, section=0, enforcement="1", position="", zone="99"):
  return {"latitude": str(lat), "longitude": str(lon), "lmttVe": str(limit),
          "ovrspdRegltSctnLt": str(section), "regltSe": enforcement,
          "regltSctnLcSe": position, "prtcareaType": zone}
```

`TestRefresh` 끝(`test_the_api_key_never_reaches_a_log_line` 아래)에 더한다:

```python
  def test_refresh_keeps_the_old_database_when_a_kind_field_disappears(self):
    """A renamed field reads as None on every item: every camera would land in CAMERA_SPEED
    and the kind toggles would silently stop working. Keep what we have instead."""
    for field in CAMERA_API_KIND_FIELDS:
      with self.subTest(field=field):
        seed(self.path, 10)
        items = [api_item(37.5 + i * 1e-4, 127.0, 60) for i in range(20)]
        for item in items:
          del item[field]
        self.assertEqual(camera_refresh.refresh(self.path, "KEY", opener=fake_opener([items])), 0)
        self.assertEqual(count_rows(self.path), 10)
```

`class OneShotStop` 위에 새 클래스를 더한다:

```python
class TestDue(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
    self.path = str(self.tmp_path / "korea_cameras.sqlite")

  def test_a_fresh_database_is_not_due(self):
    seed(self.path, 5)
    self.assertFalse(camera_refresh.CameraRefresher(self.path)._due())

  def test_a_fresh_database_without_kinds_is_due(self):
    """Built before the kind column: every kind toggle would do nothing until the weekly
    refresh came round. Rebuild on the next unmetered tick instead."""
    seed(self.path, 5)
    con = sqlite3.connect(self.path)
    try:
      con.execute("ALTER TABLE cameras DROP COLUMN kind")
      con.commit()
    finally:
      con.close()
    self.assertTrue(camera_refresh.CameraRefresher(self.path)._due())
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd E:/dev/sunnypilot && python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh -v`
Expected: `test_a_fresh_database_without_kinds_is_due`가 `AssertionError: False is not true`로, `test_refresh_keeps_the_old_database_when_a_kind_field_disappears`의 subtest 셋이 `AssertionError: 20 != 0`으로 실패한다.

- [ ] **Step 3: `camera_refresh.py`를 고친다**

29행 import를 교체한다:

```python
from openpilot.sunnypilot.mapd.korea.build_db import (CAMERA_API_KIND_FIELDS, SCHEMA_CAMERAS, insert_cameras,
                                                      load_cameras_api, write_db)
from openpilot.sunnypilot.mapd.korea.db import has_camera_kind
```

`current_row_count`(81-92행) 아래에 새 함수:

```python
def current_has_kind(path: str) -> bool:
  """Whether the live database carries the kind column. False when there is no usable database."""
  try:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
  except sqlite3.Error:
    return False
  try:
    return has_camera_kind(con)
  except sqlite3.Error:
    return False
  finally:
    con.close()
```

`refresh()`에서 fetch의 `except` 블록(`return 0`) 다음, `rows = list(load_cameras_api(items))` 앞에 넣는다:

```python
  missing = [field for field in CAMERA_API_KIND_FIELDS if not any(field in item for item in items)]
  if items and missing:
    # A renamed field reads as None on every item, which files every camera under
    # CAMERA_SPEED: a database that looks fine and ignores the kind toggles. load_cameras
    # raises on the CSV equivalent; this path keeps the database it has instead.
    LOG.warning("camera refresh: the api stopped sending %s -- keeping the existing database", missing)
    return 0
```

`_due`의 마지막 줄 `return age >= REFRESH_INTERVAL_S`(159행)를 교체한다(위 TID251 주석과 `try` 블록은 그대로):

```python
    # A database from before the kind column opens fine but ignores every kind toggle, so
    # replace it on the next unmetered tick rather than whenever the week runs out.
    return age >= REFRESH_INTERVAL_S or not current_has_kind(self.cameras_path)
```

- [ ] **Step 4: 호스트와 컨테이너에서 돌린다**

Run:
```bash
cd E:/dev/sunnypilot
python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh -v
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh'
```
Expected: 둘 다 `OK`.

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/mapd/korea/camera_refresh.py openpilot/sunnypilot/mapd/korea/tests/test_camera_refresh.py
git commit -F - <<'EOF'
feat: rebuild a camera database without kinds early, and refuse an api that drops them

A camera database from before the kind column is due for a refresh at
once, so the kind toggles start working on the next unmetered
connection instead of within the week. A refresh whose items lack one
of the three code fields keeps the existing database.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 4: 파라미터와 SCC-Map 카메라 점

**Files:**
- Modify: `openpilot/common/params_keys.h:277-278` (새 키 다섯)
- Modify: `openpilot/sunnypilot/mapd/korea/db.py` (`CAMERA_ZONE` 아래에 `CAMERA_KIND_PARAMS`)
- Modify: `openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py` — 머리 주석(7-14행), import(26-27행), 상수(42행 아래), `__init__`(73-74행), `update_location`(207행), `read_camera_params`(새), `camera_point`(새), `publish_targets`(253-282행), `tick`(287행)
- Test: `openpilot/sunnypilot/mapd/tests/test_korea_map_data.py`

**Interfaces:**
- Consumes: `next_camera(..., kinds=...)`, `Camera.lat/lon/kind`(Task 2).
- Produces:
  - `db.CAMERA_KIND_PARAMS: dict[int, str]` — `{CAMERA_SPEED: "KoreaCameraSpeedEnabled", CAMERA_SIGNAL: "KoreaCameraSignalEnabled", CAMERA_SECTION: "KoreaCameraSectionEnabled", CAMERA_ZONE: "KoreaCameraZoneEnabled"}` (Task 5·8이 쓴다)
  - `korea_map_data.CAMERA_MARGIN_RANGE = (0, 300)` (Task 8의 테스트가 쓴다)
  - `KoreaMapData.read_camera_params() -> None` → `self.camera_kinds: frozenset[int]`, `self.camera_margin: int`
  - `KoreaMapData.camera_point() -> tuple[float, float, float] | None` — `(lat, lon, velocity m/s)`

- [ ] **Step 1: 실패하는 테스트를 쓴다 (`mapd/tests/test_korea_map_data.py`)**

27행 import를 교체한다:

```python
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, BUMP_VIRTUAL, CAMERA_KIND_PARAMS,
                                                 CAMERA_SECTION, CAMERA_SIGNAL, CAMERA_SPEED, CAMERA_ZONE, Bump,
                                                 Camera, Link)
```

`make_data`의 `data.bump_targets = {}` 아래에 두 줄:

```python
  data.camera_kinds = frozenset(CAMERA_KIND_PARAMS)
  data.camera_margin = 50
```

`make_bump_data`(85-95행)를 교체한다:

```python
def make_bump_data(bump=None, enabled=True, arch_kph=25, trapezoid_kph=35, position=None, localizer_valid=True,
                   camera=None, margin=50):
  """A KoreaMapData wired only far enough to exercise publish_targets."""
  data = KoreaMapData.__new__(KoreaMapData)
  data.mem_params = StubMemParams()
  data.bump = bump
  data.bump_enabled = enabled
  data.bump_targets = {BUMP_ARCH: arch_kph * CV.KPH_TO_MS, BUMP_TRAPEZOID: trapezoid_kph * CV.KPH_TO_MS}
  data.camera = camera
  data.camera_margin = margin
  data.last_position = position if position is not None else Coordinate(37.5, 127.0)
  data.localizer_valid = localizer_valid
  data.curve_points = []
  return data
```

`StubDB.next_camera`(326행)의 시그니처:

```python
  def next_camera(self, lat, lon, heading_deg, route=None, kinds=None):
```

`TestTickOrdering.test_tick_runs_publish_targets_after_the_base_tick`에서 `data.read_bump_params = ...` 아래에 한 줄을 더하고, 기대 목록을 바꾼다:

```python
    data.read_camera_params = lambda: calls.append("read_camera_params")
```

```python
    self.assertEqual(calls, ["read_bump_params", "read_camera_params", "update_destination", "sm.update",
                             "update_location", "publish", "publish_targets"])
```

`test_an_early_return_in_update_location_still_clears_the_target`의 `data.read_bump_params = lambda: None ...` 아래에 한 줄:

```python
    data.read_camera_params = lambda: None  # covered by TestReadCameraParams; irrelevant here
```

`TestRouteReachesTheLookups` 끝에 메서드를 더한다:

```python
  def test_the_enabled_kinds_reach_next_camera(self):
    data = make_data()
    data.sm = SingleLocationSM(valid_llk())
    data.last_position = Coordinate(37.5000, 127.0200)
    data.camera_kinds = frozenset({CAMERA_ZONE})

    seen = {}
    data.db = SimpleNamespace(
      reload_if_changed=lambda: False,
      current_link=lambda *a, **k: None,
      next_camera=lambda *a, **k: seen.update(kinds=k.get("kinds")),
      next_bump=lambda *a, **k: None,
    )

    data.update_location()
    self.assertEqual(seen["kinds"], frozenset({CAMERA_ZONE}))
```

파일 끝에 두 클래스를 더한다:

```python
class TestReadCameraParams(OpenpilotTestCase):
  """Round-trips the real Params keys, like TestReadBumpParams: a wrong key name or a lost
  default only shows up here."""

  def read(self):
    data = KoreaMapData.__new__(KoreaMapData)
    data.params = Params()
    data.read_camera_params()
    return data

  def test_the_defaults_are_every_kind_on_and_50_m(self):
    """get_bool does not fall back to a default -- manager_init writes the defaults into the
    unset params at boot. So the defaults are checked here, and the read separately below."""
    params = Params()
    for key in CAMERA_KIND_PARAMS.values():
      self.assertTrue(params.get_default_value(key), key)
    self.assertEqual(params.get_default_value("KoreaCameraMargin"), 50)

  def test_a_kind_that_is_off_is_left_out(self):
    params = Params()
    for key in CAMERA_KIND_PARAMS.values():
      params.put_bool(key, True, block=True)
    params.put_bool("KoreaCameraZoneEnabled", False, block=True)
    self.assertEqual(self.read().camera_kinds, {CAMERA_SPEED, CAMERA_SIGNAL, CAMERA_SECTION})

  def test_the_margin_falls_back_to_its_default_and_is_clamped(self):
    self.assertEqual(self.read().camera_margin, 50)  # unset: get_sanitize_int_param reads the default
    Params().put("KoreaCameraMargin", 500, block=True)
    self.assertEqual(self.read().camera_margin, 300)


class TestCameraTarget(unittest.TestCase):
  """The next camera's SCC-Map point: its limit, camera_margin metres short of the camera."""

  CAR = Coordinate(37.5000, 127.0200)
  AT = Coordinate(37.5000, 127.0257)   # ~503 m east

  def camera(self, at=None, limit_kph=50):
    at = at if at is not None else self.AT
    return Camera(lat=at.latitude, lon=at.longitude, limit_kph=limit_kph,
                  distance_m=self.CAR.distance_to(at), section_m=0, kind=CAMERA_SPEED)

  def publish(self, data):
    data.publish_targets()
    return json.loads(data.mem_params.values["MapTargetVelocities"])

  def test_the_point_sits_the_margin_short_of_the_camera(self):
    camera = self.camera()
    points = self.publish(make_bump_data(camera=camera, margin=50, position=self.CAR))
    self.assertEqual(len(points), 1)
    point = Coordinate(points[0]["latitude"], points[0]["longitude"])
    self.assertAlmostEqual(self.CAR.distance_to(point), camera.distance_m - 50., delta=0.5)
    self.assertAlmostEqual(point.distance_to(self.AT), 50., delta=0.5)
    # the camera's own limit: no speed limit offset on this path
    self.assertAlmostEqual(points[0]["velocity"], 50 * CV.KPH_TO_MS)

  def test_a_zero_margin_puts_the_point_on_the_camera(self):
    points = self.publish(make_bump_data(camera=self.camera(), margin=0, position=self.CAR))
    self.assertAlmostEqual(points[0]["latitude"], self.AT.latitude)
    self.assertAlmostEqual(points[0]["longitude"], self.AT.longitude)

  def test_inside_the_margin_the_point_is_the_car(self):
    """SCC-Map keeps a point at distance zero due, so the limit holds until the camera is passed."""
    near = Coordinate(37.5000, 127.0203)   # ~26 m east, inside a 50 m margin
    points = self.publish(make_bump_data(camera=self.camera(at=near), margin=50, position=self.CAR))
    self.assertAlmostEqual(points[0]["latitude"], self.CAR.latitude)
    self.assertAlmostEqual(points[0]["longitude"], self.CAR.longitude)

  def test_the_camera_joins_the_bump_and_the_curves_nearest_first(self):
    data = make_bump_data(bump=Bump(lat=37.5000, lon=127.0217, kind=BUMP_ARCH, distance_m=150.),
                          camera=self.camera(), position=self.CAR)
    data.curve_points = [(37.5000, 127.0234, 12.)]
    self.assertEqual([p["velocity"] for p in self.publish(data)], [25 * CV.KPH_TO_MS, 12., 50 * CV.KPH_TO_MS])

  def test_an_invalid_localizer_drops_the_camera_too(self):
    data = make_bump_data(camera=self.camera(), position=self.CAR, localizer_valid=False)
    self.assertEqual(self.publish(data), [])
```

- [ ] **Step 2: 실패를 확인한다**

Run:
```bash
cd E:/dev/sunnypilot
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_korea_map_data'
```
Expected: `ImportError: cannot import name 'CAMERA_KIND_PARAMS' from 'openpilot.sunnypilot.mapd.korea.db'`

- [ ] **Step 3: `params_keys.h`에 키를 더하고 재빌드한다**

`// korea map` 블록의 `{"KoreaExternalNavEnabled", ...}`(278행) 앞에 넣는다(알파벳순):

```cpp
    {"KoreaCameraMargin", {PERSISTENT | BACKUP, INT, "50"}},              // m, 제한속도 도달 여유 거리
    {"KoreaCameraSectionEnabled", {PERSISTENT | BACKUP, BOOL, "1"}},      // 구간단속 시점·종점
    {"KoreaCameraSignalEnabled", {PERSISTENT | BACKUP, BOOL, "1"}},       // 신호·과속
    {"KoreaCameraSpeedEnabled", {PERSISTENT | BACKUP, BOOL, "1"}},        // 과속
    {"KoreaCameraZoneEnabled", {PERSISTENT | BACKUP, BOOL, "1"}},         // 노인·어린이 보호구역
```

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
```
Expected: `scons: done building targets.`

- [ ] **Step 4: `db.py`에 토글 매핑을 더한다**

Task 1에서 넣은 `CAMERA_ZONE = 3 ...` 줄 아래:

```python

# The toggle behind each kind, all on by default (params_keys.h). korea_map_data drops the
# kinds that are off; map_controller keeps SCC-Map on while any of them is on.
CAMERA_KIND_PARAMS = {
  CAMERA_SPEED: "KoreaCameraSpeedEnabled",
  CAMERA_SIGNAL: "KoreaCameraSignalEnabled",
  CAMERA_SECTION: "KoreaCameraSectionEnabled",
  CAMERA_ZONE: "KoreaCameraZoneEnabled",
}
```

- [ ] **Step 5: `korea_map_data.py`를 고친다**

머리 주석 11-13행("Current speed limits come from ... what a camera calls for.")을 교체한다:

```python
Current speed limits come from ITS 표준노드링크 MAX_SPD. The "next" speed limit is the next
speed camera ahead, and on liveMapDataSP it only feeds the speed-limit-ahead sign:
SpeedLimitResolver ignores speedLimitAhead in Korea mode. The slowdown goes to
SmartCruiseControlMap instead, as one more MapTargetVelocities point beside the bumps and
the curves (publish_targets). SpeedLimitAssist would hold it behind a confirmation prompt
under 80 km/h and add the speed limit offset on top.
```

26-27행 import를 교체한다:

```python
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, CAMERA_KIND_PARAMS, Bump, Camera,
                                                 KoreaMapDB, Link, mtime_or_none)
```

`BUMP_TRAPEZOID_SPEED_RANGE = (20, 50)`(42행) 아래:

```python

# m, how far before a camera to be at its limit. Bounds, not the default -- that lives in
# params_keys.h, like the bump speeds.
CAMERA_MARGIN_RANGE = (0, 300)
```

`__init__`의 `self.bump_targets: dict[int, float] = {}`(74행) 아래:

```python
    # read_camera_params overwrites both on the first tick, before anything reads them
    self.camera_kinds: frozenset[int] = frozenset(CAMERA_KIND_PARAMS)
    self.camera_margin = 0
```

`update_location`의 `next_camera` 호출(207행):

```python
      self.camera = self.db.next_camera(lat, lon, self.last_bearing, route=self.route, kinds=self.camera_kinds)
```

`read_bump_params`(243-251행) 아래에 두 메서드를 넣는다:

```python
  def read_camera_params(self) -> None:
    """Same 1 Hz as read_bump_params, for the same reason."""
    self.camera_kinds = frozenset(kind for kind, key in CAMERA_KIND_PARAMS.items() if self.params.get_bool(key))
    self.camera_margin = get_sanitize_int_param("KoreaCameraMargin", *CAMERA_MARGIN_RANGE, self.params)

  def camera_point(self) -> tuple[float, float, float] | None:
    """The next camera as an SCC-Map target: its limit, camera_margin metres short of it.

    The point sits on the straight line from the car to the camera. SCC-Map measures
    straight-line distance too, so the car reaches the limit about camera_margin metres
    before the camera as the crow flies -- on a winding road that is a little early, never
    late. Inside the margin the point is the car's own position: SCC-Map keeps treating a
    point at distance zero as due, so the car stays near the limit until next_camera lets
    the camera go.

    No speed limit offset. SpeedLimitAssist adds one to road limits; a camera enforces its
    own number.
    """
    if self.camera is None or self.last_position is None:
      return None
    camera, car = self.camera, self.last_position
    share = max(0., camera.distance_m - self.camera_margin) / camera.distance_m if camera.distance_m > 0. else 0.
    return (car.latitude + (camera.lat - car.latitude) * share,
            car.longitude + (camera.lon - car.longitude) * share,
            camera.limit_kph * CV.KPH_TO_MS)
```

`publish_targets`(253-282행)를 교체한다:

```python
  def publish_targets(self) -> None:
    """Hand the next bump, the next camera AND the curves ahead to SmartCruiseControlMap.

    One writer, not two: SCC-Map reads the whole list from this one param, so a second
    writer would delete the first one's points every tick. The bump feature shipped first
    and owned the param alone -- it now shares it.

    Written on every tick, cleared when there is nothing ahead. SCC-Map has no staleness
    check of its own -- it trusts whatever is in the param -- so 'stop writing' is not a
    way to turn this off; only an empty list is.

    Also requires localizer_valid: last_position/last_bearing only update while the
    localizer is valid (see update_location), so a localizer that stops updating would
    otherwise freeze self.bump and self.camera (and republish self.curve_points against a
    stale car position) forever -- the car keeps moving, SCC-Map keeps seeing a constant
    distance, and the slowdown never releases.
    """
    points: list[tuple[float, float, float]] = []
    if self.localizer_valid and self.last_position is not None:
      if self.bump_enabled and self.bump is not None:
        target = self.bump_targets.get(self.bump.kind, 0.)
        if target > 0.:
          points.append((self.bump.lat, self.bump.lon, target))
      camera = self.camera_point()
      if camera is not None:
        points.append(camera)
      points.extend(self.curve_points)
      points.sort(key=lambda p: self.last_position.distance_to(Coordinate(p[0], p[1])))

    self.mem_params.put("MapTargetVelocities", json.dumps(
      [{"latitude": lat, "longitude": lon, "velocity": velocity} for lat, lon, velocity in points]))
    if self.last_position is not None:
      self.mem_params.put("LastGPSPosition", json.dumps(self.last_position.as_dict()))
```

`tick`의 `self.read_bump_params()`(287행) 아래에 한 줄:

```python
    self.read_camera_params()
```

- [ ] **Step 6: 테스트를 돌린다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.mapd.tests.test_korea_map_data openpilot.sunnypilot.mapd.tests.test_mapd_source -v'
```
Expected: `OK`.

- [ ] **Step 7: 커밋한다**

```bash
git add openpilot/common/params_keys.h openpilot/sunnypilot/mapd/korea/db.py \
  openpilot/sunnypilot/mapd/live_map_data/korea_map_data.py openpilot/sunnypilot/mapd/tests/test_korea_map_data.py
git commit -F - <<'EOF'
feat: slow for the next camera through SCC-Map, a set distance before it

korea_map_data reads the four camera kind toggles and the arrival margin
every tick, hands the enabled kinds to next_camera, and publishes the
camera's limit as a MapTargetVelocities point the margin short of the
camera. The speed limit ahead sign still shows the true distance.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 5: SCC-Map 한국 분기를 카메라 토글로도 켠다

**Files:**
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py:10-12` (import), `:100-119` (`_get_enabled`)
- Test: `openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/tests/test_map_controller.py`

**Interfaces:**
- Consumes: `db.CAMERA_KIND_PARAMS`(Task 4).
- Produces: 한국 모드에서 `SmartCruiseControlMap._get_enabled()`는 `KoreaSpeedBumpEnabled`, `KoreaExternalNavEnabled`, 카메라 토글 넷 중 하나라도 켜져 있으면 `True`.

- [ ] **Step 1: 테스트를 고치고 실패하는 테스트를 쓴다**

import에 한 줄(16행 아래):

```python
from openpilot.sunnypilot.mapd.korea.db import CAMERA_KIND_PARAMS
```

`TestSmartCruiseControlMap` 안, `reset_params` 아래에 더한다:

```python
  # Every Korea toggle that feeds SCC-Map. On a device the camera kinds are on by default
  # (params_keys.h, written by manager_init at boot), so a test that wants the Korea branch
  # off turns them all off rather than trusting this test's empty params.
  KOREA_TOGGLES = ("KoreaSpeedBumpEnabled", "KoreaExternalNavEnabled", *CAMERA_KIND_PARAMS.values())

  def korea_toggles_off(self):
    for key in self.KOREA_TOGGLES:
      self.params.put_bool(key, False, block=True)
```

기존 테스트 셋을 고친다:
- `test_korea_mode_disables_map_even_with_toggle_on`: `self.params.put_bool("KoreaSpeedBumpEnabled", False, block=True)`(67행) → `self.korea_toggles_off()`
- `test_korea_source_is_gated_on_the_bump_toggle_not_the_osm_toggle`: 89행 같은 줄 → `self.korea_toggles_off()`
- `test_korea_source_is_also_gated_on_the_external_nav_toggle`: 102-103행 두 줄 → `self.korea_toggles_off()` 한 줄

`test_korea_bump_toggle_does_not_leak_into_the_osm_source` 위에 새 테스트:

```python
  def test_korea_source_is_also_gated_on_each_camera_kind_toggle(self):
    """korea_map_data publishes a camera point while any kind is on; the controller must not
    stay off just because the bump and nav toggles are."""
    self.params.put("MapDataSource", int(MapSource.korea), block=True)
    for key in CAMERA_KIND_PARAMS.values():
      with self.subTest(key=key):
        self.korea_toggles_off()
        self.assertFalse(SmartCruiseControlMap()._get_enabled())
        self.params.put_bool(key, True, block=True)
        self.assertTrue(SmartCruiseControlMap()._get_enabled())
```

- [ ] **Step 2: 실패를 확인한다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.tests.test_map_controller -v'
```
Expected: `test_korea_source_is_also_gated_on_each_camera_kind_toggle`의 subtest 넷이 `AssertionError: False is not true`로 실패. 나머지는 통과.

- [ ] **Step 3: `map_controller.py`를 고친다**

import(10행 `from openpilot.sunnypilot.mapd import MapSource` 아래):

```python
from openpilot.sunnypilot.mapd.korea.db import CAMERA_KIND_PARAMS
```

`_get_enabled`(100-119행)를 교체한다:

```python
  def _get_enabled(self) -> bool:
    """One set of toggles per source, because the two sources fill this controller differently.

    OSM: the native mapd binary streams curve target velocities, gated by
    SmartCruiseControlMap. Korea: korea_map_data writes the next speed bump, the next speed
    camera and the curves ahead, so any Korea toggle that feeds one of them enables this:
    KoreaSpeedBumpEnabled, the four camera kind toggles, or KoreaExternalNavEnabled -- curve
    targets need a fetched route, and KoreaExternalNavEnabled is what starts the route
    thread. An empty MapTargetVelocities list still leaves the state machine a no-op, so
    enabling on a toggle whose feature has nothing ahead costs nothing. Neither source's
    toggles may enable the other source -- the danger is a snapshot that stops advancing
    after a source switch, and each writer is only trusted to keep its own param fresh.
    """
    source = self.params.get("MapDataSource", return_default=True)
    if source == MapSource.osm:
      return self.params.get_bool("SmartCruiseControlMap")
    if source == MapSource.korea:
      keys = ("KoreaSpeedBumpEnabled", "KoreaExternalNavEnabled", *CAMERA_KIND_PARAMS.values())
      return any(self.params.get_bool(key) for key in keys)
    return False
```

- [ ] **Step 4: 테스트를 돌린다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.tests.test_map_controller openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.tests.test_vision_controller'
```
Expected: `OK`.

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py \
  openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/tests/test_map_controller.py
git commit -F - <<'EOF'
feat: enable SCC-Map in Korea mode when any camera kind is on

korea_map_data now publishes a camera point, so the Korea branch has to
turn on for the camera kind toggles as well as for bumps and routed
curves.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 6: 한국 모드에서 SLA가 카메라를 무시한다

**Files:**
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_resolver.py` — import(15행 근처), `__init__`(69행 아래), `update_params`(92-97행), `_process_map_data`(130-131행)
- Test: `openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/tests/test_speed_limit_resolver.py`

**Interfaces:**
- Consumes: 없음(`MapSource`, 기존 `MapDataSource` 파라미터).
- Produces: `SpeedLimitResolver.map_source` — `MapDataSource` 값. 한국 모드에서 `speedLimitAhead`는 limit 계산에 들어가지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

import에 세 줄(12행 `from openpilot.cereal import custom` 근처):

```python
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.sunnypilot.mapd import MapSource
```

`TestSpeedLimitResolverValidation` 끝에 더한다:

```python
  @parameterized.expand([(MapSource.korea, 80.), (MapSource.osm, 50.)], names=["source", "expected_kph"])
  def test_speed_limit_ahead_only_counts_outside_korea(self, resolver_class, mocker, source, expected_kph):
    """Korea publishes the next speed camera as speedLimitAhead, and SmartCruiseControlMap
    slows for it. OSM's is the next posted limit, still SLA's to adapt to."""
    Params().put("MapDataSource", int(source), block=True)
    resolver = resolver_class()
    resolver.policy = Policy.map_data_only
    sm_mock = setup_sm_mock(mocker)
    map_data = sm_mock['liveMapDataSP']
    map_data.speedLimit = 80 * CV.KPH_TO_MS
    map_data.speedLimitAhead = 50 * CV.KPH_TO_MS
    map_data.speedLimitAheadValid = True
    map_data.speedLimitAheadDistance = 50.  # well inside the ~150 m it takes to go from 80 to 50

    resolver.update(80 * CV.KPH_TO_MS, sm_mock)

    self.assertAlmostEqual(resolver.speed_limit, expected_kph * CV.KPH_TO_MS)
```

- [ ] **Step 2: 실패를 확인한다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/selfdrive/controls/lib/speed_limit sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_resolver -v'
```
Expected: `test_speed_limit_ahead_only_counts_outside_korea_0`이 `AssertionError: 13.88... != 22.22...`로 실패. `_1`(OSM)은 통과.

- [ ] **Step 3: `speed_limit_resolver.py`를 고친다**

import(15행 `from openpilot.sunnypilot import ...` 아래):

```python
from openpilot.sunnypilot.mapd import MapSource
```

`__init__`의 `self.offset_value = ...`(69행) 아래:

```python
    self.map_source = self.params.get("MapDataSource", return_default=True)
```

`update_params`의 if 블록 끝(97행 아래):

```python
      self.map_source = self.params.get("MapDataSource", return_default=True)
```

`_process_map_data`의 `next_speed_limit = ...`(131행)를 교체한다:

```python
    # Korea publishes the next speed camera as speedLimitAhead. SmartCruiseControlMap slows
    # for it (korea_map_data.publish_targets); here it would only add the confirmation prompt
    # SLA puts on anything under 80 km/h, and the speed limit offset on top.
    next_speed_limit = 0.
    if map_data.speedLimitAheadValid and self.map_source != MapSource.korea:
      next_speed_limit = map_data.speedLimitAhead
```

- [ ] **Step 4: 테스트를 돌린다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/selfdrive/controls/lib/speed_limit sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_resolver openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_assist'
```
Expected: `OK`.

- [ ] **Step 5: 커밋한다**

```bash
git add openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_resolver.py \
  openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/tests/test_speed_limit_resolver.py
git commit -F - <<'EOF'
feat: leave Korean speed cameras to SCC-Map instead of SLA

In Korea mode speedLimitAhead is the next speed camera, which SCC-Map now
slows for. SLA kept it behind a driver confirmation below 80 km/h and
added the speed limit offset, so the resolver ignores it there. OSM mode
is unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 7: 기기 설정 화면 (tici, mici)

**Files:**
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py` — `_initialize_items`(117행 아래, 163-180행 목록), `_update_state`(270행 아래)
- Modify: `openpilot/selfdrive/ui/mici/layouts/settings/toggles.py` — 59행 아래, 105-134행, 141행 아래
- Test: `openpilot/selfdrive/ui/tests/test_speed_limit_settings_korea_gating.py`, `openpilot/selfdrive/ui/mici/tests/test_toggles_api_key.py`

**Interfaces:**
- Consumes: Task 4의 파라미터 키.
- Produces: tici `SpeedLimitSettingsLayout._camera_kinds`(토글 넷의 튜플), `._camera_margin`. mici `_refresh_toggles`에 카메라 키 넷.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`test_speed_limit_settings_korea_gating.py` 머리 주석의 `_speed_bump ...` 항목 아래(13행 뒤)에 두 줄을 더한다:

```
  - the four camera kind toggles require MapDataSource == korea only -- they also filter the
    speed limit ahead sign, which every car shows; _camera_margin also requires long or ICBM.
```

`test_bump_speed_label_converts_for_is_metric` 위에 새 테스트:

```python
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_camera_kind_gating(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_speed_limit_settings_camera_kind_gating")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise_sub_layouts.speed_limit_settings import SpeedLimitSettingsLayout
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.sunnypilot.mapd import MapSource

    # Same process-wide-singleton hazard as the tests above.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original_source = ui_state.params.get("MapDataSource", return_default=True)
    original_cp = ui_state.CP
    original_cp_sp = ui_state.CP_SP
    original_has_long = ui_state.has_longitudinal_control
    original_has_icbm = ui_state.has_icbm

    def _restore():
      ui_state.params.put("MapDataSource", int(original_source), block=True)
      ui_state.CP = original_cp
      ui_state.CP_SP = original_cp_sp
      ui_state.has_longitudinal_control = original_has_long
      ui_state.has_icbm = original_has_icbm
    self.addCleanup(_restore)

    layout = SpeedLimitSettingsLayout(lambda: None)

    # CP_SP stays None for the same reason as in test_speed_bump_gating.
    ui_state.CP = SimpleNamespace()
    ui_state.CP_SP = None

    with subtests.test(case="korea + no longitudinal -> kinds enabled (they filter the HUD sign), margin disabled"):
      ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
      ui_state.has_longitudinal_control = False
      ui_state.has_icbm = False
      layout._update_state()
      self.assertTrue(all(toggle.action_item.enabled for toggle in layout._camera_kinds))
      self.assertFalse(layout._camera_margin.action_item.enabled)

    with subtests.test(case="korea + longitudinal -> margin enabled too"):
      ui_state.has_longitudinal_control = True
      layout._update_state()
      self.assertTrue(layout._camera_margin.action_item.enabled)

    with subtests.test(case="osm -> everything camera-related disabled"):
      ui_state.params.put("MapDataSource", int(MapSource.osm), block=True)
      layout._update_state()
      self.assertFalse(any(toggle.action_item.enabled for toggle in layout._camera_kinds))
      self.assertFalse(layout._camera_margin.action_item.enabled)
```

`test_toggles_api_key.py`의 `TestKoreaModeGating` 끝에 새 테스트:

```python
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_camera_kind_toggles_follow_the_map_source_only(self):
    """Not offroad-gated and not gated on longitudinal control: a kind that is off also
    leaves the speed limit ahead sign, which a stock-ACC car shows too."""
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_camera_kind_toggles")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.mici.layouts.settings.toggles import TogglesLayoutMici
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.sunnypilot.mapd import MapSource

    _ensure_params_dir(self, ui_state)

    original_source = ui_state.params.get("MapDataSource", return_default=True)
    original_started = ui_state.started

    def _restore():
      ui_state.params.put("MapDataSource", int(original_source), block=True)
      ui_state.started = original_started
    self.addCleanup(_restore)

    layout = TogglesLayoutMici()
    toggles = dict(layout._refresh_toggles)
    keys = ("KoreaCameraSpeedEnabled", "KoreaCameraSignalEnabled", "KoreaCameraSectionEnabled", "KoreaCameraZoneEnabled")

    ui_state.started = True  # onroad is no reason to lock these
    ui_state.params.put("MapDataSource", int(MapSource.osm), block=True)
    self.assertFalse(any(toggles[key].enabled for key in keys))

    ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
    self.assertTrue(all(toggles[key].enabled for key in keys))
```

- [ ] **Step 2: 실패를 확인한다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/ui sp-build:/work/openpilot/selfdrive/
docker exec sp-build bash -lc 'cd /work && xvfb-run -a ./.venv/bin/python -m unittest openpilot.selfdrive.ui.tests.test_speed_limit_settings_korea_gating openpilot.selfdrive.ui.mici.tests.test_toggles_api_key -v'
```
Expected: `AttributeError: 'SpeedLimitSettingsLayout' object has no attribute '_camera_kinds'`와 `KeyError: 'KoreaCameraSpeedEnabled'`. 결과 줄에 `skipped`가 없어야 한다 — 있으면 xvfb가 안 잡힌 것이다.

- [ ] **Step 3: tici `speed_limit_settings.py`를 고친다**

`_initialize_items`의 `self._route_api_key = ...` 블록(109-117행) 아래에 넣는다:

```python
    self._camera_speed = toggle_item_sp(
      title=lambda: tr("Speed Cameras"),
      description=tr("Slow down for fixed cameras that enforce speed only. Turning a type off also " +
                     "hides its cameras from the speed limit ahead sign."),
      param="KoreaCameraSpeedEnabled")

    self._camera_signal = toggle_item_sp(
      title=lambda: tr("Signal + Speed Cameras"),
      description=tr("Slow down for intersection cameras that enforce both red lights and speed. " +
                     "Turning a type off also hides its cameras from the speed limit ahead sign."),
      param="KoreaCameraSignalEnabled")

    self._camera_section = toggle_item_sp(
      title=lambda: tr("Section Enforcement"),
      description=tr("Slow down at the start and end cameras of average-speed sections. The speed " +
                     "between them is not held. Turning a type off also hides its cameras from the " +
                     "speed limit ahead sign."),
      param="KoreaCameraSectionEnabled")

    self._camera_zone = toggle_item_sp(
      title=lambda: tr("Protected Zones"),
      description=tr("Slow down for cameras in school and senior protection zones. A camera inside a " +
                     "zone follows this toggle whatever else it enforces. Turning a type off also " +
                     "hides its cameras from the speed limit ahead sign."),
      param="KoreaCameraZoneEnabled")

    self._camera_kinds = (self._camera_speed, self._camera_signal, self._camera_section, self._camera_zone)

    self._camera_margin = option_item_sp(
      title=lambda: tr("Camera Arrival Margin"),
      param="KoreaCameraMargin",
      min_value=0, max_value=300, value_change_step=10,
      description=tr("Reach the camera's limit about this far before the camera. Speed detectors " +
                     "often sit tens of metres ahead of the pole."),
      label_callback=lambda value: f"{value} m")
```

`items` 목록(163-180행)에서 `self._route_api_key,` 다음, `self._speed_bump,` 앞에 넣는다:

```python
      *self._camera_kinds,
      self._camera_margin,
```

`_update_state`의 `self._bump_trapezoid_speed.action_item.set_enabled(bump_on)`(270행) 아래에 넣는다:

```python
    # The kind toggles are not gated on longitudinal control: a kind that is off also leaves
    # the speed limit ahead sign, which every car shows. The margin only moves the SCC-Map
    # slowdown, so it gets the same gate as Speed Bump Slowdown.
    for toggle in self._camera_kinds:
      toggle.action_item.set_enabled(is_korea)
    self._camera_margin.action_item.set_enabled(is_korea and has_long_or_icbm)
```

- [ ] **Step 4: mici `toggles.py`를 고친다**

`korea_download_toggle = ...`(59행) 아래:

```python
    korea_camera_toggles = (
      ("KoreaCameraSpeedEnabled", BigParamControl("speed cameras", "KoreaCameraSpeedEnabled")),
      ("KoreaCameraSignalEnabled", BigParamControl("signal + speed cameras", "KoreaCameraSignalEnabled")),
      ("KoreaCameraSectionEnabled", BigParamControl("section enforcement cameras", "KoreaCameraSectionEnabled")),
      ("KoreaCameraZoneEnabled", BigParamControl("protected zone cameras", "KoreaCameraZoneEnabled")),
    )
```

`self._scroller.add_widgets([...])`에서 `korea_nav_toggle,` 다음 줄에:

```python
      *(toggle for _, toggle in korea_camera_toggles),
```

`self._refresh_toggles`에서 `("KoreaExternalNavEnabled", korea_nav_toggle),` 다음 줄에:

```python
      *korea_camera_toggles,
```

`korea_nav_toggle.set_enabled(...)`(141행) 아래:

```python
    # Not offroad-gated and not gated on longitudinal control: a kind that is off also leaves
    # the speed limit ahead sign, which a stock-ACC car shows too.
    for _, toggle in korea_camera_toggles:
      toggle.set_enabled(lambda: ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea)
```

- [ ] **Step 5: 테스트를 돌린다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/ui sp-build:/work/openpilot/selfdrive/
docker exec sp-build bash -lc 'cd /work && xvfb-run -a ./.venv/bin/python -m unittest openpilot.selfdrive.ui.tests.test_speed_limit_settings_korea_gating openpilot.selfdrive.ui.mici.tests.test_toggles_api_key -v'
```
Expected: `Ran 8 tests` `OK`, skip 없음. raylib 폰트 경고(`FONT: Requested codepoints glyphs found`)는 기존 노이즈다.

- [ ] **Step 6: 커밋한다**

```bash
git add openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py \
  openpilot/selfdrive/ui/mici/layouts/settings/toggles.py \
  openpilot/selfdrive/ui/tests/test_speed_limit_settings_korea_gating.py \
  openpilot/selfdrive/ui/mici/tests/test_toggles_api_key.py
git commit -F - <<'EOF'
feat: add the camera kind toggles and arrival margin to both device UIs

tici gets the four kind toggles and the 0-300 m margin above the speed
bump items; mici gets the four toggles. The toggles follow the Korea map
source only, since they also filter the speed limit ahead sign; the
margin also needs longitudinal control or ICBM.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

---

### Task 8: 써니링크 — Cruise 섹션 셋, `korea` 페이지 삭제

**Files:**
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` — 237-324행(Speed Limit Settings 안의 한국 항목 다섯) 삭제, `- id: smart_cruise`(349행) 앞에 섹션 셋
- Delete: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml`
- Regenerate: `openpilot/sunnypilot/sunnylink/settings_ui.json`
- Test: `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`, `openpilot/sunnypilot/sunnylink/tests/test_compile_settings_ui.py`

**Interfaces:**
- Consumes: Task 4의 파라미터 키, `CAMERA_KIND_PARAMS`, `CAMERA_MARGIN_RANGE`.
- Produces: `cruise` 패널의 섹션 `korea_speed_cameras`, `korea_speed_bumps`, `korea_route_map_data`(이 순서로 `speed_limits`와 `smart_cruise` 사이). `korea` 패널은 없다.

- [ ] **Step 1: 테스트를 먼저 고친다**

`test_compile_settings_ui.py`:
- 129-130행을 교체한다:
  ```python
    assert {"steering", "cruise", "display", "visuals", "toggles",
            "device", "software", "developer", "models"} <= panel_ids
    assert "korea" not in panel_ids, "the app has no sidebar entry for a korea page -- its items live on cruise"
  ```
- 168-169행을 교체한다:
  ```python
    # 9 panels + 1 vehicle = 10
    assert len(page_files) == 10, f"expected 10 pages, found {len(page_files)}: {page_files}"
  ```

`test_settings_changes.py`:
- import에 두 줄(40행 `)` 아래):
  ```python
  from openpilot.sunnypilot.mapd.korea.db import CAMERA_KIND_PARAMS
  from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import CAMERA_MARGIN_RANGE
  ```
- `class TestKoreaApiKeyRemote`(519-567행) 전체를 아래로 교체한다:

```python
KOREA_SECTIONS = ("korea_speed_cameras", "korea_speed_bumps", "korea_route_map_data")


def _section_keys(schema: dict[str, Any], panel_id: str, section_id: str) -> list[str]:
  section = _find_section(schema, panel_id, section_id)
  return [item["key"] for item in section.get("items", [])] if section else []


class TestKoreaSectionsOnCruise(OpenpilotTestCase):
  """The app's settings sidebar is hardcoded (sunnylink-frontend src/routes/+layout.svelte),
  so a korea page was never reachable. Its items, and the Korea items that sat in the speed
  limit sub-panel, now live in three cruise sections grouped by what they do. A text item
  the app cannot render shows as an empty row (SchemaItemRenderer.svelte has no branch for
  it) and takes nothing else on the page down, so the keys no longer need a page of their own."""

  def test_there_is_no_korea_panel(self, schema):
    assert _find_panel(schema, "korea") is None

  def test_the_three_sections_sit_between_speed_limits_and_smart_cruise(self, schema):
    ids = [s["id"] for s in _find_panel(schema, "cruise")["sections"]]
    start = ids.index("speed_limits")
    assert ids[start:start + 5] == ["speed_limits", *KOREA_SECTIONS, "smart_cruise"], ids

  def test_each_section_holds_its_items_in_order(self, schema):
    assert _section_keys(schema, "cruise", "korea_speed_cameras") == [
      "KoreaCameraSpeedEnabled", "KoreaCameraSignalEnabled", "KoreaCameraSectionEnabled",
      "KoreaCameraZoneEnabled", "KoreaCameraMargin", "KoreaMapApiKey"]
    assert _section_keys(schema, "cruise", "korea_speed_bumps") == [
      "KoreaSpeedBumpEnabled", "KoreaSpeedBumpArchSpeed", "KoreaSpeedBumpTrapezoidSpeed"]
    assert _section_keys(schema, "cruise", "korea_route_map_data") == [
      "KoreaExternalNavEnabled", "KoreaRouteApiKey", "KoreaMapAutoDownload"]

  def test_the_speed_limit_sub_panel_keeps_only_speed_limit_settings(self, schema):
    section = _find_section(schema, "cruise", "speed_limits")
    keys = [item["key"] for sub_panel in section["sub_panels"] for item in sub_panel["items"]]
    assert keys == ["SpeedLimitMode", "SpeedLimitPolicy", "MapDataSource", "SpeedLimitOffsetType",
                    "SpeedLimitValueOffset"], keys


class TestKoreaApiKeyRemote(OpenpilotTestCase):
  def test_api_key_item_shape(self, schema):
    item = _find_item(schema, "KoreaMapApiKey")
    assert item is not None, "KoreaMapApiKey missing from settings_ui schema"
    assert item.get("widget") == "text"
    assert item.get("secret") is True
    assert item.get("max_length") == 255

  def test_api_key_requires_attestation(self, schema):
    """requires_attestation is the only gate on this write. KoreaMapApiKey is deliberately
    left out of SENSITIVE_PARAMS, so nothing device-side asks a second time."""
    item = _find_item(schema, "KoreaMapApiKey")
    assert item is not None
    assert item.get("requires_attestation") is True

  def test_api_keys_require_korea_map_source(self, schema):
    """Both device UIs gate the keys on MapDataSource == korea (speed_limit_settings.py, mici
    toggles.py). The korea page carried that gate on its section; on cruise each item carries
    its own, like every other Korea item."""
    for key in ("KoreaMapApiKey", "KoreaRouteApiKey"):
      item = _find_item(schema, key)
      assert item is not None, f"{key} missing from settings_ui schema"
      assert _references_param_equals(item.get("enablement"), "MapDataSource", 1), \
        f"{key} missing MapDataSource == korea (1) gate"


class TestKoreaCameraKindsRemote(OpenpilotTestCase):
  @parameterized.expand(list(CAMERA_KIND_PARAMS.values()), names=["key"])
  def test_kind_toggle_needs_only_the_korea_source(self, schema, key):
    """Not the longitudinal capability: a kind that is off also leaves the speed limit ahead
    sign, which a stock-ACC car shows too. Not offroad-only: it opens no port and touches no
    credential. No onroad cycle: korea_map_data rereads it every tick."""
    item = _find_item(schema, key)
    assert item is not None, f"{key} missing from settings_ui schema"
    assert item.get("widget") == "toggle"
    assert _references_param_equals(item.get("enablement"), "MapDataSource", 1)
    assert not _references_capability_field(item.get("enablement"), "has_longitudinal_control")
    assert "offroad_only" not in _flatten_rule_types(item.get("enablement"))
    assert not item.get("needs_onroad_cycle")

  def test_margin_matches_the_backend_range(self, schema):
    """korea_map_data clamps to CAMERA_MARGIN_RANGE; a wider remote range would offer values
    that silently snap back."""
    item = _find_item(schema, "KoreaCameraMargin")
    assert item is not None, "KoreaCameraMargin missing from settings_ui schema"
    assert item.get("widget") == "option"
    assert (item.get("min"), item.get("max")) == CAMERA_MARGIN_RANGE
    assert item.get("step") == 10
    assert item.get("unit") == "m"

  def test_margin_needs_a_capability_and_the_korea_source(self, schema):
    """It only moves the SmartCruiseControlMap slowdown, which cannot act without
    longitudinal control or ICBM -- the same gate as Speed Bump Slowdown."""
    item = _find_item(schema, "KoreaCameraMargin")
    assert item is not None
    assert _references_capability_field(item.get("enablement"), "has_longitudinal_control")
    assert _references_capability_field(item.get("enablement"), "has_icbm")
    assert _references_param_equals(item.get("enablement"), "MapDataSource", 1)
```

- [ ] **Step 2: 실패를 확인한다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.sunnylink.tests.test_settings_changes openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui'
```
Expected: `TestKoreaSectionsOnCruise`·`TestKoreaCameraKindsRemote`·`test_api_keys_require_korea_map_source`·`test_panels_present`·`test_pages_dir_well_formed`가 실패한다.

- [ ] **Step 3: `cruise.yaml`에서 한국 항목 다섯을 뺀다**

Speed Limit Settings 서브패널에서 `- key: KoreaExternalNavEnabled`(237행)부터 `KoreaSpeedBumpTrapezoidSpeed` 항목 끝(324행 `equals: 1`)까지 지운다. 남는 서브패널 항목은 `SpeedLimitMode`, `SpeedLimitPolicy`, `MapDataSource`, `SpeedLimitOffsetType`, `SpeedLimitValueOffset` 순이다.

- [ ] **Step 4: `- id: smart_cruise` 앞에 섹션 셋을 넣는다**

옮기는 항목은 규칙을 한 글자도 바꾸지 않고 들여쓰기만 2칸 줄인다. API 키 두 칸만 항목 단위 `MapDataSource == 1` 게이트를 새로 단다(korea 페이지에서는 섹션에 있었다).

```yaml
- id: korea_speed_cameras
  title: Korea Speed Cameras
  description: Which fixed cameras from the Korean public database slow the car, and how early
  items:
  - key: KoreaCameraSpeedEnabled
    widget: toggle
    title: Speed Cameras
    description: Slow down for fixed cameras that enforce speed only. Turning a type off also hides its cameras
      from the speed limit ahead sign.
    enablement:
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaCameraSignalEnabled
    widget: toggle
    title: Signal + Speed Cameras
    description: Slow down for intersection cameras that enforce both red lights and speed. Turning a type off
      also hides its cameras from the speed limit ahead sign.
    enablement:
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaCameraSectionEnabled
    widget: toggle
    title: Section Enforcement
    description: Slow down at the start and end cameras of average-speed sections. The speed between them is not
      held. Turning a type off also hides its cameras from the speed limit ahead sign.
    enablement:
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaCameraZoneEnabled
    widget: toggle
    title: Protected Zones
    description: Slow down for cameras in school and senior protection zones. A camera inside a zone follows this
      toggle whatever else it enforces. Turning a type off also hides its cameras from the speed limit ahead sign.
    enablement:
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaCameraMargin
    widget: option
    title: Camera Arrival Margin
    description: Reach the camera's limit about this far before the camera. Speed detectors often sit tens of
      metres ahead of the pole.
    min: 0
    max: 300
    step: 10
    unit: m
    enablement:
    - type: any
      conditions:
      - type: capability
        field: has_longitudinal_control
        equals: true
      - type: capability
        field: has_icbm
        equals: true
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaMapApiKey
    widget: text
    secret: true
    max_length: 255
    requires_attestation: true
    title: Speed Camera API Key
    description: data.go.kr key used to refresh the speed camera database over Wi-Fi. Without it the cameras
      shipped with the database are used as-is.
    enablement:
    - type: param
      key: MapDataSource
      equals: 1
- id: korea_speed_bumps
  title: Korea Speed Bumps
  description: Speed bumps from the Korean public database
  items:
  - key: KoreaSpeedBumpEnabled
    widget: toggle
    title: Speed Bump Slowdown
    description: Slow down for speed bumps from the Korean public database. Needs korea_bumps.sqlite on the
      device — without it this does nothing.
    enablement:
    - type: any
      conditions:
      - type: capability
        field: has_longitudinal_control
        equals: true
      - type: capability
        field: has_icbm
        equals: true
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaSpeedBumpArchSpeed
    widget: option
    title: Arch Bump Target Speed
    min: 20
    max: 40
    step: 1
    unit:
      metric: km/h
      imperial: mph
    visibility:
    - type: param
      key: KoreaSpeedBumpEnabled
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
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaSpeedBumpTrapezoidSpeed
    widget: option
    title: Flat-Top Bump Target Speed
    min: 20
    max: 50
    step: 1
    unit:
      metric: km/h
      imperial: mph
    visibility:
    - type: param
      key: KoreaSpeedBumpEnabled
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
    - type: param
      key: MapDataSource
      equals: 1
- id: korea_route_map_data
  title: 'Korea Route & Map Data'
  description: Routes from a companion navigation app, and map database updates
  items:
  - key: KoreaExternalNavEnabled
    widget: toggle
    title: External Navigation Input
    description: Accept speed limit and speed camera data from a companion navigation app on the local network. Leave
      this off unless you are running one — the built-in offline map database works without it.
    enablement:
    - $ref: '#/macros/offroad'
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaRouteApiKey
    widget: text
    secret: true
    max_length: 255
    requires_attestation: true
    title: Route API Key
    description: TMAP appKey used to fetch the route to a destination sent by a companion navigation app. Without
      it, speed cameras and bumps are matched by heading alone, which can pick the wrong road at a fork.
    enablement:
    - type: param
      key: MapDataSource
      equals: 1
  - key: KoreaMapAutoDownload
    widget: toggle
    title: Download Map Data Automatically
    description: Fetch the Korean link and speed bump databases over Wi-Fi when a newer release
      is published. The link database is about 220 MB, so this only runs on an unmetered
      connection. Leave it off to copy the files by hand.
    enablement:
    - $ref: '#/macros/offroad'
    - type: param
      key: MapDataSource
      equals: 1
```

- [ ] **Step 5: `korea.yaml`을 지우고 다시 컴파일한다**

Run:
```bash
cd E:/dev/sunnypilot
git rm openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml
PYTHONUTF8=1 python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py
PYTHONUTF8=1 python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py --check
git diff --stat openpilot/sunnypilot/sunnylink/settings_ui.json
```
Expected: `--check`가 `matches compiled output`을 출력한다. `git diff --stat`은 수백 줄 규모다(파일 전체 2,615줄이 통째로 바뀌면 줄바꿈 문제이니 멈추고 확인한다).

- [ ] **Step 6: 컨테이너에서 써니링크 테스트를 돌린다**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build rm -f /work/openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest openpilot.sunnypilot.sunnylink.tests.test_settings_changes openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui openpilot.sunnypilot.sunnylink.tests.test_settings_schema'
```
Expected: `OK` — baseline에서 실패하던 `test_api_key_stays_out_of_the_cruise_panel`은 사라졌다. `test_all_schema_keys_exist_in_params`가 새 키를 모른다고 실패하면 Task 4의 scons 재빌드를 다시 한다(코드 결함이 아니다).

- [ ] **Step 7: 커밋한다**

```bash
git add openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml openpilot/sunnypilot/sunnylink/settings_ui.json \
  openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py openpilot/sunnypilot/sunnylink/tests/test_compile_settings_ui.py
git commit -F - <<'EOF'
feat: regroup the Korea settings into three Cruise sections for sunnylink

The sunnylink app hardcodes its settings sidebar, so the korea page was
never reachable. Its two API keys and the Korea items from the speed
limit sub-panel now sit in Cruise under Korea Speed Cameras, Korea Speed
Bumps and Korea Route & Map Data, with the new camera kind toggles and
arrival margin. Moved items keep their rules; the keys gain an item-level
Korea gate.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Rt95wLFnssovQ8wjCDTBRY
EOF
```

`git rm`으로 지운 `korea.yaml`은 이미 스테이징돼 있어 이 커밋에 들어간다.

---

### Task 9: 전체 검증

**Files:** 없음(검증만). 문제가 나오면 해당 태스크로 돌아가 고치고 그 태스크의 커밋 규칙대로 새 커밋을 만든다.

- [ ] **Step 1: 호스트 korea 테스트 전체**

Run:
```bash
cd E:/dev/sunnypilot
python -m unittest openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh openpilot.sunnypilot.mapd.korea.tests.test_map_download openpilot.sunnypilot.mapd.korea.tests.test_route openpilot.sunnypilot.mapd.korea.tests.test_external_source openpilot.sunnypilot.mapd.korea.tests.test_geo
```
Expected: 알려진 `PermissionError` 1건 외 통과.

- [ ] **Step 2: 컨테이너 전체 (리눅스)**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
docker exec sp-build bash -lc 'cd /work && export PATH="/work/.venv/bin:$PATH" && ./.venv/bin/scons -j8 openpilot/common'
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/mapd sp-build:/work/openpilot/sunnypilot/
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/selfdrive/controls/lib/speed_limit sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/
MSYS_NO_PATHCONV=1 docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
MSYS_NO_PATHCONV=1 docker cp openpilot/selfdrive/ui sp-build:/work/openpilot/selfdrive/
docker exec sp-build rm -f /work/openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/python -m unittest \
  openpilot.sunnypilot.mapd.korea.tests.test_build_db openpilot.sunnypilot.mapd.korea.tests.test_db \
  openpilot.sunnypilot.mapd.korea.tests.test_deploy openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh \
  openpilot.sunnypilot.mapd.korea.tests.test_map_download openpilot.sunnypilot.mapd.korea.tests.test_route \
  openpilot.sunnypilot.mapd.korea.tests.test_external_source openpilot.sunnypilot.mapd.korea.tests.test_geo \
  openpilot.sunnypilot.mapd.tests.test_korea_map_data openpilot.sunnypilot.mapd.tests.test_mapd_source \
  openpilot.sunnypilot.mapd.tests.test_mapd_process \
  openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.tests.test_map_controller \
  openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.tests.test_vision_controller \
  openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_resolver \
  openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_assist \
  openpilot.sunnypilot.sunnylink.tests.test_settings_changes openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui \
  openpilot.sunnypilot.sunnylink.tests.test_settings_schema'
docker exec sp-build bash -lc 'cd /work && xvfb-run -a ./.venv/bin/python -m unittest openpilot.selfdrive.ui.tests.test_speed_limit_settings_korea_gating openpilot.selfdrive.ui.mici.tests.test_toggles_api_key'
```
Expected: 둘 다 `OK`. 첫째의 skip은 `TestLoadLinks` 3개뿐이고, 둘째는 skip 없이 `Ran 8 tests`.

- [ ] **Step 3: 실데이터로 종류별 개수를 확인한다 (키 불필요)**

Run:
```bash
cd E:/dev/sunnypilot && python - <<'PY'
import collections, json
from openpilot.sunnypilot.mapd.korea.build_db import load_cameras_api
items = json.load(open("E:/dev/korea_map_data/cameras_raw.json", encoding="utf-8"))
print(sorted(collections.Counter(row[4] for row in load_cameras_api(items)).items()))
PY
```
Expected: `[(0, 7287), (1, 10659), (2, 1088), (3, 14381)]` — 과속·신호·구간·보호구역. 스펙 1절의 표와 같아야 한다.

- [ ] **Step 4: 린트**

Run:
```bash
MSYS_NO_PATHCONV=1 docker cp pyproject.toml sp-build:/work/pyproject.toml
docker exec sp-build bash -lc 'cd /work && ./.venv/bin/ruff check openpilot/sunnypilot/mapd openpilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control openpilot/sunnypilot/selfdrive/controls/lib/speed_limit openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise_sub_layouts/speed_limit_settings.py openpilot/selfdrive/ui/mici/layouts/settings/toggles.py openpilot/sunnypilot/sunnylink/tests openpilot/selfdrive/ui/tests/test_speed_limit_settings_korea_gating.py openpilot/selfdrive/ui/mici/tests/test_toggles_api_key.py'
```
Expected: `All checks passed!`

- [ ] **Step 5: API 키가 저장소에 없는지 확인한다**

Run: `cd E:/dev/sunnypilot && bash .superpowers/sdd/scan-api-key.sh`
Expected: 출력되는 개수가 전부 `0`이고 마지막 줄이 `SCAN COMPLETE (all counts must be 0)`. 스크립트는 키 값을 출력하지 않는다.

- [ ] **Step 6: 작업 트리를 확인한다**

Run: `cd E:/dev/sunnypilot && git status --short && git log --oneline -9`
Expected: 이 계획 전부터 있던 untracked 파일(`dev/`, `.codegraph/`, 다른 계획서 등) 외에 변경 없음. 커밋 8개(Task 1-8).

---

## 실차 확인 (사용자)

디바이스에 올린 뒤 확인한다. 카메라 DB는 `kind` 컬럼이 없으므로 첫 비종량제 연결에서 `camera_refresh`가 새로 만든다(디바이스의 `KoreaMapApiKey` 사용). 키가 없으면 PC에서 **data.go.kr 원본 CSV**로 `python -m openpilot.sunnypilot.mapd.korea.build_db --cameras <원본.csv> --out-cameras korea_cameras.sqlite`를 돌려 복사한다 — `E:/dev/korea_map_data/`에 있는 4컬럼 CSV는 이제 `KeyError`로 거부된다.

1. 아는 카메라 앞에서 HUD 앞쪽 표지가 뜨고 거리가 실제 거리로 줄어드는지.
2. `SCC-M` 배지가 뜨고, 카메라 약 50 m 앞에서 제한속도에 도달하는지.
3. 80 km/h 미만 카메라에서 SLA 확인 팝업이 뜨지 않는지.
4. 보호구역 토글을 끄면 학교 앞 카메라에서 표지와 감속이 둘 다 사라지는지.
5. 써니링크 Cruise 페이지에 Korea Speed Cameras / Korea Speed Bumps / Korea Route & Map Data 섹션이 보이는지(API 키 두 칸은 프론트엔드가 `text` 위젯을 몰라 빈 행으로 보인다 — 예상된 동작).
