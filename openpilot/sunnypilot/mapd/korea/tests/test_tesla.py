"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import unittest
import urllib.error

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.tesla import TOKEN_URL, AuthRejected, fetch_destination, parse_destination, refresh_access_token
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeResponse

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


if __name__ == "__main__":
  unittest.main()
