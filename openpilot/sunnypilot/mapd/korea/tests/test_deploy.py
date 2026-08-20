"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import sqlite3

import pytest

from openpilot.sunnypilot.mapd.korea.build_db import SCHEMA_CAMERAS, insert_cameras, write_db
from openpilot.sunnypilot.mapd.korea.deploy import sha256_of, verify


def good_db(tmp_path, n=5):
  path = str(tmp_path / "korea_cameras.sqlite")
  write_db(path, SCHEMA_CAMERAS,
           lambda con: insert_cameras(con, [(37.5 + i * 1e-4, 127.0, 60, 0) for i in range(n)]))
  return path


def test_verify_returns_the_row_count(tmp_path):
  assert verify(good_db(tmp_path, 5), "cameras", 1) == 5


def test_verify_rejects_a_short_database(tmp_path):
  with pytest.raises(ValueError, match="5 rows"):
    verify(good_db(tmp_path, 5), "cameras", 1000)


def test_verify_rejects_a_file_that_is_not_a_database(tmp_path):
  path = str(tmp_path / "korea_cameras.sqlite")
  with open(path, "wb") as f:
    f.write(b"not a database")
  with pytest.raises(sqlite3.DatabaseError):
    verify(path, "cameras", 1)


def test_verify_rejects_a_schema_version_mismatch(tmp_path):
  path = good_db(tmp_path)
  con = sqlite3.connect(path)
  con.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
  con.commit()
  con.close()
  with pytest.raises(ValueError, match="schema"):
    verify(path, "cameras", 1)


def test_sha256_matches_the_shell_format(tmp_path):
  """The device side runs `sha256sum`, which prints 64 lowercase hex characters."""
  digest = sha256_of(good_db(tmp_path))
  assert len(digest) == 64
  assert digest == digest.lower()
  assert all(c in "0123456789abcdef" for c in digest)
