"""
gamepad.py - 手柄控制（吸取 挨打就电 XInput 思路的最小实现）

LB + RB 同按：切换 暂停/恢复 惩罚输出（0.6s 去抖）。
不依赖任何第三方库；xinput1_4 不可用时回退 xinput9_1_0。
"""
import ctypes
import threading
import time

_BTN_LB = 0x0100
_BTN_RB = 0x0200


class _GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class _STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32),
                ("Gamepad", _GAMEPAD)]


_dll = None


def _load():
    global _dll
    if _dll is not None:
        return _dll if _dll is not False else None
    for name in ("xinput1_4", "xinput9_1_0"):
        try:
            _dll = getattr(ctypes.windll, name)
            return _dll
        except Exception:
            continue
    _dll = False
    return None


def read_buttons(user_index=0):
    dll = _load()
    if not dll:
        return 0
    try:
        st = _STATE()
        if dll.XInputGetState(user_index, ctypes.byref(st)) == 0:
            return int(st.Gamepad.wButtons)
    except Exception:
        pass
    return 0


def start_polling(on_toggle, on_log=None, interval=0.1):
    """LB+RB 同按 -> on_toggle()（去抖 0.6s）。返回 (stop_event, thread)。"""
    stop_ev = threading.Event()

    def run():
        log = on_log or (lambda msg: None)
        if not _load():
            log("手柄控制不可用：未找到 XInput（未连接手柄不影响运行）")
            return
        log("手柄控制已启动：LB+RB 同按 = 暂停/恢复惩罚")
        last_fire = 0.0
        prev_down = False
        while not stop_ev.is_set():
            try:
                b = read_buttons()
                down = bool(b & _BTN_LB) and bool(b & _BTN_RB)
                now = time.time()
                if down and not prev_down and now - last_fire > 0.6:
                    last_fire = now
                    try:
                        on_toggle()
                    except Exception as e:
                        log("手柄切换异常: {}".format(e))
                prev_down = down
            except Exception:
                pass
            stop_ev.wait(interval)

    t = threading.Thread(target=run, daemon=True, name="hsr-gamepad")
    t.start()
    return stop_ev, t
