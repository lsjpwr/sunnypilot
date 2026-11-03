"""
The mici HUD's auto SLA numbers (green MAX, 구간단속 AVG) come from plannerd. When plannerd dies, the
SubMaster keeps its last plan: on 2026-10-03 a green 80 stayed up for 18 minutes after a crash.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from openpilot.common.constants import CV
from openpilot.common.test import OpenpilotTestCase


class FakeSM:
  def __init__(self, msgs, alive):
    self.msgs, self.alive = msgs, alive

  def __getitem__(self, key):
    return self.msgs[key]


def plan(sla_kph):
  assist = SimpleNamespace(enabled=True, vTarget=sla_kph * CV.KPH_TO_MS)
  return SimpleNamespace(speedLimit=SimpleNamespace(assist=assist),
                         smartCruiseControl=SimpleNamespace(map=SimpleNamespace(active=False, vTarget=255.)))


class TestHudAutoSla(OpenpilotTestCase):
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_a_dead_planner_shows_none_of_its_last_plan(self, subtests):
    from openpilot.selfdrive.ui.sunnypilot.mici.onroad.hud_renderer import HudRendererSP
    from openpilot.selfdrive.ui.ui_state import ui_state

    for alive, green, avg in ((True, 80, (90, True)), (False, None, None)):
      with subtests.test(alive=alive):
        hud = HudRendererSP.__new__(HudRendererSP)  # the state update needs no fonts or window
        hud._engaged, hud._shown_prev, hud.set_speed = False, None, 127.
        sm = FakeSM({"longitudinalPlanSP": plan(80.), "carState": SimpleNamespace(vCruiseCluster=127.)},
                    {"longitudinalPlanSP": alive})
        with patch.multiple(ui_state, tesla_auto_sla=True, CP=SimpleNamespace(brand="tesla"), sm=sm,
                            section_avg=90 * CV.KPH_TO_MS, section_limit=80 * CV.KPH_TO_MS, is_metric=True):
          hud._update_auto_sla()
        assert (round(hud._sla_kph) if hud._sla_kph is not None else None) == green
        assert hud._section_avg == avg

  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_mads_engagement_starts_the_icon_fade(self):
    """Merge 5fd014bac left this block after a return in _set_speed_colors, where it never ran."""
    from openpilot.selfdrive.ui.mici.onroad.hud_renderer import HudRenderer
    from openpilot.selfdrive.ui.sunnypilot.mici.onroad import hud_renderer
    from openpilot.selfdrive.ui.ui_state import UIState

    hud = hud_renderer.HudRendererSP.__new__(hud_renderer.HudRendererSP)
    hud.blind_spot_indicators, hud._sp_engaged, hud._chestnut_fade_time = Mock(), False, 0
    with patch.object(HudRenderer, "_update_state"), patch.object(hud_renderer.HudRendererSP, "_update_auto_sla"), \
         patch.object(UIState, "engaged", new_callable=PropertyMock, return_value=True), \
         patch.object(hud_renderer.rl, "get_time", return_value=42.):
      hud._update_state()
    assert (hud._sp_engaged, hud._chestnut_fade_time) == (True, 42.)
