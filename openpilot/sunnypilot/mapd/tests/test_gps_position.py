import json
import platform

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.common.timeout import Timeout
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import KoreaMapData
from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
from openpilot.sunnypilot.mapd.mapd_manager import update_osm_db
from openpilot.sunnypilot.navd.helpers import coordinate_from_param


class TestGpsPosition(OpenpilotTestCase):
  def setUp(self):
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.params.put("LastGPSPositionLLK", '{"latitude":37.5,"longitude":127.0}', block=True)

  def test_korea_position_reaches_the_map_reader_as_bytes(self):
    data = KoreaMapData()
    data.publish_targets()

    with Timeout(2):
      value = self.mem_params.get("LastGPSPosition", block=True)
    self.assertIsInstance(value, bytes)
    self.assertEqual(json.loads(value), {"latitude": 37.5, "longitude": 127.0})
    position = coordinate_from_param("LastGPSPosition", self.mem_params)
    self.assertEqual((position.latitude, position.longitude), (37.5, 127.0))

  def test_osm_position_and_bearing_reach_the_map_reader_as_bytes(self):
    data = OsmMapData()
    publisher = messaging.PubMaster(['liveLocationKalman'])
    message = messaging.new_message('liveLocationKalman', valid=True)
    location = message.liveLocationKalman
    location.status = log.LiveLocationKalman.Status.valid
    location.positionGeodetic = {'value': [37.6, 127.1, 0.0], 'valid': True}
    location.calibratedOrientationNED = {'value': [0.0, 0.0, 1.5707963267948966], 'valid': True}
    payload = message.to_bytes()
    for _ in range(2):
      publisher.send('liveLocationKalman', payload)
      data.sm.update(1000)

    data.update_location()

    value = self.mem_params.get("LastGPSPosition")
    self.assertIsInstance(value, bytes)
    self.assertEqual(json.loads(value), {"latitude": 37.6, "longitude": 127.1, "bearing": 90.0})

  def test_empty_osm_position_reads_as_no_coordinate(self):
    update_osm_db(self.params, self.mem_params)

    self.assertEqual(self.mem_params.get("LastGPSPosition"), b"{}")
    self.assertIsNone(coordinate_from_param("LastGPSPosition", self.mem_params))

  def test_initialization_preserves_an_existing_position(self):
    value = b'{"latitude":37.7,"longitude":127.2,"bearing":45.0}'
    self.mem_params.put("LastGPSPosition", value, block=True)

    update_osm_db(self.params, self.mem_params)

    self.assertEqual(self.mem_params.get("LastGPSPosition"), value)
    position = coordinate_from_param("LastGPSPosition", self.mem_params)
    self.assertEqual((position.latitude, position.longitude), (37.7, 127.2))

  def test_invalid_osm_localization_keeps_the_cached_position(self):
    data = OsmMapData()

    data.update_location()

    self.assertEqual(json.loads(self.mem_params.get("LastGPSPosition")), {"latitude": 37.5, "longitude": 127.0})

  def test_missing_position_does_not_publish_an_origin(self):
    self.params.remove("LastGPSPositionLLK")
    with self.subTest(source="korea"):
      KoreaMapData().publish_targets()
      self.assertIsNone(self.mem_params.get("LastGPSPosition"))
    with self.subTest(source="osm"):
      OsmMapData().update_location()
      self.assertIsNone(self.mem_params.get("LastGPSPosition"))
