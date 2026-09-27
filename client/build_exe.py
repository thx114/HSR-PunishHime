# -*- coding: utf-8 -*-
"""build_exe.py - 把客户端打包成单文件 exe

两种形态:
    默认       全内置：插件全部 py + ui_winui.html + config.json 种子打进 exe，
               exe 旁边只留 崩铁客户端.config.json（唯一读写配置）
    --onedir   目录版（启动更快，体积分散）

用法:
    python client\build_exe.py            # 默认单文件
    python client\build_exe.py --onedir   # 目录版

产物:
    <惩罚姬根目录>\崩铁客户端.exe

外部文件:
    崩铁客户端.config.json   唯一配置（首次启动自动从包内种子生成）
    v4_pair.json             固定配对存档（tid）
    screenshots\ log\       截图与日志
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 惩罚姬根目录
LIB = os.path.join(ROOT, "lib")
PLUGIN = os.path.join(ROOT, "plugins", "hsr_dglab")

# 插件与客户端模块全部打进 exe；静态 import 链 PyInstaller 自动跟随，
# 这里只需声明入口与运行时动态 import 的第三方库
HIDDEN = [
    # 客户端自身的模块（client\ 目录）：必须打进 exe
    "v4_backend",
    # 插件入口（其静态 import 链会带上 hsr_dglab/ipc/ui_page/hit_logic/
    # modules.*/source_*/sio_client/capture 等）
    "start",
    # 运行时动态 import 的第三方库
    "requests", "numpy", "sounddevice", "keyboard", "webview", "qrcode",
    "qrcode.image.svg", "qrcode.image.pil", "qrcode.image.base", "qrcode.util",
    "tkinter", "tkinter.ttk", "tkinter.messagebox",
    "aiohttp", "aiohttp.web", "multidict", "yarl", "frozenlist", "aiosignal",
    "attr", "attrs", "cffi",
]
COLLECT_DATA = ["sounddevice", "webview"]

# 打进包里的插件数据文件（目标 "." -> _MEIPASS 根，__file__ 相对读取直接命中）
ADD_DATA = [
    os.path.join(PLUGIN, "ui_winui.html") + ";.",
    os.path.join(PLUGIN, "config.json") + ";.",
]

# 环境里装了一堆与客户端无关的重型包（torch/pandas…），
# 不排除的话会被 hook 链拖进来，exe 直接膨胀到几个 GB。
EXCLUDE = [
    "torch", "torchvision", "torchaudio", "tensorflow", "pandas", "pyarrow",
    "scipy", "sklearn", "matplotlib", "sympy", "IPython", "notebook", "jupyter",
    "sqlalchemy", "pytest", "sphinx", "numba", "llvmlite", "cv2", "PIL",
    "pytesseract", "onnx", "transformers", "datasets", "pygame", "wx",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onedir", action="store_true", help="目录版而不是单文件")
    ap.add_argument("--name", default="崩铁客户端")
    args = ap.parse_args()

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", os.path.join(ROOT, "dist"),
           "--workpath", os.path.join(ROOT, "temp", "pyi-build"),
           "--specpath", os.path.join(ROOT, "temp", "pyi-spec"),
           "--paths", LIB,
           "--paths", PLUGIN,
           "--name", args.name]
    cmd.append("--onedir" if args.onedir else "--onefile")
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for d in COLLECT_DATA:
        cmd += ["--collect-data", d]
    for d in ADD_DATA:
        cmd += ["--add-data", d]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    cmd.append(os.path.join(HERE, "client.py"))

    print("执行:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        print("打包失败 rc=%d" % rc)
        return rc

    src = os.path.join(ROOT, "dist", args.name + ("" if args.onedir else ".exe"))
    dst = os.path.join(ROOT, args.name + ".exe")
    if os.path.isfile(src):
        shutil.copy2(src, dst)
        print("\n已生成:", dst, "(%.1f MB)" % (os.path.getsize(dst) / 1048576.0))
    else:
        print("\n产物在:", os.path.join(ROOT, "dist", args.name))
    print("外部配置: 崩铁客户端.config.json（唯一读写目标，不存在则自动从包内种子生成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
