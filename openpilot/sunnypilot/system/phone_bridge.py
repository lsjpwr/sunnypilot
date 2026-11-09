#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

phone_bridge: the HTTP side of the phone app (sp-knavlink-tesla). The app reads what the mici HUD
shows; only a phone that was paired with the code on the comma's screen can.

Runs while PhoneBridgeEnabled is on, driving or parked. It reads messages and params and publishes
nothing, so the control path never depends on it, and selfdrived ignores it being dead.

  POST /v1/pair {"code": "123456"}  ->  {"token": "..."}; a bad code is 403, a bad request 400
  GET  /v1/hud                      ->  see PhoneBridge.hud

Everything but /v1/pair needs Authorization: Bearer <token>, or it is 401. Requests from outside the
Wi-Fi subnet are dropped unanswered. The token goes in the clear, inside the hotspot's encryption.
"""
import fcntl
import hmac
import ipaddress
import json
import platform
import secrets
import socket
import struct
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from opendbc.car.structs import car
from openpilot.cereal import log, messaging
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.selfdrive.controls.radard import RADAR_TO_CAMERA
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import comma_target_kph, section_average_display

PORT = 5556
WIFI_INTERFACE = "wlan0"
PAIR_CODE_PARAM = "PhoneBridgePairCode"
TOKEN_PARAM = "PhoneBridgeToken"
PAIR_CODE_S = 60.
MAX_CODE_FAILURES = 5
MAX_BODY = 4096  # bytes
REQUEST_TIMEOUT_S = 2.  # per socket operation, since one slow client holds up the rest
LOOP_S = 0.05  # how long a request is waited for between looks at the messages

SET_SPEED_NA = 255  # the mici HUD's "no set speed"
DESIRE_NAMES = sorted(log.Desire.schema.enumerants, key=log.Desire.schema.enumerants.get)
LANE_CHANGE_UNDER_WAY = ("laneChangeStarting", "laneChangeFinishing")  # preLaneChange still waits for the driver
LANE_CHANGE_DESIRE = {"left": "laneChangeLeft", "right": "laneChangeRight"}

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891b

SERVICES = ["carState", "controlsState", "deviceState", "longitudinalPlan", "longitudinalPlanSP", "modelV2", "radarState", "selfdriveState",
            "selfdriveStateSP"]


def new_pair_code(params, now=time.monotonic) -> str:
  """Puts up a fresh code for the bridge to accept, and returns it for the screen. CLOCK_MONOTONIC
  is the same in both processes and the param does not outlive a boot."""
  code = f"{secrets.randbelow(1_000_000):06d}"
  params.put(PAIR_CODE_PARAM, {"code": code, "expires": now() + PAIR_CODE_S}, block=True)
  return code


def wifi_subnet(interface: str = WIFI_INTERFACE) -> ipaddress.IPv4Network | None:
  """The interface's IPv4 network, or None while it has no address."""
  request = struct.pack("256s", interface.encode())
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    try:
      address, netmask = (socket.inet_ntoa(fcntl.ioctl(sock, op, request)[20:24]) for op in (SIOCGIFADDR, SIOCGIFNETMASK))
    except OSError:
      return None
  return ipaddress.IPv4Interface(f"{address}/{netmask}").network


def in_subnet(client: str, network: ipaddress.IPv4Network | None) -> bool:
  return network is not None and ipaddress.ip_address(client) in network


class PhoneBridge:
  def __init__(self, params, mem_params, sm, now=time.monotonic):
    self.params, self.mem_params, self.sm, self.now = params, mem_params, sm, now
    self._failures = (None, 0)  # the code the wrong tries were against, and how many
    self._v_ego_cluster_seen = False

  def pair(self, code: str) -> str | None:
    """The new token for the code on screen, ending the old one. None for anything else."""
    entry = self.params.get(PAIR_CODE_PARAM)
    if not entry or self.now() >= entry["expires"]:
      return None
    if not hmac.compare_digest(code.encode(), entry["code"].encode()):
      failures = self._failures[1] + 1 if self._failures[0] == entry["code"] else 1
      self._failures = (entry["code"], failures)
      if failures >= MAX_CODE_FAILURES:
        self.params.remove(PAIR_CODE_PARAM)
      return None
    self.params.remove(PAIR_CODE_PARAM)
    token = secrets.token_urlsafe(32)
    self.params.put(TOKEN_PARAM, token, block=True)
    return token

  def authorized(self, header: str | None) -> bool:
    token = self.params.get(TOKEN_PARAM)
    if not token or not header or not header.startswith("Bearer "):
      return False
    return hmac.compare_digest(header.removeprefix("Bearer ").encode(), token.encode())

  def hud(self) -> dict:
    """What the mici HUD shows, in km/h whatever the comma displays. A field whose message has gone stale is null."""
    alive = self.sm.alive
    started = self.sm["deviceState"].started if alive["deviceState"] else None
    auto_sla = alive["longitudinalPlanSP"] and self.params.get_bool("TeslaAutoSpeedLimitAssist") and self._is_tesla()
    max_kph, max_green = self._max_speed(auto_sla)
    return {"v": 1, "started": started, "engaged": self._engaged(started), "max_kph": max_kph, "max_green": max_green,
            "speed_kph": self._speed_kph(), "section": self._section(auto_sla), "lead": self._lead(), "desire": self._desire(),
            "blind": self._blind(), "alert": self._alert()}

  def _is_tesla(self) -> bool:
    car_params = self.params.get("CarParams")
    return car_params is not None and messaging.log_from_bytes(car_params, car.CarParams).brand == "tesla"

  def _engaged(self, started: bool | None) -> bool | None:
    if started is None or not (self.sm.alive["selfdriveState"] and self.sm.alive["selfdriveStateSP"]):
      return None
    return started and (self.sm["selfdriveState"].enabled or self.sm["selfdriveStateSP"].mads.enabled)

  def _max_speed(self, auto_sla: bool) -> tuple[int | None, bool | None]:
    """The number in the mici MAX box (HudRendererSP) and whether it is the green one."""
    cluster = self.sm["carState"].vCruiseCluster
    if not self.sm.alive["carState"] or (cluster == 0. and not self.sm.alive["controlsState"]):
      return None, None
    set_speed = self.sm["controlsState"].deprecated.vCruise if cluster == 0. else cluster
    if not 0 < set_speed < SET_SPEED_NA:
      return None, False
    sla_kph = None
    if auto_sla:
      plan = self.sm["longitudinalPlanSP"]
      assist, scc_map = plan.speedLimit.assist, plan.smartCruiseControl.map
      sla_kph = comma_target_kph(assist.enabled, assist.vTarget, scc_map.active, scc_map.vTarget, cluster)
    return round(set_speed if sla_kph is None else sla_kph), sla_kph is not None

  def _speed_kph(self) -> int | None:
    if not self.sm.alive["carState"]:
      return None
    car_state = self.sm["carState"]
    self._v_ego_cluster_seen = self._v_ego_cluster_seen or car_state.vEgoCluster != 0.
    return round(max(0., (car_state.vEgoCluster if self._v_ego_cluster_seen else car_state.vEgo) * CV.MS_TO_KPH))

  def _section(self, auto_sla: bool) -> dict | None:
    if not auto_sla:
      return None
    section = section_average_display(float(self.mem_params.get("KoreaSectionAverage") or 0.),
                                      float(self.mem_params.get("KoreaSectionSpeedLimit") or 0.), True)
    return None if section is None else {"avg": section[0], "over": section[1]}

  def _lead(self) -> dict | None:
    """The first of the leads mici draws (ModelRenderer._update_leads)."""
    alive = self.sm.alive
    if not alive["longitudinalPlan"]:
      return None
    e2e = self.sm["longitudinalPlan"].longitudinalPlanSource == log.LongitudinalPlan.LongitudinalPlanSource.e2e
    if e2e and not alive["modelV2"]:
      return None
    if e2e and len(self.sm["modelV2"].leadsV3) > 1:
      lead = self.sm["modelV2"].leadsV3[0]
      seen, dist_m = lead.prob > 0.5, lead.x[0]
    elif alive["radarState"]:
      lead = self.sm["radarState"].leadOne
      seen, dist_m = lead.present, lead.dRel + RADAR_TO_CAMERA
    else:
      return None
    return {"seen": seen, "dist_m": round(dist_m) if seen else None}

  def _desire(self) -> str | None:
    if not self.sm.alive["modelV2"]:
      return None
    meta = self.sm["modelV2"].meta
    if str(meta.laneChangeState) in LANE_CHANGE_UNDER_WAY and str(meta.laneChangeDirection) in LANE_CHANGE_DESIRE:
      return LANE_CHANGE_DESIRE[str(meta.laneChangeDirection)]
    # the model's vector can be longer than the enum
    name, probability = max(zip(DESIRE_NAMES[1:], list(meta.desireState)[1:], strict=False), key=lambda d: d[1], default=(None, 0.))
    return name if probability > 0.5 else None

  def _blind(self) -> dict | None:
    if not self.sm.alive["carState"]:
      return None
    return {"l": self.sm["carState"].leftBlindspot, "r": self.sm["carState"].rightBlindspot}

  def _alert(self) -> dict | None:
    if not self.sm.alive["selfdriveState"]:
      return None
    state = self.sm["selfdriveState"]
    if not state.alertText1 and not state.alertText2:
      return None
    return {"text1": state.alertText1, "text2": state.alertText2, "status": str(state.alertStatus)}


class _Server(HTTPServer):
  def __init__(self, address, handler, allowed):
    super().__init__(address, handler)
    self.allowed = allowed

  def verify_request(self, request, client_address) -> bool:
    return self.allowed(client_address[0])


def make_server(bridge: PhoneBridge, allowed, host: str = "0.0.0.0", port: int = PORT) -> HTTPServer:
  # ponytail: one thread and one request per connection, so no lock around pairing or the SubMaster. Someone on the
  # hotspot who holds a connection open delays the HUD by up to REQUEST_TIMEOUT_S per read; thread it if that shows up.
  class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, format, *args):  # noqa: A002
      pass  # four lines a second

    def _reply(self, status: int, body: dict | None = None) -> None:
      data = b"" if body is None else json.dumps(body).encode()
      self.send_response(status)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(data)))
      self.end_headers()
      self.wfile.write(data)

    def _json_body(self) -> dict | None:
      try:
        length = int(self.headers.get("Content-Length", ""))
        if not 0 < length <= MAX_BODY:
          return None
        body = json.loads(self.rfile.read(length))
      except ValueError:
        return None
      return body if isinstance(body, dict) else None

    def _authorized(self) -> bool:
      if bridge.authorized(self.headers.get("Authorization")):
        return True
      self._reply(401)
      return False

    def do_GET(self):
      if self._authorized():
        self._reply(*((200, bridge.hud()) if self.path == "/v1/hud" else (404,)))

    def do_POST(self):
      if self.path == "/v1/pair":
        code = (self._json_body() or {}).get("code")
        if not isinstance(code, str):
          self._reply(400)
          return
        token = bridge.pair(code)
        self._reply(*((200, {"token": token}) if token else (403,)))
      elif self._authorized():
        self._reply(404)

  return _Server((host, port), Handler, allowed)


def main() -> None:
  params = Params()
  sm = messaging.SubMaster(SERVICES)
  server = make_server(PhoneBridge(params, Params("/dev/shm/params") if platform.system() != "Darwin" else params, sm),
                       lambda client: in_subnet(client, wifi_subnet()))
  server.timeout = LOOP_S
  while True:
    sm.update(0)  # keeps SubMaster.alive true to the moment, phone or no phone
    server.handle_request()


if __name__ == "__main__":
  main()
