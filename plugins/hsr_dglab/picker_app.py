"""
picker_app.py - 框选/取色工具的独立进程宿主

由 region_picker 以子进程方式启动：tkinter 全程在本进程【主线程】运行
（标准用法），窗口创建/销毁完全安全，任何异常也不会波及惩罚姬主进程。

用法:
  python picker_app.py region   <结果JSON路径> [超时秒]
  python picker_app.py color    <结果JSON路径> [超时秒]
  python picker_app.py selftest <结果JSON路径>

结果 JSON: {"ok": bool, "value": ..., "message": str}
"""
import ctypes
import ctypes.wintypes
import json
import sys
import time

RESULT = {"ok": False, "value": None, "message": ""}
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B


def _key_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _armed(arm_time, delay=0.45):
    """按键生效前先等一小段（滤掉上一轮操作残留的按键状态）"""
    return time.time() - arm_time >= delay


def _write_result(path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(RESULT, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------- 框选 ----------------

def run_region(out_path, timeout):
    import tkinter as tk
    ctypes.windll.user32.SetProcessDPIAware()
    sw = int(ctypes.windll.user32.GetSystemMetrics(0))
    sh = int(ctypes.windll.user32.GetSystemMetrics(1))

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry("{}x{}+0+0".format(sw, sh))
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.35)
    root.configure(bg="black", cursor="crosshair")
    cv = tk.Canvas(root, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    rect = cv.create_rectangle(0, 0, 0, 0, outline="#ff4d4f", width=2)
    label = cv.create_text(10, 30, text="拖拽框选扣血数字区域（回车确认 · ESC 取消）",
                           fill="#ffffff", anchor="nw", font=("Consolas", 12))
    st = {"x0": None, "y0": None, "region": None,
          "done": False, "arm": time.time()}

    def on_press(e):
        st["x0"], st["y0"], st["region"] = e.x, e.y, None
        cv.coords(rect, e.x, e.y, e.x, e.y)

    def on_move(e):
        if st["x0"] is None:
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
        cv.itemconfigure(label, text="已框选 {}x{} @ {},{} —— 按回车确认（重新拖拽可调整）".format(w, h, x, y))

    def finish(cancel, region=None):
        if st["done"]:
            return
        st["done"] = True
        if cancel:
            RESULT.update(ok=False, value=None, message="已取消框选")
        elif region:
            RESULT.update(ok=True, value=region, message="框选完成")
        else:
            RESULT.update(ok=False, value=None, message="未框选任何区域")
        try:
            root.destroy()
        except Exception:
            pass

    def poll_keys():
        if st["done"]:
            return
        if _armed(st["arm"]):
            if _key_down(VK_RETURN):
                finish(st["region"] is None, st["region"])
                return
            if _key_down(VK_ESCAPE):
                finish(True)
                return
        if time.time() - st["arm"] > timeout:
            RESULT.update(ok=False, value=None, message="框选超时")
            try:
                root.destroy()
            except Exception:
                pass
            return
        root.after(30, poll_keys)

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_move)
    cv.bind("<ButtonRelease-1>", on_release)
    root.bind("<Return>", lambda e: finish(st["region"] is None, st["region"]))
    root.bind("<Escape>", lambda e: finish(True))
    root.focus_force()
    poll_keys()
    root.mainloop()


# ---------------- 取色 ----------------

def run_color(out_path, timeout):
    import tkinter as tk
    ctypes.windll.user32.SetProcessDPIAware()
    u32 = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    pt = ctypes.wintypes.POINT()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#10151c")
    cv = tk.Canvas(root, width=220, height=46, bg="#10151c",
                   highlightthickness=1, highlightbackground="#4f8cff")
    cv.pack()
    swatch = cv.create_rectangle(8, 7, 44, 43, fill="#000000", outline="#8a97a8")
    text = cv.create_text(52, 18, text="", fill="#dce3ec", anchor="w", font=("Consolas", 11))
    tip = cv.create_text(52, 34, text="回车确认取色 · ESC 取消", fill="#8a97a8",
                         anchor="w", font=("Microsoft YaHei", 9))
    st = {"hex": None, "rgb": None, "done": False, "arm": time.time()}

    # 1x1 BitBlt 采样（吸取挨打就电 lib.py 方案）：与截图管线（BitBlt/GetDIBits）
    # 颜色口径完全一致，规避桌面 DC 直接 GetPixel 的取值偏差；DC 只建一次
    screen_dc = u32.GetDC(0)
    mem_dc = gdi.CreateCompatibleDC(screen_dc)
    bmp = gdi.CreateCompatibleBitmap(screen_dc, 1, 1)
    gdi.SelectObject(mem_dc, bmp)

    def _bitblt_pixel(x, y):
        gdi.BitBlt(mem_dc, 0, 0, 1, 1, screen_dc, x, y, 0x00CC0020)
        return gdi.GetPixel(mem_dc, 0, 0)

    def _release_gdi():
        try:
            gdi.DeleteObject(bmp)
            gdi.DeleteDC(mem_dc)
            u32.ReleaseDC(0, screen_dc)
        except Exception:
            pass

    def sample():
        if st["done"]:
            return
        u32.GetCursorPos(ctypes.byref(pt))
        c = _bitblt_pixel(int(pt.x), int(pt.y))
        r, g, b = int(c & 0xFF), int((c >> 8) & 0xFF), int((c >> 16) & 0xFF)
        hexs = "#{:02X}{:02X}{:02X}".format(r, g, b)
        st["hex"], st["rgb"] = hexs, (r, g, b)
        root.geometry("+{}+{}".format(int(pt.x) + 18, int(pt.y) + 18))
        cv.itemconfigure(swatch, fill=hexs)
        cv.itemconfigure(text, text="{} R{} G{} B{}".format(hexs, r, g, b))
        root.after(40, sample)

    def finish(cancel):
        if st["done"]:
            return
        st["done"] = True
        if cancel:
            RESULT.update(ok=False, value=None, message="已取消取色")
        elif st["hex"]:
            RESULT.update(ok=True,
                          value={"hex": st["hex"], "r": st["rgb"][0],
                                 "g": st["rgb"][1], "b": st["rgb"][2],
                                 "x": int(pt.x), "y": int(pt.y)},
                          message="取色完成")
        else:
            RESULT.update(ok=False, value=None, message="未取到颜色")
        _release_gdi()
        try:
            root.destroy()
        except Exception:
            pass

    def poll_keys():
        if st["done"]:
            return
        if _armed(st["arm"]):
            if _key_down(VK_RETURN):
                finish(False)
                return
            if _key_down(VK_ESCAPE):
                finish(True)
                return
        if time.time() - st["arm"] > timeout:
            RESULT.update(ok=False, value=None, message="取色超时")
            try:
                root.destroy()
            except Exception:
                pass
            return
        root.after(30, poll_keys)

    root.bind("<Return>", lambda e: finish(False))
    root.bind("<Escape>", lambda e: finish(True))
    root.focus_force()
    sample()
    poll_keys()
    root.mainloop()


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        return 2
    mode, out_path = argv[0], argv[1]
    timeout = float(argv[2]) if len(argv) > 2 else 180.0
    try:
        if mode == "selftest":
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            RESULT.update(ok=True, value={"selftest": True}, message="selftest ok")
            root.after(150, root.destroy)
            root.mainloop()
        elif mode == "region":
            run_region(out_path, timeout)
        elif mode == "color":
            run_color(out_path, timeout)
        else:
            RESULT.update(ok=False, value=None, message="未知模式: " + mode)
    except Exception as e:
        RESULT.update(ok=False, value=None, message="工具异常: {}".format(e))
    _write_result(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
