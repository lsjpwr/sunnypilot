"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pathlib
import re
import unittest

PARAMS_KEYS_H = pathlib.Path(__file__).resolve().parents[4] / "common" / "params_keys.h"
# loggerd copies the value of every param without DONT_LOG into initData, the first record of
# every boot log and qlog, and the uploaders send both off the device (system/loggerd/logger.cc).
SECRETS = ("GithubLogToken", "KoreaMapApiKey", "KoreaRouteApiKey", "KoreaTeslaClientId", "KoreaTeslaOwnerRefreshToken",
           "KoreaTeslaRefreshToken", "KoreaTeslaVin")


class TestSecretParams(unittest.TestCase):
  def test_keys_and_tokens_never_reach_a_log(self):
    text = PARAMS_KEYS_H.read_text(encoding="utf-8")
    for key in SECRETS:
      with self.subTest(key=key):
        entry = re.search(r'\{"' + key + r'", \{([^}]*)\}', text)
        self.assertIsNotNone(entry, f"{key} is not registered")
        self.assertIn("DONT_LOG", entry.group(1))


if __name__ == "__main__":
  unittest.main()
