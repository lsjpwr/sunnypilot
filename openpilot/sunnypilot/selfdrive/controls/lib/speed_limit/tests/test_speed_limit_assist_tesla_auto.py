"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import unittest

from openpilot.cereal import custom
from opendbc.car.car_helpers import interfaces
from opendbc.car.tesla.values import CAR as TESLA
from opendbc.car.toyota.values import CAR as TOYOTA
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import comma_target_kph, section_average_display
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import SpeedLimitAssist, V_CRUISE_UNSET
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP

EventNameSP = custom.OnroadEventSP.EventName
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState


def kph(v):
  return v * CV.KPH_TO_MS


class MemParams(dict):
  """Stands in for the /dev/shm params: korea mapd's section on the way in, the average on the way out."""

  def put(self, key, value, block=False):
    self[key] = value


class TestTeslaAutoSpeedLimitAssist(OpenpilotTestCase):
  def setup_method(self):
    self.params = Params()
    self.params.put_bool("IsReleaseSpBranch", False, block=True)
    self.params.put("SpeedLimitMode", int(Mode.assist), block=True)
    self.params.put_bool("IsMetric", True, block=True)
    self.params.put_bool("TeslaAutoSpeedLimitAssist", True, block=True)
    self.params.put("TeslaAutoSpeedLimitDelay", 2.0, block=True)
    self.events_sp = EventsSP()
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)

  def make_sla(self, car_name):
    CarInterface = interfaces[car_name]
    CP = CarInterface.get_non_essential_params(car_name)
    CP_SP = CarInterface.get_non_essential_params_sp(CP, car_name)
    CI = CarInterface(CP, CP_SP)
    CI.CP.openpilotLongitudinalControl = True
    sunnypilot_interfaces.setup_interfaces(CI, self.params)
    sla = SpeedLimitAssist(CI.CP, CI.CP_SP)
    self.mem = sla.mem_params = MemParams()
    return sla

  def drive(self, seconds, limit_kph, set_kph, v_kph=None, engaged=True):
    """Run the SLA at DT_MDL. limit_kph is limit + offset as the resolver hands it over, 0 for none
    yet. Returns the events of every frame."""
    v = kph(v_kph if v_kph is not None else (limit_kph or set_kph))
    seen = []
    for _ in range(round(seconds / DT_MDL)):
      self.events_sp.clear()
      limit = kph(limit_kph)
      self.sla.update(engaged, False, v, 0., kph(set_kph), limit, limit, limit > 0., 0., self.events_sp)
      seen.append(list(self.events_sp.names))
    return seen

  @property
  def target_kph(self):
    return round(self.sla.output_v_target * CV.MS_TO_KPH)

  def test_follows_the_limit_without_asking(self):
    events = self.drive(1., limit_kph=60, set_kph=69, v_kph=65)
    assert self.sla.state in (SpeedLimitAssistState.active, SpeedLimitAssistState.adapting)
    assert self.target_kph == 60
    assert not any(EventNameSP.speedLimitPreActive in e for e in events)

  def test_announces_once_on_engagement(self):
    events = self.drive(1., limit_kph=60, set_kph=69, v_kph=65)
    assert sum(EventNameSP.speedLimitActive in e for e in events) == 1

  def test_a_new_limit_waits_for_the_delay(self):
    self.drive(1., limit_kph=60, set_kph=69)
    self.drive(1.9, limit_kph=50, set_kph=69)
    assert self.target_kph == 60
    events = self.drive(0.1, limit_kph=50, set_kph=69)
    assert self.target_kph == 50
    assert any(EventNameSP.speedLimitActive in e for e in events)

  def test_a_flicker_shorter_than_the_delay_changes_nothing(self):
    self.drive(1., limit_kph=60, set_kph=69)
    events = self.drive(0.1, limit_kph=30, set_kph=69) + self.drive(3., limit_kph=60, set_kph=69)
    assert self.target_kph == 60
    assert not any(EventNameSP.speedLimitActive in e for e in events)

  def test_a_second_change_during_the_hold_restarts_it(self):
    self.drive(1., limit_kph=60, set_kph=69)
    self.drive(1., limit_kph=50, set_kph=69)
    self.drive(1.9, limit_kph=40, set_kph=69)
    assert self.target_kph == 60
    self.drive(0.1, limit_kph=40, set_kph=69)
    assert self.target_kph == 40

  def test_zero_delay_applies_at_once(self):
    self.params.put("TeslaAutoSpeedLimitDelay", 0., block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=60, set_kph=69)
    self.drive(DT_MDL, limit_kph=50, set_kph=69)
    assert self.target_kph == 50

  def test_the_first_limit_is_taken_after_the_delay_and_announced(self):
    self.drive(1., limit_kph=0, set_kph=69, v_kph=60)
    self.drive(1.95, limit_kph=60, set_kph=69, v_kph=60)
    assert self.sla.output_v_target == V_CRUISE_UNSET
    events = self.drive(0.05, limit_kph=60, set_kph=69, v_kph=60)
    assert self.target_kph == 60
    assert any(EventNameSP.speedLimitActive in e for e in events)

  def test_a_scroll_moves_the_target_by_the_same_amount(self):
    self.drive(1., limit_kph=50, set_kph=69)
    self.drive(0.5, limit_kph=50, set_kph=74)
    assert self.target_kph == 55

  def test_a_scroll_up_to_the_target_is_not_a_nudge(self):
    # engaged where the Tesla picked 57 but the map says 60: the set speed is the ceiling
    self.drive(1., limit_kph=60, set_kph=57)
    self.drive(0.5, limit_kph=60, set_kph=60)
    assert self.target_kph == 60
    self.drive(0.5, limit_kph=60, set_kph=65)
    assert self.target_kph == 65

  def test_the_nudge_lasts_through_limit_changes_until_cruise_is_canceled(self):
    self.drive(1., limit_kph=60, set_kph=60)
    self.drive(0.5, limit_kph=60, set_kph=70)
    self.drive(3., limit_kph=50, set_kph=70)
    assert self.target_kph == 60
    self.drive(0.5, limit_kph=50, set_kph=70, engaged=False)
    self.drive(1., limit_kph=50, set_kph=57)
    assert self.target_kph == 50

  def test_the_set_speed_the_tesla_picks_at_engagement_is_not_a_scroll(self):
    self.drive(0.2, limit_kph=50, set_kph=40)
    self.drive(1., limit_kph=50, set_kph=57)
    assert self.target_kph == 50

  def test_a_scroll_down_never_takes_the_target_under_30(self):
    self.drive(1., limit_kph=100, set_kph=110)
    self.drive(0.5, limit_kph=100, set_kph=30)
    assert self.target_kph == 30
    self.drive(3., limit_kph=50, set_kph=30)
    assert self.target_kph == 30

  def test_without_a_limit_the_set_speed_rules(self):
    events = self.drive(2., limit_kph=0, set_kph=69, v_kph=60)
    assert self.sla.output_v_target == V_CRUISE_UNSET
    assert self.sla.state == SpeedLimitAssistState.pending
    assert not any(events)

  def test_switch_off_keeps_the_confirmation_flow(self):
    self.params.put_bool("TeslaAutoSpeedLimitAssist", False, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=60, set_kph=69)
    assert self.sla.state == SpeedLimitAssistState.preActive

  def test_other_brands_ignore_the_switch(self):
    assert not self.make_sla(TOYOTA.TOYOTA_RAV4_TSS2).auto_mode

  def test_the_max_speed_caps_the_target(self):
    self.params.put("TeslaAutoSpeedLimitMax", 125, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=126.5, set_kph=127)
    assert self.target_kph == 125

  def test_the_max_speed_holds_without_a_limit(self):
    self.params.put("TeslaAutoSpeedLimitMax", 125, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=0, set_kph=140, v_kph=120)
    assert self.target_kph == 125

  def test_a_scroll_above_the_max_builds_no_nudge(self):
    self.params.put("TeslaAutoSpeedLimitMax", 125, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=115, set_kph=115)
    self.drive(0.5, limit_kph=115, set_kph=135)
    assert self.target_kph == 125
    # only the 10 below the max counted: 80+15% plus 10, not plus 20
    self.drive(3., limit_kph=92, set_kph=135)
    assert self.target_kph == 102

  def test_zero_max_speed_is_off(self):
    self.drive(1., limit_kph=130, set_kph=135)
    assert self.target_kph == 130

  def enter_section(self, limit_kph, start=1.):
    self.mem["KoreaSectionSpeedLimit"] = kph(limit_kph)
    self.mem["KoreaSectionStart"] = start if limit_kph > 0 else 0.

  def test_a_section_holds_its_exact_limit(self):
    self.drive(1., limit_kph=115, set_kph=115)
    self.drive(0.5, limit_kph=115, set_kph=125)
    assert self.target_kph == 125
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=125, v_kph=100)
    assert self.target_kph == 100

  def test_a_scroll_inside_a_section_moves_its_pace_from_then_on(self):
    self.drive(1., limit_kph=115, set_kph=115)
    self.drive(0.5, limit_kph=115, set_kph=125)
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=125, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=130, v_kph=105)
    assert self.target_kph == 105
    # the time already driven at 100 is not counted again at 105, so nothing jumps
    self.drive(60., limit_kph=115, set_kph=130, v_kph=105)
    assert self.target_kph == 105
    self.enter_section(0)
    self.drive(1., limit_kph=115, set_kph=130)
    assert self.target_kph == 125

  def test_a_lower_limit_inside_a_section_still_wins(self):
    self.enter_section(100)
    self.drive(1., limit_kph=92, set_kph=115)
    assert self.target_kph == 92

  def test_cancel_inside_a_section_drops_the_scroll_but_keeps_the_section(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=120, v_kph=105)
    assert self.target_kph == 105
    self.drive(0.5, limit_kph=115, set_kph=120, v_kph=105, engaged=False)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    assert self.target_kph == 100

  def test_a_section_holds_even_without_a_road_limit(self):
    self.enter_section(100)
    self.drive(1., limit_kph=0, set_kph=115, v_kph=100)
    assert self.target_kph == 100
    assert self.sla.state == SpeedLimitAssistState.active

  def test_time_lost_in_a_section_comes_back_above_its_limit(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    self.drive(12., limit_kph=115, set_kph=115, v_kph=70)
    # 100 m behind: 100 + 100 m / 60 s = 106
    assert self.target_kph == 106
    self.drive(30., limit_kph=115, set_kph=115, v_kph=106)
    # 30 s at 106 made up 50 m of it
    assert self.target_kph == 103

  def test_running_ahead_in_a_section_is_paid_back_at_most_10_under(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    self.drive(6., limit_kph=115, set_kph=115, v_kph=130)
    # the driver's foot took the car 50 m ahead: 100 - 50 m / 60 s = 97
    assert self.target_kph == 97
    self.drive(24., limit_kph=115, set_kph=115, v_kph=130)
    # 250 m ahead would be 85, but paying back stops 10 under the pace
    assert self.target_kph == 90

  def test_a_scroll_up_in_a_section_is_not_paid_back(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    self.drive(60., limit_kph=115, set_kph=120, v_kph=105)
    assert self.target_kph == 105

  def test_the_section_counts_while_cruise_is_off(self):
    self.enter_section(100)
    self.drive(12., limit_kph=115, set_kph=115, v_kph=70, engaged=False)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    assert self.target_kph == 106

  def test_a_new_section_starts_from_nothing(self):
    self.enter_section(100, start=1.)
    self.drive(12., limit_kph=115, set_kph=115, v_kph=70)
    assert self.target_kph == 106
    # the next section starts where this one ends, with the same limit: only the start tells
    self.enter_section(100, start=2.)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    assert self.target_kph == 100

  def test_the_section_average_goes_to_shared_memory(self):
    self.enter_section(100)
    self.drive(10., limit_kph=115, set_kph=115, v_kph=90)
    assert round(self.mem["KoreaSectionAverage"] * CV.MS_TO_KPH) == 90
    self.enter_section(0)
    self.drive(1., limit_kph=115, set_kph=115)
    assert self.mem["KoreaSectionAverage"] == 0.

  def test_cancel_inside_a_section_stops_the_scroll_counting_while_off(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=125, v_kph=110)
    assert self.target_kph == 110
    # the driver holds the limit with cruise off: nothing to make up when it comes back on
    self.drive(60., limit_kph=115, set_kph=125, v_kph=100, engaged=False)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    assert self.target_kph == 100

  def test_a_scroll_inside_a_section_works_when_the_road_target_is_the_limit(self):
    # no offset: the road target and the section limit are both 100
    self.enter_section(100)
    self.drive(1., limit_kph=100, set_kph=115, v_kph=100)
    self.drive(0.5, limit_kph=100, set_kph=120, v_kph=105)
    assert self.target_kph == 105

  def test_a_scroll_down_inside_a_section_takes_effect_at_once(self):
    self.enter_section(100)
    self.drive(1., limit_kph=100, set_kph=115, v_kph=100)
    self.drive(120., limit_kph=100, set_kph=125, v_kph=110)
    assert self.target_kph == 110
    self.drive(0.5, limit_kph=100, set_kph=105, v_kph=90)
    assert self.target_kph == 90

  def test_raising_the_set_speed_to_the_section_limit_is_not_a_scroll(self):
    # the Tesla number held the car under the section limit; raising it to the limit adds no pace
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=92, v_kph=92)
    self.drive(0.5, limit_kph=115, set_kph=100, v_kph=100)
    assert self.target_kph == 100

  def test_a_scroll_up_after_the_floor_responds_at_once(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=115, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=30, v_kph=30)
    assert self.target_kph == 30
    self.drive(0.5, limit_kph=115, set_kph=40, v_kph=30)
    assert self.target_kph == 40

  def test_a_scroll_down_inside_a_section_moves_a_road_target_holding_the_car(self):
    # no offset, and a jam left the car 15 km/h behind: the road target holds it while it catches up
    self.enter_section(100)
    self.drive(1., limit_kph=100, set_kph=115, v_kph=100)
    self.drive(30., limit_kph=100, set_kph=115, v_kph=70)
    self.drive(30., limit_kph=100, set_kph=115, v_kph=100)
    assert self.target_kph == 100
    self.drive(0.5, limit_kph=100, set_kph=105, v_kph=90)
    assert self.target_kph == 90

  def test_a_scroll_down_inside_a_section_moves_a_lower_road_target(self):
    # a tunnel inside the section: its road target is under the section's pace
    self.enter_section(100)
    self.drive(1., limit_kph=92, set_kph=115, v_kph=92)
    self.drive(0.5, limit_kph=92, set_kph=105, v_kph=82)
    assert self.target_kph == 82

  def test_a_scroll_up_after_the_floor_responds_at_once_under_a_lower_road_target(self):
    self.enter_section(100)
    self.drive(1., limit_kph=92, set_kph=115, v_kph=92)
    self.drive(0.5, limit_kph=92, set_kph=30, v_kph=30)
    assert self.target_kph == 30
    self.drive(0.5, limit_kph=92, set_kph=40, v_kph=30)
    assert self.target_kph == 40

  def test_raising_the_set_speed_over_a_lower_road_target_moves_it_as_outside_a_section(self):
    # the Tesla number sat on the tunnel's road target: raising it moves the target the same way it
    # would outside a section, while the section's pace takes only the part above the section limit
    self.enter_section(100)
    self.drive(1., limit_kph=92, set_kph=92, v_kph=92)
    self.drive(0.5, limit_kph=92, set_kph=102, v_kph=92)
    assert self.target_kph == 102
    # out of the tunnel: the pace is 102, not 110
    self.drive(3., limit_kph=115, set_kph=102, v_kph=102)
    assert self.target_kph == 102

  def test_a_lower_road_limit_keeps_a_scrolled_down_road_target_at_the_floor(self):
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=135, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=40, v_kph=30)
    assert self.target_kph == 30
    # a tunnel starts: the scrolled-down road target stays at 30, not under it
    self.drive(3., limit_kph=92, set_kph=40, v_kph=30)
    assert self.target_kph == 30
    self.drive(0.5, limit_kph=92, set_kph=50, v_kph=30)
    assert self.target_kph == 40

  def test_a_lower_limit_inside_a_section_moves_the_road_target_as_outside_a_section(self):
    # a deep scroll down before the section and a scroll up inside it, then a tunnel: 97 either way
    self.drive(1., limit_kph=115, set_kph=125, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=50, v_kph=40)
    self.enter_section(100)
    self.drive(0.5, limit_kph=115, set_kph=50, v_kph=40)
    self.drive(0.5, limit_kph=115, set_kph=130, v_kph=100)
    self.drive(3., limit_kph=92, set_kph=130, v_kph=92)
    assert self.target_kph == 97

  def test_a_scroll_down_outlasts_the_road_limit_dropping_out(self):
    self.drive(1., limit_kph=92, set_kph=115, v_kph=92)
    self.drive(0.5, limit_kph=92, set_kph=105, v_kph=82)
    assert self.target_kph == 82
    self.drive(3., limit_kph=0, set_kph=105, v_kph=82)
    self.drive(3., limit_kph=92, set_kph=105, v_kph=82)
    assert self.target_kph == 82

  def test_a_scroll_up_in_a_section_that_undid_one_before_it_outlasts_the_section(self):
    # limit 115, Tesla 125, scroll to 50 (target 40), then to 130 inside a 100 section (target 120):
    # after the section the 75 that undid the scroll down stays, the 5 above 115 was the section's own
    self.drive(1., limit_kph=115, set_kph=125, v_kph=115)
    self.drive(0.5, limit_kph=115, set_kph=50, v_kph=50)
    assert self.target_kph == 40
    self.enter_section(100)
    self.drive(1., limit_kph=115, set_kph=50, v_kph=50)
    self.drive(0.5, limit_kph=115, set_kph=130, v_kph=100)
    self.drive(30., limit_kph=115, set_kph=130, v_kph=120)
    assert self.target_kph == 120
    self.enter_section(0)
    self.drive(1., limit_kph=115, set_kph=130, v_kph=120)
    assert self.target_kph == 115

  def test_a_section_ending_in_a_tunnel_keeps_the_undone_scroll_down_undone(self):
    self.drive(1., limit_kph=115, set_kph=125, v_kph=100)
    self.drive(0.5, limit_kph=115, set_kph=50, v_kph=40)
    self.enter_section(100)
    self.drive(0.5, limit_kph=115, set_kph=50, v_kph=40)
    self.drive(0.5, limit_kph=115, set_kph=130, v_kph=100)
    self.drive(3., limit_kph=92, set_kph=130, v_kph=92)
    assert self.target_kph == 97
    self.enter_section(0)
    self.drive(1., limit_kph=92, set_kph=130, v_kph=92)
    assert self.target_kph == 92


class TestCommaTargetDisplay(unittest.TestCase):
  def test_shows_the_speed_limit_target_while_it_holds_the_car_under_the_set_speed(self):
    assert round(comma_target_kph(True, kph(57), False, V_CRUISE_UNSET, 69.)) == 57

  def test_a_lower_camera_target_wins(self):
    assert round(comma_target_kph(True, kph(115), True, kph(100), 125.)) == 100

  def test_shows_nothing_when_the_set_speed_is_the_lower_one(self):
    assert comma_target_kph(True, kph(69), False, V_CRUISE_UNSET, 69.) is None
    assert comma_target_kph(True, kph(80), True, kph(75), 69.) is None

  def test_shows_nothing_without_an_active_target(self):
    assert comma_target_kph(False, kph(50), False, kph(40), 69.) is None
    assert comma_target_kph(True, V_CRUISE_UNSET, False, V_CRUISE_UNSET, 69.) is None


class TestSectionAverageDisplay(unittest.TestCase):
  def test_shows_the_average_in_display_units(self):
    assert section_average_display(kph(97), kph(100), True) == (97, False)
    assert section_average_display(kph(97), kph(100), False) == (60, False)

  def test_marks_an_average_over_the_limit(self):
    assert section_average_display(kph(102), kph(100), True) == (102, True)

  def test_an_average_that_rounds_to_the_limit_is_not_over(self):
    assert section_average_display(kph(100.4), kph(100), True) == (100, False)

  def test_shows_nothing_outside_a_section(self):
    assert section_average_display(0., kph(100), True) is None
    assert section_average_display(kph(97), 0., True) is None
