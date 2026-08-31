"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import socket
import time
import unittest

from openpilot.common.parameterized import parameterized
from openpilot.sunnypilot.mapd.korea.external_source import EXTERNAL_TTL, MAX_DATAGRAM, ExternalNavSource, parse_payload

VALID = {
  "speed_limit_kph": 60,
  "next_speed_limit_kph": 50,
  "next_speed_limit_distance_m": 320,
  "road_name": "테헤란로",
}


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


class TestParsePayload(unittest.TestCase):
  def test_parse_valid_payload(self):
    nav = parse_payload(VALID)
    self.assertEqual(nav.speed_limit_kph, 60.)
    self.assertEqual(nav.next_speed_limit_kph, 50.)
    self.assertEqual(nav.next_speed_limit_distance_m, 320.)
    self.assertEqual(nav.road_name, "테헤란로")
    self.assertGreater(nav.received_at, 0.)
    self.assertEqual(nav.raw, VALID)

  def test_parse_missing_keys_are_zero(self):
    nav = parse_payload({})
    self.assertEqual(nav.speed_limit_kph, 0.)
    self.assertEqual(nav.next_speed_limit_kph, 0.)
    self.assertEqual(nav.next_speed_limit_distance_m, 0.)
    self.assertEqual(nav.road_name, "")

  @parameterized.expand([-10, 0, 500, "60", None, [60], True, False])
  def test_parse_clamps_implausible_speed(self, value):
    self.assertEqual(parse_payload({"speed_limit_kph": value}).speed_limit_kph, 0.)

  @parameterized.expand([-1, 99999, "far"])
  def test_parse_clamps_implausible_distance(self, value):
    self.assertEqual(parse_payload({"next_speed_limit_distance_m": value}).next_speed_limit_distance_m, 0.)

  def test_parse_truncates_long_road_name(self):
    nav = parse_payload({"road_name": "가" * 500})
    self.assertEqual(len(nav.road_name), 64)

  def test_parse_rejects_non_string_road_name(self):
    self.assertEqual(parse_payload({"road_name": 123}).road_name, "")


class TestExternalNavSource(unittest.TestCase):
  def setUp(self):
    super().setUp()
    # port 0 lets the OS pick a free one so the test never collides with a running device
    self.source = ExternalNavSource(port=0)
    self.source.start()
    self.addCleanup(self.source.stop)

  def test_latest_is_none_before_anything_arrives(self):
    self.assertIsNone(self.source.latest())

  def test_datagram_lands(self):
    send(self.source.port, VALID)
    nav = wait_for(self.source)
    self.assertIsNotNone(nav)
    self.assertEqual(nav.speed_limit_kph, 60.)

  def test_malformed_datagram_is_ignored(self):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"not json at all", ("127.0.0.1", self.source.port))
    sock.sendto(json.dumps([1, 2, 3]).encode(), ("127.0.0.1", self.source.port))
    sock.close()
    time.sleep(0.2)
    self.assertIsNone(self.source.latest())

  def test_stale_data_expires(self):
    send(self.source.port, VALID)
    self.assertIsNotNone(wait_for(self.source))
    # rewind the timestamp past the TTL instead of sleeping through it
    self.source._latest.received_at -= EXTERNAL_TTL + 1.
    self.assertIsNone(self.source.latest())

  def test_deeply_nested_json_does_not_kill_loop(self):
    # RecursionError is a RuntimeError, not a ValueError/UnicodeDecodeError -- a naive
    # except clause lets it escape and kill the recv thread. Under MAX_DATAGRAM in size.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"[" * 6000, ("127.0.0.1", self.source.port))
    sock.close()
    send(self.source.port, VALID)
    nav = wait_for(self.source)
    self.assertIsNotNone(nav)
    self.assertEqual(nav.speed_limit_kph, 60.)

  def test_oversize_datagram_does_not_kill_loop(self):
    # A datagram bigger than MAX_DATAGRAM can raise OSError out of recvfrom itself
    # (WSAEMSGSIZE on Windows) -- must not be confused with the socket being closed.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"x" * (MAX_DATAGRAM + 1000), ("127.0.0.1", self.source.port))
    sock.close()
    send(self.source.port, VALID)
    nav = wait_for(self.source)
    self.assertIsNotNone(nav)
    self.assertEqual(nav.speed_limit_kph, 60.)


class TestPortRelease(unittest.TestCase):
  def test_stop_releases_the_port_for_the_next_source(self):
    """mapd_manager switches map sources in place, so the source that comes up after a
    switch has to be able to bind the port the previous one used. A close() alone does not
    free it: a thread blocked in recvfrom holds the file description open, and the bind
    comes back EADDRINUSE -- which lands in korea_main's "continue without external nav"
    path and silently disables the feature until the next reboot."""
    first = ExternalNavSource(port=0)
    first.start()
    port, thread = first.port, first._thread
    first.stop()

    self.assertFalse(thread.is_alive(), "the recv thread outlived stop() and still holds the socket")

    second = ExternalNavSource(port=port)
    second.start()          # must not raise OSError: [Errno 98] Address already in use
    try:
      self.assertEqual(second.port, port)
    finally:
      second.stop()
