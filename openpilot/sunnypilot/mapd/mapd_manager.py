#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import glob
import json
import logging
import os
import platform
import shutil
from datetime import datetime

from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, config_realtime_process
from openpilot.common.swaglog import cloudlog, ForwardingHandler
from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert
from openpilot.sunnypilot.mapd import MAPD_PATH, MapSource
from openpilot.sunnypilot.mapd.korea.camera_refresh import CameraRefresher
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNavSource
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import (KOREA_CAMERAS_PATH, KOREA_LINKS_PATH,
                                                                    KOREA_MAP_DIR, KoreaMapData)
from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
from openpilot.sunnypilot.mapd.mapd_installer import VERSION, update_installed_version


# openpilot/sunnypilot/mapd/korea/ logs through stdlib logging, not cloudlog, so that it
# stays importable under a bare interpreter for its tests. Nothing configures the root
# logger in this process, so without this bridge every message from there is discarded
# before reaching a handler -- including the two that explain why an auto-refresh did not
# happen. Same pattern as selfdrive/car/card.py:33.
_korea_log = logging.getLogger("openpilot.sunnypilot.mapd.korea")
_korea_log.setLevel(logging.INFO)
_korea_log.addHandler(ForwardingHandler(cloudlog))


def get_files_for_cleanup() -> list[str]:
  paths = [
    f"{Paths.mapd_root()}/db",
    f"{Paths.mapd_root()}/v*"
  ]
  files_to_remove = []
  for path in paths:
    if os.path.exists(path):
      files = glob.glob(path + '/**', recursive=True)
      files_to_remove.extend(files)
  # check for version and mapd files
  if not os.path.isfile(MAPD_PATH):
    files_to_remove.append(MAPD_PATH)
  return files_to_remove


def cleanup_old_osm_data(files_to_remove: list[str]) -> None:
  for file in files_to_remove:
    # Remove trailing slash if path is file
    if file.endswith('/') and os.path.isfile(file[:-1]):
      file = file[:-1]
    # Try to remove as file or symbolic link first
    if os.path.islink(file) or os.path.isfile(file):
      os.remove(file)
    elif os.path.isdir(file):  # If it's a directory
      shutil.rmtree(file, ignore_errors=False)


def request_refresh_osm_location_data(params, mem_params, nations: list[str], states: list[str] | None = None) -> None:
  params.put("OsmDownloadedDate", str(datetime.now().timestamp()), block=True)
  params.put_bool("OsmDbUpdatesCheck", False, block=True)

  osm_download_locations = {
    "nations": nations,
    "states": states or []
  }

  cloudlog.info("mapd: downloading maps for %s", json.dumps(osm_download_locations))
  mem_params.put("OSMDownloadLocations", osm_download_locations, block=True)


def filter_nations_and_states(nations: list[str], states: list[str] | None = None) -> tuple[list[str], list[str]]:
  """Filters and prepares nation and state data for OSM map download.

  If the nation is 'US' and a specific state is provided, the nation 'US' is removed from the list.
  If the nation is 'US' and the state is 'All', the 'All' is removed from the list.
  The idea behind these filters is that if a specific state in the US is provided,
  there's no need to download map data for the entire US. Conversely,
  if the state is unspecified (i.e., 'All'), we intend to download map data for the whole US,
  and 'All' isn't a valid state name, so it's removed.
  """
  if "US" in nations and states and not any(x.lower() == "all" for x in states):
    # If a specific state in the US is provided, remove 'US' from nations
    nations.remove("US")
  elif "US" in nations and states and any(x.lower() == "all" for x in states):
    # If 'All' is provided as a state (case invariant), remove those instances from states
    states = [x for x in states if x.lower() != "all"]
  elif "US" not in nations and states and any(x.lower() == "all" for x in states):
    states.remove("All")
  return nations, states or []


def update_osm_db(params, mem_params) -> None:
  if params.get_bool("OsmDbUpdatesCheck"):
    cleanup_old_osm_data(get_files_for_cleanup())
    country = params.get("OsmLocationName", return_default=True)
    state = params.get("OsmStateName", return_default=True)
    filtered_nations, filtered_states = filter_nations_and_states([country], [state])
    request_refresh_osm_location_data(params, mem_params, filtered_nations, filtered_states)

  if not mem_params.get("OSMDownloadBounds"):
    mem_params.put("OSMDownloadBounds", "", block=True)

  if not mem_params.get("LastGPSPosition"):
    mem_params.put("LastGPSPosition", "{}", block=True)


def osm_main() -> None:
  params = Params()
  mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else params
  source = params.get("MapDataSource", return_default=True)

  update_installed_version(VERSION, params)
  config_realtime_process([0, 1, 2, 3], 5)

  rk = Ratekeeper(1, print_delay_threshold=None)
  live_map_sp = OsmMapData()

  try:
    os.makedirs(Paths.mapd_root(), exist_ok=True)
  except OSError:
    cloudlog.exception("mapd: failed to make %s", Paths.mapd_root())

  while True:
    if params.get("MapDataSource", return_default=True) != source:
      return  # main() starts the source the param now names; OsmMapData holds nothing to release

    show_alert = bool(get_files_for_cleanup() and params.get_bool("OsmLocal"))
    set_offroad_alert("Offroad_OSMUpdateRequired", show_alert, "This alert will be cleared when new maps are downloaded.")

    update_osm_db(params, mem_params)
    live_map_sp.tick()
    rk.keep_time()


def korea_main() -> None:
  config_realtime_process([0, 1, 2, 3], 5)

  params = Params()
  source = params.get("MapDataSource", return_default=True)
  external_nav = params.get_bool("KoreaExternalNavEnabled")

  try:
    os.makedirs(KOREA_MAP_DIR, exist_ok=True)
  except OSError:
    cloudlog.exception("mapd: failed to make %s", KOREA_MAP_DIR)

  external = None
  if external_nav:
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
    # KoreaExternalNavEnabled ends the loop too: the socket binds once above, so the only
    # way to honour a toggle is to come back through here and let main() start us again.
    if (params.get("MapDataSource", return_default=True) != source or
        params.get_bool("KoreaExternalNavEnabled") != external_nav):
      break

    absent = [p for p in (KOREA_CAMERAS_PATH, KOREA_LINKS_PATH) if not os.path.exists(p)]
    if absent != db_missing:
      set_offroad_alert("Offroad_KoreaMapMissing", bool(absent), f"Missing {', '.join(absent)}")
      db_missing = absent

    live_map_sp.tick()
    rk.keep_time()

  # Release everything before returning: the next ExternalNavSource cannot bind udp/5555
  # while this one still holds it, a refresher left running would rewrite the camera file
  # underneath whatever starts next, and the link database is 220 MB of open sqlite.
  if external is not None:
    external.stop()
  refresher.stop()
  live_map_sp.close()


def main() -> None:
  # Supervisor. Each source loop returns when MapDataSource changes, and this picks the
  # source the param now names, so a switch applies on the next tick with no reboot.
  # Exiting instead would not work: manager registers mapd_manager with always_run, and
  # ensure_running only calls stop() -- the one thing that clears self.proc -- when
  # should_run is False, so an mapd_manager that exits stays dead until the next boot.
  # The settings UI still restricts the change to offroad, now because swapping the speed
  # limit source at speed is a bad idea, not because it would take until a restart.
  params = Params()
  while True:
    if params.get("MapDataSource", return_default=True) == MapSource.osm:
      osm_main()
    else:
      korea_main()


if __name__ == "__main__":
  main()
