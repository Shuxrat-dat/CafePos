import glob
import os
import sqlite3
import subprocess
import tempfile
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

MODULE = "reports.management.commands.backup_db"
PASSWORD = "secret-test-pass"


class BackupDbTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.folder = os.path.join(self.tmp, "backups")

    def _settings(self, engine, name):
        return SimpleNamespace(
            BASE_DIR=self.tmp,
            DATABASES={
                "default": {
                    "ENGINE": engine,
                    "NAME": name,
                    "USER": "cafepos",
                    "PASSWORD": PASSWORD,
                    "HOST": "localhost",
                    "PORT": "5432",
                }
            },
        )

    def _pg_settings(self):
        return self._settings("django.db.backends.postgresql", "cafepos")

    def _files(self, pattern):
        return sorted(glob.glob(os.path.join(self.folder, pattern)))

    def test_sqlite_copy_contains_data(self):
        source = os.path.join(self.tmp, "source.sqlite3")
        conn = sqlite3.connect(source)
        conn.execute("create table t (x integer)")
        conn.execute("insert into t values (42)")
        conn.commit()
        conn.close()
        with mock.patch(MODULE + ".settings", self._settings("django.db.backends.sqlite3", source)):
            call_command("backup_db", stdout=StringIO())
        copies = self._files("db-*.sqlite3")
        self.assertEqual(len(copies), 1)
        copy = sqlite3.connect(copies[0])
        try:
            self.assertEqual(copy.execute("select x from t").fetchone()[0], 42)
        finally:
            copy.close()

    def test_postgres_calls_pg_dump_without_password_in_command(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        out = StringIO()
        with mock.patch(MODULE + ".settings", self._pg_settings()), \
                mock.patch(MODULE + "._find_pg_dump", return_value="pg_dump"), \
                mock.patch(MODULE + ".subprocess.run", return_value=done) as run:
            call_command("backup_db", stdout=out)
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(command[0], "pg_dump")
        self.assertIn("-F", command)
        self.assertEqual(env["PGPASSWORD"], PASSWORD)
        self.assertNotIn(PASSWORD, " ".join(command))
        self.assertNotIn(PASSWORD, out.getvalue())

    def test_postgres_failure_raises_and_leaves_no_file(self):
        def failing_run(command, **kwargs):
            open(command[-1], "wb").close()  # pg_dump успел создать пустой файл
            return subprocess.CompletedProcess(command, 1, b"", b"boom")

        with mock.patch(MODULE + ".settings", self._pg_settings()), \
                mock.patch(MODULE + "._find_pg_dump", return_value="pg_dump"), \
                mock.patch(MODULE + ".subprocess.run", side_effect=failing_run):
            with self.assertRaises(CommandError) as ctx:
                call_command("backup_db", stdout=StringIO())
        self.assertIn("boom", str(ctx.exception))
        self.assertEqual(self._files("pg-*.dump"), [])

    def test_old_copies_are_pruned_to_30(self):
        os.makedirs(self.folder)
        for i in range(32):
            open(os.path.join(self.folder, f"pg-2026-01-01_00-00-{i:02d}.dump"), "wb").close()
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch(MODULE + ".settings", self._pg_settings()), \
                mock.patch(MODULE + "._find_pg_dump", return_value="pg_dump"), \
                mock.patch(MODULE + ".subprocess.run", return_value=done):
            call_command("backup_db", stdout=StringIO())
        copies = self._files("pg-*.dump")
        self.assertEqual(len(copies), 30)
        self.assertFalse(any(path.endswith("00-00-00.dump") for path in copies))
        self.assertTrue(any(path.endswith("00-00-31.dump") for path in copies))

    def test_unsupported_database_is_rejected(self):
        with mock.patch(MODULE + ".settings", self._settings("django.db.backends.mysql", "x")):
            with self.assertRaises(CommandError):
                call_command("backup_db", stdout=StringIO())