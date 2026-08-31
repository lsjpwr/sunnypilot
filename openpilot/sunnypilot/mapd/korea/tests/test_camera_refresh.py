"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from unittest import mock

from openpilot.sunnypilot.mapd.korea import camera_refresh
from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_CAMERAS, insert_cameras, load_cameras_api,
                                                      write_db)


def api_item(lat, lon, limit, section=0):
  return {"latitude": str(lat), "longitude": str(lon), "lmttVe": str(limit),
          "ovrspdRegltSctnLt": str(section)}


def fake_opener(pages, result_code="00"):
  """Stand in for urlopen. Serves `pages` (a list of item-lists) by pageNo."""
  total = sum(len(p) for p in pages)

  def opener(url, timeout=None):
    page_no = int(url.split("pageNo=")[1].split("&")[0])
    items = pages[page_no - 1] if page_no <= len(pages) else []
    body = {"header": {"resultCode": result_code, "resultMsg": "OK"},
            "body": {"items": {"item": items}, "numOfRows": camera_refresh.PAGE_SIZE,
                     "pageNo": page_no, "totalCount": total}}
    return io.BytesIO(json.dumps(body).encode())

  return opener


def count_rows(path):
  con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
  try:
    return con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
  finally:
    con.close()


def seed(path, n):
  write_db(path, SCHEMA_CAMERAS,
           lambda con: insert_cameras(con, [(37.5 + i * 1e-4, 127.0, 60, 0) for i in range(n)]))


class TestFetch(unittest.TestCase):
  def test_api_items_parse_like_the_csv_path(self):
    """One filter, one place. A row the CSV path drops must be dropped here too."""
    items = [api_item(37.5, 127.0, 60),
             api_item(37.5, 127.0, 0),        # red-light camera: no target speed
             api_item(37.5, 127.0, 999),      # above MAX_SPEED_LIMIT_KPH
             api_item(1.0, 1.0, 60)]          # outside Korea
    self.assertEqual(list(load_cameras_api(items)), [(37.5, 127.0, 60, 0)])

  def test_fetch_all_walks_every_page(self):
    pages = [[api_item(37.5, 127.0, 60)] * camera_refresh.PAGE_SIZE, [api_item(37.6, 127.1, 50)] * 7]
    items = camera_refresh.fetch_all("KEY", opener=fake_opener(pages))
    self.assertEqual(len(items), camera_refresh.PAGE_SIZE + 7)

  def test_fetch_all_rejects_an_api_error(self):
    pages = [[api_item(37.5, 127.0, 60)]]
    with self.assertRaisesRegex(RuntimeError, "30"):
      camera_refresh.fetch_all("KEY", opener=fake_opener(pages, result_code="30"))

  def test_a_one_row_page_is_not_a_bare_object(self):
    """data.go.kr collapses a single-element list into an object."""
    def opener(url, timeout=None):
      body = {"header": {"resultCode": "00"},
              "body": {"items": {"item": api_item(37.5, 127.0, 60)}, "totalCount": 1}}
      return io.BytesIO(json.dumps(body).encode())

    self.assertEqual(camera_refresh.fetch_all("KEY", opener=opener), [api_item(37.5, 127.0, 60)])

  def test_an_already_encoded_key_is_not_encoded_twice(self):
    """data.go.kr issues the key both escaped and raw. Double-escaping it answers 403.

    Found by calling the real API: the escaped form went out as %252F and was rejected.
    """
    seen = []

    def opener(url, timeout=None):
      seen.append(url)
      body = {"header": {"resultCode": "00"}, "body": {"items": {"item": []}, "totalCount": 0}}
      return io.BytesIO(json.dumps(body).encode())

    encoded = "abc%2Fdef%3D%3D"
    decoded = "abc/def=="
    camera_refresh.fetch_all(encoded, opener=opener)
    camera_refresh.fetch_all(decoded, opener=opener)

    self.assertNotIn("%252F", seen[0], "the escaped key was escaped a second time")
    # both forms must produce the identical query string
    self.assertEqual(seen[0], seen[1])


class TestRefresh(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
    self.path = str(self.tmp_path / "korea_cameras.sqlite")

  def test_refresh_replaces_the_database(self):
    seed(self.path, 10)
    before = os.path.getmtime(self.path)

    pages = [[api_item(37.5 + i * 1e-4, 127.0, 60) for i in range(20)]]
    self.assertEqual(camera_refresh.refresh(self.path, "KEY", opener=fake_opener(pages)), 20)
    self.assertEqual(count_rows(self.path), 20)
    self.assertNotEqual(os.path.getmtime(self.path), before)

  def test_refresh_refuses_to_shrink_the_database(self):
    """A partial API outage returns 200 with few rows. That must not wipe the cameras."""
    seed(self.path, 1000)

    pages = [[api_item(37.5 + i * 1e-4, 127.0, 60) for i in range(10)]]
    self.assertEqual(camera_refresh.refresh(self.path, "KEY", opener=fake_opener(pages)), 0)
    self.assertEqual(count_rows(self.path), 1000)

  def test_refresh_populates_a_missing_database(self):
    """No floor to compare against on a fresh device -- any non-empty result is an improvement."""
    pages = [[api_item(37.5 + i * 1e-4, 127.0, 60) for i in range(5)]]
    self.assertEqual(camera_refresh.refresh(self.path, "KEY", opener=fake_opener(pages)), 5)
    self.assertEqual(count_rows(self.path), 5)

  def test_refresh_keeps_the_old_database_when_the_api_fails(self):
    seed(self.path, 10)

    def boom(url, timeout=None):
      raise OSError("no route to host")

    self.assertEqual(camera_refresh.refresh(self.path, "KEY", opener=boom), 0)
    self.assertEqual(count_rows(self.path), 10)

  def test_refresh_leaves_no_temp_file_behind(self):
    seed(self.path, 1000)
    pages = [[api_item(37.5 + i * 1e-4, 127.0, 60) for i in range(10)]]
    camera_refresh.refresh(self.path, "KEY", opener=fake_opener(pages))
    self.assertFalse(os.path.exists(self.path + ".tmp"))

  def test_the_api_key_never_reaches_a_log_line(self):
    """The key rides in the query string, so every failure path is a potential leak.

    HTTPError is the realistic one: urllib stores the full url on it as .filename.
    """
    def boom(url, timeout=None):
      raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    with self.assertLogs(camera_refresh.LOG, level="DEBUG") as captured:
      camera_refresh.refresh(self.path, "SECRET-KEY-VALUE", opener=boom)
    self.assertNotIn("SECRET-KEY-VALUE", "\n".join(captured.output))


class OneShotStop(threading.Event):
  """Lets _loop run exactly one iteration: the trailing wait() ends the loop."""

  def wait(self, timeout=None):
    self.set()
    return True


class TestRefresherLoop(unittest.TestCase):
  def run_one_iteration(self, *, recv_frame, metered, api_key="a-key"):
    """Drives CameraRefresher._loop once with the device stack faked out.

    _loop imports cereal.messaging and Params inside the function so the module stays
    importable under a bare interpreter. Injecting into sys.modules keeps that property --
    a real SubMaster here would drag the whole device stack into this test file.
    """
    class FakeSubMaster:
      def __init__(self):
        self.recv_frame = {'deviceState': recv_frame}

      def update(self, timeout):
        pass

      def __getitem__(self, service):
        return types.SimpleNamespace(networkMetered=metered)

    sm = FakeSubMaster()

    messaging = types.ModuleType("cereal.messaging")
    messaging.SubMaster = lambda services: sm
    params_mod = types.ModuleType("openpilot.common.params")
    params_mod.Params = lambda: types.SimpleNamespace(get=lambda k, return_default=False: api_key)

    self.enterContext(mock.patch.dict(sys.modules, {
      "cereal": types.ModuleType("cereal"),
      "cereal.messaging": messaging,
      "openpilot.common.params": params_mod,
    }))

    refreshed = []
    self.enterContext(mock.patch.object(camera_refresh, "refresh",
                                        lambda path, key: refreshed.append(key) or True))

    r = camera_refresh.CameraRefresher("/nonexistent/korea_cameras.sqlite")  # missing file: always due
    r._stop = OneShotStop()
    r._loop()
    return refreshed

  def test_no_deviceState_yet_does_not_download(self):
    """A SubMaster that has received nothing reports networkMetered False -- the capnp
    default, not an answer. Downloading on that would put several megabytes on a metered
    link on the first tick after boot."""
    self.assertEqual(self.run_one_iteration(recv_frame=0, metered=False), [])

  def test_a_metered_network_does_not_download(self):
    self.assertEqual(self.run_one_iteration(recv_frame=1, metered=True), [])

  def test_an_unmetered_network_downloads(self):
    self.assertEqual(self.run_one_iteration(recv_frame=1, metered=False), ["a-key"])

  def test_the_refresher_thread_survives_an_unexpected_error(self):
    """The thread has no supervisor; anything escaping _loop silently ends camera refreshes
    for the life of the process."""
    self.enterContext(mock.patch.object(camera_refresh.CameraRefresher, "_due",
                                        lambda self: (_ for _ in ()).throw(RuntimeError("boom"))))
    self.assertEqual(self.run_one_iteration(recv_frame=1, metered=False), [])
