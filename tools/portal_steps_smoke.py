"""portal_web 多步点击端到端冒烟：本地两步注销页面 → 隐藏 WebView 全链路执行。

web_portal_submit 依赖正在运行的 pywebview GUI 循环（真实应用由主窗口
webview.start() 提供），因此本脚本用一个隐藏 holder 窗口撑起循环，
在循环内执行用例后销毁 holder 退出。

用例：
1) 页面「注销」→ 网页内确认层 →「确定」→「注销成功」；步骤
   [{注销, exact}, {确定, exact}] → 期望 ok=True、命中"认证成功"。
2) 同页面把按钮改为「退出登录」，步骤 [{注销, exact}] 全字匹配
   → 期望 ok=False（未找到按钮），验证全字匹配的严格性。
"""
import http.server
import threading

import webview

from core.portal_web import web_portal_submit

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>portal</title></head>
<body>
<h3>校园网自助</h3>
<button id="logout">注销</button>
<div id="panel" style="display:none">
  <p>确定要退出当前会话吗？</p>
  <button id="confirm">确定</button>
</div>
<p id="status"></p>
<script>
document.getElementById('logout').onclick = function(){
  document.getElementById('panel').style.display = 'block';
};
document.getElementById('confirm').onclick = function(){
  document.getElementById('status').innerText = '注销成功，会话已结束';
};
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    page = PAGE

    def do_GET(self):
        data = self.page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{port}/'

    holder = webview.create_window('smoke-holder', html='<p>hold</p>',
                                   hidden=True)

    def run():
        try:
            ok, msg = web_portal_submit(
                url, '', '', '',
                click_steps=[{'name': '注销', 'match': 'exact'},
                             {'name': '确定', 'match': 'exact'}],
                timeout=25)
            print(f'用例1 两步注销: ok={ok} msg={msg}', flush=True)

            class AltHandler(Handler):
                page = PAGE.replace('>注销<', '>退出登录<')

            server2 = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                      AltHandler)
            port2 = server2.server_address[1]
            threading.Thread(target=server2.serve_forever, daemon=True).start()
            ok2, msg2 = web_portal_submit(
                f'http://127.0.0.1:{port2}/', '', '', '',
                click_steps=[{'name': '注销', 'match': 'exact'}],
                timeout=15)
            print(f'用例2 全字匹配未命中: ok={ok2} msg={msg2}', flush=True)
            server2.shutdown()
        finally:
            holder.destroy()

    webview.start(run)
    server.shutdown()
    print('=== 冒烟结束 ===')


if __name__ == '__main__':
    main()
