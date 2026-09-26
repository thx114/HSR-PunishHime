"""
v4ctrl.py - DG-LAB V4 自研控制器（协议核心 + 验证器 CLI）

协议依据：开源 dglab-kit v1.0.5（github.com/dungeonlab-open/dglab-kit）
逆向提取的线缆格式 + 实抓报文验证（2026-09-26）：

  [接入握手]
    APP 连接 ws://<ip>:<port>?tid=<targetId>
    服务端(我们) -> {"type":"hello","clientId":"<appClientId>"}
    APP -> {"type":"message","data":{...}}（事件/响应，无需 clientId 路由）
    控制端 -> APP 必须带 clientId=<appClientId> 路由

  [RPC]  data = {"t":"req","reqId":"...","m":"devices.get|device.op|device.op.clear|ping","data":{...}}
         响应  {"t":"resp","reqId":"...","result":{...}|error:"invalid_operate"}

  [device.op data]  {s:slotId, c:0|1(A/B), t:<op>, v:..., d?:毫秒}
    t=0 AppendPulseData   v=[[频率,强度],...] 频率10-240 强度0-100
    t=3 AddIntensity      v=+-N   相对增减（惩罚姬从没发出去过的合法操作）
    t=4 SetTempIntensity  v=N d=毫秒
    t=7 SetIntensity      v=0（绝对值仅 0 合法，非 0 被 APP 拒绝 invalid_operate）
"""
import asyncio
import json
import os
import queue
import random
import socket
import tempfile
import threading
import time
import uuid

from aiohttp import web, WSMsgType

A, B = 0, 1
CH_NAME = {A: "A", B: "B"}

# 帧日志放根目录（<root>/v4_frames.log）：插件目录只留源码，产物统一在外面
_ROOT_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(_ROOT_DIR, "v4_frames.log")


_frame_q = queue.Queue()


def _frame_writer():
    """后台写线程：帧日志的文件与控制台 I/O 绝不占用事件循环线程。

    实测（2026-09-26 抓包）：slots.patch 洪泛期每帧同步 print+文件追加
    会把循环卡死数秒（APP 侧 resp 20ms 就回了，我们 9s 后才收到），
    pong 延迟后 APP 直接 close 1000。"""
    while True:
        try:
            text = _frame_q.get()
            stamp = time.strftime("%H:%M:%S")
            line = "[%s] %s" % (stamp, text)
            # ping/pong/插槽补丁不刷控制台（文件仍全量，排查用）
            quiet = ('"type":"ping"' in text or '"type": "ping"' in text
                     or '"type":"pong"' in text or '"type": "pong"' in text
                     or 'slots.patch' in text)
            if not quiet:
                print("   ·", line)
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        except Exception:
            pass


threading.Thread(target=_frame_writer, daemon=True,
                 name="v4-frame-log").start()


def frame_log(text: str) -> None:
    """原始帧日志（排查 APP 握手用）：入队后台写，循环线程零阻塞"""
    try:
        _frame_q.put_nowait(text)
    except Exception:
        pass


def _rid() -> str:
    return "c_%s_%d" % (uuid.uuid4().hex[:8], int(time.time() * 1000))


def _hex8() -> str:
    return "%08x" % random.randrange(1 << 32)


def lan_ip() -> str:
    """取本机局域网 IP（UDP 探测，不真正发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


class V4Controller:
    """V4 控制器核心：WS 服务端 + 设备注册表 + RPC。

    独立客户端与验证器共用本类。
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9898, tid: str = None, log=print):
        self.host = host
        self.port = port
        self.log = log
        self.tid = tid or _hex8()
        self.app_id = None            # APP 接入后分配的 clientId
        self.ws = None                # 当前 APP 连接
        self.devices = {}             # slotId -> info dict
        self._pending = {}            # reqId -> Future
        self._runner = None
        self._attached = asyncio.Event()
        self.pair_url = None       # 配对地址（start 后由调用方设置）

    def qr_content(self) -> str:
        """二维码内容：官方 dungeon-lab.cn 跳转包装链接（APP 只认这个格式）。

        依据 dglab-kit README「生成 APP 配对二维码」：
          https://dungeon-lab.cn/s/?v=1&action=socket&url=<encodeURIComponent(ws地址)>
        """
        from urllib.parse import quote
        return "https://dungeon-lab.cn/s/?v=1&action=socket&url=" + quote(self.pair_url or "", safe="")

    def qr_svg(self, box_size: int = 16) -> str:
        """配对二维码 SVG 字符串（白底黑块，浏览器直接可扫）"""
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(self.qr_content(), image_factory=qrcode.image.svg.SvgPathFillImage,
                          box_size=box_size, border=4)
        s = img.to_string()
        return s.decode("utf-8") if isinstance(s, bytes) else s

    def _qr_page(self, request) -> web.Response:
        """/qr 网页兜底：大图二维码 + 地址文本"""
        svg = self.qr_svg()
        html = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<title>V4 配对</title></head>'
                '<body style="background:#fff;margin:0;display:flex;flex-direction:column;'
                'align-items:center;justify-content:center;height:100vh;font-family:sans-serif">'
                '<div style="width:min(90vmin,480px)">%s</div>'
                '<p style="font-size:18px;margin:14px 0 4px">DG-LAB 4 APP 扫码接入</p>'
                '<code style="font-size:15px;user-select:all">%s</code>'
                '</body></html>') % (svg, self.pair_url)
        return web.Response(text=html, content_type="text/html")

    # ---------- 服务生命周期 ----------
    async def start(self) -> None:
        try:
            open(LOG_PATH, "w", encoding="utf-8").close()  # 每次启动清空帧日志
        except Exception:
            pass
        app = web.Application()
        app.router.add_get("/qr", self._qr_page)
        app.router.add_get("/", self._on_ws)
        # V4 协议支持任意 URL Path：除 /qr 外全部走 WS 握手
        app.router.add_get("/{tail:.*}", self._on_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.log("V4 服务已启动 ws://%s:%d  (tid=%s)" % (lan_ip(), self.port, self.tid))

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def wait_attach(self, timeout: float = 120.0) -> bool:
        try:
            await asyncio.wait_for(self._attached.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- WS 处理 ----------
    async def _on_ws(self, request) -> web.WebSocketResponse:
        tid = request.query.get("tid", "")
        if tid != self.tid:
            self.log("[拒绝] tid 不匹配: %r" % tid)
            return web.Response(status=403, text="bad tid")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.ws is not None:
            self.log("[拒绝] 已有 APP 在线，踢掉新连接")
            await ws.close()
            return ws
        self.ws = ws
        self.app_id = _hex8()
        self.log("[APP 已接入] clientId=%s from %s" % (self.app_id, request.remote))
        frame_log("<< CONNECT from %s" % request.remote)
        # 中继语义实测（官方 trex 中继 2026-09-26 抓取）：
        #   APP 接入后中继推给 APP 两帧：
        #     1) hello（分配 APP 的 clientId）
        #     2) controller_attached（控制方 tid）—— APP 等 this 5s，等不到就 close 1000
        self._log_out({"type": "hello", "clientId": self.app_id})
        if os.environ.get("DSH_V4_HELLO", "1") != "0":
            await self._send_raw({"type": "hello", "clientId": self.app_id})
        self._log_out({"type": "controller_attached", "clientId": self.tid})
        await self._send_raw({"type": "controller_attached", "clientId": self.tid})
        # 官方中继 10s 观察内未向 APP 推任何心跳 —— 默认不发（DSH_V4_HB=1 可实验开启）
        if os.environ.get("DSH_V4_HB") == "1":
            self._hb_task = asyncio.create_task(self._heartbeat_loop())
        self._attached.set()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    frame_log("<< %s" % msg.data[:400])
                    await self._on_frame(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    frame_log("<< [二进制 %d 字节] %s" % (len(msg.data), msg.data[:40].hex()))
                elif msg.type == WSMsgType.ERROR:
                    self.log("[WS 错误] %s" % msg.data)
        finally:
            self.log("[APP 断开] close_code=%s reason=%r" % (ws.close_code, getattr(ws, "close_reason", None)))
            frame_log("<< DISCONNECT code=%s reason=%r" % (ws.close_code, getattr(ws, "close_reason", None)))
            self.ws = None
            self.app_id = None
            self._attached.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("APP 断开"))
            self._pending.clear()
            self.devices.clear()
        return ws

    async def _send_raw(self, obj) -> None:
        if self.ws is None:
            raise ConnectionError("APP 未接入")
        await self.ws.send_str(json.dumps(obj, separators=(",", ":")))

    def _log_out(self, obj) -> None:
        try:
            frame_log(">> %s" % json.dumps(obj, ensure_ascii=False)[:400])
        except Exception:
            pass

    async def _heartbeat_loop(self) -> None:
        """实验开关 DSH_V4_HB=1：模拟中继向 APP 周期发 heartbeat"""
        try:
            while self.ws is not None:
                await self._send_raw({"type": "heartbeat"})
                await asyncio.sleep(1.5)
        except Exception:
            pass

    async def _on_frame(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except Exception:
            self.log("[原始帧·非JSON] %s" % text[:200])
            return
        ftype = frame.get("type")
        if ftype == "message":
            data = frame.get("data") or {}
            await self._on_data(data)
        elif ftype == "ping":
            # 中继语义：客户端 ping，服务端回 pong（SDK 控制端正是这么保活的）
            self._log_out({"type": "pong"})
            await self._send_raw({"type": "pong"})
        elif ftype == "heartbeat":
            pass  # 中继层心跳，合体服务无需回应
        elif ftype == "pong":
            pass
        else:
            self.log("[帧] %s" % json.dumps(frame, ensure_ascii=False)[:200])

    async def _on_data(self, data: dict) -> None:
        t = data.get("t")
        if t == "resp":
            rid = data.get("reqId")
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(data)
            else:
                self.log("[未匹配响应] %s" % json.dumps(data, ensure_ascii=False)[:200])
        elif t == "ev":
            self._on_event(data)
        elif t == "req":
            # APP 主动请求（如 ping），礼貌回应
            m = data.get("m")
            rid = data.get("reqId")
            await self._send_raw({"type": "message",
                                  "data": {"t": "resp", "reqId": rid, "result": {}}})
            if m != "ping":
                self.log("[APP请求·未知方法] %s" % m)
        else:
            self.log("[数据] %s" % json.dumps(data, ensure_ascii=False)[:200])

    def _on_event(self, data: dict) -> None:
        ev = data.get("ev")
        if ev == "devices.snapshot":
            for d in data.get("devices") or []:
                self.devices[d.get("slotId")] = d
            self.log("[设备快照] %s" % self._dev_str())
        elif ev == "devices.patch":
            for d in data.get("added") or []:
                self.devices[d.get("slotId")] = d
                self.log("[设备接入] %s" % self._dev_str(d))
            for s in data.get("removed") or []:
                self.log("[设备移除] %s" % s)
                self.devices.pop(s, None)
        elif ev == "slots.patch":
            for s in data.get("slots") or []:
                slot = self.devices.get(s.get("slotId"))
                if slot is not None:
                    self._deep_merge(slot, s)
            # 爬坡/衰减期 patch 每秒十余条，日志节流到 2s 一条，防循环被控制台 I/O 拖死
            if time.time() - getattr(self, "_last_slot_log", 0.0) > 2.0:
                self._last_slot_log = time.time()
                self.log("[插槽更新] %s" % json.dumps(data.get("slots"), ensure_ascii=False)[:200])
        else:
            self.log("[事件] %s" % json.dumps(data, ensure_ascii=False)[:200])

    @staticmethod
    def _deep_merge(dst: dict, src: dict) -> None:
        """slots.patch 增量是稀疏的：props/slotState 必须按键深合并，否则丢字段"""
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                V4Controller._deep_merge(dst[k], v)
            else:
                dst[k] = v

    def _dev_str(self, d=None) -> str:
        d = d or self.devices
        if isinstance(d, dict) and "slotId" not in d:
            return "; ".join(self._dev_str(v) for v in d.values())
        return "%s(%s/%s)" % (d.get("slotId"), d.get("name"), d.get("type"))

    # ---------- RPC ----------
    async def _rpc(self, app_id: str, method: str, data=None, timeout: float = 6.0) -> dict:
        rid = _rid()
        req = {"t": "req", "reqId": rid, "m": method}
        if data is not None:
            req["data"] = data
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send_raw({"type": "message", "clientId": app_id, "data": req})
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in resp and resp["error"]:
            raise RuntimeError("%s: %s" % (method, resp["error"]))
        return resp.get("result") or {}

    def _one_slot(self, slot: str = None) -> str:
        if slot:
            return slot
        if len(self.devices) == 1:
            return next(iter(self.devices))
        if not self.devices:
            raise RuntimeError("无已接入设备")
        raise RuntimeError("多设备需指定 slotId: %s" % ", ".join(self.devices))

    async def get_devices(self) -> dict:
        return await self._rpc(self.app_id, "devices.get")

    async def op(self, channel: int, op_type: int, value, slot: str = None,
                 duration_ms: int = None, timeout: float = 6.0,
                 priority: int = None, immediate: bool = None, ver: int = None) -> dict:
        s = self._one_slot(slot)
        payload = {"s": s, "c": channel, "t": op_type, "v": value}
        if duration_ms is not None:
            payload["d"] = int(duration_ms)
        if priority is not None:
            payload["p"] = int(priority)
        if immediate is not None:
            payload["im"] = bool(immediate)
        if ver is not None:
            payload["ver"] = int(ver)
        return await self._rpc(self.app_id, "device.op", payload, timeout)

    async def add_intensity(self, channel: int, delta: int, slot: str = None) -> dict:
        """t=3 相对增减（delta 可为负）"""
        return await self.op(channel, 3, int(delta), slot)

    async def set_temp_intensity(self, channel: int, value: int, duration_ms: int, slot: str = None) -> dict:
        """t=4 临时强度，到时自动回落"""
        return await self.op(channel, 4, int(value), slot, duration_ms=duration_ms)

    async def reset_intensity(self, channel: int, slot: str = None) -> dict:
        """t=7 v=0 归零（唯一合法的绝对强度操作）"""
        return await self.op(channel, 7, 0, slot)

    async def send_pulse(self, channel: int, frames, duration_ms: int, slot: str = None,
                         priority: int = None, immediate: bool = None, ver: int = None) -> dict:
        """t=0 波形：ver3 帧=[f1..f4,i1..i4]（默认省略）；ver2 帧=[频率,强度,间隔] 需传 ver=2；
        也接受 hex16 字符串数组（SDK 自带波形同款）"""
        return await self.op(channel, 0, frames, slot, duration_ms=duration_ms,
                             priority=priority, immediate=immediate, ver=ver)

    async def clear_ops(self, channel: int = None, slot: str = None) -> dict:
        data = {"s": slot or self._one_slot(slot)}
        if channel is not None:
            data["c"] = channel
        return await self._rpc(self.app_id, "device.op.clear", data)

    async def zero_all(self) -> None:
        """急停：A/B 全部归零 + 清空波形（尽力而为，永不抛异常）"""
        for ch in (A, B):
            try:
                await self.reset_intensity(ch)
            except Exception as e:
                self.log("[急停] %s 通道归零失败: %s" % (CH_NAME[ch], e))
            try:
                await self.clear_ops(ch)
            except Exception:
                pass
        self.log("[急停] 已发送 A/B 归零 + 清空队列")


# ---------- 验证器 CLI ----------
# V4 波形帧格式（官方文档 L821-826）：
#   ver3 数字帧（默认） = [a1,a2,a3,a4,b1,b2,b3,b4] = 4 频率 + 4 强度，一帧 4 拍×100ms
#   ver2 数字帧（需显式 ver:2） = [频率, 强度, 间隔ms]，一帧 1 拍
#   hex16 字符串 = ver3 的 8 字节打包形式，一串 4 拍
# 旧版我们发 [100,30] 二元组 → APP 按 ver3 解析强度全 0 → 收下但无输出（灯不闪的根因）
def _v3(freq, ints):
    """构造 ver3 数字帧：4 拍频率 + 4 拍强度"""
    return [freq, freq, freq, freq] + list(ints)


TEST_WAVE = [
    _v3(100, (20, 20, 40, 40)),
    _v3(100, (40, 40, 60, 60)),
    _v3(100, (60, 60, 40, 40)),
    _v3(100, (40, 40, 20, 20)),
] * 2                                             # 8 帧 × 400ms = 3.2s 温和爬坡
TEST_WAVE_10S = (_v3(100, (30, 30, 30, 30)),) * 10 \
    + (_v3(100, (50, 50, 50, 50)),) * 10 \
    + (_v3(100, (30, 30, 30, 30)),) * 5           # 25 帧 × 400ms = 10s
VER2_WAVE = [[100, i, 100] for i in (20, 40, 60, 40)] * 8    # ver2：32 拍 × 100ms = 3.2s
# hex16：前 4 字节=频率(0x64=100Hz)，后 4 字节=强度(0x1E=30% / 0x3C=60%)
HEX_WAVE = ["646464641E1E1E1E", "646464643C3C3C3C"] * 4      # 8 串 × 400ms = 3.2s


async def _cli() -> None:
    import qrcode

    c = V4Controller(port=9898)
    await c.start()
    url = "ws://%s:%d?tid=%s" % (lan_ip(), c.port, c.tid)
    c.pair_url = url
    print("=" * 56)
    print(" DG-LAB V4 控制器验证器")
    print(" 配对地址: %s" % url)
    print("=" * 56)
    # 浏览器大图二维码（终端 ASCII 在 Windows 控制台经常扫不出来）
    svg_path = os.path.join(tempfile.gettempdir(), "v4_pair_qr.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(c.qr_svg())
    opened = False
    try:
        os.startfile(svg_path)
        opened = True
    except Exception:
        pass
    if opened:
        print("已自动打开二维码图片: %s" % svg_path)
    else:
        print("请手动打开二维码网页: http://127.0.0.1:%d/qr" % c.port)

    print("等待 APP 接入（120 秒超时）...")
    if not await c.wait_attach(120):
        print("超时退出")
        await c.stop()
        return
    # 等设备快照到达
    for _ in range(40):
        if c.devices:
            break
        await asyncio.sleep(0.25)
    print("当前设备:", c._dev_str() if c.devices else "(尚未上报，可发 1 主动拉取)")

    loop = asyncio.get_event_loop()
    help_txt = ("命令: 1=拉设备列表  2/3/4=A +5/-5/归零  5/6/7=B +5/-5/归零\n"
                "      g=A/B各+15  8=ver3帧  v=ver2帧  h=hex帧  i=加急  0=长测10s  st=状态  9=清空  q=急停")
    print(help_txt)
    while True:
        cmd = await loop.run_in_executor(None, input, "v4> ")
        cmd = cmd.strip().lower()
        try:
            if cmd == "q":
                print("退出前急停（A/B 归零 + 清队列）...")
                await c.zero_all()
                break
            elif cmd == "1":
                print(await c.get_devices())
            elif cmd == "st":
                # 设备状态速览：强度/静音/输出状态（2=输出正常）
                for s, d in c.devices.items():
                    p = d.get("props") or {}
                    ss = d.get("slotState") or {}
                    ca = (ss.get("channelA") or {})
                    cb = (ss.get("channelB") or {})
                    print("slot %s 强度A=%s B=%s | A:静音=%s 输出状态=%s 上限=%s | B:静音=%s 输出状态=%s 上限=%s | 灯=%s" % (
                        s, p.get("intensityA"), p.get("intensityB"),
                        ca.get("isMuted"), p.get("channelAStatus"), ca.get("intensityMax"),
                        cb.get("isMuted"), p.get("channelBStatus"), cb.get("intensityMax"),
                        ss.get("markLight")))
            elif cmd == "2":
                print("A +5 ->", await c.add_intensity(A, 5))
            elif cmd == "3":
                print("A -5 ->", await c.add_intensity(A, -5))
            elif cmd == "4":
                print("A 归零 ->", await c.reset_intensity(A))
            elif cmd == "5":
                print("B +5 ->", await c.add_intensity(B, 5))
            elif cmd == "6":
                print("B -5 ->", await c.add_intensity(B, -5))
            elif cmd == "7":
                print("B 归零 ->", await c.reset_intensity(B))
            elif cmd == "g":
                print("A +15 ->", await c.add_intensity(A, 15))
                print("B +15 ->", await c.add_intensity(B, 15))
            elif cmd == "8":
                print("波形·ver3数字帧(3.2s) ->", await c.send_pulse(A, TEST_WAVE, 3200))
            elif cmd == "v":
                print("波形·ver2数字帧(3.2s) ->", await c.send_pulse(A, VER2_WAVE, 3200, ver=2))
            elif cmd == "0":
                print("波形长测(10s, A通道) ->", await c.send_pulse(A, TEST_WAVE_10S, 10000))
            elif cmd == "i":
                # 加急实验：p=2 最高优先级 + im 立即执行
                print("波形加急(3.2s, p=2 im=true) ->",
                      await c.send_pulse(A, TEST_WAVE, 3200, priority=2, immediate=True))
            elif cmd == "h":
                # 官方 hex16 字符串格式（SDK 自带波形同款），APP 播放路径可能与数字对不同
                print("波形·hex格式(3.2s, 100Hz/40%) ->", await c.send_pulse(A, HEX_WAVE, 3200))
            elif cmd == "9":
                print("清空 ->", await c.clear_ops())
            elif cmd == "":
                continue
            else:
                print(help_txt)
        except Exception as e:
            print("[失败]", e)
    await c.stop()
    print("已退出")


if __name__ == "__main__":
    try:
        asyncio.run(_cli())
    except KeyboardInterrupt:
        print("中断退出")
