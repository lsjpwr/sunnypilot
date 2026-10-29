"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import pickle
import requests
import gc
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from openpilot.cereal import messaging
from openpilot.common.hardware import hw
from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
import openpilot.sunnypilot.modeld_v2.modeld as modeld_module
import openpilot.sunnypilot.models.helpers as model_helpers
from openpilot.sunnypilot.modeld_v2.tests import helpers as tests_helpers
from openpilot.sunnypilot.modeld_v2.tests.helpers import DummyModel, DummyBundle, CAM_W, CAM_H
from openpilot.sunnypilot.modeld_v2.tests.test_blendful import frame
from openpilot.sunnypilot.models.fetcher import ModelParser, ModelFetcher

tmp_path = tests_helpers.tmp_path


class EndFrames(BaseException):
  pass


def model_output():
  output = frame(center=0.5)
  output['plan'] = np.zeros((1, 33, 15))
  output['plan'][0, :, 0] = np.array(modeld_module.ModelConstants.X_IDXS)
  output['plan'][0, :, 3] = 20.
  output.update({
    'plan_stds': np.ones((1, 33, 15)), 'action': np.array([[0., 1.]]),
    'road_edges': np.zeros((1, 2, 33, 2)), 'road_edges_stds': np.ones((1, 2, 33, 2)),
    'lead': np.zeros((1, 3, 6, 4)), 'lead_stds': np.ones((1, 3, 6, 4)), 'lead_prob': np.zeros((1, 3)),
    'desire_state': np.zeros((1, 8)), 'desire_pred': np.zeros((1, 4, 8)), 'meta': np.zeros((1, 55)),
    'pose': np.zeros((1, 6)), 'pose_stds': np.ones((1, 6)),
    'wide_from_device_euler': np.zeros((1, 3)), 'wide_from_device_euler_stds': np.ones((1, 3)),
    'road_transform': np.zeros((1, 6)), 'road_transform_stds': np.ones((1, 6)),
  })
  return output


def run_model_process(tmp_path, monkeypatch, *, small=False, fail_big=False):
  params = Params()
  for source, smooth in [('chestnut', '1.0')] + ([('qcom', '0.5')] if small else []):
    name = source + '.pkl'
    data = pickle.loads(pickle.dumps(tests_helpers.make_pkl_data(tests_helpers.ARCHETYPES['supercombo_non20hz'])))
    data['metadata']['input_shapes']['action_t'] = (1, 2)
    (tmp_path / name).write_bytes(pickle.dumps(data))
    params.put(model_helpers.ACTIVE_BUNDLE_KEYS[source], {
      'internalName': source, 'displayName': source.upper(), 'runner': 'tinygrad', 'generation': 10,
      'minimumSelectorVersion': model_helpers.REQUIRED_JSON_VERSION, 'ref': source,
      'models': [{'type': 'supercombo', 'artifact': {'fileName': name}}],
      'overrides': [{'key': 'long', 'value': smooth}],
    }, block=True)
  cp = modeld_module.car.CarParams(brand='tesla', longitudinalActuatorDelay=0.2)
  params.put('CarParams', cp.to_bytes(), block=True)
  params.put_bool('Blendful', True, block=True)
  monkeypatch.setattr(hw.Paths, 'model_root', staticmethod(lambda: str(tmp_path)))
  monkeypatch.setattr(model_helpers, 'chestnut_present', lambda: True)
  monkeypatch.setattr(modeld_module, 'chestnut_present', lambda: True)
  monkeypatch.setattr(modeld_module, 'wait_for_chestnut', lambda: None)
  monkeypatch.setattr(modeld_module, 'config_realtime_process', lambda *args: None)
  monkeypatch.setattr(modeld_module, 'setproctitle', lambda *args: None)
  monkeypatch.setattr(modeld_module, 'load_model', lambda path, **kwargs: pickle.loads(Path(path).read_bytes()))
  messages, delays = [], []

  class Camera:
    width, height, buffer_len = CAM_W, CAM_H, 1
    frame_id = timestamp_sof = timestamp_eof = 0

    @staticmethod
    def available_streams(*args, **kwargs):
      return [modeld_module.VisionStreamType.VISION_STREAM_NARROW_ROAD]

    def __init__(self, *args):
      pass

    def connect(self, *args):
      return True

    def recv(self):
      self.frame_id += 1
      if self.frame_id > (3 if fail_big else 1):
        raise EndFrames
      self.timestamp_sof = self.timestamp_eof = self.frame_id * 50_000_000
      return SimpleNamespace(data=b'')

  class Inputs:
    def __init__(self, services):
      self.data = {key: getattr(messaging.new_message(key), key) for key in services}
      self.frame = 59
      self.updated = self.seen = dict.fromkeys(services, False)
      self['carState'].vEgo = 20.

    def __getitem__(self, key):
      return self.data[key]

    def update(self, *args):
      self.frame += 1

  class Outputs:
    def __init__(self, *args):
      pass

    def send(self, service, message):
      messages.append((service, message.to_bytes()))

  def infer(state, bufs, transforms, inputs, after_enqueue=None):
    if isinstance(next(iter(bufs.values())), np.ndarray):
      return None  # GPU warmup; actual bundle selection and ModelState initialization remain intact.
    delays.append((state.chestnut, inputs['action_t'][1]))
    if fail_big and state.chestnut and sum(big for big, _ in delays) > 1:
      raise RuntimeError('GPU disconnected')
    return model_output()

  monkeypatch.setattr(modeld_module, 'VisionIpcClient', Camera)
  monkeypatch.setattr(modeld_module, 'SubMaster', Inputs)
  monkeypatch.setattr(modeld_module, 'PubMaster', Outputs)
  monkeypatch.setattr(modeld_module.ModelState, 'run', infer)
  gc_enabled = gc.isenabled()
  try:
    try:
      modeld_module.main()
    except EndFrames:
      pass
  finally:
    if gc_enabled:
      gc.enable()
  return params, [messaging.log_from_bytes(raw) for service, raw in messages if service == 'modelV2'], delays


class TestFallback(OpenpilotTestCase):
  def test_big_model_runs_with_default_small_slot(self, tmp_path, monkeypatch):
    params, models, _ = run_model_process(tmp_path, monkeypatch)
    assert len(models) == 1 and models[0].modelV2.big
    assert params.get_bool('ChestnutActive')
    assert params.get_bool('Blendful')
    assert models[0].modelV2.action.desiredCurvature == 0., 'Persisted Blendful must not alter model steering'

  def test_runtime_fallback_uses_small_model_delay_and_disables_blendful(self, tmp_path, monkeypatch):
    params, models, delays = run_model_process(tmp_path, monkeypatch, small=True, fail_big=True)
    assert [msg.modelV2.big for msg in models] == [True, False]
    assert not params.get_bool('ChestnutActive')
    assert all(msg.modelV2.action.desiredCurvature == 0. for msg in models)
    self.assertAlmostEqual(delays[0][1], 1.275, places=6)
    self.assertAlmostEqual(delays[-1][1], 0.775, places=6)

  def test_big_failure_without_small_preserves_inference_error(self, tmp_path, monkeypatch):
    with self.assertRaisesRegex(RuntimeError, 'GPU disconnected'):
      run_model_process(tmp_path, monkeypatch, fail_big=True)

  def test_find_dual_model_in_bundle(self, tmp_path, monkeypatch):
    lebowski_file = 'driving_lebowski.pkl'
    tsfdo_file = 'driving_tsfdo.pkl'
    (tmp_path / lebowski_file).write_bytes(b'fkasdjfkljf')
    (tmp_path / tsfdo_file).write_bytes(b'dskfajklsdjlsfka')

    monkeypatch.setattr(hw.Paths, 'model_root', staticmethod(lambda: str(tmp_path)))
    big_bundle = DummyBundle(models=[DummyModel('supercombo', lebowski_file)])
    small_bundle = DummyBundle(models=[DummyModel('supercombo', tsfdo_file)])
    big_pkl = modeld_module._find_driving_pkl(big_bundle)
    small_pkl = modeld_module._find_driving_pkl(small_bundle)

    assert big_pkl is not None and lebowski_file in big_pkl
    assert small_pkl is not None and tsfdo_file in small_pkl

  def test_download_models_and_init_modelstate_fallback(self, tmp_path, monkeypatch):
    monkeypatch.setattr(hw.Paths, 'model_root', staticmethod(lambda: str(tmp_path)))
    big_json = requests.get(ModelFetcher.MODEL_URL_CHESTNUT).json()
    big_bundle = ModelParser.parse_models(big_json)[-1]
    small_json = requests.get(ModelFetcher.MODEL_URL).json()
    small_bundle = ModelParser.parse_models(small_json)[-1]

    oob_bytes = pickle.dumps(tests_helpers.make_pkl_data(tests_helpers.ARCHETYPES['supercombo_non20hz']))
    monkeypatch.setattr(modeld_module, 'load_model', lambda path, **_: pickle.loads(open(path, 'rb').read()))

    for bundle in (big_bundle, small_bundle):
      (tmp_path / bundle.models[0].artifact.fileName).write_bytes(oob_bytes)

    monkeypatch.setattr(modeld_module, 'get_active_bundle', lambda params=None, *, chestnut=None: small_bundle)
    assert modeld_module.ModelState(CAM_W, CAM_H, chestnut=False).chestnut is False

    monkeypatch.setattr(modeld_module, 'get_active_bundle', lambda params=None, *, chestnut=None: big_bundle)
    try:
      assert modeld_module.ModelState(CAM_W, CAM_H, chestnut=True).chestnut is True
    except Exception as e:
      assert "AMD" in str(e) or "device" in str(e).lower()
