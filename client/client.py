# -*- coding: utf-8 -*-
"""
client.py - HSR PunishHime（惩罚姬）· 崩铁独立客户端

单进程四件套：
  1. V4Bridge   —— 自研 V4 控制器（v4ctrl）线程化门面，DG-LAB 4 APP 扫码接入
  2. V4Backend  —— dockdglab 兼容门面，喂给 hsr_dglab 惩罚引擎
  3. 惩罚引擎   —— hsr_dglab.HsrDGLab 原样复用（veritas/OCR/战斗/货币战争）
  4. HTTP+窗口  —— aiohttp 本地服务 WinUI 风格界面 + pywebview 独立桌面窗口

用法：
  python client.py                # 桌面窗口模式
  python client.py --headless     # 无窗口（服务仍可用浏览器访问）
  python client.py --port 5865 --v4port 9898
"""
import argparse
import asyncio
import collections
import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import webbrowser

FROZEN = bool(getattr(sys, "frozen", False))   # 单文件 exe


def _resolve_dirs():
    """定位"惩罚姬根目录"和 client 目录。

    冻结成 exe 后 __file__ 指向临时解包目录，那边的 config/plugin 全是空的；
    所以外部资源一律相对 exe 所在目录（或其上一级含 plugins 的目录）。
    """
    start = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
             else os.path.dirname(os.path.abspath(__file__)))
    for cand in (start, os.path.dirname(start)):
        if os.path.isdir(os.path.join(cand, "plugins")):
            cdir = os.path.join(cand, "client")
            return cand, (cdir if os.path.isdir(cdir) else cand)
    return start, os.path.join(start, "client")


ROOT_DIR, HERE = _resolve_dirs()
PLUGIN_DIR = os.path.join(ROOT_DIR, "plugins", "hsr_dglab")
for _p in (HERE, PLUGIN_DIR, os.path.join(ROOT_DIR, "lib"), ROOT_DIR):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import hsr_dglab   # noqa: E402  (惩罚引擎)
import ipc         # noqa: E402  (文件桥)
import start       # noqa: E402  (插件动作处理器集)
import ui_page     # noqa: E402  (模板占位符替换)

TITLE = "崩铁·挨打就电客户端"
_win = None            # pywebview 窗口
_engine = None
_bridge = None
HEADLESS = False
_stop_evt = threading.Event()


_print_q = queue.Queue()


def _print_worker():
    """控制台打印后台化：任何线程（含事件循环）都不再同步 print。

    Windows 控制台 I/O 极慢（QuickEdit 选中时会无限期阻塞），
    曾把事件循环卡死 9 秒导致 APP 判死断连。"""
    while True:
        line = _print_q.get()
        try:
            print(line)
        except Exception:
            pass


threading.Thread(target=_print_worker, daemon=True,
                 name="console-print").start()


# ---------------- 日志 ----------------
class ClientLogger:
    """DockLogger 同款接口（引擎/后端都认 success/info/warn/error/debug）

    全部级别进环形缓冲（界面日志面板读取）；调试级不再刷控制台。"""

    def __init__(self):
        self.ring = collections.deque(maxlen=400)

    def _p(self, lv, msg):
        line = "[" + time.strftime("%H:%M:%S") + "][" + lv + "] " + str(msg)
        self.ring.append(line)
        if lv != "调试":
            _print_q.put(line)

    def success(self, m):
        self._p("成功", m)

    def info(self, m):
        self._p("信息", m)

    def warn(self, m):
        self._p("警告", m)

    def error(self, m):
        self._p("错误", m)

    def debug(self, m):
        self._p("调试", m)


LOG = ClientLogger()

# ---------------- 外部配置（就放在 exe 旁边，权威） ----------------
# 规则：<根目录>\崩铁客户端.config.json 是唯一读写目标；
#       不存在就从插件目录 config.json（你现在调好的那份）复制一份出来；
#       插件目录那份不再被本客户端改动，原版 server.exe 仍可各用各的。
CONFIG_FILE = os.path.join(ROOT_DIR, "崩铁客户端.config.json")
PAIR_FILE = os.path.join(ROOT_DIR, "v4_pair.json")


def _ensure_external_config():
    if os.path.isfile(CONFIG_FILE):
        return
    src = os.path.join(PLUGIN_DIR, "config.json")
    try:
        if os.path.isfile(src):
            shutil.copy2(src, CONFIG_FILE)
            LOG.success("已生成外部配置 %s（复制自插件目录现有配置）"
                        % os.path.basename(CONFIG_FILE))
        else:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"config": {"plugins": {}, "waveform": {}}, "map": {}},
                          f, ensure_ascii=False, indent=2)
            LOG.warn("已生成空白外部配置 %s（插件目录没有 config.json）"
                     % os.path.basename(CONFIG_FILE))
    except Exception as e:
        LOG.error("生成外部配置失败: %s" % e)


_ensure_external_config()
start.CONFIG_PATH = CONFIG_FILE        # 读 + 写都走 exe 旁边这份
ui_page.CONFIG_PATH_OVERRIDE = CONFIG_FILE   # 界面默认值也读这份

# 截图/帧日志等产物也统一放 exe 旁边，插件目录保持只读源码
SHOTS_DIR = os.path.join(ROOT_DIR, "screenshots")
try:
    os.makedirs(SHOTS_DIR, exist_ok=True)
except Exception:
    pass


# ---------------- 共享 asyncio loop（V4 + UI HTTP 共用） ----------------
class LoopThread:
    def __init__(self):
        self.loop = None
        self._ready = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="asyncio")

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def start(self):
        self._t.start()
        self._ready.wait(5)


# ---------------- 状态合并（快照 + V4 块 + 心跳） ----------------
def get_status_merged():
    try:
        base = json.loads(start.get_status())
    except Exception:
        base = {"running": False}
    try:
        v4 = _bridge.status()
        base["v4"] = v4
        # 实时状态页的强度直读设备真值（引擎记账值可能与 APP 手动调节脱节）
        if v4.get("strength_a") is not None:
            base["strength_a"] = v4["strength_a"]
            base["strength_b"] = v4.get("strength_b")
    except Exception as e:
        base["v4"] = {"connected": False, "error": str(e)[:200]}
        try:
            LOG._p("警告", "V4 状态读取失败: %s" % e)
        except Exception:
            pass
    base["ipc_ts"] = time.time()
    return base


def strength_sync_loop():
    """把引擎记账强度每 0.4s 对齐设备真值。

    APP 端手动调节、舒适限幅等都会改变真值而引擎看不见；
    记账值脱节后，± 按钮与惩罚触发的绝对强度换算全会错位
    （实测曾出现目标 20 实际跳 38）。真值以 slots.patch 为准。
    同时写 v4_state.json sidecar，HUD 左上角显示 ⚡电击中 与实时强度。"""
    while not _stop_evt.is_set():
        try:
            eng = _engine
            if eng is not None and getattr(eng, "running", False) and _bridge.connected:
                real = _bridge.strength_map()
                with eng._lock:
                    eng.current_strength_a = int(real.get("A") or 0)
                    eng.current_strength_b = int(real.get("B") or 0)
                try:
                    ipc._write("v4_state.json", {
                        "shocking": _bridge.shocking,
                        "strength_a": int(real.get("A") or 0),
                        "strength_b": int(real.get("B") or 0),
                        "ts": time.time(),
                    })
                except Exception:
                    pass
            else:
                try:
                    ipc._write("v4_state.json", {"shocking": False, "ts": time.time()})
                except Exception:
                    pass
        except Exception:
            pass
        _stop_evt.wait(0.4)


# ---------------- V4 页面动作 ----------------
def _v4_step():
    try:
        return max(1, int(float(start.app.config.get("plugins", {}).get("v4_step", 5) or 5)))
    except Exception:
        return 5


def _v4_add(params):
    ch = str((params or {}).get("channel", "A"))
    step = _v4_step()
    _bridge.add(ch, step)
    return {"ok": True, "message": "V4 通道%s +%d" % (ch, step)}


def _v4_reduce(params):
    ch = str((params or {}).get("channel", "A"))
    step = _v4_step()
    _bridge.add(ch, -step)
    return {"ok": True, "message": "V4 通道%s -%d" % (ch, step)}


def _v4_reset(params):
    ch = str((params or {}).get("channel", ""))
    _bridge.zero(None if not ch or ch.lower() == "all" else ch)
    return {"ok": True, "message": "V4 强度已归零"}


def _v4_zero_all(params):
    _bridge.zero()
    return {"ok": True, "message": "V4 全通道急停归零"}


def _v4_test_wave(params):
    import hit_logic
    wave = start.app.waveform.get("hit_pulse") or ["6400000064000000"]
    pulse = hit_logic.convert_pulse_data(wave)
    _bridge.send_wave(pulse, "All")
    v, ms = __import__("v4_backend").normalize_wave(pulse)
    return {"ok": True,
            "message": "V4 测试波形已发送（约 %.1f 秒，输出 = 通道强度 × 波形强度）" % (ms / 1000.0)}


def _v4_pair(params):
    return {"ok": True, "value": {"addr": _bridge.pair_url, "tid": _bridge.tid,
                                  "qr": _bridge.qr_data_uri(),
                                  "connected": _bridge.connected}}


def _toggle_top(params):
    """客户端窗口置顶（Win32，句柄取 pywebview 原生窗口）"""
    try:
        import ctypes
        on = bool((params or {}).get("on"))
        hwnd = None
        try:
            hwnd = int(_win.native) if _win is not None else None
        except Exception:
            hwnd = None
        if not hwnd:
            return {"ok": False, "message": "窗口句柄未就绪"}
        SWP = 0x0001 | 0x0002
        ok = ctypes.windll.user32.SetWindowPos(
            hwnd, -1 if on else -2, 0, 0, 0, 0, SWP)
        if not ok:
            return {"ok": False, "message": "SetWindowPos 失败"}
        return {"ok": True, "message": "窗口已置顶" if on else "已取消置顶"}
    except Exception as e:
        return {"ok": False, "message": "置顶失败: %s" % e}


def client_dispatch(action, params):
    """命令路由：V4 专属动作在前，其余回落到 start.py 的 20 个处理器"""
    try:
        h = {
            "v4_add": _v4_add, "v4_reduce": _v4_reduce, "v4_reset": _v4_reset,
            "v4_zero_all": _v4_zero_all, "v4_test_wave": _v4_test_wave,
            "v4_pair": _v4_pair,
        }.get(action)
        if h is not None:
            return h(params if isinstance(params, dict) else {})
        res = start.dispatch(action, params)
        if isinstance(res, str):
            # start.py 部分处理器沿用 ipc 桥惯例返回 JSON 字符串
            try:
                res = json.loads(res)
            except Exception:
                res = {"ok": False, "message": str(res)}
        return res
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ---------------- 界面 HTML（读取插件模板 + 客户端桥接注入） ----------------
_UI_HTML = None


def build_ui_html():
    global _UI_HTML
    if _UI_HTML is not None:
        return _UI_HTML
    # 关键：占位符替换必须走 ui_page.build（__DEFAULTS_JSON__ 是非法 JS 字面量，
    # 原样输出会让整个脚本解析失败、所有按钮绑定失效——浏览器/WebView 双杀）
    src = ui_page.build("hsr_dglab")
    # 移除静态占位拦截（V4 动作已真实接入，双重提示会误导）
    # 占位提示文案失效（V4 动作已真实接入，旧监听器保留但不再误导）
    src = src.replace("V4 直连：当前为静态 UI 占位，动作尚未接入控制器",
                      "V4 直连已接入客户端")
    src = src.replace("当前页面只提供 UI 与数据占位，后续可直接接入 WebSocket 配对协议。",
                      "APP 扫码即连：远程强度、波形下发、急停归零全部真实生效。")
    src = src.replace("<span>静态设计</span>", "<span>V4 实时</span>")
    src = src.replace("等待 APP 接入</div>", "等待 APP 接入（二维码扫码后自动完成配对）</div>")
    # 二维码卡右侧文案块挂 id，接入后由 renderV4 切换为实时信息
    src = src.replace('<div style="min-width:0;">'
                      '\n            <div style="font-size:13px;font-weight:600;margin-bottom:7px;">等待 APP 接入',
                      '<div style="min-width:0;" id="v4PairInfo">'
                      '\n            <div style="font-size:13px;font-weight:600;margin-bottom:7px;">等待 APP 接入')
    # WebView2 没有 require：guarded 化 Electron 依赖，否则整个脚本在
    # L646 ReferenceError 崩掉，后面所有按钮绑定全部失效（任何按钮没反应的根因）
    src = src.replace("const { ipcRenderer } = require('electron');",
                      "let ipcRenderer = null;"
                      " try { ipcRenderer = require('electron'); } catch(e) {}")
    inject = """
<script>
window.__CLIENT__ = true;
window.addEventListener('error', function(e){
  try{
    fetch('/api/jserror', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({msg: String(e.message || e), line: e.lineno || 0})});
  }catch(err){}
});
let CLIENT_STATUS = null, CLIENT_META = null, v4PairInfoHTML = null;
async function apiCmd(action, params){
  const r = await fetch('/api/cmd', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action: action, params: params || {}})});
  return await r.json();
}
callEngine = function(action, params, cb, timeout){
  apiCmd(action, params).then(function(r){ try{ cb(r); }catch(e){} })
    .catch(function(){ cb({ok:false, message:'客户端服务不可达'}); });
};
// 原版页面用 Electron 的 require('fs') 读 %TEMP%/hsr_dglab_ipc 判定引擎存活；
// 独立客户端没有 Node FS，engineAlive() 会恒为 false，
// 导致「保存配置/重置配置」走 Node 写入分支并报"保存失败"。改判 HTTP 状态心跳。
engineAlive = function(){
  try{
    const s = CLIENT_STATUS;
    return !!(s && s.ipc_ts && (Date.now() / 1000 - s.ipc_ts < 3));
  }catch(e){ return false; }
};
function renderV4(s){
  try{
    const v = s && s.v4; if(!v) return;
    const addr = document.getElementById('v4Addr');
    if(addr && v.addr) addr.textContent = v.addr;
    const st = document.getElementById('v4States');
    if(st){
      const pills = st.querySelectorAll('.state');
      const step = v.connected ? ((v.slot || v.name) ? 2 : 1) : 0;
      pills.forEach(function(p, i){ p.classList.toggle('active', i === step); });
    }
    const info = document.getElementById('v4PairInfo');
    if(info){
      if(v4PairInfoHTML === null) v4PairInfoHTML = info.innerHTML;
      if(v.connected){
        const sa = (v.status_a == null) ? '-' : v.status_a;
        const sb = (v.status_b == null) ? '-' : v.status_b;
        info.innerHTML = '<div style="font-size:13px;font-weight:600;margin-bottom:7px;">已连接 · '
          + (v.name || 'APP') + '</div>'
          + '<div class="muted">通道输出状态 A=' + sa + ' / B=' + sb
          + '（2=输出正常，0=无输出）</div>'
          + '<div class="v4-note">强度在 APP 端手动调节也会实时同步到本页。</div>';
      } else if(info.innerHTML !== v4PairInfoHTML){
        info.innerHTML = v4PairInfoHTML;
      }
    }
    const qrBox = document.querySelector('#pg-v4 .qr');
    if(qrBox && v.qr){
      let img = qrBox.querySelector('img');
      if(!img){
        qrBox.innerHTML = '';
        img = document.createElement('img');
        img.alt = 'V4 配对二维码';
        img.style.width = '160px'; img.style.height = '160px';
        img.style.display = 'block';
        qrBox.appendChild(img);
      }
      if(img.getAttribute('src') !== v.qr) img.src = v.qr;
    }
    const slot = document.getElementById('v4Slot');
    if(slot && v.slot) slot.textContent = 'slotId: ' + v.slot;
    const dev = document.getElementById('v4DevName');
    if(dev && v.name) dev.textContent = v.name;
    if(typeof v.battery === 'number' && v.battery > 0){
      const bar = document.querySelector('#v4Batt i'); if(bar) bar.style.width = v.battery + '%';
      const pct = document.getElementById('v4BattPct'); if(pct) pct.textContent = v.battery + '%';
    }
  }catch(e){}
}
readStatus = function(){ return CLIENT_STATUS; };
metaInfo = function(){ return CLIENT_META; };
engineAlive = function(){
  return !!(CLIENT_STATUS && CLIENT_STATUS.ipc_ts &&
            (Date.now() / 1000 - CLIENT_STATUS.ipc_ts < 3));
};
loadConfig = async function(){
  const merged = {
    plugins: Object.assign({}, (window.DEFAULTS && DEFAULTS.plugins) || {}),
    waveform: Object.assign({}, (window.DEFAULTS && DEFAULTS.waveform) || {})
  };
  try{
    const r = await fetch('/config/hsr_dglab');
    const disk = await r.json();
    Object.assign(merged.plugins, disk.plugins || {});
    Object.assign(merged.waveform, disk.waveform || {});
  }catch(e){}
  waveformCache = merged.waveform;
  document.querySelectorAll('[data-key]').forEach(el => {
    const v = merged.plugins[el.dataset.key];
    if(v === undefined) return;
    if(el.type === 'checkbox') el.checked = (v === true || v === 'true');
    else el.value = v;
  });
};
const wc = document.getElementById('winClose');
if(wc) wc.onclick = function(){ fetch('/api/win/close', {method:'POST'}); };
// ---------------- 视频测试窗口按钮（OCR 识别页，靶窗） ----------------
(function(){
  const box = document.getElementById('ocrTools')
    || (document.getElementById('btnPick') || {}).parentNode;
  if(box && !document.getElementById('btnVideoWin')){
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.id = 'btnVideoWin';
    btn.textContent = '🎬 视频测试窗口';
    btn.title = '启动播放测试视频的靶窗（窗口标题=测试窗口，供 OCR/框选取色调试）';
    btn.onclick = function(){
      btn.disabled = true;
      fetch('/api/viewer', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:'{}'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(typeof hint === 'function') hint(d.message || '');
        btn.disabled = false;
      })
      .catch(function(){
        if(typeof hint === 'function') hint('启动请求失败');
        btn.disabled = false;
      });
    };
    box.appendChild(btn);
  }
})();
// 截图列表：模板只在截图后刷新，页面打开时永远是空的——补一次启动刷新
try { refreshShots(); } catch(e) {}
// ---------------- 运行日志面板（veritas 页） ----------------
(function(){
  const probe = document.getElementById('probeFeed');
  if(probe && !document.getElementById('runLogFeed')){
    const fold = probe.closest('.fold');
    if(fold){
      const div = document.createElement('div');
      div.className = 'fold open';
      div.style.cssText = 'display:flex;flex-direction:column;flex:1;min-height:0;';
      div.innerHTML = '<div class="head" onclick="tog(this)"><span>运行日志（引擎 / veritas / V4）</span><span>&#9662;</span></div>'
        + '<div class="body" style="display:flex;flex-direction:column;flex:1;min-height:0;">'
        + '<pre id="runLogFeed" class="feed"></pre></div>';
      fold.after(div);
    }
  }
  const NL = String.fromCharCode(10);
  setInterval(async function(){
    const el = document.getElementById('runLogFeed');
    if(!el || el.offsetParent === null) return;
    try{
      const r = await fetch('/api/logs');
      const d = await r.json();
      const lines = (d.lines || []).slice(-60);
      const txt = lines.join(NL);
      if(el.textContent !== txt){
        const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
        el.textContent = txt;
        if(stick) el.scrollTop = el.scrollHeight;
      }
    }catch(e){}
  }, 1000);
})();
(function(){
  // 无边框窗口：header 拖拽移动（自实现；easy_drag 已关闭，避免全窗口拖拽
  // 劫持边缘缩放与文本选择）
  const hd = document.querySelector('body > header');
  if(!hd) return;
  const st = document.createElement('style');
  st.textContent = 'header, header * { user-select:none !important; }'
    + 'header * { pointer-events:none !important; }';
  document.head.appendChild(st);
  hd.addEventListener('mousedown', function(e){
    if(e.button !== 0) return;
    e.preventDefault();
    const ratio = window.devicePixelRatio || 1;
    const sx = e.screenX, sy = e.screenY;
    let ox = 0, oy = 0, ready = false;
    fetch('/api/win/geo').then(function(r){ return r.json(); }).then(function(g){
      ox = g.x; oy = g.y; ready = true;
    }).catch(function(){});
    const mv = function(ev){
      if(!ready) return;
      const x = ox + (ev.screenX - sx) * ratio;
      const y = oy + (ev.screenY - sy) * ratio;
      clearTimeout(mv._t);
      mv._t = setTimeout(function(){
        fetch('/api/win/move', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({x: Math.round(x), y: Math.round(y)})});
      }, 15);
    };
    const up = function(){
      document.removeEventListener('mousemove', mv);
      document.removeEventListener('mouseup', up);
    };
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
  });
})();
(async function(){
  try{ CLIENT_META = await (await fetch('/api/meta')).json(); }catch(e){}
  try{ CLIENT_STATUS = await (await fetch('/api/status')).json(); }catch(e){}
  try{ await loadConfig(); }catch(e){}
  try{ poll(); }catch(e){}
})();
setInterval(async function(){
  try{ CLIENT_STATUS = await (await fetch('/api/status')).json(); }
  catch(e){ CLIENT_STATUS = null; }
  try{ renderV4(CLIENT_STATUS); }catch(e){}
}, 700);
</script>
"""
    src = src.replace("</body>", inject + "</body>")
    _UI_HTML = src
    return src


# ---------------- aiohttp UI 服务 ----------------
async def _ui_server(port):
    from aiohttp import web
    app = web.Application()

    async def ui_index(request):
        return web.Response(text=build_ui_html(), content_type="text/html",
                            charset="utf-8")

    async def ui_config(request):
        return web.json_response(start.app.config)

    async def api_status(request):
        # 引擎快照要拿引擎锁，而引擎操作握锁做 RPC 可能耗时数秒——
        # 丢进线程池，事件循环永不等待引擎锁（否则 pong 延迟，APP 判死断连）
        loop = asyncio.get_event_loop()
        try:
            snap = await loop.run_in_executor(None, get_status_merged)
        except Exception as e:
            # 状态查询失败不能让接口 500：前端会拿到非 JSON 体，
            # 表现为状态卡"已连接/等待接入"来回闪
            snap = {"running": False, "v4": {"connected": False},
                    "status_error": str(e)[:200], "ipc_ts": time.time()}
        return web.json_response(snap)

    async def api_meta(request):
        return web.json_response({"plugin_name": "hsr_dglab",
                                  "plugin_dir": PLUGIN_DIR})

    async def api_cmd(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action"))
        params = body.get("params") or {}
        # 关键：分发必须在非 loop 线程执行——bridge 同步操作内部用
        # run_coroutine_threadsafe(...).result()，在 loop 线程上调用会
        # 死锁到超时，卡死整个事件循环（心跳饿死 -> APP 断线）
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, client_dispatch, action, params)
        # 信封格式与 ipc.CmdWatcher 一致：{id, ok, message, value}
        env = {"id": str(body.get("id") or ""), "ok": True,
               "message": "", "value": None}
        if isinstance(result, dict):
            env.update(result)
            env.setdefault("ok", True)
        else:
            env["message"] = str(result)
        return web.json_response(env)

    async def api_close(request):
        try:
            if _win is not None:
                _win.destroy()
        except Exception:
            pass
        return web.json_response({"ok": True})

    async def api_jserror(request):
        try:
            body = await request.json()
            LOG.error("页面JS错误 L%s: %s" % (body.get("line"), body.get("msg")))
        except Exception:
            pass
        return web.json_response({"ok": True})

    async def api_logs(request):
        return web.json_response({"lines": list(LOG.ring)})

    # ---- 无边框窗口缩放（Win32 SetWindowPos，右/下/右下角拖拽）----
    _hwnd_cache = [None]
    _viewer_proc = [None]
    DEFAULT_TEST_VIDEO = ""   # 便携版：不绑本机路径，由界面选择视频
    _VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv")

    def _main_hwnd():
        if _hwnd_cache[0]:
            return _hwnd_cache[0]
        try:
            user32 = ctypes.windll.user32
            buf = ctypes.create_unicode_buffer(256)
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def _cb(hwnd, _lp):
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value.startswith("崩铁·挨打就电"):
                    found.append(hwnd)
                    return False
                return True

            user32.EnumWindows(_cb, None)
            if found:
                _hwnd_cache[0] = found[0]
        except Exception:
            pass
        return _hwnd_cache[0]

    async def api_win_geo(request):
        hwnd = _main_hwnd()
        if not hwnd:
            return web.json_response({"x": 0, "y": 0, "w": 1280, "h": 880})

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = _RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return web.json_response({"x": r.left, "y": r.top,
                                  "w": r.right - r.left, "h": r.bottom - r.top})

    async def api_win_resize(request):
        try:
            body = await request.json()
            w = max(960, int(body.get("w") or 1280))
            h = max(600, int(body.get("h") or 880))
            hwnd = _main_hwnd()
            if hwnd and not ctypes.windll.user32.IsZoomed(hwnd):
                # SWP_NOZORDER(0x0004)|SWP_NOMOVE(0x0002)：只改尺寸不动位置
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, w, h, 0x0004 | 0x0002)
        except Exception:
            pass
        return web.json_response({"ok": True})

    async def api_win_move(request):
        try:
            body = await request.json()
            x = int(body.get("x") or 0)
            y = int(body.get("y") or 0)
            hwnd = _main_hwnd()
            if hwnd and not ctypes.windll.user32.IsZoomed(hwnd):
                # SWP_NOZORDER(0x0004)|SWP_NOSIZE(0x0001)：只动位置不改尺寸
                ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0004 | 0x0001)
        except Exception:
            pass
        return web.json_response({"ok": True})

    async def api_viewer(request):
        """启动视频测试靶窗（viewer_app.py，标题固定'测试窗口'，供 OCR 调试）"""
        import subprocess
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = str(body.get("path") or DEFAULT_TEST_VIDEO).strip('"')
        if not path:
            return web.json_response({"ok": False, "message": "请先选择一个视频文件"})
        if not os.path.isfile(path):
            return web.json_response({"ok": False,
                                      "message": "视频不存在: %s" % path})
        if os.path.splitext(path)[1].lower() not in _VIDEO_EXT:
            return web.json_response({"ok": False, "message": "仅支持视频文件"})
        # 以窗口实际存在为准（DETACHED 子进程句柄 poll 不可靠，防重复叠窗）
        if ctypes.windll.user32.FindWindowW(None, "测试窗口"):
            return web.json_response({"ok": True, "message": "测试窗口已在运行"})
        proc = _viewer_proc[0]
        if proc is not None and proc.poll() is not None:
            _viewer_proc[0] = None
        vp = os.path.join(PLUGIN_DIR, "viewer_app.py")
        if not os.path.isfile(vp):
            return web.json_response({"ok": False, "message": "viewer_app.py 缺失"})
        python_exe = sys.executable
        if FROZEN:
            # 单文件 exe 的 sys.executable 就是自己，不能拿来跑 .py 脚本
            import shutil as _sh
            python_exe = (_sh.which("python") or _sh.which("python3")
                          or _sh.which("py") or "")
            if not python_exe:
                return web.json_response(
                    {"ok": False, "message": "视频测试窗口需要本机 Python（exe 内不含解释器）"})
        try:
            p = subprocess.Popen(
                [python_exe, vp, path],
                creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                close_fds=True)
            _viewer_proc[0] = p
        except Exception as e:
            return web.json_response({"ok": False, "message": "启动失败: %s" % e})
        LOG._p("信息", "视频测试窗口已启动: %s" % path)
        return web.json_response({"ok": True,
                                  "message": "测试窗口已启动（标题: 测试窗口）"})

    app.router.add_get("/", ui_index)
    app.router.add_get("/config/{name}", ui_config)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_post("/api/cmd", api_cmd)
    app.router.add_post("/api/jserror", api_jserror)
    app.router.add_get("/api/win/geo", api_win_geo)
    app.router.add_post("/api/win/resize", api_win_resize)
    app.router.add_post("/api/win/move", api_win_move)
    app.router.add_post("/api/win/close", api_close)
    app.router.add_post("/api/viewer", api_viewer)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except Exception as e:
        LOG.error("界面端口 %d 被占用或绑定失败: %s" % (port, e))
        raise
    LOG.info("界面服务 http://127.0.0.1:%d/" % port)


# ---------------- 引擎线程（镜像 start.start()） ----------------
def engine_main():
    global _engine
    try:
        ipc.clear_runtime()
        start._ipc_status = ipc.StatusWriter(provider=get_status_merged)
        start._ipc_status.start()
        eng = hsr_dglab.HsrDGLab(start.app)
        start._plugin = eng
        _engine = eng
        eng.start()
        eng.sync_strength_from_dock()
        if not HEADLESS:
            start._start_hud()
            start._start_gamepad()
        threading.Thread(target=strength_sync_loop, daemon=True,
                         name="strength-sync").start()
        LOG.success("惩罚引擎已启动")
        while eng.running:
            time.sleep(0.2)
    except Exception as e:
        LOG.error("引擎异常: %s" % e)


# ---------------- 入口 ----------------
def main():
    global _bridge, _win
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true", help="无桌面窗口")
    ap.add_argument("--port", type=int, default=5865, help="界面端口")
    ap.add_argument("--v4port", type=int, default=9898, help="V4 配对端口")
    ap.add_argument("--width", type=int, default=1000, help="窗口宽度")
    ap.add_argument("--height", type=int, default=880, help="窗口高度")
    args = ap.parse_args()
    global HEADLESS
    HEADLESS = args.headless

    print("=" * 56)
    print(" 崩铁·挨打就电 独立客户端")
    print("=" * 56)

    loop_th = LoopThread()
    loop_th.start()

    # V4 控制器
    _bridge = __import__("v4_backend").V4Bridge(loop_th.loop, port=args.v4port,
                                                log=lambda lv, m: LOG._p(lv, m))
    _bridge.start()

    # App shim（镜像 start.init，但 server 换成 V4Backend）
    disk = start.load_disk_config()
    start.app.server = __import__("v4_backend").V4Backend(
        _bridge, lambda: start.app.config, LOG)
    start.app.config = {"plugins": dict(disk.get("plugins") or {}),
                        "waveform": dict(disk.get("waveform") or {})}
    start.app.waveform = start.app.config["waveform"]
    start.app.log = LOG
    start.app.logger = LOG

    # 动作表：start.py 的 20 个处理器 + V4 扩展
    start._register_handlers()
    start._HANDLERS["get_status"] = lambda params: json.dumps(get_status_merged())
    start._HANDLERS["toggle_top"] = _toggle_top
    ipc.write_meta("hsr_dglab", PLUGIN_DIR)
    cmd = ipc.CmdWatcher(dispatch=client_dispatch)
    cmd.start()

    # 引擎
    threading.Thread(target=engine_main, daemon=True, name="engine").start()

    # UI HTTP
    asyncio.run_coroutine_threadsafe(_ui_server(args.port), loop_th.loop)

    url = "http://127.0.0.1:%d/" % args.port
    try:
        if args.headless:
            LOG.info("headless 模式：浏览器访问 " + url)
            webbrowser.open(url)
            while not _stop_evt.is_set():
                time.sleep(0.5)
        else:
            import webview
            _win = webview.create_window(TITLE, url, width=args.width,
                                         height=args.height,
                                         background_color="#1c1c1c",
                                         frameless=True, easy_drag=False)
            LOG.success("桌面窗口已打开：" + url + "（用 DG-LAB 4 APP 扫 V4 页二维码配对）")
            webview.start()
    except Exception as e:
        LOG.error("桌面窗口启动失败(%s)，改用浏览器" % e)
        webbrowser.open(url)
        while not _stop_evt.is_set():
            time.sleep(0.5)
    finally:
        # 急停兜底：退出前全通道归零
        try:
            _bridge.zero()
            LOG.info("退出：已全通道归零")
        except Exception:
            pass
        try:
            if _engine is not None:
                _engine.stop()
        except Exception:
            pass
        _bridge.stop()


if __name__ == "__main__":
    main()
