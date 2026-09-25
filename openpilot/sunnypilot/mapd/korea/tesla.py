"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla: reads the destination the car's own navigation is guiding to, through the Tesla
Fleet API, and hands it to RouteSource through NavDestination.

The driver already sends every destination to the car (NaviToTesla on the phone), so the
car is the one place that always has it -- reading it back beats a patched phone app
sending it a second time. The grant is read-only: nothing here can command the car.

Deliberately free of openpilot imports at module level so it runs under a bare Python
interpreter -- same rule as route.py. Only _loop imports the device stack.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from openpilot.sunnypilot.mapd.korea.route import in_korea

LOG = logging.getLogger(__name__)

# Korea is served from the North America / Asia-Pacific region.
FLEET_API_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
HTTP_TIMEOUT_S = 10.
# location_data is what puts coordinates in drive_state: without it Tesla answers with
# active_route_destination alone (teslamotors/fleet-telemetry#392).
VEHICLE_DATA_QUERY = urllib.parse.urlencode({"endpoints": "drive_state;location_data"})
MAX_PLACE_NAME = 64  # same cut as external_source.MAX_ROAD_NAME


class AuthRejected(Exception):
  """Tesla refused the refresh token. Only tesla_setup can get a new one."""


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


def _read_json(request: urllib.request.Request, opener) -> dict:
  with opener(request, timeout=HTTP_TIMEOUT_S) as response:
    payload = json.loads(response.read())
  if not isinstance(payload, dict):
    raise ValueError("answer is not a JSON object")
  return payload


def token_request(fields: dict[str, str]) -> urllib.request.Request:
  """Secrets travel in the form body, never the URL: urllib puts the URL in exception
  messages, and those reach cloudlog -- route.py:125 makes the same promise."""
  return urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(fields).encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")


def api_request(access_token: str, path: str) -> urllib.request.Request:
  return urllib.request.Request(FLEET_API_URL + path, headers={"Authorization": f"Bearer {access_token}"})


def refresh_access_token(client_id: str, refresh_token: str, opener=urllib.request.urlopen) -> tuple[str, str]:
  """(access token, the refresh token that replaces this one).

  Tesla's refresh tokens are single use, so the caller must save the second value before
  anything else can fail. AuthRejected when Tesla refuses the token; any other failure is
  raised as it is -- a 5xx says nothing about the token.
  """
  request = token_request({"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token})
  try:
    payload = _read_json(request, opener)
  except urllib.error.HTTPError as e:
    if e.code in (400, 401):
      raise AuthRejected(f"HTTP {e.code}") from None
    raise
  access, replacement = payload.get("access_token"), payload.get("refresh_token")
  if not (isinstance(access, str) and access and isinstance(replacement, str) and replacement):
    raise ValueError("token answer without tokens")
  return access, replacement


def fetch_destination(access_token: str, vin: str, opener=urllib.request.urlopen) -> tuple[float, float, str] | None:
  """One billed vehicle_data request. An HTTPError is raised as it is: its status decides
  what the caller does next."""
  path = f"/api/1/vehicles/{urllib.parse.quote(vin, safe='')}/vehicle_data?{VEHICLE_DATA_QUERY}"
  return parse_destination(_read_json(api_request(access_token, path), opener))
