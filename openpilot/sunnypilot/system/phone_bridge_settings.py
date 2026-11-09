"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

The comma settings list phone_bridge serves (GET and POST /v1/settings): the settings mici's toggles panel
offers, minus the ones a phone must not touch. What an item is (widget, options, the rules that lock it)
comes from sunnylink's settings_ui.json; the few items it lacks are written out here.
"""
import functools
import json
import operator
from collections.abc import Callable

from openpilot.common.constants import CV
from openpilot.common.params import UnknownKeyName
from openpilot.selfdrive.car.cruise import V_CRUISE_INITIAL, V_CRUISE_MAX
from openpilot.sunnypilot.sunnylink.capabilities import generate_capabilities
from openpilot.sunnypilot.sunnylink.tools.generate_settings_schema import DEFINITION_PATH

# key -> the Korean title; an empty one means the app gets the English title
ALLOWED_TITLES = {
  "LongitudinalPersonality": "차간 거리",
  "InitialCruiseSpeedKph": "초기 설정 속도",
  "IsMetric": "미터법 사용",
  "IsLdwEnabled": "차선 이탈 경고",
  "AlwaysOnDM": "상시 운전자 모니터링",
  "DriverMonitoringMode": "운전자 모니터링",
  "KoreaExternalNavEnabled": "한국 외부 내비 입력",
  "KoreaCameraSpeedEnabled": "속도 단속 카메라",
  "KoreaCameraSignalEnabled": "신호·속도 단속 카메라",
  "KoreaCameraSectionEnabled": "구간단속 카메라",
  "KoreaCameraZoneEnabled": "보호구역 카메라",
  "KoreaSpeedBumpEnabled": "과속방지턱 감속",
  "KoreaMapAutoDownload": "지도 자동 다운로드",
}
# The panel's other keys. A phone cannot turn sunnypilot off or read the camera and microphone recordings' switches,
# cannot see the API key, and cannot switch off the link it is using.
EXCLUDED = ("OpenpilotEnabledToggle", "KoreaMapApiKey", "RecordFront", "RecordAudio", "PhoneBridgeEnabled")

LOCK_REASONS = {"offroad_only": "offroad_only", "not_engaged": "engaged"}  # any other failed rule is "requires"
COMPARE = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
CRUISE_SPEED_STEP = 5  # in display units, as mici's picker


def _initial_cruise_speed(params) -> tuple[dict, int | None]:
  """mici's picker (get_speed_options): the speeds in the display unit, each stored in km/h."""
  conversion = 1 if params.get_bool("IsMetric") else CV.KPH_TO_MPH
  unit = "km/h" if conversion == 1 else "mph"
  speeds = range(round(V_CRUISE_INITIAL * conversion), round(V_CRUISE_MAX * conversion) + 1, CRUISE_SPEED_STEP)
  kphs = [round(speed / conversion) for speed in speeds]
  options = [{"value": kph, "label": f"{speed} {unit}"} for kph, speed in zip(kphs, speeds, strict=True)]
  saved = params.get("InitialCruiseSpeedKph", return_default=True)
  value = None if saved is None else min(kphs, key=lambda kph: abs(kph - saved))
  # mici shows the button with openpilot long control on a car that does not run the cruise itself
  rules = [{"type": "capability", "field": "has_longitudinal_control", "equals": True}, {"type": "capability", "field": "pcm_cruise", "equals": False}]
  return {"widget": "multiple_button", "title": "Initial max speed", "options": options, "enablement": rules}, value


EXTRA_ITEMS: dict[str, Callable] = {"InitialCruiseSpeedKph": _initial_cruise_speed}


@functools.cache
def _definitions() -> dict[str, dict]:
  """Every item of settings_ui.json by key."""
  with open(DEFINITION_PATH) as f:
    schema = json.load(f)
  found: dict[str, dict] = {}

  def walk(node):
    if isinstance(node, dict):
      if "key" in node and "widget" in node:
        found.setdefault(node["key"], node)
      for child in node.values():
        walk(child)
    elif isinstance(node, list):
      for child in node:
        walk(child)

  walk(schema)
  return found


class PhoneSettings:
  def __init__(self, params, allowed_titles: dict[str, str] = ALLOWED_TITLES):
    self.params, self.allowed_titles = params, allowed_titles

  def items(self, offroad: bool, idle: bool) -> list[dict]:
    """`offroad` and `idle` (not engaged) say what the rules locking an item may assume; both False while the car is unknown."""
    capabilities = generate_capabilities(self.params)
    return [item for key in self.allowed_titles if (item := self._item(key, capabilities, offroad, idle)) is not None]

  def write(self, key: str, value, offroad: bool, idle: bool) -> tuple[int, dict | None]:
    """(HTTP status, body). The rules are looked at again here, the app's list may be old."""
    item = self._item(key, generate_capabilities(self.params), offroad, idle) if key in self.allowed_titles else None
    if item is None:
      return 403, None
    if not item["enabled"]:
      return 409, item
    if item["widget"] == "toggle":
      if not isinstance(value, bool):
        return 400, None
      self.params.put_bool(key, value, block=True)
    else:
      if isinstance(value, bool) or value not in [option["value"] for option in item["options"]]:
        return 400, None
      self.params.put(key, int(value), block=True)
    return 200, self._item(key, generate_capabilities(self.params), offroad, idle)

  def _item(self, key, capabilities, offroad, idle) -> dict | None:
    """None for a key the comma does not have or settings_ui.json does not describe."""
    try:
      self.params.check_key(key)
    except UnknownKeyName:
      return None
    if key in EXTRA_ITEMS:
      spec, value = EXTRA_ITEMS[key](self.params)
    elif key in _definitions():
      spec = _definitions()[key]
      value = self.params.get_bool(key) if spec["widget"] == "toggle" else self.params.get(key, return_default=True)
    else:
      return None
    ok, reason = True, None
    for rule in spec.get("enablement", []):
      if not self._passes(rule, capabilities, offroad, idle):
        ok, reason = False, LOCK_REASONS.get(rule["type"], "requires")
        break
    return {"key": key, "title": self.allowed_titles[key] or spec["title"], "widget": spec["widget"], "value": value,
            "options": spec.get("options"), "enabled": ok, "reason": reason}

  def _passes(self, rule, capabilities, offroad, idle) -> bool:
    kind = rule["type"]
    if kind == "offroad_only":
      return offroad
    if kind == "not_engaged":
      return idle
    if kind == "capability":
      return capabilities.get(rule["field"]) == rule["equals"]
    if kind == "param":
      return self._param(rule["key"]) == rule["equals"]
    if kind == "param_compare":
      value = self._param(rule["key"])
      return isinstance(value, int | float) and COMPARE[rule["op"]](value, rule["value"])
    if kind == "not":
      return not self._passes(rule["condition"], capabilities, offroad, idle)
    if kind in ("any", "all"):
      return (any if kind == "any" else all)(self._passes(child, capabilities, offroad, idle) for child in rule["conditions"])
    return False  # a rule this code does not know locks the item

  def _param(self, key):
    try:
      return self.params.get(key, return_default=True)
    except UnknownKeyName:
      return None
