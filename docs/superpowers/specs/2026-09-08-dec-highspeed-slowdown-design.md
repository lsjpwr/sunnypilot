# DEC 고속 구간 감속 감지 (High-Speed Slowdown Detection) 설계

**Goal:** DEC(Dynamic Experimental Control)의 감속 감지 곡선이 60 km/h에서 포화해 고속도로 급정체를 구조적으로 인식하지 못하는 문제를 해소한다. `SLOW_DOWN_BP`/`SLOW_DOWN_DIST` 두 배열을 openpilot이 지원하는 속도 상단까지 연장한다.

**Architecture:** `sunnypilot/selfdrive/controls/lib/dec/constants.py`의 상수 두 줄만 바꾼다. `dec.py` 판정 로직, 필터 파라미터, 임계값은 손대지 않는다. 새 파라미터도 UI도 추가하지 않는다.

**Tech Stack:** Python 3.11+ (디바이스), `numpy.interp`. 신규 의존성 없음.

---

## Background: 왜 60 km/h까지였나

`SLOW_DOWN_BP`는 DEC 최초 커밋부터 지금까지 한 번도 수정된 적이 없다.

| 커밋 | `SLOW_DOWN_BP` | `SLOW_DOWN_DIST` |
|---|---|---|
| `5c38aeae0` Longitudinal: Dynamic Experimental Control (#572) | `[0,10,20,30,40,50,55,60]` | `[30,45,60,80,100,120,135,150]` |
| `65431f4e2` DEC: adjust dynamic speed adaptation parameters (#813) | 동일 | `[25,38,55,75,95,115,130,150]` |
| `00eb0e983` DEC: Kalman and the Magic Mode Switcheroo (#1026) | 동일 | `[32,46,64,86,108,130,145,165]` |

세 차례의 튜닝이 모두 값만 조정했고 범위는 건드리지 않았다. 판정식 자체도 최초 커밋의 `dec.py:216`부터 현재까지 동일하다.

```python
md.position.x[TRAJECTORY_SIZE - 1] < interp(v_ego_kph, SLOW_DOWN_BP, SLOW_DOWN_DIST)
```

이유는 기능의 출신에 있다. 원본 `dec.py` 헤더는 dragonpilot(Rick Lan)을 저작자로 명시한다. dragonpilot의 conditional experimental mode가 잡으려던 대상은 신호등, 정지선, 교차로 회전 — 전부 도심 상황이고 60 km/h면 충분히 덮인다. 고속 구간을 배제하기로 결정한 것이 아니라, 고속 구간을 고려한 적이 없다.

### 포화가 만드는 결과

`numpy.interp`는 마지막 브레이크포인트를 넘어서면 마지막 값으로 클램프한다. 따라서 60 km/h 이상에서 `expected_distance`는 항상 165 m다.

`md.position.x[32]`는 10초 뒤 예측 위치이므로 120 km/h 등속 주행에서 약 333 m가 나온다. 모델이 120 km/h에서 60 km/h로 감속하는 계획을 세워도 10초 평균 90 km/h, 즉 250 m로 여전히 165 m를 크게 웃돈다. 165 m 아래로 내려가려면 10초 평균이 59 km/h 이하여야 하고, 이는 120 km/h에서 거의 정지 상태까지 가는 계획을 의미한다. 실질적으로 발동 불가능하다.

### 표의 정체

기존 표는 사실상 "10초 등속 주행거리"다.

| v (km/h) | 10초 등속 (m) | `SLOW_DOWN_DIST` | 비율 |
|---|---|---|---|
| 30 | 83.3 | 86 | 1.03 |
| 40 | 111.1 | 108 | 0.97 |
| 50 | 138.9 | 130 | 0.94 |
| 55 | 152.8 | 145 | 0.95 |
| 60 | 166.7 | 165 | 0.99 |

저속(0~20 km/h)에서만 정차 여유를 위해 등속선 위로 올라가 있고, 30 km/h 이상은 등속선을 0.94~1.03 배로 따라간다. 연장의 기준선은 여기서 나온다.

## Non-goals

- 신규 파라미터 및 UI 토글. StopDistance가 쓴 param + UI 방식은 이 변경에 과하다. DEC 자체가 이미 토글 뒤에 있고, 되돌리기는 배열 두 줄을 원복하는 것으로 끝난다.
- `dec.py` 로직 수정. `speed_factor` 증폭기 포함.
- `SLOW_DOWN_PROB` 조정. 전 속도 대역에 영향을 준다.
- 레이더 장착차 경로 최적화. `_radar_mode`는 `_has_lead_filtered`를 `_has_slow_down`보다 먼저 검사하므로 앞차가 잡혀 있으면 이 변경이 닿지 않는다. 대상 차량(Tesla Model Y HW4)은 `radarUnavailable = True`라 `_radarless_mode`가 돌고, 거기서는 `_has_slow_down`이 세 번째 분기로 직접 blended를 요청한다.
- 시야 거리 자체의 개선. 이 설계는 모델이 이미 본 것을 DEC가 읽지 못하는 문제만 다룬다. 모델이 급정체를 늦게 보는 문제는 별개다.

---

## Behavior

### 연장 곡선

60 km/h 위로 10 km/h당 25 m, 즉 2.5 m/(km/h) 기울기로 150 km/h까지 잇는다.

| v (km/h) | 10초 등속 (m) | 신규 `SLOW_DOWN_DIST` | 비율 |
|---|---|---|---|
| 60 | 166.7 | 165 | 0.99 |
| 70 | 194.4 | 190 | 0.98 |
| 80 | 222.2 | 215 | 0.97 |
| 90 | 250.0 | 240 | 0.96 |
| 100 | 277.8 | 265 | 0.95 |
| 110 | 305.6 | 290 | 0.95 |
| 120 | 333.3 | 315 | 0.95 |
| 130 | 361.1 | 340 | 0.94 |
| 140 | 388.9 | 365 | 0.94 |
| 150 | 416.7 | 390 | 0.94 |

기존 표의 종단 기울기는 4.0 m/(km/h)로 등속선(2.78)보다 가팔랐다. 등속선을 따라잡는 구간이었기 때문이다. 60 km/h에서 이미 따라잡았으므로 그 이후는 등속선보다 완만한 2.5로 잇는다. 그 결과 여유가 속도에 따라 서서히 넓어져 비율이 0.99에서 0.94로 수렴한다.

### 상한을 150 km/h로 두는 이유

openpilot의 설정 속도 상한은 `V_CRUISE_MAX = 145`(`selfdrive/car/cruise.py:13`)이고 `longitudinal_planner.py:85`에서 클립된다. 그러나 `SLOW_DOWN_BP`가 참조하는 값은 `v_cruise`가 아니라 `v_ego`이며, 내리막이나 액셀 오버라이드로 실제 속도는 145를 넘을 수 있다.

마지막 점을 145에 두면 140→145만 5 km/h 스텝이 되고 값도 377.5로 소수가 나온다. 150에 두면 10 km/h 등간격과 정수 값이 유지되고, 150 초과 시 클램프되는 390 m는 등속선 대비 낮은 값이므로 보수적 방향이다.

### 발동 조건이 속도에 대해 평탄해진다

`_calculate_slow_down`의 urgency 계산은 다음과 같다.

```python
shortage_ratio = (expected_distance - endpoint_x) / expected_distance
urgency = min(1.0, shortage_ratio * 2.0)
if v_ego_kph > 25.0:
    speed_factor = 1.0 + (v_ego_kph - 25.0) / 80.0
    urgency = min(1.0, urgency * speed_factor)
```

발동 임계는 `SLOW_DOWN_PROB * 0.8 = 0.24`다. 필요한 shortage 비율은 `0.24 / (2 * speed_factor)`이며, 이를 신규 곡선에 대입하면 발동 지점이 나온다.

| v (km/h) | speed_factor | 필요 shortage | 발동 endpoint (m) | 모델의 10초 평균 계획 (km/h) | 현재 속도 대비 |
|---|---|---|---|---|---|
| 60 | 1.44 | 8.3 % | 151 | 54 | 0.91 |
| 80 | 1.69 | 7.1 % | 200 | 72 | 0.90 |
| 100 | 1.94 | 6.2 % | 249 | 90 | 0.90 |
| 120 | 2.19 | 5.5 % | 298 | 107 | 0.89 |
| 140 | 2.44 | 4.9 % | 347 | 125 | 0.89 |
| 150 | 2.56 | 4.7 % | 372 | 134 | 0.89 |

곡선의 기울기 선택과 `speed_factor`의 증가가 서로 상쇄되어, 발동 조건이 전 속도대에서 "모델이 현재 속도의 약 90% 이하 평균으로 갈 계획을 세웠을 때"로 수렴한다. 이 평탄성이 2.5 기울기를 고른 근거다.

긴급 경로(`urgency > 0.7`, `min_mode_duration`을 건너뛰고 즉시 blended)는 현재 속도의 75~81 % 계획에서 열린다. 120 km/h 기준 평균 95 km/h 계획, endpoint 265 m다.

### 곡선로 오탐 분석

`md.position.x`는 디바이스 전방 좌표이므로 곡선에서는 호길이보다 짧게 나온다. 반경 R의 곡선에서 호길이 s를 달릴 때 x 성분은 `R * sin(s/R)`이다. 120 km/h에서 s = 333 m 기준:

| 반경 (m) | x 성분 (m) | 등속 대비 | 횡가속 (m/s²) | 발동 여부 (임계 298 m) |
|---|---|---|---|---|
| 1000 | 327 | −2 % | 1.11 | 미발동 |
| 800 | 324 | −3 % | 1.39 | 미발동 |
| 500 | 309 | −7 % | 2.22 | 미발동 |
| 400 | 296 | −11 % | 2.78 | 발동 |

발동선에 걸리는 반경 400 m 곡선은 120 km/h에서 횡가속 2.78 m/s²로, 어차피 감속이 필요한 구간이다. 오탐의 방향이 안전 쪽이다.

### 오탐의 최대 피해가 제한적인 이유

blended 모드는 `longitudinal_planner.py:148-153`에서 후보 목록에 모델의 `desiredAcceleration`을 하나 더 추가할 뿐이고, 최종 선택은 `min()`이다.

```python
candidates = [(output_a_target_mpc, ...), (self.a_cruise, ...)]
if is_e2e:
    candidates.append((output_a_target_e2e, ...))
output_a_target, self.mpc.source, _ = min(candidates, key=lambda c: c[0])
```

후보가 늘어도 결과는 더 낮은 가속도, 즉 더 보수적인 방향으로만 움직인다. 오탐의 최악은 불필요한 완만한 감속이지 폭주가 아니다. 이 비대칭이 곡선을 보수적으로 깎지 않고 기존 기울기를 그대로 잇기로 한 근거다.

---

## 파일별 변경 (2개)

### 1. `openpilot/sunnypilot/selfdrive/controls/lib/dec/constants.py`

배열 두 줄. 브레이크포인트 9개 추가.

```python
  # Optimized slow down distance curve - smooth and progressive
  SLOW_DOWN_BP = [0., 10., 20., 30., 40., 50., 55., 60., 70., 80., 90., 100., 110., 120., 130., 140., 150.]
  SLOW_DOWN_DIST = [32., 46., 64., 86., 108., 130., 145., 165., 190., 215., 240., 265., 290., 315., 340., 365., 390.]
```

60 km/h 이하 구간은 값이 하나도 바뀌지 않으므로 도심 거동은 그대로다.

### 2. `openpilot/sunnypilot/selfdrive/controls/lib/dec/tests/test_dynamic_controller.py`

기존 `MockModelData`는 `position.x`를 `[0.0] * 33`으로 채운다. endpoint가 0이면 shortage 비율이 항상 1.0이라 표 값과 무관하게 발동한다. 표 확장을 검증하려면 endpoint를 지정할 수 있어야 한다.

`MockModelData`에 `endpoint` 인자를 추가하고(기본값은 현재 동작을 보존하는 `0.0`), 다음 두 가지를 검증한다.

- 120 km/h, endpoint 320 m → `_has_slow_down`이 False로 유지된다.
- 120 km/h, endpoint 250 m → `_has_slow_down`이 True로 전환된다.

기존 4개 테스트는 `FakeKalman` 주입 또는 invalid trajectory 경로를 쓰므로 표 확장의 영향을 받지 않는다.

---

## Safety

### 코드로 증명되는 것

- 60 km/h 이하 거동 불변. 해당 구간 값이 바뀌지 않는다.
- blended 모드의 출력은 `min()` 선택이므로 가속 방향으로 작용할 수 없다.
- 모델 plan 출력에 인위적 상한이 없다. `parse_model_outputs.py`에서 `plan`은 `parse_mdn`으로 그대로 회귀 파싱되며 클램프가 없다. 유일한 `np.clip`은 표준편차 `exp` 계산용이다. 따라서 고속에서 endpoint가 상수로 포화하는 실패 모드는 없다.
- 레이더 장착차는 앞차가 잡힌 상태에서 이 경로를 타지 않는다.

### 코드로 증명되지 않는 실제 위험 (심각도순)

1. **`md.position.x[32]`의 300~400 m 구간 정확도가 미검증이다.** 이 설계의 최대 가정이다. 모델이 고속에서 체계적으로 짧은 endpoint를 내놓으면 고속도로에서 상시 blended가 된다. 상단(140~150 km/h)은 120 km/h 구간보다 더 미검증이며, 실주행 노출 빈도가 낮아 발견도 늦을 수 있다.
2. **`speed_factor` 증폭기가 60 km/h 위에서 처음 실행된다.** 지금까지 표가 포화해 shortage가 발생한 적이 없으므로 이 코드 경로는 고속에서 한 번도 돈 적이 없다. 실차에서 과민하면 첫 번째로 돌릴 손잡이다.
3. **필터의 평활 시간이 짧다.** `_slow_down_filter`는 `smoothing_factor = 0.7`, `measurement_noise = 0.1`, `process_noise = 0.1`, `alpha = 1.05`로 정상상태 유효 이득이 약 0.3이다. 20 Hz 기준 시상수 0.2초 수준이므로 프레임 단위 노이즈만 걸러낸다. `position.x[32]`의 프레임간 지터가 크면 그대로 통과한다. 모드 전환기의 `min_mode_duration = 10`프레임과 신뢰도 누적이 비긴급 경로에 0.3~0.5초를 더 얹지만, 긴급 경로는 이를 우회한다.

### 검증 순서

1. **단위 테스트.** 표 확장이 의도한 endpoint에서 발동하는지 확인한다.
2. **기존 주행 로그 산점도.** `modelV2.position.x[32]` 대 `carState.vEgo`를 아무 주행 로그에서나 뽑는다. `modelV2`는 항상 로깅되고, DEC 내부의 `_endpoint_x`, `_expected_distance`, `_urgency`, `_trajectory_valid`는 어디에도 발행되지 않는 죽은 디버그 필드이므로 디바이스 코드 변경 없이 지금 있는 로그만으로 판별 가능하다. 확인할 것은 두 가지다.
   - 120 km/h 구간의 endpoint 중앙값이 320~340 m 근처에 모이는가. 체계적으로 낮으면 위험 1이 현실이다.
   - 프레임간 표준편차가 임계 여유(120 km/h에서 17 m)보다 충분히 작은가. 필터 시상수가 0.2초에 불과하므로 지터가 그대로 통과한다.
3. **실차.** 위 두 단계를 통과한 뒤에만 진행한다.

## Rollback

`constants.py`의 두 배열을 8개 원소 형태로 되돌린다. 다른 변경이 없으므로 부분 롤백이 필요 없다.

실차에서 과민 판정이 나올 경우의 단계적 완화는 다음 순서를 권한다.

1. `speed_factor`의 분모를 80에서 160으로 키운다. 120 km/h에서 2.19배가 1.59배가 된다.
2. `SLOW_DOWN_DIST`의 60 km/h 위 기울기를 2.5에서 2.0으로 줄인다. 120 km/h에서 315 m가 285 m가 된다.
3. 배열을 원복한다.

## Open items

- 검증 2단계에 쓸 주행 로그가 아직 확보되지 않았다. 로그 확보 전에는 실차 적용을 하지 않는다.
- `SLOW_DOWN_WINDOW_SIZE`, `LEAD_WINDOW_SIZE`, `SLOWNESS_WINDOW_SIZE`는 `00eb0e983`에서 WMAC를 Kalman으로 교체할 때 참조가 사라졌으나 상수만 남아 있다. 이 설계의 범위 밖이지만 별도로 정리할 여지가 있다.
