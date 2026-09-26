"""
modules/battle.py - 模块：常规战斗

常规战斗（模拟宇宙/忘却之庭/剧情等标准回合制战斗）的惩罚策略，
逻辑自引擎整体迁入（行为与重构前完全一致）：
  - 角色掉血：盾阻止 -> 战斗开关 -> 忽略名单 -> 阈值 -> 受伤加成 -> 电击
  - 角色掉盾：盾量惩罚开关 -> 阈值 -> 电击（默认关，护盾到期无法区分）
  - 角色倒地：额外重罚 + 倒地震荡波形
"""
import time

import events as E
from hit_logic import TriggerConditions
from modules.base import ModuleBase


class BattleModule(ModuleBase):
    name = "battle"

    def __init__(self, engine):
        super().__init__(engine)
        self.triggers = TriggerConditions(self.cfg)

    # ---------- 掉血路由 ----------
    def wants_drop(self, kind):
        return kind in ("hp", "shield")

    def on_settled_drop(self, kind, uid, amount, src=None):
        # 数据输入选择：battle_data_source（veritas / ocr / both）
        sel = str(self.cfg("battle_data_source", "both") or "both").lower()
        if src and sel not in ("both", "") and src != sel:
            return
        if kind == "hp":
            self._hp_drop(uid, amount)
        else:
            self._shield_drop(uid, amount)

    def on_event(self, event, payload):
        if event == E.EVT_DEFEATED and (payload or {}).get("team") == "Player":
            self._knockdown((payload or {}).get("uid"))

    # ---------- 角色掉血（移植自挨打就电 process_health_drop） ----------
    def _hp_drop(self, uid, lost):
        e = self.engine
        name = e.avatars.get(uid, f"角色{uid}")

        # 盾存在时阻止血量电击（TriggerConditions.shield_blocks_health）
        blocked, block_msg = self.triggers.shield_blocks_health(e.shield.get(uid, 0))
        if blocked:
            e._log("debug", f"{name} {block_msg}，血量损失 {lost:.0f} 不计惩罚")
            return

        # 战斗开关
        if e._b("only_in_battle", True) and not e.battle_active:
            e._log("debug", f"{name} 脱战掉血 {lost:.0f}（仅战斗中惩罚已开启）")
            return

        # 忽略名单（子串匹配）
        ignore = str(self.cfg("ignore_names", "") or "")
        if ignore:
            keywords = [s.strip() for s in ignore.replace("，", ",").split(",") if s.strip()]
            if any(k in name for k in keywords):
                e._log("debug", f"{name} 在忽略名单中，跳过本次掉血 {lost:.0f}")
                return

        # 掉落阈值（TriggerConditions.drop_threshold）
        ok, skip_msg = self.triggers.drop_threshold("health", lost)
        if not ok:
            e._log("debug", f"{name} {skip_msg}")
            return
        # 百分比阈值（崩铁扩展：按最大生命过滤刮痧）
        mx = e.max_hp.get(uid, 0)
        min_pct = e._f("hit_min_percent", 0)
        if mx > 0 and min_pct > 0 and lost / mx * 100.0 < min_pct:
            return
        # 单次掉血上限（数据异常保护：上限 buff 部分过期等残留失真）
        max_pct = e._f("hit_max_percent", 0)
        if mx > 0 and max_pct > 0 and lost / mx * 100.0 > max_pct:
            e._log("debug", "%s 单次掉血 %.0f 超过上限 %.0f%%，视为数据异常忽略" % (
                name, lost, max_pct))
            return

        e.total_lost += lost
        e.hit_count += 1
        e._last_hit = {"name": name, "lost": round(lost, 1), "ts": time.time()}
        bonus = self._compute_bonus(uid, lost, mx)
        if bonus is None:
            return  # 低于强度阈值

        cur = e.hp.get(uid, 0)
        label = (f"💥 {name} 挨打 -{lost:.0f}（{cur:.0f}/{mx:.0f}）"
                 if mx > 0 else f"💥 {name} 挨打 -{lost:.0f}")
        wave_key = "hit_pulse"
        if e._low_sustain_active:
            wave_key = "shield_pulse"  # 残血持续电期间挨打换波形，体感区分
        e.trigger(wave_key, e._f("strength_a", 20), e._f("strength_b", 20),
                  bonus, lost, label, uid=uid)

    # ---------- 强度公式（ratio 模式：掉血占血量上限的比例驱动） ----------
    def _compute_bonus(self, uid, lost, mx):
        """返回强度加成；ratio 模式下低于阈值返回 None 表示本次不电"""
        e = self.engine
        mode = str(e._s("dmg_mode", "ratio") or "ratio").lower()
        if mode == "legacy" or mx <= 0:
            return e.detector.apply(lost)

        now = time.time()
        # 1) 伤害系数：掉血占血量上限百分比 -> 基础加成
        ratio_pct = lost / mx * 100.0
        raw = e._f("dmg_factor", 1.0) * ratio_pct

        # 2) 强度阈值：低于阈值的掉血不惩罚
        thr = e._f("dmg_threshold", 2.0)
        if raw < thr:
            e._log("debug", f"掉血占比 {ratio_pct:.2f}% 加成 {raw:.2f} 低于阈值 {thr}，跳过")
            return None
        excess = raw - thr

        # 3) 阈值压缩器：超出阈值部分的增长曲线
        tmode = str(e._s("dmg_threshold_mode", "sqrt") or "sqrt").lower()
        if tmode == "sqrt":
            compressed = excess ** 0.5 if excess > 0 else 0.0
        elif tmode == "log":
            compressed = math.log1p(max(excess, 0.0))
        else:
            compressed = excess
        bonus = thr + compressed

        # 4) 向上压缩器：软上限，超出部分按 25% 保留
        cap = e._f("dmg_upper_cap", 30)
        if cap > 0 and bonus > cap:
            bonus = cap + (bonus - cap) * 0.25

        # 5) 残血：受伤角色自身残血
        low_thr = e._f("low_hp_threshold", 30)
        hpv = e.hp.get(uid, 0)
        if low_thr > 0 and mx > 0 and hpv / mx * 100.0 < low_thr:
            bonus *= (1.0 + e._f("low_hp_factor", 0.5))

        # 6) 残血人数：全队残血越多加成越大
        low_n = e._low_hp_count()
        bonus *= (1.0 + low_n * e._f("low_hp_count_factor", 0.25))

        # 7) 多角色：最近窗口内不同受伤角色数
        win = e._f("multi_window", 2.0)
        recent_uids = {u for ts, u in list(e.recent_hits) if now - ts <= win}
        n_hit = len(recent_uids)
        if n_hit >= 2:
            bonus *= (1.0 + (n_hit - 1) * e._f("multi_factor", 0.5))

        # 8) 频率-强度压缩：近 5 秒触发越多，单次加成越低
        f = sum(1 for ts, _ in list(e.recent_hits) if now - ts <= 5.0)
        if f > 1:
            bonus *= max(0.2, 1.0 - (f - 1) * e._f("freq_damp", 0.15))

        # 9) 轮次系数：战斗轮次越深加成越高
        bonus *= (1.0 + e.current_cycle * e._f("cycle_factor", 0.1))

        e._log("debug", f"[formula] mode={mode} lost={lost:.0f} mx={mx:.0f} "
                        f"ratio={ratio_pct:.2f}% raw={raw:.2f} bonus={bonus:.2f} "
                        f"low_n={low_n} hits={n_hit} freq={f}")
        return round(bonus, 2)

    # ---------- 角色掉盾（移植自挨打就电 process_shield_drop） ----------
    def _shield_drop(self, uid, lost):
        e = self.engine
        if not e._b("shield_punish_enabled", False):
            return  # 默认仅显示不惩罚（护盾到期自然消失无法与被打区分）
        name = e.avatars.get(uid, f"角色{uid}")

        if e._b("only_in_battle", True) and not e.battle_active:
            return

        ignore = str(self.cfg("ignore_names", "") or "")
        if ignore:
            keywords = [s.strip() for s in ignore.replace("，", ",").split(",") if s.strip()]
            if any(k in name for k in keywords):
                return

        ok, skip_msg = self.triggers.drop_threshold("shield", lost)
        if not ok:
            e._log("debug", f"{name} 盾量{skip_msg}")
            return

        e.shield_hit_count += 1
        e._last_hit = {"name": name + "（盾）", "lost": round(lost, 1), "ts": time.time()}
        e._log("info", f"🛡 {name} 盾量被打 -{lost:.0f}")
        label = f"🛡 {name} 盾量挨打 -{lost:.0f}"
        e.trigger("shield_pulse", e._f("shield_strength_a", 12), e._f("shield_strength_b", 12),
                  e.detector.apply(lost), lost, label, uid=uid)

    # ---------- 倒地重罚 ----------
    def _knockdown(self, uid):
        e = self.engine
        if e._b("only_in_battle", True) and not e.battle_active:
            return
        if not e._b("knockdown_enabled", True):
            return
        e.knock_count += 1
        name = e.avatars.get(uid, f"角色{uid}")

        add = e._f("knockdown_add", 15)
        overlap_max = int(e._f("overlap_strength_max", 200))
        strength_a = int(min(e._f("strength_a", 20) + e.overlap.accumulated + add, overlap_max))
        strength_b = int(min(e._f("strength_b", 20) + e.overlap.accumulated + add, overlap_max))
        target = max(strength_a, strength_b)
        e.set_strength("All", target)
        e.current_strength_a = strength_a
        e.current_strength_b = strength_b
        e.send_pulse(e._get_pulse("knockdown_pulse"), "All")
        e._log("warning", f"☠ {name} 倒地！额外重罚 +{add:.0f} -> 强度 {target}")
