# 테슬라 내비 목적지 연결 (1회 설정)

테슬라 내비의 목적지를 콤마 기기가 테슬라 서버에서 읽어 온다. 폰 앱 KNaviLink(sp-knavlink-tesla)로 안내를 시작하면 그 목적지를 기기가 차에 보내고, 차에서 바꾼 목적지는 폰 앱이 따라간다. 한 번 설정하면 운전할 때마다 할 일은 없다. 설계: `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`.

**방법은 두 가지다.** 둘 다 설정돼 있으면 기기는 Fleet을 쓴다.

| | Fleet (추천) | owner (비공식) |
|---|---|---|
| 설정 | PC에서 아래 1~4단계, 약 1시간 | PC에서 토큰 발급, 폰 Termius로 명령 두 줄, 약 10분 (아래 "owner 모드" 절) |
| 토큰 권한 | 차량 정보·위치 읽기와 차량 명령. 기기가 보내는 명령은 목적지 하나뿐이다 | 테슬라 앱 로그인과 같은 계정 전체 권한 |
| 요금 | 월 $10 공제 안에서 0원(결제 정보 등록 필요) | 없음 |
| 수명 | 테슬라 공식 API | 테슬라가 2026년에 단계적으로 닫는 중이라 언제 막힐지 모른다 |

**걸리는 시간:** 약 1시간. 대부분은 테슬라 개발자 사이트 입력이다.

**필요한 것 (Fleet 방법)**
- GitHub 계정
- 차량 소유자의 테슬라 계정
- 이 저장소가 있는 PC(Git Bash의 `openssl`, PowerShell에서 되는 `ssh`)
- 기기 SSH 접속(`ssh comma@<기기IP>`가 되는 상태)
- 기기 설정: `KoreaExternalNavEnabled` 켜짐, knavlink의 UDP 항목 패킷이 5초 안에 수신됨. 기기에 경로 API 키를 입력하지 않는다. 경로 선은 폰에서만 받으며 폰이 끊기면 새 Tesla 요청도 보내지 않는다.
- 기기에 커밋 `1eecaa7c2` 이후 빌드가 먼저 설치되어 있을 것. 이 기능이 없는 빌드는 시작할 때 자기가 모르는 파라미터 파일을 지워 토큰이 사라지고, 그 커밋 전 빌드는 토큰을 업로드되는 로그에 남긴다.

## 1. 공개키 만들기 (2분)

테슬라는 앱을 등록할 때, 도메인에 올린 공개키를 확인한다. 목적지 명령은 서명 없이 보내므로 개인키를 쓰지 않지만, 버리지 말고 저장소 밖에 보관한다.

```bash
openssl ecparam -name prime256v1 -genkey -noout -out tesla-private.pem
openssl ec -in tesla-private.pem -pubout -out com.tesla.3p.public-key.pem
```

## 2. GitHub Pages에 올리기 (10분)

1. GitHub에서 `<아이디>.github.io` 이름으로 공개 저장소를 만든다.
2. "Add file → Create new file"로 빈 파일 `.nojekyll`을 만든다. 이 파일이 없으면 `.well-known` 폴더가 공개되지 않는다.
3. 같은 방법으로 `.well-known/appspecific/com.tesla.3p.public-key.pem`을 만들고, 1단계에서 만든 `com.tesla.3p.public-key.pem`의 내용을 붙여넣는다.
4. 몇 분 뒤 브라우저에서 `https://<아이디>.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem`이 열리면 된다.

## 3. 테슬라 개발자 앱 등록 (20분)

1. https://developer.tesla.com 에 테슬라 계정으로 로그인하고 앱을 새로 만든다.
2. 이름과 설명은 자유롭게 적는다. 용도에는 "개인 차량의 내비 목적지 읽기와 보내기"라고 적는다.
3. OAuth 방식은 "Authorization Code and Machine-to-Machine"을 고른다.
4. Allowed Origin: `https://<아이디>.github.io`
5. Allowed Redirect URI: `https://<아이디>.github.io/callback`
6. 권한(Scopes)은 **Vehicle Information**, **Vehicle Location**, **Vehicle Commands** 세 개를 고른다. 충전·에너지 권한은 고르지 않는다.
7. 결제 정보를 요구하면 등록한다. 월 $10 공제 안에서는 청구액이 0원이다(아래 비용 절).
8. 만들어진 앱의 **Client ID**와 **Client Secret**을 확인한다. 어디에도 저장하거나 붙여 두지 않는다. 4단계에서 한 번 입력하면 끝이다.

## 4. 설정 스크립트 실행 (5분)

먼저 테슬라 앱을 열어 차를 깨워 둔다(연결 확인용). 그다음 PowerShell이나 Windows Terminal을 열고 PC의 저장소 루트에서 실행한다. Git Bash 창에서는 Client Secret 입력이 멈추거나 화면에 보일 수 있다.

```powershell
python -m openpilot.sunnypilot.mapd.korea.tesla_setup --host comma@<기기IP>
```

스크립트는 테슬라 로그인 전에 기기 SSH 접속부터 확인한다. 여기서 멈추면 `ssh comma@<기기IP>`가 되는지 먼저 본다.

1. Client ID, Client Secret(입력이 화면에 보이지 않는다), 도메인(`<아이디>.github.io`)을 입력한다. Redirect URI는 Enter로 기본값을 쓴다.
2. 브라우저가 열리면 테슬라에 로그인하고 동의한다.
3. 넘어간 페이지가 404여도 된다. 주소창의 주소 전체를 복사해 스크립트에 붙여넣는다.
4. 차가 여러 대면 번호를 고른다.
5. `Connection OK`와 `Saved to the device`가 나오면 끝이다.

스크립트는 토큰을 화면에 출력하지 않고, PC에 저장하지도 않는다.

### 이미 설정한 경우: 차량 명령 권한 추가 (2026-10)

예전 설정은 읽기 권한만 받았다. 폰 앱의 목적지를 차에 보내려면 한 번 더 한다.

1. developer.tesla.com의 앱 설정에서 **Vehicle Commands** 권한을 추가한다.
2. 4단계를 다시 실행한다. 권한은 로그인할 때 토큰에 붙으므로, 앱에 권한만 추가해서는 기기의 토큰이 바뀌지 않는다.
3. 동의 화면에 차량 명령 항목이 있는지 보고 허용한다.

권한 없이도 목적지 읽기와 폰 앱이 차를 따라가는 기능은 그대로 동작한다. 차로 보내기만 안 된다.

## owner 모드 (개발자 등록 없이, 비공식)

Fleet 설정 1~4단계 대신 쓰는 방법이다. 토큰 발급(tesla_auth)에 PC가 한 번 필요하고, 기기 설정은 폰 Termius로 끝난다. 위 표의 권한·수명 차이를 알고 쓴다. 기기 SSH 접속(Termius), `KoreaExternalNavEnabled` 켜짐, knavlink 연결, 커밋 `1eecaa7c2` 이후 빌드가 필요하다. 그 전 빌드는 시작할 때 owner 토큰 파일을 지우거나, 토큰을 업로드되는 로그에 남긴다.

**주의**
- **NaviToTesla에 넣은 토큰을 복사하지 않는다.** 갱신 토큰은 한 번 쓰면 무효가 되어, 기기와 NaviToTesla가 서로를 끊는다. 기기용은 새로 로그인해 따로 받는다.
- 2026-04에 테슬라가 로그인 방식을 바꿔 예전 토큰 발급 도구 대부분이 `redirect_uri` 오류로 막혔다. tesla_auth는 0.13.0에서 고쳐졌으니 0.13.0 이상을 쓴다. 예전에 받아 둔 버전이면 새로 받는다.
- 이 토큰은 계정 전체 권한이다. 채팅·메모·스크린샷에 남기지 않는다.
- 토큰을 붙여넣은 뒤에는 클립보드 기록에서 지운다. PC는 `Win+V`에서 지우고, 폰으로 옮겼으면 폰 클립보드와 옮길 때 쓴 채팅에서도 지운다.

1. PC에서 tesla_auth를 받아(https://github.com/adriankumpf/tesla_auth/releases/latest , Windows는 x64 zip) 실행하고 테슬라에 새로 로그인한다(MFA 코드 포함). 마지막 창의 **Refresh Token**을 복사한다(Access Token 아님). PC PowerShell에서 `ssh comma@<기기IP>`가 되면 2~4번을 PC에서 한다. 그러면 토큰을 폰으로 옮기지 않아도 된다. 안 되면 토큰을 폰으로 옮겨 Termius에서 한다.
2. 기기에 접속해 토큰을 넣는다. 명령을 실행하고, 토큰을 붙여넣고, Enter를 누른다. 붙여넣은 입력이 화면에 안 보이는 게 정상이다:
   ```bash
   read -rs V && printf '%s' "$V" > /data/params/d/.KoreaTeslaOwnerRefreshToken.tmp && mv -f /data/params/d/.KoreaTeslaOwnerRefreshToken.tmp /data/params/d/KoreaTeslaOwnerRefreshToken && unset V
   ```
3. VIN(17자리, 차 화면 컨트롤 → 소프트웨어)을 넣는다:
   ```bash
   read -r V && printf '%s' "$V" > /data/params/d/KoreaTeslaVin && unset V
   ```
4. 두 값이 들어갔는지 본다. 값 대신 글자 수만 나오고, 둘 다 0이 아니면 된다. 재부팅은 필요 없다:
   ```bash
   wc -c /data/params/d/KoreaTeslaOwnerRefreshToken /data/params/d/KoreaTeslaVin
   ```

두 명령은 Termius **Snippets**에 저장해 두면 탭 한 번으로 실행된다. 명령 안에 토큰이 없어서 저장해도 안전하다.

**나중에 Fleet으로 옮기기:** Fleet 설정 1~4단계를 하면 기기가 다음 조회부터 Fleet을 쓴다. 그다음 기기 셸(Termius)에서 owner 토큰을 지운다:
```bash
rm /data/params/d/KoreaTeslaOwnerRefreshToken
```

## 확인 (다음 주행)

1. 주행을 시작하고 NaviToTesla로 목적지를 보낸다.
2. 1분 안에 목적지가 기기에 들어온다:
   ```bash
   ssh comma@<기기IP> cat /data/params/d/NavDestination
   ```
3. 테슬라에서 안내를 취소하면 2분 안에 비워진다(빈 응답 두 번 연속일 때만 취소로 본다).
4. KNaviLink에서 안내를 시작하면 1분 안에 차 화면에 같은 목적지가 뜬다. 처음 한 번은 차 화면의 위치가 폰과 같은지 본다.
5. 첫 주행이 끝나면 토큰이 새것으로 바뀌었는지 본다. 설정한 시각보다 늦은 시각이 나오면 토큰 교체가 정상이다(owner 모드면 파일 이름이 `KoreaTeslaOwnerRefreshToken`이다):
   ```bash
   ssh comma@<기기IP> stat -c %y /data/params/d/KoreaTeslaRefreshToken
   ```
6. owner 모드라면 NaviToTesla로 목적지를 보낸 뒤에도 NaviToTesla가 계속 동작하는지 본다. 끊기면 두 곳이 같은 토큰을 쓰고 있는 것이다.

## 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| 주차 화면에 "Tesla connection lost" | 갱신 토큰이 거부됐다. 3개월 넘게 운전하지 않았거나, 테슬라 계정에서 앱 접근을 끊었다 | 4단계를 다시 실행 |
| 스크립트가 `WARNING: Tesla answered without the car's location` | 동의 화면에서 위치 권한이 빠졌다 | 4단계를 다시 실행하고 위치를 허용 |
| 스크립트가 `Tesla answered HTTP 4xx`로 멈춤 | 2단계 공개키가 안 보이거나, 도메인이 앱 설정과 다르다 | 2단계 4번의 주소가 열리는지 본다 |
| 목적지가 안 들어오고 알림도 없음 | `KoreaExternalNavEnabled`가 꺼졌거나 `KoreaRouteApiKey`가 없다 | 설정에서 켜고 키를 넣는다 |
| 설정을 마쳤는데 목적지가 안 들어오고 알림도 없음 | 기기가 이전 빌드여서 토큰 파일을 지웠다 | `ssh comma@<기기IP> ls -l /data/params/d/KoreaTesla*`에서 넣은 파일의 크기가 0이 아니어야 한다(기기는 등록된 키마다 빈 파일을 만든다). 0이면 기기를 업데이트하고 4단계(owner 모드면 owner 모드 2~3번)를 다시 한다 |
| 설정 후 3개월이 안 됐는데 "Tesla connection lost" | 테슬라가 다른 이유로 앱을 계속 거부한다(결제, 계정에서 차량 삭제, 앱 등록) | developer.tesla.com에서 앱과 결제 상태를 확인하고 4단계를 다시 실행 |
| owner 모드에서 "Tesla connection lost" | 토큰이 이미 쓰였거나(NaviToTesla와 같은 토큰) 폐기됐다. 또는 VIN이 그 계정에 없다 | VIN이 틀렸으면 owner 모드 3번으로 VIN만 다시 넣는다(재시작 없이 다음 조회에서 다시 시도한다). VIN이 맞으면 토큰을 새로 발급해 owner 모드 2번을 다시 한다 |
| owner 모드에서 새 토큰을 넣어도 곧바로 다시 알림 | 이 계정에서 owner API가 막혔다(403) | Fleet 설정(1~4단계)으로 옮긴다 |

## 비용

기기는 시동이 켜지고 knavlink가 연결돼 있을 때 1분에 한 번 묻는다. 차를 깨우는 호출은 하지 않는다. 기존 401 갱신, 잠든 차의 408, 429의 5분 대기와 지속적인 4xx 거절 처리는 유지한다.

Fleet 월 상한은 **9,000단위**다. 읽기 한 번은 2단위, 명령 한 번은 1단위이며 좌표 명령 실패 뒤 공유 명령과 각 경유지도 각각 센다. 모든 실제 호출 직전에 확인하고, 합계가 정확히 9,000인 예약까지 허용한다. 실패·거절·시간 초과도 이미 예약한 사용량을 돌려주지 않는다. 1단위 $0.001은 명세에서 결정한 계산 기준이며 현재 업체 요금·청구액·무료 공제를 보장하지 않는다.

월 누적은 기기의 `/data/media/0/korea_map/fleet_budget.json`에 저장해 재시작 뒤에도 이어 센다. 잠금 파일 `fleet_budget.lock`으로 겹친 소스 스레드·프로세스의 예약을 직렬화하고, 매번 최신 파일을 읽는다. 임시 파일 쓰기·파일 fsync·교체·디렉터리 fsync가 모두 성공해야 Fleet 호출을 보낸다. 읽기·형식·저장 실패나 기존 장부 소실은 호출을 막는다. 장부와 잠금 파일을 지워 한도를 초기화하지 않는다. 기기 달력의 다음 달에는 새 월 누적을 시작하며 시계가 장부보다 과거 월이면 안전하게 멈춘다.

기존 하루 읽기 300회·명령 60회도 각 실제 호출 전에 적용한다. 일일 누적은 종전처럼 메모리에만 있어 재시작하면 초기화된다. owner 모드에는 Fleet 월 누적을 적용하지 않으며 기존 일일 한도는 유지한다.

## 폰에 전하는 Fleet 상태

comma의 UDP 답에 숫자 `f`를 보낸다. 기존 `ack`, `car`, `route_ack`와 함께 있어도 답은 받은 데이터그램보다 커지지 않는다. 차 이름·좌표보다 `f`와 `route_ack`가 우선이며, 아주 작은 답에서는 `f`가 빠질 수 있다.

| f | 의미 |
|---|---|
| 0 | 사용 가능·복구됨 |
| 1 | 인증·권한·명령 거절 |
| 2 | 하루 상한 |
| 3 | 월 상한 또는 다음 호출을 예약할 여유 없음 |
| 4 | 월 예산 저장소 오류 |

폰은 `f`가 없거나 올바른 정수 0~4가 아니면 알고 있던 상태를 유지한다. 멈춘 상태에서는 차 Follow를 중단하고 comma가 해당 한도·거절에 따라 실제 Tesla 명령을 막는다. 폰 안내·2 Hz 안전운전 전송·트립/목적지 필드·경로 선 전송은 계속한다. 다음 월·일 경계, 저장소 복구, 설정 수정 뒤 실제 사용이 가능해지면 0을 보낸다.

## 끄기

- 잠시 끄기: 설정에서 `KoreaExternalNavEnabled`를 끈다. 경로 기능 전체가 함께 꺼진다.
- 완전히 끄기: 기기의 두 토큰을 모두 지우고, 테슬라 계정 설정의 서드파티 앱 관리에서 이 앱의 접근도 끊는다. owner 토큰이 남아 있으면 Fleet 토큰을 지운 뒤에도 owner 모드로 계속 읽는다.
  ```bash
  ssh comma@<기기IP> rm -f /data/params/d/KoreaTeslaRefreshToken /data/params/d/KoreaTeslaOwnerRefreshToken
  ```
- owner 모드 끄기: 기기의 owner 토큰을 지운다(Termius로 접속한 기기 셸에서). 테슬라 쪽 토큰까지 끊으려면 계정 비밀번호를 바꾸는데, 그러면 NaviToTesla 같은 다른 앱도 다시 로그인해야 한다.
  ```bash
  rm /data/params/d/KoreaTeslaOwnerRefreshToken
  ```
