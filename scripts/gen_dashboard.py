#!/usr/bin/env python3
"""
gen_dashboard.py — 生成增强版A股分析看板
v5: 移动优先双行卡片+图例+详细弹窗
v6: 趋势/时点双标签清晰展示
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUTPUT = r"C:\Users\LarkMi\AppData\Local\hermes\dashboard.html"

def generate():
    """完整流程：跑分析 + 生成看板（供cron/manual调用）。
    run_analysis() 已内置自动生成看板，此处仅做入口包装。"""
    from market_watcher import run_analysis
    return run_analysis()  # 内部已调用 generate_html_from_results


def generate_html_from_results(r, h, locked, evo_log):
    """仅生成HTML（不跑分析），供market_watcher.run_analysis()结束后调用"""
    long_up = sum(1 for s in r['long'] if 'up' in str(s.get('pred','')))
    short_down = sum(1 for s in r['short'] if 'down' in str(s.get('pred','')))
    custom_preds = r.get('locked', [])
    total_stocks = len(r['long']) + len(r['short']) + len(custom_preds)

    data = {
        'meta': {
            'time': r['meta']['time'],
            'breadth': r['meta']['market_breadth'],
            'regime': r['meta'].get('market_regime',''),
            'long_up': long_up,
            'short_down': short_down,
            'total': total_stocks,
            'phase': r['meta'].get('phase',''),
            'factors': r['meta'].get('factors_active',[]),
            'weights': r['meta'].get('adaptive_weights',{}),
        },
        'long': r['long'],
        'short': r['short'],
        'custom_preds': custom_preds,
        'history': h,
        'custom': locked,
        'evo_log': evo_log,
    }

    html = HTML_TEMPLATE.replace('__DATA_PLACEHOLDER__', json.dumps(data, ensure_ascii=False, default=str))
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'✅ 看板已生成: {OUTPUT}')
    print(f'   时间: {data["meta"]["time"]}')
    print(f'   做多: {long_up}/{len(r["long"])}看多 | 做空: {short_down}/{len(r["short"])}看空 | 阶段: {data["meta"]["regime"]}')
    trend_label = f'{h["total_verified"]}条/{h["overall_acc"] or "N/A"}'
    spot_label = f'{h["spot_verified"]}条/{h["spot_acc"] or "N/A"}'
    print(f'   趋势: {trend_label} | 时点: {spot_label}')
    return OUTPUT


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>📊 A股分析看板 v6</title>
<style>
/* ===== 基础 ===== */
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;font-size:16px;padding:12px}
h2{font-size:1.1em;margin:12px 0 8px;color:#94a3b8}
.section{margin-bottom:8px}
.count{font-size:.8em;color:#64748b;margin-left:8px}

/* ===== 元信息 ===== */
.meta{background:#1e293b;border-radius:12px;padding:12px;margin-bottom:16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:.85em}
.meta span{padding:4px 10px;background:#334155;border-radius:6px;white-space:nowrap}
.badge{padding:4px 10px;border-radius:20px;font-weight:700}
.bull{background:#064e3b;color:#6ee7b7}
.bear{background:#450a0a;color:#fca5a5}
.neutral{background:#1e3a5f;color:#93c5fd}

/* ===== 准确率面板 ===== */
.acc-panel{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px;margin-bottom:16px}
.acc-card{background:#1e293b;border-radius:10px;padding:10px;text-align:center}
.acc-card .val{font-size:1.4em;font-weight:700;color:#f8fafc}
.acc-card .lbl{font-size:.7em;color:#64748b;margin-top:2px}
.acc-good{color:#4ade80!important}
.acc-warn{color:#fbbf24!important}
.acc-bad{color:#f87171!important}

/* ===== 股票行 (双行卡片) ===== */
.stock-row{background:#1e293b;border-radius:10px;padding:10px 12px;margin-bottom:8px;cursor:pointer;transition:background .2s}
.stock-row:active{background:#334155}
.stock-row .line1{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.stock-row .line2{display:flex;align-items:center;gap:10px;margin-top:6px;font-size:.8em;color:#94a3b8;flex-wrap:wrap}
.idx{font-weight:700;color:#64748b;min-width:18px}
.name{font-weight:600;flex:1;min-width:60px}
.price{font-weight:700;color:#f8fafc;min-width:60px;text-align:right}
.chg-up{color:#f87171;font-weight:700}
.chg-down{color:#4ade80;font-weight:700}
.chg-neu{color:#94a3b8}

/* ===== 预测标签 ===== */
.pred-tag{font-size:.75em;padding:2px 8px;border-radius:12px;font-weight:600;white-space:nowrap}
.pred-tag em{font-style:normal;font-size:.7em;opacity:.6;margin-right:2px}
.strong_up,.strong_down{font-weight:700}
.strong_up{background:#450a0a;color:#fca5a5}
.up{background:#3a1a1a;color:#f87171}
.strong_down{background:#064e3b;color:#6ee7b7}
.down{background:#0f3a2e;color:#4ade80}
.neutral{background:#1e293b;color:#94a3b8}

.spot-tag{font-size:.75em;padding:2px 8px;border-radius:12px;font-weight:600;white-space:nowrap}
.spot-tag em{font-style:normal;font-size:.7em;opacity:.6;margin-right:2px}
.spot-tag.up{background:#3a1a1a;color:#f87171}
.spot-tag.down{background:#0f3a2e;color:#4ade80}
.spot-tag.flat{background:#1e293b;color:#94a3b8}

/* ===== 置信度条 ===== */
.conf-wrap{flex:1;min-width:60px}
.conf-bar{height:5px;background:#334155;border-radius:3px;overflow:hidden}
.conf-fill{height:100%;border-radius:3px}
.conf-high{background:#4ade80}
.conf-mid{background:#fbbf24}
.conf-low{background:#f87171}

/* ===== 图例栏 ===== */
.legend{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0;font-size:.75em;color:#64748b}
.legend span{display:flex;align-items:center;gap:4px}

/* ===== 因子栏 ===== */
.factors{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px;font-size:.75em}
.factors span{background:#1e293b;padding:4px 10px;border-radius:6px;color:#94a3b8}

/* ===== 自定义标的面板 ===== */
.locked-panel{background:#1e293b;border-radius:12px;padding:12px;margin-bottom:16px}
.locked-panel h3{margin-bottom:8px}
.locked-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
.locked-item{background:#0f172a;border-radius:8px;padding:8px;display:flex;justify-content:space-between;align-items:center;font-size:.85em}
.locked-item .remove{color:#f87171;cursor:pointer;font-size:1.2em;padding:2px 6px}
.search-box{display:flex;gap:8px;margin-top:8px}
.search-box input{flex:1;background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:8px;border-radius:8px;font-size:16px}
.search-box button{background:#1d4ed8;color:#fff;border:none;padding:8px 16px;border-radius:8px;font-size:16px;cursor:pointer}
.search-results{max-height:200px;overflow-y:auto;margin-top:8px}
.search-results .sr-item{padding:8px;border-bottom:1px solid #334155;cursor:pointer;display:flex;justify-content:space-between}
.search-results .sr-item:active{background:#334155}

/* ===== 弹窗 ===== */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:#1e293b;border-radius:16px;padding:20px;max-width:500px;width:90%;max-height:80vh;overflow-y:auto}
.modal h3{margin-bottom:12px}
.modal .detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.85em;margin-bottom:12px}
.modal .k{color:#64748b}
.modal .v{color:#f8fafc;text-align:right}
.modal .reason{background:#0f172a;padding:10px;border-radius:8px;margin:8px 0;font-size:.85em}
.modal .history-table{width:100%;font-size:.75em;border-collapse:collapse;margin-top:8px}
.modal .history-table th,.modal .history-table td{padding:6px 4px;border-bottom:1px solid #334155;text-align:center}
.modal .history-table th{color:#64748b}
.modal .ok{color:#4ade80}
.modal .fail{color:#f87171}
.modal .close-btn{background:#334155;color:#e2e8f0;border:none;padding:8px 20px;border-radius:8px;font-size:16px;cursor:pointer;margin-top:12px;float:right}
.modal .section-title{font-size:.9em;color:#94a3b8;margin:12px 0 6px;border-bottom:1px solid #334155;padding-bottom:4px}

/* ===== 🆕 增强弹窗 ===== */
.det-sec{margin-bottom:14px}
.det-title{font-size:.9em;color:#94a3b8;margin:0 0 8px;border-bottom:1px solid #334155;padding-bottom:4px}
.det-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:.82em}
.det-grid .k{color:#64748b}
.det-grid .v{color:#f8fafc;text-align:right}
.det-grid .fw{font-weight:700;font-size:1.1em}
.cf-bar{display:inline-block;width:60px;height:4px;background:#334155;border-radius:2px;vertical-align:middle;margin-left:4px}
.cf-bar i{display:block;height:100%;border-radius:2px;background:linear-gradient(90deg,#4ade80,#fbbf24,#f87171)}
.red{color:#f87171!important}
.green{color:#4ade80!important}
.sig-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:4px 0}
.sig-label{font-size:.82em;font-weight:600;min-width:50px}
.sig-chip{font-size:.75em;padding:2px 8px;border-radius:10px;display:inline-flex;align-items:center;gap:4px}
.sig-chip em{font-style:normal;font-size:.7em;opacity:.6;background:rgba(255,255,255,.1);padding:1px 4px;border-radius:3px}
.bull-chip{background:#064e3b;color:#6ee7b7}
.bear-chip{background:#450a0a;color:#fca5a5}
.modal .close-btn{position:sticky;bottom:0;width:100%}

/* ===== 响应式 ===== */
@media(min-width:768px){
  body{max-width:700px;margin:0 auto}
  .section.long,.section.short{display:grid;grid-template-columns:1fr;gap:0}
}
@media(min-width:1024px){
  body{max-width:900px}
}
</style>
</head>
<body>
<div id="meta" class="meta"></div>
<div id="accPanel" class="acc-panel"></div>
<div class="legend">
  <span>📈趋势=1-3日方向</span><span>📍时点=下一时段</span>
  <span style="color:#4ade80">🟢高置信≥65%</span>
  <span style="color:#fbbf24">🟡中置信45-64%</span>
  <span style="color:#f87171">🔴低置信&lt;45%</span>
</div>
<div id="factors" class="factors"></div>
<div id="lockedPanel" class="locked-panel"></div>
<div id="signalGrid"></div>
<div id="evoLogPanel" class="locked-panel" style="margin-top:16px"></div>
<div id="detailModal" class="modal-overlay" onclick="if(event.target===this)closeDetail()">
  <div class="modal" id="detailContent"></div>
</div>
<script>
const DATA=__DATA_PLACEHOLDER__;
const DIR_MAP={strong_up:'🚀强力看多',up:'📈看多',neutral:'➡️中性',down:'📉看空',strong_down:'💥强力看空'};
const SPOT_MAP={up:'涨',down:'跌',flat:'平'};
const E_LONG={strong_up:'🚀',up:'📈',neutral:'➡️'};
const E_SHORT={strong_down:'💥',down:'📉',neutral:'➡️'};

function fmt(v){if(v===null||v===undefined||v==='N/A')return'N/A';const n=Number(v);return isNaN(n)?v:n.toFixed(n<10?2:0)}

(function render(){
  // Meta
  const m=DATA.meta;
  let meta='<span>🕐'+m.time+'</span>';
  meta+='<span>'+m.breadth+'</span>';
  const rc=m.regime.includes('牛')?'bull':m.regime.includes('熊')?'bear':'neutral';
  meta+='<span class="badge '+rc+'">'+m.regime+'</span>';
  meta+='<span>🟢做多 '+m.long_up+'/'+DATA.long.length+'</span><span>🔴做空 '+m.short_down+'/'+DATA.short.length+'</span>';
  meta+='<span>📍'+m.phase+'</span>';
  document.getElementById('meta').innerHTML=meta;

  renderLocked();

  // Accuracy panel
  const h=DATA.history;
  const oa=h.overall_acc;
  const oaClass=oa===null?'':oa>=0.6?'acc-good':oa>=0.4?'acc-warn':'acc-bad';
  const sa=h.spot_acc;
  const saClass=sa===null?'':sa>=0.6?'acc-good':sa>=0.4?'acc-warn':'acc-bad';
  let acc='';
  acc+='<div class="acc-card"><div class="val">'+(h.total_verified||0)+'</div><div class="lbl">趋势已验证</div></div>';
  acc+='<div class="acc-card"><div class="val '+oaClass+'">'+(oa!==null?(oa*100).toFixed(0)+'%':'--')+'</div><div class="lbl">趋势准确率</div></div>';
  acc+='<div class="acc-card"><div class="val">'+(h.spot_verified||0)+'</div><div class="lbl">时点已验证</div></div>';
  acc+='<div class="acc-card"><div class="val '+saClass+'">'+(sa!==null?(sa*100).toFixed(0)+'%':'--')+'</div><div class="lbl">时点准确率</div></div>';
  acc+='<div class="acc-card"><div class="val">'+(DATA.long.length+DATA.short.length+DATA.custom_preds.length)+'</div><div class="lbl">标的池</div></div>';
  acc+='<div class="acc-card"><div class="val">'+(h.perf.length||0)+'</div><div class="lbl">有记录</div></div>';
  if(h.dir_stats) for(const[d,s]of Object.entries(h.dir_stats)){
    acc+='<div class="acc-card"><div class="val">'+(s.acc*100).toFixed(0)+'%</div><div class="lbl">'+(DIR_MAP[d]||d)+'</div></div>';
  }
  document.getElementById('accPanel').innerHTML=acc;

  // Signal grid
  let html='<div class="section long"><h2>🟢 做多 Top 5<span class="count">'+DATA.long.length+'只</span></h2>';
  DATA.long.forEach((s,i)=>html+=row(s,i,true));
  html+='</div><div class="section short"><h2>🔴 做空 Top 5<span class="count">'+DATA.short.length+'只</span></h2>';
  DATA.short.forEach((s,i)=>html+=row(s,i,false));
  html+='</div>';

  if(DATA.custom_preds.length){
    html+='<div class="section"><h2>📌 自定义标的<span class="count">'+DATA.custom_preds.length+'只</span></h2>';
    DATA.custom_preds.forEach((s,i)=>html+=row(s,i,s.pred&&!s.pred.includes('down')));
    html+='</div>';
  }
  document.getElementById('signalGrid').innerHTML=html;

  // Factors
  let fac='';
  if(m.factors) m.factors.forEach(f=>{fac+='<span>'+f+'</span>'});
  if(m.weights) fac+='<span>权重 T'+m.weights.trend+' P'+m.weights.pos+' V'+m.weights.vol+' M'+m.weights.momentum+'</span>';
  document.getElementById('factors').innerHTML=fac;

  renderSkillLog();
})();

function row(s,i,isLong){
  const atr=typeof s.atr==='number'?s.atr:0;
  const pred=s.pred||'neutral';
  let expRet=0;
  if(pred==='strong_up') expRet=atr;
  else if(pred==='up') expRet=atr*0.5;
  else if(pred==='down') expRet=-atr*0.5;
  else if(pred==='strong_down') expRet=-atr;
  const cc=expRet>0.5?'chg-up':expRet<-0.5?'chg-down':'chg-neu';
  const conf=s.conf||0.3;
  const cf=conf>=0.65?'conf-high':conf>=0.45?'conf-mid':'conf-low';
  const em=isLong?E_LONG[pred]||'➡️':E_SHORT[pred]||'➡️';
  const spotDir=s.spot_dir||'';
  const spotLabel=spotDir?'<span class="spot-tag '+spotDir+'"><em>时点</em>📍'+(SPOT_MAP[spotDir]||spotDir)+'</span>':'';
  const detail=encodeURIComponent(JSON.stringify({code:s.code,name:s.name||s.code,price:s.price,price_time:s.price_time||'',pred,conf,atr,expRet,reason:s.reason||'',note:s.note||'',trend:s.trend||'',pos:s.pos||0,chg:s.chg_5d||0,spotDir,side:s.side||'',ma5:s.ma5||0,ma10:s.ma10||0,chg_1d:s.chg_1d||0,chg_3d:s.chg_3d||0,vol_ratio:s.vol_ratio||1,score:s.score||0,high_10d:s.high_10d||s.price,low_10d:s.low_10d||s.price}));

  return '<div class="stock-row" onclick="openDetail(\''+detail+'\')">'
    +'<div class="line1">'
    +'<span class="idx">'+(i+1)+'</span>'
    +'<span class="name">'+em+' '+(s.name||s.code)+'</span>'
    +'<span class="price">'+fmt(s.price)+'</span>'+(s.price_time?'<span style="font-size:.62em;color:#64748b;margin-left:2px">'+s.price_time+'</span>':'')
    +'<span class="'+cc+'">'+(expRet>=0?'+':'')+expRet.toFixed(1)+'%</span>'
    +'<span class="pred-tag '+pred+'"><em>趋势</em> '+(DIR_MAP[pred]||pred)+'</span>'
    +spotLabel
    +'</div>'
    +'<div class="line2">'
    +'<span>ATR '+atr.toFixed(1)+'%</span>'
    +'<span>'+s.trend+'</span>'
    +'<span>pos '+s.pos+'%</span>'
    +'<span class="conf-wrap"><div class="conf-bar"><div class="conf-fill '+cf+'" style="width:'+(conf*100)+'%"></div></div></span>'
    +'<span style="font-size:.85em;color:#475569">'+(s.note||'').substring(0,40)+'</span>'
    +'</div></div>';
}

function renderLocked(){
  const panel=document.getElementById('lockedPanel');
  let html='<h3>📌 自定义标的 <span style="font-size:.8em;color:#64748b">('+DATA.custom.length+'只)</span></h3>';
  html+='<div class="locked-grid">';
  DATA.custom.forEach(s=>{
    html+='<div class="locked-item"><span>'+s.name+'('+s.code+')</span><span class="remove" onclick="event.stopPropagation();removeLocked(\''+s.code+'\')">✕</span></div>';
  });
  html+='</div>';
  html+='<div class="search-box"><input id="searchInput" placeholder="搜索股票代码/名称..." oninput="doSearch()"><button onclick="doSearch()">搜索</button></div>';
  html+='<div id="searchResults" class="search-results"></div>';
  panel.innerHTML=html;
}

async function doSearch(){
  const q=document.getElementById('searchInput').value.trim();
  if(q.length<1){document.getElementById('searchResults').innerHTML='';return;}
  const resp=await fetch('/api/search?q='+encodeURIComponent(q));
  const data=await resp.json();
  let html='';
  data.forEach(s=>{
    html+='<div class="sr-item" onclick="addLocked(\''+s.code+'\',\''+s.name+'\')"><span>'+s.name+'</span><span style="color:#64748b">'+s.code+'</span><span style="color:#4ade80">+添加</span></div>';
  });
  document.getElementById('searchResults').innerHTML=html||'<div style="padding:8px;color:#64748b">无匹配结果</div>';
}

async function addLocked(code,name){
  const resp=await fetch('/api/locked',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,name,reason:''})});
  const d=await resp.json();
  if(d.ok){location.reload()}else{alert(d.error||'添加失败')}
}

async function removeLocked(code){
  if(!confirm('确认删除 '+code+' ?'))return;
  const resp=await fetch('/api/locked?code='+encodeURIComponent(code),{method:'DELETE'});
  const d=await resp.json();
  if(d.ok){location.reload()}else{alert(d.error||'删除失败')}
}

let _historyCache={};
async function openDetail(detailStr){
  const d=JSON.parse(decodeURIComponent(detailStr));
  const code=d.code;
  const price=d.price, price_time=d.price_time||'', pred=d.pred, conf=d.conf||0.3;
  const ma5=d.ma5||price, ma10=d.ma10||price;
  const h10=d.high_10d||price, l10=d.low_10d||price;
  const volR=d.vol_ratio||1, score=d.score||0;
  const chg1=d.chg_1d||0, chg3=d.chg_3d||0, chg5=d.chg||0;

  // 均线状态文本
  const above=price>ma5;
  const maState=price>ma5&&ma5>ma10?'🟢 多头排列（强势）':price<ma5&&ma5<ma10?'🔴 空头排列（弱势）':price>ma5?'🟡 站上MA5但MA5<MA10（震荡偏强）':price<ma5?'🟡 跌破MA5但MA5>MA10（震荡偏弱）':'⚪ 均线纠缠（方向不明）';

  // 量价判断
  let volNote='量能正常';
  if(volR>2) volNote='🔥 爆量('+volR.toFixed(1)+'x)，多空分歧剧烈';
  else if(volR>1.5) volNote='📈 放量('+volR.toFixed(1)+'x)，交投活跃';
  else if(volR<0.5) volNote='📉 缩量('+volR.toFixed(1)+'x)，交投清淡';
  if(volR>1.3&&chg1>0) volNote+=' → 放量上涨(需求驱动)';
  else if(volR>1.3&&chg1<0) volNote+=' → 放量下跌(供给压力)';

  // 信号拆解
  const signals=[];
  if(d.trend==='up') signals.push({sig:'均线多头',wt:3,dir:'bull'});
  if(d.trend==='down') signals.push({sig:'均线空头',wt:3,dir:'bear'});
  if(d.pos>80) signals.push({sig:'高位超买('+d.pos+'%)',wt:1,dir:'bear'});
  if(d.pos<20) signals.push({sig:'低位超卖('+d.pos+'%)',wt:1,dir:'bull'});
  if(volR>1.5&&chg3>0) signals.push({sig:'放量上涨(vr='+volR.toFixed(1)+')',wt:1,dir:'bull'});
  if(volR>1.5&&chg3<0) signals.push({sig:'放量下跌(vr='+volR.toFixed(1)+')',wt:1,dir:'bear'});
  if(chg5>10) signals.push({sig:'5日强动量('+chg5.toFixed(1)+'%)',wt:2,dir:'bull'});
  if(chg5<-10) signals.push({sig:'5日弱动量('+chg5.toFixed(1)+'%)',wt:2,dir:'bear'});
  const bullSigs=signals.filter(s=>s.dir==='bull');
  const bearSigs=signals.filter(s=>s.dir==='bear');

  let html='<h3>'+d.name+' <span style="color:#64748b;font-size:.8em">'+code+'</span></h3>';

  // === 📊 预测概要 ===
  html+='<div class="det-sec"><div class="det-title">📊 预测概要</div>';
  html+='<div class="det-grid"><span class="k">现价</span><span class="v fw">'+fmt(price)+(price_time?' <span style="font-size:.65em;color:#64748b">'+price_time+'</span>':'')+'</span>';
  html+='<span class="k">趋势预测</span><span class="v"><span class="pred-tag '+pred+'">'+(DIR_MAP[pred]||pred)+'</span></span>';
  html+='<span class="k">置信度</span><span class="v">'+(conf*100).toFixed(0)+'% <span class="cf-bar"><i style="width:'+(conf*100)+'%"></i></span></span>';
  html+='<span class="k">时点</span><span class="v">📍'+(SPOT_MAP[d.spotDir]||d.spotDir||'--')+'</span>';
  html+='<span class="k">预期日收益</span><span class="v '+(d.expRet>=0?'red':'green')+'">'+(d.expRet>=0?'+':'')+(d.expRet||0).toFixed(1)+'%</span>';
  html+='<span class="k">10日位置</span><span class="v">'+d.pos+'% '+(d.pos>80?'⚠️高位':d.pos<20?'💡低位':'')+'</span>';
  html+='</div></div>';

  // === 📈 技术分析 ===
  html+='<div class="det-sec"><div class="det-title">📈 技术分析</div>';
  html+='<div class="det-grid">';
  html+='<span class="k">均线</span><span class="v">MA5 '+fmt(ma5)+' | MA10 '+fmt(ma10)+'</span>';
  html+='<span class="k">排列</span><span class="v">'+maState+'</span>';
  html+='<span class="k">量价</span><span class="v">'+volNote+'</span>';
  html+='<span class="k">波动(ATR)</span><span class="v">'+fmt(d.atr)+'% '+(d.atr>5?'⚠️高波动':d.atr>3?'📊中等':'📉低波')+'</span>';
  html+='<span class="k">涨跌</span><span class="v">1日 <span class="'+(chg1>=0?'red':'green')+'">'+(chg1>=0?'+':'')+chg1.toFixed(1)+'%</span> | 3日 <span class="'+(chg3>=0?'red':'green')+'">'+(chg3>=0?'+':'')+chg3.toFixed(1)+'%</span> | 5日 <span class="'+(chg5>=0?'red':'green')+'">'+(chg5>=0?'+':'')+chg5.toFixed(1)+'%</span></span>';
  html+='<span class="k">关键位</span><span class="v">支撑 '+fmt(l10)+' | 阻力 '+fmt(h10)+'</span>';
  html+='</div></div>';

  // === 🔍 信号拆解 ===
  html+='<div class="det-sec"><div class="det-title">🔍 信号拆解 <span style="font-size:.75em;color:#64748b">(综合分: '+(score>=0?'+':'')+score.toFixed(1)+')</span></div>';
  if(bullSigs.length){
    html+='<div class="sig-row bull"><span class="sig-label">🟢 偏多</span>';
    bullSigs.forEach(s=>{html+='<span class="sig-chip bull-chip">'+s.sig+'<em>w'+s.wt+'</em></span>';});
    html+='</div>';
  }
  if(bearSigs.length){
    html+='<div class="sig-row bear"><span class="sig-label">🔴 偏空</span>';
    bearSigs.forEach(s=>{html+='<span class="sig-chip bear-chip">'+s.sig+'<em>w'+s.wt+'</em></span>';});
    html+='</div>';
  }
  if(!bullSigs.length&&!bearSigs.length){html+='<div style="color:#64748b;padding:4px 0">无明显方向信号</div>';}
  html+='</div>';

  // === 📋 入选原因 ===
  if(d.reason){
    html+='<div class="det-sec"><div class="det-title">📋 入选原因</div>';
    html+='<div class="reason">'+d.reason+'</div></div>';
  }

  // === 📜 历史验证 ===
  html+='<div class="det-sec"><div class="det-title">📜 历史验证 <span style="font-size:.75em;color:#64748b">(最近20条)</span></div>';
  if(!_historyCache[code]){
    try{
      const resp=await fetch('/api/history?code='+encodeURIComponent(code));
      _historyCache[code]=await resp.json();
    }catch(e){_historyCache[code]=[]}
  }
  const hist=_historyCache[code]||[];
  if(hist.length){
    html+='<table class="history-table"><tr><th>预测时间</th><th>趋势</th><th>实际价</th><th>价时</th><th>趋势✓</th><th>时点✓</th></tr>';
    hist.forEach(r=>{
      const tOk=r.correct===1?'ok':r.correct===0?'fail':'';
      const sOk=r.spot_correct===1?'ok':r.spot_correct===0?'fail':'';
      const atime=(r.actual_time||'').substring(0,10);
      html+='<tr><td>'+r.time+'</td><td>'+(DIR_MAP[r.pred]||r.pred)+'</td><td>'+fmt(r.actual)+'</td><td style="font-size:.7em;color:#64748b">'+atime+'</td><td class="'+tOk+'">'+(r.correct===1?'✅':r.correct===0?'❌':'--')+'</td><td class="'+sOk+'">'+(r.spot_correct===1?'✅':r.spot_correct===0?'❌':'--')+'</td></tr>';
    });
    html+='</table>';
  }else{
    html+='<div style="color:#64748b;padding:8px">暂无历史验证数据（需交易日产生真实数据后回填）</div>';
  }
  html+='</div>';

  // === 💡 操作参考 ===
  html+='<div class="det-sec"><div class="det-title">💡 操作参考</div>';
  html+='<div class="det-grid">';
  html+='<span class="k">支撑位</span><span class="v green">'+fmt(l10)+' (10日低)</span>';
  html+='<span class="k">阻力位</span><span class="v red">'+fmt(h10)+' (10日高)</span>';
  const atrVal=(d.atr||0)*price/100;
  html+='<span class="k">ATR幅度</span><span class="v">±'+fmt(atrVal)+' (±'+fmt(d.atr||0)+'%)</span>';
  html+='<span class="k">风控参考</span><span class="v">跌破 '+fmt(l10)+' 且无承接→重新评估</span>';
  html+='</div></div>';

  html+='<button class="close-btn" onclick="closeDetail()">关闭</button>';
  document.getElementById('detailContent').innerHTML=html;
  document.getElementById('detailModal').classList.add('show');
}

function closeDetail(){document.getElementById('detailModal').classList.remove('show')}

function renderSkillLog(){
  const log=DATA.evo_log||[];
  if(!log.length)return;
  let html='<h3>🔧 自进化日志</h3>';
  log.forEach(l=>{
    html+='<div style="font-size:.8em;padding:4px 0;border-bottom:1px solid #334155">';
    html+='<span style="color:#64748b">'+l.time+'</span> ';
    html+='<span style="background:#334155;padding:1px 6px;border-radius:4px;margin-right:4px">'+(l.type||l.event)+'</span>';
    html+='<span>'+l.desc+'</span></div>';
  });
  document.getElementById('evoLogPanel').innerHTML=html;
}
</script>
</body></html>"""

if __name__ == '__main__':
    generate()
