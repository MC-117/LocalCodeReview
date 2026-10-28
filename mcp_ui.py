"""Offline MCP Apps resource and the narrowly scoped UI data interface."""
from pathlib import Path

BASE = Path(__file__).resolve().parent
UI_URI = 'ui://local-code-review/v3/review.html'
UI_MIME = 'text/html;profile=mcp-app'


def ui_resource():
    html = (BASE / 'web/index.html').read_text(encoding='utf-8')
    html = html.replace('<head>', '<head><meta name="review-transport" content="mcp">')
    css = (BASE / 'web/style.css').read_text(encoding='utf-8')
    html = html.replace('<link rel="stylesheet" href="/style.css">', '<style>' + css + '</style>')
    for name in ('bridge.js', 'viewer.js', 'app.js'):
        source = (BASE / 'web' / name).read_text(encoding='utf-8')
        # Inline only trusted local assets; source files can contain literal end tags.
        source = source.replace('</script', '<\\/script')
        html = html.replace(f'<script src="/{name}" defer></script>', '<script>' + source + '</script>')
    # Execute after the document exists, preserving the standalone defer order.
    scripts = []
    import re
    html = re.sub(r'<script>(.*?)</script>', lambda m: scripts.append(m.group(0)) or '', html, flags=re.S)
    html = html.replace('</body>', ''.join(scripts) + '</body>')
    return {'contents': [{'uri': UI_URI, 'mimeType': UI_MIME, 'text': html,
                           '_meta': {'ui': {'prefersBorder': False,
                                            'csp': {'connectDomains': [], 'resourceDomains': []}},
                                     'openai/widgetDescription': '本地代码审查、完整文件对比和编号意见。'}}]}


READ_ROUTES = ('meta', 'state', 'session', 'file', 'export', 'changes', 'compare')
WRITE_ROUTES = ('item', 'batch', 'guide', 'selection', 'snapshot')


def ui_request(app, route, body=None, view=None, standalone=False):
    """No code writes, workflow confirmations, filesystem paths or arbitrary URLs."""
    body = body or {}
    store = app.store
    with store.lock:
        if route == 'meta':
            selected = view if view is not None else store.review_scope(standalone=standalone)
            sessions = store.catalog(selected.get('threadId'))
            query = selected.get('query') or {}
            return {'root': str(store.root), 'data': str(store.data), 'token': app.token,
                    'sessions': [{k: v for k, v in s.items() if k != 'path'} for s in sessions],
                    'defaultSession': query.get('session'), 'reviewScope': selected, 'snapshots': store.snapshots(),
                    'rounds': store.rounds(selected.get('threadId'))}
        if route == 'state':
            return store.review_view()
        if route == 'session':
            value = store.session(body['id'], (view or {}).get('threadId'))
            return {'turns': value['turns'], 'warnings': value['warnings'], 'count': len(value['records'])}
        if route == 'file':
            return store.read_file(body['path'])
        if route == 'export':
            return {'text': store.export()}
        if route == 'changes':
            return store.changes(body)
        if route == 'compare':
            return store.compare_file(body['query'], body['path'], body.get('recordIds'))
        if route == 'item':
            return store.save_item(body)
        if route == 'batch':
            return store.save_batch(body)
        if route == 'guide':
            return store.save_guide(body['revision'], body['query'], body['groups'])
        if route == 'selection':
            return store.select(body)
        if route == 'snapshot':
            return store.capture(body.get('name', ''))
        raise ValueError('不支持的审查界面操作')
