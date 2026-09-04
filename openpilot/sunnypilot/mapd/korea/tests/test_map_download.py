"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import hashlib
import io
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from openpilot.sunnypilot.mapd.korea import map_download
from openpilot.sunnypilot.mapd.korea.build_db import SCHEMA_BUMPS, insert_bumps, write_db
from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH


def make_bumps_file(path, rows=3):
  """A real, openable bump database with `rows` rows."""
  write_db(path, SCHEMA_BUMPS,
           lambda con: insert_bumps(con, [(37.5 + i * 1e-4, 127.0, BUMP_ARCH) for i in range(rows)]))
  return path


def sha256_of(path):
  digest = hashlib.sha256()
  with open(path, "rb") as f:
    while chunk := f.read(1 << 20):
      digest.update(chunk)
  return digest.hexdigest()


def entry(name="korea_bumps.sqlite", url="https://example.invalid/bumps", sha="", size=1,
          table="bumps", min_rows=1):
  return map_download.Entry(name=name, url=url, sha256=sha, bytes=size, table=table, min_rows=min_rows)


def opener_for(path):
  """Stand in for urlopen: serves the bytes of `path`."""
  def opener(url, timeout=None):
    return io.BytesIO(pathlib.Path(path).read_bytes())
  return opener


class MapDownloadTestCase(unittest.TestCase):
  def setUp(self):
    super().setUp()
    # download() logs failures with exc_info=True, and several tests here deliberately fail
    # a download -- without this, Python's last-resort handler prints a traceback to stderr
    # for every one of them, which looks like an error but is not.
    logging.disable(logging.CRITICAL)
    self.addCleanup(logging.disable, logging.NOTSET)
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
    self.map_dir = str(self.tmp_path / "korea_map")
    os.makedirs(self.map_dir)


class TestReadManifest(MapDownloadTestCase):
  def test_the_shipped_manifest_parses(self):
    """The file that ships in the package must be readable by the code that reads it."""
    entries = map_download.read_manifest()
    self.assertTrue(entries)
    names = [e.name for e in entries]
    self.assertIn("korea_bumps.sqlite", names)
    self.assertIn("korea_links.sqlite", names)

  def test_cameras_are_not_in_the_manifest(self):
    """camera_refresh.py already rewrites that file from the API. Two writers on one path
    would fight, and the API copy is the fresher of the two."""
    self.assertNotIn("korea_cameras.sqlite", [e.name for e in map_download.read_manifest()])

  def test_entries_come_back_smallest_first(self):
    """On a tight disk or a slow link, take the cheap win before the 220 MB one."""
    sizes = [e.bytes for e in map_download.read_manifest()]
    self.assertEqual(sizes, sorted(sizes))

  def test_a_missing_manifest_is_not_an_error(self):
    self.assertEqual(map_download.read_manifest(str(self.tmp_path / "nope.json")), [])

  def test_a_corrupt_manifest_is_not_an_error(self):
    path = self.tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    self.assertEqual(map_download.read_manifest(str(path)), [])

  def test_a_wrong_shaped_root_is_not_an_error(self):
    """Syntactically valid JSON that is not an object escapes the OSError/ValueError
    guard entirely -- payload.get() assumes a dict and raises AttributeError instead."""
    path = self.tmp_path / "wrong_root.json"
    for root in ("null", "[]", "42", '"x"', "true"):
      with self.subTest(root=root):
        path.write_text(root, encoding="utf-8")
        self.assertEqual(map_download.read_manifest(str(path)), [])

  def test_a_non_list_databases_value_is_not_an_error(self):
    """"databases" present but not a list must not escape via a TypeError when the code
    iterates it."""
    path = self.tmp_path / "wrong_databases.json"
    for databases in ("null", "42", "true", '"x"', '{"a": 1}'):
      with self.subTest(databases=databases):
        path.write_text(f'{{"databases": {databases}}}', encoding="utf-8")
        self.assertEqual(map_download.read_manifest(str(path)), [])

  def test_an_entry_missing_a_field_is_dropped_not_fatal(self):
    """One malformed entry must not cost the other database its update."""
    path = self.tmp_path / "partial.json"
    path.write_text(json.dumps({"manifest_version": 1, "databases": [
      {"name": "korea_bumps.sqlite"},
      {"name": "korea_links.sqlite", "url": "u", "sha256": "s", "bytes": 1,
       "table": "links", "min_rows": 1},
    ]}), encoding="utf-8")
    entries = map_download.read_manifest(str(path))
    self.assertEqual([e.name for e in entries], ["korea_links.sqlite"])


class TestNeedsDownload(MapDownloadTestCase):
  def test_a_missing_file_needs_downloading(self):
    self.assertTrue(map_download.needs_download(entry(sha="abc"), self.map_dir))

  def test_a_matching_sha_does_not(self):
    path = os.path.join(self.map_dir, "korea_bumps.sqlite")
    make_bumps_file(path)
    self.assertFalse(map_download.needs_download(entry(sha=sha256_of(path)), self.map_dir))

  def test_a_different_sha_does(self):
    path = os.path.join(self.map_dir, "korea_bumps.sqlite")
    make_bumps_file(path)
    self.assertTrue(map_download.needs_download(entry(sha="deadbeef"), self.map_dir))


class TestEnoughDisk(MapDownloadTestCase):
  def test_a_tiny_file_fits(self):
    self.assertTrue(map_download.enough_disk(entry(size=1), self.map_dir))

  def test_an_absurd_file_does_not(self):
    """The .tmp sits beside the target, so the peak is the old file plus the new one."""
    self.assertFalse(map_download.enough_disk(entry(size=1 << 60), self.map_dir))


class TestDownload(MapDownloadTestCase):
  def setUp(self):
    super().setUp()
    self.source = str(self.tmp_path / "source.sqlite")
    make_bumps_file(self.source, rows=5)
    self.target = os.path.join(self.map_dir, "korea_bumps.sqlite")

  def test_a_good_download_is_installed(self):
    e = entry(sha=sha256_of(self.source), size=os.path.getsize(self.source), min_rows=5)
    self.assertTrue(map_download.download(e, self.map_dir, opener=opener_for(self.source)))
    self.assertEqual(sha256_of(self.target), e.sha256)

  def test_a_sha_mismatch_is_discarded(self):
    """Nothing downstream re-checks this. A wrong file installed here is a wrong file the
    car trusts."""
    e = entry(sha="0" * 64, size=os.path.getsize(self.source), min_rows=5)
    self.assertFalse(map_download.download(e, self.map_dir, opener=opener_for(self.source)))
    self.assertFalse(os.path.exists(self.target))

  def test_a_short_table_is_discarded(self):
    """min_rows is what catches a technically-valid database built from a broken input."""
    e = entry(sha=sha256_of(self.source), size=os.path.getsize(self.source), min_rows=999999)
    self.assertFalse(map_download.download(e, self.map_dir, opener=opener_for(self.source)))
    self.assertFalse(os.path.exists(self.target))

  def test_a_non_database_stream_is_discarded(self):
    """A server returning an HTML error page instead of a database still has to pass the
    sha256 check to reach this -- the failure this covers is sqlite3.DatabaseError out of
    db.verify(), not a hash mismatch."""
    path = self.tmp_path / "not_a_database.bin"
    path.write_bytes(b"<html>502 Bad Gateway</html>")
    e = entry(sha=sha256_of(str(path)), size=path.stat().st_size, min_rows=5)
    self.assertFalse(map_download.download(e, self.map_dir, opener=opener_for(str(path))))
    self.assertFalse(os.path.exists(self.target))
    leftovers = [n for n in os.listdir(self.map_dir) if n.endswith(".tmp")]
    self.assertEqual(leftovers, [], f"a partial download was left behind: {leftovers}")

  def test_a_failed_download_leaves_the_existing_file_alone(self):
    """links is a required file. Replacing a working one with a bad one costs speed limits."""
    make_bumps_file(self.target, rows=5)
    before = sha256_of(self.target)

    def boom(url, timeout=None):
      raise OSError("connection reset")

    e = entry(sha="0" * 64, size=1, min_rows=5)
    self.assertFalse(map_download.download(e, self.map_dir, opener=boom))
    self.assertEqual(sha256_of(self.target), before)

  def test_no_tmp_file_survives_a_failure(self):
    e = entry(sha="0" * 64, size=os.path.getsize(self.source), min_rows=5)
    map_download.download(e, self.map_dir, opener=opener_for(self.source))
    leftovers = [n for n in os.listdir(self.map_dir) if n.endswith(".tmp")]
    self.assertEqual(leftovers, [], f"a partial download was left behind: {leftovers}")

  def test_download_never_raises(self):
    """This runs on a thread with no supervisor. Anything that escapes ends downloads for
    the life of the process, silently."""
    def boom(url, timeout=None):
      raise RuntimeError("something nobody predicted")

    self.assertFalse(map_download.download(entry(sha="0" * 64), self.map_dir, opener=boom))


class TestRunOnce(MapDownloadTestCase):
  """One pass over the manifest, tested directly for speed and determinism. _loop's own
  decisions -- the opt-in gate, the deviceState and metered guards, the retry backoff, and
  the catch-all -- are covered separately in TestDownloaderLoop."""

  def setUp(self):
    super().setUp()
    self.downloader = map_download.MapDownloader(self.map_dir)
    self.source = str(self.tmp_path / "source.sqlite")
    make_bumps_file(self.source, rows=5)
    self.manifest = str(self.tmp_path / "manifest.json")

  def write_manifest(self, sha=None, size=None, min_rows=5):
    pathlib.Path(self.manifest).write_text(json.dumps({"manifest_version": 1, "databases": [{
      "name": "korea_bumps.sqlite", "url": "https://example.invalid/bumps",
      "sha256": sha if sha is not None else sha256_of(self.source),
      "bytes": size if size is not None else os.path.getsize(self.source),
      "table": "bumps", "min_rows": min_rows,
    }]}), encoding="utf-8")
    return self.manifest

  def test_it_installs_a_new_database(self):
    self.downloader._run_once(self.write_manifest(), opener=opener_for(self.source))
    self.assertTrue(os.path.exists(os.path.join(self.map_dir, "korea_bumps.sqlite")))

  def test_it_does_not_refetch_what_is_already_installed(self):
    target = os.path.join(self.map_dir, "korea_bumps.sqlite")
    make_bumps_file(target, rows=5)
    manifest = self.write_manifest(sha=sha256_of(target))

    calls = []

    def recording_opener(url, timeout=None):
      calls.append(url)
      raise AssertionError("unreachable")

    self.downloader._run_once(manifest, opener=recording_opener)
    self.assertEqual(calls, [], "re-downloaded a database that was already current")

  def test_a_disk_too_small_skips_without_touching_anything(self):
    manifest = self.write_manifest(size=1 << 60)

    calls = []

    def recording_opener(url, timeout=None):
      calls.append(url)
      raise AssertionError("unreachable")

    self.downloader._run_once(manifest, opener=recording_opener)
    self.assertEqual(calls, [], "started a download that cannot fit")
    self.assertFalse(os.path.exists(os.path.join(self.map_dir, "korea_bumps.sqlite")))

  def test_one_failing_entry_does_not_stop_the_others(self):
    """A 220 MB link download that fails must not cost the 11 MB bump download its turn."""
    second = str(self.tmp_path / "second.sqlite")
    make_bumps_file(second, rows=5)
    pathlib.Path(self.manifest).write_text(json.dumps({"manifest_version": 1, "databases": [
      {"name": "a.sqlite", "url": "https://example.invalid/a", "sha256": "0" * 64,
       "bytes": 1, "table": "bumps", "min_rows": 1},
      {"name": "korea_bumps.sqlite", "url": "https://example.invalid/b",
       "sha256": sha256_of(second), "bytes": os.path.getsize(second),
       "table": "bumps", "min_rows": 5},
    ]}), encoding="utf-8")

    self.downloader._run_once(self.manifest, opener=opener_for(second))

    self.assertTrue(os.path.exists(os.path.join(self.map_dir, "korea_bumps.sqlite")))


class ScriptedStop(threading.Event):
  """Lets _loop run for an exact number of iterations, recording every wait(timeout) call so
  a test can assert on the interval _loop chose without sleeping or touching wall-clock time."""

  def __init__(self, iterations=1):
    super().__init__()
    self.waits = []
    self._remaining = iterations

  def wait(self, timeout=None):
    self.waits.append(timeout)
    self._remaining -= 1
    if self._remaining <= 0:
      self.set()
    return True


class TestDownloaderLoop(MapDownloadTestCase):
  """_loop holds four decisions -- the opt-in gate, the deviceState guard, the metered
  guard, and the catch-all -- plus the retry backoff, none of which TestRunOnce exercises
  since it calls _run_once directly. cereal and Params are faked through sys.modules the
  same way test_camera_refresh.TestRefresherLoop fakes them for CameraRefresher._loop."""

  def drive_loop(self, *, iterations=1, run_once_results=(True,), run_once=None, recv_frame=1,
                 metered=False, enabled=True):
    class FakeSubMaster:
      def __init__(self):
        self.recv_frame = {'deviceState': recv_frame}

      def update(self, timeout):
        pass

      def __getitem__(self, service):
        return types.SimpleNamespace(networkMetered=metered)

    messaging = types.ModuleType("cereal.messaging")
    messaging.SubMaster = lambda services: FakeSubMaster()
    params_mod = types.ModuleType("openpilot.common.params")
    params_mod.Params = lambda: types.SimpleNamespace(get_bool=lambda key: enabled)

    self.enterContext(mock.patch.dict(sys.modules, {
      "cereal": types.ModuleType("cereal"),
      "cereal.messaging": messaging,
      "openpilot.common.params": params_mod,
    }))

    calls = []
    if run_once is None:
      results = iter(run_once_results)

      def run_once(self):
        calls.append(1)
        return next(results, True)

    self.enterContext(mock.patch.object(map_download.MapDownloader, "_run_once", run_once))

    downloader = map_download.MapDownloader(self.map_dir)
    downloader._stop = ScriptedStop(iterations)
    downloader._loop()
    return downloader, calls

  def test_parameter_off_never_calls_run_once(self):
    """The default is "0" on every device -- get this backwards and every car starts a
    220 MB download nobody asked for. The off branch must also keep the one-hour wait, not
    the seven-day one, so a newly-enabled toggle is picked up within the hour."""
    downloader, calls = self.drive_loop(enabled=False)
    self.assertEqual(calls, [])
    self.assertEqual(downloader._stop.waits, [map_download.RETRY_INTERVAL_S])

  def test_no_deviceState_yet_does_not_download(self):
    """A SubMaster that has received nothing reports networkMetered False -- the capnp
    default, not an answer -- which would otherwise start a download over a metered link
    on the first tick after boot."""
    downloader, calls = self.drive_loop(recv_frame=0)
    self.assertEqual(calls, [])

  def test_a_metered_network_does_not_download(self):
    downloader, calls = self.drive_loop(metered=True)
    self.assertEqual(calls, [])

  def test_enabled_unmetered_with_deviceState_calls_run_once(self):
    downloader, calls = self.drive_loop()
    self.assertEqual(calls, [1])
    self.assertEqual(downloader._stop.waits, [map_download.CHECK_INTERVAL_S])

  def test_the_loop_survives_run_once_raising(self):
    """This thread has no supervisor. Anything that escapes _loop silently ends map
    downloads for the life of the process."""
    def boom(self):
      raise RuntimeError("something nobody predicted")

    downloader, _ = self.drive_loop(run_once=boom)  # must not raise
    self.assertEqual(downloader._stop.waits, [map_download.RETRY_INTERVAL_S])

  def test_backoff_grows_on_consecutive_failures_and_resets_on_success(self):
    """Any incomplete pass must stretch the retry -- otherwise a manifest sha that can never
    match the hosted asset retries 220 MB every hour forever -- and a success must snap the
    interval back down."""
    downloader, calls = self.drive_loop(iterations=5,
                                        run_once_results=[False, False, False, True, False])
    R, C = map_download.RETRY_INTERVAL_S, map_download.CHECK_INTERVAL_S
    self.assertEqual(downloader._stop.waits, [R * 2, R * 4, R * 8, C, R * 2])
    self.assertEqual(len(calls), 5)

  def test_backoff_stops_growing_at_the_cap(self):
    """Without the cap the interval doubles forever, so a database that fails for a week
    would not be retried for months. 2**6 is 64; the cap must hold it at 24."""
    downloader, _ = self.drive_loop(iterations=6, run_once_results=[False] * 6)
    R = map_download.RETRY_INTERVAL_S
    self.assertEqual(downloader._stop.waits, [R * 2, R * 4, R * 8, R * 16, R * 24, R * 24])


class TestThreadLifecycle(MapDownloadTestCase):
  def test_start_is_idempotent_and_stop_joins(self):
    """mapd_manager's korea_main can be re-entered on a source switch; a second start()
    that spawned a second thread would double every download."""
    downloader = map_download.MapDownloader(self.map_dir)
    exited = threading.Event()

    def fake_loop(self):
      # Blocks on the real _stop event, same as the production loop's trailing wait --
      # only self._stop.set() in stop() can unblock this.
      self._stop.wait()
      exited.set()

    with mock.patch.object(map_download.MapDownloader, "_loop", fake_loop):
      downloader.start()
      first = downloader._thread
      downloader.start()
      self.assertIs(downloader._thread, first)
      downloader.stop()
      self.assertIsNone(downloader._thread)
    self.assertTrue(exited.is_set(), "stop() did not signal a live _loop to exit")


if __name__ == "__main__":
  unittest.main()
