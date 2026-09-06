"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

map_download: keeps korea_links.sqlite and korea_bumps.sqlite current from GitHub releases.

The link database is 220 MB and the bump database is 11 MB -- too big for the repo, so they
live as release assets. What ships in the repo is map_manifest.json: a few hundred bytes
naming each asset's url, sha256 and row floor. The device already receives the repo through
its normal git update, so checking "is there a newer one" costs no network at all, and the
sha256 that comes with it is the integrity check for a file the car will trust.

korea_cameras.sqlite is deliberately absent. camera_refresh.py rewrites that file from the
data.go.kr API weekly; a second writer on the same path would fight it.

The swap is atomic: download to a sibling .tmp, verify sha256 and schema and row count, then
os.replace() it in. A reader mid-query keeps its old inode and KoreaMapDB.reload_if_changed()
picks the new file up on its next tick. Nothing here needs a lock.

stdlib only, on purpose -- this module runs on the device, where nothing else is installed.
"""
import hashlib
import json
import logging
import os
import shutil
import threading
import urllib.request
from dataclasses import dataclass

from openpilot.sunnypilot.mapd.korea.db import verify

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "map_manifest.json")

HTTP_TIMEOUT_S = 30.       # per socket operation, not for the whole transfer
CHUNK = 1 << 20

CHECK_INTERVAL_S = 7 * 24 * 3600.   # ITS republishes links quarterly; bumps move less
RETRY_INTERVAL_S = 3600.

# The .tmp sits beside the target, so the peak is the old file plus the new one. The extra
# tenth is headroom for whatever else writes to /data/media while this runs.
DISK_HEADROOM = 1.1

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
  name: str
  url: str
  sha256: str
  bytes: int
  table: str
  min_rows: int


def read_manifest(path: str = MANIFEST_PATH) -> list[Entry]:
  """Every well-formed entry, smallest first. Never raises.

  A missing or corrupt manifest means "nothing to download", not a failure -- this file
  arrives through the git update like any other, and a half-written one during an update
  must not take the thread down.
  """
  try:
    with open(path, encoding="utf-8") as f:
      payload = json.load(f)
  except (OSError, ValueError):
    LOG.warning("map download: no usable manifest at %s", path, exc_info=True)
    return []

  # Syntactically valid JSON can still be the wrong shape (a bare list, a number, a string
  # of digits...) -- that is not an OSError or a ValueError, so it must be ruled out here
  # rather than left to crash payload.get() or the loop below.
  databases = payload.get("databases", []) if isinstance(payload, dict) else None
  if not isinstance(databases, list):
    LOG.warning("map download: manifest at %s is not the expected shape", path)
    return []

  entries = []
  for item in databases:
    try:
      entries.append(Entry(name=item["name"], url=item["url"], sha256=item["sha256"],
                           bytes=int(item["bytes"]), table=item["table"],
                           min_rows=int(item["min_rows"])))
    except (KeyError, TypeError, ValueError):
      # one malformed entry must not cost the other database its update
      LOG.warning("map download: skipping a malformed manifest entry: %r", item)
  return sorted(entries, key=lambda e: e.bytes)


def installed_sha256(path: str) -> str | None:
  """The sha256 of what is on disk, or None if there is nothing there.

  Recomputed every check rather than cached in a sidecar file: 220 MB is under a second
  once a week, and a sidecar would go stale the moment someone scp'd a file in by hand.
  """
  digest = hashlib.sha256()
  try:
    with open(path, "rb") as f:
      while chunk := f.read(CHUNK):
        digest.update(chunk)
  except OSError:
    return None
  return digest.hexdigest()


def target_path(map_dir: str, name: str) -> str:
  """Where a manifest entry installs to."""
  return os.path.join(map_dir, name)


def needs_download(entry: Entry, map_dir: str) -> bool:
  return installed_sha256(target_path(map_dir, entry.name)) != entry.sha256


def enough_disk(entry: Entry, map_dir: str) -> bool:
  try:
    free = shutil.disk_usage(map_dir).free
  except OSError:
    return False
  needed = int(entry.bytes * DISK_HEADROOM)
  if free < needed:
    LOG.warning("map download: %s needs %d bytes free, have %d", entry.name, needed, free)
    return False
  return True


def download(entry: Entry, map_dir: str, opener=urllib.request.urlopen) -> bool:
  """Fetch, verify, and install one database. True if it was installed.

  Never raises: a failed download is not worth taking the mapd process down for. Every
  return path leaves a usable database behind -- either the new one or the old one.

  # ponytail: no range resume, so a 220 MB transfer that drops restarts from zero. The
  # retry interval covers it; add Range if a retry loop ever shows up in the logs.
  """
  target = target_path(map_dir, entry.name)
  tmp = f"{target}.tmp"
  try:
    digest = hashlib.sha256()
    with opener(entry.url, timeout=HTTP_TIMEOUT_S) as response, open(tmp, "wb") as out:
      while chunk := response.read(CHUNK):
        digest.update(chunk)
        out.write(chunk)

    got = digest.hexdigest()
    if got != entry.sha256:
      raise ValueError(f"{entry.name}: sha256 {got[:12]}... != {entry.sha256[:12]}...")

    # Schema and row count on top of the sha, because a correct download of a database
    # built from a broken input is still a database the car should not drive on.
    count = verify(tmp, entry.table, entry.min_rows)

    os.replace(tmp, target)
    LOG.info("map download: installed %s, %d rows in %s", entry.name, count, entry.table)
    return True
  except Exception:
    LOG.warning("map download: %s failed, keeping the existing file", entry.name, exc_info=True)
    return False
  finally:
    # A partial .tmp is dead weight on a partition that just proved it was tight, and a
    # stale one would be mistaken for progress by anyone reading the directory.
    if os.path.exists(tmp):
      try:
        os.remove(tmp)
      except OSError:
        LOG.warning("map download: could not remove %s", tmp, exc_info=True)


class MapDownloader:
  """Background thread that keeps the manifest's databases current.

  Owns no database handle. It only replaces files; KoreaMapDB notices on its own.
  """

  def __init__(self, map_dir: str):
    self.map_dir = map_dir
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None
    self._fails = 0

  def start(self) -> None:
    if self._thread is not None:
      return
    self._thread = threading.Thread(target=self._loop, daemon=True)
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=2.)
      self._thread = None

  def _run_once(self, manifest_path: str = MANIFEST_PATH, opener=urllib.request.urlopen) -> bool:
    """One pass over the manifest. True if everything it wanted is now installed.

    Returns False when anything was skipped or failed, so the caller retries sooner.
    """
    complete = True
    for entry in read_manifest(manifest_path):
      if self._stop.is_set():
        return False
      if not needs_download(entry, self.map_dir):
        continue
      if not enough_disk(entry, self.map_dir):
        complete = False
        continue
      if not download(entry, self.map_dir, opener=opener):
        complete = False
    return complete

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is what
    # lets the tests run under a bare interpreter. Same pattern as CameraRefresher._loop.
    import cereal.messaging as messaging
    from openpilot.common.params import Params

    params = Params()
    sm = messaging.SubMaster(['deviceState'])

    while not self._stop.is_set():
      wait = RETRY_INTERVAL_S
      try:
        sm.update(0)
        if not params.get_bool("KoreaMapAutoDownload"):
          # Left at RETRY_INTERVAL_S, not promoted to CHECK_INTERVAL_S: this is not a
          # failure, so it must not feed the backoff below, and polling hourly is what lets
          # a newly-enabled toggle take effect within the hour instead of up to seven days.
          LOG.info("map download: KoreaMapAutoDownload is off")
        elif not sm.recv_frame['deviceState']:
          # A SubMaster that has received nothing reports networkMetered False, which is the
          # capnp default, not an answer. On the first tick after boot that would start a
          # 220 MB download over a metered link.
          LOG.info("map download: no deviceState yet, waiting")
        elif sm['deviceState'].started:
          # Every settings surface gates *changing* this toggle on being offroad; nothing
          # gated the download itself, so a toggle already on pulled 220 MB mid-drive. The
          # metered check is no substitute: on cellular networkMetered is just the user's
          # GsmMetered tap, and over Wi-Fi it is False with nothing misconfigured at all.
          LOG.info("map download: onroad, waiting")
        elif sm['deviceState'].networkMetered:
          LOG.info("map download: network is metered, waiting")
        elif self._run_once():
          self._fails = 0
          wait = CHECK_INTERVAL_S
        else:
          # An incomplete pass (a failed download, or a manifest sha that can never match a
          # hosted asset) must not retry 220 MB every hour forever. Back off geometrically,
          # capped at 24x -- a success snaps this back to 0.
          self._fails += 1
          wait = RETRY_INTERVAL_S * min(2 ** self._fails, 24)
      except Exception:
        # This thread has no supervisor. Anything that escapes here ends map downloads for
        # the life of the process, silently -- so the guard goes around the whole body
        # rather than around whichever call raised today.
        LOG.exception("map download: unexpected error, retrying later")
      self._stop.wait(wait)
