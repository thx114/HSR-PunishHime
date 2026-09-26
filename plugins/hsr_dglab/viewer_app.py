"""
viewer_app.py - 截图查看器子进程（tkinter 原生窗口）

引擎通过 IPC 目录的 viewer.json 通知显示哪张图：
  {"path": "...", "name": "..."}
常驻轮询：文件变化 -> 替换窗口内容；用户点 X 关闭 -> 进程退出。
窗口尺寸严格等于图片原始分辨率（1:1），带 Windows 标准标题栏。
"""
import ctypes
import json
import os
import sys
import traceback
import tkinter as tk

IPC_DIR = os.path.join(os.environ.get("TEMP", ""), "hsr_dglab_ipc")
VIEWER_JSON = os.path.join(IPC_DIR, "viewer.json")

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv")


def _video_mode(viewer, label, path):
    """视频回放模式：后台解码线程 + UI 30Hz 刷新（OCR 测试靶窗）。

    解码在独立守护线程按墙钟同步跳帧（高帧率录屏软件解码追不上时丢帧
    保持同步，落后超 1s 直接 seek），tkinter UI 线程只贴最新帧，绝不
    因解码卡住而"未响应"。画面 1:1 原始分辨率，窗口标题固定"测试窗口"
    （与 cw_ocr_window_title 一致即可被 get_game_window 找到）。"""
    import threading
    import time

    import cv2
    from PIL import Image, ImageTk
    probe = cv2.VideoCapture(path)
    if not probe.isOpened():
        viewer.title("无法打开视频")
        return
    fps = probe.get(cv2.CAP_PROP_FPS) or 30
    vw = int(probe.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
    vh = int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 360
    probe.release()
    label.configure(width=vw, height=vh)
    viewer.geometry("{}x{}".format(vw, vh))
    viewer.title("测试窗口")
    buf = {"img": None, "stop": False}

    def decode():
        cap = cv2.VideoCapture(path)
        f0 = 0
        t0 = None
        while not buf["stop"]:
            if t0 is None:
                t0 = time.monotonic()
            target = int((time.monotonic() - t0) * fps)
            frame = None
            if target - f0 > fps:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, target))
                f0 = target
            while f0 <= target:
                ok, f = cap.read()
                if not ok:
                    # 部分后端到流尾后 set(POS_FRAMES,0) 无效且 read 恒 False：
                    # 直接重开捕获（release + reopen 是唯一可靠回绕）。
                    # 必须 break 而不是 continue：continue 会让内层继续用旧的
                    # target（约等于整段帧数），重开后一口气解码完整段又停在
                    # 末帧，表现为「只放一次然后冻住」。break 后外层用新 t0
                    # 重算 target，才能真正从头回绕。
                    cap.release()
                    cap = cv2.VideoCapture(path)
                    t0 = time.monotonic()
                    f0 = 0
                    if not cap.isOpened():
                        time.sleep(0.2)
                    break
                frame = f
                f0 += 1
            if frame is not None:
                buf["img"] = Image.fromarray(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                time.sleep(0.004)
        cap.release()

    threading.Thread(target=decode, daemon=True,
                     name="viewer-decode").start()
    holder = {"p": None}

    def tick():
        img = buf["img"]
        if img is not None:
            holder["p"] = ImageTk.PhotoImage(img)
            label.configure(image=holder["p"])
        viewer.after(33, tick)

    tick()


def _report_crash(exc):
    """崩溃落盘，引擎/页面可查；不让窗口无声消失"""
    try:
        os.makedirs(IPC_DIR, exist_ok=True)
        with open(os.path.join(IPC_DIR, "viewer_error.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"error": repr(exc),
                       "traceback": traceback.format_exc()}, f)
    except Exception:
        pass


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()

    viewer = tk.Toplevel(root)
    viewer.resizable(False, False)
    label = tk.Label(viewer, bg="#111111")
    label.pack()
    photo = {"img": None}
    cur = {"path": None}

    def show(path, name):
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            try:
                from PIL import Image, ImageTk
                img = ImageTk.PhotoImage(Image.open(path))
            except Exception as e:
                viewer.title("无法打开: " + e.__class__.__name__)
                return
        photo["img"] = img
        label.configure(image=img, width=img.width(), height=img.height())
        viewer.geometry("")
        viewer.geometry("{}x{}".format(img.width(), img.height()))
        viewer.title("测试窗口")
        cur["path"] = path

    def poll():
        try:
            if os.path.isfile(VIEWER_JSON):
                data = json.loads(open(VIEWER_JSON, encoding="utf-8-sig").read())
                p = data.get("path")
                if p and p != cur["path"] and os.path.isfile(p):
                    show(p, data.get("name"))
        except Exception:
            pass
        root.after(200, poll)

    viewer.protocol("WM_DELETE_WINDOW", root.destroy)

    # 启动参数：视频文件进回放模式，图片直接展示
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        arg = sys.argv[1]
        if arg.lower().endswith(VIDEO_EXT):
            _video_mode(viewer, label, arg)
        else:
            show(arg, "测试窗口")
    poll()
    viewer.deiconify()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _report_crash(e)
        raise
