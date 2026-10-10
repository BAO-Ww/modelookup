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
})();
