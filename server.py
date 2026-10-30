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


def _page_args(value, default=20, maximum=50):
    try:
        offset = int(value.get('offset', 0))
        limit = int(value.get('limit', default))
    except (TypeError, ValueError):
        raise ValueError('分页参数必须是整数')
    if offset < 0 or limit < 1 or limit > maximum:
        raise ValueError(f'offset 必须大于等于 0，limit 必须在 1 到 {maximum} 之间')
    return offset, limit


def _require_precise_changes(query):
    """Full change payloads require an index-selected, immutable target."""
    source = query.get('source')
    precise = ((source == 'round' and query.get('round')) or
               (source == 'session' and query.get('session') and query.get('turnId')) or
               (source == 'snapshot' and query.get('snapshot')))
    if not precise:
        raise ValueError('请先调用 review_sources 的索引，再指定一个 round ID，或 session ID + turn ID；不能读取宽泛的全部修改')


def _compact_review(value, args):
    """Page review state returned to the assistant, while UI routes remain complete."""
    offset, limit = _page_args(args, default=20, maximum=50)
    result = dict(value)
    for kind in ('items', 'annotations', 'guides'):
        rows = value.get(kind, [])
        result[kind] = rows[offset:offset + limit]
        result[kind + 'Count'] = len(rows)
    result['offset'] = offset
    result['limit'] = limit
    result['nextOffset'] = offset + limit if any(offset + limit < len(value.get(kind, [])) for kind in ('items', 'annotations', 'guides')) else None
    result['truncated'] = result['nextOffset'] is not None
    return result


def _review_ref(store, entry, kind):
    scope = entry.get('scope') or {}
    if scope.get('source') == 'round' and scope.get('round'):
        target = 'round:' + scope['round']
    else:
        target = 'scope:' + store._scope_key(scope)
    return f'{target}:{kind}:{entry["id"]}'


def _add_review_refs(store, value):
    result = dict(value)
    result['items'] = [{**entry, 'reviewRef': _review_ref(store, entry, 'item')}
                       for entry in value.get('items', [])]
    result['annotations'] = [{**entry, 'reviewRef': _review_ref(store, entry, 'annotation')}
                             for entry in value.get('annotations', [])]
    return result


def _compact_snapshot(manifest, include_files=False):
    if include_files:
        return manifest
    return {'id': manifest['id'], 'name': manifest['name'], 'time': manifest['time'],
            'count': manifest['count'], 'skippedCount': len(manifest.get('skipped', [])),
            'unavailableCount': len(manifest.get('unavailable', {})),
            'filesIncluded': False}


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
                        elif self.path == '/api/state':
                            result = ui_request(app, 'state', body, standalone=True)
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
    def page(items, args):
        offset = int(args.get('offset', 0))
        limit = min(50, int(args.get('limit', 20)))
        if offset < 0 or limit < 1:
            raise ValueError('offset 必须大于等于 0，limit 必须在 1 到 50 之间')
        end = offset + limit
        return {'items': items[offset:end], 'offset': offset, 'limit': limit,
                'total': len(items), 'nextOffset': end if end < len(items) else None}
    def round_index(value):
        return {key: value[key] for key in ('id', 'turnId', 'turnNumber', 'label',
                                             'turnTime', 'status', 'changedFiles') if key in value}
    catalog = [
        entry('review_open', '在对话中显示本地代码审查 MCP Apps 界面。不要再打开浏览器标签。仅当宿主不支持 UI 且用户需要网页时传 browserFallback=true。', {'browserFallback': {'type': 'boolean', 'default': False}, 'threadId': {'type':'string','description':'当前对话编号，从可信上下文或 CODEX_THREAD_ID 读取，不能用项目最近会话代替。'}, 'query': {'type':'object','description':'助手根据上下文选定的 source/round/session/turnId/snapshot/start/end/include/exclude/defaults/attempts。source=round 使用固定两端；round=latest 在精确 threadId 内解析并固定最近修改轮。'}, 'reason': {'type':'string','description':'向用户说明选择此比较范围的依据。'}}),
        entry('review_context', '读取索引选定的完整意见和原生文件定位。必须提供 itemId，并同时提供 roundId 或 scopeKey，不能只凭裸编号定位。查看意见后先提出方案，等用户确认再改代码。', {'itemId': {'type': 'integer', 'minimum': 1}, 'roundId': {'type': 'string'}, 'scopeKey': {'type': 'string'}, 'historyLimit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 500}}, ['itemId']),
        entry('review_sources', '分层浏览审查来源。默认只返回短索引；用 level=sessions/snapshots/rounds 分页列目录，用 level=turns 和 sessionId 分页列某日志段的轮次，用 level=turn 或 level=round 加精确 ID 读取一个目标。threadId 精确匹配当前对话。', {
            'threadId': {'type':'string'},
            'level': {'type':'string', 'enum':['index','sessions','snapshots','rounds','reviews','turns','turn','round'], 'default':'index'},
            'sessionId': {'type':'string'}, 'turnId': {'type':'string'}, 'roundId': {'type':'string'},
            'offset': {'type':'integer', 'minimum':0, 'default':0},
            'limit': {'type':'integer', 'minimum':1, 'maximum':50, 'default':20}
        }),
        entry('review_changes', '读取完整的会话补丁或固定快照差异。必须先从 review_sources 索引选择精确目标：source=round 时传 round ID，source=session 时传 session + turnId，source=snapshot 时传 snapshot ID；宽泛查询会被拒绝。可再用 include 缩小到某文件。历史会话操作不等于当前净差异。', {'query': {'type': 'object', 'description': '必须包含精确目标；不分页、不截断补丁。'}}, ['query']),
        entry('review_results', '只读取指定已完成审查轮的完整用户意见、稳定编号、过期标记、功能导览和该轮关联方案。必须先从 review_sources 索引取得 roundId；不会返回其它轮次或对话的内容。', {'roundId': {'type': 'string', 'description': '目标已完成审查轮 ID；先从 review_sources 获取。'}}, ['roundId']),
        entry('review_save_item', '保存审查项，需最新 revision；不修改代码，不将助手自检冒充用户意见。', {'item': {'type': 'object'}}, ['item']),
        entry('review_set_guide', '根据本轮修改和对话上下文整理功能导览，关联跨目录文件并提供目的和真实验证结果。不是用户批注，不自动通过审查。相同范围按组 key 更新，意见编号保持独立。', {'revision': {'type': 'integer'}, 'query': {'type': 'object'}, 'groups': {'type': 'array', 'items': {'type': 'object'}}}, ['revision', 'query', 'groups']),
        entry('review_snapshot', '修改前记录文本快照，不依赖 Git 提交。默认只返回快照索引；需要文件哈希表时显式传 includeFiles=true。', {'name': {'type': 'string'}, 'includeFiles': {'type': 'boolean', 'default': False}}, ['name']),
        entry('review_round', '按单轮记录固定前后快照。修改前 begin，完成并验证后 finish；重复调用不覆盖端点。begin 必须传可信当前 threadId，turnId 可从日志取得，省略时精确查找该对话最新执行轮。不是代码修改授权。', {'action': {'type':'string','enum':['begin','finish']}, 'threadId': {'type':'string'}, 'turnId': {'type':'string'}, 'roundId': {'type':'string'}, 'label': {'type':'string'}}, ['action']),
        entry('review_export', '将编号意见保存到项目 .code-review/review-export.md。默认返回文件索引和预览；需要完整 Markdown 时显式传 includeText=true。', {'includeText': {'type': 'boolean', 'default': False}}),
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
                        value = {'projectRoot': str(store.root), 'presentation': 'mcp-app', 'reviewScope': view}
                        if args.get('browserFallback'):
                            value.update({k: v + ('&' if '?' in v else '?') + 'view=' + view_token for k, v in app.browser_fallback().items()})
                    elif name == 'review_ui_request':
                        view = app.views.get(str(args.get('viewToken', '')))
                        if view is None:
                            raise ValueError('界面会话无效，请重新打开审查')
                        value = ui_request(app, args['route'], args.get('payload'), view=view)
                    elif name == 'review_context':
                        round_id, scope_key = args.get('roundId'), args.get('scopeKey')
                        if not args.get('itemId') or bool(round_id) == bool(scope_key):
                            raise ValueError('review_context 必须同时提供 itemId 和且仅一个 roundId 或 scopeKey')
                        scope = store.review_scope_for_key(scope_key) if scope_key else None
                        value = store.context(args['itemId'], args.get('historyLimit', 500), scope=scope, round_id=round_id)
                        if value.get('item'):
                            value['item'] = {**value['item'], 'reviewRef': _review_ref(store, value['item'], 'item')}
                        if value.get('annotation'):
                            value['annotation'] = {**value['annotation'], 'reviewRef': _review_ref(store, value['annotation'], 'annotation')}
                    elif name == 'review_sources':
                        level, thread_id = args.get('level', 'index'), args.get('threadId')
                        if level == 'index':
                            sessions, snapshots = store.catalog(thread_id), store.snapshots()
                            rounds = store.rounds(thread_id)
                            review_scopes = store._review_index()['scopes']
                            value = {'level': 'index', 'sections': [
                                {'name': 'sessions', 'count': len(sessions)},
                                {'name': 'snapshots', 'count': len(snapshots)},
                                {'name': 'rounds', 'count': len(rounds)},
                                {'name': 'reviews', 'count': len(review_scopes)}],
                                'recentRounds': [round_index(r) for r in rounds[:5]],
                                'recentReviews': [{'key': s['key'], 'source': s.get('scope', {}).get('source'),
                                                  'round': s.get('scope', {}).get('round'),
                                                  'itemCount': s.get('itemCount', 0),
                                                  'annotationCount': s.get('annotationCount', 0)}
                                                 for s in review_scopes[:5]]}
                        elif level == 'sessions':
                            rows = [{k: s[k] for k in ('id', 'label', 'time', 'threadId', 'cwd', 'bytes') if k in s}
                                    for s in store.catalog(thread_id)]
                            value = {'level': level, **page(rows, args)}
                        elif level == 'snapshots':
                            rows = [{k: s[k] for k in ('id', 'name', 'time', 'count') if k in s}
                                    for s in store.snapshots()]
                            value = {'level': level, **page(rows, args)}
                        elif level == 'reviews':
                            rows = []
                            for entry in store._review_index()['scopes']:
                                scope = entry.get('scope', {})
                                rows.append({'key': entry['key'], 'source': scope.get('source'),
                                             'round': scope.get('round'), 'snapshot': scope.get('snapshot'),
                                             'session': scope.get('session'),
                                             'itemCount': entry.get('itemCount', 0),
                                             'annotationCount': entry.get('annotationCount', 0),
                                             'guideCount': entry.get('guideCount', 0)})
                            value = {'level': level, **page(rows, args)}
                        elif level == 'rounds':
                            value = {'level': level, **page([round_index(r) for r in store.rounds(thread_id)], args)}
                        elif level == 'round':
                            round_id = args.get('roundId')
                            if not round_id:
                                raise ValueError('读取单轮审查必须提供 roundId')
                            target = store.round(round_id)
                            if thread_id and target.get('threadId') != thread_id:
                                raise ValueError('单轮审查不属于当前对话')
                            value = {'level': level, 'round': target}
                        elif level in ('turns', 'turn'):
                            session_id = args.get('sessionId')
                            if not session_id:
                                raise ValueError('读取会话轮次必须提供 sessionId')
                            turns = store.session(session_id, thread_id)['turns']
                            if level == 'turns':
                                value = {'level': level, 'sessionId': session_id, **page(turns, args)}
                            else:
                                turn_id = args.get('turnId')
                                if not turn_id:
                                    raise ValueError('读取单轮摘要必须提供 turnId')
                                turn = next((t for t in turns if t.get('id') == turn_id), None)
                                if turn is None:
                                    raise ValueError('所选日志段中没有该轮次')
                                value = {'level': level, 'sessionId': session_id, 'turn': turn}
                        else:
                            raise ValueError('review_sources level 无效')
                    elif name == 'review_changes':
                        _require_precise_changes(args['query'])
                        value = store.changes(args['query'])
                    elif name == 'review_results':
                        value = _add_review_refs(store, store.review_view(round_id=args['roundId']))
                    elif name == 'review_save_item':
                        value = _compact_review(store.save_item(args['item']), args)
                    elif name == 'review_set_guide':
                        value = _compact_review(store.save_guide(args['revision'], args['query'], args['groups']), args)
                    elif name == 'review_snapshot':
                        value = _compact_snapshot(store.capture(args['name']), args.get('includeFiles', False))
                    elif name == 'review_round':
                        if args['action'] == 'begin':
                            value = store.begin_round(args.get('threadId'), args.get('turnId'), args.get('label'))
                        elif args['action'] == 'finish':
                            value = store.finish_round(args.get('roundId'))
                        else:
                            raise ValueError('无效的单轮记录操作')
                    elif name == 'review_export':
                        content = store.export()
                        value = content if args.get('includeText') else {
                            'path': str(store.data / 'review-export.md'), 'bytes': len(content.encode('utf-8')),
                            'preview': content[:2000], 'textIncluded': False}
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
