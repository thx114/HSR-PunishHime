"""
source_ocr.py - 数据源 B：OCR 截图路线（占位，待实现）

目标：不依赖 veritas，通过截图 + OCR 识别 4 个角色血量数字，
发同样的标准事件给 core，惩罚逻辑与 veritas 路线完全共用。

预期实现（后续版本）：
1. 定时截图
   - 复用"挨打就电"插件的 capture.py / capture_dxgi.py（GDI/DXGI 抓屏）
2. 定位血量区域
   - 崩铁战斗界面右下 4 个角色血条数字区域（按分辨率做配置）
3. OCR 识别（Umi-OCR http 接口，端口默认 1395）
   - 复用挨打就电的 ocr.py：crop_image_for_ocr / ocr_recognize_number
4. 帧间对比得到掉血量，发标准事件：
   - EVT_HP_CHANGE  {"uid": i, "hp": 数值, "name": "1号位"}
   - EVT_BATTLE_BEGIN / EVT_BATTLE_END（战斗 UI 出现/消失判断）
   - 倒地判断：血量归零 → EVT_DEFEATED

坑位提示（来自 veritas 调研，OCR 路线必须自行处理）：
- 开大招演出会隐藏 HUD，此时 OCR 全部失效 → 需要冻结最近值、演出后重同步
- 4 个角色血量数字小且密集 → 需要按分辨率标定区域 + 数字颜色过滤
- 阵容人数变化 → 每帧检测可见血条数量，动态增删 uid
"""
import events as E  # noqa: F401  实现时使用


class OcrSource:
    name = "ocr"

    def __init__(self, emit, cfg, log):
        self._emit = emit
        self._cfg = cfg
        self._log = log

    def start(self):
        self._log(
            "warning",
            "[ocr] OCR 数据源尚未实现（占位）。请将数据源切回 veritas，"
            "或等待后续版本提供 OCR 支持。",
        )

    def stop(self):
        pass
