# Codex integration status

[English](#english) | [简体中文](#简体中文)

## English

The entries below are historical validation records. Versions and verification status refer to the stated dates, not the current installation.

2026-10-25: Local Code Review 3.0.1 is installed and enabled as
`local-code-review@local-review-tools`. The marketplace is local; the plugin
configuration points at this tool's Python server and does not bind a project.

Validated:

- 33 Python unit tests, including resource discovery, private UI context, token
  rejection, project isolation and the plan confirmation boundary.
- Standalone browser regression suite.
- Real stdio MCP server connected to a sandboxed opaque-origin iframe through a
  protocol test host: full single/split file views, syntax highlighting, durable
  R feedback, expiry after code edits, export, snapshot capture, fullscreen
  capability detection, and follow-up messages that do not authorize changes.
- The component makes no network requests. MCP mode starts no HTTP listener.
- The feedback skill passes its validator.

Actual Codex UI rendering is still unverified. Installation succeeded, but the
active tool inventory in the installing turn did not include `review_open` or
the other new plugin tools. A simulated protocol host is not proof of actual
Codex rendering. Do not report native fullscreen, modal, or panel availability
until invoking the plugin through the desktop host and observing the result.

Next: load the installed plugin in Codex, then call `review_open` with the
trusted current project's absolute `projectRoot`. No browser fallback is needed
for the primary UI. A separate browser tab should only be used if the user
wants the explicit fallback after learning that this host cannot render it.

The review component uses host theme variables and custom HTML/CSS. It does not
inject controls into the built-in review editor and does not bundle the optional
OpenAI React UI library. Host fullscreen is requested only when advertised;
`requestModal` and sidebar extension integration are not implemented or claimed.

Cached feedback is in each project's `.code-review/`. After receiving feedback,
the assistant must propose a concrete plan, wait for the user's explicit chat
confirmation, and only then apply the confirmed changes.

The source is intended to be cloned into any local directory. The marketplace and MCP paths are generated per user by `configure_plugin.py`; the sandboxed MCP UI browser test passes from a clean clone. Actual Codex panel rendering must still be verified after each host/plugin reload.

### 2026-10-26 update (3.0.2)

- Primary action is now “提交意见给助手”. Opinions and checkbox edits save after
  a 700 ms pause. Export moved into the overflow menu. Opening the app alone
  does not persist default approvals.
- In-flight edits, reversions and checkbox changes are preserved. Failed saves
  retain drafts, block feedback submission and expose a retry action.
- 33 backend tests and both standalone/MCP iframe browser suites passed. The
  latter uses a protocol test host, not Codex itself.
- In the active Codex conversation, review_open is now available and its call
  succeeds. The MCP Apps inventory reported the actual review panel. Complete
  visual inspection inside the actual Codex host is still not established.
- Feedback submission requests a proposal; explicit human chat confirmation is
  still required before applying project code changes.

---

## 简体中文

Codex 集成状态

以下是各日期的历史验证记录；其中的版本号和验证状态以当时记录为准，不表示当前安装状态。

### 2026-10-25（3.0.1）

Local Code Review 3.0.1 已安装并启用为 `local-code-review@local-review-tools`。marketplace 为本地目录；插件配置指向工具的 Python 服务，不绑定特定项目。

已验证：

- 33 项 Python 单元测试，包括资源发现、私有 UI 上下文、令牌拒绝、项目隔离及方案确认边界。
- 独立网页的浏览器回归测试。
- 真实 stdio MCP 服务通过协议测试宿主连接到带 opaque origin 的沙箱 iframe：完整单页／并排代码、语法高亮、持久化 R 批注、代码变化后失效、导出、快照、全屏能力检测，以及不会授权修改的后续消息。
- 组件不发起网络请求，MCP 模式不启动 HTTP 监听器。
- feedback skill 通过校验。

当时尚未验证真实 Codex UI 渲染。安装成功，但安装轮次的可用工具列表未包含 `review_open` 等新插件工具。模拟协议宿主不能证明真实 Codex 渲染。只有通过桌面宿主调用并观察结果后，才能报告原生全屏、弹窗或面板能力可用。

后续步骤是加载已安装插件，再以可信当前项目的绝对 `projectRoot` 调用 `review_open`。主 UI 不需要浏览器回退。只有在宿主不能渲染且用户明确需要回退时，才另开浏览器。

审查组件使用宿主主题变量和自定义 HTML/CSS，不向内置审查编辑器注入控件，也未捆绑可选的 OpenAI React UI 库。仅在宿主声明支持时请求全屏；未实现或宣称支持 `requestModal` 和侧边栏扩展集成。

意见缓存在各项目的 `.code-review/` 中。收到意见后，助手必须先提出具体方案，等待用户在聊天中明确确认，然后才能实施。

源码可以克隆到任意本地目录。`configure_plugin.py` 为每位用户生成 marketplace 使用的 MCP 路径。干净克隆中的沙箱 MCP UI 浏览器测试通过；每次宿主／插件重载后的真实 Codex 面板仍应单独验证。

### 2026-10-26 更新（3.0.2）

- 主操作改为“提交意见给助手”。意见与勾选修改在停止输入 700 毫秒后保存；导出移入更多菜单。仅打开页面不会持久化默认通过结论。
- 保存期间的编辑、撤销和勾选修改均保留。保存失败时保留草稿、阻止提交旧意见，并显示重试入口。
- 33 项后端测试、独立网页测试及 MCP iframe 浏览器测试通过；后者使用协议测试宿主，不是真实 Codex。
- 当前 Codex 对话已能调用 `review_open` 并成功返回，MCP Apps 列表也出现真实审查面板；但尚未建立对真实宿主的完整视觉核实。
- 提交意见请求助手提出方案；实施代码修改前仍须用户在聊天中明确确认。
