#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

github_uploader: puts each drive's qlogs and swaglog into the owner's own GitHub repo, as the
assets of one release per drive (the tag is the route name), and keeps only the KEEP_DRIVES
newest drives there.

The device's network is the driver's phone hotspot, which leaves with the driver a few minutes
after parking. So each file goes up on its own as soon as it is closed, during the drive, instead
of in one batch at the end.

All it needs is GithubLogRepo ("owner/name") and GithubLogToken, a fine-grained token for that
one repo with Contents read and write. Nothing here talks to comma.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import zstandard

from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.xattr_cache import getxattr, setxattr

# ponytail: one asset per segment and per swaglog file, so a drive over about 8 h reaches GitHub's
# 1000 assets per release. Bundle several segments per asset if drives ever get that long.
KEEP_DRIVES = 10
UPLOAD_ATTR_NAME = "user.github.upload"  # comma's uploader marks user.upload, sunnylink's user.sunny.upload
UPLOAD_ATTR_VALUE = b"1"
# loggerd removes a segment's rlog.lock and only then flushes the segment's files
# (system/loggerd/logger.cc, LoggerState::next and ~LoggerState). Removing the lock is the last
# change to the directory, so a directory left alone this long holds finished files.
SETTLE_S = 5.
# A swaglog file belongs to the drive it was written during: from a little before the drive's
# first qlog to a while after its last, which keeps the lines logged as a drive ends but leaves
# out hours parked with the device on.
SWAGLOG_BEFORE_S = 180.
SWAGLOG_AFTER_S = 600.

SEGMENT_RE = re.compile(r"^([0-9a-f]{8}--[0-9a-f]{10})--(\d+)$")
SWAGLOG_RE = re.compile(r"^swaglog\.(\d+)$")

API_URL = "https://api.github.com"
UPLOADS_URL = "https://uploads.github.com"
HTTP_TIMEOUT_S = 60.  # a qlog is about 0.5 MB, over a phone hotspot
ROUTE_RE = re.compile(r"^[0-9a-f]{8}--[0-9a-f]{10}$")

SWAGLOG_ZSTD_LEVEL = 10  # the level loggerd writes qlogs with (system/loggerd/logger.h)
IDLE_S = 10.
UPLOAD_GAP_S = 1.  # GitHub's secondary rate limit allows 80 content-creating requests a minute
MAX_BACKOFF_S = 300.
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def is_uploaded(path: str) -> bool:
  return getxattr(path, UPLOAD_ATTR_NAME) == UPLOAD_ATTR_VALUE


def mark_uploaded(path: str) -> None:
  try:
    setxattr(path, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
  except OSError:
    # Uploaded but unmarked: the next pass sends it again, and GitHub's "already exists" counts as done.
    cloudlog.exception("github_uploader: could not mark %s", path)


def drives(log_root: str, keep: int = KEEP_DRIVES) -> dict[str, list[str]]:
  """{route: its segment directories in order} for the `keep` newest routes, oldest route first.

  Routes sort by their hex counter (system/loggerd/logger.cc logger_get_identifier). log_root
  also holds boot/ and crash/, which the pattern leaves out."""
  found: dict[str, list[tuple[int, str]]] = {}
  for name in os.listdir(log_root):
    if m := SEGMENT_RE.match(name):
      found.setdefault(m.group(1), []).append((int(m.group(2)), os.path.join(log_root, name)))
  newest = sorted(found, key=lambda route: int(route[:8], 16))[-keep:]
  return {route: [directory for _, directory in sorted(found[route])] for route in newest}


def segment_closed(directory: str, newest: bool, now: float) -> bool:
  """Whether loggerd is done with a segment.

  A lock means loggerd is still writing it -- unless a newer segment exists: loggerd writes one
  segment at a time, so that lock was left behind by a power cut and must not hold the segment
  back forever. abs() because the clock can jump when timed sets it."""
  if newest and os.path.exists(os.path.join(directory, "rlog.lock")):
    return False
  return abs(now - os.stat(directory).st_mtime) >= SETTLE_S


def pending_qlogs(log_root: str, now: float, keep: int = KEEP_DRIVES) -> list[tuple[str, str, str]]:
  """(route, asset name, path) of every finished qlog of the newest drives not uploaded yet, oldest first."""
  routes = drives(log_root, keep)
  newest = next(reversed(routes.values()))[-1] if routes else None
  pending = []
  for route, directories in routes.items():
    for directory in directories:
      qlog = os.path.join(directory, "qlog.zst")
      try:
        if os.path.exists(qlog) and not is_uploaded(qlog) and segment_closed(directory, directory == newest, now):
          pending.append((route, f"{os.path.basename(directory)}--qlog.zst", qlog))
      except OSError:
        pass  # the deleter took the segment while we looked
  return pending


def drive_windows(log_root: str, keep: int = KEEP_DRIVES) -> list[tuple[str, float, float]]:
  """(route, start, end) of the time each of the newest drives' swaglog lines can come from, oldest first."""
  windows = []
  for route, directories in drives(log_root, keep).items():
    mtimes = []
    for directory in directories:
      try:
        mtimes.append(os.stat(os.path.join(directory, "qlog.zst")).st_mtime)
      except OSError:
        pass
    if mtimes:
      windows.append((route, min(mtimes) - SWAGLOG_BEFORE_S, max(mtimes) + SWAGLOG_AFTER_S))
  return windows


def pending_swaglogs(swaglog_root: str, windows: list[tuple[str, float, float]]) -> list[tuple[str, str, str]]:
  """(route, asset name, path) of every closed swaglog file written during a drive and not uploaded yet.

  logmessaged is the only writer, and it appends to the highest-numbered file, so every lower one
  is closed (common/swaglog.py). A file goes to the newest drive whose window holds its mtime; a
  file in no window, like the device on while parked, is never uploaded."""
  files = sorted((int(m.group(1)), name) for name in os.listdir(swaglog_root) if (m := SWAGLOG_RE.match(name)))
  pending = []
  for _, name in files[:-1]:
    path = os.path.join(swaglog_root, name)
    try:
      mtime = os.stat(path).st_mtime
      route = next((r for r, start, end in reversed(windows) if start <= mtime <= end), None)
      if route is not None and not is_uploaded(path):
        pending.append((route, f"{name}.zst", path))
    except OSError:
      pass  # rotated away while we looked
  return pending


def _already_exists(e: urllib.error.HTTPError) -> bool:
  """Whether a 422 from an asset upload means the asset is there already."""
  try:
    errors = json.loads(e.read()).get("errors") or []
  except (ValueError, AttributeError):
    return False
  return any(isinstance(error, dict) and error.get("code") == "already_exists" for error in errors)


class GithubReleases:
  """The GitHub REST calls the uploader needs. `opener` has urlopen's shape, so tests can script answers.

  The token travels in a header, never in a URL: urllib puts URLs in exception messages, and those
  reach cloudlog."""

  def __init__(self, token: str, repo: str, opener=urllib.request.urlopen):
    self._token = token
    self._repo = repo
    self._opener = opener

  def _call(self, method: str, url: str, data: bytes | None = None, content_type: str = "application/json"):
    headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sunnypilot-github-uploader"}
    if data is not None:
      headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with self._opener(request, timeout=HTTP_TIMEOUT_S) as response:
      body = response.read()
    return json.loads(body) if body else None

  def release_id(self, tag: str) -> tuple[int, bool]:
    """(id, newly created) of the release for `tag`."""
    try:
      return self._call("GET", f"{API_URL}/repos/{self._repo}/releases/tags/{tag}")["id"], False
    except urllib.error.HTTPError as e:
      if e.code != 404:
        raise
    body = json.dumps({"tag_name": tag, "name": tag, "body": "qlogs and swaglog of one drive, uploaded by the device"}).encode()
    return self._call("POST", f"{API_URL}/repos/{self._repo}/releases", body)["id"], True

  def upload(self, release_id: int, name: str, data: bytes) -> None:
    url = f"{UPLOADS_URL}/repos/{self._repo}/releases/{release_id}/assets?{urllib.parse.urlencode({'name': name})}"
    try:
      self._call("POST", url, data, "application/octet-stream")
    except urllib.error.HTTPError as e:
      if not (e.code == 422 and _already_exists(e)):
        raise

  def prune(self, keep: int = KEEP_DRIVES) -> None:
    """Delete the device's releases beyond the `keep` newest, with their tags. Other releases stay.

    Newest by creation time on GitHub, not by route counter: GitHub's clock is the one to trust,
    and the counter starts over if the device's params are reset."""
    releases = self._call("GET", f"{API_URL}/repos/{self._repo}/releases?per_page=100") or []
    drive_releases = sorted((r for r in releases if ROUTE_RE.match(str(r.get("tag_name", "")))),
                            key=lambda r: r["created_at"], reverse=True)
    for release in drive_releases[keep:]:
      self._call("DELETE", f"{API_URL}/repos/{self._repo}/releases/{release['id']}")
      try:
        self._call("DELETE", f"{API_URL}/repos/{self._repo}/git/refs/tags/{release['tag_name']}")
      except urllib.error.HTTPError as e:
        if e.code not in (404, 422):  # the tag is gone already
          raise


class GithubUploader:
  """Uploads the oldest pending file per step: finished qlogs first, then closed swaglog files."""

  # The wall clock on purpose: segment_closed compares it with file mtimes.
  def __init__(self, releases, log_root: str, swaglog_root: str, clock=time.time):  # noqa: TID251
    self.releases = releases
    self.log_root = log_root
    self.swaglog_root = swaglog_root
    self._clock = clock
    self._release_ids: dict[str, int] = {}
    self._prune_due = False

  def pending(self) -> list[tuple[str, str, str]]:
    return pending_qlogs(self.log_root, self._clock()) + pending_swaglogs(self.swaglog_root, drive_windows(self.log_root))

  def step(self) -> bool:
    """Upload the oldest pending file. False when nothing is pending; a failed request raises."""
    pending = self.pending()
    if not pending:
      return False
    route, name, path = pending[0]
    try:
      with open(path, "rb") as f:
        data = f.read()
    except OSError:
      return True  # the deleter took it, and the next step moves on
    if name.startswith("swaglog."):
      data = zstandard.ZstdCompressor(level=SWAGLOG_ZSTD_LEVEL).compress(data)

    release_id = self._release_ids.get(route)
    if release_id is None:
      release_id, created = self.releases.release_id(route)
      self._release_ids[route] = release_id
      self._prune_due |= created
    if self._prune_due:
      self.releases.prune()
      self._prune_due = False
    try:
      self.releases.upload(release_id, name, data)
    except urllib.error.HTTPError as e:
      if e.code == 404:
        self._release_ids.pop(route, None)  # deleted on GitHub meanwhile; the next step makes it again
      raise
    mark_uploaded(path)
    return True


def main() -> None:
  from openpilot.cereal import log, messaging
  from openpilot.common.hardware.hw import Paths
  from openpilot.common.params import Params

  params = Params()
  sm = messaging.SubMaster(["deviceState"])
  uploader, credentials, backoff = None, None, 0.
  while True:
    sm.update(0)
    token = (params.get("GithubLogToken") or "").strip()
    repo = (params.get("GithubLogRepo") or "").strip()
    if sm["deviceState"].networkType == log.DeviceState.NetworkType.none or not token or not REPO_RE.match(repo):
      time.sleep(IDLE_S)
      continue
    if (token, repo) != credentials:
      credentials = (token, repo)
      uploader = GithubUploader(GithubReleases(token, repo), Paths.log_root(), Paths.swaglog_root())
    try:
      time.sleep(UPLOAD_GAP_S if uploader.step() else IDLE_S)
      backoff = 0.
    except urllib.error.HTTPError as e:
      # The status only: a response body can echo the request.
      cloudlog.warning("github_uploader: HTTP %d", e.code)
      backoff = min(max(2 * backoff, IDLE_S), MAX_BACKOFF_S)
      time.sleep(backoff)
    except Exception as e:
      # The network, a timeout: the type only, since a message can carry a URL.
      cloudlog.warning("github_uploader: upload failed: %s", type(e).__name__)
      backoff = min(max(2 * backoff, IDLE_S), MAX_BACKOFF_S)
      time.sleep(backoff)


if __name__ == "__main__":
  main()
