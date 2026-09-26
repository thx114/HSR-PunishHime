"""
sio_client.py - 极简 Socket.IO v5 (Engine.IO v4) 客户端

仅依赖 requests（惩罚姬自带），用轮询(polling)传输连接 veritas 的
socketioxide 服务器（默认 http://127.0.0.1:1305），不引入任何新库。

协议要点（Engine.IO v4）:
- 握手: GET /socket.io/?EIO=4&transport=polling -> "0{sid,pingInterval,...}"
- 心跳: 客户端每 pingInterval 秒 POST "2"(ping)，服务器回 "3"(pong)
- Socket.IO: 连接命名空间 POST "40"，事件帧 '42["事件名",载荷]'，
  一轮轮询可能带回多个帧，以 \x1e 分隔
"""
import json
import threading
import time

import requests


class SioClient:
    def __init__(self, url, on_event, log=None, on_state=None):
        """
        :param url: veritas 服务地址，如 http://127.0.0.1:1305
        :param on_event: 回调 fn(event_name, payload)
        :param log: 回调 fn(level, msg)
        :param on_state: 回调 fn(state)  state in ("connected", "disconnected")
        """
        self.url = (url or "http://127.0.0.1:1305").rstrip("/")
        self.on_event = on_event
        self.log = log or (lambda level, msg: None)
        self.on_state = on_state
        self.version = None            # veritas 版本号（Connected 事件）
        self.last_event_ts = 0.0       # 最近一次收到 veritas 事件的时间
        self._session = requests.Session()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False

    # ---------------- 对外 ----------------
    @property
    def connected(self):
        return self._connected

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sio-client")
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------------- 内部 ----------------
    def _base(self):
        return f"{self.url}/socket.io/"

    def _ts(self):
        return str(time.time()).replace(".", "")[-7:]

    def _set_state(self, state):
        was = self._connected
        self._connected = (state == "connected")
        if self._connected and not was:
            self.log("success", f"已连接 veritas: {self.url}")
        elif not self._connected and was:
            self.log("warning", "与 veritas 的连接断开，正在自动重连...")
        if self.on_state:
            try:
                self.on_state(state)
            except Exception:
                pass

    def _post(self, sid, body):
        self._session.post(
            self._base(),
            params={"EIO": "4", "transport": "polling", "t": self._ts(), "sid": sid},
            data=body,
            headers={"Content-Type": "text/plain; charset=UTF-8"},
            timeout=10,
        )

    def _handle_packet(self, pkt, sid):
        if pkt == "2":                      # 服务器主动 ping（兼容）
            try:
                self._post(sid, "3")
            except Exception:
                pass
        elif pkt.startswith("40"):          # 命名空间连接成功
            self._set_state("connected")
        elif pkt.startswith("42"):
            try:
                data = json.loads(pkt[2:])
                name = data[0]
                payload = data[1] if len(data) > 1 else None
                if name == "Connected" and isinstance(payload, dict):
                    self.version = payload.get("version")
                self.last_event_ts = time.time()
                self.on_event(name, payload)
            except Exception as e:
                self.log("debug", f"事件解析失败: {e} | {pkt[:120]}")
        elif pkt.startswith("44"):
            self.log("warning", f"socket.io 错误: {pkt[:120]}")
        # "3" pong / "0" 重新握手 等情况无需处理

    def _run(self):
        backoff = 1.0
        down_announced = False
        while not self._stop.is_set():
            sid = None
            try:
                # 1) 握手
                r = self._session.get(
                    self._base(),
                    params={"EIO": "4", "transport": "polling", "t": self._ts()},
                    timeout=10,
                )
                r.raise_for_status()
                if not r.text.startswith("0"):
                    raise RuntimeError(f"握手异常: {r.text[:80]}")
                info = json.loads(r.text[1:])
                sid = info["sid"]
                ping_interval = max(1.0, info.get("pingInterval", 25000) / 1000.0)

                # 2) 连接默认命名空间
                self._post(sid, "40")
                self._set_state("connected")
                backoff = 1.0
                down_announced = False

                # 3) 心跳线程（EIO4 由客户端主导 ping）
                threading.Thread(
                    target=self._ping_loop, args=(sid, ping_interval), daemon=True
                ).start()

                # 4) 长轮询接收
                while not self._stop.is_set():
                    r = self._session.get(
                        self._base(),
                        params={"EIO": "4", "transport": "polling",
                                "t": self._ts(), "sid": sid},
                        timeout=(5, ping_interval + 10),
                    )
                    if r.status_code != 200:
                        raise RuntimeError(f"poll 状态码 {r.status_code}")
                    if self._stop.is_set():
                        break
                    if r.text:
                        for pkt in r.text.split("\x1e"):
                            if pkt:
                                self._handle_packet(pkt, sid)
            except Exception as e:
                if self._stop.is_set():
                    break
                self._set_state("disconnected")
                # veritas 未启动时静默重试，只在状态切换时记一条日志
                # （惩罚姬日志页由服务端持续追加+自动滚动，刷屏会导致页面无法阅读/清空无效）
                if not down_announced:
                    down_announced = True
                    self.log("info", "[veritas] 未连接，后台自动重试中（" + str(e)[:100] + "）")
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
        self._set_state("disconnected")

    def _ping_loop(self, sid, interval):
        while not self._stop.is_set() and self._connected:
            try:
                self._post(sid, "2")
            except Exception:
                return
            for _ in range(int(interval * 5)):
                if self._stop.is_set():
                    return
                time.sleep(0.2)
