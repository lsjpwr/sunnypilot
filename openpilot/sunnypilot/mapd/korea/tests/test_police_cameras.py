"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import io
import pathlib
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock
from xml.sax.saxutils import escape

from openpilot.sunnypilot.mapd.korea import police_cameras
from openpilot.sunnypilot.mapd.korea.db import FACING_BOTH, FACING_FRONT, FACING_REAR

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

# The 고정식 sheet as the 2026-08-31 list lays it out -- a title row, an empty row, then the header, whose
# 설치장소 carries a trailing space -- with made-up cameras.
HEADER = ["연번", "장비번호", "기능", "관할서", "설치장소 ", "운영일자", "비고", "운영관리자", "연락처"]
FIXED = [
  ["무인단속장비 설치장소 안내(고정식)"],
  ["", "", ""],
  HEADER,
  [1, "T0001", "과속 및 신호", "가상서", "가상시 가상동 1-1 앞", 45000, "전면", "경기도남부경찰청", "000"],
  [2, "T0002", "과속", "가상서", "가상시 가상동 2-2 앞", 45100, "후면", "경기도남부경찰청", "000"],
  [3, "T0003", "과속 및 신호", "가상서", "가상시 가상동 3-3 앞", 45200, "양방향", "경기도남부경찰청", "000"],
  [4, "T0004", "구간단속", "가상서", "가상시 가상동 4-4 앞", 45300, "", "경기도남부경찰청", "000"],
  [5, "", "과속", "가상서", "가상시 가상동 5-5 앞", 45400, "후면", "경기도남부경찰청", "000"],
  ["", "", "", "", "", "", ""],  # the list runs on for ~60,000 rows like this
]

FILE_URL = "https://ggpolice.go.kr/include/main/files/ggpolice_cctv%28260831%29.xlsx"
PAGE = """
<!-- <a href="/include/main/files/ggpolice_cctv(291231).xlsx">a newer one, commented out</a> -->
<a href="/include/main/files/ggpolice_cctv.xls">old format</a>
<a href="/include/main/files/ggpolice_cctv(250228).xlsx">older</a>
<a href="/include/main/files/ggpolice_cctv(260831).xlsx">current</a>
"""


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
  """The parts of an xlsx read_rows reads and nothing else. Text goes through sharedStrings, ints stay numbers,
  and "" is a cell with no value, the way the real list pads its empty rows."""
  strings: list[str] = []

  def cell(ref: str, value) -> str:
    if value == "":
      return f'<c r="{ref}"/>'
    if isinstance(value, int):
      return f'<c r="{ref}"><v>{value}</v></c>'
    strings.append(value)
    return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'

  out = io.BytesIO()
  with zipfile.ZipFile(out, "w") as archive:
    for n, rows in enumerate(sheets.values(), 1):
      data = "".join(f'<row r="{r}">' + "".join(cell(f"{chr(ord('A') + c)}{r}", v) for c, v in enumerate(row)) + "</row>"
                     for r, row in enumerate(rows, 1))
      archive.writestr(f"xl/worksheets/sheet{n}.xml", f'<worksheet xmlns="{MAIN}"><sheetData>{data}</sheetData></worksheet>')
    archive.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>' +
                     "".join(f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(sheets, 1)) +
                     "</sheets></workbook>")
    archive.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PACKAGE_REL}">' +
                     "".join(f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml"/>' for n in range(1, len(sheets) + 1)) +
                     "</Relationships>")
    archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
  return out.getvalue()


def opener_for(files: dict[str, bytes]):
  """Stand in for urlopen: serves files by url, and any other url fails like an unreachable host."""
  def opener(url, timeout=None):
    if url not in files:
      raise urllib.error.URLError("unreachable")
    return io.BytesIO(files[url])
  return opener


class TestLatestFileUrl(unittest.TestCase):
  def test_the_newest_live_xlsx_wins(self):
    """The page keeps its older links inside HTML comments; an .xls is the format before 2026."""
    self.assertEqual(police_cameras.latest_file_url(PAGE), FILE_URL)

  def test_no_link_is_none(self):
    self.assertIsNone(police_cameras.latest_file_url('<!-- <a href="/include/main/files/ggpolice_cctv(260831).xlsx"> -->'))


class TestReadRows(unittest.TestCase):
  def test_the_rows_after_the_header_come_back_keyed_by_it(self):
    rows = police_cameras.read_rows(make_xlsx({"고정식": FIXED}))
    self.assertEqual([row["장비번호"] for row in rows], ["T0001", "T0002", "T0003", "T0004", ""])
    self.assertEqual(rows[0]["설치장소"], "가상시 가상동 1-1 앞")  # the header's trailing space is gone
    self.assertEqual(rows[0]["운영일자"], "45000")  # a number cell, an Excel date serial
    self.assertEqual(rows[1]["비고"], "후면")

  def test_the_sheet_is_found_by_name(self):
    rows = police_cameras.read_rows(make_xlsx({"요약": [["합계", 1]], "고정식": FIXED}))
    self.assertEqual(len(rows), 5)

  def test_a_list_without_the_sheet_raises(self):
    with self.assertRaises(ValueError):
      police_cameras.read_rows(make_xlsx({"요약": FIXED}))

  def test_a_list_without_the_header_raises(self):
    with self.assertRaises(ValueError):
      police_cameras.read_rows(make_xlsx({"고정식": [["연번", "번호", "비고"], [1, "T0001", "전면"]]}))

  def test_a_member_too_big_unpacked_raises(self):
    """A zip states its own sizes, so the cap holds on the bytes actually read."""
    data = make_xlsx({"고정식": FIXED})
    with mock.patch.object(police_cameras, "MAX_MEMBER_BYTES", 200), self.assertRaises(ValueError):
      police_cameras.read_rows(data)


class TestFacingByNumber(unittest.TestCase):
  def test_only_a_named_facing_with_a_number_counts(self):
    self.assertEqual(police_cameras.facing_by_number(police_cameras.read_rows(make_xlsx({"고정식": FIXED}))),
                     {"T0001": FACING_FRONT, "T0002": FACING_REAR, "T0003": FACING_BOTH})


class TestFetch(unittest.TestCase):
  def test_the_page_leads_to_the_list(self):
    opener = opener_for({police_cameras.PAGE_URL: PAGE.encode(), FILE_URL: make_xlsx({"고정식": FIXED})})
    self.assertEqual(police_cameras.fetch(opener), {"T0001": FACING_FRONT, "T0002": FACING_REAR, "T0003": FACING_BOTH})

  def test_a_page_without_a_list_raises(self):
    with self.assertRaises(ValueError):
      police_cameras.fetch(opener_for({police_cameras.PAGE_URL: b"<html></html>"}))

  def test_a_download_too_big_raises(self):
    opener = opener_for({police_cameras.PAGE_URL: PAGE.encode(), FILE_URL: make_xlsx({"고정식": FIXED})})
    with mock.patch.object(police_cameras, "MAX_DOWNLOAD_BYTES", 1000), self.assertRaises(ValueError):
      police_cameras.fetch(opener)

  def test_a_list_naming_no_facing_raises(self):
    """Say the 비고 column switched to codes: an empty answer must not overwrite the cache."""
    unnamed = [[*row[:6], "", *row[7:]] if i > 2 else row for i, row in enumerate(FIXED)]
    opener = opener_for({police_cameras.PAGE_URL: PAGE.encode(), FILE_URL: make_xlsx({"고정식": unnamed})})
    with self.assertRaises(ValueError):
      police_cameras.fetch(opener)


class TestCache(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.path = str(pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "korea_police_facing.json")

  def test_a_saved_cache_reads_back(self):
    police_cameras.save_cache(self.path, {"T0001": FACING_FRONT, "T0002": FACING_REAR})
    self.assertEqual(police_cameras.load_cache(self.path), {"T0001": FACING_FRONT, "T0002": FACING_REAR})

  def test_a_missing_or_broken_cache_is_empty(self):
    self.assertEqual(police_cameras.load_cache(self.path), {})
    for broken in ("{not json", "[1, 2]", '{"T0001": "front"}'):
      with self.subTest(broken=broken):
        pathlib.Path(self.path).write_text(broken, encoding="utf-8")
        self.assertEqual(police_cameras.load_cache(self.path), {})


if __name__ == "__main__":
  unittest.main()
