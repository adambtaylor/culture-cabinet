import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.commands.runserver import Command as Runserver
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings

from cabinet import boundary
from shelf.management.commands.demo_runserver import Command


class BoundaryTests(SimpleTestCase):
    def test_safe_commands_and_fixed_paths(self):
        for args in (("test",), ("check",), ("makemigrations", "--check", "--dry-run"),
                     ("demo_migrate", "--synthetic-demo", "--plan"),
                     ("demo_bootstrap", "--synthetic-demo"),
                     ("demo_runserver", "--synthetic-demo")):
            self.assertEqual(boundary.validate_command(args), args[0])
        boundary.validate_paths()
        self.assertEqual(boundary.DATABASE, boundary.ROOT / ".demo-data/synthetic.sqlite3")
        self.assertEqual(boundary.runtime_database(), ":memory:")

    def test_rejects_private_config_database_modes_and_public_binding_before_setup(self):
        cases = (
            (), ("runserver",), ("shell",), ("dbshell",), ("flush",), ("createsuperuser",),
            ("dumpdata",), ("loaddata",), ("migrate",),
            ("demo_runserver",), ("demo_migrate",), ("demo_bootstrap",),
            ("test", "--keepdb"), ("test", "--parallel"),
            ("check", "--settings=private.settings"), ("check", "--pythonpath=/fictional/private"),
            ("demo_runserver", "--synthetic-demo", "0.0.0.0:8000"),
            ("demo_runserver", "--synthetic-demo", "[::]:8000"),
            ("demo_runserver", "--synthetic-demo", "127.0.0.1:9000"),
            ("demo_runserver", "--synthetic-demo", "--mode=real"),
            ("demo_runserver", "--synthetic-demo", "--database=/fictional/private.sqlite3"),
        )
        for args in cases:
            with self.subTest(args=args), self.assertRaises(boundary.BoundaryError):
                boundary.validate_command(args)

    def test_environment_override_names_rejected_without_reading_values(self):
        class NamesOnly(dict):
            def __getitem__(self, name):
                raise AssertionError("Environment values must not be read")
        for name in ("DATABASE_URL", "DB_PATH", "DATABASE_PATH", "SQLITE_PATH", "SECRET_KEY",
                     "DEBUG", "ALLOWED_HOSTS", "DJANGO_SETTINGS_MODULE", "DJANGO_CONFIGURATION",
                     "PYTHONPATH", "CABINET_MODE", "CC_MODE", "CC_DATABASE"):
            with self.subTest(name=name), self.assertRaises(boundary.BoundaryError):
                boundary.validate_environment(NamesOnly({name: "fictional-unused"}))
        boundary.validate_environment(NamesOnly({"PATH": "unused", "LANG": "unused"}))

    def test_symlinked_store_database_and_key_are_refused(self):
        for path in (boundary.STORE, boundary.DATABASE, boundary.KEY_FILE):
            original = Path.is_symlink
            with patch.object(Path, "is_symlink", lambda p: p == path or original(p)):
                with self.assertRaises(boundary.BoundaryError):
                    boundary.validate_paths()

    def test_nonentrypoint_settings_load_refused(self):
        with patch.object(boundary, "_secret", None):
            with self.assertRaises(boundary.BoundaryError):
                boundary.settings_secret()

    def test_start_command_requires_demo_and_fixes_loopback_without_opening_socket(self):
        command = Command()
        for options in ({}, {"synthetic_demo": True, "addrport": "0.0.0.0:8000"},
                        {"synthetic_demo": True, "use_ipv6": True}):
            with self.assertRaises(CommandError):
                command.handle(**options)
        with patch.object(Runserver, "handle") as parent:
            command.handle(synthetic_demo=True)
        self.assertEqual(parent.call_args.kwargs["addrport"], "127.0.0.1:8000")
        self.assertFalse(parent.call_args.kwargs["use_reloader"])
        self.assertFalse(parent.call_args.kwargs["use_threading"])
        self.assertFalse(parent.call_args.kwargs["use_ipv6"])

    def test_actual_cli_rejects_unsafe_startup_without_listener_or_private_read(self):
        environment = dict(os.environ)
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        commands = [
            (["runserver", "0.0.0.0:8000"], {}),
            (["demo_runserver"], {}),
            (["demo_runserver", "--synthetic-demo", "0.0.0.0:8000"], {}),
            (["check", "--settings=fictional_private.settings"], {}),
            (["check", "--pythonpath=/fictional_private"], {}),
            (["check"], {"DATABASE_URL": "sqlite:////fictional_private/household.sqlite3"}),
            (["check"], {"DJANGO_SETTINGS_MODULE": "fictional_private.settings"}),
            (["demo_runserver", "--synthetic-demo"], {"CC_MODE": "real"}),
        ]
        for args, extra in commands:
            with self.subTest(args=args, names=list(extra)):
                result = subprocess.run(
                    [sys.executable, "-B", str(boundary.ROOT / "backend/manage.py"), *args],
                    env={**environment, **extra}, cwd=boundary.ROOT,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "Refused: fixed synthetic-demo configuration required.\n")


class EphemeralDatabaseTests(TestCase):
    def test_framework_tests_are_in_memory_and_admin_is_not_installed(self):
        self.assertIn("memory", connection.settings_dict["NAME"])
        self.assertNotIn("django.contrib.admin", settings.INSTALLED_APPS)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AssetBoundaryTests(SimpleTestCase):
    def test_local_asset_bytes_work_but_adjacent_secret_traversal_and_symlink_do_not(self):
        secret = "FICTIONAL-ASSET-BOUNDARY-CANARY-428"
        # Disposable fixtures use the invocation's temporary directory (TMPDIR).
        with TemporaryDirectory(prefix="asset-fixture-") as folder:
            root = Path(folder)
            (root / "static").mkdir()
            (root / "static/fixture.css").write_text("/* fictional fixture */")
            (root / "secret-canary.txt").write_text(secret)
            (root / "static/escape.css").symlink_to(root / "secret-canary.txt")
            with patch("cabinet.assets.ROOT", root):
                client = Client(enforce_csrf_checks=True)
                response = client.get("/static/fixture.css")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b"".join(response.streaming_content), b"/* fictional fixture */")
                self.assertEqual(response["Cache-Control"], "no-store")
                self.assertIn("default-src 'none'", response["Content-Security-Policy"])
                response.close()
                for path in ("/static/../secret-canary.txt", "/static/%2e%2e/secret-canary.txt",
                             "/static/escape.css", "/static/secret-canary.txt"):
                    denied = client.get(path)
                    self.assertEqual(denied.status_code, 404)
                    self.assertNotIn(secret.encode(), denied.content)
                    self.assertEqual(denied.content, b"Not available.\n")
