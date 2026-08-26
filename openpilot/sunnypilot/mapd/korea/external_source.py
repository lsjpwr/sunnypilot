"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

external_source: a UDP JSON sink a phone-side navigation app can push into. Nothing
ships that app yet -- this is the socket it will connect to; the caller is responsible
for only starting it when KoreaExternalNavEnabled is set.

Wire format, one JSON object per datagram, every key optional:

  {"speed_limit_kph": 60, "next_speed_limit_kph": 50,
   "next_speed_limit_distance_m": 320, "road_name": "테헤란로"}

Unknown keys are kept in ExternalNav.raw so a future app can extend the payload
without a wire-format change.

These values reach longitudinal control, so treat every datagram as untrusted:
implausible numbers become 0 rather than being passed through.
"""
import json
import socket
import threading
import time
from dataclasses import dataclass, field

MAX_DATAGRAM = 8192
EXTERNAL_PORT = 5555
EXTERNAL_TTL = 5.  # s, treat anything older as gone
RECV_TIMEOUT = .25  # s, how long recvfrom holds the socket before letting stop() have it

MAX_SPEED_LIMIT_KPH = 130.
MAX_DISTANCE_M = 10000.
MAX_ROAD_NAME = 64


@dataclass
class ExternalNav:
  speed_limit_kph: float = 0.
  next_speed_limit_kph: float = 0.
  next_speed_limit_distance_m: float = 0.
  road_name: str = ""
  received_at: float = 0.
  raw: dict = field(default_factory=dict)


def _bounded(payload: dict, key: str, upper: float) -> float:
  """Numbers only, and only inside (0, upper]. Anything else is 0."""
  value = payload.get(key)
  if isinstance(value, bool) or not isinstance(value, int | float):
    return 0.
  return float(value) if 0. < value <= upper else 0.


def parse_payload(payload: dict) -> ExternalNav:
  name = payload.get("road_name", "")
  return ExternalNav(
    speed_limit_kph=_bounded(payload, "speed_limit_kph", MAX_SPEED_LIMIT_KPH),
    next_speed_limit_kph=_bounded(payload, "next_speed_limit_kph", MAX_SPEED_LIMIT_KPH),
    next_speed_limit_distance_m=_bounded(payload, "next_speed_limit_distance_m", MAX_DISTANCE_M),
    road_name=name[:MAX_ROAD_NAME] if isinstance(name, str) else "",
    received_at=time.monotonic(),
    raw=payload,
  )


class ExternalNavSource:
  def __init__(self, port: int = EXTERNAL_PORT):
    self.port = port
    self._lock = threading.Lock()
    self._latest = ExternalNav()
    self._sock: socket.socket | None = None
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", self.port))
    # Not a receive deadline -- it is what makes stop() able to give the port back. A
    # thread parked in a blocking recvfrom holds the file description open right through
    # close(), so the next source to enable this gets EADDRINUSE. socket.timeout is an
    # OSError, so the loop below already treats a wakeup the same as any other.
    sock.settimeout(RECV_TIMEOUT)
    self.port = sock.getsockname()[1]  # resolves port 0 to what the OS picked
    self._sock = sock
    self._thread = threading.Thread(target=self._recv_loop, args=(sock,), daemon=True)
    self._thread.start()

  def stop(self) -> None:
    sock, self._sock = self._sock, None
    if sock is not None:
      sock.close()
    thread, self._thread = self._thread, None
    if thread is not None:
      thread.join(timeout=2.)  # the port is not free until the recv thread has let go of it

  def latest(self) -> ExternalNav | None:
    """Most recent payload, or None when nothing has arrived inside EXTERNAL_TTL."""
    with self._lock:
      nav = self._latest
    if nav.received_at == 0. or time.monotonic() - nav.received_at > EXTERNAL_TTL:
      return None
    return nav

  def _recv_loop(self, sock: socket.socket) -> None:
    while True:
      try:
        data, _ = sock.recvfrom(MAX_DATAGRAM)
      except OSError:
        if self._sock is None:
          return  # socket closed by stop()
        # The RECV_TIMEOUT wakeup arrives here a few times a second while nothing is sent,
        # and a datagram bigger than MAX_DATAGRAM also raises OSError here (WSAEMSGSIZE on
        # Windows) while the socket is still open -- drop it and keep listening rather
        # than let an oversize hostile packet kill the loop the same way.
        # ponytail: no backoff here. Every OSError reachable on an unconnected UDP socket
        # is one-shot per datagram (recvfrom blocks again next iteration), and on Linux --
        # the actual device -- an oversize datagram truncates silently instead of raising,
        # so this branch should never fire in production. Add a sleep only if a persistent
        # non-blocking error is ever observed spinning here.
        continue

      try:
        payload = json.loads(data)
        if not isinstance(payload, dict):
          continue
        nav = parse_payload(payload)
      except Exception:
        # One datagram must never kill this loop. Deeply nested JSON raises
        # RecursionError (a RuntimeError, not a ValueError), and anything uncaught
        # here silently disables the feature until the process restarts.
        continue

      with self._lock:
        self._latest = nav
