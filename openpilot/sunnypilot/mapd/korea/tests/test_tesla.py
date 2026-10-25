"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import datetime
from pathlib import Path
import ssl
import sys
import types
import tempfile
import unittest
import urllib.error
from unittest import mock

from openpilot.sunnypilot.mapd.korea import tesla
from openpilot.sunnypilot.mapd.korea.fleet_budget import FleetBudget
from openpilot.sunnypilot.mapd.korea.route import RequestBudget
from openpilot.sunnypilot.mapd.korea.tesla import (CAR_UNKNOWN, ECHO_WINDOW_S, OWNER_API_URL, OWNER_TOKEN_URL, POLL_INTERVAL_S, SEND_RETRY_S,
                                                   SEND_TRIES, TOKEN_URL, AuthRejected, TeslaDestinationSource, Trip, VehicleNotFound,
                                                   fetch_destination, owner_fetch_destination, owner_refresh_access_token, owner_vehicle_id,
                                                   parse_destination, refresh_access_token, same_trip)
from openpilot.sunnypilot.mapd.korea.tests.test_camera_refresh import OneShotStop
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeClock, FakeResponse

GANGNAM = (37.4979, 127.0276)
CITY_HALL = (37.5665, 126.9780)


def car(lat=GANGNAM[0], lon=GANGNAM[1], name="강남역"):
  """vehicle_data the way Tesla answers it with a route active, trimmed to what matters."""
  return {"response": {"drive_state": {"active_route_destination": name, "active_route_latitude": lat,
                                       "active_route_longitude": lon, "shift_state": "D"}}}


NO_ROUTE = {"response": {"drive_state": {"shift_state": "D"}}}
TOKENS = {"access_token": "ACCESS-2", "refresh_token": "REFRESH-2", "expires_in": 28800, "token_type": "Bearer"}
OWNER_TOKENS = {"access_token": "OWNER-ACCESS-2", "refresh_token": "OWNER-2", "expires_in": 28800, "token_type": "Bearer"}
OWNER_ID = 1492931520123456
# /api/1/products the way the owner API answers it: energy products carry no VIN.
PRODUCTS = {"response": [{"energy_site_id": 7, "resource_type": "battery"},
                         {"id": OWNER_ID, "vehicle_id": 99, "vin": "VIN123", "display_name": "Y"}], "count": 2}


def http_error(code):
  return urllib.error.HTTPError(tesla.FLEET_API_URL, code, "error", None, None)


TOOK = {"response": {"result": True, "reason": ""}}


class ScriptedOpener:
  """urlopen stand-in. A token request (either API) gets `token`; /api/1/products gets
  `products`; a command gets the next of `commands`, and TOOK once they run out; a
  vehicle_data request gets the next of `answers`. An exception in any place is raised
  instead of answered. `events` can be shared with a FakeParams so a test can tell which
  happened first; `contexts` records the TLS context each request asked for."""

  def __init__(self, answers=(), token=TOKENS, events=None, products=PRODUCTS, commands=()):
    self.answers = list(answers)
    self.token = token
    self.products = products
    self.commands = list(commands)
    self.events = [] if events is None else events
    self.requests = []
    self.contexts = []

  def __call__(self, request, timeout=None, context=None):
    self.requests.append(request)
    self.contexts.append(context)
    if request.full_url in (TOKEN_URL, OWNER_TOKEN_URL):
      kind, answer = "token", self.token
    elif request.full_url.endswith("/api/1/products"):
      kind, answer = "products", self.products
    elif "/command/" in request.full_url:
      kind, answer = request.full_url.rsplit("/", 1)[1], self.commands.pop(0) if self.commands else TOOK
    else:
      kind, answer = "vehicle_data", self.answers.pop(0)
    self.events.append(kind)
    if isinstance(answer, Exception):
      raise answer
    return FakeResponse(answer)

  def count(self, kind):
    return self.events.count(kind)


CREDENTIALS = {"KoreaTeslaClientId": "CLIENT", "KoreaTeslaRefreshToken": "REFRESH-1", "KoreaTeslaVin": "VIN123"}
OWNER_CREDENTIALS = {"KoreaTeslaOwnerRefreshToken": "OWNER-1", "KoreaTeslaVin": "VIN123"}


class FakeParams:
  """Params stand-in over a plain dict. Writes and removals land in `events`, next to the
  opener's requests, so a test can check what happened in which order."""

  def __init__(self, values=None, events=None):
    self.values = dict(CREDENTIALS if values is None else values)
    self.events = [] if events is None else events
    self.blocking = set()  # keys written with block=True, i.e. on flash when put() returns

  def get(self, key, return_default=False):
    return self.values.get(key)

  def put(self, key, value, block=False):
    self.events.append(f"put {key}")
    if block:
      self.blocking.add(key)
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


class TestOwnerRefresh(unittest.TestCase):
  def test_refreshes_the_way_the_tesla_app_does(self):
    """NaviToTesla's refresh (2026-09): JSON to auth.tesla.com as the ownerapi client."""
    opener = ScriptedOpener(token=OWNER_TOKENS)
    self.assertEqual(owner_refresh_access_token("OWNER-1", opener), ("OWNER-ACCESS-2", "OWNER-2"))
    request = opener.requests[0]
    self.assertEqual(request.full_url, OWNER_TOKEN_URL)
    self.assertEqual(request.get_method(), "POST")
    self.assertEqual(request.get_header("Content-type"), "application/json")
    self.assertEqual(json.loads(request.data), {"grant_type": "refresh_token", "client_id": "ownerapi",
                                                "refresh_token": "OWNER-1", "scope": "openid email offline_access"})
    self.assertNotIn("OWNER-1", request.full_url)

  def test_the_refresh_goes_over_tls_1_3(self):
    """Below 1.3 auth.tesla.com mints a Fleet token, which owner-api refuses with 403."""
    opener = ScriptedOpener(token=OWNER_TOKENS)
    owner_refresh_access_token("OWNER-1", opener)
    self.assertEqual(opener.contexts[0].minimum_version, ssl.TLSVersion.TLSv1_3)

  def test_a_refused_owner_token_is_auth_rejected(self):
    """Used already -- by NaviToTesla, if the two share one -- or revoked."""
    for code in (400, 401):
      with self.subTest(code=code), self.assertRaises(AuthRejected):
        owner_refresh_access_token("OWNER-1", ScriptedOpener(token=http_error(code)))


class TestOwnerVehicleId(unittest.TestCase):
  def test_finds_the_car_by_vin(self):
    opener = ScriptedOpener()
    self.assertEqual(owner_vehicle_id("OWNER-ACCESS-2", "VIN123", opener), str(OWNER_ID))
    request = opener.requests[0]
    self.assertEqual(request.full_url, OWNER_API_URL + "/api/1/products")
    self.assertEqual(request.get_header("Authorization"), "Bearer OWNER-ACCESS-2")

  def test_a_vin_not_on_the_account_is_vehicle_not_found(self):
    with self.assertRaises(VehicleNotFound):
      owner_vehicle_id("OWNER-ACCESS-2", "VIN999", ScriptedOpener())

  def test_an_id_that_is_not_an_integer_is_never_used(self):
    """The id goes into a URL path, and the answer is untrusted."""
    for bad in ("1/../2", 1.5, True, None):
      with self.subTest(id=bad), self.assertRaises(VehicleNotFound):
        owner_vehicle_id("OWNER-ACCESS-2", "VIN123", ScriptedOpener(products={"response": [{"id": bad, "vin": "VIN123"}]}))

  def test_an_answer_of_the_wrong_shape_is_vehicle_not_found(self):
    for products in ({"response": None}, {"response": ["x"]}, {}):
      with self.subTest(products=products), self.assertRaises(VehicleNotFound):
        owner_vehicle_id("OWNER-ACCESS-2", "VIN123", ScriptedOpener(products=products))

  def test_a_hand_typed_vin_matches_in_any_case(self):
    """Typed on a phone: a terminal keyboard does not capitalize, and a paste can carry a space."""
    for vin in ("vin123", " VIN123 "):
      with self.subTest(vin=vin):
        self.assertEqual(owner_vehicle_id("OWNER-ACCESS-2", vin, ScriptedOpener()), str(OWNER_ID))


class TestOwnerFetchDestination(unittest.TestCase):
  def test_reads_the_destination_from_owner_api_by_id(self):
    opener = ScriptedOpener([car()])
    self.assertEqual(owner_fetch_destination("OWNER-ACCESS-2", str(OWNER_ID), opener), (GANGNAM[0], GANGNAM[1], "강남역"))
    request = opener.requests[0]
    self.assertEqual(request.full_url, f"{OWNER_API_URL}/api/1/vehicles/{OWNER_ID}/vehicle_data?endpoints=drive_state%3Blocation_data")
    self.assertEqual(request.get_header("Authorization"), "Bearer OWNER-ACCESS-2")


class SourceTestCase(unittest.TestCase):
  """Drives TeslaDestinationSource.step() one tick at a time on a fake clock."""

  def setUp(self):
    super().setUp()
    directory = tempfile.TemporaryDirectory()
    self.addCleanup(directory.cleanup)
    self.budget_path = Path(directory.name) / "fleet_budget.json"
    self.today = datetime.date(2026, 10, 24)
    self.events = []
    self.clock = FakeClock(1000.)
    self.params = FakeParams(events=self.events)
    self.alerts = []

  def source(self, answers=(), token=TOKENS, products=PRODUCTS, commands=()):
    self.opener = ScriptedOpener(answers, token, self.events, products, commands)
    return TeslaDestinationSource(opener=self.opener, clock=self.clock, phone_connected=lambda: True,
                                  monthly=FleetBudget(self.budget_path, today=lambda: self.today))

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

  def test_disconnected_phone_asks_nothing(self):
    source = self.source([car()])
    source.phone_connected = lambda: False
    self.tick(source)
    self.assertEqual(self.opener.requests, [])

  def test_reconnecting_phone_reads_without_a_route_key(self):
    source = self.source([car()])
    source.phone_connected = lambda: False
    self.tick(source)
    source.phone_connected = lambda: True
    self.tick(source)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_asks_once_a_minute(self):
    source = self.source([car(), car()])
    self.tick(source)
    for _ in range(int(POLL_INTERVAL_S) - 1):
      self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_disconnect_during_token_refresh_stops_before_reading_the_car(self):
    connected = [True]
    self.opener = ScriptedOpener([car()], events=self.events)

    def disconnect(request, **kwargs):
      response = self.opener(request, **kwargs)
      connected[0] = False
      return response

    source = TeslaDestinationSource(opener=disconnect, clock=self.clock, phone_connected=lambda: connected[0],
                                    monthly=FleetBudget(self.budget_path, today=lambda: self.today))
    self.tick(source)
    self.assertEqual(self.opener.count("vehicle_data"), 0)

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


class TestFleetLimits(SourceTestCase):
  def test_failed_read_is_reserved_before_network_and_survives_restart(self):
    self.assertEqual(FleetBudget(self.budget_path, today=lambda: self.today).reserve(8998), 0)
    source = self.source([http_error(503)])
    self.tick(source)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    restarted = self.source([car()])
    self.tick(restarted)
    self.assertFalse(any("/vehicle_data?" in request.full_url for request in self.opener.requests))
    self.assertEqual(restarted.fleet_status(), 3)

  def test_daily_cap_is_checked_before_each_via(self):
    source = self.source([NO_ROUTE])
    source.commands = RequestBudget(cap=1, clock=self.clock)
    source.want(Trip(PHONE.destination, (VIA,)))
    self.tick(source)
    self.assertEqual(sum("/command/" in request.full_url for request in self.opener.requests), 1)
    self.assertEqual(source.fleet_status(), 2)

  def test_read_does_not_clear_command_daily_stop_until_rollover(self):
    source = self.source([NO_ROUTE, NO_ROUTE, NO_ROUTE])
    source.budget = RequestBudget(cap=300, clock=self.clock)
    source.commands = RequestBudget(cap=1, clock=self.clock)
    source.want(PHONE)
    self.tick(source)
    self.assertEqual(source.fleet_status(), 2)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.fleet_status(), 2)
    self.tick(source, seconds=86400)
    self.assertEqual(source.fleet_status(), 0)
    restarted = self.source([NO_ROUTE])
    self.tick(restarted)
    self.assertEqual(restarted.fleet_status(), 0)

  def test_read_daily_stop_blocks_new_trip_with_cached_access_token(self):
    source = self.source([NO_ROUTE, NO_ROUTE])
    source.budget = RequestBudget(cap=1, clock=self.clock)
    self.tick(source)
    self.assertEqual(source.fleet_status(), 2)
    source.want(PHONE)
    self.tick(source, seconds=1)
    self.assertFalse(any('/command/' in request.full_url for request in self.opener.requests))
    self.assertEqual(source.fleet_status(), 2)

  def test_credential_change_does_not_clear_daily_stop_before_recovery(self):
    source = self.source([NO_ROUTE])
    source.commands = RequestBudget(cap=1, clock=self.clock)
    source.want(PHONE)
    self.tick(source)
    self.assertEqual(source.fleet_status(), 2)
    self.params.values['KoreaTeslaRefreshToken'] = 'NEW-REFRESH'
    self.opener.token = http_error(503)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.fleet_status(), 2)
    self.assertEqual(sum('/command/' in request.full_url for request in self.opener.requests), 1)

  def test_read_monthly_stop_blocks_commands_even_with_one_unit_left(self):
    source = self.source([NO_ROUTE, NO_ROUTE])
    self.tick(source)
    self.assertEqual(source.monthly.reserve(8997), 0)
    source.want(PHONE)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertFalse(any('/command/' in request.full_url for request in self.opener.requests))
    self.assertEqual(source.fleet_status(), 3)

  def test_fallback_checks_each_daily_and_monthly_reservation(self):
    for monthly, expected in ((0, 2), (8997, 3)):
      with self.subTest(monthly=monthly):
        self.setUp()
        source = self.source([NO_ROUTE], commands=[REFUSED])
        self.assertEqual(source.monthly.reserve(monthly), 0)
        if not monthly:
          source.commands = RequestBudget(cap=1, clock=self.clock)
        source.want(PHONE)
        self.tick(source)
        self.assertEqual(sum("/command/" in request.full_url for request in self.opener.requests), 1)
        self.assertEqual(source.fleet_status(), expected)

  def test_read_and_every_via_share_the_monthly_ceiling(self):
    source = self.source([NO_ROUTE])
    self.assertEqual(source.monthly.reserve(8996), 0)
    source.want(Trip(PHONE.destination, (VIA, (*GANGNAM, "B"))))
    self.tick(source)
    self.assertEqual(sum("/command/" in request.full_url for request in self.opener.requests), 2)
    self.assertEqual(source.fleet_status(), 3)

  def test_failed_command_is_reserved_before_network_and_counted_on_retry(self):
    source = self.source([NO_ROUTE], commands=[http_error(503)])
    opener = self.opener

    def network(request, **kwargs):
      if "/vehicle_data?" in request.full_url:
        self.assertEqual(source.monthly.reserve(8999), 3)  # the read's two units are already durable
      elif "/command/" in request.full_url:
        self.assertEqual(source.monthly.reserve(8998), 3)  # read plus at least one command
      return opener(request, **kwargs)

    source._opener = network  # external HTTP boundary
    source.want(PHONE)
    self.tick(source)
    self.tick(source, seconds=SEND_RETRY_S)
    self.assertEqual(source.monthly.reserve(8996), 0)
    self.assertEqual(source.monthly.reserve(1), 3)

  def test_monthly_and_storage_stop_cached_token_commands_after_a_failed_poll(self):
    for stopped in (3, 4):
      with self.subTest(stopped=stopped):
        self.setUp()
        source = self.source([NO_ROUTE, NO_ROUTE])
        self.tick(source)
        if stopped == 3:
          self.assertEqual(source.monthly.reserve(8998), 0)
        else:
          self.budget_path.write_text('corrupt')
        count = len(self.opener.requests)
        source.want(PHONE)
        self.tick(source, seconds=POLL_INTERVAL_S)
        self.assertEqual(len(self.opener.requests), count)
        self.assertEqual(source.fleet_status(), stopped)

  def test_production_storage_failures_prevent_reads_and_cached_token_commands(self):
    for boundary in ('pathlib.Path.read_text', 'tempfile.NamedTemporaryFile', 'os.replace', 'os.fsync'):
      with self.subTest(boundary=boundary):
        self.setUp()
        source = self.source([NO_ROUTE, NO_ROUTE])
        self.tick(source)
        count = len(self.opener.requests)
        source.want(PHONE)
        with mock.patch(boundary, side_effect=OSError("storage unavailable")):
          self.tick(source, seconds=POLL_INTERVAL_S)
        self.assertEqual(len(self.opener.requests), count)
        self.assertEqual(source.fleet_status(), 4)

  def test_refused_via_stops_remaining_commands_and_reports_refusal(self):
    source = self.source([NO_ROUTE], commands=[TOOK, REFUSED])
    source.want(Trip(PHONE.destination, (VIA, (*GANGNAM, "B"))))
    self.tick(source)
    self.assertEqual(sum("/command/" in request.full_url for request in self.opener.requests), 2)
    self.assertEqual(source.fleet_status(), 1)

  def test_month_and_storage_recovery_report_available_and_resume_commands(self):
    for stopped in (3, 4):
      with self.subTest(stopped=stopped):
        self.setUp()
        source = self.source([NO_ROUTE, NO_ROUTE, NO_ROUTE])
        self.tick(source)
        if stopped == 3:
          self.assertEqual(source.monthly.reserve(8998), 0)
        else:
          self.budget_path.write_text('corrupt')
        source.want(PHONE)
        self.tick(source, seconds=POLL_INTERVAL_S)
        self.assertEqual(source.fleet_status(), stopped)
        if stopped == 3:
          self.today = datetime.date(2026, 11, 1)
        else:
          self.budget_path.write_text('{"month":"2026-10","units":2}')  # repair known synthetic state
        self.tick(source, seconds=POLL_INTERVAL_S)
        self.assertEqual(source.fleet_status(), 0)
        self.assertEqual(sum('/command/' in request.full_url for request in self.opener.requests), 1)


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

  def test_guidance_ended_in_the_car_clears_our_destination_on_the_second_empty_answer(self):
    """One empty answer can be a glitch, and the phone app ends its guidance on this."""
    source = self.source([car(), NO_ROUTE, NO_ROUTE])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNotNone(self.nav())
    self.assertEqual(source.car(), (GANGNAM[0], GANGNAM[1], "강남역"))
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(self.nav())
    self.assertIsNone(source.car())

  def test_guidance_ended_in_the_car_keeps_a_destination_it_did_not_write(self):
    """Something else -- the UDP socket, athenad -- wrote after us. Not the car's to cancel."""
    source = self.source([car(), NO_ROUTE, NO_ROUTE])
    self.tick(source)
    other = json.dumps({"latitude": CITY_HALL[0], "longitude": CITY_HALL[1], "place_name": None, "place_details": None})
    self.params.values["NavDestination"] = other
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.params.values["NavDestination"], other)

  def test_the_car_back_on_the_same_route_after_one_empty_answer_is_no_change(self):
    source = self.source([car(), NO_ROUTE, car()])
    for seconds in (0., POLL_INTERVAL_S, POLL_INTERVAL_S):
      self.tick(source, seconds=seconds)
    self.assertEqual(self.events.count("put NavDestination"), 1)

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
    # Params.put only queues the write unless block=True, so "before" must mean on flash.
    self.assertIn("KoreaTeslaRefreshToken", self.params.blocking)

  def test_the_access_token_is_kept_between_polls(self):
    source = self.source([car(), car()])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("token"), 1)
    self.assertEqual(self.opener.requests[-1].get_header("Authorization"), "Bearer ACCESS-2")


class TestFailures(SourceTestCase):
  def assert_asks_again_after(self, error, seconds):
    source = self.source([error, car()])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=seconds - 1.)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("vehicle_data"), 2)
    self.assertIsNotNone(self.nav())

  def test_an_unavailable_car_is_asked_again_next_minute(self):
    self.assert_asks_again_after(http_error(408), POLL_INTERVAL_S)

  def test_a_server_error_is_asked_again_next_minute(self):
    self.assert_asks_again_after(http_error(503), POLL_INTERVAL_S)

  def test_a_network_error_is_asked_again_next_minute(self):
    self.assert_asks_again_after(urllib.error.URLError("no route to host"), POLL_INTERVAL_S)

  def test_too_many_requests_waits_five_minutes(self):
    self.assert_asks_again_after(http_error(429), tesla.RATE_LIMIT_PAUSE_S)


class TestAuth(SourceTestCase):
  def test_a_refused_refresh_token_raises_the_alert_and_stops_asking(self):
    source = self.source([car()], token=http_error(400))
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts, [True])
    self.assertEqual(self.opener.count("token"), 1)
    self.assertEqual(self.opener.count("vehicle_data"), 0)

  def test_a_new_token_from_setup_is_tried_and_clears_the_alert(self):
    source = self.source([car()], token=http_error(400))
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.opener.token = TOKENS
    self.params.values["KoreaTeslaRefreshToken"] = "REFRESH-FROM-SETUP"
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts, [True, False])
    self.assertIsNotNone(self.nav())

  def test_a_grant_without_location_raises_the_alert(self):
    """403: the owner took vehicle_location away, or never gave it. Asking again changes nothing."""
    source = self.source([http_error(403), car()])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts[-1], True)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_an_expired_access_token_is_refreshed_on_the_next_tick(self):
    """Usually the first poll of a drive: waiting a minute to retry would delay the route."""
    source = self.source([car(), http_error(401), car(*CITY_HALL)])
    self.tick(source)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("token"), 2)
    self.assertEqual((self.nav()["latitude"], self.nav()["longitude"]), CITY_HALL)
    self.assertNotIn(True, self.alerts)

  def test_a_token_refused_right_after_refresh_raises_the_alert(self):
    """A token minted this tick and refused anyway will not be accepted on a retry -- and
    retrying every tick would spend the day's cap in five minutes."""
    source = self.source([http_error(401), car()])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.alerts[-1], True)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_any_other_lasting_4xx_raises_the_alert_and_stops_asking(self):
    """No VIN on the account, no partner registration, the wrong region, unpaid billing:
    none of it changes by asking again, and every ask is billed."""
    for code in (400, 402, 404, 412, 421):
      with self.subTest(code=code):
        self.setUp()
        source = self.source([http_error(code), car()])
        with self.assertLogs(tesla.LOG, level="WARNING"):
          self.tick(source)
        self.tick(source, seconds=POLL_INTERVAL_S)
        self.assertEqual(self.opener.count("vehicle_data"), 1)
        self.assertIs(self.alerts[-1], True)


class TestOwnerMode(SourceTestCase):
  def setUp(self):
    super().setUp()
    self.params = FakeParams(OWNER_CREDENTIALS, self.events)

  def test_an_owner_token_and_vin_read_the_car_through_the_owner_api(self):
    source = self.source([car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual([r.full_url for r in self.opener.requests],
                     [OWNER_TOKEN_URL, OWNER_API_URL + "/api/1/products",
                      f"{OWNER_API_URL}/api/1/vehicles/{OWNER_ID}/vehicle_data?endpoints=drive_state%3Blocation_data"])
    self.assertEqual(self.nav()["place_name"], "강남역")

  def test_the_new_owner_refresh_token_is_saved_before_anything_else_is_asked(self):
    source = self.source([car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual(self.params.values["KoreaTeslaOwnerRefreshToken"], "OWNER-2")
    self.assertIn("KoreaTeslaOwnerRefreshToken", self.params.blocking)
    self.assertLess(self.events.index("put KoreaTeslaOwnerRefreshToken"), self.events.index("products"))

  def test_the_car_is_looked_up_once_per_access_token(self):
    source = self.source([car(), car()], token=OWNER_TOKENS)
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("products"), 1)
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_no_vin_asks_nothing(self):
    self.params.values.pop("KoreaTeslaVin")
    source = self.source([car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual(self.opener.requests, [])

  def test_a_vin_not_on_the_account_raises_the_alert_and_stops_asking(self):
    self.params.values["KoreaTeslaVin"] = "VIN999"
    source = self.source([car()], token=OWNER_TOKENS)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIs(self.alerts[-1], True)
    self.assertEqual(self.opener.count("products"), 1)
    self.assertEqual(self.opener.count("vehicle_data"), 0)

  def test_fixing_only_the_vin_asks_again(self):
    """Refused credentials are remembered as a whole, so a VIN fixed by hand over ssh is tried
    on the next poll -- no restart, no new token."""
    self.params.values["KoreaTeslaVin"] = "VIN999"
    source = self.source([car()], token=OWNER_TOKENS)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.params.values["KoreaTeslaVin"] = "VIN123"
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNotNone(self.nav())
    self.assertIs(self.alerts[-1], False)

  def test_owner_api_403_raises_the_alert(self):
    """The account is off the owner API, or auth.tesla.com minted a Fleet token anyway."""
    source = self.source([http_error(403), car()], token=OWNER_TOKENS)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIs(self.alerts[-1], True)
    self.assertEqual(self.opener.count("vehicle_data"), 1)

  def test_new_credentials_are_never_judged_by_the_old_access_token(self):
    """Another account's token and VIN written within the old access token's hours: refresh
    first, or the new VIN is looked up under the old account and refused."""
    source = self.source([car(), car()], token=OWNER_TOKENS)
    self.tick(source)
    self.params.values.update({"KoreaTeslaOwnerRefreshToken": "OWNER-OTHER", "KoreaTeslaVin": "VIN456"})
    self.opener.products = {"response": [{"id": 7, "vin": "VIN456"}]}
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.count("token"), 2)
    self.assertEqual(self.opener.requests[-1].full_url, f"{OWNER_API_URL}/api/1/vehicles/7/vehicle_data?endpoints=drive_state%3Blocation_data")
    self.assertNotIn(True, self.alerts)


class TestModeChoice(SourceTestCase):
  def test_fleet_wins_when_both_are_set(self):
    """Official, and its token is not the whole account. Passes before Task 2 too: it pins the
    order once the owner mode exists."""
    self.params.values["KoreaTeslaOwnerRefreshToken"] = "OWNER-1"
    source = self.source([car()])
    self.tick(source)
    self.assertEqual(self.opener.requests[0].full_url, TOKEN_URL)
    self.assertTrue(self.opener.requests[-1].full_url.startswith(tesla.FLEET_API_URL))
    self.assertEqual(self.params.values["KoreaTeslaOwnerRefreshToken"], "OWNER-1")

  def test_setting_up_fleet_later_drops_the_owner_access_token(self):
    """The two APIs do not take each other's tokens."""
    self.params = FakeParams(OWNER_CREDENTIALS, self.events)
    source = self.source([car(), car()], token=OWNER_TOKENS)
    self.tick(source)
    self.assertEqual(self.opener.count("products"), 1)
    self.params.values.update({"KoreaTeslaClientId": "CLIENT", "KoreaTeslaRefreshToken": "REFRESH-1"})  # tesla_setup ran
    self.opener.token = TOKENS
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(self.opener.requests[-2].full_url, TOKEN_URL)
    self.assertEqual(self.opener.requests[-1].get_header("Authorization"), "Bearer ACCESS-2")

  def test_a_refused_fleet_login_never_falls_back_to_the_owner_api(self):
    self.params.values["KoreaTeslaOwnerRefreshToken"] = "OWNER-1"
    source = self.source([car()], token=http_error(400))
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertNotIn(OWNER_TOKEN_URL, [r.full_url for r in self.opener.requests])
    self.assertIs(self.alerts[-1], True)


PHONE = Trip((CITY_HALL[0], CITY_HALL[1], "서울시청"))
VIA = (37.5547, 126.9707, "서울역")
REFUSED = {"response": {"result": False, "reason": "not supported"}}


class TestCarForThePhone(SourceTestCase):
  def test_unknown_until_the_first_answer_then_the_cars_destination(self):
    source = self.source([car()])
    self.assertEqual(source.car(), CAR_UNKNOWN)
    self.tick(source)
    self.assertEqual(source.car(), (GANGNAM[0], GANGNAM[1], "강남역"))

  def test_unknown_again_on_a_new_drive(self):
    source = self.source([car()])
    self.tick(source)
    self.tick(source, started=False, seconds=10.)
    self.tick(source, seconds=10.)
    self.assertEqual(source.car(), CAR_UNKNOWN)  # the answer to this drive's first poll is still to come

  def test_a_failed_poll_says_nothing_about_the_car(self):
    source = self.source([http_error(503)])
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.assertEqual(source.car(), CAR_UNKNOWN)

  def test_guides_to_is_the_cars_destination_within_a_jitter(self):
    source = self.source([car()])
    self.tick(source)
    self.assertTrue(source.guides_to((GANGNAM[0] + 0.0002, GANGNAM[1])))
    self.assertFalse(source.guides_to(CITY_HALL))

  def test_an_unavailable_car_is_omitted_until_a_fresh_answer(self):
    source = self.source([car(), http_error(503), car()])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.car(), CAR_UNKNOWN)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.car(), (*GANGNAM, "강남역"))
    self.assertEqual(self.events.count("put NavDestination"), 1)

  def test_disconnect_omits_the_previous_observation_until_the_next_minute_read(self):
    source = self.source([car(), car()])
    self.tick(source)
    source.phone_connected = lambda: False
    self.tick(source, seconds=1.)
    self.assertEqual(source.car(), CAR_UNKNOWN)
    source.phone_connected = lambda: True
    self.tick(source, seconds=1.)
    self.assertEqual(source.car(), CAR_UNKNOWN)
    self.assertEqual(self.opener.count("vehicle_data"), 1)
    self.tick(source, seconds=POLL_INTERVAL_S - 2.)
    self.assertEqual(source.car(), (*GANGNAM, "강남역"))
    self.assertEqual(self.opener.count("vehicle_data"), 2)

  def test_a_cached_car_end_expires_without_another_fleet_answer(self):
    source = self.source([NO_ROUTE, NO_ROUTE])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(source.car())
    self.clock.now += POLL_INTERVAL_S + 1.
    self.assertEqual(source.car(), CAR_UNKNOWN)

  def test_unavailable_reads_break_the_consecutive_car_end_observations(self):
    source = self.source([car(), NO_ROUTE, http_error(503), NO_ROUTE, NO_ROUTE])
    self.tick(source)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.car(), (*GANGNAM, "강남역"))
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(source.car())


class TestSendToTheCar(SourceTestCase):
  def commands(self):
    return [(r.full_url.rsplit("/", 1)[1], json.loads(r.data)) for r in self.opener.requests if "/command/" in r.full_url]

  def test_the_phones_trip_goes_to_the_car_as_coordinates(self):
    source = self.source([NO_ROUTE])
    source.want(PHONE)
    self.tick(source)
    self.assertEqual(self.commands(), [("navigation_gps_request", {"lat": CITY_HALL[0], "lon": CITY_HALL[1], "order": 1})])
    request = [r for r in self.opener.requests if "/command/" in r.full_url][0]
    self.assertEqual(request.full_url, f"{tesla.FLEET_API_URL}/api/1/vehicles/VIN123/command/navigation_gps_request")
    self.assertEqual(request.get_header("Authorization"), "Bearer ACCESS-2")

  def test_nothing_is_sent_before_the_drives_first_answer(self):
    """The car may already guide there: what it has is known first."""
    source = self.source([http_error(503)])
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.assertEqual(self.commands(), [])

  def test_the_same_trip_is_sent_once(self):
    source = self.source([NO_ROUTE])
    source.want(PHONE)
    self.tick(source)
    for _ in range(5):
      source.want(Trip((CITY_HALL[0] + 0.0001, CITY_HALL[1], "서울시청")))  # the next datagrams, a jitter apart
      self.tick(source, seconds=1.)
    self.assertEqual(len(self.commands()), 1)

  def test_vias_follow_as_next_stops_last_one_first(self):
    source = self.source([NO_ROUTE])
    other = (37.5, 127.0, "B")
    source.want(Trip(PHONE.destination, (VIA, other)))
    self.tick(source)
    self.assertEqual([body.get("order") for _, body in self.commands()], [1, 2, 2])
    self.assertEqual([(body["lat"], body["lon"]) for _, body in self.commands()[1:]], [other[:2], VIA[:2]])

  def test_passing_front_vias_does_not_send_the_trip_again(self):
    other = (37.5, 127.0, "B")
    source = self.source([NO_ROUTE])
    source.want(Trip(PHONE.destination, (VIA, other)))
    self.tick(source)
    source.want(Trip(PHONE.destination, (other,)))
    self.tick(source, seconds=1.)
    source.want(PHONE)
    self.tick(source, seconds=1.)
    self.assertEqual(len(self.commands()), 3)

  def test_trip_edits_are_distinct_from_passing_front_vias(self):
    other, last = (37.5, 127.0, "B"), (37.6, 127.1, "C")
    for edited in (Trip(PHONE.destination, (VIA, other, last, (*GANGNAM, "D"))),
                   Trip(PHONE.destination, (other, VIA, last)),
                   Trip(PHONE.destination, (VIA, last)),
                   Trip(PHONE.destination, (VIA, other)),
                   Trip((*GANGNAM, "D"), (VIA, other, last))):
      with self.subTest(edited=edited):
        self.setUp()
        source = self.source([NO_ROUTE])
        source.want(Trip(PHONE.destination, (VIA, other, last)))
        self.tick(source)
        source.want(edited)
        self.tick(source, seconds=1.)
        self.assertEqual(len(self.commands()), 5 + len(edited.vias))

  def test_adding_a_passed_via_back_sends_a_new_trip(self):
    source = self.source([NO_ROUTE])
    source.want(Trip(PHONE.destination, (VIA,)))
    self.tick(source)
    source.want(PHONE)
    self.tick(source, seconds=1.)
    source.want(Trip(PHONE.destination, (VIA,)))
    self.tick(source, seconds=1.)
    self.assertEqual(len(self.commands()), 4)

  def test_disconnect_during_a_fleet_read_never_sends_the_phone_trip(self):
    connected = [True]
    self.opener = ScriptedOpener([car()], events=self.events)

    def disconnect(request, **kwargs):
      response = self.opener(request, **kwargs)
      if "/vehicle_data?" in request.full_url:
        connected[0] = False
      return response

    source = TeslaDestinationSource(opener=disconnect, clock=self.clock, phone_connected=lambda: connected[0],
                                    monthly=FleetBudget(self.budget_path, today=lambda: self.today))
    source.want(PHONE)
    self.tick(source)
    self.assertEqual(self.commands(), [])
    self.assertEqual(source.car(), CAR_UNKNOWN)

  def test_a_car_that_takes_no_coordinates_is_shared_the_destination(self):
    for refusal in (http_error(403), http_error(400), REFUSED):
      with self.subTest(refusal=refusal):
        self.setUp()
        source = self.source([NO_ROUTE], commands=[refusal])
        source.want(Trip(PHONE.destination, (VIA,)))
        with self.assertLogs(tesla.LOG, level="WARNING"):
          self.tick(source)
        (_, gps), (command, share) = self.commands()
        self.assertEqual(command, "navigation_request")
        self.assertEqual(share["type"], "share_ext_content_raw")
        self.assertEqual(share["value"], {"android.intent.extra.TEXT": "37.566500,126.978000"})

  def test_after_one_refusal_coordinates_are_not_asked_again(self):
    source = self.source([NO_ROUTE], commands=[REFUSED])
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    source.want(Trip((37.5, 127.0, "B")))
    self.tick(source, seconds=1.)
    self.assertEqual([c for c, _ in self.commands()], ["navigation_gps_request", "navigation_request", "navigation_request"])

  def test_a_new_via_alone_is_not_shared_again(self):
    """A share carries one place; the car has that one already."""
    source = self.source([NO_ROUTE], commands=[REFUSED])
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    source.want(Trip(PHONE.destination, (VIA,)))
    self.tick(source, seconds=1.)
    self.assertEqual(len(self.commands()), 2)

  def test_a_grant_without_vehicle_cmds_is_not_asked_again(self):
    """403 to both: tesla_setup has to run again. Asking every trip only bills."""
    source = self.source([NO_ROUTE], commands=[http_error(403), http_error(403)])
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    source.want(Trip((37.5, 127.0, "B")))
    self.tick(source, seconds=SEND_RETRY_S)
    self.assertEqual(len(self.commands()), 2)
    self.assertNotIn(True, self.alerts)  # reading the car's destination still works

  def test_an_asleep_car_is_tried_again_then_left_alone(self):
    source = self.source([NO_ROUTE], commands=[http_error(408)] * SEND_TRIES)
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
      self.tick(source, seconds=SEND_RETRY_S - 1.)
      self.assertEqual(len(self.commands()), 1)
      for _ in range(SEND_TRIES):
        self.tick(source, seconds=SEND_RETRY_S)
    self.assertEqual(len(self.commands()), SEND_TRIES)

  def test_an_expired_access_token_is_refreshed_and_the_trip_sent(self):
    source = self.source([NO_ROUTE, NO_ROUTE], commands=[http_error(401)])
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.tick(source, seconds=1.)
    self.assertEqual(self.opener.count("token"), 2)
    self.assertEqual(len(self.commands()), 2)

  def test_a_car_already_guiding_there_is_sent_nothing(self):
    """The phone took the destination from the car."""
    source = self.source([car(*CITY_HALL, name="서울시청")])
    self.tick(source)
    source.want(PHONE)
    self.tick(source, seconds=1.)
    self.assertEqual(self.commands(), [])

  def test_a_trip_ended_and_picked_again_is_sent_again(self):
    source = self.source([NO_ROUTE])
    source.want(PHONE)
    self.tick(source)
    source.want(None)
    self.tick(source, seconds=1.)
    source.want(PHONE)
    self.tick(source, seconds=1.)
    self.assertEqual(len(self.commands()), 2)

  def test_the_daily_command_cap_stops_sending(self):
    source = self.source([NO_ROUTE])
    source.commands = RequestBudget(cap=0, clock=FakeClock())
    source.want(PHONE)
    with self.assertLogs(tesla.LOG, level="WARNING"):
      self.tick(source)
    self.assertEqual(self.commands(), [])


class TestLastChangeWins(SourceTestCase):
  def sent(self, answers):
    """A source that sent PHONE to a car with no route, its poll answers still to come."""
    source = self.source([NO_ROUTE, *answers])
    source.want(PHONE)
    self.tick(source)
    return source

  def test_the_car_taking_the_trip_is_the_phones_destination_and_no_write(self):
    source = self.sent([car(CITY_HALL[0] + 0.002, CITY_HALL[1], "37.566500,126.978000")])  # searched, 220 m off
    self.tick(source, seconds=ECHO_WINDOW_S + 1.)
    self.assertEqual(source.car(), PHONE.destination)
    self.assertNotIn("put NavDestination", self.events)

  def test_anything_the_car_shows_right_after_a_send_is_the_car_taking_it(self):
    """A share the car searched far off is the car's mistake, not a new destination for the phone."""
    source = self.sent([car(37.3, 127.3, "어딘가")])
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.car(), PHONE.destination)
    self.assertNotIn("put NavDestination", self.events)

  def test_the_car_on_its_way_to_a_via_is_still_the_trip(self):
    source = self.source([NO_ROUTE, car(*VIA[:2], name=VIA[2])])
    source.want(Trip(PHONE.destination, (VIA,)))
    self.tick(source)
    self.tick(source, seconds=ECHO_WINDOW_S + 1.)
    self.assertEqual(source.car(), PHONE.destination)
    self.assertNotIn("put NavDestination", self.events)

  def test_a_destination_picked_on_the_car_later_wins(self):
    source = self.sent([car(CITY_HALL[0] + 0.001, CITY_HALL[1]), car()])
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=ECHO_WINDOW_S)
    self.assertEqual(source.car(), (GANGNAM[0], GANGNAM[1], "강남역"))
    self.assertEqual((self.nav()["latitude"], self.nav()["longitude"]), GANGNAM)

  def test_the_car_ending_the_shared_trip_ends_it_for_the_phone(self):
    source = self.sent([car(CITY_HALL[0] + 0.001, CITY_HALL[1]), NO_ROUTE, NO_ROUTE])
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertEqual(source.car(), PHONE.destination)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(source.car())

  def test_a_share_the_car_could_not_find_never_reads_as_an_ending(self):
    """The car took the command but guides nowhere: the phone keeps its guidance."""
    source = self.sent([NO_ROUTE, NO_ROUTE])
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.tick(source, seconds=POLL_INTERVAL_S)
    self.assertIsNone(source.car())  # no route all along, so no change for the phone to follow

  def test_at_the_first_answer_the_phones_trip_beats_what_the_car_kept(self):
    source = self.source([car()])
    source.want(PHONE)
    self.tick(source)
    self.assertNotIn("put NavDestination", self.events)
    self.assertEqual(len([r for r in self.opener.requests if "/command/" in r.full_url]), 1)
    self.assertEqual(source.car(), CAR_UNKNOWN)  # until the car shows it took the trip


class TestSameTrip(unittest.TestCase):
  def test_a_jitter_is_the_same_trip_and_another_via_is_not(self):
    moved = Trip((CITY_HALL[0] + 0.0001, CITY_HALL[1], "다른 이름"))
    self.assertTrue(same_trip(PHONE, moved))
    self.assertFalse(same_trip(PHONE, Trip(PHONE.destination, (VIA,))))
    self.assertTrue(same_trip(PHONE, Trip(PHONE.destination, (VIA,)), vias=False))
    self.assertTrue(same_trip(None, None))
    self.assertFalse(same_trip(PHONE, None))


class TestLoop(unittest.TestCase):
  def run_loop(self, step, started=True, alerts=None):
    """_loop for one iteration, with the device stack faked through sys.modules under the
    fork's openpilot.cereal path -- the technique test_camera_refresh.TestRefresherLoop uses for CameraRefresher._loop."""
    class FakeSubMaster:
      def __init__(self, services):
        pass

      def update(self, timeout):
        pass

      def __getitem__(self, service):
        return types.SimpleNamespace(started=started)

    sink = [] if alerts is None else alerts
    messaging = types.ModuleType("openpilot.cereal.messaging")
    messaging.SubMaster = FakeSubMaster
    cereal = types.ModuleType("openpilot.cereal")
    cereal.messaging = messaging
    params_mod = types.ModuleType("openpilot.common.params")
    params_mod.Params = FakeParams
    alertmanager = types.ModuleType("openpilot.selfdrive.selfdrived.alertmanager")
    alertmanager.set_offroad_alert = lambda *a: sink.append(a)
    self.enterContext(mock.patch.dict(sys.modules, {
      "openpilot.cereal": cereal,
      "openpilot.cereal.messaging": messaging,
      "openpilot.common.params": params_mod,
      "openpilot.selfdrive.selfdrived.alertmanager": alertmanager,
    }))
    source = TeslaDestinationSource()
    source.step = step
    source._stop = OneShotStop()
    source._loop()

  def test_started_comes_from_deviceState(self):
    seen = []
    self.run_loop(lambda params, started, set_alert: seen.append(started))
    self.assertEqual(seen, [True])

  def test_the_alert_is_offroad_korea_tesla_auth(self):
    alerts = []
    self.run_loop(lambda params, started, set_alert: set_alert(True), alerts=alerts)
    self.assertEqual(alerts, [("Offroad_KoreaTeslaAuth", True)])

  def test_the_thread_survives_an_unexpected_error(self):
    """No supervisor: anything escaping _loop ends destinations from the car until the
    process restarts."""
    def boom(params, started, set_alert):
      raise RuntimeError("boom")
    with self.assertLogs(tesla.LOG, level="ERROR"):
      self.run_loop(boom)


if __name__ == "__main__":
  unittest.main()
