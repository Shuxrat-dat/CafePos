"""Хранилище бота: заведения, ссылки подключения, чаты получателей. Один файл SQLite."""
import hashlib
import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
create table if not exists venues(
  id integer primary key autoincrement,
  key_hash text unique not null,
  name text not null,
  lang text not null default 'uz',
  snapshot text,
  snapshot_at real,
  morning_sent text not null default '',
  shopping_sent text not null default '',
  created_at real not null
);
create table if not exists links(
  code text primary key,
  venue_id integer not null,
  expires_at real not null,
  used integer not null default 0
);
create table if not exists chats(
  chat_id text not null,
  venue_id integer not null,
  title text not null default '',
  lang text not null default 'uz',
  off text not null default '[]',
  added_at real not null,
  primary key(chat_id, venue_id)
);
create table if not exists meta(k text primary key, v text);
"""

LINK_TTL = 48 * 3600


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _one(self, sql, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _run(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # ---------- заведения ----------

    def create_venue(self, name, lang, now=None):
        key = secrets.token_urlsafe(32)
        cur = self._run(
            "insert into venues(key_hash, name, lang, created_at) values (?,?,?,?)",
            (hash_key(key), name[:100], lang, now or time.time()),
        )
        return cur.lastrowid, key

    def venue_by_key(self, key):
        if not key:
            return None
        return self._one("select * from venues where key_hash=?", (hash_key(key),))

    def venue(self, venue_id):
        return self._one("select * from venues where id=?", (venue_id,))

    def all_venues(self):
        return self._all("select * from venues")

    def rename_venue(self, venue_id, name, lang):
        self._run("update venues set name=?, lang=? where id=?", (name[:100], lang, venue_id))

    def set_snapshot(self, venue_id, data, now=None):
        self._run("update venues set snapshot=?, snapshot_at=? where id=?",
                  (json.dumps(data, ensure_ascii=False), now or time.time(), venue_id))

    def snapshot(self, venue):
        """(данные, время получения) или (None, None)."""
        if not venue.get("snapshot"):
            return None, None
        try:
            return json.loads(venue["snapshot"]), venue["snapshot_at"]
        except ValueError:
            return None, None

    def mark_sent(self, venue_id, field, day):
        if field not in ("morning_sent", "shopping_sent"):
            raise ValueError(field)
        self._run(f"update venues set {field}=? where id=?", (day, venue_id))

    # ---------- ссылки подключения ----------

    def new_link(self, venue_id, now=None, ttl=LINK_TTL):
        code = secrets.token_urlsafe(9)
        self._run("insert into links(code, venue_id, expires_at) values (?,?,?)",
                  (code, venue_id, (now or time.time()) + ttl))
        return code

    def active_link(self, venue_id, now=None):
        row = self._one(
            "select code from links where venue_id=? and used=0 and expires_at>? order by expires_at desc limit 1",
            (venue_id, now or time.time()),
        )
        return row["code"] if row else None

    def consume_link(self, code, now=None):
        """Одноразовая ссылка: возвращает id заведения и сразу гасит ссылку."""
        with self.lock:
            row = self.db.execute(
                "select venue_id from links where code=? and used=0 and expires_at>?", (code, now or time.time())
            ).fetchone()
            if not row:
                return None
            self.db.execute("update links set used=1 where code=?", (code,))
            self.db.commit()
            return row["venue_id"]

    # ---------- чаты ----------

    def add_chat(self, chat_id, venue_id, title, lang, off, now=None):
        chat_id = str(chat_id)
        with self.lock:
            existing = self.db.execute(
                "select 1 from chats where chat_id=? and venue_id=?", (chat_id, venue_id)).fetchone()
            if existing:
                self.db.execute("update chats set title=? where chat_id=? and venue_id=?", (title, chat_id, venue_id))
            else:
                self.db.execute(
                    "insert into chats(chat_id, venue_id, title, lang, off, added_at) values (?,?,?,?,?,?)",
                    (chat_id, venue_id, title, lang, json.dumps(sorted(off)), now or time.time()))
            self.db.commit()
        return not existing

    def chats_of_venue(self, venue_id):
        rows = self._all("select * from chats where venue_id=? order by added_at", (venue_id,))
        for r in rows:
            r["off"] = set(json.loads(r["off"]))
        return rows

    def venues_of_chat(self, chat_id):
        return self._all(
            "select v.* from venues v join chats c on c.venue_id=v.id where c.chat_id=? order by c.added_at",
            (str(chat_id),))

    def chat(self, chat_id):
        """Настройки чата (язык и отключённые типы одинаковы для всех его заведений)."""
        row = self._one("select * from chats where chat_id=? order by added_at limit 1", (str(chat_id),))
        if row:
            row["off"] = set(json.loads(row["off"]))
        return row

    def set_lang(self, chat_id, lang):
        self._run("update chats set lang=? where chat_id=?", (lang, str(chat_id)))

    def set_off(self, chat_id, off):
        self._run("update chats set off=? where chat_id=?", (json.dumps(sorted(off)), str(chat_id)))

    def remove_chat(self, chat_id, venue_id=None):
        if venue_id is None:
            self._run("delete from chats where chat_id=?", (str(chat_id),))
        else:
            self._run("delete from chats where chat_id=? and venue_id=?", (str(chat_id), venue_id))

    def remove_all_chats(self, venue_id):
        self._run("delete from chats where venue_id=?", (venue_id,))
        self._run("update links set used=1 where venue_id=?", (venue_id,))

    # ---------- служебное ----------

    def get_meta(self, key, default=None):
        row = self._one("select v from meta where k=?", (key,))
        return row["v"] if row else default

    def set_meta(self, key, value):
        self._run("insert into meta(k,v) values (?,?) on conflict(k) do update set v=excluded.v", (key, str(value)))
