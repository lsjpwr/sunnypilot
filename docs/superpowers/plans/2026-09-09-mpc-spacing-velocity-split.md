# MPC 간격/상대속도 비용 분리 (Spacing–Velocity Cost Split) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종방향 MPC의 비용 잔차에 뭉쳐 있는 간격 오차와 상대속도를 분리해 각각 독립 가중하고, 두 조절값을 sunnylink에서 바꿀 수 있게 한다.

**Architecture:** acados OCP 파라미터를 6개에서 8개로(`v_lead`, `lead_equiv_factor` 추가), 비용 잔차를 6개에서 7개로(`v_ego - v_lead` 추가) 늘린다. 안전 제약은 손대지 않는다. 두 조절값은 `LongitudinalMpc` 인스턴스 속성이고, `StopDistanceController`와 같은 형태의 컨트롤러가 Params에서 읽어 채운다.

**Tech Stack:** Python 3.12 (컨테이너), CasADi 3.6.7 / acados (NONLINEAR_LS, Gauss-Newton, HPIPM), `numpy`, stdlib `unittest` via `tools/test_runner.py`, SCons.

**설계 문서:** `docs/superpowers/specs/2026-09-09-mpc-spacing-velocity-split-design.md` (commit `f8254cc8c`)

## Global Constraints

- 코드·주석·커밋 메시지는 영어. 이 계획서와 설계서만 한국어.
- Python 들여쓰기 2칸 (레포 기존 규약).
- 테스트 프레임워크는 stdlib `unittest`, 베이스 클래스는 `OpenpilotTestCase`. pytest는 레포 의존성이 아니다. 실행은 `python tools/test_runner.py <경로>`.
- 안전 제약 `ocp.model.con_h_expr`(`long_mpc.py:169-173`)를 바꾸지 않는다. 계속 원래의 `x_obstacle`(`p[2]`)과 `desired_dist_comfort`를 쓴다.
- `COMFORT_BRAKE`, `STOP_DISTANCE`, `T_FOLLOW`, `A_CHANGE_COST`, `J_EGO_COST`, `X_EGO_OBSTACLE_COST`, `DANGER_ZONE_COST`, `LEAD_DANGER_FACTOR`를 바꾸지 않는다.
- 출하 기본값은 중립이다. `LEAD_EQUIV_FACTOR = 1.`, `LEAD_VELOCITY_COST = 0.`
- 최종 차원은 정확히 `PARAM_DIM = 8`, `COST_E_DIM = 6`, `COST_DIM = 7`.
- 새 cereal 필드나 UI 페이지를 만들지 않는다.
- 이 계획은 레포 커밋까지다. 실차 적용은 설계서 Safety 절의 검증 순서를 따라 별도로 판단한다.

---

## 실행 환경 (모든 스텝 공통)

호스트는 Windows이고 acados/`.venv`가 없다. 검증은 이미 떠 있는 Docker 컨테이너 `sp-build`에서 한다.

컨테이너의 git HEAD는 호스트보다 뒤지만, 이 계획이 닿는 임포트 경로 전체에서 워킹트리 내용이 호스트와 md5가 일치함을 확인했다. 아래 모든 기대값은 추정이 아니라 이 환경에서 실측한 값이다.

호스트 레포 루트(`E:\dev\sunnypilot`)에서 쓰는 명령 셋.

```bash
# 파일을 컨테이너로 복사
docker cp <호스트 상대경로> sp-build:/work/<같은 경로>

# 솔버 재빌드 (acados 코드 생성까지 scons가 알아서 한다)
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  scons -j8 openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/'

# 테스트 실행
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py <경로>'
```

**`python3 long_mpc.py`를 직접 실행하지 않는다.** 컨테이너에서 그냥 실행하면 `FileNotFoundError: .../acados_layout.json`으로 죽는다. acados가 `ACADOS_SOURCE_DIR`과 `ACADOS_PYTHON_INTERFACE_PATH`를 필요로 하는데 이를 `SConstruct:125-126`이 설정하기 때문이다. `scons`를 쓰면 이 문제가 없다.

`long_mpc.py`를 바꾸면 반드시 재빌드해야 한다. 안 하면 테스트가 옛 `.so`를 계속 쓴다.

호스트 워킹트리는 LF다 (`core.autocrlf=false`). `docker cp`는 양방향 모두 줄바꿈을 바꾸지 않는다.

### 측정된 기준선

| 경로 | 변경 전 |
|---|---|
| `openpilot/sunnypilot/selfdrive/controls/lib/tests/` | 103 passed |
| `openpilot/sunnypilot/selfdrive/controls/lib/` | 198 passed |
| `openpilot/sunnypilot/sunnylink/tests/` | 110 passed |
| `openpilot/selfdrive/test/longitudinal_maneuvers/` | 4 passed |

`compile_settings_ui.py --check`는 현재 동기 상태다 (`settings_ui.json matches compiled output`).

### 측정된 변경 후 거동

컨테이너에서 Task 1의 변경을 그대로 적용해 빌드하고 측정했다. 접근 시나리오는 `v_ego = 30 m/s`, `d_rel = 60 m`, `v_lead = 20 m/s`, 5틱이다.

| 설정 | solution_status | a_solution 최소 | v_solution 마지막 |
|---|---|---|---|
| f=1, cost=0 (중립) | 0 | −2.6217 | 19.127 |
| f=0, cost=0 | 0 | −2.4588 | 15.656 |
| f=0, cost=0.27 | 0 | −2.6228 | 19.083 |
| f=0, cost=1.0 | 0 | −2.9225 | 19.952 |
| f=0, cost=2.0 | 0 | −3.0821 | 20.074 |
| f=1, cost=1.0 | 0 | −2.9783 | 20.041 |

범위 상한(cost=2.0)에서도 `solution_status = 0`이다. 솔버 발산은 관측되지 않았다.

리드 없음 + cost=2.0에서 `a_solution` 최대는 `+2.0000`으로, `ACCEL_MAX`에서 포화한다. 중립 설정에서도 같은 `+2.0000`이다.

---

## File Structure

| 파일 | 책임 | 태스크 |
|---|---|---|
| `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` | OCP 파라미터·비용 벡터 확장, 두 인스턴스 속성 | 1 |
| `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py` | 골든 중립성 + 차원 + 분리 거동 | 1 |
| `openpilot/common/params_keys.h` | Params 키 2개 등록 | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py` | Params → mpc 속성 배선 | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py` | 컨트롤러 클램프·주기 | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` | 컨트롤러 생성·구동 3줄 | 2 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml` | 섹션 1개, 위젯 2개 | 2 |
| `openpilot/sunnypilot/sunnylink/settings_ui.json` | `compile_settings_ui.py` 산출물 | 2 |
| `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py` | 위젯 존재·범위 가드 | 2 |

태스크 1만 마쳐도 거동은 현재와 같다. 조절 수단이 없을 뿐이므로 여기서 멈출 수 있다.

---

### Task 1: MPC 비용 벡터에 상대속도 항 추가

**Files:**
- Modify: `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py:35-44`, `:105-110`, `:139-147`, `:157-163`, `:180`, `:215-220`, `:265-270`, `:328-334`
- Create: `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py`

**Interfaces:**
- Consumes: 없음. 이 계획의 첫 태스크다.
- Produces:
  - `PARAM_DIM = 8`, `COST_E_DIM = 6`, `COST_DIM = 7` (모듈 상수, `int`)
  - `LEAD_VELOCITY_COST = 0.`, `LEAD_EQUIV_FACTOR = 1.` (모듈 상수, `float`)
  - `LongitudinalMpc.lead_velocity_cost: float` — 5번 잔차의 가중치. 태스크 2의 컨트롤러가 쓴다.
  - `LongitudinalMpc.lead_equiv_factor: float` — 분리 비율. 태스크 2의 컨트롤러가 쓴다.
  - `set_weights`, `set_cur_state`, `update`, `run`의 시그니처는 바뀌지 않는다.

- [ ] **Step 1: 기준선 측정**

Run:
```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/'
```

Expected: `103 passed`

- [ ] **Step 2: 테스트 파일 작성**

`openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py`를 새로 만든다.

골든 배열은 변경 전 빌드에서 실측한 값이다. 다시 계산하거나 반올림하지 않는다.

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import (COST_DIM, COST_E_DIM, LongitudinalMpc,
                                                                            PARAM_DIM)

# Recorded from the solver before the split existed. The neutral defaults have to reproduce
# these: they are the only baseline the change can be rolled back to.
GOLDEN_APPROACH_A = [0.000000000, -0.358865855, -0.939113951, -1.261081262, -1.585006312, -2.446618260,
                     -2.621677558, -2.120822120, -1.461936525, -0.813169851, -0.326543488, -0.072883003,
                     -0.006324400]
GOLDEN_APPROACH_V = [30.000000000, 29.987539380, 29.852333150, 29.470354815, 28.778597418, 27.518714739,
                     25.582907308, 23.442195648, 21.576175520, 20.233230784, 19.481336567, 19.190088083,
                     19.126832171]
GOLDEN_STEADY_A = [0.000000000, 0.006870168, 0.018425980, 0.027558601, 0.045057799, 0.097510452, 0.148850166,
                   0.152023490, 0.116644870, 0.063494895, 0.015301168, -0.012992610, -0.021085749]
GOLDEN_STEADY_V = [30.000000000, 30.000238547, 30.002873563, 30.010856997, 30.028506817, 30.073059395,
                   30.167155465, 30.302966490, 30.442897927, 30.549230428, 30.601213942, 30.602897266,
                   30.575681910]

# Measured drift between the stock build and the split at neutral defaults is 5e-10: the residual
# value is unchanged, but the regenerated C evaluates a different expression tree and that reaches
# HPIPM's iterates. 1e-8 is two orders above that noise and eight below the smallest behavior
# change the split produces (0.16 m/s^2). If this ever fails, the defaults stopped being neutral --
# investigate, do not loosen the tolerance.
NEUTRAL_ATOL = 1e-8


def radar_state(d_rel: float, v_lead: float, present: bool = True):
  msg = messaging.new_message('radarState')
  lead = msg.radarState.leadOne
  lead.present = present
  lead.dRel = d_rel
  lead.vLead = v_lead
  return msg.radarState.as_reader()


def drive(mpc: LongitudinalMpc, v_ego: float, d_rel: float, v_lead: float, ticks: int = 5) -> LongitudinalMpc:
  """Five ticks so a_prev has settled -- the first solve starts from an all-zero previous plan."""
  reader = radar_state(d_rel, v_lead)
  for _ in range(ticks):
    mpc.set_weights()
    mpc.set_cur_state(v_ego, 0.)
    mpc.update(reader)
  return mpc


def tuned(equiv_factor: float, velocity_cost: float) -> LongitudinalMpc:
  mpc = LongitudinalMpc()
  mpc.lead_equiv_factor = equiv_factor
  mpc.lead_velocity_cost = velocity_cost
  return mpc


class TestCostVectorLayout(OpenpilotTestCase):
  def test_the_split_adds_two_parameters_and_one_residual(self):
    self.assertEqual(PARAM_DIM, 8)
    self.assertEqual(COST_DIM, 7)
    self.assertEqual(COST_E_DIM, 6)

  def test_a_fresh_mpc_starts_neutral(self):
    # Every path that builds an MPC without the sunnypilot controller -- replay, the maneuver
    # harness, test_longitudinal.py -- has to keep stock behavior.
    mpc = LongitudinalMpc()
    self.assertEqual(mpc.lead_velocity_cost, 0.)
    self.assertEqual(mpc.lead_equiv_factor, 1.)


class TestNeutralDefaults(OpenpilotTestCase):
  """The design rests on this: at factor 1.0 with a zero velocity weight the solver produces
  what it produced before the split existed."""

  def test_the_approach_solution_is_unchanged(self):
    mpc = drive(LongitudinalMpc(), v_ego=30., d_rel=60., v_lead=20.)
    self.assertEqual(mpc.solution_status, 0)
    np.testing.assert_allclose(mpc.a_solution, GOLDEN_APPROACH_A, atol=NEUTRAL_ATOL)
    np.testing.assert_allclose(mpc.v_solution, GOLDEN_APPROACH_V, atol=NEUTRAL_ATOL)

  def test_the_steady_solution_is_unchanged(self):
    mpc = drive(LongitudinalMpc(), v_ego=30., d_rel=60., v_lead=30.)
    self.assertEqual(mpc.solution_status, 0)
    np.testing.assert_allclose(mpc.a_solution, GOLDEN_STEADY_A, atol=NEUTRAL_ATOL)
    np.testing.assert_allclose(mpc.v_solution, GOLDEN_STEADY_V, atol=NEUTRAL_ATOL)


class TestTheSplit(OpenpilotTestCase):
  def test_shedding_the_equivalence_brakes_less(self):
    # At factor 0 the gap residual loses its relative-velocity content. With no velocity weight
    # to replace it the solver sees a surplus where it used to see a deficit, so it brakes less.
    stock = drive(LongitudinalMpc(), v_ego=30., d_rel=60., v_lead=20.)
    shed = drive(tuned(0., 0.), v_ego=30., d_rel=60., v_lead=20.)

    self.assertEqual(shed.solution_status, 0)
    self.assertGreater(shed.a_solution.min(), stock.a_solution.min())

  def test_the_velocity_weight_puts_the_braking_back(self):
    # LeadVelocityCost is the replacement knob. It has to restore what the shed equivalence took.
    shed = drive(tuned(0., 0.), v_ego=30., d_rel=60., v_lead=20.)
    weighted = drive(tuned(0., 1.), v_ego=30., d_rel=60., v_lead=20.)

    self.assertEqual(weighted.solution_status, 0)
    self.assertLess(weighted.a_solution.min(), shed.a_solution.min())

  def test_the_top_of_the_offered_range_still_converges(self):
    # 2.0 is the maximum the UI offers. A weight that stops the solver converging would show up
    # as solution_status != 0, which sends run() into reset() every tick.
    mpc = drive(tuned(0., 2.), v_ego=30., d_rel=60., v_lead=20.)
    self.assertEqual(mpc.solution_status, 0)

  def test_no_lead_stays_bounded_by_accel_max(self):
    # process_lead() fakes a lead 10 m/s faster than ego when there is none, so a non-zero
    # velocity weight pushes for acceleration. It has to stay inside the box constraint; the
    # planner's min() against a_cruise then caps it further.
    mpc = LongitudinalMpc()
    mpc.lead_equiv_factor = 0.
    mpc.lead_velocity_cost = 2.
    reader = radar_state(60., 20., present=False)
    for _ in range(5):
      mpc.set_weights()
      mpc.set_cur_state(30., 0.)
      mpc.update(reader)

    # The acceleration bound is a slack constraint, not a hard box: the stock solver already
    # overshoots it by about 5e-6. 1e-3 is well clear of that and still fails loudly on a runaway.
    self.assertEqual(mpc.solution_status, 0)
    self.assertLessEqual(mpc.a_solution.max(), 2.0 + 1e-3)
```

- [ ] **Step 3: 테스트를 돌려 5개가 실패하는지 확인한다**

Run:
```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py'
```

Expected: `4 passed, 3 failed, 1 error`

이 계획은 변경 전 빌드에 이 테스트 파일을 실제로 올려 측정했다. 여덟 개의 결과는 다음과 같다.

| 테스트 | 변경 전 | 이유 |
|---|---|---|
| `test_the_split_adds_two_parameters_and_one_residual` | FAIL | `AssertionError: 6 != 8` |
| `test_a_fresh_mpc_starts_neutral` | ERROR | `AttributeError: 'LongitudinalMpc' object has no attribute 'lead_velocity_cost'` |
| `test_shedding_the_equivalence_brakes_less` | FAIL | 속성을 설정해도 솔버가 무시해 두 실행이 같다. `-2.621677558117767 not greater than -2.621677558117767` |
| `test_the_velocity_weight_puts_the_braking_back` | FAIL | 같은 이유, 부등호만 반대 |
| `test_the_approach_solution_is_unchanged` | PASS | 골든 가드 |
| `test_the_steady_solution_is_unchanged` | PASS | 골든 가드 |
| `test_the_top_of_the_offered_range_still_converges` | PASS | `solution_status`만 보므로 속성이 무시돼도 통과한다 |
| `test_no_lead_stays_bounded_by_accel_max` | PASS | 회귀 가드 |

통과하는 넷 중 셋은 변경을 이끄는 red 테스트가 아니라 **회귀 가드**다. 특히 골든 2개는 중립성을 지키는 것이 목적이므로 양쪽에서 통과해야 맞다.

**골든 2개가 반드시 통과해야 한다.** 실패하면 컨테이너의 `long_mpc.py`가 호스트와 다르거나 `.so`가 오래된 것이다. 진행하지 말고 원인을 찾는다.

- [ ] **Step 4: 상수와 모델 파라미터를 늘린다**

`openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py`.

먼저 차원 상수. 변경 전:

```python
X_DIM = 3
U_DIM = 1
PARAM_DIM = 6
COST_E_DIM = 5
COST_DIM = COST_E_DIM + 1
CONSTR_DIM = 4
```

변경 후:

```python
X_DIM = 3
U_DIM = 1
PARAM_DIM = 8
COST_E_DIM = 6
COST_DIM = COST_E_DIM + 1
CONSTR_DIM = 4
```

다음으로 비용 상수. `DANGER_ZONE_COST = 100.` 바로 아래에 두 줄을 넣는다.

변경 전:

```python
A_CHANGE_COST = 200.
DANGER_ZONE_COST = 100.
CRASH_DISTANCE = .25
```

변경 후:

```python
A_CHANGE_COST = 200.
DANGER_ZONE_COST = 100.
# Neutral defaults. LEAD_EQUIV_FACTOR 1.0 keeps the stock gap residual; LEAD_VELOCITY_COST 0.0
# leaves the relative-velocity residual inert. Both are overridden at runtime per instance.
LEAD_VELOCITY_COST = 0.
LEAD_EQUIV_FACTOR = 1.
CRASH_DISTANCE = .25
```

마지막으로 `gen_long_model`의 런타임 파라미터. 변경 전:

```python
  lead_t_follow = SX.sym('lead_t_follow')
  lead_danger_factor = SX.sym('lead_danger_factor')
  model.p = vertcat(a_min, a_max, x_obstacle, a_prev, lead_t_follow, lead_danger_factor)
```

변경 후:

```python
  lead_t_follow = SX.sym('lead_t_follow')
  lead_danger_factor = SX.sym('lead_danger_factor')
  v_lead = SX.sym('v_lead')
  lead_equiv_factor = SX.sym('lead_equiv_factor')
  model.p = vertcat(a_min, a_max, x_obstacle, a_prev, lead_t_follow, lead_danger_factor, v_lead, lead_equiv_factor)
```

- [ ] **Step 5: 비용 잔차를 분리한다**

같은 파일 `gen_long_ocp` 안. 먼저 파라미터 언팩. 변경 전:

```python
  lead_t_follow = ocp.model.p[4]
  lead_danger_factor = ocp.model.p[5]

  ocp.cost.yref = np.zeros((COST_DIM, ))
```

변경 후:

```python
  lead_t_follow = ocp.model.p[4]
  lead_danger_factor = ocp.model.p[5]
  v_lead = ocp.model.p[6]
  lead_equiv_factor = ocp.model.p[7]

  ocp.cost.yref = np.zeros((COST_DIM, ))
```

다음으로 잔차 자체. 변경 전:

```python
  costs = [((x_obstacle - x_ego) - (desired_dist_comfort)) / (v_ego + 10.),
           x_ego,
           v_ego,
           a_ego,
           a_ego - a_prev,
           j_ego]
```

변경 후:

```python
  # The stopping-distance equivalence folds the lead's speed into x_obstacle and ego's into the
  # desired distance. Expanding the residual shows what that leaves: a gap error plus a relative
  # velocity weighted (v_ego + v_lead) / (2 * COMFORT_BRAKE) times as heavily -- 12x at highway
  # speed -- with no way to tune the ratio, because both sit in one residual under one weight.
  # lead_equiv_factor sheds that equivalence from the cost path only; the constraint below keeps
  # the full expression. At 1.0 nothing is shed and this is the stock residual.
  shed = 1. - lead_equiv_factor
  x_obstacle_cost = x_obstacle - shed * get_stopped_equivalence_factor(v_lead)
  desired_dist_cost = desired_dist_comfort - shed * get_stopped_equivalence_factor(v_ego)

  costs = [((x_obstacle_cost - x_ego) - (desired_dist_cost)) / (v_ego + 10.),
           x_ego,
           v_ego,
           a_ego,
           a_ego - a_prev,
           v_ego - v_lead,
           j_ego]
```

새 항의 위치는 임의가 아니다. 4번(`a_ego - a_prev`) 뒤에 두어야 `set_cost_weights`가 하드코딩한 `W[4,4]` 테이퍼가 계속 A_CHANGE_COST를 가리키고, `j_ego` 앞에 두어야 `cost_y_expr_e = vertcat(*costs[:-1])`이 제어 입력만 떨군다.

`get_stopped_equivalence_factor`는 `(v**2) / (2 * COMFORT_BRAKE)`이며 CasADi 심볼에도 그대로 적용된다. 같은 식을 다시 쓰지 말고 이 함수를 호출한다.

마지막으로 파라미터 초기값. 변경 전:

```python
  ocp.parameter_values = np.array([-1.2, 1.2, 0.0, 0.0, get_T_FOLLOW(), LEAD_DANGER_FACTOR])
```

변경 후:

```python
  ocp.parameter_values = np.array([-1.2, 1.2, 0.0, 0.0, get_T_FOLLOW(), LEAD_DANGER_FACTOR,
                                   0.0, LEAD_EQUIV_FACTOR])
```

- [ ] **Step 6: 인스턴스 속성과 런타임 배선을 넣는다**

같은 파일 `LongitudinalMpc`. 먼저 생성자. 변경 전:

```python
  def __init__(self, dt=DT_MDL):
    self.dt = dt
    self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    self.reset()
    self.source = LongitudinalPlanSource.cruise
    self.stop_distance = STOP_DISTANCE
```

변경 후:

```python
  def __init__(self, dt=DT_MDL):
    self.dt = dt
    self.solver = AcadosOcpSolverCython(MODEL_NAME, ACADOS_SOLVER_TYPE, N)
    # Set before reset(): reset() calls set_weights(), which reads lead_velocity_cost.
    self.lead_velocity_cost = LEAD_VELOCITY_COST
    self.lead_equiv_factor = LEAD_EQUIV_FACTOR
    self.reset()
    self.source = LongitudinalPlanSource.cruise
    self.stop_distance = STOP_DISTANCE
```

**순서가 중요하다.** `reset()`이 마지막 줄에서 `self.set_weights()`를 부르고, 그 안에서 `self.lead_velocity_cost`를 읽는다. `reset()` 뒤에 두면 생성자와 솔버 실패 복구 경로(`run()`의 `self.reset()`) 양쪽에서 `AttributeError`가 난다.

다음으로 가중치. 변경 전:

```python
    cost_weights = [X_EGO_OBSTACLE_COST, X_EGO_COST, V_EGO_COST, A_EGO_COST, jerk_factor * a_change_cost, jerk_factor * J_EGO_COST]
```

변경 후:

```python
    cost_weights = [X_EGO_OBSTACLE_COST, X_EGO_COST, V_EGO_COST, A_EGO_COST, jerk_factor * a_change_cost,
                    self.lead_velocity_cost, jerk_factor * J_EGO_COST]
```

모듈 상수가 아니라 인스턴스 속성을 읽는 것이 핵심이다. `longitudinal_planner.py:118`이 매 사이클 `set_weights()`를 불러 이 리스트를 다시 만들므로, 모듈 상수를 런타임에 바꾸는 방식은 여기서 덮인다.

마지막으로 `update()`의 파라미터 채우기. 변경 전:

```python
    self.params[:,4] = t_follow
    self.params[:,5] = LEAD_DANGER_FACTOR

    self.run()
```

변경 후:

```python
    self.params[:,4] = t_follow
    self.params[:,5] = LEAD_DANGER_FACTOR

    # The cost path sheds get_stopped_equivalence_factor(v_lead) from x_obstacle, so v_lead has to
    # be the speed of whichever lead won that node's min. Another lead's speed would shed a
    # quantity that is not in there.
    lead_choice = np.argmin(x_obstacles, axis=1)
    v_leads = np.column_stack([lead_xv_0[:,1], lead_xv_1[:,1]])
    self.params[:,6] = np.take_along_axis(v_leads, lead_choice[:,None], axis=1)[:,0]
    self.params[:,7] = self.lead_equiv_factor

    self.run()
```

- [ ] **Step 7: 솔버를 재빌드한다**

Run:
```bash
docker cp openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py \
  sp-build:/work/openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  scons -j8 openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/'
```

Expected: 마지막 줄이 `scons: done building targets.`이고, 그 앞에 다음 두 줄이 보여야 한다.

```
  [LINK] openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/c_generated_code/libacados_ocp_solver_long.so
  [LINK] openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so
```

CasADi 3.6.7 버전 경고(`Please note that the following versions of CasADi are officially supported`)가 나오는 것은 정상이며 변경 전에도 나온다.

`[LINK]` 줄이 안 보이면 scons가 재빌드를 안 한 것이다. 그대로 진행하면 다음 스텝이 옛 `.so`를 쓴다.

- [ ] **Step 8: 테스트를 돌려 전부 통과하는지 확인한다**

Run:
```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py'
```

Expected: `8 passed`

골든 2개가 여전히 통과하는 것이 이 스텝의 핵심이다. 실측 기준으로 변경 전후 차이는 `a_solution` 최대 `4.5e-10`, `v_solution` 최대 `4.9e-10`이며 허용 오차 `1e-8` 안에 든다.

골든이 실패하면 허용 오차를 늘리지 말고 원인을 찾는다. 중립 기본값이 중립이 아니게 된 것이며, 이 설계 전체가 그 성질 위에 서 있다.

- [ ] **Step 9: 회귀를 돌린다**

Run:
```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/'
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/selfdrive/test/longitudinal_maneuvers/'
```

Expected: 각각 `206 passed`, `4 passed`

첫 번째의 기준선은 `198 passed`이고 Step 2에서 8개를 더했다. 두 번째는 종방향 기동 테스트로, 변경 전후 모두 `4 passed`여야 한다. 컨테이너에서 이 변경을 그대로 적용해 실측한 값이다.

`longitudinal_maneuvers`가 실패하면 되돌린다. 중립 기본값이 스톡 거동을 재현하지 못한다는 뜻이고, 골든 테스트가 잡지 못한 경로가 있다는 뜻이다.

- [ ] **Step 10: 커밋**

```bash
git add openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py \
        openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py
git commit -F - <<'EOF'
feat: split the long MPC's spacing and relative-velocity costs

The main cost residual folds two things into one expression. Expanding it
leaves a gap error plus a relative velocity weighted
(v_ego + v_lead) / (2 * COMFORT_BRAKE) times as heavily -- 12x at highway
speed against a gain of 1 on the gap. One weight scales both, and the
ratio between them is fixed by COMFORT_BRAKE, so neither can be tuned
without moving the other.

Add v_lead and a split factor as OCP parameters and a sixth residual for
v_ego - v_lead. The factor sheds the stopping-distance equivalence from
the cost path; the danger-zone constraint keeps the full expression and
is untouched. The new residual sits before j_ego so the hardcoded W[4,4]
taper still names A_CHANGE_COST and the terminal slice still drops only
the control input.

Ships neutral: factor 1.0 sheds nothing and a zero velocity weight leaves
the residual inert. Solutions match the pre-split solver to 5e-10, which
is regenerated-C rounding rather than a behavior change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
EOF
```

---

### Task 2: 두 조절값을 sunnylink에 노출

**Files:**
- Modify: `openpilot/common/params_keys.h:294-295`
- Create: `openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py`
- Create: `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py`
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py:17`, `:33`, `:81`
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml` (파일 끝)
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui.json` (생성물)
- Modify: `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py` (파일 끝)

**Interfaces:**
- Consumes: 태스크 1의 `LongitudinalMpc.lead_velocity_cost: float`, `LongitudinalMpc.lead_equiv_factor: float`. 컨트롤러는 이 두 속성에 쓰기만 한다.
- Produces:
  - `LongCostTuningController(mpc, params=None)` — `update(sm) -> None` 하나를 공개한다. `sm`은 읽지 않지만 `StopDistanceController.update`와 시그니처를 맞춘다.
  - `LEAD_VELOCITY_COST_MIN = 0.0`, `LEAD_VELOCITY_COST_MAX = 2.0`, `LEAD_EQUIV_FACTOR_MIN = 0.0`, `LEAD_EQUIV_FACTOR_MAX = 1.0` (모듈 상수, `float`)
  - Params 키 `LeadVelocityCost`(기본 `"0.0"`), `LeadEquivFactor`(기본 `"1.0"`)

- [ ] **Step 1: Params 키를 등록한다**

`openpilot/common/params_keys.h`. 변경 전:

```c
    // Stop Distance
    {"StopDistance", {PERSISTENT | BACKUP, FLOAT, "6.0"}},
```

변경 후:

```c
    // Stop Distance
    {"StopDistance", {PERSISTENT | BACKUP, FLOAT, "6.0"}},

    // Longitudinal MPC cost split
    {"LeadVelocityCost", {PERSISTENT | BACKUP, FLOAT, "0.0"}},
    {"LeadEquivFactor", {PERSISTENT | BACKUP, FLOAT, "1.0"}},
```

기본 문자열은 태스크 1의 `LEAD_VELOCITY_COST`/`LEAD_EQUIV_FACTOR`와 같은 값이어야 한다. 다르면 Params를 읽는 순간 중립이 깨진다.

- [ ] **Step 2: 컨트롤러 테스트를 작성한다**

`openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py`를 새로 만든다.

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LEAD_EQUIV_FACTOR, LEAD_VELOCITY_COST
from openpilot.sunnypilot.selfdrive.controls.lib.long_cost_tuning import (LEAD_EQUIV_FACTOR_MAX, LEAD_EQUIV_FACTOR_MIN,
                                                                          LEAD_VELOCITY_COST_MAX,
                                                                          LEAD_VELOCITY_COST_MIN,
                                                                          LongCostTuningController)

TICKS_PER_READ = int(1. / DT_MDL)


class FakeMpc:
  """Stands in for LongitudinalMpc: the controller only ever writes these two."""

  def __init__(self):
    self.lead_velocity_cost = LEAD_VELOCITY_COST
    self.lead_equiv_factor = LEAD_EQUIV_FACTOR


class FakeParams:
  """Params stand-in. Counts reads so the 1 Hz throttle is observable."""

  KEYS = ("LeadVelocityCost", "LeadEquivFactor")

  def __init__(self, cost=0.0, factor=1.0):
    self.values = {"LeadVelocityCost": cost, "LeadEquivFactor": factor}
    self.gets = 0
    self.puts: list[tuple[str, float]] = []

  def get(self, key, return_default=False):
    assert key in self.KEYS, f"unexpected param read: {key}"
    self.gets += 1
    return self.values[key]

  def put(self, key, value, block=False):
    assert key in self.KEYS, f"unexpected param write: {key}"
    self.values[key] = value
    self.puts.append((key, value))


def build(cost=0.0, factor=1.0):
  mpc, params = FakeMpc(), FakeParams(cost, factor)
  return LongCostTuningController(mpc, params), mpc, params


def drive(controller, ticks):
  for _ in range(ticks):
    controller.update({})


class TestParamReading(OpenpilotTestCase):
  def test_in_range_values_reach_the_mpc(self):
    _, mpc, _ = build(cost=0.8, factor=0.4)
    self.assertEqual(mpc.lead_velocity_cost, 0.8)
    self.assertEqual(mpc.lead_equiv_factor, 0.4)

  def test_out_of_range_values_are_clamped_and_written_back(self):
    _, mpc, params = build(cost=9.0, factor=5.0)
    self.assertEqual(mpc.lead_velocity_cost, LEAD_VELOCITY_COST_MAX)
    self.assertEqual(mpc.lead_equiv_factor, LEAD_EQUIV_FACTOR_MAX)
    self.assertEqual(sorted(params.puts), [("LeadEquivFactor", 1.0), ("LeadVelocityCost", 2.0)])

  def test_negative_values_are_clamped_to_the_floor(self):
    _, mpc, params = build(cost=-1.0, factor=-1.0)
    self.assertEqual(mpc.lead_velocity_cost, LEAD_VELOCITY_COST_MIN)
    self.assertEqual(mpc.lead_equiv_factor, LEAD_EQUIV_FACTOR_MIN)
    self.assertEqual(len(params.puts), 2)

  def test_an_in_range_value_is_not_written_back(self):
    _, _, params = build(cost=0.5, factor=0.5)
    self.assertEqual(params.puts, [])

  def test_a_nan_falls_through_to_the_neutral_end_of_each_range(self):
    # The two clamps are written in opposite orders on purpose. Python's min/max return the
    # non-NaN operand, so max(MIN, min(MAX, nan)) yields MAX and min(MAX, max(MIN, nan)) yields
    # MIN. Neutral is 0.0 for the weight and 1.0 for the factor, which are opposite ends.
    _, mpc, _ = build(cost=float("nan"), factor=float("nan"))
    self.assertEqual(mpc.lead_velocity_cost, LEAD_VELOCITY_COST)
    self.assertEqual(mpc.lead_equiv_factor, LEAD_EQUIV_FACTOR)


class TestReadThrottle(OpenpilotTestCase):
  def test_params_are_read_once_a_second(self):
    # Two reads per pass, one per key. The constructor makes a pass at frame 0, and so does the
    # first update(): it reads before it advances the frame.
    controller, _, params = build()
    self.assertEqual(params.gets, 2)

    drive(controller, TICKS_PER_READ)
    self.assertEqual(params.gets, 4)

    drive(controller, TICKS_PER_READ)
    self.assertEqual(params.gets, 6)

  def test_a_mid_second_change_is_not_picked_up_early(self):
    # One tick first, to consume the frame-0 read the constructor already did.
    controller, mpc, params = build(cost=0.0)
    drive(controller, 1)
    params.values["LeadVelocityCost"] = 1.5

    drive(controller, TICKS_PER_READ - 1)
    self.assertEqual(mpc.lead_velocity_cost, 0.0)

    drive(controller, 1)
    self.assertEqual(mpc.lead_velocity_cost, 1.5)
```

- [ ] **Step 3: 테스트를 돌려 임포트가 실패하는지 확인한다**

Run:
```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py'
```

Expected: `1 collection error in 0.30s`, 그 위에 다음 줄이 나온다.

```
ModuleNotFoundError: No module named 'openpilot.sunnypilot.selfdrive.controls.lib.long_cost_tuning'
```

- [ ] **Step 4: 컨트롤러를 작성한다**

`openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py`를 새로 만든다.

```python
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL

# Range offered by the UI. 0.0 is the shipped weight and leaves the relative-velocity residual
# inert. 2.0 is about seven times the weight that reproduces stock braking at 30 m/s once the
# equivalence is fully shed -- measured at 0.27 -- so there is room to tune past parity while
# staying far from the weights where the solver stops converging.
LEAD_VELOCITY_COST_MIN = 0.0
LEAD_VELOCITY_COST_MAX = 2.0

# 1.0 keeps the stock gap residual; 0.0 sheds the stopping-distance equivalence from the cost.
LEAD_EQUIV_FACTOR_MIN = 0.0
LEAD_EQUIV_FACTOR_MAX = 1.0


class LongCostTuningController:
  """Feeds the MPC's spacing/velocity split from Params, in the shape StopDistanceController uses."""

  def __init__(self, mpc, params=None):
    self._mpc = mpc
    self._params = params or Params()
    self._frame = 0
    self._read_params()

  def _read_params(self) -> None:
    if self._frame % int(1. / DT_MDL) != 0:
      return

    # Argument order differs between the two on purpose. Python's min/max return the non-NaN
    # operand, so max(MIN, min(MAX, nan)) lands on MAX and min(MAX, max(MIN, nan)) lands on MIN.
    # Neutral is the floor for the weight and the ceiling for the factor, so a NaN param has to
    # fall through in opposite directions to leave the solver where it shipped.
    cost = float(self._params.get("LeadVelocityCost", return_default=True))
    clipped_cost = min(LEAD_VELOCITY_COST_MAX, max(LEAD_VELOCITY_COST_MIN, cost))
    if clipped_cost != cost:
      self._params.put("LeadVelocityCost", clipped_cost, block=True)

    factor = float(self._params.get("LeadEquivFactor", return_default=True))
    clipped_factor = max(LEAD_EQUIV_FACTOR_MIN, min(LEAD_EQUIV_FACTOR_MAX, factor))
    if clipped_factor != factor:
      self._params.put("LeadEquivFactor", clipped_factor, block=True)

    self._mpc.lead_velocity_cost = clipped_cost
    self._mpc.lead_equiv_factor = clipped_factor

  def update(self, sm) -> None:
    self._read_params()
    self._frame += 1
```

`long_mpc`에서 아무것도 임포트하지 않는다. 중립값은 이 컨트롤러가 아니라 태스크 1의 모듈 상수가 정의하고, 테스트는 그쪽에서 직접 가져온다. 여기서 임포트하면 `ruff` F401이 걸린다 (확인함).

**주의:** `clipped != value` 비교는 NaN에서 항상 참이므로, NaN 파라미터는 매번 되쓰기가 일어난다. 이는 의도한 동작이다. 첫 되쓰기가 유효한 값을 남기므로 다음 초부터는 정상이다.

- [ ] **Step 5: 테스트를 돌려 통과하는지 확인한다**

Run:
```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py'
```

Expected: `7 passed`

이 일곱 개는 계획을 쓰면서 실제 컨트롤러와 함께 컨테이너에서 돌려 확인했다. `TestReadThrottle`의 두 테스트는 첫 `update()`가 프레임을 올리기 전에 읽으므로 생성자의 frame 0 읽기를 한 번 더 한다는 사실에 의존한다. 이 성질을 놓치면 두 테스트 모두 어긋난다.

- [ ] **Step 6: 플래너에 배선한다**

`openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` 세 곳.

임포트. `stop_distance` 임포트 바로 위에 넣어 알파벳 순서를 지킨다. 변경 전:

```python
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import StopDistanceController
```

변경 후:

```python
from openpilot.sunnypilot.selfdrive.controls.lib.long_cost_tuning import LongCostTuningController
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import StopDistanceController
```

생성. 변경 전:

```python
    self.stop_distance = StopDistanceController(mpc)
```

변경 후:

```python
    self.stop_distance = StopDistanceController(mpc)
    self.long_cost_tuning = LongCostTuningController(mpc)
```

구동. 변경 전:

```python
    self.dec.update(sm)
    self.stop_distance.update(sm)
```

변경 후:

```python
    self.dec.update(sm)
    self.stop_distance.update(sm)
    self.long_cost_tuning.update(sm)
```

- [ ] **Step 7: sunnylink 위젯을 추가하고 컴파일한다**

`openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml` 맨 끝에 섹션 하나를 덧붙인다. 파일은 137행이고 `advanced_services` 섹션의 `QuickBootToggle` 아이템으로 끝난다. 들여쓰기를 기존 섹션과 맞춘다.

```yaml
- id: longitudinal_tuning
  title: Longitudinal Tuning
  description: Cost weights inside the longitudinal MPC. Defaults reproduce stock behavior.
  items:
  - key: LeadEquivFactor
    widget: option
    title: Spacing Cost Split
    description: How much of the lead's stopping distance stays folded into the gap cost. At
      1.0 the cost weights relative velocity about twelve times as heavily as the gap error at
      highway speed, which is stock. Lower values shed that and leave a plain gap error.
    details: The gap cost and the relative-velocity cost share one expression, so their ratio
      is fixed by physics and grows with speed. This separates them. Lower it only together
      with Lead Velocity Cost, which is what puts the damping back.
    min: 0.0
    max: 1.0
    step: 0.1
    enablement:
    - $ref: '#/macros/longitudinal'
  - key: LeadVelocityCost
    widget: option
    title: Lead Velocity Cost
    description: Weight on the difference between your speed and the lead's. Zero is stock.
      Raising it damps closing speed independently of the gap, at a gain that does not grow
      with speed.
    details: Only useful once Spacing Cost Split is below 1.0, which is what makes room for
      it. Around 0.27 reproduces stock braking at 108 km/h with the split fully applied.
    min: 0.0
    max: 2.0
    step: 0.1
    enablement:
    - $ref: '#/macros/longitudinal'
```

그 다음 컴파일한다.

Run:
```bash
docker cp openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml \
  sp-build:/work/openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py'
docker cp sp-build:/work/openpilot/sunnypilot/sunnylink/settings_ui.json \
  openpilot/sunnypilot/sunnylink/settings_ui.json
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py --check'
```

Expected: 마지막 명령이 `--check: /work/openpilot/sunnypilot/sunnylink/settings_ui.json matches compiled output`를 출력하고 종료 코드 0.

`settings_ui.json`은 손으로 고치지 않는다. 컨테이너에서 생성해 호스트로 되가져온다.

- [ ] **Step 8: sunnylink 가드 테스트를 추가한다**

`openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py` 맨 끝에 덧붙인다. 이 파일의 기존 테스트들이 `schema` 픽스처를 받는 형태이므로 같은 형태를 쓴다.

이 파일은 `schema` 픽스처(`:154`, `generate_schema()`를 부른다)와 `_find_item(schema, key)`(`:69`), `_find_section(schema, panel_id, section_id)`(`:76`) 헬퍼를 이미 갖고 있고, `self.assertX`가 아니라 맨 `assert`를 쓴다. 새 임포트나 헬퍼를 만들지 말고 그대로 쓴다.

```python


class TestLongitudinalCostSplit(OpenpilotTestCase):
  def test_the_cost_split_widgets_are_present(self, schema):
    for key in ("LeadEquivFactor", "LeadVelocityCost"):
      item = _find_item(schema, key)
      assert item is not None, f"{key} missing from settings_ui schema"
      assert item.get("widget") == "option"

  def test_the_cost_split_widgets_reach_the_neutral_defaults(self, schema):
    """Neutral is the factor's ceiling and the weight's floor. A range that excluded either
    would leave stock behavior unreachable from the UI."""
    factor = _find_item(schema, "LeadEquivFactor")
    assert factor is not None
    assert factor.get("min") == 0.0 and factor.get("max") == 1.0

    cost = _find_item(schema, "LeadVelocityCost")
    assert cost is not None
    assert cost.get("min") == 0.0 and cost.get("max") == 2.0

  def test_the_widgets_live_on_the_developer_page(self, schema):
    """Cost-function coefficients, not a user setting: a wrong value changes longitudinal
    behavior at once, so they do not belong on the cruise page next to Stop Distance."""
    section = _find_section(schema, "developer", "longitudinal_tuning")
    assert section is not None, "longitudinal_tuning section missing from the developer panel"
    assert {item["key"] for item in section.get("items", [])} == {"LeadEquivFactor", "LeadVelocityCost"}
```

Run:
```bash
docker cp openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py \
  sp-build:/work/openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/sunnylink/tests/'
```

Expected: `113 passed`

기준선은 `110 passed`이고 위에서 3개를 더했다.

- [ ] **Step 9: 전체 회귀를 돌린다**

Run:
```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/'
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/selfdrive/test/longitudinal_maneuvers/'
```

Expected: 각각 `213 passed`, `4 passed`

첫 번째는 태스크 1 종료 시점의 `206 passed`에 이 태스크의 컨트롤러 테스트 7개를 더한 값이다. 두 번째는 변하지 않아야 한다. 플래너 배선은 중립 기본값을 쓰므로 기동 결과에 영향이 없다.

`params_keys.h`를 바꿨지만 이 테스트들은 `FakeParams`를 쓰므로 C++ 재컴파일 없이 통과한다. 실기기에는 재컴파일이 필요하다.

- [ ] **Step 10: 커밋**

```bash
git add openpilot/common/params_keys.h \
        openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py \
        openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_tuning.py \
        openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py \
        openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml \
        openpilot/sunnypilot/sunnylink/settings_ui.json \
        openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py
git commit -F - <<'EOF'
feat: expose the longitudinal cost split over sunnylink

The split factor and the relative-velocity weight are both runtime values
-- one is an OCP parameter written every cycle, the other a cost weight
rebuilt every cycle -- so they can be tuned on the road without
regenerating the acados solver. Only the parameter and cost dimensions
are fixed at codegen time.

Read them from Params at 1 Hz and write them onto the MPC instance, the
shape StopDistanceController already uses. They have to be instance
attributes rather than module constants: the stock planner calls
set_weights() every cycle and rebuilds the weight list from scratch.

The two clamps are written in opposite orders so a NaN param falls
through to the neutral end of each range -- the floor for the weight, the
ceiling for the factor.

Put the widgets on the developer page. These are cost-function
coefficients, and a wrong value changes longitudinal behavior at once.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UmDw2rkYwMzrJCtYfjEHCX
EOF
```

---

## 실차 적용 전 게이트

레포 커밋과 별개로, 기기에 올리기 전에 다음을 통과해야 한다.

1. **기기 재빌드.** acados 생성 C와 `params_keys.h`가 모두 재컴파일을 부른다. 이 계획의 검증은 컨테이너에서만 이뤄졌다.
2. **중립 확인.** 기기에 올린 직후 두 값이 각각 `0.0`, `1.0`인지 확인한다. 중립이면 거동이 지금과 같아야 하며, 달라진다면 재빌드가 제대로 안 된 것이다.
3. **오프라인 기동 비교.** `selfdrive/test/longitudinal_maneuvers/`로 `f = 0`의 몇 가지 `LeadVelocityCost` 값을 비교한다. 어느 기동이 accordion과 간격 벌어짐을 드러내는지는 실제로 돌려보고 정한다. 이 계획은 그 값을 추측해 적지 않는다.

3번을 건너뛰고 실차로 가지 않는다. 중립으로 출하하므로 급할 이유가 없다.

## 튜닝 출발점

실측된 등가점은 `f = 0`, `LeadVelocityCost = 0.27`이다. 30 m/s 접근 시나리오에서 최대 감속 −2.6228(현재 −2.6217), 10초 후 속도 19.083(현재 19.127)으로 현재 거동을 거의 그대로 재현한다.

여기서 두 방향으로 움직인다.

- **간격이 과하게 벌어지는 증상:** `LeadVelocityCost`를 0.27보다 낮춘다. 상대속도 비중이 줄어 간격 오차가 상대적으로 강해진다.
- **accordion:** `LeadVelocityCost`를 0.27보다 올린다. 실측 상한 2.0에서도 솔버는 수렴했다.

`f`는 0과 1 사이 중간값도 쓸 수 있다. 0.5는 현재 감쇠의 절반을 남기고 나머지를 `LeadVelocityCost`로 대체한다는 뜻이다.

`f = 1`에서는 `LeadVelocityCost`를 올려도 감쇠를 더하기만 할 수 있다(실측: `f=1, c=1.0`에서 최대 감속 −2.9783). 줄이려면 반드시 `f`를 내려야 한다.

## Rollback

설계서 Rollback 절과 같다. 단계적으로 되돌린다.

1. **값만 되돌린다.** sunnylink에서 `LeadEquivFactor = 1.0`, `LeadVelocityCost = 0.0`. 재빌드 없이 즉시 현재 거동으로 복귀한다. 중립 기본값을 설계한 이유가 이것이다.
2. **범위를 좁힌다.** `developer.yaml`의 `LeadVelocityCost` 상한을 낮춘다. YAML과 `compile_settings_ui.py`만 다시 돌리면 되고 코드는 그대로다.
3. **구조를 되돌린다.** 태스크 1을 revert하고 솔버를 재빌드한다.

## 실차 관찰 항목

- 정속 추종에서 간격이 설정 personality에 맞게 수렴하는가.
- 앞차 속도 변화에 대한 가감속 반복이 줄었는가.
- 리드가 없는 구간에서 이상 가속이 없는가. 설계상 `ACCEL_MAX`와 플래너의 `min()`이 이중으로 막지만, 실측은 정지 상태 출발과 저속 구간에서만 확인되지 않았다.
- `cloudlog`에 `Long mpc reset, solution_status:`가 뜨는가. 뜨면 그 시점의 두 파라미터 값을 기록하고 즉시 중립으로 되돌린다.
