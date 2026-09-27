# -*- coding: utf-8 -*-
"""
v4_backend.py - 用自研 V4 控制器顶替 dockdglab 的后端门面

对惩罚引擎 (hsr_dglab.HsrDGLab) 暴露与 dockdglab.DockDGLab 相同的接口：
  log / get_config / get_active / get_strength / set_strength /
  add_strength / reduce_strength / send_waveform / clear_waveform /
  coyote_strength / coyote_stop_punish

引擎的蓝牙分支 (_bt()==True) 是完整能力路径：
  绝对强度(coyote_strength set) -> 波形(send_waveform) -> 收尾自动归零
V4 下这些全部真实生效——这正是惩罚姬 App 中继做不到的部分。

新能力（2026-09-26 实机验证）：
  - 远程强度 t=3 相对增减（APP 滑块实时跟随）
  - 波形帧格式：ver3 数字 8 元组 / hex16 字符串直通（SDK 自带波形同款）
"""
import asyncio
import base64
import json
import os
import sys
import threading
import time

import v4ctrl

A, B = 0, 1
_CH = {"A": (0,), "B": (1,), "ALL": (0, 1)}


def _root_dir():
    """惩罚姬根目录：exe 所在目录，或其上级含 plugins 的目录"""
    start = (os.path.dirname(os.path.abspath(sys.executable))
             if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))
    for cand in (start, os.path.dirname(start)):
        if os.path.isdir(os.path.join(cand, "plugins")):
            return cand
    return start


def _pair_dir():
    """配对存档就放 exe 旁边（根目录）；旧的 client/v4_pair.json 自动搬过去"""
    root = _root_dir()
    new = os.path.join(root, "v4_pair.json")
    old = os.path.join(root, "client", "v4_pair.json")
    if not os.path.isfile(new) and os.path.isfile(old):
        try:
            import shutil as _sh
            _sh.copy2(old, new)
        except Exception:
            pass
    return root


def _ch_idx(channel):
    """'A'/'B'/'All' 或 0/1 -> 通道号元组"""
    if isinstance(channel, str):
        return _CH.get(channel.strip().upper() if channel.strip().upper() in ("ALL",) else channel.strip().upper()[:1],
                       (0,))
    return (int(channel),)


def normalize_wave(wave):
    """把引擎交来的波形统一成 V4 AppendPulseData 的 v 列表。

    引擎惯例：convert_pulse_data 产出 [[freq, strength], ...]（每对 100ms）；
    也接受 hex16 字符串（每串 4 拍 = 400ms，SDK 自带波形同款直通）。
    返回 (v, duration_ms)；空波形返回 (None, 0)。
    """
    if not isinstance(wave, list) or not wave:
        return None, 0
    if all(isinstance(x, str) and len(x) == 16 for x in wave):
        return [str(x).lower() for x in wave], len(wave) * 400
    pairs = []
    for p in wave:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            pairs.append((int(p[0]) & 0xFF, max(0, min(100, int(p[1])))))
    if not pairs:
        return None, 0
    # 每 4 对 [f,i] 组一个 ver3 帧：[f1,f2,f3,f4,i1,i2,i3,i4]，一帧 4 拍 400ms
    frames = []
    for i in range(0, len(pairs), 4):
        grp = pairs[i:i + 4]
        fs = [f for f, _ in grp] + [grp[-1][0]] * (4 - len(grp))
        ins = [s for _, s in grp] + [grp[-1][1]] * (4 - len(grp))
        frames.append(fs + ins)
    return frames, len(pairs) * 100


class V4Bridge:
    """V4Controller 的线程化门面：共享 asyncio loop，暴露同步 API。

    loop 由 client.py 创建（与 UI HTTP 服务共用），所有异步操作经
    run_coroutine_threadsafe 提交，任何线程都可安全调用。
    """

    # 配对信息存档：记住上次的 targetId，重启后地址/二维码不变，APP 可自动重连。
    # 冻结成 exe 后 __file__ 在临时目录里，必须落到 exe 旁的 client/ 目录。
    PAIR_FILE = os.path.join(_pair_dir(), "v4_pair.json")

    def _load_pair_tid(self):
        try:
            with open(self.PAIR_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
            tid = str(d.get("tid") or "").strip().lower()
            if tid and len(tid) == 8 and all(c in "0123456789abcdef" for c in tid):
                return tid
        except Exception:
            pass
        return None

    def _save_pair(self):
        try:
            with open(self.PAIR_FILE, "w", encoding="utf-8") as f:
                json.dump({"tid": self._ctl.tid, "port": self.port,
                           "addr": self._ctl.pair_url,
                           "saved": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def __init__(self, loop, port=9898, log=print, config_fn=None):
        self.loop = loop
        self.port = port
        self.out = log
        self._config_fn = config_fn
        self._ctl = v4ctrl.V4Controller(host="0.0.0.0", port=port,
                                        tid=self._load_pair_tid(), log=self._frame_log)
        self._ctl.pair_url = "ws://%s:%d?tid=%s" % (v4ctrl.lan_ip(), port, self._ctl.tid)
        self._save_pair()
        self._qr_cache = None
        self._qr_lock = threading.Lock()
        self._streams = {}   # channel -> asyncio.Task（补货流）

    # ---- 生命周期 ----
    def start(self):
        fut = asyncio.run_coroutine_threadsafe(self._ctl.start(), self.loop)
        fut.result(10)
        self.out("success", "V4 配对地址: %s" % self._ctl.pair_url)

    def stop(self):
        self._stop_streams((A, B))
        try:
            asyncio.run_coroutine_threadsafe(self._ctl.stop(), self.loop).result(5)
        except Exception:
            pass

    def _frame_log(self, text):
        try:
            self.out("debug", text)
        except Exception:
            print(text)

    # ---- 同步操作 ----
    def _call(self, coro, timeout=8.0):
        if not self._ctl.app_id:
            raise RuntimeError("APP 未接入（扫码配对后可用）")
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("APP 应答超时（%.0fs）：链路拥塞或已断开" % timeout)
        except asyncio.CancelledError:
            raise RuntimeError("RPC 被取消（链路切换中）")

    @property
    def connected(self):
        return bool(self._ctl.app_id)

    @property
    def tid(self):
        return self._ctl.tid

    @property
    def shocking(self):
        """是否有波形补货流在跑（= 正在电击）"""
        return any(not t.done() for t in self._streams.values())

    @property
    def pair_url(self):
        return self._ctl.pair_url

    def qr_data_uri(self):
        """配对二维码 data URI（懒生成并缓存，tid 不变则不变）"""
        with self._qr_lock:
            if self._qr_cache is None:
                svg = self._ctl.qr_svg(box_size=10)
                self._qr_cache = ("data:image/svg+xml;base64," +
                                  base64.b64encode(svg.encode("utf-8")).decode("ascii"))
            return self._qr_cache

    def strength_map(self):
        for info in (self._ctl.devices or {}).values():
            p = info.get("props") or {}
            return {"A": int(p.get("intensityA") or 0), "B": int(p.get("intensityB") or 0)}
        return {"A": 0, "B": 0}

    def add(self, channel, delta):
        """相对强度（t=3）——远程强度的核心操作"""
        for c in _ch_idx(channel):
            self._call(self._ctl.add_intensity(c, int(delta)), 5)

    def set_strength(self, channel, value):
        """绝对强度：0 走 t=7 归零（协议唯一绝对合法值），其余换算成增量 t=3"""
        value = int(value)
        for c in _ch_idx(channel):
            if value == 0:
                self._call(self._ctl.reset_intensity(c), 5)
            else:
                name = "A" if c == 0 else "B"
                delta = value - int(self.strength_map().get(name) or 0)
                if delta:
                    self._call(self._ctl.add_intensity(c, delta), 5)

    def zero(self, channel=None):
        """急停归零：channel=None 时全通道（zero_all 永不抛异常）"""
        self._stop_streams((A, B) if channel is None else _ch_idx(channel))
        if channel is None:
            asyncio.run_coroutine_threadsafe(self._ctl.zero_all(), self.loop).result(5)
            return
        for c in _ch_idx(channel):
            self._call(self._ctl.reset_intensity(c), 5)

    def _wave_opts(self):
        """从客户端配置读波形参数：d 基准（毫秒/帧）与通道。

        APP 会把波形循环补满 d——d 给多大就重复多少遍，默认 100ms/帧
        正好播一遍。通道支持只发 A 或 B（单通道用户省一半指令）。"""
        d_ms, ch = 100, "All"
        try:
            cfg = (self._config_fn() if self._config_fn else {}) or {}
            plug = cfg.get("plugins", {}) or {}
            d_ms = max(1, int(float(plug.get("wave_d_ms", 100))))
            c = str(plug.get("wave_channel", "All")).strip().upper()
            if c in ("A", "B"):
                ch = c
        except Exception:
            pass
        return d_ms, ch

    def send_wave(self, wave, channel="All", duration=None):
        """流式下发波形（非阻塞）。

        新触发 im=true 替换正在播放的波形（v3 清除后插入语义，挨打即时切换）；
        后续分块排队补货，队列不排空——一次性整条下发时 APP 会在
        播完 completed 后 1 秒断开连接（code 1000，实测 5 次全复现）。"""
        v, ms = normalize_wave(wave)
        if not v:
            raise RuntimeError("波形为空")
        _, ch = self._wave_opts()
        if channel == "All" and ch in ("A", "B"):
            channel = ch
        for c in _ch_idx(channel):
            self._start_stream(c, v, duration)

    def _start_stream(self, ch, v, duration=None):
        async def runner():
            old = self._streams.get(ch)
            if old is not None:
                old.cancel()
            task = asyncio.current_task()
            self._streams[ch] = task
            try:
                await self._stream_coro(ch, v, duration)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.out("error", "波形流下发失败: %s" % e)
            finally:
                if self._streams.get(ch) is task:
                    self._streams.pop(ch, None)
        asyncio.run_coroutine_threadsafe(runner(), self.loop)

    async def _stream_coro(self, ch, v, duration=None):
        """每块 4 串，每 1.2s 补一块。
        d = wave_d_ms（默认 100ms/帧）× 块帧数：APP 把波形循环补满 d，
        d 给多大就重复多少遍（d=0 会持续输出不停，实测电了一分钟）。
        duration（秒）显式给出时以其为准（持续电补满重发间隔）。
        首块 im=true 替换旧任务，后续块排队，队列深度稳定不排空。"""
        chunk, interval, first, i = 4, 1.2, True, 0
        d_per, _ = self._wave_opts()
        while i < len(v):
            part = v[i:i + chunk]
            i += chunk
            d_ms = int(duration * 1000) if duration else d_per * len(part)
            self._fire(ch, part, d_ms, first)
            first = False
            if i < len(v):
                await asyncio.sleep(interval)

    def _fire(self, ch, part, ms, immediate):
        async def go():
            try:
                await self._ctl.send_pulse(ch, part, ms, immediate=immediate)
            except Exception as e:
                # 未接 APP 时打击风暴会瞬间刷爆日志：同文案 5s 节流
                now = time.time()
                msg = str(e)
                if (msg != getattr(self, "_fire_err_msg", None)
                        or now - getattr(self, "_fire_err_last", 0) > 5):
                    self._fire_err_msg = msg
                    self._fire_err_last = now
                    self.out("error", "波形块下发失败: %s" % e)
        asyncio.ensure_future(go())

    def _stop_streams(self, chans):
        for c in chans:
            t = self._streams.pop(c, None)
            if t is not None:
                t.cancel()

    def clear(self, channel=None):
        """清空波形队列（device.op.clear）并停掉补货流"""
        chans = (A, B) if channel is None else _ch_idx(channel)
        self._stop_streams(chans)
        if channel is None:
            self._call(self._ctl.clear_ops(), 5)
            return
        for c in _ch_idx(channel):
            self._call(self._ctl.clear_ops(c), 5)

    def status(self):
        """给 UI 快照用的 v4 状态块（url/state 兼容旧渲染契约）"""
        c = self._ctl
        d = {"connected": self.connected, "addr": c.pair_url, "tid": c.tid,
             "qr": self.qr_data_uri()}
        for slot, info in (c.devices or {}).items():
            p = info.get("props") or {}
            d.update({
                "slot": slot,
                "name": "%s(%s)" % (info.get("name", "?"), info.get("type", "")),
                "battery": p.get("power"),
                "strength_a": p.get("intensityA"),
                "strength_b": p.get("intensityB"),
                "status_a": p.get("channelAStatus"),
                "status_b": p.get("channelBStatus"),
            })
            break
        # 渲染契约（ui_winui render 读 url/state）：0=等待接入 1=已连接 2=设备就绪
        d["url"] = c.pair_url
        d["state"] = 2 if d.get("slot") else (1 if self.connected else 0)
        return d


class V4Backend:
    """dockdglab.DockDGLab 兼容门面：惩罚引擎无感切换到 V4 后端"""

    def __init__(self, bridge, config_loader, log):
        self.bridge = bridge
        self._config_loader = config_loader
        self._log = log

    # ---- 日志 ----
    def log(self, level, msg):
        self._log(level, msg)

    # ---- 配置 ----
    def get_config(self, name):
        return self._config_loader()

    # ---- 链路状态 ----
    def get_active(self):
        # 引擎 _bt() 探测：coyote=="connect" -> 走蓝牙分支（完整能力路径）
        return {"coyote": "connect" if self.bridge.connected else "",
                "app": "connect" if self.bridge.connected else ""}

    def get_strength(self):
        return {"code": 200, "msg": dict(self.bridge.strength_map())}

    # ---- 强度 ----
    def set_strength(self, channel, strength):
        self.bridge.set_strength(channel, strength)
        return {"code": 200, "msg": "ok"}

    def add_strength(self, channel, strength):
        self.bridge.add(channel, int(strength))
        return {"code": 200, "msg": "ok"}

    def reduce_strength(self, channel, strength):
        self.bridge.add(channel, -int(strength))
        return {"code": 200, "msg": "ok"}

    def coyote_strength(self, option="set", channel="A", strength=0):
        if str(option) == "set":
            self.bridge.set_strength(channel, strength)
        return {"code": 200, "msg": "ok"}

    # ---- 波形 ----
    def send_waveform(self, waveform=None, channel="All", total_duration=None, **_):
        self.bridge.send_wave(waveform, channel, total_duration)
        return {"code": 200, "msg": "ok"}

    def clear_waveform(self):
        self.bridge.clear()
        return {"code": 200, "msg": "ok"}

    def coyote_stop_punish(self):
        self.bridge.clear()
        return {"code": 200, "msg": "ok"}
