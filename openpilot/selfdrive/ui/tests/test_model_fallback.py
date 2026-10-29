import os
import unittest
from unittest.mock import patch

from openpilot.cereal import log
from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.models.helpers import ACTIVE_BUNDLE_KEYS, REQUIRED_JSON_VERSION


class ModelMessages(dict):
  alive = valid = {'modelV2': True}


@unittest.skipIf(not os.environ.get('DISPLAY'), 'needs a display; run under xvfb-run')
class TestModelFallbackDisplay(OpenpilotTestCase):
  def setup_method(self):
    from openpilot.selfdrive.ui.ui_state import ui_state, ChestnutState
    from openpilot.selfdrive.ui.sunnypilot import model_info
    from openpilot.selfdrive.ui.sunnypilot.mici.layouts import models as mici_models
    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.models import ModelsLayout

    self.ui_state = ui_state
    self.failed = ChestnutState.FAILED
    self.model_info = model_info
    self.mici_models = mici_models
    self.models_layout = ModelsLayout.__new__(ModelsLayout)

  def test_custom_small_is_named_after_runtime_fallback(self):
    params = Params()
    for source in ('qcom', 'chestnut'):
      params.put(ACTIVE_BUNDLE_KEYS[source], {
        'internalName': source, 'displayName': source.upper(), 'runner': 'tinygrad',
        'minimumSelectorVersion': REQUIRED_JSON_VERSION,
      }, block=True)
    with patch.multiple(self.ui_state, params=params, started=True, chestnut_present=True,
                        chestnut_active=False, chestnut_loading=False, active_runner_tinygrad=True,
                        chestnut_state=self.failed, sm=ModelMessages(modelV2=log.ModelDataV2(big=False))):
      self.assertEqual(self.model_info.carrying_model(), ('qcom', 'qcom', 'QCOM'))
      self.assertEqual(self.mici_models._model_info()[0], 'qcom')
      self.assertIn('QCOM', self.models_layout._status_note())

  def test_missing_custom_fallback_does_not_claim_the_failed_big_is_driving(self):
    params = Params()
    params.put(ACTIVE_BUNDLE_KEYS['chestnut'], {
      'internalName': 'big', 'displayName': 'BIG', 'runner': 'tinygrad',
      'minimumSelectorVersion': REQUIRED_JSON_VERSION,
    }, block=True)
    with patch.multiple(self.ui_state, params=params, started=True, chestnut_present=True,
                        chestnut_active=False, chestnut_loading=False, active_runner_tinygrad=True,
                        chestnut_state=self.failed, sm=ModelMessages(modelV2=log.ModelDataV2(big=False))):
      self.assertEqual(self.model_info.carrying_model(), (None, None, None))
      self.assertEqual(self.mici_models._model_info()[0], 'unavailable')

  def test_stock_big_fallback_keeps_the_default_small(self):
    params = Params()
    params.put(ACTIVE_BUNDLE_KEYS['qcom'], {
      'internalName': 'custom', 'displayName': 'CUSTOM', 'runner': 'tinygrad',
      'minimumSelectorVersion': REQUIRED_JSON_VERSION,
    }, block=True)
    with patch.multiple(self.ui_state, params=params, started=True, chestnut_present=True,
                        chestnut_active=False, chestnut_loading=False, active_runner_tinygrad=False,
                        chestnut_state=self.failed):
      name = self.model_info.default_model_name('qcom')
      self.assertEqual(self.model_info.carrying_model(), ('qcom', name, name))
