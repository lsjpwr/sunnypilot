#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert
from openpilot.sunnypilot.mapd.korea.camera_refresh import CameraRefresher
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNavSource
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import (KOREA_CAMERAS_PATH, KOREA_LINKS_PATH,
                                                                    KOREA_MAP_DIR, KoreaMapData)


def main_thread() -> None:
  config_realtime_process([0, 1, 2, 3], 5)

  try:
    os.makedirs(KOREA_MAP_DIR, exist_ok=True)
  except OSError:
    cloudlog.exception("mapd: failed to make %s", KOREA_MAP_DIR)

  external = None
  if Params().get_bool("KoreaExternalNavEnabled"):
    external = ExternalNavSource()
    external.start()
    cloudlog.info("mapd: external nav listening on udp/%d", external.port)

  live_map_sp = KoreaMapData(external=external)

  refresher = CameraRefresher(KOREA_CAMERAS_PATH)
  refresher.start()
  rk = Ratekeeper(1, print_delay_threshold=None)

  # only touch the param on a transition; this loop runs forever and params live on flash
  db_missing = None

  while True:
    absent = [p for p in (KOREA_CAMERAS_PATH, KOREA_LINKS_PATH) if not os.path.exists(p)]
    if bool(absent) != db_missing:
      set_offroad_alert("Offroad_KoreaMapMissing", bool(absent), f"Missing {', '.join(absent)}")
      db_missing = bool(absent)

    live_map_sp.tick()
    rk.keep_time()


def main() -> None:
  main_thread()


if __name__ == "__main__":
  main()
