#!/usr/bin/env python3
"""
Glance - a tray system monitor for Windows 11.

One icon in the system tray. Click it and a panel drops down (or up, if your
taskbar is at the bottom) with CPU, memory, GPU, disk, network and battery,
each with a live graph and the apps behind the numbers.

Run:  pythonw glance.pyw      (or double-click "Start Glance.bat")
Needs: PySide6, psutil        (install with "Install.bat")
"""
import os
import sys
import json
import socket
import time
import ctypes
import threading
import traceback
from collections import deque, defaultdict

__version__ = "1.0.0"
FROZEN = getattr(sys, "frozen", False)          # running from the installed Glance.exe
# APP_DIR: where Glance's own files live (read-only when installed in Program Files)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
# HERE: where Glance writes its config, logs and debug files
HERE = (os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Glance")
        if FROZEN else APP_DIR)
os.makedirs(HERE, exist_ok=True)
LOG_PATH = os.path.join(HERE, "glance-error.log")


def _log_exception(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n[%Y-%m-%d %H:%M:%S]\n") + text)
    except Exception:
        pass
    sys.__stderr__ and sys.__stderr__.write(text)


sys.excepthook = _log_exception

try:
    import psutil
    from PySide6.QtCore import (Qt, QObject, Signal, QRect, QRectF, QPointF, QSize,
                                QSettings, QFileInfo, QLockFile, QDir, QTimer)
    from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QPen, QPainterPath, QFont,
                               QFontDatabase, QAction, QActionGroup, QCursor, QLinearGradient,
                               QGuiApplication, QFontMetrics)
    from PySide6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QWidget, QVBoxLayout,
                                   QHBoxLayout, QGridLayout, QLabel, QPushButton, QStackedWidget,
                                   QButtonGroup, QSizePolicy, QFileIconProvider, QLayout,
                                   QMessageBox)
except ImportError as e:  # friendly message when run before Install.bat
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(
            None, f"Glance is missing a package: {e.name}\n\nRun Install.bat first.", "Glance", 0x10)
    raise

IS_WIN = sys.platform == "win32"
APP = "Glance"
HISTORY = 60          # seconds of graph history
PANEL_W = 400         # width of the panel content
SHADOW = 14           # px of soft shadow around the panel

C = {
    "bg": "#1f1f22", "card": "rgba(255,255,255,14)", "card_hi": "rgba(255,255,255,26)",
    "border": "rgba(255,255,255,30)",
    "text": "#f2f2f5", "sub": "#b6bcc6", "dim": "#8e95a1",
    "cpu": "#5b8cff", "mem": "#a78bfa", "gpu": "#34d399", "disk": "#f5a524",
    "net": "#22d3ee", "net_up": "#f472b6", "bat": "#84cc16",
    "temp": "#fb7185",
    "red": "#ff5a5f", "amber": "#f5a524", "green": "#34c759",
}
GIB = 1024 ** 3


# ---------------------------------------------------------------- formatting
def fmt_rate(b):
    b = max(0.0, b)
    if b < 1000:
        return f"{b:.0f} B/s"
    if b < 1e6:
        return f"{b / 1e3:.1f} kB/s" if b < 1e4 else f"{b / 1e3:.0f} kB/s"
    if b < 1e9:
        return f"{b / 1e6:.1f} MB/s"
    return f"{b / 1e9:.2f} GB/s"


def split_rate(b):
    num, unit = fmt_rate(b).split(" ")
    return num, unit


def fmt_bytes(b):
    if b >= GIB:
        return f"{b / GIB:.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b:.0f} B"


def fmt_dur(seconds):
    m = int(seconds) // 60
    d, rem = divmod(m, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _ival(x):
    return x.value if hasattr(x, "value") else int(x)


def qc(spec):
    """QColor from '#hex' or 'rgba(r,g,b,a)'."""
    if isinstance(spec, QColor):
        return QColor(spec)
    if spec.startswith("rgba("):
        r, g, b, a = (int(float(x)) for x in spec[5:-1].split(","))
        return QColor(r, g, b, a)
    return QColor(spec)


# ------------------------------------------------------------ Windows theme
CONFIG_PATH = os.path.join(HERE, "glance-config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _abgr(v):
    return "#{:02x}{:02x}{:02x}".format(v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


DEFAULT_THEME = {"accent": "#4f9bff", "light1": "#7ab4ff", "light2": "#99c9ff", "light3": "#c2e0ff",
                 "dark1": "#1f5fe0", "dark2": "#173f99", "dark3": "#0f2a66", "start": "#202020",
                 "prevalence": False, "dark": True}


def read_theme():
    """Accent colours exactly as Windows stores them for Start, taskbar and controls."""
    t = dict(DEFAULT_THEME)
    if not IS_WIN:
        return t
    import winreg

    def val(path, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    acc = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"
    pal = val(acc, "AccentPalette")
    if isinstance(pal, (bytes, bytearray)) and len(pal) >= 32:
        cols = ["#{:02x}{:02x}{:02x}".format(pal[i], pal[i + 1], pal[i + 2]) for i in range(0, 32, 4)]
        t.update(light3=cols[0], light2=cols[1], light1=cols[2], accent=cols[3],
                 dark1=cols[4], dark2=cols[5], dark3=cols[6])
    menu = val(acc, "AccentColorMenu")
    if isinstance(menu, int):
        t["accent"] = _abgr(menu)
    start = val(acc, "StartColorMenu")
    pers = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    t["prevalence"] = val(pers, "ColorPrevalence") == 1
    t["dark"] = val(pers, "SystemUsesLightTheme") != 1
    if t["prevalence"] and isinstance(start, int):
        t["start"] = _abgr(start)
    else:
        t["start"] = "#202020" if t["dark"] else "#f3f3f3"
    return t


THEME = dict(DEFAULT_THEME)


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WinCompAttrData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(_AccentPolicy)),
                ("SizeOfData", ctypes.c_size_t)]


class _Margins(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


def _dwm_set(hwnd, attr, value):
    v = ctypes.c_int(value)
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(v),
                                                      ctypes.sizeof(v))


def apply_backdrop(hwnd, mode, tint, alpha):
    """Give a window the Windows 11 acrylic material. Returns the mode that took effect."""
    if not IS_WIN or mode == "none":
        return "none"
    try:
        _dwm_set(hwnd, 20, 1)            # DWMWA_USE_IMMERSIVE_DARK_MODE
        _dwm_set(hwnd, 33, 2)            # DWMWA_WINDOW_CORNER_PREFERENCE = round
        if mode == "dwm":
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd),
                                                              ctypes.byref(_Margins(-1, -1, -1, -1)))
            if _dwm_set(hwnd, 38, 3) == 0:   # DWMWA_SYSTEMBACKDROP_TYPE = transient (acrylic)
                return "dwm"
            mode = "swca"
        c = QColor(tint)
        a = max(0, min(255, int(alpha * 255)))
        policy = _AccentPolicy(4, 0, (a << 24) | (c.blue() << 16) | (c.green() << 8) | c.red(), 0)
        data = _WinCompAttrData(19, ctypes.pointer(policy), ctypes.sizeof(policy))
        if ctypes.windll.user32.SetWindowCompositionAttribute(ctypes.c_void_p(hwnd), ctypes.byref(data)):
            return "swca"
    except Exception:
        _log_exception(*sys.exc_info())
    return "none"


def on_color(bg):
    """Black or white text, whichever reads better on this background."""
    c = QColor(bg)
    lum = 0.2126 * c.redF() ** 2.2 + 0.7152 * c.greenF() ** 2.2 + 0.0722 * c.blueF() ** 2.2
    return "#000000" if lum > 0.22 else "#ffffff"


CORNER_RADIUS = 8   # Windows 11 flyout radius (logical px)


def round_window(hwnd, width, height, radius):
    """Clip a window to a rounded rectangle (physical pixels)."""
    if not IS_WIN:
        return False
    try:
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        rgn = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        return bool(user32.SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(rgn), True))
    except Exception:
        _log_exception(*sys.exc_info())
        return False


def strain(pct, busy, high):
    if pct >= high:
        return "Strained", C["red"]
    if pct >= busy:
        return "Busy", C["amber"]
    return "Normal", C["green"]


# ------------------------------------------------------- Windows GPU counters
class _FmtValue(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FmtValue)]


class PdhWildcard:
    """Reads wildcard performance counters (the same ones Task Manager uses)."""
    FMT_DOUBLE = 0x00000200
    FMT_NOCAP100 = 0x00008000
    MORE_DATA = 0x800007D2

    def __init__(self, paths):
        self.ok = False
        self.counters = {}
        if not IS_WIN:
            return
        try:
            pdh = ctypes.WinDLL("pdh")
            for fn in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData",
                       "PdhGetFormattedCounterArrayW"):
                getattr(pdh, fn).restype = ctypes.c_ulong
            self.pdh = pdh
            self.query = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, ctypes.c_void_p(0), ctypes.byref(self.query)) != 0:
                return
            for key, path in paths.items():
                h = ctypes.c_void_p()
                if pdh.PdhAddEnglishCounterW(self.query, ctypes.c_wchar_p(path),
                                             ctypes.c_void_p(0), ctypes.byref(h)) == 0:
                    self.counters[key] = h
            if self.counters:
                pdh.PdhCollectQueryData(self.query)
                self.ok = True
        except Exception:
            self.ok = False

    def collect(self):
        out = {}
        if not self.ok or self.pdh.PdhCollectQueryData(self.query) != 0:
            return out
        fmt = self.FMT_DOUBLE | self.FMT_NOCAP100
        for key, h in self.counters.items():
            size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
            r = self.pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size),
                                                      ctypes.byref(count), None)
            if r != self.MORE_DATA or size.value == 0:
                out[key] = []
                continue
            buf = (ctypes.c_byte * size.value)()
            r = self.pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size),
                                                      ctypes.byref(count), buf)
            if r != 0:
                out[key] = []
                continue
            arr = ctypes.cast(buf, ctypes.POINTER(_FmtItem))
            out[key] = [(arr[i].szName or "", arr[i].FmtValue.doubleValue)
                        for i in range(count.value) if arr[i].FmtValue.CStatus in (0, 1)]
        return out


def parse_gpu(items):
    """Instance names look like pid_1234_luid_0x0_0x0000D1A3_phys_0_eng_0_engtype_3D."""
    per_engine = defaultdict(float)
    per_pid = defaultdict(lambda: defaultdict(float))
    for name, val in items:
        try:
            parts = name.split("_")
            pid = int(parts[1])
            luid = parts[3] + parts[4]
            etype = name.split("engtype_", 1)[1]
        except (IndexError, ValueError):
            continue
        per_engine[(luid, etype)] += val
        per_pid[pid][etype] += val
    by_type = defaultdict(float)
    for (_, etype), v in per_engine.items():
        by_type[etype] = max(by_type[etype], min(100.0, v))
    total = max(by_type.values(), default=0.0)
    pid_pct = {p: min(100.0, max(d.values())) for p, d in per_pid.items()}
    return total, dict(by_type), pid_pct


# -------------------------------------------------------------- temperatures
# Temperatures come from LibreHardwareMonitor's open-source library, the same
# approach as HWMonitor: a small reader runs with admin rights (Windows asks
# once when it starts) and hands readings to the panel through a JSON file.
LHM_DIR = os.path.join(APP_DIR, "LibreHardwareMonitor")
SENSOR_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "Glance", "sensors.json")
STOP_FILE = SENSOR_FILE + ".stop"
CPU_TEMP_NAMES = ("CPU Package", "Core (Tctl/Tdie)", "Core (Tctl)", "Tctl/Tdie", "Tdie", "Core Max",
                  "Core Average")


def find_lhm_dll():
    best = None
    for root, _, files in os.walk(LHM_DIR):
        if "LibreHardwareMonitorLib.dll" in files:
            path = os.path.join(root, "LibreHardwareMonitorLib.dll")
            if best is None or len(path) < len(best):
                best = path
    return best


def find_lhm_exe():
    for root, _, files in os.walk(LHM_DIR):
        if "LibreHardwareMonitor.exe" in files:
            return os.path.join(root, "LibreHardwareMonitor.exe")
    return None


def pawnio_installed():
    if not IS_WIN:
        return False
    try:
        psutil.win_service_get("PawnIO")
        return True
    except Exception:
        return False


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    for _ in range(10):  # the panel may be reading it at this exact moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.03)


def sensor_helper(out_path, parent_pid):
    """Runs elevated. Reads every sensor once a second until Glance exits."""
    global SENSOR_FILE, STOP_FILE
    SENSOR_FILE, STOP_FILE = out_path, out_path + ".stop"

    def emit(**kw):
        kw.update(pid=os.getpid(), ts=time.time())
        try:
            _write_json(SENSOR_FILE, kw)
        except OSError:
            pass

    owner_file = out_path + ".owner"
    last_owner_seen = [time.time()]

    def keep_going():
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass
            return False
        alive = parent_pid <= 0 or psutil.pid_exists(parent_pid)
        if not alive:                    # a restarted Glance takes over this reader
            try:
                with open(owner_file, encoding="utf-8") as f:
                    owner = int(f.read().strip() or 0)
                alive = owner > 0 and psutil.pid_exists(owner)
            except (OSError, ValueError):
                pass
        if alive:
            last_owner_seen[0] = time.time()
        return time.time() - last_owner_seen[0] < 20

    dll = find_lhm_dll()
    if not dll:
        emit(status="no_lib")
        return
    folder = os.path.dirname(dll)
    for root, _, files in os.walk(LHM_DIR):  # .NET refuses files still marked "downloaded"
        for f in files:
            try:
                os.remove(os.path.join(root, f) + ":Zone.Identifier")
            except OSError:
                pass
    try:
        import pythonnet
        core = any(f.endswith(".runtimeconfig.json") for f in os.listdir(folder))
        pythonnet.load("coreclr" if core else "netfx")
        import clr
    except Exception as e:
        emit(status="no_pythonnet", error=f"{type(e).__name__}: {e}")
        return
    try:
        sys.path.append(folder)
        clr.AddReference(dll)
        from LibreHardwareMonitor.Hardware import Computer, SensorType
        comp = Computer()
        for attr in ("IsCpuEnabled", "IsGpuEnabled", "IsMotherboardEnabled", "IsStorageEnabled",
                     "IsMemoryEnabled", "IsControllerEnabled"):
            try:
                setattr(comp, attr, True)
            except Exception:
                pass
        comp.Open()
    except Exception as e:
        emit(status="error", error=f"{type(e).__name__}: {e}")
        return

    temp_t, fan_t, power_t = SensorType.Temperature, SensorType.Fan, SensorType.Power
    pawnio = pawnio_installed()
    try:
        while keep_going():
            groups = []
            for hw in comp.Hardware:
                try:
                    hw.Update()
                    subs = list(hw.SubHardware)
                    for sub in subs:
                        sub.Update()
                except Exception:
                    continue
                for h in [hw] + subs:
                    g = {"name": str(h.Name), "type": str(h.HardwareType), "temps": [], "fans": [], "power": []}
                    for sensor in h.Sensors:
                        v = sensor.Value
                        if v is None:
                            continue
                        v = float(v)
                        kind, name = sensor.SensorType, str(sensor.Name)
                        if kind == temp_t and 0 < v < 150:
                            g["temps"].append([name, round(v, 1)])
                        elif kind == fan_t and v > 0:
                            g["fans"].append([name, round(v)])
                        elif kind == power_t and v > 0:
                            g["power"].append([name, round(v, 1)])
                    if g["temps"] or g["fans"] or g["power"]:
                        groups.append(g)
            emit(status="ok", groups=groups, pawnio=pawnio)
            time.sleep(1.0)
    finally:
        try:
            comp.Close()
        except Exception:
            pass


def read_temps():
    """Panel side: read the helper's latest snapshot."""
    try:
        with open(SENSOR_FILE, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {"status": "off"}
    if time.time() - obj.get("ts", 0) > 6:
        return {"status": "stopped"}
    if obj.get("status") != "ok":
        return obj
    groups = []
    for g in obj.get("groups", []):
        g = dict(g)
        g["temps"] = [[n, v] for n, v in g["temps"] if not any(w in n for w in JUNK_SENSORS) and v >= 1]
        g["integrated"] = is_integrated_gpu(g)
        groups.append(g)
    groups.sort(key=group_rank)
    obj["groups"] = groups
    cpu = gpu = None
    cpu_power = gpu_power = None
    gpus = [g for g in groups if g["type"].startswith("Gpu")]
    main_gpu = next((g for g in gpus if not g["integrated"]), gpus[0] if gpus else None)
    for g in groups:
        temps = dict((n, v) for n, v in g["temps"])
        power = dict((n, v) for n, v in g["power"])
        if g["type"] == "Cpu" and cpu is None and temps:
            cpu = next((temps[n] for n in CPU_TEMP_NAMES if n in temps), max(temps.values()))
            cpu_power = power.get("CPU Package") or power.get("Package")
        if g is main_gpu:
            if temps:
                gpu = temps.get("GPU Core", next(iter(temps.values())))
            gpu_power = (power.get("GPU Package") or power.get("GPU Power")
                         or next(iter(power.values()), None))
    obj.update(cpu=cpu, gpu=gpu, cpu_power=cpu_power, gpu_power=gpu_power,
               gpu_name=main_gpu["name"] if main_gpu else None)
    return obj


JUNK_SENSORS = ("Resolution", "Distance to TjMax", "Limit", "Critical", "Warning")


def is_integrated_gpu(g):
    """Ryzen/Intel built-in graphics, so a discrete card (e.g. GeForce) wins."""
    if not g["type"].startswith("Gpu"):
        return False
    name = g["name"]
    if g["type"] == "GpuIntel" and "Arc" not in name:
        return True
    return name.strip().endswith("Radeon(TM) Graphics") or "Vega" in name or name.endswith("Graphics")


def group_rank(g):
    t = g["type"]
    if t == "Cpu":
        return 0
    if t.startswith("Gpu"):
        return 3 if g.get("integrated") else 1
    return {"Motherboard": 2, "SuperIO": 2, "Memory": 4, "Storage": 5}.get(t, 6)


def temp_color(v):
    return C["red"] if v >= 85 else C["amber"] if v >= 70 else C["text"]


# ------------------------------------------------------------------ sampler
class Sampler(QObject):
    """Collects readings once a second on a background thread."""
    updated = Signal(object)

    def __init__(self):
        super().__init__()
        self.detail = False          # per-app numbers only while the panel is open
        self._stop = False
        self.ncpu = psutil.cpu_count() or 1
        psutil.cpu_percent(percpu=True)
        self.prev_t = time.time()
        self.prev_net = psutil.net_io_counters()
        self.prev_disk = self._disk_io()
        self.gpu = PdhWildcard({
            "eng": r"\GPU Engine(*)\Utilization Percentage",
            "vram": r"\GPU Adapter Memory(*)\Dedicated Usage",
        })
        self.has_battery = self._battery() is not None
        self.proc_cache, self.exe_cache, self.prev_io = {}, {}, {}
        self.slow_cache, self.slow_t = {}, 0.0
        self._thread = threading.Thread(target=self._loop, name="sampler", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True

    def _loop(self):
        while not self._stop:
            t0 = time.time()
            try:
                self.updated.emit(self.sample())
            except Exception:
                _log_exception(*sys.exc_info())
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    @staticmethod
    def _disk_io():
        try:
            return psutil.disk_io_counters()
        except Exception:
            return None

    @staticmethod
    def _battery():
        try:
            return psutil.sensors_battery()
        except Exception:
            return None

    def _slow(self):
        """Things that barely change: drives, network adapters."""
        if time.time() - self.slow_t < 10 and self.slow_cache:
            return self.slow_cache
        parts = []
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in p.opts or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            parts.append({"mount": p.mountpoint, "total": u.total, "used": u.used,
                          "free": u.free, "pct": u.percent})
        ifaces = []
        try:
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                st = stats.get(name)
                if not st or not st.isup or "loopback" in name.lower():
                    continue
                for a in addrs:
                    if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                        ifaces.append((name, a.address))
                        break
        except Exception:
            pass
        self.slow_cache = {"parts": parts, "ifaces": ifaces}
        self.slow_t = time.time()
        return self.slow_cache

    def _procs(self, dt, gpu_pid):
        agg = defaultdict(lambda: {"cpu": 0.0, "mem": 0, "disk": 0.0, "gpu": 0.0, "exe": None, "n": 0})
        seen, new_io = set(), {}
        for p in psutil.process_iter(["name"]):
            pid = p.pid
            if pid == 0:
                continue
            proc = self.proc_cache.get(pid)
            if proc is None or not proc.is_running():
                proc = p
                self.proc_cache[pid] = p
            seen.add(pid)
            name = (p.info.get("name") or "").strip()
            if not name or name.lower() in ("system idle process", "idle"):
                continue
            if name.lower().endswith(".exe"):
                name = name[:-4]
            a = agg[name]
            a["n"] += 1
            try:
                a["cpu"] += proc.cpu_percent(None) / self.ncpu
            except (psutil.Error, OSError):
                pass
            try:
                a["mem"] += proc.memory_info().rss
            except (psutil.Error, OSError):
                pass
            try:
                io = proc.io_counters()
                tot = io.read_bytes + io.write_bytes
                if pid in self.prev_io:
                    a["disk"] += max(0, tot - self.prev_io[pid]) / dt
                new_io[pid] = tot
            except (psutil.Error, OSError, AttributeError):
                pass
            a["gpu"] = max(a["gpu"], gpu_pid.get(pid, 0.0))
            if a["exe"] is None:
                if pid not in self.exe_cache:
                    try:
                        self.exe_cache[pid] = proc.exe() or ""
                    except (psutil.Error, OSError):
                        self.exe_cache[pid] = ""
                if self.exe_cache[pid]:
                    a["exe"] = self.exe_cache[pid]
        for pid in list(self.proc_cache):
            if pid not in seen:
                self.proc_cache.pop(pid, None)
                self.exe_cache.pop(pid, None)
        self.prev_io = new_io

        def top(key, n=6, floor=0.0):
            rows = [(nm, v["exe"], v[key], v["n"]) for nm, v in agg.items() if v[key] > floor]
            rows.sort(key=lambda r: r[2], reverse=True)
            return rows[:n]

        return {"cpu": top("cpu"), "mem": top("mem"), "disk": top("disk"), "gpu": top("gpu", floor=0.05)}

    def sample(self):
        now = time.time()
        dt = max(1e-3, now - self.prev_t)
        self.prev_t = now

        per = psutil.cpu_percent(percpu=True) or [0.0]
        try:
            f = psutil.cpu_freq()
            freq = f.current if f and f.current else None
        except Exception:
            freq = None
        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
        except Exception:
            sw = None

        net = psutil.net_io_counters()
        down = max(0, net.bytes_recv - self.prev_net.bytes_recv) / dt
        up = max(0, net.bytes_sent - self.prev_net.bytes_sent) / dt
        self.prev_net = net

        dk = self._disk_io()
        rd = wr = 0.0
        if dk and self.prev_disk:
            rd = max(0, dk.read_bytes - self.prev_disk.read_bytes) / dt
            wr = max(0, dk.write_bytes - self.prev_disk.write_bytes) / dt
        self.prev_disk = dk

        gpu = None
        if self.gpu.ok:
            raw = self.gpu.collect()
            total, by_type, pid_pct = parse_gpu(raw.get("eng", []))
            vram = sum(v for _, v in raw.get("vram", []))
            gpu = {"total": total, "engines": by_type, "vram": vram, "pid": pid_pct}

        bat = self._battery()
        slow = self._slow()
        d = {
            "cpu": {"total": sum(per) / len(per), "percore": per, "freq": freq,
                    "cores": len(per), "phys": psutil.cpu_count(logical=False) or len(per),
                    "nproc": len(psutil.pids()), "uptime": now - psutil.boot_time()},
            "mem": {"total": vm.total, "used": vm.total - vm.available, "avail": vm.available,
                    "pct": vm.percent,
                    "swap_used": sw.used if sw else 0, "swap_total": sw.total if sw else 0},
            "gpu": gpu,
            "disk": {"read": rd, "write": wr, "parts": slow["parts"]},
            "net": {"down": down, "up": up, "recv": net.bytes_recv, "sent": net.bytes_sent,
                    "ifaces": slow["ifaces"]},
            "bat": None if bat is None else {"pct": bat.percent, "plugged": bat.power_plugged,
                                             "secs": bat.secsleft},
            "procs": None,
            "temps": read_temps() if IS_WIN or os.path.exists(SENSOR_FILE) else {"status": "off"},
        }
        if self.detail:
            d["procs"] = self._procs(dt, gpu["pid"] if gpu else {})
        return d


# -------------------------------------------------------------- small widgets
def lbl(text="", px=13, color=None, weight=400):
    w = QLabel(text)
    w.setStyleSheet(f"color:{color or C['text']}; font-size:{px}px; font-weight:{weight};"
                    f" background:transparent;")
    return w


def recolor(w, color, px, weight=400):
    w.setStyleSheet(f"color:{color}; font-size:{px}px; font-weight:{weight}; background:transparent;")


class Card(QWidget):
    def __init__(self, color=C["card"], radius=8):
        super().__init__()
        self._c, self._r = qc(color), radius

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._c)
        p.drawRoundedRect(QRectF(self.rect()), self._r, self._r)


class Pill(QLabel):
    def set(self, text, color):
        c = QColor(color)
        self.setText(text)
        self.setStyleSheet(
            f"color:{color}; background:rgba({c.red()},{c.green()},{c.blue()},38);"
            f" border-radius:8px; padding:1px 8px; font-size:11px; font-weight:600;")


class Sparkline(QWidget):
    def __init__(self, color, color2=None, fixed_max=None, min_max=1.0):
        super().__init__()
        self.c1 = QColor(color)
        self.c2 = QColor(color2) if color2 else None
        self.fixed_max, self.min_max = fixed_max, min_max
        self.a, self.b = [], []

    def set_data(self, a, b=None):
        self.a, self.b = list(a), list(b or [])
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 9))
        p.drawRoundedRect(r, 6, 6)
        series = [(self.a, self.c1)] + ([(self.b, self.c2)] if self.c2 else [])
        mx = self.fixed_max or max(self.min_max, max((max(s) for s, _ in series if s), default=0) * 1.15)
        inner = r.adjusted(2, 4, -2, -2)
        step = inner.width() / (HISTORY - 1)
        for s, c in series:
            if len(s) < 2:
                continue
            pts = [QPointF(inner.right() - (len(s) - 1 - i) * step,
                           inner.bottom() - min(1.0, v / mx) * inner.height())
                   for i, v in enumerate(s)]
            line = QPainterPath(pts[0])
            for pt in pts[1:]:
                line.lineTo(pt)
            fill = QPainterPath(line)
            fill.lineTo(pts[-1].x(), inner.bottom())
            fill.lineTo(pts[0].x(), inner.bottom())
            fill.closeSubpath()
            g = QLinearGradient(0, inner.top(), 0, inner.bottom())
            top, bot = QColor(c), QColor(c)
            top.setAlpha(80)
            bot.setAlpha(0)
            g.setColorAt(0, top)
            g.setColorAt(1, bot)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawPath(fill)
            p.setPen(QPen(c, 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawPath(line)


class Bar(QWidget):
    def __init__(self, color=C["cpu"], height=6):
        super().__init__()
        self.color = color
        self.segs = []
        self.setFixedHeight(height)

    def set(self, frac, color=None):
        self.segs = [(frac, color or self.color)]
        self.update()

    def set_segments(self, segs):
        self.segs = segs
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        track = QPainterPath()
        track.addRoundedRect(r, rad, rad)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 24))
        p.drawPath(track)
        p.setClipPath(track)
        x = r.left()
        for frac, color in self.segs:
            w = r.width() * max(0.0, min(1.0, frac))
            if w <= 0:
                continue
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(x, r.top(), w, r.height()), rad, rad)
            x += w


class CoreBars(QWidget):
    """One small vertical bar per logical core."""

    def __init__(self, color):
        super().__init__()
        self.color = QColor(color)
        self.vals = []
        self.setFixedHeight(38)

    def set(self, vals):
        self.vals = vals
        self.update()

    def paintEvent(self, _):
        if not self.vals:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.vals)
        gap = 3 if n <= 24 else 2
        w = (self.width() - gap * (n - 1)) / n
        h = self.height()
        for i, v in enumerate(self.vals):
            x = i * (w + gap)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 20))
            p.drawRoundedRect(QRectF(x, 0, w, h), 2, 2)
            fh = h * min(1.0, v / 100)
            p.setBrush(QColor(C["red"]) if v >= 90 else self.color)
            p.drawRoundedRect(QRectF(x, h - fh, w, fh), 2, 2)


class KV(QWidget):
    """Label / value rows."""

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(0, 1)
        self.rows = []

    def set(self, items):
        while len(self.rows) < len(items):
            k = lbl("", 13, C["sub"])
            v = lbl("", 13, C["text"], 600)
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            i = len(self.rows)
            self.grid.addWidget(k, i, 0)
            self.grid.addWidget(v, i, 1)
            self.rows.append((k, v))
        for i, (k, v) in enumerate(self.rows):
            if i < len(items):
                key, val = items[i][0], items[i][1]
                color = items[i][2] if len(items[i]) > 2 else C["text"]
                header = len(items[i]) > 3 and items[i][3]
                if header:
                    recolor(k, C["text"], 12, 600)
                else:
                    recolor(k, C["sub"], 13)
                k.setText(key)
                v.setText(val)
                recolor(v, color, 13, 600)
                k.show()
                v.show()
            else:
                k.hide()
                v.hide()
        self.setVisible(bool(items))


_icon_provider = None
_icon_cache = {}


def app_icon(name, exe, size=18):
    global _icon_provider
    key = (name, exe)
    if key in _icon_cache:
        return _icon_cache[key]
    pm = QPixmap()
    if IS_WIN and exe:
        try:
            if _icon_provider is None:
                _icon_provider = QFileIconProvider()
            pm = _icon_provider.icon(QFileInfo(exe)).pixmap(size, size)
        except Exception:
            pm = QPixmap()
    if pm.isNull():  # letter avatar
        ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1
        pm = QPixmap(int(size * ratio), int(size * ratio))
        pm.setDevicePixelRatio(ratio)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        hue = sum(map(ord, name)) * 37 % 360
        p.setBrush(QColor.fromHsl(hue, 120, 95))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, size, size), 5, 5)
        f = QFont(p.font())
        f.setPixelSize(int(size * 0.6))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, (name[:1] or "?").upper())
        p.end()
    _icon_cache[key] = pm
    return pm


class AppList(Card):
    NAME_W = 170

    def __init__(self, title, accent, n=5):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 12)
        lay.setSpacing(7)
        self.title = lbl(title, 12, C["sub"], 500)
        lay.addWidget(self.title)
        self.empty = lbl("Collecting…", 12, C["dim"])
        lay.addWidget(self.empty)
        self.rows = []
        for _ in range(n):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            ic = QLabel()
            ic.setFixedSize(18, 18)
            nm = lbl("", 13)
            nm.setFixedWidth(self.NAME_W)
            bar = Bar(accent, 5)
            bar.setMinimumWidth(40)
            val = lbl("", 13, C["text"], 600)
            val.setFixedWidth(76)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            h.addWidget(ic)
            h.addWidget(nm)
            h.addWidget(bar, 1)
            h.addWidget(val)
            lay.addWidget(w)
            self.rows.append((w, ic, nm, bar, val))

    def set_rows(self, rows):
        """rows: list of (name, exe, value, text, count)"""
        if rows is None:
            self.empty.setText("Collecting…")
            self.empty.show()
            for r in self.rows:
                r[0].hide()
            return
        self.empty.setText("Nothing busy right now")
        self.empty.setVisible(not rows)
        top = max((r[2] for r in rows), default=0) or 1
        for i, (w, ic, nm, bar, val) in enumerate(self.rows):
            if i >= len(rows):
                w.hide()
                continue
            name, exe, v, text, count = rows[i]
            ic.setPixmap(app_icon(name, exe))
            nm.ensurePolished()
            shown = f"{name} ({count})" if count > 1 else name
            nm.setText(QFontMetrics(nm.font()).elidedText(shown, Qt.ElideRight, self.NAME_W))
            nm.setToolTip(exe or name)
            bar.set(v / top)
            val.setText(text)
            w.show()


class Hero(Card):
    """Big number, subtitle, status pill and graph."""

    def __init__(self, accent, accent2=None, fixed_max=None, min_max=1.0):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 13)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(3)
        vrow = QHBoxLayout()
        vrow.setSpacing(5)
        self.value = lbl("—", 30, C["text"], 700)
        self.unit = lbl("", 14, C["sub"], 500)
        self.unit.setContentsMargins(0, 0, 0, 5)
        vrow.addWidget(self.value, 0, Qt.AlignBottom)
        vrow.addWidget(self.unit, 0, Qt.AlignBottom)
        vrow.addStretch()
        left.addLayout(vrow)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.sub = lbl("", 12, C["sub"])
        self.pill = Pill()
        srow.addWidget(self.sub)
        srow.addWidget(self.pill)
        srow.addStretch()
        left.addLayout(srow)
        top.addLayout(left, 1)
        self.spark = Sparkline(accent, accent2, fixed_max, min_max)
        self.spark.setFixedSize(128, 52)
        top.addWidget(self.spark, 0, Qt.AlignTop)
        lay.addLayout(top)
        self.bar = Bar(accent, 6)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.extra = QVBoxLayout()
        self.extra.setSpacing(8)
        lay.addLayout(self.extra)
        self.kv = KV()
        lay.addWidget(self.kv)

    def set_value(self, value, unit, color=C["text"]):
        self.value.setText(value)
        recolor(self.value, color, 30, 700)
        self.unit.setText(unit)


class Section(QWidget):
    """A page: uppercase heading + stacked cards."""

    def __init__(self, title):
        super().__init__()
        self.setFixedWidth(PANEL_W)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)
        h = lbl(title.upper(), 11, C["sub"], 600)
        h.setStyleSheet(h.styleSheet() + " letter-spacing:1px;")
        self.lay.addWidget(h)

    def refresh(self, d, hist):
        pass


# --------------------------------------------------------------------- pages
class CpuPage(Section):
    def __init__(self):
        super().__init__("CPU")
        self.hero = Hero(C["cpu"], fixed_max=100)
        self.cores = CoreBars(C["cpu"])
        self.hero.extra.addWidget(self.cores)
        self.apps = AppList("Top apps by CPU", C["cpu"])
        self.lay.addWidget(self.hero)
        self.lay.addWidget(self.apps)

    def refresh(self, d, hist):
        c = d["cpu"]
        st = strain(c["total"], 60, 85)
        self.hero.set_value(f"{c['total']:.0f}", "%", C["red"] if st[0] == "Strained" else C["text"])
        sub = f"{c['phys']} cores · {c['cores']} threads"
        if c["freq"]:
            sub += f" · {c['freq'] / 1000:.2f} GHz"
        self.hero.sub.setText(sub)
        self.hero.pill.set(*st)
        self.hero.spark.set_data(hist["cpu"])
        self.cores.set(c["percore"])
        self.hero.kv.set([("Processes", str(c["nproc"])), ("Up for", fmt_dur(c["uptime"]))])
        p = d["procs"]
        self.apps.set_rows(None if p is None else
                           [(n, e, v, f"{v:.1f}%", k) for n, e, v, k in p["cpu"]])


class MemPage(Section):
    def __init__(self):
        super().__init__("Memory")
        self.hero = Hero(C["mem"], fixed_max=100)
        self.hero.bar.show()
        self.apps = AppList("Top apps by memory", C["mem"])
        self.lay.addWidget(self.hero)
        self.lay.addWidget(self.apps)

    def refresh(self, d, hist):
        m = d["mem"]
        st = strain(m["pct"], 75, 90)
        self.hero.set_value(f"{m['used'] / GIB:.2f}", "GB", C["red"] if st[0] == "Strained" else C["text"])
        self.hero.sub.setText(f"of {m['total'] / GIB:.0f} GB")
        self.hero.pill.set(*st)
        self.hero.spark.set_data(hist["mem"])
        self.hero.bar.set(m["pct"] / 100, C["red"] if st[0] == "Strained" else C["mem"])
        rows = [("In use", f"{fmt_bytes(m['used'])}  ({m['pct']:.0f}%)"),
                ("Available", fmt_bytes(m["avail"]))]
        if m["swap_total"]:
            rows.append(("Page file", f"{fmt_bytes(m['swap_used'])} of {fmt_bytes(m['swap_total'])}"))
        self.hero.kv.set(rows)
        p = d["procs"]
        self.apps.set_rows(None if p is None else
                           [(n, e, v, fmt_bytes(v), k) for n, e, v, k in p["mem"]])


def _pretty_engine(name):
    out = ""
    for ch in name.replace("_", " "):
        if ch.isupper() and out and out[-1].islower():
            out += " "
        out += ch
    return out


class GpuPage(Section):
    def __init__(self):
        super().__init__("GPU")
        self.hero = Hero(C["gpu"], fixed_max=100)
        self.apps = AppList("Top apps by GPU", C["gpu"])
        self.lay.addWidget(self.hero)
        self.lay.addWidget(self.apps)

    def refresh(self, d, hist):
        g = d["gpu"]
        if not g:
            self.hero.set_value("—", "")
            self.hero.sub.setText("GPU counters not available")
            return
        st = strain(g["total"], 60, 90)
        self.hero.set_value(f"{g['total']:.0f}", "%", C["red"] if st[0] == "Strained" else C["text"])
        self.hero.sub.setText(f"VRAM in use {fmt_bytes(g['vram'])}" if g["vram"] else "Busiest engine")
        self.hero.pill.set(*st)
        self.hero.spark.set_data(hist["gpu"])
        eng = sorted(g["engines"].items(), key=lambda kv: kv[1], reverse=True)[:4]
        self.hero.kv.set([(_pretty_engine(k), f"{v:.0f}%") for k, v in eng])
        p = d["procs"]
        self.apps.set_rows(None if p is None else
                           [(n, e, v, f"{v:.0f}%", k) for n, e, v, k in p["gpu"]])


class DiskPage(Section):
    def __init__(self):
        super().__init__("Disk")
        self.hero = Hero(C["disk"], C["net_up"], min_max=1e6)
        self.hero.bar.show()
        self.drives = KV()
        self.hero.extra.addWidget(self.drives)
        self.apps = AppList("Top apps by disk activity", C["disk"])
        self.lay.addWidget(self.hero)
        self.lay.addWidget(self.apps)

    def refresh(self, d, hist):
        k = d["disk"]
        parts = k["parts"]
        sysdrive = (os.environ.get("SystemDrive", "C:") + "\\").upper()
        main = next((p for p in parts if p["mount"].upper() == sysdrive), parts[0] if parts else None)
        if main:
            st = strain(main["pct"], 85, 95)
            self.hero.set_value(f"{main['free'] / GIB:.2f}", "GB free")
            self.hero.sub.setText(f"{main['mount']}  {main['used'] / GIB:.0f} of {main['total'] / GIB:.0f} GB used")
            self.hero.pill.set(*st)
            self.hero.bar.set(main["pct"] / 100, C["red"] if st[0] == "Strained" else C["disk"])
        self.hero.spark.set_data(hist["disk_r"], hist["disk_w"])
        self.drives.set([(f"Drive {p['mount']}", f"{p['free'] / GIB:.0f} GB free of {p['total'] / GIB:.0f} GB",
                          C["red"] if p["pct"] >= 95 else C["text"])
                         for p in parts if p is not main][:4])
        self.hero.kv.set([("Reading", fmt_rate(k["read"]), C["disk"]),
                          ("Writing", fmt_rate(k["write"]), C["net_up"])])
        p = d["procs"]
        self.apps.set_rows(None if p is None else
                           [(n, e, v, fmt_rate(v), c) for n, e, v, c in p["disk"]])


class NetPage(Section):
    def __init__(self):
        super().__init__("Network")
        self.hero = Hero(C["net"], C["net_up"], min_max=50e3)
        self.lay.addWidget(self.hero)
        self.ifaces = Card()
        il = QVBoxLayout(self.ifaces)
        il.setContentsMargins(14, 11, 14, 12)
        il.setSpacing(7)
        il.addWidget(lbl("Connections", 12, C["sub"], 500))
        self.if_kv = KV()
        il.addWidget(self.if_kv)
        self.lay.addWidget(self.ifaces)

    def refresh(self, d, hist):
        n = d["net"]
        num, unit = split_rate(n["down"])
        self.hero.set_value(num, unit + "  ↓")
        self.hero.sub.setText(f"↑ {fmt_rate(n['up'])}")
        self.hero.pill.hide()
        self.hero.spark.set_data(hist["net_d"], hist["net_u"])
        self.hero.kv.set([("Downloading", fmt_rate(n["down"]), C["net"]),
                          ("Uploading", fmt_rate(n["up"]), C["net_up"]),
                          ("Received since boot", fmt_bytes(n["recv"])),
                          ("Sent since boot", fmt_bytes(n["sent"]))])
        self.if_kv.set([(name, ip) for name, ip in n["ifaces"][:4]] or [("No active adapter", "")])


class BatPage(Section):
    def __init__(self):
        super().__init__("Battery")
        self.hero = Hero(C["bat"], fixed_max=100)
        self.hero.bar.show()
        self.lay.addWidget(self.hero)

    def refresh(self, d, hist):
        b = d["bat"]
        if not b:
            self.hero.set_value("—", "")
            self.hero.sub.setText("No battery")
            return
        pct = b["pct"]
        low = pct <= 15 and not b["plugged"]
        self.hero.set_value(f"{pct:.0f}", "%", C["red"] if low else C["text"])
        secs = b["secs"]
        timed = isinstance(secs, (int, float)) and 0 < secs < 10 * 86400
        if b["plugged"]:
            self.hero.sub.setText("Plugged in")
            self.hero.pill.set("Charging" if pct < 100 else "Full", C["green"])
        else:
            self.hero.sub.setText(f"{fmt_dur(secs)} left" if timed else "On battery")
            self.hero.pill.set("On battery", C["red"] if low else C["amber"])
        color = C["red"] if pct <= 15 else C["amber"] if pct <= 35 else C["bat"]
        self.hero.bar.set(pct / 100, color)
        self.hero.spark.set_data(hist["bat"])
        self.hero.kv.set([("Power source", "Charger" if b["plugged"] else "Battery"),
                          ("Time left", fmt_dur(secs) if timed and not b["plugged"] else "—")])


PER_GROUP = {"Cpu": 4, "GpuNvidia": 3, "GpuAmd": 3, "GpuIntel": 3, "Motherboard": 4, "SuperIO": 4,
             "Storage": 1, "Memory": 2}


class TempsPage(Section):
    def __init__(self):
        super().__init__("Temperatures")
        self.on_enable = None
        self.mode = None
        self.hero = Hero(C["temp"], C["gpu"], fixed_max=100)
        self.lay.addWidget(self.hero)

        self.list_card = Card()
        ll = QVBoxLayout(self.list_card)
        ll.setContentsMargins(14, 11, 14, 12)
        ll.setSpacing(7)
        ll.addWidget(lbl("Sensors", 12, C["sub"], 500))
        self.sensors = KV()
        ll.addWidget(self.sensors)
        self.lay.addWidget(self.list_card)

        self.fan_card = Card()
        fl = QVBoxLayout(self.fan_card)
        fl.setContentsMargins(14, 11, 14, 12)
        fl.setSpacing(7)
        fl.addWidget(lbl("Fans", 12, C["sub"], 500))
        self.fans = KV()
        fl.addWidget(self.fans)
        self.lay.addWidget(self.fan_card)

        self.setup = Card()
        sl = QVBoxLayout(self.setup)
        sl.setContentsMargins(14, 13, 14, 14)
        sl.setSpacing(8)
        self.setup_title = lbl("", 14, C["text"], 600)
        self.setup_text = lbl("", 12, C["sub"])
        self.setup_text.setWordWrap(True)
        self.setup_btn = QPushButton("")
        self.setup_btn.setStyleSheet(BTN_CSS.replace(C["card"], C["card_hi"], 1))
        self.setup_btn.setFixedHeight(32)
        self.setup_btn.setCursor(Qt.PointingHandCursor)
        self.setup_btn.clicked.connect(self._action)
        sl.addWidget(self.setup_title)
        sl.addWidget(self.setup_text)
        sl.addWidget(self.setup_btn)
        self.lay.addWidget(self.setup)

    def _action(self):
        if self.mode == "enable" and self.on_enable:
            self.on_enable()
        elif self.mode == "folder" and IS_WIN:
            os.startfile(APP_DIR)
        elif self.mode == "lhm" and IS_WIN:
            exe = find_lhm_exe()
            if exe:
                ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, os.path.dirname(exe), 1)

    def _show_setup(self, title, text, button=None, mode=None):
        self.setup_title.setText(title)
        self.setup_text.setText(text)
        self.mode = mode
        self.setup_btn.setText(button or "")
        self.setup_btn.setVisible(bool(button))
        self.setup.show()

    def refresh(self, d, hist):
        t = d.get("temps") or {"status": "off"}
        status = t.get("status")
        ok = status == "ok"
        self.hero.setVisible(ok)
        self.list_card.setVisible(ok)
        self.fan_card.setVisible(False)
        self.setup.hide()
        if status == "off":
            self._show_setup("Temperatures need admin access",
                             "Like HWMonitor, Glance asks Windows for permission to read the hardware "
                             "sensors. Click below and approve the prompt.",
                             "Enable temperatures", "enable")
        elif status == "starting":
            self._show_setup("Waiting for permission…",
                             "Approve the Windows prompt (it may be flashing on the taskbar).")
        elif status == "declined":
            self._show_setup("Permission was declined",
                             "Glance can't read temperatures without it. Try again whenever you like.",
                             "Try again", "enable")
        elif status == "stopped":
            self._show_setup("Sensor reader stopped", "It isn't running at the moment.",
                             "Start it again", "enable")
        elif status in ("no_lib", "no_pythonnet") and FROZEN:
            self._show_setup("Sensor library missing",
                             "Part of Glance's install is missing. Reinstalling Glance will fix it."
                             + (f"\n\nDetails: {t['error']}" if t.get("error") else ""))
        elif status in ("no_lib", "no_pythonnet"):
            self._show_setup("One-time setup needed",
                             "Run Install-Temps.bat in the Glance folder. It downloads the open-source "
                             "LibreHardwareMonitor sensor library, then come back and click Try again."
                             + (f"\n\nDetails: {t['error']}" if t.get("error") else ""),
                             "Open Glance folder", "folder")
        elif status == "error":
            self._show_setup("Couldn't read the sensors", t.get("error", "Unknown error"),
                             "Try again", "enable")
        if not ok:
            return

        cpu, gpu = t.get("cpu"), t.get("gpu")
        main = cpu if cpu is not None else gpu
        if main is not None:
            label = "Hot" if main >= 85 else "Warm" if main >= 70 else "Cool"
            self.hero.set_value(f"{main:.0f}", "°C  CPU" if cpu is not None else "°C  GPU",
                                temp_color(main))
            self.hero.pill.set(label, C["red"] if main >= 85 else C["amber"] if main >= 70 else C["green"])
            self.hero.pill.show()
        else:
            self.hero.set_value("—", "")
            self.hero.pill.hide()
        bits = []
        if cpu is not None and gpu is not None:
            bits.append(f"GPU {gpu:.0f}°C")
        if t.get("cpu_power"):
            bits.append(f"CPU {t['cpu_power']:.0f} W")
        if t.get("gpu_power"):
            bits.append(f"GPU {t['gpu_power']:.0f} W")
        self.hero.sub.setText(" · ".join(bits))
        self.hero.spark.set_data(hist["temp_cpu"], hist["temp_gpu"])

        rows, fans = [], []
        has_dgpu = any(g["type"].startswith("Gpu") and not g.get("integrated") for g in t.get("groups", []))
        for g in t.get("groups", []):
            if g.get("integrated") and has_dgpu:
                continue
            if g["temps"] and len(rows) < 22:
                limit = PER_GROUP.get(g["type"], 3)
                temps = g["temps"]
                if g["type"] == "Cpu":
                    key = [x for x in temps if x[0] in CPU_TEMP_NAMES]
                    temps = key + [x for x in temps if x not in key]
                rows.append((g["name"], "", C["text"], True))
                rows += [(f"   {n}", f"{v:.0f}°C", temp_color(v)) for n, v in temps[:limit]]
            fans += [(n if g["type"] in ("Motherboard", "SuperIO") or n.startswith("GPU")
                      else f"{g['name']} {n}", f"{v:,} RPM") for n, v in g["fans"]]
        self.sensors.set(rows[:24])
        self.fans.set(fans[:6])
        self.fan_card.setVisible(bool(fans))
        if cpu is None and not t.get("pawnio"):
            self._show_setup("CPU sensors need one more step",
                             "CPU and motherboard temperatures need the free PawnIO driver. Open "
                             "LibreHardwareMonitor below, approve the prompt and accept its driver "
                             "install, then close it and restart Glance.",
                             "Open LibreHardwareMonitor", "lhm")


class Tile(Card):
    def __init__(self, title, accent, accent2=None, fixed_max=None, min_max=1.0):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.on_click = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        self.title = lbl(title, 12, C["sub"], 500)
        self.value = lbl("—", 20, C["text"], 700)
        self.sub = lbl("", 11, C["dim"])
        self.spark = Sparkline(accent, accent2, fixed_max, min_max)
        self.spark.setFixedHeight(34)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)
        lay.addSpacing(6)
        lay.addWidget(self.spark)

    def mousePressEvent(self, e):
        if self.on_click and e.button() == Qt.LeftButton:
            self.on_click()


class OverviewPage(Section):
    def __init__(self, has_gpu, has_bat):
        super().__init__("Overview")
        grid = QGridLayout()
        grid.setSpacing(8)
        self.tiles = {
            "cpu": Tile("CPU", C["cpu"], fixed_max=100),
            "mem": Tile("Memory", C["mem"], fixed_max=100),
        }
        if has_gpu:
            self.tiles["gpu"] = Tile("GPU", C["gpu"], fixed_max=100)
        self.tiles["disk"] = Tile("Disk", C["disk"], C["net_up"], min_max=1e6)
        self.tiles["net"] = Tile("Network", C["net"], C["net_up"], min_max=50e3)
        self.tiles["temp"] = Tile("Temps", C["temp"], C["gpu"], fixed_max=100)
        if has_bat:
            self.tiles["bat"] = Tile("Battery", C["bat"], fixed_max=100)
        for i, t in enumerate(self.tiles.values()):
            grid.addWidget(t, i // 2, i % 2)
        self.lay.addLayout(grid)

    def refresh(self, d, hist):
        c, m, k, n = d["cpu"], d["mem"], d["disk"], d["net"]
        t = self.tiles
        t["cpu"].value.setText(f"{c['total']:.0f}%")
        recolor(t["cpu"].value, C["red"] if c["total"] >= 85 else C["text"], 20, 700)
        t["cpu"].sub.setText(f"{c['cores']} threads")
        t["cpu"].spark.set_data(hist["cpu"])
        t["mem"].value.setText(f"{m['pct']:.0f}%")
        recolor(t["mem"].value, C["red"] if m["pct"] >= 90 else C["text"], 20, 700)
        t["mem"].sub.setText(f"{m['used'] / GIB:.1f} of {m['total'] / GIB:.0f} GB")
        t["mem"].spark.set_data(hist["mem"])
        if "gpu" in t and d["gpu"]:
            g = d["gpu"]
            t["gpu"].value.setText(f"{g['total']:.0f}%")
            recolor(t["gpu"].value, C["red"] if g["total"] >= 90 else C["text"], 20, 700)
            t["gpu"].sub.setText(f"VRAM {fmt_bytes(g['vram'])}" if g["vram"] else "")
            t["gpu"].spark.set_data(hist["gpu"])
        t["disk"].value.setText(fmt_rate(k["read"] + k["write"]))
        t["disk"].sub.setText(f"R {fmt_rate(k['read'])} · W {fmt_rate(k['write'])}")
        t["disk"].spark.set_data(hist["disk_r"], hist["disk_w"])
        t["net"].value.setText(f"↓ {fmt_rate(n['down'])}")
        t["net"].sub.setText(f"↑ {fmt_rate(n['up'])}")
        t["net"].spark.set_data(hist["net_d"], hist["net_u"])
        tp = d.get("temps") or {}
        if tp.get("status") == "ok" and (tp.get("cpu") is not None or tp.get("gpu") is not None):
            main = tp["cpu"] if tp.get("cpu") is not None else tp["gpu"]
            t["temp"].value.setText(f"{main:.0f}\u00b0C")
            recolor(t["temp"].value, temp_color(main), 20, 700)
            t["temp"].sub.setText(f"GPU {tp['gpu']:.0f}\u00b0C" if tp.get("cpu") is not None and tp.get("gpu")
                                  is not None else ("CPU" if tp.get("cpu") is not None else "GPU"))
            t["temp"].spark.set_data(hist["temp_cpu"], hist["temp_gpu"])
        else:
            t["temp"].value.setText("\u2014")
            recolor(t["temp"].value, C["text"], 20, 700)
            t["temp"].sub.setText("Click to set up")
            t["temp"].spark.set_data([])
        if "bat" in t and d["bat"]:
            b = d["bat"]
            t["bat"].value.setText(f"{b['pct']:.0f}%")
            t["bat"].sub.setText("Charging" if b["plugged"] else "On battery")
            t["bat"].spark.set_data(hist["bat"])


# --------------------------------------------------------------------- panel
class CurrentStack(QStackedWidget):
    """A stacked widget that sizes itself to the visible page only."""

    def sizeHint(self):
        w = self.currentWidget()
        return w.sizeHint() if w else super().sizeHint()

    def minimumSizeHint(self):
        w = self.currentWidget()
        return w.minimumSizeHint() if w else super().minimumSizeHint()


BTN_CSS = f"""
QPushButton {{ background:{C['card']}; color:{C['text']}; border:none; border-radius:6px;
              font-size:13px; font-weight:600; padding:0 12px; }}
QPushButton:hover {{ background:{C['card_hi']}; }}
QPushButton:pressed {{ background:{C['border']}; }}
"""


class Panel(QWidget):
    def __init__(self, has_gpu, has_bat):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(APP)
        self.last_hide = 0.0
        self.anchor = None
        self.on_visibility = None
        self.settings_menu = None
        self.data = self.hist = None
        cfg = load_config()
        self.backdrop_mode = cfg.get("backdrop", "dwm") if IS_WIN else "none"
        self.tint_alpha = float(cfg.get("tint_alpha", 0.80 if self.backdrop_mode == "dwm" else 0.88))
        self.backdrop = "none"           # what actually took effect
        self.shadow = 0 if self.backdrop_mode != "none" else SHADOW
        self.corners = cfg.get("corners", "round")
        self.tab_buttons = []

        root = QVBoxLayout(self)
        m = self.shadow + 12
        root.setContentsMargins(m, m, m, m)
        root.setSpacing(10)
        root.setSizeConstraint(QLayout.SetFixedSize)

        # tab bar
        tabs_card = Card(C["card"], 8)
        tabs_card.setFixedWidth(PANEL_W)
        tl = QHBoxLayout(tabs_card)
        tl.setContentsMargins(4, 4, 4, 4)
        tl.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.stack = CurrentStack()

        pages = [("All", C["text"], OverviewPage(has_gpu, has_bat)),
                 ("CPU", C["cpu"], CpuPage()),
                 ("Memory", C["mem"], MemPage())]
        if has_gpu:
            pages.append(("GPU", C["gpu"], GpuPage()))
        self.temps_page = TempsPage()
        pages += [("Disk", C["disk"], DiskPage()), ("Network", C["net"], NetPage()),
                  ("Temps", C["temp"], self.temps_page)]
        if has_bat:
            pages.append(("Battery", C["bat"], BatPage()))
        self.pages = []
        self.key_index = {}
        keys = {"CPU": "cpu", "Memory": "mem", "GPU": "gpu", "Disk": "disk",
                "Network": "net", "Battery": "bat", "Temps": "temp"}
        for i, (name, color, page) in enumerate(pages):
            b = QPushButton({"Network": "Net", "Battery": "Batt", "Memory": "RAM",
                             "Temps": "Temp"}.get(name, name))
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.setToolTip(name)
            self.tab_buttons.append(b)
            self.group.addButton(b, i)
            tl.addWidget(b, 1)
            self.stack.addWidget(page)
            self.pages.append(page)
            if name in keys:
                self.key_index[keys[name]] = i
        for key, tile in pages[0][2].tiles.items():
            if key in self.key_index:
                tile.on_click = (lambda idx=self.key_index[key]: self.select(idx))
        self.group.idClicked.connect(self.select)
        root.addWidget(tabs_card)
        root.addWidget(self.stack)

        # footer
        foot = QHBoxLayout()
        foot.setSpacing(8)
        tm = QPushButton("Open Task Manager")
        tm.setStyleSheet(BTN_CSS)
        tm.setFixedHeight(34)
        tm.setCursor(Qt.PointingHandCursor)
        tm.clicked.connect(self.open_task_manager)
        gear = QPushButton("⚙")
        gear.setStyleSheet(BTN_CSS + "QPushButton { font-size:16px; padding:0; }")
        gear.setFixedSize(38, 34)
        gear.setToolTip("Settings")
        gear.setCursor(Qt.PointingHandCursor)
        gear.clicked.connect(lambda: self.settings_menu and self.settings_menu.popup(
            gear.mapToGlobal(gear.rect().bottomLeft())))
        quit_b = QPushButton("Quit")
        quit_b.setStyleSheet(BTN_CSS)
        quit_b.setFixedHeight(34)
        quit_b.setCursor(Qt.PointingHandCursor)
        quit_b.clicked.connect(QApplication.quit)
        foot.addWidget(tm, 1)
        foot.addWidget(gear)
        foot.addWidget(quit_b)
        root.addLayout(foot)

        self.apply_theme()
        self.select(0)

    # -- theme
    def apply_theme(self):
        """Selected tab uses the Windows accent, like Start and Settings do."""
        for b in self.tab_buttons:
            b.setStyleSheet(f"""
                QPushButton {{ background:transparent; color:{C['sub']}; border:none; border-radius:6px;
                              font-size:12px; font-weight:600; padding:0 6px; }}
                QPushButton:hover {{ color:{C['text']}; background:{C['card_hi']}; }}
                QPushButton:checked {{ background:{THEME['light2']}; color:{on_color(THEME['light2'])}; }}""")
        if self.backdrop == "swca" and self.isVisible():
            self.backdrop = apply_backdrop(int(self.winId()), "swca", THEME["start"], self.tint_alpha)
        self.update()

    def _ensure_backdrop(self):
        if self.backdrop_mode != "none" and self.backdrop == "none":
            self.backdrop = apply_backdrop(int(self.winId()), self.backdrop_mode, THEME["start"],
                                           self.tint_alpha)
            self._apply_corners()
            if self.backdrop == "none":          # fall back to the painted look
                self.backdrop_mode = "none"
                self.shadow = SHADOW
                m = SHADOW + 12
                self.layout().setContentsMargins(m, m, m, m)

    # -- behaviour
    def select(self, idx):
        self.group.button(idx).setChecked(True)
        for i, page in enumerate(self.pages):   # only the visible page counts for size
            pol = QSizePolicy.Preferred if i == idx else QSizePolicy.Ignored
            page.setSizePolicy(pol, pol)
        self.stack.setCurrentIndex(idx)
        if self.data:
            self.pages[idx].refresh(self.data, self.hist)
        self.stack.updateGeometry()
        self.layout().activate()
        self.adjustSize()
        self.place()

    def refresh(self, data, hist):
        self.data, self.hist = data, hist
        if self.isVisible():
            self.stack.currentWidget().refresh(data, hist)
            self.stack.updateGeometry()
            self.layout().activate()

    def open_task_manager(self):
        self.hide()
        try:
            if IS_WIN:
                os.startfile("taskmgr.exe")
        except OSError:
            pass

    def show_at(self, anchor):
        self.anchor = anchor
        if self.data:
            self.stack.currentWidget().refresh(self.data, self.hist)
        self.layout().activate()
        self.adjustSize()
        self.place()
        self.show()
        self._ensure_backdrop()
        self.raise_()
        self.activateWindow()

    def place(self):
        if self.anchor is None:
            return
        a = self.anchor
        screen = QGuiApplication.screenAt(a.center()) or QGuiApplication.primaryScreen()
        avail, full = screen.availableGeometry(), screen.geometry()
        w, h = self.width(), self.height()
        x = a.center().x() - w // 2
        sh = self.shadow
        gap = 6 if sh else 12                            # Windows flyouts float ~12px off the taskbar
        x = max(avail.left() - sh + (0 if sh else 12), min(x, avail.right() - w + sh - (0 if sh else 12)))
        if a.center().y() < full.center().y():          # taskbar at top: drop down
            y = max(a.bottom(), avail.top()) - sh + gap
        else:                                            # taskbar at bottom: pop up
            y = min(a.top(), avail.bottom()) - h + sh - gap
        self.move(x, y)

    def _apply_corners(self):
        if self.backdrop == "none" or self.corners != "round":
            return
        dpr = self.devicePixelRatioF()
        round_window(int(self.winId()), round(self.width() * dpr), round(self.height() * dpr),
                     round(CORNER_RADIUS * dpr))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_corners()
        if self.isVisible():
            self.place()

    def showEvent(self, e):
        super().showEvent(e)
        if self.on_visibility:
            self.on_visibility(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.last_hide = time.time()
        if self.on_visibility:
            self.on_visibility(False)

    def keyPressEvent(self, e):
        k = int(e.key())
        one = _ival(Qt.Key.Key_1)
        if k == _ival(Qt.Key.Key_Escape):
            self.hide()
        elif one <= k <= one + 8 and k - one < len(self.pages):
            self.select(k - one)
        else:
            super().keyPressEvent(e)

    def _paint_edge(self, p):
        """Anti-aliased hairline that hides the stair-steps of the window clip."""
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setBrush(Qt.NoBrush)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = CORNER_RADIUS if self.corners == "round" else 0
        p.setPen(QPen(QColor(255, 255, 255, 38), 1))
        p.drawRoundedRect(r, rad, rad)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.backdrop == "dwm":
            # Windows draws the acrylic; we add the Start-menu tint on top of it
            tint = QColor(THEME["start"])
            tint.setAlphaF(self.tint_alpha)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), tint)
            self._paint_edge(p)
            return
        if self.backdrop == "swca":
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.transparent)
            self._paint_edge(p)
            return
        sh = self.shadow
        body = QRectF(self.rect()).adjusted(sh, sh, -sh, -sh)
        p.setPen(Qt.NoPen)
        for i in range(sh, 0, -1):          # soft shadow
            alpha = int(36 * (1 - i / sh) ** 2)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(body.adjusted(-i, -i + 3, i, i + 3), 8 + i, 8 + i)
        bg = QColor(THEME["start"]) if THEME.get("prevalence") else QColor(C["bg"])
        p.setBrush(bg)
        p.setPen(QPen(qc(C["border"]), 1))
        p.drawRoundedRect(body, 8, 8)


# ---------------------------------------------------------------- tray icon
def make_tray_icon(style, value, hot):
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 0, size)
    if hot:
        g.setColorAt(0, QColor("#ff6b6b"))
        g.setColorAt(1, QColor("#d62f3a"))
    else:
        g.setColorAt(0, QColor(THEME["light3"]))
        g.setColorAt(1, QColor(THEME["light1"]))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawRoundedRect(QRectF(2, 2, size - 4, size - 4), 15, 15)
    if style == "pulse":
        path = QPainterPath(QPointF(9, 34))
        for x, y in ((21, 34), (27, 19), (35, 47), (41, 28), (45, 34), (55, 34)):
            path.lineTo(x, y)
        p.setPen(QPen(QColor("white"), 5.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    else:
        text = str(int(round(value)))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(40 if len(text) <= 2 else 29)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, text)
    p.end()
    return QIcon(pm)


# ------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def self_command():
    """(program, leading args) that start this copy of Glance."""
    if FROZEN:
        return sys.executable, ""
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return exe, f'"{os.path.abspath(__file__)}" '


def autostart_command():
    exe, args = self_command()
    return f'"{exe}" {args}'.strip()


def autostart_enabled():
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on):
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


# ------------------------------------------------------------- controller
class Glance(QObject):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP, APP)
        self.icon_style = self.settings.value("icon_style", "pulse")
        self.hist = defaultdict(lambda: deque(maxlen=HISTORY))
        self.icon_key = None
        self.last = None

        THEME.update(read_theme())
        self._write_theme_debug()
        self.sampler = Sampler()
        self.panel = Panel(self.sampler.gpu.ok, self.sampler.has_battery)
        self.panel.on_visibility = self._visibility
        self.panel.temps_page.on_enable = lambda: self.start_temps(True)
        try:
            os.makedirs(os.path.dirname(SENSOR_FILE), exist_ok=True)
            with open(SENSOR_FILE + ".owner", "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        self.temps_state = None          # None, "starting" or "declined"
        self.temps_launch_t = 0.0

        self.menu = self._build_menu()
        self.panel.settings_menu = self.menu

        self.tray = QSystemTrayIcon(make_tray_icon("pulse", 0, False))
        self.tray.setToolTip(APP)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.show()

        self.sampler.updated.connect(self.on_data)
        self.sampler.start()
        if IS_WIN and self._temps_wanted():
            QTimer.singleShot(800, self.start_temps)
        self.theme_timer = QTimer(self)
        self.theme_timer.timeout.connect(self._check_theme)
        self.theme_timer.start(2000)

    def _write_theme_debug(self):
        try:
            with open(os.path.join(HERE, "theme-debug.json"), "w", encoding="utf-8") as f:
                json.dump(THEME, f, indent=1)
        except OSError:
            pass

    def _check_theme(self):
        """Follow accent-colour changes live, like Start does."""
        t = read_theme()
        if t != THEME:
            THEME.update(t)
            self._write_theme_debug()
            self.panel.apply_theme()
            self.icon_key = None
            if self.last:
                self._update_tray(self.last)

    def _build_menu(self):
        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{ background:#2b2b2b; color:{C['text']}; border:1px solid #3d3d3d;
                     border-radius:8px; padding:4px; font-size:13px; }}
            QMenu::item {{ padding:6px 22px 6px 26px; border-radius:5px; }}
            QMenu::item:selected {{ background:#3a3a3a; }}
            QMenu::separator {{ height:1px; background:#3d3d3d; margin:4px 6px; }}""")
        ver = QAction(f"Glance {__version__}", menu)
        ver.setEnabled(False)
        menu.addAction(ver)
        open_a = QAction("Open Glance", menu)
        open_a.triggered.connect(self.toggle_panel)
        menu.addAction(open_a)
        menu.addSeparator()

        sub = menu.addMenu("Tray icon shows")
        sub.setStyleSheet(menu.styleSheet())
        grp = QActionGroup(sub)
        for key, label in (("pulse", "Pulse (turns red under strain)"), ("cpu", "CPU %"), ("mem", "Memory %")):
            a = QAction(label, sub)
            a.setCheckable(True)
            a.setChecked(self.icon_style == key)
            a.triggered.connect(lambda _=False, k=key: self._set_style(k))
            grp.addAction(a)
            sub.addAction(a)

        if IS_WIN:
            self.temps_action = QAction("Read temperatures (asks for admin)", menu)
            self.temps_action.setCheckable(True)
            self.temps_action.setChecked(self._temps_wanted())
            self.temps_action.toggled.connect(self._toggle_temps)
            menu.addAction(self.temps_action)
            auto = QAction("Start with Windows", menu)
            auto.setCheckable(True)
            auto.setChecked(autostart_enabled())
            auto.toggled.connect(self._toggle_autostart)
            menu.addAction(auto)
        menu.addSeparator()
        quit_a = QAction("Quit Glance", menu)
        quit_a.triggered.connect(QApplication.quit)
        menu.addAction(quit_a)
        return menu

    def _temps_wanted(self):
        return str(self.settings.value("temps", "false")).lower() == "true"

    def _toggle_temps(self, on):
        self.settings.setValue("temps", "true" if on else "false")
        if on:
            self.start_temps(True)
        else:
            try:
                open(STOP_FILE, "w").close()
            except OSError:
                pass
            self.temps_state = None

    def _helper_alive(self):
        t = read_temps()
        if t.get("status") in ("off", "stopped"):
            return False
        try:
            with open(SENSOR_FILE, encoding="utf-8") as f:
                pid = json.load(f).get("pid", 0)
            return psutil.pid_exists(pid)
        except (OSError, ValueError):
            return False

    def start_temps(self, user_initiated=False):
        """Launch the sensor reader with admin rights (Windows shows its permission prompt)."""
        if not IS_WIN or self.temps_state == "starting" or self._helper_alive():
            return
        if user_initiated and not self._temps_wanted():
            self.settings.setValue("temps", "true")
            if hasattr(self, "temps_action"):
                self.temps_action.blockSignals(True)
                self.temps_action.setChecked(True)
                self.temps_action.blockSignals(False)
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass
        self.temps_state = "starting"
        self.temps_launch_t = time.time()
        exe, args = self_command()
        params = f'{args}--sensors --parent {os.getpid()} --out "{SENSOR_FILE}"'

        def launch():
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, APP_DIR, 0)
            self.temps_state = "launched" if r > 32 else "declined"

        threading.Thread(target=launch, daemon=True).start()

    def _temps_status(self, t):
        status = t.get("status")
        if status in ("off", "stopped"):
            if self.temps_state == "declined":
                return {"status": "declined"}
            if self.temps_state == "starting" or (
                    self.temps_state == "launched" and time.time() - self.temps_launch_t < 20):
                return {"status": "starting"}
            if status == "stopped" and not self._temps_wanted():
                return {"status": "off"}
        elif self.temps_state is not None:
            self.temps_state = None
        return t

    def _toggle_autostart(self, on):
        try:
            set_autostart(on)
        except OSError as e:
            QMessageBox.warning(None, APP, f"Couldn't change the startup setting:\n{e}")

    def _set_style(self, key):
        self.icon_style = key
        self.settings.setValue("icon_style", key)
        self.icon_key = None
        if self.last:
            self._update_tray(self.last)

    def _visibility(self, visible):
        self.sampler.detail = visible

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_panel()

    def toggle_panel(self):
        if self.panel.isVisible():
            self.panel.hide()
            return
        if time.time() - self.panel.last_hide < 0.3:   # the click that just closed it
            return
        geo = self.tray.geometry()
        if not geo.isValid() or geo.isEmpty():
            geo = QRect(QCursor.pos(), QSize(1, 1))
        self.sampler.detail = True
        self.panel.show_at(geo)

    def on_data(self, d):
        d["temps"] = self._temps_status(d.get("temps") or {"status": "off"})
        self.last = d
        h = self.hist
        tp = d["temps"]
        if tp.get("status") == "ok":
            h["temp_cpu"].append(tp["cpu"] if tp.get("cpu") is not None else 0.0)
            h["temp_gpu"].append(tp["gpu"] if tp.get("gpu") is not None else 0.0)
        h["cpu"].append(d["cpu"]["total"])
        h["mem"].append(d["mem"]["pct"])
        h["gpu"].append(d["gpu"]["total"] if d["gpu"] else 0.0)
        h["disk_r"].append(d["disk"]["read"])
        h["disk_w"].append(d["disk"]["write"])
        h["net_d"].append(d["net"]["down"])
        h["net_u"].append(d["net"]["up"])
        h["bat"].append(d["bat"]["pct"] if d["bat"] else 0.0)
        self._update_tray(d)
        self.panel.refresh(d, h)

    def _update_tray(self, d):
        cpu, mem = d["cpu"]["total"], d["mem"]["pct"]
        gpu = d["gpu"]["total"] if d["gpu"] else 0
        tp = d.get("temps") or {}
        ctemp = tp.get("cpu") if tp.get("status") == "ok" else None
        gtemp = tp.get("gpu") if tp.get("status") == "ok" else None
        hot = (cpu >= 85 or mem >= 90 or gpu >= 90 or (ctemp or 0) >= 90 or (gtemp or 0) >= 90)
        value = cpu if self.icon_style == "cpu" else mem if self.icon_style == "mem" else 0
        key = (self.icon_style, int(round(value)), hot)
        if key != self.icon_key:
            self.icon_key = key
            self.tray.setIcon(make_tray_icon(self.icon_style, value, hot))
        tip = f"CPU {cpu:.0f}%  ·  RAM {mem:.0f}%"
        if d["gpu"]:
            tip += f"  ·  GPU {gpu:.0f}%"
        if ctemp is not None or gtemp is not None:
            tip += "\n" + "   ".join(x for x in (f"CPU {ctemp:.0f}\u00b0C" if ctemp is not None else "",
                                                 f"GPU {gtemp:.0f}\u00b0C" if gtemp is not None else "") if x)
        tip += f"\n↓ {fmt_rate(d['net']['down'])}   ↑ {fmt_rate(d['net']['up'])}"
        self.tray.setToolTip(tip)

    def shutdown(self):
        self.sampler.stop()
        self.tray.hide()


def pick_font():
    fams = set(QFontDatabase.families())
    for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"):
        if f in fams:
            return f
    return QApplication.font().family()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)
    f = QFont(pick_font())
    f.setPixelSize(13)
    app.setFont(f)

    lock = QLockFile(QDir.temp().absoluteFilePath("glance.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, APP, "Glance is already running. Look for its icon in the tray.")
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP, "No system tray found.")
        return 1

    g = Glance()
    app.aboutToQuit.connect(g.shutdown)
    g.tray.showMessage(APP, "Running in the tray. Click the icon for your system stats.",
                       QSystemTrayIcon.Information, 3000)
    rc = app.exec()
    lock.unlock()
    return rc


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def selftest(out_path):
    """Used by the release build: prove the packaged app can start and find its parts."""
    report = {"version": __version__, "frozen": FROZEN}
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication(sys.argv)
        Panel(False, False)
        report["qt"] = "ok"
        s = Sampler()
        d = s.sample()
        report["sampler"] = f"cpu {d['cpu']['total']:.0f}% mem {d['mem']['pct']:.0f}%"
        report["gpu_counters"] = s.gpu.ok
        dll = find_lhm_dll()
        report["lhm_dll"] = dll
        import pythonnet
        pythonnet.load("netfx")
        import clr
        clr.AddReference(dll)
        from LibreHardwareMonitor.Hardware import Computer  # noqa: F401
        report["pythonnet"] = "ok"
        report["ok"] = True
        del app
    except Exception as e:
        report["ok"] = False
        report["error"] = f"{type(e).__name__}: {e}"
        report["trace"] = traceback.format_exc()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest(_arg("--selftest", "selftest.json")))
    if "--sensors" in sys.argv:
        try:
            sensor_helper(_arg("--out", SENSOR_FILE), int(_arg("--parent", "0")))
        except Exception:
            _log_exception(*sys.exc_info())
        sys.exit(0)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        _log_exception(*sys.exc_info())
        if IS_WIN:
            ctypes.windll.user32.MessageBoxW(None, f"Glance crashed. Details are in:\n{LOG_PATH}", APP, 0x10)
        raise
