"""
test_mock.py - 开发自测脚本（不影响插件运行）

本机模拟两件东西：
  1. veritas 的 Socket.IO 轮询服务器 (127.0.0.1:1305)
  2. 惩罚姬 dockdglab 后端 (127.0.0.1:5000)
驱动插件核心跑一遍完整事件流，验证 掉血->加成->叠加->强度/波形 输出。

用法: python test_mock.py
"""
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 真 veritas 在跑时 1305 不可绑；可用环境变量换端口
MOCK_VERITAS_PORT = int(os.environ.get("MOCK_VERITAS_PORT", "13051"))

TEST_CONFIG = {
    "plugins": {
        # ---- 模块 ----
        "module_battle_enabled": True,
        "module_cw_enabled": True,
        "battle_data_source": "veritas",
        "dmg_mode": "legacy",
        "low_sustain_enabled": False,
        # ---- 货币战争 ----
        "cw_total_source": "ocr",
        "cw_ocr_mode": "delta",
        "cw_ocr_enabled": False,
        "cw_ocr_url": "http://127.0.0.1:1395",
        "cw_ocr_colors": "#F78679",
        "cw_ocr_tolerance": 30,
        "cw_ocr_x": 0, "cw_ocr_y": 0, "cw_ocr_w": 0, "cw_ocr_h": 0,
        "cw_ocr_interval": 0.3,
        "cw_total_drop_threshold": 1,
        "cw_delta_dedup": 2.0,
        "cw_ocr_confirm_frames": 1,
        "cw_total_strength_a": 25,
        "cw_total_strength_b": 25,
        "cw_sustain_enabled": True,
        "cw_sustain_duration": 1.0,
        "cw_sustain_strength": 0,
        "cw_sustain_stop_after": True,
        "cw_total_uid": "",
        "cw_probe": False,
        # ---- 常规战斗 ----
        "veritas_url": "http://127.0.0.1:%d" % MOCK_VERITAS_PORT,
        "only_in_battle": True,
        "hit_aggregate_window": 0.1,
        "strength_a": 20,
        "strength_b": 20,
        "trigger_interval": 0.1,
        "health_drop_threshold": 0,
        "hit_min_percent": 0,
        "shield_punish_enabled": True,
        "shield_strength_a": 18,
        "shield_strength_b": 17,
        "shield_drop_threshold": 0,
        "shield_blocks_health": True,
        "damage_enabled": True,
        "damage_mid_value": 3000,
        "damage_max_bonus": 10,
        "damage_formula": "default",
        "overlap_enabled": True,
        "overlap_strength_add": 1,
        "overlap_strength_max": 200,
        "overlap_duration_multiplier": 1.5,
        "reset_overlap_on_battle": True,
        "overlap_decay_enabled": False,
        "knockdown_enabled": True,
        "knockdown_add": 15,
        "battle_end_mode": "clear",
        "battle_end_strength": 0,
        "ignore_names": "",
    },
    "waveform": {
        "hit_pulse": ["1414141464646464", "0A0A0A0A50505050", "0A0A0A0A0A0A0A0A"],
        "shield_pulse": ["0A0A0A0A50505050", "0A0A0A0A50505050", "0A0A0A0A28282828", "0A0A0A0A0A0A0A0A"],
        "knockdown_pulse": ["1414141464646464", "1414141464646464", "0A0A0A0A14141414"],
        "cw_total_pulse": ["1414141464646464", "0A0A0A0A64646464", "0A0A0A0A0A0A0A0A"],
    },
}

# ================= Mock veritas =================


class MockVeritas:
    def __init__(self):
        self.clients = {}
        self._lock = threading.Lock()
        self._sid = 0

    def handle_get(self, qs):
        sid = qs.get("sid", [None])[0]
        if not sid:
            with self._lock:
                self._sid += 1
                sid = f"mock{self._sid}"
                self.clients[sid] = queue.Queue()
            info = {"sid": sid, "upgrades": ["websocket"], "pingInterval": 25000,
                    "pingTimeout": 20000, "maxPayload": 1000000}
            return "0" + json.dumps(info, separators=(",", ":"))
        q = self.clients.get(sid)
        if q is None:
            return ""
        try:
            return q.get(timeout=15)
        except queue.Empty:
            return ""

    def handle_post(self, qs, body):
        sid = qs.get("sid", [None])[0]
        q = self.clients.get(sid)
        if q is None:
            return
        if body == "2":            # EIO4: 服务器回应客户端 ping
            q.put("3")
        elif body.startswith("40"):
            q.put('40{"sid":"%s"}' % sid)

    def emit(self, name, payload):
        frame = '42[%s,%s]' % (
            json.dumps(name),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        with self._lock:
            for q in list(self.clients.values()):
                q.put(frame)


# ================= Mock dockdglab 后端 =================


class MockDock:
    def __init__(self):
        self.actions = []      # (path, json)
        self._lock = threading.Lock()
        self._sim = {"A": 0, "B": 0}   # 模拟设备强度状态（add/reduce 增量语义）
        self._sim_log = []     # (channel, 模拟后强度值)

    def record(self, path, data):
        with self._lock:
            self.actions.append((path, data))
            if path == "/websocket":
                act = data.get("action")
                ch = data.get("channel", "All")
                v = int(data.get("strength") or 0)
                if act == "set_strength":
                    targets = ("A", "B") if ch == "All" else (ch,)
                    for c in targets:
                        self._sim[c] = v
                elif act == "add_strength":
                    targets = ("A", "B") if ch == "All" else (ch,)
                    for c in targets:
                        self._sim[c] += v
                elif act == "reduce_strength":
                    targets = ("A", "B") if ch == "All" else (ch,)
                    for c in targets:
                        self._sim[c] -= v
                else:
                    return
                self._sim_log.append((ch, self._sim["A"] if ch in ("All", "A")
                                      else self._sim[ch]))

    def set_strengths(self):
        """等效绝对强度视图：A/B 成对相同值归并为 All（引擎对 All 分两条增量）"""
        with self._lock:
            raw = list(self._sim_log)
        res = []
        i = 0
        while i < len(raw):
            ch, v = raw[i]
            if (i + 1 < len(raw) and ch == "A" and raw[i + 1][0] == "B"
                    and raw[i + 1][1] == v):
                res.append(("All", v))
                i += 2
            else:
                res.append((ch, v))
                i += 1
        return res

    def waveforms(self):
        with self._lock:
            return [(d.get("channel"), d.get("waveform"))
                    for p, d in self.actions
                    if p == "/websocket" and d.get("action") == "send_waveform"]

    def clears(self):
        with self._lock:
            return [d for p, d in self.actions
                    if p == "/websocket" and d.get("action") == "clear_waveform"]

    def logs(self):
        with self._lock:
            return [(d.get("type"), d.get("text")) for p, d in self.actions if p == "/add_log"]


VERITAS = MockVeritas()
DOCK = MockDock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, body, code=200):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/socket.io/"):
            qs = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    qs.setdefault(k, []).append(v)
            self._reply(VERITAS.handle_get(qs))
        elif self.path.startswith("/config/"):
            self._reply(json.dumps(TEST_CONFIG, ensure_ascii=False))
        else:
            self._reply("{}", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if self.path.startswith("/socket.io/"):
            qs = {}
            for kv in self.path.split("?", 1)[1].split("&"):
                k, _, v = kv.partition("=")
                qs.setdefault(k, []).append(v)
            VERITAS.handle_post(qs, raw)
            self._reply("ok")
        elif self.path in ("/websocket", "/coyote"):
            try:
                DOCK.record(self.path, json.loads(raw))
            except Exception:
                DOCK.record(self.path, {"raw": raw})
            self._reply(json.dumps({"msg": "ok"}))
        elif self.path == "/add_log":
            try:
                DOCK.record("/add_log", json.loads(raw))
            except Exception:
                pass
            self._reply(json.dumps({"msg": "ok"}))
        else:
            self._reply("{}", 404)


def start_servers():
    v = ThreadingHTTPServer(("127.0.0.1", MOCK_VERITAS_PORT), Handler)
    d = ThreadingHTTPServer(("127.0.0.1", 5000), Handler)
    threading.Thread(target=v.serve_forever, daemon=True).start()
    threading.Thread(target=d.serve_forever, daemon=True).start()
    return v, d


# ================= 测试驱动 =================


def wait_for(fn, timeout=5.0, desc=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"等待超时: {desc}")


def main():
    servers = start_servers()
    print("[mock] veritas@1305 dock@5000 已启动")

    import dockdglab
    import hsr_dglab as core_mod

    class FakeApp:
        pass

    app = FakeApp()
    app.config = TEST_CONFIG
    app.waveform = TEST_CONFIG["waveform"]
    app.server = dockdglab.DockDGLab()

    core = core_mod.HsrDGLab(app)
    core._bt_mode = False   # mock 只有 /websocket 后端，强制 App 中继分支
    core.start()

    P = "Player"
    stat = lambda uid, t, v: VERITAS.emit("OnStatChange", {
        "entity": {"uid": uid, "team": P}, "property": {"type": t, "value": v}})

    # 1. 连接 + 心跳 + 进战斗 + 阵容
    VERITAS.emit("Connected", {"version": "0.2.48"})
    VERITAS.emit("Heartbeat", None)
    time.sleep(0.3)
    VERITAS.emit("OnBattleBegin", {"max_waves": 2, "max_cycles": 30, "stage_id": 20101})
    VERITAS.emit("OnSetBattleLineup", {"avatars": [
        {"id": 1308, "name": "花火"}, {"id": 1003, "name": "景元"},
        {"id": 1301, "name": "刃"}, {"id": 1224, "name": "藿藿"}]})
    for uid in (1308, 1003, 1301, 1224):
        stat(uid, "MaxHP", 6000)
        stat(uid, "CurrentHP", 6000)
    time.sleep(0.3)
    logs = DOCK.logs()
    assert any("阵容更新" in (t or "") for _, t in logs), f"缺少阵容日志: {logs}"
    print("[PASS] 连接/阵容事件处理")

    # 2. 第一次挨打: -500 -> bonus 2.67 + overlap 0.89 -> str 23
    stat(1308, "CurrentHP", 5500)
    wait_for(lambda: DOCK.set_strengths(), desc="第一次 set_strength")
    time.sleep(0.2)
    strengths = DOCK.set_strengths()
    assert strengths[-1] == ("All", 23), f"期望 All 23, 实际 {strengths}"
    wf = DOCK.waveforms()
    assert wf and wf[-1][1] == [[20, 100], [10, 80], [10, 10]], f"波形错误: {wf[-1]}"
    print(f"[PASS] 首次挨打 -> {strengths[-1]}  波形 {wf[-1][1]}")

    # 3. 连续挨打: -100 -> 叠加生效, 强度 23 不变(不重发) 但波形重发
    n_set = len(DOCK.set_strengths())
    n_wf = len(DOCK.waveforms())
    stat(1308, "CurrentHP", 5400)
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="第二次波形")
    time.sleep(0.2)
    assert len(DOCK.set_strengths()) == n_set, (
        f"强度不应变化（同为23）: {DOCK.set_strengths()} "
        f"sustain={core._low_sustain_active} cfg={core._cfg('low_sustain_enabled')}")
    print("[PASS] 连续挨打叠加（强度不变时跳过重发）")

    # 4. 大掉血: -2500 -> r=0.833 重伤段 -> bonus 8.33
    stat(1308, "CurrentHP", 2900)
    wait_for(lambda: len(DOCK.set_strengths()) > n_set, desc="重伤加成")
    time.sleep(0.2)
    s = DOCK.set_strengths()[-1]
    assert s == ("All", 31), f"期望 All 31 (20+8.67重伤+2.62叠加), 实际 {s}"
    print(f"[PASS] 重伤加成 -> {s}")

    # 5. 敌人掉血不触发
    n_wf = len(DOCK.waveforms())
    VERITAS.emit("OnStatChange", {"entity": {"uid": 9999, "team": "Enemy"},
                                  "property": {"type": "CurrentHP", "value": 100}})
    time.sleep(0.3)
    assert len(DOCK.waveforms()) == n_wf, "敌人掉血不应触发"
    print("[PASS] 敌方事件过滤")

    # 6. 倒地重罚
    VERITAS.emit("OnEntityDefeated", {"killer": {"uid": 9999, "team": "Enemy"},
                                      "entity_defeated": {"uid": 1308, "team": P}})
    wait_for(lambda: len(DOCK.set_strengths()) > 0 and DOCK.set_strengths()[-1][1] > 31,
             desc="倒地重罚")
    time.sleep(0.2)
    s = DOCK.set_strengths()[-1]
    assert s == ("All", 37), f"期望 All 37 (20+2.62叠加+15), 实际 {s}"
    assert "倒地" in (DOCK.logs()[-1][1] or ""), DOCK.logs()[-1]
    print(f"[PASS] 倒地重罚 -> {s}")

    # 7. 战斗结束 -> clear -> 0；脱战掉血照常触发
    # （仅战斗中惩罚选项已移除：veritas 只在战斗内读数据，脱战过滤无意义）
    VERITAS.emit("OnBattleEnd", {"total_damage": 123456})
    wait_for(lambda: DOCK.set_strengths() and DOCK.set_strengths()[-1] == ("All", 0),
             desc="战斗结束清零")
    n = len(DOCK.set_strengths())
    stat(1308, "CurrentHP", 2500)
    time.sleep(0.3)
    assert len(DOCK.set_strengths()) > n, "脱战掉血应照常触发"
    print("[PASS] 战斗结束清零 + 脱战掉血照常触发")

    # 8. 实时状态接口（配置页面轮询的 get_status / str_up 动作）
    import start as start_mod
    start_mod._plugin = core
    st = json.loads(start_mod.get_status())
    assert st.get("running") is True, st
    assert st["battle_active"] is False
    assert len(st["avatars"]) == 4, st["avatars"]
    by_name = {a["name"]: a for a in st["avatars"]}
    assert by_name["花火"]["hp"] == 2500 and by_name["花火"]["max_hp"] == 6000, by_name
    assert st["hit_count"] >= 3 and st["total_lost"] > 0
    # 仅战斗中惩罚已移除：脱战掉血照常触发，强度不再是 0
    assert st["strength_a"] > 0, st
    assert st["last_hit"] and st["last_hit"]["name"] == "花火", st["last_hit"]
    r = json.loads(start_mod.str_clear())
    wait_for(lambda: DOCK.set_strengths() and DOCK.set_strengths()[-1] == ("All", 0),
             desc="str_clear 先归零")
    r = json.loads(start_mod.str_up())
    wait_for(lambda: DOCK.set_strengths() and DOCK.set_strengths()[-1] == ("All", 1),
             desc="str_up 手动调节")
    st2 = json.loads(start_mod.get_status())
    assert st2["strength_a"] == 1 and st2["strength_b"] == 1, st2
    r = json.loads(start_mod.str_clear())
    wait_for(lambda: DOCK.set_strengths()[-1] == ("All", 0), desc="str_clear 清零")
    print("[PASS] 实时状态快照 + 手动强度调节/清零")

    # 9. 掉血聚合（还原挨打就电逐帧采样语义）：0.1s 窗口内两笔合并为一次触发
    VERITAS.emit("OnBattleBegin", {"max_waves": 3, "stage_id": 20201})
    time.sleep(0.2)
    n_wf = len(DOCK.waveforms())
    n_set = len(DOCK.set_strengths())
    stat(1003, "CurrentHP", 5900)   # 景元 -100
    stat(1003, "CurrentHP", 5800)   # 景元 -100（窗口内立即到达 -> 合并为 -200）
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="聚合触发")
    time.sleep(0.3)
    assert len(DOCK.waveforms()) == n_wf + 1, "两笔掉血应合并为一次波形"
    assert len(DOCK.set_strengths()) == n_set + 1, "两笔掉血应合并为一次强度设置"
    s = DOCK.set_strengths()[-1]
    assert s == ("All", 22), f"聚合后期望 All 22 (20+2.0加成+0.89叠加), 实际 {s}"
    print(f"[PASS] 掉血聚合 -> {s}（两笔合一）")

    # 10. 盾量惩罚（移植挨打就电盾量条）+ 有盾阻止血量惩罚
    stat(1308, "MaxShield", 2000)
    stat(1308, "Shield", 2000)      # 上盾：只记录不触发
    time.sleep(0.3)
    n_wf = len(DOCK.waveforms())
    stat(1308, "Shield", 1000)      # 盾被打 -1000
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="盾量惩罚")
    time.sleep(0.3)
    # 挨打就电原版语义：叠加值只在窗口期内计入（此处窗口已过期，仅加成生效）
    # A/B 基础强度不同(18/17) -> 分通道设置（挨打就电同款双通道手感）
    # 挨打就电双通道手感：A(18+4.33=22) 恰与当前一致 -> 跳过；B(17+4.33=21) 单独更新
    s_last = DOCK.set_strengths()[-1]
    assert s_last == ("B", 21), f"盾量惩罚期望仅 B 更新到 21, 实际 {s_last}"
    st = json.loads(start_mod.get_status())
    assert st["strength_a"] == 22 and st["strength_b"] == 21, st
    wf = DOCK.waveforms()[-1][1]
    assert wf == [[10, 80], [10, 80], [10, 40], [10, 10]], f"盾量波形错误: {wf}"
    print(f"[PASS] 盾量惩罚 -> A22/B21（仅 B 重发） 波形 {wf}")

    n_wf = len(DOCK.waveforms())
    stat(1308, "CurrentHP", 2400)   # 有盾时掉血
    time.sleep(0.6)
    assert len(DOCK.waveforms()) == n_wf, "有盾时血量掉血不应触发"
    print("[PASS] 有盾时阻止血量惩罚")

    n_set = len(DOCK.set_strengths())
    stat(1308, "Shield", 0)         # 盾破 -1000（窗口外，仅加成）
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="盾破惩罚")
    time.sleep(0.3)
    assert len(DOCK.set_strengths()) == n_set, "强度已达标不应重发 set_strength"
    st = json.loads(start_mod.get_status())
    assert st["strength_a"] == 22 and st["strength_b"] == 21, st
    by_name = {a["name"]: a for a in st["avatars"]}
    assert by_name["花火"]["shield"] == 0 and by_name["花火"]["max_shield"] == 2000
    assert st["shield_hit_count"] == 2, st
    assert st["modules"]["battle"] is True and st["modules"]["cw"] is True, st["modules"]
    print("[PASS] 盾破惩罚 -> 波形触发（强度保持 A22/B21 不重发） + 快照盾量/模块字段")

    # 11. 模块开关：常规战斗模块停用 -> 掉血不触发；恢复 -> 正常
    TEST_CONFIG["plugins"]["module_battle_enabled"] = False
    time.sleep(0.2)
    n_wf = len(DOCK.waveforms())
    stat(1003, "CurrentHP", 5700)   # 景元 -100
    time.sleep(0.4)
    assert len(DOCK.waveforms()) == n_wf, "战斗模块停用后不应触发"
    TEST_CONFIG["plugins"]["module_battle_enabled"] = True
    n_set = len(DOCK.set_strengths())
    stat(1003, "CurrentHP", 5600)   # 恢复后 -100（加成 2.0 -> 目标 A22/B21，与当前一致）
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="模块恢复触发")
    time.sleep(0.3)
    assert len(DOCK.set_strengths()) == n_set, "强度已达标不应重发"
    st = json.loads(start_mod.get_status())
    assert st["strength_a"] == 22 and st["strength_b"] == 21, st
    print("[PASS] 模块开关控制 -> 波形恢复（强度保持 A22/B21）")

    cw = next(m for m in core.modules if m.name == "cw")

    # 12. 货币战争：扣血飘字（delta 模式）-> 触发 + 持续电击 -> 结束清波
    time.sleep(0.5)  # 确保叠加窗口过期
    n_wf = len(DOCK.waveforms())
    n_clear = len(DOCK.clears())    # 触发前基准（新逻辑：发送前 clear 也计入）
    cw._on_ocr_delta(10, False)     # 识别到 -10
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="货币战争触发")
    s = DOCK.set_strengths()[-1]
    # 新公式：基础强度 25 x 当前血量系数(血量未知=1.0) + 多次掉血叠加 0.875
    #          = 25.875 -> 25（不再有局内的受伤加成）
    assert s == ("All", 25), f"货币战争期望 All 25 (25x1.0+0.875), 实际 {s}"
    # 局内的受伤加成系数不得影响货币战争
    n_set = len(DOCK.set_strengths())
    TEST_CONFIG["plugins"]["damage_max_bonus"] = 999
    TEST_CONFIG["plugins"]["damage_mid_value"] = 1
    cw.engine.overlap_cw.reset()
    cw.engine.current_strength_a = None
    cw.engine.current_strength_b = None
    time.sleep(0.3)                 # 让 cw 触发间隔(0.1s)过去
    cw._on_ocr_delta(11, False)
    wait_for(lambda: len(DOCK.set_strengths()) > n_set, desc="货币战争第二刀")
    s2 = DOCK.set_strengths()[-1]
    assert s2 == ("All", 25), f"局内伤加不应影响货币战争，实际 {s2}"
    TEST_CONFIG["plugins"]["damage_max_bonus"] = 10
    TEST_CONFIG["plugins"]["damage_mid_value"] = 3000
    wait_for(lambda: len(DOCK.clears()) > n_clear and not cw.engine.sustain_active(),
             desc="持续电击结束清波")
    time.sleep(0.3)
    assert not cw.engine.sustain_active(), "持续电击应已结束"
    print(f"[PASS] 货币战争扣血电击 -> {s} + 不受局内伤加影响({s2}) + 持续电击后清波")

    # 13. 飘字去重：同一数值窗口内不重复触发；获得量(+)忽略
    n_wf = len(DOCK.waveforms())
    cw._on_ocr_delta(10, False)     # 去重窗口内
    cw._on_ocr_delta(5, True)       # 获得量
    time.sleep(0.4)
    assert len(DOCK.waveforms()) == n_wf, "去重/获得量不应触发"
    print("[PASS] 飘字去重 + 获得量忽略")

    # 14. 数值模式（pool）：差值触发
    #     当前血量 88 -> 系数 1.06 -> 25x1.06 = 26.5；叠加窗口已过，不计入 -> 26
    n_wf = len(DOCK.waveforms())
    n_set = len(DOCK.set_strengths())
    n_clear2 = len(DOCK.clears())   # 触发前基准
    cw._on_pool_value(100)          # 基准
    cw._on_pool_value(88)           # -12
    wait_for(lambda: len(DOCK.waveforms()) > n_wf, desc="pool 差值触发")
    time.sleep(0.3)
    assert len(DOCK.set_strengths()) == n_set + 1, "血量系数把强度从 25 抬到 26，应重发一次"
    s_pool = DOCK.set_strengths()[-1]
    assert s_pool == ("All", 26), f"pool 期望 All 26，实际 {s_pool}"
    st = json.loads(start_mod.get_status())
    assert st["strength_a"] == 26 and st["strength_b"] == 26, st
    assert st["cw"]["hp_factor"] == 1.06, st["cw"]
    wait_for(lambda: len(DOCK.clears()) > n_clear2 and not cw.engine.sustain_active(),
             desc="第二次持续电击清波")
    print(f"[PASS] 总血量数值模式 -> {s_pool}（血量系数 1.06）+ 持续电击清波")

    # ================= 强度公式（ratio 模式） =================
    # 15. ratio 基础：掉血占比 8.33% -> 加成 2+sqrt(6.33)=4.52；窗口过期无叠加 -> 24
    TEST_CONFIG["plugins"]["dmg_mode"] = "ratio"
    TEST_CONFIG["plugins"]["low_sustain_enabled"] = False
    core.recent_hits.clear()        # 排除频率压缩/多角色的历史干扰
    time.sleep(2.2)                 # 等 overlap 窗口完全过期
    battle = next(m for m in core.modules if m.name == "battle")
    battle_triggers_before = len(DOCK.waveforms())
    stat(1308, "CurrentHP", 6000)   # 先抬回基准（前面测试把血打低了）
    time.sleep(0.4)
    stat(1308, "CurrentHP", 5500)   # -500 / 6000 = 8.33%
    wait_for(lambda: len(DOCK.waveforms()) > battle_triggers_before, desc="ratio 触发")
    time.sleep(0.2)
    s = DOCK.set_strengths()[-1]
    assert s == ("All", 24), f"ratio 期望 All 24, 实际 {s}"
    print(f"[PASS] ratio 公式基础 -> {s}（占比 8.33% -> 加成 4.52，无叠加）")

    # 16. 阈值跳过：掉血占比 0.83% < 2% 不触发
    n_set = len(DOCK.set_strengths())
    stat(1308, "CurrentHP", 5450)   # -50 / 6000 = 0.83%
    time.sleep(0.5)
    assert len(DOCK.set_strengths()) == n_set, "低于强度阈值不应触发"
    print("[PASS] 强度阈值 -> 刮痧跳过")

    # 17. 残血持续电：单角色残血 -> 持续电强度 12
    n_clear3 = len(DOCK.clears())   # 残电前基准（前面测试的发送前 clear 已计入）
    TEST_CONFIG["plugins"]["low_sustain_enabled"] = True
    TEST_CONFIG["plugins"]["low_sustain_strength"] = 12
    TEST_CONFIG["plugins"]["low_sustain_step"] = 4
    stat(1308, "CurrentHP", 1500)   # 25% < 30%
    wait_for(lambda: any(v == 12 for _, v in DOCK.set_strengths()),
             timeout=6, desc="残血持续电强度")
    print("[PASS] 残血持续电 -> 单角色强度 12")

    # 18. 残血人数加成：第二角色也残血 -> 12+4=16
    stat(1003, "MaxHP", 6000)
    stat(1003, "CurrentHP", 1000)   # 16.7% < 30%
    wait_for(lambda: any(v == 16 for _, v in DOCK.set_strengths()),
             timeout=6, desc="残血人数强度")
    print("[PASS] 残血人数加成 -> 双残血强度 16")

    # 19. 恢复：脱离残血 -> 清波结束
    stat(1308, "CurrentHP", 5400)
    stat(1003, "CurrentHP", 5500)
    wait_for(lambda: not core._low_sustain_active, timeout=8, desc="残血持续电结束")
    wait_for(lambda: len(DOCK.clears()) > n_clear3, timeout=4, desc="残电结束清波")
    TEST_CONFIG["plugins"]["low_sustain_enabled"] = False
    print("[PASS] 残血恢复 -> 持续电结束清波")

    # ================= 蓝牙直连分支（/coyote） =================
    # 20. 蓝牙模式：强度绝对设置 + 波形循环播放/停止
    core._bt_mode = True          # 强制蓝牙分支
    with core._sustain_lock:      # 清掉前面持续电测试遗留的时间窗
        core._sustain_until = 0.0
    n0 = len(DOCK.actions)
    core.set_strength("All", 20)
    sets = [a for p, a in DOCK.actions[n0:]
            if p == "/coyote" and a.get("action") == "strength"
            and a.get("option") == "set"]
    # "All" 必须拆成 A/B 两条（server 蓝牙接口不认 All）
    assert [(a.get("channel"), a.get("strength")) for a in sets] == \
        [("A", 20), ("B", 20)], f"蓝牙强度指令异常: {sets}"
    n1 = len(DOCK.actions)
    waves_before = len(DOCK.waveforms())
    core.send_pulse([["100", "100"], ["100", "60"]])
    coy = [a for p, a in DOCK.actions[n1:] if p == "/coyote"]
    kinds = [a.get("action") for a in coy]
    assert "start_punish" not in kinds, f"蓝牙不应使用 start_punish: {kinds}"
    assert len(DOCK.waveforms()) > waves_before, "蓝牙应经 websocket 装波形"
    time.sleep(0.8)               # 等一次性停止定时器（下限 0.5s + 余量）
    zeros = [a for p, a in DOCK.actions[n1:]
             if p == "/coyote" and a.get("action") == "strength"
             and a.get("option") == "set" and int(a.get("strength") or 0) == 0]
    assert zeros, "蓝牙单次电击结束应强度归零"
    core._bt_mode = False         # 还原
    print("[PASS] 蓝牙直连 -> strength set / set_waveform+start_punish / 自动停止")

    # 21. 红字严格颜色匹配：只数目标色（曼哈顿 <= tol*3）；
    #     旧宽松色相规则对 (255,60,60) 也算红，严格模式近似度10正确排除
    from total_hp_ocr import count_color_pixels, count_red_pixels, parse_colors
    tgt = parse_colors("#EE7A74")
    px_t = bytes((0x74, 0x7A, 0xEE, 255))    # BGRA 目标色 (238,122,116)
    px_near = bytes((60, 60, 255, 255))      # (255,60,60) 距目标 135
    px_white = bytes((255, 255, 255, 255))
    px_black = bytes((0, 0, 0, 255))
    buf = px_t * 5 + px_near * 5 + px_white * 3 + px_black * 3   # 16 像素
    assert count_color_pixels(buf, 4, 4, tgt, 10) == 5, "近似度10应只数目标色"
    assert count_color_pixels(buf, 4, 4, tgt, 50) == 10, "近似度50应圈入近色"
    assert count_color_pixels(buf, 4, 4, [], 10) == 0, "无目标色应返回0"
    assert count_red_pixels(buf, 4, 4) == 10, "宽松规则对照: 5目标+5近红"
    print("[PASS] 红字严格颜色匹配 -> 近似度10只认目标色，50圈入近色")

    core.stop()
    for srv in servers:
        srv.shutdown()

    print("\n全部测试通过 ✓")


if __name__ == "__main__":
    main()
