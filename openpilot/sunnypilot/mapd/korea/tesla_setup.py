"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tesla_setup: the one-time setup behind korea/tesla.py. Runs on the PC:

  python -m openpilot.sunnypilot.mapd.korea.tesla_setup --host comma@<device-ip>

Registers the app's domain with Tesla, walks the owner through Tesla's login, and writes
KoreaTeslaClientId, KoreaTeslaVin and KoreaTeslaRefreshToken to the device over ssh. The
client secret is asked for, used and dropped. No secret or token is printed, put on a
command line or written to this machine's disk -- which is why --host is required instead
of a fallback that prints the values. docs/korea_tesla_destination.md has the steps
before this one.
"""
import argparse
import getpass
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from openpilot.sunnypilot.mapd.korea.tesla import (FLEET_API_URL, HTTP_TIMEOUT_S, api_request, parse_destination, token_request,
                                                   vehicle_data_request)

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
# Read-only on purpose: nothing on the device may command the car.
SCOPES = "openid offline_access vehicle_device_data vehicle_location"
DEVICE_PARAMS_DIR = "/data/params/d"


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
  return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
    "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES, "state": state,
    # Without it the consent page lets the owner untick vehicle_location, and vehicle_data
    # then answers without the destination's coordinates -- silently, on every drive.
    "require_requested_scopes": "true",
  })


def code_from_redirect(pasted: str, state: str) -> str:
  """The authorization code in the address Tesla sent the browser to. ValueError when that
  address is not the answer to this login."""
  query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
  if query.get("state") != [state]:
    raise ValueError("that address is not from this login (state does not match)")
  if "error" in query:
    raise ValueError(f"Tesla refused the login: {query['error'][0]}")
  if not query.get("code"):
    raise ValueError("that address has no code in it")
  return query["code"][0]


def choose_vin(vehicles: list) -> str:
  if not vehicles:
    raise ValueError("this Tesla account has no vehicle")
  if len(vehicles) == 1:
    return vehicles[0]["vin"]
  for i, vehicle in enumerate(vehicles, 1):
    print(f"  {i}. {vehicle.get('display_name') or '(no name)'}  {vehicle['vin']}")
  number = int(input("Which car? "))
  if not 1 <= number <= len(vehicles):
    raise ValueError(f"pick a number from 1 to {len(vehicles)}")
  return vehicles[number - 1]["vin"]


def check_device(host: str) -> None:
  """Fail before the Tesla login rather than after it: a login thrown away because ssh
  cannot reach the device has to be redone from the start."""
  try:
    result = subprocess.run(["ssh", host, f"test -d {DEVICE_PARAMS_DIR}"])
  except FileNotFoundError:
    raise ValueError("ssh is not installed on this PC") from None
  if result.returncode != 0:
    raise ValueError(f"cannot reach {host} over ssh, or it has no {DEVICE_PARAMS_DIR}")


def push_param(host: str, key: str, value: str) -> None:
  """One param on the device. The value rides ssh's stdin, never the command line, and
  lands by rename so the running device never reads half a token."""
  tmp = f"{DEVICE_PARAMS_DIR}/.{key}.tmp"
  subprocess.run(["ssh", host, f"cat > {tmp} && mv -f {tmp} {DEVICE_PARAMS_DIR}/{key}"], input=value.encode(), check=True)


def _send(request: urllib.request.Request) -> dict:
  with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
    return json.loads(response.read() or b"{}")


def _register_domain(partner_token: str, domain: str) -> None:
  request = urllib.request.Request(FLEET_API_URL + "/api/1/partner_accounts", data=json.dumps({"domain": domain}).encode(),
                                   headers={"Authorization": f"Bearer {partner_token}", "Content-Type": "application/json"},
                                   method="POST")
  try:
    _send(request)
  except urllib.error.HTTPError as e:
    # A second run may be refused as already registered. A real failure surfaces at the
    # vehicle list below, with Tesla's reason in it.
    print(f"Domain registration answered HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def _check(access_token: str, vin: str) -> None:
  """One real read, so a missing location grant shows up now instead of on the road."""
  try:
    data = _send(vehicle_data_request(access_token, vin))
  except urllib.error.HTTPError as e:
    if e.code != 408:
      raise
    print("The car is asleep, so the connection check was skipped.")
    return
  drive_state = (data.get("response") or {}).get("drive_state") or {}
  if "latitude" not in drive_state:
    print("WARNING: Tesla answered without the car's location. Run this again and allow Vehicle Location.")
  destination = parse_destination(data)
  print(f"Connection OK. The car's destination right now: {(destination[2] or '(unnamed)') if destination else 'none'}")


def main() -> None:
  parser = argparse.ArgumentParser(description="One-time Tesla setup so the device can read the car's navigation destination.")
  parser.add_argument("--host", required=True, help="ssh target, e.g. comma@192.168.1.50")
  args = parser.parse_args()
  try:
    check_device(args.host)
  except ValueError as e:
    sys.exit(f"Setup stopped before login: {e}")

  client_id = input("Tesla app Client ID: ").strip()
  client_secret = getpass.getpass("Tesla app Client Secret (not shown): ").strip()
  domain = input("App domain, e.g. yourname.github.io: ").strip()
  redirect_uri = input(f"Redirect URI [https://{domain}/callback]: ").strip() or f"https://{domain}/callback"

  try:
    partner = _send(token_request({"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                                   "scope": "openid vehicle_device_data vehicle_location", "audience": FLEET_API_URL}))
    _register_domain(partner["access_token"], domain)

    state = secrets.token_urlsafe(16)
    url = authorize_url(client_id, redirect_uri, state)
    print(f"\nLog in to Tesla in the browser. If none opens, open this address:\n{url}\n")
    webbrowser.open(url)
    code = code_from_redirect(input("Paste the address of the page you land on (a 404 page is fine): "), state)
    tokens = _send(token_request({"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
                                  "code": code, "audience": FLEET_API_URL, "redirect_uri": redirect_uri}))
    refresh_token = tokens["refresh_token"]

    vin = choose_vin(_send(api_request(tokens["access_token"], "/api/1/vehicles")).get("response") or [])
    _check(tokens["access_token"], vin)
  except urllib.error.HTTPError as e:
    sys.exit(f"Tesla answered HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
  except urllib.error.URLError as e:
    sys.exit(f"Could not reach Tesla: {e.reason}")
  except (KeyError, IndexError, ValueError) as e:
    sys.exit(f"Setup stopped: {e}")

  push_param(args.host, "KoreaTeslaClientId", client_id)
  push_param(args.host, "KoreaTeslaVin", vin)
  # Last, so the device never holds a token without the rest of the setup.
  push_param(args.host, "KoreaTeslaRefreshToken", refresh_token)
  print("Saved to the device. It reads the car's destination from the next drive on.")


if __name__ == "__main__":
  main()
