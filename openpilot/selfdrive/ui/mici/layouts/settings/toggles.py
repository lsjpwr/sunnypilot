from openpilot.cereal import log

from openpilot.system.ui.widgets.scroller import NavScroller
from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigToggle, BigParamControl, BigMultiParamToggle
from openpilot.selfdrive.ui.mici.widgets.dialog import BigInputDialog
from openpilot.system.ui.lib.application import gui_app
from openpilot.selfdrive.ui.layouts.settings.common import restart_needed_callback
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.mici.layouts.settings.initial_cruise_speed import InitialCruiseSpeedButton
from openpilot.sunnypilot.mapd import MapSource

PERSONALITY_TO_INT = log.LongitudinalPersonality.schema.enumerants


class TogglesLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()

    self._personality_toggle = BigMultiParamToggle("following distance", "LongitudinalPersonality", ["aggressive", "standard", "relaxed"],
                                                   description="Standard is recommended.\n" +
                                                               "Aggressive follows closer, with firmer gas and braking.\n" +
                                                               "Relaxed leaves more space.\n" +
                                                               "Use the steering wheel distance button on supported cars.")
    self._initial_cruise_speed = InitialCruiseSpeedButton()
    self._initial_cruise_speed.set_visible(lambda: ui_state.CP is not None and ui_state.has_longitudinal_control and not ui_state.CP.pcmCruise)
    is_metric_toggle = BigToggle("use metric units", initial_state=ui_state.params.get_bool("IsMetric"), toggle_callback=self._on_metric_toggle)
    ldw_toggle = BigParamControl("lane departure warnings", "IsLdwEnabled",
                                 description="Warn when you drift across a detected lane line.\n" +
                                             "Only above 31 mph (50 km/h), with no turn signal.")
    always_on_dm_toggle = BigParamControl("always-on driver monitor", "AlwaysOnDM", description="Monitor the driver even when sunnypilot is not engaged.")
    self._dm_mode_toggle = BigMultiParamToggle("driver monitoring", "DriverMonitoringMode", ["standard", "relaxed", "off"])
    record_front = BigParamControl("record & upload cabin camera", "RecordFront",
                                   description_icon=gui_app.texture("icons_mici/settings/device/cameras.png", 64, 64),
                                   toggle_callback=restart_needed_callback, description="Upload cabin camera data to help improve driver monitoring.")
    record_mic = BigParamControl("record & upload mic audio", "RecordAudio", description_icon=gui_app.texture("icons_mici/microphone.png", 64, 64),
                                 toggle_callback=restart_needed_callback,
                                 description="Record microphone audio while driving.\n" +
                                             "Audio is included in dashcam videos in sunnylink.")
    enable_openpilot = BigParamControl("enable sunnypilot", "OpenpilotEnabledToggle", toggle_callback=restart_needed_callback,
                                       description="Enable to use sunnypilot driver assistance.\n" +
                                                   "Disable to use your car's stock driver assistance.")


    korea_nav_toggle = BigParamControl("korean external navigation input", "KoreaExternalNavEnabled")
    korea_bump_toggle = BigParamControl("korean speed bump slowdown", "KoreaSpeedBumpEnabled")
    korea_download_toggle = BigParamControl("auto-download map data", "KoreaMapAutoDownload")

    korea_camera_toggles = (
      ("KoreaCameraSpeedEnabled", BigParamControl("speed cameras", "KoreaCameraSpeedEnabled")),
      ("KoreaCameraSignalEnabled", BigParamControl("signal + speed cameras", "KoreaCameraSignalEnabled")),
      ("KoreaCameraSectionEnabled", BigParamControl("section enforcement cameras", "KoreaCameraSectionEnabled")),
      ("KoreaCameraZoneEnabled", BigParamControl("protected zone cameras", "KoreaCameraZoneEnabled")),
    )

    def api_key_callback(text):
      # The dialog always seeds empty now (see edit_api_key below), so a bare tap on
      # confirm submits "". If a key is already stored, treat that as a no-op instead
      # of wiping it -- otherwise "open to check whether a key is set, then hit the
      # obvious confirm button" silently destroys a live credential with no undo.
      # Trade-off: the key can no longer be cleared from mici, only replaced. The big
      # UI can still clear it (its dialog pre-fills under password_mode, so the user
      # can delete the characters and confirm) -- losing a rarely-used clear button on
      # the smaller surface is preferable to silent data loss on a credential.
      if not text and ui_state.params.get("KoreaMapApiKey"):
        return
      ui_state.params.put("KoreaMapApiKey", text)
      self._api_key_btn.set_value("Set" if text else "Not set")

    def edit_api_key():
      # Seed empty, not the stored key -- this dialog has no password/masking mode,
      # so pre-filling it would put the live credential on screen in cleartext.
      gui_app.push_widget(BigInputDialog("enter data.go.kr service key...",
                                         "",
                                         minimum_length=0,
                                         confirm_callback=api_key_callback))

    has_key = bool(ui_state.params.get("KoreaMapApiKey"))
    self._api_key_btn = BigButton("speed camera API key", "Set" if has_key else "Not set")
    self._api_key_btn.set_click_callback(edit_api_key)

    self._scroller.add_widgets([
      self._personality_toggle,
      self._initial_cruise_speed,
      is_metric_toggle,
      ldw_toggle,
      always_on_dm_toggle,
      self._dm_mode_toggle,
      record_front,
      record_mic,
      enable_openpilot,
      korea_nav_toggle,
      *(toggle for _, toggle in korea_camera_toggles),
      korea_bump_toggle,
      korea_download_toggle,
      self._api_key_btn,
    ])

    # Toggle lists
    self._refresh_toggles = (
      ("IsMetric", is_metric_toggle),
      ("IsLdwEnabled", ldw_toggle),
      ("AlwaysOnDM", always_on_dm_toggle),
      ("RecordFront", record_front),
      ("RecordAudio", record_mic),
      ("OpenpilotEnabledToggle", enable_openpilot),
      ("KoreaExternalNavEnabled", korea_nav_toggle),
      *korea_camera_toggles,
      ("KoreaSpeedBumpEnabled", korea_bump_toggle),
      ("KoreaMapAutoDownload", korea_download_toggle),
    )

    enable_openpilot.set_enabled(lambda: not ui_state.engaged)
    record_front.set_enabled(False if ui_state.params.get_bool("RecordFrontLock") else (lambda: not ui_state.engaged))
    record_mic.set_enabled(lambda: not ui_state.engaged)
    # Remote schema restricts KoreaExternalNavEnabled to offroad (opening a control-plane
    # UDP port mid-drive from a phone is not allowed) -- match that here for parity.
    korea_nav_toggle.set_enabled(lambda: ui_state.is_offroad() and ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea)
    # Not offroad-gated and not gated on longitudinal control: a kind that is off also leaves
    # the speed limit ahead sign, which a stock-ACC car shows too.
    for _, toggle in korea_camera_toggles:
      toggle.set_enabled(lambda: ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea)
    # Not offroad-gated, unlike the nav toggle: this opens no port and touches no
    # credential, it only stops a comfort slowdown. Also requires longitudinal control or
    # ICBM -- SmartCruiseControlMap (which this toggle ultimately drives) cannot act without
    # one of them, so without this gate the toggle would do nothing on a stock-ACC car.
    korea_bump_toggle.set_enabled(
      lambda: ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea and
      ui_state.CP is not None and (ui_state.has_longitudinal_control or ui_state.has_icbm))
    # Offroad-gated for bandwidth, not safety: this downloads a ~220 MB map database, and that
    # transfer must not compete with the car's connection while driving.
    korea_download_toggle.set_enabled(
      lambda: ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea
              and ui_state.is_offroad())
    self._api_key_btn.set_enabled(lambda: ui_state.params.get("MapDataSource", return_default=True) == MapSource.korea)


    if ui_state.params.get_bool("ShowDebugInfo"):
      gui_app.set_show_touches(True)
      gui_app.set_show_fps(True)

    ui_state.add_engaged_transition_callback(self._update_toggles)

  def _update_state(self):
    super()._update_state()

    if ui_state.sm.updated["selfdriveState"]:
      personality = PERSONALITY_TO_INT[ui_state.sm["selfdriveState"].personality]
      if personality != ui_state.personality and ui_state.started:
        self._personality_toggle.set_value(self._personality_toggle._options[personality])
      ui_state.personality = personality

  def show_event(self):
    super().show_event()
    self._update_toggles()

  def _on_metric_toggle(self, state: bool):
    ui_state.params.put_bool("IsMetric", state, block=True)
    self._update_toggles()

  def _update_toggles(self):
    ui_state.update_params()
    self._initial_cruise_speed.refresh()

    if ui_state.CP is not None:
      self._personality_toggle.set_visible(ui_state.has_longitudinal_control)

    # Refresh toggles from params to mirror external changes
    for key, item in self._refresh_toggles:
      item.set_checked(ui_state.params.get_bool(key))
