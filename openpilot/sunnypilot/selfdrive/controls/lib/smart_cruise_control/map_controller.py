import json
import math
import platform

from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.mapd import MapSource
from openpilot.sunnypilot.mapd.korea.db import CAMERA_KIND_PARAMS, CAMERA_MAX_DISTANCE_M
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
SLOWDOWN_DECEL_MIN = 0.5
SLOWDOWN_DECEL_MAX = 1.2
# m/s^2. A camera point says when its slowdown starts (start_s: seconds before the camera at the car's speed);
# the decel is then what takes that speed down to the camera's limit from there, never firmer than
# MapSlowdownDecel and never softer than this.
RAMP_DECEL_MIN = 0.05


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


def allowed_speed(tv: float, decel: float, d: float) -> float:
  """The speed allowed d metres before a point so that a steady decel still reaches tv TARGET_OFFSET seconds early."""
  return math.sqrt(tv ** 2 + 2. * decel * max(d - tv * TARGET_OFFSET, 0.))


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
    # the camera point being slowed for (its lat, lon) and the speed its slowdown is worked out from: the car's
    # own (or the speed whose start is here, if higher) until the slowdown starts, then held (_camera_allowed)
    self._camera_key: tuple[float, float] | None = None
    self._camera_v = 0.
    self._camera_started = False

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

    # The speed allowed here so that a steady decel still reaches each point's speed TARGET_OFFSET seconds
    # before the point; the lowest of them is the target. Bumps and curves slow at slowdown_decel, a camera at
    # what its start works out to (_camera_allowed). A point above the current speed counts too: it keeps the
    # car from speeding up past what it can shed in time.
    min_v = 100.0
    target_lat = 0.0
    target_lon = 0.0
    for target_velocity, d in zip(forward_points, forward_distances, strict=True):
      tv = target_velocity["velocity"]
      if tv <= 0.:
        continue  # no speed to slow to: the old state machine ignored a 0 target too
      if "start_s" in target_velocity:
        v_allowed = self._camera_allowed(target_velocity, tv, d)
      else:
        v_allowed = allowed_speed(tv, self.slowdown_decel, d)
      if v_allowed < min_v:
        min_v = v_allowed
        target_lat, target_lon = target_velocity["latitude"], target_velocity["longitude"]

    self.v_target = min_v
    self.target_lat = target_lat
    self.target_lon = target_lon

  def _camera_allowed(self, point: dict, tv: float, d: float) -> float:
    """A camera's slowdown starts start_s seconds before it at the car's speed (1 km at 100 km/h by default,
    capped where cameras come into view) and is as gentle as reaching tv by the point allows: a little too
    fast eases off over the whole stretch, far too fast starts earlier at MapSlowdownDecel.

    The speed is the car's until the slowdown starts, then held for this camera. Worked out afresh from a
    speed the slowdown itself lowers, the start would keep moving in with the car and the braking would bunch
    up at the camera. Not the set speed: under auto SLA the car often runs well under it, and a start worked
    out from the set speed came late (615 m instead of 1150 m at 115 under a 125 set speed).

    Before the start it is never under the speed whose start is where the car is: slowed by traffic or a red
    light short of the start, the car may pick up to that speed, as one arriving at it would. Worked out from
    the lower speed, the slowdown came out at its gentlest and held the car a few km/h over the camera's
    limit all the way in (39 km/h 650 m before a 30 camera after a red light, 2026-10-09)."""
    margin = point.get("margin_m", 0.)
    if point["start_s"] <= 0.:
      return allowed_speed(tv, self.slowdown_decel, d)  # no start set: the strength alone, as before 2026-10-07
    key = (point["latitude"], point["longitude"])
    if key != self._camera_key:
      self._camera_key, self._camera_started = key, False
    if not self._camera_started:
      self._camera_v = max(self.v_ego, (d + margin) / point["start_s"])
    start = min(point["start_s"] * self._camera_v, CAMERA_MAX_DISTANCE_M)
    ramp = max(start - margin, 0.)
    needed = (self._camera_v ** 2 - tv ** 2) / (2. * max(ramp - tv * TARGET_OFFSET, 1.))
    v_allowed = allowed_speed(tv, max(RAMP_DECEL_MIN, min(needed, self.slowdown_decel)), d)
    if v_allowed < self.v_ego:
      self._camera_started = True  # the slowdown has begun: it holds the car under its own speed
    return v_allowed

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
