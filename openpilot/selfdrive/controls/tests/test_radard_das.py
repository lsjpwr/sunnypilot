from openpilot.cereal import custom, messaging
from opendbc.car.structs import car
from opendbc.car.tesla.values import DAS_CUTIN_TRACK_ID_BASE, TeslaFlags
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.radard import DAS_CONFIRMED_PROB, RADAR_TO_CAMERA, RadarD
from openpilot.sunnypilot.selfdrive.controls.lib.das_lead import MIN_EVIDENCE, DasLeadConfirmer

DAS_CP = car.CarParams.new_message(brand="tesla", flags=int(TeslaFlags.HW4_GEN2 | TeslaFlags.HW4_GEN2_VEHICLE_BUS))


class FakeSM:
  """The part of SubMaster that RadarD.update reads."""

  def __init__(self):
    self.data = {}
    self.seen = {'modelV2': True}
    self.recv_frame = {'carState': 0}
    self.logMonoTime = {'modelV2': 0}

  def __getitem__(self, key):
    return self.data[key]

  def all_checks(self):
    return True

  def step(self, v_ego, probs, x=40.0, y=0.0):
    """One model frame. probs are leadsV3 slots 0 and 1, both at (x, y) in the model frame."""
    self.recv_frame['carState'] += 1
    self.data['carState'] = car.CarState.new_message(vEgo=v_ego)
    model = messaging.new_message('modelV2').modelV2
    model.velocity.x = [v_ego]
    for lead, prob in zip(model.init('leadsV3', 3), (*probs, 0.0), strict=True):
      lead.prob = prob
      lead.x, lead.y, lead.v, lead.a = [x] * 6, [y] * 6, [v_ego] * 6, [0.0] * 6
      lead.xStd, lead.yStd, lead.vStd, lead.aStd = [1.0] * 6, [1.0] * 6, [1.0] * 6, [1.0] * 6
    self.data['modelV2'] = model


def radar_data(*points):
  """points are (trackId, dRel, yRel)."""
  rr = car.RadarData.new_message()
  for pt, (track_id, d_rel, y_rel) in zip(rr.init('points', len(points)), points, strict=True):
    pt.trackId, pt.dRel, pt.yRel, pt.vRel = track_id, d_rel, y_rel, 0.0
  return rr


class StubConfirmer:
  def __init__(self, confirmed):
    self.confirmed = confirmed
    self.calls = []

  def update(self, v_ego, das_lead, das_cutin, candidates):
    self.calls.append((v_ego, das_lead, das_cutin, candidates))
    return self.confirmed


class AlwaysOn:
  def get(self, key, return_default=False):
    return True


def das_radard(confirmer):
  rd = RadarD(DAS_CP, custom.CarParamsSP.new_message())
  rd.das_confirmer = confirmer
  return rd


class TestRadardDasMode(OpenpilotTestCase):
  def test_das_points_never_become_tracks(self):
    rd, sm = das_radard(StubConfirmer([False, False])), FakeSM()
    sm.step(1.0, (0.0, 0.0))  # below V_EGO_STATIONARY, where a radar track alone would make a lead
    rd.update(sm, radar_data((5, 10.0, 0.0)))
    assert rd.tracks == {}
    assert not rd.radar_state.leadOne.present

  def test_confirmer_gets_split_points_and_candidates_in_radar_frame(self):
    stub = StubConfirmer([False, False])
    rd, sm = das_radard(stub), FakeSM()
    sm.step(20.0, (0.3, 0.1), x=40.0, y=0.5)
    rd.update(sm, radar_data((5, 38.0, -0.5), (DAS_CUTIN_TRACK_ID_BASE + 7, 15.0, 2.0)))
    v_ego, das_lead, das_cutin, candidates = stub.calls[-1]
    assert v_ego == 20.0
    assert das_lead == (38.0, -0.5)
    assert das_cutin == (15.0, 2.0)
    self.assertAlmostEqual(candidates[0][0], 40.0 - RADAR_TO_CAMERA, places=4)
    assert candidates[0][1] == -0.5
    self.assertAlmostEqual(candidates[0][2], 0.3, places=6)
    self.assertAlmostEqual(candidates[1][2], 0.1, places=6)

  def test_confirmed_low_prob_lead_is_present_with_model_values(self):
    rd, sm = das_radard(StubConfirmer([True, False])), FakeSM()
    sm.step(20.0, (0.3, 0.3), x=40.0)
    rd.update(sm, radar_data())
    lead_one, lead_two = rd.radar_state.leadOne, rd.radar_state.leadTwo
    assert lead_one.present and not lead_one.radar
    self.assertAlmostEqual(lead_one.modelProb, DAS_CONFIRMED_PROB, places=6)
    self.assertAlmostEqual(lead_one.dRel, 40.0 - RADAR_TO_CAMERA, places=4)
    self.assertAlmostEqual(lead_one.vLead, 20.0, places=4)
    assert not lead_two.present

  def test_confirmation_never_lowers_a_prob_or_touches_the_filter(self):
    rd, sm = das_radard(StubConfirmer([True, True])), FakeSM()
    sm.step(20.0, (0.8, 0.3))
    rd.update(sm, radar_data())
    self.assertAlmostEqual(rd.radar_state.leadOne.modelProb, 0.8, places=6)
    self.assertAlmostEqual(rd.lead_prob_filters[1].x, 0.3, places=6)

  def test_unconfirmed_low_prob_lead_stays_absent(self):
    rd, sm = das_radard(StubConfirmer([False, False])), FakeSM()
    sm.step(20.0, (0.3, 0.3))
    rd.update(sm, radar_data())
    assert not rd.radar_state.leadOne.present

  def test_real_confirmer_earns_trust_then_confirms_a_new_lead(self):
    rd, sm = das_radard(DasLeadConfirmer(params=AlwaysOn())), FakeSM()
    for _ in range(MIN_EVIDENCE):  # a confident model lead that DAS agrees with
      sm.step(20.0, (0.95, 0.0), x=40.0 + RADAR_TO_CAMERA)
      rd.update(sm, radar_data((5, 40.0, 0.0)))
    for _ in range(20):  # the lead leaves: the filtered prob decays well below the gate
      sm.step(20.0, (0.0, 0.0))
      rd.update(sm, radar_data())
    assert not rd.radar_state.leadOne.present

    sm.step(20.0, (0.3, 0.0), x=30.0 + RADAR_TO_CAMERA)  # a new, low-confidence lead that DAS also sees
    rd.update(sm, radar_data((9, 30.0, 0.0)))
    assert rd.radar_state.leadOne.present
    self.assertAlmostEqual(rd.radar_state.leadOne.modelProb, DAS_CONFIRMED_PROB, places=6)


class TestRadardOtherCars(OpenpilotTestCase):
  def test_other_cars_get_no_confirmer_and_keep_their_tracks(self):
    for CP in (car.CarParams.new_message(brand="toyota"),
               car.CarParams.new_message(brand="tesla", flags=int(TeslaFlags.HW4_GEN2)),
               car.CarParams.new_message(brand="hyundai", flags=int(TeslaFlags.HW4_GEN2_VEHICLE_BUS))):
      with self.subTest(brand=CP.brand, flags=CP.flags):
        rd, sm = RadarD(CP, custom.CarParamsSP.new_message()), FakeSM()
        assert rd.das_confirmer is None
        sm.step(20.0, (0.0, 0.0))
        rd.update(sm, radar_data((5, 30.0, 0.0)))
        assert list(rd.tracks) == [5]
