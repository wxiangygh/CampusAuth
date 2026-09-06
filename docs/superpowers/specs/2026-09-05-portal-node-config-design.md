# 工作流节点自定义配置 + 网页化 Portal 认证 设计

## 背景

CampusAuth 的工作流节点目前只允许配置通用执行参数（启用、超时、重试、重试间隔、失败跳过）。校园网 Portal 认证/注销的服务器地址（`portal_ip` / `portal_port`）是**全局配置**，放在设置页，且登录逻辑写死为对 dr1003 JSONP 接口 `http://{ip}:{port}/eportal/portal/login` 的直连 HTTP GET（见 `core/auth.py:111` / `:172`）。

这带来几个问题：

1. **普通用户难以配置**：直连 HTTP 方式要求用户理解抓包得到的接口地址与参数，普通用户不会抓包，但他们知道"认证网页的网址"和"登录按钮的名称"。
2. **无法区分有线/无线**：很多校园网的有线（宿舍网口）与无线（校园 WiFi）使用不同的认证地址或参数，当前只有一套全局配置。
3. **服务器地址是全局的**：不同工作流、不同节点无法各自指定认证地址。
4. **自动重连行为写死**：`reconnect_watchdog` 直接调用 `connect_warp_result` 重连 WARP，用户无法自定义断开后执行什么。

## 设计目标

- 工作流节点支持**每节点自定义配置参数**，首先落地 Portal 认证/注销两类节点。
- Portal 认证/注销新增**网页点击方式**：加载用户提供的完整认证网址 → 自动填账号密码 → 点击登录按钮；作为普通用户的首选方式。
- **保留现有直连 HTTP（dr1003 JSONP）方式**作为可选，内置默认工作流与老用户零迁移、行为不变。
- Portal 节点支持**有线/无线两套配置分别保存**，运行时"用户指定优先，未指定则自动检测（优先无线）"。
- 前端：选中单个节点时底部悬浮操作区新增**「编辑」按钮**；**双击节点**同样打开配置弹窗；弹窗**居中、不可移动**。
- 设置页**移除「校园网认证服务器」**，认证地址改由节点配置填写。
- 设置页新增**「自动重连工作流绑定」**下拉（必选），看门狗改为运行绑定的工作流并在主界面显示进度。

## 关键决策记录（与用户确认）

| 议题 | 决策 |
| --- | --- |
| 「登录按钮名称」含义 | 改用网页点击认证：加载认证网页、按名称定位并点击登录按钮 |
| 「认证网址」范围 | 用户填写**完整网址**（含账号密码输入页），系统不做任何拼接 |
| 渲染/驱动方式 | 复用 **pywebview 隐藏窗口**（全自动，无新依赖） |
| 账号/密码框定位 | **自动启发式**为主，节点配置提供**可选高级覆盖选择器** |
| 成功判定 | 启发式错误检测 + 下游节点验证；节点可选填**自定义成功/失败关键词** |
| 按钮匹配 | **多策略自动匹配**：innerText → value → name → id → title/aria-label |
| 有线/无线运行时选择 | **自动检测 + 允许手动指定**；指定优先，未指定自动检测 |
| 有线/无线检测方法 | **优先无线的简单启发式** |
| 现有 HTTP-JSONP | **混合保留**：节点新增「认证方式」下拉（网页点击 / 直连 HTTP），默认网页点击 |
| 自动重连默认绑定 | **default_auth**（完整认证工作流） |
| 自动重连进度 | **在主界面显示进度**（与手动认证一致） |

## 方案选择

**混合方案（已选定）**：Portal 节点通过 `params.method` 在"网页点击"与"直连 HTTP"之间切换。

- 网页点击：新增 `core/portal_web.py`，用 pywebview 隐藏窗口驱动认证页。
- 直连 HTTP：沿用 `core/auth.py` 现有 `portal_login` / `portal_logout`，仅允许按节点覆盖服务器地址。

内置工作流的 Portal 节点 `params` 为空 ⇒ 运行时回退为 HTTP 方式 + 全局 `portal_ip:portal_port` ⇒ **开箱行为与现在完全一致，无需迁移**。用户新加的 Portal 节点默认网页点击方式，由用户填写网址与按钮名。

不选"完全替换为网页点击"：会让内置默认工作流的 portal 节点（dr1003 JSONP 接口并非可点击页面）在用户填入真实登录页前失效，破坏现有安装。

## 详细设计

### 1. 数据模型：`StepSpec.params`

`core/workflow.py` 的 `StepSpec` 新增字段并在 `from_dict` 中保留：

```python
@dataclasses.dataclass(frozen=True)
class StepSpec:
    id: str
    enabled: bool = True
    retries: int = 0
    timeout: float = 15.0
    retry_delay: float = 1.0
    continue_on_error: bool = False
    params: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        raw_params = value.get("params")
        return cls(...,
                   params=dict(raw_params) if isinstance(raw_params, dict) else {})
```

- `core/config.py:_normalize_steps` 已对每个 step 做 `copy.deepcopy(item)`，**params 天然被持久化**，无需改配置层。
- 动作函数签名为 `action(context, step)`，可直接读 `step.params`。
- `params` 只对需要的节点有意义；其他节点为空 dict。

**Portal 节点 `params` 结构**（`portal_login` 与 `portal_logout` 相同）：

```jsonc
{
  "link_mode": "auto",              // auto | wired | wireless（运行时用哪套）
  "wireless": {
    "method": "web",                // web | http
    "auth_url": "",                 // web：完整认证网址（用户填写，不拼接）
    "button_name": "",              // web：登录/注销按钮名称
    "user_selector": "",            // web 高级：账号框选择器（可选）
    "pass_selector": "",            // web 高级：密码框选择器（可选）
    "success_keyword": "",          // web 高级：成功关键词（可选）
    "fail_keyword": "",             // web 高级：失败关键词（可选）
    "server": ""                    // http："ip:port"，留空回退全局 portal_ip:portal_port
  },
  "wired": { /* 同 wireless 结构 */ }
}
```

### 2. 有线/无线检测：`core/network.py:detect_link_type()`

```python
def detect_link_type() -> str:
    """返回 'wireless' 或 'wired'。优先无线的简单启发式。"""
```

判定顺序：
1. `netsh wlan show interfaces` 输出中存在已连接（`已连接` / `connected`）且有 SSID ⇒ `wireless`。
2. 否则检测以太网适配器：`Get-NetAdapter -Physical`（或 `ipconfig`）中存在非无线、`Status=Up`/`已连接` 且分到 IP 的适配器 ⇒ `wired`。
3. 都判不出 ⇒ `wireless`（兜底，与现状一致）。

### 3. 网页化 Portal 自动化：新增 `core/portal_web.py`

```python
def web_portal_submit(url, username, password, button_name, *,
                      user_selector="", pass_selector="",
                      success_keyword="", fail_keyword="",
                      timeout=15.0, cancelled=lambda: False) -> tuple[bool, str]:
    """用隐藏 pywebview 窗口打开认证页，自动填账号密码并点击按钮，返回 (成功, 消息)。"""
```

流程：
1. `webview.create_window(title, url, hidden=True, width=1000, height=760)`（无需 `js_api`）。
2. 等待页面加载：订阅 `win.events.loaded` 或轮询 `win.evaluate_js('document.readyState')` 直到 `complete`（受 `timeout` 与 `cancelled` 约束）。
3. 一次 `evaluate_js` 完成填充 + 点击，返回 JSON 状态 `{filled_user, filled_pass, clicked, matched_by}`：
   - **定位密码框**：优先 `pass_selector`；否则取可见的 `input[type=password]`。
   - **定位账号框**：优先 `user_selector`；否则在可见 `input[type=text|email|tel]` 中取 name/id/placeholder 命中 `/user|account|name|手机|学号|用户|账号/i` 者，再退化为密码框之前最近的可见文本输入框。
   - **写值**：用原生 value setter 赋值后派发 `input`、`change` 事件（兼容 React/Vue 受控组件）。
   - **定位按钮**：候选 `button, input[type=submit], input[type=button], a[role=button], [onclick]`；按 innerText → value → name → id → title/aria-label 顺序做**包含匹配**，命中第一个可点击元素 `.click()`。
4. 结果轮询（每 ~0.5s，至 `timeout`）：`evaluate_js` 读取 `document.body.innerText` 与 `location.href`：
   - 命中 `fail_keyword`（或默认错误词 `密码错误|账号或密码|用户名或密码|认证失败|不存在|错误`）⇒ 失败并返回页面提示。
   - 命中 `success_keyword`（或默认成功词 `成功|已登录|欢迎`，或 URL 已离开 `auth_url`）⇒ 成功。
   - 超时仍无明确信号 ⇒ 视为"已提交"成功（真正联网与否交给下游节点验证）。
5. **无论成功/失败/取消/超时都 `win.destroy()`**，避免窗口泄漏；异常路径用 try/finally 保证。

**风险与兜底**：隐藏 WebView2 是否可靠执行 JS 需在实现早期用本地 HTML fixture 验证；若隐藏窗口不渲染，退化为"创建到屏幕外/最小化"的窗口。认证页若把表单放在 iframe 内，本期不处理（记为非目标，后续可扩展遍历同源 iframe）。

### 4. Portal 动作分派：`core/auth_workflow.py` + `core/auth.py`

`core/auth.py` 的 `portal_login` / `portal_logout`（HTTP）**保持不变**，作为 legacy 路径。

`core/auth_workflow.py` 新增参数解析并改造两个动作：

```python
def _resolve_portal_params(step, config) -> dict:
    """按 link_mode（auto 时调用 detect_link_type）选出 wired/wireless 变体，
    并补齐默认值：method 缺省 http、server 缺省全局 portal_ip:portal_port。"""

def _portal_login(context, step):
    if _skip_if_ready(context): ...
    p = _resolve_portal_params(step, context.config)
    if p["method"] == "web":
        from core.portal_web import web_portal_submit
        ok, msg = web_portal_submit(p["auth_url"], context.config.get("username",""),
                                    context.config.get("password",""), p["button_name"],
                                    user_selector=p["user_selector"], pass_selector=p["pass_selector"],
                                    success_keyword=p["success_keyword"], fail_keyword=p["fail_keyword"],
                                    timeout=min(step.timeout, context.remaining()),
                                    cancelled=context.cancelled)
        return StepResult.ok(msg) if ok else StepResult.fail(msg, code="portal_failed", retryable=...)
    # http：用 variant.server 覆盖 portal_ip/portal_port 后调用现有实现
    eff = {**context.config, **_server_to_ip_port(p["server"])}
    ok, msg = portal_login(eff, timeout=...)
    ...
```

`_portal_logout_action` 同理（web 方式打开注销网址并点击注销按钮；http 方式沿用现有 `portal_logout`）。重试语义、`_skip_if_ready`、进度发布保持不变。

### 5. 配置 schema 与默认值：`core/config.py`

- `DEFAULT_CONFIG` 新增 `"reconnect_workflow": "default_auth"`。
- **保留** `portal_ip` / `portal_port`（不再出现在设置页 UI），仅作 HTTP 方式的隐藏回退种子。
- `_builtin_workflows()` 的 Portal 节点 steps **保持空 params**（运行时回退 http + 全局服务器），内置默认行为不变。
- `WORKFLOW_CATALOG`（`core/auth_workflow.py`）为 `portal_login` / `portal_logout` 增加标记，供前端识别哪些节点有自定义配置：
  ```python
  'portal_login': {..., 'configurable': True, 'config_kind': 'portal'},
  'portal_logout': {..., 'configurable': True, 'config_kind': 'portal'},
  ```
  `workflow_catalog()` 已把 metadata 透出给前端。

### 6. 前端：工作流节点配置弹窗

**`WorkflowView.vue`**
- 底部悬浮操作区 `selCount === 1` 分支新增 `编辑` 按钮 → `openConfig(selIndices[0])`。
- 每个 `.wf-row` 增加 `@dblclick="onRowDblClick($event, row.index)"`：非交互元素（`!isInteractive(e.target)`）时选中该节点并 `openConfig(index)`，避免双击行内数字框误触发。
- 新增弹窗状态 `configIndex` / `configVisible`；弹窗用 naive-ui `n-modal`（`preset="card"`），**居中、无拖拽**（naive-ui 模态默认居中且不可拖动，不额外加任何 drag 行为即满足"不允许移动、默认居中"）。
- 弹窗内容：
  - **通用参数区**（所有节点）：启用、超时、重试、重试间隔、失败跳过——直接编辑 `workflow.steps[i]`，复用 `updateStep`/`pushUndo`/`markDirty`。
  - **Portal 专属区**（仅 `config_kind === 'portal'`）：渲染 `PortalNodeConfig.vue`。
- 编辑写入 `steps[i].params`，`markDirty()`，经现有「保存/另存为」持久化（params 随 steps 一并提交，后端 `validate` 不受影响）。

**新增 `frontend/src/components/PortalNodeConfig.vue`**（`v-model:params`）
- 「认证方式」下拉：网页点击 / 直连 HTTP（默认网页点击；空 params 显示为 HTTP 并预填全局 `portal_ip:portal_port`，做到所见即所得）。
- 「运行时连接」下拉：自动检测 / 强制有线 / 强制无线 ⇒ `link_mode`。
- 「有线 / 无线」分段切换：切换当前编辑的变体（`wired` / `wireless`），两套分别保存。
- 变体字段：网页方式显示 认证网址、按钮名称；HTTP 方式显示 服务器（ip:port）。「高级」折叠区显示 账号框选择器、密码框选择器、成功关键词、失败关键词。

**`defaultStep()`**：新增 portal 节点时 `params = { link_mode:'auto', wireless:{method:'web'}, wired:{method:'web'} }`（新节点默认网页方式，待用户填写）。

### 7. 前端：设置页改动

**`SettingsView.vue`**
- **移除**「校园网认证服务器」输入组（`portal_ip` / `portal_port`）及其在 watch/`initSettings` 中的相关项（`store.form` 仍保留这两个字段用于回退种子，不再渲染）。
- **新增**「自动重连执行的工作流」`n-select`：选项为全部工作流，**无空选项**（必选），绑定 `store.form.reconnect_workflow`。参照现有按钮绑定的即时保存 watch。

**`store.js`**
- `form` 新增 `reconnect_workflow: 'default_auth'`；保留 `portal_ip` / `portal_port`（仅不在设置页展示）。
- `collectFormConfig()` 增加 `reconnect_workflow`；`portal_ip`/`portal_port` 维持原样回传（值不变，等价于不动）。
- `initSettings`（在 SettingsView）读取 `config.reconnect_workflow`，若其指向的工作流已删除则回退 `default_auth`。

### 8. 自动重连：`core/reconnect_watchdog.py`

`_tick` 触发重连时改为运行绑定工作流，并开启操作纪元以便前端显示进度：

```python
wf_id = cfg.get('reconnect_workflow') or 'default_auth'
if not core.state._auth_lock.acquire(blocking=False):
    ...  # 忙碌则本轮跳过（保持现状）
try:
    app_state.start_operation('auth')          # 新纪元，前端进度归属本次重连
    success, msg = run_workflow_by_id(wf_id)   # 走标准工作流，_publish 推送进度
    ...
finally:
    core.state._auth_lock.release()
```

- 移除对 `connect_warp_result` 的直接调用（其能力已被 `connect_warp` 节点覆盖）。
- 失败后重新计时、`manual_disconnect_at` / `NO_RECONNECT_CODES` 等既有判定逻辑不变。

### 9. 后端 API：`tray_app.py`

- `auto_save_form` 的 `allowed` 集合新增 `'reconnect_workflow'`（`portal_ip`/`portal_port` 保留）。
- 其余工作流保存/校验 API 无需改动：params 随 steps 走，`_validate_workflow_steps` → `validate_auth_workflow` → `RUNNER.validate` 只校验 `id`/`enabled`，params 透明通过。

## 兼容性与迁移

- **零迁移**：内置工作流 Portal 节点空 params ⇒ HTTP + 全局服务器，行为与当前版本一致。
- `portal_ip` / `portal_port` 保留在配置与 `store.form`，仅从设置页 UI 移除；作为 HTTP 方式与弹窗预填的种子。
- 老版本保存的自定义工作流若无 params，同样按空 params 回退，不受影响。
- 新增配置键 `reconnect_workflow` 由 `_merge_defaults` 补默认值 `default_auth`。

## 测试策略

- **单元（Python）**：
  - `StepSpec.from_dict` 保留/清洗 params；params 经 save→load→validate 往返不丢。
  - `detect_link_type()`：mock `run_command` 覆盖 无线已连接 / 仅有线 / 都无 三种解析。
  - `_resolve_portal_params()`：link_mode=auto/wired/wireless、变体缺失回退、server 缺省回退全局。
  - `reconnect_watchdog._tick`：mock `run_workflow_by_id` 与 `app_state`，验证按 `reconnect_workflow` 运行且开启纪元；锁忙碌时跳过。
  - 现有 `tests/test_workflow.py`、`tests/test_workflow_tuning.py` 全绿。
- **网页自动化**：提供本地 HTML fixture（含账号框/密码框/登录按钮/成功页），手动验证填充、多策略按钮匹配、关键词判定、取消与超时都正确销毁窗口。JS 逻辑无法单测，记为手动验证项。
- **前端**：手动验证 编辑按钮、双击打开、弹窗居中不可移动、有线/无线分别保存、方式切换、设置页移除服务器项与新增重连绑定。

## 范围边界 / 非目标

- 本期只有 `portal_login` / `portal_logout` 拥有自定义业务参数；其他节点弹窗仅展示通用参数。
- **工作流其余节点（enable_ipv4 / disable_ipv4 / IPv6 / WARP）仍只面向 WLAN**；有线/无线切换**只影响 Portal 节点选用哪套配置**，不改造整条链路的网卡选择。真正端到端的"有线认证"不在本期范围。
- 不引入 Playwright/Selenium 等新依赖。
- 不处理认证页表单位于跨源 iframe 的情形。

## 风险

1. 隐藏 WebView2 执行 JS 的可靠性——早期用 fixture 验证，必要时退化为屏幕外/最小化窗口。
2. 认证页样式千差万别，启发式可能定位失败——用高级选择器覆盖 + 自定义关键词 + 下游连通性验证兜底。
3. 部分门户只有 JSONP 接口、无可点击页面——保留 HTTP 方式覆盖此情形。
4. 自动重连改为跑 `default_auth` 会触发完整 Portal+IPv6+WARP 流程并弹出进度，比原先的"仅重连 WARP"更重——这是用户明确选择（默认绑定 default_auth + 显示进度），可在设置页改绑更轻的工作流。
