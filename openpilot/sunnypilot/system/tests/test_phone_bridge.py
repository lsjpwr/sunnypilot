"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import http.client
import ipaddress
import json
import pathlib
import re
import threading

import pytest

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.sunnypilot.system.phone_bridge import (MAX_BODY, PAIR_CODE_PARAM, PAIR_CODE_LIFETIME_S, SERVICES, TOKEN_PARAM, PhoneBridge, in_subnet,
                                                      make_server, new_pair_code, wifi_subnet)


class FakeParams:
  def __init__(self, **values):
    self.values = values

  def get(self, key):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key))

  def put(self, key, value, block=False):
    assert block, "a write the next request depends on has to land first"
    self.values[key] = value

  def remove(self, key):
    self.values.pop(key, None)


class Clock:
  def __init__(self):
    self.t = 1000.

  def __call__(self):
    return self.t


class Phone:
  """The app's side of the HTTP connection, against a real server on a free port."""

  def __init__(self, port):
    self.port = port

  def request(self, method, path, body=None, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
    all_headers = dict(headers or {})
    if token is not None:
      all_headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body if body is None or isinstance(body, bytes) else json.dumps(body), all_headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw) if raw else None

  def pair(self, code):
    return self.request("POST", "/v1/pair", {"code": code})


@pytest.fixture
def world():
  params, clock = FakeParams(), Clock()
  servers = []

  def start(allowed=lambda ip: True, sm=None, mem_params=None):
    server = make_server(PhoneBridge(params, mem_params or FakeParams(), sm, clock), allowed, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    servers.append(server)
    return Phone(server.server_address[1])

  yield params, clock, start
  for server in servers:
    server.shutdown()
    server.server_close()


def wrong(code):
  return "000000" if code != "000000" else "111111"


class TestPairing:
  def test_the_shown_code_buys_a_token(self, world):
    params, clock, start = world
    code = new_pair_code(params, clock)

    status, body = start().pair(code)

    assert status == 200
    assert body["token"] == params.get(TOKEN_PARAM)
    assert len(body["token"]) >= 32

  def test_a_new_code_is_six_digits(self, world):
    params, clock, _ = world
    for _ in range(50):
      code = new_pair_code(params, clock)
      assert len(code) == 6 and code.isdigit()

  def test_a_code_works_once(self, world):
    params, clock, start = world
    code = new_pair_code(params, clock)
    phone = start()

    assert phone.pair(code)[0] == 200
    assert phone.pair(code)[0] == 403

  def test_a_code_dies_after_its_minute(self, world):
    params, clock, start = world
    code = new_pair_code(params, clock)
    phone = start()

    clock.t += PAIR_CODE_LIFETIME_S + 1

    assert phone.pair(code)[0] == 403
    assert params.get(TOKEN_PARAM) is None

  def test_no_code_on_screen_means_no_pairing(self, world):
    _, _, start = world
    assert start().pair("123456")[0] == 403

  def test_five_wrong_tries_close_the_code(self, world):
    params, clock, start = world
    code = new_pair_code(params, clock)
    phone = start()

    for _ in range(5):
      assert phone.pair(wrong(code))[0] == 403

    assert phone.pair(code)[0] == 403
    assert params.get(TOKEN_PARAM) is None
    assert params.get(PAIR_CODE_PARAM) is None, "the dialog on the comma closes when the code is gone"

  def test_four_wrong_tries_leave_it_open(self, world):
    params, clock, start = world
    code = new_pair_code(params, clock)
    phone = start()

    for _ in range(4):
      phone.pair(wrong(code))

    assert phone.pair(code)[0] == 200

  def test_a_new_code_starts_a_new_count(self, world):
    params, clock, start = world
    phone = start()
    first = new_pair_code(params, clock)
    for _ in range(4):
      phone.pair(wrong(first))

    second = new_pair_code(params, clock)
    phone.pair(wrong(second))

    assert phone.pair(second)[0] == 200

  def test_pairing_again_ends_the_old_token(self, world):
    params, clock, start = world
    phone = start()
    old = phone.pair(new_pair_code(params, clock))[1]["token"]

    new = phone.pair(new_pair_code(params, clock))[1]["token"]

    assert new != old
    assert phone.request("GET", "/v1/hud", token=old)[0] == 401

  @pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"code": 123456}', b'{"nothing": "here"}'])
  def test_a_malformed_request_is_400_and_costs_no_try(self, world, body):
    params, clock, start = world
    code = new_pair_code(params, clock)
    phone = start()

    for _ in range(5):
      assert phone.request("POST", "/v1/pair", body)[0] == 400

    assert phone.pair(code)[0] == 200

  def test_an_oversized_body_is_refused_unread(self, world):
    _, _, start = world
    status, _ = start().request("POST", "/v1/pair", b"x" * (MAX_BODY + 1))
    assert status == 400


class TestAuth:
  def test_no_token_is_401(self, world):
    _, _, start = world
    assert start().request("GET", "/v1/hud")[0] == 401

  def test_wrong_token_is_401(self, world):
    params, clock, start = world
    phone = start()
    phone.pair(new_pair_code(params, clock))
    assert phone.request("GET", "/v1/hud", token="wrong")[0] == 401

  def test_an_unpaired_comma_accepts_no_token_at_all(self, world):
    _, _, start = world
    phone = start()
    assert phone.request("GET", "/v1/hud", token="")[0] == 401
    assert phone.request("GET", "/v1/hud", headers={"Authorization": "Bearer"})[0] == 401

  def test_a_token_with_foreign_bytes_is_401_not_a_crash(self, world):
    params, clock, start = world
    phone = start()
    phone.pair(new_pair_code(params, clock))
    assert phone.request("GET", "/v1/hud", headers={"Authorization": "Bearer caf\xe9".encode("latin-1")})[0] == 401

  def test_an_unknown_path_needs_the_token_before_it_is_404(self, world):
    params, clock, start = world
    phone = start()
    token = phone.pair(new_pair_code(params, clock))[1]["token"]

    assert phone.request("GET", "/v1/nope")[0] == 401
    assert phone.request("POST", "/v1/nope", {})[0] == 401
    assert phone.request("GET", "/v1/nope", token=token)[0] == 404
    assert phone.request("POST", "/v1/nope", {}, token=token)[0] == 404


class TestSubnet:
  def test_a_request_from_outside_is_dropped_before_anything_is_read(self, world):
    params, clock, start = world
    code = new_pair_code(params, clock)
    seen = []
    phone = start(allowed=lambda ip: seen.append(ip) or False)

    with pytest.raises((http.client.HTTPException, ConnectionError)):
      phone.pair(code)

    assert seen == ["127.0.0.1"]
    assert params.get(TOKEN_PARAM) is None

  @pytest.mark.parametrize("client,inside", [("192.168.43.7", True), ("192.168.44.7", False), ("100.64.0.1", False)])
  def test_in_subnet(self, client, inside):
    assert in_subnet(client, ipaddress.ip_network("192.168.43.0/24")) is inside

  def test_no_wifi_means_nobody_is_inside(self):
    assert in_subnet("192.168.43.7", None) is False

  def test_wifi_subnet_reads_the_interface(self):
    assert wifi_subnet("lo") == ipaddress.ip_network("127.0.0.0/8")

  def test_wifi_subnet_of_a_missing_interface_is_none(self):
    assert wifi_subnet("nosuchif0") is None


class TestRegistration:
  @pytest.mark.parametrize("enabled", [True, False])
  @pytest.mark.parametrize("started", [True, False])
  def test_it_runs_on_the_switch_alone(self, started, enabled):
    from openpilot.system.manager.process_config import managed_processes
    proc = managed_processes["phone_bridge"]
    assert proc.should_run(started, FakeParams(PhoneBridgeEnabled=enabled), None) is enabled

  def test_a_dead_bridge_never_blocks_engagement(self):
    """selfdrived refuses to engage on any process that should run and does not."""
    source = (pathlib.Path(__file__).resolve().parents[3] / "selfdrive" / "selfdrived" / "selfdrived.py").read_text(encoding="utf-8")
    assert re.search(r"self\.ignored_processes = \{[^}]*'phone_bridge'", source)

  def test_the_switch_defaults_to_off(self):
    text = (pathlib.Path(__file__).resolve().parents[3] / "common" / "params_keys.h").read_text(encoding="utf-8")
    assert re.search(r'\{"PhoneBridgeEnabled", \{[^}]*BOOL, "0"\}', text)


def reader(service, fields):
  return getattr(log.Event.new_message(**{service: fields}).as_reader(), service)


class FakeSM:
  """A SubMaster holding real capnp readers, so enums and lists behave the way they do on the device."""

  def __init__(self, **messages):
    self.data = {s: reader(s, messages.get(s, {})) for s in SERVICES}
    self.alive = dict.fromkeys(SERVICES, True)
    self.valid = dict.fromkeys(SERVICES, True)

  def __getitem__(self, service):
    return self.data[service]


TESLA = car.CarParams.new_message(brand="tesla").to_bytes()
KPH_TO_MS = CV.KPH_TO_MS
AUTO_SLA = {"TeslaAutoSpeedLimitAssist": True, "CarParams": TESLA}
IN_SECTION = {"KoreaSectionAverage": 72 * KPH_TO_MS, "KoreaSectionSpeedLimit": 80 * KPH_TO_MS}
CRUISE = {"vCruiseCluster": 127.}
BY_PLAN = {"longitudinalPlanSource": "cruise"}
BY_MODEL = {"longitudinalPlanSource": "e2e"}
ALL_FIELDS = ("started", "engaged", "max_kph", "max_green", "speed_kph", "section", "lead", "desire", "blind", "alert")


def hud(world, params=None, mem_params=None, stale=(), invalid=(), **messages):
  bridge_params, clock, start = world
  bridge_params.values.update(params or {})
  sm = FakeSM(**messages)
  for service in stale:
    sm.alive[service] = False
  for service in invalid:
    sm.valid[service] = False
  phone = start(sm=sm, mem_params=FakeParams(**(mem_params or {})))
  status, body = phone.request("GET", "/v1/hud", token=phone.pair(new_pair_code(bridge_params, clock))[1]["token"])
  assert status == 200
  return body


def sla_plan(kph):
  return {"speedLimit": {"assist": {"enabled": True, "vTarget": kph * KPH_TO_MS}}, "smartCruiseControl": {"map": {"active": False, "vTarget": 255.}}}


def e2e_leads(prob, x):
  lead = {"prob": prob, "x": [x]}
  return {"leadsV3": [lead, lead, lead]}


def desires(**by_index):
  state = [0.] * 8
  for index, value in by_index.items():
    state[int(index.removeprefix("d"))] = value
  return state


class TestHud:
  def test_version_and_started(self, world):
    body = hud(world, deviceState={"started": True})
    assert body["v"] == 1 and body["started"] is True

  def test_parked_is_not_started(self, world):
    assert hud(world, deviceState={"started": False})["started"] is False

  @pytest.mark.parametrize("enabled,mads,expected", [(True, False, True), (False, True, True), (False, False, False)])
  def test_engaged_counts_mads_like_mici(self, world, enabled, mads, expected):
    body = hud(world, deviceState={"started": True}, selfdriveState={"enabled": enabled}, selfdriveStateSP={"mads": {"enabled": mads}})
    assert body["engaged"] is expected

  def test_nothing_is_engaged_when_parked(self, world):
    assert hud(world, deviceState={"started": False}, selfdriveState={"enabled": True})["engaged"] is False

  def test_max_is_the_set_speed_in_kph(self, world):
    body = hud(world, carState={"vCruiseCluster": 100.4})
    assert (body["max_kph"], body["max_green"]) == (100, False)

  def test_max_falls_back_to_controls_without_a_cluster_speed(self, world):
    assert hud(world, carState={"vCruiseCluster": 0.}, controlsState={"deprecated": {"vCruise": 90.}})["max_kph"] == 90

  def test_a_stale_fallback_is_no_max_at_all(self, world):
    body = hud(world, stale=["controlsState"], carState={"vCruiseCluster": 0.}, controlsState={"deprecated": {"vCruise": 90.}})
    assert (body["max_kph"], body["max_green"]) == (None, None)

  @pytest.mark.parametrize("cruise", [255., -1.])
  def test_no_max_while_cruise_is_not_set(self, world, cruise):
    body = hud(world, carState={"vCruiseCluster": cruise})
    assert (body["max_kph"], body["max_green"]) == (None, False)

  def test_max_goes_green_when_the_comma_holds_the_car_under_the_tesla_speed(self, world):
    body = hud(world, params=AUTO_SLA, carState=CRUISE, longitudinalPlanSP=sla_plan(80))
    assert (body["max_kph"], body["max_green"]) == (80, True)

  def test_green_needs_the_auto_sla_switch(self, world):
    body = hud(world, params={"CarParams": TESLA}, carState=CRUISE, longitudinalPlanSP=sla_plan(80))
    assert (body["max_kph"], body["max_green"]) == (127, False)

  def test_green_needs_a_tesla(self, world):
    params = {**AUTO_SLA, "CarParams": car.CarParams.new_message(brand="hyundai").to_bytes()}
    assert hud(world, params=params, carState=CRUISE, longitudinalPlanSP=sla_plan(80))["max_green"] is False

  def test_a_dead_planner_shows_none_of_its_last_plan(self, world):
    body = hud(world, params=AUTO_SLA, stale=["longitudinalPlanSP"], carState=CRUISE, longitudinalPlanSP=sla_plan(80), mem_params=IN_SECTION)
    assert (body["max_kph"], body["max_green"], body["section"]) == (127, False, None)

  def test_speed_is_the_cluster_speed_once_the_car_has_one(self, world):
    assert hud(world, carState={"vEgo": 20., "vEgoCluster": 78 * KPH_TO_MS})["speed_kph"] == 78

  def test_speed_is_the_wheel_speed_without_one(self, world):
    assert hud(world, carState={"vEgo": 78 * KPH_TO_MS, "vEgoCluster": 0.})["speed_kph"] == 78

  @pytest.mark.parametrize("average,expected", [(72, {"avg": 72, "over": False}), (90, {"avg": 90, "over": True})])
  def test_section_average(self, world, average, expected):
    body = hud(world, params=AUTO_SLA, carState=CRUISE, longitudinalPlanSP=sla_plan(80),
               mem_params={"KoreaSectionAverage": average * KPH_TO_MS, "KoreaSectionSpeedLimit": 80 * KPH_TO_MS})
    assert body["section"] == expected

  def test_section_stays_in_kph_on_a_car_shown_in_miles(self, world):
    body = hud(world, params={**AUTO_SLA, "IsMetric": False}, carState=CRUISE, longitudinalPlanSP=sla_plan(80), mem_params=IN_SECTION)
    assert body["section"] == {"avg": 72, "over": False}

  def test_no_section_outside_one(self, world):
    assert hud(world, params=AUTO_SLA, carState=CRUISE, longitudinalPlanSP=sla_plan(80))["section"] is None

  def test_e2e_lead_comes_from_the_model(self, world):
    body = hud(world, longitudinalPlan=BY_MODEL, modelV2=e2e_leads(.9, 32.4), radarState={"leadOne": {"present": False}})
    assert body["lead"] == {"seen": True, "dist_m": 32}

  def test_e2e_lead_below_half_is_not_seen(self, world):
    body = hud(world, longitudinalPlan=BY_MODEL, modelV2=e2e_leads(.4, 32.4), radarState={"leadOne": {"present": True, "dRel": 9.}})
    assert body["lead"] == {"seen": False, "dist_m": None}

  def test_any_other_source_takes_the_radar_lead_at_the_camera(self, world):
    body = hud(world, longitudinalPlan=BY_PLAN, modelV2=e2e_leads(.9, 90.), radarState={"leadOne": {"present": True, "dRel": 30.}})
    assert body["lead"] == {"seen": True, "dist_m": 32}

  def test_no_radar_lead(self, world):
    body = hud(world, longitudinalPlan=BY_PLAN, radarState={"leadOne": {"present": False, "dRel": 30.}})
    assert body["lead"] == {"seen": False, "dist_m": None}

  def test_e2e_with_a_dead_model_does_not_borrow_the_radar(self, world):
    body = hud(world, stale=["modelV2"], longitudinalPlan=BY_MODEL, modelV2=e2e_leads(.9, 32.), radarState={"leadOne": {"present": True, "dRel": 30.}})
    assert body["lead"] is None

  @pytest.mark.parametrize("plan", [BY_MODEL, BY_PLAN])
  def test_mici_draws_no_lead_on_an_invalid_radar_state(self, world, plan):
    body = hud(world, invalid=["radarState"], longitudinalPlan=plan, modelV2=e2e_leads(.9, 32.), radarState={"leadOne": {"present": True, "dRel": 30.}})
    assert body["lead"] is None

  @pytest.mark.parametrize("state,direction,expected", [
    ("laneChangeStarting", "left", "laneChangeLeft"),
    ("laneChangeFinishing", "right", "laneChangeRight"),
  ])
  def test_a_lane_change_under_way_beats_the_model(self, world, state, direction, expected):
    assert hud(world, modelV2={"meta": {"laneChangeState": state, "laneChangeDirection": direction, "desireState": desires(d1=.9)}})["desire"] == expected

  def test_a_lane_change_only_waiting_is_not_under_way(self, world):
    meta = {"laneChangeState": "preLaneChange", "laneChangeDirection": "left", "desireState": desires(d2=.8)}
    assert hud(world, modelV2={"meta": meta})["desire"] == "turnRight"

  def test_desire_none_never_wins(self, world):
    assert hud(world, modelV2={"meta": {"desireState": desires(d0=.9, d1=.1)}})["desire"] is None

  def test_desire_at_a_half_is_not_enough(self, world):
    assert hud(world, modelV2={"meta": {"desireState": desires(d1=.5)}})["desire"] is None

  def test_the_model_slot_past_the_named_desires_is_ignored(self, world):
    assert hud(world, modelV2={"meta": {"desireState": desires(d7=.9)}})["desire"] is None

  def test_blind_spots(self, world):
    assert hud(world, carState={"leftBlindspot": False, "rightBlindspot": True})["blind"] == {"l": False, "r": True}

  def test_alert_is_passed_as_it_is(self, world):
    body = hud(world, selfdriveState={"alertText1": "Take Control", "alertText2": "Lane Departure", "alertStatus": "critical"})
    assert body["alert"] == {"text1": "Take Control", "text2": "Lane Departure", "status": "critical"}

  def test_no_alert_is_null(self, world):
    assert hud(world, selfdriveState={"alertText1": "", "alertText2": ""})["alert"] is None

  @pytest.mark.parametrize("service,fields", [
    ("deviceState", ("started", "engaged")),
    ("selfdriveState", ("engaged", "alert")),
    ("selfdriveStateSP", ("engaged",)),
    ("carState", ("max_kph", "max_green", "speed_kph", "blind")),
    ("controlsState", ()),  # only needed without a cluster speed
    ("longitudinalPlan", ("lead",)),
    ("modelV2", ("desire",)),
  ])
  def test_a_stale_message_blanks_its_fields_and_no_others(self, world, service, fields):
    body = hud(world, params=AUTO_SLA, stale=[service], mem_params=IN_SECTION, deviceState={"started": True},
               selfdriveState={"enabled": True, "alertText1": "a", "alertStatus": "normal"}, carState=CRUISE, longitudinalPlan=BY_PLAN,
               longitudinalPlanSP=sla_plan(80), modelV2={"meta": {"desireState": desires(d1=.9)}}, radarState={"leadOne": {"present": True, "dRel": 30.}})
    for field in ALL_FIELDS:
      assert (body[field] is None) is (field in fields), field

  def test_a_stale_radar_blanks_the_radar_lead(self, world):
    assert hud(world, stale=["radarState"], longitudinalPlan=BY_PLAN)["lead"] is None

  def test_a_comma_that_hears_nothing_invents_nothing(self, world):
    body = hud(world, stale=list(SERVICES))
    assert body == {"v": 1, **dict.fromkeys(ALL_FIELDS)}
