"""
LAN Chat 2.0 — мессенджер для компьютеров в одной Wi-Fi сети (Windows и macOS).

Интерфейс — HTML/CSS в собственном окне приложения (pywebview), не в браузере.
Возможности: автопоиск собеседника, всплывающие уведомления поверх всех окон,
реакции эмодзи, светлая/тёмная тема, режим «без звука», автозапуск, история.

Запуск из исходника:  pip install pywebview  и  python lanchat.py
"""
import base64
import json
import logging
import math
import os
import platform
import plistlib
import re
import shlex
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import wave
import webbrowser
import zlib
from collections import OrderedDict

import webview

APP_NAME = "LANChat"
APP_VERSION = "2.4.0"            # при сборке подставляется номер сборки
APP_ID = "lanchat"
TCP_PORT = 50505
UDP_PORT = 50506
IS_MAC = platform.system() == "Darwin"
IS_WIN = os.name == "nt"

SELFTEST_DIR = os.environ.get("LANCHAT_SELFTEST")       # режим самопроверки для сборки
DATA_DIR = SELFTEST_DIR or os.path.expanduser("~")
SETTINGS_PATH = os.path.join(DATA_DIR, ".lanchat.json")
HISTORY_PATH = os.path.join(DATA_DIR, ".lanchat_history.json")
LOG_PATH = os.path.join(DATA_DIR, ".lanchat_error.log")
SOUND_DIR = os.path.join(DATA_DIR, ".lanchat_sounds")
HISTORY_LIMIT = 1000

lock = threading.RLock()


# ---------- Хранение ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


settings = load_json(SETTINGS_PATH, {})
if not isinstance(settings, dict):
    settings = {}
settings.setdefault("uid", uuid.uuid4().hex)
settings.setdefault("name", socket.gethostname().split(".")[0])
settings.setdefault("theme", "system")
settings.setdefault("mute_until", 0)
settings.setdefault("sound", "drop")
settings.setdefault("popup_time", 60)          # секунд; 0 — пока не закроют
save_json(SETTINGS_PATH, settings)

messages = load_json(HISTORY_PATH, [])
if not isinstance(messages, list):
    messages = []
peers = {}            # ip -> имя
peer_uids = {}        # uid собеседника -> его текущий ip

# ---------- Картинки ----------
# Картинки не копируются никуда: у отправителя сообщение хранит путь к файлу на его компьютере,
# а получатель при показе запрашивает файл у отправителя напрямую по Wi-Fi и держит его только в памяти.
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
MAX_IMAGE = 25 * 1024 * 1024
PASTE_DIR = os.path.join(SELFTEST_DIR, "pasted") if SELFTEST_DIR else \
    os.path.join(os.path.expanduser("~"), "Pictures", "LAN Chat")
shared_files = {}     # id файла -> путь (мои отправленные картинки)
pending_attach = {}   # токен -> картинка, выбранная, но ещё не отправленная
image_cache = OrderedDict()   # id сообщения -> (mime, байты), только в памяти
IMAGE_CACHE_LIMIT = 150 * 1024 * 1024


def save_settings():
    save_json(SETTINGS_PATH, settings)


def save_history():
    with lock:
        del messages[:-HISTORY_LIMIT]
        save_json(HISTORY_PATH, messages)


def find_msg(msg_id):
    for m in reversed(messages):
        if m.get("id") == msg_id:
            return m
    return None


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


MY_IP = get_local_ip()


def system_is_dark():
    try:
        if IS_MAC:
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=2)
            return "Dark" in r.stdout
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        pass
    return False


def resolved_dark():
    t = settings.get("theme", "system")
    return t == "dark" or (t == "system" and system_is_dark())


def is_muted():
    mu = settings.get("mute_until", 0)
    return mu == -1 or time.time() < mu


# ---------- Автозапуск ----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.lanchat.app.plist")


def launch_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
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
                plistlib.dump({"Label": "com.lanchat.app", "ProgramArguments": cmd,
                               "RunAtLoad": True, "LimitLoadToSessionType": "Aqua",
                               "AbandonProcessGroup": True}, f)
        elif os.path.exists(MAC_PLIST):
            os.remove(MAC_PLIST)


# ---------- Окна ----------

class UI:
    main = None
    popup = None
    popup_count = 0
    popup_token = 0
    ready = False
    quitting = False
    just_updated = False


ui = UI()


def push(event):
    """Отправить событие в интерфейс."""
    w = ui.main
    if w is None or not ui.ready:
        return
    try:
        w.evaluate_js("window.app && app.onEvent(%s)" % json.dumps(event))
    except Exception:
        pass


def main_has_focus():
    try:
        return bool(ui.main.evaluate_js("document.hasFocus() && !document.hidden"))
    except Exception:
        return False


def restore_main():
    w = ui.main
    if w is None:
        return
    try:
        w.restore()
        w.show()
    except Exception:
        pass
    push({"t": "focus"})


# ---------- Звуки ----------
# Звуки синтезируются самой программой при первом запуске — внешние файлы не нужны.

SOUNDS = [("drop", "Капля"), ("chime", "Колокольчик"), ("pop", "Поп"),
          ("melody", "Мелодия"), ("system", "Системный")]
SOUND_IDS = {s for s, _ in SOUNDS}
SR = 44100


def _note(buf, start, freq, dur, amp, decay, harmonics=(1.0,), slide_to=None):
    n0, n = int(start * SR), int(dur * SR)
    if len(buf) < n0 + n:
        buf.extend([0.0] * (n0 + n - len(buf)))
    phase = 0.0
    for i in range(n):
        t = i / SR
        f = freq if slide_to is None else freq * (slide_to / freq) ** min(1.0, t / 0.12)
        phase += 2 * math.pi * f / SR
        env = min(1.0, t / 0.004) * math.exp(-decay * t)
        buf[n0 + i] += amp * env * sum(h * math.sin(phase * (k + 1)) for k, h in enumerate(harmonics))


def synth(kind):
    b = []
    if kind == "drop":
        _note(b, 0, 1500, 0.4, 0.6, 9, (1.0, 0.15), slide_to=620)
    elif kind == "chime":
        _note(b, 0, 784, 1.1, 0.4, 4, (1.0, 0.35, 0.12))
        _note(b, 0.13, 1046.5, 1.1, 0.4, 4, (1.0, 0.35, 0.12))
    elif kind == "pop":
        _note(b, 0, 420, 0.16, 0.8, 28, (1.0, 0.2), slide_to=260)
    elif kind == "melody":
        for i, f in enumerate((1046.5, 1318.5, 1568.0)):
            _note(b, i * 0.11, f, 0.6, 0.35, 7, (1.0, 0.25))
    peak = max(abs(x) for x in b) or 1.0
    return [x / peak * 0.55 for x in b]


def sound_path(kind):
    path = os.path.join(SOUND_DIR, f"{kind}-v1.wav")
    if not os.path.exists(path):
        os.makedirs(SOUND_DIR, exist_ok=True)
        frames = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in synth(kind))
        tmp = path + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(frames)
        os.replace(tmp, path)
    return path


def prepare_sounds():
    for kind, _ in SOUNDS:
        if kind != "system":
            try:
                sound_path(kind)
            except OSError:
                pass


def play_sound(kind=None):
    kind = kind or settings.get("sound", "drop")
    try:
        if kind == "system":
            if IS_MAC:
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif IS_WIN:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        path = sound_path(kind)
        if IS_MAC:
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_WIN:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


# ---------- Всплывающее уведомление ----------

def close_popup(target=None):
    with lock:
        p = ui.popup
        if p is None or (target is not None and p is not target):
            return
        ui.popup = None
        ui.popup_count = 0
    try:
        p.destroy()
    except Exception:
        pass


def _popup_expired(p, token):
    if ui.popup is p and ui.popup_token == token:
        close_popup(p)


def arm_popup_timer(p):
    """(Пере)запускает таймер скрытия: каждое новое сообщение продлевает показ."""
    with lock:
        ui.popup_token += 1
        token = ui.popup_token
    secs = settings.get("popup_time", 60)
    if secs and secs > 0:
        t = threading.Timer(secs, _popup_expired, args=(p, token))
        t.daemon = True
        t.start()


class PopupApi:
    def open_main(self):
        close_popup()
        restore_main()

    def close(self):
        threading.Thread(target=close_popup, daemon=True).start()


def show_popup(name, text):
    with lock:
        p = ui.popup
        ui.popup_count += 1
        count = ui.popup_count
    data = {"name": name, "text": text, "count": count, "theme": settings.get("theme", "system")}

    if p is not None:                     # окошко уже на экране — обновляем его
        try:
            p.evaluate_js("upd(%s)" % json.dumps(data))
            arm_popup_timer(p)
            return
        except Exception:
            close_popup(p)
            with lock:
                ui.popup_count = 1
            data["count"] = 1

    w, h = 350, 106
    try:
        scr = webview.screens[0]
        sw, sh = scr.width, scr.height
    except Exception:
        sw, sh = 1440, 900
    x = sw - w - 16
    y = 38 if IS_MAC else sh - h - 56
    html = POPUP_HTML.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    p = webview.create_window(
        "LAN Chat", html=html, width=w, height=h, x=x, y=y,
        frameless=True, easy_drag=False, on_top=True, focus=False, resizable=False,
        js_api=PopupApi(), background_color="#1F1F23" if resolved_dark() else "#FFFFFF")
    with lock:
        ui.popup = p
    arm_popup_timer(p)


def notify(name, text):
    if is_muted() or main_has_focus():
        return
    play_sound()
    show_popup(name, text)


def quit_app():
    ui.quitting = True
    close_popup()
    try:
        ui.main.destroy()
    except Exception:
        os._exit(0)


def on_closing():
    if ui.quitting:
        return True
    # крестик сворачивает окно: сообщения продолжают приходить
    threading.Thread(target=lambda: ui.main.minimize(), daemon=True).start()
    return False


def on_loaded():
    ui.ready = True
    update_title()
    if SELFTEST_DIR:
        threading.Thread(target=selftest, daemon=True).start()


# ---------- Сеть ----------

def peers_list():
    with lock:
        return [{"ip": ip, "name": n} for ip, n in peers.items()]


def remember_uid(uid, ip):
    if uid and ip:
        peer_uids[str(uid)[:64]] = ip


def add_peer(ip, name):
    with lock:
        if peers.get(ip) == name:
            return
        is_new = ip not in peers
        peers[ip] = name
    push({"t": "peers", "peers": peers_list()})
    if is_new:
        threading.Thread(target=flush_receipts, daemon=True).start()


def udp_announcer():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    subnet_bcast = MY_IP.rsplit(".", 1)[0] + ".255"
    while True:
        msg = json.dumps({"app": APP_ID, "uid": settings["uid"], "name": settings["name"]}).encode()
        for addr in ("255.255.255.255", subnet_bcast):
            try:
                s.sendto(msg, (addr, UDP_PORT))
            except OSError:
                pass
        time.sleep(3)


def udp_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", UDP_PORT))
    except OSError:
        return
    while True:
        try:
            data, (ip, _) = s.recvfrom(2048)
            info = json.loads(data.decode())
            if info.get("app") == APP_ID and info.get("uid") != settings["uid"]:
                remember_uid(info.get("uid"), ip)
                add_peer(ip, str(info.get("name", ip))[:40])
        except (OSError, ValueError, AttributeError):
            continue


def apply_reaction(msg_id, emoji, uid, on):
    with lock:
        m = find_msg(msg_id)
        if m is None:
            return None
        reactions = m.setdefault("reactions", {})
        users = reactions.setdefault(emoji, [])
        if on and uid not in users:
            users.append(uid)
        if not on and uid in users:
            users.remove(uid)
        if not users:
            del reactions[emoji]
        result = dict(reactions)
    save_history()
    return result


def handle_client(conn, ip):
    try:
        _handle_client(conn, ip)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def clean_reply(r):
    if not isinstance(r, dict) or not r.get("id"):
        return None
    return {"id": str(r["id"])[:64], "name": str(r.get("name", ""))[:40], "text": str(r.get("text", ""))[:300]}


def clean_image(i):
    if not isinstance(i, dict) or not i.get("id"):
        return None
    mime = str(i.get("mime", ""))
    return {"id": str(i["id"])[:64], "name": os.path.basename(str(i.get("name", "image")))[:120],
            "size": int(i.get("size") or 0), "mime": mime if mime in IMAGE_TYPES.values() else "image/png"}


def _handle_client(conn, ip):
    chunks = []
    try:
        conn.settimeout(5)
        while True:
            b = conn.recv(65536)
            if not b:
                break
            chunks.append(b)
        data = json.loads(b"".join(chunks).decode())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    kind = data.get("type")

    if kind == "show":                                   # повторный запуск программы
        if ip in ("127.0.0.1", MY_IP):
            restore_main()
        return

    if kind == "get_file":
        serve_file(conn, str(data.get("id", ""))[:64])
        return

    if kind == "msg":
        m = {"id": str(data.get("id", uuid.uuid4().hex))[:64],
             "uid": str(data.get("uid", ""))[:64],
             "name": str(data.get("name", ip))[:40],
             "text": str(data.get("text", ""))[:10000],
             "ts": time.time(), "mine": False, "reactions": {}, "unread": True, "ip": ip}
        reply, image = clean_reply(data.get("reply")), clean_image(data.get("image"))
        if reply:
            m["reply"] = reply
        if image:
            m["image"] = image
        remember_uid(m["uid"], ip)
        with lock:
            if find_msg(m["id"]):
                return
            messages.append(m)
        save_history()
        add_peer(ip, m["name"])
        push({"t": "msg", "msg": m})
        update_title()
        preview = ("📷 Картинка" + (" · " + m["text"] if m["text"] else "")) if image else m["text"]
        notify(m["name"], preview)

    elif kind == "read":
        ids = data.get("ids")
        if isinstance(ids, list):
            set_status({str(i)[:64] for i in ids[:5000]}, "read")

    elif kind == "typing":
        remember_uid(data.get("uid"), ip)
        add_peer(ip, str(data.get("name", ip))[:40])
        push({"t": "typing", "uid": str(data.get("uid", ""))[:64],
              "name": str(data.get("name", ip))[:40], "on": bool(data.get("on"))})

    elif kind == "react":
        emoji = str(data.get("emoji", ""))[:16]
        if not emoji:
            return
        r = apply_reaction(str(data.get("id", "")), emoji, str(data.get("uid", "")), bool(data.get("on")))
        if r is not None:
            push({"t": "react", "id": data.get("id"), "reactions": r})


def open_server_socket():
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


def send_payload(payload, timeout=3):
    """Отправляет всем собеседникам. Возвращает True, если хоть кто-то получил."""
    raw = json.dumps(payload, ensure_ascii=False).encode()
    delivered = False
    for p in peers_list():
        try:
            with socket.create_connection((p["ip"], TCP_PORT), timeout=timeout) as s:
                s.sendall(raw)
            delivered = True
        except OSError:
            pass
    return delivered


def deliver(m):
    payload = {"type": "msg", "id": m["id"], "uid": settings["uid"], "name": settings["name"], "text": m["text"]}
    if m.get("reply"):
        payload["reply"] = m["reply"]
    if m.get("image"):
        payload["image"] = {k: v for k, v in m["image"].items() if k != "path"}   # путь не передаём
    ok = send_payload(payload)
    with lock:
        if ok:
            m.pop("failed", None)
        else:
            m["failed"] = True
    save_history()
    if ok:
        set_status({m["id"]}, "delivered")
    else:
        push({"t": "failed", "id": m["id"]})


# ---------- Передача картинок ----------

def file_path_for(fid):
    p = shared_files.get(fid)
    if p:
        return p
    with lock:
        for m in reversed(messages):
            img = m.get("image") or {}
            if m.get("mine") and img.get("id") == fid and img.get("path"):
                shared_files[fid] = img["path"]
                return img["path"]
    return None


def serve_file(conn, fid):
    """Отдаёт собеседнику мою картинку прямо с диска."""
    path = file_path_for(fid)
    data, hdr = b"", {"ok": False, "reason": "missing"}
    try:
        if path and os.path.isfile(path) and os.path.getsize(path) <= MAX_IMAGE:
            with open(path, "rb") as f:
                data = f.read()
            hdr = {"ok": True, "size": len(data),
                   "mime": IMAGE_TYPES.get(os.path.splitext(path)[1].lower(), "image/png")}
    except OSError:
        data, hdr = b"", {"ok": False, "reason": "missing"}
    head = json.dumps(hdr).encode()
    try:
        conn.settimeout(60)
        conn.sendall(struct.pack(">I", len(head)) + head + data)
    except OSError:
        pass


def fetch_file(ip, fid):
    with socket.create_connection((ip, TCP_PORT), timeout=4) as s:
        s.sendall(json.dumps({"type": "get_file", "id": fid}).encode())
        s.shutdown(socket.SHUT_WR)
        s.settimeout(30)
        buf = bytearray()
        while True:
            b = s.recv(262144)
            if not b:
                break
            buf += b
            if len(buf) > MAX_IMAGE + 65536:
                raise ValueError("слишком большой файл")
    n = struct.unpack(">I", bytes(buf[:4]))[0]
    hdr = json.loads(bytes(buf[4:4 + n]).decode())
    data = bytes(buf[4 + n:])
    if not hdr.get("ok"):
        raise FileNotFoundError(hdr.get("reason", "missing"))
    if len(data) != hdr.get("size"):
        raise ValueError("картинка пришла не полностью")
    return hdr.get("mime", "image/png"), data


def cache_put(key, mime, data):
    image_cache[key] = (mime, data)
    image_cache.move_to_end(key)
    while sum(len(v[1]) for v in image_cache.values()) > IMAGE_CACHE_LIMIT and len(image_cache) > 1:
        image_cache.popitem(last=False)


def image_bytes(msg_id):
    """(mime, байты) картинки из сообщения. Ошибки: FileNotFoundError, ConnectionError."""
    with lock:
        m = find_msg(msg_id)
    img = (m or {}).get("image")
    if not img:
        raise FileNotFoundError("missing")
    if m.get("mine"):
        path = img.get("path")
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("moved")
        with open(path, "rb") as f:
            return img.get("mime", "image/png"), f.read()
    if msg_id in image_cache:
        image_cache.move_to_end(msg_id)
        return image_cache[msg_id]
    ips = [ip for ip in (peer_uids.get(m.get("uid")), m.get("ip")) if ip]
    last_err = ConnectionError("offline")
    for ip in dict.fromkeys(ips):
        try:
            mime, data = fetch_file(ip, img["id"])
            cache_put(msg_id, mime, data)
            return mime, data
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as e:
            last_err = ConnectionError(str(e) or "offline")
    raise last_err


def to_data_url(mime, data):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def add_attachment(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_TYPES:
        return {"error": f"«{os.path.basename(path)}» — это не картинка"}
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"error": f"Не удалось открыть «{os.path.basename(path)}»"}
    if size > MAX_IMAGE:
        return {"error": f"«{os.path.basename(path)}» больше 25 МБ"}
    token = uuid.uuid4().hex
    pending_attach[token] = {"path": path, "name": os.path.basename(path), "size": size, "mime": IMAGE_TYPES[ext]}
    with open(path, "rb") as f:
        thumb = to_data_url(IMAGE_TYPES[ext], f.read())
    return {"token": token, "name": os.path.basename(path), "thumb": thumb}


def make_png(path, w, h, top, bottom):
    """Простая картинка-градиент (для самопроверки, без сторонних библиотек)."""
    rows = bytearray()
    for y in range(h):
        t = y / (h - 1)
        rows.append(0)
        for x in range(w):
            s = x / (w - 1)
            rows += bytes(int((top[i] * (1 - t) + bottom[i] * t) * (0.75 + 0.25 * s)) for i in range(3))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


# ---------- Доставлено / прочитано ----------

STATUS_RANK = {"sending": 0, "delivered": 1, "read": 2}
receipt_lock = threading.Lock()


def set_status(ids, status):
    """Мои сообщения: «отправляется» -> «доставлено» -> «прочитано» (только вперёд)."""
    changed, now = [], time.time()
    with lock:
        for m in messages:
            if m.get("mine") and m["id"] in ids and \
                    STATUS_RANK.get(m.get("status"), 1) < STATUS_RANK[status]:
                m["status"] = status
                m.pop("failed", None)
                if status == "read":
                    m["read_ts"] = now
                changed.append(m["id"])
    if changed:
        save_history()
        push({"t": "status", "ids": changed, "status": status, "ts": now})


def unread_count():
    with lock:
        return sum(1 for m in messages if not m.get("mine") and m.get("unread"))


def update_title():
    n = unread_count()
    try:
        ui.main.set_title(f"({n}) LAN Chat" if n else "LAN Chat")
    except Exception:
        pass


def flush_receipts():
    """Отправляет собеседнику отметки «прочитано». Если он не в сети — дошлём позже."""
    with receipt_lock:
        with lock:
            ids = [m["id"] for m in messages if m.get("receipt_pending")]
        if not ids or not peers_list():
            return
        if send_payload({"type": "read", "uid": settings["uid"], "ids": ids}):
            with lock:
                for m in messages:
                    if m["id"] in ids:
                        m.pop("receipt_pending", None)
            save_history()


def mark_all_read():
    changed = False
    with lock:
        for m in messages:
            if not m.get("mine") and m.get("unread"):
                m["unread"] = False
                m["receipt_pending"] = True
                changed = True
    if changed:
        save_history()
        update_title()
    flush_receipts()


# ---------- Мост «интерфейс → программа» ----------

class Api:
    def get_state(self):
        with lock:
            return {"me": {"uid": settings["uid"], "name": settings["name"]},
                    "messages": list(messages), "peers": peers_list(),
                    "theme": settings.get("theme", "system"),
                    "mute_until": settings.get("mute_until", 0),
                    "sound": settings.get("sound", "drop"),
                    "sounds": [{"id": k, "name": n} for k, n in SOUNDS],
                    "popup_time": settings.get("popup_time", 60),
                    "version": APP_VERSION, "just_updated": ui.just_updated,
                    "update": upd.latest if upd.latest and upd.latest.get("available") else None,
                    "autostart": autostart_enabled(), "ip": MY_IP}

    def set_sound(self, kind):
        if kind in SOUND_IDS:
            settings["sound"] = kind
            save_settings()
            threading.Thread(target=play_sound, args=(kind,), daemon=True).start()   # прослушать
        return settings["sound"]

    def set_popup_time(self, secs):
        try:
            secs = int(secs)
        except (TypeError, ValueError):
            secs = 60
        if secs in (0, 10, 60, 120):
            settings["popup_time"] = secs
            save_settings()
        return settings["popup_time"]

    def test_notification(self):
        threading.Thread(target=play_sound, daemon=True).start()
        show_popup("LAN Chat", "Так будет выглядеть уведомление о новом сообщении 👋")
        return True

    def on_focus(self):
        threading.Thread(target=close_popup, daemon=True).start()

    def mark_read(self):
        threading.Thread(target=mark_all_read, daemon=True).start()

    def typing(self, on):
        threading.Thread(target=send_payload, daemon=True, args=(
            {"type": "typing", "uid": settings["uid"], "name": settings["name"], "on": bool(on)},),
            kwargs={"timeout": 1}).start()

    def send(self, text, reply=None, attachments=None):
        """Возвращает список созданных сообщений: картинки по одной, подпись — у последней."""
        text = str(text or "").strip()[:10000]
        files = [pending_attach.pop(t) for t in (attachments or []) if t in pending_attach]
        if not text and not files:
            return []
        reply = clean_reply(reply)
        created, now = [], time.time()
        for i, f in enumerate(files):
            fid = uuid.uuid4().hex
            shared_files[fid] = f["path"]
            created.append({"text": text if i == len(files) - 1 else "",
                            "image": {"id": fid, "name": f["name"], "size": f["size"],
                                      "mime": f["mime"], "path": f["path"]}})
        if not files:
            created.append({"text": text})
        out = []
        for i, part in enumerate(created):
            m = {"id": uuid.uuid4().hex, "uid": settings["uid"], "name": settings["name"],
                 "ts": now + i * 0.001, "mine": True, "reactions": {}, "status": "sending", **part}
            if reply and i == 0:
                m["reply"] = reply
            out.append(m)
        with lock:
            messages.extend(out)
        save_history()
        for m in out:
            threading.Thread(target=deliver, args=(m,), daemon=True).start()
        return out

    def pick_images(self):
        types = ("Картинки (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp)",)
        try:
            paths = ui.main.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=types)
        except Exception:
            paths = ui.main.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
        return [add_attachment(p) for p in (paths or [])][:10]

    def add_image_data(self, name, data_url):
        """Вставленная или перетащенная картинка: сохраняем на ЭТОМ компьютере и отправляем ссылку."""
        try:
            head, b64 = str(data_url).split(",", 1)
            mime = head[5:].split(";")[0]
            ext = next((e for e, mt in IMAGE_TYPES.items() if mt == mime and e != ".jpeg"), None)
            if not ext:
                return {"error": "Этот формат картинки не поддерживается"}
            data = base64.b64decode(b64)
            if len(data) > MAX_IMAGE:
                return {"error": "Картинка больше 25 МБ"}
            os.makedirs(PASTE_DIR, exist_ok=True)
            base = os.path.splitext(os.path.basename(str(name or "")))[0] or "Картинка"
            base = "".join(c for c in base if c not in '\\/:*?"<>|')[:60] or "Картинка"
            path = os.path.join(PASTE_DIR, f"{base} {time.strftime('%Y-%m-%d %H.%M.%S')}{ext}")
            n = 1
            while os.path.exists(path):
                n += 1
                path = os.path.join(PASTE_DIR, f"{base} {time.strftime('%Y-%m-%d %H.%M.%S')} ({n}){ext}")
            with open(path, "wb") as f:
                f.write(data)
            return add_attachment(path)
        except (OSError, ValueError) as e:
            return {"error": f"Не удалось добавить картинку: {e}"}

    def remove_attachment(self, token):
        pending_attach.pop(token, None)
        return True

    def get_image(self, msg_id):
        try:
            mime, data = image_bytes(str(msg_id))
            return {"ok": True, "data": to_data_url(mime, data)}
        except FileNotFoundError as e:
            return {"ok": False, "reason": str(e) if str(e) in ("moved", "missing") else "missing"}
        except Exception:
            return {"ok": False, "reason": "offline"}

    def save_image(self, msg_id):
        try:
            mime, data = image_bytes(str(msg_id))
        except Exception:
            return {"ok": False}
        with lock:
            name = ((find_msg(msg_id) or {}).get("image") or {}).get("name") or "image.png"
        res = ui.main.create_file_dialog(webview.FileDialog.SAVE, save_filename=name)
        path = res[0] if isinstance(res, (list, tuple)) and res else res
        if not path:
            return {"ok": False, "cancelled": True}
        with open(path, "wb") as f:
            f.write(data)
        return {"ok": True}

    def resend(self, msg_id):
        with lock:
            m = find_msg(msg_id)
        if m and m.get("mine"):
            threading.Thread(target=deliver, args=(m,), daemon=True).start()
            return True
        return False

    def react(self, msg_id, emoji):
        emoji = str(emoji)[:16]
        with lock:
            m = find_msg(msg_id)
            if m is None:
                return None
            on = settings["uid"] not in m.get("reactions", {}).get(emoji, [])
        r = apply_reaction(msg_id, emoji, settings["uid"], on)
        threading.Thread(target=send_payload, daemon=True, args=(
            {"type": "react", "id": msg_id, "emoji": emoji, "uid": settings["uid"], "on": on},)).start()
        return r

    def set_theme(self, theme):
        if theme in ("light", "dark", "system"):
            settings["theme"] = theme
            save_settings()
        return settings["theme"]

    def set_mute(self, kind):
        now = time.time()
        if kind == "1h":
            until = now + 3600
        elif kind == "8h":
            until = now + 8 * 3600
        elif kind == "tomorrow":
            t = time.localtime(now + 86400)
            until = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 8, 0, 0, 0, 0, -1))
        elif kind == "forever":
            until = -1
        else:
            until = 0
        settings["mute_until"] = until
        save_settings()
        return until

    def set_autostart(self, on):
        try:
            set_autostart(bool(on))
        except OSError:
            pass
        return autostart_enabled()

    def set_name(self, name):
        name = str(name or "").strip()[:40]
        if name:
            settings["name"] = name
            save_settings()
        return settings["name"]

    def check_update(self):
        try:
            info = check_update()
            return {"available": info["available"], "version": info["version"], "current": APP_VERSION}
        except Exception as e:
            return {"error": str(e)[:200], "current": APP_VERSION}

    def update_now(self):
        threading.Thread(target=run_update, daemon=True).start()

    def open_url(self, url):
        """Ссылка из сообщения открывается в обычном браузере, а не в окне чата."""
        url = str(url or "").strip()
        if re.match(r"^https?://[^\s]+$", url, re.I) and len(url) <= 4000:
            webbrowser.open(url)
            return True
        return False

    def open_releases(self):
        webbrowser.open(RELEASES_PAGE)

    def add_peer(self, ip):
        ip = str(ip or "").strip()
        try:
            socket.inet_aton(ip)
        except OSError:
            return False
        add_peer(ip, ip)
        return True

    def quit(self):
        threading.Thread(target=quit_app, daemon=True).start()


# ---------- Самопроверка (для автоматической сборки) ----------

def screenshot(name):
    path = os.path.join(SELFTEST_DIR, name + ".png")
    try:
        if IS_MAC:
            subprocess.run(["screencapture", "-x", path], timeout=20)
        elif IS_WIN:
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                  "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                  "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                  f"$bmp.Save('{path}')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=30,
                           creationflags=0x08000000)
    except Exception:
        pass


def selftest():
    result = {"checks": {}}
    checks = result["checks"]

    def watchdog():
        time.sleep(300)
        result["error"] = "timeout"
        save_json(os.path.join(SELFTEST_DIR, "selftest.json"), result)
        os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        time.sleep(2.5)
        now = time.time()
        me, peer = settings["uid"], "peer-demo"
        pic_peer = os.path.join(SELFTEST_DIR, "chart.png")
        pic_mine = os.path.join(SELFTEST_DIR, "mine.png")
        make_png(pic_peer, 480, 280, (96, 140, 255), (40, 70, 180))
        make_png(pic_mine, 360, 240, (255, 190, 120), (220, 90, 120))
        fid_peer, fid_mine = uuid.uuid4().hex, uuid.uuid4().hex
        shared_files[fid_peer] = pic_peer             # «компьютер собеседника» — этот же, по 127.0.0.1
        demo = [
            (now - 26 * 3600, False, "Ноутбук", "Привет! Не забудь, завтра созвон в 10:00", {"👍": [me]}, {}),
            (now - 25.9 * 3600, True, settings["name"], "Помню, спасибо!", {}, {}),
            (now - 180, True, settings["name"], "Можешь скинуть отчёт по продажам за октябрь?", {"⏳5мин": [peer]}, {}),
            (now - 120, False, "Ноутбук", "Да, через 10 минут пришлю.\nТам ещё добавила сравнение с сентябрём 📊 Источник: https://example.com/report.",
             {"🔥": [me], "❤️": [peer, me]}, {}),
            (now - 90, False, "Ноутбук", "Вот график 👇", {},
             {"image": {"id": fid_peer, "name": "chart.png", "size": os.path.getsize(pic_peer), "mime": "image/png"},
              "ip": "127.0.0.1"}),
            (now - 40, True, settings["name"], "Супер, спасибо 🙌", {"😂": [peer]}, {"reply": "Вот график"}),
            (now - 30, True, settings["name"], "", {},
             {"image": {"id": fid_mine, "name": "mine.png", "size": os.path.getsize(pic_mine),
                        "mime": "image/png", "path": pic_mine}}),
        ]
        with lock:
            messages.clear()
            for ts, mine, name, text, reacts, extra in demo:
                m = {"id": uuid.uuid4().hex, "uid": me if mine else peer, "name": name,
                     "text": text, "ts": ts, "mine": mine, "reactions": reacts, **extra}
                if extra.get("reply"):
                    src_m = next(x for x in messages if x["text"].startswith(extra["reply"]))
                    m["reply"] = {"id": src_m["id"], "name": src_m["name"], "text": "📷 " + src_m["text"]}
                if mine:
                    m.update(status="read", read_ts=ts + 20)
                messages.append(m)
            messages[-1]["status"] = "delivered"
            messages[-1].pop("read_ts", None)
            peers["192.168.1.24"] = "Ноутбук"
            peer_uids[peer] = "127.0.0.1"
        push({"t": "reload"})
        time.sleep(2)

        ev = ui.main.evaluate_js
        checks["messages_rendered"] = ev("document.querySelectorAll('.msg').length") == len(demo)
        checks["reaction_chips"] = (ev("document.querySelectorAll('.chip').length") or 0) >= 4
        checks["day_separators"] = ev("document.querySelectorAll('.day').length") == 2

        checks["status_ticks"] = ev("document.querySelectorAll('.st.read').length") == 3 and \
            ev("document.querySelectorAll('.st.delivered').length") == 1
        set_status({messages[-1]["id"]}, "read")                   # собеседник прочитал
        time.sleep(0.6)
        checks["read_receipt"] = ev("document.querySelectorAll('.st.read').length") == 4
        time.sleep(1.5)
        checks["images_loaded"] = ev("[...document.querySelectorAll('.pic img')]"
                                     ".filter(i => i.complete && i.naturalWidth > 0).length") == 2
        checks["reply_quote"] = ev("document.querySelectorAll('.msg .quote').length") == 1
        checks["links"] = ev("[...document.querySelectorAll('.text a')].map(a => a.dataset.url).join()") == "https://example.com/report"
        push({"t": "typing", "uid": peer, "name": "Ноутбук", "on": True})
        time.sleep(0.6)
        checks["typing_shown"] = bool(ev("!!document.getElementById('typing')")) and \
            "печатает" in (ev("document.getElementById('status').textContent") or "")

        ev("app.applyTheme('light')")
        time.sleep(1.2)
        screenshot("1-light")
        push({"t": "typing", "uid": peer, "name": "Ноутбук", "on": False})

        target = messages[3]["id"]
        before = "😮" in messages[3]["reactions"]
        ev("react(%s, '😮')" % json.dumps(target))      # так же, как клик в интерфейсе
        time.sleep(1)
        checks["reaction_toggle"] = (not before) and "😮" in messages[3]["reactions"]
        checks["reaction_shown"] = bool(ev("[...document.querySelectorAll('.chip')].some(c => c.textContent.includes('😮'))"))

        ev("document.querySelectorAll('.msg.them .act-react')[1].click()")
        time.sleep(0.8)
        checks["picker_opens"] = bool(ev("!!document.querySelector('.picker')"))
        screenshot("2-light-picker")
        ev("app.closeAll()")

        ev("app.applyTheme('dark')")
        time.sleep(1.2)
        screenshot("3-dark")
        checks["dark_theme"] = ev("document.documentElement.dataset.theme") == "dark"

        ev("document.getElementById('btn-more').click()")
        time.sleep(0.6)
        screenshot("4-dark-menu")
        ev("app.closeAll()")

        settings["popup_time"] = 120
        show_popup("Ноутбук", "Привет! Это уведомление поверх всех окон 👋")
        time.sleep(2.5)
        checks["popup"] = ui.popup is not None
        show_popup("Ноутбук", "И ещё одно — окошко не мигает, а обновляется")
        time.sleep(1)
        top = ui.popup.evaluate_js("document.getElementById('top').textContent") if ui.popup else ""
        checks["popup_counter"] = "2 новых" in (top or "")
        screenshot("5-popup")
        checks["popup_stays"] = ui.popup is not None              # не исчезает через несколько секунд
        close_popup()

        ok_sounds = True
        for kind, _ in SOUNDS:
            if kind == "system":
                continue
            with wave.open(sound_path(kind)) as w:
                ok_sounds = ok_sounds and w.getnframes() > SR * 0.1
        play_sound("chime")
        checks["sounds"] = ok_sounds

        ev("app.applyTheme('light')")
        ev("notifySettings()")
        time.sleep(1)
        checks["notify_settings"] = ev("document.querySelectorAll('.opt').length") == len(SOUNDS)
        screenshot("6-notify-settings")
        ev("document.querySelector('.overlay').remove()")

        # ответ на сообщение + вложение в поле ввода
        ev("startReply(findMsg(%s))" % json.dumps(messages[3]["id"]))
        att = add_attachment(pic_mine)
        ev("addAttach(%s)" % json.dumps(att))
        ev("ta.value='Отлично, сейчас посмотрю';autosize()")
        time.sleep(0.8)
        checks["reply_bar"] = bool(ev("document.getElementById('reply-bar').classList.contains('show')"))
        checks["attach_tray"] = ev("document.querySelectorAll('#tray .thumb').length") == 1
        screenshot("8-reply-attach")
        ev("document.querySelector('.pic img').click()")
        time.sleep(0.8)
        checks["lightbox"] = bool(ev("!!document.querySelector('.lightbox img')"))
        screenshot("9-lightbox")
        ev("closeLightbox();cancelReply();clearAttach()")

        info = check_update()                              # настоящий запрос к GitHub
        checks["update_check"] = bool(info.get("url")) and bool(info.get("version"))
        checks["update_detects_newer"] = check_update(current="0.0.1")["available"]
        upd.latest = None
        dest = os.path.join(SELFTEST_DIR, "dl-" + asset_name())
        download(info["url"], dest, lambda p: None)
        ok_dl = os.path.getsize(dest) > 5_000_000
        if IS_MAC:
            out = os.path.join(SELFTEST_DIR, "dl-unpacked")
            subprocess.run(["ditto", "-x", "-k", dest, out], check=True, timeout=180)
            ok_dl = ok_dl and os.path.isfile(os.path.join(out, "LANChat.app", "Contents", "MacOS", "LANChat"))
            subprocess.run(["rm", "-rf", out])
        os.remove(dest)
        checks["update_download"] = ok_dl
        push({"t": "update", "state": "available", "version": "9.9.9"})
        time.sleep(0.8)
        checks["update_banner"] = bool(ev("document.getElementById('update').classList.contains('show')"))
        screenshot("7-update")
        push({"t": "update", "state": "downloading", "pct": 42, "version": "9.9.9"})
        time.sleep(0.6)
        checks["update_progress"] = "42" in (ev("document.getElementById('update').textContent") or "")
        result["ok"] = all(checks.values())
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)
    save_json(os.path.join(SELFTEST_DIR, "selftest.json"), result)
    code = 0 if result.get("ok") else 1
    quit_app()
    time.sleep(3)
    os._exit(code)


# ---------- Обновление ----------

REPO = "tagea/lanchat"
UPDATE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY = 6 * 3600


class Upd:
    latest = None
    busy = False


upd = Upd()


def ssl_context():
    try:
        import certifi                       # свои сертификаты: на Mac системные Python не видит
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def http_open(url, timeout=20, api=False):
    headers = {"User-Agent": f"LANChat/{APP_VERSION}"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("LANCHAT_GH_TOKEN") if SELFTEST_DIR else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
    except urllib.error.URLError as e:
        # антивирус или корпоративная сеть подменяют сертификат — доверяем системному хранилищу
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:4])


def asset_name():
    return "LANChat-Setup.exe" if IS_WIN else "LANChat-mac.zip"


def check_update(current=None):
    current = current or APP_VERSION
    with http_open(UPDATE_API, api=True) as r:
        data = json.load(r)
    version = str(data.get("tag_name", "")).lstrip("v")
    url = next((a.get("browser_download_url") for a in data.get("assets", [])
                if a.get("name") == asset_name()), None)
    info = {"version": version, "current": current, "url": url,
            "available": bool(url) and vtuple(version) > vtuple(current)}
    upd.latest = info
    return info


def auto_check_loop():
    time.sleep(10)
    while True:
        try:
            info = check_update()
            if info["available"]:
                push({"t": "update", "state": "available", "version": info["version"]})
        except Exception:
            pass                             # нет интернета — проверим позже
        time.sleep(CHECK_EVERY)


def download(url, dest, progress):
    with http_open(url, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else 0
            if pct != last:
                last = pct
                progress(pct)
    if total and done != total:
        raise RuntimeError("файл скачался не полностью")


def mac_app_path():
    app = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
    return app if app.endswith(".app") else None


def install_windows(setup):
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"],
                     creationflags=flags, close_fds=True)


def install_mac(zip_path, workdir):
    app = mac_app_path()
    if not app:
        raise RuntimeError("не найдено приложение LANChat.app")
    parent = os.path.dirname(app)
    if not (os.access(parent, os.W_OK) and os.access(app, os.W_OK)):
        raise RuntimeError(f"нет прав на запись в папку {parent}")
    out = os.path.join(workdir, "new")
    subprocess.run(["ditto", "-x", "-k", zip_path, out], check=True, timeout=180)
    new = os.path.join(out, "LANChat.app")
    if not os.path.isdir(new):
        raise RuntimeError("в архиве нет LANChat.app")
    script = os.path.join(workdir, "update.sh")
    with open(script, "w") as f:
        f.write(f"""#!/bin/sh
pid={os.getpid()}; app={shlex.quote(app)}; new={shlex.quote(new)}
i=0
while kill -0 "$pid" 2>/dev/null && [ $i -lt 150 ]; do sleep 0.2; i=$((i+1)); done
rm -rf "$app.old"
if mv "$app" "$app.old"; then
  if mv "$new" "$app"; then rm -rf "$app.old"; else mv "$app.old" "$app"; fi
fi
xattr -cr "$app" 2>/dev/null
open "$app"
""")
    subprocess.Popen(["/bin/sh", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_update():
    if upd.busy:
        return
    upd.busy = True
    version = ""
    try:
        info = upd.latest if upd.latest and upd.latest.get("available") else check_update()
        version = info["version"]
        if not info["available"]:
            push({"t": "update", "state": "none", "version": APP_VERSION})
            return
        if not getattr(sys, "frozen", False) or not (IS_WIN or IS_MAC):
            webbrowser.open(RELEASES_PAGE)        # запуск из исходника — просто открываем страницу
            push({"t": "update", "state": "available", "version": version})
            return
        workdir = tempfile.mkdtemp(prefix="lanchat-update-")
        dest = os.path.join(workdir, asset_name())
        push({"t": "update", "state": "downloading", "pct": 0, "version": version})
        download(info["url"], dest,
                 lambda p: push({"t": "update", "state": "downloading", "pct": p, "version": version}))
        push({"t": "update", "state": "installing", "version": version})
        if IS_WIN:
            install_windows(dest)
        else:
            install_mac(dest, workdir)
        time.sleep(0.8)
        quit_app()                                # новая версия запустится сама
    except Exception as e:
        log_line("update failed:\n" + traceback.format_exc())
        push({"t": "update", "state": "error", "msg": str(e)[:200], "version": version})
    finally:
        upd.busy = False


# ---------- Запуск и диагностика ----------

WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def webview2_installed():
    """Без WebView2 интерфейс открылся бы в устаревшем движке Internet Explorer и не работал."""
    import winreg
    keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}")]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                v = winreg.QueryValueEx(k, "pv")[0]
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            pass
    return False


def message_box(text, title="LAN Chat", flags=0x10):
    if SELFTEST_DIR:
        print(text, file=sys.stderr)
        return 0
    try:
        if IS_WIN:
            import ctypes
            return ctypes.windll.user32.MessageBoxW(None, text, title, flags)
        if IS_MAC:
            script = f"display alert {json.dumps(title)} message {json.dumps(text)}"
            subprocess.run(["osascript", "-e", script], timeout=600)
    except Exception:
        pass
    return 0


def log_line(text):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def report_crash():
    tb = traceback.format_exc()
    log_line(f"LAN Chat {APP_VERSION} ({platform.platform()}) crashed:\n{tb}")
    message_box("LAN Chat не смог запуститься.\n\n"
                f"Подробности сохранены в файле:\n{LOG_PATH}\n\n{tb[-700:]}")


def setup_logging():
    h = logging.FileHandler(LOG_PATH, encoding="utf-8", delay=True)
    h.setLevel(logging.ERROR)
    h.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s"))
    logging.getLogger("pywebview").addHandler(h)
    threading.excepthook = lambda a: log_line(
        "thread error:\n" + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))


def main():
    setup_logging()
    if IS_WIN and not webview2_installed():
        answer = message_box(
            "Для работы LAN Chat нужен компонент Microsoft WebView2.\n"
            "В Windows 11 он уже есть, а в Windows 10 его иногда нет.\n\n"
            "Нажмите «Да», чтобы скачать его (бесплатно, от Microsoft). "
            "После установки запустите LAN Chat снова.\n\n"
            "«Нет» — попробовать запустить без него.",
            flags=0x04 | 0x30)                      # Да/Нет + значок предупреждения
        if answer == 6:                             # «Да»
            webbrowser.open(WEBVIEW2_URL)
            return

    try:
        srv = open_server_socket()
    except OSError:
        # программа уже запущена — просто показываем её окно
        try:
            with socket.create_connection(("127.0.0.1", TCP_PORT), timeout=2) as s:
                s.sendall(json.dumps({"type": "show"}).encode())
        except OSError:
            pass
        return

    if autostart_enabled() and not SELFTEST_DIR:
        try:
            set_autostart(True)          # обновить путь, если программу переместили
        except OSError:
            pass

    threading.Thread(target=tcp_server, args=(srv,), daemon=True).start()
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=udp_announcer, daemon=True).start()
    threading.Thread(target=prepare_sounds, daemon=True).start()
    if not SELFTEST_DIR:
        threading.Thread(target=auto_check_loop, daemon=True).start()
        prev = settings.get("last_version")
        ui.just_updated = bool(prev) and prev != APP_VERSION
        settings["last_version"] = APP_VERSION
        save_settings()

    ui.main = webview.create_window(
        "LAN Chat", html=MAIN_HTML, js_api=Api(), width=540, height=740, min_size=(380, 480),
        minimized="--minimized" in sys.argv,
        background_color="#161618" if resolved_dark() else "#FFFFFF")
    ui.main.events.closing += on_closing
    ui.main.events.loaded += on_loaded
    webview.start()
    if SELFTEST_DIR:
        time.sleep(10)           # код завершения задаёт сама самопроверка
    os._exit(0)


# ---------- Интерфейс ----------

POPUP_HTML = r"""<!doctype html><html data-theme="system"><head><meta charset="utf-8"><style>
:root{--bg:#fff;--text:#18181b;--muted:#71717a;--line:#e4e4e7;--accent:#2563eb;--hover:#f1f1f3}
html[data-theme=dark]{--bg:#1f1f23;--text:#f4f4f5;--muted:#a1a1aa;--line:#33333a;--accent:#5b8cff;--hover:#2c2c32}
@media (prefers-color-scheme:dark){html[data-theme=system]{--bg:#1f1f23;--text:#f4f4f5;--muted:#a1a1aa;--line:#33333a;--accent:#5b8cff;--hover:#2c2c32}}
html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);cursor:pointer;
 font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
 -webkit-user-select:none;user-select:none;-webkit-font-smoothing:antialiased}
.w{box-sizing:border-box;height:100%;border:1px solid var(--line);border-left:3px solid var(--accent);
 padding:9px 10px 11px 14px;display:flex;flex-direction:column;gap:2px}
.top{display:flex;align-items:center;justify-content:space-between;font-size:11.5px;color:var(--muted);margin-bottom:1px}
.cnt{color:var(--accent);font-weight:600}
.x{width:22px;height:22px;border:0;border-radius:6px;background:none;color:var(--muted);font-size:17px;line-height:1;
 cursor:pointer;display:grid;place-items:center;padding:0}
.x:hover{background:var(--hover);color:var(--text)}
.n{font-weight:600;font-size:14px;color:var(--text);padding-right:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t{font-size:13.5px;line-height:1.35;color:var(--text);opacity:.85;display:-webkit-box;padding-right:8px;
 -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap}
</style></head><body><div class="w">
<div class="top"><span id="top"></span><button class="x" id="x" title="Закрыть">&times;</button></div>
<div class="n" id="n"></div><div class="t" id="t"></div></div>
<script>
function plural(n,a,b,c){const m=n%10,h=n%100;return m===1&&h!==11?a:(m>=2&&m<=4&&(h<12||h>14)?b:c)}
function upd(D){
  document.documentElement.dataset.theme=D.theme;
  const top=document.getElementById('top');top.textContent='LAN Chat · ';
  if(D.count>1){const s=document.createElement('span');s.className='cnt';
    s.textContent=D.count+' '+plural(D.count,'новое сообщение','новых сообщения','новых сообщений');top.append(s)}
  else top.append('сейчас');
  document.getElementById('n').textContent=D.name;
  document.getElementById('t').textContent=D.text;
}
upd(__DATA__);
const api=()=>window.pywebview&&pywebview.api;
document.getElementById('x').addEventListener('click',e=>{e.stopPropagation();if(api())api().close()});
document.body.addEventListener('click',()=>{if(api())api().open_main()});
</script></body></html>"""

MAIN_HTML = r"""<!doctype html>
<html lang="ru" data-theme="system">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LAN Chat</title>
<style>
:root{
  --bg:#ffffff;--text:#18181b;--muted:#8b8b93;--line:#ececee;--mine:#f4f4f5;--hover:#f1f1f3;
  --chip:#f4f4f5;--chip-on:#eaf0ff;--chip-on-line:#bccdfa;--accent:#2563eb;--input:#f7f7f8;
  --danger:#d93025;--menu:#ffffff;--online:#22a06b;--overlay:rgba(0,0,0,.25);
  --shadow:0 10px 30px rgba(0,0,0,.12),0 1px 3px rgba(0,0,0,.08);
}
html[data-theme=dark]{
  --bg:#161618;--text:#ececef;--muted:#8d8d96;--line:#29292e;--mine:#242428;--hover:#26262b;
  --chip:#232327;--chip-on:#1d2846;--chip-on-line:#34508f;--accent:#5b8cff;--input:#1e1e22;
  --danger:#ff6b5e;--menu:#222226;--online:#3ecf8e;--overlay:rgba(0,0,0,.5);
  --shadow:0 10px 30px rgba(0,0,0,.5),0 1px 3px rgba(0,0,0,.4);
}
@media (prefers-color-scheme:dark){html[data-theme=system]{
  --bg:#161618;--text:#ececef;--muted:#8d8d96;--line:#29292e;--mine:#242428;--hover:#26262b;
  --chip:#232327;--chip-on:#1d2846;--chip-on-line:#34508f;--accent:#5b8cff;--input:#1e1e22;
  --danger:#ff6b5e;--menu:#222226;--online:#3ecf8e;--overlay:rgba(0,0,0,.5);
  --shadow:0 10px 30px rgba(0,0,0,.5),0 1px 3px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);display:flex;flex-direction:column;overflow:hidden;
  font:15px/1.45 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;transition:background .2s,color .2s}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
svg{display:block}

/* шапка */
header{display:flex;align-items:center;gap:12px;padding:10px 14px 10px 16px;border-bottom:1px solid var(--line)}
.avatar{width:34px;height:34px;border-radius:50%;background:var(--mine);display:grid;place-items:center;
  font-weight:600;font-size:14px;flex:none}
.who{flex:1;min-width:0}
.who-name{font-weight:600;font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who-status{font-size:12.5px;color:var(--muted);display:flex;align-items:center;gap:6px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted);opacity:.6}
.dot.on{background:var(--online);opacity:1}
.icon-btn{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;color:var(--muted);
  transition:background .12s,color .12s}
.icon-btn:hover,.icon-btn.active{background:var(--hover);color:var(--text)}
.icon-btn.muted-on{color:var(--danger)}

/* лента */
#feed{flex:1;overflow-y:auto;scroll-behavior:smooth}
#feed::-webkit-scrollbar{width:10px}
#feed::-webkit-scrollbar-thumb{background:var(--line);border-radius:10px;border:3px solid var(--bg)}
.col{max-width:720px;margin:0 auto;padding:12px 22px 10px}
.day{display:flex;align-items:center;gap:14px;margin:18px 0 16px;color:var(--muted);
  font-size:11.5px;letter-spacing:.08em;text-transform:uppercase}
.day::before,.day::after{content:"";flex:1;height:1px;background:var(--line)}
.msg{display:flex;flex-direction:column;margin:4px 0 16px;animation:in .18s ease-out}
@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.msg.them{align-items:flex-start}
.msg.mine{align-items:flex-end}
.bubble{max-width:82%}
.mine .bubble{background:var(--mine);border-radius:14px;padding:10px 14px 11px;max-width:78%}
.meta{font-size:13.5px;color:var(--muted);margin-bottom:3px;white-space:nowrap}
.meta b{font-weight:500;color:var(--text)}
.text{white-space:pre-wrap;overflow-wrap:anywhere;-webkit-user-select:text;user-select:text}
.text a{color:var(--accent);text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--accent) 40%,transparent);
  text-underline-offset:2px;cursor:pointer;word-break:break-all}
.text a:hover{text-decoration-color:var(--accent)}
.reactions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 9px;border-radius:999px;
  background:var(--chip);border:1px solid transparent;font-size:14px;transition:transform .1s,background .12s}
.chip:hover{transform:translateY(-1px)}
.chip.on{background:var(--chip-on);border-color:var(--chip-on-line)}
.chip .n{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.chip.on .n{color:var(--accent)}
.actions{display:flex;gap:2px;margin-top:6px;margin-left:-6px}
.mine .actions{margin-left:0;margin-right:-6px;opacity:0;transition:opacity .12s}
.msg.mine:hover .actions,.msg.mine .actions:focus-within{opacity:1}
.act{width:28px;height:28px;border-radius:7px;display:grid;place-items:center;color:var(--muted)}
.act:hover{background:var(--hover);color:var(--text)}
.st{display:inline-flex;vertical-align:-3px;margin-left:5px;color:var(--muted)}
.st.read{color:var(--accent)}
.st.sending{opacity:.6}
.typing{display:flex;align-items:center;gap:9px;margin:2px 0 14px;color:var(--muted);font-size:13.5px;
  animation:in .18s ease-out}
.typing b{font-weight:500;color:var(--text)}
.dots{display:inline-flex;gap:4px;background:var(--mine);border-radius:12px;padding:9px 11px}
.dots i{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:blink 1.3s infinite ease-in-out}
.dots i:nth-child(2){animation-delay:.18s}
.dots i:nth-child(3){animation-delay:.36s}
@keyframes blink{0%,60%,100%{opacity:.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}
.who-status.typing-on{color:var(--muted)}
.failed{font-size:12.5px;color:var(--danger);margin-top:5px;cursor:pointer}
.failed:hover{text-decoration:underline}
.empty{text-align:center;color:var(--muted);margin-top:28vh;font-size:14px}
.empty b{display:block;color:var(--text);font-weight:600;font-size:15px;margin-bottom:4px}

/* поле ввода */
footer{padding:8px 16px 14px}
.composer{max-width:720px;margin:0 auto;display:flex;flex-direction:column;background:var(--input);
  border:1px solid var(--line);border-radius:18px;padding:5px;transition:border-color .15s}
.cin{display:flex;align-items:flex-end;gap:2px}
.reply-bar{display:none;align-items:center;gap:8px;margin:2px 2px 6px;padding:7px 5px 7px 12px;
  background:var(--mine);border-radius:12px}
.reply-bar.show{display:flex}
.rq{flex:1;min-width:0;font-size:13px;line-height:1.35;color:var(--muted)}
.rq b{display:block;font-weight:600;color:var(--text);font-size:12.5px}
.rq span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.xbtn{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;color:var(--muted);flex:none}
.xbtn:hover{background:var(--hover);color:var(--text)}
.tray{display:none;gap:8px;padding:4px 4px 8px;flex-wrap:wrap}
.tray.show{display:flex}
.thumb{position:relative;width:68px;height:68px;border-radius:10px;overflow:hidden;background:var(--mine);
  border:1px solid var(--line)}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb button{position:absolute;top:4px;right:4px;width:20px;height:20px;border-radius:50%;
  background:rgba(0,0,0,.6);color:#fff;display:grid;place-items:center}
.quote{padding:7px 11px;margin:3px 0 8px;font-size:13.5px;line-height:1.35;border-radius:10px;
  background:var(--mine);color:var(--muted);cursor:pointer;max-width:420px;transition:filter .12s}
.mine .quote{background:var(--bg)}
.quote:hover{filter:brightness(.97)}
.quote b{display:block;font-weight:600;color:var(--text);font-size:12.5px}
.quote span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap}
.quote:hover span{color:var(--text)}
.msg.flash{animation:flash 1.6s ease-out}
@keyframes flash{0%,30%{background:var(--chip-on)}100%{background:transparent}}
.msg{border-radius:12px}
.pic{display:block;margin:3px 0 4px;border-radius:12px;overflow:hidden;background:var(--mine);cursor:zoom-in;
  max-width:min(360px,100%);width:fit-content}
.mine .pic{background:var(--bg)}
.pic img{display:block;max-width:100%;max-height:340px;object-fit:contain}
.pic.loading{width:240px;height:150px;display:grid;place-items:center;color:var(--muted);cursor:default}
.pic.missing{display:flex;align-items:center;gap:10px;padding:11px 13px;cursor:default;font-size:13px;line-height:1.4;
  color:var(--muted);max-width:330px;border:1px dashed var(--line);background:transparent}
.pic.missing .ic{flex:none}
.pic.missing .btn{padding:4px 10px;font-size:12.5px;flex:none}
.lightbox{position:fixed;inset:0;background:rgba(10,10,12,.88);z-index:35;display:flex;flex-direction:column;animation:fade .15s}
.lb-top{display:flex;align-items:center;gap:8px;padding:10px 12px;color:#fff;font-size:13.5px}
.lb-top .nm{flex:1;opacity:.8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-top .btn{color:#fff;background:rgba(255,255,255,.14)}
.lb-top .btn:hover{background:rgba(255,255,255,.24)}
.lb-img{flex:1;display:grid;place-items:center;min-height:0;padding:0 20px 20px}
.lb-img img{max-width:100%;max-height:calc(100vh - 80px);object-fit:contain;border-radius:6px}
.dropzone{position:fixed;inset:8px;border:2px dashed var(--accent);border-radius:16px;z-index:30;pointer-events:none;
  background:color-mix(in srgb,var(--accent) 8%,var(--bg));display:none;place-items:center;color:var(--accent);font-size:15px}
.dropzone.show{display:grid}
.composer:focus-within{border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
textarea{flex:1;border:0;background:transparent;color:var(--text);resize:none;outline:none;font:inherit;
  padding:7px 4px;max-height:160px;line-height:1.4}
textarea::placeholder{color:var(--muted)}
.send{width:34px;height:34px;border-radius:50%;background:var(--accent);color:#fff;display:grid;place-items:center;
  transition:opacity .15s,transform .1s}
.send:disabled{opacity:.3;cursor:default}
.send:not(:disabled):active{transform:scale(.94)}
.hint{max-width:720px;margin:6px auto 0;text-align:center;font-size:11.5px;color:var(--muted)}

/* всплывающие элементы */
.menu{position:fixed;z-index:20;background:var(--menu);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow);padding:6px;min-width:250px;max-width:300px;animation:pop .12s ease-out;
  max-height:calc(100vh - 70px);overflow-y:auto}
@keyframes pop{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:none}}
.mi{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:8px 10px;border-radius:8px;font-size:14px}
.mi:hover{background:var(--hover)}
.mi .ic{color:var(--muted);flex:none}
.mi .lb{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ml{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mi .ck{color:var(--accent);flex:none}
.mi.danger{color:var(--danger)}
.mi.danger .ic{color:var(--danger)}
.ml{padding:6px 10px 4px;font-size:12px;color:var(--muted)}
.ms{height:1px;background:var(--line);margin:5px 4px}
.picker{position:fixed;z-index:20;display:flex;gap:2px;padding:4px;background:var(--menu);border:1px solid var(--line);
  border-radius:999px;box-shadow:var(--shadow);animation:pop .12s ease-out}
.picker button{width:36px;height:36px;border-radius:50%;font-size:20px;display:grid;place-items:center;transition:transform .1s}
.picker button:hover{background:var(--hover);transform:scale(1.18)}
.picker button.on{background:var(--chip-on)}
.picker button.wide{width:auto;padding:0 11px;border-radius:18px;font-size:13.5px;white-space:nowrap;gap:4px;
  display:flex;align-items:center;margin-left:2px;border:1px solid var(--line)}
.picker button.wide:hover{transform:scale(1.06)}
.grid{position:fixed;z-index:20;display:grid;grid-template-columns:repeat(8,34px);gap:2px;padding:8px;
  background:var(--menu);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);animation:pop .12s ease-out}
.grid button{width:34px;height:34px;border-radius:8px;font-size:19px}
.grid button:hover{background:var(--hover)}
.update{display:none;align-items:center;gap:10px;padding:8px 14px 8px 16px;background:var(--chip-on);
  border-bottom:1px solid var(--line);font-size:13.5px;position:relative;overflow:hidden}
.update.show{display:flex}
.update .ic{color:var(--accent);flex:none}
.update .lb{flex:1;min-width:0}
.update .lb b{font-weight:600}
.update .btn{padding:5px 12px;font-size:13px;white-space:nowrap}
.update .bar{position:absolute;left:0;bottom:0;height:2px;background:var(--accent);transition:width .2s}
.update.err{background:color-mix(in srgb,var(--danger) 12%,var(--bg))}
.update.err .ic{color:var(--danger)}
.spin{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%);background:var(--text);color:var(--bg);
  font-size:13px;padding:8px 14px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:40}
.toast.show{opacity:1}
.overlay{position:fixed;inset:0;background:var(--overlay);display:grid;place-items:center;z-index:30;animation:fade .15s}
@keyframes fade{from{opacity:0}}
.dialog{width:min(360px,90vw);background:var(--menu);border:1px solid var(--line);border-radius:14px;
  box-shadow:var(--shadow);padding:18px}
.dialog h3{margin:0 0 4px;font-size:15px;font-weight:600}
.dialog p{margin:0 0 12px;font-size:13px;color:var(--muted)}
.dialog input{width:100%;font:inherit;color:var(--text);background:var(--input);border:1px solid var(--line);
  border-radius:9px;padding:8px 11px;outline:none}
.dialog input:focus{border-color:var(--accent)}
.row{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
.btn{padding:7px 14px;border-radius:9px;font-size:14px}
.btn.ghost:hover{background:var(--hover)}
.btn.primary{background:var(--accent);color:#fff}
.dialog.wide{width:min(400px,92vw)}
.sec{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:16px 0 6px}
.opts{display:flex;flex-direction:column;gap:1px}
.opt{display:flex;align-items:center;gap:11px;padding:8px 10px;border-radius:9px;text-align:left;width:100%;font-size:14px}
.opt:hover{background:var(--hover)}
.radio{width:16px;height:16px;border-radius:50%;border:1.5px solid var(--muted);flex:none;display:grid;place-items:center}
.opt.on .radio{border-color:var(--accent)}
.opt.on .radio::after{content:"";width:8px;height:8px;border-radius:50%;background:var(--accent)}
.opt .lb{flex:1}
.opt .play{font-size:12px;color:var(--muted);opacity:0;transition:opacity .12s}
.opt:hover .play{opacity:1}
.seg{display:flex;gap:3px;background:var(--input);border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{flex:1;padding:6px 4px;border-radius:7px;font-size:13px;color:var(--muted);white-space:nowrap}
.seg button:hover{color:var(--text)}
.seg button.on{background:var(--bg);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.15)}
.row.split{justify-content:space-between}
</style>
</head>
<body>
<header>
  <div class="avatar" id="avatar">·</div>
  <div class="who">
    <div class="who-name" id="peer-name">LAN Chat</div>
    <div class="who-status"><span class="dot" id="dot"></span><span id="status">ищу собеседника в сети…</span></div>
  </div>
  <button class="icon-btn" id="btn-mute" title="Звук и уведомления"></button>
  <button class="icon-btn" id="btn-theme" title="Тема"></button>
  <button class="icon-btn" id="btn-more" title="Настройки"></button>
</header>

<div class="update" id="update"></div>
<main id="feed"><div class="col" id="col"></div></main>

<footer>
  <div class="composer">
    <div class="reply-bar" id="reply-bar"></div>
    <div class="tray" id="tray"></div>
    <div class="cin">
      <button class="icon-btn" id="btn-attach" title="Добавить картинку"></button>
      <textarea id="ta" rows="1" placeholder="Сообщение…"></textarea>
      <button class="icon-btn" id="btn-emoji" title="Эмодзи"></button>
      <button class="send" id="btn-send" title="Отправить" disabled></button>
    </div>
  </div>
  <div class="hint">Enter — отправить · Shift+Enter — новая строка</div>
</footer>
<div class="toast" id="toast"></div>
<div class="dropzone" id="dropzone">Отпустите, чтобы прикрепить картинку</div>

<script>
const REACTIONS=['👍','❤️','😂','😮','😢','🔥','🎉','👎','⏳5мин'];
const REACTION_LABELS={'⏳5мин':'⏳ 5 мин'};   // реакции-фразы
const rLabel=e=>REACTION_LABELS[e]||e;
const EMOJI=['😀','😂','🥲','😊','😍','😘','😎','🤔','😅','😉','🙃','😴','😭','😱','🤗','🥳',
             '👍','👎','👏','🙏','💪','👋','🤝','🙌','❤️','🔥','🎉','✨','✅','❌','☕','💬'];
const MONTHS=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
const I={
 smilePlus:'<path d="M22 11v1a10 10 0 1 1-9-10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" x2="9.01" y1="9" y2="9"/><line x1="15" x2="15.01" y1="9" y2="9"/><path d="M16 5h6"/><path d="M19 2v6"/>',
 smile:'<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" x2="9.01" y1="9" y2="9"/><line x1="15" x2="15.01" y1="9" y2="9"/>',
 reply:'<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
 image:'<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
 imageOff:'<line x1="2" x2="22" y1="2" y2="22"/><path d="M10.41 10.41a2 2 0 1 1-2.83-2.83"/><line x1="13.5" x2="6" y1="13.5" y2="21"/><line x1="18" x2="21" y1="12" y2="15"/><path d="M3.59 3.59A1.99 1.99 0 0 0 3 5v14a2 2 0 0 0 2 2h14c.55 0 1.052-.22 1.41-.59"/><path d="M21 15V5a2 2 0 0 0-2-2H9"/>',
 x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
 bell:'<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
 bellOff:'<path d="M8.7 3A6 6 0 0 1 18 8a21.3 21.3 0 0 0 .6 5"/><path d="M17 17H3s3-2 3-9a4.67 4.67 0 0 1 .3-1.7"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="m2 2 20 20"/>',
 sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
 moon:'<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
 monitor:'<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
 sliders:'<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
 tick:'<path d="M4 12.5l4.5 4.5L19 6.5"/>',
 ticks:'<path d="M1.5 12.5L6 17 16.5 6.5"/><path d="M11 16l1 1L22.5 6.5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
 refresh:'<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/>',
 alert:'<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
 more:'<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
 send:'<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
 check:'<path d="M20 6 9 17l-5-5"/>',
 user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
 plug:'<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a6 6 0 0 1-12 0V8Z"/>',
 power:'<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
 rocket:'<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>'
};
const svg=(n,s=18,w=1.75)=>`<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round">${I[n]}</svg>`;
const $=s=>document.querySelector(s);
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const api=()=>window.pywebview.api;
const S={me:{uid:'',name:''},messages:[],peers:[],theme:'system',mute:0,autostart:false,ip:'',
  sound:'drop',sounds:[],popupTime:60,version:''};
const feed=$('#feed'),col=$('#col'),ta=$('#ta');

/* ---------- время ---------- */
const now=()=>Date.now()/1000;
const dayKey=ts=>new Date(ts*1000).toDateString();
function dayLabel(ts){
  const d=new Date(ts*1000),t=new Date(),y=new Date(Date.now()-864e5);
  if(d.toDateString()===t.toDateString())return'Сегодня';
  if(d.toDateString()===y.toDateString())return'Вчера';
  return d.getDate()+' '+MONTHS[d.getMonth()]+(d.getFullYear()!==t.getFullYear()?' '+d.getFullYear():'');
}
const hhmm=ts=>new Date(ts*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
function rel(ts){
  const s=now()-ts;
  if(s<45)return'только что';
  if(s<3600)return Math.round(s/60)+' мин назад';
  return hhmm(ts);
}

/* ---------- лента ---------- */
const findMsg=id=>S.messages.find(m=>m.id===id);
function render(){
  col.textContent='';
  if(!S.messages.length){
    const e=el('div','empty');e.append(el('b',null,'Пока нет сообщений'),
      el('span',null,S.peers.length?'Напишите первым 👋':'Запустите LAN Chat на втором компьютере'));
    col.append(e);return;
  }
  let last=null;
  for(const m of S.messages){
    if(dayKey(m.ts)!==last){col.append(el('div','day',dayLabel(m.ts)));last=dayKey(m.ts)}
    col.append(msgEl(m));
  }
  renderTyping(true);
  feed.style.scrollBehavior='auto';feed.scrollTop=feed.scrollHeight;feed.style.scrollBehavior='';
}
const typing={};
function setTyping(uid,name,on){
  if(on)typing[uid]={name,until:Date.now()+6000};else delete typing[uid];
  renderTyping();renderHeader();
}
function activeTyping(){
  const t=Date.now();for(const k in typing)if(typing[k].until<t)delete typing[k];
  return Object.values(typing);
}
function renderTyping(noScroll){
  const list=activeTyping(),old=document.getElementById('typing');
  const nearBottom=feed.scrollHeight-feed.scrollTop-feed.clientHeight<120;
  if(old)old.remove();
  if(!list.length||col.querySelector('.empty'))return;
  const t=el('div','typing');t.id='typing';
  const dots=el('span','dots');dots.innerHTML='<i></i><i></i><i></i>';
  const txt=el('span');txt.append(el('b',null,list[0].name),document.createTextNode(' печатает…'));
  t.append(dots,txt);col.append(t);
  if(!noScroll&&nearBottom)feed.scrollTop=feed.scrollHeight;
}
setInterval(()=>{if(Object.keys(typing).length){const n=Object.keys(typing).length;activeTyping();
  if(Object.keys(typing).length!==n){renderTyping();renderHeader()}}},1000);
function appendMsg(m){
  if(col.querySelector('.empty')){render();return}
  const nearBottom=feed.scrollHeight-feed.scrollTop-feed.clientHeight<120;
  const prev=S.messages[S.messages.length-2];
  if(!prev||dayKey(prev.ts)!==dayKey(m.ts))col.append(el('div','day',dayLabel(m.ts)));
  col.append(msgEl(m));
  renderTyping(true);
  if(m.mine||nearBottom)feed.scrollTop=feed.scrollHeight;
}
function msgEl(m){
  const box=el('div','msg '+(m.mine?'mine':'them'));box.dataset.id=m.id;
  const b=el('div','bubble'),meta=el('div','meta');
  const t=el('span','time',rel(m.ts));t.dataset.ts=m.ts;t.title=dayLabel(m.ts)+', '+hhmm(m.ts);
  meta.append(el('b',null,m.mine?'Я':m.name),document.createTextNode(' · '),t);
  if(m.mine&&m.status)meta.append(statusEl(m));
  b.append(meta);
  if(m.reply)b.append(quoteEl(m.reply));
  if(m.image)b.append(picEl(m));
  if(m.text||!m.image)b.append(textEl(m.text));
  box.append(b);
  const r=el('div','reactions');fillReactions(r,m);box.append(r);
  const a=el('div','actions');
  const rb=el('button','act act-react');rb.title='Реакция';rb.innerHTML=svg('smilePlus',16);
  rb.onclick=e=>{e.stopPropagation();openPicker(rb,m.id)};
  const rp=el('button','act act-reply');rp.title='Ответить';rp.innerHTML=svg('reply',16);rp.onclick=()=>startReply(m);
  a.append(rb,rp);box.append(a);
  if(m.failed)box.append(failedEl(m.id));
  return box;
}
/* ---------- ссылки ---------- */
const URL_RE=/\b(?:https?:\/\/|www\.)[^\s<>"«»]+/gi;
function trimUrl(u){
  // убрать знаки препинания в конце: «смотри https://site.ru/page.» → без точки
  while(/[.,;:!?'’”)\]}]$/.test(u)){
    const last=u.slice(-1);
    if(last===')'&&(u.match(/\(/g)||[]).length>=(u.match(/\)/g)||[]).length)break;
    if(last===']'&&(u.match(/\[/g)||[]).length>=(u.match(/\]/g)||[]).length)break;
    u=u.slice(0,-1);
  }
  return u;
}
function textEl(text){
  const d=el('div','text');let pos=0;text=text||'';
  for(const mt of text.matchAll(URL_RE)){
    const url=trimUrl(mt[0]);if(!url)continue;
    if(mt.index>pos)d.append(text.slice(pos,mt.index));
    const a=el('a',null,url);
    const href=/^https?:\/\//i.test(url)?url:'https://'+url;
    a.href=href;a.title=href;a.dataset.url=href;
    d.append(a);pos=mt.index+url.length;
  }
  if(pos<text.length)d.append(text.slice(pos));
  return d;
}
document.addEventListener('click',e=>{
  const a=e.target.closest('.text a');if(!a)return;
  e.preventDefault();
  if(window.getSelection&&String(window.getSelection()).length)return;   // выделяли текст, а не кликали
  api().open_url(a.dataset.url);
});
document.addEventListener('auxclick',e=>{const a=e.target.closest('.text a');if(a){e.preventDefault();api().open_url(a.dataset.url)}});

/* ---------- цитата и картинка в сообщении ---------- */
function quoteEl(r){
  const q=el('div','quote');q.title='Перейти к сообщению';
  q.append(el('b',null,r.name===S.me.name?'Я':r.name),el('span',null,r.text||'…'));
  q.onclick=()=>jumpTo(r.id);return q;
}
function jumpTo(id){
  const box=col.querySelector(`.msg[data-id="${CSS.escape(id)}"]`);
  if(!box){toast('Исходное сообщение не найдено');return}
  box.scrollIntoView({block:'center',behavior:'smooth'});
  box.classList.remove('flash');void box.offsetWidth;box.classList.add('flash');
}
const imgCache={};
function picEl(m){
  const p=el('div','pic loading');p.dataset.id=m.id;
  p.innerHTML=svg('image',22,1.5);loadPic(p,m);return p;
}
async function loadPic(p,m){
  const r=imgCache[m.id]?{ok:true,data:imgCache[m.id]}:await api().get_image(m.id);
  p.textContent='';
  if(r.ok){
    imgCache[m.id]=r.data;
    const nearBottom=feed.scrollHeight-feed.scrollTop-feed.clientHeight<160;
    const img=new Image();img.alt=m.image.name||'';img.src=r.data;
    img.onload=()=>{if(nearBottom)feed.scrollTop=feed.scrollHeight};
    p.className='pic';p.append(img);p.onclick=()=>openLightbox(m);
    return;
  }
  p.className='pic missing'+(r.reason==='offline'?' offline':'');p.onclick=null;
  const ic=el('span','ic');ic.innerHTML=svg('imageOff',20,1.5);
  const txt=r.reason==='offline'?`Картинка хранится на компьютере «${m.name}». Сейчас он не в сети.`
    :r.reason==='moved'?'Файл перемещён или удалён с этого компьютера.'
    :'Отправитель удалил или переместил этот файл.';
  p.append(ic,el('span',null,txt));
  if(r.reason==='offline'){const b=el('button','btn ghost','Повторить');b.onclick=e=>{e.stopPropagation();
    p.className='pic loading';p.innerHTML=svg('image',22,1.5);loadPic(p,m)};p.append(b)}
}
function retryOfflinePics(){
  document.querySelectorAll('.pic.offline').forEach(p=>{const m=findMsg(p.dataset.id);
    if(m){p.className='pic loading';p.innerHTML=svg('image',22,1.5);loadPic(p,m)}});
}
function openLightbox(m){
  closeLightbox();
  const o=el('div','lightbox'),top=el('div','lb-top'),wrap=el('div','lb-img'),img=new Image();
  img.src=imgCache[m.id];img.alt=m.image.name||'';
  const save=el('button','btn','Сохранить…');
  save.onclick=async e=>{e.stopPropagation();const r=await api().save_image(m.id);if(r&&r.ok)toast('Картинка сохранена')};
  const cl=el('button','btn','Закрыть');cl.onclick=closeLightbox;
  top.append(el('span','nm',(m.mine?'Я':m.name)+' · '+(m.image.name||'')),save,cl);
  wrap.append(img);o.append(top,wrap);
  wrap.onclick=e=>{if(e.target===wrap)closeLightbox()};
  document.body.append(o);
}
function closeLightbox(){document.querySelector('.lightbox')?.remove()}

/* ---------- ответ ---------- */
let replyTo=null;
function startReply(m){
  replyTo={id:m.id,name:m.mine?S.me.name:m.name,
    text:(m.image?'📷 '+(m.text||'Картинка'):m.text).slice(0,300)};
  const bar=$('#reply-bar');bar.textContent='';bar.classList.add('show');
  const q=el('div','rq');q.append(el('b',null,'Ответ: '+(m.mine?'себе':m.name)),el('span',null,replyTo.text));
  const x=el('button','xbtn');x.title='Отменить ответ';x.innerHTML=svg('x',15,2);x.onclick=cancelReply;
  bar.append(q,x);closeAll();ta.focus();
}
function cancelReply(){replyTo=null;const bar=$('#reply-bar');bar.classList.remove('show');bar.textContent=''}

/* ---------- вложения ---------- */
let attach=[];
function addAttach(a){
  if(!a)return;
  if(a.error){toast(a.error,3000);return}
  attach.push(a);renderTray();
}
function renderTray(){
  const t=$('#tray');t.textContent='';t.classList.toggle('show',attach.length>0);
  for(const a of attach){
    const th=el('div','thumb');th.title=a.name;const img=new Image();img.src=a.thumb;
    const x=el('button');x.title='Убрать';x.innerHTML=svg('x',12,2.4);
    x.onclick=()=>{attach=attach.filter(z=>z!==a);api().remove_attachment(a.token);renderTray()};
    th.append(img,x);t.append(th);
  }
  autosize();
}
function clearAttach(){for(const a of attach)api().remove_attachment(a.token);attach=[];renderTray()}
$('#btn-attach').onclick=async()=>{closeAll();const list=await api().pick_images();(list||[]).forEach(addAttach);ta.focus()};
function readFile(f){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(f)})}
async function addFiles(files){
  for(const f of files){
    if(!f.type.startsWith('image/')){toast('Можно прикреплять только картинки',2500);continue}
    if(f.size>25*1024*1024){toast('Картинка больше 25 МБ',2500);continue}
    addAttach(await api().add_image_data(f.name||'Картинка',await readFile(f)));
  }
  ta.focus();
}
ta.addEventListener('paste',e=>{
  const files=[...(e.clipboardData?.items||[])].filter(i=>i.kind==='file'&&i.type.startsWith('image/')).map(i=>i.getAsFile()).filter(Boolean);
  if(files.length){e.preventDefault();addFiles(files)}
});
let dragDepth=0;
document.addEventListener('dragenter',e=>{if([...(e.dataTransfer?.types||[])].includes('Files')){dragDepth++;$('#dropzone').classList.add('show')}});
document.addEventListener('dragleave',()=>{if(--dragDepth<=0){dragDepth=0;$('#dropzone').classList.remove('show')}});
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('#dropzone').classList.remove('show');
  const files=[...(e.dataTransfer?.files||[])];if(files.length)addFiles(files)});

function statusEl(m){
  const s=el('span','st '+m.status);
  if(m.status==='read'){s.innerHTML=svg('ticks',16,2);s.title='Прочитано'+(m.read_ts?' в '+hhmm(m.read_ts):'')}
  else if(m.status==='delivered'){s.innerHTML=svg('tick',15,2);s.title='Доставлено, ещё не прочитано'}
  else{s.innerHTML=svg('clock',13,2);s.title='Отправляется…'}
  return s;
}
function updateStatus(ids,status,ts){
  for(const id of ids){
    const m=findMsg(id);if(!m)continue;
    m.status=status;if(status==='read')m.read_ts=ts;m.failed=false;
    const box=col.querySelector(`.msg[data-id="${CSS.escape(id)}"]`);if(!box)continue;
    box.querySelector('.failed')?.remove();
    const meta=box.querySelector('.meta');meta.querySelector('.st')?.remove();meta.append(statusEl(m));
  }
}
function failedEl(id){
  const f=el('div','failed','Не доставлено · повторить');
  f.onclick=async()=>{f.remove();const mm=findMsg(id);if(mm)mm.failed=false;await api().resend(id)};
  return f;
}
function fillReactions(r,m){
  r.textContent='';
  for(const [e,users] of Object.entries(m.reactions||{})){
    if(!users.length)continue;
    const c=el('button','chip'+(users.includes(S.me.uid)?' on':''));
    c.append(el('span',null,rLabel(e)),el('span','n',String(users.length)));
    c.title=users.includes(S.me.uid)?'Нажмите, чтобы убрать':'Нажмите, чтобы добавить';
    c.onclick=()=>react(m.id,e);r.append(c);
  }
  r.style.display=r.children.length?'flex':'none';
}
function updateReactions(id){
  const m=findMsg(id),box=col.querySelector(`.msg[data-id="${CSS.escape(id)}"] .reactions`);
  if(m&&box)fillReactions(box,m);
}
async function react(id,e){
  closeAll();
  const r=await api().react(id,e);
  const m=findMsg(id);if(r&&m){m.reactions=r;updateReactions(id)}
}

/* ---------- шапка и иконки ---------- */
function isMuted(){return S.mute===-1||now()<S.mute}
function renderHeader(){
  const p=S.peers[0],ty=activeTyping();
  $('#peer-name').textContent=p?p.name:'LAN Chat';
  $('#avatar').textContent=p?(p.name.trim()[0]||'·').toUpperCase():'·';
  $('#dot').className='dot'+(p||ty.length?' on':'');
  const st=$('#status');st.parentElement.classList.toggle('typing-on',ty.length>0);
  st.textContent=ty.length?'печатает…':p?(S.peers.length>1?'в сети, ещё '+(S.peers.length-1):'в сети'):'ищу собеседника в сети…';
}
function renderIcons(){
  const m=isMuted(),bm=$('#btn-mute');
  bm.innerHTML=svg(m?'bellOff':'bell');bm.classList.toggle('muted-on',m);
  bm.title=m?muteLabel():'Звук и уведомления включены';
  $('#btn-theme').innerHTML=svg({light:'sun',dark:'moon',system:'monitor'}[S.theme]);
  $('#btn-more').innerHTML=svg('more');
  $('#btn-emoji').innerHTML=svg('smile');
  $('#btn-attach').innerHTML=svg('image');
  $('#btn-send').innerHTML=svg('send',18,2.2);
}
function muteLabel(){
  if(S.mute===-1)return'Без звука, пока не включите';
  const d=new Date(S.mute*1000);
  return'Без звука до '+(d.toDateString()===new Date().toDateString()?'':dayLabel(S.mute).toLowerCase()+', ')+hhmm(S.mute);
}
function applyTheme(t,persist=true){
  S.theme=t;document.documentElement.dataset.theme=t;renderIcons();
  if(persist)api().set_theme(t);
}

/* ---------- меню ---------- */
function closeAll(){document.querySelectorAll('.menu,.picker,.grid').forEach(e=>e.remove());
  document.querySelectorAll('.icon-btn.active').forEach(b=>b.classList.remove('active'))}
function place(node,anchor,above){
  document.body.append(node);
  const r=anchor.getBoundingClientRect(),w=node.offsetWidth,h=node.offsetHeight;
  let left=Math.min(Math.max(8,r.right-w),innerWidth-w-8);
  let top=above?r.top-h-6:r.bottom+6;
  if(!above&&top+h>innerHeight-8)top=r.top-h-6;
  if(top<8)top=r.bottom+6;
  node.style.left=left+'px';node.style.top=top+'px';
}
function openMenu(btn,items){
  const wasOpen=btn.classList.contains('active');closeAll();if(wasOpen)return;
  btn.classList.add('active');
  const menu=el('div','menu');
  for(const it of items){
    if(it.sep){menu.append(el('div','ms'));continue}
    if(it.label){menu.append(el('div','ml',it.label));continue}
    const b=el('button','mi'+(it.danger?' danger':''));
    b.innerHTML=(it.icon?`<span class="ic">${svg(it.icon,16)}</span>`:'<span style="width:16px"></span>');
    b.append(el('span','lb',it.text));
    if(it.checked){const c=el('span','ck');c.innerHTML=svg('check',16,2.2);b.append(c)}
    b.onclick=e=>{e.stopPropagation();closeAll();it.onClick()};
    menu.append(b);
  }
  place(menu,btn,false);
}
$('#btn-mute').onclick=e=>{e.stopPropagation();const m=isMuted();openMenu(e.currentTarget,[
  ...(m?[{label:muteLabel()}]:[]),
  {text:'Звук и уведомления включены',icon:'bell',checked:!m,onClick:()=>setMute('off')},
  {sep:1},
  {text:'Без звука на 1 час',icon:'bellOff',onClick:()=>setMute('1h')},
  {text:'Без звука на 8 часов',icon:'bellOff',onClick:()=>setMute('8h')},
  {text:'До завтра, 8:00',icon:'moon',onClick:()=>setMute('tomorrow')},
  {text:'Пока не включу',icon:'bellOff',checked:S.mute===-1,onClick:()=>setMute('forever')},
  {sep:1},
  {text:'Звук и время показа…',icon:'sliders',onClick:notifySettings},
])};
async function setMute(kind){
  S.mute=await api().set_mute(kind);renderIcons();
  toast(kind==='off'?'Звук и уведомления включены':muteLabel());
}
$('#btn-theme').onclick=e=>{e.stopPropagation();openMenu(e.currentTarget,[
  {text:'Светлая',icon:'sun',checked:S.theme==='light',onClick:()=>applyTheme('light')},
  {text:'Тёмная',icon:'moon',checked:S.theme==='dark',onClick:()=>applyTheme('dark')},
  {text:'Как в системе',icon:'monitor',checked:S.theme==='system',onClick:()=>applyTheme('system')},
])};
$('#btn-more').onclick=e=>{e.stopPropagation();openMenu(e.currentTarget,[
  {text:'Моё имя: '+S.me.name,icon:'user',onClick:changeName},
  {text:'Добавить собеседника по IP',icon:'plug',onClick:addPeer},
  {text:'Уведомления и звук…',icon:'sliders',onClick:notifySettings},
  {text:'Запускать при входе в систему',icon:'rocket',checked:S.autostart,onClick:toggleAutostart},
  {text:'Проверить обновления',icon:'refresh',onClick:checkUpdates},
  {sep:1},{label:'LAN Chat '+S.version+' · IP '+S.ip},{sep:1},
  {text:'Выйти из LAN Chat',icon:'power',danger:1,onClick:()=>api().quit()},
])};
async function changeName(){
  const v=await dialog('Ваше имя','Так вас увидит собеседник.',S.me.name);
  if(v){S.me.name=await api().set_name(v);toast('Имя сохранено')}
}
async function addPeer(){
  const v=await dialog('Добавить собеседника','IP-адрес второго компьютера — он указан в его меню «⋯».','','192.168.1.20');
  if(v){toast(await api().add_peer(v)?'Собеседник добавлен':'Неверный IP-адрес')}
}
async function toggleAutostart(){
  S.autostart=await api().set_autostart(!S.autostart);
  toast(S.autostart?'Автозапуск включён':'Автозапуск выключен');
}

/* ---------- настройки уведомлений ---------- */
const TIMES=[[10,'10 сек'],[60,'1 мин'],[120,'2 мин'],[0,'Пока не закрою']];
function notifySettings(){
  closeAll();
  const o=el('div','overlay'),d=el('div','dialog wide');
  d.append(el('h3',null,'Уведомления'),
    el('p',null,'Как сообщать о новом сообщении, когда вы работаете в другой программе.'));
  d.append(el('div','sec','Звук сообщения'));
  const list=el('div','opts');
  const drawSounds=()=>{list.textContent='';
    for(const s of S.sounds){
      const b=el('button','opt'+(S.sound===s.id?' on':''));
      b.append(el('span','radio'),el('span','lb',s.name),el('span','play','▶ прослушать'));
      b.onclick=async()=>{S.sound=await api().set_sound(s.id);drawSounds()};
      list.append(b);
    }};
  drawSounds();d.append(list);
  d.append(el('div','sec','Показывать уведомление на экране'));
  const seg=el('div','seg');
  const drawSeg=()=>{seg.textContent='';
    for(const [v,l] of TIMES){
      const b=el('button',S.popupTime===v?'on':'',l);
      b.onclick=async()=>{S.popupTime=await api().set_popup_time(v);drawSeg()};
      seg.append(b);
    }};
  drawSeg();d.append(seg);
  const row=el('div','row split'),test=el('button','btn ghost','Проверить уведомление'),ok=el('button','btn primary','Готово');
  test.onclick=()=>api().test_notification();
  const done=()=>{o.remove();document.removeEventListener('keydown',esc);ta.focus()};
  const esc=e=>{if(e.key==='Escape')done()};
  ok.onclick=done;o.onclick=e=>{if(e.target===o)done()};document.addEventListener('keydown',esc);
  row.append(test,ok);d.append(row);o.append(d);document.body.append(o);
}
function markRead(){
  if(!(window.pywebview&&pywebview.api)||!document.hasFocus()||document.hidden)return;
  if(S.messages.some(m=>!m.mine&&m.unread)){S.messages.forEach(m=>{if(!m.mine)m.unread=false});api().mark_read()}
}
window.addEventListener('focus',()=>{if(window.pywebview&&pywebview.api){api().on_focus();markRead()}});
document.addEventListener('visibilitychange',markRead);

/* ---------- реакции и эмодзи ---------- */
function openPicker(btn,id){
  closeAll();
  const m=findMsg(id),p=el('div','picker');
  for(const e of REACTIONS){
    const b=el('button',((m.reactions?.[e]||[]).includes(S.me.uid)?'on ':'')+(REACTION_LABELS[e]?'wide':''),rLabel(e));
    if(REACTION_LABELS[e])b.title='Буду через 5 минут';
    b.onclick=ev=>{ev.stopPropagation();react(id,e)};p.append(b);
  }
  // сбоку от кнопки, чтобы не закрывать текст сообщения
  document.body.append(p);
  const r=btn.getBoundingClientRect(),w=p.offsetWidth,h=p.offsetHeight;
  let left=m.mine?r.left-w-6:r.right+6;
  left=Math.min(Math.max(8,left),innerWidth-w-8);
  let top=r.top+r.height/2-h/2;
  if(top+h>innerHeight-8)top=r.top-h-6;
  p.style.left=left+'px';p.style.top=Math.max(8,top)+'px';
}
$('#btn-emoji').onclick=e=>{
  e.stopPropagation();const btn=e.currentTarget;const wasOpen=!!$('.grid');closeAll();if(wasOpen)return;
  const g=el('div','grid');
  for(const em of EMOJI){const b=el('button',null,em);b.onclick=ev=>{ev.stopPropagation();insert(em)};g.append(b)}
  place(g,btn,true);
};
function insert(t){
  const s=ta.selectionStart,e=ta.selectionEnd;
  ta.value=ta.value.slice(0,s)+t+ta.value.slice(e);ta.selectionStart=ta.selectionEnd=s+t.length;
  ta.focus();autosize();
}
document.addEventListener('click',e=>{if(!e.target.closest('.menu,.picker,.grid'))closeAll()});
document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;
  if(document.querySelector('.lightbox')){closeLightbox();return}
  if(document.querySelector('.menu,.picker,.grid')){closeAll();return}
  if(replyTo)cancelReply();});

/* ---------- отправка ---------- */
function autosize(){ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,160)+'px';
  $('#btn-send').disabled=!ta.value.trim()&&!attach.length}
ta.addEventListener('input',()=>{autosize();typingSignal()});
let typingSent=0;
function typingSignal(){
  if(ta.value.trim()){
    if(Date.now()-typingSent>2500){typingSent=Date.now();api().typing(true)}
  }else if(typingSent){typingSent=0;api().typing(false)}
}
ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send()}});
$('#btn-send').onclick=send;
async function send(){
  const v=ta.value.trim();if(!v&&!attach.length)return;
  const tokens=attach.map(a=>a.token),reply=replyTo;
  ta.value='';attach=[];renderTray();cancelReply();autosize();closeAll();typingSent=0;
  const list=await api().send(v,reply,tokens);
  for(const m of list||[]){if(!findMsg(m.id)){if(failedIds.has(m.id))m.failed=true;S.messages.push(m);appendMsg(m)}}
}

/* ---------- прочее ---------- */
let toastTimer;
function toast(t,ms){const n=$('#toast');n.textContent=t;n.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>n.classList.remove('show'),ms||1800)}

/* ---------- обновление ---------- */
function renderUpdate(u){
  const box=$('#update');box.textContent='';box.className='update';
  if(!u||u.state==='none')return;
  box.classList.add('show');
  const ic=el('span','ic'),lb=el('span','lb');
  if(u.state==='available'){
    ic.innerHTML=svg('download',17);
    lb.append('Доступна новая версия ',el('b',null,u.version));
    const go=el('button','btn primary','Обновить');go.onclick=()=>{renderUpdate({state:'downloading',pct:0,version:u.version});api().update_now()};
    const later=el('button','btn ghost','Позже');later.onclick=()=>renderUpdate(null);
    box.append(ic,lb,later,go);
  }else if(u.state==='downloading'){
    ic.innerHTML=svg('refresh',17);ic.firstChild.classList.add('spin');
    lb.textContent='Скачиваю версию '+u.version+'… '+(u.pct||0)+'%';
    const bar=el('div','bar');bar.style.width=(u.pct||0)+'%';
    box.append(ic,lb,bar);
  }else if(u.state==='installing'){
    ic.innerHTML=svg('refresh',17);ic.firstChild.classList.add('spin');
    lb.textContent='Устанавливаю… LAN Chat перезапустится через несколько секунд';
    box.append(ic,lb);
  }else if(u.state==='error'){
    box.classList.add('err');ic.innerHTML=svg('alert',17);
    lb.textContent='Не удалось обновить: '+(u.msg||'ошибка');lb.title=u.msg||'';
    const man=el('button','btn ghost','Скачать вручную');man.onclick=()=>api().open_releases();
    const again=el('button','btn primary','Повторить');again.onclick=()=>{renderUpdate({state:'downloading',pct:0,version:u.version});api().update_now()};
    box.append(ic,lb,man,again);
  }
}
async function checkUpdates(){
  toast('Проверяю обновления…');
  const r=await api().check_update();
  if(r.error){toast('Не удалось проверить: нет связи с интернетом',3000);return}
  if(r.available)renderUpdate({state:'available',version:r.version});
  else toast('У вас последняя версия ('+r.current+')',2500);
}
function dialog(title,text,value,placeholder){
  return new Promise(res=>{
    closeAll();
    const o=el('div','overlay'),d=el('div','dialog'),inp=el('input');
    inp.value=value||'';inp.placeholder=placeholder||'';
    const ok=el('button','btn primary','Сохранить'),no=el('button','btn ghost','Отмена'),row=el('div','row');
    row.append(no,ok);d.append(el('h3',null,title),el('p',null,text),inp,row);o.append(d);document.body.append(o);
    const done=v=>{o.remove();res(v);ta.focus()};
    ok.onclick=()=>done(inp.value.trim()||null);no.onclick=()=>done(null);
    o.onclick=e=>{if(e.target===o)done(null)};
    inp.onkeydown=e=>{if(e.key==='Enter')done(inp.value.trim()||null);if(e.key==='Escape')done(null)};
    setTimeout(()=>{inp.focus();inp.select()},30);
  });
}
setInterval(()=>{
  document.querySelectorAll('.time').forEach(t=>t.textContent=rel(+t.dataset.ts));
  if(S.mute>0&&!isMuted()){S.mute=0}
  renderIcons();
},30000);

/* ---------- связь с программой ---------- */
const failedIds=new Set();
const app={
  onEvent(e){
    switch(e.t){
      case'msg':if(!findMsg(e.msg.id)){delete typing[e.msg.uid];renderHeader();
        S.messages.push(e.msg);appendMsg(e.msg);markRead()}break;
      case'status':updateStatus(e.ids,e.status,e.ts);break;
      case'typing':setTyping(e.uid,e.name,e.on);break;
      case'react':{const m=findMsg(e.id);if(m){m.reactions=e.reactions;updateReactions(e.id)}break}
      case'peers':S.peers=e.peers;renderHeader();if(col.querySelector('.empty'))render();if(S.peers.length)retryOfflinePics();break;
      case'failed':{
        const m=findMsg(e.id);if(m)m.failed=true;else failedIds.add(e.id);
        const box=col.querySelector(`.msg[data-id="${CSS.escape(e.id)}"]`);
        if(box&&!box.querySelector('.failed'))box.append(failedEl(e.id));
        toast(S.peers.length?'Сообщение не доставлено':'Собеседник пока не найден');break}
      case'reload':load();break;
      case'focus':ta.focus();break;
      case'update':if(e.state==='none')toast('У вас последняя версия',2500);else renderUpdate(e);break;
    }
  },
  applyTheme,closeAll,
};
window.app=app;
let loading=false;
async function load(){
  if(loading)return;loading=true;
  try{
    const st=await api().get_state();
    S.me=st.me;S.messages=st.messages;S.peers=st.peers;S.mute=st.mute_until;S.autostart=st.autostart;S.ip=st.ip;
    S.sound=st.sound;S.sounds=st.sounds;S.popupTime=st.popup_time;S.version=st.version;
    for(const id of failedIds){const m=findMsg(id);if(m)m.failed=true}
    applyTheme(st.theme,false);renderHeader();render();ta.focus();markRead();
    if(st.update)renderUpdate({state:'available',version:st.update.version});
    if(st.just_updated)toast('LAN Chat обновлён до версии '+st.version,4000);
  }finally{loading=false}
}
renderIcons();
if(window.pywebview&&window.pywebview.api)load();
else window.addEventListener('pywebviewready',load);
</script>
</body>
</html>"""


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        report_crash()
        os._exit(1)
