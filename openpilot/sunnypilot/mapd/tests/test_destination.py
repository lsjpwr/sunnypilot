import json
import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openpilot.common.params import Params, ParamKeyFlag
from openpilot.common.test import OpenpilotTestCase
from openpilot.common.timeout import Timeout
from openpilot.sunnypilot.mapd import MapSource, mapd_manager
from openpilot.sunnypilot.mapd.korea.external_source import EXTERNAL_PORT, ExternalNavSource
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
    self.data = KoreaMapData(external=self.external, route_source=self.route)

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

  def test_phone_cancellation_keeps_new_remote_details_at_the_same_coordinates(self):
    self.send_phone({"destination_lat": 37.5665, "destination_lon": 126.9780})
    self.destination()
    setNavDestination(37.5665, 126.9780, "서울시청", "다른 입구")
    expected = {"latitude": 37.5665, "longitude": 126.9780, "place_name": "서울시청", "place_details": "다른 입구"}
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

  def test_car_cancellation_keeps_new_remote_details_at_the_same_coordinates(self):
    source = self.car_source(car(), NO_ROUTE, NO_ROUTE)
    source.step(self.params, True, lambda _: None)
    self.destination()
    setNavDestination(37.4979, 127.0276, "강남역", "다른 입구")
    expected = {"latitude": 37.4979, "longitude": 127.0276, "place_name": "강남역", "place_details": "다른 입구"}
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

  def test_arrival_clears_the_cached_phone_destination_without_a_new_packet(self):
    self.send_phone({"session": 100, "trip": 1, "route_id": 1, "destination_lat": 37.5665, "destination_lon": 126.9780})
    self.destination()

    self.route.set_position(37.5665, 126.9780)
    self.data.update_destination()

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

  def test_offroad_clear_is_not_undone_by_a_cached_phone_destination(self):
    self.send_phone({"session": 100, "trip": 1, "route_id": 1, "destination_lat": 37.5665, "destination_lon": 126.9780})
    self.destination()

    self.params.put_bool("IsOffroad", True, block=True)
    self.params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)
    self.data.update_destination()
    # Wait behind any destination write queued by this tick.
    self.data.params.put("DongleId", "after-tick")
    with Timeout(2):
      self.params.get("DongleId", block=True)

    self.assertIsNone(self.params.get("NavDestination"))


class TestKoreaDestinationLoop(OpenpilotTestCase):
  def test_real_loop_delivers_the_destination_then_keeps_it_cleared_offroad(self):
    params = Params()
    params.put("MapDataSource", int(MapSource.korea), block=True)
    params.put_bool("KoreaExternalNavEnabled", True, block=True)
    params.put_bool("KoreaMapAutoDownload", False, block=True)
    params.put_bool("IsOffroad", False, block=True)
    phone = self.enterContext(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    phone.settimeout(2)
    ticks = []

    def advance():
      ticks.append(True)
      if len(ticks) == 1:
        payload = {"session": 100, "trip": 1, "route_id": 1, "seq": 1,
                   "destination_lat": 37.5665, "destination_lon": 126.9780}
        phone.sendto(json.dumps(payload).encode(), ("127.0.0.1", EXTERNAL_PORT))
        self.assertEqual(json.loads(phone.recv(4096))["ack"], 1)
      elif len(ticks) == 2:
        with Timeout(2):
          self.assertEqual(params.get("NavDestination", block=True), {
            "latitude": 37.5665, "longitude": 126.9780, "place_name": None, "place_details": None,
          })
        params.put_bool("IsOffroad", True, block=True)
        params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)
      elif len(ticks) == 3:
        params.put("MapDataSource", int(MapSource.osm), block=True)
      else:
        self.fail("mapd ignored the source change")

    directory = self.enterContext(tempfile.TemporaryDirectory())
    with mock.patch.object(mapd_manager, "config_realtime_process"), \
         mock.patch.object(mapd_manager, "Ratekeeper", return_value=SimpleNamespace(keep_time=advance)), \
         mock.patch.object(mapd_manager, "KOREA_MAP_DIR", directory):
      mapd_manager.korea_main()

    self.assertEqual(len(ticks), 3)
    self.assertIsNone(params.get("NavDestination"))
