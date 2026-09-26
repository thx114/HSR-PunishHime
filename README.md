# PunishHime（惩罚姬）

**Take damage, get zapped.** — A DG-Lab Coyote E-stim punishment companion for *Honkai: Star Rail*.

崩坏：星穹铁道「挨打就电」惩罚客户端。程序盯着你的血条，你被打一下，郊狼（DG-Lab Coyote）就电你一下——强度、波形、叠加、持续电全部可调。

> **PunishHime** = Punish + 姬（Hime）。中文圈叫「惩罚姬」，英文就叫 PunishHime。

---

## ⚠️ Safety First / 安全声明（必读）

- **18+ only.** This tool controls a real E-stim device. Adults, of sound mind, **with explicit consent of the person being shocked**.
- **E-stim safety**: start from low intensity; never run current across the chest or head; do NOT use if you have a pacemaker, heart condition, epilepsy, or are pregnant.
- Use at your own risk. The authors take no responsibility for injury or damaged relationships with your HP bar.

- **仅限 18 岁以上、自愿、清醒的场景使用**，被电的人必须知情同意。
- 电刺激安全底线：强度从低开始；电流绝对不要经过胸腔和头部；心脏起搏器 / 心脏疾病 / 癫痫 / 孕期禁止使用。
- 风险自担。作者不对任何人身伤害负责。

---

## What it does

PunishHime watches the game and translates HP loss into E-stim punishment through the official **DG-Lab 4** phone app (V4 WebSocket protocol — pair by scanning a QR code).

Included: a standalone **Honkai: Star Rail client** (single-file exe) plus a plugin engine with two damage pipelines:

| Module | Data source | What it punishes |
|---|---|---|
| Battle (常规战斗) | veritas memory hook | character HP/shield loss, knockdown |
| Currency Wars (货币战争) | screen capture + OCR | total HP loss, via red floating-text detection |

Damage pipeline: threshold → damage-ratio bonus → consecutive-hit stacking → low-HP factor → strength/waveform → optional sustained shock.

### Highlights

- **Strict red-text detection** — the damage flash is matched against a configurable target color (default `#EE7A74`) with a Manhattan tolerance, so gold UI edges never trigger a false positive. Fully configurable: target color(s), tolerance, minimum pixel count.
- **Per-context parameter isolation** — Currency Wars reads its own `cw_*` parameters; in-battle bonuses never leak into it.
- **HP factor** — strength scales up as your total HP drops.
- **Overlap stacking with decay** — consecutive hits within a window stack up (configurable add / cap / window / decay), Currency Wars keeps its own independent stack.
- **Sustained shock mode**, per-scene waveforms, battle-end behaviors.
- **WinUI-style web UI** served locally; desktop window via pywebview, or headless for servers/SSH.
- **Bluetooth branch** for direct-coyote setups, HUD overlay, gamepad guard, region pickers.

## Getting started

### Download (recommended)

Grab the portable zip from Releases, unzip anywhere (not `C:\Program Files`), run `崩铁客户端.exe`, scan the QR in the UI with the DG-Lab 4 app. Same Wi-Fi as the phone. Windows 10/11 x64 + WebView2.

Optional: run [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) at `127.0.0.1:1395` for total-HP numbers; damage detection works without it (red-text only).

### Build from source

    pip install -r requirements.txt
    python client\build_exe.py        # PyInstaller -> 崩铁客户端.exe
    python client\make_portable.py    # -> dist\崩铁客户端-便携版（zip-ready）

Run tests:

    cd plugins\hsr_dglab
    python test_mock.py               # 21 checks, no device needed

## Repo layout

    client/                     standalone HSR client (aiohttp + pywebview + V4 bridge)
      client.py                 entry: HTTP server, UI window, plugin host
      v4_backend.py             dockdglab-compatible facade for the engine
      build_exe.py / make_portable.py
    plugins/hsr_dglab/          the punishment engine (plugin)
      hsr_dglab.py              engine host: wiring, strength dispatch, HUD
      hit_logic.py              scoped-config strength formulas (prefix readers)
      modules/
        battle.py               HP/shield/knockdown pipeline (veritas)
        currency_wars.py        OCR pipeline: pool values, red-text strict matching
      total_hp_ocr.py           capture filter, color matcher, OCR client
      v4ctrl.py / dockdglab.py  DG-Lab V4 controller / facade
      ui_winui.html / ui_page.py  config UI
      test_mock.py              end-to-end mock tests (no device)

## Configuration cheat-sheet (Currency Wars)

| Key | Meaning |
|---|---|
| `cw_red_colors` | target color(s) of the damage flash, `#RRGGBB` sep by `|` |
| `cw_red_tolerance` | Manhattan tolerance (pixel matches within `tol×3`) |
| `cw_red_min` | minimum matching pixels to count as one hit |
| `cw_red_interval` / `cw_red_cooldown` | detection cadence / debounce |
| `cw_total_strength_a/b` | base strength |
| `cw_hp_factor` / `cw_hp_ref` | low-HP strength scaling |
| `cw_overlap_*` | consecutive-hit stacking (independent from battle module) |

Everything is editable in the web UI (货币战争 page) and saved to `崩铁客户端.config.json`.

## Credits

- Formula & UI concepts ported from the **挨打就电** plugin.
- [veritas](https://github.com/hessiser/veritas) by hessiser — memory hook data source.
- [DG-Lab](https://www.dg-lab.com/) Coyote + V4 protocol, [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR).

## License

[MIT](LICENSE) — but the safety rules above are not optional.
