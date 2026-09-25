"""OfferDesk DB helper — SQLite locally, Postgres when DATABASE_URL is set.

Usage:
    import db as dbmod
    conn = dbmod.connect()          # Conn wrapper
    conn.execute("SELECT * FROM deals WHERE user_id=?", (uid,)).fetchall()
    new_id = conn.insert_id("INSERT INTO deals (user_id, ...) VALUES (?, ...)", (...))
    conn.commit(); conn.close()

Write ALL SQL with SQLite-style "?" placeholders; the wrapper translates
them to "%s" for psycopg2 automatically. Rows are dict-like on both backends
(sqlite3.Row locally, RealDictRow on Postgres), so row["col"] and dict(row)
both work.
"""
import os
import sqlite3

try:
    import psycopg2
    import psycopg2.extras
    HAVE_PG = True
except ImportError:  # optional — only needed when DATABASE_URL is set
    HAVE_PG = False

BASE = os.path.dirname(os.path.abspath(__file__))


def is_postgres():
    return bool(os.environ.get("DATABASE_URL"))


def sqlite_path():
    return os.environ.get("DB_PATH") or os.path.join(BASE, "offerdesk.db")


class Conn:
    def __init__(self, raw, pg):
        self._raw = raw
        self._pg = pg

    def _sql(self, sql):
        # psycopg2 uses %s; sqlite uses ?. No literal "?" appears in our SQL.
        return sql.replace("?", "%s") if self._pg else sql

    def execute(self, sql, params=()):
        if self._pg:
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self._raw.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def insert_id(self, sql, params=()):
        """INSERT a row and return its new id.

        Postgres has no lastrowid, so we use RETURNING id there.
        """
        if self._pg:
            cur = self.execute(sql + " RETURNING id", params)
            return cur.fetchone()["id"]
        cur = self.execute(sql, params)
        return cur.lastrowid

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def connect():
    if is_postgres():
        if not HAVE_PG:
            raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed")
        return Conn(psycopg2.connect(os.environ["DATABASE_URL"]), True)
    raw = sqlite3.connect(sqlite_path())
    raw.row_factory = sqlite3.Row
    return Conn(raw, False)
