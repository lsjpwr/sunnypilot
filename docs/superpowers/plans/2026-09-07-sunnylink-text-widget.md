# 써니링크 원격 텍스트 위젯 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 써니링크 설정 스키마에 자유 입력 문자열 위젯(`widget: text`)을 추가하고, 그 첫 소비자로 `KoreaMapApiKey`를 신규 격리 패널 `korea`에 노출한다.

**Architecture:** 위젯 타입은 세 곳에 중복 정의되어 있다 — 출력 JSON 스키마(강제력 있음), 입력 YAML 스키마(에디터 힌트), 검증 스크립트. 세 곳 모두에 `text`를 추가하고 아이템 필드 `secret`/`max_length`를 새로 정의한다. 컴파일러는 미지 필드를 그대로 통과시키므로 동작 변경이 없고, 출력 키 순서 목록에만 두 필드를 넣는다. 실제 아이템은 크루즈 페이지가 아니라 신규 페이지 `korea.yaml`에 둔다 — 써니링크 앱이 미지 위젯을 어떻게 다루는지 알 수 없고, 크루즈가 mici 사용자의 유일한 원격 설정 경로이기 때문이다.

**Tech Stack:** YAML + JSON Schema, Python 3.12 (컨테이너), `unittest` + `OpenpilotTestCase`. 신규 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-09-07-sunnylink-text-widget-design.md`

## Global Constraints

- **`schema_version`를 "1.0"에서 바꾸지 않는다.** `getParamsMetadata()`는 인자를 받지 않으므로(`sunnylinkd.py:204`) 기기가 앱 버전을 알 수 없다. 버전을 올려도 아무것도 게이팅하지 못하고, 구버전 앱에 스키마 전체를 거부할 이유만 준다.
- **`sunnylinkd.py`를 수정하지 않는다.** `KoreaMapApiKey`를 `SENSITIVE_PARAMS`에 넣지 않는다. 게이트는 `requires_attestation`(앱 모달)뿐이다.
- **`settings_ui.json`을 손으로 편집하지 않는다.** 생성 산출물이다. `settings_ui_src/pages/*.yaml`을 고치고 `compile_settings_ui.py`로 재생성한다.
- **`dev/` 아래 어떤 것도 `git add` 하지 않는다.** 파일을 경로로 명시해 스테이징한다. `git add -A`, `git add .` 금지.
- **테스트·검증·린트는 컨테이너 `sp-build`에서 돌린다.** 로컬 Windows 파이썬에는 `capnp`가 없어 임포트가 실패한다.
- **`settings_ui.json`을 로컬 Windows에서 컴파일하지 않는다.** 텍스트 모드 쓰기가 파일 전체를 CRLF로 바꿔 2532줄짜리 가짜 diff를 만든다. 컴파일은 컨테이너에서 하고 결과를 `docker cp`로 가져온다.
- **`icon` 값은 이미 다른 페이지가 쓰는 이름만 쓴다.** 아이콘 이름은 앱 애셋으로 매핑되므로 새 이름은 그 자체로 렌더링을 깰 수 있다.
- **검증 게이트는 `sunnylink/tests/` 디렉터리 전체를 돌린다.** Task가 건드리는 모듈 하나만 돌리면 안 된다 — 패널 수나 페이지 수를 세는 픽스처는 형제 모듈에 있고, `test_settings_changes` 단독 실행은 그것을 볼 수 없다. `unittest discover`는 여기서 동작하지 않으므로(`ImportError: Start directory is not importable`) 네 모듈을 이름으로 지정한다. CI도 같은 범위를 본다 — `tools/test_runner.py:149`가 저장소 루트에서 `path.rglob("test_*.py")`로 수집한다.
- 코드·주석·커밋 메시지는 영어로 쓴다. 저장소의 기존 관례다.

### 반복해서 쓰는 명령

컨테이너 `/work`는 오래된 체크아웃이므로, 무엇을 돌리든 **먼저 파일을 복사한다.**

```bash
# 1) 작업 트리 -> 컨테이너
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/

# 2) settings_ui.json 재생성 (컨테이너에서 쓰고, 결과를 되가져온다)
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py'
docker cp sp-build:/work/openpilot/sunnypilot/sunnylink/settings_ui.json openpilot/sunnypilot/sunnylink/settings_ui.json

# 3) 테스트 (네 모듈 전부. discover는 여기서 안 된다)
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m unittest \
  openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui \
  openpilot.sunnypilot.sunnylink.tests.test_settings_schema \
  openpilot.sunnypilot.sunnylink.tests.test_settings_changes \
  openpilot.sunnypilot.sunnylink.tests.test_capabilities 2>&1 | tail -6'

# 4) 스키마 검증기 (수동 스크립트, CI에 연결되어 있지 않다)
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py 2>&1 | tail -4'

# 5) 린트
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m ruff check openpilot/sunnypilot/sunnylink/'
```

---

## File Structure

| 파일 | 책임 | Task |
|---|---|---|
| `openpilot/sunnypilot/sunnylink/settings_ui.schema.json` | 출력 계약. `additionalProperties: false`라서 여기 없는 필드는 `test_validator_accepts_real_json`에서 실패한다 | 1 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/_schemas/page.schema.json` | 입력(YAML) 계약. 어느 스크립트도 읽지 않는 에디터 힌트 전용 | 1 |
| `openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py` | `VALID_WIDGETS` + 신규 `check_text_items` 규칙 | 1 |
| `openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py` | `_ITEM_KEY_ORDER` 출력 키 순서 | 1 |
| `openpilot/sunnypilot/sunnylink/tools/extract_settings_ui.py` | `_ITEM_ORDER`. 컴파일러 목록과 대칭을 유지 | 1 |
| `openpilot/sunnypilot/sunnylink/docs/README.md` | 위젯/필드 레퍼런스 표 | 1 |
| `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml` | **신규.** 격리된 한국 자격증명 페이지 | 2 |
| `openpilot/sunnypilot/sunnylink/settings_ui.json` | 생성 산출물. Task 1에서는 내용이 바뀌지 않고, Task 2에서 `korea` 패널이 추가된다 | 2 |
| `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py` | 두 Task 모두 테스트를 여기 추가한다 | 1, 2 |

**두 Task로 나눈 이유가 곧 되돌리기 경계다.** Task 1은 위젯 타입만 추가하며 `settings_ui.json`이 한 글자도 바뀌지 않는다 — 즉 앱이 보는 것이 그대로다. Task 2가 앱이 보는 내용을 바꾸는 유일한 커밋이므로, 기기에서 문제가 생기면 Task 2 커밋 하나만 `git revert` 하면 된다.

---

## Task 1: 텍스트 위젯 타입 추가 (출력 JSON 불변)

**Files:**
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui.schema.json:181-184`
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui_src/_schemas/page.schema.json:69`
- Modify: `openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py:24`
- Modify: `openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py:107-125`
- Modify: `openpilot/sunnypilot/sunnylink/tools/extract_settings_ui.py:54-72`
- Modify: `openpilot/sunnypilot/sunnylink/docs/README.md:113-140`
- Test: `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`

**Interfaces:**
- Consumes: 없음 (첫 Task)
- Produces:
  - `check_text_items(data: dict, result: ValidationResult) -> None` — `validate_settings_ui.py`의 모듈 레벨 함수. `widget: "text"`인 아이템이 `options`/`min`/`max`/`step`을 가지면 `result.error("text items", ...)`, 아니면 `result.ok("text items")`.
  - `VALID_WIDGETS`에 `"text"` 포함
  - `settings_ui.schema.json`의 `$defs.SchemaItem.properties`에 `secret`(boolean), `max_length`(integer) 선언
  - `page.schema.json`의 `$defs.Item.properties`에 동일한 두 필드 선언
  - 테스트 모듈 상수 `PAGE_SCHEMA_PATH`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`의 임포트 블록을 다음으로 바꾼다. 기존 임포트는 그대로 두고 세 줄을 더한다.

```python
from openpilot.sunnypilot.sunnylink.tools.generate_settings_schema import (
  DEFINITION_PATH,
  SCHEMA_VERSION,
  TORQUE_VERSIONS_PATH,
  _build_torque_options,
  _load_torque_versions,
  generate_schema,
)
from openpilot.sunnypilot.sunnylink.tools.validate_settings_ui import (
  VALID_WIDGETS,
  ValidationResult,
  check_text_items,
)
from openpilot.common.test import OpenpilotTestCase
```

`SCHEMA_VALIDATOR_PATH` 정의 바로 아래에 경로 상수를 더한다.

```python
SCHEMA_VALIDATOR_PATH = os.path.join(os.path.dirname(DEFINITION_PATH), "settings_ui.schema.json")
PAGE_SCHEMA_PATH = os.path.join(
  os.path.dirname(DEFINITION_PATH), "settings_ui_src", "_schemas", "page.schema.json"
)
```

파일 맨 끝에 다음을 덧붙인다.

```python
def _text_item_schema(**extra: Any) -> dict[str, Any]:
  """Minimal one-item schema for exercising check_text_items in isolation."""
  item: dict[str, Any] = {"key": "SomeString", "widget": "text", "title": "Some String"}
  item.update(extra)
  return {"panels": [{"id": "p", "label": "P", "icon": "device", "order": 1,
                      "sections": [{"id": "s", "title": "S", "items": [item]}]}]}


class TestTextWidget(OpenpilotTestCase):
  def test_text_is_a_valid_widget_everywhere(self):
    """Three files carry their own widget list. A widget one accepts and another rejects is
    an authoring trap: the compile succeeds, then the compiled file fails validation."""
    assert "text" in VALID_WIDGETS
    with open(SCHEMA_VALIDATOR_PATH) as f:
      out_enum = json.load(f)["$defs"]["SchemaItem"]["properties"]["widget"]["enum"]
    assert "text" in out_enum
    with open(PAGE_SCHEMA_PATH) as f:
      in_enum = json.load(f)["$defs"]["Item"]["properties"]["widget"]["enum"]
    assert "text" in in_enum

  def test_secret_and_max_length_are_declared(self):
    """settings_ui.schema.json sets additionalProperties: false on items, so an undeclared
    field does not get ignored -- it fails validation of the compiled file."""
    with open(SCHEMA_VALIDATOR_PATH) as f:
      props = json.load(f)["$defs"]["SchemaItem"]["properties"]
    assert props["secret"]["type"] == "boolean"
    assert props["max_length"]["type"] == "integer"
    with open(PAGE_SCHEMA_PATH) as f:
      page_props = json.load(f)["$defs"]["Item"]["properties"]
    assert page_props["secret"]["type"] == "boolean"
    assert page_props["max_length"]["type"] == "integer"

  def test_schema_version_is_not_bumped(self):
    """getParamsMetadata() takes no arguments (sunnylinkd.py:204), so the device never learns
    which app version it is talking to. A bump cannot gate anything; it can only give an
    older app a reason to reject the whole file."""
    assert SCHEMA_VERSION == "1.0"


class TestTextItemValidation(OpenpilotTestCase):
  @parameterized.expand(["options", "min", "max", "step"], names=["field"])
  def test_text_item_rejects_numeric_fields(self, field):
    """min/max/step/options belong to option and multiple_button. On a text item they are an
    authoring mistake the frontend has no way to act on."""
    result = ValidationResult()
    check_text_items(_text_item_schema(**{field: 1}), result)
    assert not result.success

  def test_plain_text_item_passes(self):
    result = ValidationResult()
    check_text_items(_text_item_schema(secret=True, max_length=255), result)
    assert result.success

  def test_non_text_items_keep_their_numeric_fields(self):
    """The rule must key on the widget, not on the mere presence of min/max -- otherwise it
    would reject every slider in the schema."""
    data = _text_item_schema(min=1)
    data["panels"][0]["sections"][0]["items"][0]["widget"] = "option"
    result = ValidationResult()
    check_text_items(data, result)
    assert result.success
```

- [ ] **Step 2: 실패를 확인한다**

```bash
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m unittest openpilot.sunnypilot.sunnylink.tests.test_settings_changes -v 2>&1 | tail -5'
```

Expected: 임포트 단계에서 실패한다.
`ImportError: cannot import name 'check_text_items' from 'openpilot.sunnypilot.sunnylink.tools.validate_settings_ui'`

- [ ] **Step 3: 검증 규칙을 구현한다**

`openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py:24`를 바꾼다.

```python
VALID_WIDGETS = {"toggle", "option", "multiple_button", "button", "info", "text"}
```

같은 파일에서 `check_vehicle_brands` 함수 정의가 끝난 직후, `def validate(` 앞에 다음을 넣는다.

```python
# Fields that belong to option / multiple_button. A text item carrying one is an authoring
# mistake: the frontend cannot render a free-text box and a slider for the same param.
_TEXT_FORBIDDEN_FIELDS = ("options", "min", "max", "step")


def check_text_items(data: dict, result: ValidationResult) -> None:
  """Check 11: text items must not carry option/slider attributes."""
  errors: list[str] = []

  for path, item in collect_all_items(data):
    if item.get("widget") != "text":
      continue
    extra = [f for f in _TEXT_FORBIDDEN_FIELDS if f in item]
    if extra:
      errors.append(f"{path}: text item '{item.get('key', '?')}' must not set {', '.join(extra)}")

  if errors:
    result.error("text items", "; ".join(errors))
  else:
    result.ok("text items")
```

같은 파일의 `validate()` 안에서 검사 목록에 새 검사를 등록한다. `# Checks 2-10` 주석을 `# Checks 2-11`로 고치고, `check_vehicle_brands(data, result)` 다음 줄에 추가한다.

```python
  # Checks 2-11
  check_structural(data, result)
  check_item_completeness(data, result)
  check_no_duplicate_keys(data, result)
  check_rule_wellformedness(data, result)
  check_capability_refs(data, result)
  check_no_self_reference(data, result)
  check_sub_panel_triggers(data, result)
  check_ordering(data, result)
  check_vehicle_brands(data, result)
  check_text_items(data, result)
```

- [ ] **Step 4: 두 JSON 스키마에 `text`와 신규 필드를 선언한다**

`openpilot/sunnypilot/sunnylink/settings_ui.schema.json`의 `"widget"` 속성 블록(181-184행)을 다음으로 바꾼다. `secret`과 `max_length`는 그 바로 뒤에 붙인다.

```json
        "widget": {
          "type": "string",
          "description": "The UI widget type to render.",
          "enum": ["toggle", "option", "multiple_button", "button", "info", "text"]
        },
        "secret": {
          "type": "boolean",
          "description": "For 'text' widgets: the value is a credential. The UI must mask input and must not display the stored value.",
          "default": false
        },
        "max_length": {
          "type": "integer",
          "description": "Maximum number of characters a 'text' widget accepts.",
          "minimum": 1
        },
```

`openpilot/sunnypilot/sunnylink/settings_ui_src/_schemas/page.schema.json:69` 한 줄을 다음 세 줄로 바꾼다.

```json
        "widget": {"type": "string", "enum": ["toggle", "option", "multiple_button", "button", "info", "text"]},
        "secret": {"type": "boolean", "description": "For 'text' widgets: mask input, never display the stored value."},
        "max_length": {"type": "integer", "minimum": 1, "description": "Maximum characters a 'text' widget accepts."},
```

- [ ] **Step 5: 출력 키 순서 목록 두 개에 신규 필드를 넣는다**

`openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py:107-125`의 `_ITEM_KEY_ORDER`에서 `"widget",` 다음 줄에 두 항목을 넣는다.

```python
_ITEM_KEY_ORDER = [
  "key",
  "widget",
  "secret",
  "max_length",
  "needs_onroad_cycle",
  "requires_attestation",
  "blocked",
  "title",
  "description",
  "details",
  "title_param_suffix",
  "min",
  "max",
  "step",
  "unit",
  "options",
  "visibility",
  "enablement",
  "sub_items",
]
```

`openpilot/sunnypilot/sunnylink/tools/extract_settings_ui.py:54-72`의 `_ITEM_ORDER`에 같은 두 줄을 같은 위치에 넣는다. 두 목록은 같은 계약을 두 곳에 적어둔 것이므로 갈라지면 안 된다.

```python
_ITEM_ORDER = [
  "key",
  "widget",
  "secret",
  "max_length",
  "needs_onroad_cycle",
  "requires_attestation",
  "blocked",
  "title",
  "description",
  "details",
  "title_param_suffix",
  "min",
  "max",
  "step",
  "unit",
  "options",
  "visibility",
  "enablement",
  "sub_items",
]
```

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

```bash
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m unittest \
  openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui \
  openpilot.sunnypilot.sunnylink.tests.test_settings_schema \
  openpilot.sunnypilot.sunnylink.tests.test_settings_changes \
  openpilot.sunnypilot.sunnylink.tests.test_capabilities 2>&1 | tail -6'
```

Expected: `Ran 105 tests` / `OK`. `test_settings_changes`가 48개(기존 39개 + 신규 9개 — `TestTextWidget` 3개, `TestTextItemValidation` 6개. `parameterized`가 `test_text_item_rejects_numeric_fields`를 필드 4개로 펼치고 나머지 2개가 더해진다), 형제 세 모듈이 57개(`test_compile_settings_ui` 17, `test_settings_schema` 26, `test_capabilities` 14)다. 판정 기준은 **`OK`이고 실패가 0인 것**이다.

이 Task는 페이지를 만들지 않으므로 형제 모듈의 패널 수·페이지 수 픽스처는 아직 그대로 통과한다. 그것이 바뀌는 것은 Task 2다.

`OK:` / `ERROR:` 줄이 요약 뒤에 남아 있으면 안 된다. `ValidationResult`는 검사할 때마다 출력하므로 `check_text_items`를 직접 부르는 테스트는 `contextlib.redirect_stdout`으로 감싼다.

- [ ] **Step 7: 출력 JSON이 바뀌지 않았음을 확인한다**

이것이 이 Task의 핵심 불변식이다. 위젯 타입만 넓혔고 그것을 쓰는 아이템이 없으므로 앱이 받는 파일이 그대로여야 한다.

```bash
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py --check'
git status --short openpilot/sunnypilot/sunnylink/settings_ui.json
```

Expected: `--check: /work/openpilot/sunnypilot/sunnylink/settings_ui.json matches compiled output`, 그리고 `git status`는 아무것도 출력하지 않는다.

`settings_ui.json`이 수정된 것으로 나오면 무언가 잘못된 것이다 — 되돌리고(`git checkout -- openpilot/sunnypilot/sunnylink/settings_ui.json`) 원인을 찾는다.

- [ ] **Step 8: 검증기와 린트를 돌린다**

```bash
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py 2>&1 | tail -4'
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m ruff check openpilot/sunnypilot/sunnylink/'
```

Expected: `Summary: 11 checks passed, 0 checks failed` / `Result: PASS`, 그리고 `All checks passed!`

검사 개수가 10에서 11로 늘어난 것이 `check_text_items`가 실제로 등록되었다는 증거다.

- [ ] **Step 9: 문서 표를 갱신한다**

`openpilot/sunnypilot/sunnylink/docs/README.md:113-121`의 위젯 표에 행을 더한다.

```markdown
| Widget | Use for | Fields needed |
|--------|---------|---------------|
| `toggle` | On/off boolean | `title` |
| `multiple_button` | 2-4 discrete options | `title` + `options` array |
| `option` | Numeric range or dropdown | `title` + `min/max/step` or `options` |
| `text` | Free-text string | `title` + optional `max_length`, `secret` |
| `info` | Read-only display | `title` |
```

같은 파일의 아이템 필드 표에서 `widget` 행을 고치고 두 행을 더한다. `unit` 행 바로 아래에 넣는다.

```markdown
| `widget` | Yes | `toggle`, `option`, `multiple_button`, `button`, `info`, `text` |
```

```markdown
| `max_length` | For `text` | Maximum characters accepted. Device-side dialogs cap strings at 255 |
| `secret` | For `text` | `true` when the value is a credential. Frontend masks input and must not display the stored value |
```

- [ ] **Step 10: 커밋**

```bash
git add openpilot/sunnypilot/sunnylink/settings_ui.schema.json \
        openpilot/sunnypilot/sunnylink/settings_ui_src/_schemas/page.schema.json \
        openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py \
        openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py \
        openpilot/sunnypilot/sunnylink/tools/extract_settings_ui.py \
        openpilot/sunnypilot/sunnylink/docs/README.md \
        openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py
git commit -m "feat: teach the settings schema a free-text widget

None of the schema's 97 keys is a writable string today: LanguageSetting,
the one string param that appears at all, is widget: info. A param like
KoreaMapApiKey therefore has no remote representation, which matters on
mici, where the device has no cruise settings screen and sunnylink is the
only way in.

Adds widget: text along with secret (mask the value, it is a credential)
and max_length, mirroring the device-side dialog's 255-character cap. The
widget list lives in three files with different enforcement -- the output
schema fails the compiled JSON, VALID_WIDGETS fails check_structural, and
page.schema.json only drives editor hints -- so all three move together.

No item uses the new widget yet, so settings_ui.json is byte-identical and
the app sees exactly what it saw before."
```

---

## Task 2: `korea` 격리 패널에 `KoreaMapApiKey` 노출

**Files:**
- Create: `openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml`
- Modify: `openpilot/sunnypilot/sunnylink/settings_ui.json` (생성 산출물 — 손으로 고치지 않는다)
- Test: `openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`

**Interfaces:**
- Consumes: Task 1이 만든 `widget: "text"`, `secret`, `max_length`. 세 스키마 모두 이들을 이미 받아들인다.
- Produces: 컴파일된 `settings_ui.json`에 `id: korea` 패널. 섹션 `korea_credentials`, 아이템 `KoreaMapApiKey`.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py`의 `_find_section` 정의 바로 아래에 헬퍼 두 개를 더한다.

```python
def _find_panel(schema: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
  for panel in schema.get("panels", []):
    if panel.get("id") == panel_id:
      return panel
  return None


def _panel_item_keys(schema: dict[str, Any], panel_id: str) -> set[str]:
  """Every param key reachable inside one panel."""
  panel = _find_panel(schema, panel_id)
  if panel is None:
    return set()
  return {item["key"] for item in _walk_items({"panels": [panel]}) if "key" in item}
```

파일 맨 끝에 다음을 덧붙인다.

```python
class TestKoreaApiKeyRemote(OpenpilotTestCase):
  def test_korea_panel_present_and_remote_configurable(self, schema):
    panel = _find_panel(schema, "korea")
    assert panel is not None, "korea panel missing from settings_ui schema"
    assert panel.get("remote_configurable") is True
    assert panel.get("order") == 8

  def test_korea_panel_reuses_an_existing_icon(self, schema):
    """A new page hands the app two things it may not know: a new widget and a new icon
    name. The icon resolves to an app asset, so an unknown name can break the page on its
    own. Reusing a name already in the schema keeps this to one unknown."""
    panel = _find_panel(schema, "korea")
    assert panel is not None
    others = {p.get("icon") for p in schema.get("panels", []) if p.get("id") != "korea"}
    assert panel.get("icon") in others, \
      f"korea panel icon {panel.get('icon')!r} is used by no other panel"

  def test_api_key_item_shape(self, schema):
    item = _find_item(schema, "KoreaMapApiKey")
    assert item is not None, "KoreaMapApiKey missing from settings_ui schema"
    assert item.get("widget") == "text"
    assert item.get("secret") is True
    assert item.get("max_length") == 255

  def test_api_key_requires_attestation(self, schema):
    """requires_attestation is the only gate on this write. KoreaMapApiKey is deliberately
    left out of SENSITIVE_PARAMS, so nothing device-side asks a second time."""
    item = _find_item(schema, "KoreaMapApiKey")
    assert item is not None
    assert item.get("requires_attestation") is True

  def test_api_key_section_requires_korea_map_source(self, schema):
    """Both device UIs gate the key on MapDataSource == korea (speed_limit_settings.py:233,
    mici toggles.py:135). Offering it under OSM would be a field that changes nothing."""
    section = _find_section(schema, "korea", "korea_credentials")
    assert section is not None, "korea.korea_credentials section missing"
    assert _references_param_equals(section.get("visibility"), "MapDataSource", 1), \
      "korea_credentials missing MapDataSource == korea (1) visibility gate"

  def test_api_key_stays_out_of_the_cruise_panel(self, schema):
    """The separate page exists so a widget the app may not understand cannot take cruise
    down with it -- and cruise is the only remote path a mici owner has to StopDistance,
    DEC, and the speed limit settings. Moving the key back into cruise undoes that."""
    assert "KoreaMapApiKey" in _panel_item_keys(schema, "korea")
    assert "KoreaMapApiKey" not in _panel_item_keys(schema, "cruise")
```

- [ ] **Step 2: 실패를 확인한다**

```bash
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m unittest openpilot.sunnypilot.sunnylink.tests.test_settings_changes.TestKoreaApiKeyRemote -v 2>&1 | tail -12'
```

Expected: 6개 테스트가 모두 실패한다. 첫 실패 메시지는
`AssertionError: korea panel missing from settings_ui schema`

- [ ] **Step 3: 페이지를 만든다**

`openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml`을 새로 만들고 정확히 다음 내용을 넣는다.

```yaml
# Page: korea
# Edit this file. Run compile_settings_ui.py to emit settings_ui.json.
#
# Deliberately separate from cruise.yaml. `widget: text` is new to the schema and the
# sunnylink app's handling of an unknown widget is not knowable from this repo. Cruise is
# the only remote path a mici owner has to StopDistance, DEC, and the speed limit
# settings, so the new widget lands where a bad guess costs one page and nothing else.
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
  visibility:
  - type: param
    key: MapDataSource
    equals: 1
  items:
  - key: KoreaMapApiKey
    widget: text
    secret: true
    max_length: 255
    requires_attestation: true
    title: Speed Camera API Key
    description: data.go.kr key used to refresh the speed camera database over Wi-Fi.
      Without it the cameras shipped with the database are used as-is. Other Korean
      map settings live under Cruise.
```

`icon: cruise_control`은 크루즈 페이지가 이미 쓰는 이름이다. 새 아이콘 이름을 지어내면 앱이 애셋을 찾지 못해 그 자체로 페이지가 깨질 수 있다.

`order: 8`은 비어 있는 슬롯이다 — software가 7, developer가 9다. `check_ordering`이 중복 order를 에러로 잡으므로 기존 번호와 겹치면 안 된다.

- [ ] **Step 4: `settings_ui.json`을 재생성한다**

로컬 Windows에서 컴파일하면 파일 전체가 CRLF로 바뀌어 2532줄짜리 가짜 diff가 생긴다. 컨테이너에서 쓰고 결과만 가져온다.

```bash
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py'
docker cp sp-build:/work/openpilot/sunnypilot/sunnylink/settings_ui.json openpilot/sunnypilot/sunnylink/settings_ui.json
git diff --stat openpilot/sunnypilot/sunnylink/settings_ui.json
```

Expected: `Wrote /work/openpilot/sunnypilot/sunnylink/settings_ui.json`, 그리고 `git diff --stat`이 **20줄 남짓 추가**로 나온다.

수백 줄 변경으로 나오면 줄바꿈이 CRLF로 뒤집힌 것이다. `git checkout -- openpilot/sunnypilot/sunnylink/settings_ui.json`으로 되돌리고 이 Step을 다시 한다.

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

```bash
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m unittest \
  openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui \
  openpilot.sunnypilot.sunnylink.tests.test_settings_schema \
  openpilot.sunnypilot.sunnylink.tests.test_settings_changes \
  openpilot.sunnypilot.sunnylink.tests.test_capabilities 2>&1 | tail -6'
```

Expected: `Ran 110 tests` / `OK`, 실패 0. `test_validator_accepts_real_json`이 함께 통과하는 것이 중요하다 — 그것이 `secret`/`max_length`가 출력 스키마에 제대로 선언되었는지를 실제로 검사하는 테스트다.

**페이지를 하나 늘리고 `text` 아이템을 처음 실어 보내므로, 형제 모듈 세 곳이 함께 깨진다. 이 Task에서 같이 고친다.** `test_compile_settings_ui.py`의 `test_panels_present`(패널 수 하드코딩 — 세지 말고 id 집합에서 유도한다)와 `test_pages_dir_well_formed`(페이지 파일 수 — 의도적 트립와이어이므로 숫자와 주석을 함께 올린다), 그리고 `test_settings_schema.py:21`의 네 번째 위젯 enum 사본 `VALID_WIDGET_TYPES`(다섯 번째 동기화 대상을 만들지 말고 지운 뒤 `validate_settings_ui`의 `VALID_WIDGETS`를 임포트한다). `test_settings_changes` 하나만 돌리면 이 세 실패가 보이지 않는다 — 이 계획이 처음에 놓쳤던 지점이 정확히 여기다.

- [ ] **Step 6: 검증기와 린트를 돌린다**

```bash
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py 2>&1 | tail -4'
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m ruff check openpilot/sunnypilot/sunnylink/'
```

Expected: `Summary: 11 checks passed, 0 checks failed` / `Result: PASS`, 그리고 `All checks passed!`

`check_no_duplicate_keys`가 통과했다는 것은 `KoreaMapApiKey`가 어느 다른 패널에도 없다는 뜻이고, `check_text_items`가 통과했다는 것은 새 아이템이 숫자 위젯 속성을 달고 있지 않다는 뜻이다.

- [ ] **Step 7: 커밋**

기기에서 문제가 생기면 되돌릴 커밋이 바로 이것이다. 한 커밋으로 유지한다.

```bash
git add openpilot/sunnypilot/sunnylink/settings_ui_src/pages/korea.yaml \
        openpilot/sunnypilot/sunnylink/settings_ui.json \
        openpilot/sunnypilot/sunnylink/tests/test_settings_changes.py
git commit -m "feat: expose the Korean speed camera API key over sunnylink

mici has no cruise, steering, visuals, display, or vehicle settings
screen, so sunnylink is the only way to configure most of this branch on
that hardware. The API key was the one Korean setting with no remote
representation at all.

It lands on its own page rather than beside the other Korean settings in
cruise. widget: text is new, getParamsMetadata() takes no arguments so the
device cannot tell which app version it is talking to, and nothing in this
repo says what the app does with a widget it does not recognise. Cruise is
the page a mici owner can least afford to lose -- StopDistance, DEC, and
the speed limit settings all live there -- so the unknown goes somewhere
nothing else depends on. Reverting this commit restores the previous
schema exactly.

The icon reuses cruise_control rather than introducing a name the app has
no asset for, which would be a second unknown in the same change."
```

---

## 완료 검증

두 Task를 모두 마친 뒤 한 번 돌린다.

```bash
docker cp openpilot/sunnypilot/sunnylink sp-build:/work/openpilot/sunnypilot/
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m unittest \
  openpilot.sunnypilot.sunnylink.tests.test_compile_settings_ui \
  openpilot.sunnypilot.sunnylink.tests.test_settings_schema \
  openpilot.sunnypilot.sunnylink.tests.test_settings_changes \
  openpilot.sunnypilot.sunnylink.tests.test_capabilities 2>&1 | tail -6'
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/compile_settings_ui.py --check'
docker exec sp-build bash -lc 'cd /work && .venv/bin/python openpilot/sunnypilot/sunnylink/tools/validate_settings_ui.py 2>&1 | tail -4'
docker exec sp-build bash -lc 'cd /work && .venv/bin/python -m ruff check openpilot/sunnypilot/sunnylink/'
git status --short
```

Expected: `Ran 110 tests` / `OK`, 그리고 요약 뒤에 `OK:`/`ERROR:` 줄이 남지 않는다.

전부 통과하고 `git status`에 추적되지 않은 기존 항목(`.codegraph/`, `dev/`, `docs/superpowers/plans/` 아래 예전 파일들) 말고는 아무것도 남지 않아야 한다.

`korea` 패널이 실제로 나왔는지 눈으로 확인한다.

```bash
python -c "import json; d=json.load(open('openpilot/sunnypilot/sunnylink/settings_ui.json')); print([p['id'] for p in d['panels']])"
```

Expected: 목록에 `korea`가 들어 있다.

---

## 이 계획이 검증하지 못하는 것

테스트는 **우리 쪽 계약만** 검증한다. 써니링크 앱이 `widget: text`를 실제로 렌더링하는지는 기기에서만 알 수 있다. 기기 업데이트 후 앱을 열고 다음 표로 판정한다.

| 증상 | 해석 | 대응 |
|---|---|---|
| Korea 패널이 보이고 키 입력이 된다 | 앱이 `text`를 지원한다 | 완료 |
| 패널은 보이는데 항목이 없다 | 앱이 미지 위젯을 건너뛴다 | 우리 쪽 스키마는 옳다. 업스트림에 위젯 지원 PR |
| Korea 패널만 안 보인다 | 앱이 패널 단위로 실패한다 | 무해. 두거나 되돌린다 |
| 패널은 보이는데 탭 아이콘이 이상하거나 Cruise와 시각적으로 헷갈린다 | `cruise_control` 아이콘을 Cruise와 공유한 결과다. 스키마에서 아이콘이 중복된 것은 이 페이지가 처음이고, 앱이 아이콘 이름으로 무언가를 식별하는지는 알 수 없다 | 업스트림에 앱이 애셋을 가진 별도 아이콘 이름을 요청한다. 코드 변경 없음 |
| **설정 화면 전체가 안 뜬다** | 앱이 스키마 전체 파싱에 실패한다 | Task 2 커밋을 `git revert` 한다 |

되돌림은 즉시 반영되지 않는다. 스키마는 기기가 커밋된 `settings_ui.json`에서 생성해 앱에 보내므로, 되돌린 뒤 기기가 소프트웨어 업데이트를 받아야 복구가 끝난다.

또 하나: `validate_settings_ui.py`는 CI에도 pre-commit에도 연결되어 있지 않은 수동 스크립트다. 따라서 `check_text_items`는 누군가 그 스크립트를 돌릴 때만 실행된다. Task 1의 단위 테스트가 규칙 자체의 동작을 고정하지만, 잘못 작성된 `text` 아이템이 커밋되는 것을 자동으로 막지는 못한다. 검증기를 CI에 연결하는 것은 이 계획의 범위 밖이다.
