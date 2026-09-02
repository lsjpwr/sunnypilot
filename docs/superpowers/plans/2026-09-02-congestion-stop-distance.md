# 정체 구간 정차 간격 조절 (Congestion Stop Distance) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 앞차가 느릴 때(정체·신호대기)만 MPC의 정차 목표 간격을 6.0 m에서 사용자 지정값(4.0~6.0 m)으로 줄여, 주변 차량의 끼어들기를 막는다.

**Architecture:** `STOP_DISTANCE` 상수를 바꾸는 대신 MPC에 들어가는 `x_obstacle`(`params[:,2]`)을 런타임에 오프셋한다. MPC 비용 함수가 x에 대해 평행이동 불변(`X_EGO_COST = 0.`)이라 두 방식은 수학적으로 등가이고 acados 재빌드가 필요 없다. 정체 판정·파라미터 읽기·슬루는 sunnypilot 측 신규 컨트롤러가 담당하고, 업스트림 diff는 2줄로 제한한다.

**Tech Stack:** Python 3.11 (디바이스), stdlib + numpy(테스트 전용), cereal messaging, acados(기존 빌드), stdlib `unittest` via `tools/test_runner.py`. 신규 런타임 의존성 없음.

**설계 문서:** `docs/superpowers/specs/2026-09-02-congestion-stop-distance-design.md` (commit `4ca7a26fdc`)

## Global Constraints

- 파일 헤더: 신규 sunnypilot 파일은 기존 파일과 동일한 MIT 라이선스 헤더로 시작한다 (`Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.` 블록).
- 들여쓰기: Python 2칸, C++ 4칸 (레포 기존 규약).
- 코드·주석·커밋 메시지는 영어. 이 계획서와 설계서만 한국어.
- 테스트 프레임워크는 stdlib `unittest`. pytest는 레포 의존성이 아니다. 실행은 `python tools/test_runner.py <경로>`.
- 파라미터 키 이름: `StopDistance`. 타입 `FLOAT`, 기본값 `"6.0"`, 플래그 `PERSISTENT | BACKUP`.
- 값 범위: `STOP_DISTANCE_MIN = 4.0`, `STOP_DISTANCE_MAX = 6.0`, UI 스텝 0.5 m. 하한 4.0은 안전 요구사항이며 낮추지 않는다.
- 정체 게이트 임계: `CONGESTION_LEAD_V_ENTER = 8.33` m/s, `CONGESTION_LEAD_V_EXIT = 13.89` m/s. 자차 속도가 아니라 `radarState.leadOne.vLead` 기준.
- 슬루: `OFFSET_SLEW = 0.5` m/s (증가 방향만 제한), `STANDSTILL_V = 0.3` m/s (정차 중 증가 동결).
- 기본값 6.0에서는 모든 코드 경로가 변경 전과 비트 단위로 동일해야 한다.
- `settings_ui.json`은 생성물이다. 절대 손으로 고치지 않고 `compile_settings_ui.py`로 재생성한다.

## 실행 환경 (모든 태스크 공통)

호스트는 Windows이고 acados/`.venv`가 없다. 검증은 이미 빌드된 Docker 컨테이너 `sp-build`에서 한다. 컨테이너의 `/work`는 이 레포의 복사본이며, 이 계획이 건드리는 6개 기존 파일은 호스트 `HEAD`와 md5가 일치함을 확인했다.

각 태스크에서 파일을 수정한 뒤 다음 순서로 검증한다.

```bash
# 1. 수정/신규 파일을 컨테이너로 복사 (예시)
docker cp openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py

# 2. C++ 헤더를 건드린 태스크만: params 라이브러리 재빌드 (증분 약 3초)
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && scons -j$(nproc)'

# 3. 테스트 실행
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/'
```

호스트 워킹트리는 LF다 (`core.autocrlf=false`, `.gitattributes`에 `* text=auto`). `docker cp`는 양방향 모두 줄바꿈을 바꾸지 않는다.

이 계획의 Task 1~3과 Task 5의 스키마 경로는 컨테이너에서 한 번 통째로 적용해 실행한 뒤 원상 복구했다. 아래 각 Step의 기대 결과(테스트 건수 포함)는 추정이 아니라 실측값이다.

- Task 1~3 전체 적용: `21 passed`
- 종방향 회귀 (`openpilot/sunnypilot/selfdrive/controls/lib/` + `test_following_distance.py`): `210 passed`
- Task 5 스키마 경로 (`params_keys.h` + `cruise.yaml` + 재컴파일): `92 passed, 1 skipped`

Task 4(디바이스 UI)만 실측하지 않았다. raylib 위젯 구성은 `openpilot/selfdrive/ui/tests/`가 커버한다.

## File Structure

| 파일 | 책임 | 태스크 |
|---|---|---|
| `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` | 오프셋 훅 2줄. `self.stop_distance` 속성 하나만 노출하고 그 이상은 모른다 | 1 |
| `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py` | 훅 + 컨트롤러 + 배선 전체 테스트 | 1·2·3 |
| `openpilot/common/params_keys.h` | `StopDistance` 키 등록 | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py` | 파라미터 읽기, 정체 판정, 슬루. MPC를 아는 유일한 sunnypilot 모듈 | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` | 컨트롤러 소유·매 틱 호출 (3줄) | 3 |
| `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py` | 디바이스 UI 항목 | 4 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml` | 원격 UI 항목 (소스) | 5 |
| `openpilot/sunnypilot/sunnylink/settings_ui.json` | 원격 UI 항목 (생성물) | 5 |
| `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py` | 원격 쓰기를 `SENSITIVE_PARAMS`로 게이팅 | 5 |
| `openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py` | 원격 차단 회귀 테스트 | 5 |

---

### Task 1: MPC 오프셋 훅

MPC에 `stop_distance` 속성을 추가하고 `params[:,2]` 대입 시점에 오프셋을 적용한다. 기본값이 `STOP_DISTANCE`이므로 이 태스크만으로는 동작이 전혀 바뀌지 않는다. 그 사실 자체가 테스트 대상이다.

**Files:**
- Modify: `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py:215-219` (`__init__`), `:332` (`update`)
- Create: `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces: `LongitudinalMpc.stop_distance: float` — 쓰기 가능한 인스턴스 속성, 기본값 `STOP_DISTANCE`(6.0), 단위는 미터. Task 2의 `StopDistanceController`가 매 틱 여기에 쓴다. MPC는 `STOP_DISTANCE - self.stop_distance`를 `params[:,2]`에 더한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py`를 새로 만든다.

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.common.parameterized import parameterized
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, STOP_DISTANCE


def radar_state(d_rel: float, v_lead: float, present: bool = True):
  msg = messaging.new_message('radarState')
  lead = msg.radarState.leadOne
  lead.present = present
  lead.dRel = d_rel
  lead.vLead = v_lead
  return msg.radarState.as_reader()


def solve(mpc: LongitudinalMpc, v_ego: float, d_rel: float, v_lead: float) -> np.ndarray:
  """Run one MPC tick and return a copy of the obstacle positions it was handed."""
  mpc.set_weights()
  mpc.set_cur_state(v_ego, 0.)
  mpc.update(radar_state(d_rel, v_lead))
  return np.array(mpc.params[:, 2])


class TestMpcStopDistanceOffset(OpenpilotTestCase):
  """The two-line hook in long_mpc.py. The obstacle must move by exactly
  STOP_DISTANCE - mpc.stop_distance, and not at all at the default."""

  def test_a_fresh_mpc_defaults_to_the_stock_stop_distance(self):
    # Every path that builds an MPC without the sunnypilot controller -- replay,
    # test_longitudinal.py, the maneuver harness -- has to keep stock behavior.
    self.assertEqual(LongitudinalMpc().stop_distance, STOP_DISTANCE)

  def test_the_default_leaves_the_obstacle_exactly_where_it_was(self):
    # A stopped lead contributes no stopping-equivalence term, so the obstacle handed to
    # the solver is dRel itself at every horizon index. An accidental constant offset
    # shows up here as a whole-array shift.
    obstacle = solve(LongitudinalMpc(), v_ego=0., d_rel=20., v_lead=0.)
    np.testing.assert_array_equal(obstacle, np.full_like(obstacle, 20.))

  # Bypasses the congestion gate on purpose: this pins the offset arithmetic alone.
  # The gate itself is covered by TestCongestionGate.
  @parameterized.expand([0., 8.33, 16.67, 27.78])
  def test_shortening_the_stop_distance_pushes_the_obstacle_out_by_the_difference(self, v):
    mpc = LongitudinalMpc()
    baseline = solve(mpc, v_ego=v, d_rel=40., v_lead=v)

    mpc.stop_distance = 4.0
    shifted = solve(mpc, v_ego=v, d_rel=40., v_lead=v)

    np.testing.assert_allclose(shifted - baseline, 2.0, atol=1e-9)
```

- [ ] **Step 2: 테스트를 컨테이너로 복사하고 실패 확인**

```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py'
```

Expected: FAIL. `test_a_fresh_mpc_defaults_to_the_stock_stop_distance`는 `AttributeError: 'LongitudinalMpc' object has no attribute 'stop_distance'`, `test_shortening_...` 4건은 차이가 2.0이 아니라 0.0이라 assert 실패. `test_the_default_leaves_...`는 이미 통과한다 (회귀 고정선이므로 정상).

- [ ] **Step 3: `__init__`에 속성 추가**

`long_mpc.py:215-219`의 `__init__` 마지막 줄로 추가한다.

```python
class LongitudinalMpc:
  def __init__(self, dt=DT_MDL):
    self.dt = dt
    self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    self.reset()
    self.source = LongitudinalPlanSource.cruise
    self.stop_distance = STOP_DISTANCE
```

- [ ] **Step 4: `update()`에 오프셋 적용**

`long_mpc.py`의 `self.params[:,2] = np.min(x_obstacles, axis=1)` 한 줄(Step 3 이후 333행)을 바꾼다. 주변 줄은 손대지 않는다.

```python
    self.params[:,0] = ACCEL_MIN
    self.params[:,1] = ACCEL_MAX
    self.params[:,2] = np.min(x_obstacles, axis=1) + (STOP_DISTANCE - self.stop_distance)
    self.params[:,3] = np.copy(self.a_prev)
    self.params[:,4] = t_follow
    self.params[:,5] = LEAD_DANGER_FACTOR
```

오프셋을 `x_obstacles` 생성 시점이 아니라 여기에 넣는 이유: 위쪽 `self.source = MPC_SOURCES[np.argmin(x_obstacles[0])]`의 입력을 건드리지 않기 위해서다. 두 컬럼에 같은 상수를 더하므로 argmin 결과는 실제로 동일하지만, 값과 판정을 분리해 두면 회귀 시 원인을 좁히기 쉽다.

- [ ] **Step 5: 테스트 통과 확인**

```bash
docker cp openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py \
  sp-build:/work/openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py'
```

Expected: PASS, `6 passed`.

- [ ] **Step 6: 커밋**

```bash
git add openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py \
        openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
git commit -m "feat: let the longitudinal MPC hold a shorter stop distance

The gap the car keeps from a stopped lead is STOP_DISTANCE alone: the
Driving Personality term is t_follow * v_ego, which is zero at a
standstill. STOP_DISTANCE is baked into the acados cost, so changing it
means regenerating the solver.

Offsetting x_obstacle at runtime is the equivalent that does not.
X_EGO_COST is zero, so every x-dependent cost and constraint term is a
difference (x_obstacle - x_ego), the dynamics are translation-invariant
in x, and x0 is reset to the origin every solve. A constant offset
therefore translates the solution rigidly: same deceleration, same jerk,
same curve shape.

Defaults to STOP_DISTANCE, so replay, the maneuver harness, and every
other path that builds an MPC without a controller are bit-identical."
```

---

### Task 2: 정체 컨트롤러

파라미터를 읽고, 앞차 속도로 정체를 판정하고, 오프셋을 슬루해서 MPC에 쓰는 컨트롤러. 이 태스크가 기능의 전부이며 아직 아무도 호출하지 않는다.

**Files:**
- Modify: `openpilot/common/params_keys.h:284-287` (Smart Cruise Control 블록 뒤)
- Create: `openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py`
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py` (import 교체 + 클래스 추가)

**Interfaces:**
- Consumes: `LongitudinalMpc.stop_distance` (Task 1)
- Produces:
  - `StopDistanceController(mpc, params=None)` — `mpc`는 `stop_distance` 속성을 가진 객체, `params`는 `Params()` 대체용(테스트 주입). 생성자에서 파라미터를 한 번 읽는다.
  - `StopDistanceController.update(sm) -> None` — `sm`은 `sm['radarState'].leadOne`(`.present`, `.vLead`)과 `sm['carState'].vEgo`를 지원하는 매핑. 매 호출마다 `mpc.stop_distance`를 갱신한다.
  - 모듈 상수 `STOP_DISTANCE_MIN = 4.0`, `STOP_DISTANCE_MAX = 6.0`, `CONGESTION_LEAD_V_ENTER = 8.33`, `CONGESTION_LEAD_V_EXIT = 13.89`, `OFFSET_SLEW = 0.5`, `STANDSTILL_V = 0.3`.

- [ ] **Step 1: 실패하는 테스트 작성**

`test_stop_distance.py`의 import 블록을 아래로 교체하고, 파일 끝에 헬퍼와 네 클래스를 덧붙인다. Task 1이 만든 `radar_state`/`solve`/`TestMpcStopDistanceOffset`은 그대로 둔다.

import 블록 (기존 import 문 전체를 이것으로 교체):

```python
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.common.parameterized import parameterized
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import (LEAD_DANGER_FACTOR, LongitudinalMpc,
                                                                            STOP_DISTANCE)
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import (CONGESTION_LEAD_V_ENTER, CONGESTION_LEAD_V_EXIT,
                                                                       OFFSET_SLEW, STANDSTILL_V, STOP_DISTANCE_MAX,
                                                                       STOP_DISTANCE_MIN, StopDistanceController)
```

파일 끝에 추가:

```python
class FakeMpc:
  """Stands in for LongitudinalMpc: the controller only ever writes stop_distance."""

  def __init__(self):
    self.stop_distance = STOP_DISTANCE


class FakeParams:
  """Params stand-in. Counts reads so the 1 Hz throttle is observable."""

  def __init__(self, value: float = STOP_DISTANCE):
    self.value = value
    self.gets = 0
    self.puts: list[float] = []

  def get(self, key, return_default=False):
    assert key == "StopDistance", f"unexpected param read: {key}"
    self.gets += 1
    return self.value

  def put(self, key, value, block=False):
    assert key == "StopDistance", f"unexpected param write: {key}"
    self.value = value
    self.puts.append(value)


def sm(v_ego: float = 20., v_lead: float = 0., lead_present: bool = True) -> dict:
  """The two services the controller reads, as real cereal readers."""
  radar = messaging.new_message('radarState')
  radar.radarState.leadOne.present = lead_present
  radar.radarState.leadOne.vLead = v_lead

  car = messaging.new_message('carState')
  car.carState.vEgo = v_ego

  return {'radarState': radar.radarState.as_reader(), 'carState': car.carState.as_reader()}


def drive(controller: StopDistanceController, ticks: int, **kwargs) -> None:
  message = sm(**kwargs)
  for _ in range(ticks):
    controller.update(message)


def build_controller(value: float = 4.0):
  mpc, params = FakeMpc(), FakeParams(value)
  return StopDistanceController(mpc, params), mpc, params


class TestParamReading(OpenpilotTestCase):
  def test_an_out_of_range_value_is_clamped_and_written_back(self):
    # Both directions: a hand-edited param file or a stale remote write must not reach
    # the MPC, and the stored value is corrected so the UI agrees with what is applied.
    for stored, expected in ((0., STOP_DISTANCE_MIN), (99., STOP_DISTANCE_MAX)):
      with self.subTest(stored=stored):
        _, _, params = build_controller(stored)
        self.assertEqual(params.puts, [expected])
        self.assertEqual(params.value, expected)

  def test_an_in_range_value_is_not_written_back(self):
    _, _, params = build_controller(4.5)
    self.assertEqual(params.puts, [])

  def test_the_param_is_reread_at_1_hz_not_every_tick(self):
    # A read every tick would be 20 param-store reads per second, which is what the
    # throttle in DEC exists to avoid.
    period = int(1. / DT_MDL)
    ctrl, _, params = build_controller()
    self.assertEqual(params.gets, 1)         # the constructor's own read

    drive(ctrl, period)                      # frames 0..19 -- only frame 0 reads
    self.assertEqual(params.gets, 2)

    drive(ctrl, 1)                           # frame 20
    self.assertEqual(params.gets, 3)

  def test_a_value_changed_at_runtime_reaches_the_mpc(self):
    ctrl, mpc, params = build_controller(STOP_DISTANCE)
    drive(ctrl, 200)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

    params.value = STOP_DISTANCE_MIN
    drive(ctrl, 200)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)


class TestCongestionGate(OpenpilotTestCase):
  def test_a_fast_lead_keeps_the_stock_gap(self):
    # 60 km/h lead: plain highway following, which Driving Personality owns.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=16.67)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_a_slow_lead_opens_the_gate(self):
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=2.78)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

  def test_no_lead_keeps_the_stock_gap(self):
    # process_lead() fakes an obstacle 50 m out when the lead is gone, so there is
    # nothing real for the offset to act on.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=0., lead_present=False)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_the_gate_has_hysteresis_so_stop_and_go_does_not_chatter(self):
    # Entering costs 30 km/h, leaving costs 50. Without the gap the offset would flip on
    # every surge of a congested queue, and the solution is only a rigid translation
    # while the offset holds still.
    ctrl, mpc, _ = build_controller()

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_EXIT - 1.)      # 40 km/h, never entered
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_ENTER - 1.)     # 26 km/h, enters
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_EXIT - 1.)      # 40 km/h, still held
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_EXIT + 1.)      # 54 km/h, releases
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)


class TestSlew(OpenpilotTestCase):
  def test_the_offset_grows_at_the_slew_rate(self):
    # A slow car cutting in opens the gate at the exact moment braking is needed. A step
    # change would push the obstacle away and weaken that braking.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 1, v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE - OFFSET_SLEW * DT_MDL)

  def test_the_offset_shrinks_in_a_single_tick(self):
    # Shrinking asks for more brake, which is the safe direction and needs no limit.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 1, v_lead=CONGESTION_LEAD_V_EXIT + 1.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_the_offset_is_frozen_at_a_standstill(self):
    # A growing offset reads to the MPC as the lead pulling away. should_stop() would
    # release and the car would creep forward after it had already stopped.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_ego=STANDSTILL_V - 0.1, v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_an_offset_reached_before_stopping_survives_the_stop(self):
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_ego=5., v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 200, v_ego=0., v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)


class TestDangerZoneFloor(OpenpilotTestCase):
  def test_the_hard_floor_at_the_minimum_setting_stays_above_2_5_m(self):
    # The MPC's danger-zone constraint is (x_obstacle - x_ego) >= LEAD_DANGER_FACTOR *
    # desired_dist_comfort, and desired_dist_comfort is STOP_DISTANCE at a standstill.
    # In shifted coordinates that is 4.5 m; in real ones it is 4.5 minus the offset.
    # A tripwire: it fails if upstream retunes LEAD_DANGER_FACTOR or STOP_DISTANCE
    # without anyone revisiting STOP_DISTANCE_MIN.
    offset = STOP_DISTANCE - STOP_DISTANCE_MIN
    floor = LEAD_DANGER_FACTOR * STOP_DISTANCE - offset
    self.assertGreaterEqual(floor, 2.5)
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py'
```

Expected: FAIL, 수집 단계에서 `ModuleNotFoundError: No module named 'openpilot.sunnypilot.selfdrive.controls.lib.stop_distance'`.

- [ ] **Step 3: 파라미터 키 등록**

`params_keys.h`의 Smart Cruise Control 블록과 Torque 블록 사이에 새 블록을 넣는다.

```cpp
    // Smart Cruise Control
    {"MapTargetVelocities", {CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"SmartCruiseControlMap", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"SmartCruiseControlVision", {PERSISTENT | BACKUP, BOOL, "0"}},

    // Stop Distance
    {"StopDistance", {PERSISTENT | BACKUP, FLOAT, "6.0"}},

    // Torque lateral control custom params
```

`FLOAT`은 이미 `TorqueParamsOverrideFriction`, `TorqueParamsOverrideLatAccelFactor`가 쓰고 있다. 정수 데시미터 트릭이 필요 없다.

- [ ] **Step 4: 컨트롤러 구현**

`openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py`를 새로 만든다.

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import STOP_DISTANCE

# Range offered by the UI. 4.0 m is the floor: the MPC's danger-zone constraint sits at
# LEAD_DANGER_FACTOR * 6.0 = 4.5 m in shifted coordinates, so a 4.0 m target still leaves a
# 2.5 m hard floor in real ones. Below that there is nothing left for a rear-end shove to spend.
STOP_DISTANCE_MIN = 4.0
STOP_DISTANCE_MAX = 6.0

# Congestion is read off the lead, not off ego speed. A lead stopped at a light is known
# tens of metres out, so the offset is fully applied before braking even starts. Gating on
# ego speed instead only opens once deceleration is nearly over, and the remainder fills in
# after the car has stopped -- which creeps it forward. The gap between the two thresholds
# keeps the offset constant across a whole stop-and-go episode; the solution is only a rigid
# translation of the stock one while the offset holds still.
CONGESTION_LEAD_V_ENTER = 8.33   # m/s (30 km/h)
CONGESTION_LEAD_V_EXIT = 13.89   # m/s (50 km/h)

OFFSET_SLEW = 0.5                # m/s
STANDSTILL_V = 0.3               # m/s, matches should_stop() in drive_helpers


class StopDistanceController:
  """Shortens the gap the MPC holds from a slow lead, in metres."""

  def __init__(self, mpc, params=None):
    self._mpc = mpc
    self._params = params or Params()
    self._frame = 0
    self._congested = False
    self._offset = 0.
    self._applied = 0.
    self._read_params()

  def _read_params(self) -> None:
    if self._frame % int(1. / DT_MDL) != 0:
      return

    # Clamp and write back, the same shape as get_sanitize_int_param. Only one caller
    # needs the float version, so it stays here until a second one shows up.
    value = float(self._params.get("StopDistance", return_default=True))
    clipped = max(STOP_DISTANCE_MIN, min(STOP_DISTANCE_MAX, value))
    if clipped != value:
      self._params.put("StopDistance", clipped, block=True)

    self._offset = STOP_DISTANCE - clipped

  def update(self, sm) -> None:
    self._read_params()

    # No lead means no obstacle to sit behind: process_lead() fakes one 50 m out, so the
    # offset would have nothing to act on.
    lead = sm['radarState'].leadOne
    threshold = CONGESTION_LEAD_V_EXIT if self._congested else CONGESTION_LEAD_V_ENTER
    self._congested = bool(lead.present) and lead.vLead < threshold

    target = self._offset if self._congested else 0.

    # Only the direction that shortens the gap is rate-limited, and it is frozen at a
    # standstill: a growing offset reads as the lead pulling away, which would creep the
    # car forward after it had already stopped. Shrinking the offset asks for more brake,
    # so it applies at once.
    if target > self._applied:
      if sm['carState'].vEgo > STANDSTILL_V:
        self._applied = min(target, self._applied + OFFSET_SLEW * DT_MDL)
    else:
      self._applied = target

    self._mpc.stop_distance = STOP_DISTANCE - self._applied
    self._frame += 1
```

- [ ] **Step 5: 재빌드 후 테스트 통과 확인**

`params_keys.h`는 `params.cc`로 컴파일되므로 `Params()`가 새 키를 알려면 재빌드가 필요하다 (증분 약 3초).

```bash
docker cp openpilot/common/params_keys.h sp-build:/work/openpilot/common/params_keys.h
docker cp openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py
docker cp openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && scons -j$(nproc) 2>&1 | tail -2'
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py'
```

Expected: `scons: done building targets.` 그리고 PASS, `19 passed`.

- [ ] **Step 6: 커밋**

```bash
git add openpilot/common/params_keys.h \
        openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py \
        openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
git commit -m "feat: add a congestion-gated stop distance controller

Reads StopDistance at 1 Hz, decides whether traffic is congested, and
writes the resulting target to the MPC.

The gate reads the lead's speed, not ego speed. Approaching a car stopped
at a light from 60 km/h, an ego-speed gate opens with 3.4 s of braking
left; at the slew rate only 83% of the offset applies before the car
stops and the rest fills in afterwards, which releases should_stop() and
creeps the car forward a third of a metre. The lead is known to be
stopped from tens of metres out, so a lead-speed gate is already fully
applied when braking starts.

Entering costs 30 km/h and leaving costs 50, so the offset stays constant
across a whole stop-and-go episode. That matters beyond chatter: the
solution is only a rigid translation while the offset is constant. A
speed taper would shrink it during launch, which reads as the lead
closing in and costs about 14% of the room over the acceleration -- the
opposite of the point.

Growth is rate-limited and frozen at a standstill; shrinking is
immediate. A slow car cutting in opens the gate at the moment braking is
needed, and pushing the obstacle away then is the wrong direction."
```

---

### Task 3: 플래너 배선

컨트롤러를 sunnypilot 플래너가 소유하고 매 틱 호출하게 한다. 업스트림 `LongitudinalPlanner.update()`는 첫 줄에서 `LongitudinalPlannerSP.update(self, sm)`를 호출하고 `self.mpc.update(...)`는 그 뒤이므로, 같은 틱의 풀이에 반영된다.

**Files:**
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` (import 1줄, `__init__` 1줄, `update` 1줄)
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py` (import 4줄 + 클래스 추가)

**Interfaces:**
- Consumes: `StopDistanceController(mpc, params=None)`, `.update(sm)` (Task 2)
- Produces: `LongitudinalPlannerSP.stop_distance: StopDistanceController`. Task 4·5는 이 속성을 쓰지 않는다. 배선은 여기서 끝난다.

- [ ] **Step 1: 실패하는 테스트 작성**

`test_stop_distance.py`의 import 블록을 아래로 교체한다. Task 2 블록에서 네 줄(`custom`, `structs`, `Params`, `LongitudinalPlanner`)이 늘어난 것 외에 변경 없다.

```python
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.cereal import custom
from opendbc.car import structs
from openpilot.common.params import Params
from openpilot.common.parameterized import parameterized
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import (LEAD_DANGER_FACTOR, LongitudinalMpc,
                                                                            STOP_DISTANCE)
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import (CONGESTION_LEAD_V_ENTER, CONGESTION_LEAD_V_EXIT,
                                                                       OFFSET_SLEW, STANDSTILL_V, STOP_DISTANCE_MAX,
                                                                       STOP_DISTANCE_MIN, StopDistanceController)
```

파일 끝에 추가:

```python
class MockSubMaster(dict):
  def __init__(self, services: dict):
    super().__init__(services)
    self.valid = dict.fromkeys(services, True)
    self.logMonoTime = dict.fromkeys(services, 0)
    self.updated = dict.fromkeys(services, True)
    self.recv_frame = dict.fromkeys(services, 1)

  def all_checks(self, service_list=None) -> bool:
    return True


def planner_sm(v_ego: float, v_lead: float) -> MockSubMaster:
  services = {}
  for service in ("controlsState", "vehicleParameters", "carStateSP",
                  "liveMapDataSP", "gpsLocationExternal", "gpsLocation"):
    services[service] = getattr(messaging.new_message(service), service)

  radar = messaging.new_message('radarState')
  radar.radarState.leadOne.present = True
  radar.radarState.leadOne.dRel = 20.
  radar.radarState.leadOne.vLead = v_lead
  services['radarState'] = radar.radarState.as_reader()

  car_state = messaging.new_message('carState')
  car_state.carState.vEgo = v_ego
  car_state.carState.vCruise = 100.
  car_state.carState.vCruiseCluster = 100.
  services['carState'] = car_state.carState.as_reader()

  selfdrive_state = messaging.new_message('selfdriveState')
  selfdrive_state.selfdriveState.enabled = True
  services['selfdriveState'] = selfdrive_state.selfdriveState.as_reader()

  car_control = messaging.new_message('carControl')
  car_control.carControl.enabled = True
  services['carControl'] = car_control.carControl.as_reader()

  model = messaging.new_message('modelV2')
  model.modelV2.orientationRate.z = [0.01] * 33   # a straight path divides by zero in SCC vision
  model.modelV2.velocity.x = [v_ego] * 33
  model.modelV2.position.x = [float(i) for i in range(33)]
  services['modelV2'] = model.modelV2.as_reader()

  return MockSubMaster(services)


def build_planner(v_ego: float) -> LongitudinalPlanner:
  CP = structs.CarParams()
  CP.steerRatio = 15.0
  CP.wheelbase = 2.7
  CP.longitudinalActuatorDelay = 0.2
  CP_SP = custom.CarParamsSP.new_message().as_reader()
  return LongitudinalPlanner(CP, CP_SP, init_v=v_ego)


class TestPlannerWiring(OpenpilotTestCase):
  """The controller has to reach the MPC in the tick it runs, or the offset is always one
  frame stale. LongitudinalPlannerSP.update() runs first inside
  LongitudinalPlanner.update(), before self.mpc.update()."""

  def test_a_congested_tick_reaches_the_mpc_before_it_solves(self):
    Params().put("StopDistance", STOP_DISTANCE_MIN)
    planner = build_planner(v_ego=20.)
    self.assertEqual(planner.mpc.stop_distance, STOP_DISTANCE)

    planner.update(planner_sm(v_ego=20., v_lead=0.))

    self.assertEqual(planner.mpc.stop_distance, STOP_DISTANCE - OFFSET_SLEW * DT_MDL)

  def test_a_free_flowing_tick_leaves_the_mpc_at_stock(self):
    Params().put("StopDistance", STOP_DISTANCE_MIN)
    planner = build_planner(v_ego=20.)

    planner.update(planner_sm(v_ego=20., v_lead=25.))

    self.assertEqual(planner.mpc.stop_distance, STOP_DISTANCE)
```

`OpenpilotTestCase`는 테스트마다 `OpenpilotPrefix`로 파라미터 저장소를 격리하므로 `Params().put`이 다른 테스트로 새지 않는다.

- [ ] **Step 2: 테스트 실패 확인**

```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py'
```

Expected: FAIL. `test_a_congested_tick_reaches_the_mpc_before_it_solves`가 `6.0 != 5.975` — 컨트롤러가 아직 연결되지 않아 `stop_distance`가 그대로다.

- [ ] **Step 3: 플래너에 배선**

`longitudinal_planner.py`의 import 블록에서 `speed_limit_resolver` 줄 뒤에 추가한다 (경로 알파벳 순).

```python
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import StopDistanceController
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
```

`__init__`의 `self.sla = SpeedLimitAssist(CP, CP_SP)` 뒤에 추가한다.

```python
    self.sla = SpeedLimitAssist(CP, CP_SP)
    self.stop_distance = StopDistanceController(mpc)
    self.generation = int(model_bundle.generation) if (model_bundle := get_active_bundle()) else None
```

`update()`에 한 줄 추가한다.

```python
  def update(self, sm: messaging.SubMaster) -> None:
    self.events_sp.clear()
    self.dec.update(sm)
    self.stop_distance.update(sm)
    self.e2e_alerts_helper.update(sm, self.events_sp)
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py'
```

Expected: PASS, `21 passed`.

- [ ] **Step 5: 종방향 회귀 테스트**

플래너를 건드렸으므로 기존 종방향·DEC 테스트가 그대로 통과하는지 확인한다.

```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/ \
    openpilot/selfdrive/controls/tests/test_following_distance.py'
```

Expected: PASS. `test_following_distance.py` 18건(e2e 2 x personality 3 x speed 3)이 전부 통과해야 한다. 이것이 "기본값에서 순정과 동일"의 최종 증거다.

- [ ] **Step 6: 커밋**

```bash
git add openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py \
        openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py
git commit -m "feat: run the stop distance controller from the sunnypilot planner

LongitudinalPlanner.update() calls LongitudinalPlannerSP.update() first
and self.mpc.update() afterwards, so the target set here reaches the same
tick's solve rather than the next one."
```

---

### Task 4: 디바이스 UI

tici 설정 화면의 Cruise 패널에 항목을 추가한다. mici는 대상이 아니다. 축소 스킨이라 DEC·SCC·CustomAcc 등 sunnypilot 종방향 항목이 하나도 없다.

**Files:**
- Modify: `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py`

**Interfaces:**
- Consumes: `StopDistance` 파라미터 키 (Task 2)
- Produces: 없음 (UI 말단)

- [ ] **Step 1: 항목 생성**

`_initialize_items()`의 `dec_map_max_speed_option` 정의 바로 뒤에 추가한다.

```python
    self.stop_distance_option = option_item_sp(
      param="StopDistance",
      title=lambda: tr("Stop Distance in Traffic"),
      min_value=400,
      max_value=600,
      value_change_step=50,
      use_float_scaling=True,
      description=lambda: tr("Gap held from the lead car when stopping behind slow traffic. "
                             "Applies while the lead is under 30 km/h and returns to the stock "
                             "6.0 m once it passes 50 km/h. Driving Personality sets the gap at "
                             "speed; this sets it at a standstill, where Personality has no "
                             "effect. Your car's own AEB is unaffected."),
      label_callback=lambda x: f'{x / 100:.1f} m' if ui_state.is_metric else f'{x / 100 * 3.28084:.1f} ft',
    )
```

`use_float_scaling=True`면 위젯이 내부적으로 값을 x100 정수로 다루고 파라미터에는 `value / 100.0`을 float으로 쓴다 (`option_control.py:48`, `:70`). 그래서 `min_value`/`max_value`/`value_change_step`이 400/600/50이고 `label_callback`도 400~600을 받는다.

- [ ] **Step 2: 리스트에 배치**

같은 함수의 `items` 리스트에서 `dec_map_max_speed_option` 뒤에 넣는다.

```python
    items = [
      self.icbm_toggle,
      self.dec_option,
      self.dec_map_max_speed_option,
      self.stop_distance_option,
      self.scc_v_toggle,
      self.scc_m_toggle,
      self.custom_acc_toggle,
      self.custom_acc_short_increment,
      self.custom_acc_long_increment,
      self.sla_settings_button,
    ]
```

- [ ] **Step 3: 종방향 게이팅**

`_update_state()`의 `if has_long or has_icbm:` 분기에서 `dec_map_max_speed_option` 줄 뒤에 추가한다. DEC와 같은 조건(`has_long`)을 쓴다. ICBM 단독 차량은 크루즈 버튼을 흉내 낼 뿐 이 설정이 먹이는 MPC 목표를 만들지 않는다.

```python
      if has_long or has_icbm:
        self.custom_acc_toggle.action_item.set_enabled(((has_long and not ui_state.CP.pcmCruise) or has_icbm) and ui_state.is_offroad())
        self.dec_option.action_item.set_enabled(has_long)
        self.dec_map_max_speed_option.action_item.set_enabled(has_long)
        self.stop_distance_option.action_item.set_enabled(has_long)
        self.scc_v_toggle.action_item.set_enabled(True)
```

`else` 분기에도 대응하는 두 줄을 넣는다. 파라미터를 지우면 기본값 6.0으로 되돌아가므로 순정 동작이 된다.

```python
      else:
        ui_state.params.remove("CustomAccIncrementsEnabled")
        ui_state.params.remove("DynamicExperimentalControl")
        ui_state.params.remove("DynamicExperimentalControlMapMaxSpeed")
        ui_state.params.remove("SmartCruiseControlVision")
        ui_state.params.remove("SmartCruiseControlMap")
        ui_state.params.remove("StopDistance")
        self.custom_acc_toggle.action_item.set_enabled(False)
        self.dec_option.action_item.set_enabled(False)
        self.dec_map_max_speed_option.action_item.set_enabled(False)
        self.stop_distance_option.action_item.set_enabled(False)
        self.scc_v_toggle.action_item.set_enabled(False)
        self.scc_m_toggle.action_item.set_enabled(False)
```

- [ ] **Step 4: UI 테스트 실행**

raylib은 임포트 시점에 디스플레이를 요구하므로 `xvfb-run`이 필요하다.

```bash
docker cp openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py \
  sp-build:/work/openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  xvfb-run -a --server-args="-screen 0 2160x1080x24" \
  python tools/test_runner.py openpilot/selfdrive/ui/tests/'
```

Expected: PASS. 이 스위트는 설정 화면을 구성해 위젯 트리를 돌기 때문에 잘못된 `option_item_sp` 인자나 등록되지 않은 파라미터 키가 여기서 잡힌다.

- [ ] **Step 5: 라벨 계산 수동 확인**

```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && python -c "
for x in (400, 450, 500, 550, 600):
    print(x, f\"{x/100:.1f} m\", f\"{x/100*3.28084:.1f} ft\")
"'
```

Expected:
```
400 4.0 m 13.1 ft
450 4.5 m 14.8 ft
500 5.0 m 16.4 ft
550 5.5 m 18.0 ft
600 6.0 m 19.7 ft
```

- [ ] **Step 6: 커밋**

```bash
git add openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py
git commit -m "feat: expose the stop distance on the cruise settings page

FLOAT storage with use_float_scaling rather than an integer decimetre
count: the sunnylink option widget has no scale field, so a decimetre
integer would render as '45 m' on the remote frontend.

Gated on has_longitudinal_control the same way DEC is -- an ICBM-only car
emulates cruise buttons and never produces the MPC target this setting
feeds."
```

---

### Task 5: 원격 노출 + 쓰기 게이팅

sunnylink 설정 스키마에 항목을 넣고, 같은 커밋에서 원격 쓰기를 `SENSITIVE_PARAMS`로 막는다. 두 변경은 갈라놓으면 안 된다. 스키마만 먼저 올리면 그 사이에 폰에서 제동 거리를 몰래 줄일 수 있는 창이 열린다.

**Files:**
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml`
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui.json` (생성물, 손으로 고치지 않음)
- Modify: `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py:73-76`
- Modify: `openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py`

**Interfaces:**
- Consumes: `StopDistance` 파라미터 키 (Task 2)
- Produces: 없음 (마지막 태스크)

- [ ] **Step 1: 실패하는 테스트 작성**

`test_sunnylinkd.py`의 `test_saveParams_sensitive_blocked_by_default`에서 dict의 `"JoystickDebugMode": "1",` 줄 뒤에 한 줄을 넣는다. `SENSITIVE_PARAMS`의 "Require physical presence" 그룹 순서와 맞춘다.

```python
      "LongitudinalManeuverMode": "1",
      "JoystickDebugMode": "1",
      "StopDistance": "4.0",
      "RecordFront": "1",
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
docker cp openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py \
  sp-build:/work/openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py'
```

Expected: FAIL. `test_saveParams_sensitive_blocked_by_default`가 `AssertionError: 1 != 0` — `StopDistance`가 게이트를 통과해 저장되었다.

- [ ] **Step 3: SENSITIVE_PARAMS에 등록**

`sunnylinkd.py`의 `SENSITIVE_PARAMS`에서 "Require physical presence" 그룹에 추가한다.

```python
  # Require physical presence
  "LongitudinalManeuverMode",
  "JoystickDebugMode",
  "StopDistance",
```

`saveParams`가 강제하는 것은 `BLOCKED_PARAMS`, `SENSITIVE_PARAMS`(`SunnylinkAllowSensitiveWrite` 토글), `IsEngaged` 셋뿐이다. YAML의 `enablement`는 프론트엔드 힌트일 뿐 기기측 강제가 없으므로, `SENSITIVE_PARAMS` 등록이 원격 쓰기를 막는 유일한 수단이다.

- [ ] **Step 4: 테스트 통과 확인**

```bash
docker cp openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py \
  sp-build:/work/openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py'
```

Expected: PASS, `9 passed` (기존 테스트 수 그대로 — 새 키를 기존 dict에 끼워 넣었을 뿐이다).

- [ ] **Step 5: YAML 항목 추가**

`cruise.yaml`의 `core_cruise_features` 섹션에서 `LongitudinalPersonality` 항목 바로 뒤, `IntelligentCruiseButtonManagement` 앞에 넣는다. Personality가 속도 구간의 간격을, 이 항목이 정차 간격을 담당하므로 나란히 두는 편이 읽힌다.

```yaml
  - key: StopDistance
    widget: option
    title: Stop Distance in Traffic
    description: Gap held from the lead car when stopping behind slow traffic. Applies while
      the lead is under 30 km/h and returns to the stock 6.0 m once it passes 50 km/h. Driving
      Personality sets the gap at speed; this sets it at a standstill, where Personality has
      no effect. Your car's own AEB is unaffected.
    min: 4.0
    max: 6.0
    step: 0.5
    unit: m
    visibility:
    - $ref: '#/macros/longitudinal'
    enablement:
    - $ref: '#/macros/longitudinal'
```

`unit`은 문자열 `m`이다. `SpeedLimitValueOffset`처럼 `{metric, imperial}` 객체를 쓰면 안 된다. 그 항목은 사용자 단위로 저장되지만 `StopDistance`는 항상 미터로 저장되므로, `imperial: ft`를 선언하면 미터 값에 피트 라벨이 붙는다.

- [ ] **Step 6: settings_ui.json 재생성**

호스트에는 PyYAML이 없다. 컨테이너에서 돌리고 결과를 호스트로 되가져온다.

```bash
docker cp openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml \
  sp-build:/work/openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py'
docker cp sp-build:/work/openpilot/sunnypilot/sunnylink/settings_ui.json \
  openpilot/sunnypilot/sunnylink/settings_ui.json
```

Expected: `Wrote /work/openpilot/sunnypilot/sunnylink/settings_ui.json`

- [ ] **Step 7: 스키마 테스트 실행**

```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/sunnylink/'
```

Expected: PASS. 세 가지가 자동으로 검증된다.
- `test_compile_settings_ui.py::test_compiled_matches_committed` — 커밋된 JSON이 YAML 컴파일 결과와 일치
- `test_settings_schema.py::test_all_schema_keys_exist_in_params` — `StopDistance`가 `params_keys.h`에 등록되어 있음 (Task 2)
- `test_settings_schema.py::test_no_duplicate_keys_across_panels` — 키 중복 없음

- [ ] **Step 8: 커밋**

```bash
git add openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml \
        openpilot/sunnypilot/sunnylink/settings_ui.json \
        openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py \
        openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py
git commit -m "feat: expose the stop distance over sunnylink, behind sensitive writes

The schema entry and the SENSITIVE_PARAMS registration belong in one
commit. saveParams enforces exactly three things -- BLOCKED_PARAMS,
SENSITIVE_PARAMS behind SunnylinkAllowSensitiveWrite, and IsEngaged. A
YAML enablement rule is a frontend hint with no device-side check, so
landing the schema alone would open a window where a phone could shorten
the car's stopping gap unattended.

settings_ui.json is generated; regenerated with compile_settings_ui.py."
```

---

## 전체 검증

5개 태스크가 모두 끝난 뒤 한 번 돌린다.

```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  xvfb-run -a --server-args="-screen 0 2160x1080x24" python tools/test_runner.py \
    openpilot/sunnypilot/selfdrive/controls/lib/ \
    openpilot/sunnypilot/sunnylink/ \
    openpilot/selfdrive/controls/tests/test_following_distance.py \
    openpilot/selfdrive/ui/tests/'
```

Expected: 전부 PASS.

`test_following_distance.py`가 이 계획의 핵심 안전 확인이다. 기본값 6.0에서 18가지 (e2e 2 x personality 3 x speed 3) 조합의 정상 상태 추종 간격이 순정과 같아야 한다.

## 롤백

- 사용자 롤백: `StopDistance`를 6.0으로 되돌리거나 파라미터를 삭제한다 (기본값 6.0).
- 코드 롤백: Task 1의 2줄만 되돌리면 나머지 태스크의 변경이 전부 no-op이 된다. 컨트롤러는 계속 돌지만 `mpc.stop_distance`를 아무도 읽지 않는다.
