"""
ui_page.py - 插件配置页面（多页签 SPA：实时 / 常规战斗 / 货币战争 / OCR 识别 / 波形）

运行环境：惩罚姬 Electron 窗口（nodeIntegration，可直接 fs 读写）。
本 server 版未开放插件级 HTTP 接口（POST /plugin/{name}/{action} 405、
/plugin/config 400），因此页面与引擎通过 ipc.py 文件桥通信：

  配置读取  插件目录 config.json（meta.json 指路）+ 内置 DEFAULTS 兜底
  配置保存  引擎在线走命令桥（热更新+写盘），离线直写 config.json
  实时状态  status.json 心跳（引擎 0.5s 覆写，3s 判离线）
  页面动作  cmd.json / cmd_result.json

注入安全：动态内容一律 json.dumps + "</" -> "<\\/"，防止闭合标签截断脚本。
"""
import json
import os


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>崩铁</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:"Microsoft YaHei",sans-serif;}
:root{--bg:#0d1117;--card:#161d27;--card2:#1c2530;--bd:#263241;--text:#dce3ec;
      --dim:#8a97a8;--ac:#4f8cff;--ok:#2ecc71;--warn:#f5a623;--bad:#ff5d5d;}
html,body{height:100%;}
body{background:var(--bg);color:var(--text);display:flex;flex-direction:column;
     overflow:hidden;-webkit-app-region:drag;}
button,input,select,textarea,main,footer,nav{ -webkit-app-region:no-drag; }
.winbtn{position:fixed;top:12px;right:14px;width:22px;height:22px;border-radius:50%;
        background:#ef4444;border:none;cursor:pointer;z-index:99;-webkit-app-region:no-drag;}
header{display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--card);
       border-bottom:1px solid var(--bd);flex-wrap:wrap;}
header .t{font-weight:700;font-size:15px;}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid var(--bd);color:var(--dim);}
.badge.on{color:#0d1117;background:var(--ok);border-color:var(--ok);font-weight:600;}
.badge.off{color:var(--bad);border-color:var(--bad);}
.badge.battle{color:#0d1117;background:var(--warn);border-color:var(--warn);font-weight:600;}
.topwrap{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim);}
.topwrap input{width:16px;height:16px;accent-color:var(--ac);cursor:pointer;}
nav.tabs{display:flex;gap:4px;padding:8px 16px 0;background:var(--card);border-bottom:1px solid var(--bd);}
nav.tabs .tab{height:34px;padding:0 16px;border:1px solid var(--bd);border-bottom:none;
              border-radius:10px 10px 0 0;background:transparent;color:var(--dim);
              cursor:pointer;font-size:13px;}
nav.tabs .tab.active{background:var(--bg);color:var(--text);font-weight:600;border-color:var(--bd);}
main{flex:1;overflow-y:auto;padding:14px 16px;}
.page{display:none;}
.page.active{display:block;}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;margin-bottom:12px;}
.card h3{font-size:12px;color:var(--dim);font-weight:600;margin-bottom:10px;letter-spacing:2px;}
.avatar{margin-bottom:10px;}
.avatar .row1{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;}
.avatar .nm{font-weight:600;}
.avatar .hp{color:var(--dim);font-size:12px;font-family:Consolas,monospace;}
.bar{height:10px;background:#0a0e13;border-radius:6px;overflow:hidden;border:1px solid var(--bd);}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#2ecc71,#27ae60);
       border-radius:6px;transition:width .25s;}
.bar i.mid{background:linear-gradient(90deg,#f5a623,#e67e22);}
.bar i.low{background:linear-gradient(90deg,#ff5d5d,#e74c3c);}
.bar i.sh{background:linear-gradient(90deg,#4fc3f7,#29b6f6);}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;}
.stat{flex:1;min-width:96px;background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:8px 10px;}
.stat .k{font-size:11px;color:var(--dim);}
.stat .v{font-size:16px;font-weight:700;margin-top:2px;font-family:Consolas,monospace;}
.stat .v.hot{color:var(--warn);}
.controls{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;}
.btn{height:32px;padding:0 14px;border-radius:8px;border:1px solid var(--bd);
     background:var(--card2);color:var(--text);cursor:pointer;font-size:13px;}
.btn.primary{background:var(--ac);border-color:var(--ac);color:#fff;font-weight:600;}
.btn.danger{border-color:var(--bad);color:var(--bad);}
.btn:hover{filter:brightness(1.2);}
.fold{border:1px solid var(--bd);border-radius:12px;margin-bottom:12px;background:var(--card);overflow:hidden;}
.fold>.head{padding:10px 14px;cursor:pointer;font-size:13px;color:var(--dim);
            display:flex;justify-content:space-between;user-select:none;}
.fold>.body{display:none;padding:6px 14px 14px;}
.fold.open>.body{display:block;}
.fi{display:flex;align-items:center;gap:10px;margin:9px 0;}
.fi label{flex:0 0 200px;font-size:13px;}
.fi input[type=text],.fi input[type=number],.fi select,.fi textarea{flex:1;background:#0a0e13;
    border:1px solid var(--bd);border-radius:8px;color:var(--text);height:30px;padding:0 10px;
    font-size:13px;outline:none;min-width:0;}
.fi textarea{height:auto;min-height:56px;padding:8px 10px;resize:vertical;font-family:Consolas,monospace;}
.fi input:focus,.fi select:focus,.fi textarea:focus{border-color:var(--ac);}
.fi .chk{flex:0 0 auto;width:18px;height:18px;accent-color:var(--ac);}
.duo{display:flex;gap:8px;flex:1;min-width:0;}
.duo input{flex:1;min-width:0;}
.duo .cell{display:flex;flex-direction:column;gap:2px;flex:1;min-width:0;}
.duo .cell .mini{font-size:11px;color:var(--dim);}
.duo .cell input{width:100%;}
.c70{flex:7 !important;} .c30{flex:3 !important;}
.regrow{display:flex;align-items:center;gap:6px;flex:1;min-width:0;flex-wrap:wrap;}
.regrow .pt{display:flex;align-items:center;background:#0a0e13;border:1px solid var(--bd);
            border-radius:8px;height:30px;padding:0 4px;flex:1;min-width:120px;}
.regrow .pt .brk{color:var(--dim);font-family:Consolas,monospace;font-size:13px;padding:0 2px;}
.regrow .pt input{flex:1;background:transparent;border:none;color:var(--text);height:28px;
                  padding:0 4px;font-size:13px;outline:none;min-width:60px;font-family:Consolas,monospace;}
footer{display:flex;gap:10px;align-items:center;padding:10px 16px;background:var(--card);
       border-top:1px solid var(--bd);}
#hint{font-size:12px;color:var(--dim);margin-left:auto;}
.offline{color:var(--dim);font-size:13px;padding:18px 0;text-align:center;}
.feed{flex:1;min-height:260px;overflow:auto;background:#0d1117;color:#9fe8a2;
  font:12px/1.55 Consolas,monospace;padding:10px;border-radius:8px;
  border:1px solid var(--bd);white-space:pre-wrap;word-break:break-all;margin:0;}
</style>
</head>
<body>
<button class="winbtn" id="winClose"></button>
<header>
  <span class="t">崩铁</span>
  <span class="badge off" id="bVeritas">veritas 未连接</span>
  <span class="badge" id="bBattle">未战斗</span>
  <span class="badge" id="bSource">战斗输入: -</span>
  <span class="badge" id="bCw">货币战争 待机</span>
</header>
<nav class="tabs">
  <button class="tab active" data-page="live">实时状态</button>
  <button class="tab" data-page="battle">常规战斗</button>
  <button class="tab" data-page="cw">货币战争</button>
  <button class="tab" data-page="ocr">OCR 识别</button>
<button class="tab" data-page="veritas">veritas</button>
  <button class="tab" data-page="wave">波形配置</button>
</nav>
<main>
  <div class="page active" id="pg-live">
    <div class="card">
      <h3>游戏内显示与控制</h3>
      <div class="fi"><label>悬浮窗 HUD</label>
        <input type="checkbox" class="chk" data-key="hud_enabled"></div>
      <div class="fi"><label>手柄控制</label>
        <input type="checkbox" class="chk" data-key="gamepad_enabled"></div>
      <div class="fi"><label></label>
        <div style="display:flex;gap:8px;flex:1;">
          <button class="btn" id="btnPause">暂停/恢复惩罚输出</button>
        </div></div>
    </div>
    <div class="card">
      <h3>识别目标窗口</h3>
      <div class="fi"><label>游戏窗口标题</label><input type="text" data-key="cw_ocr_window_title"></div>
      <div class="fi"><label></label>
        <div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">OCR / 加号 / 红字都从这个窗口截图。<span id="winState">窗口状态：-</span></div></div>
    </div>
    <div class="card">
      <h3>战斗阵容与血量</h3>
      <div id="avatars"><div class="offline">等待插件启动…</div></div>
    </div>
    <div class="card">
      <h3>常规战斗统计</h3>
      <div class="stats">
        <div class="stat"><div class="k">挨打次数</div><div class="v" id="sHit">-</div></div>
        <div class="stat"><div class="k">盾量挨打</div><div class="v" id="sShieldHit">-</div></div>
        <div class="stat"><div class="k">累计掉血</div><div class="v" id="sLost">-</div></div>
        <div class="stat"><div class="k">倒地次数</div><div class="v" id="sKnock">-</div></div>
        <div class="stat"><div class="k">叠加强度</div><div class="v" id="sOvl">-</div></div>
        <div class="stat"><div class="k">当前强度 A/B</div><div class="v" id="sStr">-</div></div>
      </div>
      <div style="margin-top:8px;font-size:12px;color:var(--dim);" id="lastHit">&nbsp;</div>
      <div class="controls">
        <button class="btn primary" id="btnShock">测试电击</button>
        <button class="btn" id="btnShockCw">测试货币波形</button>
        <button class="btn" id="btnDown">强度 −1</button>
        <button class="btn" id="btnUp">强度 +1</button>
        <button class="btn danger" id="btnClear">强度清零</button>
        <button class="btn" id="btnConn">测试连接</button>
      </div>
    </div>
  </div>

  <div class="page" id="pg-battle">
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>模块与数据输入</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>模块: 常规战斗</label><input type="checkbox" class="chk" data-key="module_battle_enabled"></div>
        <div class="fi"><label>战斗模块数据输入</label>
          <select data-key="battle_data_source"><option value="veritas">veritas · 角色血量盾量惩罚</option>
            <option value="ocr">OCR · 暂未实现</option><option value="both">两者</option></select></div>
        <div class="fi"><label>veritas 地址</label><input type="text" data-key="veritas_url"></div>
        <div class="fi"><label></label>
          <div style="color:var(--dim);font-size:12px;flex:1;">角色血量、盾量下降经 veritas 检测并按常规战斗规则电击，货币战争战斗内同样生效。</div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>强度公式</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>公式模式</label>
          <select data-key="dmg_mode"><option value="ratio">ratio · 按掉血占血量上限比例</option>
            <option value="legacy">legacy · 挨打就电绝对掉血</option></select></div>
        <div class="fi"><label>伤害系数</label><input type="number" data-key="dmg_factor" step="0.1"></div>
        <div class="fi"><label>强度阈值</label><input type="number" data-key="dmg_threshold" step="0.5"></div>
        <div class="fi"><label>阈值压缩器</label>
          <select data-key="dmg_threshold_mode"><option value="sqrt">sqrt · 平方根</option>
            <option value="log">log · 对数</option><option value="linear">linear · 线性</option></select></div>
        <div class="fi"><label>向上压缩器</label><input type="number" data-key="dmg_upper_cap" step="5"></div>
        <div class="fi"><label>频率压缩器</label><input type="number" data-key="freq_damp" step="0.05"></div>
        <div class="fi"><label>多角色系数</label><input type="number" data-key="multi_factor" step="0.1"></div>
        <div class="fi"><label>多角色窗口秒</label><input type="number" data-key="multi_window" step="0.5"></div>
        <div class="fi"><label>轮次系数</label><input type="number" data-key="cycle_factor" step="0.05"></div>
        <div class="fi"><label>残血阈值 %</label><input type="number" data-key="low_hp_threshold" step="5"></div>
        <div class="fi"><label>残血强度系数</label><input type="number" data-key="low_hp_factor" step="0.1"></div>
        <div class="fi"><label>残血人数系数</label><input type="number" data-key="low_hp_count_factor" step="0.05"></div>
        <div class="fi"><label>残血持续电</label><input type="checkbox" class="chk" data-key="low_sustain_enabled"></div>
        <div class="fi"><label>持续电基础强度</label><input type="number" data-key="low_sustain_strength" step="1"></div>
        <div class="fi"><label>持续电每残血加成</label><input type="number" data-key="low_sustain_step" step="1"></div>
        <div class="fi"><label></label>
          <div style="color:var(--dim);font-size:12px;flex:1;">加成 = 伤害系数 × 掉血占比，经阈值、压缩、残血、多角色、频率、轮次调制后叠加到基础强度；多角色挨打时波形按角色轮转相位区分。</div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>强度与阈值</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>基础强度 A / B</label>
          <div class="duo">
            <div class="cell"><span class="mini">强度A</span><input type="number" data-key="strength_a"></div>
            <div class="cell"><span class="mini">强度B</span><input type="number" data-key="strength_b"></div>
          </div></div>
        <div class="fi"><label>血量掉落阈值</label><input type="number" data-key="health_drop_threshold"></div>
        <div class="fi"><label>单次最小掉血</label><input type="number" data-key="hit_min_percent"></div>
        <div class="fi"><label>触发间隔（秒）</label><input type="number" data-key="trigger_interval" step="0.05"></div>
        <div class="fi"><label>掉血聚合窗口（秒）</label><input type="number" data-key="hit_aggregate_window" step="0.05"></div>
        <div class="fi"><label>忽略角色</label><input type="text" data-key="ignore_names"></div>
      </div>
    </div>
    <div class="fold">
      <div class="head" onclick="tog(this)"><span>盾量惩罚</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>盾量惩罚</label><input type="checkbox" class="chk" data-key="shield_punish_enabled"></div>
        <div class="fi"><label>盾量强度 A / B</label>
          <div class="duo">
            <div class="cell"><span class="mini">强度A</span><input type="number" data-key="shield_strength_a"></div>
            <div class="cell"><span class="mini">强度B</span><input type="number" data-key="shield_strength_b"></div>
          </div></div>
        <div class="fi"><label>盾量掉落阈值</label><input type="number" data-key="shield_drop_threshold"></div>
        <div class="fi"><label>有盾时阻止血量惩罚</label><input type="checkbox" class="chk" data-key="shield_blocks_health"></div>
      </div>
    </div>
    <div class="fold">
      <div class="head" onclick="tog(this)"><span>受伤加成</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用受伤加成</label><input type="checkbox" class="chk" data-key="damage_enabled"></div>
        <div class="fi"><label>中等伤害参考值</label><input type="number" data-key="damage_mid_value"></div>
        <div class="fi"><label>最大加成强度</label><input type="number" data-key="damage_max_bonus"></div>
        <div class="fi"><label>加成公式</label>
          <select data-key="damage_formula"><option>default</option><option>script</option></select></div>
        <div class="fi"><label>加成脚本</label><textarea data-key="damage_script"></textarea></div>
      </div>
    </div>
    <div class="fold">
      <div class="head" onclick="tog(this)"><span>连续叠加</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用连续叠加</label><input type="checkbox" class="chk" data-key="overlap_enabled"></div>
        <div class="fi"><label>每次叠加强度</label><input type="number" data-key="overlap_strength_add"></div>
        <div class="fi"><label>强度上限</label><input type="number" data-key="overlap_strength_max"></div>
        <div class="fi"><label>叠加窗口倍率</label><input type="number" data-key="overlap_duration_multiplier" step="0.1"></div>
        <div class="fi"><label>进战斗清空叠加</label><input type="checkbox" class="chk" data-key="reset_overlap_on_battle"></div>
        <div class="fi"><label>启用自然回落</label><input type="checkbox" class="chk" data-key="overlap_decay_enabled"></div>
        <div class="fi"><label>回落方式</label>
          <select data-key="overlap_decay_mode"><option>instant</option><option>linear</option>
            <option>percent</option><option>ratio_accel</option><option>script</option></select></div>
        <div class="fi"><label>线性回落速度</label><input type="number" data-key="overlap_decay_value"></div>
        <div class="fi"><label>百分比回落</label><input type="number" data-key="overlap_decay_percent"></div>
        <div class="fi"><label>加速回落系数</label><input type="number" data-key="overlap_decay_ratio_accel" step="0.1"></div>
        <div class="fi"><label>回落脚本</label><textarea data-key="overlap_decay_script"></textarea></div>
      </div>
    </div>
    <div class="fold">
      <div class="head" onclick="tog(this)"><span>倒地与战斗结束</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>角色倒地重罚</label><input type="checkbox" class="chk" data-key="knockdown_enabled"></div>
        <div class="fi"><label>倒地额外强度</label><input type="number" data-key="knockdown_add"></div>
        <div class="fi"><label>战斗结束处理</label>
          <select data-key="battle_end_mode"><option>none</option><option>clear</option><option>set</option></select></div>
        <div class="fi"><label>结束强度</label><input type="number" data-key="battle_end_strength"></div>
      </div>
    </div>
  </div>

  <div class="page" id="pg-cw">
    <div class="card">
      <h3>货币战争 · 实时</h3>
      <div class="stats">
        <div class="stat"><div class="k">总血量</div><div class="v" id="cwTotal">-</div></div>
        <div class="stat"><div class="k">扣血飘字</div><div class="v" id="cwDelta">-</div></div>
        <div class="stat"><div class="k">挨打次数</div><div class="v" id="cwHits">-</div></div>
        <div class="stat"><div class="k">持续电击</div><div class="v" id="cwSus">-</div></div>
        <div class="stat"><div class="k">血量系数</div><div class="v" id="cwHpK">-</div></div>
        <div class="stat"><div class="k">数据输入</div><div class="v" id="cwSrc" style="font-size:12px;">-</div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>模块与数据输入</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>模块: 货币战争</label><input type="checkbox" class="chk" data-key="module_cw_enabled"></div>
        <div class="fi"><label>总血量数据输入</label>
          <div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">OCR 外置识别（掉血由红字判定，OCR 只维护总血量数值）</div></div>
        <div class="fi"><label>OCR 读取内容</label>
          <div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">血量差值模式：识别下方血量数字，与上次差值即掉血量。</div></div>
        <div class="fi"><label>触发间隔（秒）</label><input type="number" data-key="cw_trigger_interval" step="0.05"></div>
        <div class="fi"><label></label>
          <div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">「当前血量系数」和「多次掉血叠加」是货币战争自己的两个系数，与常规战斗页的同名参数完全独立。</div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>总血量惩罚</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>掉落阈值</label>
          <input type="number" data-key="cw_total_drop_threshold"></div>
        <div class="fi"><label>总血量强度 A / B</label>
          <div class="duo">
            <div class="cell"><span class="mini">强度A</span><input type="number" data-key="cw_total_strength_a"></div>
            <div class="cell"><span class="mini">强度B</span><input type="number" data-key="cw_total_strength_b"></div>
          </div></div>
        <div class="fi"><label>血量数字颜色滤镜</label>
          <div class="duo">
            <div class="cell c70"><span class="mini">滤镜目标色</span>
              <input type="text" data-key="cw_ocr_colors" placeholder="#FFFFFF|#FFBE68"></div>
            <div class="cell c30"><span class="mini">近似度</span>
              <input type="number" data-key="cw_ocr_tolerance"></div>
          </div>
          <button class="btn" id="btnPickColor">取色</button></div>
        <div class="fi"><label>血量数字识别区域</label>
          <div class="regrow">
            <span class="pt"><span class="brk">【</span><input data-pt="1" placeholder="111, 222"><span class="brk">】</span></span>
            <span class="pt"><span class="brk">【</span><input data-pt="2" placeholder="333, 444"><span class="brk">】</span></span>
            <button class="btn primary" id="btnPick">框选区域</button>
          </div></div>
        <div class="fi"><label>加号检测</label>
          <input type="checkbox" class="chk" data-key="cw_plus_enabled"></div>
        <div class="fi"><label>检测点位置</label>
          <div class="regrow">
            <input type="text" data-key="cw_plus_positions" style="flex:1" placeholder="100,200|300,400">
            <button class="btn" id="btnPlusTest">测试</button>
            <button class="btn" id="btnPlusAdd">添加检测点</button>
          </div></div>
        <div class="fi"><label>检测点颜色</label>
          <input type="text" data-key="cw_plus_colors" placeholder="#FFBE68|#FFFFFF"></div>
        <div class="fi"><label>反向检测位置</label>
          <div class="regrow">
            <input type="text" data-key="cw_plus_negative_positions" style="flex:1" placeholder="500,600|700,800">
            <button class="btn" id="btnPlusNegAdd">添加反向点</button>
          </div></div>
        <div class="fi"><label>反向检测颜色</label>
          <input type="text" data-key="cw_plus_negative_colors" placeholder="#333333"></div>
        <div class="fi"><label>加号检测近似度</label>
          <input type="number" data-key="cw_plus_tolerance"></div>
        <div class="fi"><label>红字掉血检测</label>
          <input type="checkbox" class="chk" data-key="cw_red_gate"></div>
        <div class="fi"><label>红字目标色</label>
          <input type="text" data-key="cw_red_colors" placeholder="#EE7A74（多色用|分隔，留空=旧宽松判定）"></div>
        <div class="fi"><label>红字近似度</label>
          <input type="number" data-key="cw_red_tolerance"></div>
        <div class="fi"><label>红字最少像素</label>
          <input type="number" data-key="cw_red_min"></div>
        <div class="fi"><label>红字检测区域</label>
          <div class="regrow">
            <span class="pt"><span class="brk">【</span><input data-rpt="1" placeholder="留空=用识别区"><span class="brk">】</span></span>
            <span class="pt"><span class="brk">【</span><input data-rpt="2" placeholder=""><span class="brk">】</span></span>
            <button class="btn primary" id="btnPickRed">框选红区</button>
          </div></div>
        <div class="fi"><label></label><div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">红字检测区域留空 = 沿用「血量数字识别区域」。红字只在扣血瞬间出现，框住数字即可。</div></div>
        <div class="fi"><label>红字检测间隔（秒）</label>
          <input type="number" data-key="cw_red_interval" step="0.01"></div>
        <div class="fi"><label>加号检测间隔（秒）</label>
          <input type="number" data-key="cw_plus_interval" step="0.5"></div>
        <div class="fi"><label>红字等效掉血量</label>
          <input type="number" data-key="cw_red_hit_amount"></div>
        <div class="fi"><label>红字冷却（秒）</label>
          <input type="number" data-key="cw_red_cooldown" step="0.1"></div>
        <div class="fi"><label>总血量OCR间隔（秒）</label>
          <input type="number" data-key="cw_hp_ocr_interval" step="0.1"></div>
        <div class="fi"><label></label><div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">加号检测按「加号检测间隔」固定节拍跑，通过后才做红字检测与血量 OCR（其他窗口会被加号闸拦住）。掉血检测在红区内找「红字目标色」（近似度内数像素，数够「红字最少像素」才算一次），完全不经过 OCR。总血量另走低频 OCR，只更新数值、绝不参与掉血判定。</div></div>
        <div class="fi"><label></label><div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">框选坐标自动换算为「识别目标窗口」的相对坐标（窗口标题已移到「实时状态」页），窗口移动自动跟随。</div></div>
        <div class="fi"><label></label>
          <div style="display:flex;gap:8px;flex:1;">
            <button class="btn primary" id="btnOcrTest">测试识别</button>
            <button class="btn" id="btnDebugShot">诊断截图</button>
          </div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>当前血量系数</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用当前血量系数</label><input type="checkbox" class="chk" data-key="cw_hp_factor_enabled"></div>
        <div class="fi"><label>参考血量</label><input type="number" data-key="cw_hp_ref"></div>
        <div class="fi"><label>系数增量</label><input type="number" data-key="cw_hp_factor" step="0.1"></div>
        <div class="fi"><label></label>
          <div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">系数 = 1 + 系数增量 x (参考血量 - 当前血量) / 参考血量，比值限制在 0 ~ 1。满血时系数 1，血量归零时 1 + 系数增量。该系数乘在基础强度上，只作用于货币战争，与局内参数无关。</div></div>
      </div>
    </div>
    <div class="fold">
      <div class="head" onclick="tog(this)"><span>多次掉血（连续叠加）</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用多次掉血叠加</label><input type="checkbox" class="chk" data-key="cw_overlap_enabled"></div>
        <div class="fi"><label>每次叠加强度</label><input type="number" data-key="cw_overlap_strength_add"></div>
        <div class="fi"><label>强度上限</label><input type="number" data-key="cw_overlap_strength_max"></div>
        <div class="fi"><label>叠加窗口倍率</label><input type="number" data-key="cw_overlap_duration_multiplier" step="0.1"></div>
        <div class="fi"><label>启用自然回落</label><input type="checkbox" class="chk" data-key="cw_overlap_decay_enabled"></div>
        <div class="fi"><label>回落方式</label>
          <select data-key="cw_overlap_decay_mode"><option>instant</option><option>linear</option>
            <option>percent</option><option>ratio_accel</option><option>script</option></select></div>
        <div class="fi"><label>线性回落速度</label><input type="number" data-key="cw_overlap_decay_value"></div>
        <div class="fi"><label>百分比回落</label><input type="number" data-key="cw_overlap_decay_percent"></div>
        <div class="fi"><label>加速回落系数</label><input type="number" data-key="cw_overlap_decay_ratio_accel" step="0.1"></div>
        <div class="fi"><label>回落脚本</label><textarea data-key="cw_overlap_decay_script"></textarea></div>
        <div class="fi"><label></label>
          <div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">与局内「连续叠加」同一套机制，但参数与叠加状态完全独立：短时间内连续挨打，每次追加「每次叠加强度」，越接近强度上限追加越少；窗口外按回落方式衰减。</div></div>
      </div>
    </div>
    <div class="fold">
      <div class="head" onclick="tog(this)"><span>电击后持续电击</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用持续电击</label><input type="checkbox" class="chk" data-key="cw_sustain_enabled"></div>
        <div class="fi"><label>持续时长</label><input type="number" data-key="cw_sustain_duration" step="0.5"></div>
        <div class="fi"><label>持续期间保底强度</label><input type="number" data-key="cw_sustain_strength"></div>
        <div class="fi"><label>持续结束后清波形停止输出</label><input type="checkbox" class="chk" data-key="cw_sustain_stop_after"></div>
      </div>
    </div>
  </div>

  <div class="page" id="pg-ocr">
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>外置 OCR 连接设置</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用 OCR 识别</label><input type="checkbox" class="chk" data-key="cw_ocr_enabled"></div>
        <div class="fi"><label>Umi-OCR 地址</label><input type="text" data-key="cw_ocr_url"></div>
        <div class="fi"><label>OCR 模型配置</label><input type="text" data-key="cw_ocr_model"></div>
        <div class="fi"><label>OCR 轮询间隔</label><input type="number" data-key="cw_ocr_interval" step="0.1"></div>
        <div class="fi"><label></label>
          <div style="color:var(--dim);font-size:12px;flex:1;">滤镜、区域、框选取色在「货币战争」页。</div></div>
        <div class="fi"><label></label>
          <div style="color:var(--dim);font-size:12px;flex:1;">游戏窗口截图工具在本页底部。</div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>截图游戏窗口</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label></label>
          <div class="regrow" style="flex:1;">
            <button class="btn primary" id="btnGameShot">截图游戏窗口</button>
            <select id="shotSel" style="flex:1;min-width:0;"></select>
            <button class="btn" id="btnShotOpen">打开</button>
            <button class="btn" id="btnShotDel">删除</button>
          </div></div>
        <div class="fi"><label></label>
          <div class="regrow" style="flex:1;" id="ocrTools"></div></div>
        <div class="fi"><label></label>
          <div style="color:var(--dim);font-size:12px;flex:1;">截图保存到插件 screenshots/ 目录。</div></div>
      </div>
    </div>
  </div>

  <div class="page" id="pg-veritas">
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>veritas 连接</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>veritas Socket.IO 地址</label><input type="text" data-key="veritas_url" style="flex:1;"></div>
        <div class="fi"><label></label>
          <div class="regrow" style="flex:1;">
            <button class="btn" id="btnTestConn">测试连接</button>
            <span id="connState" style="font-size:12px;color:var(--dim);"></span>
          </div></div>
      </div>
    </div>
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>探测模式</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>启用探测模式</label><input type="checkbox" class="chk" data-key="cw_probe"></div>
        <div class="fi"><label></label>
          <div style="color:var(--dim);font-size:12px;flex:1;">开启后记录全部属性变化事件（含敌方/模式专属实体），用于排查数据源。</div></div>
      </div>
    </div>
    <div class="fold open" style="display:flex;flex-direction:column;flex:1;min-height:0;">
      <div class="head" onclick="tog(this)"><span>属性变化事件流</span><span>&#9662;</span></div>
      <div class="body" style="display:flex;flex-direction:column;flex:1;min-height:0;">
        <pre id="probeFeed" class="feed"></pre>
      </div>
    </div>
  </div>

  <div class="page" id="pg-wave">
    <div class="fold open">
      <div class="head" onclick="tog(this)"><span>波形</span><span>&#9662;</span></div>
      <div class="body">
        <div class="fi"><label>hit_pulse</label><textarea data-wave-key="hit_pulse"></textarea></div>
        <div class="fi"><label>shield_pulse</label><textarea data-wave-key="shield_pulse"></textarea></div>
        <div class="fi"><label>knockdown_pulse</label><textarea data-wave-key="knockdown_pulse"></textarea></div>
        <div class="fi"><label>cw_total_pulse</label><textarea data-wave-key="cw_total_pulse"></textarea></div>
        <div class="fi"><label>波形 d（毫秒/帧）</label><input type="number" data-key="wave_d_ms" step="50"></div>
        <div class="fi"><label>波形通道</label><select data-key="wave_channel"><option value="All">A + B</option><option value="A">仅 A</option><option value="B">仅 B</option></select></div>
        <div class="fi"><label></label><div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;">d 默认 100=正好播一遍（APP 会把波形循环补满 d，调大就重复多遍；被截断就调大）。只用 A 通道就选「仅 A」（B 的指令也会画在 APP 波形图上）。保存配置即时生效。</div></div>
        <div class="fi"><label></label><div style="flex:1;color:var(--dim);font-size:12px;line-height:1.5;"><b>每串 16 位十六进制 = 4 拍（每拍 100ms）</b>：前 4 字节=频率 Hz（0x64=100Hz，<b>越大越密越麻</b>，10Hz 会变成"敲"），后 4 字节=强度%（0x1E=30%、0x64=100%）。串数=时长，每串 400ms。</div></div>
      </div>
    </div>
  </div>
</main>
<footer>
  <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim);cursor:pointer;">
    <input type="checkbox" id="chkTop">窗口置顶</label>
  <button class="btn primary" id="btnSave">保存配置</button>
  <button class="btn" id="btnReset">重置配置</button>
  <span id="hint"></span>
</footer>

<script>
const plugin = "__PLUGIN__";
const BASE = "http://127.0.0.1:5000";
const DEFAULTS = __DEFAULTS_JSON__;
let waveformCache = {};

// ---- 关窗（最先挂，保证任何情况下都能关） ----
try{
  const { ipcRenderer } = require('electron');
  document.getElementById('winClose').onclick = () => ipcRenderer.send('window:close');
}catch(e){ try{ document.getElementById('winClose').onclick = () => window.close(); }catch(e2){} }

function tog(el){ el.parentElement.classList.toggle('open'); }

// ---- 页签切换 ----
const tabs = document.querySelectorAll('nav.tabs .tab');
const pages = document.querySelectorAll('main > .page');
tabs.forEach(t => {
  t.onclick = () => {
    tabs.forEach(x => x.classList.remove('active'));
    pages.forEach(p => p.classList.remove('active'));
    t.classList.add('active');
    const pg = document.getElementById('pg-' + t.dataset.page);
    if(pg) pg.classList.add('active');
  };
});

function hint(t){
  const h = document.getElementById('hint'); h.textContent = t;
  clearTimeout(h._t); h._t = setTimeout(() => h.textContent = '', 5000);
}
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
document.getElementById('chkTop').onchange = function(){
  callEngine('toggle_top', {on: this.checked, title: document.title},
             r => hint(r.message || '已执行'));
};

// ---------------- 文件桥（引擎通过 ipc.py 交换数据） ----------------
function nodeReq(m){ try{ return require(m); }catch(e){ return null; } }
const NODE_FS = nodeReq('fs'), NODE_OS = nodeReq('os'), NODE_PATH = nodeReq('path');
function ipcDir(){
  return (NODE_FS && NODE_OS && NODE_PATH)
    ? NODE_PATH.join(NODE_OS.tmpdir(), 'hsr_dglab_ipc') : null;
}
function readJson(p){
  try{
    // 引擎历史版本可能写入 BOM；JSON.parse 遇 BOM 抛异常 -> 永远“引擎不可达”
    const t = NODE_FS.readFileSync(p, 'utf8').replace(/^\uFEFF/, '');
    return JSON.parse(t);
  }catch(e){ return null; }
}
function metaInfo(){
  const d = ipcDir();
  return d ? readJson(NODE_PATH.join(d, 'meta.json')) : null;
}
function readStatus(){
  const d = ipcDir();
  return d ? readJson(NODE_PATH.join(d, 'status.json')) : null;
}
function engineAlive(){
  const s = readStatus();
  return !!(s && s.ipc_ts && (Date.now() / 1000 - s.ipc_ts < 3));
}
let cmdSeq = 1;
function callEngine(action, params, cb, timeout){
  const to = timeout || 6000;
  const d = ipcDir();
  if(!d || !NODE_FS){ cb({ok:false, message:'需在惩罚姬客户端内使用'}); return; }
  const id = Date.now() + '-' + (cmdSeq++);
  try{
    NODE_FS.writeFileSync(NODE_PATH.join(d, 'cmd.json'),
      JSON.stringify({id: id, action: action, params: params || {}}));
  }catch(e){ cb({ok:false, message:'写入命令失败: ' + e}); return; }
  const t0 = Date.now();
  const timer = setInterval(() => {
    const r = readJson(NODE_PATH.join(d, 'cmd_result.json'));
    if(r && r.id === id){ clearInterval(timer); cb(r); return; }
    if(Date.now() - t0 > to){
      clearInterval(timer);
      cb({ok:false, message:'插件引擎无响应'});
    }
  }, 120);
}

// ---------------- 配置读取（config.json + 默认值兜底） ----------------
function parsePt(s){
  const m = String(s || '').split(/[ ,，]+/).map(Number);
  if(m.length >= 2 && isFinite(m[0]) && isFinite(m[1])) return [Math.round(m[0]), Math.round(m[1])];
  return null;
}
async function loadConfig(){
  const merged = {
    plugins: Object.assign({}, DEFAULTS.plugins || {}),
    waveform: Object.assign({}, DEFAULTS.waveform || {})
  };
  let disk = null;
  const m = metaInfo();
  if(m && m.plugin_dir){
    disk = readJson(NODE_PATH.join(m.plugin_dir, 'config.json'));
  }
  if(disk && disk.config) disk = disk.config;
  if(!disk){
    try{
      const r = await fetch(BASE + '/config/' + encodeURIComponent(plugin) + '?t=' + Date.now(), {cache: 'no-store'});
      disk = await r.json();
    }catch(e){ disk = null; }
  }
  if(disk){
    Object.assign(merged.plugins, disk.plugins || {});
    Object.assign(merged.waveform, disk.waveform || {});
  }
  waveformCache = merged.waveform;
  document.querySelectorAll('[data-key]').forEach(el => {
    const v = merged.plugins[el.dataset.key];
    if(v === undefined) return;
    if(el.type === 'checkbox') el.checked = (v === true || v === 'true');
    else el.value = v;
  });
  const x = merged.plugins.cw_ocr_x, y = merged.plugins.cw_ocr_y,
        w = merged.plugins.cw_ocr_w, h = merged.plugins.cw_ocr_h;
  document.querySelector('[data-pt="1"]').value = (x !== undefined ? x + ', ' + y : '');
  document.querySelector('[data-pt="2"]').value = (x !== undefined ? (x + w) + ', ' + (y + h) : '');
  const rw = Number(merged.plugins.cw_red_w || 0), rh = Number(merged.plugins.cw_red_h || 0);
  const rq1 = document.querySelector('[data-rpt="1"]'), rq2 = document.querySelector('[data-rpt="2"]');
  if(rq1 && rq2){
    if(rw > 0 && rh > 0){
      const rx = Number(merged.plugins.cw_red_x || 0), ry = Number(merged.plugins.cw_red_y || 0);
      rq1.value = rx + ', ' + ry;
      rq2.value = (rx + rw) + ', ' + (ry + rh);
    } else { rq1.value = ''; rq2.value = ''; }
  }
  // 波形框按"一行一个 16 位十六进制"展示（JSON 数组也认，保存时两种都行）
  document.querySelectorAll('[data-wave-key]').forEach(el => {
    const v = merged.waveform[el.dataset.waveKey];
    if(v !== undefined) el.value = (Array.isArray(v) ? v : [v]).join('\n');
  });
}
// 波形解析：一行一个 16 位十六进制，或 JSON 数组 ["xxxx...","xxxx..."]
// 统一返回大写数组；格式不对返回 null —— 由调用方明确报错，绝不静默写回旧值
let waveErrors = [];
const HEX16 = /^[0-9a-fA-F]{16}$/;
function parseWave(text){
  const t = String(text == null ? '' : text).trim();
  if(!t) return null;
  if(t.charAt(0) === '['){
    try{
      const a = JSON.parse(t);
      if(Array.isArray(a)){
        const cells = a.map(x => String(x).replace(/["'\s]/g, '').trim());
        if(cells.length && cells.every(x => HEX16.test(x))) return cells.map(x => x.toUpperCase());
      }
    }catch(e){}
    return null;
  }
  const cells = t.split(/[\s,;]+/).map(s => s.replace(/["']/g, '').trim()).filter(Boolean);
  if(cells.length && cells.every(x => HEX16.test(x))) return cells.map(x => x.toUpperCase());
  return null;
}
function collect(){
  const p = {};
  document.querySelectorAll('[data-key]').forEach(el => {
    let v;
    if(el.type === 'checkbox') v = el.checked;
    else if(el.type === 'number') v = (el.value === '' ? 0 : Number(el.value));
    else v = el.value;
    p[el.dataset.key] = v;
  });
  const p1 = parsePt(document.querySelector('[data-pt="1"]').value);
  const p2 = parsePt(document.querySelector('[data-pt="2"]').value);
  if(p1 && p2){
    p.cw_ocr_x = Math.min(p1[0], p2[0]);
    p.cw_ocr_y = Math.min(p1[1], p2[1]);
    p.cw_ocr_w = Math.max(1, Math.abs(p2[0] - p1[0]));
    p.cw_ocr_h = Math.max(1, Math.abs(p2[1] - p1[1]));
  }
  const rp1 = parsePt(document.querySelector('[data-rpt="1"]').value);
  const rp2 = parsePt(document.querySelector('[data-rpt="2"]').value);
  if(rp1 && rp2){
    p.cw_red_x = Math.min(rp1[0], rp2[0]);
    p.cw_red_y = Math.min(rp1[1], rp2[1]);
    p.cw_red_w = Math.max(1, Math.abs(rp2[0] - rp1[0]));
    p.cw_red_h = Math.max(1, Math.abs(rp2[1] - rp1[1]));
  } else {
    p.cw_red_x = 0; p.cw_red_y = 0; p.cw_red_w = 0; p.cw_red_h = 0;
  }
  const w = {};
  waveErrors = [];
  document.querySelectorAll('[data-wave-key]').forEach(el => {
    const raw = String(el.value == null ? '' : el.value).trim();
    if(!raw){                                   // 空框：保持原值不动
      const old = waveformCache[el.dataset.waveKey];
      if(old !== undefined) w[el.dataset.waveKey] = old;
      return;
    }
    const cells = parseWave(raw);
    if(cells) w[el.dataset.waveKey] = cells;
    else waveErrors.push(el.dataset.waveKey);   // 格式不对 -> 保存时明确报错
  });
  return {plugins: p, waveform: w};
}
function writeDiskConfig(cfg){
  const m = metaInfo();
  if(!m || !m.plugin_dir) return false;
  try{
    const cfgPath = NODE_PATH.join(m.plugin_dir, 'config.json');
    let data = readJson(cfgPath) || {};
    data.config = data.config || {};
    data.config.plugins = Object.assign({}, data.config.plugins || {}, cfg.plugins);
    data.config.waveform = Object.assign({}, data.config.waveform || {}, cfg.waveform);
    NODE_FS.writeFileSync(cfgPath, JSON.stringify(data, null, 2));
    return true;
  }catch(e){ return false; }
}
document.getElementById('btnSave').onclick = () => {
  const cfg = collect();
  if(waveErrors.length){
    hint('波形格式不对，未保存：' + waveErrors.join('、')
         + '（每行一个 16 位十六进制，或 JSON 数组）');
    return;
  }
  if(engineAlive()){
    callEngine('apply_config', {config: cfg}, r => {
      if(r.ok) hint(r.message || '已保存并即时生效');
      else if(writeDiskConfig(cfg)) hint('已保存');
      else hint(r.message || '保存失败');
    });
  } else if(writeDiskConfig(cfg)){
    hint('已保存');
  } else {
    hint(!metaInfo() ? '保存失败：未找到引擎信息'
                     : '保存失败：配置文件写入失败');
  }
};
document.getElementById('btnReset').onclick = async () => {
  if(!confirm('确定重置为默认配置？')) return;
  const cfg = {plugins: Object.assign({}, DEFAULTS.plugins || {}),
               waveform: Object.assign({}, DEFAULTS.waveform || {})};
  if(engineAlive()) callEngine('apply_config', {config: cfg}, r => {
    hint(r.message || '已重置'); loadConfig();
  });
  else { writeDiskConfig(cfg); hint('已重置'); setTimeout(loadConfig, 300); }
};

// ---------------- 框选（引擎原生 tkinter 覆盖层，物理像素直出） ----------------
let pickBusy = false;  // 框选/取色共用互斥，防止排队堆积连环弹出
document.getElementById('btnPick').onclick = () => {
  if(pickBusy){ hint('已有框选/取色在进行'); return; }
  pickBusy = true;
  hint('拖拽框选扣血数字区域，回车确认');
  callEngine('pick_region', {}, r => {
    pickBusy = false;
    if(!r.ok){ hint(r.message || '框选失败'); return; }
    if(!r.value){ hint('已取消框选'); return; }
    let x1 = r.value.x, y1 = r.value.y;
    const tEl = document.querySelector('[data-key=cw_ocr_window_title]');
    const title = tEl ? (tEl.value || '崩坏：星穹铁道') : '崩坏：星穹铁道';
    const fillPts = (msg) => {
      document.querySelector('[data-pt="1"]').value = x1 + ', ' + y1;
      document.querySelector('[data-pt="2"]').value = (x1 + r.value.w) + ', ' + (y1 + r.value.h);
      hint(msg || '区域已填入，记得保存配置');
    };
    callEngine('window_anchor', {title: title}, a => {
      if(a && a.ok && a.value){
        x1 -= a.value.x; y1 -= a.value.y;
        fillPts('已按窗口「' + title + '」相对坐标填入，记得保存');
      } else {
        anchorMiss(title, '——已按屏幕坐标填入')(a);
      }
    }, 8000);
  }, 130000);
};

document.getElementById('btnPickRed').onclick = () => {
  if(pickBusy){ hint('已有框选/取色在进行'); return; }
  pickBusy = true;
  hint('拖拽框选红字检测区域，回车确认');
  callEngine('pick_region', {}, r => {
    pickBusy = false;
    if(!r.ok){ hint(r.message || '框选失败'); return; }
    if(!r.value){ hint('已取消框选'); return; }
    let x1 = r.value.x, y1 = r.value.y;
    const tEl = document.querySelector('[data-key=cw_ocr_window_title]');
    const title = tEl ? (tEl.value || '崩坏：星穹铁道') : '崩坏：星穹铁道';
    const fillRed = (msg) => {
      document.querySelector('[data-rpt="1"]').value = x1 + ', ' + y1;
      document.querySelector('[data-rpt="2"]').value = (x1 + r.value.w) + ', ' + (y1 + r.value.h);
      hint(msg || '红字区域已填入，记得保存配置');
    };
    callEngine('window_anchor', {title: title}, a => {
      if(a && a.ok && a.value){
        x1 -= a.value.x; y1 -= a.value.y;
        fillRed('已按窗口「' + title + '」相对坐标填入红字区域，记得保存');
      } else {
        anchorMiss(title, '——已按屏幕坐标填入')(a);
      }
    }, 8000);
  }, 130000);
};

function appendPair(posEl, colorEl, x, y, hex) {
  const ps = posEl.value.trim(), cs = colorEl.value.trim();
  posEl.value = ps ? ps + '|' + x + ',' + y : x + ',' + y;
  colorEl.value = cs ? cs + '|' + hex : hex;
}
function anchorMiss(title, extra) {
  return (a) => {
    const c = (a && a.candidates || []).slice(0, 8).join(' / ');
    hint('⚠ 未找到窗口「' + title + '」' + extra + '。当前可见窗口: ' + (c || '无')
      + '。提示：播放中的媒体播放器标题会变成文件名——改成其关键字或暂停后再取');
  };
}
document.getElementById('btnPlusAdd').onclick = () => {
  hint('鼠标移到常驻特征上回车取色');
  callEngine('pick_color', {}, r => {
    if(!r.ok || !r.value){ hint(r.message || '取色失败'); return; }
    const v = r.value;
    const finish = (msg) => {
      appendPair(document.querySelector('[data-key=cw_plus_positions]'),
                 document.querySelector('[data-key=cw_plus_colors]'), v.x, v.y, v.hex);
      hint(msg || ('已添加检测点 ' + v.x + ',' + v.y + ' ' + v.hex + '，记得保存配置'));
    };
    const tEl = document.querySelector('[data-key=cw_ocr_window_title]');
    const title = tEl ? (tEl.value || '崩坏：星穹铁道') : '崩坏：星穹铁道';
    callEngine('window_anchor', {title: title}, a => {
      if(a && a.ok && a.value){
        // 取色给的是屏幕物理坐标；检测点必须与识别区域同口径（窗口客户区
        // 相对）。此前直接存屏幕坐标 -> 采样点整体偏移（实测 Y 偏 80+px），
        // 现场取色后永远检测不到（本 bug 根因）。
        v.x = Math.round(v.x - a.value.x);
        v.y = Math.round(v.y - a.value.y);
        if(v.x < 0 || v.y < 0){
          hint('⚠ 取色点不在窗口「' + title + '」客户区内，可能取到了别的窗口');
          return;
        }
        finish('已按窗口「' + title + '」相对坐标添加检测点，记得保存');
      }
      else { anchorMiss(title, '——点已按屏幕坐标添加')(a); }
    }, 8000);
  }, 130000);
};
// 加号测试：直接拿界面上（可能还没保存的）点位去测当前画面
document.getElementById('btnPlusTest').onclick = () => {
  const gv = (k) => { const el = document.querySelector('[data-key=' + k + ']'); return el ? el.value : ''; };
  hint('正在测当前画面…');
  callEngine('plus_test', {
    cw_plus_positions: gv('cw_plus_positions'),
    cw_plus_colors: gv('cw_plus_colors'),
    cw_plus_negative_positions: gv('cw_plus_negative_positions'),
    cw_plus_negative_colors: gv('cw_plus_negative_colors'),
    cw_plus_tolerance: gv('cw_plus_tolerance'),
    cw_ocr_window_title: gv('cw_ocr_window_title')
  }, r => hint(r.message || (r.ok ? '测试完成' : '测试失败')), 20000);
};

document.getElementById('btnPlusNegAdd').onclick = () => {
  hint('请把鼠标移到「非结算画面」才有的特征上，回车取色…');
  callEngine('pick_color', {}, r => {
    if(!r.ok || !r.value){ hint(r.message || '取色失败'); return; }
    const v = r.value;
    const tEl = document.querySelector('[data-key=cw_ocr_window_title]');
    const title = tEl ? (tEl.value || '崩坏：星穹铁道') : '崩坏：星穹铁道';
    callEngine('window_anchor', {title: title}, a => {
      if(a && a.ok && a.value){
        v.x = Math.round(v.x - a.value.x);
        v.y = Math.round(v.y - a.value.y);
        appendPair(document.querySelector('[data-key=cw_plus_negative_positions]'),
                   document.querySelector('[data-key=cw_plus_negative_colors]'), v.x, v.y, v.hex);
        hint('已添加反向点 ' + v.x + ',' + v.y + ' ' + v.hex + '（窗口相对），记得保存配置');
      } else {
        anchorMiss(title, '——反向点未添加')(a);
      }
    }, 8000);
  }, 130000);
};

// ---------------- 动作按钮（走命令桥） ----------------
function doAction(action, label){
  callEngine(action, {}, r => hint(r.message || (label || action) + ' 已执行'));
}
document.getElementById('btnShock').onclick = () => doAction('test_shock', '测试电击');
// 直接用 cw_total_pulse + 货币通道强度测一段（不用进游戏）
document.getElementById('btnShockCw').onclick = () => {
  const gv = (k, d) => { const el = document.querySelector('[data-key=' + k + ']'); return (el && el.value !== '') ? el.value : d; };
  hint('正在发送货币波形测试…');
  callEngine('test_shock', {wave: 'cw_total_pulse',
                            strength_a: gv('cw_total_strength_a', 24),
                            strength_b: gv('cw_total_strength_b', 24)},
             r => hint(r.message || '已发送'), 30000);
};
document.getElementById('btnUp').onclick = () => doAction('str_up', '强度+1');
document.getElementById('btnDown').onclick = () => doAction('str_down', '强度-1');
document.getElementById('btnClear').onclick = () => doAction('str_clear', '强度清零');
document.getElementById('btnConn').onclick = () => doAction('test_conn', '测试连接');
document.getElementById('btnOcrTest').onclick = () => doAction('ocr_test', '测试识别');
document.getElementById('btnDebugShot').onclick = () => doAction('debug_shot', '诊断截图');
document.getElementById('btnPause').onclick = () => doAction('toggle_pause', '暂停/恢复');
document.getElementById('btnPickColor').onclick = () => {
  if(pickBusy){ hint('已有框选/取色在进行'); return; }
  pickBusy = true;
  hint('光标对准目标颜色后回车');
  callEngine('pick_color', {}, r => {
    pickBusy = false;
    if(!r.ok || !r.value){ hint(r.message || '取色失败'); return; }
    document.querySelector('[data-key=cw_ocr_colors]').value = r.value.hex;
    hint('已填入颜色 ' + r.value.hex + '，记得保存');
  }, 130000);
};

// ---------------- 实时轮询（status.json 心跳） ----------------
function badge(el, text, cls){ el.textContent = text; el.className = 'badge ' + (cls || ''); }
function render(d){
  const bV = document.getElementById('bVeritas');
  const bB = document.getElementById('bBattle');
  const bS = document.getElementById('bSource');
  const bC = document.getElementById('bCw');
  if(!d || d.running !== true){
    badge(bV, '插件未启动', 'off');
    badge(bB, '未战斗', '');
    badge(bS, '战斗输入: -', '');
    document.getElementById('avatars').innerHTML =
      '<div class="offline">插件未启动 — 配置仍可编辑保存，启动插件后自动显示实时数据</div>';
    return;
  }
  badge(bV, d.connected ? ('veritas 已连接' + (d.version ? ' · v' + d.version : ''))
                        : 'veritas 未连接', d.connected ? 'on' : 'off');
  badge(bB, d.battle_active ? '&#9876; 战斗中' : '未战斗', d.battle_active ? 'battle' : '');
  badge(bS, '战斗输入: ' + (d.data_source || '-'), '');

  const list = d.avatars || [];
  const box = document.getElementById('avatars');
  if(!list.length){
    box.innerHTML = '<div class="offline">等待战斗阵容</div>';
  } else {
    box.innerHTML = list.map(a => {
      let pct = 0;
      if(a.hp != null && a.max_hp) pct = Math.max(0, Math.min(100, a.hp / a.max_hp * 100));
      const cls = pct <= 25 ? 'low' : (pct <= 55 ? 'mid' : '');
      const hpText = (a.hp != null)
        ? (Math.round(a.hp) + ' / ' + (a.max_hp ? Math.round(a.max_hp) : '?') + ' · ' + pct.toFixed(1) + '%')
        : '等待数据';
      let spct = 0;
      if(a.shield != null && a.max_shield) spct = Math.max(0, Math.min(100, a.shield / a.max_shield * 100));
      const shieldRow = (a.shield != null && (a.shield > 0 || a.max_shield > 0))
        ? ('<div class="row1"><span class="nm" style="font-size:11px;color:#7ec8ff;">盾</span>' +
           '<span class="hp">' + Math.round(a.shield) + (a.max_shield ? ' / ' + Math.round(a.max_shield) : '') + '</span></div>' +
           '<div class="bar" style="height:6px;"><i class="sh" style="width:' + spct + '%"></i></div>')
        : '';
      return '<div class="avatar"><div class="row1"><span class="nm">' + esc(a.name) +
             '</span><span class="hp">' + hpText + '</span></div>' +
             '<div class="bar"><i class="' + cls + '" style="width:' + pct + '%"></i></div>' +
             shieldRow + '</div>';
    }).join('');
  }
  document.getElementById('sHit').textContent = d.hit_count;
  document.getElementById('sShieldHit').textContent = d.shield_hit_count;
  document.getElementById('sLost').textContent = d.total_lost;
  document.getElementById('sKnock').textContent = d.knock_count;
  document.getElementById('sOvl').textContent = '+' + (d.overlap || 0);
  document.getElementById('sStr').textContent =
    (d.strength_a == null ? '-' : d.strength_a) + ' / ' + (d.strength_b == null ? '-' : d.strength_b);
  const lh = d.last_hit;
  document.getElementById('lastHit').textContent = lh
    ? ('最近挨打: ' + esc(lh.name) + ' −' + lh.lost + ' · ' + Math.max(0, Math.round(Date.now()/1000 - lh.ts)) + 's 前')
    : ' ';

  const cw = d.cw || {};
  const cwOn = (d.modules || {}).cw === true;
  const cwCard = document.querySelector('#pg-cw .card');
  cwCard.style.display = cwOn ? 'block' : 'none';
  bC.style.display = cwOn ? '' : 'none';
  if(cwOn){
    badge(bC, cw.sustain_active ? '⚡ 货币战争 持续电击中' : '货币战争 待机',
          cw.sustain_active ? 'battle' : '');
    document.getElementById('cwTotal').textContent =
      (cw.total_hp == null ? '-' : Math.round(cw.total_hp));
    document.getElementById('cwDelta').textContent =
      (cw.last_delta == null ? '-' : cw.last_delta);
    document.getElementById('cwHits').textContent = cw.total_hits || 0;
    const hpK = document.getElementById('cwHpK');
    if(hpK) hpK.textContent = (cw.hp_factor == null ? '-' : 'x' + Number(cw.hp_factor).toFixed(2));
    const sus = document.getElementById('cwSus');
    sus.textContent = cw.sustain_active ? '⚡ 持续中' : '待机';
    sus.className = 'v' + (cw.sustain_active ? ' hot' : '');
    document.getElementById('cwSrc').textContent =
      'OCR' + (cw.ocr_enabled ? ' · 已启用' : ' · 未启用');
    const wsEl = document.getElementById('winState');
    if(wsEl) wsEl.innerHTML = '窗口状态：' + (cw.win_found
      ? '<b style="color:#3ddc84">已找到</b>'
      : '<b style="color:#ff6b6b">未找到</b>（检查标题 / 游戏是否启动）');
  }
}
function poll(){
  const s = readStatus();
  const fresh = !!(s && s.ipc_ts && (Date.now() / 1000 - s.ipc_ts < 3));
  render(fresh ? s : null);
}


// ---------------- 截图游戏窗口（调试工具） ----------------
function refreshShots(){
  callEngine('list_shots', {}, r => {
    const sel = document.getElementById('shotSel');
    sel.innerHTML = '';
    const items = (r && r.value) || [];
    if(!items.length){
      sel.innerHTML = '<option value="">暂无截图</option>';
      return;
    }
    items.forEach(it => {
      const o = document.createElement('option');
      o.value = it.name;
      o.textContent = it.name + ' · ' + Math.round(it.size / 1024) + 'KB';
      sel.appendChild(o);
    });
  }, 8000);
}
document.getElementById('btnGameShot').onclick = () => {
  hint('正在截图游戏窗口…');
  callEngine('game_shot', {}, r => {
    hint(r.message || (r.ok ? '已截图' : '截图失败'));
    if(r.ok) refreshShots();
  }, 20000);
};
function openShot(){
  const name = document.getElementById('shotSel').value;
  if(!name){ hint('没有可打开的截图'); return; }
  callEngine('open_shot', {name: name}, r => {
    hint(r.message || (r.ok ? '已打开' : '打开失败'));
  }, 10000);
}
document.getElementById('btnShotOpen').onclick = openShot;
document.getElementById('shotSel').addEventListener('dblclick', openShot);
document.getElementById('btnShotDel').onclick = () => {
  const name = document.getElementById('shotSel').value;
  if(!name){ hint('没有可删除的截图'); return; }
  callEngine('del_shot', {name: name}, r => {
    hint(r.message || (r.ok ? '已删除' : '删除失败'));
    if(r.ok) refreshShots();
  }, 8000);
};
refreshShots();

// ---------------- veritas 事件流 ----------------
let lastFeedLen = -1;
function renderProbe(d){
  const feed = document.getElementById('probeFeed');
  if(!feed) return;
  const evs = d && d.probe_events || [];
  if(evs.length !== lastFeedLen){
    lastFeedLen = evs.length;
    feed.textContent = evs.length ? evs.join('\n') : '暂无事件';
    feed.scrollTop = feed.scrollHeight;
  }
  const cs = document.getElementById('connState');
  if(cs) cs.textContent = d
    ? (d.connected ? '已连接' + (d.version ? ' · v' + d.version : '')
                   : (d.running ? '未连接' : '插件未启动'))
    : '';
}
const _origPoll = poll;
poll = function(){
  _origPoll();
  try{
    const s = readStatus();
    if(s && s.running) renderProbe(s);
  }catch(e){}
};

loadConfig();
poll();
setInterval(poll, 800);
</script>
</body>
</html>
"""


def _load_defaults():
    """读取插件 config.json 的默认配置（页面填充兜底）"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("config", {})
    except Exception:
        return {}


def _safe_json(obj) -> str:
    """合法 JS 字面量（转义 </ 防止闭合标签截断脚本）"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(plugin_name: str) -> str:
    """生成插件配置页面 HTML（默认值 + 插件名安全注入）"""
    return (PAGE_HTML
            .replace("__DEFAULTS_JSON__", _safe_json(_load_defaults()))
            .replace("__PLUGIN__", plugin_name))
