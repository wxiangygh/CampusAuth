"""网页化 Portal 认证/注销：用隐藏 pywebview 窗口驱动认证网页。

普通用户只需提供「认证网址」和「点击步骤列表」，本模块负责：
加载页面 → 自动填账号密码（启发式，可高级覆盖选择器）→ 按顺序执行多步点击
（步骤之间等待页面发生变化再推进，支持全字/包含匹配）→ 依据关键词/页面
跳转判定结果。全程复用应用已内置的 pywebview，无新增依赖。

设计要点：
- 隐藏窗口（hidden=True）在后台完成，用户无感；无论成功/失败/取消/超时都销毁窗口，避免泄漏。
- 填值用原生 value setter + 派发 input/change 事件，兼容 React/Vue 等受控组件。
- 多步点击：注销常见"先点注销、再点确认"的多步页面，步骤列表由用户自定义，
  每步可独立选择全字匹配（按钮文本完全相等）或包含匹配（文本包含即命中）。
- 成功判定保守：仅在命中明确成功短语或页面已跳转时判成功；命中明确错误短语判失败；
  其余情况视为"已提交"，真正联网与否交给下游节点验证。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

logger = logging.getLogger('wifi_tray')

# 单步点击后等待页面变化的最长时间；超时视为该步未生效并中止
_STEP_CHANGE_TIMEOUT = 8.0
# 按钮未立即出现时的重试寻找时长（页面仍在渲染的情况）
_STEP_FIND_RETRY = 2.0
# 步骤数上限，防止误配超长列表拖垮整体超时
_MAX_STEPS = 8

# 明确的失败/成功短语（保守，避免"错误""失败"等泛词造成误判）
_DEFAULT_FAIL_RE = re.compile(
    r'密码错误|密码不正确|账号或密码|用户名或密码|账号不存在|用户不存在|认证失败|登录失败|'
    r'incorrect\s+password|invalid\s+(?:password|username|account)|'
    r'authentication\s+failed|login\s+failed',
    re.IGNORECASE)
_DEFAULT_SUCCESS_RE = re.compile(
    r'认证成功|登录成功|注销成功|已登录|正在登录|正在跳转|欢迎您|'
    r'log(?:in|ged)\s+in|logout\s+succe|login\s+succe',
    re.IGNORECASE)

# 填充账号密码：CFG 占位符会被替换为 JSON 配置对象
_FILL_JS = r"""(function(cfg){
  function visible(el){
    if(!el) return false;
    try{
      var r=el.getBoundingClientRect(), s=window.getComputedStyle(el);
      return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none';
    }catch(e){ return false; }
  }
  function setVal(el,v){
    if(!el) return false;
    try{
      var proto=(el.tagName==='TEXTAREA')?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;
      var d=Object.getOwnPropertyDescriptor(proto,'value');
      if(d&&d.set) d.set.call(el,v); else el.value=v;
      el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
      el.dispatchEvent(new Event('blur',{bubbles:true}));
      return true;
    }catch(e){ return false; }
  }
  function bySelector(sel){
    if(!sel) return null;
    try{ return document.querySelector(sel); }catch(e){ return null; }
  }
  var passEl=bySelector(cfg.pass_selector);
  if(!passEl||!visible(passEl)){
    var pw=Array.prototype.filter.call(document.querySelectorAll('input[type=password]'),visible);
    passEl=pw.length?pw[0]:null;
  }
  var userEl=bySelector(cfg.user_selector);
  if(!userEl||!visible(userEl)){
    var re=/(user|account|name|mobile|phone|手机|学号|工号|用户|账号|姓名)/i;
    var texts=Array.prototype.filter.call(
      document.querySelectorAll('input[type=text],input[type=email],input[type=tel],input[type=number],input:not([type])'),
      visible);
    userEl=texts.filter(function(e){
      var s=(e.name||'')+' '+(e.id||'')+' '+(e.placeholder||'')+' '+(e.getAttribute('aria-label')||'');
      return re.test(s);
    })[0]||null;
    if(!userEl&&passEl){
      var all=Array.prototype.slice.call(document.querySelectorAll('input'));
      var pi=all.indexOf(passEl);
      for(var i=pi-1;i>=0;i--){
        var e=all[i], t=(e.type||'text').toLowerCase();
        if(visible(e)&&(t==='text'||t==='email'||t==='tel'||t==='number'||t==='')){ userEl=e; break; }
      }
    }
    if(!userEl&&texts.length) userEl=texts[0];
  }
  var filled_user=setVal(userEl,cfg.username);
  var filled_pass=setVal(passEl,cfg.password);
  return JSON.stringify({
    filled_user:filled_user, filled_pass:filled_pass,
    has_user:!!userEl, has_pass:!!passEl
  });
})(__CFG__);"""

# 点击一步：CFG={names:[按钮名(支持逗号分隔多个别名)], exact:bool}
# 匹配策略顺序：可见文本 → value → name → id → title/aria → 深扫描文本。
# exact=true 全字匹配（规范化空白后完全相等），false 包含匹配。
# names 为空时回退点击第一个可见 submit 按钮（兼容未配置按钮名的历史用法）。
# 深扫描：网页内确认弹层的按钮常是绑定事件的 <a>/<span>（如 layui-layer
# 的 <a class="layui-layer-btn0">确定</a>），不在传统按钮标签集合里，
# 按文本在全元素中找"最内层"（文本最短）的可见匹配才能命中。
_DEEP_CLICK_TAIL = r"""
  if(!btn){
    var all=document.querySelectorAll('a,span,div,p,li,label');
    var best=null, bestLen=1e6;
    for(var j=0;j<all.length;j++){
      var e2=all[j];
      if(!visible(e2)) continue;
      var t2=norm(e2.innerText||''), v2=norm(e2.value||'');
      var hit=false;
      for(var k=0;k<names.length;k++){
        var n2=names[k]; if(!n2) continue;
        if(exact){ if(t2===n2||v2===n2){ hit=true; break; } }
        else if((t2&&t2.indexOf(n2)>=0)||(v2&&v2.indexOf(n2)>=0)){ hit=true; break; }
      }
      if(hit && t2.length>0 && t2.length<bestLen){ best=e2; bestLen=t2.length; }
    }
    if(best){ btn=best; matched_by='deep-text'; }
  }
  var clicked=false;
  if(btn){
    try{ btn.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); }catch(e){}
    try{ btn.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); }catch(e){}
    try{ btn.click(); clicked=true; }catch(e){ clicked=false; }
  }
  return JSON.stringify({clicked:clicked, matched_by:matched_by});
})(__CFG__);"""

_CLICK_STEP_JS = r"""(function(cfg){
  function visible(el){
    if(!el) return false;
    try{
      var r=el.getBoundingClientRect(), s=window.getComputedStyle(el);
      return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none';
    }catch(e){ return false; }
  }
  function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
  var names=cfg.names||[], exact=!!cfg.exact;
  var cands=Array.prototype.filter.call(
    document.querySelectorAll('button,input[type=submit],input[type=button],a[role=button],[onclick]'),
    visible);
  function hitAny(s){
    if(!s) return false;
    for(var i=0;i<names.length;i++){
      var n=names[i]; if(!n) continue;
      if(exact){ if(norm(s)===n) return true; }
      else if(s.indexOf(n)>=0) return true;
    }
    return false;
  }
  function firstBy(get){
    for(var i=0;i<cands.length;i++){ if(hitAny(get(cands[i]))) return cands[i]; }
    return null;
  }
  var btn=null, matched_by='';
  if(!names.length){
    btn=cands.filter(function(e){return (e.type||'')==='submit';})[0]||cands[0]||null;
    matched_by=btn?'fallback':'';
  } else {
    btn=firstBy(function(e){return norm(e.innerText||e.textContent||'');}); if(btn) matched_by='text';
    if(!btn){ btn=firstBy(function(e){return norm(e.value||'');}); if(btn) matched_by='value'; }
    if(!btn){ btn=firstBy(function(e){return (e.name||'');}); if(btn) matched_by='name'; }
    if(!btn){ btn=firstBy(function(e){return (e.id||'');}); if(btn) matched_by='id'; }
    if(!btn){ btn=firstBy(function(e){return norm((e.title||'')+' '+(e.getAttribute('aria-label')||''));}); if(btn) matched_by='aria'; }
  }""" + _DEEP_CLICK_TAIL

_POLL_JS = ("JSON.stringify({text:(document.body?document.body.innerText:'').slice(0,4000),"
            "url:location.href})")

# 探测登录表单是否已渲染（可见密码框数量；0 = 未就绪）
_HAS_FORM_JS = ("(function(){try{"
                "var n=0,l=document.querySelectorAll('input[type=password]');"
                "for(var i=0;i<l.length;i++){"
                "var r=l[i].getBoundingClientRect();"
                "if(r.width>0&&r.height>0)n++;}"
                "return String(n);}catch(e){return '0';}})()")


def _evaluate(window, js):
    """安全执行 evaluate_js，失败返回 None。"""
    try:
        return window.evaluate_js(js)
    except Exception as exc:
        logger.debug('portal_web evaluate_js failed: %s', exc)
        return None


def _parse_json(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def normalize_click_steps(value, limit: int = _MAX_STEPS) -> list[dict]:
    """把用户配置的点击步骤规范化为 [{name, match}]，非法项剔除。

    match 仅接受 'exact'（全字匹配）/ 'contains'（包含匹配），缺省 contains。
    name 按逗号/顿号等拆分为多个别名时保留原始字符串，由 JS 端统一拆分。
    """
    if not isinstance(value, (list, tuple)):
        return []
    steps = []
    for item in value[:limit]:
        if isinstance(item, str):
            item = {'name': item}
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        match = str(item.get('match') or 'contains').strip().lower()
        if match not in ('exact', 'contains'):
            match = 'contains'
        steps.append({'name': name, 'match': match})
    return steps


def build_click_steps(click_steps, button_name: str = '') -> list[dict]:
    """合成实际执行的步骤列表：click_steps 优先，空则回退单按钮（包含匹配）。

    button_name 兼容旧配置（单个「按钮名称」字段）；两者都为空时返回空列表，
    执行端回退点击第一个可见 submit 按钮（历史行为）。
    """
    steps = normalize_click_steps(click_steps)
    if steps:
        return steps
    name = str(button_name or '').strip()
    if name:
        return [{'name': name, 'match': 'contains'}]
    return []


def _step_aliases(name: str) -> list[str]:
    """步骤名拆分为多个别名（与旧按钮名称一致的分隔符）。"""
    return [s for s in re.split(r'[,，、/|；;\s]+', name or '') if s]


def web_portal_submit(url, username, password, button_name, *,
                      user_selector='', pass_selector='',
                      success_keyword='', fail_keyword='',
                      click_steps=None,
                      timeout=15.0, cancelled=lambda: False) -> tuple[bool, str]:
    """打开认证网址，自动填账号密码并按步骤点击，返回 (是否成功, 消息)。

    Args:
        url: 完整认证网址（用户填写，系统不拼接）。
        username/password: 账号密码（来自全局配置；注销页可为空串）。
        button_name: 旧版单「按钮名称」兼容参数，仅当 click_steps 为空时使用。
        click_steps: 多步点击列表 [{name, match}]，match='exact'(全字匹配) 或
            'contains'(包含匹配)；按顺序执行，步骤之间等待页面发生变化再推进。
        user_selector/pass_selector: 可选高级覆盖选择器。
        success_keyword/fail_keyword: 可选自定义成功/失败关键词（步骤等待与
            最终判定阶段都会检查，越早命中越早返回）。
        timeout: 整体超时（秒），受调用方剩余时限约束。
        cancelled: 取消检测回调。

    Returns:
        (success, message)。按钮无法点击等硬性失败返回 False；无明确信号时
        以"已提交"为成功，交由下游节点验证真实连通性。
    """
    url = str(url or '').strip()
    if not url:
        return False, '认证网址未配置'
    if not url.lower().startswith(('http://', 'https://')):
        url = 'http://' + url

    steps = build_click_steps(click_steps, button_name)
    try:
        import webview
    except Exception as exc:  # pragma: no cover - 运行环境始终有 pywebview
        logger.error('portal_web: pywebview 不可用: %s', exc)
        return False, f'网页认证组件不可用：{exc}'

    started = time.monotonic()
    total_timeout = max(3.0, float(timeout))

    def remaining() -> float:
        return max(0.0, total_timeout - (time.monotonic() - started))

    window = None
    try:
        window = webview.create_window(
            'CampusAuth Portal', url,
            width=1000, height=780, hidden=True, resizable=True)
    except Exception as exc:
        logger.error('portal_web: 创建认证窗口失败: %s', exc)
        return False, f'打开认证网页失败：{exc}'
    if window is None:
        return False, '打开认证网页失败'

    def check_keywords(text):
        """步骤等待期间也检查关键词：命中失败短语返回 (False, msg)，
        命中成功短语返回 (True, msg)，无信号返回 None。"""
        text = text or ''
        if fail_kw and fail_kw in text:
            return False, f'认证失败：{_snippet(text, fail_kw)}'
        m = _DEFAULT_FAIL_RE.search(text)
        if m:
            return False, f'认证失败：{_snippet(text, m.group(0))}'
        if success_kw and success_kw in text:
            return True, '认证成功'
        if _DEFAULT_SUCCESS_RE.search(text):
            return True, '认证成功'
        return None

    try:
        # === 等待页面加载 ===
        loaded = threading.Event()
        try:
            window.events.loaded += lambda *a: loaded.set()
        except Exception:
            pass
        load_budget = min(remaining(), max(4.0, total_timeout * 0.6))
        loaded.wait(load_budget)
        if cancelled():
            return False, '已取消'
        # loaded 事件可能因隐藏窗口未触发，用 readyState 兜底确认
        deadline = time.monotonic() + min(remaining(), 4.0)
        ready = False
        while time.monotonic() < deadline and not cancelled():
            state = _evaluate(window, 'document.readyState')
            if state == 'complete':
                ready = True
                break
            time.sleep(0.2)
        if cancelled():
            return False, '已取消'
        if not ready and not loaded.is_set():
            logger.warning('portal_web: 页面加载未确认，仍尝试提交（%s）', url)

        # === 填充账号密码（登录流；注销页填不进也无妨）===
        if str(username or '') or str(password or ''):
            cfg = {
                'username': str(username or ''),
                'password': str(password or ''),
                'user_selector': str(user_selector or ''),
                'pass_selector': str(pass_selector or ''),
            }
            fill_js = _FILL_JS.replace('__CFG__', json.dumps(cfg, ensure_ascii=False))
            fill_result = _parse_json(_evaluate(window, fill_js))
            if cancelled():
                return False, '已取消'
            if not fill_result:
                return False, '认证页面无法交互（可能未加载完成或脚本被拦截）'
            if (cfg['username'] and not fill_result.get('filled_user')) or \
               (cfg['password'] and not fill_result.get('filled_pass')):
                logger.warning('portal_web: 账号/密码可能未填入：user=%s pass=%s',
                               fill_result.get('has_user'), fill_result.get('has_pass'))

        # === 按顺序执行点击步骤，步骤之间等待页面变化 ===
        fail_kw = str(fail_keyword or '').strip()
        success_kw = str(success_keyword or '').strip()
        origin_url = url
        for idx, step in enumerate(steps):
            aliases = _step_aliases(step['name'])
            click_cfg = {'names': aliases, 'exact': step['match'] == 'exact'}
            click_js = _CLICK_STEP_JS.replace(
                '__CFG__', json.dumps(click_cfg, ensure_ascii=False))
            label = '/'.join(aliases)
            # 变化基线必须在点击前截取：btn.click() 同步触发 DOM 更新，
            # 点击后再截图会把已变化的内容当基线，导致永远"无变化"
            prev = _parse_json(_evaluate(window, _POLL_JS)) or {}
            # 按钮可能随页面渲染稍后出现：短暂重试寻找
            click_result = None
            find_deadline = time.monotonic() + min(remaining(), _STEP_FIND_RETRY)
            while True:
                click_result = _parse_json(_evaluate(window, click_js))
                if (click_result and click_result.get('clicked')) \
                        or cancelled() or time.monotonic() >= find_deadline:
                    break
                time.sleep(0.25)
            if cancelled():
                return False, '已取消'
            if not click_result or not click_result.get('clicked'):
                hint = f'（第 {idx + 1}/{len(steps)} 步，按钮「{label}」，' \
                       f'{"全字" if step["match"] == "exact" else "包含"}匹配）'
                return False, f'未找到或无法点击按钮{hint}'
            logger.info('portal_web: 第 %d/%d 步已点击「%s」（%s匹配，命中方式=%s）',
                        idx + 1, len(steps), label, step['match'],
                        click_result.get('matched_by', ''))
            if idx == len(steps) - 1:
                break  # 最后一步：交给下方结果判定

            # 等待页面变化（文本或 URL 任一变化即推进）
            change_deadline = time.monotonic() + min(
                remaining(), _STEP_CHANGE_TIMEOUT)
            changed = False
            while time.monotonic() < change_deadline and not cancelled():
                cur = _parse_json(_evaluate(window, _POLL_JS))
                if cur:
                    verdict = check_keywords(cur.get('text', ''))
                    if verdict is not None:
                        return verdict
                    if (cur.get('url') or '') != (prev.get('url') or '') or \
                            (cur.get('text') or '') != (prev.get('text') or ''):
                        changed = True
                        break
                time.sleep(0.3)
            if cancelled():
                return False, '已取消'
            if not changed:
                return False, (f'第 {idx + 1} 步「{label}」点击后页面无变化，'
                               f'已中止（请确认按钮名称与页面弹窗形式）')
            logger.info('portal_web: 第 %d 步后页面已变化，继续下一步', idx + 1)

        # === 轮询判定最终结果 ===
        poll_deadline = time.monotonic() + remaining()
        while time.monotonic() < poll_deadline and not cancelled():
            snap = _parse_json(_evaluate(window, _POLL_JS))
            if snap:
                verdict = check_keywords(snap.get('text', ''))
                if verdict is not None:
                    return verdict
                last_url = snap.get('url', '') or ''
                if last_url and last_url != origin_url \
                        and 'about:blank' not in last_url:
                    return True, '已提交（页面已跳转）'
            time.sleep(0.5)
        if cancelled():
            return False, '已取消'
        # 超时仍无明确信号：视为已提交，交下游验证
        return True, '已提交认证，等待网络验证'
    except Exception as exc:
        logger.exception('portal_web: 认证过程异常')
        return False, f'网页认证异常：{exc}'
    finally:
        _destroy(window)


def _snippet(text: str, keyword: str, width: int = 60) -> str:
    """截取关键词附近的一小段文本用于消息展示。"""
    try:
        idx = text.index(keyword)
    except ValueError:
        return text[:width].strip()
    start = max(0, idx - 10)
    return text[start:start + width].strip()


def _destroy(window) -> None:
    if window is None:
        return
    try:
        window.destroy()
    except Exception as exc:
        logger.debug('portal_web: 销毁认证窗口失败: %s', exc)
