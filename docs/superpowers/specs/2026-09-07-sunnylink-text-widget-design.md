# 써니링크 원격 텍스트 위젯 (Sunnylink Text Widget) 설계

**Goal:** 써니링크 설정 스키마에 자유 입력 문자열 위젯(`widget: text`)을 추가하고, 그 첫 소비자로 `KoreaMapApiKey`(data.go.kr 서비스 키)를 원격 설정 가능하게 만든다. 기기에서 키를 넣는 경로는 이미 있다 — big UI의 Speed Camera API Key 다이얼로그(`cruise_sub_layouts/speed_limit_settings.py:100-107`)와 mici toggles 화면의 버튼(`mici/layouts/settings/toggles.py:84-85`)이다. 이 작업이 더하는 것은 **원격 경로**이며, 그 값어치는 100자 안팎의 base64 서비스 키를 기기 화면 키보드로 치지 않아도 된다는 데 있다.

**Architecture:** 위젯 타입은 세 곳에 중복 정의되어 있다 — 입력 YAML 스키마, 출력 JSON 스키마, 검증 스크립트. 세 곳 모두에 `text`를 추가하고, 아이템 필드로 `secret`(마스킹 힌트)과 `max_length`를 새로 정의한다. 컴파일러(`compile_settings_ui.py`)는 미지 필드를 그대로 통과시키므로 수정하지 않는다. 실제 아이템은 기존 크루즈 페이지가 아니라 **신규 격리 페이지** `korea.yaml`에 둔다 — 써니링크 앱이 미지 위젯을 어떻게 다루는지 알 수 없고, 크루즈 페이지가 mici 사용자의 유일한 원격 설정 경로이기 때문이다.

**Tech Stack:** YAML + JSON Schema, Python 3.11+ (컴파일러/검증기), pytest. 신규 의존성 없음.

---

## Background: 왜 이 작업이 필요한가

### mici에는 설정 화면이 없다

mici 기기의 로컬 UI는 다음 화면만 가진다: developer, device, firehose, network, software, toggles, models, sunnylink, home, onboarding. **cruise, steering, visuals, display, vehicle 화면이 없다.** 따라서 mici 사용자에게 `StopDistance`, DEC, 방지턱 목표 속도, MADS 설정 등은 써니링크 원격이 유일한 경로다.

**단, API 키는 이 목록의 예외다.** mici의 toggles 화면에는 `BigButton("speed camera API key", ...)`가 있고(`openpilot/selfdrive/ui/mici/layouts/settings/toggles.py:84-85`), `MapDataSource == korea`일 때 활성화된다(`:135`). toggles는 mici가 **가진** 화면이다. 즉 mici 사용자가 이 키를 아예 넣을 수 없는 것이 아니다 — 기기 화면 키보드로 100자 안팎의 base64 키를 치는 것이 괴로울 뿐이다. cruise/steering/visuals/display/vehicle에 대한 위 주장은 그대로 유효하고, 이 작업이 메우는 것은 "설정할 방법이 없다"가 아니라 "원격으로 설정할 방법이 없다"다.

**기록 정정.** 커밋 `eee418fe8`의 메시지는 이 구분을 흐린다 — "sunnylink is the only way to configure most of this branch on that hardware. The API key was the one Korean setting with no remote representation at all". 뒷문장은 원격 스키마에 대해서는 사실이지만, 앞문장과 붙여 읽으면 mici 사용자에게 다른 경로가 아예 없다는 인상을 준다. 커밋 메시지는 다시 쓰지 않는다. 두 기록을 대조하는 사람은 이 문서 쪽이 맞다.

### 커버리지는 이미 거의 완전하다

기기 big UI가 노출하는 파라미터와 원격 스키마(`settings_ui.json`, 97개 키)를 비교하면 원격에 없는 것은 4개뿐이다.

| param | 원격에 없는 이유 |
|---|---|
| `SunnylinkEnabled` | 원격으로 써니링크를 켤 수 없다 (닭-달걀). mici 로컬 UI에 있음 |
| `SunnylinkAllowSensitiveWrite` | `BLOCKED_PARAMS` — 영구 차단. 원격 허용 시 민감 게이트 자체가 무의미해진다 |
| `EnableSunnylinkUploader` | `SENSITIVE_PARAMS` (프라이버시) |
| `KoreaMapApiKey` | **스키마에 자유 입력 위젯이 없다** |

앞의 셋은 의도된 보안 결정이다. 남는 실질적 빈틈은 `KoreaMapApiKey` 하나이며, 이 문서의 대상이다.

### 자유 입력 위젯이 존재한 적이 없다

`page.schema.json`의 위젯 enum은 `["toggle", "option", "multiple_button", "button", "info"]`이고, 이는 upstream/master와 동일하다 (2026-09-07 기준 upstream이 우리보다 48커밋 앞서 있으나 `openpilot/sunnypilot/sunnylink/`를 건드린 커밋은 **0개**).

원격 스키마 97개 키 중 **쓰기 가능한 문자열 파라미터가 하나도 없다.** 문자열 파라미터인 `LanguageSetting`조차 `widget: info`(읽기 전용)로 되어 있다. `TorqueControlTune`은 문자열 값을 쓰지만 `multiple_button`의 열거된 옵션이므로 자유 입력이 아니다. `button` 위젯은 스키마에 선언만 되어 있고 어느 페이지도 사용하지 않는다.

---

## Constraints: 조사로 확정된 사실

이 설계는 다음 사실 위에 서 있다. 구현 중 이와 어긋나는 것을 발견하면 설계를 다시 봐야 한다.

1. **앱 버전 협상이 없다.** `getParamsMetadata()`는 인자를 받지 않는다 (`sunnylinkd.py:204`). 기기가 스키마 전체를 무조건 전송하므로, 앱 버전에 따라 다른 내용을 내보낼 방법이 없다.
2. **위젯 enum이 세 곳에 있다.** `settings_ui_src/_schemas/page.schema.json:69`(입력), `settings_ui.schema.json:181-184`(출력), `tools/validate_settings_ui.py:24`의 `VALID_WIDGETS`. 강제력은 서로 다르다 — `page.schema.json`은 어느 스크립트도 읽지 않는 **에디터 힌트 전용**이고(`sunnylink/docs/README.md:25`의 `yaml-language-server` 주석이 유일한 소비자), 실제로 실패를 만드는 것은 `settings_ui.schema.json`(테스트 `test_validator_accepts_real_json`이 `additionalProperties: false`로 검사)과 `VALID_WIDGETS`(`check_structural`)다. 그래도 세 곳을 함께 고친다 — 하나만 빠지면 편집기가 유효한 YAML에 오류 표시를 낸다.

   **정정 (구현 후).** 실제로는 **네 곳**이었다. `tests/test_settings_schema.py:21`이 `VALID_WIDGET_TYPES`라는 네 번째 사본을 들고 있었고, 이 설계도 계획도 그것을 몰랐다. 그래서 Task 1이 넓히지 않았고, `korea` 페이지가 들어온 뒤 `test_all_items_have_key_and_widget`이 컴파일된 스키마를 거부하며 CI를 빨갛게 만들었다. 다섯 번째 동기화 대상을 만드는 대신 그 사본을 지우고 `from openpilot.sunnypilot.sunnylink.tools.validate_settings_ui import VALID_WIDGETS`로 바꿨다. 그래서 지금은 위 세 곳이 정말로 전부다.
3. **컴파일러는 미지 아이템 필드를 통과시킨다.** `_canon_item`(`compile_settings_ui.py:148-153`)이 `_ITEM_KEY_ORDER`에 없는 키를 뒤에 그대로 붙인다. 따라서 `secret`/`max_length`를 위해 컴파일러를 고칠 필요는 없다.
4. **페이지 레벨 `visibility`가 스키마에 없다.** 페이지가 가진 속성은 `id, label, icon, order, remote_configurable, description, kind, sections, items, sub_panels`뿐이다. 조건부 노출은 섹션 또는 아이템 레벨에서만 가능하다.
5. **페이지는 자동 발견된다.** `_load_pages`(`compile_settings_ui.py`)가 `pages/` 아래 `_`로 시작하지 않는 모든 `.yaml`/`.yml`을 읽는다. 파일을 두면 등록된다.
6. **한 파라미터 키는 한 패널에만 존재할 수 있다.** `check_no_duplicate_keys`(`validate_settings_ui.py`)가 패널 간 키 중복을 에러로 잡는다.
7. **`requires_attestation`은 앱 UI 전용이다.** 출력 스키마 설명 그대로 "the UI must show an attestation modal before any write". 기기 쪽 강제는 없다. 기기 쪽 실제 강제는 `SENSITIVE_PARAMS`뿐이다.
8. **`getParams`에는 차단 목록이 없다.** 앱은 이미 `KoreaMapApiKey` 값을 읽을 수 있다. 따라서 `secret`은 표시 위생이지 유출 방지 수단이 아니다. (업스트림 기존 동작이며 이 작업의 범위 밖)

---

## Design

### 1. 위젯 타입 추가

세 곳 모두에 `"text"`를 추가한다.

- `openpilot/sunnypilot/sunnylink/settings_ui_src/_schemas/page.schema.json:69`
- `openpilot/sunnypilot/sunnylink/settings_ui.schema.json:181-184`
- `openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py:24` — `VALID_WIDGETS`

`tests/test_settings_schema.py`도 자기 사본(`VALID_WIDGET_TYPES`)을 들고 있었다(Constraint 2의 정정 참고). 네 번째 목록을 넓히는 대신 지우고 `VALID_WIDGETS`를 임포트하도록 바꿨으므로, 이 세 곳이 전부다.

### 2. 신규 아이템 필드

두 입력/출력 스키마 양쪽의 Item 정의에 추가한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `secret` | boolean | 참이면 앱이 입력을 마스킹하고, 값 대신 설정 여부만 표시해야 한다 |
| `max_length` | integer | 저장 가능한 최대 문자 수 |

`max_length: 255`는 기기 쪽 입력 다이얼로그와 맞춘 값이다 — `InputDialogSP`가 `Keyboard(max_text_size=255)`를 쓴다 (`system/ui/sunnypilot/widgets/input_dialog.py:25`).

컴파일러의 `_ITEM_KEY_ORDER`(`compile_settings_ui.py:107-125`)에 두 필드를 넣어 출력 키 순서를 고정한다. `widget` 다음, `needs_onroad_cycle` 앞에 `secret`과 `max_length`를 둔다. 기능이 아니라 출력 안정성을 위한 조치다.

역방향 도구 `extract_settings_ui.py`의 `_ITEM_ORDER`에도 같은 두 항목을 같은 자리에 넣는다. 두 목록은 같은 계약을 두 곳에 적어둔 것이고(`_ITEM_ORDER`의 주석: "mirrors settings_ui.json conventions"), 둘 다 미지 필드를 뒤에 그대로 붙이므로 기능 차이는 없다 — 갈라진 채로 두지 않기 위한 조치다.

`sunnylink/docs/README.md`의 위젯 표(113-121행)와 아이템 필드 표(123-140행)에 `text`, `secret`, `max_length`를 더한다. 이 표들이 YAML 작성자가 읽는 유일한 레퍼런스다.

### 3. 신규 페이지 `korea.yaml`

`openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml`을 새로 만든다.

```yaml
# Page: korea
# Edit this file. Run compile_settings_ui.py to emit settings_ui.json.
id: korea
label: Korea Map
icon: cruise_control
order: 8
remote_configurable: true
description: Korean public map database credentials
sections:
- id: korea_credentials
  title: ''
  description: ''
  enablement:
  - type: param
    key: MapDataSource
    equals: 1
  items:
  - key: KoreaMapApiKey
    widget: text
    requires_attestation: true
    secret: true
    max_length: 255
    title: Speed Camera API Key
    description: data.go.kr key used to refresh the speed camera database over Wi-Fi.
      Without it the cameras shipped with the database are used as-is. Other Korean
      map settings live under Cruise.
```

세 가지 결정에 이유가 있다.

**격리한 이유.** 앱이 미지 위젯을 만났을 때 그 아이템만 건너뛸지, 페이지를 못 그릴지, 스키마 전체를 거부할지 알 수 없다. 아이템을 크루즈 페이지에 두면 최악의 경우 mici 사용자가 `StopDistance`, DEC, 속도 제한 설정을 전부 잃는다. 별도 페이지면 최악의 경우 잃는 것이 이 페이지 하나뿐이다.

**아이콘을 새로 만들지 않는 이유.** 신규 페이지는 미지 요소를 두 개 만든다 — 미지 위젯과 미지 아이콘 이름. 아이콘 이름은 앱이 애셋으로 매핑하므로, 앱이 모르는 이름을 주면 그 자체로 렌더링이 깨질 수 있다. 기존 `cruise_control`을 재사용해 미지 요소를 하나로 줄인다.

**`order: 8`인 이유.** 현재 사용 중인 순서는 steering 1, cruise 2, display 3, visuals 4, toggles 5, device 6, software 7, developer 9, models 10, vehicle 99다. 8이 비어 있으므로 기존 페이지 번호를 건드리지 않고 삽입할 수 있다.

**오프로드 게이트를 걸지 않는 이유.** 기기 big UI(`speed_limit_settings.py:233`)와 mici(`toggles.py:135`) 모두 API 키 편집을 오프로드로 제한하지 않는다. 이 설정은 포트를 열지 않고 주행 동작을 바꾸지 않는다. 게다가 `saveParams`가 이미 `IsEngaged` 중 모든 쓰기를 차단한다 (`sunnylinkd.py:283`). 파리티를 지킨다.

**섹션에 `enablement`를 건 이유.** 페이지 레벨 `visibility`가 스키마에 없으므로(Constraint 4) 조건은 섹션에 건다. 섹션은 `visibility`와 `enablement`를 모두 받고 렌더링 결과도 사실상 같지만(`sunnylink/docs/README.md:137-138` — 둘 다 숨기지 않고 UNAVAILABLE 배지와 함께 흐리게 표시한다), 다른 한국어 설정은 전부 같은 조건을 `enablement`로 적고 있고(`cruise.yaml:243-246, 254-257, 271-273, 296-298, 321-323`), 두 기기 UI도 이것을 활성/비활성으로 구현한다(`speed_limit_settings.py:233`, `toggles.py:135`). 미지 요소를 줄이는 것이 존재 이유인 페이지에서 주변과 다른 필드를 쓸 이유가 없다. 패널 자체를 숨기지 못하는 것은 받아들인다 — 스키마를 확장하는 것보다 미지 요소를 늘리지 않는 편이 낫다.

### 4. `sunnylinkd.py`는 변경하지 않는다

게이트는 `requires_attestation`(앱 모달)뿐이다. `KoreaMapApiKey`를 `SENSITIVE_PARAMS`에 넣지 않는다.

**게이트가 강해서가 아니다.** `requires_attestation`은 앱 UI 전용 힌트다 — 출력 스키마 설명 그대로 "the UI must show an attestation modal before any write"이고 기기 쪽 강제는 없다(Constraint 7). 세 커밋 전 `027c425175`가 `StopDistance`를 `SENSITIVE_PARAMS`에 넣으며 남긴 이유가 정확히 그것이다: "A YAML enablement rule is a frontend hint with no device-side check, so landing the schema alone would open a window where a phone could shorten the car's stopping gap unattended." 그래서 이 브랜치의 대표 예시인 `StopDistance`는 지금 `SENSITIVE_PARAMS`에 있다(`sunnylinkd.py:76`). 같은 종류의 힌트를 이유로 이 항목만 빼는 것은 게이트의 세기로는 정당화되지 않는다. **차이는 게이트가 아니라 파라미터가 할 수 있는 일에 있다.**

**근거는 폭발 반경이다.** 원격으로 쓰인 값이 만들 수 있는 최악은 과속 카메라 데이터 갱신 실패다. API URL은 하드코딩되어 있고(`camera_refresh.py:31`), 키는 `urllib.parse.urlencode`를 거치므로(`camera_refresh.py:59`) 쓰인 값이 파라미터를 주입하거나 요청을 다른 호스트로 돌릴 수 없다. 잃는 것은 사용자 본인의 저가치 서드파티 자격증명 하나이고, 차의 거동은 바뀌지 않는다. `StopDistance`는 차의 정지 거리를 줄인다. 두 항목을 다르게 다루는 근거는 이 차이뿐이다. 읽기는 `getParams`로 이미 가능하다(Constraint 8).

**"민감 토글을 먼저 켜야 해서"는 근거가 아니다.** mici 사용자가 `StopDistance`를 원격으로 쓰려면 이미 `SunnylinkAllowSensitiveWrite`를 켠 상태여야 한다. 그 사용자에게 `KoreaMapApiKey`를 목록에 더하는 한계 비용은 0이다. 이 결정은 편의가 아니라 위 폭발 반경 위에 선다.

### 5. 검증 규칙

`validate_settings_ui.py`에 `text` 전용 규칙을 추가한다: `widget: text`인 아이템이 `options`, `min`, `max`, `step` 중 하나라도 가지면 에러. 숫자/열거형 위젯의 속성이 문자열 아이템에 잘못 붙는 것을 막는다.

기존 규칙이 추가 작업 없이 잡아주는 것:

- `check_no_duplicate_keys` — `KoreaMapApiKey`가 크루즈 페이지에도 있으면 에러
- `check_item_completeness` — `title` 누락, `title == key` 금지
- `check_structural` — `VALID_WIDGETS`에 없는 위젯 이름

### 6. 테스트

`openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`에 추가한다. 이 파일은 이미 컴파일된 `settings_ui.json`을 픽스처로 읽어 스키마 사실을 단언하는 패턴을 쓴다(`test_driver_monitoring_mode_requires_attestation` 등).

1. `korea` 패널이 존재하고 `remote_configurable: true`이며 `order == 8`이다
2. 패널의 아이콘이 기존 페이지가 이미 쓰는 이름 중 하나다 — 신규 아이콘 이름이 몰래 들어오는 것을 막는다
3. `KoreaMapApiKey` 아이템의 `widget == "text"`, `secret is True`, `max_length == 255`, `requires_attestation is True`
4. `korea_credentials` 섹션의 `enablement`가 `MapDataSource == 1`을 요구한다
5. `KoreaMapApiKey`가 크루즈 패널에는 없다
6. 검증 규칙 자체의 테스트: `options`를 단 `text` 아이템이 `validate_settings_ui`에서 에러가 된다

**테스트가 보장하지 못하는 것**: 써니링크 앱이 이 스키마를 실제로 렌더링하는지. 테스트는 우리 쪽 계약만 검증한다. 앱 동작은 기기에서만 확인된다.

---

## 실패 모드와 대응

반영 후 기기에서 앱을 열어 확인한다. 증상별 해석과 대응은 다음과 같다.

| 증상 | 해석 | 대응 |
|---|---|---|
| Korea 패널이 보이고 키 입력이 된다 | 앱이 `text`를 지원한다 | 완료 |
| 패널은 보이는데 항목이 없다 | 앱이 미지 위젯을 건너뛴다 | 우리 쪽 스키마는 옳다. 업스트림에 위젯 지원 PR |
| Korea 패널만 안 보인다 | 앱이 패널 단위로 실패한다 | 무해. 두거나 되돌린다 |
| 패널은 보이는데 탭 아이콘이 이상하거나 Cruise와 시각적으로 헷갈린다 | `cruise_control`을 Cruise와 공유한 결과다. 스키마에서 아이콘이 중복된 것은 이 페이지가 처음이고, 앱이 아이콘 이름으로 무언가를 식별하는지는 이 저장소에서 알 수 없다 — 미지를 피하려고 쓴 재사용이 그 자체로 두 번째 미지가 된 경우다 | 업스트림에 앱이 애셋을 가진 별도 아이콘 이름을 요청한다. 코드 변경 없음 |
| **설정 화면 전체가 안 뜬다** | 앱이 스키마 전체 파싱에 실패한다 | 즉시 되돌린다 |

### 되돌리기

전체 변경을 한 커밋으로 만든다. `git revert <sha>` 한 번으로 원상복구된다.

**되돌림이 즉시 반영되지는 않는다.** 스키마는 기기가 커밋된 `settings_ui.json`에서 생성해 앱에 보낸다. 따라서 되돌린 뒤 기기가 소프트웨어 업데이트를 받아야 복구가 완료된다. 그전까지 깨진 상태가 유지된다.

부분 되돌림도 가능하다: `korea.yaml`만 삭제하고 재컴파일하면 위젯 enum 확장은 남지만 그것을 쓰는 아이템이 없으므로 출력 JSON이 변경 전과 같아진다.

---

## Out of Scope

- **`SunnylinkEnabled`, `SunnylinkAllowSensitiveWrite`, `EnableSunnylinkUploader` 원격화** — 모두 의도된 보안 결정이다. `SunnylinkAllowSensitiveWrite`는 원격 허용 시 민감 게이트가 무의미해지고, `SunnylinkEnabled`는 닭-달걀이며, 셋 다 mici 로컬 UI에서 설정 가능하다.
- **mici에 크루즈/속도 제한 화면 추가** — 별개 작업. 이 설계는 원격 경로만 다룬다.
- **`getParams` 차단 목록** — 앱이 모든 파라미터 값을 읽을 수 있는 것은 업스트림 기존 동작이다. 이 작업이 만든 문제가 아니고 이 작업이 악화시키지도 않는다.
- **`AdbEnabled`가 `BLOCKED_PARAMS`와 `SENSITIVE_PARAMS`에 중복된 것** — `BLOCKED`가 먼저 검사되므로 `SENSITIVE` 쪽 항목은 죽은 코드다. 무해한 별건.
- **`text` 위젯의 두 번째 소비자** — 지금 필요한 문자열 파라미터는 `KoreaMapApiKey` 하나뿐이다. 다른 것을 미리 옮기지 않는다.

---

## 완료 검증

1. `python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py` 실행 후 `settings_ui.json`에 `korea` 패널이 있다
2. `python openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py`가 통과한다
3. `test_settings_changes.py`의 신규 테스트가 통과한다
4. 기기 업데이트 후 써니링크 앱에서 위 실패 모드 표를 따라 확인한다
