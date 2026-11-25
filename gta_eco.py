"""
GTA Eco - Công cụ treo máy GTA V tiết kiệm tài nguyên (Windows)
Chỉ chỉnh ở phía hệ điều hành (priority, CPU core, EcoQoS, RAM).
Không inject, không đọc/ghi bộ nhớ game.
"""
import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import psutil
import pystray
from PIL import Image, ImageDraw
import raid_alert

try:
    import winreg
except ImportError:  # không phải Windows
    winreg = None

APP_NAME = "GTAEco"
GAME_NAMES = ("GTA5.exe", "GTA5_Enhanced.exe")
CFG_DIR = os.path.join(os.environ.get("APPDATA", "."), APP_NAME)
CFG_FILE = os.path.join(CFG_DIR, "config.json")

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
psapi = ctypes.windll.psapi

PRIORITIES = {
    "Thấp nhất (Idle)": psutil.IDLE_PRIORITY_CLASS,
    "Dưới bình thường": psutil.BELOW_NORMAL_PRIORITY_CLASS,
    "Bình thường": psutil.NORMAL_PRIORITY_CLASS,
}
TIMER_ACTIONS = ["Không", "Đóng game", "Tắt máy tính"]
HIDE_MODES = ["Không", "Thu nhỏ xuống thanh tác vụ", "Ẩn hoàn toàn"]

NCPU = psutil.cpu_count(logical=True) or 2
DEFAULT_CFG = {
    "enabled": False,
    "priority": "Thấp nhất (Idle)",
    "cores": list(range(min(2, NCPU))),
    "hide_mode": "Ẩn hoàn toàn",
    "ecoqos": True,
    "mem_low": True,
    "trim": True,
    "trim_minutes": 5,
    "timer_action": "Không",
    "timer_minutes": 120,
    "autostart": False,
    "start_hidden": False,
    "close_to_tray": True,
    "tg_token": "",
    "tg_chat": "",
    "notify_raid": True,
    "notify_closed": True,
    "rec_enabled": True,
    "audio_device": "CABLE Output",
    "audio_threshold": 0.01,
    "match_threshold": 0.55,
    "keep_days": 3,
}


# ---------------------------------------------------------------- cấu hình
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    cfg["enabled"] = False  # mỗi lần mở app luôn ở chế độ CHƠI
    return cfg


def save_cfg(cfg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def set_autostart(on):
    if winreg is None:
        return
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        cmd = f'"{exe}" "{os.path.abspath(__file__)}"'
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    try:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ------------------------------------------------------------ Windows API
class PowerThrottling(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong)]


class MemPriority(ctypes.Structure):
    _fields_ = [("MemoryPriority", ctypes.c_ulong)]


def _set_proc_info(pid, info_class, struct):
    h = kernel32.OpenProcess(0x0200, False, pid)  # PROCESS_SET_INFORMATION
    if not h:
        return False
    ok = kernel32.SetProcessInformation(h, info_class, ctypes.byref(struct),
                                        ctypes.sizeof(struct))
    kernel32.CloseHandle(h)
    return bool(ok)


def set_ecoqos(pid, on):
    return _set_proc_info(pid, 4, PowerThrottling(1, 1, 1 if on else 0))


def set_mem_priority(pid, low):
    return _set_proc_info(pid, 0, MemPriority(1 if low else 5))


def trim_ram(pid):
    h = kernel32.OpenProcess(0x0500, False, pid)  # QUERY_INFO | SET_QUOTA
    if h:
        psapi.EmptyWorkingSet(h)
        kernel32.CloseHandle(h)


_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def game_windows(pid, only_visible=False):
    found = []

    def cb(hwnd, _):
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.GetWindowTextLengthW(hwnd) > 0:
            if not only_visible or user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return found


def show_game_windows(pid):
    """Hiện lại mọi cửa sổ của game (kể cả đang bị ẩn / thu nhỏ)."""
    for hwnd in game_windows(pid):
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return True


def find_game():
    for p in psutil.process_iter(["name"]):
        if p.info["name"] in GAME_NAMES:
            return p
    return None


# ------------------------------------------------------------------ engine
class Engine(threading.Thread):
    def __init__(self, cfg, log):
        super().__init__(daemon=True)
        self.cfg, self.log = cfg, log
        self.version = 0
        self.applied = -1
        self.proc = None
        self.start_time = None
        self.timer_fired = False
        self.touched = False
        self.last_trim = 0
        self.status = "Đang chờ GTA V…"
        self.info = ""
        self.expected_close = False
        self.notify = lambda msg: None

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                self.log(f"Lỗi: {e}")
            time.sleep(2)

    def tick(self):
        cfg = self.cfg
        if self.proc and not self.proc.is_running():
            self.log("Game đã đóng.")
            if self.cfg["enabled"] and self.cfg["notify_closed"] and not self.expected_close:
                self.notify("⚠️ GTA V đã đóng hoặc bị crash khi đang treo máy.")
            self.expected_close = False
            self.proc = self.start_time = None
            self.applied = -1
            self.touched = False
        if not self.proc:
            self.info = ""
            self.proc = find_game()
            if not self.proc:
                self.status = "Đang chờ GTA V…"
                return
            self.proc.cpu_percent(None)
            self.start_time = time.time()
            self.timer_fired = False
            self.applied = -1
            self.log(f"Đã thấy {self.proc.name()} (PID {self.proc.pid})")

        if not cfg["enabled"]:
            if self.applied != -2:
                self.restore()
                self.applied = -2
            self.status = "Chế độ CHƠI – không can thiệp vào game"
        else:
            if self.applied != self.version:
                self.apply()
                self.applied = self.version
            self.status = "Chế độ TREO MÁY – đang tiết kiệm tài nguyên ✔"
            if cfg["trim"] and time.time() - self.last_trim >= cfg["trim_minutes"] * 60:
                trim_ram(self.proc.pid)
                self.last_trim = time.time()

        cpu = self.proc.cpu_percent(None) / NCPU
        ram = self.proc.memory_info().rss / 1024 ** 2
        self.info = f"CPU {cpu:.1f}%  ·  RAM {ram:.0f} MB"
        self.check_timer()

    def apply(self):
        cfg, pid = self.cfg, self.proc.pid
        try:
            self.touched = True
            self.proc.nice(PRIORITIES.get(cfg["priority"], psutil.IDLE_PRIORITY_CLASS))
            cores = [c for c in cfg["cores"] if c < NCPU] or list(range(NCPU))
            self.proc.cpu_affinity(cores)
            set_ecoqos(pid, cfg["ecoqos"])
            set_mem_priority(pid, cfg["mem_low"])
            self.apply_hide()
            self.log(f"Đã áp dụng: {cfg['priority']}, core {cores}, "
                     f"EcoQoS={'bật' if cfg['ecoqos'] else 'tắt'}")
        except psutil.AccessDenied:
            self.log("Thiếu quyền – hãy chạy app bằng Run as administrator.")

    def apply_hide(self):
        pid = self.proc.pid
        show_game_windows(pid)  # reset trước khi áp chế độ mới
        mode = self.cfg["hide_mode"]
        if mode == "Không":
            return
        cmd = 0 if mode == "Ẩn hoàn toàn" else 6  # SW_HIDE / SW_MINIMIZE
        for hwnd in game_windows(pid, only_visible=True):
            user32.ShowWindow(hwnd, cmd)
        self.log("Game đang chạy ngầm (" + mode.lower() + "). "
                 "Bấm Ctrl+Alt+F10 để quay lại chơi.")

    def restore(self):
        if not self.touched:
            return
        self.touched = False
        try:
            show_game_windows(self.proc.pid)
            self.proc.nice(psutil.NORMAL_PRIORITY_CLASS)
            self.proc.cpu_affinity(list(range(NCPU)))
            set_ecoqos(self.proc.pid, False)
            set_mem_priority(self.proc.pid, False)
            self.log("Đã trả game về thiết lập mặc định (chế độ CHƠI).")
        except Exception as e:
            self.log(f"Không khôi phục được: {e}")

    def remaining(self):
        if (self.cfg["timer_action"] == "Không" or not self.start_time
                or self.timer_fired):
            return None
        return max(0, self.cfg["timer_minutes"] * 60 - (time.time() - self.start_time))

    def check_timer(self):
        left = self.remaining()
        if left is None or left > 0:
            return
        self.timer_fired = True
        action = self.cfg["timer_action"]
        self.log(f"Hết giờ hẹn → {action}")
        if self.cfg["notify_closed"]:
            self.notify(f"⏰ Hết giờ hẹn → {action}")
        if action == "Đóng game":
            self.expected_close = True
            self.proc.terminate()
        elif action == "Tắt máy tính":
            os.system("shutdown /s /t 30")
            self.log("Máy sẽ tắt sau 30 giây (gõ 'shutdown /a' trong CMD để huỷ).")


class Hotkey(threading.Thread):
    """Phím tắt toàn cục Ctrl+Alt+F10 để bật/tắt chế độ treo máy ngay trong game."""

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback

    def run(self):
        u = ctypes.windll.user32
        # MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, VK_F10
        if not u.RegisterHotKey(None, 1, 0x0001 | 0x0002 | 0x4000, 0x79):
            self.callback(False)
            return
        msg = ctypes.wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.callback(True)


# --------------------------------------------------------------------- GUI
def make_icon(on=True):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(46, 160, 67, 255) if on else (128, 128, 128, 255))
    d.rectangle((28, 14, 36, 34), fill="white")
    d.ellipse((24, 38, 40, 54), fill="white")
    return img


class App:
    def __init__(self):
        self.cfg = load_cfg()
        self.root = tk.Tk()
        self.root.title("GTA Eco – Treo máy tiết kiệm tài nguyên")
        self.root.geometry("560x840")
        self.root.resizable(False, False)
        self.engine = Engine(self.cfg, self.log)
        self.engine.notify = self.notify
        self.rec = raid_alert.Recorder(self.cfg, self.log, self.on_raid,
                                       lambda: None)
        self.build_ui()
        self.engine.start()
        self.rec.start()
        self.setup_tray()
        Hotkey(self.on_hotkey).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.cfg["start_hidden"]:
            self.root.withdraw()
        self.refresh()

    # ---- giao diện
    def build_ui(self):
        cfg = self.cfg
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        r, t2 = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(r, text="Treo máy")
        nb.add(t2, text="Chuông điện thoại")
        self.build_raid_tab(t2)
        pad = {"padx": 12, "pady": 4}

        self.status_var = tk.StringVar()
        self.info_var = tk.StringVar()
        self.timer_var = tk.StringVar()
        top = ttk.LabelFrame(r, text="Trạng thái")
        top.pack(fill="x", **pad)
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=8, pady=2)
        ttk.Label(top, textvariable=self.info_var).pack(anchor="w", padx=8)
        ttk.Label(top, textvariable=self.timer_var).pack(anchor="w", padx=8, pady=(0, 4))

        self.v_enabled = tk.BooleanVar(value=cfg["enabled"])
        self.btn = tk.Button(r, font=("Segoe UI", 12, "bold"), fg="white",
                             relief="flat", pady=10, command=self.toggle_afk)
        self.btn.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="Phím tắt: Ctrl + Alt + F10 (dùng được ngay khi đang trong game)",
                  foreground="gray").pack(anchor="w", padx=12)

        fh = ttk.LabelFrame(r, text="Chạy ngầm GTA V (khi bật chế độ treo máy)")
        fh.pack(fill="x", **pad)
        row = ttk.Frame(fh); row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="Cửa sổ game:").pack(side="left")
        self.v_hide = tk.StringVar(value=cfg["hide_mode"])
        cb = ttk.Combobox(row, textvariable=self.v_hide, values=HIDE_MODES,
                          state="readonly", width=26)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", self.sync)
        ttk.Button(fh, text="Hiện cửa sổ game ngay", command=self.show_game
                   ).pack(fill="x", padx=8, pady=(0, 6))

        f1 = ttk.LabelFrame(r, text="CPU")
        f1.pack(fill="x", **pad)
        row = ttk.Frame(f1); row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="Độ ưu tiên:").pack(side="left")
        self.v_prio = tk.StringVar(value=cfg["priority"])
        cb = ttk.Combobox(row, textvariable=self.v_prio, values=list(PRIORITIES),
                          state="readonly", width=22)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", self.sync)

        ttk.Label(f1, text="Core CPU cho phép game dùng:").pack(anchor="w", padx=8)
        grid = ttk.Frame(f1); grid.pack(fill="x", padx=8, pady=4)
        self.v_cores = []
        for i in range(NCPU):
            v = tk.BooleanVar(value=i in cfg["cores"])
            ttk.Checkbutton(grid, text=str(i), variable=v, command=self.sync
                            ).grid(row=i // 8, column=i % 8, sticky="w", padx=4)
            self.v_cores.append(v)

        f2 = ttk.LabelFrame(r, text="Điện năng & RAM")
        f2.pack(fill="x", **pad)
        self.v_eco = tk.BooleanVar(value=cfg["ecoqos"])
        self.v_mem = tk.BooleanVar(value=cfg["mem_low"])
        self.v_trim = tk.BooleanVar(value=cfg["trim"])
        ttk.Checkbutton(f2, text="Chế độ Eco (EcoQoS – hạ xung, giảm nhiệt)",
                        variable=self.v_eco, command=self.sync).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(f2, text="Hạ ưu tiên bộ nhớ (RAM bị trưng dụng trước)",
                        variable=self.v_mem, command=self.sync).pack(anchor="w", padx=8, pady=2)
        row = ttk.Frame(f2); row.pack(fill="x", padx=8, pady=2)
        ttk.Checkbutton(row, text="Thu gọn RAM mỗi", variable=self.v_trim,
                        command=self.sync).pack(side="left")
        self.v_trim_min = tk.IntVar(value=cfg["trim_minutes"])
        sp = ttk.Spinbox(row, from_=1, to=120, width=5, textvariable=self.v_trim_min,
                         command=self.sync)
        sp.pack(side="left", padx=4)
        sp.bind("<KeyRelease>", self.sync)
        ttk.Label(row, text="phút").pack(side="left")

        f3 = ttk.LabelFrame(r, text="Hẹn giờ (tính từ lúc thấy game)")
        f3.pack(fill="x", **pad)
        row = ttk.Frame(f3); row.pack(fill="x", padx=8, pady=6)
        self.v_tact = tk.StringVar(value=cfg["timer_action"])
        cb = ttk.Combobox(row, textvariable=self.v_tact, values=TIMER_ACTIONS,
                          state="readonly", width=14)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self.sync)
        ttk.Label(row, text="sau").pack(side="left", padx=6)
        self.v_tmin = tk.IntVar(value=cfg["timer_minutes"])
        sp = ttk.Spinbox(row, from_=1, to=1440, width=6, textvariable=self.v_tmin,
                         command=self.sync)
        sp.pack(side="left")
        sp.bind("<KeyRelease>", self.sync)
        ttk.Label(row, text="phút").pack(side="left", padx=4)

        f4 = ttk.LabelFrame(r, text="Ứng dụng")
        f4.pack(fill="x", **pad)
        self.v_auto = tk.BooleanVar(value=cfg["autostart"])
        self.v_hidden = tk.BooleanVar(value=cfg["start_hidden"])
        self.v_tray = tk.BooleanVar(value=cfg["close_to_tray"])
        for text, var in (("Chạy cùng Windows", self.v_auto),
                          ("Khởi động ẩn (chỉ hiện ở khay hệ thống)", self.v_hidden),
                          ("Bấm X để thu vào khay thay vì thoát", self.v_tray)):
            ttk.Checkbutton(f4, text=text, variable=var, command=self.sync
                            ).pack(anchor="w", padx=8, pady=1)

        ttk.Button(r, text="Thu xuống chạy ngầm", command=self.root.withdraw
                   ).pack(fill="x", padx=12, pady=4)

        lf = ttk.LabelFrame(r, text="Nhật ký")
        lf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(lf, height=6, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

    def sync(self, *_):
        c = self.cfg
        try:
            c["enabled"] = self.v_enabled.get()
            c["priority"] = self.v_prio.get()
            c["hide_mode"] = self.v_hide.get()
            c["cores"] = [i for i, v in enumerate(self.v_cores) if v.get()]
            c["ecoqos"] = self.v_eco.get()
            c["mem_low"] = self.v_mem.get()
            c["trim"] = self.v_trim.get()
            c["trim_minutes"] = max(1, self.v_trim_min.get())
            c["timer_action"] = self.v_tact.get()
            c["timer_minutes"] = max(1, self.v_tmin.get())
            if c["autostart"] != self.v_auto.get():
                set_autostart(self.v_auto.get())
            c["autostart"] = self.v_auto.get()
            c["start_hidden"] = self.v_hidden.get()
            c["close_to_tray"] = self.v_tray.get()
        except tk.TclError:  # ô số đang trống
            return
        save_cfg(c)
        self.engine.version += 1
        self.engine.timer_fired = False

    def show_game(self, *_):
        p = self.engine.proc or find_game()
        if p:
            show_game_windows(p.pid)
            self.log("Đã hiện cửa sổ game.")
        else:
            self.log("Không thấy GTA V đang chạy.")

    def toggle_afk(self, *_):
        self.v_enabled.set(not self.v_enabled.get())
        self.sync()
        self.log("Đã BẬT chế độ treo máy." if self.cfg["enabled"] else "Đã TẮT chế độ treo máy → chế độ CHƠI.")

    def on_hotkey(self, ok):
        if ok:
            self.root.after(0, self.toggle_afk)
        else:
            self.log("Không đăng ký được phím tắt Ctrl+Alt+F10 (đã bị app khác dùng).")

    def refresh(self):
        e = self.engine
        self.pb_level["value"] = min(self.rec.level / 0.1, 1.0) * 100
        n = len(self.rec.templates)
        self.v_samples.set(f"Tiếng chuông đã nạp sẵn: {n} file" if n else
                           "⚠ Chưa có file chuông trong thư mục ringtone/ – chưa thể cảnh báo")
        self.rec_var.set("● Đang nghe âm thanh game" if self.rec.stream is not None
                         else "○ Không ghi (bật chế độ treo máy để ghi)")
        on = self.cfg["enabled"]
        self.btn.config(
            text="⏸  ĐANG TREO MÁY – bấm để TẮT (quay về chơi)" if on
            else "▶  ĐANG CHƠI – bấm để BẬT chế độ treo máy",
            bg="#2ea043" if on else "#6e7681", activebackground="#2ea043" if on else "#6e7681")
        if getattr(self, "_icon_on", None) != on:
            self._icon_on = on
            self.tray.icon = make_icon(on)
        self.status_var.set(e.status)
        self.info_var.set(e.info or "—")
        left = e.remaining()
        if left is None:
            self.timer_var.set("Hẹn giờ: không dùng")
        else:
            m, s = divmod(int(left), 60)
            h, m = divmod(m, 60)
            self.timer_var.set(f"Hẹn giờ ({self.cfg['timer_action']}): còn {h:02d}:{m:02d}:{s:02d}")
        self.root.after(1000, self.refresh)

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"

        def _w():
            self.logbox.config(state="normal")
            self.logbox.insert("end", line)
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        try:
            self.root.after(0, _w)
        except Exception:
            pass

    # ---- tab cảnh báo raid
    def build_raid_tab(self, t):
        cfg, pad = self.cfg, {"padx": 12, "pady": 4}

        f = ttk.LabelFrame(t, text="Thông báo Telegram")
        f.pack(fill="x", **pad)
        self.v_tok = tk.StringVar(value=cfg["tg_token"])
        self.v_chat = tk.StringVar(value=cfg["tg_chat"])
        for i, (text, var, mask) in enumerate((("Bot token:", self.v_tok, "•"),
                                               ("Chat ID:", self.v_chat, ""))):
            ttk.Label(f, text=text).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            e = ttk.Entry(f, textvariable=var, width=46, show=mask)
            e.grid(row=i, column=1, padx=4)
            e.bind("<KeyRelease>", self.sync_raid)
        row = ttk.Frame(f)
        row.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Button(row, text="Lấy Chat ID", command=self.tg_get_chat).pack(side="left")
        ttk.Button(row, text="Gửi thử", command=self.tg_test).pack(side="left", padx=6)
        self.v_nraid = tk.BooleanVar(value=cfg["notify_raid"])
        self.v_nclosed = tk.BooleanVar(value=cfg["notify_closed"])
        ttk.Checkbutton(f, text="Báo khi có chuông điện thoại trong game", variable=self.v_nraid,
                        command=self.sync_raid).grid(row=3, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Checkbutton(f, text="Báo khi game đóng/crash hoặc hết giờ hẹn", variable=self.v_nclosed,
                        command=self.sync_raid).grid(row=4, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Label(f, foreground="gray", wraplength=500,
                  text="Tạo bot: nhắn @BotFather → /newbot để lấy token. Sau đó nhắn 1 tin bất kỳ "
                       "cho bot và bấm 'Lấy Chat ID'.").grid(row=5, column=0, columnspan=2,
                                                            sticky="w", padx=8, pady=(2, 6))

        fa = ttk.LabelFrame(t, text="Âm thanh game (qua VB-Cable)")
        fa.pack(fill="x", **pad)
        self.v_rec = tk.BooleanVar(value=cfg["rec_enabled"])
        ttk.Checkbutton(fa, text="Nghe tiếng chuông điện thoại khi bật chế độ treo máy",
                        variable=self.v_rec, command=self.sync_raid).pack(anchor="w", padx=8, pady=2)
        row = ttk.Frame(fa)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Thiết bị ghi:").pack(side="left")
        self.v_dev = tk.StringVar(value=cfg["audio_device"])
        self.cb_dev = ttk.Combobox(row, textvariable=self.v_dev, width=36,
                                   values=raid_alert.list_inputs())
        self.cb_dev.pack(side="left", padx=6)
        self.cb_dev.bind("<<ComboboxSelected>>", self.sync_raid)
        self.cb_dev.bind("<KeyRelease>", self.sync_raid)
        ttk.Button(row, text="↻", width=3,
                   command=lambda: self.cb_dev.config(values=raid_alert.list_inputs())).pack(side="left")

        self.v_thr = tk.DoubleVar(value=cfg["audio_threshold"])
        self.v_match = tk.DoubleVar(value=cfg["match_threshold"])
        for text, var, lo, hi in (("Ngưỡng có tiếng (nhỏ = nhạy):", self.v_thr, 0.001, 0.1),
                                  ("Độ khớp tối thiểu để báo:", self.v_match, 0.3, 0.95)):
            row = ttk.Frame(fa)
            row.pack(fill="x", padx=8, pady=2)
            ttk.Label(row, text=text, width=30).pack(side="left")
            ttk.Scale(row, from_=lo, to=hi, variable=var, command=self.sync_raid,
                      length=200).pack(side="left")

        row = ttk.Frame(fa)
        row.pack(fill="x", padx=8, pady=(4, 6))
        self.pb_level = ttk.Progressbar(row, length=220, maximum=100)
        self.pb_level.pack(side="left")
        ttk.Button(row, text="Kiểm tra mức âm (10 giây)", command=self.test_audio
                   ).pack(side="left", padx=8)
        self.rec_var = tk.StringVar()
        ttk.Label(fa, textvariable=self.rec_var, foreground="gray").pack(anchor="w", padx=8)
        self.v_samples = tk.StringVar()
        ttk.Label(fa, textvariable=self.v_samples, foreground="gray").pack(anchor="w", padx=8, pady=(0, 6))


    def sync_raid(self, *_):
        c = self.cfg
        try:
            c["tg_token"] = self.v_tok.get().strip()
            c["tg_chat"] = self.v_chat.get().strip()
            c["notify_raid"] = self.v_nraid.get()
            c["notify_closed"] = self.v_nclosed.get()
            c["rec_enabled"] = self.v_rec.get()
            c["audio_device"] = self.v_dev.get().strip()
            c["audio_threshold"] = round(self.v_thr.get(), 4)
            c["match_threshold"] = round(self.v_match.get(), 2)
        except tk.TclError:
            return
        save_cfg(c)

    def notify(self, msg):
        c = self.cfg
        if not (c["tg_token"] and c["tg_chat"]):
            return

        def work():
            ok, err = raid_alert.tg_send(c["tg_token"], c["tg_chat"], msg)
            if not ok:
                self.log(f"Gửi Telegram lỗi: {err}")
        threading.Thread(target=work, daemon=True).start()

    def on_raid(self, label, score):
        self.log(f"📞 Có chuông điện thoại trong game (độ khớp {score:.2f})")
        if self.cfg["notify_raid"]:
            self.notify(f"📞 GTA có cuộc gọi đến (tiếng chuông, độ khớp {score:.2f})\nVào game kiểm tra.")
        try:
            self.tray.notify("Có chuông điện thoại trong game", "GTA Eco")
        except Exception:
            pass

    def tg_get_chat(self):
        tok = self.v_tok.get().strip()

        def work():
            cid, err = raid_alert.tg_find_chat_id(tok)

            def done():
                if cid:
                    self.v_chat.set(cid)
                    self.sync_raid()
                    self.log(f"Đã lấy Chat ID: {cid}")
                else:
                    self.log(f"Chưa lấy được Chat ID: {err}")
            self.root.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def tg_test(self):
        self.sync_raid()

        def work():
            ok, err = raid_alert.tg_send(self.cfg["tg_token"], self.cfg["tg_chat"],
                                         "✅ GTA Eco: kết nối Telegram thành công.")
            self.log("Đã gửi thử Telegram." if ok else f"Gửi thử lỗi: {err}")
        threading.Thread(target=work, daemon=True).start()

    def test_audio(self):
        self.rec.test_until = time.time() + 10
        self.log("Đang nghe 10 giây – hãy cho game phát tiếng và xem thanh mức âm.")

    # ---- khay hệ thống
    def setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Mở GTA Eco", lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem(
                lambda _: "⏸ TẮT chế độ treo máy (về chơi)" if self.cfg["enabled"]
                else "▶ BẬT chế độ treo máy",
                self.tray_toggle),
            pystray.MenuItem("Hiện cửa sổ game", lambda: self.root.after(0, self.show_game)),
            pystray.MenuItem("Thoát", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon(APP_NAME, make_icon(), "GTA Eco", menu)
        self.tray.run_detached()

    def tray_toggle(self, *_):
        self.root.after(0, self.toggle_afk)

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def on_close(self):
        if self.cfg["close_to_tray"]:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        if self.engine.proc and self.cfg["enabled"]:
            self.engine.restore()
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def single_instance():
    kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}_mutex")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if single_instance():
        App().run()
