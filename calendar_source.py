"""Read CalendarTask using the SQLite codec shipped in its Windows install."""

import ctypes as c
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from dateutil.rrule import rrulestr

KST = timezone(timedelta(hours=9), "KST")
PUBLISH_WINDOW_END = timedelta(days=89)  # The 90-day window includes today.
DEFAULT_EVENT_TIME = "12:00"
BROADCAST, COLLABORATION, OFF_AIR = 1, 2, 3
TIME_PREFIX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?!\d)", re.ASCII)
CELL_DATE = re.compile(r"dkcal_mdays_([0-9]{8})\Z")


@dataclass(frozen=True)
class Event:
    source: str
    day: str
    title: str
    time: str
    calendar_type: int = BROADCAST


class Blob(c.Structure):
    _fields_ = [("size", c.c_uint32), ("data", c.c_void_p)]


def dpapi(value, *, protect=False):
    crypt32 = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = (c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p,
                         c.c_void_p, c.c_uint32, c.POINTER(Blob))
    function.restype = c.c_int
    kernel.LocalFree.argtypes = (c.c_void_p,)
    kernel.LocalFree.restype = c.c_void_p
    buffer = c.create_string_buffer(value)
    source, result = Blob(len(value), c.addressof(buffer)), Blob()
    try:
        if not function(c.byref(source), None, None, None, None, 1, c.byref(result)):
            raise c.WinError(c.get_last_error())
        return c.string_at(result.data, result.size)
    finally:
        c.memset(buffer, 0, len(buffer))
        if result.data:
            c.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def read_rows():
    if os.name != "nt" or c.sizeof(c.c_void_p) != 4:
        raise RuntimeError("CalendarTask DB에는 32비트 Windows Python이 필요합니다.")
    folder = Path(os.environ["APPDATA"]) / "CalendarTask"
    key = dpapi((folder / "Db" / "calendar.key").read_bytes())
    if len(key) != 36:
        raise RuntimeError("CalendarTask DB 키 형식을 확인할 수 없습니다.")
    checksum = 0x4B455943
    for byte in key[4:]:
        checksum = ((checksum * 33) ^ byte) & 0xFFFFFFFF
    if checksum != int.from_bytes(key[:4], "little"):
        raise RuntimeError("CalendarTask DB 키 검증에 실패했습니다.")

    with os.add_dll_directory(folder):
        dll = c.CDLL(str(folder / "dkbase.dll"))
    ptr, string, integer = c.c_void_p, c.c_char_p, c.c_int
    for name, args, result in (
        ("open_v2", (string, c.POINTER(ptr), integer, ptr), integer),
        ("key", (ptr, string, integer), integer),
        ("busy_timeout", (ptr, integer), integer),
        ("prepare_v2", (ptr, string, integer, c.POINTER(ptr), ptr), integer),
        ("step", (ptr,), integer),
        ("column_count", (ptr,), integer),
        ("column_text", (ptr, integer), string),
        ("finalize", (ptr,), integer),
        ("close", (ptr,), integer),
    ):
        function = getattr(dll, f"sqlite3_{name}")
        function.argtypes, function.restype = args, result

    db = ptr()

    def check(code, expected=0):
        if code != expected:
            raise RuntimeError(f"CalendarTask DB 읽기 실패 (SQLite {code}).")

    def query(sql):
        stmt = ptr()
        try:
            check(dll.sqlite3_prepare_v2(db, sql.encode(), -1, c.byref(stmt), None))
            rows = []
            width = dll.sqlite3_column_count(stmt)
            while (code := dll.sqlite3_step(stmt)) == 100:
                rows.append(tuple((dll.sqlite3_column_text(stmt, i) or b"").decode("utf-8")
                                  for i in range(width)))
            check(code, 101)
            return rows
        finally:
            if stmt:
                dll.sqlite3_finalize(stmt)

    try:
        path = (folder / "Db" / "calendar.db").as_uri().encode()
        check(dll.sqlite3_open_v2(path, c.byref(db), 65, None))  # READONLY | URI
        check(dll.sqlite3_key(db, key[4:], 32))
        check(dll.sqlite3_busy_timeout(db, 2000))
        query("BEGIN")
        cells = query("SELECT it_id, it_unique_id, it_content FROM item_table ORDER BY it_id")
        events = query("SELECT ev_id, ev_unique_id, ev_date, ev_start_date, ev_end_date, "
                       "ev_content, ev_recurrence FROM event_table ORDER BY ev_id")
        query("COMMIT")
        return cells, events
    finally:
        if db:
            dll.sqlite3_close(db)


def parse_title(raw, default_time=DEFAULT_EVENT_TIME):
    title = raw.strip()
    if not title or raw.startswith("!"):
        return None
    match = TIME_PREFIX.match(raw)
    event_time = match[0] if match else default_time
    if match:
        title = raw[match.end():].strip() or event_time
    calendar_type = BROADCAST
    if "휴방" in title:
        calendar_type = OFF_AIR
    elif "합방" in title:
        calendar_type = COLLABORATION
    # SOOP uses a 15-unit HTML maxlength for calendar titles.
    return title.encode("utf-16le")[:30].decode("utf-16le", errors="ignore"), event_time, calendar_type


def date_part(value):
    return date.fromisoformat(value.strip().split("T", 1)[0].split(" ", 1)[0])


def build_events(cells, rows, today, default_time=DEFAULT_EVENT_TIME):
    result = []
    rolling_sources = set()
    for row_id, uid, text in cells:
        parsed = [entry for line in text.splitlines() if (entry := parse_title(line, default_time))]
        if not parsed:
            continue
        match = CELL_DATE.fullmatch(uid)
        if not match:
            raise ValueError(f"날짜 칸 ID 형식을 확인할 수 없습니다 (행 {row_id}).")
        day = datetime.strptime(match[1], "%Y%m%d").date().isoformat()
        result.extend(Event(f"cell:{row_id}", day, *entry) for entry in parsed)

    for row_id, _uid, event_date, start_date, end_date, raw, rule in rows:
        entry = parse_title(raw, default_time)
        if entry is None:
            continue
        source = f"event:{row_id}"
        start = date_part(start_date or event_date)
        if not rule.strip():
            result.append(Event(source, start.isoformat(), *entry))
            continue

        rule = rule.strip()
        # shortcut: expand RFC 5545 rules, add a decoder when a different CalendarTask format is observed.
        if not (rule.upper().startswith(("FREQ=", "RRULE:", "DTSTART"))
                and "FREQ=" in rule.upper()):
            raise ValueError(f"지원하지 않는 CalendarTask 반복 규칙입니다 (일정 {row_id}).")
        finite = bool(re.search(r"(?:^|[;:])(?:UNTIL|COUNT)=", rule, re.I))
        limit = date_part(end_date) if end_date else None
        if limit is not None and limit < start:
            limit = None
        rolling = limit is None and not finite
        if rolling:
            rolling_sources.add(source)
        zone = KST if re.search(r"UNTIL=\d{8}T\d{6}Z", rule, re.I) else None
        dates = rrulestr(rule, dtstart=datetime.combine(start, time.min, zone))
        if rolling or limit:
            lower = max(today, start) if rolling else start
            upper = today + PUBLISH_WINDOW_END if rolling else limit
            occurrences = dates.between(datetime.combine(lower, time.min, zone),
                                        datetime.combine(upper, time.max, zone), inc=True) if lower <= upper else []
        else:
            occurrences = dates
        result.extend(Event(source, occurrence.date().isoformat(), *entry) for occurrence in occurrences)
    return result, rolling_sources
