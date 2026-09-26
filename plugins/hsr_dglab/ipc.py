"""
ipc.py - 插件引擎 <-> 配置页 文件桥（%TEMP%/hsr_dglab_ipc/）

当前 server 版未开放插件级 HTTP 接口（POST /plugin/{name}/{action} 一律 405、
/plugin/config 一律 400"没有该插件"，官方 create_ui 模板同样失效）。
配置页运行在 Electron（nodeIntegration 可直接 fs 读写），因此用文件桥：

  meta.json         引擎初始化时写入 {plugin_name, plugin_dir}（常驻）
  status.json       引擎运行中每 0.5s 覆写实时快照（含 ipc_ts 心跳）
  cmd.json          页面写入 {id, action, params}，引擎取走即删
  cmd_result.json   引擎回写 {id, ok, message, value}

页面判定插件状态：status.json 存在且 ipc_ts 距今 < 3s。
"""
import json
import os
import tempfile
import threading
import time

IPC_DIR = os.path.join(tempfile.gettempdir(), "hsr_dglab_ipc")

# 状态文件心跳过期秒数（页面同款判定）
HEARTBEAT_EXPIRE = 3.0


def _path(name):
    return os.path.join(IPC_DIR, name)


def _read(name):
    try:
        with open(_path(name), encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _write(name, obj):
    """原子写入；os.replace 与页面读取句柄相撞时短暂重试（Windows 共享冲突）"""
    last = None
    for attempt in range(8):
        try:
            os.makedirs(IPC_DIR, exist_ok=True)
            tmp = _path("{}.{}.{}.tmp".format(name, os.getpid(),
                                              threading.get_ident()))
            # 千万不能用 utf-8-sig：BOM 会让页面 JSON.parse 直接抛异常
            # （页面 readJson 不剥 BOM），表现为页面永远“引擎不可达”。
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, _path(name))
            return True
        except Exception as e:
            last = e
            time.sleep(0.03)
    try:
        print("[ipc] write {} failed: {}".format(name, last), flush=True)
    except Exception:
        pass
    return False


def write_meta(plugin_name, plugin_dir):
    _write("meta.json", {"plugin_name": plugin_name, "plugin_dir": plugin_dir})


def read_status():
    return _read("status.json")


def read_result():
    return _read("cmd_result.json")


def send_cmd(action, params=None, timeout=6.0):
    """引擎侧自调用入口（当前无页面时也可直接调用）"""
    cmd = {"id": "{}-{}".format(int(time.time() * 1000), action)}
    _write("cmd.json", {"id": cmd["id"], "action": action, "params": params or {}})
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = read_result()
        if r and r.get("id") == cmd["id"]:
            return r
        time.sleep(0.1)
    return {"id": cmd["id"], "ok": False, "message": "引擎无响应"}


class CmdWatcher(threading.Thread):
    """轮询 cmd.json -> dispatch(action, params) -> 回写 cmd_result.json

    派发在独立工作线程中执行：框选/取色等阻塞型动作（可达 2 分钟）
    绝不卡住轮询线程，避免“一次卡住、之后所有命令全部无效”。
    """

    #: 命令从写入到被派发的最长等待：超龄命令视为页面已超时放弃，直接丢弃
    CMD_MAX_AGE = 20.0

    def __init__(self, dispatch, interval=0.15):
        super().__init__(daemon=True)
        self.dispatch = dispatch
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def _poll_once(self):
        cmd_path = _path("cmd.json")
        if not os.path.exists(cmd_path):
            return
        # 原子认领：rename 为本进程专属文件（多消费者场景下只有一个能抢到）
        claim = _path("cmd_claim_{}.json".format(os.getpid()))
        try:
            os.replace(cmd_path, claim)
        except Exception:
            return  # 被其他消费者抢走 / 正在写入
        cmd = None
        age = 0.0
        try:
            age = time.time() - os.path.getmtime(claim)
            cmd = _read(os.path.basename(claim))
        except Exception:
            pass
        try:
            os.remove(claim)
        except Exception:
            pass
        if not cmd or cmd.get("id") is None:
            return
        if age > self.CMD_MAX_AGE:
            return  # 过期命令：页面早已超时放弃，不再派发（防幽灵弹出）
        threading.Thread(target=self._dispatch_async, args=(cmd,),
                         daemon=True).start()

    def _dispatch_async(self, cmd):
        result = {"id": cmd.get("id"), "ok": False,
                  "message": "", "value": None}
        try:
            out = self.dispatch(str(cmd.get("action")),
                                cmd.get("params") or {})
            had_ok = False
            if isinstance(out, dict):
                result.update(out)
                had_ok = "ok" in out
            elif isinstance(out, str):
                try:
                    parsed = json.loads(out)
                    if isinstance(parsed, dict):
                        result.update(parsed)
                        had_ok = "ok" in parsed
                    else:
                        result["message"] = out
                except Exception:
                    result["message"] = out
            if not had_ok:
                result["ok"] = True   # handler 未显式给 ok 视为成功
        except Exception as e:
            result["message"] = str(e)
        _write("cmd_result.json", result)

    def stop(self):
        self._stop.set()


class StatusWriter(threading.Thread):
    """周期性覆写 status.json（页面心跳判定插件是否在跑）"""

    def __init__(self, provider, interval=0.5):
        super().__init__(daemon=True)
        self.provider = provider
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = self.provider()
                if isinstance(snap, str):
                    try:
                        snap = json.loads(snap)
                    except Exception:
                        snap = {}
                snap = dict(snap or {})
                snap["ipc_ts"] = time.time()
                _write("status.json", snap)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


def clear_runtime():
    """停机清理：状态与命令文件（meta.json 保留供页面定位插件目录）"""
    for name in ("status.json", "cmd.json", "cmd_result.json"):
        try:
            os.remove(_path(name))
        except Exception:
            pass