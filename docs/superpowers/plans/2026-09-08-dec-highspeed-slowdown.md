# DEC 고속 구간 감속 감지 (High-Speed Slowdown Detection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DEC의 감속 감지 거리 곡선을 60 km/h에서 150 km/h까지 연장해, 고속도로 급정체에서 모델의 감속 계획이 blended 모드 전환으로 이어지게 한다.

**Architecture:** `WMACConstants.SLOW_DOWN_BP` / `SLOW_DOWN_DIST` 두 배열에 브레이크포인트 9개를 덧붙인다. 판정 로직, Kalman 필터 파라미터, `speed_factor` 증폭기, 임계값은 전부 그대로 둔다. 60 km/h 이하 구간 값은 한 글자도 바뀌지 않으므로 도심 거동은 불변이다.

**Tech Stack:** Python 3.11 (디바이스), `numpy.interp`, stdlib `unittest` via `tools/test_runner.py`. 신규 런타임 의존성 없음.

**설계 문서:** `docs/superpowers/specs/2026-09-08-dec-highspeed-slowdown-design.md` (commit `97d1591f3`)

## Global Constraints

- 코드·주석·커밋 메시지는 영어. 이 계획서와 설계서만 한국어.
- Python 들여쓰기 2칸 (레포 기존 규약).
- 테스트 프레임워크는 stdlib `unittest`. pytest는 레포 의존성이 아니다. 실행은 `python tools/test_runner.py <경로>`.
- 최종 배열 값은 다음과 정확히 일치해야 한다. 반올림하거나 자리수를 바꾸지 않는다.
  - `SLOW_DOWN_BP = [0., 10., 20., 30., 40., 50., 55., 60., 70., 80., 90., 100., 110., 120., 130., 140., 150.]`
  - `SLOW_DOWN_DIST = [32., 46., 64., 86., 108., 130., 145., 165., 190., 215., 240., 265., 290., 315., 340., 365., 390.]`
- 앞의 8개 원소(0~60 km/h)는 변경 전과 비트 단위로 동일해야 한다.
- `dec.py`는 건드리지 않는다. `speed_factor`, `SLOW_DOWN_PROB`, `SLOW_DOWN_WINDOW_SIZE`, 필터 파라미터 모두 불변.
- 신규 파라미터·UI·cereal 필드를 추가하지 않는다.
- 이 계획은 디바이스에 올리는 것까지가 아니라 레포 커밋까지다. 실차 적용은 아래 "실차 적용 전 게이트"를 통과한 뒤 별도로 판단한다.

## 실행 환경 (모든 스텝 공통)

호스트는 Windows이고 acados/`.venv`가 없다. 검증은 이미 떠 있는 Docker 컨테이너 `sp-build`에서 한다.

컨테이너의 git HEAD는 `3a3f954588`로 호스트(`97d1591f3`)보다 뒤지만, **워킹트리 내용은 이 계획이 닿는 임포트 경로 전체에서 호스트와 md5가 일치함을 확인했다.**

| 파일 | md5 |
|---|---|
| `openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py` | `7101c2ed3ae970545363c95f12d9a793` |
| `openpilot/sunnypilot/selfdrive/controls/lib/dec/dec.py` | `691d66d12975305896b6254963df2657` |
| `openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py` | `6ed8b0e04cbf8dc7d1a3d962ef0127f9` |
| `openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` | `75b050344b330ac805957de2ce9b6700` |
| `openpilot/selfdrive/controls/lib/longitudinal_planner.py` | `9a6788551216d60704e8b4d4cc22517e` |
| `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` | `c7ca902cec58f29fcf2f1ce50752dbc9` |

아래 각 스텝의 기대 결과는 추정이 아니라 이 환경에서 실측한 값이다.

호스트 레포 루트(`E:\dev\sunnypilot`)에서 다음 두 명령을 쓴다.

```bash
# 파일을 컨테이너로 복사
docker cp openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py

# 테스트 실행
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/'
```

C++ 변경이 없으므로 `scons` 재빌드는 필요 없다.

호스트 워킹트리는 LF다 (`core.autocrlf=false`). `docker cp`는 양방향 모두 줄바꿈을 바꾸지 않는다.

## File Structure

| 파일 | 책임 | 스텝 |
|---|---|---|
| `openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py` | `MockModelData`에 endpoint 주입 지원 + 고속 구간 회귀 테스트 3개 | 2·3 |
| `openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py` | 거리 곡선 배열 두 줄 | 5 |

---

### Task 1: 감속 감지 곡선을 150 km/h까지 연장

**Files:**
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py:19-23` (`MockModelData`) 및 파일 끝
- Modify: `openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py:11-13`

**Interfaces:**
- Consumes: 없음. 이 계획의 첫 태스크다.
- Produces: `WMACConstants.SLOW_DOWN_BP`, `WMACConstants.SLOW_DOWN_DIST` — 각각 17개 원소의 `list[float]`. `dec.py`의 `_calculate_slow_down`이 `interp(self._v_ego_kph, SLOW_DOWN_BP, SLOW_DOWN_DIST)`로 읽는다. 시그니처 변화 없음.
- Produces: `MockModelData(valid: bool = True, endpoint: float = 0.0)` — 테스트 전용. `endpoint`는 `position.x`의 마지막 원소에 들어간다. 기본값 `0.0`은 변경 전 동작과 동일하다.

- [ ] **Step 1: 기준선 측정**

변경 전 테스트 수를 기록해 둔다. 이후 스텝의 증감을 이 값 기준으로 읽는다.

Run:
```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/'
```

Expected: `8 passed`

내역은 `test_dynamic_controller.py` 4개(`test_initial_mode_is_acc`, `test_standstill_triggers_blended`, `test_emergency_blended_on_fcw`, `test_radarless_slowdown_triggers_blended`) + `test_dec_planner_gate.py` 4개다.

- [ ] **Step 2: `MockModelData`가 endpoint를 받게 한다**

`openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py`의 19~23행을 찾는다.

변경 전:

```python
class MockModelData:
  def __init__(self, valid=True):
    size = 33 if valid else 10  # incomplete if invalid
    self.position = type("Pos", (), {"x": [0.0] * size})()
    self.orientation = type("Ori", (), {"x": [0.0] * size})()
```

변경 후:

```python
class MockModelData:
  def __init__(self, valid=True, endpoint=0.0):
    size = 33 if valid else 10  # incomplete if invalid
    position_x = [0.0] * size
    position_x[-1] = endpoint
    self.position = type("Pos", (), {"x": position_x})()
    self.orientation = type("Ori", (), {"x": [0.0] * size})()
```

`endpoint`의 기본값이 `0.0`이므로 인자를 주지 않는 기존 호출은 변경 전과 완전히 동일한 객체를 만든다. `valid=False`(size 10) 경로에서 마지막 원소를 채워도 무해하다. `_calculate_slow_down`이 길이 검사에서 먼저 걸러내므로 `position.x`를 읽지 않는다.

- [ ] **Step 3: 실패하는 테스트를 작성한다**

같은 파일 맨 끝, `test_radarless_slowdown_triggers_blended` 아래에 이어 붙인다. 클래스 본문이므로 들여쓰기 2칸을 유지한다.

```python

  def test_highspeed_endpoint_above_curve_stays_acc(self, mock_cp, mock_mpc, default_sm):
    mock_cp.radarUnavailable = True
    controller = DynamicExperimentalController(mock_cp, mock_mpc, params=MockParams())

    # 120 km/h against a 320 m endpoint: above the 315 m curve value, so no shortage at all.
    default_sm["carState"] = MockCarState(vEgo=120.0 / 3.6, vCruise=120.0)
    default_sm["modelV2"] = MockModelData(valid=True, endpoint=320.0)

    for _ in range(20):
      controller.update(default_sm)

    assert not controller._has_slow_down
    assert controller.mode() == "acc"

  def test_highspeed_slowdown_triggers_blended(self, mock_cp, mock_mpc, default_sm):
    mock_cp.radarUnavailable = True
    controller = DynamicExperimentalController(mock_cp, mock_mpc, params=MockParams())

    # 120 km/h against a 250 m endpoint: 21% short of 315 m, urgency clears the 0.7 emergency bar.
    default_sm["carState"] = MockCarState(vEgo=120.0 / 3.6, vCruise=120.0)
    default_sm["modelV2"] = MockModelData(valid=True, endpoint=250.0)

    for _ in range(3):
      controller.update(default_sm)

    assert controller._has_slow_down
    assert controller.mode() == "blended"

  def test_curve_below_60kph_is_unchanged(self, mock_cp, mock_mpc, default_sm):
    mock_cp.radarUnavailable = True
    controller = DynamicExperimentalController(mock_cp, mock_mpc, params=MockParams())

    # 60 km/h still reads 165 m off the curve, so a 160 m endpoint stays under the threshold.
    default_sm["carState"] = MockCarState(vEgo=60.0 / 3.6, vCruise=60.0)
    default_sm["modelV2"] = MockModelData(valid=True, endpoint=160.0)

    for _ in range(20):
      controller.update(default_sm)

    assert not controller._has_slow_down
    assert controller.mode() == "acc"
```

세 테스트 모두 `mock_cp.radarUnavailable = True`가 필수다. 레이더 모드(`_radar_mode`)는 `_has_lead_filtered`를 `_has_slow_down`보다 먼저 검사하고, `default_sm`의 `radarState`는 `present=1.0`이라 앞차가 잡힌 것으로 판정되어 slow_down 분기에 도달하지 못한다.

반복 횟수가 다른 이유는 다음과 같다. endpoint 250 m는 urgency 0.903으로 `_radarless_mode`의 긴급 경로(`urgency > 0.7`)를 타서 `min_mode_duration`을 건너뛰고 즉시 blended가 되므로 3회면 충분하다. 나머지 두 테스트는 모드가 바뀌지 않음을 확인하는 것이므로 여유 있게 20회를 돈다.

- [ ] **Step 4: 테스트를 돌려 1개가 실패하는지 확인한다**

Run:
```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/'
```

Expected: `10 passed, 1 failed`

실패하는 것은 `test_highspeed_slowdown_triggers_blended` 하나이며, 메시지는 다음과 같다.

```
FAILED openpilot.sunnypilot.selfdrive.controls.lib.dec.tests.test_dynamic_controller.TestDynamicExperimentalController.test_highspeed_slowdown_triggers_blended
  File "/work/openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py", line 132, in test_highspeed_slowdown_triggers_blended
    assert controller._has_slow_down
AssertionError
```

나머지 두 테스트가 이 시점에 이미 통과하는 것은 정상이다. 구 곡선에서 `expected_distance`가 165 m로 포화하므로 320 m와 160 m 모두 부족분이 생기지 않는다. 두 테스트는 변경을 이끄는 red 테스트가 아니라 회귀 가드다. 특히 `test_curve_below_60kph_is_unchanged`는 60 km/h 이하 구간이 변경 전후 동일함을 지키는 것이 목적이므로 양쪽에서 통과해야 맞다.

여기서 `11 passed`가 나오면 `constants.py`가 이미 수정된 것이다. 되돌리고 다시 실행한다.

- [ ] **Step 5: 곡선을 연장한다**

`openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py`의 11~13행.

변경 전:

```python
  # Optimized slow down distance curve - smooth and progressive
  SLOW_DOWN_BP = [0., 10., 20., 30., 40., 50., 55., 60.]
  SLOW_DOWN_DIST = [32., 46., 64., 86., 108., 130., 145., 165.]
```

변경 후:

```python
  # Optimized slow down distance curve - smooth and progressive.
  # Above 60 km/h the curve continues at 2.5 m per km/h, tracking roughly 95% of the distance
  # covered in the model's 10 s horizon. It stops at 150 km/h: V_CRUISE_MAX is 145, and interp
  # clamps past the last breakpoint, which is the conservative direction.
  SLOW_DOWN_BP = [0., 10., 20., 30., 40., 50., 55., 60., 70., 80., 90., 100., 110., 120., 130., 140., 150.]
  SLOW_DOWN_DIST = [32., 46., 64., 86., 108., 130., 145., 165., 190., 215., 240., 265., 290., 315., 340., 365., 390.]
```

앞의 8개 원소는 그대로 두고 뒤에 9개를 덧붙이기만 한다. 기존 값을 다시 타이핑하지 말고 배열 끝에 이어 쓴다.

- [ ] **Step 6: 테스트를 돌려 전부 통과하는지 확인한다**

Run:
```bash
docker cp openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py \
  sp-build:/work/openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/'
```

Expected: `11 passed`

Step 1의 8개에서 3개가 늘고 실패가 없어야 한다.

- [ ] **Step 7: 종방향 회귀를 돌린다**

곡선은 DEC 밖에서 참조되지 않지만, DEC는 `LongitudinalPlannerSP`를 통해 종방향 플래너에 물려 있다.

Run:
```bash
docker exec sp-build bash -lc 'cd /work && source .venv/bin/activate && \
  python tools/test_runner.py openpilot/sunnypilot/selfdrive/controls/lib/'
```

Expected: `198 passed`

이 경로의 변경 전 기준선은 `195 passed`다(실측). Step 3에서 추가한 3개가 늘어난 값이며, 이 계획은 기존 테스트를 하나도 건드리지 않으므로 그 외의 증감은 없어야 한다. 실패가 하나라도 나오면 Step 5를 되돌리고 원인을 찾는다.

- [ ] **Step 8: 커밋**

```bash
git add openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py \
        openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py
git commit -F - <<'EOF'
fix: let DEC see slowdowns above 60 km/h

DEC compares the model's 10 s trajectory endpoint against a speed-indexed
distance curve to decide when to hand longitudinal control to the model.
The curve's breakpoints stopped at 60 km/h and numpy.interp clamps past
the last one, so the threshold sat at 165 m for every higher speed.

At 120 km/h the endpoint is near 333 m even when the model plans a real
slowdown, so the comparison could never fire on a highway. The saturation
was never a decision about high-speed driving: the breakpoints have not
changed since the feature arrived from dragonpilot, where it existed to
catch red lights and intersections.

Continue the curve to 150 km/h at 2.5 m per km/h, the slope the table
already implies. Values at and below 60 km/h are untouched, and the
detection logic, filters and thresholds are left alone.
EOF
```

---

## 실차 적용 전 게이트

레포 커밋과 별개로, 디바이스에 올리기 전에 설계 문서 Safety 절의 위험 1을 판별해야 한다. 이 게이트는 주행 로그가 확보된 뒤에 수행한다.

`modelV2`는 항상 로깅되고 DEC 내부의 `_endpoint_x`는 어디에도 발행되지 않는 죽은 디버그 필드이므로, 디바이스 코드를 바꾸지 않고 기존 로그만으로 확인할 수 있다.

로그에서 뽑을 것은 두 계열이다.

- `modelV2.position.x[32]`
- `carState.vEgo`

통과 기준 두 가지.

1. **중앙값.** 100~130 km/h 구간에서 `position.x[32]`의 중앙값이 해당 속도의 10초 등속 주행거리(120 km/h면 333 m)의 90 % 이상이어야 한다. 체계적으로 낮으면 곡선이 상시 발동하므로 적용하지 않는다.
2. **프레임간 지터.** 같은 구간에서 `position.x[32]`의 프레임간 표준편차가 임계 여유보다 충분히 작아야 한다. 120 km/h의 여유는 315 − 298 = 17 m다. `_slow_down_filter`의 정상상태 유효 이득이 약 0.3, 20 Hz 기준 시상수 0.2초에 불과해 프레임 단위 노이즈만 걸러낸다.

둘 중 하나라도 못 미치면 설계 문서 Rollback 절의 단계적 완화를 먼저 적용한다.

1. `speed_factor`의 분모를 80에서 160으로 키운다 (120 km/h에서 2.19배 → 1.59배).
2. 60 km/h 위 기울기를 2.5에서 2.0으로 줄인다 (120 km/h에서 315 m → 285 m).
3. 배열을 8개 원소로 원복한다.

## 실차 관찰 항목

적용 후 첫 고속도로 주행에서 볼 것.

- 곡선로에서 이유 없는 감속이 붙는가. 붙는다면 반경 400 m 이하 구간인지 확인한다. 설계상 그 구간은 발동이 예상되며 감속 자체가 타당하다.
- 급정체 접근에서 blended 전환이 실제로 일어나는가. 전환 자체는 `longitudinalPlanSP.dec.state`로 확인할 수 있다.
- 오탐의 최대 피해는 완만한 불필요 감속이다. blended 모드는 `longitudinal_planner.py:148-153`의 `min()` 후보를 하나 늘릴 뿐이라 가속 방향으로 작용할 수 없다.
