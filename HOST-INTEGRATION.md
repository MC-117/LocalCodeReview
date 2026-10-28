# Codex integration status

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

## 2026-10-26 update (3.0.2)

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
