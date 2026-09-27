"""
start.py - 崩铁 · 挨打就电（veritas 版）插件入口

配置页面为完全自定义 HTML 实时面板（ui_page.py）：
  - 实时显示 veritas 连接状态、角色血量条、挨打统计、当前强度
  - 页面轮询 get_status 动作获取快照
  - 手动控制: 测试电击 / 强度± / 清零
  - 惩罚设置与波形编辑，保存后即时生效

数据源双路线，共用同一套惩罚处理（hsr_dglab.py）：
  A. veritas（内存钩子，精确）—— 默认，推荐
  B. ocr（截图识别，占位待实现）

数据来源: veritas https://github.com/hessiser/veritas
"""
import datetime
import json
import os
import struct
import threading
import time
import zlib

import dockdglab

import hsr_dglab
import ipc
import region_picker
import screen_capture
import total_hp_ocr
import ui_page

plugin_name = "hsr_dglab"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")


class App:
    author = ""
    network_data = {}
    html = ""
    ui = None
    config = None
    waveform = None
    log = None
    http = None
    server = None


app = App()


class DockLogger:
    """适配惩罚姬日志接口"""

    def __init__(self, server):
        self.server = server

    def success(self, msg):
        self.server.log("success", msg)

    def info(self, msg):
        self.server.log("info", msg)

    def warn(self, msg):
        self.server.log("warning", msg)

    def error(self, msg):
        self.server.log("error", msg)

    def debug(self, msg):
        self.server.log("debug", msg)


def load_disk_config():
    """读取插件 config.json（配置页保存的持久层，权威来源）"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("config", {}) or {}
    except Exception:
        return {}


def init():
    global app
    app.server = dockdglab.DockDGLab()
    # 尽早声明 DPI aware：server（PyInstaller）默认 DPI-unaware，坐标被系统
    # 虚拟化成逻辑像素，而框选工具回传物理像素；不统一会导致 OCR 截图区域
    # 错位。必须在任何窗口/GDI 调用之前执行。
    try:
        screen_capture.ensure_dpi_aware()
    except Exception:
        pass
    app.config = app.server.get_config(plugin_name) or {}
    disk = load_disk_config()
    if disk.get("plugins"):
        # 插件目录 config.json 为权威配置（配置页直接读写该文件）
        app.config = {"plugins": dict(disk.get("plugins") or {}),
                      "waveform": dict(disk.get("waveform") or {})}
    app.waveform = app.config.get("waveform", {})
    app.log = DockLogger(app.server)
    app.logger = app.log
    app.html = ui_page.build(plugin_name)
    # 写入文件桥 meta（配置页据此定位 config.json，插件未启动也能读写配置）
    ipc.write_meta(plugin_name, PLUGIN_DIR)
    # 命令桥从 server 启动即挂载：框选/取色/置顶/配置保存不依赖插件启动
    global _ipc_cmd
    _register_handlers()
    if _ipc_cmd is None:
        _ipc_cmd = ipc.CmdWatcher(dispatch=dispatch)
        _ipc_cmd.start()
    app.log.success(f"{plugin_name} 插件初始化完成")


# ---------------- 页面动作：实时状态 ----------------

def get_status():
    """实时状态快照（页面每 0.8s 轮询）"""
    p = _plugin
    if p is None or not getattr(p, "running", False):
        return json.dumps({"running": False})
    try:
        return json.dumps({"running": True, **p.snapshot()})
    except Exception as e:
        return json.dumps({"running": False, "error": str(e)})


def get_waveform():
    """当前波形配置（页面编辑用）"""
    return json.dumps({"value": app.waveform or {}, "message": "ok"})


def reload_config():
    """重读配置并让运行中的插件立即生效"""
    if app.server is None:
        return json.dumps({"message": "后端未初始化"})
    app.config = app.server.get_config(plugin_name) or {}
    app.waveform = app.config.get("waveform", {})
    p = _plugin
    if p:
        p.waveform = app.waveform
    return json.dumps({"message": "配置已重载，切换数据来源需重启插件"})


# ---------------- 页面动作：手动控制 ----------------

def _adjust(delta):
    p = _plugin
    if p is None or not getattr(p, "running", False):
        return json.dumps({"message": "插件未启动"})
    target = p.adjust_strength(delta)
    return json.dumps({"message": f"强度 -> {target}"})


def str_up():
    return _adjust(1)


def str_down():
    return _adjust(-1)


def str_clear():
    p = _plugin
    if p is None or not getattr(p, "running", False):
        return json.dumps({"message": "插件未启动"})
    p.clear_strength()
    return json.dumps({"message": "强度已清零"})


# ---------------- 页面动作：测试 ----------------

def test_conn():
    """测试 veritas 连接"""
    try:
        import requests
        url = str(app.config.get("plugins", {}).get("veritas_url",
                                                   "http://127.0.0.1:1305")).rstrip("/")
        r = requests.get(f"{url}/socket.io/",
                         params={"EIO": "4", "transport": "polling", "t": "1"}, timeout=3)
        if r.status_code == 200 and r.text.startswith("0"):
            return json.dumps({"message": "veritas 在线，可以启动插件"})
        return json.dumps({"message": f"veritas 响应异常: HTTP {r.status_code}"})
    except Exception as e:
        return json.dumps({"message": f"连接失败: {e}"})


def test_shock(params=None):
    """发送一次测试电击：设强度 A/B -> 发单轮波形 -> 播完清波恢复原强度

    参考 mc_dglab：dock 后端波形会循环播放，必须 clear_waveform 才停。
    每一步都写惩罚姬日志，测试是否生效看日志即可。
    """
    try:
        pc = app.config.get("plugins", {})
        _pm = params if isinstance(params, dict) else {}
        _wave_key = str(_pm.get("wave") or "hit_pulse")
        sa = int(float(_pm.get("strength_a", pc.get("strength_a", 20)) or 20))
        sb = int(float(_pm.get("strength_b", pc.get("strength_b", 20)) or 20))

        # 探活：拿不到强度说明郊狼没连上
        try:
            probe = app.server.get_strength()
        except Exception as e:
            app.log.error(f"测试电击失败：郊狼探活异常 {e}")
            return json.dumps({"ok": False, "message": f"郊狼未连接: {e}"})
        if isinstance(probe, dict) and probe.get("error"):
            app.log.error("测试电击失败：郊狼探活被拒绝 "
                          + str(probe.get("error")))
            return json.dumps({"ok": False,
                               "message": "郊狼未连接: " + str(probe.get("error"))})
        old = {}
        try:
            m = probe.get("msg") if isinstance(probe, dict) else None
            if isinstance(m, dict):
                old = {"A": int(m.get("A") or 0), "B": int(m.get("B") or 0)}
        except Exception:
            pass
        # 完整回显 server 的强度状态：用于甄别"目标值"与"设备实际值"
        app.log.info(f"郊狼探活完整响应: {json.dumps(probe, ensure_ascii=False)[:400]}")
        if old:
            app.log.info(f"测试电击：郊狼在线，server 记录目标强度 A{old.get('A')}/B{old.get('B')}，"
                         f"本次从设备实际强度起步")
        else:
            app.log.info("测试电击：郊狼在线，未读到 server 强度记录，从设备实际强度起步")

        def _dock_call(resp, what):
            if isinstance(resp, dict):
                if resp.get("error"):
                    raise RuntimeError(what + ": " + str(resp.get("error")))
                code = resp.get("code")
                if code is not None and int(code) != 200:
                    raise RuntimeError(what + ": "
                                       + f"code={code} msg={resp.get('msg')}")

        # App 中继远程调不动强度（APP 拒绝非零 set），蓝牙直连才行——
        # 因此强度设置放在各分支内部，App 分支只提示手动调节
        p = _plugin
        if p is None or not getattr(p, "running", False):
            return json.dumps({"ok": False, "message": "插件未启动"})

        from hit_logic import convert_pulse_data
        _raw = (app.waveform.get(_wave_key)
                or app.waveform.get("hit_pulse")
                or ["6400000064000000"])
        pulse = convert_pulse_data(_raw)
        app.log.info(f"测试电击：波形 {_wave_key}（{len(pulse)} 段）")
        if isinstance(pulse, list) and pulse:
            wave = pulse              # 单轮：一次按下只播一遍（多遍会叠加成"多轮"体感）
        else:
            wave = pulse
        # 实际播放时长 = 帧数(4串/帧) × wave_d_ms，与 v4_backend 的 d 逻辑一致；
        # 旧算法按 100ms/串 估 1.0s，d=125 时波形 0.375s 就播完了，
        # 收尾定时器却还挂在 1.0s 上——连点时正好砸中下一发
        _frames = max(1, -(-len(wave) // 4)) if isinstance(wave, list) and wave else 10
        try:
            _dms = int(float(pc.get("wave_d_ms", 100)))
        except Exception:
            _dms = 100
        duration = _frames * _dms / 1000.0 + 0.1
        # 蓝牙直连优先：设备通过电脑蓝牙连接时必须走 /coyote 接口
        bt = False
        try:
            act = app.server.get_active()
            bt = isinstance(act, dict) and str(act.get("coyote", "")).lower() == "connect"
        except Exception:
            bt = False
        if bt:
            # 序号守卫：与引擎共用一套输出序号，连点/战斗触发互不残杀
            _gen = p.output_seq_bump()
            p.set_strength("A", sa)
            p.set_strength("B", sb)
            app.log.info(f"测试电击：强度 A -> {sa} / B -> {sb}（蓝牙绝对设置）")
            _dock_call(app.server.send_waveform(waveform=wave, channel="All"),
                       "蓝牙波形")
            app.log.success(f"测试电击[蓝牙]已发送：强度 A{sa}/B{sb}，"
                            f"波形 {len(wave)} 段播放约 {duration:.1f} 秒"
                            f"——此刻设备应有体感；结束后强度自动归零")
            def _bt_stop():
                try:
                    if getattr(p, "_output_seq", 0) != _gen:
                        return  # 已有更新的输出，本收尾作废
                    try:
                        app.server.clear_waveform()
                    except Exception:
                        pass
                    try:
                        app.server.coyote_stop_punish()
                    except Exception:
                        pass
                    # server 蓝牙强度接口只认 "A"/"B"，传 "All" 会被静默忽略
                    for _ch in ("A", "B"):
                        try:
                            app.server.coyote_strength(option="set",
                                                       channel=_ch, strength=0)
                        except Exception:
                            pass
                    app.log.info("测试电击[蓝牙]：波形已停止，强度已归零（A/B 分通道）")
                except Exception as e:
                    app.log.warn(f"蓝牙波形停止失败: {e}")
            t = threading.Timer(duration, _bt_stop)
            t.daemon = True
            t.start()
            return json.dumps({"ok": True, "message": "测试电击[蓝牙]已发送"})

        # App 中继：波形推送可用（抓包实锤 completed）；
        # 强度由用户手机 APP 手动控制，插件只清波形队列、绝不碰强度
        _dock_call(app.server.send_waveform(waveform=wave, channel="All",
                                            total_duration=duration),
                   "发送波形")
        app.log.success(f"测试电击[App]波形已推送：{len(wave)} 段约 {duration:.1f} 秒"
                        f"——请先在手机 APP 手动把强度调到 A{sa}/B{sb}！"
                        f"（APP 不接受远程非零强度，插件只负责波形与急停）")

        _gen = p.output_seq_bump()

        def _app_stop():
            """测试收尾：只清波形队列，保持用户手动强度"""
            try:
                if getattr(p, "_output_seq", 0) != _gen:
                    return  # 已有更新的输出，本收尾作废
                app.server.clear_waveform()
                app.log.info("测试电击：波形队列已清（强度保持你手动设置的值）")
            except Exception as e:
                app.log.warn(f"测试电击收尾失败: {e}")

        t = threading.Timer(duration + 0.5, _app_stop)
        t.daemon = True
        t.start()

        return json.dumps({"ok": True,
                           "message": f"测试电击波形已发送，"
                                      f"请在 APP 手动调强度 A{sa}/B{sb}"})
    except Exception as e:
        app.log.error(f"测试电击失败: {e}")
        return json.dumps({"ok": False, "message": f"发送失败: {e}"})


def ocr_test():
    """单次 OCR 识别测试（挨打就电语义：区域永远相对窗口客户区）"""
    try:
        pc = app.config.get("plugins", {})
        url = str(pc.get("cw_ocr_url", "http://127.0.0.1:1395") or "http://127.0.0.1:1395")
        x = int(float(pc.get("cw_ocr_x", 0) or 0))
        y = int(float(pc.get("cw_ocr_y", 0) or 0))
        w = int(float(pc.get("cw_ocr_w", 0) or 0))
        h = int(float(pc.get("cw_ocr_h", 0) or 0))
        colors = total_hp_ocr.parse_colors(pc.get("cw_ocr_colors", "#F78679") or "#F78679")
        tol = float(pc.get("cw_ocr_tolerance", 30) or 30)
        model = str(pc.get("cw_ocr_model", "") or "").strip() or None
        title = str(pc.get("cw_ocr_window_title", "") or "崩坏：星穹铁道")
        if w <= 0 or h <= 0:
            return json.dumps({"message": "识别区域未设置：先在货币战争页框选区域并保存配置"})
        st, raw = {}, []
        keep_sign = str(pc.get("cw_ocr_mode", "pool") or "pool") != "delta"
        value, is_gain = total_hp_ocr.ocr_region(
            url, x, y, w, h, colors, tol, model,
            window_title=title, stats=st, raw_texts=raw, keep_sign=keep_sign)
        pct = ("%.1f" % (st.get("kept", 0) * 100.0 / max(1, st.get("total", 1))))
        probe_msg = ""
        if pc.get("cw_plus_enabled"):
            try:
                import screen_capture as _sc
                hwnd = _sc.get_game_window(process_title=title)
                if not hwnd:
                    raise RuntimeError("未找到窗口「{}」".format(title))
                ox, oy = _sc.get_client_offset(hwnd)
                bgra, _, _, bw, bh, _ = _sc.capture_screen_region(
                    ox, oy, 4096, 4096)
                ok, bits = total_hp_ocr.check_plus_points(
                    bgra, bw, bh, 0, 0,
                    total_hp_ocr.parse_coordinates(pc.get("cw_plus_positions", "") or ""),
                    total_hp_ocr.parse_colors(pc.get("cw_plus_colors", "") or ""),
                    total_hp_ocr.parse_coordinates(pc.get("cw_plus_negative_positions", "") or "") or None,
                    total_hp_ocr.parse_colors(pc.get("cw_plus_negative_colors", "") or "") or None,
                    float(pc.get("cw_plus_tolerance", 30) or 30))
                probe_msg = " · 加号: {} {}".format("✓通过" if ok else "✗未过", bits)
            except Exception as pe:
                probe_msg = " · 加号检测失败: {}".format(str(pe)[:60])
        if value is None:
            if st.get("kept", 0) == 0:
                return json.dumps({"message": f"滤镜后 0 像素命中：目标色与实际颜色不符，近似度 {tol}"})
            if not raw:
                return json.dumps({"message": f"滤镜保留 {pct}% 但 Umi-OCR 无返回，确认 Umi-OCR 已启动且地址正确{probe_msg}"})
            return json.dumps({"message": f"滤镜保留 {pct}% 但没识别出数字，原文: {raw[:3]}{probe_msg}"})
        tag = "获得量 " if is_gain else "血量 "
        return json.dumps({"message": f"{tag}读数: {value} · 滤镜保留 {pct}%{probe_msg} · 原文: {raw[:3]}"})
    except Exception as e:
        return json.dumps({"message": f"OCR 测试失败: {e}"})


def plus_test(params=None):
    """加号检测测试：立刻截当前画面判定一次，回传每个检测点实际取到的颜色，
    现场判断「现在这个画面能不能测到这些点」。params 中的键优先于已保存配置
    （支持改了还没保存就测）。"""
    try:
        import ctypes
        import screen_capture as _sc
        pc = app.config.get("plugins", {})
        p = params if isinstance(params, dict) else {}

        def pick(key, default=None):
            v = p.get(key)
            if v is None or v == "":
                v = pc.get(key)
            return default if v is None else v

        title = str(pick("cw_ocr_window_title", "崩坏：星穹铁道")
                    or "崩坏：星穹铁道")
        positions = total_hp_ocr.parse_coordinates(
            str(pick("cw_plus_positions", "") or ""))
        colors = total_hp_ocr.parse_colors(str(pick("cw_plus_colors", "") or ""))
        neg_pos = total_hp_ocr.parse_coordinates(
            str(pick("cw_plus_negative_positions", "") or ""))
        neg_colors = total_hp_ocr.parse_colors(
            str(pick("cw_plus_negative_colors", "") or ""))
        tol = int(float(pick("cw_plus_tolerance", 30) or 30))
        if not positions or not colors:
            return json.dumps({"ok": False,
                               "message": "先「添加检测点」取色，再点测试"})
        hwnd = _sc.get_game_window(process_title=title)
        if not hwnd:
            cands = _sc.list_visible_windows(10)
            return json.dumps({"ok": False,
                               "message": "未找到窗口「{}」。可见窗口: {}"
                                          .format(title, " / ".join(cands))})
        if ctypes.windll.user32.IsIconic(hwnd):
            return json.dumps({"ok": False,
                               "message": "窗口「{}」已最小化".format(title)})
        ox, oy = _sc.get_client_offset(hwnd)
        # 只截检测点周边（测试要快，不整屏截图）
        pad = 14
        bx = max(0, min(q[0] for q in positions) - pad)
        by = max(0, min(q[1] for q in positions) - pad)
        bw = (max(q[0] for q in positions) + pad) - bx
        bh = (max(q[1] for q in positions) + pad) - by
        bgra, _, _, bw, bh, _ = _sc.capture_screen_region(ox + bx, oy + by, bw, bh)
        smp = []
        ok, bits = total_hp_ocr.check_plus_points(
            bgra, bw, bh, bx, by, positions, colors,
            neg_pos or None, neg_colors or None, tol, samples=smp)
        pos_bits = bits.split("!")[0]
        neg_bits = bits.split("!")[1] if "!" in bits else ""
        thr = tol * 3
        parts = []
        for i, s in enumerate(smp):
            rgb = (int(s[2]), int(s[3]), int(s[4]))
            d = min(abs(rgb[0] - c[0]) + abs(rgb[1] - c[1]) + abs(rgb[2] - c[2])
                    for c in colors)
            hit = i < len(pos_bits) and pos_bits[i] == "1"
            parts.append("#{:02X}{:02X}{:02X}{}".format(
                rgb[0], rgb[1], rgb[2],
                "✓" if hit else "✗(距离{}>{})".format(d, thr)))
        msg = "加号{}（{}/{}）取样 {}".format(
            "通过" if ok else "未通过", pos_bits.count("1"), len(positions),
            " / ".join(parts))
        if neg_bits and "1" in neg_bits:
            msg += " · 反向点命中，一票否决"
        return json.dumps({"ok": True,
                           "value": {"pass": ok, "bits": bits,
                                     "samples": [list(s) for s in smp]},
                           "message": msg})
    except Exception as e:
        return json.dumps({"ok": False,
                           "message": "加号测试失败: {}".format(str(e)[:120])})


def _shots_dir():
    """截图目录：放到根目录（exe 旁边）的 screenshots/，不再写插件目录；
    冻结成单文件 exe 时 __file__ 在临时解包目录，改放 exe 旁边"""
    try:
        return screen_capture.shots_dir()
    except Exception:
        pass
    import sys as _sys
    base = (os.path.dirname(os.path.abspath(_sys.executable))
            if getattr(_sys, "frozen", False)
            else os.path.dirname(os.path.dirname(PLUGIN_DIR)))
    d = os.path.join(base, "screenshots")
    os.makedirs(d, exist_ok=True)
    return d


def game_shot():
    """截图游戏窗口（挨打就电同款调试工具）：整窗客户区 -> PNG 存 screenshots/"""
    try:
        pc = app.config.get("plugins", {})
        title = str(pc.get("cw_ocr_window_title", "") or "崩坏：星穹铁道")
        hwnd = screen_capture.get_game_window(process_title=title)
        if not hwnd:
            cands = screen_capture.list_visible_windows(12)
            return json.dumps({"ok": False, "message":
                "未找到窗口「{}」。可见窗口: {}".format(title, " / ".join(cands))})
        import ctypes
        if ctypes.windll.user32.IsIconic(hwnd):
            return json.dumps({"ok": False, "message": f"窗口「{title}」已最小化，先还原再截图"})
        bgra, _, _, w, h, _ = screen_capture.capture_screen_fast(hwnd=hwnd)
        if not bgra:
            return json.dumps({"ok": False, "message": "截图数据为空"})
        os.makedirs(_shots_dir(), exist_ok=True)
        name = "shot_{}.png".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        _write_png(os.path.join(_shots_dir(), name), bgra, w, h)
        return json.dumps({"ok": True, "value": {"name": name, "w": w, "h": h},
                           "message": f"已截图 {name}"})
    except Exception as e:
        return json.dumps({"ok": False, "message": f"截图失败: {e}"})


_viewer_proc = None


def open_shot(params):
    """在原生窗口 1:1 打开截图；已有查看器则替换内容"""
    global _viewer_proc
    try:
        name = os.path.basename(str((params or {}).get("name", "")))
        if not name.lower().endswith(".png"):
            return json.dumps({"ok": False, "message": "非法文件名"})
        p = os.path.join(_shots_dir(), name)
        if not os.path.isfile(p):
            return json.dumps({"ok": False, "message": "文件不存在"})
        import ipc
        os.makedirs(ipc.IPC_DIR, exist_ok=True)
        ipc._write("viewer.json", {"path": p, "name": name})
        if _viewer_proc is None or _viewer_proc.poll() is not None:
            # 打包环境里 sys.executable 是惩罚姬主程序 exe，
            # 必须用 PATH 上的独立 python + 剥离 _MEI 环境变量
            # （region_picker 同款，否则 tkinter 会加载到 server 的 Tcl 直接崩）
            from region_picker import _find_python, _clean_env, _CREATE_NO_WINDOW
            py = _find_python()
            if not py:
                return json.dumps({"ok": False,
                                   "message": "未找到 python 解释器，无法打开查看窗口"})
            flags = _CREATE_NO_WINDOW if py.lower().endswith("python.exe") else 0
            import subprocess
            here = os.path.dirname(os.path.abspath(__file__))
            _viewer_proc = subprocess.Popen(
                [py, os.path.join(here, "viewer_app.py"), p],
                cwd=here, env=_clean_env(), creationflags=flags)
        return json.dumps({"ok": True, "message": f"已在窗口打开 {name}"})
    except Exception as e:
        return json.dumps({"ok": False, "message": f"打开失败: {e}"})


def list_shots():
    """列出已保存的截图（新->旧）"""
    try:
        d = _shots_dir()
        items = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                if not name.lower().endswith(".png"):
                    continue
                p = os.path.join(d, name)
                try:
                    st = os.stat(p)
                    items.append({"name": name, "size": st.st_size,
                                  "mtime": int(st.st_mtime)})
                except OSError:
                    pass
        items.sort(key=lambda it: -it["mtime"])
        return json.dumps({"ok": True, "value": items, "message": "ok"})
    except Exception as e:
        return json.dumps({"ok": False, "message": f"列举失败: {e}", "value": []})


def del_shot(params):
    """删除一张截图（只允许文件名，禁路径穿越）"""
    try:
        name = os.path.basename(str((params or {}).get("name", "")))
        if not name.lower().endswith(".png"):
            return json.dumps({"ok": False, "message": "非法文件名"})
        p = os.path.join(_shots_dir(), name)
        if not os.path.isfile(p):
            return json.dumps({"ok": False, "message": "文件不存在"})
        os.remove(p)
        return json.dumps({"ok": True, "message": f"已删除 {name}"})
    except Exception as e:
        return json.dumps({"ok": False, "message": f"删除失败: {e}"})


_pick_lock = threading.Lock()  # 框选/取色互斥：阻塞型操作，防排队堆积连环弹出
_hud = None
_gamepad_stop = None


def _plugins_cfg():
    try:
        return app.config.get("plugins") or {}
    except Exception:
        return {}


def _cfg_flag(key, default=False):
    v = _plugins_cfg().get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "是", "on")
    return bool(v)


def _start_hud():
    """游戏内悬浮窗：常驻线程 + 永不销毁 root（挨打就电 overlay 模型）。
    插件重启时复用已存在的实例（隐藏/显示切换），绝不重复建 root。"""
    global _hud
    if _hud is not None:
        try:
            _hud.resume()
        except Exception:
            pass
        return
    try:
        from hud import HudWindow

        def provider():
            try:
                return json.loads(get_status())
            except Exception:
                return {}

        _hud = HudWindow(provider=provider,
                         get_flag=lambda: _cfg_flag("hud_enabled", True))
        threading.Thread(target=_hud.create, daemon=True,
                         name="hsr-hud").start()
        app.log.success("HUD 悬浮窗线程已启动")
    except Exception as e:
        app.log.warn(f"HUD 启动失败: {e}")


def _start_gamepad():
    """手柄控制：LB+RB 同按暂停/恢复（gamepad_enabled 默认关）。"""
    global _gamepad_stop
    if _gamepad_stop is not None:
        return
    if not _cfg_flag("gamepad_enabled", False):
        return
    try:
        import gamepad

        def on_toggle():
            p = _plugin
            if p:
                p.toggle_paused()

        _gamepad_stop, _ = gamepad.start_polling(
            on_toggle, on_log=lambda msg: app.log.info(msg))
        app.log.success("手柄控制已启动")
    except Exception as e:
        app.log.warn(f"手柄控制启动失败: {e}")


def _write_png(path, bgra, width, height):
    """极简 PNG 编码（zlib，色彩类型6 RGBA）：无第三方依赖"""
    width, height = int(width), int(height)
    stride = width * 4
    raw = bytearray()
    for row_idx in range(height):
        row = bytearray(bgra[row_idx * stride:(row_idx + 1) * stride])
        if len(row) < stride:
            break
        row[0::4], row[2::4] = row[2::4], row[0::4]   # BGRA -> RGBA
        row[3::4] = b"\xff" * width                   # alpha 置不透明
        raw.append(0)
        raw += row

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 5))
                + chunk(b"IEND", b""))


def debug_shot():
    """诊断截图：当前 OCR 区域 + 游戏窗口客户区存 screenshots/，供远程排查"""
    try:
        import screen_capture
        pc = _plugins_cfg()
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = _shots_dir()

        title = str(pc.get("cw_ocr_window_title", "") or "崩坏：星穹铁道")
        hwnd = None
        try:
            hwnd = screen_capture.get_game_window(process_title=title)
        except Exception:
            hwnd = None

        x = int(float(pc.get("cw_ocr_x", 0) or 0))
        y = int(float(pc.get("cw_ocr_y", 0) or 0))
        w = int(float(pc.get("cw_ocr_w", 0) or 0))
        h = int(float(pc.get("cw_ocr_h", 0) or 0))
        if hwnd:
            # 坐标为窗口客户区相对坐标（挨打就电同款），截图前加当前偏移
            ox, oy = screen_capture.get_client_offset(hwnd)
            x, y = x + ox, y + oy

        paths = []
        if w > 0 and h > 0:
            bgra, _, _, rw, rh, _ = screen_capture.capture_screen_region(
                x, y, min(w, 4000), min(h, 4000))
            if bgra:
                p1 = os.path.join(out_dir, "debug_region_{}.png".format(ts))
                _write_png(p1, bgra, rw, rh)
                paths.append(p1)
        if hwnd:
            bgra2, _, _, cw2, ch2, _ = screen_capture.capture_screen_fast(hwnd=hwnd)
        else:
            bgra2, _, _, cw2, ch2, _ = screen_capture.capture_screen_fast()
        if bgra2:
            p2 = os.path.join(out_dir, "debug_full_{}.png".format(ts))
            _write_png(p2, bgra2, cw2, ch2)
            paths.append(p2)
        if not paths:
            return {"ok": False, "message": "诊断截图失败：区域未设置且截图为空"}
        names = ", ".join(os.path.basename(p) for p in paths)
        return {"ok": True,
                "message": "已保存 {}".format(names),
                "value": paths}
    except Exception as e:
        return {"ok": False, "message": "诊断截图失败: {}".format(e)}


def toggle_pause():
    """页面暂停/恢复惩罚输出（与手柄 LB+RB 等效）"""
    p = _plugin
    if not p:
        return {"ok": False, "message": "插件未启动"}
    paused = p.toggle_paused()
    return {"ok": True,
            "message": "已暂停惩罚输出" if paused else "已恢复惩罚输出",
            "value": {"paused": paused}}


def pick_region():
    """原生全屏框选（独立进程 tkinter 覆盖层），返回物理像素区域"""
    if not _pick_lock.acquire(blocking=False):
        return {"ok": False, "value": None,
                "message": "已有框选/取色在进行"}
    try:
        region, message = region_picker.pick_region(120)
        if region is None:
            return {"ok": False, "value": None, "message": message}
        return {"ok": True, "value": region, "message": message}
    except Exception as e:
        return {"ok": False, "value": None, "message": f"框选失败: {e}"}
    finally:
        _pick_lock.release()


def pick_color():
    """原生跟随取色（独立进程 tkinter 读数窗），返回 {hex,r,g,b}"""
    if not _pick_lock.acquire(blocking=False):
        return {"ok": False, "value": None,
                "message": "已有框选/取色在进行"}
    try:
        color, message = region_picker.pick_color(120)
        if color is None:
            return {"ok": False, "value": None, "message": message}
        return {"ok": True, "value": color, "message": message}
    except Exception as e:
        return {"ok": False, "value": None, "message": f"取色失败: {e}"}
    finally:
        _pick_lock.release()


def toggle_top(params):
    """配置窗口置顶/取消（Win32；Electron 30 渲染进程无 remote，只能由引擎代劳）"""
    try:
        import ctypes
        on = bool(params.get("on"))
        hwnd = screen_capture.find_main_window_by_title("惩罚姬")
        if not hwnd:
            return {"ok": False, "message": "未找到惩罚姬主窗口"}
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP = 0x0001 | 0x0002  # SWP_NOSIZE | SWP_NOMOVE
        ok = ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, SWP)
        if not ok:
            return {"ok": False, "message": "SetWindowPos 失败"}
        return {"ok": True, "message": "窗口已置顶" if on else "已取消置顶"}
    except Exception as e:
        return {"ok": False, "message": f"置顶失败: {e}"}


def window_anchor(params=None):
    """游戏窗口客户区左上角（框选结果转窗口相对坐标用）；找不到窗口 ok=False

    标题以页面传入为准（params.title）——页面显示的标题与服务端
    实际匹配的标题必须一致，否则报错会对不上号。
    """
    try:
        pc = app.config.get("plugins", {})
        title = str((params or {}).get("title")
                    or pc.get("cw_ocr_window_title", "")
                    or "崩坏：星穹铁道")
        hwnd = screen_capture.get_game_window(process_title=title)
        if hwnd:
            import ctypes
            if ctypes.windll.user32.IsIconic(hwnd):
                return json.dumps({"ok": False, "value": None,
                                   "message": f"窗口「{title}」已最小化，先还原再框选"})
            x, y = screen_capture.get_client_offset(hwnd)
            return json.dumps({"ok": True, "value": {"x": x, "y": y},
                               "message": f"窗口客户区: {x},{y}"})
        cands = [t for t in screen_capture.list_visible_windows(12)]
        return json.dumps({"ok": False, "value": None,
                           "message": f"未找到窗口「{title}」",
                           "candidates": cands})
    except Exception as e:
        return json.dumps({"ok": False, "value": None, "message": f"获取失败: {e}"})


# ---------------- 启停 ----------------

_plugin = None


# ---------------- 文件桥动作分发 ----------------

_ipc_status = None
_ipc_cmd = None

_HANDLERS = {}


def apply_config(params):
    """配置页保存：写回插件 config.json + 热更新内存配置"""
    cfg = params.get("config") or {}
    plugins = cfg.get("plugins") or {}
    waveform = cfg.get("waveform") or {}
    # 持久化（保留文件内其他字段）
    try:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
        data.setdefault("config", {})
        data["config"].setdefault("plugins", {}).update(plugins)
        data["config"].setdefault("waveform", {}).update(waveform)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        return {"ok": False, "message": f"写入配置文件失败: {e}"}
    # 热更新内存（引擎各处均为实时读取）
    app.config = {"plugins": dict(app.config.get("plugins", {})),
                  "waveform": dict(app.config.get("waveform", {}))}
    app.config["plugins"].update(plugins)
    app.config["waveform"].update(waveform)
    app.waveform = app.config["waveform"]
    p = _plugin
    if p:
        p.waveform = app.waveform
        if hasattr(p, "apply_config"):
            try:
                p.apply_config()
            except Exception:
                pass
    return {"ok": True, "message": "配置已保存并即时生效"}


def _register_handlers():
    _HANDLERS.update({
        "get_status": lambda params: get_status(),
        "get_waveform": lambda params: get_waveform(),
        "apply_config": apply_config,
        "reload_config": lambda params: reload_config(),
        "str_up": lambda params: str_up(),
        "str_down": lambda params: str_down(),
        "str_clear": lambda params: str_clear(),
        "test_conn": lambda params: test_conn(),
        "test_shock": lambda params: test_shock(params),
        "ocr_test": lambda params: ocr_test(),
        "plus_test": lambda params: plus_test(params),
        "window_anchor": lambda params: window_anchor(params),
        "pick_region": lambda params: pick_region(),
        "pick_color": lambda params: pick_color(),
        "toggle_top": lambda params: toggle_top(params),
        "debug_shot": lambda params: debug_shot(),
        "toggle_pause": lambda params: toggle_pause(),
        "game_shot": lambda params: game_shot(),
        "list_shots": lambda params: list_shots(),
        "open_shot": lambda params: open_shot(params),
        "del_shot": lambda params: del_shot(params),
    })


def dispatch(action, params):
    fn = _HANDLERS.get(action)
    if fn is None:
        return {"ok": False, "message": f"未知动作: {action}"}
    return fn(params if isinstance(params, dict) else {})


def start():
    global _plugin, _ipc_status
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    ipc.clear_runtime()
    _ipc_status = ipc.StatusWriter(provider=get_status)
    _ipc_status.start()
    _plugin = hsr_dglab.HsrDGLab(app)
    _plugin.start()
    _plugin.sync_strength_from_dock()
    _start_hud()
    _start_gamepad()
    cur = _plugin
    while cur is not None and cur.running:
        # 阻塞保持运行状态，惩罚姬据此判断插件开启中。
        # 每轮重新取快照：stop() 会把全局 _plugin 置 None，
        # 直接解引用全局会在停止瞬间崩溃（'NoneType' has no running）。
        import time
        time.sleep(0.2)
        cur = _plugin
    if _ipc_status:
        _ipc_status.stop()
    ipc.clear_runtime()


def stop():
    global _plugin, _hud, _gamepad_stop
    p = _plugin
    if p:
        p.stop()   # 先停引擎（running=False，start 线程循环自然退出）
    _plugin = None
    if _hud:
        _hud.stop()   # 只隐藏不销毁（挨打就电铁律），下次 start 复用
    if _gamepad_stop:
        _gamepad_stop.set()
        _gamepad_stop = None
    if _ipc_status:
        _ipc_status.stop()
    # 命令桥保持挂载（server 生命周期内框选/取色/置顶始终可用）
