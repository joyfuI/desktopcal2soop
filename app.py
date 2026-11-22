"""Portable Windows notification-area entry point."""

import configparser
import ctypes
import sys
import threading
from pathlib import Path

from soop_client import SoopLoginBlocked, load_config
from sync import synchronize

BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG = BASE / "config.ini"
STATE = BASE / "sync.db"
ICON = Path(__file__).resolve().with_name("DesktopCal2SOOP.ico")


def startup_menu_item():
    import winreg
    from pystray import MenuItem

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    value_name = "DesktopCal2SOOP"
    command = f'"{Path(sys.executable).resolve()}"'
    registered = False

    def matches(key):
        try:
            return winreg.QueryValueEx(key, value_name) == (command, winreg.REG_SZ)
        except FileNotFoundError:
            return False

    def checked(item):
        nonlocal registered
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as key:
                registered = matches(key)
        except FileNotFoundError:
            registered = False
        except OSError:
            pass  # Keep the last known state when Windows cannot read the key.
        return registered

    def toggle(icon, item):
        nonlocal registered
        try:
            access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, access=access) as key:
                enabled = matches(key)
                if enabled:
                    winreg.DeleteValue(key, value_name)
                else:
                    if len(command.encode("utf-16-le")) > 260 * 2:
                        raise ValueError("자동 실행에 등록하기에는 EXE 경로가 너무 깁니다.")
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
                registered = not enabled
        except (OSError, ValueError) as error:
            icon.notify(str(error), "부팅 시 자동 실행 설정 실패")

    return MenuItem("부팅 시 자동 실행", toggle, checked=checked)


def run_tray():
    import pystray
    from PIL import Image

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateMutexW(None, 0, "Local\\DesktopCal2SOOP")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            return
        stop, request = threading.Event(), threading.Event()
        request.set()

        with Image.open(ICON) as icon_file:
            image = icon_file.convert("RGBA")

        def quit_app(icon, item):
            stop.set()
            request.set()

        def work(icon):
            icon.visible = True
            interval = 10
            paused = False
            while not stop.is_set():
                manual = request.wait(interval * 60)
                if stop.is_set():
                    break
                request.clear()
                if manual:
                    paused = False
                if paused:
                    continue
                try:
                    interval = load_config(CONFIG).getint("sync", "interval_minutes")
                    if interval < 1:
                        interval = 10
                        raise ValueError("동기화 주기는 1분 이상의 정수로 입력하세요.")
                    synchronize(CONFIG, STATE, stop.is_set)
                except SoopLoginBlocked as error:
                    paused = True
                    icon.notify(str(error), "SOOP 동기화 실패")
                except configparser.Error:
                    icon.notify("config.ini의 형식을 확인하세요.", "SOOP 동기화 실패")
                except FileNotFoundError:
                    icon.notify("CalendarTask 설치 또는 DB 파일을 확인하세요.", "SOOP 동기화 실패")
                except Exception as error:
                    reason = " ".join(str(error).split())[:220] or type(error).__name__
                    icon.notify(reason, "SOOP 동기화 실패")
            icon.stop()

        items = [
            pystray.MenuItem("동기화", lambda icon, item: request.set(), default=True),
        ]
        if getattr(sys, "frozen", False):
            items.append(startup_menu_item())
        items.append(pystray.MenuItem("종료", quit_app))
        icon = pystray.Icon("DesktopCal2SOOP", image, "DesktopCal2SOOP", pystray.Menu(*items))
        icon.run(setup=work)
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    run_tray()
