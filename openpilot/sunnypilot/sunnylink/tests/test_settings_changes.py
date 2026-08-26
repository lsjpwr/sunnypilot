"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Per-bug regression tests for the Raylib-vs-schema parity audit. Each test
isolates one of the gating bugs that the design-overhaul branch fixes so a
future regression is loud and obvious. These tests are intentionally narrow
and additive — they do not replace the broader test_settings_schema.py.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openpilot.common.parameterized import parameterized

from openpilot.sunnypilot.sunnylink.tools.generate_settings_schema import (
  DEFINITION_PATH,
  TORQUE_VERSIONS_PATH,
  _build_torque_options,
  _load_torque_versions,
  generate_schema,
)
from openpilot.common.test import OpenpilotTestCase


SCHEMA_VALIDATOR_PATH = os.path.join(os.path.dirname(DEFINITION_PATH), "settings_ui.schema.json")


def _walk_items(schema: dict[str, Any]):
  """Yield every item dict from the schema."""
  def _yield(item: dict[str, Any]):
    yield item
    for sub in item.get("sub_items", []):
      yield from _yield(sub)

  for panel in schema.get("panels", []):
    for section in panel.get("sections", []):
      for item in section.get("items", []):
        yield from _yield(item)
      for sp in section.get("sub_panels", []):
        for item in sp.get("items", []):
          yield from _yield(item)
    for item in panel.get("items", []):
      yield from _yield(item)
    for sp in panel.get("sub_panels", []):
      for item in sp.get("items", []):
        yield from _yield(item)
  for brand in schema.get("vehicle_settings", {}).values():
    items = brand.get("items", []) if isinstance(brand, dict) else brand
    for item in items:
      yield from _yield(item)


def _find_item(schema: dict[str, Any], key: str) -> dict[str, Any] | None:
  for item in _walk_items(schema):
    if item.get("key") == key:
      return item
  return None


def _find_section(schema: dict[str, Any], panel_id: str, section_id: str) -> dict[str, Any] | None:
  for panel in schema.get("panels", []):
    if panel.get("id") != panel_id:
      continue
    for section in panel.get("sections", []):
      if section.get("id") == section_id:
        return section
  return None


def _flatten_rule_types(rules: list[dict[str, Any]] | None) -> set[str]:
  out: set[str] = set()

  def _walk(rule: dict[str, Any]) -> None:
    out.add(rule.get("type", ""))
    if rule.get("type") == "not" and "condition" in rule:
      _walk(rule["condition"])
    elif rule.get("type") in ("any", "all"):
      for c in rule.get("conditions", []):
        _walk(c)

  for rule in rules or []:
    _walk(rule)
  return out


def _references_capability_field(rules: list[dict[str, Any]] | None, field: str) -> bool:
  found = False

  def _walk(rule: dict[str, Any]) -> None:
    nonlocal found
    if rule.get("type") == "capability" and rule.get("field") == field:
      found = True
    elif rule.get("type") == "not" and "condition" in rule:
      _walk(rule["condition"])
    elif rule.get("type") in ("any", "all"):
      for c in rule.get("conditions", []):
        _walk(c)

  for rule in rules or []:
    _walk(rule)
  return found


def _references_param_equals(rules: list[dict[str, Any]] | None, key: str, equals: Any) -> bool:
  """True if some (possibly nested, under any/all/not) rule is `type: param, key: <key>, equals: <equals>`."""
  found = False

  def _walk(rule: dict[str, Any]) -> None:
    nonlocal found
    if rule.get("type") == "param" and rule.get("key") == key and rule.get("equals") == equals:
      found = True
    elif rule.get("type") == "not" and "condition" in rule:
      _walk(rule["condition"])
    elif rule.get("type") in ("any", "all"):
      for c in rule.get("conditions", []):
        _walk(c)

  for rule in rules or []:
    _walk(rule)
  return found


def schema():
  return generate_schema()


class TestMadsBrandGates(OpenpilotTestCase):
  def test_mads_main_cruise_has_brand_gate(self, schema):
    """MadsMainCruiseAllowed must gate on brand and tesla_has_vehicle_bus."""
    item = _find_item(schema, "MadsMainCruiseAllowed")
    assert item is not None
    assert _references_capability_field(item.get("enablement"), "brand")
    assert _references_capability_field(item.get("enablement"), "tesla_has_vehicle_bus")

  def test_mads_unified_engagement_has_brand_gate(self, schema):
    """MadsUnifiedEngagementMode must mirror MadsMainCruiseAllowed brand-gate."""
    item = _find_item(schema, "MadsUnifiedEngagementMode")
    assert item is not None
    assert _references_capability_field(item.get("enablement"), "brand")
    assert _references_capability_field(item.get("enablement"), "tesla_has_vehicle_bus")


class TestTestManeuversSection(OpenpilotTestCase):
  def test_lateral_maneuver_mode_in_test_maneuvers(self, schema):
    section = _find_section(schema, "developer", "test_maneuvers")
    assert section is not None, "developer.test_maneuvers section missing"
    keys = {item["key"] for item in section.get("items", [])}
    assert "LateralManeuverMode" in keys
    assert "LongitudinalManeuverMode" in keys

  def test_test_maneuvers_section_requires_attestation(self, schema):
    section = _find_section(schema, "developer", "test_maneuvers")
    assert section is not None
    assert section.get("attestation_required") is True

  def test_test_maneuvers_section_visibility_gate(self, schema):
    section = _find_section(schema, "developer", "test_maneuvers")
    assert section is not None
    visibility = section.get("visibility")
    assert visibility, "test_maneuvers must have visibility gate"
    vis_refs = json.dumps(visibility)
    assert "is_development" in vis_refs
    assert "is_sp_release" in vis_refs
    enablement = section.get("enablement") or []
    enable_refs = json.dumps(enablement)
    assert "ShowAdvancedControls" in enable_refs, \
      "test_maneuvers must gate ShowAdvancedControls via enablement"


class TestValidator(OpenpilotTestCase):
  def test_validator_accepts_real_json(self):
    """settings_ui.json validates against settings_ui.schema.json."""
    try:
      import jsonschema
    except ImportError:
      self.skipTest("jsonschema not installed")
    with open(DEFINITION_PATH) as f:
      data = json.load(f)
    with open(SCHEMA_VALIDATOR_PATH) as f:
      validator = json.load(f)
    jsonschema.validate(instance=data, schema=validator)


class TestTorqueOptionGeneration(OpenpilotTestCase):
  def test_torque_versions_match_generated_options(self, schema):
    versions = _load_torque_versions()
    assert versions, "latcontrol_torque_versions.json must have at least one version"
    expected = _build_torque_options(versions)
    item = _find_item(schema, "TorqueControlTune")
    assert item is not None, "TorqueControlTune item must be present"
    assert item.get("options") == expected

  def test_torque_versions_path_resolves(self):
    assert os.path.exists(TORQUE_VERSIONS_PATH), (
      f"latcontrol_torque_versions.json not found at {TORQUE_VERSIONS_PATH}"
    )


class TestReleaseBranchGates(OpenpilotTestCase):
  @parameterized.expand([
    "EnableGithubRunner",
    "QuickBootToggle",
  ], names=["key"])
  def test_sp_dev_items_gate_on_is_sp_release(self, schema, key):
    """sunnypilot dev items must hide on sunnypilot release branches (is_sp_release gate)."""
    item = _find_item(schema, key)
    assert item is not None, f"{key} not found in schema"
    rules = (item.get("visibility") or []) + (item.get("enablement") or [])
    assert _references_capability_field(rules, "is_sp_release"), f"{key} missing is_sp_release gate"


class TestSpuriousOffroadGatesDropped(OpenpilotTestCase):
  def test_disengage_on_accelerator_has_no_offroad_only(self, schema):
    item = _find_item(schema, "DisengageOnAccelerator")
    assert item is not None
    assert "offroad_only" not in _flatten_rule_types(item.get("enablement"))

  def test_dynamic_experimental_has_no_offroad_only(self, schema):
    item = _find_item(schema, "DynamicExperimentalControl")
    assert item is not None
    assert "offroad_only" not in _flatten_rule_types(item.get("enablement"))


class TestNotEngagedReplacement(OpenpilotTestCase):
  @parameterized.expand([
    "AlphaLongitudinalEnabled",
    "ToyotaEnforceStockLongitudinal",
    "ToyotaStopAndGoHack",
  ], names=["key"])
  def test_offroad_only_replaced_with_not_engaged(self, schema, key):
    """These items should use not_engaged, not offroad_only."""
    item = _find_item(schema, key)
    assert item is not None, f"{key} not found"
    rule_types = _flatten_rule_types(item.get("enablement"))
    assert "offroad_only" not in rule_types, f"{key} still uses offroad_only"
    assert "not_engaged" in rule_types, f"{key} missing not_engaged"


class TestDriverMonitoringModeRemote(OpenpilotTestCase):
  def test_driver_monitoring_mode_present(self, schema):
    """DriverMonitoringMode must be remotely configurable, not device-only."""
    item = _find_item(schema, "DriverMonitoringMode")
    assert item is not None, "DriverMonitoringMode missing from settings_ui schema"
    assert item.get("widget") == "multiple_button"
    assert [o["value"] for o in item.get("options", [])] == [0, 1, 2]

  def test_driver_monitoring_mode_is_offroad_only(self, schema):
    """Never let a phone weaken driver monitoring on a car that is driving."""
    item = _find_item(schema, "DriverMonitoringMode")
    assert item is not None
    assert "offroad_only" in _flatten_rule_types(item.get("enablement"))

  def test_driver_monitoring_mode_requires_attestation(self, schema):
    """Each remote write needs an explicit confirmation modal."""
    item = _find_item(schema, "DriverMonitoringMode")
    assert item is not None
    assert item.get("requires_attestation") is True


class TestKoreaMapRemote(OpenpilotTestCase):
  def test_external_nav_toggle_present(self, schema):
    item = _find_item(schema, "KoreaExternalNavEnabled")
    assert item is not None, "KoreaExternalNavEnabled missing from settings_ui schema"
    assert item.get("widget") == "toggle"

  def test_the_korea_map_settings_apply_without_a_cycle(self, schema):
    """Both are read every tick by mapd_manager's supervisor loop, which tears the running
    source down and starts the one the params now name. An onroad/offroad cycle would not
    help anyway: mapd_manager is registered always_run, so it never stops."""
    for key in ("MapDataSource", "KoreaExternalNavEnabled"):
      item = _find_item(schema, key)
      assert item is not None
      assert not item.get("needs_onroad_cycle"), f"{key} claims it takes an onroad cycle"

  def test_external_nav_toggle_is_offroad_only(self, schema):
    """Opening a control-plane UDP port mid-drive from a phone is not allowed."""
    item = _find_item(schema, "KoreaExternalNavEnabled")
    assert item is not None
    assert "offroad_only" in _flatten_rule_types(item.get("enablement"))

  def test_external_nav_toggle_requires_korea_map_source(self, schema):
    """Both device UIs additionally gate this on MapDataSource == korea (speed_limit_settings.py
    and mici toggles.py) -- the remote must not allow enabling it while OSM is the active source."""
    item = _find_item(schema, "KoreaExternalNavEnabled")
    assert item is not None
    assert _references_param_equals(item.get("enablement"), "MapDataSource", 1), \
      "KoreaExternalNavEnabled missing MapDataSource == korea (1) gate"


class TestKoreaMapSettings(OpenpilotTestCase):
  """The source selector and the external-nav toggle must exist on the remote surface.
  A raylib-only setting is invisible to sunnylink users."""

  def test_the_remote_settings_are_in_the_schema(self):
    schema = generate_schema()
    for key in ("MapDataSource", "KoreaExternalNavEnabled"):
      self.assertIsNotNone(_find_item(schema, key), f"{key} missing from the sunnylink schema")

  def test_the_api_key_stays_off_the_remote_surface(self):
    """The schema has no free-text widget (settings_ui.schema.json enumerates
    toggle/option/multiple_button/button/info), and an API key does not belong on a
    remote surface anyway. It is device-only, entered through InputDialogSP."""
    self.assertIsNone(_find_item(generate_schema(), "KoreaMapApiKey"))

  def test_the_source_selector_offers_exactly_two_sources(self):
    """The option values are the button indices the raylib widget writes to the param.
    A third option here without a MapSource member would write a value nothing handles."""
    item = _find_item(generate_schema(), "MapDataSource")
    self.assertEqual([o["value"] for o in item["options"]], [0, 1])


class TestSmartCruiseControlMapRemote(OpenpilotTestCase):
  """SmartCruiseControlMap consumes map curve geometry that only the OSM path produces
  (cruise.py:175-176 additionally disables the tici toggle when not OSM). mici has no
  SCC-Map surface, so the remote must independently match the tici rule."""

  def test_smart_cruise_control_map_requires_osm_source(self, schema):
    """MapDataSource == osm must be a top-level enablement item, ANDed with the existing
    any(has_longitudinal_control, has_icbm) block -- not folded inside that any block's own
    conditions list, which would let MapDataSource == osm alone satisfy the whole rule with
    no capability check at all."""
    item = _find_item(schema, "SmartCruiseControlMap")
    assert item is not None
    enablement = item.get("enablement") or []
    assert any(r.get("type") == "param" and r.get("key") == "MapDataSource" and r.get("equals") == 0
               for r in enablement), "MapDataSource == osm (0) missing as a top-level (ANDed) enablement item"

  def test_smart_cruise_control_map_still_requires_a_capability(self, schema):
    """The MapDataSource gate must not have replaced the capability check."""
    item = _find_item(schema, "SmartCruiseControlMap")
    assert item is not None
    assert _references_capability_field(item.get("enablement"), "has_longitudinal_control")
    assert _references_capability_field(item.get("enablement"), "has_icbm")
