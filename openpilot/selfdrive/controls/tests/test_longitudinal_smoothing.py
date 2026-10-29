import unittest

from opendbc.car.tesla.interface import CarInterface
from opendbc.car.tesla.values import CAR
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import DT_CTRL, DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.selfdrive.controls.lib.longcontrol import LongControl, LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner


class TestLongitudinalSmoothing(OpenpilotTestCase):
  def _planner_and_sm(self, speed_kph, vision_enabled, desired_acceleration):
    params = Params()
    params.put_bool("SmartCruiseControlVision", vision_enabled, block=True)
    params.put("DynamicExperimentalControl", 0, block=True)

    CP = CarInterface.get_non_essential_params(CAR.TESLA_MODEL_Y)
    CP_SP = CarInterface.get_non_essential_params_sp(CP, CAR.TESLA_MODEL_Y)
    CP.openpilotLongitudinalControl = True
    v_ego = speed_kph / 3.6
    planner = LongitudinalPlanner(CP, CP_SP, init_v=v_ego)

    services = ("carControl", "carState", "carStateSP", "controlsState", "selfdriveState", "vehicleParameters",
                "modelV2", "radarState", "liveMapDataSP", "gpsLocation", "gpsLocationExternal")
    sm = {service: getattr(messaging.new_message(service), service) for service in services}
    sm["carControl"].enabled = True
    sm["carState"].vEgo = v_ego
    sm["carState"].vCruise = sm["carState"].vCruiseCluster = 100.0
    sm["carStateSP"].speedLimit = 100.0 / 3.6
    sm["controlsState"].longControlState = "pid"
    sm["selfdriveState"].enabled = True
    sm["selfdriveState"].experimentalMode = False
    sm["selfdriveState"].personality = "standard"
    sm["radarState"].leadOne.present = False
    sm["radarState"].leadTwo.present = False
    sm["modelV2"].velocity.x = [v_ego] * 33
    sm["modelV2"].orientationRate.z = [1.0 / v_ego] * 33
    sm["modelV2"].action.desiredAcceleration = desired_acceleration
    sm["modelV2"].meta.disengagePredictions.gasPressProbs = [1.0, 1.0]
    return planner, sm

  def test_vision_turn_engagement_brakes_gradually_without_a_lead(self):
    planner, sm = self._planner_and_sm(60.0, True, 0.0)
    for _ in range(3):
      planner.update(sm)
    targets = [float(planner.output_a_target)]
    self.assertGreater(targets[0], 0.0)
    self.assertFalse(planner.output_should_stop)

    sm["modelV2"].orientationRate.z = [2.5 / sm["carState"].vEgo] * 33
    for _ in range(20):
      planner.update(sm)
      targets.append(float(planner.output_a_target))
      self.assertFalse(planner.output_should_stop)

    self.assertLess(targets[-1], -0.1, "Vision turn control must still brake within one second")
    # Cruise allows about 1.02 m/s^3 at 60 km/h; 1.05 leaves a small numerical margin.
    for cycle, (previous, current) in enumerate(zip(targets, targets[1:], strict=False), start=1):
      self.assertLessEqual(abs(current - previous), 1.05 * DT_MDL,
                           f"Vision-only acceleration jumped {previous:.6f} -> {current:.6f} m/s^2 on cycle {cycle}")

  def test_e2e_exit_restores_cruise_acceleration_gradually(self):
    planner, sm = self._planner_and_sm(90.0, False, -0.8)
    for _ in range(3):
      planner.update(sm)
    self.assertGreater(planner.output_a_target, 0.0)

    sm["selfdriveState"].experimentalMode = True
    planner.update(sm)
    e2e_target = float(planner.output_a_target)
    self.assertAlmostEqual(e2e_target, -0.8, places=6, msg="E2E braking must apply immediately")
    self.assertFalse(planner.output_should_stop)

    sm["selfdriveState"].experimentalMode = False
    targets = [e2e_target]
    for _ in range(int(2.0 / DT_MDL)):
      planner.update(sm)
      targets.append(float(planner.output_a_target))
      self.assertFalse(planner.output_should_stop)
    # Cruise allows 0.8 m/s^3 at 90 km/h; 0.82 leaves a small numerical margin.
    for cycle, (previous, current) in enumerate(zip(targets, targets[1:], strict=False), start=1):
      self.assertLessEqual(abs(current - previous), 0.82 * DT_MDL,
                           f"E2E exit acceleration jumped {previous:.6f} -> {current:.6f} m/s^2 on cycle {cycle}")
    self.assertLess(targets[1], 0.0, "E2E exit must retain braking while cruise acceleration recovers")
    self.assertGreater(targets[-1], 0.0, "Cruise acceleration must recover within two seconds")

  def test_stronger_e2e_braking_applies_immediately_during_recovery(self):
    planner, sm = self._planner_and_sm(90.0, False, -0.8)
    for _ in range(3):
      planner.update(sm)
    sm["selfdriveState"].experimentalMode = True
    planner.update(sm)
    self.assertAlmostEqual(planner.output_a_target, -0.8, places=6)

    sm["selfdriveState"].experimentalMode = False
    for _ in range(3):
      planner.update(sm)
    self.assertGreater(planner.output_a_target, -0.8)
    self.assertLess(planner.output_a_target, 0.0)
    self.assertFalse(planner.output_should_stop)

    sm["selfdriveState"].experimentalMode = True
    sm["modelV2"].action.desiredAcceleration = -2.5
    planner.update(sm)
    self.assertAlmostEqual(planner.output_a_target, -2.5, places=6, msg="Stronger E2E braking must bypass comfort smoothing")
    self.assertFalse(planner.output_should_stop)

  def test_lead_slowdown_bypasses_vision_comfort_smoothing(self):
    for lead_name in ("leadOne", "leadTwo"):
      with self.subTest(lead=lead_name):
        planner, sm = self._planner_and_sm(60.0, True, 0.0)
        for _ in range(3):
          planner.update(sm)
        sm["modelV2"].orientationRate.z = [2.5 / sm["carState"].vEgo] * 33
        planner.update(sm)
        previous = float(planner.output_a_target)
        self.assertGreater(previous, 0.0)

        lead = getattr(sm["radarState"], lead_name)
        lead.present = True
        lead.dRel = 20.0
        lead.vLead = 0.0
        lead.aLeadK = -2.0
        lead.aLeadTau = 1.5
        lead.modelProb = 1.0
        planner.update(sm)
        self.assertLess(planner.output_a_target, previous - 0.1, "Lead braking must exceed the comfort step on the same tick")
        self.assertLess(planner.output_a_target, 0.0)

  def test_low_speed_e2e_exit_releases_stop_and_returns_to_pid(self):
    planner, sm = self._planner_and_sm(0.36, False, -0.8)
    CP_SP = CarInterface.get_non_essential_params_sp(planner.CP, CAR.TESLA_MODEL_Y)
    controller = LongControl(planner.CP, CP_SP)
    for _ in range(3):
      planner.update(sm)
    self.assertFalse(planner.output_should_stop)
    sm["selfdriveState"].experimentalMode = True
    sm["modelV2"].action.shouldStop = True
    planner.update(sm)
    self.assertTrue(planner.output_should_stop, "The model stop request must propagate on the same tick")
    controller.update(True, sm["carState"], planner.output_a_target, planner.output_should_stop, (ACCEL_MIN, ACCEL_MAX))
    self.assertEqual(controller.long_control_state, LongCtrlState.stopping)

    sm["selfdriveState"].experimentalMode = False
    for cycle in range(round(1.0 / DT_MDL)):
      planner.update(sm)
      for _ in range(round(DT_MDL / DT_CTRL)):
        controller.update(True, sm["carState"], planner.output_a_target, planner.output_should_stop, (ACCEL_MIN, ACCEL_MAX))
      sm["controlsState"].longControlState = controller.long_control_state
      if (cycle + 1) * DT_MDL >= 0.6:
        self.assertFalse(planner.output_should_stop, "Low-speed stop must release within 0.6 seconds of E2E exit")
    self.assertEqual(controller.long_control_state, LongCtrlState.pid, "LongControl must return to PID within one second")

  def test_reset_discards_negative_acceleration_history(self):
    for reset in ("off", "unset_cruise"):
      with self.subTest(reset=reset):
        planner, sm = self._planner_and_sm(0.36, False, -0.8)
        sm["selfdriveState"].experimentalMode = True
        planner.update(sm)
        self.assertLess(planner.output_a_target, 0.0)
        self.assertTrue(planner.output_should_stop)

        sm["selfdriveState"].experimentalMode = False
        sm["carState"].aEgo = 0.2
        if reset == "off":
          sm["controlsState"].longControlState = "off"
        else:
          sm["carState"].vCruise = V_CRUISE_UNSET
        planner.update(sm)
        self.assertGreater(planner.output_a_target, 0.0, "Reset must use measured acceleration instead of stale braking")
        self.assertFalse(planner.output_should_stop)


if __name__ == "__main__":
  unittest.main()
