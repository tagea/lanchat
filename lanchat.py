"""
LAN Chat — простой мессенджер для компьютеров в одной Wi-Fi сети.
Работает на Windows и macOS.

Запуск из исходника:  python lanchat.py   (на Mac: python3 lanchat.py)
Флаг --minimized — запуск свернутым (используется автозапуском).
"""
import json
import os
import platform
import plistlib
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog

APP_NAME = "LANChat"
TCP_PORT = 50505          # порт для сообщений
UDP_PORT = 50506          # порт для автопоиска собеседников
APP_ID = "lanchat"
MY_NAME = socket.gethostname().split(".")[0]
IS_MAC = platform.system() == "Darwin"
IS_WIN = os.name == "nt"

events = queue.Queue()    # события из сетевых потоков -> в окно
peers = {}                # ip -> имя компьютера

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".lanchat.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # пакет не отправляется
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


MY_IP = get_local_ip()


def add_peer(ip, name):
    if peers.get(ip) != name:
        peers[ip] = name
        events.put(("peers", None))


# ---------- Автозапуск ----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.lanchat.app.plist")


def launch_command():
    """Команда, которой система запустит программу при входе."""
    if getattr(sys, "frozen", False):          # собранное приложение
        return [sys.executable]
    exe = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw                          # без чёрного окна консоли
    return [exe, os.path.abspath(__file__)]


def autostart_enabled():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
            return True
        except OSError:
            return False
    if IS_MAC:
        return os.path.exists(MAC_PLIST)
    return False


def set_autostart(enabled):
    cmd = launch_command() + ["--minimized"]
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    elif IS_MAC:
        if enabled:
            os.makedirs(os.path.dirname(MAC_PLIST), exist_ok=True)
            with open(MAC_PLIST, "wb") as f:
                plistlib.dump({
                    "Label": "com.lanchat.app",
                    "ProgramArguments": cmd,
                    "RunAtLoad": True,
                    "LimitLoadToSessionType": "Aqua",
                }, f)
        elif os.path.exists(MAC_PLIST):
            os.remove(MAC_PLIST)


# ---------- Сеть ----------

def udp_announcer():
    """Каждые 3 секунды сообщает в сеть: «я здесь»."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps({"app": APP_ID, "name": MY_NAME}).encode()
    subnet_bcast = MY_IP.rsplit(".", 1)[0] + ".255"
    while True:
        for addr in ("255.255.255.255", subnet_bcast):
            try:
                s.sendto(msg, (addr, UDP_PORT))
            except OSError:
                pass
        time.sleep(3)


def udp_listener():
    """Слушает объявления других компьютеров."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", UDP_PORT))
    except OSError:
        return                                  # автопоиск недоступен, остаётся ручной IP
    while True:
        try:
            data, (ip, _) = s.recvfrom(2048)
            if ip == MY_IP:
                continue
            info = json.loads(data.decode())
            if info.get("app") == APP_ID:
                add_peer(ip, info.get("name", ip))
        except (OSError, ValueError):
            continue


def handle_client(conn, ip):
    chunks = []
    try:
        conn.settimeout(5)
        while True:
            b = conn.recv(4096)
            if not b:
                break
            chunks.append(b)
        msg = json.loads(b"".join(chunks).decode())
    except (OSError, ValueError):
        return
    finally:
        conn.close()
    name = msg.get("name", ip)
    add_peer(ip, name)
    events.put(("msg", (name, msg.get("text", ""))))


def open_server_socket():
    """Открывает порт сообщений. Если занят — программа уже запущена."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WIN:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", TCP_PORT))
    srv.listen()
    return srv


def tcp_server(srv):
    while True:
        conn, (ip, _) = srv.accept()
        threading.Thread(target=handle_client, args=(conn, ip), daemon=True).start()


def send_message(text):
    targets = dict(peers)
    if not targets:
        events.put(("info", "Собеседник не найден. Проверьте, что программа запущена на втором компьютере."))
        return
    payload = json.dumps({"name": MY_NAME, "text": text}).encode()
    for ip, name in targets.items():
        try:
            with socket.create_connection((ip, TCP_PORT), timeout=3) as s:
                s.sendall(payload)
        except OSError:
            events.put(("info", f"Не удалось доставить сообщение на {name} ({ip})"))


# ---------- Окно ----------

class ChatApp:
    def __init__(self, root, start_minimized=False):
        self.root = root
        self.popup = None
        root.title(f"LAN Chat — {MY_NAME}")
        root.geometry("460x540")
        root.minsize(320, 300)
        root.protocol("WM_DELETE_WINDOW", self.hide)   # крестик сворачивает, не закрывает
        if IS_MAC:
            # клик по иконке в Dock снова открывает окно
            root.createcommand("::tk::mac::ReopenApplication", self.show)

        top = tk.Frame(root)
        top.pack(fill="x", padx=8, pady=(8, 0))
        self.status = tk.Label(top, anchor="w", fg="#555")
        self.status.pack(side="left", fill="x", expand=True)

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=8, pady=4)
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(bar, text="Запускать при входе в систему",
                       variable=self.autostart_var,
                       command=self.toggle_autostart).pack(side="left")
        tk.Button(bar, text="Выход", command=root.destroy).pack(side="right")
        tk.Button(bar, text="Добавить IP", command=self.add_ip).pack(side="right", padx=4)

        # Без звука: 0 = звук включён, -1 = выключен бессрочно, иначе — до этого момента
        self.settings = load_settings()
        self.mute_until = self.settings.get("mute_until", 0)
        mute_bar = tk.Frame(root)
        mute_bar.pack(fill="x", padx=8, pady=(0, 4))
        self.mute_btn = tk.Menubutton(mute_bar, relief="raised", padx=8)
        menu = tk.Menu(self.mute_btn, tearoff=False)
        menu.add_command(label="Включить звук и уведомления", command=lambda: self.set_mute(0))
        menu.add_separator()
        menu.add_command(label="Без звука на 1 час", command=lambda: self.set_mute(time.time() + 3600))
        menu.add_command(label="Без звука на 8 часов", command=lambda: self.set_mute(time.time() + 8 * 3600))
        menu.add_command(label="Без звука до завтра (8:00)", command=lambda: self.set_mute(self.tomorrow_8am()))
        menu.add_command(label="Без звука, пока не включу", command=lambda: self.set_mute(-1))
        self.mute_btn.config(menu=menu)
        self.mute_btn.pack(side="left")
        self.mute_text = None
        self.update_mute_label()

        self.log = scrolledtext.ScrolledText(root, wrap="word", state="disabled",
                                             font=("Helvetica", 12))
        self.log.pack(fill="both", expand=True, padx=8)
        self.log.tag_config("me", foreground="#1a5fb4")
        self.log.tag_config("them", foreground="#26a269")
        self.log.tag_config("info", foreground="#999")

        bottom = tk.Frame(root)
        bottom.pack(fill="x", padx=8, pady=8)
        self.entry = tk.Entry(bottom, font=("Helvetica", 12))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.send())
        tk.Button(bottom, text="Отправить", command=self.send).pack(side="right", padx=(6, 0))
        self.entry.focus_set()

        self.update_status()
        self.write("info", f"Мой адрес: {MY_IP}. Ищу собеседника в сети…")
        root.after(200, self.poll)
        if start_minimized:
            root.after(100, root.iconify)

    # ----- Без звука -----

    @staticmethod
    def tomorrow_8am():
        t = time.localtime(time.time() + 86400)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 8, 0, 0, 0, 0, -1))

    def is_muted(self):
        return self.mute_until == -1 or time.time() < self.mute_until

    def set_mute(self, until):
        self.mute_until = until
        self.settings["mute_until"] = until
        save_settings(self.settings)
        self.update_mute_label()
        self.write("info", "Звук и уведомления включены" if until == 0
                   else f"Режим без звука: {self.mute_text.lower()}")

    def update_mute_label(self):
        if not self.is_muted():
            self.mute_until = 0
            text = "Звук: вкл"
        elif self.mute_until == -1:
            text = "Без звука"
        else:
            fmt = "%H:%M" if time.localtime(self.mute_until).tm_mday == time.localtime().tm_mday else "%d.%m %H:%M"
            text = "Без звука до " + time.strftime(fmt, time.localtime(self.mute_until))
        if text != self.mute_text:
            self.mute_text = text
            self.mute_btn.config(text=text + "  ▾")
            mark = "" if text == "Звук: вкл" else " [без звука]"
            self.root.title(f"LAN Chat — {MY_NAME}{mark}")

    def toggle_autostart(self):
        want = self.autostart_var.get()
        try:
            set_autostart(want)
            self.write("info", "Автозапуск включён" if want else "Автозапуск выключен")
        except OSError as e:
            self.autostart_var.set(not want)
            messagebox.showerror("Автозапуск", f"Не удалось изменить настройку:\n{e}")

    def write(self, tag, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.config(state="disabled")
        self.log.see("end")

    def update_status(self):
        if peers:
            names = ", ".join(f"{n} ({ip})" for ip, n in peers.items())
            self.status.config(text=f"В сети: {names}")
        else:
            self.status.config(text="Собеседник пока не найден")

    def send(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.write("me", f"[{time.strftime('%H:%M')}] Я: {text}")
        threading.Thread(target=send_message, args=(text,), daemon=True).start()

    def add_ip(self):
        ip = simpledialog.askstring("Добавить вручную",
                                    "IP-адрес второго компьютера\n(он показан в его окне):",
                                    parent=self.root)
        if ip:
            add_peer(ip.strip(), ip.strip())

    def hide(self):
        self.root.iconify()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.entry.focus_set()

    def app_has_focus(self):
        try:
            return self.root.focus_get() is not None and self.root.state() == "normal"
        except (KeyError, tk.TclError):
            return False

    def notify(self, name, text):
        """Всплывающее окошко поверх всех программ."""
        if self.is_muted():
            return                            # сообщение есть в чате, но без звука и окошка
        self.root.bell()
        if self.app_has_focus():
            return
        if self.popup is not None:
            try:
                self.popup.destroy()
            except tk.TclError:
                pass

        p = tk.Toplevel(self.root)
        self.popup = p
        if not IS_MAC:
            p.overrideredirect(True)          # без рамки на Windows
        else:
            p.title("Новое сообщение")
        p.attributes("-topmost", True)
        p.configure(bg="#2b2d31")

        w, h = 330, 110
        sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
        x = sw - w - 20
        y = 40 if IS_MAC else sh - h - 60     # Mac: справа вверху, Windows: над панелью задач
        p.geometry(f"{w}x{h}+{x}+{y}")

        short = text if len(text) <= 120 else text[:117] + "…"
        tk.Label(p, text=name, bg="#2b2d31", fg="#ffffff", anchor="w",
                 font=("Helvetica", 12, "bold")).pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(p, text=short, bg="#2b2d31", fg="#dddddd", anchor="nw", justify="left",
                 wraplength=w - 24, font=("Helvetica", 11)).pack(fill="both", expand=True, padx=12)

        def open_chat(_=None):
            p.destroy()
            self.show()

        for widget in [p] + list(p.winfo_children()):
            widget.bind("<Button-1>", open_chat)
        p.after(8000, lambda: p.winfo_exists() and p.destroy())

    def poll(self):
        try:
            while True:
                kind, data = events.get_nowait()
                if kind == "msg":
                    name, text = data
                    self.write("them", f"[{time.strftime('%H:%M')}] {name}: {text}")
                    self.notify(name, text)
                elif kind == "peers":
                    self.update_status()
                elif kind == "info":
                    self.write("info", data)
        except queue.Empty:
            pass
        self.update_mute_label()               # таймер «без звука» истёк — вернуть звук
        self.root.after(200, self.poll)


def main():
    try:
        srv = open_server_socket()
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("LAN Chat", "LAN Chat уже запущен.")
        return

    # если программу переместили, обновляем путь в автозапуске
    if autostart_enabled():
        try:
            set_autostart(True)
        except OSError:
            pass

    threading.Thread(target=tcp_server, args=(srv,), daemon=True).start()
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=udp_announcer, daemon=True).start()

    root = tk.Tk()
    ChatApp(root, start_minimized="--minimized" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
