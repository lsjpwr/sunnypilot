"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.cereal import custom
from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.auto_speed_limit import V_CRUISE_UNSET
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode as SpeedLimitMode


def compare_cluster_target(v_cruise_cluster: float, target_set_speed: float, is_metric: bool) -> tuple[bool, bool]:
  speed_conv = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
  v_cruise_cluster_conv = round(v_cruise_cluster * speed_conv)
  target_set_speed_conv = round(target_set_speed * speed_conv)

  req_plus = v_cruise_cluster_conv < target_set_speed_conv
  req_minus = v_cruise_cluster_conv > target_set_speed_conv

  return req_plus, req_minus


def set_speed_limit_assist_availability(CP: car.CarParams, CP_SP: custom.CarParamsSP, params: Params | None = None) -> bool:
  if params is None:
    params = Params()

  is_release = params.get_bool("IsReleaseSpBranch")
  disallow_in_release = CP.brand == "tesla" and is_release
  always_disallow = CP.brand == "rivian"
  allowed = True

  if disallow_in_release or always_disallow:
    allowed = False

  if not CP.openpilotLongitudinalControl and CP_SP.pcmCruiseSpeed:
    allowed = False

  if not allowed:
    if params.get("SpeedLimitMode", return_default=True) == SpeedLimitMode.assist:
      params.put("SpeedLimitMode", int(SpeedLimitMode.warning), block=True)

  return allowed


def comma_target_kph(sla_enabled: bool, sla_target: float, map_active: bool, map_target: float,
                     v_cruise_cluster_kph: float) -> float | None:
  """What the comma MAX box shows instead of the Tesla set speed: the lower of the enabled speed limit
  assist target (with no limit known yet, the max speed alone) and the active SCC-Map target (limits,
  구간단속, cameras, bumps), while it holds the car under the Tesla set speed. None otherwise."""
  targets = [v for on, v in ((sla_enabled, sla_target), (map_active, map_target)) if on and v < V_CRUISE_UNSET]
  if not targets or v_cruise_cluster_kph <= 0.:
    return None
  target_kph = min(targets) * CV.MS_TO_KPH
  return target_kph if round(target_kph) < round(v_cruise_cluster_kph) else None


def section_average_display(avg: float, limit: float, is_metric: bool) -> tuple[int, bool] | None:
  """The 구간단속 average the mici HUD shows, in display units, and whether it is over the section's
  limit. None outside a section."""
  if avg <= 0. or limit <= 0.:
    return None
  factor = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
  shown = round(avg * factor)
  return shown, shown > round(limit * factor)
