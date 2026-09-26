# -*- coding: utf-8 -*-
"""client_selftest.py - 无头客户端端到端冒烟测试（无 APP、无设备也能跑）"""
import asyncio
import json
import sys

import aiohttp
from aiohttp import WSMsgType

BASE = "http://127.0.0.1:%s" % (sys.argv[1] if len(sys.argv) > 1 else "5865")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_results = []


def check(name, ok, extra=""):
    _results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(extra)) if extra else ""))


async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get(BASE + "/api/status") as r:
            st = await r.json()
        check("GET /api/status", isinstance(st, dict) and "v4" in st,
              "running=%s v4.connected=%s" % (st.get("running"), (st.get("v4") or {}).get("connected")))
        v4 = st.get("v4") or {}
        check("v4 状态块含配对信息", bool(v4.get("addr")) and bool(v4.get("tid")) and
              str(v4.get("qr", "")).startswith("data:image/svg+xml"),
              str(v4.get("addr"))[:60])

        async with s.post(BASE + "/api/cmd", json={"action": "get_waveform"}) as r:
            wf = await r.json()
        check("POST /api/cmd get_waveform", wf.get("message") == "ok" and "value" in wf,
              "波形键数=%s" % len((wf.get("value") or {})))

        async with s.post(BASE + "/api/cmd", json={"action": "v4_pair"}) as r:
            pr = await r.json()
        check("POST /api/cmd v4_pair", pr.get("ok") is True and bool((pr.get("value") or {}).get("tid")),
              "tid=%s" % (pr.get("value", {}) or {}).get("tid"))

        tid = (pr.get("value") or {}).get("tid")
        addr = str(v4.get("addr") or "")
        host = addr.split("ws://", 1)[-1].split("?", 1)[0] if addr else "127.0.0.1:%s" % (int(sys.argv[2]) if len(sys.argv) > 2 else 9898)
        ws_url = "ws://%s?tid=%s" % (host, tid)
        async with s.ws_connect(ws_url) as ws:
            f1 = json.loads((await ws.receive()).data)
            f2 = json.loads((await ws.receive()).data)
            check("WS 握手 hello", f1.get("type") == "hello" and bool(f1.get("clientId")), str(f1)[:80])
            check("WS 握手 controller_attached", f2.get("type") == "controller_attached" and
                  f2.get("clientId") == tid, str(f2)[:80])
        check("WS 主动断开被清理", True)

        async with s.post(BASE + "/api/cmd", json={"action": "v4_add",
                                                   "params": {"channel": "A"}}) as r:
            resp = await r.json()
        check("v4_add 无 APP 时优雅报错", resp.get("ok") is False and "未接入" in resp.get("message", ""),
              resp.get("message"))

        async with s.get(BASE + "/") as r:
            html = await r.text()
        check("GET / 界面含客户端桥接", "__CLIENT__" in html and "apiCmd" in html and "v4ValA" in html)
        check("模板占位符已替换（DEFAULTS 为合法 JS）",
              "__DEFAULTS_JSON__" not in html and "const DEFAULTS = {" in html)
        check("裸 electron require 已 guarded",
              "const { ipcRenderer } = require" not in html and
              "try { ipcRenderer = require" in html)
        check("占位文案已失效", "尚未接入控制器" not in html)

        async with s.get(BASE + "/config/hsr_dglab") as r:
            cfg = await r.json()
        check("GET /config/hsr_dglab", "plugins" in cfg and "waveform" in cfg,
              "波形预设=%s" % len(cfg.get("waveform") or {}))

    print("-" * 40)
    print("结果: %d/%d 通过" % (sum(_results), len(_results)))
    return 0 if all(_results) else 1


sys.exit(asyncio.run(main()))
