import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_accounts.history import create_link, is_junction, is_link, remove_link, _linked
from codex_accounts.native import codex_binary, _is_wrapper
from codex_accounts.cli import run


class WindowsIntegrationTests(unittest.TestCase):
    def test_codex_binary_finds_official_cli(self):
        binary = codex_binary()
        self.assertTrue(Path(binary).is_file())
        self.assertFalse(_is_wrapper(Path(binary)))
        self.assertIn("codex", Path(binary).name.lower())

    def test_is_wrapper_identifies_current_scripts(self):
        py_scripts = Path(sys.executable).parent / "Scripts"
        codex_script = py_scripts / "codex.exe"
        if codex_script.exists():
            self.assertTrue(_is_wrapper(codex_script))

    def test_junction_and_hardlink_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            target_dir = temp_path / "target_dir"
            target_dir.mkdir()
            (target_dir / "test.txt").write_text("hello", encoding="utf-8")

            link_dir = temp_path / "link_dir"
            create_link(link_dir, target_dir, target_is_directory=True)
            self.assertTrue(link_dir.exists())
            self.assertTrue(_linked(link_dir, target_dir))
            self.assertEqual((link_dir / "test.txt").read_text(encoding="utf-8"), "hello")

            remove_link(link_dir)
            self.assertFalse(link_dir.exists())
            self.assertTrue(target_dir.exists())

            target_file = temp_path / "file.txt"
            target_file.write_text("world", encoding="utf-8")
            link_file = temp_path / "link_file.txt"
            create_link(link_file, target_file, target_is_directory=False)
            self.assertTrue(link_file.exists())
            self.assertTrue(_linked(link_file, target_file))

            remove_link(link_file)
            self.assertFalse(link_file.exists())
            self.assertTrue(target_file.exists())

    def test_shell_init_powershell(self):
        with patch("sys.stdout.write") as mock_write, patch("builtins.print") as mock_print:
            code = run(["shell-init", "powershell"])
            self.assertEqual(code, 0)
            mock_print.assert_called()
            output = mock_print.call_args[0][0]
            self.assertIn("function codex {", output)
            self.assertIn("CODEX_ACCOUNTS_CODEX_BIN", output)

    def test_shell_init_cmd(self):
        with patch("builtins.print") as mock_print:
            code = run(["shell-init", "cmd"])
            self.assertEqual(code, 0)
            mock_print.assert_called()
            output = mock_print.call_args[0][0]
            self.assertIn("doskey codex=", output)

    def test_private_directory_preserves_inheritance(self):
        from codex_accounts.state import private_directory
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "test_private"
            private_directory(target_dir)
            self.assertTrue(target_dir.is_dir())
            test_file = target_dir / "test.txt"
            test_file.write_text("ok", encoding="utf-8")
            self.assertEqual(test_file.read_text(encoding="utf-8"), "ok")
