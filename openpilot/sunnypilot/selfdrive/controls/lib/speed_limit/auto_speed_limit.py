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


def _scrolled(nudge: float, base: float, prev: float, v_cruise_cluster: float) -> float:
  """The nudge of a target base + nudge after the set speed moved from prev to v_cruise_cluster. A
  target that was holding the car under the set speed moves with the scroll; when the set speed was
  the ceiling, only the part of a scroll up above the target counts. Never under NUDGE_MIN_SPEED, nor
  under base when that is lower."""
  if base + nudge < prev:
    nudge += v_cruise_cluster - prev
  elif v_cruise_cluster > prev:
    nudge = max(nudge, v_cruise_cluster - base)
  return max(nudge, min(base, NUDGE_MIN_SPEED) - base)


class AutoSpeedLimit:
  """Speed limit target without confirmation, for TeslaAutoSpeedLimitAssist.

  The Tesla set speed stays the ceiling: the planner takes the min of it and this target. Under
  it the target is the speed limit plus offset. A new value is taken only after it has held for
  `delay` seconds, so a 0.1 s Tesla flicker or a 1 s map flap changes nothing. A set speed change
  the driver scrolls in while engaged moves the target by the same amount (the nudge), and the
  nudge lasts until cruise is canceled.

  Inside a 구간단속 section (from korea mapd) the target is also capped at the section's pace: its
  exact limit, moved only by scrolls made inside it and only from then on. Those scrolls move the
  pace and the road target, each by the same rules as the drive's nudge, and end with the section or
  a cancel, but for the part of a scroll up that undid a scroll down made before the section: that
  part stays. Time lost against that pace comes back above it and a lead taken over it is given back
  below it, so the section average lands on the pace.
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
    self.drop_section_scrolls()
    self.changed = False
    self._pending = limit
    self._pending_frames = 0
    self._cluster_prev: float | None = None

  def drop_section_scrolls(self) -> None:
    """A cancel or a new section: scrolls made in the section are gone."""
    self.section_nudge = self.section_road_nudge = 0.

  @property
  def v_target(self) -> float:
    targets = []
    if self.limit > 0.:
      # the road target, plus what scrolls made inside a section moved it by
      targets.append(self.limit + self.nudge + self.section_road_nudge)
    if self.section > 0.:
      # a 구간단속 section: its pace, raised by time lost in it or lowered to give a lead back
      adjust = max(self.section_credit / SECTION_CREDIT_TAU, -SECTION_PAYBACK_MAX)
      targets.append(max(self._section_pace + adjust, min(self.section, NUDGE_MIN_SPEED)))
    return min(targets) if targets else V_CRUISE_UNSET

  @property
  def _section_pace(self) -> float:
    return self.section + self.section_nudge

  @property
  def section_average(self) -> float:
    """The section's average speed so far, as its cameras will time it; 0. outside a section."""
    return self._section_dist / self._section_time if self.section > 0. and self._section_time > 0. else 0.

  def track_section(self, section: float, start: float, v_ego: float, dt: float) -> None:
    """Every frame, engaged or not: the cameras time the whole section either way. A new limit or
    a new start (a section right after another of the same limit) starts from nothing."""
    if section != self.section or start != self.section_start:
      self.section, self.section_start = section, start
      # a scroll up made in the section stays only as far as it undid a scroll down from before it
      self.nudge += min(max(self.section_road_nudge, 0.), max(-self.nudge, 0.))
      self.drop_section_scrolls()
      self.section_credit = self._section_time = self._section_dist = 0.
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
    if abs(v_cruise_cluster - prev) < SCROLL_EPSILON:
      return
    if self.section > 0.:
      # inside a section a scroll moves the section's pace and the road target from now on, each
      # measured from itself; the drive's nudge waits
      self.section_nudge = _scrolled(self.section_nudge, self.section, prev, v_cruise_cluster)
      if self.limit > 0.:
        self.section_road_nudge = _scrolled(self.nudge + self.section_road_nudge, self.limit, prev, v_cruise_cluster) - self.nudge
    elif self.limit > 0.:
      self.nudge = _scrolled(self.nudge, self.limit, prev, v_cruise_cluster)

  def _clamp_nudge(self) -> None:
    # a new limit keeps the road target at the floor or above, so a scroll up moves it at once: the
    # drive's nudge on its own and with the section's scrolls. With no limit there is no road target,
    # so the clamp waits for the next limit.
    if self.limit <= 0.:
      return
    floor = min(self.limit, NUDGE_MIN_SPEED) - self.limit
    road = max(self.nudge + self.section_road_nudge, floor)
    self.nudge = max(self.nudge, floor)
    self.section_road_nudge = road - self.nudge
