"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import socket
import time

import pytest

from openpilot.sunnypilot.mapd.korea.external_source import EXTERNAL_TTL, ExternalNavSource, parse_payload

VALID = {
  "speed_limit_kph": 60,
  "next_speed_limit_kph": 50,
  "next_speed_limit_distance_m": 320,
  "road_name": "테헤란로",
}


def test_parse_valid_payload():
  nav = parse_payload(VALID)
  assert nav.speed_limit_kph == 60.
  assert nav.next_speed_limit_kph == 50.
  assert nav.next_speed_limit_distance_m == 320.
  assert nav.road_name == "테헤란로"
  assert nav.received_at > 0.
  assert nav.raw == VALID


def test_parse_missing_keys_are_zero():
  nav = parse_payload({})
  assert nav.speed_limit_kph == 0.
  assert nav.next_speed_limit_kph == 0.
  assert nav.next_speed_limit_distance_m == 0.
  assert nav.road_name == ""


@pytest.mark.parametrize("value", [-10, 0, 500, "60", None, [60]])
def test_parse_clamps_implausible_speed(value):
  assert parse_payload({"speed_limit_kph": value}).speed_limit_kph == 0.


@pytest.mark.parametrize("value", [-1, 99999, "far"])
def test_parse_clamps_implausible_distance(value):
  assert parse_payload({"next_speed_limit_distance_m": value}).next_speed_limit_distance_m == 0.


def test_parse_truncates_long_road_name():
  nav = parse_payload({"road_name": "가" * 500})
  assert len(nav.road_name) == 64


def test_parse_rejects_non_string_road_name():
  assert parse_payload({"road_name": 123}).road_name == ""


def send(port, payload):
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.sendto(json.dumps(payload).encode(), ("127.0.0.1", port))
  sock.close()


def wait_for(source, timeout=2.):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    nav = source.latest()
    if nav is not None:
      return nav
    time.sleep(0.01)
  return None


@pytest.fixture
def source():
  # port 0 lets the OS pick a free one so the test never collides with a running device
  src = ExternalNavSource(port=0)
  src.start()
  yield src
  src.stop()


def test_latest_is_none_before_anything_arrives(source):
  assert source.latest() is None


def test_datagram_lands(source):
  send(source.port, VALID)
  nav = wait_for(source)
  assert nav is not None
  assert nav.speed_limit_kph == 60.


def test_malformed_datagram_is_ignored(source):
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.sendto(b"not json at all", ("127.0.0.1", source.port))
  sock.sendto(json.dumps([1, 2, 3]).encode(), ("127.0.0.1", source.port))
  sock.close()
  time.sleep(0.2)
  assert source.latest() is None


def test_stale_data_expires(source):
  send(source.port, VALID)
  nav = wait_for(source)
  assert nav is not None
  # rewind the timestamp past the TTL instead of sleeping through it
  source._latest.received_at -= EXTERNAL_TTL + 1.
  assert source.latest() is None
