"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pathlib
import re
import unittest

MAPD_DIR = pathlib.Path(__file__).resolve().parents[2]
TOP_LEVEL_CEREAL = re.compile(r"^\s*(import|from)\s+cereal\b")


class TestDeviceImports(unittest.TestCase):
  def test_no_top_level_cereal_import(self):
    """Device code imports cereal as openpilot.cereal: the device puts only the repo root on
    PYTHONPATH. A top-level `cereal` import kills a thread on its first line there, and a
    sys.modules fake of the same wrong path hides it from the thread's own test."""
    found = []
    for path in sorted(MAPD_DIR.rglob("*.py")):
      if "tests" in path.relative_to(MAPD_DIR).parts:
        continue
      for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if TOP_LEVEL_CEREAL.match(line):
          found.append(f"{path.relative_to(MAPD_DIR).as_posix()}:{number}")
    self.assertEqual(found, [], "import cereal as openpilot.cereal")


if __name__ == "__main__":
  unittest.main()
