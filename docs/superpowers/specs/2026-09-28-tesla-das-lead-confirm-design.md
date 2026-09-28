# Tesla DAS 앞차 확인 (DAS Lead Confirmation) 설계

**Goal:** 비전 전용 Tesla(2026+ Model Y 주니퍼, HW4 gen2)에서 앞차 인식을 앞당긴다. Tesla 자체 인식이 vehicle bus로 보내는 `DAS_object`(0x309)가 모델의 저확신 앞차 후보와 같은 자리를 가리키면, 모델 prob이 0.5에 닿기 전에 그 후보를 앞차로 인정한다. 메시지가 없거나 믿을 수 없으면 지금과 똑같이 동작한다.

**Architecture:** opendbc Tesla `RadarInterface`가 `HW4_GEN2_VEHICLE_BUS` 차량에서 0x309를 해석해 radarTracks 점으로 낸다. radard는 이 차량에서 점을 레이더 트랙으로 쓰지 않고 새 모듈 `DasLeadConfirmer`에 넘긴다. 확인기는 주행 중 자기검증을 통과한 뒤에만, DAS와 자리가 맞는 모델 후보의 prob을 게이트 바로 위로 올린다. 앞차의 거리·속도·가속도는 항상 모델 값이다.

**Tech Stack:** Python(opendbc `CANParser`, radard), DBC 텍스트. 새 의존성 없음. cereal 스키마 변경 없음.

---

## 배경

### 지금 주니퍼에서 앞차가 생기는 조건

HW4 gen2는 `radarUnavailable = True`다(`opendbc_repo/opendbc/car/tesla/interface.py:49`). radarTracks 점은 늘 비어 있고, 앞차는 모델 `leadsV3` prob이 0.5를 넘을 때만 생긴다(`openpilot/selfdrive/controls/radard.py:161,170`). prob 필터는 올라갈 때 지연이 없다(`radard.py:253-258`). 그래서 인식 시점은 이 0.5 게이트가 정한다.

### DAS_object (0x309)

커뮤니티 DBC(joshwardell/model3dbc `Model3CAN.dbc`, 옛 펌웨어 역공학)는 이 메시지를 VehicleBus에 둔다. `DAS_objectId`(0..5)로 멀티플렉스된다. 이 설계가 쓰는 것은 두 슬롯이다.

| 신호 | 비트 | 배율, 오프셋 | SNA |
|---|---|---|---|
| `DAS_objectId` (mux) | 0\|3 | 1, 0 | 없음 (0 LEAD_VEHICLES, 3 CUTIN_VEHICLE) |
| `*VehType` | 3\|3 | 1, 0 | 없음 |
| `*VehRelevantForControl` | 7\|1 | 1, 0 | 없음 |
| `*VehDx` | 8\|8 | 0.5, 0 (m) | 255 |
| `*VehVxRel` | 16\|4 | 4, −30 (m/s) | 15 |
| `*VehDy` | 20\|7 | 0.35, −22.05 (m) | 없음 |
| `*VehId` | 27\|7 | 1, 0 | 127 |

`*`는 슬롯 0에선 `DAS_lead`, 슬롯 3에선 `DAS_cutin`이다. 두 슬롯은 비트 배치가 같다.

주니퍼 펌웨어가 이 메시지를 보내는지, 배치가 같은지는 확인되지 않았다. 참고 원격(sunnypilot, commaai, bfayers, dzid26, sarumpj) 어디에도 주니퍼 0x309 기록이 없다(2026-09-28 조사). party bus DBC 동기화 PR commaai/opendbc#3301(SW 2026.8.3 기준)에도 없다.

### 왜 가짜 레이더 트랙으로 바로 넣지 않나

1. 상대속도가 4 m/s 단위다. radard의 트랙은 `vLead = vRel + v_ego`를 그대로 내고(`radard.py:233`), MPC는 `vLead`를 그대로 쓴다(`openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py:318`). 앞차 속도가 14.4 km/h 계단으로 튄다.
2. 자차 4 m/s 이상에선 트랙도 비전 prob > 0.5 확인 뒤에만 쓰인다(`radard.py:161`). 인식이 빨라지지 않는다.
3. 4 m/s 미만에선 트랙만으로 앞차가 된다(`radard.py:173-180`). 검증 안 된 배치로 저속 유령 정지가 날 수 있다.

## Non-goals

- DAS 거리·속도를 MPC에 넣는 것, DAS만으로 앞차를 만드는 것.
- `leadVeh2`, 좌/우 차량, 도로 표지(신호등 색 포함), 헤딩 슬롯.
- 다른 Tesla(HW3, HW4 gen1, vehicle bus 하네스 차량). 조건은 `HW4_GEN2_VEHICLE_BUS`로 한정한다.
- 기기 설정 화면의 토글. sunnylink에만 노출한다.
- cereal 스키마 변경. radarState에 새 필드를 넣지 않는다.
- commaai/openpilot#37824(모델 앞차 궤적을 MPC에 쓰기). 별도로 보류한다.

---

## 설계

### 1. DBC — `opendbc_repo/opendbc/dbc/tesla_model3_vehicle.dbc`

주니퍼의 vehicle bus DBC다(`opendbc_repo/opendbc/car/tesla/values.py:72`). 주니퍼 전용 `SeatBeltStatus`가 이미 들어 있다(`tesla_model3_vehicle.dbc:239-242`). 여기에 `BO_ 777 DAS_object: 8 VEH`와 위 표의 신호 13개(mux 1개, 슬롯당 6개), `VAL_`(objectId 이름, SNA)를 추가한다.

opendbc 파서는 멀티플렉스 표기를 읽지만 뜻은 무시한다(`opendbc_repo/opendbc/can/dbc.py:125-151`). 모든 프레임에서 모든 신호를 해석하므로, 소비자가 `DAS_objectId`로 프레임을 나눠야 한다.

### 2. RadarInterface — `opendbc_repo/opendbc/car/tesla/radar_interface.py`

- **DAS 모드:** `CP.flags & TeslaFlags.HW4_GEN2_VEHICLE_BUS`. 그 밖의 차량은 기존 코드 경로를 그대로 탄다(Continental 레이더 또는 빈 점).
- **파서:** `CANParser(DBC[fp][Bus.adas], [("DAS_object", float("nan"))], CANBUS.vehicle)`. 주기를 `nan`으로 주면 alive 검사가 꺼진다(`opendbc_repo/opendbc/can/parser.py:179`). 메시지가 없어도 CAN 오류가 생기지 않는다.
- **해석:** 매 호출마다 파서를 갱신하고, `vl_all["DAS_object"]`의 i번째 프레임마다 `DAS_objectId`를 본다. 0이면 lead 슬롯, 3이면 cut-in 슬롯을 그 프레임 값으로 갱신한다. 나머지 id는 버린다.
- **유효 조건:** Dx가 SNA가 아님(< 127.5 m), Id가 SNA가 아님(≠ 127), VxRel이 SNA가 아님(≠ 30 m/s), `RelevantForControl == 1`. 유효하면 `(dRel = Dx, yRel = −Dy, vRel = VxRel)`과 갱신 시점을 저장한다. 무효 프레임이 오면 그 슬롯을 비운다.
- **Dy 부호:** DAS Dy는 왼쪽이 +라고 가정한다(ISO 8855). radard의 `yRel`은 오른쪽이 +이므로(`radard.py:122`에서 `-lead.y[0]`와 비교) 부호를 뒤집는다. 로그로 확인할 가정이다(6절).
- **발행:** 5호출마다(card 100 Hz 기준 20 Hz, `RadarInterfaceBase`와 같은 주기) `RadarData`를 반환한다. 0.5초(50호출) 안에 갱신된 슬롯만 점으로 넣는다. `trackId`는 슬롯 기준값 + DAS Id다(lead 0, cut-in 128). 메시지가 없으면 빈 `RadarData`가 같은 주기로 나간다.
- **오류:** `errors`를 절대 설정하지 않는다. `openpilot/selfdrive/selfdrived/selfdrived.py:420-425`가 레이더 오류를 경고와 해제로 바꾸기 때문이다.
- **radarUnavailable:** `True` 그대로 둔다. DEC 레이더 없음 분기(`openpilot/sunnypilot/selfdrive/controls/lib/dec/dec.py:422`)와 `test_car_interfaces`의 레이더 고장 검사 대상 여부가 바뀌지 않는다.

### 3. radard — `openpilot/selfdrive/controls/radard.py`

- `self.das_mode = CP.brand == "tesla" and bool(CP.flags & TeslaFlags.HW4_GEN2_VEHICLE_BUS)`.
- DAS 모드에선 `rr.points`로 `Track`을 만들지 않는다. `self.tracks`가 늘 비므로 매칭과 저속 오버라이드 경로를 타지 않는다. 오늘의 주니퍼와 같다.
- 대신 점을 확인기에 넘긴다. lead 점과 cut-in 점은 `trackId`로 나눈다(128 미만이 lead). 모델 후보는 레이더 좌표로 바꿔 넘긴다. 슬롯 i마다 `(leadsV3[i].x[0] − RADAR_TO_CAMERA, −leadsV3[i].y[0], leadsV3[i].prob)`.
- 슬롯 i의 게이트 값은 `lead_prob = filter[i].x`이고, 확인되면 `max(lead_prob, DAS_CONFIRMED_PROB)`이다. 필터는 모델 prob으로만 갱신한다. 확인이 필터 상태에 섞이지 않는다.
- `DAS_CONFIRMED_PROB = 0.51`. 게이트 0.5 바로 위이고 FCW 기준 0.9(`long_mpc.py:373-374`)와 멀다. 확인만으로 FCW가 뜨지 않는다. 로그의 `radarState.leadOne.modelProb == 0.51`이 확인 흔적이 된다.

### 4. DasLeadConfirmer — `openpilot/sunnypilot/selfdrive/controls/lib/das_lead.py` (신규)

capnp에 의존하지 않는 순수 파이썬이다. 입력은 튜플 리스트, 출력은 슬롯별 bool 두 개다.

**일치 규칙.** DAS 점 `(d, y)`와 모델 후보 `(d_m, y_m)`이 `|d − d_m| < max(0.25·d_m, 5.0)`이고 `|y − y_m| < 1.5`면 일치한다. 거리 식은 radard의 `dist_sane`과 같다(`radard.py:132`).

**자기검증.**
- 증거 프레임: `v_ego > 5 m/s`, 슬롯 0 후보의 prob > 0.9, DAS lead 점이 있음.
- 증거 값: DAS lead 점이 슬롯 0 후보와 일치하면 1, 아니면 0. 최근 200개(20 Hz로 10초 분량)를 보관한다.
- 켜짐: 증거 100개(5초 분량) 이상이고 평균 0.8 이상. 꺼짐: 평균 0.6 미만. 프로세스 시작(주행)마다 초기화한다.
- 상태가 바뀔 때 `cloudlog.info` 한 줄을 남긴다.

**확인 (슬롯 i).** 스위치 켜짐, 자기검증 켜짐, 후보 prob ≥ 0.2, DAS 점(lead 또는 cut-in) 중 하나가 후보와 일치. 마지막 일치 뒤 0.5초(10프레임)는 확인을 유지한다. DAS 값이 깜빡여도 앞차가 깜빡이지 않게 하려는 것이다. 유지 중이라도 스위치나 자기검증이 꺼지면 바로 멈춘다.

여기서 후보 prob은 필터를 거치지 않은 모델 prob(`leadsV3[i].prob`)이다. 자기검증의 0.9도 같다.

**스위치.** `TeslaDasLeadConfirm`을 1초마다 읽는다. `openpilot/sunnypilot/selfdrive/controls/lib/long_cost_tuning.py`의 `LongCostTuningController`와 같은 방식이고, 테스트용으로 `params`를 주입할 수 있다.

**조정 상수.** 모듈 상단에 이름을 붙여 둔다: 최소 후보 prob 0.2, 좌우 허용 1.5 m, 신뢰 켜짐 0.8 / 꺼짐 0.6, 증거 최소 100개 / 보관 200개, 유지 10프레임, 증거 최소 속도 5 m/s.

### 5. 스위치 — Param과 sunnylink

- `openpilot/common/params_keys.h`: `{"TeslaDasLeadConfirm", {PERSISTENT | BACKUP, BOOL, "1"}}`. 기본값은 매니저가 시작할 때 채운다(`openpilot/system/manager/manager.py:56-60`).
- `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/vehicle.yaml`의 Tesla 절에 토글을 넣고, `python sunnypilot/sunnylink/tools/compile_settings_ui.py`로 `settings_ui.json`을 재생성한다.
- `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py`의 `SENSITIVE_PARAMS`에 등록하고, `athena/tests/test_sunnylinkd.py`의 목록도 맞춘다. `StopDistance`(`027c425175`)와 같은 이유다. 휴대폰이 사람 없이 제동 동작을 바꾸지 못하게 한다. 주행 중(engaged) 쓰기는 sunnylinkd가 이미 막는다.

### 6. 로그 분석 스크립트 — `openpilot/sunnypilot/tools/tesla_das_report.py` (신규)

rlog 경로를 받아 보고서를 찍는다. 0x309를 원시 CAN에서 직접 해석하므로, 새 코드가 돌지 않던 예전 로그에도 쓸 수 있다. 확인기는 4절의 클래스를 그대로 다시 돌린다.

1. 버스별 0x309 프레임 수, mux id별 주기.
2. 값 분포: SNA 비율, `RelevantForControl` 비율, Dx 범위.
3. 모델 확신 앞차(prob > 0.9)와 DAS lead의 거리 오차 분포, Dy 부호 일치율.
4. 자기검증이 켜졌을 시점과 켜져 있던 비율.
5. 앞차 등장(필터 prob의 0.5 상향 돌파)마다 확인이 몇 ms 먼저였을지. 확인됐지만 모델이 3초 안에 0.5를 못 넘은 경우(잠재 유령)의 개수.

---

## 동작 요약

| 상황 | 결과 |
|---|---|
| vehicle bus 미연결 (`HW4_GEN2_VEHICLE_BUS` 없음) | DAS 모드 아님. 지금과 동일 |
| 0x309 없음 | 빈 점. 지금과 동일 |
| 0x309 있음, 배치나 단위가 틀림 | 자기검증 불통과. 지금과 동일 |
| 신뢰됨, 후보 prob < 0.2 | 확인 안 함 |
| 신뢰됨, 후보 prob 0.2~0.5, DAS와 자리 일치 | 앞차 인정. 값은 모델, `modelProb` 0.51 |
| 모델 prob > 0.5 | 기존과 동일 |
| 스위치 꺼짐 | 확인 안 함. 점과 자기검증은 그대로 돈다 |

## Safety

### 테스트로 증명하는 것

- 메시지가 없으면 RadarInterface가 20 Hz로 빈 `RadarData`를 내고 오류가 없다.
- 무효 프레임(SNA, relevant 0), 0.5초 넘은 값, 다른 mux id는 점이 되지 않는다.
- DAS 모드 radard는 트랙을 만들지 않는다. 확인은 모델 후보가 있을 때만 일어나고, 앞차 값은 모델 값이다.
- 스위치가 꺼지면 확인이 멈춘다.
- DAS 모드가 아닌 차량은 코드 경로가 같다. CI의 Tesla(HW4 gen2가 아닌 Model Y)·Hyundai 구간에서 card·radard replay 출력이 수정 전과 같다.

### 검증 못 하는 위험

- 주니퍼에서 0x309가 있는지, 배치·Dy 부호·`RelevantForControl`의 뜻이 같은지.
- 자기검증은 거리 배치 오류를 잡지만 "Tesla가 relevant라 부르는 물체"의 뜻 차이는 못 잡는다. 확인은 모델 후보와 자리가 겹칠 때만 일어나므로 영향은 좁다.
- 확인된 앞차의 속도는 prob 0.2~0.5인 모델 추정치다. 평소보다 부정확할 수 있다.

### 검증 순서

1. 단위 테스트, replay 불변 확인. 실차가 필요 없다.
2. 기존 로그에 분석 스크립트: 존재, 배치, 부호.
3. 실차 주행 뒤 분석 스크립트: 확인 시점, 잠재 유령 수.

## Rollback

- sunnylink(민감 쓰기 허용 필요)나 SSH로 `TeslaDasLeadConfirm`을 끄면 1초 안에 확인이 멈춘다. 점은 계속 나오지만 radard가 트랙으로 쓰지 않으므로 오늘과 같다.
- 코드: 본체 커밋과 opendbc 핀을 되돌린다.

## 파일별 변경

**opendbc** (서브모듈. `lsjpwr/opendbc`에 새 브랜치로 푸시)

- `opendbc/dbc/tesla_model3_vehicle.dbc`
- `opendbc/car/tesla/radar_interface.py`
- `opendbc/car/tesla/tests/test_radar_interface.py` (신규)

**본체**

- `openpilot/selfdrive/controls/radard.py`
- `openpilot/sunnypilot/selfdrive/controls/lib/das_lead.py` (신규)
- `openpilot/sunnypilot/selfdrive/controls/lib/tests/test_das_lead.py` (신규)
- `openpilot/common/params_keys.h`
- `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/vehicle.yaml`, `openpilot/sunnypilot/sunnylink/settings_ui.json`(재생성)
- `openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py`, `openpilot/sunnypilot/sunnylink/athena/tests/test_sunnylinkd.py`
- `openpilot/sunnypilot/tools/tesla_das_report.py` (신규)
- `opendbc_repo` 핀

## 로그 확인 뒤 다시 볼 가정

- Dy 부호(왼쪽이 +).
- `RelevantForControl == 1` 요구. 주니퍼에서 이 비트가 켜지지 않으면 기능이 조용히 꺼져 있게 된다. 분석 스크립트 2번 항목이 알려준다.
- 4절의 조정 상수.
