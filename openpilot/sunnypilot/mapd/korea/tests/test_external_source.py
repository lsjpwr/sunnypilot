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
from openpilot.sunnypilot.mapd.korea.external_source import (EXTERNAL_TTL, MAX_DATAGRAM, MAX_ITEMS, MAX_VIAS, ExternalNavSource,
                                                            PhoneItem, answer, parse_payload)
from openpilot.sunnypilot.mapd.korea.tesla import CAR_UNKNOWN, MAX_PLACE_NAME

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

  def test_destination_lands(self):
    nav = parse_payload({**VALID, "destination_lat": 37.4979, "destination_lon": 127.0276})
    self.assertEqual(nav.destination, (37.4979, 127.0276))

  def test_no_destination_key_is_none(self):
    self.assertIsNone(parse_payload(VALID).destination)

  def test_destination_outside_korea_is_rejected(self):
    nav = parse_payload({**VALID, "destination_lat": 35.6762, "destination_lon": 139.6503})
    self.assertIsNone(nav.destination)

  def test_zero_destination_means_guidance_ended(self):
    nav = parse_payload({**VALID, "destination_lat": 0, "destination_lon": 0})
    self.assertIsNone(nav.destination)

  def test_non_numeric_destination_is_rejected(self):
    nav = parse_payload({**VALID, "destination_lat": "37.5", "destination_lon": None})
    self.assertIsNone(nav.destination)

  def test_huge_destination_integer_degrades_gracefully(self):
    # JSON integers have no size limit, so a bare integer literal can exceed double range.
    # This must degrade gracefully with the destination as None, not raise OverflowError.
    # One bad field must not drop the entire datagram.
    huge_int = int("9" * 400)
    nav = parse_payload({**VALID, "destination_lat": huge_int, "destination_lon": 127.0276})
    self.assertIsNone(nav.destination)
    self.assertEqual(nav.speed_limit_kph, 60.)  # other fields in the same payload still parse


ITEM = {"k": "cam", "lat": 37.5, "lon": 127.0, "d": 211, "lim": 50, "age": 300, "rear": True, "sig": True}


class TestParseItems(unittest.TestCase):
  def test_an_item_lands_with_every_field(self):
    cam, end = parse_payload({"items": [ITEM, {**ITEM, "k": "sec_end", "d": 900, "lim": 110}]}).items
    self.assertEqual(cam, PhoneItem("cam", 37.5, 127.0, 50, 211., .3, rear=True, signal=True))
    self.assertEqual((end.kind, end.distance_m, end.limit_kph), ("sec_end", 900., 110))

  def test_no_list_is_none_and_an_empty_list_is_nothing_ahead(self):
    self.assertIsNone(parse_payload(VALID).items)
    self.assertIsNone(parse_payload({"items": "cam"}).items)
    self.assertEqual(parse_payload({"items": []}).items, ())

  @parameterized.expand([
    ("unknown kind", {**ITEM, "k": "parking"}),
    ("the section the car is in, which has no place", {"k": "sec", "d": 12000, "lim": 110, "age": 300}),
    ("unhashable kind", {**ITEM, "k": ["cam"]}),
    ("outside Korea", {**ITEM, "lat": 35.68, "lon": 139.65}),
    ("no place", {k: v for k, v in ITEM.items() if k not in ("lat", "lon")}),
    ("text place", {**ITEM, "lat": "37.5"}),
    ("bool place", {**ITEM, "lat": True}),
    ("huge place", {**ITEM, "lat": int("9" * 400)}),
    ("behind", {**ITEM, "d": -1}),
    ("too far", {**ITEM, "d": 20000}),
    ("no age", {**ITEM, "age": -1}),
    ("not an object", "cam"),
    ("null", None),
  ])
  def test_a_bad_item_is_skipped_not_the_datagram(self, _, bad):
    nav = parse_payload({**VALID, "items": [bad, ITEM]})
    self.assertEqual(len(nav.items), 1)
    self.assertEqual(nav.speed_limit_kph, 60.)

  def test_a_limit_out_of_range_is_no_limit(self):
    self.assertEqual(parse_payload({"items": [{**ITEM, "lim": 500}]}).items[0].limit_kph, 0)
    self.assertEqual(parse_payload({"items": [{**ITEM, "k": "bump", "lim": 0}]}).items[0].limit_kph, 0)

  def test_flags_are_set_only_by_true(self):
    item = parse_payload({"items": [{**ITEM, "rear": 1, "sig": "yes"}]}).items[0]
    self.assertFalse(item.rear)
    self.assertFalse(item.signal)

  def test_at_most_ten_items_are_read(self):
    self.assertEqual(len(parse_payload({"items": [ITEM] * 15}).items), MAX_ITEMS)

  def test_ages_are_seconds_and_the_fresher_sdk_counts(self):
    nav = parse_payload({"gps_age_ms": 800, "tmap_age_ms": 9000, "kakao_age_ms": 400})
    self.assertEqual((nav.gps_age_s, nav.sdk_age_s), (.8, .4))
    nav = parse_payload({"tmap_age_ms": -1, "kakao_age_ms": -1})
    self.assertEqual((nav.gps_age_s, nav.sdk_age_s), (None, None))
    self.assertEqual(parse_payload({"tmap_age_ms": 300, "kakao_age_ms": "x"}).sdk_age_s, .3)


class TestParseTrip(unittest.TestCase):
  def test_the_destinations_name_and_vias_land_in_order(self):
    nav = parse_payload({"destination_lat": 37.5665, "destination_lon": 126.978, "destination_name": "서울시청",
                         "via": [{"lat": 37.5547, "lon": 126.9707, "name": "서울역"}, {"lat": 37.5, "lon": 127.0}]})
    self.assertEqual(nav.destination_name, "서울시청")
    self.assertEqual(nav.vias, ((37.5547, 126.9707, "서울역"), (37.5, 127.0, "")))

  def test_a_bad_via_is_skipped_not_the_trip(self):
    vias = [{"lat": 35.6762, "lon": 139.6503}, {"lat": "37.5", "lon": 127.0}, {"lat": True, "lon": 127.0}, "x",
            {"lat": 37.5, "lon": 127.0, "name": 7}]
    self.assertEqual(parse_payload({"via": vias}).vias, ((37.5, 127.0, ""),))

  def test_at_most_five_vias_and_a_name_is_cut(self):
    nav = parse_payload({"via": [{"lat": 37.5, "lon": 127.0}] * 9, "destination_name": "가" * 100})
    self.assertEqual(len(nav.vias), MAX_VIAS)
    self.assertEqual(len(nav.destination_name), MAX_PLACE_NAME)

  def test_no_vias_is_an_empty_trip(self):
    self.assertEqual(parse_payload({"via": "x"}).vias, ())
    self.assertEqual(parse_payload({}).vias, ())


class TestAnswer(unittest.TestCase):
  def test_the_cars_destination_rides_along(self):
    self.assertEqual(json.loads(answer(7, (37.4979, 127.0276, "강남역"), 500)),
                     {"ack": 7, "car": {"lat": 37.4979, "lon": 127.0276, "name": "강남역"}})

  def test_no_route_is_null_and_unknown_is_left_out(self):
    self.assertEqual(json.loads(answer(7, None, 500)), {"ack": 7, "car": None})
    self.assertEqual(json.loads(answer(7, CAR_UNKNOWN, 500)), {"ack": 7})

  def test_never_bigger_than_the_datagram(self):
    """The name goes first, then the place."""
    car = (37.4979, 127.0276, "가" * 64)
    for limit, keys in ((500, {"lat", "lon", "name"}), (60, {"lat", "lon"}), (len('{"seq":7}'), None)):
      with self.subTest(limit=limit):
        out = answer(7, car, limit)
        self.assertLessEqual(len(out), limit)
        self.assertEqual(set(json.loads(out)["car"]) if keys else json.loads(out), keys or {"ack": 7})


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

  def _exchange(self, payload):
    """What comes back to the sender of payload within 0.5 s, or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.addCleanup(sock.close)
    sock.settimeout(.5)
    sock.sendto(json.dumps(payload).encode(), ("127.0.0.1", self.source.port))
    try:
      return json.loads(sock.recv(MAX_DATAGRAM))
    except TimeoutError:
      return None

  def test_a_datagram_with_a_seq_is_answered_to_its_sender(self):
    # the phone app shows the driver whether the comma hears it
    self.assertEqual(self._exchange({**VALID, "seq": 812}), {"ack": 812})

  def test_the_answer_carries_what_the_car_guides_to(self):
    self.source.car = lambda: (37.4979, 127.0276, "강남역")
    self.assertEqual(self._exchange({**VALID, "seq": 9})["car"], {"lat": 37.4979, "lon": 127.0276, "name": "강남역"})

  @parameterized.expand([("none", None), ("bool", True), ("negative", -1), ("float", 1.5), ("string", "812")])
  def test_no_answer_without_a_plain_seq(self, _, seq):
    payload = VALID if seq is None else {**VALID, "seq": seq}
    self.assertIsNone(self._exchange(payload))
    self.assertIsNotNone(self.source.latest())  # the datagram itself still lands

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
