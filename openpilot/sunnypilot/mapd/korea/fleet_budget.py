"""Durable Fleet reservations. A failed request still consumes its reservation."""
import datetime
import json
import os
from pathlib import Path
import tempfile

MONTHLY_CAP = 9000


class FleetBudget:
  def __init__(self, path, today=datetime.date.today):
    self.path = Path(path)
    self.today = today

  def reserve(self, units: int) -> int:
    """0: reserved, 3: monthly cap, 4: storage unsafe. No cached accounting."""
    import fcntl

    if type(units) is not int or units < 0:
      raise ValueError("invalid Fleet reservation")
    lock_path = self.path.with_suffix(".lock")
    try:
      try:
        lock = lock_path.open("xb")
        new = True
      except FileExistsError:
        lock = lock_path.open("rb")
        new = False
      with lock:
        # A separate, permanent inode keeps replacement and overlapping sources serialized.
        fcntl.flock(lock, fcntl.LOCK_EX)
        month = self.today().isoformat()[:7]
        try:
          record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
          if not new:
            raise ValueError("Fleet ledger disappeared") from None
          record = {"month": month, "units": 0}
        if (not isinstance(record, dict) or not isinstance(record.get("month"), str)
            or type(record.get("units")) is not int or record["units"] < 0):
          raise ValueError("invalid Fleet ledger")
        recorded_month = datetime.date.fromisoformat(record["month"] + "-01").isoformat()[:7]
        if recorded_month != record["month"] or recorded_month > month:
          raise ValueError("invalid Fleet month")
        spent = record["units"] if recorded_month == month else 0
        if spent + units > MONTHLY_CAP or units == 0 and spent >= MONTHLY_CAP:
          return 3
        if units or new or recorded_month != month:
          self._write({"month": month, "units": spent + units})
        return 0
    except (OSError, ValueError, TypeError):
      return 4

  def _write(self, record: dict) -> None:
    temporary = None
    try:
      with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".fleet-", delete=False) as stream:
        temporary = stream.name
        data = json.dumps(record).encode()
        if stream.write(data) != len(data):
          raise OSError("short Fleet ledger write")
        stream.flush()
        os.fsync(stream.fileno())
      os.replace(temporary, self.path)
      directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
      try:
        os.fsync(directory)
      finally:
        os.close(directory)
    finally:
      if temporary is not None:
        try:
          os.unlink(temporary)
        except FileNotFoundError:
          pass
