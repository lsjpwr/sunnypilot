"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla: reads the destination the car's own navigation is guiding to, through Tesla's
servers while the phone is connected, and shares it with the phone through NavDestination.

The other way too: the trip the phone app (sp-knavlink-tesla) guides, which reaches this
module through KoreaMapData, is sent to the car, and what the car guides to goes back to
the phone in the comma's answer to each datagram (external_source.py). Whichever of the
phone and the car changed its destination last wins. The destination is the one command
sent to the car.

Two ways in. The Fleet API is official; tesla_setup sets it up from a PC, with vehicle_cmds
for the destinations. The owner API is the Tesla app's own, unofficial one: an owner token
put on the device by hand over ssh, no developer app, but the token is the whole account.
Fleet wins when both are set.

Deliberately free of openpilot imports at module level so it runs under a bare Python
interpreter -- same rule as route.py. Only _loop imports the device stack.
"""
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from openpilot.sunnypilot.mapd.korea.fleet_budget import FleetBudget
from openpilot.sunnypilot.mapd.korea.geo import haversine
from openpilot.sunnypilot.mapd.korea.route import RequestBudget, in_korea

LOG = logging.getLogger(__name__)

# Korea is served from the North America / Asia-Pacific region.
FLEET_API_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
HTTP_TIMEOUT_S = 10.
# location_data is what puts coordinates in drive_state: without it Tesla answers with
# active_route_destination alone (teslamotors/fleet-telemetry#392).
VEHICLE_DATA_QUERY = urllib.parse.urlencode({"endpoints": "drive_state;location_data"})
MAX_PLACE_NAME = 64  # same cut as external_source.MAX_ROAD_NAME

# The owner API is the Tesla app's own, unofficial one -- NaviToTesla uses it. It needs no
# developer app and bills nothing, but its token is the whole account, which is why Fleet
# wins when both are set (TeslaDestinationSource.step).
OWNER_API_URL = "https://owner-api.teslamotors.com"
OWNER_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
OWNER_CLIENT_ID = "ownerapi"
OWNER_SCOPE = "openid email offline_access"
# auth.tesla.com picks the kind of token by the TLS version of the refresh: below 1.3 it mints
# a Fleet token, which owner-api refuses with 403 (snowake.dev, 2026-06). Only the refresh
# needs it -- the token's kind is settled there -- so the API calls keep the default context.
OWNER_TLS = ssl.create_default_context()
OWNER_TLS.minimum_version = ssl.TLSVersion.TLSv1_3

POLL_INTERVAL_S = 60.
RATE_LIMIT_PAUSE_S = 300.
# Five hours of driving at one request a minute; monthly accounting uses the agreed units,
# not a guarantee about current Tesla prices or credits.
DAILY_REQUEST_CAP = 300
# Closer than this is the same destination, avoiding repeated writes for GPS jitter.
SAME_DESTINATION_M = 50.
FLEET, OWNER = "fleet", "owner"
# Where each mode keeps its single-use refresh token.
REFRESH_TOKEN_KEYS = {FLEET: "KoreaTeslaRefreshToken", OWNER: "KoreaTeslaOwnerRefreshToken"}

# Polls in a row with no route before the car's guidance counts as ended. The phone ends its
# own guidance on it, so one empty answer -- a glitch -- must not be enough.
CANCEL_POLLS = 2
# Sending the car a destination. Commands are billed too; a trip costs one to seven of them.
DAILY_COMMAND_CAP = 60
SEND_RETRY_S = 30.  # the car asleep, or Tesla busy: try again this much later,
SEND_TRIES = 4  # this many times, then leave the trip alone until it changes
# What the car reports after it was sent a trip is the car taking it, not a new destination:
# anything within this distance of one of the trip's places (the car searches a shared point,
# so it can land a little off, and it reports each via on the way), and anything at all this
# soon after the send.
ECHO_M = 500.
ECHO_WINDOW_S = 150.
CAR_UNKNOWN = "unknown"  # TeslaDestinationSource.car() before this drive's first answer


class AuthRejected(Exception):
  """Tesla refused the refresh token. Only a new login replaces it: tesla_setup for Fleet, a token tool for the owner API."""


class VehicleNotFound(Exception):
  """The owner account has no car with KoreaTeslaVin. Only fixing the VIN helps."""


class CommandRefused(Exception):
  """The car answered a destination with result false."""


class FleetStopped(Exception):
  """A local budget or its storage prevented a network request."""


@dataclass(frozen=True)
class Trip:
  """What the phone app guides to, each place (lat, lon, name): the destination, and the vias on the way in order."""
  destination: tuple[float, float, str]
  vias: tuple[tuple[float, float, str], ...] = ()

  def places(self) -> tuple[tuple[float, float, str], ...]:
    return (self.destination, *self.vias)


def _near(a, b, metres: float = SAME_DESTINATION_M) -> bool:
  return haversine(a[0], a[1], b[0], b[1]) <= metres


def same_trip(a: Trip | None, b: Trip | None, vias: bool = True) -> bool:
  """The same places within a jitter; with vias=False the destination alone."""
  if a is None or b is None:
    return a is b
  return _near(a.destination, b.destination) and (not vias or len(a.vias) == len(b.vias) and all(map(_near, a.vias, b.vias)))


def parse_destination(payload) -> tuple[float, float, str] | None:
  """(lat, lon, name) of the car's active route; None for no route and for anything that
  does not look like one. It becomes a route that reaches longitudinal control, so it is
  untrusted the same way external_source.py treats a datagram."""
  response = payload.get("response") if isinstance(payload, dict) else None
  drive_state = response.get("drive_state") if isinstance(response, dict) else None
  if not isinstance(drive_state, dict):
    return None
  lat, lon = drive_state.get("active_route_latitude"), drive_state.get("active_route_longitude")
  if not all(isinstance(v, int | float) for v in (lat, lon)) or not in_korea(lat, lon):
    return None
  name = drive_state.get("active_route_destination")
  return float(lat), float(lon), name[:MAX_PLACE_NAME] if isinstance(name, str) else ""


def _read_json(request: urllib.request.Request, opener, context: ssl.SSLContext | None = None) -> dict:
  with opener(request, timeout=HTTP_TIMEOUT_S, context=context) as response:
    payload = json.loads(response.read())
  if not isinstance(payload, dict):
    raise ValueError("answer is not a JSON object")
  return payload


def token_request(fields: dict[str, str]) -> urllib.request.Request:
  """Secrets travel in the form body, never the URL: urllib puts the URL in exception
  messages, and those reach cloudlog -- route.py:125 makes the same promise."""
  return urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(fields).encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")


def api_request(access_token: str, path: str, base: str = FLEET_API_URL) -> urllib.request.Request:
  return urllib.request.Request(base + path, headers={"Authorization": f"Bearer {access_token}"})


def vehicle_data_request(access_token: str, vin: str) -> urllib.request.Request:
  return api_request(access_token, f"/api/1/vehicles/{urllib.parse.quote(vin, safe='')}/vehicle_data?{VEHICLE_DATA_QUERY}")


def _refresh(request: urllib.request.Request, opener, context: ssl.SSLContext | None = None) -> tuple[str, str]:
  """(access token, the refresh token that replaces the one in `request`).

  Tesla's refresh tokens are single use, so the caller must save the second value before
  anything else can fail. AuthRejected when Tesla refuses the token; any other failure is
  raised as it is -- a 5xx says nothing about the token.
  """
  try:
    payload = _read_json(request, opener, context)
  except urllib.error.HTTPError as e:
    if e.code in (400, 401):
      raise AuthRejected(f"HTTP {e.code}") from None
    raise
  access, replacement = payload.get("access_token"), payload.get("refresh_token")
  if not (isinstance(access, str) and access and isinstance(replacement, str) and replacement):
    raise ValueError("token answer without tokens")
  return access, replacement


def refresh_access_token(client_id: str, refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]:
  """Fleet: a refresh needs only the client id, never the secret."""
  return _refresh(token_request({"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token}), opener)


def fetch_destination(access_token: str, vin: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None:
  """One billed vehicle_data request. An HTTPError is raised as it is: its status decides
  what the caller does next."""
  return parse_destination(_read_json(vehicle_data_request(access_token, vin), opener))


def owner_token_request(refresh_token: str) -> urllib.request.Request:
  """The refresh the way the Tesla app's own client makes it; NaviToTesla sends this same JSON
  (2026-09). The token rides the body, never the URL -- same promise as token_request."""
  body = {"grant_type": "refresh_token", "client_id": OWNER_CLIENT_ID, "refresh_token": refresh_token, "scope": OWNER_SCOPE}
  return urllib.request.Request(OWNER_TOKEN_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                method="POST")


def owner_refresh_access_token(refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]:
  """refresh_access_token for an owner token, over TLS 1.3. Single use as well."""
  return _refresh(owner_token_request(refresh_token), opener, OWNER_TLS)


def owner_vehicle_id(access_token: str, vin: str, opener=urllib.request.urlopen) -> str:
  """The owner API addresses a car by its id in /api/1/products, not by VIN -- NaviToTesla
  does the same. The answer is untrusted and the id goes into a path, so only an int is
  taken (bool, an int subclass, is not). A VIN typed by hand on a phone may be lower case or
  padded, so it is compared without either."""
  products = _read_json(api_request(access_token, "/api/1/products", OWNER_API_URL), opener).get("response")
  for product in products if isinstance(products, list) else []:
    if isinstance(product, dict) and str(product.get("vin")).upper() == vin.strip().upper() and type(product.get("id")) is int:
      return str(product["id"])
  raise VehicleNotFound("no car with this VIN on the account")


def owner_fetch_destination(access_token: str, vehicle_id: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None:
  """fetch_destination through the owner API."""
  path = f"/api/1/vehicles/{urllib.parse.quote(vehicle_id, safe='')}/vehicle_data?{VEHICLE_DATA_QUERY}"
  return parse_destination(_read_json(api_request(access_token, path, OWNER_API_URL), opener))


def command_request(access_token: str, base: str, vehicle: str, command: str, body: dict) -> urllib.request.Request:
  return urllib.request.Request(f"{base}/api/1/vehicles/{urllib.parse.quote(vehicle, safe='')}/command/{command}",
                                data=json.dumps(body).encode(), method="POST",
                                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})


def share_body(lat: float, lon: float, now_ms: int) -> dict:
  """navigation_request: what a phone shares with the car, here the point as text, which the
  car searches the way it does an address."""
  return {"type": "share_ext_content_raw", "locale": "ko-KR", "timestamp_ms": str(now_ms),
          "value": {"android.intent.extra.TEXT": f"{lat:.6f},{lon:.6f}"}}


def holds_destination(raw, written: tuple[float, float]) -> bool:
  """Does NavDestination still hold exactly what its writer wrote? json round-trips a
  float exactly, so equality is right here -- unlike between two readings of the car."""
  try:
    dest = json.loads(raw)
    return (float(dest["latitude"]), float(dest["longitude"])) == written
  except (TypeError, ValueError, KeyError):
    return False


class TeslaDestinationSource:
  """Background thread that copies the car's navigation destination into NavDestination, and
  gives the car the phone app's trip.

  step() makes every decision and gets the device
  state as arguments, so the tests drive it with no device stack; _loop only feeds it.
  want() is called from KoreaMapData's loop and car() from the UDP thread: both swap one
  immutable value, which needs no lock.
  """

  def __init__(self, opener=urllib.request.urlopen, clock=time.monotonic, phone_connected=lambda: False, monthly=None):
    self._opener = opener
    self._clock = clock
    self.phone_connected = phone_connected
    self.budget = RequestBudget(cap=DAILY_REQUEST_CAP)
    self.commands = RequestBudget(cap=DAILY_COMMAND_CAP)
    self.monthly = monthly
    self._fleet_status = 0
    self._access_token: str | None = None
    # The credentials the access token was minted under. Any change -- the other mode, tesla_setup,
    # a token or VIN written by hand -- must not be judged by it: the two APIs do not take each
    # other's tokens, and another account's VIN is not on this one.
    self._minted_for: tuple | None = None
    # The owner API's id for the car, looked up once per access token.
    self._owner_vehicle: str | None = None
    # What this thread last wrote, so the car repeating it is not another write.
    self._written: tuple[float, float] | None = None
    # The credentials Tesla refused: (mode, client id, refresh token, VIN). Polling waits for
    # any of them to change -- tesla_setup writing new ones, or a VIN fixed by hand over ssh.
    self._rejected: tuple | None = None
    self._next_poll = 0.
    self._was_started = False
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None
    # The car's navigation for the phone: CAR_UNKNOWN until this drive's first answer, None for
    # no route, else (lat, lon, name). It changes only when the car's answer changes.
    self._car = CAR_UNKNOWN
    self._car_raw: tuple[float, float, str] | None = None  # the car's last answer, to tell a change from a repeat
    self._empty_polls = 0
    self._polled = False  # an answer this drive, so the car's destination is known before one is sent
    self._want: Trip | None = None  # the phone app's trip, each second from KoreaMapData
    self._sent: Trip | None = None  # the trip the car was given or already had, so it is not sent again
    self._sent_places: tuple = ()
    self._sent_at = float("-inf")
    self._send_after = 0.
    self._send_tries = 0
    self._gps: bool | None = None  # does the car take a destination as coordinates? Unknown until asked
    self._refused: tuple | None = None  # the credentials the car refused a destination under
    self._command_refused: Trip | None = None

  def want(self, trip: Trip | None) -> None:
    """The trip the phone app guides, None while it guides nowhere or is quiet."""
    self._want = trip

  def car(self):
    """What the car's navigation guides to, for the phone app: CAR_UNKNOWN, None or (lat, lon, name)."""
    return self._car

  def fleet_status(self) -> int:
    """Wire f: available, refusal, daily cap, monthly cap, or unsafe budget storage."""
    return self._fleet_status

  def _reserve(self, daily: RequestBudget, units: int, mode: str) -> None:
    if not daily.allow():
      self._fleet_status = 2
      raise FleetStopped
    status = 0
    if mode == FLEET:
      if self.monthly is None:
        from openpilot.common.hardware.hw import Paths
        self.monthly = FleetBudget(Path(Paths.korea_map_root()) / "fleet_budget.json")
      status = self.monthly.reserve(units)
      if status:
        self._fleet_status = status
        raise FleetStopped
      status = self.monthly.reserve(0)
      if status == 4:
        self._fleet_status = status
        raise FleetStopped
    daily.spend()
    daily_stopped = not self.budget.allow() or not self.commands.allow()
    refused = (self._refused == self._minted_for
               or self._command_refused is not None and same_trip(self._want, self._command_refused))
    self._fleet_status = status or (2 if daily_stopped else int(refused))

  def guides_to(self, point) -> bool:
    car = self._car
    return isinstance(car, tuple) and _near(car, point)

  def start(self) -> None:
    if self._thread is not None:
      return
    self._thread = threading.Thread(target=self._loop, daemon=True)
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()
    if self._thread is not None:
      # ponytail: join gives up after 2 s while a request can take 10 s, so a korea_main restart
      # in the middle of a token refresh can let two threads spend the same single-use token.
      # Normally that is one spurious alert healed on the next minute, unless Tesla revokes the
      # whole token family on reuse. Join longer if it is ever seen.
      self._thread.join(timeout=2.)
      self._thread = None

  def step(self, params, started: bool, set_alert) -> None:
    """One tick. set_alert(bool) shows or clears Offroad_KoreaTeslaAuth."""
    now = self._clock()
    if started and not self._was_started:
      # A new drive. The offroad transition cleared NavDestination, so what this thread wrote
      # last drive is no reason to skip it now -- and the first poll of a drive does not wait
      # out the last drive's minute. What the car had is asked again, and a trip the phone
      # still guides is checked against it.
      self._written = None
      self._next_poll = now
      self._car, self._car_raw, self._empty_polls, self._polled = CAR_UNKNOWN, None, 0, False
      self._sent, self._sent_places, self._sent_at, self._send_tries, self._send_after = None, (), float("-inf"), 0, 0.
    self._was_started = started
    if not started or not self.phone_connected():
      return
    if now >= self._next_poll:
      self._next_poll = now + POLL_INTERVAL_S
      self._poll(params, set_alert, now)
    self._send(now)

  def _poll(self, params, set_alert, now: float) -> None:
    client_id, vin = params.get("KoreaTeslaClientId"), params.get("KoreaTeslaVin")
    fleet_token, owner_token = params.get("KoreaTeslaRefreshToken"), params.get("KoreaTeslaOwnerRefreshToken")
    # Fleet first when both are set: it is official, and its token is not the whole account. The
    # owner API is for owners without a Tesla developer app. No fallback either way.
    if client_id and fleet_token and vin:
      mode, refresh_token = FLEET, fleet_token
    elif owner_token and vin:
      mode, refresh_token = OWNER, owner_token
    else:
      return
    credentials = (mode, client_id, refresh_token, vin)
    if credentials == self._rejected:
      return
    if credentials != self._minted_for:
      self._access_token = None
      self._command_refused = None
    if not self.budget.allow():
      self._fleet_status = 2
      LOG.warning("tesla: daily request cap reached")
      return

    fresh = self._access_token is None
    try:
      if fresh:
        if mode == FLEET:
          self._access_token, refresh_token = refresh_access_token(client_id, refresh_token, self._opener)
        else:
          self._access_token, refresh_token = owner_refresh_access_token(refresh_token, self._opener)
        credentials = self._minted_for = (mode, client_id, refresh_token, vin)
        self._owner_vehicle = None
        # Single use: Tesla retired the old token the moment it answered. Save the new one
        # before anything else can fail, or nothing that works is left on the device. Params.put
        # only queues the write here unless block=True, and this is the one write that must be
        # on flash before the token is used.
        params.put(REFRESH_TOKEN_KEYS[mode], refresh_token, block=True)
        set_alert(False)
      destination = self._fetch(mode, vin)
    except FleetStopped:
      return
    except AuthRejected:
      LOG.warning("tesla: refresh token rejected, set up the Tesla login again")
      self._reject(credentials, set_alert)
      return
    except VehicleNotFound:
      LOG.warning("tesla: no car with KoreaTeslaVin on the owner account")
      self._reject(credentials, set_alert)
      return
    except urllib.error.HTTPError as e:
      LOG.warning("tesla: request failed: HTTP %d", e.code)
      if e.code == 401 and not fresh:
        # The access token outlived its hours. Refresh on the next tick rather than a minute
        # from now -- this is usually the first poll of a drive.
        self._access_token = None
        self._next_poll = now
      elif e.code == 429:
        self._next_poll = now + RATE_LIMIT_PAUSE_S
      elif 400 <= e.code < 500 and e.code != 408:
        # A token refused right after its refresh, a grant without vehicle_location, a VIN the
        # account no longer has, a missing partner registration, the wrong region, unpaid
        # billing, an account the owner API no longer serves: none of it changes by asking
        # again, and on Fleet every ask is billed. 408 (the car is asleep) and 5xx stay "ask
        # again next minute".
        self._reject(credentials, set_alert)
      return
    except Exception as e:
      # The network, a timeout, a body that is not JSON: all "ask again next minute". Only the
      # type is logged -- a message can carry the URL, and a body the car's position.
      LOG.warning("tesla: request failed: %s", type(e).__name__)
      return
    self._observe(params, destination, now)

  def _fetch(self, mode: str, vin: str) -> tuple[float, float, str] | None:
    if mode == OWNER and self._owner_vehicle is None:
      self._owner_vehicle = owner_vehicle_id(self._access_token, vin, self._opener)
    self._reserve(self.budget, 2, mode)
    if mode == FLEET:
      return fetch_destination(self._access_token, vin, self._opener)
    return owner_fetch_destination(self._access_token, self._owner_vehicle, self._opener)

  def _reject(self, credentials: tuple, set_alert) -> None:
    self._fleet_status = 1
    self._rejected = credentials
    self._access_token = None
    set_alert(True)

  def _observe(self, params, reading: tuple[float, float, str] | None, now: float) -> None:
    """One answer of the car. Only a change counts -- the car repeating its destination is
    never a write -- so the last change wins wherever it was made: on the car's screen, or in
    the phone app through _send."""
    first, self._polled = not self._polled, True
    if reading is None:
      self._empty_polls += 1
      if self._empty_polls < CANCEL_POLLS:
        return
      self._car_raw = None
      if self._car is not None:
        self._car = None
        self._apply(params, None)
      return
    self._empty_polls = 0
    if self._car_raw is not None and _near(reading, self._car_raw):
      return
    self._car_raw = reading
    if self._sent_places and (now - self._sent_at <= ECHO_WINDOW_S or any(_near(reading, p, ECHO_M) for p in self._sent_places)):
      # The car taking the trip _send gave it, or on its way to one of the vias: still the
      # phone's trip. ponytail: a destination picked on the car's screen this soon after a
      # send is taken for the car taking it, until the car's next change.
      self._car = self._sent_places[0]
      return
    if first and self._want is not None:
      return  # the phone already guides: its trip is newer than what the car kept, and _send gives it
    self._car = reading
    self._sent_places = ()
    self._apply(params, reading)

  def _send(self, now: float) -> None:
    """Gives the car the phone app's trip, once this drive's first answer has said what the car has."""
    trip = self._want
    if trip is None:
      self._sent = None  # the next trip goes to the car even if it is this one again
      return
    if not self.budget.allow():
      self._fleet_status = 2
      return
    if self._fleet_status in (3, 4):
      return
    if (not self._polled or self._access_token is None or now < self._send_after or self._minted_for == self._refused
        or same_trip(trip, self._sent, vias=self._gps is not False)):
      return
    if self._car_raw is not None and _near(self._car_raw, trip.destination) and (not trip.vias or self._gps is False):
      # The car guides there already: the phone took it from the car, or the car kept it.
      self._sent, self._sent_places, self._car = trip, trip.places(), trip.destination
      return
    if not self.commands.allow():
      self._fleet_status = 2
      LOG.warning("tesla: daily command cap reached")
      self._sent = trip
      return
    try:
      self._command(trip)
    except FleetStopped:
      self._send_after = now + POLL_INTERVAL_S
    except urllib.error.HTTPError as e:
      LOG.warning("tesla: destination not sent: HTTP %d", e.code)
      if e.code == 401:
        self._access_token, self._next_poll = None, now  # the next tick's poll refreshes it
      elif e.code in (408, 429) or e.code >= 500:
        self._retry(trip, now)
      else:
        # 403 is a grant without vehicle_cmds, which only tesla_setup changes; nothing else
        # here changes by asking again either.
        self._refused = self._minted_for
        self._fleet_status = 1
    except CommandRefused:
      self._sent = trip
      self._command_refused = trip
      self._fleet_status = 1
    except Exception as e:
      LOG.warning("tesla: destination not sent: %s", type(e).__name__)
      self._retry(trip, now)
    else:
      self._command_refused = None
      if self._fleet_status == 1:
        self._fleet_status = 0
      # Not self._car yet: that is the car's answer. A share the car could not find would
      # otherwise read as the car ending the guidance, and end the phone's.
      self._sent, self._sent_places, self._sent_at, self._send_tries = trip, trip.places(), now, 0

  def _retry(self, trip: Trip, now: float) -> None:
    self._send_tries += 1
    if self._send_tries < SEND_TRIES:
      self._send_after = now + SEND_RETRY_S
    else:
      self._sent, self._send_tries = trip, 0

  def _command(self, trip: Trip) -> None:
    """navigation_gps_request while the car takes coordinates, the vias with it; else
    navigation_request, which shares one place, the destination."""
    mode, _, _, vin = self._minted_for
    base, vehicle = (FLEET_API_URL, vin) if mode == FLEET else (OWNER_API_URL, self._owner_vehicle)
    lat, lon, _ = trip.destination
    if self._gps is not False:
      try:
        took = self._post(base, vehicle, "navigation_gps_request", {"lat": lat, "lon": lon, "order": 1})
      except urllib.error.HTTPError as e:
        if e.code in (401, 408, 429) or e.code >= 500:
          raise  # about the token, the car or Tesla, not about the command
        took = False
      if took:
        self._gps = True
        # ponytail: order 2 makes a place the next stop (Teslemetry's reading of the command),
        # so the last via goes in first. Unverified on the car: check its trip the first time.
        for via_lat, via_lon, _ in reversed(trip.vias):
          if not self._post(base, vehicle, "navigation_gps_request", {"lat": via_lat, "lon": via_lon, "order": 2}):
            raise CommandRefused
        return
    if not self._post(base, vehicle, "navigation_request", share_body(lat, lon, int(time.time() * 1000))):  # noqa: TID251
      raise CommandRefused
    if self._gps is None:
      # Only now: a grant without vehicle_cmds refuses both, and says nothing about coordinates.
      LOG.warning("tesla: the car takes no coordinates, sharing destinations instead")
      self._gps = False

  def _post(self, base: str, vehicle: str, command: str, body: dict) -> bool:
    """One billed command; True when the car took it."""
    self._reserve(self.commands, 1, self._minted_for[0])
    response = _read_json(command_request(self._access_token, base, vehicle, command, body), self._opener).get("response")
    if isinstance(response, dict) and response.get("result") is True:
      return True
    LOG.warning("tesla: %s refused: %s", command, str(response.get("reason") if isinstance(response, dict) else response)[:80])
    return False

  def _apply(self, params, destination: tuple[float, float, str] | None) -> None:
    if destination is None:
      # Guidance ended in the car. Clear only what this thread wrote: a destination from the
      # UDP socket or athenad is not the car's to cancel.
      if self._written is not None and holds_destination(params.get("NavDestination"), self._written):
        params.remove("NavDestination")
      self._written = None
      return
    lat, lon, name = destination
    if self._written is not None and haversine(lat, lon, *self._written) <= SAME_DESTINATION_M:
      # An unchanged car destination must not restore a trip the phone already ended.
      return
    params.put("NavDestination", json.dumps({"latitude": lat, "longitude": lon,
                                             "place_name": name or None, "place_details": None}))
    self._written = (lat, lon)

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is what
    # lets the tests run under a bare interpreter
    from openpilot.cereal import messaging
    from openpilot.common.params import Params
    from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert

    params = Params()
    sm = messaging.SubMaster(['deviceState'])

    def set_alert(show: bool) -> None:
      set_offroad_alert("Offroad_KoreaTeslaAuth", show)

    while not self._stop.is_set():
      try:
        sm.update(0)
        # A SubMaster that has received nothing reads the capnp default, started=False --
        # "not yet", which is the safe answer here.
        self.step(params, sm['deviceState'].started, set_alert)
      except Exception:
        # No supervisor: dying here ends destinations from the car until the process
        # restarts. Keep going and try again next second.
        LOG.exception("tesla: source loop error")
      self._stop.wait(1.)
