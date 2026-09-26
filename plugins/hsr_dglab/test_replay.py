"""
test_replay.py - 用真实 veritas 采集日志回放，验证角色血量/盾量惩罚链路

数据源: %TEMP%/veritas_cap2.log（货币战争 4 关实测采集）
方法: MockVeritas 按时间压缩回放全部事件 -> 插件 core 真实处理
      -> 统计 MockDock 收到的 set_strength / send_waveform
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_mock import MockVeritas, MockDock, TEST_CONFIG  # noqa: E402

LOG = os.path.join(os.environ.get("TEMP", ""), "veritas_cap2.log")
SCALE = 50  # 时间压缩倍数（50x 快进）

VERITAS = MockVeritas()
DOCK = MockDock()


def main():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    # 真 veritas 在跑时 1305 不可绑——mock 换 13051 并让插件配置指过去
    TEST_CONFIG["plugins"]["veritas_url"] = "http://127.0.0.1:13051"
    # 回放走新公式 + 残血持续电（接近线上真实配置）
    TEST_CONFIG["plugins"]["dmg_mode"] = "ratio"
    TEST_CONFIG["plugins"]["low_sustain_enabled"] = True

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, text, code=200):
            raw = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            qs = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    qs.setdefault(k, []).append(v)
            self._reply(VERITAS.handle_get(qs))

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            qs = {}
            if self.path.startswith("/socket.io/") and "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    qs.setdefault(k, []).append(v)
            if "/websocket" in self.path or "/coyote" in self.path:
                try:
                    DOCK.record(self.path, json.loads(raw))
                except Exception:
                    DOCK.record(self.path, {"raw": raw})
                self._reply(json.dumps({"msg": "ok"}))
            elif self.path.startswith("/socket.io/"):
                VERITAS.handle_post(qs, raw)
                self._reply("ok")
            elif self.path == "/add_log":
                self._reply(json.dumps({"msg": "ok"}))
            else:
                self._reply("ok")

    v = ThreadingHTTPServer(("127.0.0.1", 13051), Handler)
    d = ThreadingHTTPServer(("127.0.0.1", 5000), Handler)
    threading.Thread(target=v.serve_forever, daemon=True).start()
    threading.Thread(target=d.serve_forever, daemon=True).start()

    # 解析采集日志 -> (rel_ts, name, payload)
    events = []
    t0 = None
    for ln in open(LOG, encoding="utf-8"):
        if ln.startswith("#"):
            continue
        parts = ln.split(" | ", 2)
        if len(parts) < 3:
            continue
        ts, name, body = parts
        hh, mm, ss = [int(x) for x in ts.split(":")]
        cur = hh * 3600 + mm * 60 + ss
        if t0 is None:
            t0 = cur
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        events.append((cur - t0, name, payload))
    print("[replay] 事件数:", len(events))

    import dockdglab
    import hsr_dglab as core_mod

    class FakeApp:
        pass
    app = FakeApp()
    app.config = TEST_CONFIG
    app.waveform = TEST_CONFIG["waveform"]
    app.server = dockdglab.DockDGLab()

    core = core_mod.HsrDGLab(app)
    core.start()
    time.sleep(1.5)

    prev = 0.0
    for rel, name, payload in events:
        delay = (rel - prev) / SCALE
        if delay > 0:
            time.sleep(delay)
        prev = rel
        VERITAS.emit(name, payload)
    time.sleep(2.5)

    adds = [a for p, a in DOCK.actions
            if a.get("action") in ("add_strength", "reduce_strength")]
    equiv = DOCK.set_strengths()   # 增量归一化后的等效绝对强度序列
    waves = [a for p, a in DOCK.actions if a.get("action") == "send_waveform"]
    clears = [a for p, a in DOCK.actions if a.get("action") == "clear_waveform"]
    print()
    print("=== 回放结果 ===")
    print("强度增量指令次数:", len(adds))
    print("等效强度轨迹（末 10 条）:")
    for ch, v in equiv[-10:]:
        print(f"    {ch} -> {v}")
    print("send_waveform 次数:", len(waves), "通道:", sorted({w.get("channel") for w in waves}))
    print("clear_waveform 次数:", len(clears))
    snap = core.snapshot()
    print("战斗中:", snap.get("battle_active"), "| 阵容:", [(a.get("name"), a.get("hp")) for a in snap.get("avatars", [])])


if __name__ == "__main__":
    main()
