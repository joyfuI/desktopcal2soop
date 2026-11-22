"""Reconcile CalendarTask snapshots with linked SOOP calendar events."""

import json
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import asdict
from datetime import datetime

from calendar_source import (BROADCAST, COLLABORATION, OFF_AIR, Event, KST, PUBLISH_WINDOW_END,
                             TIME_PREFIX, build_events, read_rows)
from soop_client import SoopClient, SoopError, SoopEventNotFound, load_config


def changes(wanted, current):
    new, old = defaultdict(list), defaultdict(list)
    for event in wanted:
        new[event.source, event.day].append(event)
    for event_id, event in current.items():
        old[event.source, event.day].append((event_id, event))

    add, edit, remove = [], [], []
    for key in sorted(new.keys() | old.keys()):
        unmatched = new[key].copy()
        leftovers = []
        for event_id, event in old[key]:
            if event in unmatched:
                unmatched.remove(event)
            else:
                leftovers.append((event_id, event))
        remaining = []
        for event_id, previous in leftovers:
            replacement = next((event for event in unmatched if event.title == previous.title), None)
            if replacement is None:
                remaining.append((event_id, previous))
            else:
                unmatched.remove(replacement)
                edit.append((event_id, replacement))
        if len(remaining) == len(unmatched) == 1:
            edit.append((remaining[0][0], unmatched[0]))
        else:
            add.extend(unmatched)
            remove.extend(remaining)
    return add, edit, remove


def remote_event_id(row):
    value = str(row.get("idx")) if isinstance(row, dict) else ""
    if not (value.isascii() and value.isdecimal()):
        raise SoopError("SOOP 일정 목록에서 일정 ID를 확인할 수 없습니다.")
    return int(value)


def ids(rows):
    return {remote_event_id(row) for row in rows}


def event_key(event):
    return event.day, event.title, event.time, event.calendar_type


def published_events(rows):
    calendar_types = {str(value): value for value in (BROADCAST, COLLABORATION, OFF_AIR)}
    for row in rows:
        remote_id = remote_event_id(row)
        calendar_type = calendar_types.get(str(row.get("calendarType")))
        if calendar_type is not None:
            yield remote_id, (row.get("eventDate"), row.get("title"), str(row.get("eventTime", ""))[:5], calendar_type)


def store_event(db, user, event_id, event):
    db.execute("INSERT INTO synced(user, remote_id, source, day, title, time, calendar_type) VALUES (?, ?, ?, ?, ?, ?, ?) "
               "ON CONFLICT(user, remote_id) DO UPDATE SET source=excluded.source, day=excluded.day, "
               "title=excluded.title, time=excluded.time, calendar_type=excluded.calendar_type",
               (user, event_id, event.source, *event_key(event)))


def recover_create(db, client):
    pending = db.execute("SELECT event, before_ids, started FROM pending WHERE user=?", (client.user,)).fetchone()
    if not pending:
        return
    event, before, started = Event(**json.loads(pending[0])), set(json.loads(pending[1])), pending[2]
    matches = [remote_id for remote_id, key in published_events(client.list_events(event.day))
               if remote_id not in before and key == event_key(event)]
    if len(matches) > 1:
        raise SoopError("등록 결과가 여러 일정과 일치합니다. SOOP 캘린더를 확인하세요.")
    if not matches and time.time() - started < 60:
        raise SoopError("이전 등록 결과를 확인 중입니다. 1분 뒤 다시 시도하세요.")
    with db:
        if matches:
            store_event(db, client.user, matches[0], event)
        db.execute("DELETE FROM pending WHERE user=?", (client.user,))


def recover_links(db, client, candidates, linked_ids, stopped):
    days_by_month = {event.day[:7]: event.day for event in candidates}
    remote = {}
    for day in sorted(days_by_month.values()):
        if stopped():
            return None
        remote.update(published_events(client.list_events(day)))
    if stopped():
        return None

    available_ids = defaultdict(deque)
    for remote_id in sorted(remote.keys() - linked_ids):
        available_ids[remote[remote_id]].append(remote_id)

    unmatched = []
    with db:
        claim = db.execute("INSERT INTO recovered (user) VALUES (?) ON CONFLICT(user) DO NOTHING", (client.user,))
        if not claim.rowcount:
            # The add list may be stale if another run just saved the recovered links.
            return None
        for event in candidates:
            matches = available_ids.get(event_key(event))
            if matches:
                store_event(db, client.user, matches.popleft(), event)
            else:
                unmatched.append(event)
    return unmatched


def missing_links(client, current, stopped):
    by_month = defaultdict(dict)
    for remote_id, event in current.items():
        by_month[event.day[:7]][remote_id] = event.day

    missing = set()
    for linked in by_month.values():
        if stopped():
            return None
        present = ids(client.list_events(min(linked.values())))
        missing.update(linked.keys() - present)
    return missing


def can_register(day, today):
    return today.isoformat() <= day <= (today + PUBLISH_WINDOW_END).isoformat()


def synchronize(config_path, state_path, stopped):
    default_time = load_config(config_path)["sync"]["default_time"]
    if not TIME_PREFIX.fullmatch(default_time):
        raise ValueError("config.ini의 [sync] default_time에 24시간제 HH:mm 형식의 시간을 입력하세요.")
    today = datetime.now(KST).date()
    wanted, _ = build_events(*read_rows(), today, default_time)
    if stopped():
        return
    client = SoopClient(config_path)
    try:
        with closing(sqlite3.connect(state_path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS synced (
                    user TEXT, remote_id INTEGER, source TEXT, day TEXT, title TEXT, time TEXT,
                    calendar_type INTEGER NOT NULL,
                    PRIMARY KEY (user, remote_id)
                );
                CREATE TABLE IF NOT EXISTS pending (
                    user TEXT PRIMARY KEY, event TEXT, before_ids TEXT, started REAL
                );
                CREATE TABLE IF NOT EXISTS recovered (user TEXT PRIMARY KEY);
            """)
            recover_create(db, client)
            needs_recovery = db.execute("SELECT 1 FROM recovered WHERE user=?", (client.user,)).fetchone() is None
            linked = {row[0]: Event(*row[1:]) for row in db.execute(
                "SELECT remote_id, source, day, title, time, calendar_type FROM synced WHERE user=? ORDER BY remote_id",
                (client.user,))}
            today_iso = today.isoformat()
            current = {remote_id: event for remote_id, event in linked.items() if event.day >= today_iso}
            missing = missing_links(client, current, stopped)
            if missing is None or stopped():
                return
            if missing:
                with db:
                    db.executemany("DELETE FROM synced WHERE user=? AND remote_id=?",
                                   ((client.user, remote_id) for remote_id in missing))
                for remote_id in missing:
                    del current[remote_id]

            add, edit, remove = changes((event for event in wanted if event.day >= today_iso), current)
            deferred = {(event.source, event.day) for event in add if not can_register(event.day, today)}
            add = [event for event in add if (event.source, event.day) not in deferred]
            if needs_recovery:
                add = recover_links(db, client, add, linked.keys() - missing, stopped)
                if add is None:
                    return
            for remote_id, event in edit:
                if stopped():
                    return
                if event.day < datetime.now(KST).date().isoformat():
                    continue
                try:
                    client.update(remote_id, event)
                except SoopEventNotFound:
                    if remote_id in ids(client.list_events(event.day)):
                        raise
                    with db:
                        db.execute("DELETE FROM synced WHERE user=? AND remote_id=?", (client.user, remote_id))
                    add.append(event)
                else:
                    with db:
                        store_event(db, client.user, remote_id, event)
            for event in add:
                if stopped():
                    return
                if not can_register(event.day, datetime.now(KST).date()):
                    continue
                before = sorted(ids(client.list_events(event.day)))
                if stopped():
                    return
                if event.day < datetime.now(KST).date().isoformat():
                    continue
                with db:
                    db.execute("INSERT INTO pending VALUES (?, ?, ?, ?)",
                               (client.user, json.dumps(asdict(event)), json.dumps(before), time.time()))
                try:
                    remote_id = client.create(event)
                except SoopError as error:
                    if not error.ambiguous:
                        with db:
                            db.execute("DELETE FROM pending WHERE user=?", (client.user,))
                    raise
                with db:
                    store_event(db, client.user, remote_id, event)
                    db.execute("DELETE FROM pending WHERE user=?", (client.user,))
            for remote_id, event in remove:
                if stopped():
                    return
                if ((event.source, event.day) in deferred
                        or event.day < datetime.now(KST).date().isoformat()):
                    continue
                try:
                    client.delete(remote_id)
                except SoopError:
                    rows = client.list_events(event.day)
                    if remote_id in ids(rows):
                        raise
                with db:
                    db.execute("DELETE FROM synced WHERE user=? AND remote_id=?", (client.user, remote_id))
    finally:
        client.session.close()

