"""
hit_logic.py - 崩铁挨打就电 · 惩罚逻辑库

公式移植自"挨打就电"插件（HitElectricLogic.py）：
- calculate_damage_bonus   掉血量 -> 强度增幅（分段公式）
- DamageDetector           受伤程度检测（支持自定义脚本）
- OverlapProcessor         连续挨打叠加 + 自然回落
- convert_pulse_data 等    DGLab 波形工具
所有函数为纯逻辑，配置通过 cfg(key, default) 取值函数注入。
"""


# ============================================================
# 受伤程度检测
# ============================================================

def calculate_damage_bonus(lost_hp, mid_value, max_bonus):
    """
    掉血量 -> 强度增幅（分段公式，移植自挨打就电）

    r = lost_hp / mid_value
    - 轻伤  r < 10%:   max_bonus * 0.2
    - 中伤  10% ~ 50%: 线性 0.2 -> 0.6
    - 重伤  50% ~ 100%: 二次曲线 0.6 -> 1.2
    - 极限  r >= 100%: max_bonus * 1.2
    """
    if mid_value <= 0 or lost_hp <= 0 or max_bonus <= 0:
        return 0

    r = min(lost_hp / mid_value, 1.0)

    if r < 0.1:
        return max_bonus * 0.2
    elif r < 0.5:
        t = (r - 0.1) / 0.4
        return max_bonus * (0.2 + t * 0.4)
    elif r < 1.0:
        t = (r - 0.5) / 0.5
        return max_bonus * (0.6 + t * t * 0.6)
    else:
        return max_bonus * 1.2


def scoped_cfg(cfg, prefix=""):
    """带前缀的配置读取器：先读 <prefix>name，缺省回落共享 name。

    给"货币战争"这类需要独立参数的模块用——它们各自一份参数，
    不去借用局内（角色战斗）的设置。prefix 为空时就是原来的 cfg。
    """
    if not prefix:
        return cfg

    def _c(name, default=None):
        v = cfg(prefix + name, None)
        if v is not None:
            return v
        return cfg(name, default)
    return _c


class DamageDetector:
    """受伤程度检测模块（default 公式 / script 自定义脚本）"""

    def __init__(self, cfg, prefix=""):
        self._cfg = scoped_cfg(cfg, prefix)

    def apply(self, lost_hp):
        if not self._cfg("damage_enabled", True):
            return 0
        if lost_hp <= 0:
            return 0
        formula = self._cfg("damage_formula", "default")
        mid_value = self._cfg("damage_mid_value", 3000)
        max_bonus = self._cfg("damage_max_bonus", 10)
        if formula == "script":
            return self._run_script(lost_hp, mid_value, max_bonus)
        return calculate_damage_bonus(lost_hp, mid_value, max_bonus)

    def _run_script(self, lost_hp, mid_value, max_bonus):
        script = self._cfg("damage_script", "")
        if not script:
            return calculate_damage_bonus(lost_hp, mid_value, max_bonus)
        try:
            safe_builtins = {
                "max": max, "min": min, "abs": abs, "int": int,
                "float": float, "round": round, "len": len,
                "True": True, "False": False, "None": None,
            }
            local_vars = {
                "lost_hp": lost_hp,
                "mid_value": mid_value,
                "max_bonus": max_bonus,
                "result": 0,
            }
            exec(script, {"__builtins__": safe_builtins}, local_vars)
            return max(0, float(local_vars.get("result", 0)))
        except Exception:
            return 0


# ============================================================
# Overlap 叠加模块
# ============================================================

class OverlapProcessor:
    """
    连续挨打叠加模块（移植自挨打就电）

    管理 overlap 累加、衰减、上限规则。
    支持 5 种自然回落模式: instant / linear / percent / ratio_accel / script
    """

    DECAY_MODES = ("instant", "linear", "percent", "ratio_accel", "script")

    def __init__(self, cfg, prefix=""):
        self._cfg = scoped_cfg(cfg, prefix)
        self._prefix = prefix
        self.accumulated = 0.0
        self.active_until = 0.0
        self.base_until = 0.0
        self._initial_overlap_time = 0.0
        self._last_overlap_time = 0.0
        self._overlap_count = 0
        self._last_decay_time = 0.0

    def reset(self):
        self.accumulated = 0
        self._initial_overlap_time = 0.0
        self._last_overlap_time = 0.0
        self._overlap_count = 0
        self.active_until = 0.0
        self.base_until = 0.0

    def max_strength(self):
        """本模块的叠加上限（带前缀时读 <prefix>overlap_strength_max）"""
        return self._cfg("overlap_strength_max", 200)

    def is_overlap(self, now):
        if self._cfg("overlap_decay_enabled", False):
            return self.accumulated > 0 or now < self.active_until
        return now < self.active_until or self.active_until == 0.0

    def apply_decay(self, now):
        """每帧/周期调用，处理叠加值自然回落（基于实际时间差）"""
        if not self._cfg("overlap_decay_enabled", False):
            return
        if self.accumulated <= 0:
            return
        if now < self.active_until:
            self._last_decay_time = now
            return

        if self._last_decay_time <= 0:
            self._last_decay_time = now
        dt = now - self._last_decay_time
        if dt <= 0:
            return
        self._last_decay_time = now

        mode = self._cfg("overlap_decay_mode", "instant")
        if mode == "instant":
            self.accumulated = 0
        elif mode == "linear":
            decay_val = self._cfg("overlap_decay_value", 1)
            self.accumulated = max(0, self.accumulated - decay_val * dt)
        elif mode == "percent":
            decay_pct = self._cfg("overlap_decay_percent", 10)
            factor = (1 - decay_pct / 100.0) ** dt
            self.accumulated = max(0, self.accumulated * factor)
            if self.accumulated < 0.5:
                self.accumulated = 0
        elif mode == "ratio_accel":
            overlap_max = self._cfg("overlap_strength_max", 200)
            accel_factor = self._cfg("overlap_decay_ratio_accel", 0.5)
            if overlap_max > 0:
                ratio = self.accumulated / overlap_max
                decay_per_sec = max(0.1, ratio * accel_factor * overlap_max * 0.05)
                self.accumulated = max(0, self.accumulated - decay_per_sec * dt)
            else:
                self.accumulated = 0
        elif mode == "script":
            self._run_decay_script(now, dt)

    def _run_decay_script(self, now, dt):
        script = self._cfg("overlap_decay_script", "")
        if not script:
            self.accumulated = 0
            return
        try:
            safe_builtins = {
                "max": max, "min": min, "abs": abs, "int": int,
                "float": float, "round": round, "len": len,
                "True": True, "False": False, "None": None,
            }
            local_vars = {
                "accumulated": self.accumulated,
                "strength_max": self._cfg("overlap_strength_max", 200),
                "strength_add": self._cfg("overlap_strength_add", 1),
                "initial_overlap_time": self._initial_overlap_time,
                "overlap_count": self._overlap_count,
                "last_overlap_time": self._last_overlap_time,
                "now": now,
                "dt": dt,
            }
            exec(script, {"__builtins__": safe_builtins}, local_vars)
            self.accumulated = max(0, float(local_vars.get("accumulated", 0)))
        except Exception:
            self.accumulated = 0

    def compute(self, now, base_strength, damage_bonus, pulse_duration=0):
        """
        计算一次电击的叠加

        Returns:
            (overlap_add, total_add, proximity, damage_bonus_after_weaken)
        """
        overlap_max = self._cfg("overlap_strength_max", 200)
        current_total = base_strength + self.accumulated + damage_bonus
        proximity = min(current_total / overlap_max, 1.0) if overlap_max > 0 else 0

        total_add = damage_bonus
        overlap_add = 0

        in_overlap = self.is_overlap(now)
        if in_overlap and self._cfg("overlap_enabled", True):
            overlap_add_base = self._cfg("overlap_strength_add", 1)
            overlap_add = overlap_add_base * (1.0 - proximity)
            self.accumulated += overlap_add
            total_add += self.accumulated
            if self._initial_overlap_time == 0.0:
                self._initial_overlap_time = now
            self._last_overlap_time = now
            self._overlap_count += 1

        if pulse_duration > 0:
            self.update_timing(now, pulse_duration, proximity)

        damage_bonus_out = damage_bonus
        if self._cfg("damage_enabled", True) and damage_bonus > 0:
            cap = self._cfg("damage_max_bonus", 10) * 1.2
            if damage_bonus + self.accumulated > cap:
                excess = damage_bonus + self.accumulated - cap
                weaken_amount = min(damage_bonus, excess)
                damage_bonus_out = damage_bonus - weaken_amount * 0.75
                total_add = damage_bonus_out + self.accumulated

        return overlap_add, total_add, proximity, damage_bonus_out

    def update_timing(self, now, pulse_duration, proximity):
        """更新叠加窗口时间"""
        duration_mult_base = self._cfg("overlap_duration_multiplier", 1.5)
        duration_mult = 1.0 + (duration_mult_base - 1.0) * (1.0 - proximity)
        self.base_until = now + pulse_duration
        self.active_until = now + pulse_duration * duration_mult

    def format_log(self, damage_bonus, total_add, strength_a, strength_b):
        """格式化强度增幅日志"""
        if total_add <= 0:
            return None
        parts = []
        if damage_bonus > 0:
            parts.append(f"dmg+{damage_bonus:.0f}")
        if self.accumulated > 0:
            parts.append(f"ovlp+{self.accumulated:.0f}")
        if parts:
            return f"加成: {', '.join(parts)} | 合计+{total_add:.0f} | 强度 A:{strength_a:.0f} B:{strength_b:.0f}"
        return None


# ============================================================
# DGLab 波形工具（移植自挨打就电）
# ============================================================

def hex_pulse_to_pair(hex_str):
    """'1414141464646464' -> [频率, 强度]"""
    return [int(hex_str[0:2], 16), int(hex_str[8:10], 16)]


def convert_pulse_data(pulse_data):
    """兼容 数组/字符串/hex16 等格式，统一转换为 [[freq, strength], ...]"""
    if isinstance(pulse_data, list):
        result = []
        for pulse in pulse_data:
            if isinstance(pulse, str) and len(pulse) == 16:
                result.append(hex_pulse_to_pair(pulse))
            elif isinstance(pulse, list) and len(pulse) == 2:
                result.append(pulse)
            else:
                result.append(pulse)
        return result
    elif isinstance(pulse_data, str) and len(pulse_data) == 16:
        return [hex_pulse_to_pair(pulse_data)]
    return pulse_data


def get_pulse_duration(pulse_data):
    """波形时长，每个单元 100ms"""
    if isinstance(pulse_data, list) and pulse_data:
        return len(pulse_data) * 0.1
    return 0.5


# ============================================================
# 触发条件判断模块（移植自挨打就电 TriggerConditions）
# ============================================================

class TriggerConditions:
    """
    触发条件判断模块（移植自挨打就电 HitElectricLogic.TriggerConditions）

    提供触发条件的链式判断，每个方法返回 (should_trigger, debug_msg)。

    未移植: immune_check（多角色切换免疫）——挨打就电在 OCR 采样下切人
    会读到血量跳变需要免疫帧；内存路线按 uid 精确追踪各角色血量，
    切人不产生任何跳变，天然免疫，故该检测无需移植。
    未移植: OCRValidator（OCR 误读校验）——内存读数为精确值，无 OCR 误差。
    """

    def __init__(self, cfg):
        self._cfg = cfg

    def shield_blocks_health(self, current_shield):
        """盾存在时阻止血量电击"""
        if self._cfg("shield_blocks_health", True) and current_shield > 0:
            return True, f"盾存在时阻止血量电击 (盾={current_shield:.0f})"
        return False, ""

    def drop_threshold(self, bar_type, drop_amount, should_trigger=True):
        """掉落阈值检测"""
        if not should_trigger:
            return False, ""
        threshold = self._cfg(f"{bar_type}_drop_threshold", 0)
        if threshold > 0 and drop_amount < threshold:
            return False, f"{bar_type}减少{drop_amount:.0f}未达阈值{threshold}, 跳过电击"
        return True, ""


# ============================================================
# 掉血聚合器（还原挨打就电的逐帧采样聚合语义）
# ============================================================

class HitAggregator:
    """
    掉血/掉盾聚合器

    挨打就电以 10Hz 截图采样，同一采样帧内发生的多次掉血会合并为
    一次触发；内存路线收到的是每次伤害实例的独立事件。
    本模块用迟滞滑窗还原该语义：窗口（默认 0.1s，同挨打就电帧间隔）
    内同一来源的掉血合并为一次触发。

    用法: add() 累积 -> flush(now) 冲刷已到期的键
    (core 在事件入口与后台 tick 双路调用 flush)。
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._pending = {}  # key -> {"amount": float, "last_ts": float}

    def window(self):
        try:
            return max(0.0, float(self._cfg("hit_aggregate_window", 0.1) or 0.0))
        except (TypeError, ValueError):
            return 0.1

    def add(self, key, amount, now):
        """累积一笔掉血/掉盾"""
        entry = self._pending.get(key)
        if entry is None:
            self._pending[key] = {"amount": amount, "last_ts": now}
        else:
            entry["amount"] += amount
            entry["last_ts"] = now

    def flush(self, now):
        """冲刷窗口已过期的键，返回 [(key, amount), ...]"""
        window = self.window()
        out = []
        for key in list(self._pending):
            entry = self._pending[key]
            if now - entry["last_ts"] >= window:
                out.append((key, entry["amount"]))
                del self._pending[key]
        return out

    def discard(self, key):
        """撤销某键的全部挂起（veritas 毛刺过滤用）"""
        self._pending.pop(key, None)

    def flush_all(self):
        """立即冲刷全部挂起（战斗结束等场景）"""
        out = [(k, e["amount"]) for k, e in self._pending.items()]
        self._pending.clear()
        return out

    def pending_keys(self):
        return list(self._pending.keys())
