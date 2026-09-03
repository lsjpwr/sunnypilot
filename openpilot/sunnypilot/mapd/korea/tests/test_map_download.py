"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import hashlib
import io
import json
import os
import pathlib
import tempfile
import unittest

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


if __name__ == "__main__":
  unittest.main()
