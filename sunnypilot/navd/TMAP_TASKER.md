# TMAP → Tasker → tmapd 연동 가이드

폰에서 TMAP 알림을 Tasker로 잡아 comma 디바이스의 `tmapd`(포트 5505)로 전달합니다.
디바이스는 폰 핫스팟에 연결되어 있어야 하며, 써니링크에서 `TMAP Alerts (Tasker)` 토글을 켜야 합니다.

## 1. 디바이스 IP 확인

폰 핫스팟에서 comma 디바이스의 IP를 확인합니다 (보통 `192.168.x.x`, 핫스팟 설정 > 연결된 기기).
고정성이 필요하면 Tasker 변수 `%COMMA_IP`로 저장해두고 아래에서 사용하세요.

연결 테스트:
```
GET http://<디바이스IP>:5505/v1/ping   →  {"status": "ok"}
```

## 2. Tasker 프로필

- **Profile**: Event > UI > Notification, Owner Application: `TMAP`
- **Task**: 알림 텍스트(`%evtprm2`, `%evtprm3`)를 파싱해 HTTP Request 액션 실행

### HTTP Request 액션 설정

- Method: `POST`
- URL: `http://%COMMA_IP:5505/v1/alert`
- Headers: `Content-Type: application/json`
- Body:
```json
{"type": "cam_fixed", "speed_limit": 50, "distance": 600}
```

### type 값

| type | 의미 | speed_limit |
|---|---|---|
| `cam_fixed` | 고정식 과속단속 | 필수 (km/h) |
| `cam_mobile` | 이동식 단속 | 필수 (km/h) |
| `cam_section_start` | 구간단속 시작 | 필수 (km/h) |
| `cam_section_end` | 구간단속 종료 | 필수 (km/h) |
| `speed_bump` | 과속방지턱 | 생략 |

`distance`는 TMAP 알림이 표시하는 잔여 거리(m)입니다. 알림 텍스트에서
정규식으로 추출하세요 (예: `(\d+)m 앞.*단속`). 같은 타입의 알림이 갱신되면
다시 POST하면 됩니다 — tmapd가 최신 값으로 교체하고, 이후에는 차량 속도로
잔여 거리를 자동 감쇠시킵니다.

## 3. 동작 확인

PC나 폰 터미널에서 가짜 알림 주입:
```bash
curl -X POST http://<디바이스IP>:5505/v1/alert \
  -H "Content-Type: application/json" \
  -d '{"type": "cam_fixed", "speed_limit": 50, "distance": 600}'
```

디바이스 SSH에서 수신 확인:
```bash
cd /data/openpilot && python3 -c "
import cereal.messaging as messaging
sm = messaging.SubMaster(['externalNavDataSP'])
sm.update(2000)
for a in sm['externalNavDataSP'].alerts:
    print(a.alertType, f'{a.distance:.0f}m', f'{a.speedLimit*3.6:.0f}km/h')"
```

## 4. 경로 인지형 제어 (선택)

써니링크에서 `TMAP: API Key`에 [openapi.sk.com](https://openapi.sk.com)의 appKey를 입력하고
`TMAP: Route Control`을 켜면, 목적지를 전달했을 때 경로상 회전·고속도로 진출입로
앞에서 자동 감속합니다. 써니링크에서 텍스트 입력이 안 되면 폰 브라우저/터미널에서 한 번만:

```bash
curl -X POST http://<디바이스IP>:5505/v1/apikey \
  -H "Content-Type: application/json" -d '{"key": "YOUR_APP_KEY"}'
```

또는 SSH로 `echo -n 'YOUR_APP_KEY' > /data/params/d/TmapApiKey`. 한 번 저장하면 유지됩니다.

**경로 API는 목적지 설정 1회 + 경로 이탈 시 재탐색(60초 쿨다운)**만
호출하므로 무료티어(일 1,000건)로 충분합니다.

```
POST /v1/route        {"lat": 37.49, "lon": 127.02, "name": "강남역"}
POST /v1/route/clear  {}    ← 안내 종료 시
```

목적지 좌표는 Tasker에서 TMAP 안내 시작을 감지해 보내거나(공유 인텐트/클립보드 파싱),
수동으로 한 번 POST해도 됩니다.

## 5. Tasker 프로젝트 가져오기

레포의 `sunnypilot/navd/tmap_sunnypilot.tsk.xml`을 폰으로 복사한 뒤:

1. Tasker 홈 하단 탭 길게 누름 > **가져오기(Import Project)** > 파일 선택
2. 전역 변수 `%COMMA_IP`에 디바이스 IP 설정 (변수 탭에서 추가)
3. Tasker에 **알림 접근 권한** 부여 (Android 설정 > 알림 접근)
4. 프로필 `TMAP Notification Relay`의 Owner Application이 설치된 TMAP 패키지와
   일치하는지 확인 (기본 `com.skt.tmap.ku`)

포함된 구성:
- **프로필** TMAP Notification Relay → **태스크** TMAP Alert Send: 알림 텍스트에서
  거리(`NNNm`)·제한속도(`NN km/h`)를 정규식으로 뽑고, 키워드(고정/이동식/구간단속/방지턱)로
  타입을 분류해 POST. 매칭 실패 시 전송하지 않음.
- **태스크** TMAP Route Start (`%par1`=위도, `%par2`=경도, `%par3`=이름): 목적지 전달용.
  다른 프로필/씬/위젯에서 Perform Task로 호출하세요.
- **태스크** TMAP Route Clear: 안내 종료 시 호출.

> **중요**: TMAP 알림 문구는 버전마다 다릅니다. 가져온 뒤 실제 알림으로 한 번
> 주행 테스트를 하고, `TMAP Alert Send` 태스크의 If 조건 키워드와 정규식을
> 본인 TMAP 버전의 문구에 맞게 다듬으세요. Tasker 버전에 따라 XML import가
> 거부되면, 위 구조(변수 추출 → 타입 분류 → HTTP Request)를 수동으로 만들면 됩니다.

## 제한사항

- 타입별로 활성 알림 1개만 유지합니다 (같은 타입 연속 카메라는 최신 것으로 교체).
- 알림은 120초 또는 통과 후 50m가 지나면 자동 삭제됩니다.
- 인증이 없으므로 핫스팟 네트워크에 신뢰할 수 없는 기기를 연결하지 마세요.
