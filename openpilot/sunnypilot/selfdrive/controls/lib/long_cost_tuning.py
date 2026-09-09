"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL

# Range offered by the UI. 0.0 is the shipped weight and leaves the relative-velocity residual
# inert. 2.0 is about seven times the weight that reproduces stock braking at 30 m/s once the
# equivalence is fully shed -- measured at 0.27 -- so there is room to tune past parity while
# staying far from the weights where the solver stops converging.
LEAD_VELOCITY_COST_MIN = 0.0
LEAD_VELOCITY_COST_MAX = 2.0

# 1.0 keeps the stock gap residual; 0.0 sheds the stopping-distance equivalence from the cost.
LEAD_EQUIV_FACTOR_MIN = 0.0
LEAD_EQUIV_FACTOR_MAX = 1.0


class LongCostTuningController:
  """Feeds the MPC's spacing/velocity split from Params, in the shape StopDistanceController uses."""

  def __init__(self, mpc, params=None):
    self._mpc = mpc
    self._params = params or Params()
    self._frame = 0
    self._read_params()

  def _read_params(self) -> None:
    if self._frame % int(1. / DT_MDL) != 0:
      return

    # Argument order differs between the two on purpose. Python's min/max return the non-NaN
    # operand, so max(MIN, min(MAX, nan)) lands on MAX and min(MAX, max(MIN, nan)) lands on MIN.
    # Neutral is the floor for the weight and the ceiling for the factor, so a NaN param has to
    # fall through in opposite directions to leave the solver where it shipped.
    cost = float(self._params.get("LeadVelocityCost", return_default=True))
    clipped_cost = min(LEAD_VELOCITY_COST_MAX, max(LEAD_VELOCITY_COST_MIN, cost))
    if clipped_cost != cost:
      self._params.put("LeadVelocityCost", clipped_cost, block=True)

    factor = float(self._params.get("LeadEquivFactor", return_default=True))
    clipped_factor = max(LEAD_EQUIV_FACTOR_MIN, min(LEAD_EQUIV_FACTOR_MAX, factor))
    if clipped_factor != factor:
      self._params.put("LeadEquivFactor", clipped_factor, block=True)

    self._mpc.lead_velocity_cost = clipped_cost
    self._mpc.lead_equiv_factor = clipped_factor

  def update(self, sm) -> None:
    self._read_params()
    self._frame += 1
