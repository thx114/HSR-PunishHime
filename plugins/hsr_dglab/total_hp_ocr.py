"""
total_hp_ocr.py - 货币战争扣血飘字/总血量 OCR（外置 OCR + 挨打就电滤镜）

流程：GDI 小区域截屏 -> 挨打就电式颜色滤镜 -> BMP -> 外置 OCR（Umi-OCR HTTP）
      -> 提取带符号数字

滤镜（移植自挨打就电 image.py 的 parse_colors / color_match / apply_ocr_filter）：
  目标色支持 "#RRGGBB" 十六进制、(r,g,b)、"色1|色2" 多色（与挨打就电格式一致）
  匹配：|r-tr|+|g-tg|+|b-tb| <= tolerance*3，命中 -> 黑，未命中 -> 白（黑字白底）

颜色与近似度均由用户在面板配置（默认 #F78679 / 30）。

坐标一律物理像素：
  - 框选工具（Electron 覆盖层）返回 物理像素 = DIP * scaleFactor
  - 本模块启动时 SetProcessDPIAware()，GDI 截图坐标 = 物理像素

Umi-OCR HTTP API：POST {url}/api/ocr {"base64": ...} -> {"code":100,"data":[{"text":...}]}
"""
import base64
import re
import struct

import requests

import screen_capture as capture

# 默认目标色：货币战争扣血飘字实测（用户校准值 #F78679）
DEFAULT_TARGET_COLORS = [(247, 134, 121)]
DEFAULT_TOLERANCE = 30


def ensure_dpi_aware():
    """兼容入口：截图后端自带 DPI 处理"""
    capture.ensure_dpi_aware()


# ---------------- 颜色解析（移植挨打就电 parse_color / parse_colors） ----------------

def parse_color(color):
    """'#RRGGBB' / [r,g,b] -> (r,g,b)，失败返回 None"""
    if isinstance(color, (list, tuple)):
        if len(color) >= 3:
            try:
                return (int(color[0]), int(color[1]), int(color[2]))
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(color, str):
        c = color.strip()
        if c.startswith("#") and len(c) == 7:
            try:
                return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
            except ValueError:
                return None
        if c.isdigit() and len(c) == 6:
            try:
                return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
            except ValueError:
                return None
    return None


def parse_colors(colors):
    """支持 列表 / 元组 / '#RRGGBB' / '色1|色2' 字符串 -> [(r,g,b), ...]"""
    if not colors:
        return []
    result = []
    if isinstance(colors, list):
        for c in colors:
            if isinstance(c, (list, tuple)):
                p = parse_color(c)
                if p:
                    result.append(p)
            else:
                result.extend(parse_colors(c))
        return result
    if isinstance(colors, str):
        for part in colors.split("|"):
            p = parse_color(part)
            if p:
                result.append(p)
        return result
    p = parse_color(colors)
    return [p] if p else []


def color_match(r, g, b, target_colors, tolerance=30):
    """挨打就电 color_match：曼哈顿色距 <= tolerance*3 命中"""
    threshold = tolerance * 3
    for tr, tg, tb in target_colors:
        if abs(r - tr) + abs(g - tg) + abs(b - tb) <= threshold:
            return True
    return False


try:
    import numpy as _np
except Exception:
    _np = None


def apply_ocr_filter(bgra, width, height, target_colors=None, tolerance=None,
                     stats=None):
    """挨打就电 apply_ocr_filter（feather=0）—— numpy 向量化版

    BGRA 输入，目标色命中 -> 黑(0,0,0)，未命中 -> 白(255,255,255)
    返回 24bit RGB 字节。
    stats 传 dict 时回填 kept/total/bbox（墨迹包围盒，供 OCR 前裁剪提速）。
    numpy 不可用时回退纯 Python 循环（挨打就电同款语义不变）。
    """
    if not target_colors:
        target_colors = DEFAULT_TARGET_COLORS
    if tolerance is None:
        tolerance = DEFAULT_TOLERANCE
    n = width * height
    tcs = [(int(t[0]), int(t[1]), int(t[2])) for t in target_colors]
    th = int(tolerance) * 3

    if _np is not None and n >= 2048:
        arr = _np.frombuffer(bgra[:n * 4], _np.uint8).reshape(n, 4)
        r = arr[:, 2].astype(_np.int16)
        g = arr[:, 1].astype(_np.int16)
        b = arr[:, 0].astype(_np.int16)
        hit = _np.zeros(n, dtype=bool)
        for tr, tg, tb in tcs:
            hit |= (_np.abs(r - tr) + _np.abs(g - tg) + _np.abs(b - tb)) <= th
        m = hit.reshape(height, width)
        # 去噪：8 邻域内命中 < 2 的孤点/细噪剔除（数字笔画保留）
        if m.any():
            padded = _np.zeros((height + 2, width + 2), dtype=bool)
            padded[1:-1, 1:-1] = m
            cnt = _np.zeros((height, width), dtype=_np.uint8)
            for dy in (0, 1, 2):
                for dx in (0, 1, 2):
                    if dy == 1 and dx == 1:
                        continue
                    cnt += padded[dy:dy + height, dx:dx + width]
            m = m & (cnt >= 2)
            hit = m.reshape(-1)
        rgb = _np.full((n, 3), 255, _np.uint8)
        rgb[hit] = 0
        if stats is not None:
            stats["kept"] = int(hit.sum())
            stats["total"] = n
            idx = _np.flatnonzero(hit)
            if idx.size:
                ys, xs = _np.divmod(idx, width)
                stats["bbox"] = (int(xs.min()), int(ys.min()),
                                 int(xs.max()), int(ys.max()))
            else:
                stats["bbox"] = None
        return rgb.tobytes()

    out = bytearray(b"\xff" * (n * 3))  # 白底
    kept = 0
    bx0 = by0 = 1 << 30
    bx1 = by1 = -1
    for i in range(n):
        idx = i * 4
        b = bgra[idx]
        g = bgra[idx + 1]
        r = bgra[idx + 2]
        for tr, tg, tb in tcs:
            if abs(r - tr) + abs(g - tg) + abs(b - tb) <= th:
                j = i * 3
                out[j] = 0
                out[j + 1] = 0
                out[j + 2] = 0
                kept += 1
                x = i % width
                y = i // width
                if x < bx0:
                    bx0 = x
                if x > bx1:
                    bx1 = x
                if y < by0:
                    by0 = y
                if y > by1:
                    by1 = y
                break
    if stats is not None:
        stats["kept"] = kept
        stats["total"] = n
        stats["bbox"] = (bx0, by0, bx1, by1) if kept else None
    return bytes(out)


def _crop_rgb(rgb, width, height, bbox, margin=6):
    """按墨迹包围盒裁剪 RGB 图（四周留 margin 像素）"""
    if not bbox:
        return rgb, width, height
    cx0 = max(0, bbox[0] - margin)
    cy0 = max(0, bbox[1] - margin)
    cx1 = min(width - 1, bbox[2] + margin)
    cy1 = min(height - 1, bbox[3] + margin)
    cw = cx1 - cx0 + 1
    ch = cy1 - cy0 + 1
    if cw * ch >= width * height:
        return rgb, width, height
    row_raw = width * 3
    out = bytearray(cw * ch * 3)
    for row in range(ch):
        s = (cy0 + row) * row_raw + cx0 * 3
        out[row * cw * 3:(row + 1) * cw * 3] = rgb[s:s + cw * 3]
    return bytes(out), cw, ch


def parse_coordinates(coords):
    """挨打就电同款多点坐标解析："x1,y1|x2,y2" / 列表 -> [[x,y],...]"""
    if not coords:
        return []
    if isinstance(coords, list):
        out = []
        for c in coords:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                out.append([int(c[0]), int(c[1])])
            elif isinstance(c, str):
                out.extend(parse_coordinates(c))
        return out
    if isinstance(coords, str):
        result = []
        for group in coords.split('|'):
            group = group.strip()
            if not group:
                continue
            parts = group.replace(' ', '').split(',')
            if len(parts) >= 2:
                try:
                    result.append([int(parts[0]), int(parts[1])])
                except ValueError:
                    continue
        return result
    return []


def count_red_pixels(bgra, w, h, rmin=120, dg=45, db=25):
    """红色数字预筛：扣血瞬间血量数字变红。返回区域内红色像素数。

    用途：OCR 请求（HTTP）是整条链路最贵的一环，没红字说明数字没变化，
    直接跳过请求。规则刻意放宽（R 明显高于 G/B 即算红），能覆盖红色数字
    的发光/渐变动画；对纯色/灰阶窗口 0 命中。
    """
    if _np is None or w <= 0 or h <= 0 or len(bgra) < w * h * 4:
        return 0
    arr = _np.frombuffer(bgra[:w * h * 4], _np.uint8)
    arr = arr.reshape(h, w, 4).astype(_np.int16)
    r = arr[:, :, 2]
    g = arr[:, :, 1]
    b = arr[:, :, 0]
    mask = (r > rmin) & ((r - g) > dg) & ((r - b) > db)
    return int(mask.sum())


def count_color_pixels(bgra, w, h, target_colors, tolerance=10):
    """严格颜色命中计数（红字掉血检测主判定，取代宽松色相预筛）。

    区域内与任一目标色的曼哈顿色距 <= tolerance*3 的像素数。
    与加号/OCR 滤镜同口径（挨打就电 color_match）：只认真实出现的目标色，
    发光/渐变边缘与金色 UI 暗边都不算。实测样图：#EE7A74 近似度 10 圈中
    ~1670 像素（数字本体，聚成数字簇），旧宽松规则同图误抓 10 万+
    （全是金色暗边）。target_colors 为 parse_colors() 输出；
    numpy 不可用时退纯 Python 循环（红区只有几万像素，够快）。
    """
    if not target_colors or w <= 0 or h <= 0 or len(bgra) < w * h * 4:
        return 0
    n = w * h
    threshold = tolerance * 3
    if _np is not None:
        arr = _np.frombuffer(bgra[:n * 4], _np.uint8).reshape(h, w, 4)
        arr = arr[:, :, :3].astype(_np.int16)      # BGRA -> BGR
        dist = None
        for tr, tg, tb in target_colors:
            t = _np.asarray((tb, tg, tr), dtype=_np.int16)
            d = _np.abs(arr - t).sum(axis=2)
            dist = d if dist is None else _np.minimum(dist, d)
        return int((dist <= threshold).sum())
    total = 0
    for i in range(n):
        o = i * 4
        bb = bgra[o]
        gg = bgra[o + 1]
        rr = bgra[o + 2]
        for tr, tg, tb in target_colors:
            if abs(rr - tr) + abs(gg - tg) + abs(bb - tb) <= threshold:
                total += 1
                break
    return total


def check_plus_points(bgra, cw, ch, ox, oy, positions, colors,
                      negative_positions=None, negative_colors=None,
                      tolerance=30, samples=None):
    """加号检测（挨打就电 check_healthbar_exists / check_positions_match 同款）。

    bgra 为一次截图（覆盖全部检测点），cw/ch 其尺寸，ox/oy 为该截图左上角
    对应的窗口客户区坐标。positions 为客户区相对点列表，colors 为颜色列表：
    每个采样点对整组颜色做曼哈顿近似匹配（挨打就电 _np_color_match_pixels
    同口径，tol*3）。正向点用 7x7 patch 匹配（patch 内任一像素命中即算该点
    命中——结算画面整体呼吸/位移动画下单像素命中率不可用，反向点保持
    精确单像素，veto 要准不要宽）；全部正向点命中才算通过（result 正位串
    每位一点，'1'=命中）。
    反向点：任一反向点命中其颜色 -> 直接不通过（反向位串附在 '!' 之后）。
    返回 (ok, result_str)。
    """
    if _np is None or not positions or not colors:
        return False, "0"
    if cw <= 0 or ch <= 0 or len(bgra) < cw * ch * 4:
        return False, "0"
    arr = _np.frombuffer(bgra[:cw * ch * 4], _np.uint8).reshape(ch, cw, 4)

    def match_bits(pos_list, color_list, patch=0, samples=None):
        if not pos_list or not color_list:
            return False, "0"
        tcols = [_np.asarray(c, dtype=_np.int16) for c in color_list]
        bits = []
        count = 0
        for px, py in pos_list:
            x = int(px) - ox
            y = int(py) - oy
            hit = False
            if 0 <= x < cw and 0 <= y < ch:
                if samples is not None:
                    # 诊断用：记录该点中心像素的实际 RGB（给 HUD 显示）
                    samples.append((int(px), int(py),
                                    int(arr[y, x, 2]), int(arr[y, x, 1]),
                                    int(arr[y, x, 0])))
                if patch:
                    # 动画兼容：patch 内任一像素命中即该点命中（结算 UI
                    # 呼吸/位移 ±几 px，单像素命中率在动画画面上不可用）
                    x0 = max(0, x - patch); x1 = min(cw, x + patch + 1)
                    y0 = max(0, y - patch); y1 = min(ch, y + patch + 1)
                    flat = arr[y0:y1, x0:x1, :3].astype(_np.int16)
                    flat = flat.reshape(-1, 3)[:, ::-1]  # BGR->RGB
                    md = _np.full(flat.shape[0], 999999, _np.int32)
                    for tc in tcols:
                        d = (_np.abs(flat[:, 0] - tc[0])
                             + _np.abs(flat[:, 1] - tc[1])
                             + _np.abs(flat[:, 2] - tc[2]))
                        md = _np.minimum(md, d)
                    hit = bool((md <= int(tolerance) * 3).any())
                else:
                    b, g, r = arr[y, x, 0], arr[y, x, 1], arr[y, x, 2]
                    pa = _np.asarray([(int(r), int(g), int(b))],
                                     dtype=_np.int16)
                    md = _np.full(1, 999999, _np.int32)
                    for tc in tcols:
                        d = (_np.abs(pa[:, 0] - tc[0])
                             + _np.abs(pa[:, 1] - tc[1])
                             + _np.abs(pa[:, 2] - tc[2]))
                        md = _np.minimum(md, d)
                    hit = bool((md <= int(tolerance) * 3)[0])
            bits.append('1' if hit else '0')
            if hit:
                count += 1
        return count == len(pos_list), ''.join(bits)

    ok, pos_bits = match_bits(positions, colors, patch=3, samples=samples)
    neg_bits = ""
    if negative_positions and negative_colors:
        _, neg_bits = match_bits(negative_positions, negative_colors)
        if '1' in neg_bits:
            ok = False
    return ok, pos_bits + ('!' + neg_bits if neg_bits else '')


def probe_region(x, y, w, h, target_colors=None, tolerance=None,
                 min_pixels=20, window_title="崩坏：星穹铁道", stats=None):
    """结算画面探测（挨打就电 plus 检测同款思路）。

    框选结算画面中一处常驻特征（如血量图标/标签），每次识别前截取该小区域，
    统计目标色命中像素数：>= min_pixels 视为在结算画面，否则跳过本轮识别。
    坐标与 ocr_region 一致：window_title 窗口客户区相对坐标，自动跟随。
    返回 (ok, kept, total)。
    """
    import ctypes
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w <= 0 or h <= 0:
        raise ValueError("探测区域未配置（先框选结算画面的常驻特征）")
    hwnd = capture.get_game_window(process_title=window_title or "崩坏：星穹铁道")
    if not hwnd:
        raise RuntimeError("未找到窗口「{}」（窗口未开或标题不匹配）".format(window_title))
    if ctypes.windll.user32.IsIconic(hwnd):
        raise RuntimeError("窗口「{}」已最小化——还原窗口后再探测".format(window_title))
    origin_x, origin_y = capture.get_client_offset(hwnd)
    bgra, _, _, rw, rh, _ = capture.capture_screen_region(
        origin_x + x, origin_y + y, w, h)
    st = stats if stats is not None else {}
    apply_ocr_filter(bgra, rw, rh, target_colors, tolerance, stats=st)
    kept = int(st.get("kept", 0))
    return kept >= max(1, int(min_pixels)), kept, int(st.get("total", rw * rh))


def rgb_to_bmp(rgb, width, height):
    """RGB 字节 -> 24bit BMP（自下而上行序），纯 struct"""
    row_raw = width * 3
    pad = (4 - row_raw % 4) % 4
    row = row_raw + pad
    image_size = row * height
    file_size = 14 + 40 + image_size
    out = bytearray()
    out += b"BM"
    out += struct.pack("<IHHI", file_size, 0, 0, 54)
    out += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24,
                       0, image_size, 2835, 2835, 0, 0)
    zero = b"\x00" * pad
    for y in range(height - 1, -1, -1):
        out += rgb[y * row_raw:(y + 1) * row_raw]
        out += zero
    return bytes(out)


def extract_signed(text, keep_sign=False, join_digits=False):
    """OCR 文本 -> (数值, 是否为获得量)

      "-10" / "10" -> (10, False)   扣血（负号被 OCR 丢失也按扣血处理）
      "+2"         -> (2, True)     获得量，忽略
      "挑战进度"    -> (None, False)
    keep_sign=True 时保留原符号（血量池模式：当前血量可为负数）
    join_digits=True 时拼接被空格拆开的数字（"7 9" -> 79，血量池专用；
    飘字模式禁止——两个飘字并排会被错误合并）
    """
    if not text:
        return None, False
    if join_digits:
        text = re.sub(r"(?<=\d)[ ]+(?=\d)", "", text)
        text = re.sub(r"-[ ]+(?=\d)", "-", text)
    is_gain = "+" in text
    # 候选词必须含真实数字，才做 O/o/B/Z 字母纠错（防止 "No" -> "N0" 幻影）
    # 且数字前后不能紧贴字母/数字/等号（排除 base64 / code=101 这类噪音词）
    for mt in re.finditer(r"(?<![A-Za-z0-9=])[+-]?[0-9OoBZ,，]+", text):
        tok = mt.group()
        if not re.search(r"\d", tok):
            continue
        s = tok
        for src, dst in ((",", ""), ("，", ""), ("O", "0"), ("o", "0"),
                         ("B", "8"), ("Z", "2")):
            s = s.replace(src, dst)
        m = re.search(r"-?\d+", s)
        if m:
            try:
                v = int(m.group())
                return (v if keep_sign else abs(v)), is_gain
            except (TypeError, ValueError):
                return None, False
    return None, False


def _extract_texts(res):
    """Umi-OCR 返回结构宽松解析 -> 文本列表"""
    if not isinstance(res, dict):
        return [str(res)]
    data = res.get("data")
    texts = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("text") is not None:
                texts.append(str(item["text"]))
            elif isinstance(item, str):
                texts.append(item)
    elif isinstance(data, str):
        texts.append(data)
    return texts


def _ocr_bgra_request(bgra, w, h, url, target_colors=None, tolerance=None,
                      ocr_model=None, timeout=4.0,
                      stats=None, raw_texts=None, keep_sign=False,
                      join_digits=False):
    """滤镜->墨迹裁剪->Umi 请求->解析（ocr_region 与加号检测共用的尾段）。"""
    _st = stats if stats is not None else {}
    rgb = apply_ocr_filter(bgra, w, h, target_colors, tolerance, stats=_st)
    # 墨迹过少（去噪后 <12px）不可能有数字：跳过 OCR，防噪声幻觉读数
    if _st.get("kept", 0) < 12:
        return None, False
    # 墨迹裁剪：只把数字所在的小块发给 OCR（大区域高频率的关键提速）
    rgb, rw, rh = _crop_rgb(rgb, w, h, (_st or {}).get("bbox"))
    bmp = rgb_to_bmp(rgb, rw, rh)
    body = {"base64": base64.b64encode(bmp).decode("ascii")}
    # OCR 参数与挨打就电 ocr_api_data 完全一致：
    #   英文/数字模型（仅数字效果）+ tbpu.parser=none 忽略排版 + 纯文本输出
    body["options"] = {
        "ocr.language": str(ocr_model) if ocr_model else "models/config_en.txt",
        "ocr.cls": False,
        "ocr.limit_side_len": 960,
        "tbpu.parser": "none",
        "data.format": "text",
    }
    res = requests.post(url.rstrip("/") + "/api/ocr", json=body, timeout=timeout)
    rj = res.json()
    # Umi-OCR: code 100=成功；101=图内无文本；其余=失败。非 100 不当读数
    if isinstance(rj, dict) and rj.get("code") not in (None, 100):
        if raw_texts is not None:
            raw_texts.append("code=%s" % rj.get("code"))
        return None, False
    texts = _extract_texts(rj)
    if raw_texts is not None:
        raw_texts.extend(texts)
    for t in texts:
        tl = t.lower()
        if "no text" in tl or "path:" in tl:
            continue  # Umi 无文本/错误文案，不当读数
        value, is_gain = extract_signed(t, keep_sign=keep_sign,
                                        join_digits=join_digits)
        if value is not None:
            return value, is_gain
    return None, False


def ocr_region(url, x, y, w, h, target_colors=None, tolerance=None,
               ocr_model=None, timeout=4.0, window_title="崩坏：星穹铁道",
               stats=None, raw_texts=None, keep_sign=False, join_digits=False):
    """截取物理像素区域并 OCR（挨打就电同款：坐标永远相对窗口客户区）。

    (x, y) 为「window_title 对应窗口」客户区相对坐标，每次截图实时取
    窗口当前位置——窗口移动自动跟随（挨打就电唯一行为，无开关）。
    窗口未找到 -> RuntimeError（调用方按轮次跳过并提示）。
    stats 传 dict 回填滤镜 kept/total；raw_texts 传 list 回填识别原文。
    keep_sign=True 保留符号（血量池模式，血量可为负）。
    返回 (number, is_gain)：失败 (None, False)
    """
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w <= 0 or h <= 0:
        raise ValueError("OCR 区域未配置（先用框选工具选定）")
    hwnd = capture.get_game_window(process_title=window_title or "崩坏：星穹铁道")
    if not hwnd:
        raise RuntimeError("未找到窗口「{}」（窗口未开或标题不匹配）".format(window_title))
    import ctypes as _ct
    if _ct.windll.user32.IsIconic(hwnd):
        raise RuntimeError("窗口「{}」已最小化——还原窗口后再识别".format(window_title))
    origin_x, origin_y = capture.get_client_offset(hwnd)
    bgra, _, _, rw, rh, _ = capture.capture_screen_region(
        origin_x + x, origin_y + y, w, h)
    return _ocr_bgra_request(bgra, rw, rh, url, target_colors, tolerance,
                             ocr_model, timeout, stats, raw_texts,
                             keep_sign, join_digits)
