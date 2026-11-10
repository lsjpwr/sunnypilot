"""Closed loop: does the cruise candidate's A_CRUISE_MIN cap the slowdowns SCC-Map and SLA ask for?

SCC-Map and SLA hand the planner a lower v_cruise and return a_ego as their a_target (map_controller.py,
speed_limit_assist.py get_a_target_from_control), so with cruise out of the MPC (commaai #38367) the
cruise candidate is what brakes. This drives the real LongitudinalPlanner with no lead and a car that
follows output_a_target with a 0.3 s lag, and replaces update_targets with the targets those controllers
produce. Run as a script to print the numbers for both A_CRUISE_MIN values.
"""
from unittest.mock import patch

from opendbc.car.tesla.interface import CarInterface
from opendbc.car.tesla.values import CAR
from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
import openpilot.selfdrive.controls.lib.longitudinal_planner as lp
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.map_controller import allowed_speed

KPH = 1 / 3.6
ACTUATOR_TAU = 0.3  # s, car's response to the accel command
ARRIVAL_MARGIN_KPH = 3.0


def run(a_cruise_min, start_kph, target_fn, distance_m):
  """target_fn(s_travelled) -> v_target (m/s). Returns (speed at distance_m in km/h, min accel, time)."""
  Params().put_bool("SmartCruiseControlVision", False, block=True)
  CP = CarInterface.get_non_essential_params(CAR.TESLA_MODEL_Y)
  CP_SP = CarInterface.get_non_essential_params_sp(CP, CAR.TESLA_MODEL_Y)
  CP.openpilotLongitudinalControl = True
  v = start_kph * KPH
  planner = lp.LongitudinalPlanner(CP, CP_SP, init_v=v)
  services = ("carControl", "carState", "carStateSP", "controlsState", "selfdriveState", "vehicleParameters",
              "modelV2", "radarState", "liveMapDataSP", "gpsLocation", "gpsLocationExternal")
  sm = {s: getattr(messaging.new_message(s), s) for s in services}
  sm["carControl"].enabled = True
  sm["carState"].vCruise = sm["carState"].vCruiseCluster = start_kph
  sm["controlsState"].longControlState = "pid"
  sm["selfdriveState"].enabled = True
  sm["selfdriveState"].personality = "standard"
  sm["modelV2"].action.desiredAcceleration = 3.5  # model wants nothing; the targets decide
  sm["modelV2"].meta.disengagePredictions.gasPressProbs = [1.0, 1.0]

  s, a, t, a_min = 0.0, 0.0, 0.0, 0.0
  state = {"s": 0.0}

  def update_targets(self, sm_, v_ego, a_ego, v_cruise):
    vt = min(v_cruise, target_fn(state["s"]))
    return vt, a_ego

  with patch.object(lp, "A_CRUISE_MIN", a_cruise_min), patch.object(LongitudinalPlannerSP, "update_targets", update_targets):
    while s < distance_m and t < 120:
      sm["carState"].vEgo = v
      sm["carState"].aEgo = a
      sm["modelV2"].velocity.x = [v] * 33
      planner.update(sm)
      a += (float(planner.output_a_target) - a) * DT_MDL / ACTUATOR_TAU
      a_min = min(a_min, a)
      v = max(v + a * DT_MDL, 0.0)
      s += v * DT_MDL
      state["s"] = s
      t += DT_MDL
  return v / KPH, a_min, t


def scc_map(start_kph=100, point_kph=60, decel=0.6):
  # the car starts where the ramp from point_kph at `decel` meets start_kph, plus 50 m of cruise
  vt, v0 = point_kph * KPH, start_kph * KPH
  d0 = (v0 ** 2 - vt ** 2) / (2 * decel) + vt * 1.0 + 50
  return (lambda s: allowed_speed(vt, decel, d0 - s)), d0


def sla_step(start_kph=100, limit_kph=80, lead_m=300):
  # SLA's v_target drops to the new limit lead_m before the sign
  return (lambda s: limit_kph * KPH), lead_m


def curve(start_kph=100, curve_kph=50, decel=1.2):
  return scc_map(start_kph, curve_kph, decel)


CASES = {
  "scc_map_100_60_decel0.6": (100, 60, scc_map(100, 60, 0.6), 0.6),
  "sla_100_80_in_300m": (100, 80, sla_step(100, 80, 300), 1.5),
  "curve_100_50_decel1.2": (100, 50, curve(100, 50, 1.2), 1.2),
}


class TestCruiseMinClosedLoop(OpenpilotTestCase):
  def test_scc_map_slowdown_arrives_at_its_speed(self):
    start, target, (fn, d), _ = CASES["scc_map_100_60_decel0.6"]
    arrive, _, _ = run(lp.A_CRUISE_MIN, start, fn, d)
    self.assertLessEqual(arrive, target + ARRIVAL_MARGIN_KPH)


if __name__ == "__main__":
  for name, (start, target, (fn, d), design) in CASES.items():
    for a_min in (-0.5, -1.2):
      arrive, peak, t = run(a_min, start, fn, d)
      print(f"{name:28s} A_CRUISE_MIN={a_min:+.1f}  arrive={arrive:5.1f} km/h (target {target}, over {arrive - target:+5.1f})"
            f"  peak decel={peak:+.2f} m/s^2 (design -{design})  over {d:.0f} m in {t:.1f} s")
