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
import json
import os
import subprocess
import sys

from openpilot.sunnypilot.mapd.korea.db import verify

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


RELEASE_URL = "https://github.com/{repo}/releases/download/{tag}/{name}"


def emit_manifest(tag: str, links: str | None, bumps: str | None, repo: str) -> str:
  """The manifest JSON for a release, as a string.

  Generated rather than hand-written: every field but the tag is derived from the file that
  will actually be uploaded, so the sha256 in the manifest cannot disagree with the asset.
  A file that is not present is left out -- publishing only the bump database is a normal
  thing to want. A file that is present is only ever entered into the manifest after it
  passes the same verify() check the device will run -- a manifest is worthless as a
  pre-flight check if it can describe a build the device was always going to reject.
  """
  databases = []
  for path, name, table, min_rows in ((links, "korea_links.sqlite", "links", MIN_LINKS),
                                      (bumps, "korea_bumps.sqlite", "bumps", MIN_BUMPS)):
    if path is None or not os.path.exists(path):
      continue
    # The device installs under `name` and looks for nothing else, but `gh` uploads the
    # asset under the basename it was handed. A mismatch produces a manifest whose name is
    # right and whose url 404s, so it has to fail here rather than fleet-wide afterwards.
    got = os.path.basename(path)
    if got != name:
      raise ValueError(f"{path}: the device only installs {name}, so uploading it as {got} 404s every device")
    verify(path, table, min_rows)
    databases.append({
      "name": name,
      "url": RELEASE_URL.format(repo=repo, tag=tag, name=name),
      "sha256": sha256_of(path),
      "bytes": os.path.getsize(path),
      "table": table,
      "min_rows": min_rows,
    })
  return json.dumps({"manifest_version": 1, "databases": databases}, indent=2) + "\n"


def main() -> None:
  parser = argparse.ArgumentParser(description="Verify and deploy the Korean map databases.")
  parser.add_argument("--cameras", default="korea_cameras.sqlite")
  parser.add_argument("--links", default="korea_links.sqlite")
  parser.add_argument("--bumps", default="korea_bumps.sqlite")
  parser.add_argument("--host", help="ssh target, e.g. comma@192.168.1.50. Omit to verify only.")
  parser.add_argument("--emit-manifest", metavar="TAG",
                      help="print the manifest json for a release tag instead of deploying")
  parser.add_argument("--repo", default="lsjpwr/sunnypilot",
                      help="github repo that hosts the release assets")
  args = parser.parse_args()

  if args.emit_manifest:
    print(emit_manifest(args.emit_manifest, links=args.links, bumps=args.bumps, repo=args.repo),
          end="")
    return

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
