"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla: reads the destination the car's own navigation is guiding to, through Tesla's
servers, and hands it to RouteSource through NavDestination.

The driver already sends every destination to the car (NaviToTesla on the phone), so the
car is the one place that always has it -- reading it back beats a patched phone app
sending it a second time. Nothing here commands the car.

Two ways in. The Fleet API is official and its grant is read-only; tesla_setup sets it up
from a PC. The owner API is the Tesla app's own, unofficial one: an owner token put on the
device by hand over ssh, no developer app, but the token is the whole account. Fleet wins
when both are set.

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
# Five hours of driving at one request a minute. Tesla bills 500 data requests per $1 and
# takes $10 a month off, so this guards against a runaway, not against normal use.
DAILY_REQUEST_CAP = 300
# Closer than this is the same destination. Rewriting one that only jittered would have
# RouteSource drop its route and ask TMAP again (route.py:402).
SAME_DESTINATION_M = 50.
FLEET, OWNER = "fleet", "owner"
# Where each mode keeps its single-use refresh token.
REFRESH_TOKEN_KEYS = {FLEET: "KoreaTeslaRefreshToken", OWNER: "KoreaTeslaOwnerRefreshToken"}


class AuthRejected(Exception):
  """Tesla refused the refresh token. Only a new login replaces it: tesla_setup for Fleet, a token tool for the owner API."""


class VehicleNotFound(Exception):
  """The owner account has no car with KoreaTeslaVin. Only fixing the VIN helps."""


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


def _is_ours(raw, written: tuple[float, float]) -> bool:
  """Does NavDestination still hold exactly what this thread wrote? json round-trips a
  float exactly, so equality is right here -- unlike between two readings of the car."""
  try:
    dest = json.loads(raw)
    return (float(dest["latitude"]), float(dest["longitude"])) == written
  except (TypeError, ValueError, KeyError):
    return False


class TeslaDestinationSource:
  """Background thread that copies the car's navigation destination into NavDestination.

  Same start/stop shape as RouteSource. step() makes every decision and gets the device
  state as arguments, so the tests drive it with no device stack; _loop only feeds it.
  """

  def __init__(self, opener=urllib.request.urlopen, clock=time.monotonic):
    self._opener = opener
    self._clock = clock
    self.budget = RequestBudget(cap=DAILY_REQUEST_CAP)
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
      # whole token family on reuse. RouteSource shares the limit; join longer if it is ever seen.
      self._thread.join(timeout=2.)
      self._thread = None

  def step(self, params, started: bool, set_alert) -> None:
    """One tick. set_alert(bool) shows or clears Offroad_KoreaTeslaAuth."""
    now = self._clock()
    if started and not self._was_started:
      # A new drive. The offroad transition cleared NavDestination, so what this thread wrote
      # last drive is no reason to skip it now -- and the first poll of a drive does not wait
      # out the last drive's minute.
      self._written = None
      self._next_poll = now
    self._was_started = started
    if not started or now < self._next_poll:
      return
    self._next_poll = now + POLL_INTERVAL_S

    # Without the TMAP key a destination buys no route, only a bill.
    if not params.get("KoreaRouteApiKey"):
      return
    client_id, vin = params.get("KoreaTeslaClientId"), params.get("KoreaTeslaVin")
    fleet_token, owner_token = params.get("KoreaTeslaRefreshToken"), params.get("KoreaTeslaOwnerRefreshToken")
    # Fleet first when both are set: it is official, and its grant cannot command the car. The
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
    if not self.budget.allow():
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
    self._apply(params, destination)

  def _fetch(self, mode: str, vin: str) -> tuple[float, float, str] | None:
    if mode == OWNER and self._owner_vehicle is None:
      self._owner_vehicle = owner_vehicle_id(self._access_token, vin, self._opener)
    self.budget.spend()
    if mode == FLEET:
      return fetch_destination(self._access_token, vin, self._opener)
    return owner_fetch_destination(self._access_token, self._owner_vehicle, self._opener)

  def _reject(self, credentials: tuple, set_alert) -> None:
    self._rejected = credentials
    self._access_token = None
    set_alert(True)

  def _apply(self, params, destination: tuple[float, float, str] | None) -> None:
    if destination is None:
      # Guidance ended in the car. Clear only what this thread wrote: a destination from the
      # UDP socket or athenad is not the car's to cancel.
      if self._written is not None and _is_ours(params.get("NavDestination"), self._written):
        params.remove("NavDestination")
      self._written = None
      return
    lat, lon, name = destination
    if self._written is not None and haversine(lat, lon, *self._written) <= SAME_DESTINATION_M:
      # Also the case after RouteSource cleared it on arrival: writing it back would only
      # have RouteSource clear it again, every minute, for as long as the car is parked there.
      return
    params.put("NavDestination", json.dumps({"latitude": lat, "longitude": lon,
                                             "place_name": name or None, "place_details": None}))
    self._written = (lat, lon)

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is what
    # lets the tests run under a bare interpreter -- same as RouteSource._loop
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
