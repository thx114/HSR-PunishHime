# -*- coding: utf-8 -*-
"""
v4_mock_app.py - V4 控制器线缆协议自检（回环模拟手机 APP）

覆盖：hello 握手 / devices.snapshot / devices.get / t=3 +-N / t=7 v=0 /
     t=4 临时强度 / t=0 波形 / device.op.clear / error 传递 / 断连清理
"""
import asyncio
import json

import aiohttp
from aiohttp import WSMsgType

import v4ctrl
from v4ctrl import V4Controller, A, B

PORT = 19898
TID = v4ctrl._hex8()
seen_reqs = []          # 收到的 req data 列表
state = {"reject_next": False}


async def mock_app(ws):
    """模拟手机 APP：回应 RPC、上报设备快照"""
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            break
        frame = json.loads(msg.data)
        if frame.get("type") != "message":
            continue
        data = frame.get("data") or {}
        if data.get("t") != "req":
            continue
        seen_reqs.append(data)
        m = data.get("m")
        rid = data.get("reqId")
        resp = {"t": "resp", "reqId": rid}
        if state["reject_next"]:
            state["reject_next"] = False
            resp["error"] = "invalid_operate"
        elif m == "devices.get":
            resp["result"] = {"devices": [{"slotId": "_mock1", "name": "MockCoyote", "type": "COYOTE_030"}]}
        else:
            resp["result"] = {}
        await ws.send_str(json.dumps({"type": "message", "data": resp}))


def find_op(op_type, **kw):
    for d in seen_reqs:
        if d.get("m") != "device.op":
            continue
        payload = d.get("data") or {}
        if payload.get("t") != op_type:
            continue
        if all(payload.get(k) == v for k, v in kw.items()):
            return payload
    return None


async def main():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(("[PASS] " if cond else "[FAIL] ") + name)

    c = V4Controller(host="127.0.0.1", port=PORT, tid=TID)
    await c.start()

    session = aiohttp.ClientSession()
    ws = await session.ws_connect("ws://127.0.0.1:%d/?tid=%s" % (PORT, TID))
    hello = json.loads((await ws.receive()).data)
    check("hello 握手", hello.get("type") == "hello" and len(hello.get("clientId", "")) == 8)

    reader = asyncio.create_task(mock_app(ws))
    await asyncio.sleep(0.1)

    # APP 上报设备快照（模拟接入即推）
    snap = {"type": "message", "data": {"t": "ev", "ev": "devices.snapshot",
            "devices": [{"slotId": "_mock1", "name": "MockCoyote", "type": "COYOTE_030",
                         "slotState": {"hasDevice": True}}]}}
    await ws.send_str(json.dumps(snap))
    await asyncio.sleep(0.3)
    check("设备快照注册", "_mock1" in c.devices)

    # devices.get 主动拉取
    r = await c.get_devices()
    check("devices.get RPC", r.get("devices", [{}])[0].get("slotId") == "_mock1")

    # t=3 相对强度 +-N
    await c.add_intensity(A, 5)
    check("t=3 A通道+5", find_op(3, s="_mock1", c=0, v=5) is not None)
    await c.add_intensity(B, -3)
    check("t=3 B通道-3(负值)", find_op(3, s="_mock1", c=1, v=-3) is not None)

    # t=7 归零（v 必须为 0）
    await c.reset_intensity(A)
    check("t=7 A归零 v=0", find_op(7, s="_mock1", c=0, v=0) is not None)

    # t=4 临时强度（带时长）
    await c.set_temp_intensity(B, 10, 2000)
    check("t=4 B临时10/2s", find_op(4, s="_mock1", c=1, v=10, d=2000) is not None)

    # t=0 波形（帧数组 + 时长）
    wave = [[100, 30], [100, 60], [100, 90], [100, 60]]
    await c.send_pulse(A, wave, 3000)
    check("t=0 波形推送", find_op(0, s="_mock1", c=0, v=wave, d=3000) is not None)

    # error 传递：mock 拒绝下一次操作
    state["reject_next"] = True
    try:
        await c.add_intensity(A, 99)
        check("error 传递->异常", False)
    except RuntimeError as e:
        check("error 传递->异常", "invalid_operate" in str(e))

    # device.op.clear
    await c.clear_ops()
    clears = [d for d in seen_reqs if d.get("m") == "device.op.clear"]
    check("device.op.clear", len(clears) >= 1 and clears[-1]["data"].get("s") == "_mock1")

    # 急停：A/B 各一次 t=7 v=0
    await c.zero_all()
    check("急停 A 归零", find_op(7, s="_mock1", c=0, v=0) is not None)
    check("急停 B 归零", find_op(7, s="_mock1", c=1, v=0) is not None)

    # tid 错误拒绝
    try:
        ws_bad = await session.ws_connect("ws://127.0.0.1:%d/?tid=wrongwrong" % PORT)
        bad = await ws_bad.receive()
        check("错误tid被拒", ws_bad.closed or bad.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING))
    except Exception:
        check("错误tid被拒", True)

    # 断连清理
    await ws.close()
    await asyncio.sleep(0.3)
    check("断连后设备清空", len(c.devices) == 0 and c.ws is None)

    reader.cancel()
    await session.close()
    await c.stop()

    fails = [n for n, ok in results if not ok]
    print("-" * 40)
    print("全部通过 ✓" if not fails else "失败: %s" % fails)


if __name__ == "__main__":
    asyncio.run(main())
