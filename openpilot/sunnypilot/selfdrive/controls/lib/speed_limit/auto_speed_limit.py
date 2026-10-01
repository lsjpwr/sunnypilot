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
# s. Inside a 구간단속 section, time lost to a slowdown comes back above the section's pace and a
# lead the driver took is given back below it, at credit / SECTION_CREDIT_TAU.
SECTION_CREDIT_TAU = 60.
# m/s (10 km/h). Giving a lead back never takes the target further under the pace than this.
SECTION_PAYBACK_MAX = 2.78


class AutoSpeedLimit:
  """Speed limit target without confirmation, for TeslaAutoSpeedLimitAssist.

  The Tesla set speed stays the ceiling: the planner takes the min of it and this target. Under
  it the target is the speed limit plus offset. A new value is taken only after it has held for
  `delay` seconds, so a 0.1 s Tesla flicker or a 1 s map flap changes nothing. A set speed change
  the driver scrolls in while engaged moves the target by the same amount (the nudge), and the
  nudge lasts until cruise is canceled.

  Inside a 구간단속 section (from korea mapd) the target is also capped at the section's pace: its
  exact limit, moved only by scrolls made inside it and only from then on. Time lost against that
  pace comes back above it and a lead taken over it is given back below it, so the section
  average lands on the pace.
  """

  def __init__(self):
    self.section = 0.
    self.section_start = 0.
    self.section_credit = 0.
    self._section_time = 0.
    self._section_dist = 0.
    self.reset(0.)

  def reset(self, limit: float) -> None:
    """Engagement: take the limit as it is, with no hold and no nudge. A 구간단속 section and its
    credit carry on (track_section keeps them), but a scroll made in it before the cancel is gone."""
    self.limit = limit
    self.nudge = 0.
    self.section_nudge = 0.
    self.changed = False
    self._pending = limit
    self._pending_frames = 0
    self._cluster_prev: float | None = None

  @property
  def v_target(self) -> float:
    targets = []
    if self.limit > 0.:
      targets.append(self.limit + self.nudge)
    if self.section > 0.:
      # a 구간단속 section: its pace, raised by time lost in it or lowered to give a lead back
      adjust = max(self.section_credit / SECTION_CREDIT_TAU, -SECTION_PAYBACK_MAX)
      targets.append(max(self._section_pace + adjust, min(self.section, NUDGE_MIN_SPEED)))
    return min(targets) if targets else V_CRUISE_UNSET

  @property
  def _section_pace(self) -> float:
    return max(self.section + self.section_nudge, min(self.section, NUDGE_MIN_SPEED))

  @property
  def section_average(self) -> float:
    """The section's average speed so far, as its cameras will time it; 0. outside a section."""
    return self._section_dist / self._section_time if self.section > 0. and self._section_time > 0. else 0.

  def track_section(self, section: float, start: float, v_ego: float, dt: float) -> None:
    """Every frame, engaged or not: the cameras time the whole section either way. A new limit or
    a new start (a section right after another of the same limit) starts from nothing."""
    if section != self.section or start != self.section_start:
      self.section, self.section_start = section, start
      self.section_nudge = self.section_credit = self._section_time = self._section_dist = 0.
    if self.section <= 0.:
      return
    # credit: how far the car is behind the pace it was asked to keep, in m
    self.section_credit += (self._section_pace - v_ego) * dt
    self._section_time += dt
    self._section_dist += v_ego * dt

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
    if prev is None or not scroll_allowed:
      return
    delta = v_cruise_cluster - prev
    if abs(delta) < SCROLL_EPSILON:
      return
    if self.section > 0.:
      # inside a section a scroll moves the section's pace from now on; the drive's nudge waits
      self.section_nudge += delta
      return
    if self.limit <= 0.:
      return
    if self.limit + self.nudge < prev:
      # the road target was holding the car under the set speed: move it with the scroll
      self.nudge += delta
    elif delta > 0.:
      # the set speed was the ceiling, so only the part of the scroll above the road target counts
      self.nudge = max(self.nudge, v_cruise_cluster - self.limit)
    self._clamp_nudge()

  def _clamp_nudge(self) -> None:
    self.nudge = max(self.nudge, min(self.limit, NUDGE_MIN_SPEED) - self.limit)
