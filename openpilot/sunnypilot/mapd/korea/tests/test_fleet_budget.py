import datetime
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from openpilot.sunnypilot.mapd.korea.fleet_budget import FleetBudget


def reserve_one(args):
  path, today = args
  return FleetBudget(path, today=lambda: today).reserve(1)


class TestFleetBudget(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.path = Path(self.directory.name) / "fleet_budget.json"
    self.today = datetime.date(2026, 10, 24)

  def budget(self):
    return FleetBudget(self.path, today=lambda: self.today)

  def test_restart_retains_reservations_and_allows_exactly_9000(self):
    self.assertEqual(self.budget().reserve(8998), 0)
    self.assertEqual(self.budget().reserve(2), 0)
    self.assertEqual(self.budget().reserve(1), 3)

  def test_month_rollover_and_clock_rollback(self):
    self.assertEqual(self.budget().reserve(9000), 0)
    self.today = datetime.date(2026, 11, 1)
    self.assertEqual(self.budget().reserve(2), 0)
    self.today = datetime.date(2026, 10, 31)
    self.assertEqual(self.budget().reserve(1), 4)

  def test_corrupt_ledger_never_resets_even_next_month(self):
    for record in ('bad json', '{}', 'null', '{"month":"2026-10","units":true}',
                   '{"month":"2026-10","units":-1}', '{"month":"2026-13","units":0}'):
      with self.subTest(record=record):
        self.path.write_text(record)
        self.assertEqual(self.budget().reserve(1), 4)

  def test_deleted_ledger_does_not_reset_a_previous_budget(self):
    self.assertEqual(self.budget().reserve(9000), 0)
    self.path.unlink()
    self.assertEqual(self.budget().reserve(1), 4)

  def test_unreadable_ledger_and_failed_write_replace_or_sync_fail_closed(self):
    self.assertEqual(self.budget().reserve(2), 0)
    for boundary in ('pathlib.Path.read_text', 'tempfile.NamedTemporaryFile', 'os.replace', 'os.fsync'):
      with self.subTest(boundary=boundary), mock.patch(boundary, side_effect=OSError("storage unavailable")):
        self.assertEqual(self.budget().reserve(1), 4)
    with mock.patch('os.fsync', side_effect=[None, OSError("directory fsync failed")]):
      self.assertEqual(self.budget().reserve(1), 4)

  def test_overlapping_threads_and_processes_never_overwrite_newer_reservations(self):
    for executor in (ThreadPoolExecutor, ProcessPoolExecutor):
      with self.subTest(executor=executor.__name__):
        self.path.write_text(json.dumps({"month": "2026-10", "units": 8998}))
        options = {"mp_context": multiprocessing.get_context("spawn")} if executor is ProcessPoolExecutor else {}
        with executor(max_workers=4, **options) as pool:
          results = list(pool.map(reserve_one, [(self.path, self.today)] * 8))
        self.assertEqual(sorted(results), [0, 0, 3, 3, 3, 3, 3, 3])
        self.assertEqual(self.budget().reserve(1), 3)


if __name__ == "__main__":
  unittest.main()
