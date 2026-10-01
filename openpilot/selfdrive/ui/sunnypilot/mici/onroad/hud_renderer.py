"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.selfdrive.ui.mici.onroad.hud_renderer import HudRenderer
from openpilot.selfdrive.ui.sunnypilot.onroad.blind_spot_indicators import BlindSpotIndicators
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import comma_target_kph

SLA_GREEN = (0x80, 0xd8, 0xa6)  # the big HUD's speed limit assist green


class HudRendererSP(HudRenderer):
  def __init__(self):
    super().__init__()
    self.blind_spot_indicators = BlindSpotIndicators()
    self._sla_kph: float | None = None
    self._shown_prev: float | None = None

  def _update_state(self) -> None:
    super()._update_state()
    self.blind_spot_indicators.update()
    self._update_auto_sla()

  def _update_auto_sla(self) -> None:
    sla_kph = None
    if ui_state.tesla_auto_sla and ui_state.CP is not None and ui_state.CP.brand == "tesla":
      plan = ui_state.sm['longitudinalPlanSP']
      assist, scc_map = plan.speedLimit.assist, plan.smartCruiseControl.map
      sla_kph = comma_target_kph(assist.active, assist.vTarget, scc_map.active, scc_map.vTarget,
                                 ui_state.sm['carState'].vCruiseCluster)
    # bring the MAX box up whenever the number it shows changes, the way a set speed change does
    shown = sla_kph if sla_kph is not None else self.set_speed
    if self._engaged and self._shown_prev is not None and round(shown) != round(self._shown_prev):
      self._set_speed_changed_time = rl.get_time()
    self._shown_prev = shown
    self._sla_kph = sla_kph

  def _display_set_speed(self) -> float:
    return self._sla_kph if self._sla_kph is not None else super()._display_set_speed()

  def _set_speed_colors(self, alpha: float) -> tuple[rl.Color, rl.Color]:
    if self._sla_kph is None:
      return super()._set_speed_colors(alpha)
    color = rl.Color(*SLA_GREEN, int(255 * 0.9 * alpha))
    return color, color

  def _render(self, rect: rl.Rectangle) -> None:
    super()._render(rect)
    self.blind_spot_indicators.render(rect)

  def _has_blind_spot_detected(self) -> bool:

    return self.blind_spot_indicators.detected
