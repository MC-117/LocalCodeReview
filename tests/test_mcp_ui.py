import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from mcp_ui import UI_URI, ui_resource, ui_request
from server import App
from review_core import ReviewStore
from review_core import scope_identity


class McpUiTests(unittest.TestCase):
    def test_hidden_source_controls_do_not_change_scope_identity(self):
        self.assertEqual(scope_identity({'source':'session','session':'s','snapshot':''}),
                         scope_identity({'source':'session','session':'s','snapshot':'new-snapshot'}))
        self.assertEqual(scope_identity({'source':'git'}),
                         scope_identity({'source':'git','snapshot':'old','session':'old','start':2}))
        self.assertNotEqual(scope_identity({'source':'snapshot','snapshot':'a'}),
                            scope_identity({'source':'snapshot','snapshot':'b'}))

    def test_resource_is_self_contained_and_uses_host_bridge(self):
        value = ui_resource()['contents'][0]
        self.assertEqual(value['mimeType'], 'text/html;profile=mcp-app')
        self.assertEqual(value['_meta']['ui']['csp']['connectDomains'], [])
        text = value['text']
        self.assertIn('content="mcp"', text)
        self.assertNotIn('<script src=', text)
        self.assertNotIn('<link rel="stylesheet"', text)
        self.assertIn('ui/initialize', text)
        self.assertLess(text.index('<div id="toast"'), text.index('window.ReviewHost='))

    def test_protocol_resources_and_render_metadata_without_http(self):
        root = BASE / '.test-artifacts' / 'mcp-empty-project'
        root.mkdir(parents=True, exist_ok=True)
        requests = [
            ('initialize', {'protocolVersion': '2025-06-18'}),
            ('tools/list', {}), ('resources/list', {}),
            ('resources/read', {'uri': UI_URI}),
            ('resources/read', {'uri': 'file:///private'}),
            ('tools/call', {'name': 'review_open', 'arguments': {'projectRoot': str(root)}}),
            ('tools/call', {'name': 'review_ui_request', 'arguments': {'projectRoot': str(root), 'viewToken': 'wrong', 'route': 'state'}}),
        ]
        data = '\n'.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}) for i, (method, params) in enumerate(requests)) + '\n'
        process = subprocess.run([sys.executable, str(BASE/'server.py'), '--mcp'], input=data, encoding='utf-8', capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertIn('resources', replies[0]['result']['capabilities'])
        tools = {t['name']: t for t in replies[1]['result']['tools']}
        self.assertEqual(tools['review_open']['_meta']['ui']['resourceUri'], UI_URI)
        self.assertEqual(tools['review_ui_request']['_meta']['ui']['visibility'], ['app'])
        self.assertIn('error', replies[4])
        render = replies[5]['result']
        self.assertNotIn('viewToken', render['content'][0]['text'])
        self.assertNotIn('url', render['structuredContent'])
        self.assertTrue(render['_meta']['reviewContext']['viewToken'])
        self.assertIn('error', replies[6])
        self.assertNotIn('http://', process.stderr)

    def test_ui_cannot_confirm_plan_or_escape_project(self):
        scratch = BASE / '.test-artifacts' / 'mcp-ui-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            app = App(ReviewStore(root), None)
            self.assertEqual(ui_request(app, 'state')['annotations'], [])
            for route in ('confirm_plan', 'begin_changes', 'exec', 'https://example.com'):
                with self.assertRaises(ValueError):
                    ui_request(app, route, {'userConfirmation': '确认'})
            with self.assertRaises(ValueError):
                ui_request(app, 'file', {'path': '../outside.txt'})
            self.assertIsNone(app.http)


    def make_sessions(self, root, sessions):
        sessions.mkdir()
        for name, thread, timestamp in [('current','current-chat','2026-10-24'), ('recent','other-chat','2026-10-26')]:
            data={'type':'session_meta','payload':{'id':thread,'cwd':str(root),'timestamp':timestamp}}
            (sessions/(name+'.jsonl')).write_text(json.dumps(data)+'\n',encoding='utf-8')

    def test_exact_current_chat_even_without_code_and_newer_other_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sessions=root/'sessions';self.make_sessions(root,sessions)
            store=ReviewStore(root,sessions=sessions)
            current=store.review_scope('current-chat')
            match=store.session(current['query']['session'])
            self.assertEqual(match['records'],[])
            self.assertEqual(next(s['threadId'] for s in store.catalog() if s['id']==current['query']['session']),'current-chat')
            self.assertIsNone(store.review_scope('missing-chat')['query'])
            self.assertIsNone(store.review_scope()['query'])

    def test_assistant_scope_filters_and_snapshot_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sessions=root/'sessions';self.make_sessions(root,sessions)
            store=ReviewStore(root,sessions=sessions)
            scope=store.review_scope('current-chat',{'start':2,'end':4,'include':'src/*','exclude':'vendor/*'},'本次接口修改')
            self.assertEqual(scope['query']['start'],'2')
            self.assertEqual(scope['query']['include'],'src/*')
            self.assertEqual(scope['reason'],'本次接口修改')
            snap=store.capture('before')
            selected=store.review_scope('current-chat',{'source':'snapshot','snapshot':snap['id']})
            self.assertEqual(selected['query']['source'],'snapshot')
            for query in [{'session':'foreign'},{'start':4,'end':2},{'start':-1},{'defaults':'true'},{'source':'shell'},{'source':'snapshot','snapshot':'missing'}]:
                with self.assertRaises((ValueError, KeyError)):
                    store.review_scope('current-chat',query)

    def test_view_scopes_for_same_project_do_not_mutate_other_panel(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sessions=root/'sessions';self.make_sessions(root,sessions)
            app=App(ReviewStore(root,sessions=sessions),None)
            first=app.store.review_scope('current-chat',{'include':'src/*'})
            second=app.store.review_scope('other-chat',{'include':'tests/*'})
            one=ui_request(app,'meta',view=first)
            two=ui_request(app,'meta',view=second)
            self.assertNotEqual(one['defaultSession'],two['defaultSession'])
            self.assertEqual(ui_request(app,'meta',view=first)['reviewScope']['query']['include'],'src/*')


if __name__ == '__main__':
    unittest.main()
