"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import unittest
import urllib.error

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.route import RequestBudget
from openpilot.sunnypilot.mapd.korea.tesla import (POLL_INTERVAL_S, TOKEN_URL, AuthRejected, TeslaDestinationSource, fetch_destination,
                                                   parse_destination, refresh_access_token)
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeClock, FakeResponse

GANGNAM = (37.4979, 127.0276)
CITY_HALL = (37.5665, 126.9780)


def car(lat=GANGNAM[0], lon=GANGNAM[1], name="강남역"):
  """vehicle_data the way Tesla answers it with a route active, trimmed to what matters."""
  return {"response": {"drive_state": {"active_route_destination": name, "active_route_latitude": lat,
                                       "active_route_longitude": lon, "shift_state": "D"}}}


NO_ROUTE = {"response": {"drive_state": {"shift_state": "D"}}}
TOKENS = {"access_token": "ACCESS-2", "refresh_token": "REFRESH-2", "expires_in": 28800, "token_type": "Bearer"}


def http_error(code):
  return urllib.error.HTTPError(tesla.FLEET_API_URL, code, "error", None, None)


class ScriptedOpener:
  """urlopen stand-in. A token request gets `token`; a vehicle_data request gets the next of
  `answers`. An exception in either place is raised instead of answered. `events` can be
  shared with a FakeParams so a test can tell which happened first."""

  def __init__(self, answers=(), token=TOKENS, events=None):
    self.answers = list(answers)
    self.token = token
    self.events = [] if events is None else events
    self.requests = []

  def __call__(self, request, timeout=None):
    self.requests.append(request)
    is_token = request.full_url == TOKEN_URL
    self.events.append("token" if is_token else "vehicle_data")
    answer = self.token if is_token else self.answers.pop(0)
    if isinstance(answer, Exception):
      raise answer
    return FakeResponse(answer)

  def count(self, kind):
    return self.events.count(kind)


CREDENTIALS = {"KoreaTeslaClientId": "CLIENT", "KoreaTeslaRefreshToken": "REFRESH-1", "KoreaTeslaVin": "VIN123",
               "KoreaRouteApiKey": "TMAP"}


class FakeParams:
  """Params stand-in over a plain dict. Writes and removals land in `events`, next to the
  opener's requests, so a test can check what happened in which order."""

  def __init__(self, values=None, events=None):
    self.values = dict(CREDENTIALS if values is None else values)
    self.events = [] if events is None else events

  def get(self, key, return_default=False):
    return self.values.get(key)

  def put(self, key, value):
    self.events.append(f"put {key}")
    self.values[key] = value

  def remove(self, key):
    self.events.append(f"remove {key}")
    self.values.pop(key, None)


class TestParseDestination(unittest.TestCase):
  def test_an_active_route_is_the_destination(self):
    self.assertEqual(parse_destination(car()), (GANGNAM[0], GANGNAM[1], "강남역"))

  def test_no_active_route_is_no_destination(self):
    self.assertIsNone(parse_destination(NO_ROUTE))

  def test_null_coordinates_are_no_destination(self):
    self.assertIsNone(parse_destination(car(lat=None, lon=None)))

  def test_coordinates_as_text_are_no_destination(self):
    self.assertIsNone(parse_destination(car(lat="37.4979", lon="127.0276")))

  def test_a_destination_outside_korea_is_no_destination(self):
    self.assertIsNone(parse_destination(car(lat=35.6762, lon=139.6503)))

  def test_a_long_name_is_cut(self):
    self.assertEqual(len(parse_destination(car(name="가" * 100))[2]), tesla.MAX_PLACE_NAME)

  def test_a_name_that_is_not_text_is_dropped(self):
    self.assertEqual(parse_destination(car(name=12))[2], "")

  def test_an_answer_of_the_wrong_shape_is_no_destination(self):
    for payload in ([], None, {"response": []}, {"response": {"drive_state": "x"}}):
      with self.subTest(payload=payload):
        self.assertIsNone(parse_destination(payload))


class TestRefreshAccessToken(unittest.TestCase):
  def test_returns_the_access_token_and_its_replacement(self):
    self.assertEqual(refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener()), ("ACCESS-2", "REFRESH-2"))

  def test_the_refresh_token_travels_in_the_body_only(self):
    opener = ScriptedOpener()
    refresh_access_token("CLIENT", "REFRESH-1", opener)
    request = opener.requests[0]
    self.assertNotIn("REFRESH-1", request.full_url)
    self.assertIn("grant_type=refresh_token", request.data.decode())
    self.assertIn("refresh_token=REFRESH-1", request.data.decode())

  def test_a_refused_token_is_auth_rejected(self):
    for code in (400, 401):
      with self.subTest(code=code), self.assertRaises(AuthRejected):
        refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener(token=http_error(code)))

  def test_a_server_error_is_not_a_rejection(self):
    """A 5xx says nothing about the token. Calling it a rejection would stop polling until the
    owner re-ran the setup, for an outage on Tesla's side."""
    with self.assertRaises(urllib.error.HTTPError):
      refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener(token=http_error(503)))

  def test_an_answer_without_tokens_is_an_error(self):
    with self.assertRaises(ValueError):
      refresh_access_token("CLIENT", "REFRESH-1", ScriptedOpener(token={"access_token": "ACCESS-2"}))


class TestFetchDestination(unittest.TestCase):
  def test_reads_the_destination_with_a_bearer_header(self):
    opener = ScriptedOpener([car()])
    self.assertEqual(fetch_destination("ACCESS-2", "VIN123", opener), (GANGNAM[0], GANGNAM[1], "강남역"))
    request = opener.requests[0]
    self.assertEqual(request.get_header("Authorization"), "Bearer ACCESS-2")
    self.assertNotIn("ACCESS-2", request.full_url)

  def test_asks_for_location_data(self):
    """Without location_data Tesla leaves the coordinates out of drive_state (fleet-telemetry#392)."""
    opener = ScriptedOpener([car()])
    fetch_destination("ACCESS-2", "VIN123", opener)
    self.assertIn("/api/1/vehicles/VIN123/vehicle_data?endpoints=drive_state%3Blocation_data", opener.requests[0].full_url)

  def test_an_http_error_reaches_the_caller(self):
    with self.assertRaises(urllib.error.HTTPError):
      fetch_destination("ACCESS-2", "VIN123", ScriptedOpener([http_error(408)]))


class SourceTestCase(unittest.TestCase):
  """Drives TeslaDestinationSource.step() one tick at a time on a fake clock."""

  def setUp(self):
    super().setUp()
    self.events = []
    self.clock = FakeClock(1000.)
    self.params = FakeParams(events=self.events)
    self.alerts = []

  def source(self, answers=(), token=TOKENS):
    self.opener = ScriptedOpener(answers, token, self.events)
    return TeslaDestinationSource(opener=self.opener, clock=self.clock)

  def tick(self, source, started=True, seconds=0.):
    self.clock.now += seconds
    source.step(self.params, started, self.alerts.append)

  def nav(self):
    raw = self.params.values.get("NavDestination")
    return None if raw is None else json.loads(raw)


class TestPollingConditions(SourceTestCase):
  def test_parked_asks_nothing(self):
    source = self.source([car()])
    self.tick(source, started=False)
    self.assertEqual(self.opener.requests, [])

  def test_missing_setup_asks_nothing(self):
    for key in ("KoreaTeslaClientId", "KoreaTeslaRefreshToken", "KoreaTeslaVin"):
      with self.subTest(missing=key):
        self.params = FakeParams({k: v for k, v in CREDENTIALS.items() if k != key}, self.events)
        source = self.source([car()])
        self.tick(source)
        self.assertEqual(self.opener.requests, [])

  def test_no_tmap_key_asks_nothing(self):
    """Without it a destination buys no route, only a bill."""
    self.params.values.pop("KoreaRouteApiKey")
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.opener.requests, [])

  def test_asks_once_a_minute(self):
    source = self.source([car(), car()])
    self.tick(source)
    for _ in range(int(POLL_INTERVAL_S) - 1):
      self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_a_new_drive_asks_at_once(self):
    """The first poll of a drive does not wait out the last drive's minute."""
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, started=False, seconds=10.)
    self.tick(source, seconds=10.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_the_daily_cap_stops_asking(self):
    source = self.source([car(), car()])
    source.budget = RequestBudget(cap=1, clock=FakeClock())
    self.tick(source)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_the_daily_cap_is_300(self):
    """Five hours of driving at one request a minute (spec, 비용 절)."""
    self.assertEqual(TeslaDestinationSource().budget.cap, 300)


class TestWriteRules(SourceTestCase):
  def test_the_cars_destination_is_written(self):
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.nav(), {"latitude": GANGNAM[0], "longitude": GANGNAM[1], "place_name": "강남역", "place_details": None})

  def test_the_same_destination_is_not_written_back_after_arrival(self):
    """RouteSource clears it on arrival while the car still shows the route. Writing it back
    would only have RouteSource clear it again, every minute, for as long as the car is parked."""
    source = self.source([car(), car()])
    self.tick(source)
    self.params.remove("NavDestination")  # RouteSource: arrived
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(self.nav())

  def test_a_jitter_under_50_m_is_the_same_destination(self):
    source = self.source([car(), car(lat=GANGNAM[0] + 0.0002)])  # ~22 m north
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.events.count("put NavDestination"), 1)

  def test_a_changed_destination_is_written(self):
    source = self.source([car(), car(*CITY_HALL, name="서울시청")])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual((self.nav()["latitude"], self.nav()["longitude"]), CITY_HALL)

  def test_guidance_ended_in_the_car_clears_our_destination(self):
    source = self.source([car(), NO_ROUTE])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(self.nav())

  def test_guidance_ended_in_the_car_keeps_a_destination_it_did_not_write(self):
    """Something else -- the UDP socket, athenad -- wrote after us. Not the car's to cancel."""
    source = self.source([car(), NO_ROUTE])
    self.tick(source)
    other = json.dumps({"latitude": CITY_HALL[0], "longitude": CITY_HALL[1], "place_name": None, "place_details": None})
    self.params.values["NavDestination"] = other
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.params.values["NavDestination"], other)

  def test_a_new_drive_writes_the_same_destination_again(self):
    """The offroad transition cleared NavDestination. The car still guiding there means the
    route thread should have it again."""
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, started=False, seconds=10.)
    self.params.remove("NavDestination")  # manager: CLEAR_ON_OFFROAD_TRANSITION
    self.tick(source, seconds=10.)
    self.assertIsNotNone(self.nav())


class TestTokens(SourceTestCase):
  def test_the_new_refresh_token_is_saved_before_the_car_is_asked(self):
    """Single use: once Tesla has answered, only the new token works. Had the car's answer
    raised before the save, the device would be left holding a dead one."""
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.params.values["KoreaTeslaRefreshToken"], "REFRESH-2")
    self.assertLess(self.events.index("put KoreaTeslaRefreshToken"), self.events.index("vehicle_data"))

  def test_the_access_token_is_kept_between_polls(self):
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("token"), 1)
    self.assertEqual(self.opener.requests[-1].get_header("Authorization"), "Bearer ACCESS-2")


if __name__ == "__main__":
  unittest.main()
