import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock, patch

from app import startup_menu_item


@unittest.skipUnless(sys.platform == "win32", "Windows registry menu")
class StartupMenuTests(unittest.TestCase):
    def setUp(self):
        import winreg

        self.icon = Mock()
        path = Path(sys.executable).resolve().with_name("DesktopCal2SOOP test.exe")
        self.command = f'"{path}"'
        self.enterContext(patch.object(sys, "executable", str(path)))
        self.registry = SimpleNamespace(**self.enterContext(patch.multiple(
            winreg, OpenKey=DEFAULT, CreateKeyEx=DEFAULT, QueryValueEx=DEFAULT,
            SetValueEx=DEFAULT, DeleteValue=DEFAULT,
        )))
        self.key = self.registry.CreateKeyEx.return_value.__enter__.return_value
        self.registry.OpenKey.return_value = self.registry.CreateKeyEx.return_value
        self.registration = (self.command, winreg.REG_SZ)
        self.item = startup_menu_item()

    def test_moved_exe_replaces_old_registration_and_can_be_disabled(self):
        self.registry.QueryValueEx.return_value = (r'"C:\Old\DesktopCal2SOOP.exe"', self.registration[1])
        self.assertFalse(self.item.checked)
        self.registry.CreateKeyEx.assert_not_called()

        self.item(self.icon)
        self.registry.SetValueEx.assert_called_once_with(
            self.key, "DesktopCal2SOOP", 0, self.registration[1], self.command,
        )
        self.registry.QueryValueEx.return_value = self.registration
        self.assertTrue(self.item.checked)

        self.item(self.icon)
        self.registry.DeleteValue.assert_called_once_with(self.key, "DesktopCal2SOOP")
        self.registry.QueryValueEx.side_effect = FileNotFoundError
        self.assertFalse(self.item.checked)
        self.icon.notify.assert_not_called()

    def test_registry_access_failure_keeps_last_checked_state(self):
        self.registry.QueryValueEx.return_value = self.registration
        self.assertTrue(self.item.checked)

        self.registry.OpenKey.side_effect = PermissionError("읽기 거부")
        self.registry.DeleteValue.side_effect = PermissionError("쓰기 거부")
        self.item(self.icon)

        self.assertTrue(self.item.checked)
        self.icon.notify.assert_called_once_with("쓰기 거부", "부팅 시 자동 실행 설정 실패")
