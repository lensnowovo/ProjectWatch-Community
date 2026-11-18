"""Fence legacy Studio mutations with the dispatch lease, in their write transaction.

This is a trusted internal SQL connection, not a sandbox for arbitrary agent SQL.
Reads remain ordinary reads. Connections retain their creation-time guard so a
connection escaping its worker scope cannot become an unfenced writer.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import traceback

_log = logging.getLogger(__name__)
# Other connections wait 10 s for the write lock, then fail their statement. A transaction held
# this long is logged with where it began, so the holder behind a "database is locked" is known.
SLOW_WRITE_SECONDS = 5.0


def _operation(sql):
    # Internal SQL may start with comments; WITH is conservatively write-fenced.
    sql = re.sub(r"\A(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", "", sql, flags=re.S)
    return sql.split(None, 1)[0].upper().rstrip(";") if sql else ""


class FencedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection.before_statement(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection.before_statement(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql):
        if self.connection.guard is None:
            return super().executescript(sql)
        self.connection.run_script(self, sql)
        return self


class FencedConnection(sqlite3.Connection):
    guard = None
    keep_open = False
    _write_began = None

    def _began(self):
        if self._write_began is None and self.in_transaction:
            self._write_began = (time.monotonic(), traceback.extract_stack(limit=10)[:-2])

    def _ended(self, closing=False):
        if self._write_began is None or (self.in_transaction and not closing):
            return
        started, stack = self._write_began
        self._write_began = None
        held = time.monotonic() - started
        if held >= SLOW_WRITE_SECONDS:
            frames = [f for f in stack if 'project_watch' in f.filename and 'write_fencing' not in f.filename] or stack[-3:]
            where = ' <- '.join(f'{f.filename.rsplit("project_watch", 1)[-1]}:{f.lineno} {f.name}' for f in reversed(frames))
            _log.warning('write transaction held %.1f s, opened at %s', held, where)

    def rollback(self):
        super().rollback()
        self._ended()

    def close(self):
        if not self.keep_open:
            self._ended(closing=True)  # closing discards an open transaction
        # A read session's shared connection outlives each borrower's closing();
        # what the borrower left uncommitted is discarded, as a real close would.
        if not self.keep_open:
            super().close()
        elif self.in_transaction:
            self.rollback()

    def before_statement(self, sql):
        if self.guard is None:
            return
        operation = _operation(sql)
        if operation in {"", "SELECT", "EXPLAIN", "PRAGMA", "BEGIN", "SAVEPOINT", "ROLLBACK"}:
            return
        if not self.in_transaction:
            super().execute("BEGIN IMMEDIATE")
        try:
            self.guard(self)
        except BaseException:
            self.rollback()
            raise

    def execute(self, sql, parameters=()):
        self.before_statement(sql)
        try:
            return super().execute(sql, parameters)
        finally:
            self._began()
            self._ended()

    def executemany(self, sql, parameters):
        self.before_statement(sql)
        try:
            return super().executemany(sql, parameters)
        finally:
            self._began()
            self._ended()

    def cursor(self, factory=FencedCursor):
        return super().cursor(factory)

    def run_script(self, cursor, script):
        # sqlite3.executescript implicitly commits. Execute complete statements
        # individually instead, including triggers and semicolons in literals.
        statement = ""
        for char in script:
            statement += char
            if char == ";" and sqlite3.complete_statement(statement):
                cursor.execute(statement)
                statement = ""
        if statement.strip():
            cursor.execute(statement)

    def executescript(self, sql):
        if self.guard is None:
            return super().executescript(sql)
        cursor = self.cursor()
        self.run_script(cursor, sql)
        return cursor

    def commit(self):
        if self.guard is not None and self.in_transaction:
            try:
                self.guard(self)
            except BaseException:
                self.rollback()
                raise
        result = super().commit()
        self._ended()
        return result

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.rollback()
        else:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        return False
