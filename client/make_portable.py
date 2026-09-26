# -*- coding: utf-8 -*-
"""make_portable.py - 生成便携版目录（可直接压缩发给别人）

产出自包含：exe 已内置全部运行库，外面只需要 plugins/（插件源码与界面）：
    dist\\崩铁客户端-便携版\\
        崩铁客户端.exe           单文件客户端（含 aiohttp/webview/qrcode/numpy 等）
        崩铁客户端.config.json   波形与参数（随包带上当前调校）
        使用说明.txt
        放行防火墙.bat           手机连不上时用（管理员运行）
        plugins\\hsr_dglab\\     插件源码 + 界面模板 + 配置种子

用法: python client\\make_portable.py
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "dist", "崩铁客户端-便携版")
EXE = os.path.join(ROOT, "崩铁客户端.exe")
CFG = os.path.join(ROOT, "崩铁客户端.config.json")
PLUG = os.path.join(ROOT, "plugins", "hsr_dglab")

# 便携包里不需要的：配置备份、开发/测试脚本、设计参考、运行期产物
SKIP_NAMES = {"config.waveform.bak.json", "test_mock.py", "test_replay.py",
              "v4_mock_app.py", "rs.html", "v4_frames.log"}

README = """崩铁·挨打就电 · 便携版
========================================
【怎么跑】
1) 把整个文件夹解压到任意普通目录（不要放 C:\\Program Files，程序要往自己旁边写文件）
2) 双击  崩铁客户端.exe
3) 手机 DG-LAB 4 APP → 扫界面里 V4 页的二维码完成配对
   · 手机和电脑必须在同一个局域网（同一个 WiFi）
   · APP 连不上时：右键「放行防火墙.bat」→ 以管理员身份运行，再重扫

【文件说明（都在这堆文件里，可随时改）】
  崩铁客户端.config.json   波形 + 全部惩罚参数（界面「保存配置」也写这里）
  v4_pair.json             配对存档；删掉会重新生成新二维码（首次运行自动创建）
  screenshots\\            截图产物（诊断截图 / 截图游戏窗口 / 区域框选）
  v4_frames.log            V4 通信日志，排查用（每次启动清空）
  plugins\\hsr_dglab\\     插件本体（界面就是这里的 ui_winui.html）

【运行环境】
  · Windows 10/11 64 位；需要 WebView2 运行时（Win10/11 一般自带，
    缺失时程序会自动改用默认浏览器打开界面）
  · 不需要装 Python（已打包成单个 exe）
  · 可选：Umi-OCR 在 http://127.0.0.1:1395 提供「货币战争总血量数值」；
    没装也能用——掉血由红字判定，只是总血量数值读不到
  · 「视频测试窗口」功能需要本机有 Python（便携版不含解释器）

【常见问题】
  · 界面打不开：可能 5865 端口被占用，命令行加 --port 5866
  · 想换配对端口：命令行加 --v4port 9899，记得同步放行该端口
  · 无窗口运行：崩铁客户端.exe --headless --port 5865 --v4port 9898
"""

BAT = """@echo off
chcp 936 >nul
net session >nul 2>&1
if errorlevel 1 (
  echo [!] 请右键本文件，选择“以管理员身份运行”
  pause
  exit /b 1
)
netsh advfirewall firewall delete rule name="崩铁客户端 V4配对" >nul 2>&1
netsh advfirewall firewall add rule name="崩铁客户端 V4配对" dir=in action=allow protocol=TCP localport=9898 >nul
echo [OK] 已放行 TCP 9898（V4 配对端口，手机 APP 需要它）
echo      如果改了 --v4port，请把端口号换成对应值。
pause
"""


def ignore(dirname, names):
    out = []
    for n in names:
        # 排除缓存、我的配置备份（config.json 之外的 config.json.*）与测试脚本
        if (n == "__pycache__" or n in SKIP_NAMES
                or (n.startswith("config.json.") and n != "config.json")):
            out.append(n)
    return out


def main():
    if not os.path.isfile(EXE):
        print("缺少 %s，先跑 python client\\build_exe.py" % EXE)
        return 1
    shutil.rmtree(PKG, ignore_errors=True)
    os.makedirs(PKG)

    shutil.copy2(EXE, PKG)
    if os.path.isfile(CFG):
        shutil.copy2(CFG, PKG)
    else:
        print("提示：没有 %s，首次运行会从 plugins 的 config.json 自动生成" % CFG)

    shutil.copytree(PLUG, os.path.join(PKG, "plugins", "hsr_dglab"), ignore=ignore)
    # lib/ 不再随包：那是原版 Python 3.11 的二进制，本 exe 用不到，
    # 而且 exe 已内置 aiohttp/keyboard/sounddevice/cffi 等全部依赖（实测无 lib 正常）

    with open(os.path.join(PKG, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README)
    with open(os.path.join(PKG, "放行防火墙.bat"), "w", encoding="gbk") as f:
        f.write(BAT)

    total = sum(os.path.getsize(os.path.join(r, x))
                for r, _, fs in os.walk(PKG) for x in fs)
    print("便携目录: %s" % PKG)
    print("文件数 %d，合计 %.1f MB" % (
        sum(len(fs) for _, _, fs in os.walk(PKG)), total / 1048576.0))
    for line in sorted(os.listdir(PKG)):
        print("   ", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
