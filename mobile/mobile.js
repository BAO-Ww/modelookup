/* ============================================================
   型号查询 · 手机版增强层（样机 v1）
   注入位置：<head> 内、模板主脚本之前。
   职责：
   ① fetch 拦截：把桌面版「本地服务」的 /api/* 调用静默合成掉
      （手机上没有本地服务）——模板一行不改，桌面版零风险；
      云端调用（authFetch → /login /perm /pubsync /puball …）
      一律放行，登录/发布同步/补图上云全部真实可用。
   ② 竖屏交互：品牌/品类筛选折叠展开（配合 mobile.css）。
   ============================================================ */
(function(){
"use strict";

/* ---------- ① /api/* 本地服务合成 ---------- */
var FAKE = {
  '/api/state':     function(){ return { status:'done', rev:0, built:'', price_at:'', stock_at:'', alert_ts:0, alert_msg:'', log:[] }; },
  '/api/token':     function(){ return { token:null }; },
  '/api/perm':      function(){ return { ok:true }; },
  '/api/stock':     function(){ return { ok:false, msg:'样机暂不带用友实时库存（正式版从云端拉）' }; },
  '/api/appcheck':  function(){ return { ok:false, msg:'手机版无需软件更新' }; },
  '/api/appinstall':function(){ return { ok:false, msg:'手机版无需软件更新' }; },
  '/api/updateall': function(){ return { ok:false, running:false, msg:'手机版不触发云端更新（数据自动跟随云端）' }; },
  '/api/update':    function(){ return { ok:false, running:false, msg:'手机版不触发云端更新' }; },
  '/api/cancel':    function(){ return { ok:true }; },
  '/api/focus':     function(){ return { ok:true }; },
  '/api/ping':      function(){ return { app:'modelookup-mobile', ver:'0.0.0' }; }
};
var _origFetch = window.fetch;
window.fetch = function(u){
  try{
    var path = String(typeof u === 'string' ? u : (u && u.url) || '');
    if (path.charAt(0) === '/' && path.indexOf('/api/') === 0){
      for (var k in FAKE){
        if (path === k || path.indexOf(k + '?') === 0 || path.indexOf(k) === 0){
          var body = JSON.stringify(FAKE[k]());
          return Promise.resolve(new Response(body, {
            status: 200, headers: { 'Content-Type': 'application/json; charset=utf-8' }
          }));
        }
      }
    }
  }catch(e){}
  return _origFetch.apply(window, arguments);
};

/* ---------- ② 品牌/品类折叠（DOM 就绪后挂一次） ---------- */
function initCollapse(){
  ['brandbox','catbox'].forEach(function(id){
    var box = document.getElementById(id);
    if (!box || box.dataset.mlfold) return;
    box.dataset.mlfold = '1';
    box.classList.remove('open');
    var head = box.querySelector('.fhead');
    if (head) head.addEventListener('click', function(){ box.classList.toggle('open'); });
  });
}
if (document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', initCollapse);
} else {
  initCollapse();
}
/* initUI 重建侧栏后 folding 标记可能被清（innerHTML 未重建 fgroup 本体，保险起见轮询几轮） */
[400, 1200, 3000].forEach(function(ms){ setTimeout(initCollapse, ms); });

/* ============================================================
   ③ 底部导航：首页 / 消息 / 我的（2026-10-11 实机反馈：
      通知横幅滚走找不到 + 需要账号/发布统计的独立入口）
   横幅节点（pubwarn/yyalert/lossalert）整节点搬进「消息」页——
   模板后续 innerHTML 更新与 close 事件仍作用在同一节点上，零侵入。
   ============================================================ */
var ML_VER = 'm1.2';
var ML_ICONS = {
  home: '<svg viewBox="0 0 24 24" fill="none"><path d="M4 11.2 12 4l8 7.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M6.5 10.5V20h11v-9.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  msg:  '<svg viewBox="0 0 24 24" fill="none"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.6 3.6V17H6.5A2.5 2.5 0 0 1 4 14.5v-8Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  me:   '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="8.2" r="3.6" stroke="currentColor" stroke-width="1.8"/><path d="M4.8 20c.9-3.4 3.8-5.2 7.2-5.2s6.3 1.8 7.2 5.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
};
var BANNER_IDS = ['pubwarn', 'yyalert', 'lossalert'];

function mlEscape(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function mlAuth(){
  try { return JSON.parse(localStorage.getItem('ml_auth') || 'null'); } catch(e){ return null; }
}
function mlCount(q, f){
  try { return q((window.__ML && f) ? window.__ML[f] : null); } catch(e){ return 0; }
}

function initTabs(){
  if (document.getElementById('ml-tabbar')) return;
  if (!document.getElementById('lossalert')) return;      // 模板 DOM 未就绪，等下轮

  /* -- 顶栏 = 搜索框 + 方式四分段（合成一个固定头，原生 App 观感） -- */
  var sb = document.querySelector('.side .searchbar');
  if (sb && !document.getElementById('ml-topsearch')){
    sb.id = 'ml-topsearch';
  }
  var header = document.getElementById('ml-header');
  if (!header){
    header = document.createElement('div');
    header.id = 'ml-header';
    document.body.appendChild(header);
  }
  if (sb && sb.parentNode !== header) header.appendChild(sb);

  /* 方式四分段：topbar 的事件监听是容器委托（搬走子节点就断链——实测），
     所以 topbar 整体进抽屉保留原逻辑，顶栏放"影子分段"转发点击、轮询同步选中态 */
  var topbar = document.getElementById('topbar');
  if (topbar && !document.getElementById('ml-topbar2')){
    var bar2 = document.createElement('div');
    bar2.id = 'ml-topbar2';
    [['','全部'],['z','正品'],['l','滞销'],['s','样机']].forEach(function(p){
      var shadow = document.createElement('button');
      shadow.className = 'chip' + (p[0] === '' ? ' on' : '');
      shadow.dataset.kind = p[0];
      shadow.textContent = p[1];
      shadow.addEventListener('click', function(){
        var real = topbar.querySelector('[data-kind="' + p[0] + '"]');
        if (real) real.click();
      });
      bar2.appendChild(shadow);
    });
    header.appendChild(bar2);
  }

  /* 「筛选到 N 条」提示归位到列表上方（不占吸顶区） */
  var hint = document.getElementById('hint');
  var listEl = document.getElementById('list');
  if (hint && listEl && hint.nextElementSibling !== listEl){
    listEl.parentNode.insertBefore(hint, listEl);
  }
  var sh = document.getElementById('stickhead');
  if (sh) sh.style.display = 'none';               // topbar 已进抽屉，原吸顶容器退场
  var sp = document.getElementById('stickspacer');
  if (sp) sp.style.display = 'none';

  /* -- 筛选抽屉：品牌/品类/方式开关价格(原topbar)/清理价/子用户发布 -- */
  if (!document.getElementById('ml-drawer')){
    var drawer = document.createElement('div');
    drawer.id = 'ml-drawer';
    drawer.innerHTML = '<div class="ml-drawer-head"><b>筛选</b>'
      + '<button id="ml-drawer-done">完成</button></div>';
    ['brandbox','catbox'].forEach(function(id){
      var n = document.getElementById(id);
      if (n) drawer.appendChild(n);
    });
    if (topbar){
      var grp = document.createElement('div');
      grp.className = 'ml-drawer-group';
      grp.innerHTML = '<h5>方式 · 开关 · 价格类型</h5>';
      grp.appendChild(topbar);                     // 整节点搬入：委托监听、逻辑零改动
      drawer.appendChild(grp);
    }
    ['tools','subrec','imgfile','jsonfile'].forEach(function(id){
      var n = document.getElementById(id);
      if (n){ drawer.appendChild(n); }
    });
    document.body.appendChild(drawer);
    var mask = document.createElement('div');
    mask.id = 'ml-drawer-mask';
    document.body.appendChild(mask);
    mask.addEventListener('click', mlDrawerClose);
    drawer.querySelector('#ml-drawer-done').addEventListener('click', mlDrawerClose);

    /* 顶栏筛选按钮（漏斗 + 生效条件数角标） */
    var fbtn = document.createElement('button');
    fbtn.id = 'ml-filterbtn';
    fbtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M4 5h16l-6.2 7.4V19l-3.6-2v-4.6L4 5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg><em id="ml-filtercnt" hidden></em>';
    fbtn.addEventListener('click', mlDrawerOpen);
    sb.appendChild(fbtn);
    setInterval(function(){ mlFilterCnt(); mlSyncSeg(); }, 1000);
  }

  /* -- 消息视图：横幅整节点搬入 -- */
  var msg = document.createElement('div');
  msg.id = 'view-msg'; msg.className = 'ml-view'; msg.hidden = true;
  var empty = document.createElement('div');
  empty.className = 'ml-empty'; empty.id = 'ml-msg-empty';
  empty.innerHTML = '暂无消息<br><span style="font-size:12px">亏损提醒 / 没库存提醒 / 用友告警会出现在这里</span>';
  msg.appendChild(empty);
  BANNER_IDS.forEach(function(id){
    var n = document.getElementById(id);
    if (n) msg.appendChild(n);                            // hidden 的照搬，CSS/模板照常管
  });

  /* -- 我的视图 -- */
  var me = document.createElement('div');
  me.id = 'view-me'; me.className = 'ml-view'; me.hidden = true;

  /* -- tabbar -- */
  var nav = document.createElement('nav');
  nav.id = 'ml-tabbar';
  nav.innerHTML =
    '<button data-tab="home" class="on">' + ML_ICONS.home + '首页</button>'
    + '<button data-tab="msg">' + ML_ICONS.msg + '消息<em id="ml-badge" hidden></em></button>'
    + '<button data-tab="me">' + ML_ICONS.me + '我的</button>';
  nav.addEventListener('click', function(e){
    var b = e.target.closest('button');
    if (!b) return;
    var tab = b.dataset.tab;
    ['home','msg','me'].forEach(function(t){
      document.body.classList.toggle('ml-tab-' + t, t === tab);
      nav.querySelector('[data-tab=' + t + ']').classList.toggle('on', t === tab);
    });
    /* 模板全局 [hidden]{display:none!important} 会压过 CSS 切换，hidden 属性必须同步 */
    document.getElementById('view-msg').hidden = tab !== 'msg';
    document.getElementById('view-me').hidden = tab !== 'me';
    if (ML_STATE !== 'drawer'){ ML_STATE = (tab === 'home') ? '' : tab; if (ML_STATE) mlGuardPush(); }
    if (tab === 'msg') window.scrollTo(0, 0);
    if (tab === 'me') mlRenderMe();
    mlUpdateBadge();
  });

  document.body.appendChild(msg);
  document.body.appendChild(me);
  document.body.appendChild(nav);
  setInterval(mlUpdateBadge, 3000);
  mlUpdateBadge();
}

function mlDrawerOpen(){
  document.body.classList.add('ml-drawer-open');
  if (ML_STATE !== 'drawer'){ ML_STATE = 'drawer'; mlGuardPush(); }
}
function mlDrawerClose(){
  document.body.classList.remove('ml-drawer-open');
  if (ML_STATE === 'drawer') ML_STATE = '';
}
function mlFilterCnt(){
  var n = 0, el;
  try {
    el = document.querySelector('#chips .chip.on[data-brand]');
    if (el && el.dataset.brand) n++;
    el = document.querySelector('#catchips .chip.on[data-cat]');
    if (el && el.dataset.cat) n++;
    el = document.getElementById('pmin');
    if (el && el.value) n++;
    el = document.getElementById('pmax');
    if (el && el.value) n++;
  } catch(e){}
  var bd = document.getElementById('ml-filtercnt');
  if (bd){ bd.textContent = n; bd.hidden = !n; }
}

function mlSyncSeg(){
  var topbar = document.getElementById('topbar');
  var bar2 = document.getElementById('ml-topbar2');
  if (!topbar || !bar2) return;
  Array.prototype.forEach.call(topbar.querySelectorAll('[data-kind]'), function(real){
    var sh = bar2.querySelector('[data-kind="' + real.dataset.kind + '"]');
    if (sh) sh.classList.toggle('on', real.classList.contains('on'));
  });
}

/* ---------- 安卓返回键：抽屉开→关抽屉；消息/我的→回首页；再按才退出 ---------- */
var ML_STATE = '';                       // '' | 'drawer' | 'msg' | 'me'
function mlGuardPush(){ try { history.pushState(null, ''); } catch(e){} }
window.addEventListener('popstate', function(){
  if (ML_STATE === 'drawer'){ mlDrawerClose(); ML_STATE = ''; mlGuardPush(); return; }
  if (ML_STATE){ mlSwitchTab('home'); ML_STATE = ''; mlGuardPush(); }
});

function mlUpdateBadge(){
  var nav = document.getElementById('ml-tabbar');
  if (!nav) return;
  var n = 0;
  BANNER_IDS.forEach(function(id){
    var x = document.getElementById(id);
    if (x && !x.hidden && (x.textContent || '').trim()) n++;
  });
  var bd = document.getElementById('ml-badge');
  if (bd){ bd.textContent = n > 9 ? '9+' : n; bd.hidden = !n; }
  var em = document.getElementById('ml-msg-empty');
  if (em) em.style.display = n ? 'none' : 'block';
}

function mlRenderMe(){
  var me = document.getElementById('view-me');
  if (!me) return;
  var a = mlAuth();
  var markedN = 0, allN = 0;
  try { markedN = Object.keys(window.__ML.MARKED || {}).length; } catch(e){}
  try { allN = (window.__ML.ALL || []).length; } catch(e){}
  var built = '';
  try {
    var el = document.querySelector('.sidecount .mt b');
    built = el ? el.textContent : '';
  } catch(e){}
  var roleTxt = !a ? '未登录' : (a.role === 'admin' ? '管理员' : '子账号');
  me.innerHTML =
    '<div class="ml-card"><h4>账号</h4>'
    + '<div class="ml-kv"><span>当前账号</span><b>' + (a ? mlEscape(a.u) : '未登录') + '</b></div>'
    + '<div class="ml-kv"><span>身份</span><b>' + roleTxt + '</b></div>'
    + '<button class="ml-btn danger" id="ml-logout">' + (a ? '退出登录' : '去首页登录') + '</button>'
    + '</div>'
    + '<div class="ml-card"><h4>我的数据</h4>'
    + '<div class="ml-kv"><span>已发布</span><b>' + markedN + ' 个型号</b></div>'
    + '<div class="ml-kv"><span>库内型号</span><b>' + allN + ' 个</b></div>'
    + '<div class="ml-kv"><span>数据快照</span><b>' + mlEscape(built || '见首页侧栏') + '</b></div>'
    + '<div class="ml-kv"><span>App 版本</span><b>' + ML_VER + '</b></div>'
    + '</div>'
    + '<div class="ml-card"><h4>我的补图</h4>'
    + '<button class="ml-btn" id="ml-expimg">导出我补的图</button>'
    + '<button class="ml-btn" id="ml-impimg">导入补图</button>'
    + '</div>';
  me.querySelector('#ml-logout').addEventListener('click', function(){
    if (!a){ mlSwitchTab('home'); return; }
    if (!confirm('退出登录 ' + a.u + '？')) return;
    try { localStorage.setItem('ml_auth', 'null'); } catch(e){}
    try { localStorage.removeItem('ml_auth_host'); } catch(e){}
    location.reload();
  });
  me.querySelector('#ml-expimg').addEventListener('click', function(){
    var b = document.getElementById('expimg'); if (b) b.click();
  });
  me.querySelector('#ml-impimg').addEventListener('click', function(){
    var b = document.getElementById('impimg'); if (b) b.click();
  });
}

function mlSwitchTab(tab){
  var nav = document.getElementById('ml-tabbar');
  if (!nav) return;
  var b = nav.querySelector('[data-tab=' + tab + ']');
  if (b) b.click();
}

if (document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', function(){ setTimeout(initTabs, 300); });
} else {
  setTimeout(initTabs, 300);
}
[1200, 3000, 6000].forEach(function(ms){ setTimeout(initTabs, ms); });
})();
