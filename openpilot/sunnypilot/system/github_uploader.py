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
of in one batch at the end. The newest drive and its diagnostics go before the backlog.

All it needs is GithubLogRepo ("owner/name") and GithubLogToken, a fine-grained token for that
one repo with Contents read and write. Nothing here talks to comma.

It also keeps cameras.md at the top of that repo: the recent and the excluded speed cameras as map
links, which mapd writes to KoreaCameraPage (korea_map_data.camera_page). That goes up while parked,
once no log file is waiting.
"""
import base64
from datetime import UTC, datetime, timedelta, timezone
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import zstandard

from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import SEGMENT_LENGTH
from openpilot.system.loggerd.xattr_cache import getxattr, setxattr

# ponytail: one asset per segment and per swaglog file, so a drive over about 8 h reaches GitHub's
# 1000 assets per release. Bundle several segments per asset if drives ever get that long.
KEEP_DRIVES = 30  # routes, one per onroad cycle: a busy day makes about ten
UPLOAD_ATTR_NAME = "user.github.upload"  # comma's uploader marks user.upload, sunnylink's user.sunny.upload
UPLOAD_ATTR_VALUE = b"1"
UPLOAD_ATTR_SKIPPED = b"skipped"
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
UPLOAD_GAP_S = 10.  # Leave room for release metadata and cleanup under GitHub's 500 content requests/hour.
MAX_BACKOFF_S = 300.
RATE_LIMIT_BACKOFF_S = 60.  # once GitHub rate-limits, it asks for at least a minute before the next try
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
PAGE_PATH = "cameras.md"
STATUS_UPDATE_S = 60.


def is_uploaded(path: str) -> bool:
  return getxattr(path, UPLOAD_ATTR_NAME) in (UPLOAD_ATTR_VALUE, UPLOAD_ATTR_SKIPPED)


def mark_uploaded(path: str, skipped: bool = False) -> None:
  try:
    setxattr(path, UPLOAD_ATTR_NAME, UPLOAD_ATTR_SKIPPED if skipped else UPLOAD_ATTR_VALUE)
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
      # A segment closed before timed set the clock (the device boots two months behind) keeps a
      # bogus early mtime, so the start can't be earlier than the drive's segments allow.
      start = max(min(mtimes), max(mtimes) - SEGMENT_LENGTH * (len(directories) + 1))
      windows.append((route, start - SWAGLOG_BEFORE_S, max(mtimes) + SWAGLOG_AFTER_S))
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


class PartialAsset(Exception):
  """GitHub held a half-made asset under this name (or doesn't list it yet): the file stays pending and goes up again."""


def qlog_start(data: bytes) -> float | None:
  """The recorded drive start, never the date GitHub happened to receive the file."""
  from openpilot.cereal import log

  try:
    raw = zstandard.ZstdDecompressor().stream_reader(data).read()
    start, first_mono, gps, clock = None, None, None, None
    for event in log.Event.read_multiple_bytes(raw):
      kind = event.which()
      if kind == "initData" and event.initData.wallTimeNanos and start is None:
        start = event.initData.wallTimeNanos * 1e-9
        first_mono = event.logMonoTime
      elif kind in ("gpsLocation", "gpsLocationExternal"):
        location = getattr(event, kind)
        if location.hasFix and location.unixTimestampMillis > 0:
          gps = (event.logMonoTime, location.unixTimestampMillis * 1_000_000)
      elif kind == "clocks" and event.clocks.wallTimeNanos:
        clock = (event.logMonoTime, event.clocks.wallTimeNanos)
    if start is not None:
      # initData can predate timed's clock correction; a fixed GPS or the last clock gives the real start.
      reference = gps or clock
      if reference is not None:
        start = (reference[1] + first_mono - reference[0]) * 1e-9
      datetime.fromtimestamp(start, UTC)  # reject an invalid clock without blocking the log upload
      return start
  except Exception:
    pass  # malformed logs are still worth uploading for diagnosis
  return None


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
      self._check_existing(release_id, name, len(data))

  def _check_existing(self, release_id: int, name: str, size: int) -> None:
    """Accept an asset already there only if it is whole.

    An upload cut off midway leaves a half-made asset holding the name, and GitHub answers every
    retry with already_exists. Such an asset is deleted and PartialAsset raised, so the file stays
    pending and goes up again."""
    page = 1
    while assets := self._call("GET", f"{API_URL}/repos/{self._repo}/releases/{release_id}/assets?per_page=100&page={page}"):
      asset = next((a for a in assets if a.get("name") == name), None)
      if asset is not None:
        if asset.get("state") == "uploaded" and asset.get("size") == size:
          return
        self._call("DELETE", f"{API_URL}/repos/{self._repo}/releases/assets/{asset['id']}")
        raise PartialAsset(name)
      page += 1
    raise PartialAsset(name)  # not listed yet: try again later

  def prune(self, keep: int = KEEP_DRIVES, protected: set[str] | None = None) -> None:
    """Delete the device's releases beyond the `keep` newest, with their tags. Other releases stay.

    Protect the device's newest local drives even when older backfill was published later.
    Fill any remaining slots by publication time, including after a device counter reset."""
    releases = self._call("GET", f"{API_URL}/repos/{self._repo}/releases?per_page=100") or []
    drive_releases = sorted((r for r in releases if ROUTE_RE.match(str(r.get("tag_name", "")))),
                            key=lambda r: r.get("published_at") or r["created_at"], reverse=True)
    protected = protected or set()
    older = [r for r in drive_releases if r["tag_name"] not in protected]
    slots = max(0, keep - sum(r["tag_name"] in protected for r in drive_releases))
    for release in older[slots:]:
      self._call("DELETE", f"{API_URL}/repos/{self._repo}/releases/{release['id']}")
      try:
        self._call("DELETE", f"{API_URL}/repos/{self._repo}/git/refs/tags/{release['tag_name']}")
      except urllib.error.HTTPError as e:
        if e.code not in (404, 422):  # the tag is gone already
          raise

  def update_release(self, release_id: int, name: str, body: str) -> None:
    self._call("PATCH", f"{API_URL}/repos/{self._repo}/releases/{release_id}", json.dumps({"name": name, "body": body}).encode())

  def put_file(self, path: str, text: str) -> bool:
    """Make `path` on the default branch hold `text`. False, and no commit, when it does already, as
    after a restart."""
    url = f"{API_URL}/repos/{self._repo}/contents/{path}"
    try:
      current = self._call("GET", url)
    except urllib.error.HTTPError as e:
      if e.code != 404:
        raise
      current = None
    data = text.encode()
    body = {"message": f"update {path}", "content": base64.b64encode(data).decode()}
    if current is not None:
      if base64.b64decode(current.get("content", "")) == data:  # GitHub breaks it into lines, which b64decode skips
        return False
      body["sha"] = current["sha"]
    self._call("PUT", url, json.dumps(body).encode())
    return True


class GithubUploader:
  """Uploads the newest drive's finished qlogs and diagnostics before older drives."""

  # The wall clock on purpose: segment_closed compares it with file mtimes.
  def __init__(self, releases, log_root: str, swaglog_root: str, clock=time.time):  # noqa: TID251
    self.releases = releases
    self.log_root = log_root
    self.swaglog_root = swaglog_root
    self._clock = clock
    self._release_ids: dict[str, int] = {}
    self._prune_due = False
    self._page_sent: str | None = None
    self._start_times: dict[str, float | None] = {}
    self._status_due: set[str] = set()
    self._status_updated: dict[str, float] = {}

  def pending(self) -> list[tuple[str, str, str]]:
    files = pending_qlogs(self.log_root, self._clock()) + pending_swaglogs(self.swaglog_root, drive_windows(self.log_root))
    # Stable: within each drive, qlogs remain in segment order, then its diagnostics.
    return sorted(files, key=lambda item: int(item[0][:8], 16), reverse=True)

  def _publish_status(self, pending: list[tuple[str, str, str]]) -> bool:
    now = self._clock()
    routes = drives(self.log_root)
    for route in sorted(self._status_due, reverse=True):
      waiting = [name for r, name, _ in pending if r == route]
      if waiting and abs(now - self._status_updated.get(route, 0.)) < STATUS_UPDATE_S:
        continue
      self._status_updated[route] = now
      try:
        paths = [os.path.join(d, "qlog.zst") for d in routes.get(route, []) if os.path.isfile(os.path.join(d, "qlog.zst"))]
        if route not in self._start_times:
          self._start_times[route] = None
          if paths:
            with open(paths[0], "rb") as f:
              self._start_times[route] = qlog_start(f.read())
        attributes = [getxattr(p, UPLOAD_ATTR_NAME) for p in paths]
        uploaded = attributes.count(UPLOAD_ATTR_VALUE)
        skipped = attributes.count(UPLOAD_ATTR_SKIPPED)
        zone = timezone(timedelta(hours=9))
        start = self._start_times[route]
        started = datetime.fromtimestamp(start, zone).isoformat(timespec="seconds") if start is not None else "unknown"
        name = f"{started} · {route}" if start is not None else route
        body = "\n\n".join((
          f"Drive start (KST): **{started}**",
          f"Route: `{route}`",
          f"qlogs: **{uploaded} uploaded / {len(paths) - uploaded - skipped} waiting / {skipped} skipped** ({len(paths)} locally retained segments).",
          f"Diagnostic logs waiting: **{sum(n.startswith('swaglog.') for n in waiting)}**.",
          f"Updated (KST): {datetime.fromtimestamp(now, zone).isoformat(timespec='seconds')}",
          "The segment still being recorded waits until it closes. Upload continues while the device is connected.",
        ))
        self.releases.update_release(self._release_ids[route], name, body)
      except Exception as e:
        # Optional metadata must never hold up the next drive's logs or expose a response body.
        cloudlog.warning("github_uploader: release status failed: %s", type(e).__name__)
        continue
      self._status_due.remove(route)
      return True
    return False

  def step(self, page: str | None = None) -> bool:
    """Upload the newest drive's next file, or with none pending, `page` as cameras.md if it changed: last, so
    it never holds up a drive's logs. False when there is nothing to do; a failed request raises."""
    pending = self.pending()
    if not pending:
      if self._publish_status(pending):
        return True
      if page and page != self._page_sent:
        if self.releases.put_file(PAGE_PATH, page):
          cloudlog.info("github_uploader: %s updated", PAGE_PATH)
        self._page_sent = page
        return True
      # Cleanup can time out on old releases; it must never stand between a drive and its logs.
      if self._prune_due:
        self.releases.prune(protected=set(drives(self.log_root)))
        self._prune_due = False
      return False
    route, name, path = pending[0]
    try:
      with open(path, "rb") as f:
        data = f.read()
    except FileNotFoundError:
      return True  # the deleter took it, and the next step moves on
    if name.startswith("swaglog."):
      data = zstandard.ZstdCompressor(level=SWAGLOG_ZSTD_LEVEL).compress(data)

    release_id = self._release_ids.get(route)
    if release_id is None:
      release_id, created = self.releases.release_id(route)
      self._release_ids[route] = release_id
      self._prune_due |= created
    try:
      self.releases.upload(release_id, name, data)
    except urllib.error.HTTPError as e:
      if e.code == 404:
        self._release_ids.pop(route, None)  # deleted on GitHub meanwhile; the next step makes it again
      elif e.code == 422:
        # GitHub refuses this file for good (an empty qlog left by a power cut, a release's 1001st
        # asset). Skip it, or it would hold back every file after it.
        cloudlog.warning("github_uploader: GitHub refused %s (HTTP 422), skipped", name)
        mark_uploaded(path, skipped=True)
        self._status_due.add(route)
        self._publish_status(pending[1:])
        return True
      raise
    mark_uploaded(path)
    self._status_due.add(route)
    self._publish_status(pending[1:])
    return True


def next_backoff(backoff: float, http_status: int | None) -> float:
  """Seconds to wait after a failed step: doubling from IDLE_S up to MAX_BACKOFF_S, and at least
  RATE_LIMIT_BACKOFF_S when GitHub rate-limits (403 or 429)."""
  floor = RATE_LIMIT_BACKOFF_S if http_status in (403, 429) else IDLE_S
  return min(max(2 * backoff, floor), MAX_BACKOFF_S)


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
    # cameras.md only while parked: on the road it changes, and would be a commit, at every camera passed
    page = None if sm["deviceState"].started else params.get("KoreaCameraPage")
    status = None
    try:
      time.sleep(UPLOAD_GAP_S if uploader.step(page) else IDLE_S)
      backoff = 0.
      continue
    except urllib.error.HTTPError as e:
      # The status only: a response body can echo the request.
      cloudlog.warning("github_uploader: HTTP %d", e.code)
      status = e.code
    except Exception as e:
      # The network, a timeout: the type only, since a message can carry a URL.
      cloudlog.warning("github_uploader: upload failed: %s", type(e).__name__)
    backoff = next_backoff(backoff, status)
    time.sleep(backoff)


if __name__ == "__main__":
  main()
