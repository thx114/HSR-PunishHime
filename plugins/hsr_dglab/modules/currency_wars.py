"""
modules/currency_wars.py - 模块：货币战争（总血量）

只负责"玩家总血量"线（货币战争血池，非角色血量）：
  扣血 -> 当前血量系数 + 多次掉血叠加 -> 电击 -> 持续电击（时长/保底强度/结束清波可配）

加成只用货币战争自己的两个系数，不借用局内（角色战斗）的任何系数：
  1) 当前血量系数  cw_hp_factor_*   乘在基础强度上（血量越低越强）
  2) 多次掉血叠加  cw_overlap_*     机制同局内"连续叠加"，但参数与状态完全独立

数据输入（二选一）：
  A. ocr（默认，外置 Umi-OCR）：
     - cw_ocr_mode=delta：识别红色扣血飘字（如 -10），读到即本次掉血量；
       "+"开头的获得量自动忽略；同一数字 cw_delta_dedup 秒内不重复触发
     - cw_ocr_mode=pool：识别总血量数值，与上次差值作为掉血量
     - 颜色/近似度（cw_ocr_colors / cw_ocr_tolerance）面板可配
掉血判定只看红字（扣血瞬间数字变红），总血量另走低频 OCR 只更新数值。
"""
import ctypes
import threading
import time

import events as E
from modules.base import ModuleBase

try:
    import total_hp_ocr
except Exception:  # 非 Windows 环境仅影响 OCR
    total_hp_ocr = None


class CurrencyWarsModule(ModuleBase):
    name = "cw"  # module_cw_enabled

    def __init__(self, engine):
        super().__init__(engine)
        self.total_hp = None          # pool 模式的上次总血量
        self.total_hits = 0
        self.total_lost = 0.0
        self.last_delta = None        # 最近一次识别的扣血量（面板显示）
        self._ocr_thread = None
        self._ocr_stop = threading.Event()
        self._last_ocr_err_ts = 0.0
        # 飘字去重：同一数值在窗口内只触发一次
        self._delta_dedup = {}        # value -> first_seen_ts
        self._ocr_pending = None      # (value, ts) 待确认读数（连续两帧同值才触发）
        self._ocr_static = {}         # value -> {count, bbox} 静态文字跟踪
        self._static_warn_ts = 0.0
        self._pool_seen = {}          # value -> [ts] 血量读数稳定性跟踪
        self._pool_last_ts = 0.0      # 最近一次采纳血量读数的时间
        # 延迟统计：加号=相邻两次检测的时间戳差；OCR=距上次 OCR 发起的时间
        # （被加号拦截时不执行 OCR，该值持续上涨，直观暴露拦截/断流）
        self._last_ocr_ts = None
        self._last_plus_ts = None
        self._lat_plus_ms = 0
        self._loop_alive_ts = 0.0
        # 加号滚动多数闸（动画兼容）：最近 10 轮检测的命中环
        self._plus_ring = []
        self._plus_last_ts = 0.0
        self._plus_open = False
        # 红色数字预筛（扣血瞬间变红）：只有红字才值得发 OCR 请求
        self._red_gate = False
        self._red_min = 25
        self._last_red = 0
        # 严格颜色匹配（取代宽松色相预筛）：目标色 + 近似度
        self._red_colors = []          # [(r,g,b), ...] 空 = 回落旧宽松判定
        self._red_tolerance = 10
        self._red_mode = "hue"         # "color" | "hue"（快照展示用）
        # 掉血检测（红字，零 OCR）与低频总血量 OCR
        self._red_hit_amount = 1.0
        self._red_cooldown = 0.6
        self._red_hit_ts = 0.0
        self._red_hits = 0
        self._hp_ocr_interval = 0.4
        # 固定节拍：加号检测 / 红字检测 / 总血量 OCR
        self._plus_interval = 1.0
        self._red_interval = 0.03
        self._last_plus_check_ts = 0.0
        self._last_red_check_ts = 0.0
        self._last_plus_samples = []
        self._last_plus_bits = ""
        # 红字检测区域（独立于识别区域；0 = 沿用识别区域）
        self._red_x = 0
        self._red_y = 0
        self._red_w = 0
        self._red_h = 0
        # 目标窗口是否找到（实时状态页显示）
        self._win_found = False

    # ---------- 数据输入 ----------
    @property
    def input_source(self):
        # 只保留 OCR 路径（veritas_uid 实体方案已移除），无论旧配置写了什么
        return "ocr"

    @property
    def ocr_mode(self):
        return str(self.engine._cfg("cw_ocr_mode", "delta") or "delta").lower()

    def start(self):
        self._ocr_stop.clear()
        self._delta_dedup.clear()
        self._ocr_pending = None
        self._ocr_static.clear()
        self._pool_seen.clear()
        self._pool_last_ts = 0.0
        self._plus_ring = []
        self._plus_last_ts = 0.0
        self._plus_open = False
        if self.input_source == "ocr" and self.engine._b("cw_ocr_enabled", False):
            self._ocr_thread = threading.Thread(target=self._ocr_loop,
                                                name="cw-ocr", daemon=True)
            self._ocr_thread.start()
            self.log("info", "OCR 轮询已启动，模式: " + self.ocr_mode)
        else:
            self.log("info", "总血量数据输入未启用")

    def stop(self):
        self._ocr_stop.set()

    def on_event(self, event, payload):
        """货币战争只走 OCR 路径，veritas 事件不参与判血。"""
        return

    # ---------- OCR 路径 ----------
    class _ProbeSkip(Exception):
        """探测闸未通过：当前不在结算画面，跳过本轮识别"""

    def _ocr_loop(self):
        e = self.engine
        if total_hp_ocr is None:
            self.log("error", "total_hp_ocr 不可用")
            return
        while not self._ocr_stop.is_set() and e.running:
            self._loop_alive_ts = time.time()
            try:
                url = str(e._cfg("cw_ocr_url", "http://127.0.0.1:1395") or "")
                # 必须 int 化：_f() 返回 float，下游加号分支裁剪用 range(h)
                # 和切片索引，float 会直接 TypeError('float' object cannot be
                # interpreted as an integer)，导致加号闸一通过就 OCR 失败
                x = int(e._f("cw_ocr_x", 0))
                y = int(e._f("cw_ocr_y", 0))
                w = int(e._f("cw_ocr_w", 0))
                h = int(e._f("cw_ocr_h", 0))
                # 红字检测区域（可单独框选；w/h<=0 表示沿用识别区域）
                self._red_x = int(e._f("cw_red_x", 0))
                self._red_y = int(e._f("cw_red_y", 0))
                self._red_w = int(e._f("cw_red_w", 0))
                self._red_h = int(e._f("cw_red_h", 0))
                # 红色数字预筛（扣血瞬间数字变红）：没红字就不发 Umi 请求
                self._red_gate = e._b("cw_red_gate", False)
                self._red_min = int(e._f("cw_red_min", 25))
                # 红字严格颜色匹配：区域内真出现目标色（近似度 tol*3）并数够
                # cw_red_min 个才算掉血；目标色留空回落旧宽松色相判定
                self._red_colors = total_hp_ocr.parse_colors(
                    str(e._cfg("cw_red_colors", "") or ""))
                self._red_tolerance = max(1, int(e._f("cw_red_tolerance", 10)))
                # 掉血检测（红字直接触发，不经过 OCR）；总血量 OCR 低频
                self._red_hit_amount = e._f("cw_red_hit_amount", 1)
                self._red_cooldown = e._f("cw_red_cooldown", 0.6)
                self._hp_ocr_interval = max(0.05, e._f("cw_hp_ocr_interval", 0.4))
                self._plus_interval = max(0.05, e._f("cw_plus_interval", 1.0))
                self._red_interval = max(0.005, e._f("cw_red_interval", 0.03))
                colors = total_hp_ocr.parse_colors(
                    e._cfg("cw_ocr_colors", "#F78679") or "#F78679")
                tolerance = e._f("cw_ocr_tolerance", 30)
                model = str(e._cfg("cw_ocr_model", "") or "").strip() or None
                title = str(e._cfg("cw_ocr_window_title", "")
                            or "崩坏：星穹铁道")
                # 加号检测（check_healthbar_exists 同款）：多点采样、每点对
                # 整组颜色曼哈顿近似（正向点 9x9 patch 匹配，动画兼容）。
                # 单轮全中不可靠（结算画面整体呼吸动画），由下方滚动多数闸
                # 裁决（最近 10 轮 >=3 中放行）；反向点保持精确单像素 veto。
                # 与识别共用一次截图。
                plus_pts = total_hp_ocr.parse_coordinates(
                    e._cfg("cw_plus_positions", "") or "") \
                    if e._b("cw_plus_enabled", False) else []
                neg_pts = total_hp_ocr.parse_coordinates(
                    e._cfg("cw_plus_negative_positions", "") or "")
                # 加号闸 = 「现在是不是结算画面」，兼防其他窗口误识别。它要截
                # 覆盖全部点位的大图（实测 11ms），所以固定低频跑；闸门没开时
                # 红字检测和血量 OCR 都不执行。
                now = time.time()
                # 闸门活性：只有「最近 3 秒内真的跑过加号检测」时闸门才算数。
                # 找不到窗口/流程卡住时，加号检测压根不会执行（异常发生在检测
                # 之前），没有这条闸门就会一直挂着上一次的 true —— HUD 于是在
                # 别的画面上显示 加号:true。
                if self._plus_open and (now - (self._plus_last_ts or 0)) > 3.0:
                    self._plus_open = False
                    self._plus_ring.clear()
                # ---- 加号检测：固定节拍 cw_plus_interval（无快慢档、无复查）----
                # 开闸 = 最近 10 次检测里 >=3 次命中（滚动多数闸，动画兼容）。
                # 节拍越大开闸越慢（1s -> 约 3s），想快就把间隔调小。
                _plus_due = ((now - (self._last_plus_check_ts or 0))
                             >= self._plus_interval)
                value = None
                if plus_pts and _plus_due:
                    st = {}
                    import screen_capture as _sc
                    hwnd = _sc.get_game_window(process_title=title)
                    self._win_found = bool(hwnd)
                    if not hwnd:
                        raise RuntimeError("未找到窗口「{}」".format(title))
                    if ctypes.windll.user32.IsIconic(hwnd):
                        raise RuntimeError("窗口「{}」已最小化".format(title))
                    origin_x, origin_y = _sc.get_client_offset(hwnd)
                    pcolors = total_hp_ocr.parse_colors(
                        e._cfg("cw_plus_colors", "") or "")
                    neg_colors = total_hp_ocr.parse_colors(
                        e._cfg("cw_plus_negative_colors", "") or "")
                    ptol = e._f("cw_plus_tolerance", 30)
                    # 覆盖矩形：全部检测点 ∪ 识别区域（外扩 6px，客户区内截断）
                    # 坐标 int 化：x/y/w/h 来自 _f() 为 float，直接进 GDI 会炸
                    all_pts = plus_pts + (neg_pts if neg_colors else [])
                    bx = int(max(0, min([p[0] for p in all_pts] + [x]) - 6))
                    by = int(max(0, min([p[1] for p in all_pts] + [y]) - 6))
                    bw = int((max([p[0] for p in all_pts] + [x + w]) + 6) - bx)
                    bh = int((max([p[1] for p in all_pts] + [y + h]) + 6) - by)
                    bgra, _, _, bw, bh, _ = _sc.capture_screen_region(
                        origin_x + bx, origin_y + by, bw, bh)
                    _smp = []
                    ok, bits = total_hp_ocr.check_plus_points(
                        bgra, bw, bh, bx, by, plus_pts, pcolors,
                        neg_pts if neg_colors else None, neg_colors or None,
                        ptol, samples=_smp)
                    # 诊断用：本轮每个正向点实际取到的 RGB + 判定串
                    self._last_plus_samples = _smp
                    self._last_plus_bits = bits
                    # 动画兼容滚动多数闸：最近 10 轮 >=3 中视为结算画面。
                    # 【关键】ring 必须裁剪成最近 10 个：只 append 不裁剪的话
                    # sum>=3 一旦攒够就永久为真（换画面/退游戏都不会再关闸）。
                    # 另加「最近 3 轮至少 1 中」：画面一切走，最多 3 轮就关闸，
                    # 不会被历史命中拖住。
                    now = time.time()
                    if now - (self._plus_last_ts or 0) > 3:
                        self._plus_ring.clear()
                    if self._plus_last_ts:
                        self._lat_plus_ms = int((now - self._plus_last_ts) * 1000)
                    self._plus_last_ts = now
                    self._last_plus_check_ts = now
                    self._plus_ring.append(1 if ok else 0)
                    if len(self._plus_ring) > 10:
                        del self._plus_ring[:-10]
                    self._plus_open = (sum(self._plus_ring) >= 3
                                       and sum(self._plus_ring[-3:]) >= 1)
                    if not self._plus_open:
                        if now - self._last_ocr_err_ts > 10:
                            self._last_ocr_err_ts = now
                            self.log("debug",
                                     "加号检测未通过 {}（{}/10），跳过识别"
                                     .format(bits, sum(self._plus_ring)))
                if plus_pts and not self._plus_open:
                    # 闸门没开（不在结算画面）：红字检测与血量 OCR 都不跑
                    raise self._ProbeSkip()
                # ---- 过闸后的截图：一次覆盖「识别区域 ∪ 红字区域」，各自裁剪 ----
                # 红字区域可单独框选（cw_red_x/y/w/h）；没配置就沿用识别区域
                if self._red_w > 0 and self._red_h > 0:
                    rx, ry, rw2, rh2 = (self._red_x, self._red_y,
                                        self._red_w, self._red_h)
                else:
                    rx, ry, rw2, rh2 = x, y, w, h
                _now1 = time.time()
                # 红字检测：固定节拍 cw_red_interval（只在过闸后跑）
                _need_red = (self._red_gate
                             and (_now1 - (self._last_red_check_ts or 0))
                             >= self._red_interval)
                # 总血量 OCR：固定节拍 cw_hp_ocr_interval（也只在过闸后跑）
                _need_ocr = ((_now1 - (self._last_ocr_ts or 0))
                             >= self._hp_ocr_interval)
                if not _need_red and not _need_ocr:
                    raise self._ProbeSkip()
                st2 = {}
                import screen_capture as _sc_r
                _hwnd_r = _sc_r.get_game_window(process_title=title)
                self._win_found = bool(_hwnd_r)
                if not _hwnd_r:
                    raise RuntimeError("未找到窗口「{}」".format(title))
                if ctypes.windll.user32.IsIconic(_hwnd_r):
                    raise RuntimeError("窗口「{}」已最小化".format(title))
                _ox_r, _oy_r = _sc_r.get_client_offset(_hwnd_r)
                _ux, _uy = min(x, rx), min(y, ry)
                _uw = max(x + w, rx + rw2) - _ux
                _uh = max(y + h, ry + rh2) - _uy
                _ub, _, _, _uw, _uh, _ = _sc_r.capture_screen_region(
                    _ox_r + _ux, _oy_r + _uy, _uw, _uh)
                if _need_red:
                    self._last_red_check_ts = _now1
                    # 掉血检测：红字直接触发，零 OCR
                    _rsub = self._crop_bgra(_ub, _uw, rx - _ux, ry - _uy,
                                            rw2, rh2)
                    if self._red_colors:
                        # 严格模式：区域内数目标色像素（曼哈顿 <= tol*3），
                        # 数够 cw_red_min 才算；近似度由面板配置
                        self._red_mode = "color"
                        self._last_red = total_hp_ocr.count_color_pixels(
                            _rsub, rw2, rh2, self._red_colors,
                            self._red_tolerance)
                    else:
                        self._red_mode = "hue"
                        self._last_red = total_hp_ocr.count_red_pixels(
                            _rsub, rw2, rh2)
                    if self._last_red >= self._red_min:
                        self._on_red_hit()
                if _need_ocr:
                    self._last_ocr_ts = _now1
                    _osub = self._crop_bgra(_ub, _uw, x - _ux, y - _uy, w, h)
                    value, is_gain = total_hp_ocr._ocr_bgra_request(
                        _osub, w, h, url, colors, tolerance, model,
                        stats=st2, keep_sign=True, join_digits=True)
                if value is not None:
                    # 飘字方案（delta）已停用：连续确认 + 碎片抑制也压不住飘字
                    # 叠加误读（210）与常驻 UI 幻影（1-3 X -> 1）。当前走血量差值。
                    # if self.ocr_mode == "delta":
                    #     self._on_ocr_delta(value, is_gain, st.get("bbox"))
                    # else:
                    # 红字检测开启 -> OCR 只更新总血量数值，掉血由红字判定
                    self._pool_accept(value, detect=not self._red_gate)
            except self._ProbeSkip:
                pass  # 非结算画面：静默跳过本轮
            except Exception as ex:
                now = time.time()
                if now - self._last_ocr_err_ts > 10:
                    self._last_ocr_err_ts = now
                    self.log("warning", "OCR 失败: " + str(ex)[:120]
                             + "（确认 Umi-OCR 已启动/区域已框选保存）")
            # 循环节拍：红字检测开着按红字间隔，否则按总血量 OCR 间隔
            interval = (self._red_interval if self._red_gate
                        else self._hp_ocr_interval)
            deadline = time.time() + interval
            while time.time() < deadline and not self._ocr_stop.is_set():
                time.sleep(0.1)

    @staticmethod
    def _crop_bgra(bgra, stride_w, cx, cy, cw2, ch2):
        """从一张宽 stride_w 的 BGRA 大图里裁出 (cx,cy,cw2,ch2) 小图。"""
        row = stride_w * 4
        return b"".join(
            bgra[(cy + i) * row + cx * 4:(cy + i) * row + (cx + cw2) * 4]
            for i in range(ch2))

    def _on_ocr_delta(self, value, is_gain, bbox=None):
        """飘字模式：识别到的数字即本次扣血量

        触发前四道闸：
          1. 获得量（+N）忽略；0 值无意义直接丢弃
          2. 碎片抑制：已触发数值的消散残影子串（-10 残影 -> 1）
          3. 连续两帧确认：瞬态误读（两数字叠读成 210）不触发
          4. 静态拒识：bbox 纹丝不动的重复读数是常驻 UI 文字，自动忽略
        """
        if is_gain:
            self.engine._log("debug", "[货币战争] 识别到获得量 +{}，忽略".format(value))
            return
        if value == 0:
            return
        now = time.time()
        window = max(0.5, self.engine._f("cw_delta_dedup", 2.0))
        sv = str(value)
        # 2. 碎片抑制（对已触发值）
        for other, ts in list(self._delta_dedup.items()):
            if now - ts < window and len(str(other)) > len(sv) and sv in str(other):
                self.engine._log("debug",
                                 "[货币战争] 飘字 {} 是已触发 {} 的消散碎片，跳过".format(value, other))
                self._ocr_pending = None
                return
        # 3. 静态文字拒识（每次读数都计数；真飘字持续上移，bbox 纹丝不动的是常驻文字）
        if bbox is not None:
            entry = self._ocr_static.setdefault(value, {"count": 0, "bbox": None})
            b = entry["bbox"]
            if (b is not None and abs(bbox[0] - b[0]) < 8 and abs(bbox[1] - b[1]) < 8
                    and abs(bbox[2] - b[2]) < 8 and abs(bbox[3] - b[3]) < 8):
                entry["count"] += 1
            else:
                entry["count"] = 1
                entry["bbox"] = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
            if entry["count"] >= 3:
                if now - self._static_warn_ts > 30:
                    self._static_warn_ts = now
                    self.log("warning",
                             "读数 {} 连续多次位置不变——框选区域疑似包含常驻 UI 文字，"
                             "已自动忽略；请缩小/移动框选区域避开固定文字".format(value))
                self.engine._log("debug", "[货币战争] 静态文字 {} 忽略".format(value))
                self._ocr_pending = None
                return
        # 4. 连续 N 帧确认（cw_ocr_confirm_frames，默认 3）：瞬态误读（两数字
        #    叠读成 210）和单帧噪声都活不过确认；真飘字寿命约 1s（5Hz 下 5 帧）
        need = max(1, int(self.engine._f("cw_ocr_confirm_frames", 3)))
        if self._ocr_pending is not None and self._ocr_pending[0] == value                 and now - self._ocr_pending[2] <= 0.6:
            self._ocr_pending = (value, self._ocr_pending[1] + 1, now)
        else:
            self._ocr_pending = (value, 1, now)
        if self._ocr_pending[1] < need:
            self.engine._log("debug", "[货币战争] 读数 {} 确认 {}/{}".format(
                value, self._ocr_pending[1], need))
            return
        self._ocr_pending = None
        # 去重：同一数值窗口内不重复触发（飘字会停留约 1s，OCR 高频采样）
        first = self._delta_dedup.get(value)
        if first is not None and now - first < window:
            self.engine._log("debug", "[货币战争] 飘字 {} 在去重窗口内，跳过".format(value))
            return
        # 清理过期记录
        for k in list(self._delta_dedup.keys()):
            if now - self._delta_dedup[k] >= window:
                del self._delta_dedup[k]
        self._delta_dedup[value] = now
        self._on_damage(float(value), "ocr")

    def _pool_accept(self, value, detect=True):
        """血量读数稳定性闸：0.6s 内同值出现 >=2 次才采纳。

        detect=True 时读数差值判定掉血（旧行为，红字检测关闭时用）；
        detect=False 时只更新总血量数值，掉血判定交给红字检测（OCR 不参与）。

        血量数字变化时有弹跳/闪烁动画，动画帧会读出单帧垃圾
        （如 '1Q'、'19'）；真值会连续稳定出现多次。采纳后清空该值
        记录——同一读数不会重复进入差值。
        """
        now = time.time()
        # 窗口随采样间隔自适应（0.6s 在低采样率下永远凑不齐两次）
        win = max(1.2, self.engine._f("cw_ocr_interval", 0.5) * 3)
        lst = [ts for ts in self._pool_seen.get(value, []) if now - ts <= win]
        lst.append(now)
        self._pool_seen[value] = lst
        for k in list(self._pool_seen.keys()):
            if k != value:
                old = [ts for ts in self._pool_seen[k] if now - ts <= win]
                if old:
                    self._pool_seen[k] = old
                else:
                    del self._pool_seen[k]
        if len(lst) < 2:
            self.engine._log("debug", "[货币战争] 读数 {} 首见，等待复现".format(value))
            return
        self._pool_seen[value] = []
        # 前缀碎片防御：新值是当前血量的数字前缀（少一位）且刚采纳过 ->
        # 动画帧丢位误读（69 读成 6），丢弃；1.5s 后过期，真掉到该值会由
        # 后续稳定读数确认。
        if self.total_hp is not None:
            sp, sc = str(int(value)), str(int(self.total_hp))
            if (len(sp) < len(sc) and sc.startswith(sp)
                    and now - self._pool_last_ts <= 1.5):
                self.engine._log("debug",
                                 "[货币战争] 读数 {} 疑似 {} 的丢位误读，丢弃"
                                 .format(value, sc))
                return
        self._pool_last_ts = now
        self._on_pool_value(float(value), detect)

    def _on_pool_value(self, value, detect=True):
        """数值模式：与上次差值为掉血量（当前血量可为负数）

        血量满值 100、可为负；单次读数跳变超过 300 视为误读丢弃
        （真掉血最大也就 ~200：100 -> -100 一刀）。
        """
        e = self.engine
        old = self.total_hp
        if old is not None and abs(value) > 999:
            e._log("debug", "[货币战争] 读数 {} 超出合理范围，丢弃".format(value))
            return
        self.total_hp = value
        if old is None:
            e._log("info", "[货币战争] 总血量基准: {:.0f}".format(value))
            return
        if abs(value - old) > 300:
            e._log("debug",
                   "[货币战争] 血量 {} -> {} 跳变异常，疑似误读，丢弃".format(old, value))
            self.total_hp = old
            return
        if value >= old:
            return
        if not detect:
            # 掉血判定归红字检测：OCR 只负责总血量数值（绝不参与掉血判定）
            e._log("debug",
                   "[货币战争] 总血量 {} -> {}（OCR 只更新数值）".format(old, value))
            return
        self._on_damage(old - value, "ocr")

    def _on_red_hit(self):
        """红字命中 = 掉血事件（完全不经过 OCR）

        红字闪烁期间会连续命中很多轮，用 cw_red_cooldown 去抖，
        保证一次闪红只算一次掉血。等效掉血量由 cw_red_hit_amount 给出
        （用于阈值与强度加成），真实伤害值不依赖 OCR。
        """
        now = time.time()
        if now - (self._red_hit_ts or 0) < self._red_cooldown:
            return
        self._red_hit_ts = now
        self._red_hits += 1
        self._on_damage(self._red_hit_amount, "红字")

    # ---------- 惩罚管线 ----------
    def _hp_factor(self):
        """当前血量系数：血量越低惩罚越强（货币战争自己的系数）

        k = 1 + cw_hp_factor * 缺失比例
        缺失比例 = clamp((cw_hp_ref - 当前总血量) / cw_hp_ref, 0, 1)

        满血（>= 参考血量）时 k = 1；血量归零时 k = 1 + cw_hp_factor。
        """
        e = self.engine
        if not e._b("cw_hp_factor_enabled", True):
            return 1.0
        hp = self.total_hp
        ref = e._f("cw_hp_ref", 100.0)
        if hp is None or ref <= 0:
            return 1.0
        miss = (ref - float(hp)) / ref
        miss = max(0.0, min(1.0, miss))
        return 1.0 + e._f("cw_hp_factor", 0.5) * miss

    def _on_damage(self, lost, src_label):
        e = self.engine
        thr = e._f("cw_total_drop_threshold", 1)
        if not e.drop_threshold_ok(thr, lost):
            e._log("debug", "[货币战争] 扣血 {:.0f} 未达阈值 {:.0f}，跳过".format(lost, thr))
            return
        self.total_hits += 1
        self.total_lost += lost
        self.last_delta = round(lost, 1)
        e._last_hit = {"name": "货币战争总血量", "lost": round(lost, 1), "ts": time.time()}
        # 加成只有两项，全部是货币战争自己的：
        #   1) 当前血量系数 k  乘在基础强度上（血量越低越强）
        #   2) 多次掉血叠加    引擎按 cw_overlap_* 累加（scope="cw"）
        # 局内的受伤加成(damage_*) / 叠加(overlap_*) / 频率 / 多角色 / 轮次 一律不参与
        k = self._hp_factor()
        base_a = e._f("cw_total_strength_a", 20) * k
        base_b = e._f("cw_total_strength_b", 20) * k
        label = "💀 货币战争 | 总血量 -{:.0f} | {}（血量系数 x{:.2f}）".format(
            lost, src_label, k)
        e.trigger("cw_total_pulse", base_a, base_b, 0, lost, label, scope="cw")
        if e._b("cw_sustain_enabled", True):
            e.sustain("cw_total_pulse",
                      e._f("cw_sustain_duration", 3.0),
                      e._f("cw_sustain_strength", 0),
                      e._b("cw_sustain_stop_after", True))

    # ---------- 状态 ----------
    def snapshot(self):
        return {
            "input_source": self.input_source,
            "win_found": bool(self._win_found),
            "ocr_mode": self.ocr_mode,
            "ocr_enabled": self.engine._b("cw_ocr_enabled", False),
            "total_hp": self.total_hp,
            "last_delta": self.last_delta,
            "total_hits": self.total_hits,
            "total_lost": round(self.total_lost, 1),
            "sustain_active": self.engine.sustain_active(),
            "hp_factor": round(self._hp_factor(), 3),
            "overlap_accum": round(self.engine.overlap_cw.accumulated, 1),
            "lat_ocr_ms": (int((time.time() - self._last_ocr_ts) * 1000)
                           if self._last_ocr_ts else 0),
            "lat_plus_ms": self._lat_plus_ms,
            "lat_ts": self._loop_alive_ts,
            "plus_gate": self._plus_open,
            "plus_hits": sum(self._plus_ring),
            "red_gate": self._red_gate,
            "red_count": self._last_red,
            "red_mode": self._red_mode,
            "gate_ok": bool(self._plus_open),
            "plus_bits": self._last_plus_bits,
            "plus_samples": [list(s) for s in (self._last_plus_samples or [])],
            "red_hits": self._red_hits,
            "hp_ocr_interval": self._hp_ocr_interval,
            "plus_interval": self._plus_interval,
            "red_interval": self._red_interval,
        }
