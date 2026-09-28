#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import argparse
from collections import Counter

import numpy as np

from opendbc.car import structs
from opendbc.car.tesla.radar_interface import DAS_DX_SNA, DAS_ID_SNA, DAS_OBJECT_SIGNAL_PREFIX, DAS_VX_REL_SNA, RadarInterface
from opendbc.car.tesla.values import CANBUS, CAR, TeslaFlags
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.radard import RADAR_TO_CAMERA, split_das_points
from openpilot.sunnypilot.selfdrive.controls.lib.das_lead import EVIDENCE_PROB, DasLeadConfirmer
from openpilot.tools.lib.logreader import LogReader

DESCRIPTION = """Reports what Tesla's DAS_object (0x309) looks like in a route and what DAS lead confirmation would
have done with it. It decodes raw CAN, so routes recorded before the feature existed work too."""

DAS_OBJECT_ADDR = 0x309
ONSET_WINDOW = 3.0  # s: a confirmation the model doesn't follow within this is a potential phantom lead


class AlwaysOn:
  """Params stand-in: the report shows what the confirmer would do with the switch on."""

  def get(self, key, return_default=False):
    return True


def lead_onsets(frames) -> tuple[list[float], int, int]:
  """frames are (t, filtered model prob, slot 0 confirmed, trusted) per model frame. An onset is the filtered prob
  crossing radard's 0.5 gate. A confirmation run is consecutive frames where only the confirmation makes the lead.

  Returns how many ms each onset came after the confirmation had already made the lead, the number of runs that
  start without a lead and are not followed by an onset within ONSET_WINDOW, and the number of runs that kept a
  model lead past its own gate."""
  times = [f[0] for f in frames]
  lead = [f[1] > 0.5 for f in frames]
  confirmed_only = [f[2] and f[1] <= 0.5 for f in frames]
  onsets = [k for k in range(1, len(frames)) if lead[k] and not lead[k - 1]]

  advantages = []
  for k in onsets:
    j = k
    while j > 0 and confirmed_only[j - 1]:
      j -= 1
    advantages.append((times[k] - times[j]) * 1e3)

  phantoms = extensions = 0
  for a in range(len(frames)):
    if not confirmed_only[a] or (a > 0 and confirmed_only[a - 1]):
      continue
    if a > 0 and lead[a - 1]:
      extensions += 1
    elif not any(times[a] < times[k] <= times[a] + ONSET_WINDOW for k in onsets):
      phantoms += 1
  return advantages, phantoms, extensions


def analyze(msgs) -> dict:
  CP = structs.CarParams.new_message(carFingerprint=str(CAR.TESLA_MODEL_Y), radarUnavailable=True,
                                     flags=int(TeslaFlags.HW4_GEN2 | TeslaFlags.HW4_GEN2_VEHICLE_BUS))
  ri = RadarInterface(CP, structs.CarParamsSP())
  assert ri.das_cp is not None
  confirmer = DasLeadConfirmer(params=AlwaysOn())
  prob_filter = FirstOrderFilter(0.0, 0.2, DT_MDL)  # radard's lead prob filter, fed model probs only

  can_times: list[float] = []
  bus_counts: Counter[int] = Counter()
  mux_counts: Counter[int] = Counter()
  slot_counts: dict[int, Counter[str]] = {object_id: Counter() for object_id in DAS_OBJECT_SIGNAL_PREFIX}
  slot_dx: dict[int, list[float]] = {object_id: [] for object_id in DAS_OBJECT_SIGNAL_PREFIX}
  das_lead = das_cutin = None
  v_ego = 0.0
  frames = []  # per model frame: (t, filtered model prob, slot 0 confirmed, trusted)
  distance_errors: list[float] = []
  sign_agreement: list[bool] = []

  for msg in msgs:
    t = msg.logMonoTime * 1e-9
    which = msg.which()
    if which == "can":
      can_times.append(t)
      bus_counts.update(c.src for c in msg.can if c.address == DAS_OBJECT_ADDR)
      rr = ri.update([(msg.logMonoTime, [(c.address, c.dat, c.src) for c in msg.can])])

      vl = ri.das_cp.vl_all["DAS_object"]  # the frames this update() parsed
      for i, object_id in enumerate(vl["DAS_objectId"]):
        object_id = int(object_id)
        mux_counts[object_id] += 1
        prefix = DAS_OBJECT_SIGNAL_PREFIX.get(object_id)
        if prefix is None:
          continue
        counts, d_rel = slot_counts[object_id], vl[f"{prefix}Dx"][i]
        counts["frames"] += 1
        counts["dx_sna"] += d_rel >= DAS_DX_SNA
        counts["vx_sna"] += vl[f"{prefix}VxRel"][i] >= DAS_VX_REL_SNA
        counts["id_sna"] += int(vl[f"{prefix}Id"][i]) == DAS_ID_SNA
        counts["relevant"] += vl[f"{prefix}RelevantForControl"][i] == 1
        if d_rel < DAS_DX_SNA:
          slot_dx[object_id].append(d_rel)

      if rr is not None:
        das_lead, das_cutin = split_das_points(rr)

    elif which == "carState":
      v_ego = msg.carState.vEgo

    elif which == "modelV2" and len(msg.modelV2.leadsV3) > 1:
      leads = msg.modelV2.leadsV3
      candidates = [(leads[i].x[0] - RADAR_TO_CAMERA, -leads[i].y[0], leads[i].prob) for i in range(2)]
      if leads[0].prob > prob_filter.x:
        prob_filter.x = leads[0].prob
      else:
        prob_filter.update(leads[0].prob)
      confirmed = confirmer.update(v_ego, das_lead, das_cutin, candidates)[0]
      frames.append((t, prob_filter.x, confirmed, confirmer.trusted))

      if candidates[0][2] > EVIDENCE_PROB and das_lead is not None:
        distance_errors.append(das_lead[0] - candidates[0][0])
        if abs(candidates[0][1]) > 0.5:
          sign_agreement.append((das_lead[1] > 0) == (candidates[0][1] > 0))

  slots = {}
  for object_id, counts in slot_counts.items():
    n, dx = counts["frames"], sorted(slot_dx[object_id])
    slots[object_id] = {"frames": n, **{k: counts[k] / n if n else 0.0 for k in ("dx_sna", "vx_sna", "id_sna", "relevant")},
                        "dx": (dx[0], float(np.median(dx)), dx[-1]) if dx else None}

  t0 = frames[0][0] if frames else 0.0
  trusted_times = [t - t0 for t, _, _, trusted in frames if trusted]
  advantages_ms, phantoms, extensions = lead_onsets(frames)
  return {
    "can_msgs": len(can_times),
    "duration": can_times[-1] - can_times[0] if can_times else 0.0,
    "bus_counts": bus_counts,
    "mux_counts": mux_counts,
    "slots": slots,
    "distance_errors": distance_errors,
    "sign_agreement": sum(sign_agreement) / len(sign_agreement) if sign_agreement else None,
    "trusted_at": trusted_times[0] if trusted_times else None,
    "trusted_ratio": len(trusted_times) / len(frames) if frames else 0.0,
    "advantages_ms": advantages_ms,
    "phantoms": phantoms,
    "extensions": extensions,
  }


def print_report(r: dict) -> None:
  if not r["can_msgs"]:
    print("no CAN in this log: use rlogs, qlogs drop CAN")
    return

  print(f"duration: {r['duration']:.0f} s")
  print("0x309 frames by bus: " + (", ".join(f"bus {bus} {n}" for bus, n in sorted(r["bus_counts"].items())) or "none"))
  if not r["mux_counts"]:
    print(f"no DAS_object on bus {CANBUS.vehicle} (vehicle bus): the confirmation would stay off")
    return

  duration = max(r["duration"], 1e-3)
  print("rate by DAS_objectId: " + ", ".join(f"{object_id} {n / duration:.1f} Hz" for object_id, n in sorted(r["mux_counts"].items())))
  for object_id, s in r["slots"].items():
    dx = f"{s['dx'][0]:.1f}/{s['dx'][1]:.1f}/{s['dx'][2]:.1f} m" if s["dx"] else "none"
    ratios = f"SNA dx {s['dx_sna']:.0%} vx {s['vx_sna']:.0%} id {s['id_sna']:.0%}, relevant {s['relevant']:.0%}"
    print(f"{DAS_OBJECT_SIGNAL_PREFIX[object_id]}: {s['frames']} frames, {ratios}, dx min/median/max {dx}")

  errors = np.array(r["distance_errors"])
  if len(errors):
    p90 = np.percentile(np.abs(errors), 90)
    print(f"DAS lead minus confident model lead: {len(errors)} frames, median {np.median(errors):+.1f} m, |error| p90 {p90:.1f} m")
  else:
    print("DAS lead minus confident model lead: no frames with both")
  if r["sign_agreement"] is None:
    print("yRel sign agreement: no frames with a clear lateral offset")
  else:
    print(f"yRel sign agreement: {r['sign_agreement']:.0%} (near 0% means DAS Dy is positive to the right)")

  if r["trusted_at"] is None:
    print("self-check: never trusted DAS")
  else:
    print(f"self-check: trusted DAS after {r['trusted_at']:.1f} s, for {r['trusted_ratio']:.0%} of model frames")

  advantages = r["advantages_ms"]
  if advantages:
    first = sum(a > 0 for a in advantages)
    print(f"lead onsets: {len(advantages)}, confirmation first on {first}, median {np.median(advantages):.0f} ms, max {max(advantages):.0f} ms")
  else:
    print("lead onsets: none")
  print(f"potential phantom leads (confirmed, model not past 0.5 within {ONSET_WINDOW:.0f} s): {r['phantoms']}")
  print(f"model leads kept past their own gate by confirmation: {r['extensions']}")


def main():
  parser = argparse.ArgumentParser(description=DESCRIPTION)
  parser.add_argument("routes", nargs="+", help="rlog paths, URLs or route names that LogReader accepts")
  args = parser.parse_args()
  for route in args.routes:
    print(f"=== {route}")
    print_report(analyze(LogReader(route)))


if __name__ == "__main__":
  main()
