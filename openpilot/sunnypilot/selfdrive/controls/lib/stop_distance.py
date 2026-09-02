"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import STOP_DISTANCE

# Range offered by the UI. 4.0 m is the floor: the MPC's danger-zone constraint sits at
# LEAD_DANGER_FACTOR * 6.0 = 4.5 m in shifted coordinates, so a 4.0 m target still leaves a
# 2.5 m hard floor in real ones. Below that there is nothing left for a rear-end shove to spend.
STOP_DISTANCE_MIN = 4.0
STOP_DISTANCE_MAX = 6.0

# Congestion is read off the lead, not off ego speed. A lead stopped at a light is known
# tens of metres out, so the offset is fully applied before braking even starts. Gating on
# ego speed instead only opens once deceleration is nearly over, and the remainder fills in
# after the car has stopped -- which creeps it forward. The gap between the two thresholds
# keeps the offset constant across a whole stop-and-go episode; the solution is only a rigid
# translation of the stock one while the offset is constant.
CONGESTION_LEAD_V_ENTER = 8.33   # m/s (30 km/h)
CONGESTION_LEAD_V_EXIT = 13.89   # m/s (50 km/h)

OFFSET_SLEW = 0.5                # m/s
STANDSTILL_V = 0.3               # m/s, matches should_stop() in drive_helpers


class StopDistanceController:
  """Shortens the gap the MPC holds from a slow lead, in metres."""

  def __init__(self, mpc, params=None):
    self._mpc = mpc
    self._params = params or Params()
    self._frame = 0
    self._congested = False
    self._offset = 0.
    self._applied = 0.
    self._read_params()

  def _read_params(self) -> None:
    if self._frame % int(1. / DT_MDL) != 0:
      return

    # Clamp and write back, the same shape as get_sanitize_int_param. Only one caller
    # needs the float version, so it stays here until a second one shows up.
    value = float(self._params.get("StopDistance", return_default=True))
    clipped = max(STOP_DISTANCE_MIN, min(STOP_DISTANCE_MAX, value))
    if clipped != value:
      self._params.put("StopDistance", clipped, block=True)

    self._offset = STOP_DISTANCE - clipped

  def update(self, sm) -> None:
    self._read_params()

    # No lead means no obstacle to sit behind: process_lead() fakes one 50 m out, so the
    # offset would have nothing to act on.
    lead = sm['radarState'].leadOne
    threshold = CONGESTION_LEAD_V_EXIT if self._congested else CONGESTION_LEAD_V_ENTER
    self._congested = bool(lead.present) and lead.vLead < threshold

    target = self._offset if self._congested else 0.

    # Only the direction that shortens the gap is rate-limited, and it is frozen at a
    # standstill: a growing offset reads as the lead pulling away, which would creep the
    # car forward after it had already stopped. Shrinking the offset asks for more brake,
    # so it applies at once.
    if target > self._applied:
      if sm['carState'].vEgo > STANDSTILL_V:
        self._applied = min(target, self._applied + OFFSET_SLEW * DT_MDL)
    else:
      self._applied = target

    self._mpc.stop_distance = STOP_DISTANCE - self._applied
    self._frame += 1
