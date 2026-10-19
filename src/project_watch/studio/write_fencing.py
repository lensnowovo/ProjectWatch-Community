"""Fence legacy Studio mutations with the dispatch lease, in their write transaction.

This is a trusted internal SQL connection, not a sandbox for arbitrary agent SQL.
Reads remain ordinary reads. Connections retain their creation-time guard so a
connection escaping its worker scope cannot become an unfenced writer.
"""

from __future__ import annotations

import re
import sqlite3


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
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.before_statement(sql)
        return super().executemany(sql, parameters)

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
        return super().commit()

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
