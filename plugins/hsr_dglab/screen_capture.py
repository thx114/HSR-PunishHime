"""
capture.py - 截图后端（移植自挨打就电 capture.py / lib.py）

- GDI BitBlt + GetDIBits（纯 ctypes，无第三方依赖）
- capture_screen_fast(region, hwnd): 支持相对游戏窗口客户区截图（窗口移动跟随）
- capture_screen_region(l, t, w, h): 绝对屏幕区域截图（限制最大 4000px）
- get_game_window / get_client_offset / find_window_by_keywords: 游戏窗口定位
  （psutil 可选：装了按进程名找，没装按窗口标题模糊匹配）

坐标均为物理像素；调用方需已 SetProcessDPIAware()。
"""
import ctypes
import ctypes.wintypes
import os
import time

SRCCOPY = 0x00CC0020

# 游戏窗口 hwnd 短缓存：psutil 全量进程扫描很贵（几十~上百毫秒），
# OCR 轮询每轮都调用本函数，不应每轮付这个钱。缓存 3s，期间用
# IsWindow 微秒级校验 hwnd 存活；失效或超时才真正重扫。
_game_hwnd_cache = [None, 0.0]
_GAME_HWND_TTL = 3.0

_dpi_done = False


def ensure_dpi_aware():
    """进程按物理像素工作（重复调用无害）"""
    global _dpi_done
    if not _dpi_done:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        _dpi_done = True


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def _capture_with_dc(desktop_dc, src_x, src_y, cap_w, cap_h):
    mem_dc = None
    bitmap = None
    try:
        mem_dc = ctypes.windll.gdi32.CreateCompatibleDC(desktop_dc)
        bitmap = ctypes.windll.gdi32.CreateCompatibleBitmap(desktop_dc, cap_w, cap_h)
        ctypes.windll.gdi32.SelectObject(mem_dc, bitmap)
        ctypes.windll.gdi32.BitBlt(mem_dc, 0, 0, cap_w, cap_h, desktop_dc,
                                   src_x, src_y, SRCCOPY)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = cap_w
        bmi.bmiHeader.biHeight = -cap_h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf_size = cap_w * cap_h * 4
        buf = (ctypes.c_ubyte * buf_size)()
        ctypes.windll.gdi32.GetDIBits(mem_dc, bitmap, 0, cap_h, buf,
                                      ctypes.byref(bmi), 0)
        return bytes(buf)
    finally:
        if bitmap:
            ctypes.windll.gdi32.DeleteObject(bitmap)
        if mem_dc:
            ctypes.windll.gdi32.DeleteDC(mem_dc)


def capture_screen_fast(region=None, hwnd=None):
    """按窗口客户区截图（hwnd 为空时全屏）。

    返回 (bgra, rx, ry, rw, rh, img_width)：region 为窗口客户区相对坐标
    """
    ensure_dpi_aware()
    if not hwnd:
        return _capture_fullscreen()

    client_rect = RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    client_width = client_rect.right - client_rect.left
    client_height = client_rect.bottom - client_rect.top

    client_point = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(client_point))
    client_left, client_top = client_point.x, client_point.y

    if client_width <= 0:
        client_width = max(1, client_width)
    if client_height <= 0:
        client_height = max(1, client_height)

    desktop_dc = None
    try:
        desktop_dc = ctypes.windll.user32.GetDC(0)
        if region:
            try:
                rx, ry, rw, rh = [int(x) for x in region]
            except Exception:
                rx, ry, rw, rh = 0, 0, client_width, client_height
            rx = max(0, rx)
            ry = max(0, ry)
            rw = max(1, min(rw, client_width - rx))
            rh = max(1, min(rh, client_height - ry))
            buf = _capture_with_dc(desktop_dc, client_left + rx, client_top + ry, rw, rh)
            return buf, rx, ry, rw, rh, rw
        buf = _capture_with_dc(desktop_dc, client_left, client_top,
                               client_width, client_height)
        return buf, 0, 0, client_width, client_height, client_width
    finally:
        if desktop_dc:
            ctypes.windll.user32.ReleaseDC(0, desktop_dc)


def _capture_fullscreen():
    width = ctypes.windll.user32.GetSystemMetrics(0)
    height = ctypes.windll.user32.GetSystemMetrics(1)
    desktop_dc = None
    try:
        desktop_dc = ctypes.windll.user32.GetDC(0)
        buf = _capture_with_dc(desktop_dc, 0, 0, width, height)
        return buf, 0, 0, width, height, width
    finally:
        if desktop_dc:
            ctypes.windll.user32.ReleaseDC(0, desktop_dc)


def capture_screen_region(left, top, width, height):
    """绝对屏幕区域截图（物理像素），限制最大 4000px。

    区域整块落在虚拟屏幕之外时显式报错——静默返回纯黑会让
    “坐标错位”伪装成“识别不到”。
    """
    # 调用方可能传 float（配置 _f() 读取），GDI 参数必须整型
    left, top, width, height = int(left), int(top), int(width), int(height)
    ensure_dpi_aware()
    MAX_SIZE = 4000
    width = min(width, MAX_SIZE)
    height = min(height, MAX_SIZE)
    vx = ctypes.windll.user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vy = ctypes.windll.user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    vw = ctypes.windll.user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = ctypes.windll.user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    if (left + width <= vx or top + height <= vy
            or left >= vx + vw or top >= vy + vh):
        raise RuntimeError(
            "截图区域 ({},{},{},{}) 完全在屏幕外（屏幕 {}x{} @ {},{}）"
            .format(left, top, width, height, vw, vh, vx, vy))
    desktop_dc = None
    try:
        desktop_dc = ctypes.windll.user32.GetDC(0)
        buf = _capture_with_dc(desktop_dc, left, top, width, height)
        return buf, left, top, width, height, width
    finally:
        if desktop_dc:
            ctypes.windll.user32.ReleaseDC(0, desktop_dc)


def find_window_by_keywords(keyword):
    """枚举可见顶层窗口，按标题模糊匹配，返回 hwnd 列表（挨打就电同款）"""
    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(handle, l_param):
        if ctypes.windll.user32.IsWindowVisible(handle):
            length = ctypes.windll.user32.GetWindowTextLengthW(handle)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(handle, buf, length + 1)
                title = buf.value
                if title and keyword.lower() in title.lower():
                    hwnds.append(handle)
                    return False
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return hwnds


def list_visible_windows(limit=10):
    """枚举可见顶层窗口标题（诊断用：锚点找不到时给用户候选）"""
    titles = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(handle, l_param):
        if ctypes.windll.user32.IsWindowVisible(handle):
            length = ctypes.windll.user32.GetWindowTextLengthW(handle)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(handle, buf, length + 1)
                if buf.value:
                    titles.append(buf.value)
        return len(titles) < limit * 3

    ctypes.windll.user32.EnumWindows(callback, 0)
    return titles[:limit]


def find_main_window_by_title(keyword):
    """按标题包含匹配找可见顶层窗口，返回第一个 hwnd（找不到 None）"""
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(handle, l_param):
        if ctypes.windll.user32.IsWindowVisible(handle):
            length = ctypes.windll.user32.GetWindowTextLengthW(handle)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(handle, buf, length + 1)
                if keyword in buf.value:
                    result.append(handle)
                    return False
        return not result

    ctypes.windll.user32.EnumWindows(callback, 0)
    return result[0] if result else None


def get_game_window(process_title="崩坏：星穹铁道", process_exeName="StarRail.exe"):
    """定位游戏窗口（psutil 按进程名优先，标题兜底；结果 3s 缓存）"""
    now = time.time()
    cached = _game_hwnd_cache[0]
    if cached and now - _game_hwnd_cache[1] < _GAME_HWND_TTL:
        if ctypes.windll.user32.IsWindow(cached):
            return cached
        _game_hwnd_cache[0] = None
    hwnd = _lookup_game_window(process_title, process_exeName)
    if hwnd:
        _game_hwnd_cache[0] = hwnd
        _game_hwnd_cache[1] = now
    return hwnd


def _lookup_game_window(process_title, process_exeName):
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info["name"] == process_exeName:
                hwnds = find_window_by_keywords(process_exeName)
                if hwnds:
                    return hwnds[0]
    except ImportError:
        pass
    except Exception:
        pass
    hwnds = find_window_by_keywords(process_title)
    if hwnds:
        return hwnds[0]
    return None


def get_client_offset(hwnd):
    """窗口客户区左上角的屏幕坐标"""
    if hwnd:
        point = ctypes.wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(point))
        return point.x, point.y
    return 0, 0


def shots_dir():
    """截图目录：统一放 exe/根目录旁边（<root>\\screenshots），插件目录不再写产物"""
    root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    d = os.path.join(root, "screenshots")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def save_screenshot_sync(bmp_data, width, height, filename):
    """调试用：保存截图为 PNG（需要 PIL，可选）"""
    screenshot_dir = shots_dir()
    buf_size = width * height * 4
    if len(bmp_data) < buf_size:
        raise ValueError("截图数据不足: {} < {}".format(len(bmp_data), buf_size))
    from PIL import Image
    img_path = os.path.join(screenshot_dir, filename.replace(".bmp", ".png"))
    img = Image.frombytes("RGBA", (width, height), bytes(bmp_data[:buf_size]), "raw", "BGRA")
    img.save(img_path)
    return img_path