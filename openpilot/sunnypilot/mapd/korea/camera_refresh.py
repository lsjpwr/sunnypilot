"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

camera_refresh: keeps korea_cameras.sqlite current from the data.go.kr open API.

Speed cameras move. The link database does not -- ITS republishes quarterly and is
220 MB, so it ships with the device (see the deploy task). Cameras are 3 MB and change
often enough to be worth fetching, which is exactly why the two live in separate files.

The swap is atomic: write_db builds a sibling .tmp and os.replace()s it in, so a reader
mid-query keeps its old inode and KoreaMapDB.reload_if_changed() picks the new file up
on its next tick. Nothing here needs a lock.

stdlib only, on purpose -- this module runs on the device, where pyshp and pyproj are
not installed.
"""
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from openpilot.sunnypilot.mapd.korea.build_db import SCHEMA_CAMERAS, insert_cameras, load_cameras_api, write_db

API_URL = "https://api.data.go.kr/openapi/tn_pubr_public_unmanned_traffic_camera_api"
PAGE_SIZE = 1000
MAX_PAGES = 200          # 44 pages in 2026-08; a runaway loop guard, not a real limit
HTTP_TIMEOUT_S = 30.

REFRESH_INTERVAL_S = 7 * 24 * 3600.   # the dataset's referenceDate moves monthly at most
RETRY_INTERVAL_S = 3600.

# Refuse a result this much smaller than what we already have. A partial API outage
# answers 200 with a short page; without this it would quietly wipe the cameras.
MIN_KEEP_RATIO = 0.8

LOG = logging.getLogger(__name__)


def fetch_all(api_key: str, opener=urllib.request.urlopen) -> list[dict]:
  """Every page of the camera dataset, as raw API items.

  Raises RuntimeError on an API-level error and OSError on a transport failure. The
  caller keeps the existing database in both cases.
  """
  items: list[dict] = []
  for page_no in range(1, MAX_PAGES + 1):
    # unquote first: data.go.kr hands out the key in two forms, "일반 인증키(Encoding)"
    # with %2F/%3D already escaped and "일반 인증키(Decoding)" raw. urlencode would escape
    # the percent signs of the first one again (%2F -> %252F) and the server answers 403.
    # A decoded key holds only base64 characters, so unquote is a no-op on it -- this
    # accepts either form.
    query = urllib.parse.urlencode({"serviceKey": urllib.parse.unquote(api_key), "pageNo": page_no,
                                    "numOfRows": PAGE_SIZE, "type": "json"})
    with opener(f"{API_URL}?{query}", timeout=HTTP_TIMEOUT_S) as response:
      payload = json.loads(response.read())

    code = payload.get("header", {}).get("resultCode")
    if code != "00":
      # deliberately not logging the url -- it carries the service key
      raise RuntimeError(f"camera api: resultCode {code!r}: {payload.get('header', {}).get('resultMsg')!r}")

    page = payload.get("body", {}).get("items", {}).get("item", [])
    # data.go.kr collapses a one-element list into a bare object. extend() on a dict would
    # silently append its KEYS, so normalise before anything downstream sees it.
    if isinstance(page, dict):
      page = [page]
    items.extend(page)
    if len(page) < PAGE_SIZE:
      return items

  raise RuntimeError(f"camera api: still paging after {MAX_PAGES} pages")


def current_row_count(path: str) -> int:
  """How many cameras the live database holds. 0 if there is no usable database yet."""
  try:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
  except sqlite3.Error:
    return 0
  try:
    return con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
  except sqlite3.Error:
    return 0
  finally:
    con.close()


def refresh(cameras_path: str, api_key: str, opener=urllib.request.urlopen) -> int:
  """Rebuild the camera database from the API. Returns the new row count, or 0 if it kept
  the old one.

  Never raises: a failed refresh is not worth taking the mapd process down for. Every
  return path leaves a usable database behind -- either the new one or the old one.
  """
  try:
    items = fetch_all(api_key, opener=opener)
  except Exception:
    # the api key is in no exception message we raise, and urllib puts the url only in
    # HTTPError.filename, which %s of the exception does not print
    LOG.warning("camera refresh: fetch failed, keeping the existing database", exc_info=True)
    return 0

  rows = list(load_cameras_api(items))
  floor = int(current_row_count(cameras_path) * MIN_KEEP_RATIO)
  if len(rows) < floor:
    LOG.warning("camera refresh: got %d rows, need at least %d -- keeping the existing database",
                len(rows), floor)
    return 0
  if not rows:
    LOG.warning("camera refresh: the api returned no usable rows")
    return 0

  try:
    return write_db(cameras_path, SCHEMA_CAMERAS, lambda con: insert_cameras(con, rows))
  except Exception:
    LOG.exception("camera refresh: failed to write %s", cameras_path)
    return 0


class CameraRefresher:
  """Background thread that refreshes the camera database roughly weekly.

  Owns no database handle. It only replaces the file; KoreaMapDB notices on its own.
  """

  def __init__(self, cameras_path: str):
    self.cameras_path = cameras_path
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None:
      return
    self._thread = threading.Thread(target=self._loop, daemon=True)
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=2.)
      self._thread = None

  def _due(self) -> bool:
    try:
      # TID251 bans time.time() because a monotonic clock is what you want for measuring
      # a duration. This is not that: it compares two wall-clock instants, one of which is
      # a file mtime. time.monotonic() has an arbitrary epoch, so subtracting an mtime from
      # it yields a meaningless number. Wall clock is the correct clock here.
      age = time.time() - os.path.getmtime(self.cameras_path)  # noqa: TID251
    except OSError:
      return True  # no database at all: fetch one
    return age >= REFRESH_INTERVAL_S

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is
    # what lets the tests above run under a bare interpreter
    import cereal.messaging as messaging
    from openpilot.common.params import Params

    params = Params()
    sm = messaging.SubMaster(['deviceState'])

    while not self._stop.is_set():
      wait = RETRY_INTERVAL_S
      if self._due():
        sm.update(0)
        api_key = params.get("KoreaMapApiKey", return_default=True) or ""
        if not api_key:
          LOG.info("camera refresh: no KoreaMapApiKey set")
        elif sm['deviceState'].networkMetered:
          LOG.info("camera refresh: network is metered, waiting")
        elif refresh(self.cameras_path, api_key):
          wait = REFRESH_INTERVAL_S
      self._stop.wait(wait)
