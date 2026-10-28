import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from review_core import ReviewStore, scope_identity
from server import App
from mcp_ui import ui_request


class RoundTests(unittest.TestCase):
    def setUp(self):
        scratch = BASE / '.test-artifacts' / 'round-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.base = Path(self.tmp.name)
        self.root = self.base / '中文 工具'; self.root.mkdir()
        self.other = self.base / '原聊天目录'; self.other.mkdir()
        self.logs = self.base / 'sessions'; self.logs.mkdir()
        self.store = ReviewStore(self.root, sessions=self.logs)
        self.write('src/example.py', '# unchanged\nvalue = 1\n# tail\n')
        self.log('first', 'chat', 'turn-one', '2026-10-26T10:00:00Z', '第一轮：脚本修改')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        p = self.root / path; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')

    def log(self, name, thread, turn, time, label, patch=None, cwd=None):
        data = [
            {'type':'session_meta','payload':{'id':thread,'cwd':str(cwd or self.other),'timestamp':time}},
            {'type':'event_msg','timestamp':time,'payload':{'type':'task_started','turn_id':turn}},
            {'type':'turn_context','timestamp':time,'payload':{'turn_id':turn}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':label}]}}
        ]
        if patch:
            data += [{'type':'response_item','payload':{'type':'custom_tool_call','call_id':turn,'name':'apply_patch','input':patch}},
                     {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':turn,'output':'Success. Updated the following files'}}]
        (self.logs / (name+'.jsonl')).write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in data)+'\n', encoding='utf-8')

    def query(self, r):
        return {'source':'round','round':r['id']}

    def test_two_rounds_are_complete_and_immutable_after_later_edits_and_restart(self):
        r1 = self.store.begin_round('chat', 'turn-one')
        script = 'from pathlib import Path; p=Path(__import__("sys").argv[1]); p.write_text(p.read_text().replace("value = 1", "value = 2"))'
        subprocess.run([sys.executable,'-c',script,str(self.root/'src/example.py')],check=True)
        r1 = self.store.finish_round(r1['id'])
        self.log('resume','chat','turn-two','2026-10-26T10:05:00Z','第二轮')
        r2 = self.store.begin_round('chat')
        self.assertEqual(r2['turnId'],'turn-two')
        self.write('src/example.py','# unchanged\nvalue = 3\n# tail\n')
        r2 = self.store.finish_round(r2['id'])
        self.write('src/example.py','# unrelated later edit\nvalue = 99\n')
        store = ReviewStore(self.root,sessions=self.logs)
        a = store.compare_file(self.query(r1),'src/example.py')
        b = store.compare_file(self.query(r2),'src/example.py')
        self.assertEqual(a['before']['lines'],['# unchanged','value = 1','# tail'])
        self.assertEqual(a['after']['lines'],['# unchanged','value = 2','# tail'])
        self.assertEqual(b['before']['lines'],a['after']['lines'])
        self.assertEqual(b['after']['lines'],['# unchanged','value = 3','# tail'])
        self.assertEqual(store.begin_round('chat','turn-one'),r1)
        self.assertEqual(store.finish_round(r1['id']),r1)
        self.assertEqual(store.round_changes(self.query(r1))['records'][0]['afterHash'],a['after']['hash'])
        self.assertTrue(any('tok-' in line for line in a['after']['htmlLines']))
        selected = store.review_scope('chat',{'source':'round','round':'latest'})
        self.assertEqual(selected['query']['round'],r2['id'])
        self.assertEqual(store.review_scope('chat')['query']['round'],r2['id'])
        self.assertEqual(a['round']['request'],'第一轮：脚本修改')

    def test_new_deleted_chinese_files_and_filters_without_git(self):
        self.write('将删除.js','const old = 1;\n')
        r = self.store.begin_round('chat')
        (self.root/'将删除.js').unlink()
        self.write('目录/新增 中文.ts','const text: string = "中文";\n')
        r = self.store.finish_round(r['id']); q = self.query(r)
        actions = {x['path']:x['action'] for x in self.store.changes(q)['records']}
        self.assertEqual(actions,{'将删除.js':'Delete','目录/新增 中文.ts':'Add'})
        added = self.store.compare_file(q,'目录/新增 中文.ts')
        self.assertFalse(added['before']['exists']); self.assertTrue(added['after']['available'])
        deleted = self.store.compare_file(q,'将删除.js')
        self.assertFalse(deleted['after']['exists']); self.assertEqual(deleted['before']['lines'],['const old = 1;'])
        self.assertEqual(len(self.store.changes({**q,'include':'目录/*'})['records']),1)
        self.assertEqual(len(self.store.changes({**q,'exclude':'目录/*'})['records']),1)
        with self.assertRaises(ValueError): self.store.compare_file(q,'../outside.py')

    def test_cross_cwd_requires_exact_thread_and_relative_paths_use_original_cwd(self):
        absolute = str(self.root/'src/example.py')
        patch = f'*** Begin Patch\n*** Update File: {absolute}\n@@\n-value = 1\n+value = 2\n*** Update File: src/other.py\n@@\n-old\n+new\n*** End Patch'
        self.log('first','chat','turn-one','2026-10-26T10:00:00Z','跨目录',patch)
        self.log('foreign','foreign-chat','foreign-turn','2026-10-26T11:00:00Z','无关')
        self.assertEqual(self.store.catalog(),[])
        self.assertEqual(len(self.store.catalog('chat')),1)
        selected = self.store.review_scope('chat',{'source':'session','turnId':'turn-one'})
        self.assertEqual(selected['query']['start'],'1')
        records = self.store.changes(selected['query'])['records']
        self.assertEqual([r['path'] for r in records],['src/example.py'])
        self.assertIsNone(self.store.review_scope('missing')['query'])
        with self.assertRaises(ValueError): self.store.review_scope('chat',{'threadId':'foreign-chat'})
        with self.assertRaises(ValueError): self.store.find_turn('chat','foreign-turn')

    def test_incomplete_empty_and_foreign_rounds_never_masquerade_as_latest_edit(self):
        r = self.store.begin_round('chat')
        with self.assertRaises(ValueError): self.store.changes(self.query(r))
        with self.assertRaises(ValueError): self.store.review_scope('chat',{'source':'round','round':'latest'})
        r = self.store.finish_round(r['id'])
        self.assertEqual(r['changedFiles'],0)
        self.assertEqual(self.store.review_scope('chat')['query']['source'],'session')
        with self.assertRaises(ValueError): self.store.review_scope('other',self.query(r))
        with self.assertRaises(ValueError): self.store.begin_round('chat','missing-turn')

    def test_skipped_files_are_unavailable_not_invented_as_new_or_deleted(self):
        # Supported text extension, binary content at baseline.
        (self.root/'data.py').write_bytes(b'\x00binary old')
        r = self.store.begin_round('chat')
        self.write('data.py','value = 2\n')
        r = self.store.finish_round(r['id']); q = self.query(r)
        record = next(x for x in self.store.changes(q)['records'] if x['path']=='data.py')
        self.assertEqual(record['action'],'Update')
        view = self.store.compare_file(q,'data.py')
        self.assertFalse(view['before']['available']); self.assertTrue(view['before']['exists'])
        self.assertTrue(view['after']['available']); self.assertEqual(view['ops'],[])

    def test_old_approval_and_note_expire_without_rebinding_to_new_code(self):
        r = self.store.begin_round('chat')
        self.write('src/example.py','value = 2\n')
        r = self.store.finish_round(r['id']); q = self.query(r)
        result = self.store.changes(q); h = result['records'][0]['afterHash']
        item = {'title':'example','refs':[{'path':'src/example.py'}],'scope':q,'status':'approved',
                'confirmApproval':True,'seenHashes':{'src/example.py':h},'seenDiff':result['fingerprint']}
        state = self.store.save_batch({'revision':0,'items':[item],'annotations':[
            {'text':'保留接口','target':{'kind':'file','label':'example'},'refs':item['refs'],'scope':q,'seenHashes':{'src/example.py':h}}
        ]})
        self.assertFalse(state['items'][0]['stale'])
        self.write('src/example.py','value = 3\n')
        state = self.store.review_view()
        self.assertTrue(state['items'][0]['stale']); self.assertTrue(state['annotations'][0]['stale'])
        item.update(id=1,seenHashes={'src/example.py':self.store.read_file('src/example.py')['hash']},seenDiff=self.store.changes(q)['fingerprint'])
        with self.assertRaises(ValueError): self.store.save_item({**item,'revision':state['revision']})
        self.assertEqual(self.store.compare_file(q,'src/example.py')['after']['lines'],['value = 2'])

    def test_ui_scope_freezes_round_and_cannot_collect_or_confirm(self):
        r = self.store.begin_round('chat'); self.write('src/example.py','value = 2\n')
        r = self.store.finish_round(r['id'])
        view = self.store.review_scope('chat',{'source':'round','round':'latest'})
        app = App(self.store,None)
        data = ui_request(app,'meta',view=view)
        self.assertEqual(data['reviewScope']['query']['round'],r['id'])
        self.assertEqual(len(data['rounds']),1)
        for route in ('round','begin_round','finish_round','confirm_plan'):
            with self.assertRaises(ValueError): ui_request(app,route,{'roundId':r['id']},view)
        self.assertNotEqual(scope_identity(self.query(r)),scope_identity({'source':'round','round':'another'}))

    def test_mcp_round_tools_capture_endpoints_over_real_stdio(self):
        process = subprocess.Popen([sys.executable,str(BASE/'server.py'),'--mcp','--sessions',str(self.logs)],
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        def call(name,args):
            process.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{'projectRoot':str(self.root),**args}}})+'\n');process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            self.assertNotIn('error',reply)
            return reply['result']['structuredContent']
        try:
            r = call('review_round',{'action':'begin','threadId':'chat'})
            self.write('src/example.py','value = 2\n')
            done = call('review_round',{'action':'finish','roundId':r['id']})
            self.assertEqual(done['status'],'completed')
            sources = call('review_sources',{'threadId':'chat'})
            self.assertEqual(sources['sessions'][0]['turns'][0]['id'],'turn-one')
            opened = call('review_open',{'threadId':'chat','query':{'source':'round','round':'latest'}})
            self.assertEqual(opened['reviewScope']['query']['round'],r['id'])
        finally:
            process.stdin.close();process.wait(timeout=10);process.stdout.close();process.stderr.close()
