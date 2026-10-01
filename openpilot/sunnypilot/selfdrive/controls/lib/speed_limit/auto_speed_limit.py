"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
# Same value as selfdrive.car.cruise.V_CRUISE_UNSET; importing it from there is an import cycle
# (cruise -> cruise_ext -> speed_limit_assist -> here).
V_CRUISE_UNSET = 255.
# m/s. A scroll down never takes the target under this, nor under the zone's own target when that is lower.
NUDGE_MIN_SPEED = 8.33
# m/s. The Tesla set speed moves in whole km/h or mph, so anything smaller is noise.
SCROLL_EPSILON = 1e-3


class AutoSpeedLimit:
  """Speed limit target without confirmation, for TeslaAutoSpeedLimitAssist.

  The Tesla set speed stays the ceiling: the planner takes the min of it and this target. Under
  it the target is the speed limit plus offset. A new value is taken only after it has held for
  `delay` seconds, so a 0.1 s Tesla flicker or a 1 s map flap changes nothing. A set speed change
  the driver scrolls in while engaged moves the target by the same amount (the nudge), and the
  nudge lasts until cruise is canceled.
  """

  def __init__(self):
    self.reset(0.)

  def reset(self, limit: float) -> None:
    """Engagement: take the limit as it is, with no hold and no nudge."""
    self.limit = limit
    self.nudge = 0.
    self.changed = False
    self._pending = limit
    self._pending_frames = 0
    self._cluster_prev: float | None = None

  @property
  def v_target(self) -> float:
    return self.limit + self.nudge if self.limit > 0. else V_CRUISE_UNSET

  def update(self, limit: float, v_cruise_cluster: float, scroll_allowed: bool, delay: float, dt: float) -> None:
    self._update_limit(limit, delay, dt)
    self._update_nudge(v_cruise_cluster, scroll_allowed)

  def _update_limit(self, limit: float, delay: float, dt: float) -> None:
    self.changed = False
    if limit == self.limit:
      # back before the hold ran out: it was a flicker
      self._pending, self._pending_frames = limit, 0
      return
    if limit != self._pending:
      self._pending, self._pending_frames = limit, 0
    self._pending_frames += 1
    if self._pending_frames >= round(delay / dt):
      self.limit, self.changed = limit, True
      self._pending_frames = 0
      self._clamp_nudge()

  def _update_nudge(self, v_cruise_cluster: float, scroll_allowed: bool) -> None:
    prev, self._cluster_prev = self._cluster_prev, v_cruise_cluster
    if prev is None or not scroll_allowed or self.limit <= 0.:
      return
    delta = v_cruise_cluster - prev
    if abs(delta) < SCROLL_EPSILON:
      return
    if self.v_target < prev:
      # this target was holding the car under the set speed: move it with the scroll
      self.nudge += delta
    elif delta > 0.:
      # the set speed was the ceiling, so only the part of the scroll above this target counts
      self.nudge = max(self.nudge, v_cruise_cluster - self.limit)
    self._clamp_nudge()

  def _clamp_nudge(self) -> None:
    self.nudge = max(self.nudge, min(self.limit, NUDGE_MIN_SPEED) - self.limit)
