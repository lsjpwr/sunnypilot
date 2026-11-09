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

from openpilot.sunnypilot.system.phone_bridge import (MAX_BODY, PAIR_CODE_PARAM, PAIR_CODE_S, TOKEN_PARAM, PhoneBridge, in_subnet, make_server,
                                                      new_pair_code, wifi_subnet)


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

    clock.t += PAIR_CODE_S + 1

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
