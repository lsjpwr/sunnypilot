"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from collections import deque

from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.swaglog import cloudlog

# Tuning. Revisit once tesla_das_report.py has run on real routes.
MIN_CANDIDATE_PROB = 0.2   # raw model prob below which a DAS match confirms nothing
MAX_LATERAL_ERROR = 1.5    # m
EVIDENCE_PROB = 0.9        # raw model prob that makes a frame self-check evidence
MIN_EVIDENCE_SPEED = 5.0   # m/s
MIN_EVIDENCE = 100         # frames, 5 s at 20 Hz
EVIDENCE_WINDOW = 200      # frames, 10 s at 20 Hz
TRUST_ON = 0.8
TRUST_OFF = 0.6
CONFIRM_HOLD_FRAMES = 10   # 0.5 s at 20 Hz, so DAS flicker doesn't flicker the lead


def matches(point: tuple[float, float], candidate: tuple[float, float, float]) -> bool:
  """point is (dRel, yRel), candidate is (dRel, yRel, prob). The distance tolerance is radard's dist_sane."""
  d, y = candidate[0], candidate[1]
  return abs(point[0] - d) < max(0.25 * d, 5.0) and abs(point[1] - y) < MAX_LATERAL_ERROR


class DasLeadConfirmer:
  """Confirms low-confidence model leads that Tesla's own vision (DAS_object) sees in the same place.

  Everything is in radard's radar frame. Nothing is confirmed until DAS has agreed with confident model leads
  for a while, so a firmware whose DAS_object differs from the assumed layout leaves this inert."""

  def __init__(self, params=None):
    self._params = params or Params()
    self._frame = 0
    self._enabled = False
    self._evidence: deque[bool] = deque(maxlen=EVIDENCE_WINDOW)
    self._frames_since_match = [CONFIRM_HOLD_FRAMES + 1] * 2
    self.trusted = False

  def update(self, v_ego: float, das_lead: tuple[float, float] | None, das_cutin: tuple[float, float] | None,
             candidates: list[tuple[float, float, float]]) -> list[bool]:
    """candidates are leadsV3 slots 0 and 1 as (dRel, yRel, raw prob). Returns whether each one is confirmed."""
    if self._frame % int(1. / DT_MDL) == 0:
      self._enabled = bool(self._params.get("TeslaDasLeadConfirm", return_default=True))
    self._frame += 1

    self._update_trust(v_ego, das_lead, candidates[0])

    points = [p for p in (das_lead, das_cutin) if p is not None]
    confirmed = []
    for i, candidate in enumerate(candidates):
      if not (self._enabled and self.trusted) or candidate[2] < MIN_CANDIDATE_PROB:
        self._frames_since_match[i] = CONFIRM_HOLD_FRAMES + 1
      elif any(matches(p, candidate) for p in points):
        self._frames_since_match[i] = 0
      else:
        self._frames_since_match[i] = min(self._frames_since_match[i] + 1, CONFIRM_HOLD_FRAMES + 1)
      confirmed.append(self._frames_since_match[i] <= CONFIRM_HOLD_FRAMES)
    return confirmed

  def _update_trust(self, v_ego: float, das_lead: tuple[float, float] | None, candidate: tuple[float, float, float]) -> None:
    if v_ego > MIN_EVIDENCE_SPEED and candidate[2] > EVIDENCE_PROB and das_lead is not None:
      self._evidence.append(matches(das_lead, candidate))
    if not self._evidence:
      return

    agreement = sum(self._evidence) / len(self._evidence)
    if not self.trusted and len(self._evidence) >= MIN_EVIDENCE and agreement >= TRUST_ON:
      self.trusted = True
    elif self.trusted and agreement < TRUST_OFF:
      self.trusted = False
    else:
      return
    cloudlog.info(f"DasLeadConfirmer: trusted={self.trusted} evidence={len(self._evidence)} agreement={agreement:.2f}")
