"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error

import zstandard

from openpilot.sunnypilot.system.github_uploader import (KEEP_DRIVES, SETTLE_S, SWAGLOG_AFTER_S, GithubReleases, GithubUploader, drive_windows,
                                                         is_uploaded, mark_uploaded, pending_qlogs, pending_swaglogs)

NOW = 1_790_000_000.
OLD = NOW - 3600.  # settled long ago
TOKEN = "github_pat_TEST"
REPO = "owner/comma-logs"


def route(counter: int) -> str:
  return f"{counter:08x}--0123456789"


class LogDirs(unittest.TestCase):
  """A throwaway realdata and swaglog directory per test."""

  def setUp(self):
    root = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, root)
    self.log_root = os.path.join(root, "realdata")
    self.swaglog_root = os.path.join(root, "log")
    os.makedirs(self.log_root)
    os.makedirs(self.swaglog_root)

  def segment(self, counter: int, num: int, mtime: float = OLD, locked: bool = False, qlog: bytes = b"qlog") -> str:
    directory = os.path.join(self.log_root, f"{route(counter)}--{num}")
    os.makedirs(directory)
    path = os.path.join(directory, "qlog.zst")
    with open(path, "wb") as f:
      f.write(qlog)
    os.utime(path, (mtime, mtime))
    if locked:
      open(os.path.join(directory, "rlog.lock"), "w").close()
    os.utime(directory, (mtime, mtime))  # last: creating the files above moved it
    return path

  def swaglog(self, index: int, mtime: float, text: bytes = b'{"msg$s": "x"}\n') -> str:
    path = os.path.join(self.swaglog_root, f"swaglog.{index:010d}")
    with open(path, "wb") as f:
      f.write(text)
    os.utime(path, (mtime, mtime))
    return path


class TestPendingQlogs(LogDirs):
  def test_finished_segments_go_oldest_first_named_by_segment(self):
    a0, a1, b0 = self.segment(1, 0), self.segment(1, 1), self.segment(2, 0)
    self.assertEqual(pending_qlogs(self.log_root, NOW), [(route(1), f"{route(1)}--0--qlog.zst", a0),
                                                        (route(1), f"{route(1)}--1--qlog.zst", a1),
                                                        (route(2), f"{route(2)}--0--qlog.zst", b0)])

  def test_the_segment_being_written_waits(self):
    self.segment(1, 0)
    self.segment(1, 1, locked=True)
    self.assertEqual([name for _, name, _ in pending_qlogs(self.log_root, NOW)], [f"{route(1)}--0--qlog.zst"])

  def test_a_lock_left_by_a_power_cut_does_not_hold_a_segment_back(self):
    stale = self.segment(1, 5, locked=True)
    self.segment(2, 0)
    self.assertIn(stale, [path for _, _, path in pending_qlogs(self.log_root, NOW)])

  def test_a_segment_closed_moments_ago_waits_for_its_files_to_settle(self):
    path = self.segment(1, 0, mtime=NOW - SETTLE_S + 1)
    self.assertEqual(pending_qlogs(self.log_root, NOW), [])
    self.assertEqual([p for _, _, p in pending_qlogs(self.log_root, NOW + 1)], [path])

  def test_uploaded_qlogs_are_skipped(self):
    done = self.segment(1, 0)
    mark_uploaded(done)
    self.assertTrue(is_uploaded(done))
    self.assertEqual(pending_qlogs(self.log_root, NOW), [])

  def test_only_the_newest_drives_count(self):
    for counter in range(1, KEEP_DRIVES + 3):
      self.segment(counter, 0)
    self.assertEqual([r for r, _, _ in pending_qlogs(self.log_root, NOW)], [route(c) for c in range(3, KEEP_DRIVES + 3)])

  def test_boot_and_crash_folders_are_not_drives(self):
    os.makedirs(os.path.join(self.log_root, "boot"))
    os.makedirs(os.path.join(self.log_root, "crash"))
    self.assertEqual(pending_qlogs(self.log_root, NOW), [])


class TestPendingSwaglogs(LogDirs):
  def test_each_file_goes_to_the_drive_it_was_written_during(self):
    self.segment(1, 0, mtime=OLD)
    self.segment(2, 0, mtime=NOW - 60)
    during_1 = self.swaglog(1, OLD - 30)
    after_1 = self.swaglog(2, OLD + SWAGLOG_AFTER_S - 1)
    self.swaglog(3, OLD + SWAGLOG_AFTER_S + 1)  # parked with the device on
    during_2 = self.swaglog(4, NOW - 60)
    self.swaglog(5, NOW)  # still being written
    self.assertEqual(pending_swaglogs(self.swaglog_root, drive_windows(self.log_root)),
                     [(route(1), "swaglog.0000000001.zst", during_1), (route(1), "swaglog.0000000002.zst", after_1),
                      (route(2), "swaglog.0000000004.zst", during_2)])

  def test_a_file_in_two_windows_goes_to_the_newer_drive(self):
    self.segment(1, 0, mtime=NOW - 300)
    self.segment(2, 0, mtime=NOW - 60)
    self.swaglog(1, NOW - 200)
    self.swaglog(2, NOW)
    self.assertEqual([r for r, _, _ in pending_swaglogs(self.swaglog_root, drive_windows(self.log_root))], [route(2)])

  def test_uploaded_files_are_skipped(self):
    self.segment(1, 0, mtime=NOW - 60)
    mark_uploaded(self.swaglog(1, NOW - 60))
    self.swaglog(2, NOW)
    self.assertEqual(pending_swaglogs(self.swaglog_root, drive_windows(self.log_root)), [])


def http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
  fp = io.BytesIO(json.dumps(body).encode()) if body is not None else None
  return urllib.error.HTTPError("https://api.github.com", code, "error", None, fp)


class FakeResponse:
  def __init__(self, payload):
    self._body = b"" if payload is None else json.dumps(payload).encode()

  def read(self):
    return self._body

  def __enter__(self):
    return self

  def __exit__(self, *args):
    return False


class ScriptedOpener:
  """urlopen stand-in: answers requests in order from `answers`, a payload or an exception to raise."""

  def __init__(self, *answers):
    self.answers = list(answers)
    self.requests = []

  def __call__(self, request, timeout=None):
    self.requests.append(request)
    answer = self.answers.pop(0)
    if isinstance(answer, Exception):
      raise answer
    return FakeResponse(answer)

  def calls(self):
    return [(r.get_method(), r.full_url) for r in self.requests]


def drive_releases(count: int) -> list[dict]:
  return [{"id": i, "tag_name": route(i), "created_at": f"2026-10-{i:02d}T00:00:00Z"} for i in range(1, count + 1)]


class TestGithubReleases(unittest.TestCase):
  def test_an_existing_release_is_found_by_its_tag(self):
    opener = ScriptedOpener({"id": 7})
    self.assertEqual(GithubReleases(TOKEN, REPO, opener).release_id(route(1)), (7, False))
    self.assertEqual(opener.calls(), [("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{route(1)}")])

  def test_a_missing_release_is_created(self):
    opener = ScriptedOpener(http_error(404), {"id": 8})
    self.assertEqual(GithubReleases(TOKEN, REPO, opener).release_id(route(1)), (8, True))
    self.assertEqual(opener.calls()[1], ("POST", f"https://api.github.com/repos/{REPO}/releases"))
    self.assertEqual(json.loads(opener.requests[1].data)["tag_name"], route(1))

  def test_other_errors_while_finding_a_release_raise(self):
    with self.assertRaises(urllib.error.HTTPError):
      GithubReleases(TOKEN, REPO, ScriptedOpener(http_error(401))).release_id(route(1))

  def test_an_asset_goes_to_the_uploads_host_as_raw_bytes(self):
    opener = ScriptedOpener({"id": 1})
    GithubReleases(TOKEN, REPO, opener).upload(7, f"{route(1)}--0--qlog.zst", b"\x28\xb5\x2f\xfd")
    request = opener.requests[0]
    self.assertEqual((request.get_method(), request.full_url),
                     ("POST", f"https://uploads.github.com/repos/{REPO}/releases/7/assets?name={route(1)}--0--qlog.zst"))
    self.assertEqual(request.data, b"\x28\xb5\x2f\xfd")
    self.assertEqual(request.get_header("Content-type"), "application/octet-stream")

  def test_an_asset_already_there_counts_as_uploaded(self):
    already = http_error(422, {"message": "Validation Failed", "errors": [{"resource": "ReleaseAsset", "code": "already_exists", "field": "name"}]})
    GithubReleases(TOKEN, REPO, ScriptedOpener(already)).upload(7, "a", b"x")

  def test_other_upload_errors_raise(self):
    for error in (http_error(422, {"message": "Validation Failed", "errors": [{"code": "invalid"}]}), http_error(422), http_error(403)):
      with self.subTest(code=error.code), self.assertRaises(urllib.error.HTTPError):
        GithubReleases(TOKEN, REPO, ScriptedOpener(error)).upload(7, "a", b"x")

  def test_prune_keeps_the_newest_drives_and_leaves_other_releases_alone(self):
    releases = drive_releases(KEEP_DRIVES + 2) + [{"id": 99, "tag_name": "v1.0", "created_at": "2020-01-01T00:00:00Z"}]
    opener = ScriptedOpener(releases, None, None, None, None)
    GithubReleases(TOKEN, REPO, opener).prune()
    self.assertEqual(opener.calls()[1:], [("DELETE", f"https://api.github.com/repos/{REPO}/releases/2"),
                                          ("DELETE", f"https://api.github.com/repos/{REPO}/git/refs/tags/{route(2)}"),
                                          ("DELETE", f"https://api.github.com/repos/{REPO}/releases/1"),
                                          ("DELETE", f"https://api.github.com/repos/{REPO}/git/refs/tags/{route(1)}")])

  def test_a_tag_already_gone_does_not_stop_pruning(self):
    opener = ScriptedOpener(drive_releases(KEEP_DRIVES + 1), None, http_error(422))
    GithubReleases(TOKEN, REPO, opener).prune()
    self.assertEqual(len(opener.requests), 3)

  def test_the_token_goes_in_a_header_never_in_a_url(self):
    opener = ScriptedOpener(http_error(404), {"id": 8}, {"id": 1})
    releases = GithubReleases(TOKEN, REPO, opener)
    releases.upload(releases.release_id(route(1))[0], "a", b"x")
    for request in opener.requests:
      self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
      self.assertNotIn(TOKEN, request.full_url)


class FakeReleases:
  """GithubReleases stand-in: hands out release ids and records the uploads."""

  def __init__(self):
    self.ids: dict[str, int] = {}
    self.assets: list[tuple[int, str, bytes]] = []
    self.lookups = 0
    self.prunes = 0
    self.upload_error: Exception | None = None
    self.prune_error: Exception | None = None

  def release_id(self, tag):
    self.lookups += 1
    if tag in self.ids:
      return self.ids[tag], False
    self.ids[tag] = len(self.ids) + 1
    return self.ids[tag], True

  def upload(self, release_id, name, data):
    if self.upload_error is not None:
      raise self.upload_error
    self.assets.append((release_id, name, data))

  def prune(self):
    if self.prune_error is not None:
      error, self.prune_error = self.prune_error, None
      raise error
    self.prunes += 1


class TestGithubUploader(LogDirs):
  def uploader(self, releases: FakeReleases) -> GithubUploader:
    return GithubUploader(releases, self.log_root, self.swaglog_root, clock=lambda: NOW)

  def test_one_file_per_step_qlogs_as_they_are_then_swaglogs_compressed(self):
    qlog = self.segment(1, 0, qlog=b"QLOG")
    swaglog = self.swaglog(1, OLD, b"SWAG")
    self.swaglog(2, NOW)
    releases = FakeReleases()
    uploader = self.uploader(releases)
    self.assertEqual([uploader.step(), uploader.step(), uploader.step()], [True, True, False])
    (first_id, first_name, first_data), (second_id, second_name, second_data) = releases.assets
    self.assertEqual((first_id, first_name, first_data), (1, f"{route(1)}--0--qlog.zst", b"QLOG"))
    self.assertEqual((second_id, second_name), (1, "swaglog.0000000001.zst"))
    self.assertEqual(zstandard.ZstdDecompressor().decompress(second_data), b"SWAG")
    self.assertTrue(is_uploaded(qlog) and is_uploaded(swaglog))

  def test_a_new_drive_gets_its_release_once_and_prunes_old_drives(self):
    self.segment(1, 0)
    self.segment(1, 1)
    releases = FakeReleases()
    uploader = self.uploader(releases)
    uploader.step()
    uploader.step()
    self.assertEqual((releases.ids, releases.lookups, releases.prunes), ({route(1): 1}, 1, 1))

  def test_a_failed_prune_is_tried_again_on_the_next_step(self):
    self.segment(1, 0)
    releases = FakeReleases()
    releases.prune_error = urllib.error.URLError("offline")
    uploader = self.uploader(releases)
    with self.assertRaises(urllib.error.URLError):
      uploader.step()
    self.assertTrue(uploader.step())
    self.assertEqual((releases.prunes, len(releases.assets)), (1, 1))

  def test_a_failed_upload_raises_and_leaves_the_file_pending(self):
    path = self.segment(1, 0)
    releases = FakeReleases()
    releases.upload_error = http_error(500)
    with self.assertRaises(urllib.error.HTTPError):
      self.uploader(releases).step()
    self.assertFalse(is_uploaded(path))

  def test_a_release_deleted_on_github_is_looked_up_again(self):
    self.segment(1, 0)
    releases = FakeReleases()
    releases.upload_error = http_error(404)
    uploader = self.uploader(releases)
    with self.assertRaises(urllib.error.HTTPError):
      uploader.step()
    releases.upload_error = None
    self.assertTrue(uploader.step())
    self.assertEqual(releases.lookups, 2)

  def test_a_file_deleted_before_it_is_read_is_skipped(self):
    releases = FakeReleases()
    uploader = self.uploader(releases)
    uploader.pending = lambda: [(route(1), f"{route(1)}--0--qlog.zst", os.path.join(self.log_root, "gone"))]
    self.assertTrue(uploader.step())
    self.assertEqual(releases.assets, [])

  def test_a_file_that_cannot_be_read_raises_and_stays_pending(self):
    directory = os.path.join(self.log_root, f"{route(1)}--0")
    os.makedirs(os.path.join(directory, "qlog.zst"))  # open() fails with IsADirectoryError, not FileNotFoundError
    os.utime(directory, (OLD, OLD))
    uploader = self.uploader(FakeReleases())
    with self.assertRaises(IsADirectoryError):
      uploader.step()
    self.assertEqual(len(uploader.pending()), 1)


if __name__ == "__main__":
  unittest.main()
