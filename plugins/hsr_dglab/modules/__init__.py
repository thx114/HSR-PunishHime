"""
modules - 惩罚策略模块

  battle         常规战斗（角色血量 + 角色盾量 + 倒地），可选数据输入
  cw             货币战争（总血量电击 + 持续电击），OCR / veritas uid 输入
"""
from modules.battle import BattleModule
from modules.currency_wars import CurrencyWarsModule


def create_modules(engine):
    return [BattleModule(engine), CurrencyWarsModule(engine)]
