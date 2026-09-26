"""
region_picker.py - 框选/取色调度器

方案（完整吸取 挨打就电 的成熟设计）：
1. 首选独立 python 子进程运行 picker_app.py —— tkinter 在子进程主线程
   （标准用法），崩溃只影响当次操作，绝不波及 server；子进程使用清洗过
   的环境变量（剥掉 PyInstaller 泄漏的 TCL_LIBRARY 等，否则外部 python
   的 tkinter 会加载到 server 自带 Tcl 而版本冲突）。
2. 无外部 python 时的进程内兜底：吸取挨打就电 overlay.py 的铁律
   （"stop 时隐藏不销毁，避免 tkinter 后台线程重复创建问题"）——
   全生命周期只用【一个常驻后台线程 + 一个永不销毁的 Tk root】，
   每次框选/取色只在其上创建 Toplevel，用完 destroy Toplevel、root 隐藏。

返回约定：pick_region -> ({x,y,w,h}|None, message)
          pick_color  -> ({"hex","r","g","b"}|None, message)
"""
import ctypes
import ctypes.wintypes
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

import screen_capture as capture

PICK_TIMEOUT = 180.0

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(_HERE, "picker_app.py")
_CREATE_NO_WINDOW = 0x08000000

# 子进程结果文件里的 value 哨兵：表示子进程根本没跑起来，需要兜底
_FALLBACK = "__fallback__"

# server 是 PyInstaller 打包：进程环境带着 TCL_LIBRARY/TK_LIBRARY（指向
# 自带解包目录 _MEIxxxx/_tcl_data 的 Tcl 8.6.x）。把环境原样传给外部
# python 会让其 tkinter 加载到 server 的 tcl 初始化脚本，报
# "version conflict for package Tcl" 而起不来 —— 子进程必须用清洗过的环境。
_ENV_STRIP = ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH", "TK_PATH",
              "PYTHONHOME", "PYTHONPATH", "_MEIPASS2", "PYTHONEXECUTABLE",
              "PYTHONSTARTUP", "PYTHONOPTIMIZE")

VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
_KEY_ARM_DELAY = 0.45  # 按键生效前先等一小段（滤掉上一轮操作残留的按键状态）


def _key_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _armed(arm_time):
    return time.time() - arm_time >= _KEY_ARM_DELAY


def _clean_env():
    env = dict(os.environ)
    for key in _ENV_STRIP:
        env.pop(key, None)
    return env


def _find_python():
    for name in ("pythonw.exe", "pythonw", "python.exe", "python"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ============================================================
# 首选：独立子进程
# ============================================================

def _external(mode, timeout):
    """独立进程运行工具。

    返回 (value, message)：
      - value 为 _FALLBACK 哨兵 => 子进程无法启动，调用方应走进程内兜底
      - 其他 => 结果照常（value 可为 None，message 说明原因）
    """
    py = _find_python()
    if not py:
        return _FALLBACK, "未找到 python 解释器"
    out = os.path.join(tempfile.gettempdir(), "hsr_dglab_ipc",
                       "pick_{}_{}.json".format(os.getpid(), int(time.time() * 1000)))
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    flags = _CREATE_NO_WINDOW if py.lower().endswith("python.exe") else 0
    try:
        proc = subprocess.Popen([py, _APP, mode, out, str(timeout)],
                                cwd=_HERE, env=_clean_env(), creationflags=flags)
    except Exception as e:
        return _FALLBACK, "工具子进程启动失败: {}".format(e)

    deadline = time.time() + timeout + 15
    while time.time() < deadline:
        if os.path.exists(out):
            break
        if proc.poll() is not None and not os.path.exists(out):
            return _FALLBACK, "工具子进程提前退出"
        time.sleep(0.08)
    else:
        try:
            proc.kill()
        except Exception:
            pass
        return None, "框选/取色超时"

    try:
        with open(out, encoding="utf-8-sig") as f:
            res = json.load(f)
        try:
            os.remove(out)
        except Exception:
            pass
    except Exception as e:
        return None, "读取工具结果失败: {}".format(e)
    return res.get("value"), res.get("message") or ""


def pick_region(timeout=PICK_TIMEOUT):
    """阻塞框选，返回 ({x,y,w,h} | None, message)"""
    value, message = _external("region", timeout)
    if value is _FALLBACK:
        return _inproc_region(timeout)
    return value, message


def pick_color(timeout=PICK_TIMEOUT):
    """阻塞取色，返回 ({"hex","r","g","b"} | None, message)"""
    value, message = _external("color", timeout)
    if value is _FALLBACK:
        return _inproc_color(timeout)
    return value, message


# ============================================================
# 兜底：进程内常驻 tk 线程（挨打就电 overlay 模型）
# 一个常驻后台线程 + 一个永不销毁的 Tk root（withdraw 常驻），
# 每次操作只创建 Toplevel，用完即毁；杜绝线程反复建 root 的原生崩溃。
# ============================================================

_worker_thread = None
_req_queue = None
_worker_ready = None


def _ensure_worker():
    global _worker_thread, _req_queue, _worker_ready
    if _worker_thread is not None and _worker_thread.is_alive() and _worker_ready:
        return
    _req_queue = queue.Queue()
    _worker_ready = threading.Event()
    _worker_thread = threading.Thread(target=_tk_worker, daemon=True,
                                      name="hsr-picker-tk")
    _worker_thread.start()
    _worker_ready.wait(5)


def _tk_worker():
    import tkinter as tk
    capture.ensure_dpi_aware()
    root = tk.Tk()
    root.withdraw()  # 常驻但隐藏 —— 永不销毁
    _worker_ready.set()
    while True:
        try:
            req = _req_queue.get()
        except Exception:
            return
        if req is None:
            return
        res = {}
        try:
            if req["mode"] == "region":
                _overlay_region_once(root, res, req["timeout"])
            else:
                _overlay_color_once(root, res, req["timeout"])
        except Exception as e:
            res["error"] = str(e)
        req["result"] = res
        req["done"].set()


def _overlay_region_once(root, res, timeout):
    import tkinter as tk
    sw = int(ctypes.windll.user32.GetSystemMetrics(0))
    sh = int(ctypes.windll.user32.GetSystemMetrics(1))
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.geometry("{}x{}+0+0".format(sw, sh))
    top.attributes("-topmost", True)
    top.attributes("-alpha", 0.35)
    top.configure(bg="black", cursor="crosshair")
    cv = tk.Canvas(top, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    rect = cv.create_rectangle(0, 0, 0, 0, outline="#ff4d4f", width=2)
    label = cv.create_text(10, 30, text="拖拽框选扣血数字区域（回车确认 · ESC 取消）",
                           fill="#ffffff", anchor="nw", font=("Consolas", 12))
    st = {"x0": None, "y0": None, "region": None, "arm": time.time()}

    def finish():
        try:
            top.destroy()
        except Exception:
            pass

    def on_press(e):
        st["x0"], st["y0"] = e.x, e.y
        cv.coords(rect, e.x, e.y, e.x, e.y)

    def on_move(e):
        if st["x0"] is None or not top.winfo_exists():
            return
        x, y = min(st["x0"], e.x), min(st["y0"], e.y)
        w, h = abs(e.x - st["x0"]), abs(e.y - st["y0"])
        cv.coords(rect, st["x0"], st["y0"], e.x, e.y)
        cv.coords(label, x + 8, max(30, y - 24))
        cv.itemconfigure(label, text="物理像素 {},{}  {}x{}（松开后回车确认）".format(x, y, w, h))

    def on_release(e):
        if st["x0"] is None:
            return
        x, y = min(st["x0"], e.x), min(st["y0"], e.y)
        w, h = abs(e.x - st["x0"]), abs(e.y - st["y0"])
        if w < 5 or h < 5:
            st["x0"] = st["y0"] = st["region"] = None
            cv.itemconfigure(label, text="拖拽框选扣血数字区域（回车确认 · ESC 取消）")
            return
        st["region"] = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
        cv.itemconfigure(label, text="已框选 {}x{} @ {},{} —— 按回车确认（重新拖拽可调整）".format(
            w, h, x, y))

    def poll_keys():
        if not top.winfo_exists():
            return
        if _armed(st["arm"]):
            if _key_down(VK_RETURN):
                if st["region"]:
                    res["region"] = st["region"]
                finish()
                return
            if _key_down(VK_ESCAPE):
                res["cancelled"] = True
                finish()
                return
        if time.time() - st["arm"] > timeout:
            res["timeout"] = True
            finish()
            return
        root.after(30, poll_keys)

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_move)
    cv.bind("<ButtonRelease-1>", on_release)
    top.bind("<Return>", lambda e: (st["region"] and res.update(region=st["region"]),
                                    finish()))
    top.bind("<Escape>", lambda e: (res.update(cancelled=True), finish()))
    top.focus_force()
    poll_keys()
    root.wait_window(top)


def _overlay_color_once(root, res, timeout):
    import tkinter as tk
    u32 = ctypes.windll.user32
    pt = ctypes.wintypes.POINT()

    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    top.configure(bg="#10151c")
    cv = tk.Canvas(top, width=220, height=46, bg="#10151c",
                   highlightthickness=1, highlightbackground="#4f8cff")
    cv.pack()
    swatch = cv.create_rectangle(8, 7, 44, 43, fill="#000000", outline="#8a97a8")
    text = cv.create_text(52, 18, text="", fill="#dce3ec", anchor="w",
                          font=("Consolas", 11))
    cv.create_text(52, 34, text="回车确认 · ESC 取消", fill="#8a97a8",
                   anchor="w", font=("Microsoft YaHei", 9))
    st = {"hex": None, "rgb": None, "arm": time.time()}

    def finish():
        try:
            top.destroy()
        except Exception:
            pass

    def sample():
        """BitBlt 1x1 采样 —— 与截图管线颜色口径一致（吸取挨打就电 lib.py）"""
        if not top.winfo_exists():
            return
        try:
            u32.GetCursorPos(ctypes.byref(pt))
            top.geometry("+{}+{}".format(int(pt.x) + 18, int(pt.y) + 18))
            bgra = capture.capture_screen_region(int(pt.x), int(pt.y), 1, 1)[0]
            if bgra and len(bgra) >= 3:
                b, g, r = bgra[0], bgra[1], bgra[2]
                hexs = "#{:02X}{:02X}{:02X}".format(r, g, b)
                st["hex"], st["rgb"] = hexs, (r, g, b)
                cv.itemconfigure(swatch, fill=hexs)
                cv.itemconfigure(text, text="{} R{} G{} B{}".format(hexs, r, g, b))
        except Exception:
            pass
        root.after(60, sample)

    def poll_keys():
        if not top.winfo_exists():
            return
        if _armed(st["arm"]):
            if _key_down(VK_RETURN):
                if st["hex"]:
                    res["color"] = {"hex": st["hex"], "r": st["rgb"][0],
                                    "g": st["rgb"][1], "b": st["rgb"][2],
                                    "x": int(pt.x), "y": int(pt.y)}
                finish()
                return
            if _key_down(VK_ESCAPE):
                res["cancelled"] = True
                finish()
                return
        if time.time() - st["arm"] > timeout:
            res["timeout"] = True
            finish()
            return
        root.after(30, poll_keys)

    top.bind("<Return>", lambda e: (st["hex"] and res.update(
        color={"hex": st["hex"], "r": st["rgb"][0],
               "g": st["rgb"][1], "b": st["rgb"][2]}), finish()))
    top.bind("<Escape>", lambda e: (res.update(cancelled=True), finish()))
    top.focus_force()
    sample()
    poll_keys()
    root.wait_window(top)


def _inproc_region(timeout):
    _ensure_worker()
    if not _worker_ready or not _worker_ready.is_set():
        return None, "框选失败: 工具线程未就绪"
    req = {"mode": "region", "timeout": timeout, "done": threading.Event()}
    _req_queue.put(req)
    if not req["done"].wait(timeout + 5):
        return None, "框选超时"
    res = req.get("result") or {}
    if "error" in res:
        return None, "框选失败: {}".format(res["error"])
    if res.get("region"):
        return res["region"], "框选完成"
    if res.get("timeout"):
        return None, "框选超时"
    return None, "已取消框选"


def _inproc_color(timeout):
    _ensure_worker()
    if not _worker_ready or not _worker_ready.is_set():
        return None, "取色失败: 工具线程未就绪"
    req = {"mode": "color", "timeout": timeout, "done": threading.Event()}
    _req_queue.put(req)
    if not req["done"].wait(timeout + 5):
        return None, "取色超时"
    res = req.get("result") or {}
    if "error" in res:
        return None, "取色失败: {}".format(res["error"])
    if res.get("color"):
        return res["color"], "取色完成"
    if res.get("timeout"):
        return None, "取色超时"
    return None, "已取消取色"
