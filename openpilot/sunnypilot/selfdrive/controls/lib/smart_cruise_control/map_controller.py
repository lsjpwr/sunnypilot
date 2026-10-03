import json
import math
import platform

from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.mapd import MapSource
from openpilot.sunnypilot.mapd.korea.db import CAMERA_KIND_PARAMS
from openpilot.sunnypilot.navd.helpers import coordinate_from_param, Coordinate
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control import MIN_V

MapState = VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.MapState

ACTIVE_STATES = (MapState.turning, )
ENABLED_STATES = (MapState.enabled, MapState.overriding, *ACTIVE_STATES)

R = 6373000.0  # approximate radius of earth in meters
TO_RADIANS = math.pi / 180
TO_DEGREES = 180 / math.pi
TARGET_OFFSET = 1.0  # seconds - This controls how soon before the curve you reach the target velocity. It also helps
                     # reach the target velocity when inaccuracies in the distance modeling logic would cause overshoot.
                     # The value is multiplied against the target velocity to determine the additional distance. This is
                     # done to keep the distance calculations consistent but results in the offset actually being less
                     # time than specified depending on how much of a speed differential there is between v_ego and the
                     # target velocity.

# m/s^2. MapSlowdownDecel bounds. The target comes down along v = sqrt(v_point^2 + 2 * decel * d), so the
# car eases in early at this decel instead of braking late at the planner's 1.2 m/s^2 cruise cap, which
# every camera slowdown hit on 2026-10-03.
SLOWDOWN_DECEL_MIN = 0.3
SLOWDOWN_DECEL_MAX = 1.2


def velocities_from_param(param: str, params: Params):
  if params is None:
    params = Params()

  json_str = params.get(param)
  if json_str is None:
    return None

  velocities = json.loads(json_str)

  return velocities




# points should be in radians
# output is meters
def distance_to_point(ax, ay, bx, by):
  a = math.sin((bx-ax)/2)*math.sin((bx-ax)/2) + math.cos(ax) * math.cos(bx)*math.sin((by-ay)/2)*math.sin((by-ay)/2)
  c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

  return R * c  # in meters


class SmartCruiseControlMap:
  v_target: float = 0
  a_target: float = 0.
  v_ego: float = 0.
  a_ego: float = 0.
  output_v_target: float = V_CRUISE_UNSET
  output_a_target: float = 0.

  def __init__(self):
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.enabled = self._get_enabled()
    self.long_enabled = False
    self.long_override = False
    self.is_enabled = False
    self.is_active = False
    self.state = MapState.disabled
    self.v_cruise = 0
    self.target_lat = 0.0
    self.target_lon = 0.0
    self.frame = -1

    self.last_position = coordinate_from_param("LastGPSPosition", self.mem_params) or Coordinate(0.0, 0.0)
    self.target_velocities = velocities_from_param("MapTargetVelocities", self.mem_params) or []
    self.slowdown_decel = self._read_slowdown_decel()

  def get_v_target_from_control(self) -> float:
    if self.is_active:
      return max(self.v_target, MIN_V)

    return V_CRUISE_UNSET

  def get_a_target_from_control(self) -> float:
    return self.a_ego

  def _get_enabled(self) -> bool:
    """One set of toggles per source, because the two sources fill this controller differently.

    OSM: the native mapd binary streams curve target velocities, gated by
    SmartCruiseControlMap. Korea: korea_map_data writes the next speed bump, the next speed
    camera and the curves ahead, so any Korea toggle that feeds one of them enables this:
    KoreaSpeedBumpEnabled, the four camera kind toggles, or KoreaExternalNavEnabled -- curve
    targets need a fetched route, and KoreaExternalNavEnabled is what starts the route
    thread. An empty MapTargetVelocities list still leaves the state machine a no-op, so
    enabling on a toggle whose feature has nothing ahead costs nothing. Neither source's
    toggles may enable the other source -- the danger is a snapshot that stops advancing
    after a source switch, and each writer is only trusted to keep its own param fresh.
    """
    source = self.params.get("MapDataSource", return_default=True)
    if source == MapSource.osm:
      return self.params.get_bool("SmartCruiseControlMap")
    if source == MapSource.korea:
      keys = ("KoreaSpeedBumpEnabled", "KoreaExternalNavEnabled", *CAMERA_KIND_PARAMS.values())
      return any(self.params.get_bool(key) for key in keys)
    return False

  def _read_slowdown_decel(self) -> float:
    # argument order: a NaN param falls through to the max, the firm slowdown this replaced
    value = float(self.params.get("MapSlowdownDecel", return_default=True))
    return max(SLOWDOWN_DECEL_MIN, min(SLOWDOWN_DECEL_MAX, value))

  def update_params(self):
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.enabled = self._get_enabled()
      self.slowdown_decel = self._read_slowdown_decel()

  def update_calculations(self) -> None:
    self.last_position = coordinate_from_param("LastGPSPosition", self.mem_params) or Coordinate(0.0, 0.0)
    lat = self.last_position.latitude
    lon = self.last_position.longitude

    self.target_velocities = velocities_from_param("MapTargetVelocities", self.mem_params) or []

    if self.last_position is None or self.target_velocities is None:
      return

    min_dist = 1000
    min_idx = 0
    distances = []

    # find our location in the path
    for i in range(len(self.target_velocities)):
      target_velocity = self.target_velocities[i]
      tlat = target_velocity["latitude"]
      tlon = target_velocity["longitude"]
      d = distance_to_point(lat * TO_RADIANS, lon * TO_RADIANS, tlat * TO_RADIANS, tlon * TO_RADIANS)
      distances.append(d)
      if d < min_dist:
        min_dist = d
        min_idx = i

    # only look at values from our current position forward
    forward_points = self.target_velocities[min_idx:]
    forward_distances = distances[min_idx:]

    # The speed allowed here so that a steady slowdown_decel still reaches each point's speed
    # TARGET_OFFSET seconds before the point; the lowest of them is the target. A point above the
    # current speed counts too: it keeps the car from speeding up past what it can shed in time.
    min_v = 100.0
    target_lat = 0.0
    target_lon = 0.0
    for target_velocity, d in zip(forward_points, forward_distances, strict=True):
      tv = target_velocity["velocity"]
      v_allowed = math.sqrt(tv ** 2 + 2. * self.slowdown_decel * max(d - tv * TARGET_OFFSET, 0.))
      if v_allowed < min_v:
        min_v = v_allowed
        target_lat, target_lon = target_velocity["latitude"], target_velocity["longitude"]

    self.v_target = min_v
    self.target_lat = target_lat
    self.target_lon = target_lon

  def _update_state_machine(self) -> tuple[bool, bool]:
    # ENABLED, TURNING
    if self.state != MapState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = MapState.disabled
      elif self.long_override:
        self.state = MapState.overriding

      else:
        # ENABLED
        if self.state == MapState.enabled:
          if self.v_cruise > self.v_target != 0:
            self.state = MapState.turning

        # TURNING
        elif self.state == MapState.turning:
          if self.v_cruise <= self.v_target or self.v_target == 0:
            self.state = MapState.enabled

        # OVERRIDING
        elif self.state == MapState.overriding:
          if not self.long_override:
            if self.v_cruise > self.v_target != 0:
              self.state = MapState.turning
            else:
              self.state = MapState.enabled

    # DISABLED
    elif self.state == MapState.disabled:
      if self.long_enabled and self.enabled:
        if self.long_override:
          self.state = MapState.overriding
        else:
          self.state = MapState.enabled

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES

    return enabled, active

  def update(self, long_enabled: bool, long_override: bool, v_ego, a_ego, v_cruise) -> None:
    self.long_enabled = long_enabled
    self.long_override = long_override
    self.v_ego = v_ego
    self.a_ego = a_ego
    self.v_cruise = v_cruise

    self.update_params()
    self.update_calculations()

    self.is_enabled, self.is_active = self._update_state_machine()

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
