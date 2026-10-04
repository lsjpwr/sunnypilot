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
import os
import re

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
