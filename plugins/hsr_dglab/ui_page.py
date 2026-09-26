"""
ui_page.py - 插件配置页面（WinUI Fluent 风格重制版）

模板文件：本目录 ui_winui.html（HoYoShadeHub 同款设计稿整合而来，
第 7 个页签「V4 直连」为自研 V4 控制器预留，含配对/设备/实时强度卡片）。

运行环境：惩罚姬 Electron 窗口（nodeIntegration，可直接 fs 读写）。
本 server 版未开放插件级 HTTP 接口，页面与引擎通过 ipc.py 文件桥通信：

  配置读取  插件目录 config.json（meta.json 指路）+ 模板内置 DEFAULTS 兜底
  配置保存  引擎在线走命令桥（热更新+写盘），离线直写 config.json
  实时状态  status.json 心跳（引擎 0.5s 覆写，3s 判离线）
  页面动作  cmd.json / cmd_result.json

注入安全：动态内容一律 json.dumps + "</" -> "<\\/"，防止闭合标签截断脚本。
回退方案：旧版页面完整保留在 ui_page_legacy.py（PAGE_HTML）。
"""
import json
import os

_TEMPLATE = "ui_winui.html"

# 客户端可指定外部配置路径（exe 旁边的 "崩铁客户端.config.json"）；
# 保持 None 时继续读插件目录 config.json（原版 server.exe 行为不变）
CONFIG_PATH_OVERRIDE = None


def _read_config(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("config", {}) or {}
    except Exception:
        return {}


def _load_defaults() -> dict:
    """页面默认值 = 插件目录 config.json，再被外部配置覆盖。

    先铺插件目录的默认值、再让外部配置（exe 旁边的
    崩铁客户端.config.json）覆盖，是为了让**新增参数**在旧的外部配置
    里缺键时也能显示正确默认值否则新输入框是空的，保存时会被
    当成 0 写进去（例如新加的 cw_overlap_* / cw_hp_*）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    base = _read_config(os.path.join(here, "config.json"))
    out = dict(base)
    out["plugins"] = dict(base.get("plugins") or {})
    out["waveform"] = dict(base.get("waveform") or {})
    if CONFIG_PATH_OVERRIDE and os.path.isfile(CONFIG_PATH_OVERRIDE):
        over = _read_config(CONFIG_PATH_OVERRIDE)
        out["plugins"].update(over.get("plugins") or {})
        out["waveform"].update(over.get("waveform") or {})
    return out


def _safe_json(obj) -> str:
    """合法 JS 字面量（转义 </ 防止闭合标签截断脚本）"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _template() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _TEMPLATE)
    with open(path, encoding="utf-8") as f:
        return f.read()


def build(plugin_name: str) -> str:
    """生成插件配置页面 HTML（默认值 + 插件名安全注入）"""
    return (_template()
            .replace("__DEFAULTS_JSON__", _safe_json(_load_defaults()))
            .replace("__PLUGIN__", plugin_name))
