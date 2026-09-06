"""
Regression test for SpeedLimitSettingsLayout's korea-mode gating -- the big-UI (tici)
counterpart of the mici gating covered by
openpilot/selfdrive/ui/mici/tests/test_toggles_api_key.py::TestKoreaModeGating.

speed_limit_settings.py:_update_state() enables three action items on different rules:
  - _map_source only requires offroad, independent of MapDataSource.
  - _external_nav requires MapDataSource == korea AND offroad (KoreaExternalNavEnabled
    is offroad-only, same as the mici korea_nav_toggle).
  - _api_key requires MapDataSource == korea only -- it stays enabled onroad.
  - _speed_bump requires MapDataSource == korea AND (longitudinal control or ICBM), since
    it ultimately drives SmartCruiseControlMap, which cannot act without one of those;
    _bump_arch_speed and _bump_trapezoid_speed additionally require KoreaSpeedBumpEnabled.
Previously uncovered by any committed test. Drives the real _update_state() on a real
constructed layout rather than reimplementing the predicate.
"""
import os
import shutil
import unittest
from types import SimpleNamespace

import pyray as rl

from openpilot.common.test import OpenpilotTestCase


class TestSpeedLimitSettingsKoreaGating(OpenpilotTestCase):
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_external_nav_api_key_map_source_enabled_state(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_speed_limit_settings_korea_gating")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise_sub_layouts.speed_limit_settings import SpeedLimitSettingsLayout
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.sunnypilot.mapd import MapSource

    # Same process-wide-singleton hazard documented in mici/tests/test_toggles_api_key.py
    # (see _ensure_params_dir / TestKoreaModeGating there): ui_state.params may already be
    # bound to a directory some earlier test's OpenpilotPrefix has since deleted, silently
    # no-op'ing put()/get(). Recreate it defensively -- which test in the whole run first
    # imports ui_state is not something this file controls.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original_source = ui_state.params.get("MapDataSource", return_default=True)
    original_started = ui_state.started

    def _restore():
      ui_state.params.put("MapDataSource", int(original_source), block=True)
      ui_state.started = original_started
    self.addCleanup(_restore)

    layout = SpeedLimitSettingsLayout(lambda: None)

    with subtests.test(case="korea + offroad -> external_nav, api_key, map_source all enabled"):
      ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
      ui_state.started = False
      layout._update_state()
      self.assertTrue(layout._external_nav.action_item.enabled)
      self.assertTrue(layout._api_key.action_item.enabled)
      self.assertTrue(layout._map_source.action_item.enabled)

    with subtests.test(case="korea + onroad -> external_nav and map_source disabled, api_key stays enabled"):
      ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
      ui_state.started = True
      layout._update_state()
      self.assertFalse(layout._external_nav.action_item.enabled)
      self.assertTrue(layout._api_key.action_item.enabled)
      self.assertFalse(layout._map_source.action_item.enabled)

    with subtests.test(case="osm + offroad -> external_nav and api_key disabled"):
      ui_state.params.put("MapDataSource", int(MapSource.osm), block=True)
      ui_state.started = False
      layout._update_state()
      self.assertFalse(layout._external_nav.action_item.enabled)
      self.assertFalse(layout._api_key.action_item.enabled)

  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_speed_bump_gating(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_speed_limit_settings_speed_bump_gating")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise_sub_layouts.speed_limit_settings import SpeedLimitSettingsLayout
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.sunnypilot.mapd import MapSource

    # Same process-wide-singleton hazard as the test above: ui_state.params may be bound
    # to a directory an earlier test's OpenpilotPrefix has since deleted.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original_source = ui_state.params.get("MapDataSource", return_default=True)
    original_bump = ui_state.params.get_bool("KoreaSpeedBumpEnabled")
    original_cp = ui_state.CP
    original_cp_sp = ui_state.CP_SP
    original_has_long = ui_state.has_longitudinal_control
    original_has_icbm = ui_state.has_icbm

    def _restore():
      ui_state.params.put("MapDataSource", int(original_source), block=True)
      ui_state.params.put_bool("KoreaSpeedBumpEnabled", original_bump, block=True)
      ui_state.CP = original_cp
      ui_state.CP_SP = original_cp_sp
      ui_state.has_longitudinal_control = original_has_long
      ui_state.has_icbm = original_has_icbm
    self.addCleanup(_restore)

    layout = SpeedLimitSettingsLayout(lambda: None)

    # has_longitudinal_control/has_icbm are cached ui_state attributes (refreshed from CP/CP_SP
    # elsewhere, not read fresh per call) -- set directly, same as ui_state.is_metric and
    # ui_state.started elsewhere in this file. CP only needs to be non-None for the bump gate;
    # CP_SP is held None so the unrelated Speed Limit Assist block further down in
    # _update_state (which reads ui_state.CP.brand once CP_SP is ALSO set) takes its "no CP"
    # branch instead of touching a bare stand-in CP object.
    ui_state.CP = SimpleNamespace()
    ui_state.CP_SP = None
    ui_state.has_longitudinal_control = True
    ui_state.has_icbm = False

    with subtests.test(case="korea + bump off -> toggle enabled, both speeds disabled"):
      ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
      ui_state.params.put_bool("KoreaSpeedBumpEnabled", False, block=True)
      layout._update_state()
      self.assertTrue(layout._speed_bump.action_item.enabled)
      self.assertFalse(layout._bump_arch_speed.action_item.enabled)
      self.assertFalse(layout._bump_trapezoid_speed.action_item.enabled)

    with subtests.test(case="korea + bump on -> both speeds enabled"):
      ui_state.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
      layout._update_state()
      self.assertTrue(layout._bump_arch_speed.action_item.enabled)
      self.assertTrue(layout._bump_trapezoid_speed.action_item.enabled)

    with subtests.test(case="osm -> everything bump-related disabled even with the toggle on"):
      ui_state.params.put("MapDataSource", int(MapSource.osm), block=True)
      ui_state.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
      layout._update_state()
      self.assertFalse(layout._speed_bump.action_item.enabled)
      self.assertFalse(layout._bump_arch_speed.action_item.enabled)
      self.assertFalse(layout._bump_trapezoid_speed.action_item.enabled)

    with subtests.test(case="korea + no longitudinal + no icbm -> everything bump-related disabled " +
                            "even with the toggle on (SmartCruiseControlMap could not act anyway)"):
      ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
      ui_state.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
      ui_state.has_longitudinal_control = False
      ui_state.has_icbm = False
      layout._update_state()
      self.assertFalse(layout._speed_bump.action_item.enabled)
      self.assertFalse(layout._bump_arch_speed.action_item.enabled)
      self.assertFalse(layout._bump_trapezoid_speed.action_item.enabled)

    with subtests.test(case="korea + icbm only (no full longitudinal) -> still enabled"):
      ui_state.has_longitudinal_control = False
      ui_state.has_icbm = True
      layout._update_state()
      self.assertTrue(layout._speed_bump.action_item.enabled)

    with subtests.test(case="korea + no CP at all -> disabled even though has_longitudinal_control is stale-True"):
      ui_state.CP = None
      ui_state.has_longitudinal_control = True
      ui_state.has_icbm = False
      layout._update_state()
      self.assertFalse(layout._speed_bump.action_item.enabled)

  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_auto_download_gating(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_speed_limit_settings_auto_download_gating")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise_sub_layouts.speed_limit_settings import SpeedLimitSettingsLayout
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.sunnypilot.mapd import MapSource

    # Same process-wide-singleton hazard as the tests above: ui_state.params may be bound
    # to a directory an earlier test's OpenpilotPrefix has since deleted.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original_source = ui_state.params.get("MapDataSource", return_default=True)
    original_started = ui_state.started

    def _restore():
      ui_state.params.put("MapDataSource", int(original_source), block=True)
      ui_state.started = original_started
    self.addCleanup(_restore)

    layout = SpeedLimitSettingsLayout(lambda: None)

    # Explicit, not inherited: ui_state is a process-wide singleton and this loop asserts
    # enabled is True, which only holds offroad. Leaving it to the default made this subtest
    # depend on running before the onroad one below.
    ui_state.started = False

    for source, expected, why in (
      (MapSource.korea, True, "the korea source is the only one this downloads for"),
      (MapSource.osm, False, "OSM gets its map data from the mapd binary, not from us"),
    ):
      with subtests.test(source=source):
        ui_state.params.put("MapDataSource", int(source), block=True)
        layout._update_state()
        assert layout._auto_download.action_item.enabled is expected, why

    with subtests.test(case="korea + onroad -> disabled (220 MB must not compete with the drive)"):
      ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
      ui_state.started = True
      layout._update_state()
      self.assertFalse(layout._auto_download.action_item.enabled)

  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_bump_speed_label_converts_for_is_metric(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_speed_limit_settings_bump_speed_label")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise_sub_layouts.speed_limit_settings import SpeedLimitSettingsLayout
    from openpilot.selfdrive.ui.ui_state import ui_state

    # Same process-wide-singleton hazard as the tests above: ui_state.params may be bound
    # to a directory an earlier test's OpenpilotPrefix has since deleted.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    # is_metric is a cached ui_state attribute (refreshed from the IsMetric param only by
    # update_params(), not read fresh per call) -- set it directly, same as ui_state.started
    # in test_external_nav_api_key_map_source_enabled_state above, rather than through params.
    original_is_metric = ui_state.is_metric

    def _restore():
      ui_state.is_metric = original_is_metric
    self.addCleanup(_restore)

    layout = SpeedLimitSettingsLayout(lambda: None)

    with subtests.test(case="metric -> km/h, value unconverted"):
      ui_state.is_metric = True
      self.assertEqual(layout._get_bump_speed_label(25), "25 km/h")

    with subtests.test(case="imperial -> mph, value converted (not a label-only unit swap)"):
      ui_state.is_metric = False
      self.assertEqual(layout._get_bump_speed_label(25), "15.5 mph")
