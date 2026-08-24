#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import logging
import os

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, config_realtime_process
from openpilot.common.swaglog import cloudlog, ForwardingHandler
from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert
from openpilot.sunnypilot.mapd.korea.camera_refresh import CameraRefresher
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNavSource
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import (KOREA_CAMERAS_PATH, KOREA_LINKS_PATH,
                                                                    KOREA_MAP_DIR, KoreaMapData)


# openpilot/sunnypilot/mapd/korea/ logs through stdlib logging, not cloudlog, so that it
# stays importable under a bare interpreter for its tests. Nothing configures the root
# logger in this process, so without this bridge every message from there is discarded
# before reaching a handler -- including the two that explain why an auto-refresh did not
# happen. Same pattern as selfdrive/car/card.py:33.
_korea_log = logging.getLogger("openpilot.sunnypilot.mapd.korea")
_korea_log.setLevel(logging.INFO)
_korea_log.addHandler(ForwardingHandler(cloudlog))


def main_thread() -> None:
  config_realtime_process([0, 1, 2, 3], 5)

  try:
    os.makedirs(KOREA_MAP_DIR, exist_ok=True)
  except OSError:
    cloudlog.exception("mapd: failed to make %s", KOREA_MAP_DIR)

  external = None
  if Params().get_bool("KoreaExternalNavEnabled"):
    try:
      external = ExternalNavSource()
      external.start()
      cloudlog.info("mapd: external nav listening on udp/%d", external.port)
    except OSError:
      # The port can already be taken. This is an optional input hook, so failing to bind
      # must not take the process down -- an unhandled raise here reaches manager, and
      # processNotRunning blocks engagement over a convenience feature.
      external = None
      cloudlog.exception("mapd: external nav failed to start, continuing without it")

  live_map_sp = KoreaMapData(external=external)

  refresher = CameraRefresher(KOREA_CAMERAS_PATH)
  refresher.start()
  rk = Ratekeeper(1, print_delay_threshold=None)

  # only touch the param when the message would change; this loop runs forever and params
  # live on flash. Keyed on the file list, not just on "any missing", so copying one of the
  # two files updates the text instead of leaving it naming both.
  db_missing: list[str] | None = None

  while True:
    absent = [p for p in (KOREA_CAMERAS_PATH, KOREA_LINKS_PATH) if not os.path.exists(p)]
    if absent != db_missing:
      set_offroad_alert("Offroad_KoreaMapMissing", bool(absent), f"Missing {', '.join(absent)}")
      db_missing = absent

    live_map_sp.tick()
    rk.keep_time()


def main() -> None:
  main_thread()


if __name__ == "__main__":
  main()
