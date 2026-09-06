"""调试 BJUT Portal 注销流程：驱动真实页面，逐步转储按钮与页面状态。"""
import json
import threading

import webview

URL = 'https://lgn.bjut.edu.cn/a79.htm'

DUMP_JS = r"""(function(){
  function visible(el){
    try{
      var r=el.getBoundingClientRect(), s=window.getComputedStyle(el);
      return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';
    }catch(e){ return false; }
  }
  function dumpDoc(doc, label, out){
    var list=doc.querySelectorAll('button,input[type=submit],input[type=button],a[role=button],[onclick]');
    for(var i=0;i<list.length;i++){
      var e=list[i];
      out.push({frame:label, tag:e.tagName,
        text:((e.innerText||e.textContent||'')+'').replace(/\s+/g,' ').trim().slice(0,30),
        value:(e.value||'').slice(0,30), name:(e.name||''), id:(e.id||''),
        outer:(e.outerHTML||'').slice(0,200),
        visible:visible(e)});
    }
    for(var f=0;f<doc.defaultView.frames.length;f++){
      try{ dumpDoc(doc.defaultView.frames[f].document, label+':'+f, out); }catch(e){}
    }
  }
  var out=[];
  try{ dumpDoc(document, 'top', out); }catch(e){ out.push({error:String(e)}); }
  var src=(document.documentElement?document.documentElement.outerHTML:'');
  var snippets=[];
  var idx=0;
  while((idx=src.indexOf('logout', idx))>=0){
    snippets.push(src.slice(Math.max(0,idx-60), idx+120));
    idx+=6;
    if(snippets.length>=6) break;
  }
  return JSON.stringify({url:location.href, title:document.title, buttons:out,
    logout_snippets:snippets});
})()"""

CLICK_JS = r"""(function(name){
  function visible(el){
    try{
      var r=el.getBoundingClientRect(), s=window.getComputedStyle(el);
      return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';
    }catch(e){ return false; }
  }
  var found=null, where='';
  function scan(doc, label){
    var list=doc.querySelectorAll('button,input[type=submit],input[type=button],a[role=button],[onclick]');
    for(var i=0;i<list.length;i++){
      var e=list[i];
      var t=((e.innerText||e.textContent||'')+'').replace(/\s+/g,' ').trim();
      var v=(e.value||'');
      if(!found && (t.indexOf(name)>=0 || v.indexOf(name)>=0)){ found=e; where=label+'/text='+t+'/value='+v; }
    }
    for(var f=0;f<doc.defaultView.frames.length;f++){
      try{ scan(doc.defaultView.frames[f].document, label+':'+f); }catch(e){}
    }
  }
  scan(document, 'top');
  if(found){ try{ found.click(); }catch(e){ return JSON.stringify({clicked:false, error:String(e)}); } }
  return JSON.stringify({clicked:!!found, where:where});
})"""


def main():
    window = webview.create_window('portal-debug', URL, width=1000, height=780,
                                   hidden=True)
    holder_ready = threading.Event()

    def run():
        try:
            import time
            time.sleep(3)
            for attempt in range(3):
                state = window.evaluate_js('document.readyState')
                print(f'readyState={state}', flush=True)
                if state == 'complete':
                    break
                time.sleep(1)
            dump = window.evaluate_js(DUMP_JS)
            print('=== 点击前 ===', flush=True)
            data = json.loads(dump)
            print('buttons:', json.dumps(data.get('buttons'), ensure_ascii=False)[:900], flush=True)
            for snip in data.get('logout_snippets', []):
                print('logout源码片段:', repr(snip[:170]), flush=True)

            js = CLICK_JS + "('%s');" % '本机注销'
            r = window.evaluate_js(js)
            print('点击本机注销:', r, flush=True)
            time.sleep(2.5)
            dump = window.evaluate_js(DUMP_JS)
            data = json.loads(dump)
            print('=== 点击后 ===', flush=True)
            print('url:', data.get('url'), flush=True)
            print('buttons:', json.dumps(data.get('buttons'), ensure_ascii=False)[:900], flush=True)
        finally:
            holder_ready.set()

    def closer():
        holder_ready.wait(60)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=closer, daemon=True).start()
    # pywebview 需要 func 在循环内执行
    webview.start(run)


if __name__ == '__main__':
    main()
