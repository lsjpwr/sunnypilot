"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import http.client
import ast
import ipaddress
import json
import pathlib
import re
import threading

import pytest

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.params import Params, UnknownKeyName
from openpilot.sunnypilot.system import phone_bridge_settings
from openpilot.sunnypilot.system.phone_bridge_settings import ALLOWED_TITLES, EXCLUDED, PhoneSettings
from openpilot.sunnypilot.system.phone_bridge import (MAX_BODY, PAIR_CODE_PARAM, PAIR_CODE_LIFETIME_S, SERVICES, TOKEN_PARAM, PhoneBridge, in_subnet,
                                                      make_server, new_pair_code, wifi_subnet)


class FakeParams:
  def __init__(self, **values):
    self.values = values
    self.defaults, self.keys = {}, None  # keys None: every key exists

  def check_key(self, key):
    if self.keys is not None and key not in self.keys:
      raise UnknownKeyName(key)

  def get(self, key, return_default=False):
    value = self.values.get(key)
    return self.defaults.get(key) if value is None and return_default else value

  def get_bool(self, key):
    return bool(self.values.get(key))

  def put(self, key, value, block=False):
    assert block, "a write the next request depends on has to land first"
    self.values[key] = value

  def put_bool(self, key, value, block=False):
    self.put(key, value, block)

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


CAPS = {"has_longitudinal_control": True, "has_icbm": False, "pcm_cruise": False}
OFFROAD = {"deviceState": {"started": False}}
PARKED_ON = {"deviceState": {"started": True}, "selfdriveState": {"enabled": False}}
DRIVING = {"deviceState": {"started": True}, "selfdriveState": {"enabled": True}}
SETTING_DEFAULTS = {"MapDataSource": 1, "LongitudinalPersonality": 1, "DriverMonitoringMode": 0, "InitialCruiseSpeedKph": 40}


@pytest.fixture
def settings_world(world, monkeypatch):
  """A paired phone, and what it takes to move the comma: the car's capabilities, params and messages."""
  params, clock, start = world
  params.defaults = dict(SETTING_DEFAULTS)
  params.values["IsMetric"] = True
  caps = dict(CAPS)
  monkeypatch.setattr(phone_bridge_settings, "generate_capabilities", lambda _: caps)

  def pair(messages=OFFROAD):
    sm = FakeSM(**messages)
    phone = start(sm=sm)
    phone.token = phone.pair(new_pair_code(params, clock))[1]["token"]
    return phone, sm

  return params, caps, pair


def settings_of(phone):
  status, body = phone.request("GET", "/v1/settings", token=phone.token)
  assert status == 200 and body["v"] == 1
  return {item["key"]: item for item in body["items"]}


def post(phone, key, value):
  return phone.request("POST", "/v1/settings", {"key": key, "value": value}, token=phone.token)


class TestSettingsList:
  def test_an_item_carries_what_settings_ui_json_says(self, settings_world):
    params, _, pair = settings_world
    params.values["LongitudinalPersonality"] = 2

    item = settings_of(pair()[0])["LongitudinalPersonality"]

    assert item == {"key": "LongitudinalPersonality", "title": "차간 거리", "widget": "multiple_button", "value": 2, "enabled": True, "reason": None,
                    "options": [{"value": 0, "label": "Aggressive"}, {"value": 1, "label": "Standard"}, {"value": 2, "label": "Relaxed"}]}

  def test_a_toggle_has_a_bool_value_and_no_options(self, settings_world):
    params, _, pair = settings_world
    params.values["IsLdwEnabled"] = True

    items = settings_of(pair()[0])

    item = items["IsLdwEnabled"]
    assert (item["title"], item["widget"], item["value"], item["options"]) == ("차선 이탈 경고", "toggle", True, None)
    assert items["AlwaysOnDM"]["value"] is False

  def test_the_list_is_the_allow_list_in_its_order(self, settings_world):
    _, _, pair = settings_world
    assert list(settings_of(pair()[0])) == list(ALLOWED_TITLES)

  def test_without_a_korean_title_the_english_one_goes_out(self, settings_world):
    params, _, _ = settings_world
    items = PhoneSettings(params, {"IsLdwEnabled": "", "IsMetric": "미터법 사용"}).items(True, True)
    assert [item["title"] for item in items] == ["Enable Lane Departure Warnings", "미터법 사용"]

  @pytest.mark.parametrize("key", EXCLUDED)
  def test_the_excluded_are_never_listed(self, settings_world, key):
    _, _, pair = settings_world
    assert key not in settings_of(pair()[0])

  def test_a_key_the_comma_lacks_is_skipped(self, settings_world):
    params, _, pair = settings_world
    params.keys = set(ALLOWED_TITLES) - {"KoreaMapAutoDownload"}

    items = settings_of(pair()[0])

    assert "KoreaMapAutoDownload" not in items and "IsMetric" in items

  def test_an_allowed_key_settings_ui_json_does_not_describe_is_skipped(self, settings_world):
    params, _, _ = settings_world
    assert [item["key"] for item in PhoneSettings(params, {"NoSuchItem": "?", "IsMetric": "미터법 사용"}).items(True, True)] == ["IsMetric"]

  def test_the_list_needs_the_token(self, settings_world):
    _, _, pair = settings_world
    phone, _ = pair()
    assert phone.request("GET", "/v1/settings")[0] == 401
    assert phone.request("POST", "/v1/settings", {"key": "IsMetric", "value": True})[0] == 401


class TestSettingsLocks:
  """What mici's toggles panel enables (selfdrive/ui/mici/layouts/settings/toggles.py), key by key.
  One deliberate difference: mici leaves driver monitoring mode open while driving, the phone follows settings_ui.json and waits for offroad."""

  def test_driver_monitoring_mode_waits_for_offroad(self, settings_world):
    _, _, pair = settings_world
    assert settings_of(pair(OFFROAD)[0])["DriverMonitoringMode"]["enabled"] is True
    item = settings_of(pair(PARKED_ON)[0])["DriverMonitoringMode"]
    assert (item["enabled"], item["reason"]) == (False, "offroad_only")

  def test_a_comma_that_does_not_know_if_it_is_parked_locks_what_waits_for_offroad(self, settings_world):
    _, _, pair = settings_world
    phone, sm = pair(OFFROAD)
    sm.alive["deviceState"] = False
    assert settings_of(phone)["DriverMonitoringMode"]["reason"] == "offroad_only"

  @pytest.mark.parametrize("key", ["KoreaExternalNavEnabled", "KoreaMapAutoDownload"])
  def test_nav_input_and_map_download_need_offroad_and_the_korean_source(self, settings_world, key):
    params, _, pair = settings_world
    assert settings_of(pair(OFFROAD)[0])[key]["enabled"] is True
    assert settings_of(pair(DRIVING)[0])[key]["reason"] == "offroad_only"
    params.values["MapDataSource"] = 0
    assert settings_of(pair(OFFROAD)[0])[key]["reason"] == "requires"

  @pytest.mark.parametrize("key", ["KoreaCameraSpeedEnabled", "KoreaCameraSignalEnabled", "KoreaCameraSectionEnabled", "KoreaCameraZoneEnabled"])
  def test_camera_kinds_need_the_korean_source_and_nothing_else(self, settings_world, key):
    params, _, pair = settings_world
    assert settings_of(pair(DRIVING)[0])[key]["enabled"] is True
    params.values["MapDataSource"] = 0
    assert settings_of(pair(DRIVING)[0])[key]["reason"] == "requires"

  @pytest.mark.parametrize(("longitudinal", "icbm", "enabled"), [(True, False, True), (False, True, True), (False, False, False)])
  def test_speed_bump_slowdown_needs_longitudinal_control_or_icbm(self, settings_world, longitudinal, icbm, enabled):
    _, caps, pair = settings_world
    caps.update(has_longitudinal_control=longitudinal, has_icbm=icbm)
    item = settings_of(pair(DRIVING)[0])["KoreaSpeedBumpEnabled"]
    assert (item["enabled"], item["reason"]) == (enabled, None if enabled else "requires")

  def test_the_following_distance_needs_longitudinal_control(self, settings_world):
    _, caps, pair = settings_world
    caps["has_longitudinal_control"] = False
    assert settings_of(pair(DRIVING)[0])["LongitudinalPersonality"]["reason"] == "requires"

  @pytest.mark.parametrize("key", ["IsMetric", "IsLdwEnabled", "AlwaysOnDM"])
  def test_these_stay_open_while_driving(self, settings_world, key):
    _, _, pair = settings_world
    assert settings_of(pair(DRIVING)[0])[key]["enabled"] is True

  @pytest.mark.parametrize(("rule", "not_engaged", "reason"), [
    ({"type": "not_engaged"}, True, None),
    ({"type": "not_engaged"}, False, "engaged"),
    ({"type": "param_compare", "key": "Level", "op": ">", "value": 0}, True, "requires"),
    ({"type": "param_compare", "key": "Level", "op": "<=", "value": 0}, True, None),
    ({"type": "not", "condition": {"type": "param", "key": "Level", "equals": 0}}, True, "requires"),
    ({"type": "all", "conditions": [{"type": "offroad_only"}, {"type": "capability", "field": "brand", "equals": "rivian"}]}, True, "requires"),
    ({"type": "any", "conditions": [{"type": "capability", "field": "brand", "equals": "rivian"}, {"type": "param", "key": "Level", "equals": 0}]}, True, None),
    ({"type": "a rule from the future"}, True, "requires"),
  ])
  def test_each_rule_kind(self, settings_world, monkeypatch, rule, not_engaged, reason):
    params, caps, _ = settings_world
    params.values["Level"] = 0
    caps["brand"] = "tesla"
    only_item = {"IsMetric": {"key": "IsMetric", "widget": "toggle", "title": "t", "enablement": [rule]}}
    monkeypatch.setattr(phone_bridge_settings, "_definitions", lambda: only_item)

    item = PhoneSettings(params, {"IsMetric": ""}).items(offroad=True, not_engaged=not_engaged)[0]

    assert (item["enabled"], item["reason"]) == (reason is None, reason)

  def test_the_first_rule_that_fails_names_the_reason(self, settings_world, monkeypatch):
    params, _, _ = settings_world
    rules = [{"type": "offroad_only"}, {"type": "param", "key": "MapDataSource", "equals": 9}]
    monkeypatch.setattr(phone_bridge_settings, "_definitions", lambda: {"IsMetric": {"key": "IsMetric", "widget": "toggle", "title": "t", "enablement": rules}})
    assert PhoneSettings(params, {"IsMetric": ""}).items(offroad=False, not_engaged=True)[0]["reason"] == "offroad_only"
    assert PhoneSettings(params, {"IsMetric": ""}).items(offroad=True, not_engaged=True)[0]["reason"] == "requires"


class TestInitialCruiseSpeed:
  def test_the_options_are_mici_pickers_in_kph(self, settings_world):
    _, _, pair = settings_world
    item = settings_of(pair()[0])["InitialCruiseSpeedKph"]
    assert (item["title"], item["widget"], item["value"]) == ("초기 설정 속도", "multiple_button", 40)
    assert [option["value"] for option in item["options"]] == list(range(40, 146, 5))
    assert item["options"][0] == {"value": 40, "label": "40 km/h"}

  def test_in_miles_the_labels_follow_the_unit_and_the_stored_value_stays_kph(self, settings_world):
    params, _, pair = settings_world
    params.values["IsMetric"] = False
    options = settings_of(pair()[0])["InitialCruiseSpeedKph"]["options"]
    assert options[0] == {"value": 40, "label": "25 mph"}
    assert options[1] == {"value": 48, "label": "30 mph"}

  def test_a_saved_speed_between_two_options_shows_the_nearest(self, settings_world):
    params, _, pair = settings_world
    params.values["InitialCruiseSpeedKph"] = 47
    assert settings_of(pair()[0])["InitialCruiseSpeedKph"]["value"] == 45

  def test_it_is_locked_without_longitudinal_control_or_on_a_pcm_cruise_car(self, settings_world):
    _, caps, pair = settings_world
    caps["pcm_cruise"] = True
    assert settings_of(pair()[0])["InitialCruiseSpeedKph"]["reason"] == "requires"
    caps.update(pcm_cruise=False, has_longitudinal_control=False)
    assert settings_of(pair()[0])["InitialCruiseSpeedKph"]["reason"] == "requires"

  def test_it_is_written_in_kph(self, settings_world):
    params, _, pair = settings_world
    params.values["IsMetric"] = False

    status, item = post(pair()[0], "InitialCruiseSpeedKph", 48)

    assert status == 200 and item["value"] == 48 and params.values["InitialCruiseSpeedKph"] == 48


class TestSettingsWrite:
  def test_a_toggle_is_written_and_the_item_comes_back(self, settings_world):
    params, _, pair = settings_world

    status, item = post(pair()[0], "IsLdwEnabled", True)

    assert status == 200 and params.values["IsLdwEnabled"] is True
    assert (item["key"], item["value"], item["enabled"]) == ("IsLdwEnabled", True, True)

  def test_a_choice_is_written_as_the_integer(self, settings_world):
    params, _, pair = settings_world

    status, item = post(pair()[0], "LongitudinalPersonality", 0)

    assert status == 200 and params.values["LongitudinalPersonality"] == 0 and item["value"] == 0

  @pytest.mark.parametrize("key", [*EXCLUDED, "NoSuchParam", "DoNotTrack"])
  def test_what_is_not_on_the_list_is_403_and_stays_as_it_was(self, settings_world, key):
    params, _, pair = settings_world
    assert post(pair()[0], key, True)[0] == 403
    assert key not in params.values

  def test_an_allowed_key_the_comma_lacks_is_403(self, settings_world):
    params, _, pair = settings_world
    params.keys = set(ALLOWED_TITLES) - {"IsLdwEnabled"}
    assert post(pair()[0], "IsLdwEnabled", True)[0] == 403

  def test_a_locked_item_is_409_with_its_reason_and_stays_as_it_was(self, settings_world):
    params, _, pair = settings_world

    status, item = post(pair(DRIVING)[0], "DriverMonitoringMode", 2)

    assert status == 409 and item["reason"] == "offroad_only" and item["enabled"] is False
    assert "DriverMonitoringMode" not in params.values

  def test_the_lock_is_looked_at_again_when_the_write_comes(self, settings_world):
    params, _, pair = settings_world
    phone, sm = pair(OFFROAD)
    assert settings_of(phone)["DriverMonitoringMode"]["enabled"] is True

    sm.data["deviceState"] = reader("deviceState", {"started": True})

    assert post(phone, "DriverMonitoringMode", 2)[0] == 409
    assert "DriverMonitoringMode" not in params.values

  @pytest.mark.parametrize(("key", "value"), [
    ("IsLdwEnabled", 1), ("IsLdwEnabled", "true"), ("IsLdwEnabled", None), ("IsLdwEnabled", [True]),
    ("LongitudinalPersonality", 3), ("LongitudinalPersonality", True), ("LongitudinalPersonality", "1"), ("LongitudinalPersonality", None),
    ("InitialCruiseSpeedKph", 41),
  ])
  def test_a_value_of_the_wrong_form_is_400_and_nothing_is_written(self, settings_world, key, value):
    params, _, pair = settings_world
    assert post(pair()[0], key, value)[0] == 400
    assert key not in params.values

  @pytest.mark.parametrize("body", [b"", b"not json", b"[1]", b'{"value": true}', b'{"key": "IsLdwEnabled"}', b'{"key": 5, "value": true}'])
  def test_a_malformed_request_is_400(self, settings_world, body):
    params, _, pair = settings_world
    phone, _ = pair()
    assert phone.request("POST", "/v1/settings", body, token=phone.token)[0] == 400
    assert "IsLdwEnabled" not in params.values

  def test_a_paired_phone_cannot_reach_an_unlisted_setting_path(self, settings_world):
    _, _, pair = settings_world
    phone, _ = pair()
    assert phone.request("GET", "/v1/settings/IsMetric", token=phone.token)[0] == 404
    assert phone.request("POST", "/v1/setting", {"key": "IsMetric", "value": True}, token=phone.token)[0] == 404


PANEL_FILES = ("selfdrive/ui/mici/layouts/settings/toggles.py",  # the panel
               "selfdrive/ui/layouts/settings/initial_cruise_speed.py")  # where its initial max speed button keeps its key
NOT_A_SETTING = {"ShowDebugInfo", "RecordFrontLock", "MapDataSource"}  # read by the panel, not offered by it


def panel_keys() -> set[str]:
  """Every string in the panel's source that is the name of a param."""
  root = pathlib.Path(__file__).resolve().parents[3]
  known = {key.decode() for key in Params().all_keys()}
  return {node.value for name in PANEL_FILES for node in ast.walk(ast.parse((root / name).read_text(encoding="utf-8")))
          if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in known}


class TestPanelGuard:
  """The allow list and the exclusion list have to say something about every key mici's toggles panel has."""

  def test_every_panel_key_is_classified(self):
    unclassified = panel_keys() - set(ALLOWED_TITLES) - set(EXCLUDED) - NOT_A_SETTING
    assert not unclassified, f"{sorted(unclassified)} are on mici's toggles panel: put each in ALLOWED_TITLES or EXCLUDED (phone_bridge_settings.py)"

  def test_no_listed_key_has_left_the_panel(self):
    gone = (set(ALLOWED_TITLES) | set(EXCLUDED) | NOT_A_SETTING) - panel_keys()
    assert not gone, f"{sorted(gone)} are no longer on mici's toggles panel: take each out of phone_bridge_settings.py"

  def test_a_key_is_not_both_allowed_and_excluded(self):
    assert not set(ALLOWED_TITLES) & set(EXCLUDED)

  def test_every_allowed_key_has_an_item_to_serve(self):
    served = set(phone_bridge_settings.EXTRA_ITEMS) | set(phone_bridge_settings._definitions())
    assert set(ALLOWED_TITLES) <= served
