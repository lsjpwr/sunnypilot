# 종방향 MPC 간격/상대속도 비용 분리 (Spacing–Velocity Cost Split)

**Goal:** 종방향 MPC의 비용 함수에서 한 잔차에 뭉쳐 있는 "간격 오차"와 "상대속도"를 분리해, 둘을 독립적으로 가중할 수 있게 한다. 분리 비율과 상대속도 가중치는 런타임 값으로 두어 sunnylink에서 조절한다.

**Architecture:** acados OCP의 파라미터를 2개(`v_lead`, `equiv_factor`) 늘리고, 비용 잔차 벡터에 `v_ego - v_lead` 항을 하나 추가한다. 안전 제약(`con_h_expr`)은 손대지 않는다. 기본값은 현재 거동과 수치적으로 동일한 지점에 둔다.

**Tech Stack:** Python 3.11, CasADi/acados (NONLINEAR_LS, Gauss-Newton, HPIPM), `numpy`, stdlib `unittest` via `tools/test_runner.py`. 신규 런타임 의존성 없음.

---

## 배경: 왜 지금은 못 만지는가

MPC의 주 비용 잔차는 `long_mpc.py:157`의 한 줄이다.

```python
((x_obstacle - x_ego) - desired_dist_comfort) / (v_ego + 10.)
```

`x_obstacle`과 `desired_dist_comfort`는 각각 이렇게 정의된다.

```python
x_obstacle           = x_lead + v_lead**2 / (2 * COMFORT_BRAKE)      # update() + get_stopped_equivalence_factor
desired_dist_comfort = v_ego**2 / (2 * COMFORT_BRAKE) + t_follow * v_ego + STOP_DISTANCE
```

두 제곱항이 만나면 상대속도 항이 떨어져 나온다.

```
(x_obstacle − x_ego) − desired_dist_comfort
  = (x_lead − x_ego) − t_follow·v_ego − STOP_DISTANCE     ← 간격 오차
    − (v_ego − v_lead)(v_ego + v_lead) / (2·COMFORT_BRAKE) ← 상대속도
```

즉 **상대속도 항은 이미 존재한다.** 없어서 못 쓰는 것이 아니다. 문제는 두 가지다.

첫째, **이득이 물리상수에 고정되어 있다.** 상대속도의 계수는 `(v_ego + v_lead) / (2·COMFORT_BRAKE)`이고 `COMFORT_BRAKE = 2.5`는 상수다. 간격 오차의 계수는 1이다. 둘의 비율을 바꿀 방법이 없다.

둘째, **그 이득이 속도에 비례해 커진다.**

| 속도 (v_ego = v_lead) | 상대속도 이득 | 간격 오차 이득 |
|---|---|---|
| 36 km/h (10 m/s) | 4 | 1 |
| 72 km/h (20 m/s) | 8 | 1 |
| 108 km/h (30 m/s) | 12 | 1 |
| 130 km/h (36.1 m/s) | 14.4 | 1 |

고속도로에서는 상대속도가 간격 오차보다 12배 이상 무겁다. 두 항이 잔차 하나에 들어 있으므로 `X_EGO_OBSTACLE_COST = 3` 하나가 둘을 함께 스케일한다.

이 배치가 관측된 증상 두 가지를 함께 설명한다. 정속 추종에서 간격이 설정보다 크게 벌어진 채 잘 수렴하지 않는 것은 고속에서 간격 오차의 상대 비중이 1/12로 떨어지기 때문이고, 앞차 속도 변화에 대한 과민 반응은 같은 이유의 뒷면이다. **어느 쪽을 고치든 두 항을 먼저 떼어놓아야 한다.**

### 왜 항을 더하는 것만으로는 안 되는가

`(v_ego - v_lead)` 잔차를 하나 덧붙이고 기존 잔차를 그대로 두는 방법을 먼저 검토했고, 기각했다. NONLINEAR_LS의 가중치 행렬 `W`는 Gauss-Newton 헤시안 `JᵀWJ`에 들어가므로 양의 준정부호여야 하고, 따라서 **음수 가중치를 줄 수 없다.** 항을 더하면 상대속도 감쇠를 키우는 것만 가능하고 줄이는 것은 불가능하다. 간격이 벌어지는 증상은 이득을 낮춰야 고쳐지므로, 덧셈만으로는 목표의 절반이 구조적으로 닿지 않는다.

기존 잔차에서 상대속도 성분을 덜어내는 경로가 반드시 필요하다.

---

## Non-goals

- 안전 제약을 바꾸지 않는다. `con_h_expr`(`long_mpc.py:169-173`)는 계속 원래의 `x_obstacle`과 `desired_dist_comfort`를 쓴다. 위험구간 방어선은 이 변경과 무관하게 유지된다.
- `COMFORT_BRAKE`, `STOP_DISTANCE`, `T_FOLLOW`, `A_CHANGE_COST`, `J_EGO_COST`, `X_EGO_OBSTACLE_COST` 등 기존 상수를 바꾸지 않는다.
- 튜닝된 값을 기본값으로 출하하지 않는다. 이 문서의 범위는 조절 가능한 구조를 만드는 것까지다.
- MPC의 상태·제어 차원(`X_DIM`, `U_DIM`)을 바꾸지 않는다.
- 새 cereal 필드나 UI 페이지를 만들지 않는다.

---

## 설계

### 파라미터 배치

`PARAM_DIM`을 6에서 8로 늘린다.

```python
p = [a_min, a_max, x_obstacle, a_prev, lead_t_follow, lead_danger_factor,
     v_lead,        # 6 (신규) 해당 노드에서 선택된 리드의 속도
     equiv_factor]  # 7 (신규) 분리 비율 f, 0.0~1.0
```

비용 전용 장애물 위치를 별도 파라미터로 넘기지 않는다. `x_obstacle`, `v_lead`, `equiv_factor`가 모두 파라미터이므로 필요한 값은 기호적으로 유도된다.

```python
shed_lead = (1 - equiv_factor) * v_lead**2 / (2 * COMFORT_BRAKE)
shed_ego  = (1 - equiv_factor) * v_ego**2  / (2 * COMFORT_BRAKE)

costs[0] = ((x_obstacle - shed_lead - x_ego) - (desired_dist_comfort - shed_ego)) / (v_ego + 10.)
```

`x_obstacle`에서 빼는 형태를 택한 이유가 있다. `update()`가 `params[:,2]`에 `(STOP_DISTANCE - self.stop_distance)` 시프트를 이미 적용하는데(`long_mpc.py:333`, `StopDistanceController`가 쓰는 경로), 비용용 위치를 따로 계산하면 그 시프트를 두 곳에서 일치시켜야 한다. 빼기 형태는 시프트를 자동으로 물려받는다.

`f = 1`이면 `shed_lead`와 `shed_ego`가 모두 정확히 `0.0`이 되어 잔차의 값이 현재 식과 같아진다. `f = 0`이면 두 제곱항이 완전히 사라져 잔차는 순수 간격 오차가 된다.

### 비용 벡터 배치

`COST_DIM`을 6에서 7로, `COST_E_DIM`을 5에서 6으로 늘린다. 새 항은 5번, `j_ego` **앞**에 넣는다.

```
0 dist   1 x_ego   2 v_ego   3 a_ego   4 a_ego-a_prev   5 v_ego-v_lead   6 j_ego
```

이 위치는 임의가 아니다.

- `set_cost_weights`(`long_mpc.py:249-258`)는 `W[4,4]`를 하드코딩해 A_CHANGE_COST에 시간 테이퍼를 건다. 새 항을 4번 뒤에 넣으면 이 인덱스가 그대로 유효하다.
- `cost_y_expr_e = vertcat(*costs[:-1])`은 마지막 항을 떨궈 종단 노드 비용을 만든다. 제어 입력인 `j_ego`가 마지막에 남아 있어야 이 슬라이싱이 계속 옳다. `v_ego - v_lead`는 상태와 파라미터만의 함수이므로 종단 노드에 남아도 문제가 없다.
- `reset()`의 `np.zeros((N+1, COST_DIM))`, `yref[N][:COST_E_DIM]`, `W[:COST_E_DIM, :COST_E_DIM]`는 모두 상수 기반이라 자동으로 따라온다.

결과적으로 손댈 곳은 상수 3개(`PARAM_DIM`, `COST_DIM`, `COST_E_DIM`)와 명시적 리스트 2개(`gen_long_ocp`의 `costs`, `set_weights`의 `cost_weights`)뿐이다.

### 상대속도 잔차를 정규화하지 않는 이유

0번 잔차는 `(v_ego + 10.)`으로 나눈다. 거리 오차를 속도에 대해 무디게 만들어, 저속에서 같은 미터 오차가 과도한 비용이 되지 않게 하려는 정규화다.

5번 잔차에는 이 정규화를 적용하지 않는다. 속도 의존 이득을 **없애는 것**이 이 설계의 목적이기 때문이다. 정규화를 붙이면 `1/(v+10)`이라는 새로운 속도 의존성이 생겨, 방향만 다를 뿐 같은 문제로 돌아간다.

### 리드 선택

`x_obstacle`은 노드마다 lead0/lead1 중 작은 쪽을 고른다(`long_mpc.py:323`). `v_lead`도 **같은 노드에서 같은 쪽**을 골라야 `shed_lead` 뺄셈이 성립한다. 다른 리드의 속도를 쓰면 빼는 양이 `x_obstacle`에 실제로 들어 있는 양과 달라진다.

```python
sel = np.argmin(x_obstacles, axis=1)
v_leads = np.column_stack([lead_xv_0[:,1], lead_xv_1[:,1]])
self.params[:,6] = np.take_along_axis(v_leads, sel[:,None], axis=1)[:,0]
self.params[:,7] = self.lead_equiv_factor
```

`self.source`가 이미 `np.argmin(x_obstacles[0])`로 같은 선택을 하고 있으므로(`long_mpc.py:324`), 노드 0에서 두 선택은 일치한다.

### 중립 기본값

`equiv_factor = 1.0`, `LeadVelocityCost = 0.0`을 기본값으로 한다. 이 지점에서 솔버 거동은 현재와 **수치적으로 동일**하며, 근거는 셋이다.

1. `f = 1`이면 `1 - equiv_factor`가 정확히 `0.0`이고, `0.0 * x`는 유한한 `x`에 대해 정확히 `0.0`이다. 뺄셈이 피감수를 바꾸지 않는다.
2. `W[5,5] = 0`이면 Gauss-Newton 헤시안 `JᵀWJ`에서 해당 행의 기여가 정확히 `0.0`이고, 비용 `½(y−yref)ᵀW(y−yref)`에서도 마찬가지다. QP의 상태·제어 차원은 변하지 않으므로 HPIPM이 푸는 문제 자체가 동일하다.
3. 위 둘은 `v_lead`가 유한할 때만 성립한다. `process_lead`(`long_mpc.py:306`)가 `v_lead`를 `[0, 1e8]`로 클립하므로 inf/nan이 들어올 경로가 없다.

실측으로 확인했다. 두 시나리오(v_ego 30 m/s, d_rel 60 m에 v_lead 20 / 30 m/s, 5틱)에서 변경 전후 `a_solution`의 최대 차이는 `4.5e-10`, `v_solution`은 `4.9e-10`이었다. 비트 단위로 같지는 않은데, 생성된 C가 값은 같아도 다른 식 트리로 계산하고 그 차이가 HPIPM 반복에 실린 결과다. 실제 거동 차이(아래 분리 측정에서 가장 작은 것이 0.16 m/s^2)보다 여덟 자릿수 아래이므로 무시할 수 있고, 테스트는 `atol=1e-8`로 잡는다.

**튜닝된 값을 기본으로 출하하지 않는다.** 오프라인 검증 없이 정한 값을 기본에 박으면 되돌릴 기준선이 사라진다. 중립으로 내보내고, 값은 도로에서 sunnylink로 찾는다. 이것이 두 값을 런타임 파라미터로 만든 이유 전체다.

### 리드가 없을 때

`process_lead`는 리드가 없으면 `v_lead = v_ego + 10.0`인 가짜 리드를 만든다. `LeadVelocityCost > 0`이면 5번 잔차가 `-10`이 되어 가속을 밀어낸다.

**별도 게이팅을 넣지 않는다.** `longitudinal_planner.py:148-153`이 후보들 중 가장 작은 가속도를 `min()`으로 고르므로, MPC가 과가속을 원해도 `a_cruise`가 항상 이긴다. 리드가 없을 때의 MPC 과가속은 이 변경 이전에도 이미 같은 방식으로 가려져 있었다.

가중치를 리드 유무로 게이팅하는 대안은 기각했다. `set_cost_weights`가 N번의 `cost_set` 호출을 도는데 이를 매 사이클 한 번 더 돌게 되고, 노드별로 다른 리드가 선택될 수 있어 "리드 유무"라는 단일 불린이 실제 상태를 대표하지 못한다.

이 가정은 테스트로 못 박는다. 아래 Safety 절 참조.

### 런타임 배선

두 값은 **mpc 인스턴스 속성**이어야 한다. 순정 `longitudinal_planner.py:118`이 매 사이클 `set_weights()`를 부르고, 그 안에서 모듈 상수로 `cost_weights` 리스트를 다시 만들기 때문이다. 모듈 상수를 런타임에 바꾸는 방식은 이 재구성에 덮인다.

배선은 `StopDistanceController`(`sunnypilot/selfdrive/controls/lib/stop_distance.py`)를 그대로 따른다. 같은 문제를 이미 같은 방식으로 푼 선례다.

- Params를 1 Hz로만 읽는다 (`self._frame % int(1. / DT_MDL) != 0`).
- 범위 밖 값은 클램프하고 되쓴다. 인자 순서는 NaN이 안전한 쪽으로 떨어지도록 잡는다.
- `update(sm)`에서 mpc 속성을 갱신한다.

### sunnylink 노출

`developer.yaml`에 `longitudinal_tuning` 섹션을 새로 만들고 `option` 위젯 2개를 넣는다. 일반 사용자 페이지가 아닌 개발자 페이지를 택한 이유는, 이 값들이 MPC 비용 함수 내부 계수라 잘못 넣으면 종방향 거동이 즉시 바뀌기 때문이다.

| Params 키 | 범위 | step | 기본 | 의미 |
|---|---|---|---|---|
| `LeadVelocityCost` | 0.0 ~ 2.0 | 0.1 | 0.0 | 5번 잔차의 가중치 |
| `LeadEquivFactor` | 0.0 ~ 1.0 | 0.1 | 1.0 | 분리 비율 f. 1.0이 현재 거동 |

`LeadVelocityCost`의 상한 2.0은 다음 계산에서 나왔다. 30 m/s에서 현재의 상대속도 감쇠를 `f = 0` 상태에서 복원하려면, `W₀·(12·Δv / 40)² = W₅·Δv²`에서 `W₅ ≈ 3 · 0.09 ≈ 0.27`이 필요하다. 상한 2.0은 그 약 7배로 공격적인 튜닝까지 여유가 있고, 솔버가 `solution_status != 0`으로 발산하기 시작하는 대역(경험적으로 1000 이상)에서는 멀다.

10 m/s에서의 복원값은 `3 · (4/20)² ≈ 0.12`로 다르다. 하나의 상수가 두 속도의 현재 감쇠를 동시에 재현할 수는 없는데, 이는 결함이 아니라 목적이다. 속도 의존 이득을 속도 무관 상수로 바꾸는 것이 이 설계다.

두 키는 `common/params_keys.h`에 `{PERSISTENT | BACKUP, FLOAT, "<기본값>"}`으로 등록한다. `StopDistance`(`params_keys.h:295`)와 같은 형태다.

---

## 파일별 변경

| 파일 | 변경 | 태스크 |
|---|---|---|
| `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` | 상수 3개, `gen_long_model`의 `model.p`, `gen_long_ocp`의 `costs`·`parameter_values`, `LongitudinalMpc.__init__`의 속성 2개, `set_weights`의 리스트, `update`의 리드 선택 | 1 |
| `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_long_cost_split.py` | 신규. 골든 중립성 + 분리 거동 | 1 |
| `openpilot/common/params_keys.h` | 키 2개 등록 | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py` | 신규. `LongCostTuningController` | 2 |
| `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` | import 1줄, 생성 1줄, `update` 1줄 | 2 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/developer.yaml` | 섹션 1개, 아이템 2개 | 2 |
| `openpilot/sunnypilot/sunnylink/settings_ui.json` | `compile_settings_ui.py` 산출물 | 2 |
| `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py` | 위젯 존재·범위 가드 | 2 |

태스크 1만 마쳐도 거동은 현재와 동일하다. 조절 수단이 없을 뿐이므로 안전하게 멈출 수 있는 경계다.

---

## Safety

### 증명되는 것

- **중립성.** `f = 1`, `LeadVelocityCost = 0`에서 솔버 출력이 변경 전과 같다. 변경 전 빌드에서 고정 시나리오의 `a_solution`/`v_solution`을 골든으로 기록하고, 변경 후 재현을 단언한다. 이것이 이 설계의 핵심 주장이며 테스트 하나로 못 박힌다.
- **안전 제약 불변.** `con_h_expr`가 참조하는 `x_obstacle`(`p[2]`)과 `desired_dist_comfort`는 손대지 않는다. `f`와 `LeadVelocityCost`가 어떤 값이어도 위험구간 슬랙 제약은 변경 전과 같다.
- **리드 없을 때의 과가속 차단.** `LeadVelocityCost > 0`, 리드 없음 조건에서 플래너의 `min()`이 `a_cruise`를 고르는지 테스트한다.

### 미검증 위험

1. **솔버 수렴.** 잔차가 하나 늘면 Gauss-Newton 헤시안의 조건수가 바뀐다. `qp_solver_iter_max = 10`, `qp_tol = 1e-3`으로 반복이 빡빡하게 묶여 있어, 특정 가중치 조합에서 `solution_status != 0` → `self.reset()` 경로를 탈 수 있다. 중립 기본값에서는 헤시안 기여가 0이라 영향이 없지만, 튜닝 구간에서는 미지수다. 실차 전 오프라인 기동 테스트로 확인한다.

2. **교차항 소실.** 현재 비용은 `W₀·[간격 + 12·Δv]²`이라 간격과 상대속도의 교차항을 포함한다. `f < 1`로 가면 `W₀·간격² + W₅·Δv²`가 되어 이 교차항이 사라진다. 의도한 변경이지만, 교차항이 실제 주행에서 어떤 역할을 했는지는 측정된 바 없다. 되돌릴 기준선은 `f = 1`뿐이다.

3. **기기 재빌드.** acados 생성 C 코드가 바뀐다(`SConscript:57`의 `source_list`에 `long_mpc.py`가 있어 `python3 long_mpc.py`가 재실행된다). `params_keys.h` 변경도 재컴파일을 부른다. 1회 비용이고 이후 튜닝은 재빌드가 없지만, 배포 시점에 이 비용이 든다.

### 검증 순서

1. 컨테이너에서 acados 재생성이 성공하는지
2. 골든 중립성 테스트 통과
3. `sunnypilot/selfdrive/controls/lib/` 전체 회귀
4. `selfdrive/test/longitudinal_maneuvers/`로 `f`와 `LeadVelocityCost` 몇 조합의 오프라인 기동 비교
5. 그 다음에야 실차

4번을 건너뛰고 실차로 가지 않는다. 중립 기본값으로 출하하므로 급할 이유가 없다.

---

## Rollback

단계적으로 되돌린다.

1. **값만 되돌린다.** sunnylink에서 `LeadEquivFactor = 1.0`, `LeadVelocityCost = 0.0`. 재빌드 없이 즉시 현재 거동으로 복귀한다. 이것이 중립 기본값을 설계한 이유다.
2. **범위를 좁힌다.** `LeadVelocityCost`의 상한을 낮춰 오설정 여지를 줄인다. 위젯 YAML만 바꾸면 되고 코드는 그대로다.
3. **구조를 되돌린다.** 태스크 1을 revert한다. 재빌드가 필요하다.

1단계가 실차에서 즉시 가능하다는 점이 이 설계의 안전 여유 전부다.

---

## Open items

- **오프라인 기동 시나리오를 아직 고르지 않았다.** `selfdrive/test/longitudinal_maneuvers/`의 기존 기동 중 어느 것이 accordion과 간격 벌어짐을 드러내는지는 태스크 1 완료 후 실제로 돌려보며 정한다. 지금 추측으로 적으면 계획서에 검증 안 된 값이 들어간다.
- **튜닝 시작값.** `f = 0`, `LeadVelocityCost = 0.27`이 30 m/s 접근 시나리오에서 현재 거동을 거의 그대로 재현한다(실측: 최대 감속 -2.6228 대 현재 -2.6217, 10초 후 속도 19.083 대 19.127). 계산으로 얻은 0.27이 측정에서 맞아떨어졌으므로 이 값을 튜닝 출발점으로 쓴다. 다만 이는 30 m/s에서의 등가점이고, 저속에서는 같은 값이 현재보다 약한 감쇠가 된다.
