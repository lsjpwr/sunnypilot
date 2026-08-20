# 한국 지도 DB 갱신

지도 데이터는 **두 파일로 나뉘어** 있고, 갱신 방식이 서로 다르다.

| 파일 | 크기 | 출처 | 갱신 |
|---|---|---|---|
| `korea_cameras.sqlite` | ~3 MB | data.go.kr 무인교통단속카메라 API | **디바이스가 자동으로** (주 1회) |
| `korea_links.sqlite` | ~220 MB | ITS 전국표준노드링크 | **수동** (분기마다, 아래 절차) |

카메라가 자주 바뀌고 크기가 작아서 자동 갱신 대상이고, 링크는 크고 거의 안 바뀌어서 수동이다. 이 분리가 두 파일로 나눈 이유 자체다.

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

## 확인

지도 DB가 없으면 오프로드 화면에 경고가 뜬다:

> Korean map database not found. Speed limit assist and road name display are unavailable until it is copied to the device.

정상 동작 확인:

```bash
ssh comma@<device-ip> "ls -la /data/media/0/korea_map/"
```

두 파일이 다 있어야 한다. 하나만 있으면 `mapd_manager`는 DB를 열지 않고 계속 대기한다 — 반쪽짜리 DB는 쓸 수 없기 때문이다.
