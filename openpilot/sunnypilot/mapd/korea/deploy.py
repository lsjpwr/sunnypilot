#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

deploy: check the built map databases, then copy them to the device.

The link database is 220 MB and ITS republishes it quarterly, for one device. That is a
scp, not a distribution channel -- so this ships no downloader, no version negotiation,
and no delta updates. What it does ship is verification, because the expensive failure
is uploading 220 MB and finding out on the device that it will not open.
"""
import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys

from openpilot.sunnypilot.mapd.korea.build_db import SCHEMA_VERSION

# Below these the file is not worth shipping. The 2026-08 datasets hold 33415 and
# 1557364; a build that lands an order of magnitude short went wrong somewhere upstream.
MIN_CAMERAS = 20000
MIN_LINKS = 1000000
# The 2026-05-15 bump dataset holds 141140 rows; 138024 survive the Korea bounds filter.
# Same ~60% floor as the camera and link thresholds above.
MIN_BUMPS = 83000

DEVICE_DIR = "/data/media/0/korea_map"
CHUNK = 1 << 20


def sha256_of(path: str) -> str:
  digest = hashlib.sha256()
  with open(path, "rb") as f:
    while chunk := f.read(CHUNK):
      digest.update(chunk)
  return digest.hexdigest()


def verify(path: str, table: str, min_rows: int) -> int:
  """Open the database the way the device will and confirm it is worth shipping.

  Raises sqlite3.DatabaseError if the file is not a database, ValueError if the schema
  version is wrong or the table is too short.
  """
  con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
  try:
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
      raise ValueError(f"{path}: schema {row and row[0]!r} != {SCHEMA_VERSION!r}")
    count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if count < min_rows:
      raise ValueError(f"{path}: {count} rows in {table}, expected at least {min_rows}")
    return count
  finally:
    con.close()


def push(path: str, host: str) -> None:
  """Copy one file to the device and confirm it arrived intact."""
  name = os.path.basename(path)
  local = sha256_of(path)
  print(f"{name}: {os.path.getsize(path) / 1e6:.1f} MB, sha256 {local[:12]}...")

  subprocess.run(["ssh", host, f"mkdir -p {DEVICE_DIR}"], check=True)
  subprocess.run(["scp", path, f"{host}:{DEVICE_DIR}/{name}"], check=True)

  result = subprocess.run(["ssh", host, f"sha256sum {DEVICE_DIR}/{name}"],
                          check=True, capture_output=True, text=True)
  remote = result.stdout.split()[0]
  if remote != local:
    raise ValueError(f"{name}: sha256 mismatch after copy ({remote[:12]}... != {local[:12]}...)")
  print(f"{name}: verified on device")


def build_targets(cameras: str, links: str, bumps: str) -> list[tuple[str, str, int]]:
  """(path, table, min_rows) for every database that is actually present.

  Cameras and links are required -- a build without them is a mistake worth failing on.
  Bumps are optional: the feature shipped later than the other two, and a device that
  never got a bump database still wants its speed limits refreshed.
  """
  targets = [(cameras, "cameras", MIN_CAMERAS), (links, "links", MIN_LINKS)]
  if os.path.exists(bumps):
    targets.append((bumps, "bumps", MIN_BUMPS))
  else:
    print(f"{os.path.basename(bumps)}: not present, skipping")
  return targets


def main() -> None:
  parser = argparse.ArgumentParser(description="Verify and deploy the Korean map databases.")
  parser.add_argument("--cameras", default="korea_cameras.sqlite")
  parser.add_argument("--links", default="korea_links.sqlite")
  parser.add_argument("--bumps", default="korea_bumps.sqlite")
  parser.add_argument("--host", help="ssh target, e.g. comma@192.168.1.50. Omit to verify only.")
  args = parser.parse_args()

  targets = build_targets(args.cameras, args.links, args.bumps)
  for path, table, minimum in targets:
    print(f"{os.path.basename(path)}: {verify(path, table, minimum)} rows in {table}")

  if args.host is None:
    print("verified. pass --host to deploy.")
    return

  for path, _, _ in targets:
    push(path, args.host)


if __name__ == "__main__":
  sys.exit(main())
