from types import SimpleNamespace
from unittest.mock import patch

from openpilot.common.realtime import Ratekeeper
from openpilot.common.test import OpenpilotTestCase


class TestRatekeeper(OpenpilotTestCase):
  def setUp(self):
    super().setUp()
    self.now = 1024.0
    self.sleeps = []
    clock = SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep)
    self.enterContext(patch("openpilot.common.realtime.time", clock))

  def sleep(self, duration):
    self.assertGreater(duration, 0)
    self.sleeps.append(duration)
    self.now += duration

  def test_first_call_sleeps_one_interval(self):
    rk = Ratekeeper(100, print_delay_threshold=None)
    self.assertFalse(rk.keep_time())
    self.assertAlmostEqual(self.now, 1024.01)
    self.assertAlmostEqual(rk.remaining, 0.01)
    self.assertEqual(rk.frame, 1)
    self.assertEqual(len(self.sleeps), 1)

  def test_normal_timing_keeps_original_phase(self):
    for rate in (0.1, 0.5, 1, 20, 100):
      with self.subTest(rate=rate):
        rk = Ratekeeper(rate, print_delay_threshold=None)
        start = self.now
        for frame in range(1, 101):
          self.now += 0.2 / rate
          rk.keep_time()
          self.assertAlmostEqual(self.now, start + (frame + 0.2) / rate)
          self.assertEqual(rk.frame, frame)

  def test_small_jitter_keeps_original_phase(self):
    for rate in (0.1, 0.5, 1, 20, 100):
      with self.subTest(rate=rate):
        rk = Ratekeeper(rate, print_delay_threshold=None)
        start = self.now
        rk.keep_time()
        for frame, work in enumerate([0.2, 0.5, 1.4, 0.1, 0.8, 1.6, 0.1, 0.2] * 4, start=2):
          self.now += work / rate
          rk.keep_time()
          self.assertAlmostEqual(rk._next_frame_time, start + (frame + 1) / rate)

  def test_resync_requires_more_than_one_interval(self):
    for overrun in (0.5, 1.0, 1.01):
      with self.subTest(overrun=overrun):
        rk = Ratekeeper(8, print_delay_threshold=None)
        rk.keep_time()
        deadline = rk._next_frame_time
        self.now = deadline + overrun / 8
        rk.monitor_time()
        expected = self.now + 1 / 8 if overrun > 1 else deadline + 1 / 8
        self.assertAlmostEqual(rk._next_frame_time, expected)
        self.assertAlmostEqual(rk.remaining, -overrun / 8)

  def test_long_stall_resumes_sleep_on_next_frame(self):
    for rate in (0.1, 0.5, 1, 20, 100):
      with self.subTest(rate=rate):
        rk = Ratekeeper(rate, print_delay_threshold=None)
        rk.keep_time()
        slept = len(self.sleeps)
        self.now += 50 / rate
        rk.keep_time()
        self.assertEqual(len(self.sleeps), slept)
        self.now += 0.001 / rate
        rk.keep_time()
        self.assertEqual(len(self.sleeps), slept + 1)
        self.assertEqual(rk.frame, 3)

  def test_monitor_only_never_sleeps_and_keeps_overload_visible(self):
    rk = Ratekeeper(100, print_delay_threshold=None)
    rk.monitor_time()
    for _ in range(200):
      self.now += 0.015
      rk.monitor_time()
    self.assertTrue(rk.lagging)
    self.assertEqual(rk.frame, 201)
    self.assertEqual(self.sleeps, [])

  def test_stall_preserves_warning_contract(self):
    for threshold in (None, 0.0, 0.02):
      with self.subTest(threshold=threshold):
        rk = Ratekeeper(100, print_delay_threshold=threshold)
        rk.keep_time()
        self.now += 0.5
        with patch("builtins.print") as warning:
          self.assertEqual(rk.keep_time(), threshold is not None)
        self.assertEqual(warning.called, threshold is not None)
        self.assertLess(rk.remaining, -0.49)
