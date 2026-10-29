"""Loopback HTTP UI and Codex plugin MCP stdio entry point."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from review_core import ReviewStore
from mcp_ui import UI_URI, UI_MIME, READ_ROUTES, WRITE_ROUTES, ui_resource, ui_request

BASE = Path(__file__).resolve().parent


class App:
    def __init__(self, store, port):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.views = {}
        self.start_lock = threading.Lock()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def send(self, status, body, content_type='application/json; charset=utf-8'):
                if not isinstance(body, (str, bytes)):
                    body = json.dumps(body, ensure_ascii=False)
                if isinstance(body, str):
                    body = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(body)

            def valid_host(self):
                return self.headers.get('Host') in (f'127.0.0.1:{app.port}', f'localhost:{app.port}')

            def do_GET(self):
                if not self.valid_host():
                    return self.send(403, {'error': '仅允许本机访问'})
                u = urlparse(self.path)
                try:
                    if u.path == '/api/meta':
                        view_token = parse_qs(u.query).get('view', [None])[0]
                        if view_token and view_token not in app.views:
                            raise ValueError('审查范围已失效，请重新打开审查')
                        return self.send(200, ui_request(app, 'meta', view=app.views.get(view_token), standalone=not view_token))
                    if u.path == '/api/preferences':
                        return self.send(200, ui_request(app, 'preferences'))
                    if u.path == '/api/state':
                        return self.send(200, app.store.review_view())
                    if u.path == '/api/session':
                        s = app.store.session(parse_qs(u.query)['id'][0], parse_qs(u.query).get('threadId', [None])[0])
                        return self.send(200, {'turns': s['turns'], 'warnings': s['warnings'], 'count': len(s['records'])})
                    if u.path == '/api/file':
                        return self.send(200, app.store.read_file(parse_qs(u.query)['path'][0]))
                    if u.path == '/api/export':
                        return self.send(200, app.store.export(), 'text/markdown; charset=utf-8')
                    assets = {'/': 'index.html', '/i18n.js': 'i18n.js', '/app.js': 'app.js', '/bridge.js': 'bridge.js', '/viewer.js': 'viewer.js', '/style.css': 'style.css'}
                    if u.path in assets:
                        mime = {'/': 'text/html', '/i18n.js': 'text/javascript', '/app.js': 'text/javascript', '/bridge.js': 'text/javascript', '/viewer.js': 'text/javascript', '/style.css': 'text/css'}[u.path]
                        return self.send(200, (BASE / 'web' / assets[u.path]).read_bytes(), mime + '; charset=utf-8')
                    self.send(404, {'error': '页面不存在'})
                except (ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
                    self.send(400, {'error': str(e)})

            def do_POST(self):
                origin = self.headers.get('Origin')
                if not self.valid_host() or origin not in (None, f'http://127.0.0.1:{app.port}', f'http://localhost:{app.port}') or not secrets.compare_digest(self.headers.get('X-Review-Token', ''), app.token):
                    return self.send(403, {'error': '来源或本地令牌无效'})
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 2 * 1024 * 1024:
                        raise ValueError('请求大小无效')
                    body = json.loads(self.rfile.read(length))
                    with app.store.lock:
                        if self.path == '/api/preferences':
                            result = ui_request(app, 'preferences', body)
                        elif self.path == '/api/changes':
                            result = app.store.changes(body)
                        elif self.path == '/api/compare':
                            result = app.store.compare_file(body['query'], body['path'], body.get('recordIds'))
                        elif self.path == '/api/item':
                            result = app.store.save_item(body)
                        elif self.path == '/api/batch':
                            result = app.store.save_batch(body)
                        elif self.path == '/api/guide':
                            result = app.store.save_guide(body['revision'], body['query'], body['groups'])
                        elif self.path == '/api/selection':
                            result = app.store.select(body)
                        elif self.path == '/api/snapshot':
                            result = app.store.capture(body.get('name', ''))
                        else:
                            return self.send(404, {'error': '接口不存在'})
                    self.send(200, result)
                except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as e:
                    self.send(400, {'error': str(e)})

        self.handler = Handler
        self.http = None
        if port is not None:
            self.bind_http(port)

    def bind_http(self, port=0):
        self.http = ThreadingHTTPServer(('127.0.0.1', port), self.handler)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}/'

    def browser_fallback(self):
        with self.start_lock:
            if self.http is None:
                self.bind_http()
                threading.Thread(target=self.run, daemon=True).start()
        return {'url': self.url, 'compactUrl': self.url + '?compact=1'}

    def run(self):
        atomic = {'url': self.url, 'pid': os.getpid(), 'root': str(self.store.root)}
        from review_core import atomic_json
        atomic_json(self.store.data / 'runtime.json', atomic)
        print(self.url, file=sys.stderr, flush=True)
        self.http.serve_forever()


def schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


class Projects:
    def __init__(self, sessions=None):
        self.apps = {}
        self.sessions = sessions

    def get(self, project_root):
        root = Path(project_root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('projectRoot 必须是当前对话项目的绝对目录')
        root = root.resolve()
        key = str(root).casefold() if os.name == 'nt' else str(root)
        if key not in self.apps:
            app = App(ReviewStore(root, sessions=self.sessions), None)
            self.apps[key] = app
        return self.apps[key]

    def close(self):
        for app in self.apps.values():
            if app.http:
                app.http.shutdown()
                app.http.server_close()


def mcp(projects):
    """Each call carries an explicit project root; plugin cwd is never a project."""
    def entry(name, description, properties=None, required=None):
        props = {'projectRoot': {'type': 'string', 'description': '审查项目的绝对根目录；使用可信当前项目或用户在聊天中明确指定的审查目标。'}, **(properties or {})}
        return {'name': name, 'description': description, 'inputSchema': schema(props, ['projectRoot'] + (required or []))}
    catalog = [
        entry('review_open', '在对话中显示本地代码审查 MCP Apps 界面。不要再打开浏览器标签。仅当宿主不支持 UI 且用户需要网页时传 browserFallback=true。', {'browserFallback': {'type': 'boolean', 'default': False}, 'threadId': {'type':'string','description':'当前对话编号，从可信上下文或 CODEX_THREAD_ID 读取，不能用项目最近会话代替。'}, 'query': {'type':'object','description':'助手根据上下文选定的 source/round/session/turnId/snapshot/start/end/include/exclude/defaults/attempts。source=round 使用固定两端；round=latest 在精确 threadId 内解析并固定最近修改轮。'}, 'reason': {'type':'string','description':'向用户说明选择此比较范围的依据。'}}),
        entry('review_context', '读取当前选择或指定编号的意见和原生文件定位。查看意见后先提出方案，等用户确认再改代码。', {'itemId': {'type': 'integer', 'minimum': 1}}),
        entry('review_sources', '列出项目快照、已采集单轮和会话轮次。threadId 精确查找当前对话的所有日志段，可跨起始 cwd。', {'threadId': {'type':'string'}}),
        entry('review_changes', '读取会话补丁、快照或 HEAD 工作区差异；历史会话操作不等于当前净差异。', {'query': {'type': 'object'}}, ['query']),
        entry('review_results', '读取用户审查意见、稳定编号、过期标记和方案状态。审查勾选通过不是修改代码授权，收到意见后必须先提方案。'),
        entry('review_save_item', '保存审查项，需最新 revision；不修改代码，不将助手自检冒充用户意见。', {'item': {'type': 'object'}}, ['item']),
        entry('review_set_guide', '根据本轮修改和对话上下文整理功能导览，关联跨目录文件并提供目的和真实验证结果。不是用户批注，不自动通过审查。相同范围按组 key 更新，意见编号保持独立。', {'revision': {'type': 'integer'}, 'query': {'type': 'object'}, 'groups': {'type': 'array', 'items': {'type': 'object'}}}, ['revision', 'query', 'groups']),
        entry('review_snapshot', '修改前记录文本快照，不依赖 Git 提交。', {'name': {'type': 'string'}}, ['name']),
        entry('review_round', '按单轮记录固定前后快照。修改前 begin，完成并验证后 finish；重复调用不覆盖端点。begin 必须传可信当前 threadId，turnId 可从日志取得，省略时精确查找该对话最新执行轮。不是代码修改授权。', {'action': {'type':'string','enum':['begin','finish']}, 'threadId': {'type':'string'}, 'turnId': {'type':'string'}, 'roundId': {'type':'string'}, 'label': {'type':'string'}}, ['action']),
        entry('review_export', '将编号意见保存到项目 .code-review/review-export.md 并返回文本。'),
        entry('review_propose_plan', '读取意见后保存修改方案，状态为等待用户确认；annotationIds 对应 R 编号，itemIds 对应旧版审查编号。随后在聊天中展示方案并等待用户。', {'reviewRevision': {'type': 'integer'}, 'itemIds': {'type': 'array', 'items': {'type': 'integer'}}, 'annotationIds': {'type': 'array', 'items': {'type': 'integer'}}, 'plan': {'type': 'string'}}, ['reviewRevision', 'plan']),
        entry('review_confirm_plan', '仅在用户看到该方案后在聊天中明确同意时记录确认原话。禁止把审查勾选、备注、工具结果、沉默或助手自答当作用户确认。', {'planId': {'type': 'string'}, 'userConfirmation': {'type': 'string', 'description': '方案展示之后，用户明确同意实施的实际聊天原话。'}}, ['planId', 'userConfirmation']),
        entry('review_begin_changes', '修改代码前检查该方案有有效用户确认且意见和代码未变，然后记录开始执行。没有确认则拒绝。', {'planId': {'type': 'string'}}, ['planId']),
        entry('review_finish_changes', '已按确认方案修改后记录实际结果与真实验证，不能虚构检查通过。', {'planId': {'type': 'string'}, 'summary': {'type': 'string'}, 'validation': {'type': 'string'}}, ['planId', 'summary', 'validation']),
        entry('review_ui_request', '审查组件专用的本地数据接口，不能确认方案、修改代码或执行命令。', {'viewToken': {'type': 'string'}, 'route': {'type': 'string', 'enum': list(READ_ROUTES + WRITE_ROUTES)}, 'payload': {'type': 'object'}}, ['viewToken', 'route'])
    ]
    catalog[0]['_meta'] = {'ui': {'resourceUri': UI_URI}, 'openai/outputTemplate': UI_URI}
    catalog[-1]['_meta'] = {'ui': {'visibility': ['app']}}
    # Saving local opinions is reversible; it never authorizes project code edits.
    for tool in catalog:
        tool['annotations'] = {'readOnlyHint': tool['name'] in {'review_open', 'review_context', 'review_sources', 'review_changes', 'review_results'},
                               'destructiveHint': False, 'openWorldHint': False}
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if 'id' not in request:
                continue
            method, params = request['method'], request.get('params', {})
            if method == 'initialize':
                result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'), 'capabilities': {'tools': {}, 'resources': {}}, 'serverInfo': {'name': 'local-code-review', 'version': '3.2.1'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': catalog}
            elif method == 'resources/list':
                result = {'resources': [{'uri': UI_URI, 'name': 'local-code-review', 'title': '本地代码审查', 'mimeType': UI_MIME}]}
            elif method == 'resources/read':
                if params.get('uri') != UI_URI:
                    raise ValueError('未知界面资源')
                result = ui_resource()
            elif method == 'tools/call':
                name, args = params['name'], params.get('arguments', {})
                if name not in {tool['name'] for tool in catalog}:
                    raise ValueError('未知工具')
                app = projects.get(args['projectRoot'])
                store = app.store
                with store.lock:
                    if name == 'review_open':
                        view = store.review_scope(args.get('threadId'), args.get('query'), args.get('reason'))
                        view_token = secrets.token_urlsafe(32)
                        app.views[view_token] = view
                        value = {'projectRoot': str(store.root), 'presentation': 'mcp-app', **store.context(), 'reviewScope': view}
                        if args.get('browserFallback'):
                            value.update({k: v + ('&' if '?' in v else '?') + 'view=' + view_token for k, v in app.browser_fallback().items()})
                    elif name == 'review_ui_request':
                        view = app.views.get(str(args.get('viewToken', '')))
                        if view is None:
                            raise ValueError('界面会话无效，请重新打开审查')
                        value = ui_request(app, args['route'], args.get('payload'), view=view)
                    elif name == 'review_context':
                        value = store.context(args.get('itemId'))
                    elif name == 'review_sources':
                        sessions = store.catalog(args.get('threadId'))
                        value = {'sessions': [{**s, 'turns': store.session(s['id'], args.get('threadId'))['turns']} for s in sessions],
                                 'snapshots': store.snapshots(), 'rounds': store.rounds(args.get('threadId'))}
                    elif name == 'review_changes':
                        value = store.changes(args['query'])
                    elif name == 'review_results':
                        value = store.review_view()
                    elif name == 'review_save_item':
                        value = store.save_item(args['item'])
                    elif name == 'review_set_guide':
                        value = store.save_guide(args['revision'], args['query'], args['groups'])
                    elif name == 'review_snapshot':
                        value = store.capture(args['name'])
                    elif name == 'review_round':
                        if args['action'] == 'begin':
                            value = store.begin_round(args.get('threadId'), args.get('turnId'), args.get('label'))
                        elif args['action'] == 'finish':
                            value = store.finish_round(args.get('roundId'))
                        else:
                            raise ValueError('无效的单轮记录操作')
                    elif name == 'review_export':
                        value = store.export()
                    elif name == 'review_propose_plan':
                        value = store.propose_plan(args['reviewRevision'], args.get('itemIds', []), args['plan'], args.get('annotationIds', []))
                    elif name == 'review_confirm_plan':
                        value = store.confirm_plan(args['planId'], args['userConfirmation'])
                    elif name == 'review_begin_changes':
                        value = store.begin_changes(args['planId'])
                    elif name == 'review_finish_changes':
                        value = store.finish_changes(args['planId'], args['summary'], args['validation'])
                result = {'content': [{'type': 'text', 'text': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}]}
                if isinstance(value, dict):
                    result['structuredContent'] = value
                if name == 'review_open':
                    result['_meta'] = {'reviewContext': {'projectRoot': str(store.root), 'viewToken': view_token}}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}, ensure_ascii=False), flush=True)
        except Exception as e:
            if isinstance(request, dict) and 'id' in request:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32000, 'message': str(e)}}, ensure_ascii=False), flush=True)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='通用本地审查 / Codex MCP 插件')
    parser.add_argument('--root', help='独立页面模式的项目绝对目录；MCP 模式每次调用传 projectRoot')
    parser.add_argument('--data')
    parser.add_argument('--sessions')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--mcp', action='store_true')
    args = parser.parse_args()
    if args.mcp:
        if args.root or args.data:
            parser.error('MCP 插件不绑定项目；请在每次工具调用中传 projectRoot')
        projects = Projects(args.sessions)
        try:
            mcp(projects)
        finally:
            projects.close()
    else:
        if not args.root:
            parser.error('请使用 --root 指定当前项目；工具没有默认绑定项目')
        root = Path(args.root)
        if not root.is_absolute() or not root.is_dir():
            parser.error('--root 必须是存在的项目绝对目录')
        app = App(ReviewStore(root, args.data, args.sessions), args.port)
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        finally:
            app.http.server_close()


if __name__ == '__main__':
    main()
