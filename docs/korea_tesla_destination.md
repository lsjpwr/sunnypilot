# 테슬라 내비 목적지 연결 (1회 설정)

폰에서 NaviToTesla로 테슬라에 보낸 목적지를 콤마 기기가 테슬라 서버에서 읽어 온다. 한 번 설정하면 운전할 때마다 할 일은 없다. 설계: `docs/superpowers/specs/2026-09-25-korea-tesla-destination-design.md`.

**걸리는 시간:** 약 1시간. 대부분은 테슬라 개발자 사이트 입력이다.

**필요한 것**
- GitHub 계정
- 차량 소유자의 테슬라 계정
- 이 저장소가 있는 PC(Git Bash의 `openssl`, `ssh`)
- 기기 SSH 접속(`ssh comma@<기기IP>`가 되는 상태)
- 기기 설정: `KoreaExternalNavEnabled` 켜짐, `KoreaRouteApiKey` 입력됨

## 1. 공개키 만들기 (2분)

테슬라는 앱을 등록할 때, 도메인에 올린 공개키를 확인한다. 이 기능은 차에 명령하지 않아 개인키를 쓰지 않지만, 버리지 말고 저장소 밖에 보관한다.

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
2. 이름과 설명은 자유롭게 적는다. 용도에는 "개인 차량의 내비 목적지 읽기"라고 적는다.
3. OAuth 방식은 "Authorization Code and Machine-to-Machine"을 고른다.
4. Allowed Origin: `https://<아이디>.github.io`
5. Allowed Redirect URI: `https://<아이디>.github.io/callback`
6. 권한(Scopes)은 **Vehicle Information**과 **Vehicle Location** 두 개만 고른다. 명령 권한은 고르지 않는다.
7. 결제 정보를 요구하면 등록한다. 월 $10 공제 안에서는 청구액이 0원이다(아래 비용 절).
8. 만들어진 앱의 **Client ID**와 **Client Secret**을 확인한다. 어디에도 저장하거나 붙여 두지 않는다. 4단계에서 한 번 입력하면 끝이다.

## 4. 설정 스크립트 실행 (5분)

먼저 테슬라 앱을 열어 차를 깨워 둔다(연결 확인용). 그다음 PC의 저장소 루트에서:

```bash
python -m openpilot.sunnypilot.mapd.korea.tesla_setup --host comma@<기기IP>
```

1. Client ID, Client Secret(입력이 화면에 보이지 않는다), 도메인(`<아이디>.github.io`)을 입력한다. Redirect URI는 Enter로 기본값을 쓴다.
2. 브라우저가 열리면 테슬라에 로그인하고 동의한다.
3. 넘어간 페이지가 404여도 된다. 주소창의 주소 전체를 복사해 스크립트에 붙여넣는다.
4. 차가 여러 대면 번호를 고른다.
5. `Connection OK`와 `Saved to the device`가 나오면 끝이다.

스크립트는 토큰을 화면에 출력하지 않고, PC에 저장하지도 않는다.

## 확인 (다음 주행)

1. 주행을 시작하고 NaviToTesla로 목적지를 보낸다.
2. 1분 안에 목적지가 기기에 들어온다:
   ```bash
   ssh comma@<기기IP> cat /data/params/d/NavDestination
   ```
3. 테슬라에서 안내를 취소하면 1분 안에 비워진다.

## 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| 주차 화면에 "Tesla connection lost" | 갱신 토큰이 거부됐다. 3개월 넘게 운전하지 않았거나, 테슬라 계정에서 앱 접근을 끊었다 | 4단계를 다시 실행 |
| 스크립트가 `WARNING: Tesla answered without the car's location` | 동의 화면에서 위치 권한이 빠졌다 | 4단계를 다시 실행하고 위치를 허용 |
| 스크립트가 `Tesla answered HTTP 4xx`로 멈춤 | 2단계 공개키가 안 보이거나, 도메인이 앱 설정과 다르다 | 2단계 4번의 주소가 열리는지 본다 |
| 목적지가 안 들어오고 알림도 없음 | `KoreaExternalNavEnabled`가 꺼졌거나 `KoreaRouteApiKey`가 없다 | 설정에서 켜고 키를 넣는다 |

## 비용

테슬라 데이터 요청은 500회당 $1이고, 계정마다 매달 $10를 깎아 준다. 기기는 주행 중에만 1분에 한 번 묻는다.

| 주행 | 요청 | 금액 |
|---|---|---|
| 1시간 | 60회 | $0.12 |
| 한 달 83시간까지 | 5,000회 | 공제로 0원 |

하루 300회(주행 5시간 분량)를 넘으면 그날은 더 묻지 않는다.

## 끄기

- 잠시 끄기: 설정에서 `KoreaExternalNavEnabled`를 끈다. 경로 기능 전체가 함께 꺼진다.
- 완전히 끄기: 기기의 토큰을 지우고, 테슬라 계정 설정의 서드파티 앱 관리에서 이 앱의 접근도 끊는다.
  ```bash
  ssh comma@<기기IP> rm /data/params/d/KoreaTeslaRefreshToken
  ```
