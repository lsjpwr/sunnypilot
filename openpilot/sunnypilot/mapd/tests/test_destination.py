import json
import socket
import tempfile
import time
from pathlib import Path

from openpilot.common.params import Params, ParamKeyFlag
from openpilot.common.test import OpenpilotTestCase
from openpilot.common.timeout import Timeout
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNavSource
from openpilot.sunnypilot.mapd.korea.fleet_budget import FleetBudget
from openpilot.sunnypilot.mapd.korea.route import RouteSource
from openpilot.sunnypilot.mapd.korea.tesla import POLL_INTERVAL_S, TeslaDestinationSource
from openpilot.sunnypilot.mapd.korea.tests.test_route import FakeClock
from openpilot.sunnypilot.mapd.korea.tests.test_tesla import CREDENTIALS, NO_ROUTE, ScriptedOpener, car
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import KoreaMapData
from openpilot.system.athena.athenad import setNavDestination


class TestDestination(OpenpilotTestCase):
  def setUp(self):
    self.params = Params()
    self.route = RouteSource()
    self.external = ExternalNavSource(port=0, route_source=self.route)
    self.external.start()
    self.addCleanup(self.external.stop)
    self.phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.addCleanup(self.phone.close)
    self.phone.settimeout(2)
    self.sequence = 0
    self.data = KoreaMapData(external=self.external)

  def send_phone(self, payload):
    self.sequence += 1
    self.phone.sendto(json.dumps({**payload, "seq": self.sequence}).encode(), ("127.0.0.1", self.external.port))
    self.assertEqual(json.loads(self.phone.recv(4096))["ack"], self.sequence)
    self.data.update_destination()

  def destination(self):
    with Timeout(2):
      return self.params.get("NavDestination", block=True)

  def wait_destination(self, expected):
    with Timeout(2):
      while self.params.get("NavDestination") != expected:
        time.sleep(0.01)

  def car_source(self, *answers):
    for key, value in CREDENTIALS.items():
      self.params.put(key, value, block=True)
    directory = self.enterContext(tempfile.TemporaryDirectory())
    self.clock = FakeClock(1000.)
    return TeslaDestinationSource(opener=ScriptedOpener(answers), clock=self.clock, phone_connected=lambda: True,
                                  monthly=FleetBudget(Path(directory) / "budget.json"))

  def test_phone_destination_reaches_params_as_json(self):
    self.send_phone({"destination_lat": 37.5665, "destination_lon": 126.9780, "destination_name": "서울시청"})

    self.assertEqual(self.destination(), {
      "latitude": 37.5665, "longitude": 126.9780, "place_name": "서울시청", "place_details": None,
    })

  def test_phone_cancellation_clears_its_destination(self):
    self.send_phone({"destination_lat": 37.5665, "destination_lon": 126.9780})
    self.destination()

    self.send_phone({"destination_lat": 0, "destination_lon": 0})

    self.assertIsNone(self.params.get("NavDestination"))

  def test_car_destination_reaches_params_as_json(self):
    source = self.car_source(car())

    source.step(self.params, True, lambda _: None)

    self.assertEqual(self.destination(), {
      "latitude": 37.4979, "longitude": 127.0276, "place_name": "강남역", "place_details": None,
    })

  def test_remote_destination_reaches_params_as_json(self):
    self.assertEqual(setNavDestination(37.5665, 126.9780, "서울시청", "중구"), {"success": 1})

    self.assertEqual(self.destination(), {
      "latitude": 37.5665, "longitude": 126.9780, "place_name": "서울시청", "place_details": "중구",
    })

  def test_existing_serialized_destination_is_read_without_rewriting(self):
    payload = '{"latitude":37.5665,"longitude":126.9780,"place_name":"서울시청","place_details":"중구"}'
    path = Path(self.params.get_param_path("NavDestination"))
    path.write_text(payload, encoding="utf-8")

    self.assertEqual(Params().get("NavDestination"), {
      "latitude": 37.5665, "longitude": 126.9780, "place_name": "서울시청", "place_details": "중구",
    })
    self.send_phone({"speed_limit_kph": 60})
    self.assertEqual(path.read_text(encoding="utf-8"), payload)

  def test_phone_cancellation_keeps_a_new_car_destination(self):
    self.send_phone({"destination_lat": 37.5665, "destination_lon": 126.9780})
    self.destination()
    self.car_source(car()).step(self.params, True, lambda _: None)
    expected = {"latitude": 37.4979, "longitude": 127.0276, "place_name": "강남역", "place_details": None}
    self.wait_destination(expected)

    self.send_phone({"destination_lat": 0, "destination_lon": 0})

    self.assertEqual(self.params.get("NavDestination"), expected)

  def test_phone_cancellation_keeps_a_new_remote_destination(self):
    self.send_phone({"destination_lat": 37.4979, "destination_lon": 127.0276})
    self.destination()
    setNavDestination(37.5665, 126.9780, "서울시청", "중구")
    expected = {"latitude": 37.5665, "longitude": 126.9780, "place_name": "서울시청", "place_details": "중구"}
    self.wait_destination(expected)

    self.send_phone({"destination_lat": 0, "destination_lon": 0})

    self.assertEqual(self.params.get("NavDestination"), expected)

  def test_car_cancellation_clears_its_destination(self):
    source = self.car_source(car(), NO_ROUTE, NO_ROUTE)
    source.step(self.params, True, lambda _: None)
    self.destination()

    for _ in range(2):
      self.clock.now += POLL_INTERVAL_S
      source.step(self.params, True, lambda _: None)

    self.assertIsNone(self.params.get("NavDestination"))

  def test_car_cancellation_keeps_a_new_phone_destination(self):
    source = self.car_source(car(), NO_ROUTE, NO_ROUTE)
    source.step(self.params, True, lambda _: None)
    self.destination()
    self.send_phone({"destination_lat": 37.5665, "destination_lon": 126.9780})
    expected = {"latitude": 37.5665, "longitude": 126.9780, "place_name": None, "place_details": None}
    self.wait_destination(expected)

    for _ in range(2):
      self.clock.now += POLL_INTERVAL_S
      source.step(self.params, True, lambda _: None)

    self.assertEqual(self.params.get("NavDestination"), expected)

  def test_arrival_does_not_restore_the_cars_unchanged_destination(self):
    source = self.car_source(car(), car())
    source.step(self.params, True, lambda _: None)
    self.destination()

    self.params.remove("NavDestination")
    self.clock.now += POLL_INTERVAL_S
    source.step(self.params, True, lambda _: None)

    self.assertIsNone(self.params.get("NavDestination"))

  def test_new_drive_can_share_the_same_car_destination_after_offroad_clear(self):
    source = self.car_source(car(), car())
    source.step(self.params, True, lambda _: None)
    expected = self.destination()
    source.step(self.params, False, lambda _: None)

    self.params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)
    self.assertIsNone(self.params.get("NavDestination"))
    source.step(self.params, True, lambda _: None)

    self.assertEqual(self.destination(), expected)

  def test_arrival_does_not_restore_the_phones_trip_from_late_packets(self):
    payload = {"session": 100, "trip": 1, "route_id": 1, "destination_lat": 37.5665, "destination_lon": 126.9780}
    self.send_phone(payload)
    self.destination()

    self.route.set_position(37.5665, 126.9780)
    self.send_phone(payload)
    self.send_phone(payload)

    self.assertIsNone(self.params.get("NavDestination"))

  def test_offroad_does_not_restore_the_phones_trip_from_late_packets(self):
    payload = {"session": 100, "trip": 1, "route_id": 1, "destination_lat": 37.5665, "destination_lon": 126.9780}
    self.send_phone(payload)
    self.destination()
    self.route.set_started(True)

    self.route.set_started(False)
    self.params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)
    self.send_phone(payload)

    self.assertIsNone(self.params.get("NavDestination"))
