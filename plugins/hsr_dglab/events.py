"""
events.py - 标准内部事件定义

数据源（veritas / ocr / ...）将游戏状态翻译成以下标准事件，
统一交给 core（hsr_dglab.HsrDGLab.emit）处理。
惩罚逻辑只认标准事件，与数据来源完全解耦。
"""

EVT_CONNECTED = "connected"        # {"version": "..."}  数据源就绪
EVT_HEARTBEAT = "heartbeat"        # None                数据源心跳
EVT_BATTLE_BEGIN = "battle_begin"  # {...}               进入战斗
EVT_BATTLE_END = "battle_end"      # {...}               战斗结束
EVT_LINEUP = "lineup"              # {"avatars": [{"id": int, "name": str}, ...]}
EVT_HP_CHANGE = "hp_change"        # {"uid": int, "hp"?: float, "max_hp"?: float, "name"?: str}
EVT_TOTAL_HP_CHANGE = "total_hp_change"  # {"total_hp": float}  玩家总血量（货币战争等模式）
EVT_DEFEATED = "defeated"          # {"uid": int, "team": "Player"|"Enemy"}
EVT_TURN = "turn"                  # {"cycle": int, "action_value": float, "wave": int}  回合推进
