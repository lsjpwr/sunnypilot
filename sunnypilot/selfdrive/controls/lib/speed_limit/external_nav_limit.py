"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

ExternalNavLimit: turns externalNavDataSP camera alerts (from tmapd) into
(speed limit, distance) solutions for the SpeedLimitResolver, with per-camera-type
enable/offset params and average-speed management for section control cameras.
"""
import time

import cereal.messaging as messaging
from cereal import custom
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD, get_sanitize_int_param
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit import LIMIT_ADAPT_ACC

AlertType = custom.ExternalNavDataSP.NavAlert.AlertType

DEFAULT_CAMERA_HOLD_DISTANCE = 300  # m, hold the limit within this distance even when already below it
CAMERA_HOLD_DISTANCE_MIN = 100
CAMERA_HOLD_DISTANCE_MAX = 1000
BUMP_HOLD_DISTANCE = 50.     # m, speed bumps engage much later than cameras
BUMP_TARGET_MIN = 15         # km/h (mph if not metric)
BUMP_TARGET_MAX = 50
TURN_HOLD_DISTANCE = 80.     # m, route turns
TURN_TARGET_MIN = 20
TURN_TARGET_MAX = 50
RAMP_HOLD_DISTANCE = 150.    # m, highway ramps
RAMP_TARGET_MIN = 40
RAMP_TARGET_MAX = 80
PASS_DETECT_DISTANCE = 60.   # m, an alert that disappears closer than this counts as passed
SECTION_MAX_DURATION = 15. * 60.  # s, safety timeout if the section end alert is never received
SECTION_MAX_LENGTH = 25000.  # m
SECTION_MIN_ELAPSED = 5.     # s, before this, use v_ego as the average speed
SECTION_COMP_GAIN = 1.0      # how aggressively to compensate when the section average exceeds the limit
SECTION_MIN_LIMIT_FACTOR = 0.85  # never compensate below this fraction of the section limit

CAMERA_ALERT_TYPES = {
  AlertType.camFixed: "fixed",
  AlertType.camMobile: "mobile",
  AlertType.camSectionStart: "section",
}


class ExternalNavLimit:
  def __init__(self):
    self.params = Params()
    self.frame = -1

    # outputs consumed by the resolver
    self.limit = 0.      # m/s, raw limit (or compensated section target)
    self.distance = 0.   # m
    self.offset = 0.     # m/s, per-type offset for the active solution

    self.enabled: dict[str, bool] = {"fixed": False, "mobile": False, "section": False, "bump": False, "route": False}
    self.offsets: dict[str, float] = {"fixed": 0., "mobile": 0., "section": 0.}
    self.is_metric = True
    self.hold_distance = float(DEFAULT_CAMERA_HOLD_DISTANCE)
    self.bump_target = 0.  # m/s
    self.turn_target = 0.  # m/s
    self.ramp_target = 0.  # m/s

    # section control state
    self.section_active = False
    self.section_limit = 0.
    self.section_pending_limit = 0.
    self.section_start_mono = 0.
    self.section_dist = 0.

    self.last_seen: dict[int, float] = {}  # AlertType -> last seen distance

  def update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.is_metric = self.params.get_bool("IsMetric")
      unit = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS
      for cam in ("fixed", "mobile", "section"):
        name = cam.capitalize()
        self.enabled[cam] = self.params.get_bool(f"TmapCam{name}Enabled")
        self.offsets[cam] = float(self.params.get(f"TmapCam{name}Offset", return_default=True)) * unit
      self.hold_distance = float(get_sanitize_int_param(
        "TmapCamHoldDistance", CAMERA_HOLD_DISTANCE_MIN, CAMERA_HOLD_DISTANCE_MAX, self.params))
      self.enabled["bump"] = self.params.get_bool("TmapBumpEnabled")
      self.bump_target = float(get_sanitize_int_param(
        "TmapBumpTargetSpeed", BUMP_TARGET_MIN, BUMP_TARGET_MAX, self.params)) * unit
      self.enabled["route"] = self.params.get_bool("TmapRouteControlEnabled")
      self.turn_target = float(get_sanitize_int_param(
        "TmapRouteTurnSpeed", TURN_TARGET_MIN, TURN_TARGET_MAX, self.params)) * unit
      self.ramp_target = float(get_sanitize_int_param(
        "TmapRouteRampSpeed", RAMP_TARGET_MIN, RAMP_TARGET_MAX, self.params)) * unit

  def _adapt_distance(self, v_ego: float, limit: float) -> float:
    """Distance needed to reach the limit at LIMIT_ADAPT_ACC."""
    if 0. < limit < v_ego:
      adapt_time = (limit - v_ego) / LIMIT_ADAPT_ACC
      return v_ego * adapt_time + 0.5 * LIMIT_ADAPT_ACC * adapt_time ** 2
    return 0.

  def _adapt_window(self, v_ego: float, limit: float) -> float:
    """Distance within which an upcoming camera limit becomes an active solution."""
    return max(self._adapt_distance(v_ego, limit), self.hold_distance)

  def _update_section_state(self, current: dict[int, float], v_ego: float) -> None:
    # entering: the start alert reaches zero, or disappears while close
    start_dist = current.get(AlertType.camSectionStart)
    start_passed = (start_dist is not None and start_dist <= 0.) or \
                   (AlertType.camSectionStart not in current and
                    self.last_seen.get(AlertType.camSectionStart, float('inf')) < PASS_DETECT_DISTANCE)
    if start_passed and not self.section_active and self.enabled["section"]:
      limit = self.section_pending_limit
      if limit > 0.:
        self.section_active = True
        self.section_limit = limit
        self.section_start_mono = time.monotonic()
        self.section_dist = 0.

    if self.section_active:
      self.section_dist += v_ego * DT_MDL
      elapsed = time.monotonic() - self.section_start_mono

      end_dist = current.get(AlertType.camSectionEnd)
      end_passed = (end_dist is not None and end_dist <= 0.) or \
                   (AlertType.camSectionEnd not in current and
                    self.last_seen.get(AlertType.camSectionEnd, float('inf')) < PASS_DETECT_DISTANCE)
      timed_out = elapsed > SECTION_MAX_DURATION or self.section_dist > SECTION_MAX_LENGTH
      if end_passed or timed_out or not self.enabled["section"]:
        self.section_active = False

  def _section_solution(self, v_ego: float) -> tuple[float, float]:
    """Compensated target so the section average converges to the limit."""
    elapsed = time.monotonic() - self.section_start_mono
    avg = self.section_dist / elapsed if elapsed > SECTION_MIN_ELAPSED else v_ego
    target = self.section_limit
    if avg > self.section_limit:
      target -= SECTION_COMP_GAIN * (avg - self.section_limit)
      target = max(target, self.section_limit * SECTION_MIN_LIMIT_FACTOR)
    return target, 0.

  def _camera_solution(self, current: dict[int, dict], v_ego: float) -> tuple[float, float, float]:
    """Nearest enabled upcoming camera or speed bump within its engagement window."""
    best = None
    for alert_type, cam in CAMERA_ALERT_TYPES.items():
      alert = current.get(alert_type)
      if alert is None or not self.enabled[cam] or alert["limit"] <= 0. or alert["distance"] < 0.:
        continue
      if alert["distance"] <= self._adapt_window(v_ego, alert["limit"] + self.offsets[cam]):
        if best is None or alert["distance"] < best[1]:
          best = (alert["limit"], alert["distance"], self.offsets[cam])

    # point targets with a fixed speed: bumps, route turns, highway ramps
    point_targets = (
      (AlertType.speedBump, "bump", self.bump_target, BUMP_HOLD_DISTANCE),
      (AlertType.turnSharp, "route", self.turn_target, TURN_HOLD_DISTANCE),
      (AlertType.ramp, "route", self.ramp_target, RAMP_HOLD_DISTANCE),
    )
    for alert_type, key, target, hold in point_targets:
      alert = current.get(alert_type)
      if alert is None or not self.enabled[key] or target <= 0. or alert["distance"] <= 0.:
        continue
      if alert["distance"] <= max(self._adapt_distance(v_ego, target), hold):
        if best is None or alert["distance"] < best[1]:
          best = (target, alert["distance"], 0.)

    return best if best is not None else (0., 0., 0.)

  def update(self, v_ego: float, sm: messaging.SubMaster) -> None:
    self.frame += 1
    self.update_params()

    # plant.py and some tests pass a plain dict instead of a SubMaster
    alive = getattr(sm, 'alive', None)
    alerts = sm['externalNavDataSP'].alerts if alive is not None and alive.get('externalNavDataSP') else []
    current_dist: dict[int, float] = {}
    current_full: dict[int, dict] = {}
    for a in alerts:
      # keep the nearest alert per type (route plans can carry several upcoming maneuvers)
      if a.alertType in current_full and current_full[a.alertType]["distance"] <= a.distance:
        continue
      current_dist[a.alertType] = a.distance
      current_full[a.alertType] = {"limit": a.speedLimit, "distance": a.distance}

    # remember the section limit while approaching, the alert is gone once we are inside
    if AlertType.camSectionStart in current_full:
      self.section_pending_limit = current_full[AlertType.camSectionStart]["limit"]

    self._update_section_state(current_dist, v_ego)
    self.last_seen = current_dist

    if self.section_active:
      self.limit, self.distance = self._section_solution(v_ego)
      self.offset = self.offsets["section"]
    else:
      self.limit, self.distance, self.offset = self._camera_solution(current_full, v_ego)
