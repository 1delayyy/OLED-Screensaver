"""
OLED Screensaver - settings app for Wallpaper Engine (Windows, Python 3.8+)
The tray icon is optional: pip install pystray pillow

Goes black after N minutes of no mouse/keyboard input, restores on activity.
Closing the window hides it (to the tray if available, else the taskbar); use Quit to exit.
"""
import ctypes
import json
import locale
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import webbrowser
import zlib
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from urllib.parse import quote_plus

try:  # optional - only needed for the system tray icon
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None

APP_NAME = "OLED Screensaver"
APP_VERSION = "1.1.0"
APP_AUTHOR = "Delay"

FROZEN = getattr(sys, "frozen", False)  # True when running as a packaged .exe
if FROZEN:  # an .exe unpacks to a temp folder, so keep settings somewhere permanent
    APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "OLED Screensaver"
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = Path(__file__).resolve().parent
OLD_SETTINGS = Path(os.environ.get("LOCALAPPDATA", "")) / "IdleBlack" / "settings.json"
SETTINGS_FILE = APP_DIR / "settings.json"
STATE_FILE = APP_DIR / "saved_wallpaper.json"
BLACK_NAME = "idle_black"
WAKE_THRESHOLD = 1.5
TEST_SECONDS = 3       # how long the 'Test now' button stays black before reverting by itself
LOOKBACK_SECONDS = 60   # restore the wallpaper that was showing this long before the screensaver started
STARTUP_VBS = (Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
               / "IdleBlack.vbs")
DEFAULTS = {"minutes": 3, "enabled": True, "we_dir": "", "startup": False,
            "display": "all", "restore_windows": False, "auto_pause": True,
            "dark_mode": True, "hide_icons": True, "taskbar_while_black": False,
            "sleep_enabled": False, "sleep_minutes": 10, "extra_programs": [],
            "taskbar_clear": False, "taskbar_opacity": 0, "ui_scale": 100, "window_size": None,
            "taskbar_fade": False, "taskbar_fade_level": 30,
            "restore_mode": "auto", "restore_choice": None,
            "fade_enabled": False, "fade_seconds": 3, "call_pause": True,
            "schedule_enabled": False, "schedule_mode": "different", "schedule_start": "22:00",
            "schedule_end": "07:00", "schedule_minutes": 5, "black_all_screens": False,
            "update_url": "", "update_check_startup": False,
            "watch_wallpaper": True, "panel_type": "auto", "display_model": ""}


# ---------- log / one copy at a time ----------
LOG_FILE = APP_DIR / "oled_screensaver.log"
SHOW_FLAG = APP_DIR / "show_window.flag"  # a second copy leaves this to ask the first one to show its window
QUIT_FLAG = APP_DIR / "quit.flag"         # the installer/uninstaller leaves this to ask a running copy to close
ERROR_COUNT = 0
_instance_handle = None


def log(msg):
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 200_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except OSError:
        pass


def log_error(where):
    """Record the exception that is being handled right now."""
    global ERROR_COUNT
    ERROR_COUNT += 1
    log(f"ERROR in {where}:\n" + traceback.format_exc())


def _open_instance_mutex():
    """(handle, already_existed) for this app's named mutex."""
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\OLEDScreensaverByDelay")
    return handle, ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def acquire_single_instance():
    """True if this is the only copy running (the lock is held until the program exits)."""
    global _instance_handle
    try:
        handle, already = _open_instance_mutex()
    except Exception:
        return True  # if the check itself fails, never stop the app from starting
    if already:
        return False
    _instance_handle = handle
    return True


def remove_flag(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def instance_running():
    """True if another copy is running right now (without taking the lock ourselves)."""
    try:
        handle, already = _open_instance_mutex()
        if handle:
            kernel32.CloseHandle(handle)
        return already
    except Exception:
        return False


# ---------- settings ----------
def load_settings():
    for path in (SETTINGS_FILE, OLD_SETTINGS if FROZEN else None):  # exe adopts settings from the old install
        if path is None:
            continue
        try:
            return {**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            continue
    return dict(DEFAULTS)


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


PORTABLE_SKIP = {"we_dir", "window_size", "display", "restore_choice", "restore_mode", "display_model"}  # specific to one PC
RANGES = {"minutes": (1, 10), "sleep_minutes": (1, 240), "ui_scale": (70, 200), "taskbar_opacity": (0, 100),
          "taskbar_fade_level": (5, 100), "fade_seconds": (1, 10), "schedule_minutes": (1, 60)}
CHOICES = {"schedule_mode": ("different", "only"), "panel_type": ("auto", "oled", "lcd")}


def clean_settings(data):
    """Keep only known settings that have the right type and a sensible value. Returns (clean, skipped_count)."""
    clean, skipped = {}, 0
    if not isinstance(data, dict):
        return clean, 1
    for key, value in data.items():
        if key not in DEFAULTS or key in PORTABLE_SKIP:
            skipped += 1
            continue
        default = DEFAULTS[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if ok:
                value = int(value)
                if key in RANGES:
                    value = max(RANGES[key][0], min(RANGES[key][1], value))
        elif isinstance(default, str):
            ok = isinstance(value, str)
            if ok and key in CHOICES and value not in CHOICES[key]:
                ok = False
            if ok and key in ("schedule_start", "schedule_end") and parse_hhmm(value) is None:
                ok = False
        elif isinstance(default, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = value is None or isinstance(value, (list, dict))
        if ok:
            clean[key] = value
        else:
            skipped += 1
    return clean, skipped


def set_startup(enabled):
    if enabled:
        if FROZEN:
            line = 'CreateObject("WScript.Shell").Run """%s"" --minimized", 0, False\n' % sys.executable
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            pyw = pyw if pyw.exists() else Path(sys.executable)
            line = ('CreateObject("WScript.Shell").Run """%s"" ""%s"" --minimized", 0, False\n'
                    % (pyw, Path(__file__).resolve()))
        STARTUP_VBS.write_text(line, encoding="utf-16")  # UTF-16 keeps non-English user names working
    else:
        STARTUP_VBS.unlink(missing_ok=True)


# ---------- idle detection ----------
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------- Wallpaper Engine helpers ----------
def find_we_dir(custom=""):
    candidates = [Path(custom)] if custom else []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            candidates.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]) / "steamapps/common/wallpaper_engine")
    except OSError:
        pass
    candidates += [Path(r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine"),
                   Path(r"C:\Program Files\Steam\steamapps\common\wallpaper_engine")]
    for c in candidates:
        if (c / "wallpaper64.exe").exists() or (c / "wallpaper32.exe").exists():
            return c
    return None


def we_exe(we_dir):
    for name in ("wallpaper64.exe", "wallpaper32.exe"):
        if (we_dir / name).exists():
            return we_dir / name


def write_png(path, w=160, h=90):
    raw = (b"\x00" + b"\x00\x00\x00" * w) * h

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def ensure_black_project(we_dir):
    folder = we_dir / "projects" / "myprojects" / BLACK_NAME
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "black.png").exists():
        write_png(folder / "black.png")
    project = folder / "project.json"
    if not project.exists():
        project.write_text(json.dumps({"file": "black.png", "preview": "black.png", "title": "Idle Black",
                                       "type": "image", "description": "Used by Idle Black", "tags": []}),
                           encoding="utf-8")
    return project


def _find_key(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            else:
                yield from _find_key(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _find_key(item, key)


def wallpaper_title(we_dir, file):
    """The wallpaper's display name, read from its project.json."""
    try:
        p = Path(str(file))
        for cand in ([p] if p.is_absolute() else [we_dir / p, we_dir / "projects" / p]):
            if cand.is_file() and cand.name.lower() != "project.json":
                cand = cand.with_name("project.json")  # e.g. scene.pkg or index.html -> the project.json beside it
            if cand.exists():
                title = json.loads(cand.read_text(encoding="utf-8-sig")).get("title")
                return title or cand.parent.name
    except (OSError, ValueError, AttributeError):
        pass
    return Path(str(file)).parent.name or str(file)


def selector_args(selector):
    """Command-line arguments that pick a screen: a location id, a monitor number, or nothing (the main screen)."""
    if isinstance(selector, str):
        return ["-location", selector]
    if selector is not None:
        return ["-monitor", str(selector)]
    return []


def get_current_wallpaper(exe, timeout=4, selector=None):
    """Ask Wallpaper Engine itself which wallpaper is on the main screen (its 'getWallpaper' command).
    Far more reliable than its config.json, which can be out of date. Returns a path or None."""
    try:
        result = subprocess.run([str(exe), "-control", "getWallpaper"] + selector_args(selector),
                                capture_output=True, timeout=timeout,
                                creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    raw = result.stdout or b""
    if b"\x00" in raw:
        text = raw.decode("utf-16-le", errors="ignore")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode(locale.getpreferredencoding(False), errors="ignore")
    for line in reversed(text.splitlines()):
        line = line.strip().strip('"').strip()
        if line and Path(line).exists():
            return line
    return None


def normalize_wallpaper_path(path):
    """Prefer the wallpaper's project.json (keeps its settings) over a bare scene.pkg / index.html."""
    p = Path(str(path))
    if p.name.lower() != "project.json" and (p.parent / "project.json").exists():
        return str(p.parent / "project.json")
    return str(p)


def read_entries(we_dir):
    """Wallpapers Wallpaper Engine says are selected: [{section, key, file, title, raw}, ...].
    Your own Windows user's section is used first, so stale sections from other profiles are ignored."""
    cfg = json.loads((we_dir / "config.json").read_text(encoding="utf-8-sig"))
    me = os.environ.get("USERNAME", "").lower()
    sections = sorted(cfg.items(), key=lambda kv: kv[0].lower() != me) if isinstance(cfg, dict) else [("", cfg)]
    for name, section in sections:
        entries = []
        for sel in _find_key(section, "selectedwallpapers"):
            if not isinstance(sel, dict):
                continue
            if sel.get("file"):
                entries.append({"key": "", "file": sel["file"], "raw": sel})
            else:
                for key, entry in sel.items():
                    if isinstance(entry, dict) and entry.get("file"):
                        entries.append({"key": str(key), "file": entry["file"], "raw": entry})
        if entries:
            for e in entries:
                e["section"] = name
                e["title"] = wallpaper_title(we_dir, e["file"])
            return entries
    return []


def current_wallpapers(we_dir):
    """[[monitor_index or None, project_file], ...] for the selected wallpapers."""
    out = []
    for e in read_entries(we_dir):
        m = re.search(r"(?i)monitor\s*(\d+)$", e["key"])
        out.append([int(m.group(1)) if m else None, e["file"]])
    return out


def open_wallpaper(exe, file, monitor=None):
    cmd = [str(exe), "-control", "openWallpaper", "-file", str(file)] + selector_args(monitor)
    try:
        result = subprocess.run(cmd, check=False, creationflags=0x08000000, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"FAILED to run {' '.join(cmd)}: {e}")
        raise
    log("ran: " + " ".join(cmd) + f"  -> exit code {result.returncode}")
    return result.returncode


# ---------- window / monitor helpers ----------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
SKIP_EXES = {"wallpaper32.exe", "wallpaper64.exe", "ui32.exe", "webwallpaper32.exe", "webwallpaper64.exe"}


def list_monitors():
    result = []

    def cb(hmon, hdc, rect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            result.append({"handle": hmon, "device": info.szDevice, "w": r.right - r.left,
                           "h": r.bottom - r.top, "primary": bool(info.dwFlags & 1), "x": r.left, "y": r.top})
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return result


def monitor_label(m, number):
    return f"Display {number}: {m['w']}x{m['h']}" + (" (primary)" if m["primary"] else "")


def exe_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


def is_cloaked(hwnd):
    v = wintypes.DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))  # DWMWA_CLOAKED
    return bool(v.value)


def minimise_windows(device="all"):
    """Minimise normal app windows on the given display ('all' = every display). Returns their handles."""
    targets = None if device == "all" else {m["handle"] for m in list_monitors() if m["device"] == device}
    minimised = []
    my_pid = os.getpid()

    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if user32.GetWindow(hwnd, 4):                      # has an owner (dialogs etc.)
                return True
            if user32.GetWindowLongW(hwnd, -20) & 0x80:        # tool window
                return True
            if not user32.GetWindowTextLengthW(hwnd) or is_cloaked(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in SKIP_CLASSES:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == my_pid or exe_name(pid.value) in SKIP_EXES:
                return True
            if targets is not None and user32.MonitorFromWindow(hwnd, 2) not in targets:
                return True
            user32.ShowWindowAsync(hwnd, 7)                    # SW_SHOWMINNOACTIVE
            minimised.append(hwnd)
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return minimised


def restore_windows(handles):
    for hwnd in handles:
        if user32.IsWindow(hwnd) and user32.IsIconic(hwnd):
            user32.ShowWindowAsync(hwnd, 9)                    # SW_RESTORE


# ---------- game / video detection ----------
psapi = ctypes.WinDLL("psapi")
shell32 = ctypes.WinDLL("shell32")
psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

GAME_PATH_HINTS = ("\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\", "\\xboxgames\\")
GAME_PATH_IGNORE = ("wallpaper_engine", "steamworks shared", "\\launcher\\", "\\redist")
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
VIDEO_PLAYERS = {"vlc.exe", "mpv.exe", "mpc-hc.exe", "mpc-hc64.exe", "potplayermini.exe",
                 "potplayermini64.exe", "wmplayer.exe"}
VIDEO_TITLES = ("youtube", "netflix", "twitch", "prime video", "disney+", "hulu", "hbo max", "crunchyroll",
                "vimeo", "plex", "apple tv", "paramount+", "peacock", "dailymotion", "bilibili")


def exe_path(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1040)
        size = wintypes.DWORD(1040)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return ""


def game_running(extra=()):
    """Name of a running game (Steam/Epic/GOG/Xbox folders, or a program from your own list), or None."""
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return None
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        path = exe_path(pid).lower() if pid else ""
        if path and os.path.basename(path) in extra:
            return os.path.basename(path)
        if path and any(h in path for h in GAME_PATH_HINTS) and not any(i in path for i in GAME_PATH_IGNORE):
            return os.path.basename(path)
    return None


def fullscreen_app_active():
    """True if a fullscreen game/video (exclusive or borderless) is in the foreground."""
    state = ctypes.c_int(0)
    if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4):
        return True  # busy / D3D fullscreen / presentation mode
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    if cls.value in SKIP_CLASSES or user32.GetWindowLongW(hwnd, -16) & 0xC00000:  # has a title bar
        return False
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
        return False
    m = info.rcMonitor
    return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom


def foreground_media():
    """Describes video playback in the foreground window (browser tab title / video player), or None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = exe_name(pid.value)
    if name in VIDEO_PLAYERS:
        return "video player"
    if name in BROWSERS:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if any(k in buf.value.lower() for k in VIDEO_TITLES):
            return "video in browser"
    return None


# ---------- themes ----------
THEMES = {
    "dark": {"bg": "#121212", "fg": "#e6e6e6", "field": "#1e1e1e", "button": "#2d2d2d",
             "trough": "#3a3a3a", "muted": "#9a9a9a", "good": "#4cd08a"},
    "light": {"bg": "#f0f0f0", "fg": "#111111", "field": "#ffffff", "button": "#e1e1e1",
              "trough": "#c8c8c8", "muted": "#555555", "good": "#0a7a45"},
}
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def set_titlebar_dark(window, dark):
    """Ask Windows to draw this window's title bar dark (Windows 10 20H1+ / 11). Harmless if unsupported."""
    try:
        window.update_idletasks()
        hwnd = user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))  # DWMWA_USE_IMMERSIVE_DARK_MODE
    except Exception:
        pass


# ---------- taskbar auto-hide ----------
class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


ABM_GETSTATE, ABM_SETSTATE = 4, 10
ABS_AUTOHIDE, ABS_ALWAYSONTOP = 1, 2
shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
shell32.SHAppBarMessage.restype = ctypes.c_size_t
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND


def taskbar_hidden():
    """True if Windows' 'Automatically hide the taskbar' is on."""
    try:
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(data)
        return bool(shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data)) & ABS_AUTOHIDE)
    except Exception:
        return False


def set_taskbar_hidden(hidden):
    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(data)
    data.hWnd = user32.FindWindowW("Shell_TrayWnd", None)
    data.lParam = ABS_AUTOHIDE if hidden else ABS_ALWAYSONTOP
    shell32.SHAppBarMessage(ABM_SETSTATE, ctypes.byref(data))


# ---------- desktop icons / display power / tray ----------
UI_STATE_FILE = APP_DIR / "ui_state.json"
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def desktop_icons_window():
    """The desktop's icon list. Wallpaper Engine moves it under a WorkerW window, so look under both."""
    found = []

    def cb(hwnd, lparam):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW"):
            view = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if view:
                icons = user32.FindWindowExW(view, None, "SysListView32", None)
                if icons:
                    found.append(icons)
                    return False  # stop searching
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def desktop_icons_visible():
    icons = desktop_icons_window()
    return bool(icons and user32.IsWindowVisible(icons))


def set_desktop_icons(visible):
    icons = desktop_icons_window()
    if icons:
        user32.ShowWindow(icons, 5 if visible else 0)  # SW_SHOW / SW_HIDE


def display_off():
    user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)  # broadcast WM_SYSCOMMAND / SC_MONITORPOWER / off


def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(20, 20, 20, 255), outline=(240, 240, 240, 255), width=4)
    d.ellipse((22, 16, 54, 48), fill=(240, 240, 240, 255))   # a simple crescent moon
    d.ellipse((30, 11, 62, 43), fill=(20, 20, 20, 255))
    return img


# ---------- transparent taskbar ----------
# Windows has no official setting for a fully clear taskbar. This uses the same undocumented call as
# TranslucentTB (SetWindowCompositionAttribute); Windows re-asserts its own look now and then, so it is re-applied.
class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def taskbar_windows():
    """The main taskbar plus the taskbar on any other monitors."""
    found = []
    main = user32.FindWindowW("Shell_TrayWnd", None)
    if main:
        found.append(main)
    child = None
    while True:
        child = user32.FindWindowExW(None, child, "Shell_SecondaryTrayWnd", None)
        if not child:
            break
        found.append(child)
    return found


def set_taskbar_clear(enabled, opacity_percent=0):
    """enabled: make the taskbar background transparent (0% = crystal clear, higher = darker black tint).
    disabled: hand the taskbar back to Windows' normal look."""
    set_attr = user32.SetWindowCompositionAttribute
    set_attr.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
    set_attr.restype = wintypes.BOOL
    for hwnd in taskbar_windows():
        accent = ACCENTPOLICY()
        if enabled:
            accent.AccentState = 2  # ACCENT_ENABLE_TRANSPARENTGRADIENT
            accent.AccentFlags = 2
            alpha = max(1, int(opacity_percent * 255 / 100))  # never exactly 0, which some builds treat specially
            accent.GradientColor = alpha << 24  # black tint at this opacity
        else:
            accent.AccentState = 0  # ACCENT_DISABLED
        data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.byref(accent), ctypes.c_void_p), ctypes.sizeof(accent))
        set_attr(hwnd, ctypes.byref(data))  # 19 = WCA_ACCENT_POLICY


# Experimental fallback: fade the WHOLE taskbar (icons and clock too) with ordinary window transparency.
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
_LAYERED_BY_US = set()


user32.GetLayeredWindowAttributes.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD)]
user32.GetLayeredWindowAttributes.restype = wintypes.BOOL


def layered_alpha(hwnd):
    """The window's current transparency (0-255), or None if it isn't a layered window."""
    key, alpha, flags = wintypes.DWORD(), ctypes.c_ubyte(), wintypes.DWORD()
    if user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)):
        return alpha.value if flags.value & 2 else None
    return None


def set_taskbar_fade(enabled, visible_percent=30):
    for hwnd in taskbar_windows():
        style = user32.GetWindowLongPtrW(hwnd, -20)  # GWL_EXSTYLE
        if enabled:
            if not style & 0x80000:  # WS_EX_LAYERED
                user32.SetWindowLongPtrW(hwnd, -20, style | 0x80000)
                _LAYERED_BY_US.add(hwnd)
            alpha = max(8, min(255, int(visible_percent * 255 / 100)))
            if layered_alpha(hwnd) != alpha:  # already right? then leave the taskbar alone (re-applying can blink)
                user32.SetLayeredWindowAttributes(hwnd, 0, alpha, 2)  # LWA_ALPHA
        else:
            if layered_alpha(hwnd) not in (None, 255):
                user32.SetLayeredWindowAttributes(hwnd, 0, 255, 2)
            if hwnd in _LAYERED_BY_US:
                user32.SetWindowLongPtrW(hwnd, -20, style & ~0x80000)
                _LAYERED_BY_US.discard(hwnd)


# ---------- taskbar diagnostics ----------
gdi32 = ctypes.WinDLL("gdi32")
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def windows_build():
    """e.g. '26300.10075' (build number + update revision)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            build = winreg.QueryValueEx(k, "CurrentBuildNumber")[0]
            try:
                return f"{build}.{winreg.QueryValueEx(k, 'UBR')[0]}"
            except OSError:
                return str(build)
    except Exception:
        return "unknown"


def describe_taskbar():
    """Class names of the taskbar windows and their children - shows how Windows has built the taskbar."""
    lines = []
    for hwnd in taskbar_windows():
        lines.append(f"  {class_name(hwnd)}")
        kids = []

        def cb(child, lparam):
            kids.append(class_name(child))
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
        lines += [f"      child: {k}" for k in kids[:15]]
    return lines or ["  (taskbar window not found)"]


def sample_taskbar():
    """Screen colours at a few spots along the main taskbar, as [(r, g, b), ...]."""
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return []
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    hdc = user32.GetDC(None)
    spots = []
    try:
        for frac in (0.04, 0.15, 0.3, 0.7, 0.85, 0.96):  # towards the edges, where there are usually no icons
            x = int(rect.left + (rect.right - rect.left) * frac)
            y = rect.top + (rect.bottom - rect.top) // 2
            c = gdi32.GetPixel(hdc, x, y)
            if c != 0xFFFFFFFF:  # CLR_INVALID
                spots.append((c & 255, (c >> 8) & 255, (c >> 16) & 255))
    finally:
        user32.ReleaseDC(None, hdc)
    return spots


# ---------- screen time recorder ----------
STATS_FILE = APP_DIR / "screen_time.json"


def load_stats():
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("days"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"days": {}}


def format_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# ---------- schedule, calls, updates, screens, install ----------
def parse_hhmm(text):
    """'22:30' -> minutes after midnight, or None if it isn't a valid time."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not m:
        return None
    hours, minutes = int(m.group(1)), int(m.group(2))
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def in_time_window(now_minutes, start, end):
    """True if the time is inside start..end. Windows that cross midnight (22:00-07:00) work too."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def capture_in_use():
    """Name of an app that is using the microphone or camera right now (a call), or None."""
    try:
        import winreg
    except ImportError:
        return None
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
    for device in ("microphone", "webcam"):
        for sub in ("", "\\NonPackaged"):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{device}{sub}")
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    if name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(key, name) as app:
                            started = winreg.QueryValueEx(app, "LastUsedTimeStart")[0]
                            stopped = winreg.QueryValueEx(app, "LastUsedTimeStop")[0]
                    except OSError:
                        continue
                    if started and started > stopped:  # a stop time of 0 means it is still in use
                        return name.replace("#", "\\").split("\\")[-1]
    return None


def parse_version(text):
    numbers = re.findall(r"\d+", str(text))
    return tuple(int(n) for n in numbers[:4]) or None


def fetch_update_info(url, timeout=6):
    """Read the latest version from a web address. The page can be JSON like {"version": "1.2.0", "url": "..."}
    or just a version number. Returns (version_text, download_link)."""
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("the address must start with http:// or https://")
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read(20000).decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return str(data.get("version", "")), str(data.get("url", ""))
    except ValueError:
        pass
    return (body.splitlines()[0].strip() if body else ""), ""


def running_exe_names():
    """Lower-case program names of everything running right now."""
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return set()
    names = set()
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        path = exe_path(pid) if pid else ""
        if path:
            names.add(os.path.basename(path).lower())
    return names


def screen_selectors(we_dir):
    """The screens Wallpaper Engine knows about: [{'selector', 'file', 'title'}, ...]. The selector is a monitor
    number or Wallpaper Engine's own location id; None means 'the main screen'."""
    screens = []
    for e in read_entries(we_dir):
        m = re.search(r"(?i)monitor\s*(\d+)$", e["key"])
        selector = int(m.group(1)) if m else (e["key"] or None)
        screens.append({"selector": selector, "file": e["file"], "title": e["title"]})
    return screens


def resource_path(name):
    """A file bundled with the program (works both as a script and as the packaged .exe)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def set_window_icon(root):
    try:
        icon = resource_path("oled.ico")
        if icon.exists():
            root.iconbitmap(default=str(icon))
    except Exception:
        pass  # a missing icon should never stop the app


INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_NAME
START_MENU_LINK = (Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu"
                   / "Programs" / f"{APP_NAME}.lnk")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\OLEDScreensaverByDelay"


def make_shortcut(link, target, icon=None):
    """Create a Windows shortcut with a throw-away VBScript (no extra packages needed)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    lines = ['Set shell = CreateObject("WScript.Shell")',
             f'Set shortcut = shell.CreateShortcut("{link}")',
             f'shortcut.TargetPath = "{target}"',
             f'shortcut.WorkingDirectory = "{Path(target).parent}"']
    if icon:
        lines.append(f'shortcut.IconLocation = "{icon}"')
    lines += [f'shortcut.Description = "{APP_NAME}"', "shortcut.Save"]
    script = Path(tempfile.gettempdir()) / "oled_make_shortcut.vbs"
    script.write_text("\n".join(lines) + "\n", encoding="utf-16")  # Windows turns \n into \r\n by itself
    try:
        subprocess.run(["wscript.exe", str(script)], check=False, timeout=30, creationflags=0x08000000)
    finally:
        script.unlink(missing_ok=True)
    return link.exists()


def close_running_copy(wait_seconds=10):
    """Ask a running copy to close (through a flag file) and wait for it. True when nothing is running."""
    if not instance_running():
        return True
    try:
        QUIT_FLAG.write_text("quit", encoding="utf-8")
    except OSError:
        return False
    deadline = time.time() + wait_seconds
    try:
        while time.time() < deadline:
            if not instance_running():
                return True
            time.sleep(0.3)
        return False
    finally:
        remove_flag(QUIT_FLAG)  # never leave a flag behind that could close a later launch


def install_app():
    """Copy this .exe into your programs folder, add a Start Menu shortcut and an 'Installed apps' entry.
    Returns (ok, message)."""
    if not FROZEN:
        return False, "Installing only works from the packaged .exe (build it with build_exe.bat)."
    import winreg
    target = INSTALL_DIR / f"{APP_NAME}.exe"
    if not close_running_copy():  # a running copy would block replacing the file, and would hold the one-copy lock
        return False, "Please close OLED Screensaver (Quit button) and run the installer again."
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if Path(sys.executable).resolve() != target.resolve():
        shutil.copy2(sys.executable, target)
    make_shortcut(START_MENU_LINK, target)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", APP_VERSION), ("Publisher", APP_AUTHOR),
                            ("InstallLocation", str(INSTALL_DIR)), ("DisplayIcon", str(target)),
                            ("UninstallString", f'"{target}" --uninstall')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    return True, str(target)


def undo_ui_changes():
    """Put back anything this app changed on your desktop (icons, taskbar look)."""
    for undo in (lambda: set_taskbar_clear(False), lambda: set_taskbar_fade(False)):
        try:
            undo()
        except Exception:
            pass
    try:
        if UI_STATE_FILE.exists():
            state = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
            if state.get("icons"):
                set_desktop_icons(True)
            if state.get("taskbar"):
                set_taskbar_hidden(False)
            UI_STATE_FILE.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def uninstall_app(delete_settings=False):
    """Remove the shortcut, startup entry and 'Installed apps' entry, then delete the program folder."""
    close_running_copy()
    undo_ui_changes()
    for path in (START_MENU_LINK, STARTUP_VBS):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except (OSError, ImportError):
        pass
    if delete_settings:
        shutil.rmtree(APP_DIR, ignore_errors=True)
    if FROZEN and INSTALL_DIR.exists():
        # a running .exe can't delete itself, so a tiny helper waits a few seconds and removes the folder
        subprocess.Popen(["cmd", "/c", f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{INSTALL_DIR}"'],
                         creationflags=0x08000000)
    return True


# ---------- display detection (brand, model, burn-in advice, presence sensor) ----------
# What follows is best-effort knowledge, written from the manufacturers' own manuals and support pages and
# from independent monitor reviews. It is a guide, never a guarantee - the manual for your exact model wins.
PNP_BRANDS = {"AUS": "ASUS", "ACI": "ASUS", "GSM": "LG", "SAM": "Samsung", "SEC": "Samsung", "DEL": "Dell",
              "DLL": "Dell", "PHL": "Philips", "MSI": "MSI", "GBT": "Gigabyte", "AOC": "AOC", "ACR": "Acer",
              "BNQ": "BenQ", "HWP": "HP", "HPN": "HP", "LEN": "Lenovo", "VSC": "ViewSonic", "ENC": "EIZO",
              "NEC": "NEC", "SNY": "Sony", "IVM": "iiyama"}

# Words people type that name a brand, and model-number patterns that give a brand away.
BRAND_WORDS = {"ASUS": "ASUS", "ROG": "ASUS", "PROART": "ASUS", "LG": "LG", "ULTRAGEAR": "LG", "SAMSUNG": "Samsung",
               "ODYSSEY": "Samsung", "MSI": "MSI", "DELL": "Dell", "ALIENWARE": "Dell", "PHILIPS": "Philips",
               "EVNIA": "Philips", "GIGABYTE": "Gigabyte", "AORUS": "Gigabyte", "AOC": "AOC", "AGON": "AOC",
               "ACER": "Acer", "PREDATOR": "Acer", "BENQ": "BenQ", "MOBIUZ": "BenQ", "HP": "HP", "OMEN": "HP",
               "LENOVO": "Lenovo", "VIEWSONIC": "ViewSonic", "EIZO": "EIZO", "SONY": "Sony", "CORSAIR": "Corsair",
               "XENEON": "Corsair"}
MODEL_FAMILIES = [("Dell", r"^AW\d{4}"), ("MSI", r"^(MPG|MAG|MD)\d{2,3}"), ("ASUS", r"^(PG|XG|PA|VG|VY|PB|MG)\d{2}"),
                  ("LG", r"^\d{2}G[A-Z]\d{2,3}"), ("Philips", r"^\d{2}M\d[A-Z]\d{4}"),
                  ("Gigabyte", r"^(FO|MO)\d{2}"), ("AOC", r"^AG\d{3}"), ("Samsung", r"^(LS)?\d{2}[A-Z]G\d{2}"),
                  ("Samsung", r"^G[5-9]\d[A-Z]{2}")]

# Model numbers (upper case, no spaces or dashes) of monitors known to use OLED / QD-OLED panels.
OLED_MODELS = {
    "ASUS": ("PG32UCDM", "PG32UCDP", "PG27UCWM", "PG34WCDN", "PG259QWS", "PG27AQDM", "PG27AQDP", "PG34WCDM",
             "PG49WCD", "PG27UCDM", "XG27AQDMG"),
    "LG": ("27GR95QE", "45GR95QE", "32GS95UE", "39GS95QE", "34GS95QE", "39GX950B", "34GX90SB", "32GX870B",
           "45GX950A"),
    "Samsung": ("G80SD", "G81SF", "G60SD", "G61SD", "G85SB", "G95SC", "G93SC"),
    "Dell": ("AW3423DW", "AW3423DWF", "AW3225QF", "AW2725DF", "AW3425DW", "AW3425DWM", "AW2725Q", "AW3426DW",
             "AW3926QW"),
    "MSI": ("MPG321URX", "MPG271QRX", "MPG341CQR", "MPG491CQP", "MPG272URX", "MPG322UR", "MPG271QR", "MAG271QPX",
            "MAG341CQP", "MAG321UPX"),
    "Philips": ("27M2N8500", "34M2C8600", "49M2C8900", "27M4N8500V"),
    "Gigabyte": ("FO27Q3", "FO27Q5P", "FO32U2", "FO32U2P", "FO48U", "MO27Q28", "MO34WQC2"),
    "AOC": ("AG276QZD", "AG456UCZD", "AG326UD"),
}

# Built-in presence (proximity) sensors. "models" are confirmed in manuals, reviews or the maker's pages; the
# "series" text says where else the maker offers them, so the report can tell you to check your exact model.
SENSOR_INFO = {
    "ASUS": {"name": "Neo Proximity Sensor", "models": ("PG259QWS", "PG32UCDM3", "PG27UCWM", "PG34WCDN"),
             "series": "ROG Swift OLED and ProArt OLED monitors",
             "how": "ASUS lets you switch it on from the OLED Care section of the monitor's on-screen menu or "
                    "from ASUS DisplayWidget Center, and adjust how far away it detects you (newer models also "
                    "have Motion Sensitivity and an Auto Away Timer)."},
    "MSI": {"name": "AI Care Sensor", "models": ("MPG341CQR", "MPG271QR", "MPG322UR"),
            "series": "MSI's current QD-OLED monitors",
            "how": "It is OFF by default. In the monitor's menu open AI Navigator > AI Care Sensor and turn it on "
                   "(or use the MSI Gaming Intelligence app). Choose Monitor Mode (works with any PC or console) "
                   "or System Mode (Windows 11 presence sensing), then Wake on Approach, Lock on Leave or "
                   "Adaptive Dim and their timers. It sees you within about a 75 degree cone."},
    "Philips": {"name": "PowerSensor", "models": ("271P4", "272S4", "241B7", "346B1", "49B2U5900C", "49B2U6900CH",
                                                  "27M4N8500V"),
                "series": "many Philips business and Evnia monitors",
                "how": "Turn it on in the monitor's menu under PowerSensor, and if it doesn't notice you, raise "
                       "its setting (0-4, where 4 reaches furthest). The classic PowerSensor detects you roughly "
                       "30-100 cm in front of the screen and only works with the monitor in landscape. Newer "
                       "Evnia models use a camera-style presence sensor instead - check your manual."},
    "Acer": {"name": "proximity sensor", "models": ("PE320QX",), "series": "",
             "how": "Look for the proximity sensor setting in the monitor's on-screen menu."},
}

# What to switch on in the monitor's own on-screen menu (OSD), from the makers' manuals and support pages.
OSD_GUIDE = {
    "LG": {"open": "Open the monitor's menu with its joystick, or press the [OLED Care] button if your model has one.",
           "steps": ["OLED Care > Screen Move: turn it ON. LG's manual recommends it, and on some UltraGear OLED "
                     "models it is OFF by default.",
                     "OLED Care > Screen Saver: turn it ON, so the screen turns off when nothing moves.",
                     "OLED Care > Image Cleaning: run it by hand after a long session (it also starts by itself "
                     "after roughly 4 hours of use, when the monitor is on standby; about 10 minutes on the "
                     "45GR95QE).",
                     "OLED Care > Pixel Cleaning: a short clean (about a minute on the 45GR95QE) - run it if you "
                     "see lines or spots.",
                     "General > Automatic Standby: turn it on, so the monitor sleeps and can run its cleaning."],
           "note": "Menu names and times come from the 27GR95QE and 45GR95QE manuals; other LG models can differ."},
    "Samsung": {"open": "Open the menu (Home button or remote, then Menu), then General & Privacy > Panel Care.",
                "steps": ["Panel Care > Pixel Shift: leave it on (it isn't available on 2024 models).",
                          "Panel Care > Adjust Logo Brightness: set it to High, to dim logos and other static "
                          "bright shapes.",
                          "Panel Care > Pixel Refresh: run it when you stop using the monitor. It powers the screen "
                          "off for over an hour and a white line may show during it. Don't unplug the monitor.",
                          "Screen Optimization runs by itself after about 4 hours of use, once the monitor is off - "
                          "so leave it plugged in."],
                "note": "From Samsung's support pages for Odyssey OLED monitors; options differ by model and year."},
    "ASUS": {"open": "Press the monitor's 5-way joystick to open the menu, then open OLED Care.",
             "steps": ["OLED Care > Screen Saver: turn it on - it dims the screen when the picture stays the same.",
                       "OLED Care > Screen Move: turn it on and pick a movement level (this is pixel shift).",
                       "OLED Care > Pixel Cleaning: run it when you finish for the day (about 6 minutes; it starts "
                       "when the monitor is turned off, so don't unplug it). Set Pixel Cleaning Reminder to 2, 4 "
                       "or 8 hours.",
                       "OLED Care > Auto Logo Brightness, and Uniform Brightness in the Image menu: leave them on to "
                       "dim static logos and keep the brightness even.",
                       "The ASUS DisplayWidget Center program shows the same OLED Care settings on your PC."],
             "note": "From the ROG Swift OLED PG32UCDM and PG32UCDM3 user guides; other ASUS models vary."},
    "MSI": {"open": "Press the monitor's joystick (Navi key) to open the menu, then open OLED Care.",
            "steps": ["OLED Care > Pixel Shift: turn it on (Slow, Normal or Fast).",
                      "OLED Care > Static Screen Detection: turn it on - it dims the picture after it has stayed "
                      "still for a while.",
                      "OLED Care > Taskbar Detection: turn it on - it dims the taskbar area, a good partner for "
                      "this app's taskbar options.",
                      "OLED Care > Multi Logo Detection and Boundary Detection: turn them on to dim logos and "
                      "window edges.",
                      "OLED Care > Panel Protect: it runs by itself after about 4 hours of use when the monitor is "
                      "off or on standby. MSI recommends letting it run, so leave the monitor plugged in."],
            "note": "From MSI's MPG OLED user guides; names vary by model."},
    "Dell": {"open": "Press the joystick at the bottom of the monitor to open the Menu Launcher, then choose Menu.",
             "steps": ["Menu > Others > OLED Panel Maintenance > Pixel Refresh: it runs by itself after about 4 hours "
                       "of use when the monitor goes on standby or off (6 to 8 minutes, the power light blinks "
                       "green). Answer 'No, do it during monitor in Standby' if you're busy.",
                       "Menu > Others > OLED Panel Maintenance > Panel Refresh (if your model has it): a longer clean "
                       "(about an hour) that the monitor asks for after roughly 1,500 hours of use.",
                       "Watch the Panel Health dot in the Menu Launcher: green is fine, yellow suggests a Pixel "
                       "Refresh, red means a refresh is overdue.",
                       "Leave the monitor plugged in, and don't press the power button during a refresh."],
             "note": "From Dell's support pages for the Alienware AW3423DW, AW3423DWF and AW3225QF. Reviews found no "
                     "logo dimming, taskbar detection or screen saver on several of these models, so this app's "
                     "black screen, hidden icons and hidden taskbar matter even more here."},
    "Philips": {"open": "Press the joystick right to open the menu, then go to Setup > OLED Panel Care.",
                "steps": ["OLED Panel Care > Screen Saver: set it to Slow or Fast (never Off).",
                          "OLED Panel Care > Pixel Orbiting: set it to Slow, Normal or Fast (this is pixel shift).",
                          "OLED Panel Care > Pixel Refresh: the monitor asks for it after 4 hours of use (and again "
                          "every 2 hours) and runs it by itself at 16 hours - say yes when you stop for the day, and "
                          "don't unplug it.",
                          "Panel Refresh runs automatically after about 2,000 hours (on the 34M2C8600).",
                          "OLED Panel Care > Auto Warning: leave it on so the monitor reminds you."],
                "note": "From the Philips Evnia 34M2C8600 and 27M2N8500 user manuals; other models can differ."},
    "Gigabyte": {"open": "Press the Control button to open the Main menu, then move it left to open OLED Care.",
                 "steps": ["OLED Care > Settings > Pixel Shift: turn it on.",
                           "OLED Care > Settings > Static Control: turn it on - it lowers the brightness when the "
                           "picture stays still.",
                           "OLED Care > Settings > Sub-Logo Dim and Corner Dim: turn them on to dim logos and the "
                           "four corners.",
                           "OLED Care > Settings > Pixel Clean: Gigabyte recommends running it after more than 4 "
                           "hours of use (Start Cleaning, or Start Time Delay to do it later).",
                           "OLED Care > Status shows how long the screen has been used."],
                 "note": "From the AORUS FO32U2, FO32U2P and FO27Q3 user guide; other models can differ."},
}
GENERIC_OLED_TIPS = [
    "In the monitor's own menu, look for a section called OLED Care, Panel Care, OLED Panel Maintenance or Screen "
    "Maintenance.",
    "Turn on pixel shift (also called Screen Move or Pixel Orbiting) and any screen saver or static-screen dimming.",
    "Run the pixel refresh / pixel cleaning when it asks and whenever you stop using the monitor.",
    "Leave the monitor plugged in, and in standby overnight, so its automatic refresh can run. Don't switch it off "
    "at the wall.",
    "Keep brightness moderate and avoid leaving bright static windows on screen for hours."]

# What this app can do for an OLED screen.
RECOMMENDED_OLED = {"enabled": True, "minutes": 2, "hide_icons": True, "taskbar_while_black": True,
                    "sleep_enabled": True, "sleep_minutes": 10, "auto_pause": True, "call_pause": True,
                    "dark_mode": True}


def parse_edid(data):
    """Read a monitor's EDID block: brand code, model name, size, resolution, refresh rate, year. None if invalid."""
    data = bytes(data)
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    code = (data[8] << 8) | data[9]
    brand_code = "".join(chr(((code >> shift) & 0x1F) + 64) for shift in (10, 5, 0))
    info = {"brand_code": brand_code, "brand": PNP_BRANDS.get(brand_code, ""), "name": "", "serial_text": "",
            "product_code": data[10] | (data[11] << 8), "serial": int.from_bytes(data[12:16], "little"),
            "week": data[16], "year": 1990 + data[17], "width_cm": data[21], "height_cm": data[22],
            "width_px": None, "height_px": None, "hz": None}
    timing_hz = range_hz = None
    for offset in (54, 72, 90, 108):
        block = data[offset: offset + 18]
        if block[0] == 0 and block[1] == 0:  # a descriptor (name, serial, range limits)
            text = block[5:18].split(b"\n")[0].decode("ascii", "ignore").strip()
            if block[3] == 0xFC:
                info["name"] = text
            elif block[3] == 0xFF:
                info["serial_text"] = text
            elif block[3] == 0xFD:
                range_hz = block[6]  # highest vertical rate the monitor accepts
        elif info["width_px"] is None:  # a timing: the first one is the monitor's preferred mode
            clock = (block[0] | (block[1] << 8)) * 10000
            h_active = block[2] | ((block[4] & 0xF0) << 4)
            h_blank = block[3] | ((block[4] & 0x0F) << 8)
            v_active = block[5] | ((block[7] & 0xF0) << 4)
            v_blank = block[6] | ((block[7] & 0x0F) << 8)
            if clock and h_active and v_active:
                info["width_px"], info["height_px"] = h_active, v_active
                total = (h_active + h_blank) * (v_active + v_blank)
                if total:
                    timing_hz = round(clock / total)
    # the basic block can't describe e.g. 4K at 240 Hz, so trust whichever of the two numbers is higher
    info["hz"] = max([hz for hz in (timing_hz, range_hz) if hz] or [None])
    return info


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128)]


user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD]
user32.EnumDisplayDevicesW.restype = wintypes.BOOL


def active_monitor_ids():
    """Windows' ids of the monitors that are switched on right now, e.g. 'MONITOR\\GSM5B09\\{guid}\\0004'."""
    ids = []
    adapter_index = 0
    while True:
        adapter = DISPLAY_DEVICEW()
        adapter.cb = ctypes.sizeof(adapter)
        if not user32.EnumDisplayDevicesW(None, adapter_index, ctypes.byref(adapter), 0):
            break
        adapter_index += 1
        if not adapter.StateFlags & 1:  # not attached to the desktop
            continue
        monitor_index = 0
        while True:
            monitor = DISPLAY_DEVICEW()
            monitor.cb = ctypes.sizeof(monitor)
            if not user32.EnumDisplayDevicesW(adapter.DeviceName, monitor_index, ctypes.byref(monitor), 0):
                break
            monitor_index += 1
            if monitor.StateFlags & 1:  # active
                ids.append(monitor.DeviceID)
    return ids


def read_display_edids():
    """The parsed EDID of every monitor that is connected and switched on right now."""
    try:
        import winreg
    except ImportError:
        return []
    wanted = {}
    for device_id in active_monitor_ids():
        m = re.match(r"MONITOR\\([^\\]+)\\(.+)$", device_id, re.I)
        if m:
            wanted[(m.group(1).upper(), m.group(2).lower())] = device_id
    found, seen = [], set()
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\DISPLAY")
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                model_key = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                model = winreg.OpenKey(root, model_key)
            except OSError:
                continue
            with model:
                j = 0
                while True:
                    try:
                        instance = winreg.EnumKey(model, j)
                    except OSError:
                        break
                    j += 1
                    try:
                        with winreg.OpenKey(model, instance) as inst:
                            driver = str(winreg.QueryValueEx(inst, "Driver")[0]).lower()
                        if wanted and (model_key.upper(), driver) not in wanted:
                            continue  # a monitor that was connected once but isn't now
                        with winreg.OpenKey(model, instance + "\\Device Parameters") as params:
                            edid = winreg.QueryValueEx(params, "EDID")[0]
                    except OSError:
                        continue
                    info = parse_edid(edid)
                    if info and (info["brand_code"], info["product_code"], info["serial"]) not in seen:
                        seen.add((info["brand_code"], info["product_code"], info["serial"]))
                        found.append(info)
    return found


def _norm(text):
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def identify_brand(model_text, edid_brand=""):
    """The brand for a model you typed: from a brand word ('LG', 'ROG'), a known model number, or the pattern of the
    model number. Falls back to the brand Windows reported."""
    words = re.findall(r"[A-Z0-9]+", str(model_text).upper())
    for word in words:
        if word in BRAND_WORDS:
            return BRAND_WORDS[word]
    norm = _norm(model_text)
    for brand, fragments in OLED_MODELS.items():
        if any(_norm(fragment) in norm for fragment in fragments):
            return brand
    for word in words + [norm]:
        for brand, pattern in MODEL_FAMILIES:
            if re.match(pattern, word):
                return brand
    return edid_brand


def display_title(info, model_text=""):
    brand = info.get("brand") or f"Unknown brand ({info.get('brand_code', '?')})"
    name = (model_text or info.get("name") or "").strip()
    if not name:
        return f"{brand} (model code {info.get('product_code', 0):04X})"
    return name if name.upper().startswith(brand.upper()) else f"{brand} {name}"


def classify_panel(info, model_text, mode="auto"):
    """('oled' | 'lcd' | 'unknown', why) for one monitor. `mode` is the 'My screen is' choice."""
    if mode == "oled":
        return "oled", "you said it is an OLED"
    if mode == "lcd":
        return "lcd", "you said it is an LCD"
    model = _norm(model_text or info.get("name", ""))
    if "OLED" in model:
        return "oled", "its name says OLED"
    brand = info.get("brand", "")
    for owner in ([brand] if brand else list(OLED_MODELS)):
        for fragment in OLED_MODELS.get(owner, ()):
            if _norm(fragment) in model:
                return "oled", f"{fragment} is a known OLED model"
    return "unknown", "it isn't in my list of OLED models"


def sensor_status(brand, model_text):
    """('yes' | 'maybe' | 'none', sensor info or None)."""
    entry = SENSOR_INFO.get(brand)
    if not entry:
        return "none", None
    model = _norm(model_text)
    if any(_norm(fragment) in model for fragment in entry["models"]):
        return "yes", entry
    return ("maybe" if entry["series"] else "none"), entry


def recommended_items(settings, taskbar_autohide, screen_count):
    """[(what, is it already on)] - the settings this app suggests for an OLED screen."""
    items = [("The screensaver is switched on", bool(settings["enabled"])),
             ("Black screen after 3 minutes or less of no activity", settings["minutes"] <= 3),
             ("Hide desktop icons while black", bool(settings["hide_icons"])),
             ("Hide the taskbar while black", bool(settings["taskbar_while_black"] or taskbar_autohide)),
             ("Always hide the taskbar (Appearance tab)", bool(taskbar_autohide)),
             ("Turn the display off after a while (While black tab)", bool(settings["sleep_enabled"])),
             ("Don't black out during games, videos and calls", bool(settings["auto_pause"] and settings["call_pause"])),
             ("Dark mode in this app", bool(settings["dark_mode"]))]
    if screen_count > 1:
        items.append(("Black out all screens", bool(settings["black_all_screens"])))
    return items


def recommended_settings(screen_count):
    """The changes 'Apply recommended settings' makes."""
    changes = dict(RECOMMENDED_OLED)
    if screen_count > 1:
        changes["black_all_screens"] = True
    return changes


def osd_lines(brand, confirmed=True):
    """The brand's own on-screen-menu steps, as report lines ([] if I have none for this brand)."""
    guide = OSD_GUIDE.get(brand)
    if not guide:
        return []
    head = (f"ON THE MONITOR ITSELF - {brand}" if confirmed else
            f"ON THE MONITOR ITSELF - the usual {brand} OLED settings (your exact model may use different names)")
    lines = [head, f"  {guide['open']}"]
    lines += [f"  {number}. {step}" for number, step in enumerate(guide["steps"], 1)]
    if guide.get("note"):
        lines.append(f"  ({guide['note']})")
    return lines + [""]


def build_display_report(displays, mode, model_override, settings, taskbar_autohide, screen_count):
    """The text shown on the Display tab."""
    typed = model_override.strip()
    if not displays and typed:  # Windows told us nothing, but you typed a model - work from that
        displays = [{"brand_code": "", "brand": "", "name": "", "product_code": 0, "serial": 0, "year": 1990}]
    if not displays:
        return ("I couldn't read your monitor's details from Windows.\n\n"
                "Type the model number printed on the back of the monitor into the box above and I'll work from "
                "that, or choose OLED or LCD in 'My screen is'.")
    lines, panels = [], []
    if typed:
        lines += [f"Using the model number you typed: {typed} (for display 1)", ""]
    for number, original in enumerate(displays, 1):
        info = dict(original)
        is_typed = number == 1 and bool(typed)
        model_text = typed if is_typed else info.get("name", "")
        source = ""
        if is_typed:
            brand = identify_brand(typed, info.get("brand", ""))
            if brand and brand != info.get("brand", ""):
                source = f" - brand worked out from the model number you typed"
            info["brand"] = brand
        panel, why = classify_panel(info, model_text, mode)
        panels.append((number, info, model_text, panel, is_typed))
        details = []
        if info.get("width_px"):
            details.append(f"{info['width_px']}x{info['height_px']}" + (f" at {info['hz']} Hz" if info.get("hz") else ""))
        if info.get("width_cm") and info.get("height_cm"):
            details.append(f"{(info['width_cm'] ** 2 + info['height_cm'] ** 2) ** 0.5 / 2.54:.0f} inch")
        if info.get("year", 1990) > 1990:
            details.append(f"made {info['year']}")
        lines.append(f"DISPLAY {number}: {display_title(info, model_text)}{source}")
        if details:
            lines.append("  " + ", ".join(details))
        label = {"oled": "OLED - can suffer burn-in", "lcd": "LCD - doesn't suffer burn-in",
                 "unknown": "can't confirm it is OLED"}[panel]
        lines.append(f"  Screen type: {label} ({why})")
        lines.append("")
    kinds = {p[3] for p in panels}
    guide_shown = set()
    if "oled" in kinds:
        lines += ["BURN-IN: OLED screens can keep a faint ghost of things that stay on screen for hours, such as the "
                  "taskbar, icons and window frames. Keeping the screen black while you're away, and hiding static "
                  "parts while you're there, is the best protection.", "",
                  "RECOMMENDED IN THIS APP"]
        lines += [f"  [{'on ' if done else 'off'}] {what}" for what, done in
                  recommended_items(settings, taskbar_autohide, screen_count)]
        lines += ["  Press 'Apply recommended settings' to switch the missing ones on.", ""]
    elif "unknown" in kinds and not typed:
        lines += ["I can't tell from the monitor's name whether it is OLED. Most monitors are LCD, and LCD screens "
                  "don't suffer burn-in. If yours is OLED, choose that in 'My screen is' (or type the model number "
                  "from the back of the monitor above) and I'll show the recommendations and the menu steps.", ""]
    elif "unknown" in kinds:
        lines += ["I can't confirm from the model number that this is an OLED monitor (most monitors are LCD, and LCD "
                  "screens don't suffer burn-in). If it is OLED, choose 'OLED or QD-OLED' in 'My screen is' to see "
                  "this app's recommended settings. The menu steps for the brand are below in case they help.", ""]
    else:
        lines += ["LCD screens don't suffer permanent burn-in, so you don't need the black screen for protection. "
                  "Turning the display off after a while (While black tab) still saves power.", ""]
    for number, info, model_text, panel, is_typed in panels:  # the monitor's own menu, per brand
        brand = info.get("brand", "")
        if brand in guide_shown or panel == "lcd":
            continue
        if panel == "oled" or (panel == "unknown" and is_typed):
            guide = osd_lines(brand, confirmed=(panel == "oled"))
            if guide:
                lines += guide
                guide_shown.add(brand)
            elif panel == "oled":
                shown_name = brand or "this brand"
                lines += [f"I don't have {shown_name}-specific menu steps for your monitor, so follow the general "
                          "guide below and your manual.", ""]
    if "oled" in kinds or (typed and "lcd" not in kinds):
        lines += ["OLED CARE IN GENERAL"] + [f"  - {tip}" for tip in GENERIC_OLED_TIPS] + [""]
    lines.append("PRESENCE SENSOR")
    shown = False
    for number, info, model_text, panel, is_typed in panels:
        status, entry = sensor_status(info.get("brand", ""), model_text)
        title = display_title(info, model_text)
        if status == "yes":
            shown = True
            lines += [f"  Display {number} ({title}) has a built-in sensor - the {entry['name']} - that detects when "
                      "no one is at the screen and then dims or blacks it out. To turn it on and calibrate it, "
                      "follow the instructions for your exact brand and model in its manual.",
                      f"  {entry['how']}", ""]
        elif status == "maybe":
            shown = True
            lines += [f"  Display {number}: {info['brand']} offers a built-in sensor - the {entry['name']} - on "
                      f"{entry['series']}. It detects when no one is at the screen and dims or blacks it out. "
                      "Check whether your exact model has one (look for it in the on-screen menu or the manual). "
                      "If it does, follow the instructions for your exact brand and model to turn it on and "
                      "calibrate it.", f"  {entry['how']}", ""]
    if not shown:
        lines += ["  I don't have information about a presence sensor for this brand or model. If your monitor has "
                  "one, its manual will call it a presence, proximity or human sensor - follow the instructions for "
                  "your exact brand and model to turn it on and calibrate it. This app's idle timer does the same "
                  "job from the PC side.", ""]
    lines.append("This is a guide based on the makers' manuals and reviews. Your monitor's own manual is the final word - "
                 "press 'Look up my manual' to find it.")
    return "\n".join(lines)


# ---------- GUI ----------
class App:
    def __init__(self, root, start_minimized):
        self.root = root
        remove_flag(SHOW_FLAG)  # stale flags from a previous run must not close or re-open anything
        remove_flag(QUIT_FLAG)
        self.s = load_settings()
        if FROZEN and self.s["startup"]:  # make sure "Start with Windows" launches this exe, not the old script
            try:
                set_startup(True)
            except OSError:
                pass
        self.is_black = False
        self.minimised = []
        self.we_dir = None
        self._scan_time = -1e9
        self._game = None
        self.history = []          # [(time first seen, wallpapers)] - only recorded when it changes
        self._cfg_mtime = None
        self._last_sample = -1e9
        self._last_tb = -1e9
        self.test_end = 0.0        # when a 'Test now' run reverts by itself (0 = no test running)
        self.icons_hidden = False      # True while we are hiding the desktop icons
        self.taskbar_forced = False    # True while we are temporarily auto-hiding the taskbar
        self.black_since = 0.0
        self.display_is_off = False
        self.snooze_until = 0.0
        self.tray = None
        self.tray_queue = queue.Queue()
        self._clear_applied = self.s["taskbar_clear"]   # True while the taskbar is being kept transparent
        self._last_clear = -1e9
        self._fade_applied = self.s["taskbar_fade"]   # True while the whole taskbar is faded
        self._save_job = None
        self.scale = 1.0
        self._base_fonts = {}
        self._wraps = []
        self._before = []
        self.stats = load_stats()
        self._sess = {"on": 0.0, "black": 0.0}   # this session's totals
        self._last_stat_t = time.monotonic()
        self._last_stat_save = time.monotonic()
        self._cur_state = "on"
        self._state_start = time.monotonic()
        self._wp_cache = None
        self._wp_loaded = False       # the Wallpaper tab's list is filled the first time you open it
        self._wp_now = None            # what Wallpaper Engine last said the main screen is showing
        self._stop = threading.Event()
        self._remembered_title = "detecting..."
        self._clear_result = "not checked yet"
        self._fade_win = None          # the black overlay used for the gradual fade
        self._fade_start = 0.0
        self._cap_time = -1e9
        self._cap_app = None           # app currently using the microphone or camera
        self._update_result = None     # (message, link, version, manual) from the background update check
        self._health = ["Health check not run yet - press 'Run health check'."]
        self._health_done = False
        self._health_running = False
        self._warn_visible = False
        self._wp_queries = 0           # how many times Wallpaper Engine was asked what it shows (see Details)
        self._wp_last_query = "never"
        self._busy = False             # a game / fullscreen video / call is active
        self._text_cache = {}          # last text written to each label, so unchanged text is never rewritten
        self._last_accent = -1e9
        self._displays = []            # monitors read from Windows (brand, model, ...)
        self._displays_loaded = False
        self._model_job = None         # the pending 'you stopped typing' update
        root.title("OLED Screensaver")
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)  # X = hide, Quit button = exit
        self.build_ui()
        self.apply_scale()
        self.refit()
        self.restore_window_size()
        self.refresh_name()
        root.bind("<Configure>", self.on_resize)
        self.resolve_we()
        self.start_wallpaper_watcher()
        self.restore()  # recover from a previous crash while black
        self.recover_ui()
        self.start_tray()
        if start_minimized:
            self.hide_window()
        log(f"{APP_NAME} {APP_VERSION} started (Wallpaper Engine: {self.we_dir})")
        self.run_health_check()
        if self.s["update_check_startup"] and self.s["update_url"]:
            self.check_updates(manual=False)
        self.tick()

    def build_ui(self):
        f = ttk.Frame(self.root, padding=(16, 12))
        f.pack(fill="both", expand=True)
        self.title_label = ttk.Label(f, text="OLED Screensaver", font=("Segoe UI", 13, "bold"))
        self.title_label.pack(anchor="w")
        self.name_var = tk.StringVar(value="Remembered wallpaper (main screen): detecting...")
        name_row = ttk.Frame(f)
        name_row.pack(fill="x", pady=(4, 8))
        self.name_label = ttk.Label(name_row, textvariable=self.name_var, wraplength=290)
        self.name_label.pack(side="left")
        ttk.Button(name_row, text="Details", command=self.show_details).pack(side="right")
        self.warn_label = ttk.Label(name_row, text="! Problem - see Details", foreground="#e5534b", cursor="hand2")
        self.warn_label.bind("<Button-1>", lambda e: self.show_details())  # only shown when something is wrong

        tabs = ttk.Notebook(f)
        tabs.pack(fill="both", expand=True)
        (main_tab, wall_tab, black_tab, look_tab, display_tab, stats_tab, app_tab, details_tab, about_tab) = (
            ttk.Frame(tabs, padding=12) for _ in range(9))
        tabs.add(main_tab, text="Screensaver")
        tabs.add(wall_tab, text="Wallpaper")
        tabs.add(black_tab, text="While black")
        tabs.add(look_tab, text="Appearance")
        tabs.add(display_tab, text="Display")
        tabs.add(stats_tab, text="Stats")
        tabs.add(app_tab, text="App")
        tabs.add(details_tab, text="Details")
        tabs.add(about_tab, text="About")
        self.tabs, self.wall_tab, self.details_tab, self.stats_tab = tabs, wall_tab, details_tab, stats_tab
        self.display_tab = display_tab

        # ----- Wallpaper tab -----
        self.build_restore_picker(wall_tab)
        self.watch_var = tk.BooleanVar(value=self.s["watch_wallpaper"])
        ttk.Checkbutton(wall_tab, text="Check what Wallpaper Engine shows in the background", variable=self.watch_var,
                        command=self.on_change).pack(anchor="w", pady=(4, 0))
        ttk.Label(wall_tab, text="Turn this off if you notice your screen flickering.").pack(anchor="w")

        # ----- Screensaver tab -----
        self.enabled = tk.BooleanVar(value=self.s["enabled"])
        ttk.Checkbutton(main_tab, text="Enabled", variable=self.enabled, command=self.on_change).pack(anchor="w")
        self.min_label = ttk.Label(main_tab, text="Changes Wallpaper to Black when no activity is detected",
                                   wraplength=330)
        self.min_label.pack(anchor="w", pady=(10, 0))
        self.slider = tk.Scale(main_tab, from_=1, to=10, orient="horizontal", showvalue=True, tickinterval=1,
                               length=200, label="Minutes of no activity", command=self.on_slide)
        self.slider.set(self.s["minutes"])
        self.slider.pack(fill="x")
        self.slider.bind("<ButtonRelease-1>", lambda e: self.on_change())
        self.on_slide(self.s["minutes"])
        self.auto_pause = tk.BooleanVar(value=self.s["auto_pause"])
        ttk.Checkbutton(main_tab, text="Auto-disable while gaming or watching video", variable=self.auto_pause,
                        command=self.on_change).pack(anchor="w", pady=(8, 0))
        ttk.Button(main_tab, text="Add programs that pause it...", command=self.edit_programs).pack(
            anchor="w", pady=(6, 0))
        self.call_var = tk.BooleanVar(value=self.s["call_pause"])
        ttk.Checkbutton(main_tab, text="Also pause during calls (microphone or camera in use)",
                        variable=self.call_var, command=self.on_change).pack(anchor="w", pady=(4, 0))
        fade_row = ttk.Frame(main_tab)
        fade_row.pack(anchor="w", pady=(8, 0))
        self.fade_on = tk.BooleanVar(value=self.s["fade_enabled"])
        ttk.Checkbutton(fade_row, text="Fade to black over", variable=self.fade_on,
                        command=self.on_change).pack(side="left")
        self.fade_secs = tk.IntVar(value=self.s["fade_seconds"])
        fade_spin = ttk.Spinbox(fade_row, from_=1, to=10, width=3, textvariable=self.fade_secs, command=self.on_change)
        fade_spin.pack(side="left", padx=4)
        fade_spin.bind("<FocusOut>", lambda e: self.on_change())
        fade_spin.bind("<Return>", lambda e: self.on_change())
        ttk.Label(fade_row, text="seconds").pack(side="left")
        self.sched_on = tk.BooleanVar(value=self.s["schedule_enabled"])
        ttk.Checkbutton(main_tab, text="Use a schedule", variable=self.sched_on, command=self.on_change).pack(
            anchor="w", pady=(8, 0))
        self.sched_modes = [("different", "Use a different timeout during these hours"),
                            ("only", "Only run during these hours")]
        self.sched_mode_var = tk.StringVar(value=next(
            (label for key, label in self.sched_modes if key == self.s["schedule_mode"]), self.sched_modes[0][1]))
        sched_combo = ttk.Combobox(main_tab, textvariable=self.sched_mode_var, state="readonly",
                                   values=[label for _, label in self.sched_modes])
        sched_combo.pack(fill="x")
        sched_combo.bind("<<ComboboxSelected>>", lambda e: self.on_change())
        times = ttk.Frame(main_tab)
        times.pack(anchor="w", pady=(4, 0))
        ttk.Label(times, text="From").pack(side="left")
        self.sched_start = tk.StringVar(value=self.s["schedule_start"])
        start_entry = ttk.Entry(times, textvariable=self.sched_start, width=6)
        start_entry.pack(side="left", padx=4)
        ttk.Label(times, text="to").pack(side="left")
        self.sched_end = tk.StringVar(value=self.s["schedule_end"])
        end_entry = ttk.Entry(times, textvariable=self.sched_end, width=6)
        end_entry.pack(side="left", padx=4)
        ttk.Label(times, text="  timeout (min)").pack(side="left")
        self.sched_min = tk.IntVar(value=self.s["schedule_minutes"])
        sched_spin = ttk.Spinbox(times, from_=1, to=60, width=3, textvariable=self.sched_min, command=self.on_change)
        sched_spin.pack(side="left", padx=4)
        for widget in (start_entry, end_entry, sched_spin):
            widget.bind("<FocusOut>", lambda e: self.on_change())
            widget.bind("<Return>", lambda e: self.on_change())

        # ----- While black tab -----
        ttk.Label(display_tab, text="Minimise windows on:").pack(anchor="w")
        self.display_options = [("all", "All displays")] + [
            (m["device"], monitor_label(m, i + 1)) for i, m in enumerate(list_monitors())]
        device, label = next((o for o in self.display_options if o[0] == self.s["display"]),
                             self.display_options[0])
        self.s["display"] = device
        self.display_var = tk.StringVar(value=label)
        combo = ttk.Combobox(display_tab, textvariable=self.display_var, state="readonly",
                             values=[lbl for _, lbl in self.display_options])
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", lambda e: self.on_change())
        self.restore_win = tk.BooleanVar(value=self.s["restore_windows"])
        ttk.Checkbutton(black_tab, text="Restore windows when I come back", variable=self.restore_win,
                        command=self.on_change).pack(anchor="w", pady=(6, 0))
        self.icons_var = tk.BooleanVar(value=self.s["hide_icons"])
        ttk.Checkbutton(black_tab, text="Hide desktop icons", variable=self.icons_var,
                        command=self.on_change).pack(anchor="w", pady=(4, 0))
        self.tbblack_var = tk.BooleanVar(value=self.s["taskbar_while_black"])
        ttk.Checkbutton(black_tab, text="Hide the taskbar", variable=self.tbblack_var,
                        command=self.on_change).pack(anchor="w", pady=(4, 0))
        self.all_var = tk.BooleanVar(value=self.s["black_all_screens"])
        ttk.Checkbutton(display_tab, text="Black out all my screens (not just the main one)", variable=self.all_var,
                        command=self.on_change).pack(anchor="w", pady=(4, 0))
        sleep_row = ttk.Frame(black_tab)
        sleep_row.pack(anchor="w", pady=(4, 0))
        self.sleep_on = tk.BooleanVar(value=self.s["sleep_enabled"])
        ttk.Checkbutton(sleep_row, text="Then turn the display off after", variable=self.sleep_on,
                        command=self.on_change).pack(side="left")
        self.sleep_min = tk.IntVar(value=self.s["sleep_minutes"])
        spin = ttk.Spinbox(sleep_row, from_=1, to=240, width=4, textvariable=self.sleep_min, command=self.on_change)
        spin.pack(side="left", padx=4)
        spin.bind("<FocusOut>", lambda e: self.on_change())
        spin.bind("<Return>", lambda e: self.on_change())
        ttk.Label(sleep_row, text="min").pack(side="left")

        # ----- App tab -----
        ttk.Label(app_tab, text="Wallpaper Engine folder (auto-detected):").pack(anchor="w")
        row = ttk.Frame(app_tab)
        row.pack(fill="x")
        self.path_var = tk.StringVar(value=self.s["we_dir"])
        ttk.Entry(row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", command=self.browse).pack(side="left", padx=(6, 0))
        self.startup = tk.BooleanVar(value=self.s["startup"])
        ttk.Checkbutton(app_tab, text="Start with Windows (minimised)", variable=self.startup,
                        command=self.on_change).pack(anchor="w", pady=(10, 0))
        ttk.Label(about_tab, text=f"{APP_NAME}  -  version {APP_VERSION}").pack(anchor="w")
        ttk.Label(about_tab, text=f"Made by {APP_AUTHOR}").pack(anchor="w")
        ttk.Separator(about_tab).pack(fill="x", pady=10)
        io_row = ttk.Frame(about_tab)
        io_row.pack(anchor="w")
        ttk.Button(io_row, text="Export settings", command=self.export_settings).pack(side="left")
        ttk.Button(io_row, text="Import settings", command=self.import_settings).pack(side="left", padx=6)
        ttk.Button(io_row, text="Reset to defaults", command=self.reset_settings).pack(side="left")
        ttk.Separator(about_tab).pack(fill="x", pady=10)
        ttk.Label(about_tab, text="Update address (a web page that lists the latest version):").pack(anchor="w")
        self.update_url = tk.StringVar(value=self.s["update_url"])
        url_entry = ttk.Entry(about_tab, textvariable=self.update_url)
        url_entry.pack(fill="x")
        url_entry.bind("<FocusOut>", lambda e: self.on_change())
        url_entry.bind("<Return>", lambda e: self.on_change())
        up_row = ttk.Frame(about_tab)
        up_row.pack(anchor="w", pady=(4, 0))
        ttk.Button(up_row, text="Check for updates", command=lambda: self.check_updates(manual=True)).pack(side="left")
        self.update_startup = tk.BooleanVar(value=self.s["update_check_startup"])
        ttk.Checkbutton(up_row, text="Check at startup", variable=self.update_startup,
                        command=self.on_change).pack(side="left", padx=6)
        self.update_note = ttk.Label(about_tab, text=f"You have version {APP_VERSION}.", wraplength=330)
        self.update_note.pack(anchor="w", pady=(4, 0))
        ttk.Separator(about_tab).pack(fill="x", pady=10)
        self.install_note = ttk.Label(about_tab, text=self.install_status_text(), wraplength=330)
        self.install_note.pack(anchor="w")
        install_row = ttk.Frame(about_tab)
        install_row.pack(anchor="w", pady=(4, 0))
        install_btn = ttk.Button(install_row, text="Install on this PC", command=self.install_clicked)
        install_btn.pack(side="left")
        uninstall_btn = ttk.Button(install_row, text="Uninstall", command=self.uninstall_clicked)
        uninstall_btn.pack(side="left", padx=6)
        if not FROZEN:  # installing needs the packaged .exe
            install_btn.state(["disabled"])
            uninstall_btn.state(["disabled"])

        # ----- Appearance tab -----
        self.dark = tk.BooleanVar(value=self.s["dark_mode"])
        ttk.Checkbutton(look_tab, text="Dark mode", variable=self.dark, command=self.on_change).pack(anchor="w")
        self.scale_slider = tk.Scale(look_tab, from_=70, to=200, resolution=10, orient="horizontal", showvalue=True,
                                     length=200, label="Text size (%) - makes everything bigger or smaller")
        self.scale_slider.set(self.s["ui_scale"])
        self.scale_slider.pack(fill="x", pady=(8, 0))
        self.scale_slider.bind("<ButtonRelease-1>", lambda e: self.on_scale())
        ttk.Separator(look_tab).pack(fill="x", pady=8)
        self.taskbar = tk.BooleanVar(value=taskbar_hidden())  # ticked if the taskbar is already auto-hidden
        ttk.Checkbutton(look_tab, text="Hide the taskbar (auto-hide)", variable=self.taskbar,
                        command=self.on_taskbar).pack(anchor="w")
        self.clear_var = tk.BooleanVar(value=self.s["taskbar_clear"])
        ttk.Checkbutton(look_tab, text="Make the taskbar background clear", variable=self.clear_var,
                        command=self.on_clear_toggle).pack(anchor="w", pady=(10, 0))
        self.tint = tk.Scale(look_tab, from_=0, to=100, orient="horizontal", showvalue=True, length=200,
                             label="Taskbar tint (0 = crystal clear)")
        self.tint.set(self.s["taskbar_opacity"])
        self.tint.pack(fill="x")
        self.tint.bind("<ButtonRelease-1>", lambda e: self.on_change())
        self.fade_var = tk.BooleanVar(value=self.s["taskbar_fade"])
        ttk.Checkbutton(look_tab, text="Fade the whole taskbar instead (experimental - icons fade too)",
                        variable=self.fade_var, command=self.on_fade_toggle).pack(anchor="w", pady=(10, 0))
        self.fade_level = tk.Scale(look_tab, from_=5, to=100, orient="horizontal", showvalue=True, length=200,
                                   label="Taskbar visibility (%)")
        self.fade_level.set(self.s["taskbar_fade_level"])
        self.fade_level.pack(fill="x")
        self.fade_level.bind("<ButtonRelease-1>", lambda e: self.on_change())
        ttk.Button(look_tab, text="Check if it works", command=self.check_taskbar_clear).pack(
            anchor="w", pady=(6, 0))
        self.clear_note = ttk.Label(look_tab, text="", wraplength=330)
        self.clear_note.pack(anchor="w", pady=(4, 0))

        # ----- Stats tab -----
        self.stats_now = ttk.Label(stats_tab, text="", wraplength=330)
        self.stats_now.pack(anchor="w")
        self.stats_view = ttk.Label(stats_tab, text="", font="TkFixedFont", justify="left")
        self.stats_view.pack(anchor="w", pady=(8, 0))
        self.stats_note = ttk.Label(stats_tab, wraplength=330,
                                    text="Counts only while this app is running. 'Black' includes the time the "
                                         "display is switched off.")
        self.stats_note.pack(anchor="w", pady=(8, 0))
        ttk.Button(stats_tab, text="Reset statistics", command=self.reset_stats).pack(anchor="w", pady=(8, 0))

        # ----- Display tab: monitor brand and model, burn-in advice, presence sensor -----
        self.panel_modes = [("auto", "Detect automatically"), ("oled", "OLED or QD-OLED"),
                            ("lcd", "LCD (IPS, VA or TN)")]
        ttk.Separator(display_tab).pack(fill="x", pady=8)
        panel_row = ttk.Frame(display_tab)
        panel_row.pack(fill="x")
        ttk.Label(panel_row, text="My screen is:").pack(side="left")
        self.panel_var = tk.StringVar(value=next(
            (label for key, label in self.panel_modes if key == self.s["panel_type"]), self.panel_modes[0][1]))
        panel_combo = ttk.Combobox(panel_row, textvariable=self.panel_var, state="readonly",
                                   values=[label for _, label in self.panel_modes])
        panel_combo.pack(side="left", fill="x", expand=True, padx=6)
        panel_combo.bind("<<ComboboxSelected>>", lambda e: self.on_display_option())
        model_row = ttk.Frame(display_tab)
        model_row.pack(fill="x", pady=(4, 0))
        ttk.Label(model_row, text="Model number (optional):").pack(side="left")
        self.display_model_var = tk.StringVar(value=self.s["display_model"])
        model_entry = ttk.Entry(model_row, textvariable=self.display_model_var)
        model_entry.pack(side="left", fill="x", expand=True, padx=6)
        model_entry.bind("<FocusOut>", lambda e: self.on_display_option())
        model_entry.bind("<Return>", lambda e: self.on_display_option())
        self.display_model_var.trace_add("write", self._model_typed)  # update while you type, no need to press Enter
        ttk.Button(model_row, text="Find instructions", command=self.on_display_option).pack(side="left")
        text_holder = ttk.Frame(display_tab)
        text_holder.pack(fill="both", expand=True, pady=(8, 0))
        self.display_text = tk.Text(text_holder, wrap="word", height=14, font="TkDefaultFont", relief="flat",
                                    highlightthickness=0)
        display_scroll = ttk.Scrollbar(text_holder, orient="vertical", command=self.display_text.yview)
        self.display_text.configure(yscrollcommand=display_scroll.set)
        self.display_text.pack(side="left", fill="both", expand=True)
        display_scroll.pack(side="right", fill="y")
        display_row = ttk.Frame(display_tab)
        display_row.pack(fill="x", pady=(8, 0))
        ttk.Button(display_row, text="Detect again", command=self.refresh_display_info).pack(side="left")
        ttk.Button(display_row, text="Look up my manual", command=self.lookup_manual).pack(side="left", padx=6)
        ttk.Button(display_row, text="Apply recommended settings", command=self.apply_recommended).pack(side="left")

        # ----- Details tab (shown right here - no pop-up window) -----
        self.details_text = tk.Text(details_tab, wrap="word", height=12, font="TkFixedFont", relief="flat",
                                    highlightthickness=0)
        self.details_text.pack(fill="both", expand=True)
        details_row = ttk.Frame(details_tab)
        details_row.pack(fill="x", pady=(8, 0))
        ttk.Button(details_row, text="Refresh", command=self.refresh_details).pack(side="left")
        ttk.Button(details_row, text="Copy to clipboard", command=self.copy_details).pack(side="left", padx=6)
        ttk.Button(details_row, text="Run health check", command=self.run_health_check).pack(side="left")
        ttk.Button(details_row, text="Open log", command=self.open_log).pack(side="left", padx=6)

        # ----- always visible -----
        self.status = ttk.Label(f, text="", wraplength=360)
        self.status.pack(anchor="w", pady=(10, 0))
        bottom = ttk.Frame(f)
        bottom.pack(fill="x", pady=(8, 0))
        self.test_btn = ttk.Button(bottom, text="Test now", command=self.test_now)
        self.test_btn.pack(side="left")
        self.snooze_btn = ttk.Button(bottom, text="Snooze 1h", command=self.toggle_snooze)
        self.snooze_btn.pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="Quit", command=self.quit).pack(side="right")
        self.credit = ttk.Label(f, text=f"Made by Delay  -  v{APP_VERSION}")
        self.credit.pack(pady=(10, 0))
        self.combo = combo
        self._wraps = [(self.min_label, 330), (self.name_label, 290), (self.status, 360),
                       (self.clear_note, 330), (self.stats_note, 330), (self.wp_help, 330),
                       (self.wp_current_label, 330), (self.update_note, 330),
                       (self.install_note, 330)]
        self.apply_theme()
        tabs.bind("<<NotebookTabChanged>>", self.on_tab_changed)

    def apply_theme(self):
        t = self.theme = THEMES["dark" if self.dark.get() else "light"]
        style = ttk.Style(self.root)
        style.theme_use("clam")  # the only built-in theme that allows full recolouring
        self.root.configure(bg=t["bg"])
        style.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"],
                        bordercolor=t["trough"], lightcolor=t["bg"], darkcolor=t["bg"], troughcolor=t["trough"],
                        focuscolor=t["bg"])
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        style.map("TCheckbutton", background=[("active", t["bg"])], foreground=[("active", t["fg"])],
                  indicatorcolor=[("selected", t["good"]), ("!selected", t["field"])])
        style.configure("TButton", background=t["button"], foreground=t["fg"], borderwidth=1)
        style.map("TButton", background=[("active", t["trough"])])
        style.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"], insertcolor=t["fg"])
        style.configure("TCombobox", fieldbackground=t["field"], background=t["button"], foreground=t["fg"],
                        arrowcolor=t["fg"], selectbackground=t["field"], selectforeground=t["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", t["field"])], foreground=[("readonly", t["fg"])],
                  selectbackground=[("readonly", t["field"])], selectforeground=[("readonly", t["fg"])])
        # the dropdown list of the combobox is a plain Tk listbox
        for opt, val in (("background", t["field"]), ("foreground", t["fg"]),
                         ("selectBackground", t["trough"]), ("selectForeground", t["fg"])):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)
        try:
            popdown = f"{self.combo}.popdown.f.l"
            self.root.tk.call(popdown, "configure", "-background", t["field"], "-foreground", t["fg"],
                              "-selectbackground", t["trough"], "-selectforeground", t["fg"])
        except tk.TclError:
            pass  # popdown not created yet - the option_add above covers it
        for scale in (self.slider, self.scale_slider, self.tint, self.fade_level):
            scale.configure(bg=t["bg"], fg=t["fg"], troughcolor=t["trough"], activebackground=t["button"],
                            highlightbackground=t["bg"])
        self.name_label.configure(foreground=t["good"])
        style.configure("TNotebook", background=t["bg"], bordercolor=t["trough"], lightcolor=t["bg"],
                        darkcolor=t["bg"])
        style.configure("TNotebook.Tab", background=t["button"], foreground=t["fg"], padding=(6, 5),
                        bordercolor=t["trough"], lightcolor=t["button"], darkcolor=t["button"])
        style.map("TNotebook.Tab", background=[("selected", t["field"]), ("active", t["trough"])],
                  foreground=[("selected", t["fg"])])
        style.configure("TSpinbox", fieldbackground=t["field"], foreground=t["fg"], background=t["button"],
                        arrowcolor=t["fg"], insertcolor=t["fg"], bordercolor=t["trough"])
        self.status.configure(foreground=t["muted"])
        self.clear_note.configure(foreground=t["muted"])
        self.stats_note.configure(foreground=t["muted"])
        self.credit.configure(foreground=t["muted"])
        self.update_note.configure(foreground=t["muted"])
        self.install_note.configure(foreground=t["muted"])
        self.wp_current_label.configure(foreground=t["good"])
        self.wp_listbox.configure(bg=t["field"], fg=t["fg"], selectbackground=t["trough"],
                                  selectforeground=t["fg"])
        self.details_text.configure(bg=t["field"], fg=t["fg"], insertbackground=t["fg"])
        self.display_text.configure(bg=t["field"], fg=t["fg"], insertbackground=t["fg"])
        set_titlebar_dark(self.root, self.dark.get())

    def on_taskbar(self):
        want = self.taskbar.get()
        try:
            set_taskbar_hidden(want)
            time.sleep(0.3)  # give Explorer a moment to apply it
        except Exception as e:
            self.set_status(f"Couldn't change the taskbar: {e}")
        actual = taskbar_hidden()
        self.taskbar.set(actual)  # always show what Windows really has
        if actual != want:
            self.set_status("Windows didn't change the taskbar setting.")

    def on_slide(self, value):
        m = int(float(value))
        self.min_label.config(text="Changes Wallpaper to Black when no activity is detected")

    def on_change(self):
        device = next((d for d, lbl in self.display_options if lbl == self.display_var.get()), "all")
        sched_start = self._time_from(self.sched_start, self.s["schedule_start"])
        sched_end = self._time_from(self.sched_end, self.s["schedule_end"])
        self.s.update(minutes=int(self.slider.get()), enabled=self.enabled.get(),
                      we_dir=self.path_var.get().strip(), startup=self.startup.get(),
                      display=device, restore_windows=self.restore_win.get(),
                      auto_pause=self.auto_pause.get(), dark_mode=self.dark.get(),
                      hide_icons=self.icons_var.get(), taskbar_while_black=self.tbblack_var.get(),
                      sleep_enabled=self.sleep_on.get(), sleep_minutes=self._sleep_minutes(),
                      taskbar_clear=self.clear_var.get(), taskbar_opacity=int(self.tint.get()),
                      taskbar_fade=self.fade_var.get(), taskbar_fade_level=int(self.fade_level.get()),
                      call_pause=self.call_var.get(), fade_enabled=self.fade_on.get(),
                      fade_seconds=self._int_from(self.fade_secs, 1, 10, self.s["fade_seconds"]),
                      schedule_enabled=self.sched_on.get(), schedule_mode=self._sched_mode_key(),
                      schedule_start=sched_start, schedule_end=sched_end,
                      schedule_minutes=self._int_from(self.sched_min, 1, 60, self.s["schedule_minutes"]),
                      black_all_screens=self.all_var.get(), update_url=self.update_url.get().strip(),
                      update_check_startup=self.update_startup.get(), watch_wallpaper=self.watch_var.get(),
                      panel_type=self._panel_mode_key(), display_model=self.display_model_var.get().strip()[:60])
        if not self.s["watch_wallpaper"]:
            self._wp_now = None  # fall back to Wallpaper Engine's settings file
        save_settings(self.s)
        self.apply_theme()
        self.apply_taskbar_clear()
        try:
            set_startup(self.s["startup"])
        except OSError as e:
            self.set_status(f"Couldn't change startup setting: {e}")
        if not self.enabled.get() and self.is_black:
            self.end_black()

    def browse(self):
        folder = filedialog.askdirectory(title="Select the wallpaper_engine folder")
        if folder:
            self.path_var.set(folder)
            self.on_change()
            self.resolve_we()

    def resolve_we(self):
        self.we_dir = find_we_dir(self.path_var.get().strip())
        if self.we_dir:
            try:
                ensure_black_project(self.we_dir)
            except OSError as e:
                self.set_status(f"Can't write black wallpaper: {e}")
                self.we_dir = None
        self._wp_cache = None
        if self._wp_loaded:
            self._refill_wallpapers()

    def restore(self):
        if not (STATE_FILE.exists() and self.we_dir):
            return
        try:
            items = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(items, dict):  # left over from a newer test build
                items = [[items.get("monitor"), items["file"]]]
            exe = we_exe(self.we_dir)
            connected = len(list_monitors()) or 99
            # numbered screens that aren't plugged in are skipped; screens named by a location id are always tried
            items = [i for i in items if not (isinstance(i[0], int) and i[0] >= connected)]
            # other screens first, the main screen (no id) last, so a wallpaper you picked always ends up on top
            for selector, file in sorted(items, key=lambda i: i[0] is None):
                open_wallpaper(exe, file, selector)
            log(f"restored {items}")
        except (OSError, subprocess.SubprocessError):
            log_error("restoring the wallpaper (Wallpaper Engine didn't answer - will retry at next start)")
            return  # keep the saved state so a later attempt can still put the wallpaper back
        except (ValueError, KeyError, IndexError, TypeError):
            log_error("reading the saved wallpaper (the file was damaged and has been discarded)")
        STATE_FILE.unlink(missing_ok=True)

    def show_details(self):
        """Switch to the Details tab (no pop-up window) and refresh what it shows."""
        self.tabs.select(self.details_tab)
        self.refresh_details()

    def on_tab_changed(self, _event=None):
        selected = self.tabs.select()
        if selected == str(self.details_tab):
            self.refresh_details()
        elif selected == str(self.stats_tab):
            self.update_stats_view(force=True)
        elif selected == str(self.display_tab):
            if self._displays_loaded:
                self.render_display_report()  # what is switched on in the app may have changed
            else:
                self.refresh_display_info()
        elif selected == str(self.wall_tab) and not self._wp_loaded:
            self._wp_loaded = True
            self._refill_wallpapers()

    def refresh_details(self):
        try:
            mons = ", ".join(monitor_label(m, i + 1) for i, m in enumerate(list_monitors()))
            lines = [f"{APP_NAME} {APP_VERSION} - made by {APP_AUTHOR}", "", "Health check:"] + list(self._health) + [
                "", f"Displays: {mons}", f"Wallpaper Engine folder: {self.we_dir}",
                f"Windows build: {windows_build()}",
                f"Taskbar effect: {'clear' if self._clear_applied else 'fade' if self._fade_applied else 'off'} "
                f"(last check: {self._clear_result})", "Taskbar windows:"] + describe_taskbar() + [""]
            header = len(lines)
            for e in (read_entries(self.we_dir) if self.we_dir else []):
                lines += [f"[{e['key'] or 'single'}] {e['title']}", f"  file: {e['file']}",
                          f"  config: {json.dumps(e['raw'])[:500]}", ""]
            if len(lines) <= header:
                lines.append("No selected wallpapers found in config.json")
            choice = self.s["restore_choice"]
            saved_line = (f"{choice['title']}  ({choice['file']})"
                          if self.s["restore_mode"] == "fixed" and choice else "Automatic")
            lines += ["Saved in this app as the wallpaper to go back to: " + saved_line,
                      "Wallpaper Engine reports it is showing (main screen): " + str(self._wp_now or "(no answer yet)"),
                      f"Times Wallpaper Engine was asked: {self._wp_queries} (last at {self._wp_last_query})",
                      "", "Will restore:",
                      json.dumps(self.apply_restore_choice(self.wallpaper_before(LOOKBACK_SECONDS)))]
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-12:] if LOG_FILE.exists() else []
            lines += ["", "Recent log (newest last):"] + (tail or ["(nothing logged yet)"])
        except Exception as ex:
            log_error("building the details")
            lines = [f"Error: {ex}"]
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", "\n".join(lines))

    def copy_details(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.details_text.get("1.0", "end"))

    def start_wallpaper_watcher(self):
        threading.Thread(target=self._watch_wallpaper, daemon=True).start()

    def _watch_once(self, state):
        """Ask Wallpaper Engine what it is showing - but only when needed. Starting its command-line program is the
        one thing here that can disturb the screen, so it happens only when its settings file changed or every two
        minutes, never while a game, video or call is active, and never if you switched it off in the Wallpaper tab.
        Returns True if it asked."""
        if not (self.s["watch_wallpaper"] and self.we_dir) or self.is_black or self._busy:
            return False
        try:
            mtime = (self.we_dir / "config.json").stat().st_mtime
        except OSError:
            mtime = None
        now = time.monotonic()
        if mtime == state.get("mtime") and now - state.get("asked", -1e9) < 120:
            return False
        state["mtime"], state["asked"] = mtime, now
        exe = we_exe(self.we_dir)
        path = get_current_wallpaper(exe) if exe else None
        self._wp_queries += 1
        self._wp_last_query = time.strftime("%H:%M:%S")
        if path:
            self._wp_now = path
        return True

    def _watch_wallpaper(self):
        """Background thread (never touches the window)."""
        state = {}
        while True:
            try:
                self._watch_once(state)
            except Exception:
                state["errors"] = state.get("errors", 0) + 1
                if state["errors"] <= 3:  # don't flood the log if it keeps failing
                    log_error("watching the wallpaper")
            if self._stop.wait(3):
                break

    def sample_wallpaper(self):
        """Remember what Wallpaper Engine is showing on the main screen (recorded only when it changes)."""
        if not self.we_dir:
            return
        main = None
        if self._wp_now:  # the watcher asked Wallpaper Engine itself
            main = [[None, normalize_wallpaper_path(self._wp_now)]]
        else:  # no answer from Wallpaper Engine yet - fall back to its config file
            try:
                mtime = (self.we_dir / "config.json").stat().st_mtime
                if mtime == self._cfg_mtime:
                    return  # config unchanged, so the wallpaper is too
                current = current_wallpapers(self.we_dir)
            except Exception:
                return  # file busy or mid-write - try again next time
            self._cfg_mtime = mtime
            if current:
                main = [[None, current[0][1]]]
        if not main or BLACK_NAME in str(main[0][1]):
            return  # nothing found, or it is our own black wallpaper
        if not self.history or self.history[-1][1] != main:
            self.history.append((time.monotonic(), main))
            del self.history[:-50]
            self._remembered_title = wallpaper_title(self.we_dir, main[0][1])
            self.refresh_name()

    def wallpaper_before(self, seconds):
        """The wallpaper that was showing `seconds` ago (or the earliest one known)."""
        cutoff = time.monotonic() - seconds
        chosen = None
        for seen_at, wallpapers in self.history:
            if seen_at > cutoff:
                break
            chosen = wallpapers
        if chosen is None and self.history:
            chosen = self.history[0][1]
        return chosen

    def collect_screens(self, exe):
        """Every screen Wallpaper Engine knows about and what each shows right now: [[selector, path], ...]."""
        found = []
        try:
            for screen in screen_selectors(self.we_dir):
                path = get_current_wallpaper(exe, selector=screen["selector"]) or screen["file"]
                path = normalize_wallpaper_path(path)
                if BLACK_NAME not in path:
                    found.append([screen["selector"], path])
        except Exception:
            log_error("reading all screens")
            return None
        return found or None

    def go_black(self):
        self.sample_wallpaper()
        exe = we_exe(self.we_dir)
        if not exe:
            return False
        targets = [None]  # None = Wallpaper Engine's main (first) screen
        saved = self.wallpaper_before(LOOKBACK_SECONDS)
        if self.s["black_all_screens"]:
            screens = self.collect_screens(exe)
            if screens:
                saved, targets = screens, [w[0] for w in screens]
        if not saved:  # nothing recorded yet - ask Wallpaper Engine right now
            now = get_current_wallpaper(exe)
            if now:
                saved = [[None, normalize_wallpaper_path(now)]]
            else:
                try:
                    current = [w for w in current_wallpapers(self.we_dir) if BLACK_NAME not in str(w[1])]
                except Exception:
                    current = []
                saved = [[None, current[0][1]]] if current else None
        saved = self.apply_restore_choice(saved)
        if not saved:
            return False
        STATE_FILE.write_text(json.dumps(saved), encoding="utf-8")
        black = self.we_dir / "projects/myprojects" / BLACK_NAME / "project.json"
        log(f"going black on {list(dict.fromkeys(targets))}; will restore {saved}")
        for target in dict.fromkeys(targets):  # each screen once; with no id only the main screen is changed
            open_wallpaper(exe, black, target)
        self.minimised = minimise_windows(self.s["display"])
        self.hide_extras()
        return True

    def end_black(self):
        self.cancel_fade()
        self.restore()
        self.show_extras()
        if self.s["restore_windows"]:
            restore_windows(self.minimised)
        self.minimised = []
        self.is_black = False
        self.test_end = 0.0
        self.test_btn.config(text="Test now")

    def test_now(self):
        """Go black for TEST_SECONDS, then revert by itself. Pressing it again ('Revert now') reverts immediately."""
        if self.is_black:
            if self.test_end:          # a test is running - revert right away
                self.end_black()
            return
        if not self.we_dir:
            self.set_status("Wallpaper Engine not found - use Browse.")
            return
        try:
            self.sample_wallpaper()
            if self.go_black():
                self.is_black = True
                self.test_end = time.monotonic() + TEST_SECONDS
                self.test_btn.config(text="Revert now")
            else:
                self.set_status("Couldn't read current wallpaper from config.json - see Details.")
        except Exception as e:
            self.set_status(f"Error: {e}")

    # ----- screen time recorder -----
    def record_time(self, now):
        """Add the time since the last tick to 'on' or 'black'."""
        dt = now - self._last_stat_t
        self._last_stat_t = now
        state = "black" if self.is_black else "on"
        if state != self._cur_state:
            self._cur_state = state
            self._state_start = now
        if 0 < dt <= 10:  # ignore big gaps (the PC was asleep or the app was frozen)
            day = self.stats["days"].setdefault(time.strftime("%Y-%m-%d"), {"on": 0.0, "black": 0.0})
            day[state] += dt
            self._sess[state] += dt
        if now - self._last_stat_save >= 60:
            self._last_stat_save = now
            self.save_stats()
        self.update_stats_view(now)

    def update_stats_view(self, now=None, force=False):
        if not force and self.tabs.select() != str(self.stats_tab):
            return  # nobody is looking at the Stats tab, so don't redraw it every second
        now = time.monotonic() if now is None else now
        since = format_duration(now - self._state_start)
        self.set_text(self.stats_now, f"Screen has been {'black' if self._cur_state == 'black' else 'on'} for {since}")
        days = self.stats["days"]
        zero = {"on": 0.0, "black": 0.0}
        today = days.get(time.strftime("%Y-%m-%d"), zero)
        week_on = week_black = 0.0
        for i in range(7):
            d = days.get(time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)), zero)
            week_on += d["on"]
            week_black += d["black"]
        all_on = sum(d["on"] for d in days.values())
        all_black = sum(d["black"] for d in days.values())

        def row(label, on, black):
            share = f"{black / (on + black) * 100:.0f}%" if on + black else "-"
            return f"{label:<13}{format_duration(on):>9}{format_duration(black):>10}{share:>7}"

        lines = [f"{'':<13}{'On':>9}{'Black':>10}{'Black%':>7}",
                 row("This session", self._sess["on"], self._sess["black"]),
                 row("Today", today["on"], today["black"]),
                 row("Last 7 days", week_on, week_black),
                 row("All recorded", all_on, all_black), ""]
        for i in range(7):  # newest first
            ts = time.time() - i * 86400
            d = days.get(time.strftime("%Y-%m-%d", time.localtime(ts)))
            if d:
                lines.append(row(time.strftime("%a %d %b", time.localtime(ts)), d["on"], d["black"]))
        self.set_text(self.stats_view, "\n".join(lines))

    def save_stats(self):
        days = self.stats["days"]
        for old in sorted(days)[:-90]:  # keep about three months
            days.pop(old, None)
        try:
            STATS_FILE.write_text(json.dumps({"days": {k: {"on": round(v["on"]), "black": round(v["black"])}
                                                      for k, v in days.items()}}), encoding="utf-8")
        except OSError:
            pass

    def reset_stats(self):
        if messagebox.askyesno("Reset statistics", "Delete all recorded screen time?"):
            self.stats = {"days": {}}
            self._sess = {"on": 0.0, "black": 0.0}
            self.save_stats()
            self.update_stats_view()

    # ----- choosing which wallpaper to go back to -----
    def set_text(self, widget, text):
        """Change a label's or button's text only when it really changed. Rewriting identical text every second
        makes Tk redraw the widget again and again, which can show up as flicker."""
        key = str(widget)
        if self._text_cache.get(key) != text:
            self._text_cache[key] = text
            widget.config(text=text)

    def set_status(self, text):
        self.set_text(self.status, text)

    def refresh_name(self):
        choice = self.s["restore_choice"]
        if self.s["restore_mode"] == "fixed" and choice:
            self.name_var.set("Goes back to: " + choice["title"])
        else:
            self.name_var.set("Remembered wallpaper (main screen): " + self._remembered_title)

    def list_wallpapers(self, refresh=False):
        """[(title, project.json path), ...] for your own, default and Workshop wallpapers."""
        if self._wp_cache is not None and not refresh:
            return self._wp_cache
        found = []
        if self.we_dir:
            roots = [self.we_dir / "projects" / "myprojects", self.we_dir / "projects" / "defaultprojects",
                     self.we_dir.parent.parent / "workshop" / "content" / "431960"]
            for root in roots:
                try:
                    for project in root.glob("*/project.json"):
                        if project.parent.name == BLACK_NAME:
                            continue
                        try:
                            title = json.loads(project.read_text(encoding="utf-8-sig")).get("title")
                        except (OSError, ValueError):
                            continue
                        found.append((str(title or project.parent.name), project))
                except OSError:
                    continue
        found.sort(key=lambda w: w[0].lower())
        self._wp_cache = found
        return found

    def choose_wallpaper(self, path):
        """Show the picked wallpaper now. If the screen is black at the moment, use it when the black ends."""
        if self.is_black:
            try:
                STATE_FILE.write_text(json.dumps([[None, str(path)]]), encoding="utf-8")
            except OSError:
                pass
            return "  (it will appear when the black screen ends)"
        exe = we_exe(self.we_dir) if self.we_dir else None
        if not exe:
            return "  (Wallpaper Engine not found)"
        try:
            open_wallpaper(exe, path, None)  # no monitor number = Wallpaper Engine's first (main) monitor
        except Exception as e:
            return f"  (couldn't apply it: {e})"
        self._wp_now = str(path)
        return "  - applied"

    def apply_restore_choice(self, saved):
        """If you picked a specific wallpaper to return to, that is what the main screen goes back to."""
        choice = self.s["restore_choice"]
        if self.s["restore_mode"] != "fixed" or not choice or not Path(choice["file"]).exists():
            return saved
        # with 'all screens' the other screens keep their own wallpapers; the main screen (no id) is restored last
        others = [list(w) for w in (saved or []) if w[0] is not None] if self.s["black_all_screens"] else []
        return others + [[None, choice["file"]]]

    def build_restore_picker(self, win):
        """The wallpaper chooser, built into the given tab."""
        box = ttk.Frame(win)
        box.pack(fill="both", expand=True)
        self.wp_help = ttk.Label(box, wraplength=330,
                                 text="Click a wallpaper to apply it now. It also becomes the one your screen "
                                      "goes back to after the black screen.")
        self.wp_help.pack(anchor="w")
        current = tk.StringVar()
        self.wp_current_label = ttk.Label(box, textvariable=current, wraplength=330)
        self.wp_current_label.pack(anchor="w", pady=(4, 0))
        search = tk.StringVar()
        ttk.Entry(box, textvariable=search).pack(fill="x", pady=(6, 0))
        holder = ttk.Frame(box)
        holder.pack(fill="both", expand=True, pady=6)
        listbox = tk.Listbox(holder, height=9, highlightthickness=0, relief="flat", exportselection=False)
        self.wp_listbox = listbox
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        shown = []
        pending = []  # the delayed 'apply' job, so scrolling with the arrow keys doesn't apply every step

        def show_current(note=""):
            choice = self.s["restore_choice"]
            fixed = self.s["restore_mode"] == "fixed" and choice
            current.set("Currently: " + (choice["title"] if fixed else "Automatic (the wallpaper that was on screen)")
                        + note)

        def refill(*_):
            query = search.get().strip().lower()
            shown[:] = [w for w in self.list_wallpapers() if query in w[0].lower()]
            listbox.delete(0, "end")
            for title, _path in shown:
                listbox.insert("end", title)

        def use_selected():
            pending.clear()
            try:
                picked = listbox.curselection()
            except tk.TclError:
                return  # the window was closed meanwhile
            if not picked or picked[0] >= len(shown):
                return
            title, path = shown[picked[0]]
            self.s["restore_mode"] = "fixed"
            self.s["restore_choice"] = {"title": title, "file": str(path)}
            save_settings(self.s)
            note = self.choose_wallpaper(path)
            show_current(note)
            self.refresh_name()
            if note.endswith("applied"):
                win.after(3000, lambda: verify(title, path))  # give Wallpaper Engine a moment to save its config

        def verify(title, path):
            """Ask Wallpaper Engine itself whether it really switched to this wallpaper."""
            choice = self.s["restore_choice"]
            if not choice or choice["file"] != str(path):
                return  # you've clicked something else since
            try:
                exe = we_exe(self.we_dir)
                now = get_current_wallpaper(exe) if exe else None
                if now is None:
                    show_current("  - applied (Wallpaper Engine didn't answer the check)")
                elif Path(now).parent.name == Path(str(path)).parent.name:
                    show_current("  - applied and confirmed by Wallpaper Engine")
                else:
                    show_current("  - sent to Wallpaper Engine, but it reports "
                                 f"'{wallpaper_title(self.we_dir, now)}' on the main screen")
            except Exception:
                pass

        def on_select(_event=None):
            if pending:
                win.after_cancel(pending.pop())
            pending.append(win.after(500, use_selected))

        listbox.bind("<<ListboxSelect>>", on_select)

        def use_auto():
            self.s["restore_mode"] = "auto"
            save_settings(self.s)
            show_current()
            self.refresh_name()

        search.trace_add("write", refill)
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Button(row, text="Automatic", command=use_auto).pack(side="left")
        ttk.Button(row, text="Refresh list", command=lambda: (self.list_wallpapers(refresh=True), refill())
                   ).pack(side="left", padx=6)
        show_current()
        self._refill_wallpapers = refill  # filled the first time the Wallpaper tab is opened

    # ----- schedule, fade, settings file, updates, health check, log -----
    def _int_from(self, var, low, high, fallback):
        try:
            return max(low, min(high, int(var.get())))
        except (ValueError, tk.TclError):
            return fallback

    def _time_from(self, var, fallback):
        text = var.get().strip()
        if parse_hhmm(text) is None:  # not a valid time - put the old one back
            var.set(fallback)
            return fallback
        return text

    def _sched_mode_key(self):
        return next((key for key, label in self.sched_modes if label == self.sched_mode_var.get()), "different")

    def schedule_info(self):
        """(allowed, timeout in seconds) after applying the schedule."""
        limit = int(self.slider.get()) * 60
        if not self.s["schedule_enabled"]:
            return True, limit
        now = time.localtime()
        inside = in_time_window(now.tm_hour * 60 + now.tm_min, parse_hhmm(self.s["schedule_start"]),
                                parse_hhmm(self.s["schedule_end"]))
        if self.s["schedule_mode"] == "only":
            return inside, limit
        return True, (int(self.s["schedule_minutes"]) * 60 if inside else limit)

    def start_fade(self):
        """Dim the main screen with a black overlay; when it is fully black the real black wallpaper is applied."""
        try:
            mons = list_monitors()
            mon = next((m for m in mons if m["primary"]), mons[0])
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.configure(bg="black")
            win.geometry(f"{mon['w']}x{mon['h']}+{mon['x']}+{mon['y']}")
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.0)
        except Exception:
            log_error("starting the fade")
            self.cancel_fade()
            return False
        self._fade_win = win
        self._fade_start = time.monotonic()
        self.root.after(50, self._fade_step)
        return True

    def _fade_step(self):
        win = self._fade_win
        if win is None:
            return
        try:
            seconds = self._int_from(self.fade_secs, 1, 10, self.s["fade_seconds"])
            progress = (time.monotonic() - self._fade_start) / seconds
            if progress > 0.05 and idle_seconds() < WAKE_THRESHOLD:  # you touched something - stop dimming
                self.cancel_fade()
                return
            win.attributes("-alpha", min(1.0, progress))
            if progress < 1.0:
                self.root.after(50, self._fade_step)
                return
            if self.go_black():  # fully black: switch the real wallpaper, then drop the overlay once it has caught up
                self.is_black = True
                self.root.after(1500, self.cancel_fade)
            else:
                self.cancel_fade()
        except Exception:
            log_error("fading to black")
            self.cancel_fade()

    def cancel_fade(self):
        win, self._fade_win = self._fade_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def export_settings(self):
        path = filedialog.asksaveasfilename(title="Export settings", defaultextension=".json",
                                            initialfile="oled-screensaver-settings.json",
                                            filetypes=[("JSON files", "*.json")])
        if not path:
            return
        data = {k: v for k, v in self.s.items() if k not in PORTABLE_SKIP}
        data["_app"] = f"{APP_NAME} {APP_VERSION}"
        try:
            Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
            messagebox.showinfo(APP_NAME, f"Settings saved to:\n{path}")
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save the file:\n{e}")

    def import_settings(self):
        path = filedialog.askopenfilename(title="Import settings",
                                          filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_NAME, f"That file couldn't be read as settings:\n{e}")
            return
        if isinstance(data, dict):
            data.pop("_app", None)
        clean, skipped = clean_settings(data)
        if not clean:
            messagebox.showwarning(APP_NAME, "That file doesn't contain any settings this app understands.")
            return
        self.s.update(clean)
        self.push_settings_to_ui()
        note = f" ({skipped} entries were ignored.)" if skipped else ""
        messagebox.showinfo(APP_NAME, f"Imported {len(clean)} settings.{note}")

    def reset_settings(self):
        if not messagebox.askyesno(APP_NAME, "Reset every setting to its default?\n"
                                             "(Your statistics and chosen wallpaper are kept.)"):
            return
        self.s.update({k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()
                       if k not in PORTABLE_SKIP})
        self.push_settings_to_ui()

    def push_settings_to_ui(self):
        """Show the values in self.s in every control, then save and apply them."""
        s = self.s
        self.enabled.set(s["enabled"])
        self.slider.set(s["minutes"])
        self.on_slide(s["minutes"])
        self.auto_pause.set(s["auto_pause"])
        self.call_var.set(s["call_pause"])
        self.fade_on.set(s["fade_enabled"])
        self.fade_secs.set(s["fade_seconds"])
        self.sched_on.set(s["schedule_enabled"])
        self.sched_mode_var.set(next((label for key, label in self.sched_modes if key == s["schedule_mode"]),
                                     self.sched_modes[0][1]))
        self.sched_start.set(s["schedule_start"])
        self.sched_end.set(s["schedule_end"])
        self.sched_min.set(s["schedule_minutes"])
        self.restore_win.set(s["restore_windows"])
        self.icons_var.set(s["hide_icons"])
        self.tbblack_var.set(s["taskbar_while_black"])
        self.all_var.set(s["black_all_screens"])
        self.sleep_on.set(s["sleep_enabled"])
        self.sleep_min.set(s["sleep_minutes"])
        self.startup.set(s["startup"])
        self.dark.set(s["dark_mode"])
        self.scale_slider.set(s["ui_scale"])
        self.clear_var.set(s["taskbar_clear"])
        self.tint.set(s["taskbar_opacity"])
        self.fade_var.set(s["taskbar_fade"])
        self.fade_level.set(s["taskbar_fade_level"])
        self.update_url.set(s["update_url"])
        self.update_startup.set(s["update_check_startup"])
        self.watch_var.set(s["watch_wallpaper"])
        self.panel_var.set(next((label for key, label in self.panel_modes if key == s["panel_type"]),
                                self.panel_modes[0][1]))
        self.display_model_var.set(s["display_model"])
        self.on_change()  # saves, re-themes, applies the taskbar effects and the Start-with-Windows entry
        self.apply_scale()
        self.refit()
        self.refresh_name()

    def check_updates(self, manual=True):
        url = self.update_url.get().strip()
        if not url:
            if manual:
                self.update_note.config(text="Type an update address first (a web page that lists the latest version).")
            return
        if manual:
            self.update_note.config(text="Checking for updates...")
        threading.Thread(target=self._update_worker, args=(url, manual), daemon=True).start()

    def _update_worker(self, url, manual):
        link, version = "", ""
        try:
            version, link = fetch_update_info(url)
            latest, current = parse_version(version), parse_version(APP_VERSION)
            if latest is None:
                message = "That address didn't give a version number."
                link = ""
            elif latest > current:
                message = f"Version {version} is available (you have {APP_VERSION})." + (f" {link}" if link else "")
            else:
                message, link = f"You're up to date (version {APP_VERSION}).", ""
            if link and not link.lower().startswith(("http://", "https://")):
                link = ""
        except Exception as e:
            message, link = f"Couldn't check for updates: {e}", ""
            log(f"update check failed: {e}")
        self._update_result = (message, link, version, manual)

    def run_health_check(self):
        if self._health_running:
            return
        self._health_running = True
        self._health = ["Running health check..."]
        threading.Thread(target=self._health_worker, daemon=True).start()

    def _health_worker(self):
        try:
            lines = self.health_checks()
        except Exception:
            log_error("health check")
            lines = ["[FAIL] The health check itself crashed - see the log."]
        self._health = lines
        self._health_done = True

    def health_checks(self):
        """A list of '[OK] / [WARN] / [FAIL] ...' lines describing whether everything the app relies on works."""
        lines = []

        def add(state, text):
            lines.append(f"[{state}] {text}")

        we = self.we_dir
        add("OK" if we else "FAIL", f"Wallpaper Engine folder: {we}" if we else
            "Wallpaper Engine folder not found - set it in the App tab")
        exe = we_exe(we) if we else None
        add("OK" if exe else "FAIL", f"Program found: {exe.name}" if exe else "Wallpaper Engine program not found")
        running = bool(running_exe_names() & {"wallpaper32.exe", "wallpaper64.exe"})
        add("OK" if running else "FAIL", "Wallpaper Engine is running" if running else
            "Wallpaper Engine is not running - start it")
        if exe:
            current = get_current_wallpaper(exe)
            if current:
                add("OK", f"Wallpaper Engine answers commands (main screen: {wallpaper_title(we, current)})")
            else:
                add("WARN", "Wallpaper Engine didn't answer. If it runs as administrator, a normal app can't "
                            "control it")
        if we:
            try:
                count = len(read_entries(we))
                add("OK" if count else "WARN", f"config.json readable ({count} screen entries)")
            except Exception as e:
                add("WARN", f"Couldn't read config.json: {e}")
            black = we / "projects" / "myprojects" / BLACK_NAME / "project.json"
            add("OK" if black.exists() else "WARN", "Black wallpaper is in place" if black.exists() else
                "Black wallpaper is missing (it is created automatically when the folder is writable)")
        want, have = bool(self.s["startup"]), STARTUP_VBS.exists()
        add("OK" if want == have else "WARN", "Start-with-Windows entry matches your setting" if want == have else
            "Start-with-Windows entry doesn't match your setting")
        add("OK" if self.tray else "WARN", "Tray icon is active" if self.tray else
            "No tray icon (rebuild with build_exe.bat to include it)")
        try:
            probe = APP_DIR / ".write_test"
            probe.write_text("x")
            probe.unlink()
            add("OK", f"Settings folder is writable: {APP_DIR}")
        except OSError as e:
            add("FAIL", f"Can't write to the settings folder: {e}")
        add("OK" if ERROR_COUNT == 0 else "WARN", "No errors logged this session" if ERROR_COUNT == 0 else
            f"{ERROR_COUNT} error(s) logged this session - see the log below")
        return lines

    def poll_background(self):
        """Pick up results from background threads (health check, update check) and update the warning label."""
        if self._health_done:
            self._health_done = False
            self._health_running = False
            if self.tabs.select() == str(self.details_tab):
                self.refresh_details()
        if self._update_result:
            message, link, version, manual = self._update_result
            self._update_result = None
            self.update_note.config(text=message)
            if link and manual and messagebox.askyesno(APP_NAME, f"Version {version} is available.\n"
                                                              "Open the download page?"):
                webbrowser.open(link)
        self.update_warning()

    def update_warning(self):
        bad = ERROR_COUNT > 0 or any(line.startswith("[FAIL]") for line in self._health)
        if bad and not self._warn_visible:
            self.warn_label.pack(side="right", padx=6)
            self._warn_visible = True
        elif not bad and self._warn_visible:
            self.warn_label.pack_forget()
            self._warn_visible = False

    def _panel_mode_key(self):
        return next((key for key, label in self.panel_modes if label == self.panel_var.get()), "auto")

    def refresh_display_info(self):
        """Read the brand and model of the connected monitors from Windows and show the advice."""
        try:
            self._displays = read_display_edids()
        except Exception:
            log_error("detecting the monitors")
            self._displays = []
        self._displays_loaded = True
        self.render_display_report()

    def render_display_report(self):
        try:
            text = build_display_report(self._displays, self._panel_mode_key(), self.display_model_var.get(),
                                        self.s, bool(self.taskbar.get()), max(1, len(list_monitors())))
        except Exception:
            log_error("describing the display")
            text = "Something went wrong while describing your display - see the log in the Details tab."
        self.display_text.delete("1.0", "end")
        self.display_text.insert("1.0", text)

    def _model_typed(self, *_):
        """Called on every keystroke in the model box; updates the advice once you pause."""
        if self._model_job is not None:
            self.root.after_cancel(self._model_job)
        self._model_job = self.root.after(700, self._model_typed_done)

    def _model_typed_done(self):
        self._model_job = None
        self.on_display_option()

    def on_display_option(self):
        self.on_change()
        self.render_display_report()

    def lookup_manual(self):
        """Open a web search for this monitor's manual, so you can follow the steps for your exact model."""
        info = self._displays[0] if self._displays else {}
        typed = self.display_model_var.get().strip()
        brand = identify_brand(typed, info.get("brand", "")) if typed else info.get("brand", "")
        model = typed or info.get("name", "")
        words = model if (not brand or model.upper().startswith(brand.upper())) else f"{brand} {model}"
        query = (f"{words} user manual OLED care presence proximity sensor" if words.strip()
                 else "monitor user manual OLED care presence proximity sensor")
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))

    def apply_recommended(self):
        changes = recommended_settings(max(1, len(list_monitors())))
        if not messagebox.askyesno(APP_NAME, "Apply the recommended settings for an OLED screen?\n\n"
                                             "This turns on: the black screen after 2 minutes, hiding desktop icons "
                                             "and the taskbar while black, turning the display off after 10 minutes, "
                                             "pausing during games and calls, and dark mode. It also turns on "
                                             "Windows' 'Automatically hide the taskbar'."):
            return
        self.s.update(changes)
        self.push_settings_to_ui()
        if not self.taskbar.get():
            self.taskbar.set(True)
            self.on_taskbar()
        self.render_display_report()

    def install_status_text(self):
        if not FROZEN:
            return "Installing only works from the packaged .exe (build it with build_exe.bat)."
        installed = INSTALL_DIR / f"{APP_NAME}.exe"
        if not installed.exists():
            return "Not installed - this copy runs from wherever you saved it."
        if Path(sys.executable).resolve() == installed.resolve():
            return f"Installed at {installed}"
        return f"Installed at {installed} (this copy is running from somewhere else)"

    def install_clicked(self):
        """Hand over to a separate installer process, then close - the installed copy starts when it is done."""
        if not FROZEN:
            return
        try:
            subprocess.Popen([sys.executable, "--install"], creationflags=0x08000000)
        except OSError as e:
            log_error("starting the installer")
            messagebox.showerror(APP_NAME, f"Couldn't start the installer:\n{e}")
            return
        self.root.after(300, self.quit)

    def uninstall_clicked(self):
        """Hand over to a separate uninstaller process (it asks you to confirm), then close."""
        if not FROZEN:
            return
        try:
            subprocess.Popen([sys.executable, "--uninstall"], creationflags=0x08000000)
        except OSError as e:
            log_error("starting the uninstaller")
            messagebox.showerror(APP_NAME, f"Couldn't start the uninstaller:\n{e}")
            return
        self.root.after(300, self.quit)

    def open_log(self):
        try:
            if not LOG_FILE.exists():
                LOG_FILE.write_text("", encoding="utf-8")
            os.startfile(str(LOG_FILE))  # Windows only
        except (OSError, AttributeError):
            messagebox.showinfo(APP_NAME, f"The log file is here:\n{LOG_FILE}")

    # ----- transparent taskbar, text size and window size -----
    def on_clear_toggle(self):
        if self.clear_var.get():
            self.fade_var.set(False)  # only one taskbar effect at a time
        self.on_change()

    def on_fade_toggle(self):
        if self.fade_var.get():
            self.clear_var.set(False)
        self.on_change()

    def _effect(self, on):
        """Turn the selected taskbar effect on or off (the fade if it's ticked, otherwise the clear background)."""
        if self.fade_var.get():
            set_taskbar_fade(on, int(self.fade_level.get()))
        else:
            set_taskbar_clear(on, int(self.tint.get()))

    def check_taskbar_clear(self):
        """Compare the taskbar's colours without and with the effect, to see if Windows honours it."""
        self.clear_note.config(text="Checking... (the taskbar will flicker for a second)")
        try:
            self._effect(False)
        except Exception:
            pass
        self.root.after(600, self._check_step2)

    def _check_step2(self):
        self._before = sample_taskbar()
        try:
            self._effect(True)
        except Exception as e:
            self.clear_note.config(text=f"Couldn't call Windows: {e}")
            return
        self.root.after(800, self._check_step3)

    def _check_step3(self):
        after = sample_taskbar()
        diff = max((abs(a - b) for p, q in zip(self._before, after) for a, b in zip(p, q)), default=0)
        fade = self.fade_var.get()
        if not (fade or self.clear_var.get()):
            try:
                self._effect(False)  # it was only a test - put the taskbar back
            except Exception:
                pass
        build = windows_build()
        self._clear_result = f"{'fade' if fade else 'clear'}: colour change {diff}"
        if diff >= 15:
            self.clear_note.config(text=f"Windows accepted it (the taskbar's colours changed). Windows build {build}.")
        elif fade:
            self.clear_note.config(text=f"The fade didn't change the taskbar either (Windows build {build}). "
                                        "Untick it. The 'Hide the taskbar' options still work.")
        else:
            self.clear_note.config(text=f"The taskbar didn't change, so Windows (build {build}) is ignoring the "
                                        "request. Updates such as KB5132057 are reported to block taskbar "
                                        "transparency tools. Try the fade option below, or the 'Hide the taskbar' "
                                        "options.")

    def apply_taskbar_clear(self):
        clear, fade = self.s["taskbar_clear"], self.s["taskbar_fade"]
        try:
            if clear:
                set_taskbar_clear(True, self.s["taskbar_opacity"])
            elif self._clear_applied:
                set_taskbar_clear(False)
            if fade:
                set_taskbar_fade(True, self.s["taskbar_fade_level"])
            elif self._fade_applied:
                set_taskbar_fade(False)
        except Exception:
            pass
        self._clear_applied, self._fade_applied = clear, fade

    def reapply_taskbar_effects(self):
        now = time.monotonic()
        if self._clear_applied and now - self._last_accent >= 30:  # re-applying the clear effect can make it blink
            self._last_accent = now
            set_taskbar_clear(True, self.s["taskbar_opacity"])
        if self._fade_applied:
            set_taskbar_fade(True, self.s["taskbar_fade_level"])  # only touches Windows if the fade was lost

    def apply_scale(self):
        """Scale all text (and so the whole window) by the 'Text size' setting."""
        self.scale = max(0.5, self.s["ui_scale"] / 100)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont", "TkFixedFont"):
            try:
                font = tkfont.nametofont(name)
            except tk.TclError:
                continue
            base = self._base_fonts.setdefault(name, font.cget("size"))
            new = int(round(base * self.scale))
            font.configure(size=new if new else base)
        self.title_label.configure(font=("Segoe UI", max(8, int(round(13 * self.scale))), "bold"))

    def refit(self):
        """Smallest allowed window = what the content needs at this text size; grow the window if it's smaller."""
        for label, base in self._wraps:
            label.configure(wraplength=int(base * self.scale))
        self.root.update_idletasks()
        need_w, need_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(need_w, need_h)
        cur_w, cur_h = self.root.winfo_width(), self.root.winfo_height()
        self.root.geometry(f"{max(cur_w, need_w)}x{max(cur_h, need_h)}")
        self.update_wraps()

    def update_wraps(self):
        width = self.root.winfo_width()
        if width < 50:
            return
        self.min_label.configure(wraplength=max(200, width - 80))
        self.name_label.configure(wraplength=max(180, width - 130))
        self.status.configure(wraplength=max(200, width - 60))
        self.clear_note.configure(wraplength=max(200, width - 80))
        self.stats_note.configure(wraplength=max(200, width - 80))
        self.wp_help.configure(wraplength=max(200, width - 80))
        self.wp_current_label.configure(wraplength=max(200, width - 80))
        self.update_note.configure(wraplength=max(200, width - 80))
        self.install_note.configure(wraplength=max(200, width - 80))

    def restore_window_size(self):
        size = self.s.get("window_size")
        try:
            w, h = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            return
        min_w, min_h = self.root.minsize()
        w = max(min_w, min(w, self.root.winfo_screenwidth()))
        h = max(min_h, min(h, self.root.winfo_screenheight()))
        self.root.geometry(f"{w}x{h}")

    def save_window_size(self):
        self._save_job = None
        try:
            if self.root.state() == "normal":
                self.s["window_size"] = [self.root.winfo_width(), self.root.winfo_height()]
                save_settings(self.s)
        except tk.TclError:
            pass

    def on_resize(self, event):
        if event.widget is not self.root:
            return
        self.update_wraps()
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(800, self.save_window_size)  # remember the size once dragging stops

    def on_scale(self):
        self.s["ui_scale"] = int(self.scale_slider.get())
        save_settings(self.s)
        self.apply_scale()
        self.refit()

    # ----- extras: desktop icons, taskbar, display power, snooze, tray -----
    def _sleep_minutes(self):
        try:
            return max(1, min(240, int(self.sleep_min.get())))
        except (ValueError, tk.TclError):
            return self.s["sleep_minutes"]

    def save_ui_state(self):
        """Note what we've temporarily changed so it can be undone even if the app is closed unexpectedly."""
        try:
            if self.icons_hidden or self.taskbar_forced:
                UI_STATE_FILE.write_text(json.dumps({"icons": self.icons_hidden, "taskbar": self.taskbar_forced}),
                                         encoding="utf-8")
            else:
                UI_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def recover_ui(self):
        if not UI_STATE_FILE.exists():
            return
        try:
            state = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
            if state.get("icons"):
                set_desktop_icons(True)
            if state.get("taskbar"):
                set_taskbar_hidden(False)
        except (OSError, ValueError):
            pass
        UI_STATE_FILE.unlink(missing_ok=True)

    def hide_extras(self):
        """Called when the screen goes black."""
        try:
            if self.s["hide_icons"] and not self.icons_hidden and desktop_icons_visible():
                set_desktop_icons(False)
                self.icons_hidden = True
            if self.s["taskbar_while_black"] and not self.taskbar_forced and not taskbar_hidden():
                set_taskbar_hidden(True)
                self.taskbar_forced = True
        except Exception:
            pass
        self.black_since = time.monotonic()
        self.display_is_off = False
        self.save_ui_state()

    def show_extras(self):
        """Undo whatever hide_extras did."""
        try:
            if self.icons_hidden:
                set_desktop_icons(True)
            if self.taskbar_forced:
                set_taskbar_hidden(False)
        except Exception:
            pass
        self.icons_hidden = False
        self.taskbar_forced = False
        self.display_is_off = False
        self.save_ui_state()

    def toggle_snooze(self):
        if self.snooze_until > time.monotonic():
            self.snooze_until = 0.0
        else:
            self.snooze_until = time.monotonic() + 3600
            if self.is_black:
                self.end_black()

    def start_tray(self):
        if pystray is None:
            return
        try:
            put = self.tray_queue.put  # the tray runs in its own thread, so hand actions over via a queue
            menu = pystray.Menu(
                pystray.MenuItem("Open", lambda: put("open"), default=True),
                pystray.MenuItem("Snooze 1 hour / Resume", lambda: put("snooze")),
                pystray.MenuItem("Test now", lambda: put("test")),
                pystray.MenuItem("Quit", lambda: put("quit")))
            self.tray = pystray.Icon("oled_screensaver", make_tray_image(), "OLED Screensaver", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def hide_window(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def edit_programs(self):
        t = self.theme
        win = tk.Toplevel(self.root)
        win.title("Programs that pause the screensaver")
        win.geometry("380x340")
        win.configure(bg=t["bg"])
        ttk.Label(win, wraplength=350,
                  text="One program per line, for example mygame.exe. While any of them is running the "
                       "screensaver stays off. Games in Steam, Epic, GOG and Xbox folders are already "
                       "detected.").pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(win, height=10, font=("Consolas", max(7, round(10 * self.scale))), bg=t["field"], fg=t["fg"],
                      insertbackground=t["fg"], relief="flat", highlightthickness=0)
        txt.pack(fill="both", expand=True, padx=10)
        txt.insert("1.0", "\n".join(self.s["extra_programs"]))
        set_titlebar_dark(win, self.dark.get())

        def save():
            self.s["extra_programs"] = [line.strip().lower() for line in txt.get("1.0", "end").splitlines()
                                        if line.strip()]
            save_settings(self.s)
            self._scan_time = 0.0  # rescan right away
            win.destroy()

        ttk.Button(win, text="Save", command=save).pack(side="right", padx=10, pady=8)

    def pause_reason(self):
        """Why the screensaver should stay off right now (game / video), or None."""
        if fullscreen_app_active():
            return "fullscreen app"
        media = foreground_media()
        if media:
            return media
        now = time.monotonic()
        if now - self._scan_time > 5:  # scanning every process is cheap, but not every second
            self._scan_time = now
            self._game = game_running(set(self.s["extra_programs"]))
        if self._game:
            return f"game ({self._game})"
        if self.s["call_pause"]:
            if now - self._cap_time > 5:
                self._cap_time = now
                self._cap_app = capture_in_use()
            if self._cap_app:
                return f"call ({self._cap_app})"
        return None

    def tick(self):
        try:
            idle = idle_seconds()
            now = time.monotonic()
            self.record_time(now)
            if QUIT_FLAG.exists():  # the installer / uninstaller asks this copy to close
                remove_flag(QUIT_FLAG)
                self.quit()
                return
            if SHOW_FLAG.exists():  # the user started a second copy - show this window instead
                remove_flag(SHOW_FLAG)
                self.show_window()
            self.poll_background()
            while not self.tray_queue.empty():  # actions chosen from the tray icon menu
                cmd = self.tray_queue.get_nowait()
                if cmd == "open":
                    self.show_window()
                elif cmd == "snooze":
                    self.toggle_snooze()
                elif cmd == "test":
                    self.test_now()
                elif cmd == "quit":
                    self.quit()
                    return
            if not self.is_black and now - self._last_sample >= 2:
                self._last_sample = now
                self.sample_wallpaper()
            if now - self._last_tb >= 3 and not self.taskbar_forced:
                self._last_tb = now
                actual = taskbar_hidden()
                if actual != self.taskbar.get():
                    self.taskbar.set(actual)
            if (self._clear_applied or self._fade_applied) and now - self._last_clear >= 10:
                self._last_clear = now  # Windows re-asserts its look now and then, so re-apply
                self.reapply_taskbar_effects()
            allowed, limit = self.schedule_info()
            reason = self.pause_reason() if (self.s["auto_pause"] and not self.is_black) else None
            self._busy = bool(reason)
            snoozed = self.snooze_until > now
            self.set_text(self.snooze_btn, "Resume" if snoozed else "Snooze 1h")
            if self._fade_win is not None and (not self.enabled.get() or snoozed or reason or not allowed):
                self.cancel_fade()  # something changed while dimming - stop and bring the screen back
            if not self.we_dir:
                self.resolve_we()
                self.set_status("Wallpaper Engine not found - use Browse.")
            elif not self.enabled.get():
                self.set_status("Disabled.")
            elif self.is_black:
                if self.test_end:  # 'Test now' run: revert on a timer, ignoring input
                    left = self.test_end - time.monotonic()
                    self.set_status(f"Test: reverting in {max(0, int(left) + 1)}s (or press Revert now).")
                    if left <= 0:
                        self.end_black()
                else:
                    if self.display_is_off:
                        self.set_status("Display is off - move the mouse to wake it.")
                    else:
                        self.set_status("Screen is black - move the mouse to restore.")
                    if (self.s["sleep_enabled"] and not self.display_is_off
                            and time.monotonic() - self.black_since >= self.s["sleep_minutes"] * 60):
                        display_off()
                        self.display_is_off = True
                    if idle < WAKE_THRESHOLD:
                        self.end_black()
            elif snoozed:
                self.set_status(f"Snoozed - back in {int((self.snooze_until - now) / 60) + 1} min.")
            elif not allowed:
                self.set_status("Outside your schedule - the screensaver is off for now.")
            elif reason:
                self.set_status(f"Paused - {reason} detected.")
            elif self._fade_win is not None:
                self.set_status("Fading to black...")
            else:
                self.set_status(f"Idle {int(idle)}s / {limit}s")
                if idle >= limit:
                    if self.s["fade_enabled"]:
                        self.start_fade()
                    elif self.go_black():
                        self.is_black = True
                    else:
                        self.set_status("Couldn't read the current wallpaper - see Details.")
        except Exception as e:  # keep the loop alive no matter what
            self.set_status(f"Error: {e}")
            log_error("main loop")
        self.root.after(1000, self.tick)

    def quit(self):
        if self.is_black:
            self.end_black()
        self.show_extras()
        for active, undo in ((self._clear_applied, set_taskbar_clear), (self._fade_applied, set_taskbar_fade)):
            if active:
                try:
                    undo(False)
                except Exception:
                    pass
        self.save_window_size()
        self.save_stats()
        self.cancel_fade()
        self._stop.set()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def run_installer_action(flag):
    """--install / --uninstall: runs without the normal window and reports through message boxes."""
    root = tk.Tk()
    root.withdraw()
    set_window_icon(root)
    try:
        if flag == "--install":
            done, info = install_app()
            if done:
                messagebox.showinfo(APP_NAME, f"{APP_NAME} was installed.\n\nYou'll find it in your Start menu, and "
                                              "in Settings > Apps if you ever want to remove it.")
                subprocess.Popen([info])  # start the installed copy
            else:
                messagebox.showerror(APP_NAME, info)
        elif messagebox.askyesno(APP_NAME, f"Remove {APP_NAME} from this PC?"):
            delete = messagebox.askyesno(APP_NAME, "Also delete your saved settings, statistics and log?")
            uninstall_app(delete_settings=delete)
            messagebox.showinfo(APP_NAME, f"{APP_NAME} was removed.")
    except Exception as e:
        log_error(flag)
        messagebox.showerror(APP_NAME, f"Something went wrong:\n{e}")
    finally:
        root.destroy()


def main():
    args = sys.argv[1:]
    for flag in ("--install", "--uninstall"):
        if flag in args:
            run_installer_action(flag)
            return
    if not acquire_single_instance():  # already running: ask that copy to show its window, then stop
        if "--minimized" not in args:
            try:
                SHOW_FLAG.write_text("show", encoding="utf-8")
            except OSError:
                pass
        return
    root = tk.Tk()
    set_window_icon(root)
    App(root, "--minimized" in args)
    root.mainloop()


if __name__ == "__main__":
    main()
