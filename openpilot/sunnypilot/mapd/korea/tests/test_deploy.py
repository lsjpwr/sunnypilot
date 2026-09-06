"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from openpilot.sunnypilot.mapd.korea import deploy, map_download
from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_BUMPS, SCHEMA_CAMERAS, SCHEMA_LINKS,
                                                      insert_bumps, insert_cameras, insert_links,
                                                      write_db)
from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH
from openpilot.sunnypilot.mapd.korea.deploy import (MIN_BUMPS, build_targets, emit_manifest,
                                                    sha256_of, verify)


class TestDeploy(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

  def good_db(self, n=5):
    path = str(self.tmp_path / "korea_cameras.sqlite")
    write_db(path, SCHEMA_CAMERAS,
             lambda con: insert_cameras(con, [(37.5 + i * 1e-4, 127.0, 60, 0) for i in range(n)]))
    return path

  def good_bumps_db(self, n=5):
    path = str(self.tmp_path / "korea_bumps.sqlite")
    write_db(path, SCHEMA_BUMPS,
             lambda con: insert_bumps(con, [(37.5, 127.0 + i * 1e-4, BUMP_ARCH) for i in range(n)]))
    return path

  def good_links_db(self, n=5):
    path = str(self.tmp_path / "korea_links.sqlite")
    points = [(37.5, 127.0), (37.6, 127.1)]
    write_db(path, SCHEMA_LINKS,
             lambda con: insert_links(con, [(60, "road", points) for _ in range(n)]))
    return path

  def test_verify_returns_the_row_count(self):
    self.assertEqual(verify(self.good_db(5), "cameras", 1), 5)

  def test_verify_rejects_a_short_database(self):
    with self.assertRaisesRegex(ValueError, "5 rows"):
      verify(self.good_db(5), "cameras", 1000)

  def test_verify_rejects_a_file_that_is_not_a_database(self):
    path = str(self.tmp_path / "korea_cameras.sqlite")
    with open(path, "wb") as f:
      f.write(b"not a database")
    with self.assertRaises(sqlite3.DatabaseError):
      verify(path, "cameras", 1)

  def test_verify_rejects_a_schema_version_mismatch(self):
    path = self.good_db()
    con = sqlite3.connect(path)
    con.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    con.commit()
    con.close()
    with self.assertRaisesRegex(ValueError, "schema"):
      verify(path, "cameras", 1)

  def test_sha256_matches_the_shell_format(self):
    """The device side runs `sha256sum`, which prints 64 lowercase hex characters."""
    digest = sha256_of(self.good_db())
    self.assertEqual(len(digest), 64)
    self.assertEqual(digest, digest.lower())
    self.assertTrue(all(c in "0123456789abcdef" for c in digest))

  def test_verify_accepts_a_bump_database(self):
    self.assertEqual(verify(self.good_bumps_db(5), "bumps", 1), 5)

  def test_verify_rejects_a_short_bump_database(self):
    with self.assertRaisesRegex(ValueError, "5 rows"):
      verify(self.good_bumps_db(5), "bumps", 1000)

  def test_build_targets_includes_bumps_when_the_file_exists(self):
    targets = build_targets(self.good_db(), str(self.tmp_path / "korea_links.sqlite"),
                            self.good_bumps_db())
    self.assertEqual([table for _, table, _ in targets], ["cameras", "links", "bumps"])

  def test_build_targets_skips_a_missing_bump_file(self):
    """Speed bumps are optional. Refreshing speed limits on a device that never got a
    bump database must not fail on the file that was never built."""
    targets = build_targets(self.good_db(), str(self.tmp_path / "korea_links.sqlite"),
                            str(self.tmp_path / "never_built.sqlite"))
    self.assertEqual([table for _, table, _ in targets], ["cameras", "links"])

  def test_verify_rejects_a_bump_database_just_under_min_bumps(self):
    with self.assertRaisesRegex(ValueError, f"{MIN_BUMPS - 1} rows"):
      verify(self.good_bumps_db(MIN_BUMPS - 1), "bumps", MIN_BUMPS)

  def test_verify_accepts_a_bump_database_at_min_bumps(self):
    self.assertEqual(verify(self.good_bumps_db(MIN_BUMPS), "bumps", MIN_BUMPS), MIN_BUMPS)


class TestEmitManifest(TestDeploy):
  """The manifest is what the device reads to decide it needs a new file. Hand-writing it
  would put the sha256 of a 220 MB file in a human's hands."""

  def test_it_emits_an_entry_per_present_file(self):
    with mock.patch.object(deploy, "MIN_LINKS", 3), mock.patch.object(deploy, "MIN_BUMPS", 2):
      links = self.good_links_db(5)
      bumps = self.good_bumps_db(5)
      payload = json.loads(emit_manifest("korea-map-2026.05", links=links, bumps=bumps,
                                         repo="lsjpwr/sunnypilot"))
    names = [d["name"] for d in payload["databases"]]
    self.assertEqual(sorted(names), ["korea_bumps.sqlite", "korea_links.sqlite"])

  def test_the_sha_matches_the_file(self):
    with mock.patch.object(deploy, "MIN_BUMPS", 2):
      bumps = self.good_bumps_db(5)
      payload = json.loads(emit_manifest("t", links=None, bumps=bumps, repo="lsjpwr/sunnypilot"))
    entry = payload["databases"][0]
    self.assertEqual(entry["sha256"], sha256_of(bumps))
    self.assertEqual(entry["bytes"], os.path.getsize(bumps))

  def test_the_url_names_the_tag_and_the_repo(self):
    with mock.patch.object(deploy, "MIN_BUMPS", 2):
      bumps = self.good_bumps_db(5)
      payload = json.loads(emit_manifest("korea-map-2026.05", links=None, bumps=bumps,
                                         repo="lsjpwr/sunnypilot"))
    self.assertEqual(payload["databases"][0]["url"],
                     "https://github.com/lsjpwr/sunnypilot/releases/download/korea-map-2026.05/korea_bumps.sqlite")

  def test_a_missing_file_is_left_out(self):
    """--links/--bumps carry string defaults and are never None in real CLI use, so the
    path that matters is one that is set but was never built, not just links=None."""
    payload = json.loads(emit_manifest("t", links=None, bumps=None, repo="lsjpwr/sunnypilot"))
    self.assertEqual(payload["databases"], [])

    missing = str(self.tmp_path / "never_built.sqlite")
    payload = json.loads(emit_manifest("t", links=missing, bumps=missing, repo="lsjpwr/sunnypilot"))
    self.assertEqual(payload["databases"], [])

  def test_the_row_floors_match_the_verify_constants(self):
    """Proves min_rows in the manifest is read from deploy's floor constants at call time,
    not baked into emit_manifest as a literal: a hardcoded 1000000/83000 would have passed
    the old version of this test, but fails here because the floors are patched to 3/2."""
    with mock.patch.object(deploy, "MIN_LINKS", 3), mock.patch.object(deploy, "MIN_BUMPS", 2):
      links = self.good_links_db(3)
      bumps = self.good_bumps_db(2)
      payload = json.loads(emit_manifest("t", links=links, bumps=bumps, repo="lsjpwr/sunnypilot"))
    floors = {d["name"]: d["min_rows"] for d in payload["databases"]}
    self.assertEqual(floors["korea_links.sqlite"], 3)
    self.assertEqual(floors["korea_bumps.sqlite"], 2)

  def test_the_emitted_manifest_is_readable_by_the_device_code(self):
    """The two ends of this contract live in different files. This is the test that fails
    when one of them drifts."""
    with mock.patch.object(deploy, "MIN_BUMPS", 2):
      bumps = self.good_bumps_db(5)
      path = self.tmp_path / "map_manifest.json"
      path.write_text(emit_manifest("t", links=None, bumps=bumps, repo="lsjpwr/sunnypilot"),
                      encoding="utf-8")
    entries = map_download.read_manifest(str(path))
    self.assertEqual([e.name for e in entries], ["korea_bumps.sqlite"])
    self.assertEqual(entries[0].sha256, sha256_of(bumps))

  def test_a_database_below_its_floor_raises_instead_of_emitting(self):
    """The failure deploy.py exists to prevent: uploading the 220 MB link database and
    finding out only on the device that it was short a row. A build that fails verify()
    must raise, not produce a manifest entry that tells every device it is safe to install."""
    with mock.patch.object(deploy, "MIN_BUMPS", 2):
      bumps = self.good_bumps_db(1)
      with self.assertRaises(ValueError):
        emit_manifest("t", links=None, bumps=bumps, repo="lsjpwr/sunnypilot")
