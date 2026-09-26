# -*- coding: utf-8 -*-
"""build_exe.py - 把客户端打包成单文件 exe（配置/插件/库全部留在 exe 外面）

用法:
    python client\build_exe.py            # 默认单文件
    python client\build_exe.py --onedir   # 目录版（启动更快，体积分散）

产物:
    <惩罚姬根目录>\崩铁客户端.exe

外部文件（不会被塞进 exe，随时可改）:
    plugins\              插件 + 波形配置 config.json
    client\v4_pair.json   固定配对存档（tid）
    lib\                  运行库
    data\ log\            数据与日志
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 惩罚姬根目录
LIB = os.path.join(ROOT, "lib")

# 外部插件是运行时动态 import 的，PyInstaller 静态分析看不到，必须显式声明
HIDDEN = [
    # 客户端自身的模块（client\ 目录）：便携包里不带 client\，必须打进 exe
    "v4_backend",
    # 外部插件是运行时动态 import 的，静态分析看不到，必须显式声明
    "requests", "numpy", "sounddevice", "keyboard", "webview", "qrcode",
    "qrcode.image.svg", "qrcode.image.pil", "qrcode.image.base", "qrcode.util",
    "tkinter", "tkinter.ttk", "tkinter.messagebox",
    "aiohttp", "aiohttp.web", "multidict", "yarl", "frozenlist", "aiosignal",
    "attr", "attrs", "cffi",
]
COLLECT_DATA = ["sounddevice", "webview"]

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
           "--name", args.name]
    cmd.append("--onedir" if args.onedir else "--onefile")
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for d in COLLECT_DATA:
        cmd += ["--collect-data", d]
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
    print("外部配置保持不变: plugins\\ client\\lib\\ data\\ log\\")
    return 0


if __name__ == "__main__":
    sys.exit(main())
