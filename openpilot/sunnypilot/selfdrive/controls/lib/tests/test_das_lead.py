"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.selfdrive.controls.lib.das_lead import (CONFIRM_HOLD_FRAMES, MIN_CANDIDATE_PROB, MIN_EVIDENCE,
                                                                  DasLeadConfirmer)

TICKS_PER_READ = int(1. / DT_MDL)
LEAD = (40.0, 0.0)             # DAS lead point, radar frame
CONFIDENT = (40.5, 0.3, 0.95)  # model slot 0 candidate that matches LEAD
FAR = (80.0, 0.0, 0.0)         # model slot 1 candidate with nothing there
WRONG = (60.0, 0.0)            # DAS point 19.5 m off CONFIDENT


class FakeParams:
  def __init__(self, enabled=True):
    self.enabled = enabled

  def get(self, key, return_default=False):
    assert key == "TeslaDasLeadConfirm", f"unexpected param read: {key}"
    return self.enabled


def trusted_confirmer(params=None):
  confirmer = DasLeadConfirmer(params or FakeParams())
  for _ in range(MIN_EVIDENCE):
    confirmer.update(20.0, LEAD, None, [CONFIDENT, FAR])
  assert confirmer.trusted
  # the frame that earned trust also confirmed CONFIDENT; fade both candidates so no hold carries into the test
  confirmer.update(20.0, None, None, [FAR, FAR])
  return confirmer


class TestSelfCheck(OpenpilotTestCase):
  def test_trust_needs_min_evidence_of_agreement(self):
    confirmer = DasLeadConfirmer(FakeParams())
    for _ in range(MIN_EVIDENCE - 1):
      confirmer.update(20.0, LEAD, None, [CONFIDENT, FAR])
    assert not confirmer.trusted
    confirmer.update(20.0, LEAD, None, [CONFIDENT, FAR])
    assert confirmer.trusted

  def test_das_in_the_wrong_place_never_earns_trust(self):
    confirmer = DasLeadConfirmer(FakeParams())
    for _ in range(3 * MIN_EVIDENCE):
      confirmer.update(20.0, WRONG, None, [CONFIDENT, FAR])
    assert not confirmer.trusted

  def test_only_fast_confident_frames_with_a_das_lead_count(self):
    confirmer = DasLeadConfirmer(FakeParams())
    for _ in range(MIN_EVIDENCE):
      confirmer.update(3.0, WRONG, None, [CONFIDENT, FAR])                # too slow
      confirmer.update(20.0, WRONG, None, [(40.5, 0.3, 0.8), FAR])        # model not confident
      confirmer.update(20.0, None, WRONG, [CONFIDENT, FAR])               # no DAS lead, only a cut-in
    # none of those counted as disagreement, so agreeing frames alone earn trust
    for _ in range(MIN_EVIDENCE):
      confirmer.update(20.0, LEAD, None, [CONFIDENT, FAR])
    assert confirmer.trusted

  def test_trust_is_withdrawn_below_sixty_percent(self):
    confirmer = trusted_confirmer()
    # 100 agreeing frames: 66 disagreeing ones leave 100/166 >= 0.6, the 67th takes it to 100/167 < 0.6
    for _ in range(66):
      confirmer.update(20.0, WRONG, None, [CONFIDENT, FAR])
    assert confirmer.trusted
    confirmer.update(20.0, WRONG, None, [CONFIDENT, FAR])
    assert not confirmer.trusted


class TestConfirmation(OpenpilotTestCase):
  def test_low_prob_candidate_where_das_sees_a_car_is_confirmed(self):
    confirmer = trusted_confirmer()
    assert confirmer.update(20.0, LEAD, None, [(40.5, 0.3, 0.3), FAR]) == [True, False]

  def test_cutin_point_confirms_too(self):
    confirmer = trusted_confirmer()
    assert confirmer.update(20.0, None, (15.0, 2.0), [FAR, (16.0, 2.5, 0.3)]) == [False, True]

  def test_candidate_below_min_prob_is_not_confirmed(self):
    confirmer = trusted_confirmer()
    assert confirmer.update(20.0, LEAD, None, [(40.5, 0.3, MIN_CANDIDATE_PROB - 0.01), FAR]) == [False, False]

  def test_match_gates(self):
    # candidate at 40 m: distance tolerance max(0.25 * 40, 5) = 10 m, lateral 1.5 m
    for point, expected in (((49.9, 0.0), True), ((50.1, 0.0), False), ((40.0, 1.4), True), ((40.0, 1.6), False)):
      with self.subTest(point=point):
        confirmer = trusted_confirmer()
        assert confirmer.update(20.0, point, None, [(40.0, 0.0, 0.3), FAR])[0] == expected

  def test_confirmation_holds_ten_frames_after_the_last_match(self):
    confirmer = trusted_confirmer()
    candidate = (40.5, 0.3, 0.3)
    assert confirmer.update(20.0, LEAD, None, [candidate, FAR])[0]
    for _ in range(CONFIRM_HOLD_FRAMES):
      assert confirmer.update(20.0, None, None, [candidate, FAR])[0]
    assert not confirmer.update(20.0, None, None, [candidate, FAR])[0]

  def test_hold_ends_when_the_candidate_fades(self):
    confirmer = trusted_confirmer()
    assert confirmer.update(20.0, LEAD, None, [(40.5, 0.3, 0.3), FAR])[0]
    assert not confirmer.update(20.0, None, None, [(40.5, 0.3, MIN_CANDIDATE_PROB - 0.01), FAR])[0]

  def test_untrusted_confirmer_confirms_nothing(self):
    confirmer = DasLeadConfirmer(FakeParams())
    assert confirmer.update(20.0, LEAD, None, [(40.5, 0.3, 0.3), FAR]) == [False, False]


class TestSwitch(OpenpilotTestCase):
  def test_switch_off_stops_confirmation_within_a_second_but_keeps_the_self_check(self):
    params = FakeParams()
    confirmer = trusted_confirmer(params)
    candidate = (40.5, 0.3, 0.3)
    assert confirmer.update(20.0, LEAD, None, [candidate, FAR])[0]
    params.enabled = False
    results = [confirmer.update(20.0, LEAD, None, [candidate, FAR])[0] for _ in range(TICKS_PER_READ)]
    assert not results[-1]
    assert confirmer.trusted

  def test_switch_off_from_the_start_still_runs_the_self_check(self):
    confirmer = trusted_confirmer(FakeParams(enabled=False))
    assert confirmer.update(20.0, LEAD, None, [(40.5, 0.3, 0.3), FAR]) == [False, False]
