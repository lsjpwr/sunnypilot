#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tmapd: receives TMAP navigation alerts (speed cameras, speed bumps) relayed
from a phone by Tasker over HTTP, tracks the remaining distance to each alert
using the device's own speed, and publishes externalNavDataSP.

Tasker protocol (HTTP POST, JSON body):
  POST http://<device-ip>:5505/v1/alert
  {"type": "cam_fixed", "speed_limit": 50, "distance": 600}

  type:        cam_fixed | cam_mobile | cam_section_start | cam_section_end | speed_bump
  speed_limit: km/h, optional (omit for speed_bump)
  distance:    m, distance to the alert point as reported by TMAP

  GET http://<device-ip>:5505/v1/ping -> {"status": "ok"} (for Tasker setup tests)
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cereal.messaging as messaging
from cereal import custom
from openpilot.common.constants import CV
from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.navd import tmap_route

HOST = "0.0.0.0"
PORT = 5505
PUBLISH_HZ = 2.
ALERT_TTL = 120.  # s, drop alerts not refreshed within this time
PASSED_MARGIN = 50.  # m, drop alerts this far behind us
MAX_BODY_SIZE = 4096
MAX_DISTANCE = 10000.  # m
MAX_SPEED_LIMIT_KPH = 200.

MANEUVER_HORIZON = 2000.  # m, publish maneuvers within this distance
OFF_ROUTE_DISTANCE = 50.  # m
OFF_ROUTE_TIME = 5.       # s, sustained deviation before re-routing
REROUTE_COOLDOWN = 60.    # s, free-tier guard: at most one route call per minute
GPS_MAX_AGE = 5.          # s

AlertType = custom.ExternalNavDataSP.NavAlert.AlertType

ALERT_TYPES = {
  "cam_fixed": AlertType.camFixed,
  "cam_mobile": AlertType.camMobile,
  "cam_section_start": AlertType.camSectionStart,
  "cam_section_end": AlertType.camSectionEnd,
  "speed_bump": AlertType.speedBump,
}
# generated internally from the route plan, not accepted over HTTP
MANEUVER_TYPES = {
  "turn_sharp": AlertType.turnSharp,
  "ramp": AlertType.ramp,
}


class AlertStore:
  """Thread-safe store of active alerts, keyed by type (one active alert per type)."""

  def __init__(self):
    self._lock = threading.Lock()
    self._alerts: dict[str, dict] = {}

  def add(self, alert_type: str, speed_limit_kph: float, distance: float) -> None:
    with self._lock:
      self._alerts[alert_type] = {
        "type": alert_type,
        "speed_limit": speed_limit_kph * CV.KPH_TO_MS,
        "remaining": distance,
        "received_mono": time.monotonic(),
      }

  def step(self, v_ego: float, dt: float) -> None:
    now = time.monotonic()
    with self._lock:
      for key in list(self._alerts):
        alert = self._alerts[key]
        alert["remaining"] -= v_ego * dt
        if (now - alert["received_mono"]) > ALERT_TTL or alert["remaining"] < -PASSED_MARGIN:
          del self._alerts[key]

  def snapshot(self) -> list[dict]:
    now = time.monotonic()
    with self._lock:
      return [{**a, "age": now - a["received_mono"]} for a in self._alerts.values()]


class RouteManager:
  """Owns the active route: async fetching, off-route detection, maneuver alerts."""

  def __init__(self, params: Params):
    self.params = params
    self._lock = threading.Lock()
    self.route: tmap_route.RoutePlan | None = None
    self.destination: tuple[float, float, str] | None = None
    self.fetch_in_progress = False
    self.last_fetch_mono = 0.
    self.off_route_since: float | None = None
    self.along = 0.

  def set_destination(self, lat: float, lon: float, name: str) -> None:
    with self._lock:
      self.destination = (lat, lon, name)
      self.route = None
      self.off_route_since = None

  def clear(self) -> None:
    with self._lock:
      self.destination = None
      self.route = None
      self.off_route_since = None

  def _fetch(self, start_lat: float, start_lon: float) -> None:
    dest = self.destination
    api_key = self.params.get("TmapApiKey")
    if dest is None or not api_key:
      self.fetch_in_progress = False
      return
    try:
      geojson = tmap_route.fetch_route(api_key, start_lat, start_lon, dest[0], dest[1], dest[2])
      plan = tmap_route.RoutePlan(geojson)
      with self._lock:
        if self.destination == dest:  # destination unchanged while fetching
          self.route = plan
          self.off_route_since = None
      cloudlog.info(f"tmapd route fetched: {plan.total_distance:.0f}m, {len(plan.maneuvers)} maneuvers")
    except Exception as e:
      cloudlog.error(f"tmapd route fetch failed: {e}")
    finally:
      self.fetch_in_progress = False

  def step(self, lat: float, lon: float, gps_valid: bool) -> None:
    """Called from the main loop with the latest GPS fix."""
    if self.destination is None or not gps_valid:
      return

    now = time.monotonic()
    need_fetch = self.route is None
    if self.route is not None:
      self.along, offset = self.route.locate(lat, lon)
      if offset > OFF_ROUTE_DISTANCE:
        if self.off_route_since is None:
          self.off_route_since = now
        elif now - self.off_route_since > OFF_ROUTE_TIME:
          need_fetch = True
      else:
        self.off_route_since = None

    if need_fetch and not self.fetch_in_progress and (now - self.last_fetch_mono) > REROUTE_COOLDOWN:
      self.fetch_in_progress = True
      self.last_fetch_mono = now
      threading.Thread(target=self._fetch, args=(lat, lon), daemon=True).start()

  def maneuver_alerts(self) -> list[dict]:
    with self._lock:
      if self.route is None:
        return []
      kinds = {"turn": "turn_sharp", "ramp": "ramp"}
      return [{"type": kinds[kind], "speed_limit": 0., "remaining": dist, "age": 0.}
              for dist, kind in self.route.upcoming_maneuvers(self.along, MANEUVER_HORIZON)]

  def status(self) -> tuple[bool, float]:
    with self._lock:
      if self.route is None:
        return False, 0.
      return True, self.route.remaining_distance(self.along)


class TmapRequestHandler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  store: AlertStore  # set on the handler class before serving
  route_manager: "RouteManager"

  def log_message(self, format, *args):  # noqa: A002, silence default stderr logging
    pass

  def _respond(self, code: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    self.send_response(code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def do_GET(self):
    if self.path == "/v1/ping":
      self._respond(200, {"status": "ok"})
    else:
      self._respond(404, {"error": "not found"})

  def do_POST(self):
    if self.path == "/v1/route":
      self._handle_route()
      return
    if self.path == "/v1/route/clear":
      self.route_manager.clear()
      self._respond(200, {"status": "ok"})
      return
    if self.path == "/v1/apikey":
      self._handle_apikey()
      return
    if self.path != "/v1/alert":
      self._respond(404, {"error": "not found"})
      return

    try:
      length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
      data = json.loads(self.rfile.read(length))
      alert_type = data["type"]
      if alert_type not in ALERT_TYPES:
        raise ValueError(f"unknown alert type: {alert_type}")
      distance = float(data["distance"])
      speed_limit_kph = float(data.get("speed_limit", 0.))
      if not (0. <= distance <= MAX_DISTANCE) or not (0. <= speed_limit_kph <= MAX_SPEED_LIMIT_KPH):
        raise ValueError("value out of range")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
      self._respond(400, {"error": str(e)})
      return

    self.store.add(alert_type, speed_limit_kph, distance)
    self._respond(200, {"status": "ok"})

  def _handle_route(self):
    try:
      length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
      data = json.loads(self.rfile.read(length))
      lat, lon = float(data["lat"]), float(data["lon"])
      name = str(data.get("name", "destination"))[:64]
      if not (33. <= lat <= 39.5) or not (124. <= lon <= 132.):  # South Korea bounds
        raise ValueError("coordinates out of range")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
      self._respond(400, {"error": str(e)})
      return
    self.route_manager.set_destination(lat, lon, name)
    self._respond(200, {"status": "ok"})

  def _handle_apikey(self):
    try:
      length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
      key = str(json.loads(self.rfile.read(length))["key"]).strip()
      if not (8 <= len(key) <= 128) or not all(c.isalnum() or c in "-_" for c in key):
        raise ValueError("invalid key format")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
      self._respond(400, {"error": str(e)})
      return
    self.route_manager.params.put("TmapApiKey", key)
    self._respond(200, {"status": "ok", "key_set": True})


def main():
  params = Params()
  store = AlertStore()
  route_manager = RouteManager(params)
  TmapRequestHandler.store = store
  TmapRequestHandler.route_manager = route_manager
  server = ThreadingHTTPServer((HOST, PORT), TmapRequestHandler)
  server_thread = threading.Thread(target=server.serve_forever, daemon=True)
  server_thread.start()
  cloudlog.info(f"tmapd listening on {HOST}:{PORT}")

  gps_service = get_gps_location_service(params)
  pm = messaging.PubMaster(['externalNavDataSP'])
  sm = messaging.SubMaster(['carState', gps_service])
  rk = Ratekeeper(PUBLISH_HZ, print_delay_threshold=None)

  maneuver_types = {**ALERT_TYPES, **MANEUVER_TYPES}

  while True:
    sm.update(0)
    v_ego = sm['carState'].vEgo if sm.alive['carState'] else 0.
    store.step(v_ego, 1. / PUBLISH_HZ)

    gps = sm[gps_service]
    gps_valid = sm.alive[gps_service] and \
        (time.monotonic() - gps.unixTimestampMillis * 1e-3) < GPS_MAX_AGE
    route_manager.step(gps.latitude, gps.longitude, gps_valid)

    alerts = store.snapshot() + route_manager.maneuver_alerts()
    route_active, dist_to_dest = route_manager.status()

    msg = messaging.new_message('externalNavDataSP')
    msg.valid = True
    data = msg.externalNavDataSP
    data.routeActive = route_active
    data.distanceToDestination = dist_to_dest
    alert_list = data.init('alerts', len(alerts))
    for i, a in enumerate(alerts):
      alert_list[i].alertType = maneuver_types[a["type"]]
      alert_list[i].speedLimit = a["speed_limit"]
      alert_list[i].distance = a["remaining"]
      alert_list[i].age = a["age"]
    pm.send('externalNavDataSP', msg)

    rk.keep_time()


if __name__ == "__main__":
  main()
