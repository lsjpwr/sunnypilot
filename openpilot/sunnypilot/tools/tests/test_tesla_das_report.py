"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.cereal import messaging
from opendbc.can import CANPacker
from opendbc.car import Bus
from opendbc.car.tesla.values import CANBUS, CAR, DBC
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.radard import RADAR_TO_CAMERA
from openpilot.sunnypilot.tools.tesla_das_report import analyze

PACKER = CANPacker(DBC[CAR.TESLA_MODEL_Y][Bus.adas])


def can_msg(t, d_rel):
  addr, dat, bus = PACKER.make_can_msg("DAS_object", CANBUS.vehicle, {
    "DAS_objectId": 0, "DAS_leadVehRelevantForControl": 1, "DAS_leadVehDx": d_rel, "DAS_leadVehDy": 0.0,
    "DAS_leadVehVxRel": 2.0, "DAS_leadVehId": 3,
  })
  msg = messaging.new_message('can', 1)
  msg.logMonoTime = int(t * 1e9)
  msg.can[0].address, msg.can[0].dat, msg.can[0].src = addr, dat, bus
  return msg.as_reader()


def car_state_msg(t, v_ego):
  msg = messaging.new_message('carState')
  msg.logMonoTime = int(t * 1e9)
  msg.carState.vEgo = v_ego
  return msg.as_reader()


def model_msg(t, prob, d_rel):
  msg = messaging.new_message('modelV2')
  msg.logMonoTime = int(t * 1e9)
  for lead, p in zip(msg.modelV2.init('leadsV3', 3), (prob, 0.0, 0.0), strict=True):
    lead.prob = p
    lead.x = [d_rel + RADAR_TO_CAMERA] * 6
    lead.y = [0.0] * 6
  return msg.as_reader()


def synthetic_route():
  """14 s: a lead both see (0-10 s), no lead (10-12 s), then a new lead DAS reports 0.5 s before the model is sure."""
  msgs = []
  for k in range(1400):  # CAN at 100 Hz
    t = k * 0.01
    msgs.append(can_msg(t, 40.0 if k < 1000 else 127.5 if k < 1200 else 30.0))  # 127.5 m is SNA: no DAS lead
    if k % 5 == 0:  # carState and model at 20 Hz
      msgs.append(car_state_msg(t, 20.0))
      if k < 1000:
        msgs.append(model_msg(t, 0.95, 40.0))
      elif k < 1200:
        msgs.append(model_msg(t, 0.0, 80.0))
      else:
        msgs.append(model_msg(t, 0.3 if k < 1250 else 0.7, 30.0))
  return msgs


class TestTeslaDasReport(OpenpilotTestCase):
  def test_synthetic_route(self):
    r = analyze(synthetic_route())
    assert r["can_msgs"] == 1400
    assert r["bus_counts"] == {CANBUS.vehicle: 1400}
    assert r["mux_counts"] == {0: 1400}
    self.assertAlmostEqual(r["slots"][0]["dx_sna"], 200 / 1400)
    self.assertAlmostEqual(r["slots"][0]["relevant"], 1.0)
    assert r["slots"][3]["frames"] == 0
    assert max(abs(e) for e in r["distance_errors"]) < 0.01
    self.assertAlmostEqual(r["trusted_at"], 5.0, delta=0.1)  # 100 agreeing frames at 20 Hz
    # the first DAS point of the new lead is published at 12.04 s and used by the 12.05 s model frame;
    # the model's own filtered prob crosses 0.5 at 12.5 s
    assert len(r["advantages_ms"]) == 1
    self.assertAlmostEqual(r["advantages_ms"][0], 450, delta=1)
    assert r["phantoms"] == 0
    assert r["extensions"] == 0
