"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

police_cameras: which way the 경기남부 police's fixed cameras face, from the list on their website.

The public standard data camera_refresh builds from says nothing about facing. 경기도남부경찰청
publishes its fixed cameras as an xlsx whose 비고 column reads 전면, 후면 or 양방향 (4801, 433 and
28 of 5262 on 2026-08-31), keyed by 장비번호 -- the API's mnlssRegltCameraManageNo. Its copy on
data.go.kr (15159700) drops 비고.

stdlib only, like camera_refresh: this runs on the device. The file is a stranger's input, so the
download and every member read from it are capped.
"""
import io
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from openpilot.sunnypilot.mapd.korea.db import FACING_BOTH, FACING_FRONT, FACING_REAR

PAGE_URL = "https://ggpolice.go.kr/main/html.do?menu=HB33B"
# The file name carries the list's date, YYMMDD
FILE_RE = re.compile(r'href="(/include/main/files/ggpolice_cctv\((\d{6})\)\.xlsx)"')
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
SHEET = "고정식"
NUMBER = "장비번호"
NOTE = "비고"
FACING_BY_NOTE = {"전면": FACING_FRONT, "후면": FACING_REAR, "양방향": FACING_BOTH}
# institutionNm of the API rows the list covers, in both spellings the 2026-08 snapshot uses (5974 and 328
# rows). A number is only its agency's own label -- 6 city rows carry one of the list's -- so only these rows
# are looked up by it (build_db.load_cameras_api).
POLICE_INSTITUTIONS = ("경기도남부경찰청", "경기남부경찰청")

HTTP_TIMEOUT_S = 30.
# The 2026-08-31 list is 0.9 MB, and its 고정식 sheet 7.3 MB unpacked: 65,094 rows, most of them empty.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def latest_file_url(html: str) -> str | None:
  """The newest list the page links to. It keeps the older ones inside HTML comments."""
  links = FILE_RE.findall(COMMENT_RE.sub("", html))
  return urllib.parse.urljoin(PAGE_URL, urllib.parse.quote(max(links, key=lambda link: link[1])[0])) if links else None


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
  # read past the cap rather than trust the size the zip states for itself
  with archive.open(name) as member:
    data = member.read(MAX_MEMBER_BYTES + 1)
  if len(data) > MAX_MEMBER_BYTES:
    raise ValueError(f"police list: {name} is over {MAX_MEMBER_BYTES} bytes unpacked")
  return data


def _column(ref: str) -> int:
  """'B12' -> 1"""
  index = 0
  for letter in re.match(r"[A-Z]*", ref).group():
    index = index * 26 + ord(letter) - ord("A") + 1
  return index - 1


def read_rows(data: bytes) -> list[dict[str, str]]:
  """The 고정식 sheet, one dict per non-empty row below the header row (the first naming both 장비번호 and
  비고), keyed by the header's stripped names. Raises ValueError without that sheet or header, or over a cap."""
  with zipfile.ZipFile(io.BytesIO(data)) as archive:
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
      shared = ["".join(t.text or "" for t in si.iter(_MAIN + "t"))
                for si in ET.fromstring(_member(archive, "xl/sharedStrings.xml")).iter(_MAIN + "si")]
    rel_id = next((sheet.get(_REL_ID) for sheet in ET.fromstring(_member(archive, "xl/workbook.xml")).iter(_MAIN + "sheet")
                   if sheet.get("name") == SHEET), None)
    target = next((rel.get("Target") for rel in ET.fromstring(_member(archive, "xl/_rels/workbook.xml.rels"))
                   if rel_id is not None and rel.get("Id") == rel_id), None)
    if target is None:
      raise ValueError(f"police list: no {SHEET} sheet")
    sheet_xml = _member(archive, target.lstrip("/") if target.startswith("/") else "xl/" + target)

  columns: dict[int, str] | None = None
  rows = []
  # iterparse and clear each row: the sheet holds ~60,000 empty rows
  for _, row in ET.iterparse(io.BytesIO(sheet_xml)):
    if row.tag != _MAIN + "row":
      continue
    values = {}
    for cell in row.iter(_MAIN + "c"):
      value = cell.find(_MAIN + "v")
      if cell.get("t") == "inlineStr":
        text = "".join(t.text or "" for t in cell.iter(_MAIN + "t"))
      elif value is None or value.text is None:
        text = ""
      else:
        text = shared[int(value.text)] if cell.get("t") == "s" else value.text
      values[_column(cell.get("r", ""))] = text.strip()
    row.clear()
    if columns is None:
      if {NUMBER, NOTE} <= set(values.values()):
        columns = {index: name for index, name in values.items() if name}
    elif any(values.values()):
      rows.append({name: values.get(index, "") for index, name in columns.items()})

  if columns is None:
    raise ValueError(f"police list: no header row naming {NUMBER} and {NOTE}")
  return rows


def facing_by_number(rows: list[dict[str, str]]) -> dict[str, int]:
  """장비번호 -> db.FACING_*, for the rows whose 비고 names one."""
  return {row[NUMBER]: FACING_BY_NOTE[row[NOTE]] for row in rows if row.get(NUMBER) and row.get(NOTE) in FACING_BY_NOTE}


def _download(url: str, opener) -> bytes:
  with opener(url, timeout=HTTP_TIMEOUT_S) as response:
    data = response.read(MAX_DOWNLOAD_BYTES + 1)
  if len(data) > MAX_DOWNLOAD_BYTES:
    raise ValueError(f"police list: {url} is over {MAX_DOWNLOAD_BYTES} bytes")
  return data


def fetch(opener=urllib.request.urlopen) -> dict[str, int]:
  """facing_by_number of the newest list on the website. Raises on any failure, an answer naming no facing
  included: the caller falls back to its cache."""
  url = latest_file_url(_download(PAGE_URL, opener).decode("utf-8", "replace"))
  if url is None:
    raise ValueError("police list: the page links to no list")
  facing = facing_by_number(read_rows(_download(url, opener)))
  if not facing:
    raise ValueError(f"police list: no row's {NOTE} names a facing")
  return facing


def load_cache(path: str) -> dict[str, int]:
  """The facings save_cache wrote last. Empty when there is no such file or it is unreadable."""
  try:
    with open(path, encoding="utf-8") as f:
      return {str(number): int(facing) for number, facing in json.load(f).items()}
  except (OSError, ValueError, AttributeError, TypeError):
    return {}


def save_cache(path: str, facing: dict[str, int]) -> None:
  """Swapped in whole, like build_db.write_db: a crash midway leaves the old cache."""
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(facing, f)
  os.replace(tmp, path)
