# 한국 지도 DB 갱신

지도 데이터는 **세 파일로 나뉘어** 있고, 갱신 방식이 서로 다르다.

| 파일 | 크기 | 출처 | 갱신 |
|---|---|---|---|
| `korea_cameras.sqlite` | ~3 MB | data.go.kr 무인교통단속카메라 API | **디바이스가 자동으로** (주 1회) |
| `korea_links.sqlite` | ~220 MB | ITS 전국표준노드링크 | **수동 빌드 + 릴리스 발행, 디바이스가 자동 수신** (분기마다, 아래 절차) |
| `korea_bumps.sqlite` | ~11 MB | data.go.kr 전국과속방지턱표준데이터 | **수동 빌드 + 릴리스 발행, 디바이스가 자동 수신, 선택 사항** (필요할 때, 아래 절차) |

카메라가 자주 바뀌고 크기가 작아서 자동 갱신 대상이고, 링크는 크고 거의 안 바뀌어서 수동이다. 방지턱은 링크보다도 더 안 바뀌는 데다 있어도 그만 없어도 그만인 선택 사항이라, 파일 하나를 통째로 더 나누게 됐다.

---

## 1. 단속카메라 — 최초 1회만 설정

API 키를 디바이스에 넣어두면 그 뒤로는 알아서 갱신된다.

1. https://www.data.go.kr/data/15028200/standard.do 에서 활용 신청 → **일반 인증키** 발급
2. 디바이스에 넣는다 (인코딩/디코딩 어느 형태든 상관없다):

   ```bash
   ssh comma@<device-ip> "echo -n '<발급받은 키>' > /data/params/d/KoreaMapApiKey"
   ```

3. 재부팅. 이후 `mapd_manager`가 주 1회 확인한다.

**갱신 조건과 안전장치**

- 네트워크가 종량제(`networkMetered`)면 건너뛴다
- 받아온 행 수가 기존 DB의 80% 미만이면 **교체하지 않는다** — API 부분 장애 때 200 응답에 짧은 페이지가 오는 경우가 있는데, 그대로 쓰면 카메라 DB가 조용히 비워진다
- 교체는 원자적이라 **주행 중에도 안전하다.** 임시 파일에 새로 만든 뒤 `os.replace`로 바꾸므로, 조회 중이던 프로세스는 옛 파일을 계속 읽고 다음 tick에서 새 파일로 넘어간다. 재부팅이 필요 없다

키를 넣지 않아도 동작한다 — 아래 절차로 만든 파일을 그대로 쓰면 된다. 자동 갱신만 안 될 뿐이다.

---

## 2. 링크 DB — 분기마다 수동

ITS가 새 노드링크를 발행하면 PC에서 다시 만들어 올린다.

### 2-1. 원본 받기

- 링크: https://www.its.go.kr/nodelink/ → 전국표준노드링크 다운로드 후 압축 해제 (`MOCT_LINK.shp` 등)
- 카메라 CSV(선택): https://www.data.go.kr/data/15028200/standard.do
  API 키를 설정했다면 카메라는 다시 만들 필요가 없다. 링크만 만들려면 `--links`만 주면 된다.

### 2-2. 빌드

`pyshp`와 `pyproj`가 필요하다. **PC 전용이며 디바이스에는 설치하지 않는다** — 그래서 `build_db.py`는 이 둘을 모듈 최상단에서 import하지 않는다.

```bash
pip install pyshp pyproj

python -m openpilot.sunnypilot.mapd.korea.build_db \
    --links MOCT_LINK.shp \
    --out-links korea_links.sqlite
```

카메라도 함께 만들려면:

```bash
python -m openpilot.sunnypilot.mapd.korea.build_db \
    --cameras 전국무인교통단속카메라표준데이터.csv \
    --links MOCT_LINK.shp \
    --out-cameras korea_cameras.sqlite \
    --out-links korea_links.sqlite
```

링크 빌드는 2~3분 걸린다. 좌표계는 `.prj` 파일을 읽어 자동 판별한다 — ITS 원본은 `.prj`가 없는 경우가 있고, 그때는 UTM-K로 간주해 재투영한다.

### 2-3. 검증

올리기 **전에** 확인한다. 220 MB를 올려놓고 디바이스에서 안 열리는 걸 발견하는 게 가장 비싼 실패다.

```bash
python -m openpilot.sunnypilot.mapd.korea.deploy \
    --cameras korea_cameras.sqlite --links korea_links.sqlite
```

기대 출력:

```
korea_cameras.sqlite: 33415 rows in cameras
korea_links.sqlite: 1557364 rows in links
verified. pass --host to deploy.
```

행 수는 데이터가 갱신되면 달라진다. 자릿수가 크게 다르면 원본이나 빌드가 잘못된 것이다.

### 2-4. 디바이스로 복사

같은 명령에 `--host`만 더한다. 복사 후 원격에서 sha256을 대조한다.

```bash
python -m openpilot.sunnypilot.mapd.korea.deploy \
    --cameras korea_cameras.sqlite --links korea_links.sqlite \
    --host comma@<device-ip>
```

파일은 `/data/media/0/korea_map/` 에 놓인다.

### 2-5. 재부팅

링크 DB는 프로세스 시작 시 한 번만 열리므로 **재부팅이 필요하다.** (카메라 DB만 무중단 교체를 지원한다.)

---

## 3. 방지턱 DB — 선택 사항, 수동

카메라·링크와 같은 파이프라인(`build_db.py` → `deploy.py`)을 타지만, 셋 중 유일하게 없어도 그만인 파일이다. 기기에 `korea_bumps.sqlite`가 없어도 카메라 갱신과 속도제한은 그대로 동작하고, 방지턱 감속만 아무 일도 하지 않는다.

### 3-1. 원본 받기

- 전국과속방지턱표준데이터: https://www.data.go.kr/data/15028195/standard.do
- 직접 내려받기: 위 페이지는 HTML 랜딩 페이지라 그대로 열면 CSV가 나오지 않는다. 실제 파일은
  `/file/speed_bump_info/info`가 아니라 `/file/download/`가 붙은 경로에 있고, User-Agent 없이는
  403이 난다. 랜딩 페이지를 먼저 방문해 쿠키를 받은 뒤 그 쿠키로 다운로드 경로를 호출한다:

  ```bash
  UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
  curl -sL -c jar.txt -A "$UA" -o /dev/null "https://file.localdata.go.kr/file/speed_bump_info/info"
  curl -sL -b jar.txt -A "$UA" -H "Referer: https://file.localdata.go.kr/file/speed_bump_info/info" \
      -o 전국과속방지턱표준데이터.csv "https://file.localdata.go.kr/file/download/speed_bump_info/info"
  ```

### 3-2. 빌드

```bash
python -m openpilot.sunnypilot.mapd.korea.build_db \
    --bumps 전국과속방지턱표준데이터.csv --out-bumps korea_bumps.sqlite
```

### 3-3. 배포

카메라·링크와 같은 명령이 존재하는 파일만 골라 검증하고 올린다. 방지턱 파일이 있으면 자동으로 같이 올라간다 — 파일명이 기본값(`korea_bumps.sqlite`)과 같다면 별도 플래그가 필요 없다:

```bash
python -m openpilot.sunnypilot.mapd.korea.deploy --host comma@<device-ip>
```

파일은 `/data/media/0/korea_map/korea_bumps.sqlite`에 놓인다. 링크 DB와 마찬가지로 프로세스 시작 시 한 번만 열리므로 **재부팅이 필요하다.**

방지턱 파일이 없으면 `deploy.py`는 건너뛰고 카메라·링크만 검증·배포한다 — 방지턱 파일 하나가 없다고 나머지 갱신까지 실패하지는 않는다.

**갱신 주기**: 카메라와 달리 자동 갱신 경로가 없다. 방지턱은 거의 변하지 않으므로 필요할 때 수동으로 다시 빌드한다.

### 3-4. 릴리스로 배포하기 (권장)

scp는 디바이스 한 대를 위한 방법이다. 여러 대에 뿌리거나 남에게 나눠줄 거면 릴리스로 올린다.
디바이스는 `KoreaMapAutoDownload`를 켜두면 Wi-Fi에서 알아서 받아간다.

**1. 매니페스트를 만든다.** sha256을 손으로 옮기지 않는다 — 도구가 실제 파일에서 뽑는다.

```bash
python -m openpilot.sunnypilot.mapd.korea.deploy \
  --links out/korea_links.sqlite \
  --bumps out/korea_bumps.sqlite \
  --emit-manifest korea-map-2026.05 \
  > openpilot/sunnypilot/mapd/korea/map_manifest.json
```

태그 이름(`korea-map-2026.05`)은 데이터 기준일로 짓는다. 코드 버전과 무관하다.

**2. 릴리스를 만들고 에셋을 올린다.**

```bash
gh release create korea-map-2026.05 \
  --repo lsjpwr/sunnypilot \
  --title "Korea map data 2026.05" \
  --notes "ITS 노드링크 2026-05, 전국과속방지턱표준데이터 2026-05-15" \
  out/korea_links.sqlite out/korea_bumps.sqlite
```

**3. 매니페스트를 커밋한다.**

```bash
git add openpilot/sunnypilot/mapd/korea/map_manifest.json
git commit -m "chore: publish korea map data 2026.05"
git push
```

순서가 중요하다. 매니페스트를 에셋보다 먼저 푸시하면, 그 사이에 업데이트를 받은 디바이스가
존재하지 않는 URL을 때리고 실패한다. 치명적이지는 않다 — 다음 재시도에서 성공한다 — 하지만
로그에 실패가 남는다.

**디바이스에서 무슨 일이 일어나는가.** 정규 업데이트로 매니페스트를 받는다. `KoreaMapAutoDownload`가
켜져 있고 계량 연결이 아니면, 매니페스트의 sha256과 디스크의 파일을 비교해서 다르면 받는다.
받은 파일은 sha256·스키마 버전·행 수를 전부 통과해야 설치된다. 하나라도 어긋나면 받은 파일을
버리고 기존 파일을 그대로 둔다. 설치된 파일은 재부팅 없이 다음 틱에 반영된다.

**받지 않는 경우와 그 이유가 로그에 남는다:**

| 로그 | 뜻 |
|---|---|
| `KoreaMapAutoDownload is off` | 토글이 꺼져 있다 (기본값) |
| `no deviceState yet, waiting` | 부팅 직후. 계량 여부를 아직 모른다 |
| `network is metered, waiting` | 테더링 등. Wi-Fi에 붙으면 받는다 |
| `needs N bytes free, have M` | `/data/media` 여유 부족 |
| `sha256 ... != ...` | 받은 파일이 매니페스트와 다르다. 에셋을 다시 올려야 한다 |
| `expected at least N` | 파일은 멀쩡한데 행이 모자란다. 빌드가 잘못됐다 |
| `schema ... != ...` | 디바이스 코드가 이 DB보다 오래됐다. 먼저 업데이트해야 한다 |

카메라(`korea_cameras.sqlite`)는 이 경로를 타지 않는다. `camera_refresh.py`가 data.go.kr API로
주 1회 직접 갱신한다.

### 3-5. 설정

크루즈 → 속도 제한 → **Speed Bump Slowdown** (원격 sunnylink에도 같은 항목이 있다). 목표 속도는 원호형 기본 25 km/h, 사다리꼴형 기본 35 km/h. 하한 20 km/h는 `SmartCruiseControl.MIN_V` 때문이다.

가상방지턱(노면표시)은 DB에 저장되지만 조회에서 제외된다 — 물리 충격이 없는 노면 표시라 감속할 이유가 없다.

---

## 확인

지도 DB가 없으면 오프로드 화면에 경고가 뜬다:

> Korean map database not found. Speed limit assist and road name display are unavailable until it is copied to the device.

정상 동작 확인:

```bash
ssh comma@<device-ip> "ls -la /data/media/0/korea_map/"
```

카메라·링크 두 파일이 다 있어야 한다 (방지턱 파일은 선택 사항이라 없어도 된다). 카메라·링크 중 하나만 있으면 `mapd_manager`는 DB를 열지 않고 계속 대기한다 — 반쪽짜리 DB는 쓸 수 없기 때문이다.
