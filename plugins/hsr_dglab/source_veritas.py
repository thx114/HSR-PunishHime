"""
source_veritas.py - 数据源 A：veritas 伤害统计（内存/钩子路线）

veritas: https://github.com/hessiser/veritas （注入游戏进程的伤害统计 DLL）
通过其 Socket.IO 接口（默认 127.0.0.1:1305）获取精确战斗事件，
翻译成标准内部事件（events.py）后交给 core 统一处理。
"""
import collections
import re
import time

from sio_client import SioClient
import events as E


def _strip_parens(text):
    """去掉属性条目里的括号补充说明：'CurrentHP（当前生命）' -> 'CurrentHP'"""
    return re.sub(r"[（(][^）)]*[）)]", "", str(text)).strip()


class VeritasSource:
    name = "veritas"

    def __init__(self, emit, cfg, log):
        """
        :param emit: core.emit(event, payload)
        :param cfg: cfg(key, default)
        :param log: log(level, msg)
        """
        self._emit = emit
        self._cfg = cfg
        self._log = log
        self.sio = None
        # 探测模式事件流（页面 veritas 页实时展示，最近 100 条）
        self.probe_events = collections.deque(maxlen=100)

    def start(self):
        self.sio = SioClient(
            url=self._cfg("veritas_url", "http://127.0.0.1:1305"),
            on_event=self._on_event,
            log=self._log,
        )
        self.sio.start()

    def stop(self):
        if self.sio:
            self.sio.stop()

    # ---------------- 事件翻译 ----------------

    def _on_event(self, name, payload):
        try:
            if name == "Connected":
                self._emit(E.EVT_CONNECTED, payload or {})
            elif name == "Heartbeat":
                self._emit(E.EVT_HEARTBEAT, None)
            elif name == "OnBattleBegin":
                self._emit(E.EVT_BATTLE_BEGIN, payload or {})
            elif name == "OnSetBattleLineup":
                avatars = (payload or {}).get("avatars", [])
                self._emit(E.EVT_LINEUP, {"avatars": avatars})
            elif name == "OnStatChange":
                self._on_stat_change(payload or {})
            elif name == "OnEntityDefeated":
                defeated = (payload or {}).get("entity_defeated") or {}
                self._emit(E.EVT_DEFEATED, {
                    "uid": defeated.get("uid"),
                    "team": defeated.get("team"),
                })
            elif name == "OnBattleEnd":
                self._emit(E.EVT_BATTLE_END, payload or {})
            elif name == "OnTurnEnd":
                info = (payload or {}).get("turn_info") or {}
                self._emit(E.EVT_TURN, {
                    "cycle": info.get("cycle") or 0,
                    "action_value": info.get("action_value") or 0.0,
                    "wave": info.get("wave") or 0,
                })
            else:
                # OnDamage / OnTurnBegin / OnUseSkill 等暂不使用
                self._log("debug", f"[veritas] 忽略事件 {name}")
        except Exception as e:
            self._log("error", f"[veritas] 事件处理异常: {e}")

    @staticmethod
    def _format_probe(entity, prop):
        """探测条目：单行干净文本，剔除一切括号补充说明"""
        uid = _strip_parens(entity.get("uid", "?"))
        team = _strip_parens(entity.get("team", "?")) or "?"
        name = _strip_parens(entity.get("name", ""))
        ptype = _strip_parens(prop.get("type", "?"))
        value = _strip_parens(prop.get("value", "?"))
        label = name + " " if name else ""
        return "{} [{}] {}uid={} {}={}".format(
            time.strftime("%H:%M:%S"), team, label, uid, ptype, value)

    def _on_stat_change(self, payload):
        entity = payload.get("entity") or {}
        prop = payload.get("property") or {}
        # 探测模式：记录全部属性变化（含敌方/模式专属实体），
        # 用于排查各模式的数据源（敌方/模式专属实体都会打出来）
        if self._cfg("cw_probe", False):
            self.probe_events.append(self._format_probe(entity, prop))
        if entity.get("team") != "Player":
            return

        uid = entity.get("uid")
        ptype = prop.get("type")
        value = prop.get("value")
        # 名字随事件透传：客户端晚于进战斗接入时会错过 OnSetBattleLineup，
        # 引擎靠 HP 事件动态注册角色，没有名字就只能显示 角色{uid}
        name = entity.get("name") or ""

        if ptype == "CurrentHP":
            self._emit(E.EVT_HP_CHANGE, {"uid": uid, "hp": value, "name": name})
        elif ptype == "MaxHP":
            self._emit(E.EVT_HP_CHANGE, {"uid": uid, "max_hp": value, "name": name})
        elif ptype == "Shield":
            self._emit(E.EVT_HP_CHANGE, {"uid": uid, "shield": value, "name": name})
        elif ptype == "MaxShield":
            self._emit(E.EVT_HP_CHANGE, {"uid": uid, "max_shield": value, "name": name})
        # Speed / ActionDelay / RallyHP 等属性暂不使用
