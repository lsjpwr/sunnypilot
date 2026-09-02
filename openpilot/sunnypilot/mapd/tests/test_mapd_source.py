"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from openpilot.common.parameterized import parameterized
from openpilot.sunnypilot.mapd import MapSource
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import KoreaMapData
from openpilot.system.manager.process_config import mapd_ready


class FakeParams:
  """Params stand-in. get() returns an int the way an INT param key does."""

  # The assert below exists to catch a param read nobody meant to add to a loop that
  # runs forever, so new keys get listed here rather than dropping the check.
  # Mapd_ClearCache is upstream's sunnylink map-deletion trigger (#1971), read once
  # per osm_main() tick.
  GETTABLE = ("MapDataSource", "Mapd_ClearCache")

  def __init__(self, source, **bools):
    self.source = source
    self.bools = bools
    self.removed: list[str] = []

  def get(self, key, return_default=False):
    assert key in self.GETTABLE, f"unexpected param read: {key}"
    if key == "MapDataSource":
      return int(self.source)
    return self.bools.get(key, False)

  def get_bool(self, key):
    return self.bools.get(key, False)

  def remove(self, key):
    self.removed.append(key)


class Stop(BaseException):
  """Ends a test's supervisor loop. BaseException on purpose: main() catches Exception so a
  crashing source cannot take the process down, so a test signal that inherits from Exception
  would be swallowed by the code under test and spin the loop forever instead of ending it."""


class Runaway(BaseException):
  """Raised instead of hanging when a source loop ignores the setting it is watching.
  BaseException for the same reason as Stop -- it must escape the code under test."""


class Boom(Exception):
  """A source loop crashing mid-tick. Exception, not BaseException, because that is what a
  real bug raises -- a truncated geometry blob, a malformed value out of the mapd binary --
  and the whole point is that main() catches exactly this."""


class OneShotRatekeeper:
  """Ratekeeper stand-in that ends the loop instead of sleeping a second per iteration.

  It applies the settings change during the tick, so the loop body runs exactly once --
  the same trick as OneShotStop in korea/tests/test_camera_refresh.py, no thread and no
  sleep. The instance doubles as the class: a source loop calls Ratekeeper(1, ...) on it.
  """

  def __init__(self, on_tick):
    self.on_tick = on_tick
    self.frames = 0

  def __call__(self, rate, print_delay_threshold=None):
    return self

  def keep_time(self):
    self.frames += 1
    if self.frames > 1:
      raise Runaway
    self.on_tick()


class FakeExternal:
  """ExternalNavSource stand-in: the real one binds udp/5555 and starts a thread."""

  def __init__(self):
    self.port = 5555
    self.stopped = False

  def start(self):
    pass

  def stop(self):
    self.stopped = True


class FakeRefresher:
  def __init__(self, cameras_path):
    self.cameras_path = cameras_path
    self.started = False
    self.stopped = False

  def start(self):
    self.started = True

  def stop(self):
    self.stopped = True


class FakeMapData:
  """KoreaMapData/OsmMapData stand-in: the real ones open messaging sockets, and the
  korean one a 220 MB sqlite database."""

  def __init__(self, external=None):
    self.external = external
    self.ticks = 0
    self.closed = False

  def tick(self):
    self.ticks += 1

  def close(self):
    self.closed = True


def _record(built, name, factory):
  """Captures what a source loop builds so a test can assert on it after the loop ends."""
  def build(*args, **kwargs):
    built[name] = factory(*args, **kwargs)
    return built[name]
  return build


def _crash():
  raise Boom("the map database handed back garbage")


class MapdSourceTestCase(unittest.TestCase):
  """Shared temp dir plus the patch helpers the source-loop tests all need."""

  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

  def patch(self, target, name, value):
    """monkeypatch.setattr with the lifetime tied to this test."""
    self.enterContext(mock.patch.object(target, name, value))

  def run_source_main(self, params, source_main, on_tick, expect=Runaway):
    """Runs one source loop to completion with every device-side collaborator faked out.

    on_tick fires inside the single loop iteration -- that is where a test changes the
    setting the loop is watching. A loop that never notices raises Runaway instead of
    hanging the suite; the caller sees it as an extra tick rather than a stuck run.

    expect is what the loop is allowed to end with. It defaults to Runaway; a test that makes
    on_tick crash passes its own exception so it still gets `built` back and can assert on what
    the crash path released.
    """
    from openpilot.sunnypilot.mapd import mapd_manager

    built: dict = {"alerts": []}
    patches = {
      "Params": lambda *a: params,
      "config_realtime_process": lambda cores, priority: None,
      "set_offroad_alert": lambda *a: built["alerts"].append(a),
      "Ratekeeper": OneShotRatekeeper(on_tick),
      "KOREA_MAP_DIR": str(self.tmp_path),
      "KOREA_CAMERAS_PATH": str(self.tmp_path / "korea_cameras.sqlite"),
      "KOREA_LINKS_PATH": str(self.tmp_path / "korea_links.sqlite"),
      "update_installed_version": lambda version, p: None,
      "update_osm_db": lambda p, mem_p: None,
      "get_files_for_cleanup": list,  # nothing to clean up, so no OSM update alert
      "ExternalNavSource": _record(built, "external", FakeExternal),
      "CameraRefresher": _record(built, "refresher", FakeRefresher),
      "KoreaMapData": _record(built, "map_data", FakeMapData),
      "OsmMapData": _record(built, "map_data", FakeMapData),
    }
    for name, value in patches.items():
      self.patch(mapd_manager, name, value)
    self.patch(mapd_manager.Paths, "mapd_root", staticmethod(lambda: str(self.tmp_path)))

    try:
      getattr(mapd_manager, source_main)()
    except expect:
      pass
    return built


class TestMapSourceEnum(unittest.TestCase):
  def test_the_enum_values_match_the_button_indices(self):
    """MultipleButtonAction stores the button index straight into the param, so the
    enum values ARE the button order. Buttons are [OpenStreetMap, korean public data]."""
    self.assertEqual(int(MapSource.osm), 0)
    self.assertEqual(int(MapSource.korea), 1)


class TestMapdBinaryGating(MapdSourceTestCase):
  @parameterized.expand([
    (MapSource.osm, True, True),
    (MapSource.osm, False, False),    # binary has nowhere to run
    (MapSource.korea, True, False),   # dir may linger from a previous osm run
    (MapSource.korea, False, False),
  ])
  def test_the_mapd_binary_runs_only_for_osm(self, source, dir_exists, expected):
    """Two publishers on liveMapDataSP overwrite each other. The binary is the osm
    producer, so korea mode must leave it stopped even when /data/media/0/osm exists."""
    from openpilot.system.manager import process_config

    root = self.tmp_path / "osm"
    if dir_exists:
      os.makedirs(root)
    self.patch(process_config.Paths, "mapd_root", staticmethod(lambda: str(root)))
    self.assertIs(mapd_ready(False, FakeParams(source), None), expected)


class TestSourceLoops(MapdSourceTestCase):
  def test_the_osm_loop_honors_a_sunnylink_map_deletion(self):
    """Upstream drives map deletion by setting Mapd_ClearCache and letting the loop notice
    (#1971). That check lived in main_thread(), which this branch split into osm_main() and
    korea_main(), so it has to be carried over by hand -- drop it and the settings button
    silently does nothing. The param is cleared afterwards so one press deletes once."""
    from openpilot.sunnypilot.mapd import mapd_manager

    cleared: list = []
    self.patch(mapd_manager, "clear_downloaded_maps", lambda p: cleared.append(p))

    params = FakeParams(MapSource.osm, Mapd_ClearCache=True)
    self.run_source_main(params, "osm_main", lambda: setattr(params, "source", MapSource.korea))

    self.assertEqual(cleared, [params], "Mapd_ClearCache did not reach clear_downloaded_maps")
    self.assertEqual(params.removed, ["Mapd_ClearCache"],
                     "the trigger was not cleared, so it would delete every tick")

  @parameterized.expand([
    ("korea_main", MapSource.korea, MapSource.osm),
    ("osm_main", MapSource.osm, MapSource.korea),
  ])
  def test_a_source_loop_returns_when_the_source_changes(self, source_main, source, switch_to):
    """Both loops run forever by design. Without an exit condition the process keeps
    publishing the old database after the user picks the other one, and only a reboot fixes
    it -- manager will not restart this process, it is registered always_run."""
    params = FakeParams(source)
    built = self.run_source_main(params, source_main, lambda: setattr(params, "source", switch_to))
    self.assertEqual(built["map_data"].ticks, 1, "the loop kept running after MapDataSource changed")

  def test_the_korea_loop_releases_its_resources_before_returning(self):
    """A switch that leaks these cannot be undone: the next ExternalNavSource cannot bind
    udp/5555 while this one holds it, the old refresh thread keeps rewriting the camera file
    underneath the new source, and the sqlite handles on a 220 MB database stay open for the
    life of the process."""
    params = FakeParams(MapSource.korea, KoreaExternalNavEnabled=True)
    built = self.run_source_main(params, "korea_main", lambda: setattr(params, "source", MapSource.osm))
    self.assertTrue(built["external"].stopped, "the UDP socket stays bound")
    self.assertTrue(built["refresher"].stopped,
                    "the camera refresh thread outlives the source that started it")
    self.assertTrue(built["map_data"].closed, "the sqlite handles stay open")

  def test_the_korea_loop_returns_when_external_nav_is_toggled(self):
    """The UDP socket binds once at startup, so this toggle only takes effect by ending the
    loop and letting main() start it again -- which is what the settings now promise, having
    dropped needs_onroad_cycle."""
    params = FakeParams(MapSource.korea)
    built = self.run_source_main(params, "korea_main",
                                 lambda: params.bools.update(KoreaExternalNavEnabled=True))
    self.assertEqual(built["map_data"].ticks, 1,
                     "the loop kept running after KoreaExternalNavEnabled changed")
    self.assertNotIn("external", built,
                     "it started with the toggle off; only a restart binds the socket")

  def test_the_korea_loop_still_alerts_on_the_missing_database(self):
    """Pre-existing behaviour, pinned here because an exit condition was added to this loop:
    the alert names the files that are actually absent, and the refresher still starts. The
    second tuple is the exit-path clear -- the outgoing source must not leave a stale banner
    for a database that OSM (the incoming source) never touches."""
    params = FakeParams(MapSource.korea)
    built = self.run_source_main(params, "korea_main", lambda: setattr(params, "source", MapSource.osm))
    missing = f"Missing {self.tmp_path / 'korea_cameras.sqlite'}, {self.tmp_path / 'korea_links.sqlite'}"
    self.assertEqual(built["alerts"], [
      ("Offroad_KoreaMapMissing", True, missing),
      ("Offroad_KoreaMapMissing", False, ""),
    ])
    self.assertTrue(built["refresher"].started)

  def test_the_osm_loop_clears_its_alert_on_exit(self):
    """Symmetric with the korea case above: Offroad_OSMUpdateRequired is CLEAR_ON_MANAGER_START,
    so the reboot the old design required used to clear it. Now that a switch does not reboot,
    the outgoing OSM loop has to clear it itself or the offroad screen keeps warning about an
    update need for a source that is no longer running."""
    params = FakeParams(MapSource.osm)
    built = self.run_source_main(params, "osm_main", lambda: setattr(params, "source", MapSource.korea))
    self.assertEqual(built["alerts"], [
      ("Offroad_OSMUpdateRequired", False, "This alert will be cleared when new maps are downloaded."),
      ("Offroad_OSMUpdateRequired", False, ""),
    ])

  @parameterized.expand([
    ("osm_main", MapSource.korea),
    ("korea_main", MapSource.osm),
  ])
  def test_a_source_exits_on_its_first_check_if_the_param_already_names_the_other_source(self, source_main,
                                                                                        other_source):
    """Finding 2: main() and the source loop used to each read MapDataSource separately, so a
    write landing in that window let the loop capture a baseline that already matched the new
    value -- its exit guard could then never fire, and the wrong source ran forever while
    mapd_ready flipped the binary out from under it. Comparing against MapSource.osm directly,
    the same test main() uses to dispatch, closes the window: even if the param already names
    the other source before this loop's first check, it must exit right there, before ticking
    even once."""
    built = self.run_source_main(FakeParams(other_source), source_main, lambda: None)
    self.assertEqual(built["map_data"].ticks, 0,
                     f"{source_main} ran a tick despite the param already naming the other source")

  def test_the_korea_loop_does_not_exit_for_an_out_of_range_source_value(self):
    """main() dispatches anything other than MapSource.osm to korea_main -- including a value
    neither source owns, e.g. a stray int written directly to the param. korea_main's exit
    guard mirrors main()'s own dispatch test (== MapSource.osm), so an out-of-range value fails
    it exactly like MapSource.korea does and the loop keeps running, matching what main() would
    do if it re-read the param: dispatch to korea_main again. Written the other way around, as
    "!= MapSource.korea", an out-of-range value would incorrectly exit and the supervisor would
    spin hot re-launching korea_main every tick."""
    built = self.run_source_main(FakeParams(7), "korea_main", lambda: None)
    self.assertEqual(built["map_data"].ticks, 2,
                     "korea_main exited instead of continuing to run for an out-of-range source value")

  def test_a_crashing_korea_loop_still_releases_its_resources(self):
    """The cleanup used to sit after the while loop, so an exception skipped all of it. That
    matters more than the crash: main() now restarts a source, and the next ExternalNavSource
    gets EADDRINUSE on a socket this one never gave back -- which korea_main deliberately
    swallows, so external nav would be dead until reboot with no symptom at all. (That stop()
    actually frees the port is pinned separately, in korea/tests/test_external_source.py.)"""
    params = FakeParams(MapSource.korea, KoreaExternalNavEnabled=True)
    built = self.run_source_main(params, "korea_main", _crash, expect=Boom)

    self.assertTrue(built["external"].stopped,
                    "a crash leaves the UDP socket bound and external nav silently dead")
    self.assertTrue(built["refresher"].stopped,
                    "a crash leaves the refresh thread rewriting the camera file")
    self.assertTrue(built["map_data"].closed,
                    "a crash leaks the sqlite handles on a 220 MB database")

  @parameterized.expand([
    ("korea_main", MapSource.korea, "Offroad_KoreaMapMissing"),
    ("osm_main", MapSource.osm, "Offroad_OSMUpdateRequired"),
  ])
  def test_a_crashing_source_clears_its_offroad_alert(self, source_main, source, key):
    """Both alert keys are CLEAR_ON_MANAGER_START, and this process no longer exits -- main()
    catches the crash and starts a source again. So a source that dies holding its alert set
    leaves a banner about a source that is not running, with nothing left to clear it."""
    built = self.run_source_main(FakeParams(source), source_main, _crash, expect=Boom)
    self.assertEqual(built["alerts"][-1], (key, False, ""),
                     f"{source_main} died without clearing {key}")


class TestSupervisor(MapdSourceTestCase):
  def test_main_dispatches_on_the_param(self):
    """One read decides which database feeds every speed limit. Getting this backwards
    means the wrong source runs for the whole drive."""
    from openpilot.sunnypilot.mapd import mapd_manager

    called = []

    def stub(name):
      def run():
        called.append(name)
        raise Stop
      return run

    self.patch(mapd_manager, "osm_main", stub("osm"))
    self.patch(mapd_manager, "korea_main", stub("korea"))

    for source, expected in ((MapSource.osm, "osm"), (MapSource.korea, "korea")):
      with self.subTest(source=int(source)):
        called.clear()
        with mock.patch.object(mapd_manager, "Params", lambda s=source: FakeParams(s)):
          with self.assertRaises(Stop):
            mapd_manager.main()
        self.assertEqual(called, [expected], f"source {source!r} started {called}")

  def test_the_supervisor_starts_the_other_source_after_a_return(self):
    """A source loop returning IS the switch. main() has to read the param again and start
    the other one; exiting instead would leave mapd_manager dead until the next reboot,
    because always_run never asks manager to stop it and only stop() clears self.proc."""
    from openpilot.sunnypilot.mapd import mapd_manager

    params = FakeParams(MapSource.korea)
    started = []

    def fake_korea():
      started.append("korea")
      params.source = MapSource.osm

    def fake_osm():
      self.assertNotIn("osm", started, "main() restarted a source that had not returned")
      started.append("osm")
      raise Stop

    self.patch(mapd_manager, "Params", lambda *a: params)
    self.patch(mapd_manager, "korea_main", fake_korea)
    self.patch(mapd_manager, "osm_main", fake_osm)

    try:
      mapd_manager.main()
    except Stop:
      pass
    self.assertEqual(started, ["korea", "osm"], "main() did not start the source the param now names")

  def test_the_supervisor_restarts_a_crashed_source_after_a_delay(self):
    """Letting the crash out would be far worse than losing map data. manager never restarts
    an always_run process that exits, and selfdrived's ignored_processes covers the mapd binary
    but not mapd_manager -- so the missing process raises processNotRunning, which is
    SOFT_DISABLE plus NO_ENTRY. One bad value would block engagement for every drive until the
    user reboots. The delay is what stops a source that fails on every start from spinning."""
    from openpilot.sunnypilot.mapd import mapd_manager

    events = []

    def fake_korea():
      events.append("start")
      if events.count("start") == 1:
        raise Boom("the map database handed back garbage")
      raise Stop

    self.patch(mapd_manager, "Params", lambda *a: FakeParams(MapSource.korea))
    self.patch(mapd_manager, "korea_main", fake_korea)
    self.patch(mapd_manager.time, "sleep", lambda s: events.append(f"slept {s}"))

    with self.assertRaises(Stop):
      mapd_manager.main()

    # The sleep entry is the assertion that matters: drop the rate limiting and this reads
    # ["start", "start"], so a source failing at startup would relaunch as fast as the loop
    # can go -- rebinding a socket and opening a 220 MB database every pass.
    self.assertEqual(events, ["start", f"slept {mapd_manager.SOURCE_RESTART_DELAY_S}", "start"])
    self.assertGreater(mapd_manager.SOURCE_RESTART_DELAY_S, 0)

  def test_the_supervisor_still_lets_a_shutdown_signal_out(self):
    """The catch is `except Exception`, so KeyboardInterrupt and SystemExit -- both
    BaseException -- must still terminate the process. Catching those would leave a
    mapd_manager that ignores shutdown and has to be killed."""
    from openpilot.sunnypilot.mapd import mapd_manager

    def fake_korea():
      raise KeyboardInterrupt

    self.patch(mapd_manager, "Params", lambda *a: FakeParams(MapSource.korea))
    self.patch(mapd_manager, "korea_main", fake_korea)
    self.patch(mapd_manager.time, "sleep",
               lambda s: self.fail("a shutdown signal was treated as a crash"))

    with self.assertRaises(KeyboardInterrupt):
      mapd_manager.main()


class TestKoreaMapDataClose(unittest.TestCase):
  def test_close_releases_the_database_and_is_safe_before_it_opens(self):
    """korea_main calls this on the way out of a switch. open_db() is lazy, so self.db is
    None for every tick before both files land -- a switch in that window must not take the
    process down with an AttributeError."""
    closed = []
    puts = []
    data = KoreaMapData.__new__(KoreaMapData)
    data.db = SimpleNamespace(close=lambda: closed.append(True))
    data.link = data.camera = data.bump = object()
    data.mem_params = SimpleNamespace(put=lambda k, v, **kw: puts.append((k, v)))

    data.close()
    self.assertEqual(closed, [True], "the sqlite handles on a 220 MB database are never released")
    self.assertEqual((data.db, data.link, data.camera, data.bump), (None, None, None, None))
    self.assertIn(("MapTargetVelocities", "[]"), puts,
                 "close() must clear MapTargetVelocities, or a departing source leaves a live deceleration target for whatever runs next")

    data.close()  # never opened, or already closed
    self.assertEqual(closed, [True])
