"""
modules/base.py - 模块基类

每个模块实现一种"玩法/场景"的惩罚策略，共享同一个引擎（engine）：
  - 引擎负责：事件状态、掉血聚合、受伤加成公式、触发（强度+波形）、持续电击
  - 模块负责：响哪些事件、用什么阈值/强度/波形

配置约定：
  - 模块总开关: module_{name}_enabled
  - 模块自有配置键自定（常规战斗沿用历史键名，货币战争用 cw_ 前缀）
"""


class ModuleBase:
    name = "base"

    def __init__(self, engine):
        self.engine = engine

    # ---------- 配置 ----------
    @property
    def enabled(self):
        return self.engine._b(f"module_{self.name}_enabled", True)

    def cfg(self, key, default=None):
        """读取插件配置（未加前缀）"""
        return self.engine._cfg(key, default)

    def log(self, level, msg):
        self.engine._log(level, f"[{self.name}] {msg}")

    # ---------- 生命周期 ----------
    def start(self):
        pass

    def stop(self):
        pass

    def tick(self, now):
        """引擎后台循环（约 0.1s 一次）"""

    # ---------- 事件 / 掉血 ----------
    def on_event(self, event, payload):
        """标准事件（events.py）"""

    def wants_drop(self, kind):
        """是否要处理某类聚合后的掉血（kind: "hp" / "shield"）"""
        return False

    def on_settled_drop(self, kind, uid, amount):
        """处理聚合后的掉血/掉盾"""
