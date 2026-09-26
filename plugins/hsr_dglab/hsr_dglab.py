"""
hsr_dglab.py - 崩铁 · 挨打就电 引擎（多模块架构）

引擎只负责共享设施：
  - 标准事件状态（阵容/血量/盾量/战斗开关）
  - 掉血聚合（还原挨打就电逐帧采样）
  - 受伤加成公式 + 连续挨打叠加（移植自挨打就电）
  - 触发服务（强度+波形）/ 持续电击服务
  - 按模块的数据输入选择（battle_data_source 等）

惩罚策略在 modules/ 下：
  battle  常规战斗（角色血量+盾量+倒地）
  cw      货币战争（总血量电击+持续电击）
"""
import collections
import os
import sys
import threading
import time

# server 的插件导入机制在启动阶段会把插件目录临时加入 sys.path，
# 运行阶段（start 线程）可能已移除；本地包（modules/events/...）必须始终可导入。
# 仅在导入期间临时加路径并在导入完成后还原，避免与其他插件的同名模块冲突
#（如挨打就电也有 capture.py）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_path_added = _HERE not in sys.path
if _path_added:
    sys.path.insert(0, _HERE)
try:
    import events as E
    from hit_logic import (DamageDetector, HitAggregator, OverlapProcessor,
                           convert_pulse_data, get_pulse_duration)
    from source_veritas import VeritasSource
    from source_ocr import OcrSource
    from modules import create_modules
finally:
    if _path_added and _HERE in sys.path:
        sys.path.remove(_HERE)

DEFAULT_PULSE = ["6400000064000000"]   # [频率100, 强度%100]：低频(<30)郊狼几乎无输出


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "是", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return default


class HsrDGLab:
    def __init__(self, app):
        self.app = app
        self.server = app.server
        self.waveform = app.waveform or {}
        self.running = False
        self._paused = False   # 手柄/页面暂停：暂停期间忽略所有惩罚输出
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._start_ts = time.time()

        # ---- 战斗状态 ----
        self.battle_active = False
        self.avatars = {}        # uid(int) -> name
        self.hp = {}             # uid -> 当前血量
        self.max_hp = {}         # uid -> 最大血量
        self.shield = {}         # uid -> 当前护盾
        self.max_shield = {}     # uid -> 最大护盾
        self.total_lost = 0.0
        self.hit_count = 0
        self.shield_hit_count = 0
        self.knock_count = 0
        self.current_cycle = 0          # 当前战斗轮次（OnTurnEnd.turn_info.cycle）
        self.recent_hits = collections.deque(maxlen=64)   # (ts, uid) 多角色判定窗口
        self._low_sustain_active = False
        self._lowhp_stop = threading.Event()
        self._lowhp_thread = None
        self._bt_mode = None            # None=未探测 True=蓝牙直连 False=App 中继

        # ---- 惩罚设施（移植自挨打就电）----
        # 局内（角色战斗）一套
        self.overlap = OverlapProcessor(self._cfg)
        self.detector = DamageDetector(self._cfg)
        # 货币战争一套：参数走 cw_ 前缀、叠加状态独立，互不借用
        # （加成只有「当前血量系数 + 多次掉血叠加」两项，不用局内的受伤加成）
        self.overlap_cw = OverlapProcessor(self._cfg, prefix="cw_")
        self._last_trigger_ts_cw = 0.0
        self.aggregator = HitAggregator(self._cfg)
        self.current_strength_a = None
        self.current_strength_b = None
        self._last_trigger_ts = 0.0
        self._last_heartbeat_warn_ts = 0.0
        self._no_veritas_warned = False
        self._last_hit = None

        # ---- 持续电击状态 ----
        self._sustain_lock = threading.RLock()
        self._sustain_gen = 0
        self._sustain_until = 0.0
        self._sustain_wave_key = None
        self._sustain_min = 0
        self._sustain_stop_after = True
        self._sustain_thread = None

        # ---- 惩罚模块 ----
        self.modules = create_modules(self)
        self._module_map = {m.name: m for m in self.modules}
        self._lowhp_thread = threading.Thread(target=self._lowhp_loop,
                                              name="lowhp", daemon=True)

        # ---- 数据源（按模块的数据输入需求构建） ----
        self.sources = self._build_sources()
        self._loop_thread = None

    # ================= 配置 =================

    def _cfg(self, key, default=None):
        return self.app.config.get("plugins", {}).get(key, default)

    def _f(self, key, default=0.0):
        return _to_float(self._cfg(key, default), default)

    def _i(self, key, default=0):
        return _to_int(self._cfg(key, default), default)

    def _b(self, key, default=False):
        return _to_bool(self._cfg(key, default), default)

    def _s(self, key, default=""):
        v = self._cfg(key, default)
        return default if v is None else str(v)

    def _build_sources(self):
        """按各模块的数据输入选择构建数据源

        battle_data_source: 战斗模块输入（veritas / ocr / both）
        cw_total_source:    货币战争总血量输入（ocr）
        cw_probe:           探测模式需要 veritas 事件流
        """
        battle_sel = str(self._cfg("battle_data_source", "veritas") or "veritas").lower()
        need_veritas = (battle_sel in ("veritas", "both")
                        or self._b("cw_probe", False))
        need_ocr = battle_sel in ("ocr", "both")

        sources = []
        if need_veritas:
            sources.append(VeritasSource(
                lambda e, p: self.emit(e, p, "veritas"), self._cfg, self._log))
        if need_ocr:
            sources.append(OcrSource(
                lambda e, p: self.emit(e, p, "ocr"), self._cfg, self._log))
            self._log("info", "战斗模块数据输入包含 OCR：角色血量 OCR 路线尚未实现，"
                              "当前仅 veritas 提供角色数据")
        return sources

    def _get_veritas_source(self):
        for src in self.sources:
            if getattr(src, "name", "") == "veritas":
                return src
        return None

    # ================= 实时状态 / 手动控制 =================

    def snapshot(self):
        """当前状态快照（线程安全）"""
        with self._lock:
            avatars = []
            for uid, name in self.avatars.items():
                hp = self.hp.get(uid)
                mx = self.max_hp.get(uid)
                pct = None
                if hp is not None and mx:
                    try:
                        pct = round(hp / mx * 100.0, 1)
                    except ZeroDivisionError:
                        pct = None
                sh = self.shield.get(uid)
                smx = self.max_shield.get(uid, 0)
                avatars.append({"id": uid, "name": name,
                                "hp": hp, "max_hp": mx, "pct": pct,
                                "shield": sh, "max_shield": smx})
            src = self._get_veritas_source()
            version = None
            if src and src.sio:
                version = src.sio.version
            cw = self._module_map.get("cw")
            return {
                "connected": self._source_connected(),
                "version": version,
                "battle_active": self.battle_active,
                "paused": self._paused,
                "data_source": str(self._cfg("battle_data_source", "veritas")),
                "modules": {m.name: m.enabled for m in self.modules},
                "cw": cw.snapshot() if cw else None,
                "avatars": avatars,
                "total_lost": round(self.total_lost, 1),
                "hit_count": self.hit_count,
                "shield_hit_count": self.shield_hit_count,
                "knock_count": self.knock_count,
                "strength_a": self.current_strength_a,
                "strength_b": self.current_strength_b,
                "overlap": round(self.overlap.accumulated, 1),
                "overlap_cw": round(self.overlap_cw.accumulated, 1),
                "last_hit": self._last_hit,
                "probe_enabled": self._b("cw_probe", False),
                "probe_events": list(src.probe_events) if src else [],
            }

    def _source_connected(self):
        src = self._get_veritas_source()
        if src and src.sio:
            if not src.sio.connected:
                return False
            # 半开连接防护：游戏关闭后 TCP 未必立刻断，90 秒无数据视为离线
            if src.sio.last_event_ts > 0 and time.time() - src.sio.last_event_ts > 90:
                return False
            return True
        return False

    def adjust_strength(self, delta):
        """手动调节强度（±1），返回目标值"""
        with self._lock:
            cur = self.current_strength_a
            if cur is None:
                cur = self._i("strength_a", 20)
            overlap_max = max(int(self._f("overlap_strength_max", 200)),
                              int(self._f("cw_overlap_strength_max", 200)))
            target = max(0, min(overlap_max, int(cur) + int(delta)))
            self.set_strength("All", target)
            self.current_strength_a = target
            self.current_strength_b = target
            return target

    def clear_strength(self):
        """强度清零并清空叠加（局内 + 货币战争两套）"""
        with self._lock:
            self.set_strength("All", 0)
            self.current_strength_a = 0
            self.current_strength_b = 0
            self.overlap.reset()
            self.overlap_cw.reset()

    def _log(self, level, msg):
        try:
            self.server.log(level, msg)
        except Exception:
            print(f"[{level}] {msg}")

    # ================= 生命周期 =================

    def start(self):
        self.running = True
        self.stop_event.clear()
        self._start_ts = time.time()
        for src in self.sources:
            try:
                src.start()
            except Exception as e:
                self._log("error", f"数据源 {getattr(src, 'name', '?')} 启动失败: {e}")
        for m in self.modules:
            try:
                m.start()
            except Exception as e:
                self._log("error", f"模块 {m.name} 启动失败: {e}")
        self._loop_thread = threading.Thread(target=self._loop, daemon=True)
        self._loop_thread.start()
        self._log("success", "崩铁·挨打就电 已启动（数据源: "
                  + ", ".join(getattr(s, "name", "?") for s in self.sources)
                  + " | 模块: " + ", ".join(m.name for m in self.modules) + "）")

    def stop(self):
        self.running = False
        self.stop_event.set()
        for m in self.modules:
            try:
                m.stop()
            except Exception:
                pass
        for src in self.sources:
            try:
                src.stop()
            except Exception:
                pass
        self._log("info", "崩铁·挨打就电 已停止")

    # ================= 标准事件入口 =================

    def emit(self, event, payload=None, source_name=None):
        """数据源统一入口（可从任意线程调用）；source_name 标记事件来源"""
        if not self.running:
            return
        try:
            with self._lock:
                # 先冲刷聚合窗口已到期的掉血/掉盾（还原挨打就电逐帧采样）
                self._flush_aggregated(time.time())
                p = payload
                if source_name and isinstance(payload, dict):
                    p = dict(payload)
                    p["_src"] = source_name
                self._handle(event, p)
                for m in self.modules:
                    try:
                        m.on_event(event, p)
                    except Exception as e:
                        self._log("error", f"模块 {m.name} 事件处理异常: {e}")
        except Exception as e:
            self._log("error", f"事件处理异常 {event}: {e}")

    def _flush_aggregated(self, now):
        """处理聚合窗口到期的掉血/掉盾 -> 路由到模块"""
        for key, amount in self.aggregator.flush(now):
            self._route_settled(key, amount)

    def _route_settled(self, key, amount):
        kind, uid, src = key
        for m in self.modules:
            if not m.enabled:
                continue
            if m.wants_drop(kind):
                m.on_settled_drop(kind, uid, amount, src)
                return
        self._log("debug", f"{kind} 掉血 {amount:.0f} 无模块处理（模块未启用）")

    def _handle(self, event, payload):
        # 收到任何数据都视为连接活跃，允许心跳告警在再次断流时重新触发
        if event != E.EVT_HEARTBEAT:
            self._last_heartbeat_warn_ts = 0.0
        if event == E.EVT_CONNECTED:
            self._no_veritas_warned = False
            version = (payload or {}).get("version", "?")
            self._log("success", f"veritas 已连接 (v{version})")

        elif event == E.EVT_HEARTBEAT:
            self._last_heartbeat_warn_ts = 0.0  # 收到心跳即清除告警状态

        elif event == E.EVT_BATTLE_BEGIN:
            self.battle_active = True
            if self._b("reset_overlap_on_battle", True):
                self.overlap.reset()
            stage = (payload or {}).get("stage_id", "")
            self._log("info", f"进入战斗 (stage={stage})，监测开启")

        elif event == E.EVT_LINEUP:
            avatars = (payload or {}).get("avatars", [])
            self.avatars = {}
            for a in avatars:
                try:
                    self.avatars[int(a.get("id"))] = str(a.get("name") or f"角色{a.get('id')}")
                except (TypeError, ValueError):
                    continue
            self.hp.clear()
            self.max_hp.clear()
            self.total_lost = 0.0
            self.hit_count = 0
            self.knock_count = 0
            self._last_hit = None
            names = "、".join(self.avatars.values()) or "?"
            self._log("info", f"阵容更新（{len(self.avatars)}人）: {names}")

        elif event == E.EVT_HP_CHANGE:
            self._on_hp_change(payload or {})

        elif event == E.EVT_TURN:
            try:
                self.current_cycle = int((payload or {}).get("cycle") or 0)
            except (TypeError, ValueError):
                self.current_cycle = 0

        elif event == E.EVT_BATTLE_END:
            # 先结算聚合窗口内未冲刷的掉血/掉盾（战斗开关仍有效）
            for key, amount in self.aggregator.flush_all():
                self._route_settled(key, amount)
            self.battle_active = False
            self.current_cycle = 0
            self._log(
                "info",
                f"战斗结束 | 挨打 {self.hit_count} 次，累计掉血 {self.total_lost:.0f}，"
                f"倒地 {self.knock_count} 次",
            )
            self.overlap.reset()
            mode = str(self._cfg("battle_end_mode", "none") or "none").lower()
            if mode == "clear":
                self.set_strength("All", 0)
                self.current_strength_a = 0
                self.current_strength_b = 0
                self._log("info", "战斗结束：强度已清零")
            elif mode == "set":
                target = self._i("battle_end_strength", 0)
                self.set_strength("All", target)
                self.current_strength_a = target
                self.current_strength_b = target
                self._log("info", f"战斗结束：强度设置为 {target}")

    # ================= 血量/盾量状态与聚合 =================

    def _on_hp_change(self, payload):
        try:
            uid = int(payload.get("uid"))
        except (TypeError, ValueError):
            return
        src = payload.get("_src") or ""

        if uid not in self.avatars:
            # veritas 中途接入 / OCR 路线未报阵容时动态注册
            self.avatars[uid] = str(payload.get("name") or f"角色{uid}")

        if payload.get("max_hp") is not None:
            self.max_hp[uid] = _to_float(payload["max_hp"], 0)
        if payload.get("max_shield") is not None:
            self.max_shield[uid] = _to_float(payload["max_shield"], 0)
        if payload.get("shield") is not None:
            value = _to_float(payload["shield"], 0)
            old = self.shield.get(uid)
            self.shield[uid] = value
            if old is not None and value < old:
                # 掉盾 -> 进入聚合窗口（等价挨打就电的盾量条检测）
                self.aggregator.add(("shield", uid, src), old - value, time.time())
                return

        if payload.get("hp") is None:
            return
        value = _to_float(payload["hp"], 0)
        old = self.hp.get(uid)
        self.hp[uid] = value

        if old is None:
            self._log("debug", f"{self.avatars[uid]} 血量基准: {value:.0f}")
            return
        if value >= old:
            self._touch_lowhp()
            return

        # 最大生命变化导致的等比压缩（buff 过期/卸下）：非掉血，跳过。
        # 特征：旧血 > 当前上限 且 新血回到满额——满血 9000（上限被 buff）
        # 过期压缩回 6000/6000 会被误判成“挨打 -3000”。
        mx_now = self.max_hp.get(uid, 0)
        if mx_now > 0 and old > mx_now and value >= mx_now * 0.995:
            self._log("debug", "%s 血量随上限压缩 %.0f->%.0f（非掉血，跳过）" % (
                self.avatars[uid], old, value))
            self._touch_lowhp()
            return

        # 掉血 -> 进入聚合窗口（等价挨打就电的逐帧采样语义）
        self.recent_hits.append((time.time(), uid))
        self.aggregator.add(("hp", uid, src), old - value, time.time())
        self._touch_lowhp()

    # ================= 残血持续电 =================

    def _low_hp_count(self):
        """残血角色数：CurrentHP/MaxHP 低于残血阈值"""
        thr = self._f("low_hp_threshold", 30)
        if thr <= 0:
            return 0
        n = 0
        for uid, hp in self.hp.items():
            mx = self.max_hp.get(uid, 0)
            if mx > 0 and hp / mx * 100.0 < thr:
                n += 1
        return n

    def _touch_lowhp(self):
        """血量变化后更新残血持续电状态（线程安全：幂等）"""
        if not (self._b("low_sustain_enabled", True) and self.running):
            return
        n = self._low_hp_count()
        if n > 0 and not self._low_sustain_active:
            self._low_sustain_active = True
            if self._lowhp_thread is None or not self._lowhp_thread.is_alive():
                self._lowhp_stop.clear()
                self._lowhp_thread = threading.Thread(target=self._lowhp_loop,
                                                      name="lowhp", daemon=True)
                self._lowhp_thread.start()
            self._log("warning", f"残血持续电开启：{n} 人残血")
        elif n == 0 and self._low_sustain_active:
            self._low_sustain_active = False
            self._log("info", "残血持续电结束")

    def _lowhp_loop(self):
        """残血期间周期发普通波形；人数越多强度越高；
        无人残血或脱离战斗自然结束（战斗结束后角色往往保持残血，
        不检查战斗状态会导致脱战后被持续电）"""
        base = self._f("low_sustain_strength", 12)
        step = self._f("low_sustain_step", 4)
        while self.running and self._low_sustain_active:
            n = self._low_hp_count()
            if n <= 0 or not self.battle_active:
                if n <= 0:
                    self._low_sustain_active = False
                else:
                    # 脱战但仍残血：静默结束，等下次进战血量变化再触发
                    self._low_sustain_active = False
                    self._log("info", "战斗已结束，残血持续电暂停")
                break
            target = int(base + (n - 1) * step)
            if self.current_strength_a != target:
                # 残血期间强度对齐到残血档：人数越多越高，覆盖连招叠加
                self.set_strength("All", target)
                self.current_strength_a = target
                self.current_strength_b = target
            self.send_pulse(self._get_pulse("hit_pulse"), "All")
            if self._lowhp_stop.wait(2.0):
                break
        if not self._low_sustain_active:
            self.clear_waveform()
            self.set_strength("All", 0)

    # ================= 触发服务（移植自挨打就电） =================

    def toggle_paused(self):
        """暂停/恢复惩罚输出（手柄或页面触发）；暂停时立即清波清强度"""
        self._paused = not self._paused
        if self._paused:
            self.clear_waveform()
            self.set_strength("All", 0)
            self._log("info", "已暂停惩罚输出（手柄/页面切换）")
        else:
            self._log("info", "已恢复惩罚输出（手柄/页面切换）")
        return self._paused

    def set_strength(self, channel, strength):
        """把通道强度调到 target。

        蓝牙直连(/coyote)：coyote_strength(set) 绝对设置，一次到位。
        App 中继(/websocket)：抓包实锤——非零绝对 set 被 APP 拒绝
        (invalid_operate)，add/reduce 假成功不发包；唯一有效的远程强度
        操作是归零(t=7 v=0)。App 模式下强度由用户在手机 APP 手动控制，
        插件只在急停（暂停/脱战/手动清零）时把强度归零。
        """
        target = int(strength)
        if self._paused and target > 0:
            self._log("debug", f"暂停中，忽略 set_strength {channel} -> {target}")
            return
        try:
            if self._bt():
                self._bt_apply_strength(channel, target)
                self._log("debug", f"set_strength(蓝牙) {channel} -> {target}")
            else:
                self._apply_strength(channel, target)
                self._log("debug", f"set_strength {channel} -> {target}")
        except Exception as e:
            self._log("error", f"set_strength 失败: {e}")

    def _bt(self):
        """设备链路探测：True=蓝牙直连(/coyote)，False=App 中继(/websocket)。
        以 server 的 /active 为权威（实测蓝牙模式返回 coyote:"connect"）。"""
        if self._bt_mode is None:
            try:
                resp = self.server.get_active()
                self._bt_mode = isinstance(resp, dict) and \
                    str(resp.get("coyote", "")).lower() == "connect"
            except Exception:
                self._bt_mode = False
            self._log("info", "设备链路: 蓝牙直连(/coyote)" if self._bt_mode
                      else "设备链路: App 中继(/websocket)")
        return self._bt_mode

    def _bt_apply_strength(self, channel, target):
        """蓝牙直连：coyote_strength(set) 绝对设置。

        注意：server 的蓝牙强度接口大小写敏感且只认 "A"/"B"——
        传 "All" 会得到"无效通道：ALL"并被静默拒绝（日志 WARN），
        因此 All 必须拆成 A/B 两次调用，否则归零永远不生效。
        """
        chans = ("A", "B") if channel == "All" else (channel,)
        for ch in chans:
            resp = self.server.coyote_strength(option="set", channel=ch,
                                               strength=target)
            if self._check_dock_error(f"蓝牙强度 {ch} -> {target}", resp):
                self._bt_mode = None   # 链路可能变化，下次操作前重探
                return
        if channel in ("All", "A"):
            self.current_strength_a = target
        if channel in ("All", "B"):
            self.current_strength_b = target

    def _apply_strength(self, channel, target):
        """App 中继强度：实测（探针 0→1→0 回读验证）server 的 add_strength /
        reduce_strength 是假成功——HTTP 回 200"已增加强度"但数值纹丝不动；
        绝对 set_strength 才真正生效。因此主路径用绝对 set，
        仅当其被拒（如设备未就绪时的 invalid_operate）才退回增量兜底。"""

        def _apply_one(ch, cur, tgt):
            tgt = int(tgt)
            if int(cur or 0) == tgt:
                return
            resp = self.server.set_strength(strength=tgt, channel=ch)
            if self._check_dock_error(f"set_strength {ch} -> {tgt}", resp):
                delta = tgt - int(cur or 0)
                try:
                    if delta > 0:
                        resp2 = self.server.add_strength(channel=ch, strength=delta)
                        self._check_dock_error(f"add_strength {ch} +{delta}", resp2)
                    elif delta < 0:
                        resp2 = self.server.reduce_strength(channel=ch, strength=-delta)
                        self._check_dock_error(f"reduce_strength {ch} {-delta}", resp2)
                except Exception as e:
                    self._log("warning", f"增量兜底 {ch} 失败: {e}")

        if channel == "All":
            _apply_one("A", self.current_strength_a, target)
            _apply_one("B", self.current_strength_b, target)
            self.current_strength_a = int(target)
            self.current_strength_b = int(target)
        elif channel == "A":
            _apply_one("A", self.current_strength_a, target)
            self.current_strength_a = int(target)
        elif channel == "B":
            _apply_one("B", self.current_strength_b, target)
            self.current_strength_b = int(target)
        else:
            _apply_one(channel, 0, target)

    def sync_strength_from_dock(self):
        """建立强度基准。

        蓝牙直连：绝对设置可靠，启动归零由插件全面接管。
        App 中继：远程强度不可用（抓包实锤：非零 set 被 APP 拒绝
        invalid_operate、add/reduce 假成功不发包），强度由用户在手机
        APP 手动控制；插件不动强度，只推波形 + 急停归零。
        """
        if self._bt():
            try:
                self._bt_apply_strength("All", 0)   # 拆 A/B，"All"会被 server 拒绝
                self._log("info", "蓝牙模式：强度已归零，插件全面接管")
            except Exception as e:
                self._log("warning", f"蓝牙归零失败: {e}")
            return
        # App 模式：启动时绝不归零——否则会把用户手动调好的强度清掉
        self.current_strength_a = 0
        self.current_strength_b = 0
        self._log("info", "App 模式：强度请在手机 APP 手动调节"
                          "（远程调不动，插件只有急停归零可用）；"
                          "插件负责波形推送与脱战/暂停急停")

    def send_pulse(self, pulse_data, channel="All"):
        if self._paused:
            self._log("debug", "暂停中，忽略波形输出")
            return
        try:
            if self._bt():
                # 蓝牙：自定义波形经 /websocket 装入发送循环（自动播放 False，
                # 播一轮即止）。绝不调用 start_punish——它的"自动播放循环"
                # 用 stop_punish 停不掉（暂停语义），会导致持续输出。
                resp = self.server.send_waveform(waveform=pulse_data,
                                                 channel=channel)
                self._log("debug", f"蓝牙波形 {len(pulse_data)} 段 -> 发送循环")
                if self._check_dock_error("蓝牙波形", resp):
                    return
                if not self.sustain_active():
                    dur = max(0.5, len(pulse_data) * 0.1)
                    threading.Timer(dur, self._bt_auto_stop).start()
            else:
                # App 中继：total_duration 必须匹配帧数（100ms/帧）；
                # 默认 1s 会让 V4 消息里的 d 与帧数不符（30 帧=3s）
                dur = round(len(pulse_data) * 0.1, 2)
                resp = self.server.send_waveform(waveform=pulse_data,
                                                 channel=channel,
                                                 total_duration=dur)
                self._log("debug", f"App 波形 {channel} -> {len(pulse_data)} 段"
                                   f" (d={dur}s)")
                self._check_dock_error("send_waveform", resp)
        except Exception as e:
            self._log("error", f"send_waveform 失败: {e}")

    def _bt_auto_stop(self):
        """蓝牙一次性电击结束：清波形 + 暂停播放 + 强度归零三重兜底。

        实测 server 的 stop_punish 是"暂停"语义停不掉自动播放，
        强度归零是唯一物理可靠的输出切断手段。"""
        try:
            if not self.sustain_active():
                try:
                    self.server.clear_waveform()
                except Exception:
                    pass
                try:
                    self.server.coyote_stop_punish()
                except Exception:
                    pass
                self.set_strength("All", 0)
                self._log("debug", "蓝牙单次电击结束（清波+归零）")
        except Exception:
            pass

    def _check_dock_error(self, what, resp):
        """dock 拒绝指令时必须在日志里可见（强度/波形没生效的关键线索）。

        两种错误格式：/websocket 用 {"error": ...}；/coyote 用 {"code": 非200, "msg": ...}
        """
        if isinstance(resp, dict):
            if resp.get("error"):
                self._log("warning", f"dock 拒绝 {what}: {resp.get('error')}")
                return True
            code = resp.get("code")
            if code is not None and int(code) != 200:
                self._log("warning",
                          f"dock 拒绝 {what}: code={code} msg={resp.get('msg')}")
                return True
        return False

    def clear_waveform(self):
        try:
            if self._bt():
                # 蓝牙下两条独立输出链都要断：惩罚播放 + 波形发送循环
                self.server.coyote_stop_punish()
                try:
                    self.server.clear_waveform()
                except Exception:
                    pass
                self._log("debug", "蓝牙波形已停止（播放+发送循环）")
            elif hasattr(self.server, "clear_waveform"):
                self.server.clear_waveform()
                self._log("debug", "waveform cleared")
        except Exception as e:
            self._log("error", f"clear_waveform 失败: {e}")

    def _get_pulse(self, key, uid=None):
        data = self.waveform.get(key)
        if not (isinstance(data, list) and data):
            data = list(DEFAULT_PULSE)
        data = list(data)
        if uid is not None:
            # 多角色同时挨打时按角色轮转波形相位，体感略微区分
            order = [u for u in self.avatars.keys()]
            if uid in order and len(data) > 1:
                shift = order.index(uid) % len(data)
                data = data[shift:] + data[:shift]
        return data

    def drop_threshold_ok(self, threshold, amount):
        """掉落阈值（TriggerConditions.drop_threshold 的直值版本）"""
        return not (threshold > 0 and amount < threshold)

    def trigger(self, wave_key, base_a, base_b, damage_bonus, lost, label, uid=None,
                scope=""):
        """统一触发电击：受伤加成 + 叠加 -> 强度 + 波形（模块只管选参）

        scope="cw" 时使用货币战争自己的一套（cw_ 参数 + 独立叠加状态），
        触发间隔、叠加上限、回落方式全部不借用局内设置。
        """
        now = time.time()
        cw = (scope == "cw")
        ov = self.overlap_cw if cw else self.overlap

        # 触发间隔限制（各作用域各自计时）
        interval = self._f("cw_trigger_interval" if cw else "trigger_interval", 0.15)
        last_ts = self._last_trigger_ts_cw if cw else self._last_trigger_ts
        if interval > 0 and now - last_ts < interval:
            self._log("debug", f"{label} 触发过快，跳过")
            return
        if cw:
            self._last_trigger_ts_cw = now
        else:
            self._last_trigger_ts = now

        pulse_data = convert_pulse_data(self._get_pulse(wave_key))
        pulse_duration = get_pulse_duration(pulse_data)

        ov.apply_decay(now)
        _, total_add, _, damage_bonus = ov.compute(
            now, base_a, damage_bonus, pulse_duration
        )

        overlap_max = ov.max_strength()
        strength_a = min(base_a + total_add, overlap_max)
        strength_b = min(base_b + total_add, overlap_max)

        log_msg = ov.format_log(damage_bonus, total_add, strength_a, strength_b)
        self._log("info", label + (" | " + log_msg if log_msg else ""))

        # 发送强度（整数目标值变化才发）
        target_a = int(strength_a)
        target_b = int(strength_b)
        if target_a == target_b:
            if self.current_strength_a != target_a:
                self.set_strength("All", target_a)
                self.current_strength_a = target_a
                self.current_strength_b = target_b
        else:
            if self.current_strength_a != target_a:
                self.set_strength("A", target_a)
                self.current_strength_a = target_a
            if self.current_strength_b != target_b:
                self.set_strength("B", target_b)
                self.current_strength_b = target_b

        # 发送波形
        self.send_pulse(pulse_data, "All")

    # ================= 持续电击服务 =================

    def sustain(self, wave_key, duration, min_strength=0, stop_after=True):
        """持续电击：duration 秒内周期性重发波形；min_strength>0 时保底强度；
        stop_after 时结束后清波形停止输出"""
        duration = max(0.0, float(duration or 0))
        if duration <= 0:
            return
        with self._sustain_lock:
            self._sustain_gen += 1
            self._sustain_until = time.time() + duration
            self._sustain_wave_key = wave_key
            self._sustain_min = int(min_strength or 0)
            self._sustain_stop_after = bool(stop_after)
        min_strength = int(min_strength or 0)
        if min_strength > 0 and (self.current_strength_a or 0) < min_strength:
            self.set_strength("All", min_strength)
            self.current_strength_a = min_strength
            self.current_strength_b = min_strength
        if not self._sustain_thread or not self._sustain_thread.is_alive():
            self._sustain_thread = threading.Thread(target=self._sustain_loop,
                                                    name="sustain", daemon=True)
            self._sustain_thread.start()

    def sustain_active(self):
        with self._sustain_lock:
            return time.time() < self._sustain_until

    def _sustain_loop(self):
        while self.running:
            with self._sustain_lock:
                gen = self._sustain_gen
                until = self._sustain_until
                wave_key = self._sustain_wave_key
                stop_after = self._sustain_stop_after
            now = time.time()
            if now >= until:
                if stop_after and gen > 0 and wave_key:
                    self.clear_waveform()
                    with self._sustain_lock:
                        if self._sustain_gen == gen:
                            self._sustain_wave_key = None
                            self._sustain_until = 0.0
                self.stop_event.wait(0.2)
                continue
            try:
                self.send_pulse(convert_pulse_data(self._get_pulse(wave_key)), "All")
            except Exception:
                pass
            while (time.time() < self._sustain_until and self.running
                   and self._sustain_gen == gen):
                time.sleep(0.25)

    # ================= 后台循环 =================

    def _loop(self):
        """聚合冲刷 + 模块 tick + 数据源存活监测 + overlap 自然回落"""
        while not self.stop_event.is_set():
            time.sleep(0.1)
            if not self.running:
                break
            now = time.time()

            with self._lock:
                # 冲刷聚合窗口到期的掉血/掉盾（无新事件时兜底）
                self._flush_aggregated(now)
                for m in self.modules:
                    try:
                        m.tick(now)
                    except Exception:
                        pass
                self.overlap.apply_decay(now)
                self.overlap_cw.apply_decay(now)

            ver = self._get_veritas_source()
            if ver is not None and ver.sio is not None:
                if not ver.sio.connected:
                    if (not self._no_veritas_warned
                            and now - self._start_ts > 15):
                        self._no_veritas_warned = True
                        self._log("warning",
                                  "尚未连接 veritas：请确认已注入并运行 "
                                  "（默认 127.0.0.1:1305），插件将持续重试")
                elif (ver.sio.last_event_ts > 0
                      and now - ver.sio.last_event_ts > 90
                      and self._last_heartbeat_warn_ts == 0.0):
                    self._last_heartbeat_warn_ts = now
                    self._log("warning",
                              "超过 90 秒未收到 veritas 数据，连接可能已断开")
