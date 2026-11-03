"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import ast
import pathlib
import unittest

OPENPILOT_DIR = pathlib.Path(__file__).resolve().parents[2]
ENDS_BLOCK = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def unreachable_lines(source: str) -> list[int]:
  """Line of every statement that follows a return, raise, continue or break in the same block."""
  found = []
  for node in ast.walk(ast.parse(source)):
    for field in ("body", "orelse", "finalbody"):
      block = getattr(node, field, None)
      if isinstance(block, list):
        found += [after.lineno for before, after in zip(block, block[1:], strict=False) if isinstance(before, ENDS_BLOCK)]
  return found


class TestUnreachableCode(unittest.TestCase):
  def test_catches_code_after_return(self):
    self.assertEqual(unreachable_lines("def f():\n  return 1\n  x = 2\n"), [3])
    self.assertEqual(unreachable_lines("def f(x):\n  if x:\n    return 1\n  return 2\n"), [])

  def test_no_unreachable_code(self):
    """A merge can resolve a conflict by gluing one side's lines after the other side's return,
    where they never run (the mici HUD's MADS fade, merge 5fd014bac). ruff has no rule for it."""
    found = []
    for path in sorted(OPENPILOT_DIR.rglob("*.py")):
      if "third_party" in path.parts:
        continue
      found += [f"{path.relative_to(OPENPILOT_DIR).as_posix()}:{line}" for line in unreachable_lines(path.read_text(encoding="utf-8"))]
    self.assertEqual(found, [], "move these lines above the return, or delete them")


if __name__ == "__main__":
  unittest.main()
