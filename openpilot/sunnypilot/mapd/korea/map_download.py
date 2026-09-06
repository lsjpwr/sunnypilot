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
import glob
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import urllib.request
from dataclasses import dataclass

from openpilot.sunnypilot.mapd.korea.db import verify

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "map_manifest.json")
MANIFEST_VERSION = 1

HTTP_TIMEOUT_S = 30.       # per socket operation, not for the whole transfer
CHUNK = 1 << 20

CHECK_INTERVAL_S = 7 * 24 * 3600.   # ITS republishes links quarterly; bumps move less
RETRY_INTERVAL_S = 3600.
# sm.update(0) is non-blocking on a socket opened microseconds earlier, so the first
# iteration after every boot reliably has no deviceState yet. That is a startup race, not a
# failure, and parking an hour on it makes a user who just enabled the toggle wait an hour
# for nothing.
STARTUP_WAIT_S = 30.

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
  if not isinstance(payload, dict):
    LOG.warning("map download: manifest at %s is not the expected shape", path)
    return []

  # An unrecognised version is written by a newer producer than this device, so its entries
  # cannot be trusted to mean what they say. Left unchecked it degrades into "every entry
  # dropped" or, worse, "a sha that can never match, retried until the cap forever".
  version = payload.get("manifest_version")
  if version != MANIFEST_VERSION:
    LOG.warning("map download: manifest at %s is version %r, expected %d",
                path, version, MANIFEST_VERSION)
    return []

  databases = payload.get("databases", [])
  if not isinstance(databases, list):
    LOG.warning("map download: manifest at %s has a non-list databases", path)
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
  """Where a manifest entry installs to.

  basename() because the manifest is repo-trusted but sanitising is free, and a name like
  "../../params/d/..." reaching os.path.join is not.
  """
  return os.path.join(map_dir, os.path.basename(name))


def clear_scratch(target: str) -> None:
  """Drop scratch files left by earlier attempts at `target`, whichever thread wrote them.

  A .tmp orphaned by a power loss is never reclaimed otherwise and is never counted as
  reclaimable, so on a tight partition enough_disk refuses forever over space held by the
  previous attempt at the very file we are fetching. Unlinking a live writer's scratch file
  is survivable: it keeps its fd, and its os.replace then fails into download's except,
  which is the same outcome that writer was already heading for.
  """
  for stale in glob.glob(glob.escape(target) + ".*.tmp"):
    try:
      os.remove(stale)
    except OSError:
      LOG.warning("map download: could not remove %s", stale, exc_info=True)


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


def download(entry: Entry, map_dir: str, opener=urllib.request.urlopen,
             stop: threading.Event | None = None) -> bool:
  """Fetch, verify, and install one database. True if it was installed.

  Never raises: a failed download is not worth taking the mapd process down for. Every
  return path leaves a usable database behind -- either the new one or the old one.

  # ponytail: no range resume, so a 220 MB transfer that drops restarts from zero. The
  # retry interval covers it; add Range if a retry loop ever shows up in the logs.
  """
  target = target_path(map_dir, entry.name)
  tmp = None
  try:
    # mkstemp, not a fixed "{target}.tmp": korea_main re-enters on a MapDataSource or
    # KoreaExternalNavEnabled change and builds a second MapDownloader while stop()'s
    # two-second join may just have abandoned the first one mid-transfer. Two writers on one
    # name means the second one's open(tmp, "wb") truncates the first one's file -- and
    # neither sha256 can see it, because each is computed from its own stream and not from
    # the file, so a mixture of two builds can reach os.replace with only verify()'s
    # COUNT(*) between it and the car.
    fd, tmp = tempfile.mkstemp(dir=map_dir, prefix=os.path.basename(entry.name) + ".",
                               suffix=".tmp")
    digest = hashlib.sha256()
    written = 0
    # os.fdopen first, so the with-block owns the descriptor even if opener() raises.
    with os.fdopen(fd, "wb") as out, opener(entry.url, timeout=HTTP_TIMEOUT_S) as response:
      while chunk := response.read(CHUNK):
        if stop is not None and stop.is_set():
          # stop() joins for two seconds and a 220 MB transfer outlives that, so the thread
          # is abandoned rather than stopped. Nothing else can end this writer but itself.
          raise InterruptedError(f"{entry.name}: stopped mid-transfer")
        written += len(chunk)
        if written > entry.bytes:
          # /data/media holds Paths.log_root() too, and loggerd's deleter answers a full
          # partition by deleting the user's oldest routes. An asset uploaded at the wrong
          # size would evict recorded drives to make room for bytes the sha check is about
          # to throw away. entry.bytes is known, so refuse before the disk pays for it.
          raise ValueError(f"{entry.name}: stream ran past the manifest's {entry.bytes} bytes")
        digest.update(chunk)
        out.write(chunk)

    got = digest.hexdigest()
    if got != entry.sha256:
      raise ValueError(f"{entry.name}: sha256 {got[:12]}... != {entry.sha256[:12]}...")

    # Schema and row count on top of the sha, because a correct download of a database
    # built from a broken input is still a database the car should not drive on.
    count = verify(tmp, entry.table, entry.min_rows)

    os.chmod(tmp, 0o644)  # mkstemp makes it 0600, and os.replace would carry that in
    os.replace(tmp, target)
    LOG.info("map download: installed %s, %d rows in %s", entry.name, count, entry.table)
    return True
  except Exception:
    LOG.warning("map download: %s failed, keeping the existing file", entry.name, exc_info=True)
    return False
  finally:
    # A partial .tmp is dead weight on a partition that just proved it was tight, and a
    # stale one would be mistaken for progress by anyone reading the directory.
    if tmp is not None and os.path.exists(tmp):
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
      # Before the disk check, not after: the space an abandoned attempt at this very file
      # is holding is exactly what would make enough_disk refuse forever.
      clear_scratch(target_path(self.map_dir, entry.name))
      if not enough_disk(entry, self.map_dir):
        complete = False
        continue
      if not download(entry, self.map_dir, opener=opener, stop=self._stop):
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
          wait = STARTUP_WAIT_S
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
