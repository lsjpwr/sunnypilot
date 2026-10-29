from types import SimpleNamespace

from openpilot.cereal import log
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.selfdrived.alertmanager import AlertManager
from openpilot.selfdrive.selfdrived.events import Events, ET
from openpilot.selfdrive.selfdrived.selfdrived import SelfdriveD
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
from openpilot.sunnypilot.selfdrive.selfdrived.events_base import EmptyAlert


class TestSelfdriveAlerts(OpenpilotTestCase):
  def test_no_entry_alert_cleanup_on_engagement_transitions(self):
    cases = (
      (False, [ET.ENABLE], True),
      (False, [ET.USER_DISABLE], True),
      (False, [ET.ENABLE, ET.NO_ENTRY], False),
      (False, [ET.NO_ENTRY], False),
      (False, [], False),
      (True, [], True),
    )
    for enabled, alert_types, cleared in cases:
      with self.subTest(enabled=enabled, alert_types=alert_types):
        events = Events()
        events.add(log.OnroadEvent.EventName.doorOpen)
        manager = AlertManager()
        manager.add_many(0, events.create_alerts([ET.NO_ENTRY], []))
        events.clear()
        if ET.NO_ENTRY in alert_types:
          events.add(log.OnroadEvent.EventName.doorOpen)

        sd = SimpleNamespace(enabled=enabled, personality=log.LongitudinalPersonality.standard,
                             state_machine=SimpleNamespace(current_alert_types=alert_types, soft_disable_timer=0),
                             CP=None, sm=SimpleNamespace(frame=1), is_metric=True,
                             events=events, events_sp=EventsSP(), AM=manager)
        SelfdriveD.update_alerts(sd, None)

        self.assertEqual(manager.current_alert == EmptyAlert, cleared)
