# 정체 구간 정차 간격 조절 (Congestion Stop Distance) 설계

**Goal:** 정체 구간에서 앞차와의 정차 간격이 주변 차량 대비 지나치게 멀어 다른 차량의 끼어들기가 빈번한 문제를 해결한다. 사용자가 4.0~6.0 m 범위에서 정차 간격을 조절할 수 있게 하고, 이 설정은 **앞차가 느릴 때만** 적용해 고속 주행 시 추종 간격은 순정 그대로 둔다.

**Architecture:** `STOP_DISTANCE` 상수를 바꾸는 대신 MPC에 들어가는 `x_obstacle` 값을 런타임에 오프셋한다. MPC 비용 함수가 x에 대해 평행이동 불변이므로(`X_EGO_COST = 0.`) 이 둘은 수학적으로 등가이며, acados 재빌드가 필요 없다. 정체 판정과 파라미터 읽기는 sunnypilot 측 신규 모듈이 담당하고, 업스트림 파일 수정은 2줄로 제한한다.

**Tech Stack:** Python 3.11+ (디바이스), stdlib + numpy. 신규 런타임 의존성 없음.

---

## Background: 왜 정차 간격만 조절할 수 없었나

openpilot의 정상 추종 간격은 `long_mpc.py:85`의 한 식으로 결정된다.

```python
def get_safe_obstacle_distance(v_ego, t_follow):
  return (v_ego**2) / (2 * COMFORT_BRAKE) + t_follow * v_ego + STOP_DISTANCE
```

정상 상태(자차와 앞차가 같은 속도)에서 첫 항이 `get_stopped_equivalence_factor`와 상쇄되므로 실제 간격은 다음과 같다.

```
간격 = t_follow x v_ego + STOP_DISTANCE
       \___________/     \____________/
       Driving           조절 수단 없음
       Personality
```

`t_follow`는 Driving Personality가 이미 조절한다(공격적 1.25 / 표준 1.45 / 여유 1.75). 그러나 **완전 정차 시 `v_ego = 0`이므로 이 항이 0이 되어 Personality가 무력화된다.** 남는 것은 `STOP_DISTANCE = 6.0` 상수뿐이고, 이는 어떤 설정으로도 바꿀 수 없었다.

| 속도 | 표준(1.45) | 공격적(1.25) | Personality 효과 |
|---|---|---|---|
| 0 km/h | 6.0 m | 6.0 m | **0 m** |
| 30 km/h | 18.1 m | 16.4 m | 1.7 m |
| 60 km/h | 30.2 m | 26.8 m | 3.3 m |
| 100 km/h | 46.3 m | 40.7 m | 5.6 m |

두 손잡이는 정확히 상보적이다. 이 설계는 Personality가 닿지 못하는 저속·정차 구간만 담당하며, 기능 중복이 발생하지 않는다.

거리 기준도 명확히 해 둔다. `dRel = lead.x[0] - RADAR_TO_CAMERA` (`radard.py:143`, `RADAR_TO_CAMERA = 1.52`)이므로 앞차 **뒷범퍼**까지의 거리다. 체감 범퍼-투-범퍼 간격은 카메라와 자차 앞범퍼 사이 오프셋만큼 더 짧다.

## Non-goals

- 속도 무관 전역 오프셋. 고속 간격은 Personality의 영역이고, 고속에서 2 m를 줄이면 운동에너지 대비 위험만 커진다.
- `COMFORT_BRAKE`, `t_follow`, `LEAD_DANGER_FACTOR` 노출.
- acados 솔버 재생성. 오프셋 방식을 택한 이유가 이것이다.
- `getParams` 블록리스트 추가. 정차 간격은 읽혀도 무해하다.
- mici UI 노출. mici 설정 화면에는 DEC·SCC·CustomAcc 등 sunnypilot 종방향 항목이 하나도 없다. 축소 스킨이므로 대상 외다.

---

## Architecture

### 데이터 흐름

```
StopDistance (FLOAT param, 4.0 ~ 6.0)
  디바이스   ui/sunnypilot/layouts/settings/cruise.py    option_item_sp
  원격       settings_ui_src/pages/cruise.yaml           widget: option
                        |
                        v  1 Hz 재읽기
  StopDistanceController.update(sm)                      [신규, sunnypilot]
    - 앞차 속도로 정체 판정 (히스테리시스)
    - 비대칭 슬루 + 정차 중 동결
                        |
                        v  mpc.stop_distance = 4.5
  LongitudinalMpc.update()                               [업스트림, 2줄]
    params[:,2] = min(x_obstacles) + (STOP_DISTANCE - self.stop_distance)
                        |
                        v
  acados: ((x_obstacle - x_ego) - desired_dist_comfort) / (v_ego + 10.)
```

### 호출 순서

`LongitudinalPlanner.update()`(업스트림)는 첫 줄에서 `LongitudinalPlannerSP.update(self, sm)`를 호출하고(77행), `self.mpc.update(...)`는 120행에서 실행한다. 따라서 SP 측에서 설정한 `mpc.stop_distance`가 같은 틱의 MPC 풀이에 반영된다.

### 오프셋을 `params[:,2]` 대입 시점에 넣는 이유

`x_obstacles` 생성 직후에 더하면 바로 아래 `self.source = MPC_SOURCES[np.argmin(x_obstacles[0])]`의 입력이 바뀐다. 두 컬럼에 같은 상수를 더하므로 argmin 결과는 실제로 동일하지만, 값과 판정을 분리해 두는 편이 회귀 시 원인을 좁히기 쉽다. `params[:,2]` 대입 한 줄만 바꾼다.

---

## Behavior

### 정체 판정: 앞차 속도 기준 히스테리시스

```python
CONGESTION_LEAD_V_ENTER = 8.33    # m/s (30 km/h)
CONGESTION_LEAD_V_EXIT = 13.89    # m/s (50 km/h)
```

자차 속도가 아니라 **앞차 속도**로 판정한다. 신호대기 중인 앞차로 접근하는 상황이 그 이유다.

```
내 속도 60 km/h, 앞차 정지, 감속 2.5 m/s^2, 정지까지 6.7초 / 55.6 m

자차속도 게이트 (30 km/h 진입)
  t=0.0s   60 km/h   게이트 닫힘   offset = 0.00 m
  t=3.4s   30 km/h   게이트 열림   offset = 0.00 m
  t=6.7s    0 km/h   정차          offset = 1.67 m   <- 83%만 적용
  t=7.4s    0 km/h   정차 후       offset = 2.00 m   <- 33 cm 크립 발생

앞차속도 게이트 (앞차 30 km/h 진입)
  t=0.0s   60 km/h   게이트 열림   offset = 0.00 m
  t=2.0s   42 km/h                offset = 2.00 m   <- 완료
  t=6.7s    0 km/h   정차          offset = 2.00 m
  이후                             변화 없음
```

자차속도 게이트는 감속이 거의 끝난 시점에 열리므로 오프셋이 정차 후에 채워진다. `x_obstacle`이 멀어지면 `should_stop(v_ego, a_target) = v_ego < 0.3 and a_target < 0.1`이 풀리고 `LongCtrlState.stopping`에서 `pid`로 전이해 차가 앞으로 기어간 뒤 다시 멈춘다. 고장으로 오인될 동작이다.

앞차가 멈춰 있다는 사실은 수십 m 전부터 알 수 있으므로, 앞차속도 게이트는 감속이 시작되기도 전에 오프셋을 채운다.

부수 효과 세 가지가 모두 안전 방향이다.

1. 게이트 조건이 `vLead < 30 km/h`이므로 앞차가 이미 느리다. 느린 차는 급제동 여력이 작아, 후술할 "앞차 예상 밖 급제동" 위험이 구조적으로 제거된다.
2. 자차속도는 정체가 아닌 저속(골목, 주차장 진입)도 잡지만 앞차속도는 "앞이 막혀 있다"를 직접 관측한다.
3. 고속 주행 중 앞차도 빠르면 게이트가 열리지 않아 100 km/h 추종 간격 46.3 m는 손대지 않는다.

앞차가 없으면 게이트는 닫힌다. `process_lead`가 리드 부재 시 `x_lead = 50.0`으로 위장하므로 오프셋이 의미를 갖지 못한다.

### 정체 에피소드 내 오프셋 고정

`X_EGO_COST = 0.`이라 MPC 비용에서 x가 단독으로 등장하는 항이 없다. x에 의존하는 항은 전부 차이(`x_obstacle - x_ego`)뿐이고 차량 동역학도 x에 대해 평행이동 불변이며 `x0 = np.zeros(X_DIM)`으로 매 틱 원점에서 출발한다. 따라서 **오프셋이 상수로 유지되는 한 해 궤적은 정확히 오프셋만큼 평행이동할 뿐 모양이 바뀌지 않는다.** 감속도 크기, 저크, 감속 곡선이 전부 순정과 동일하다.

이 성질을 지키려면 정체 에피소드 내내 오프셋이 변하지 않아야 한다. 속도 테이퍼(`offset = f(v_ego)`)를 쓰면 정지에서 출발할 때 오프셋이 줄어들면서 장애물이 다가오는 것으로 보여 가속이 억제된다. 가속 구간 약 14 m 중 2 m를 잃어 유효 여유가 14% 감소하고, 신호마다 발진이 굼떠진다. 사용자 목적과 정반대다.

히스테리시스(진입 30 / 해제 50 km/h)를 쓰면 stop-and-go 왕복 구간에서 앞차 속도가 진입 임계 아래에 머무르므로 오프셋이 고정된다. 오프셋이 변하는 것은 정체를 벗어날 때 한 번뿐이고, 그 시점은 자유 가속 구간이라 주행거리 50 m 이상에 2 m가 분산되어 체감되지 않는다.

### 비대칭 슬루 + 정차 중 동결

```python
OFFSET_SLEW = 0.5      # m/s
STANDSTILL_V = 0.3     # m/s, should_stop()과 동일 임계
```

오프셋의 두 방향은 안전 의미가 다르다.

| 방향 | 물리 의미 | 안전 |
|---|---|---|
| 오프셋 증가 | 장애물을 밀어냄 = 제동 약화 | 위험 방향 |
| 오프셋 감소 | 장애물을 당겨옴 = 제동 강화 | 안전 방향 |

증가만 제한한다. 앞차가 없다가 느린 차가 갑자기 끼어들면 게이트가 열리며 오프셋이 올라가는데, 컷인은 즉시 제동이 필요한 순간이므로 방향이 반대다. 0.5 m/s로 제한하면 그 완화가 4초에 걸쳐 분산된다.

정차 중에는 증가를 완전히 동결한다. 정차 후 오프셋이 커지면 위에서 설명한 33 cm 크립이 발생한다. 감소는 즉시 반영해도 무해하다. 이미 멈춰 있는 차에 "더 제동하라"는 요구는 움직임을 만들지 않는다.

---

## 파일별 변경 (8개)

### 1. `openpilot/common/params_keys.h`

sunnypilot 알파벳 블록(`SpeedLimitValueOffset` 근처)에 1줄 추가한다.

```cpp
{"StopDistance", {PERSISTENT | BACKUP, FLOAT, "6.0"}},
```

FLOAT 타입은 이미 `TorqueParamsOverrideLatAccelFactor`, `TorqueParamsOverrideFriction`이 쓰고 있다. 정수 데시미터 트릭이 필요 없다.

### 2. `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` (업스트림, 2줄)

`__init__`:
```python
    self.stop_distance = STOP_DISTANCE
```

`update()`, 현재 332행:
```python
    self.params[:,2] = np.min(x_obstacles, axis=1) + (STOP_DISTANCE - self.stop_distance)
```

기본값이 `STOP_DISTANCE`이므로 sunnypilot 컨트롤러가 붙지 않은 모든 경로(`test_longitudinal.py`, 리플레이, 다른 인스턴스화)의 동작이 비트 단위로 보존된다.

### 3. `openpilot/sunnypilot/selfdrive/controls/lib/stop_distance.py` (신규)

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import STOP_DISTANCE

# Range offered by the UI. 4.0 m is the floor: the MPC's danger-zone constraint sits at
# LEAD_DANGER_FACTOR * 6.0 = 4.5 m below the stock target, so a 4.0 m target still leaves a
# 2.5 m hard floor. Below that there is nothing left for a rear-end shove to spend.
STOP_DISTANCE_MIN = 4.0
STOP_DISTANCE_MAX = 6.0

# Congestion is read off the lead, not off ego speed. A lead stopped at a light is known
# tens of metres out, so the offset is fully applied before braking even starts. Gating on
# ego speed instead only opens once deceleration is nearly over, and the remainder fills in
# after the car has stopped -- which creeps it forward.
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

    value = float(self._params.get("StopDistance", return_default=True))
    clipped = float(np.clip(value, STOP_DISTANCE_MIN, STOP_DISTANCE_MAX))
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

클램프 후 되쓰기는 `get_sanitize_int_param`의 FLOAT 판박이다. 호출자가 하나뿐이므로 공용 헬퍼로 뽑지 않는다. 두 번째 소비자가 생기면 그때 `openpilot/sunnypilot/__init__.py`로 올린다.

### 4. `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` (3줄)

```python
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import StopDistanceController
...
    self.stop_distance = StopDistanceController(mpc)     # __init__
...
    self.stop_distance.update(sm)                        # update()
```

### 5. `openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py`

```python
    self.stop_distance_option = option_item_sp(
      param="StopDistance",
      title=lambda: tr("Stop Distance in Traffic"),
      min_value=400, max_value=600, value_change_step=50,
      use_float_scaling=True,
      description=lambda: tr("Gap held from the lead car when stopping behind slow traffic. "
                             "Applies while the lead is under 30 km/h and returns to the stock "
                             "6.0 m once it passes 50 km/h. Driving Personality sets the gap at "
                             "speed; this sets it at a standstill, where Personality has no "
                             "effect. Your car's own AEB is unaffected."),
      label_callback=lambda x: f"{x / 100:.1f} m" if ui_state.is_metric else f"{x / 100 * 3.28084:.1f} ft",
    )
```

`use_float_scaling=True`이면 위젯이 내부적으로 x100 정수로 다루고 파라미터에는 float 문자열로 쓴다(`option_control.py:48`). 따라서 `min_value`/`max_value`는 400/600이다. `items` 리스트에서 `dec_map_max_speed_option` 뒤에 배치한다.

### 6. `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/cruise.yaml`

`core_cruise_features` 섹션의 `LongitudinalPersonality` 바로 뒤에 삽입한다.

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

이후 `python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py`로 `settings_ui.json`을 재생성한다. `settings_ui.json`은 생성물이므로 손으로 고치지 않는다. 두 파일 모두 커밋한다.

### 7. `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py`

`SENSITIVE_PARAMS`의 "Require physical presence" 그룹에 추가한다.

```python
  # Require physical presence
  "LongitudinalManeuverMode",
  "JoystickDebugMode",
  "StopDistance",
```

`saveParams`는 `BLOCKED_PARAMS`, `SENSITIVE_PARAMS`(`SunnylinkAllowSensitiveWrite` 게이트), `IsEngaged` 세 가지만 강제한다. YAML의 `enablement: offroad`는 프론트엔드 힌트일 뿐 기기측 강제가 없으므로, 원격에서 제동 거리를 몰래 줄이는 경로를 막으려면 `SENSITIVE_PARAMS` 등록이 유일한 수단이다.

### 8. `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_stop_distance.py` (신규)

레포 규약에 따라 stdlib `unittest`로 작성한다(pytest는 의존성이 아니며 `tools/test_runner.py`가 실행한다).

| # | 검증 항목 |
|---|---|
| 1 | 기본값 6.0에서 `params[:,2]`가 변경 이전과 동일 (회귀 고정) |
| 2 | 컨트롤러 없이 만든 `LongitudinalMpc`의 `stop_distance == STOP_DISTANCE` |
| 3 | `mpc.stop_distance`를 4.0으로 직접 설정하면(게이트 우회) 정상 상태 간격이 v=0/30/60/100 km/h 전부에서 정확히 2.0 m 감소. 오프셋 산술과 게이트 로직을 분리해 검증한다 |
| 4 | 범위 밖 값(0.0, 99.0)이 클램프되고 파라미터에 되쓰기됨 |
| 5 | 파라미터 읽기가 1 Hz로 스로틀됨 (`FakeParams.get` 호출 횟수) |
| 6 | 게이트: `vLead` 60 km/h면 오프셋 0 유지, 10 km/h면 목표까지 상승 |
| 7 | 게이트: 리드 부재 시 오프셋 0 |
| 8 | 히스테리시스: 진입은 30 km/h 미만에서만, 해제는 50 km/h 초과에서만 |
| 9 | 정차 중 동결: `vEgo < 0.3`에서 오프셋이 증가하지 않음 |
| 10 | 슬루 비대칭: 증가는 `OFFSET_SLEW`로 제한, 감소는 1틱에 완료 |
| 11 | 위험구간 하한: 4.0 설정 시 정차 하드 하한이 2.5 m 이상 |

11번은 업스트림이 `LEAD_DANGER_FACTOR`를 바꾸면 잡아내기 위한 것이다.

---

## Safety

### 코드로 증명되는 것

- **감속 프로파일 불변.** `X_EGO_COST = 0.`이므로 x 의존 항이 전부 차이 형태이고 동역학도 평행이동 불변이다. 오프셋이 상수인 동안 해 궤적은 정확히 평행이동하며 감속도·저크·곡선이 순정과 동일하다.
- **위험구간 여유 절대값 보존.** 정차 시 `desired_dist_comfort = 6.0`, 제약은 `gap >= 0.75 x 6.0 = 4.5`. 기본 설정은 목표 6.0 / 하한 4.5 / 여유 1.5 m, 4.0 설정은 목표 4.0 / 하한 2.5 / 여유 1.5 m다. 여유는 같고 하한만 2 m 내려간다.
- **순정 AEB 무관.** openpilot은 자동 제동을 하지 않고 FCW는 경고뿐이다. 차량 자체 전방충돌방지보조는 openpilot 신호를 거치지 않으므로 최후 방어선이 그대로 남는다.
- **FCW 감도.** `crash_cnt` 판정은 `lead_xv_0[FCW_IDXS,0] - self.x_sol[FCW_IDXS,0] < CRASH_DISTANCE`다. 원본 리드 위치는 그대로지만 계획 궤적 `x_sol`이 오프셋만큼 앞으로 이동하므로 FCW가 더 민감해진다. `x_sol`은 차가 실제로 가려는 위치이므로 이는 정상 동작이다. 실제로 가까이 붙으니 경고가 잦아지는 것이 옳다.

### 코드로 증명되지 않는 실제 위험 (심각도순)

1. **후방 추돌 시 2차 충돌.** 뒤에서 받히면 앞으로 밀린다. 4.0 m는 완충이 2 m 적다. 어떤 소프트웨어 방어로도 막을 수 없는 유일한 항목이라 1위다. 다만 정체 게이트 덕분에 저속 구간에 한정되어 충격 자체가 작다.
2. **앞차 밀림 또는 후진.** 경사로 신호대기, 골목 후진, 주차 중 재조정. 이미 멈춘 차는 다가오는 앞차에 대응할 수단이 없다.
3. **리드 상실 후 크립 전진.** `lead.present`가 거짓이면 `process_lead`가 `x_lead = 50.0`으로 위장해 `should_stop`이 풀리고 차가 전진한다. 구조는 6.0 m에서도 동일하고 여유만 2 m 적다. 레이더 차량은 `0.75 < dRel < 25` 필터를 쓰므로 4 m 유실 위험이 낮고, 비전 전용 차량은 카메라 기준 5.5 m로 학습 분포 안이지만 여유는 줄어든다.
4. **앞차의 예상 밖 급제동.** MPC는 앞차 정지거리를 `v_lead^2 / (2 x 2.5)`로 가정한다. 앞차가 더 세게 밟으면 그 오차를 2 m 적은 완충으로 흡수해야 한다. **정체 게이트가 이 위험을 구조적으로 제거한다** — 게이트 조건이 `vLead < 30 km/h`이므로 앞차가 이미 느리고 급제동 여력이 작다.
5. **거리 추정 오차의 상대 비중.** dRel 오차 +-0.5 m가 6.0 m에서 8%, 4.0 m에서 12.5%다. 절대 오차는 같고 비율만 커진다.

### 감속 편의성

| 항목 | 영향 |
|---|---|
| 감속도 크기 | 변화 없음 |
| 감속 곡선 모양 | 변화 없음 |
| 저크 | 변화 없음 |
| 제동 개시 지점 | 오프셋만큼 뒤로 |
| 정차 후 크립 | 없음 (동결 규칙) |
| 발진 지연 | 없음 (히스테리시스로 에피소드 내 오프셋 고정) |
| FCW 알림 빈도 | 증가 (정상 동작) |

정상 상황에서 편의성 손실은 없다. 손실은 앞차가 예상보다 세게 밟는 비정상 상황에서만 나타나며, 그때 자차 제동이 그만큼 세진다.

### 하한을 4.0 m로 고정하는 이유

3.0 m를 허용하면 위험구간 하한이 1.5 m가 되어 후방 추돌 시 완충이 사실상 사라진다. UI와 컨트롤러 양쪽에서 4.0을 강제한다.

---

## Rollback

- `StopDistance`를 6.0으로 되돌리면 순정 동작. 파라미터를 삭제해도 기본값 6.0으로 동일하다.
- 코드 롤백은 항목 2의 2줄만 되돌리면 나머지 파일 변경이 전부 no-op이 된다.

## Open items

- 진입/해제 임계(30 / 50 km/h)는 코드 상수다. 한국 도심 정체가 0~20 km/h 대역이라 30 km/h 진입이 충분히 덮는다. 실주행에서 부족하면 `StopDistanceMaxSpeed` 파라미터로 승격한다.
- 지도 제한속도 대비 실제 속도 비율을 쓰는 정체 판정은 검토 후 배제했다. `MapDataSource` 의존성이 생기고 지도 부재 시 실패 모드가 늘어나는데 정확도 이득이 그만큼 되지 않는다.
