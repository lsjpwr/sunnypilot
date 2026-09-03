"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pathlib
import sqlite3
import tempfile
import unittest

from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_BUMPS, SCHEMA_CAMERAS,
                                                      insert_bumps, insert_cameras, write_db)
from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH
from openpilot.sunnypilot.mapd.korea.deploy import MIN_BUMPS, build_targets, sha256_of, verify


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
