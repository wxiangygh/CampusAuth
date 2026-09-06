"""BJUT Portal 注销全流程真机验证：本机注销 → 网页内确认层(确定/取消) → 注销成功。

重点验证：确认层的「确定」按钮的真实标签形态，以及深扫描文本点击能否命中。
"""
import json
import threading

import webview

URL = 'https://lgn.bjut.edu.cn/a79.htm'

# 深扫描点击：先按钮标签集合，再扩展到 a/span/div/p/li/label（网页内弹层按钮
# 常是绑定事件的 <a>，不在传统按钮集合里）；取文本最短（最内层）的可见匹配。
DEEP_CLICK_JS = r"""(function(name){
  function visible(el){
    try{
      var r=el.getBoundingClientRect(), s=window.getComputedStyle(el);
      return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';
    }catch(e){ return false; }
  }
  function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
  var btn=null, where='';
  var sel='button,input[type=submit],input[type=button],a[role=button],[onclick]';
  var list=document.querySelectorAll(sel);
  for(var i=0;i<list.length;i++){
    var e=list[i];
    var t=norm(e.innerText||e.textContent||''), v=norm(e.value||'');
    if((t&&t.indexOf(name)>=0)||(v&&v.indexOf(name)>=0)){ btn=e; where='button-set|'+e.tagName; break; }
  }
  if(!btn){
    var all=document.querySelectorAll('a,span,div,p,li,label');
    var best=null, bestLen=1e6;
    for(var j=0;j<all.length;j++){
      var e2=all[j];
      if(!visible(e2)) continue;
      var t2=norm(e2.innerText||''), v2=norm(e2.value||'');
      var hit=((t2&&t2.indexOf(name)>=0)||(v2&&v2.indexOf(name)>=0));
      if(hit && t2.length>0 && t2.length<bestLen){ best=e2; bestLen=t2.length; }
    }
    if(best){ btn=best; where='deep-text|'+best.tagName+'|len='+bestLen; }
  }
  if(!btn) return JSON.stringify({clicked:false});
  try{ btn.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); }catch(e){}
  try{ btn.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); }catch(e){}
  try{ btn.click(); }catch(e){ return JSON.stringify({clicked:false,error:String(e)}); }
  return JSON.stringify({clicked:true, where:where,
    outer:(btn.outerHTML||'').slice(0,150)});
})"""

SNAP_JS = r"""(function(){
  var out=[];
  var all=document.querySelectorAll('a,span,button,input');
  for(var i=0;i<all.length;i++){
    var e=all[i];
    var t=((e.innerText||e.value||'')+'').replace(/\s+/g,' ').trim();
    if(t==='确定'||t==='取消'){ out.push({tag:e.tagName, text:t, outer:(e.outerHTML||'').slice(0,140)}); }
  }
  return JSON.stringify({matches:out, text:(document.body.innerText||'').slice(0,200)});
})()"""


def main():
    window = webview.create_window('portal-verify', URL, width=1000, height=780,
                                   hidden=True)
    done = threading.Event()

    def run():
        try:
            import time
            time.sleep(3)
            for _ in range(3):
                if window.evaluate_js('document.readyState') == 'complete':
                    break
                time.sleep(1)
            r1 = json.loads(window.evaluate_js(DEEP_CLICK_JS + "('本机注销');"))
            print('① 点击本机注销:', r1, flush=True)
            time.sleep(2)
            snap = json.loads(window.evaluate_js(SNAP_JS))
            print('② 确认层元素:', json.dumps(snap['matches'], ensure_ascii=False)[:500], flush=True)
            r2 = json.loads(window.evaluate_js(DEEP_CLICK_JS + "('确定');"))
            print('③ 点击确定:', r2, flush=True)
            time.sleep(4)
            final = window.evaluate_js(SNAP_JS)
            print('④ 注销后页面文本:', json.loads(final)['text'][:160], flush=True)
        finally:
            done.set()

    def closer():
        done.wait(60)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=closer, daemon=True).start()
    webview.start(run)


if __name__ == '__main__':
    main()
