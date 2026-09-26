"""
hud.py - 游戏内悬浮窗（完整吸取 挨打就电 overlay.py 的成熟模型）

铁律（挨打就电 overlay.py 原注释）："stop 时隐藏不销毁，避免 tkinter
后台线程重复创建问题" —— 全生命周期只有一个常驻后台线程 + 一个永不
销毁的 Tk root；可见性只做 withdraw/deiconify 切换，配置热切换无线程 churn。

数据来自 provider()（引擎 get_status 的快照 dict），每 0.5s 刷新。
"""
import json
import os
import tempfile
import threading
import time


class HudWindow:
    def __init__(self, provider, get_flag):
        """provider() -> 状态快照 dict；get_flag() -> bool（hud_enabled 实时读取）"""
        self._provider = provider
        self._get_flag = get_flag
        self.root = None
        self._visible = True
        self._stopped = threading.Event()

    def create(self, on_log=None):
        import tkinter as tk
        self._log = on_log or (lambda msg: None)
        try:
            import screen_capture as _sc
            _sc.ensure_dpi_aware()
        except Exception:
            pass
        root = tk.Tk()
        self.root = root
        root.title("hsr-hud")
        root.geometry("+10+10")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", "black")
        root.configure(bg="black")

        self._top = tk.Label(root, text="崩铁·挨打就电", bg="black", fg="#eaf2ff",
                             font=("Microsoft YaHei", 12, "bold"))
        self._top.pack(anchor="w")
        self._sub = tk.Label(root, text="", bg="black", fg="#9fb6d4",
                             font=("Microsoft YaHei", 10))
        self._sub.pack(anchor="w")

        self._log("HUD 悬浮窗已创建")
        self._tick()
        try:
            root.mainloop()
        except Exception as e:
            self._log("HUD 主循环异常: {}".format(e))

    def _tick(self):
        root = self.root
        if root is None:
            return
        # 铁律（挨打就电 overlay.py）：stop 只隐藏、永不销毁——
        # 销毁后在新线程重建 root 是 tkinter 线程亲和崩溃的根源。
        if self._stopped.is_set():
            if self._visible:
                try:
                    root.withdraw()
                    self._visible = False
                except Exception:
                    pass
        else:
            want = bool(self._get_flag())
            if want and not self._visible:
                try:
                    root.deiconify()
                    self._visible = True
                except Exception:
                    pass
            elif (not want) and self._visible:
                try:
                    root.withdraw()
                    self._visible = False
                except Exception:
                    pass
            if want and self._visible:
                try:
                    self._render(self._provider() or {})
                except Exception:
                    pass
        root.after(500, self._tick)

    def resume(self):
        """插件重启时复用常驻 root：只解除隐藏标记，绝不重建线程/root"""
        self._stopped.clear()

    def _read_v4(self):
        """客户端直连模式的状态 sidecar（v4_state.json，3s 内有效）。
        惩罚姬原生模式没有这个文件 -> 返回 None，行为不变。"""
        try:
            p = os.path.join(tempfile.gettempdir(), "hsr_dglab_ipc", "v4_state.json")
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - float(d.get("ts") or 0) < 3:
                return d
        except Exception:
            pass
        return None

    def _render(self, d):
        if d.get("running") is not True:
            self._top.config(text="崩铁·挨打就电（等待数据…）")
            self._sub.config(text="启动插件后显示实时状态")
            return
        paused = d.get("paused") is True
        tag = " ⏸暂停" if paused else ""
        hp = "-"
        avatars = d.get("avatars") or []
        if avatars:
            a = avatars[0]
            name = a.get("name", "?")
            if a.get("hp") is not None and a.get("max_hp"):
                hp = "{} {}/{}".format(name, int(round(a["hp"])), int(round(a["max_hp"])))
            elif a.get("hp") is not None:
                hp = "{} {}".format(name, int(round(a["hp"])))
        sa, sb = d.get("strength_a"), d.get("strength_b")
        ovl = float(d.get("overlap") or 0)
        v4 = self._read_v4()
        shock = ""
        if v4 and v4.get("shocking"):
            shock = "⚡电击中 | "
            va, vb = v4.get("strength_a"), v4.get("strength_b")
            if isinstance(va, (int, float)):
                sa = va
            if isinstance(vb, (int, float)):
                sb = vb
        self._top.config(text="{}{}HP {} | 强度 {}/{} | 叠加 +{:.1f}".format(
            shock, tag, hp,
            "-" if sa is None else int(round(sa)),
            "-" if sb is None else int(round(sb)), ovl))
        cw = d.get("cw") or {}
        total = cw.get("total_hp")
        lost = float(cw.get("total_lost") or 0)
        hits = int(cw.get("total_hits") or 0)
        sustain = " ⚡持续中" if cw.get("sustain_active") else ""
        last = d.get("last_hit")
        last_txt = ""
        if last:
            ago = max(0, int(round(time.time() - float(last.get("ts") or time.time()))))
            last_txt = " | 最近挨打 {} -{}（{}s前）".format(
                last.get("name", "?"), last.get("lost"), ago)
        lat_txt = ""
        lat_ts = float(cw.get("lat_ts") or 0)
        if lat_ts and time.time() - lat_ts < 5:
            ocr_lat = int(cw.get("lat_ocr_ms") or 0)
            plus_lat = int(cw.get("lat_plus_ms") or 0)
            if ocr_lat:
                lat_txt += " | OCR {}ms".format(ocr_lat)
            if plus_lat:
                lat_txt += " · 加号 {}ms".format(plus_lat)
        # 调试行：加号闸状态 / 当前红像素数 / 总血量（未读到显示 N/A）
        # 闸门用 gate_ok（开闸且未过期）；老版本没有该字段时退回 plus_gate
        dbg = ""
        if cw:
            gate = cw.get("gate_ok")
            if gate is None:
                gate = cw.get("plus_gate")
            # 取样：加号点这一轮实际取到的 RGB（一眼看出为什么 true）
            smp = cw.get("plus_samples") or []
            smp_txt = ""
            if smp:
                smp_txt = "  取样:" + ",".join(
                    "#{:02X}{:02X}{:02X}".format(
                        int(s[2]), int(s[3]), int(s[4]))
                    for s in smp if len(s) >= 5)
            dbg = " | 加号:{}  红:{}  血量:{}{}".format(
                "true" if gate else "false",
                int(cw.get("red_count") or 0),
                "N/A" if total is None else int(round(total)),
                smp_txt)
        self._sub.config(text="[CW] 总血 {} | 掉血 {:.1f} | 打击 {}{}{}{}{}".format(
            "-" if total is None else int(round(total)), lost, hits,
            sustain, last_txt, lat_txt, dbg))

    def stop(self):
        self._stopped.set()
