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
import secrets
import socket
import struct
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 5556
WIFI_INTERFACE = "wlan0"
PAIR_CODE_PARAM = "PhoneBridgePairCode"
TOKEN_PARAM = "PhoneBridgeToken"
PAIR_CODE_S = 60.
MAX_CODE_FAILURES = 5
MAX_BODY = 4096  # bytes
REQUEST_TIMEOUT_S = 2.  # per socket operation, since one slow client holds up the rest

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891b


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
  return ipaddress.ip_interface(f"{address}/{netmask}").network


def in_subnet(client: str, network: ipaddress.IPv4Network | None) -> bool:
  return network is not None and ipaddress.ip_address(client) in network


class PhoneBridge:
  def __init__(self, params, mem_params, sm, now=time.monotonic):
    self.params, self.mem_params, self.sm, self.now = params, mem_params, sm, now
    self._failures = (None, 0)  # the code the wrong tries were against, and how many

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

    def log_message(self, format, *args):
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
        self._reply(404)

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
