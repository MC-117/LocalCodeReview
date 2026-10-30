import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from review_core import ReviewStore, decode, patch_literals
from server import App, Projects, _add_review_refs


class ReviewTests(unittest.TestCase):
    def setUp(self):
        scratch = BASE / '.test-artifacts' / 'test-runs'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.tmp.name) / '中文项目'
        self.root.mkdir()
        self.sessions = Path(self.tmp.name) / 'sessions'
        self.sessions.mkdir()
        self.store = ReviewStore(self.root, Path(self.tmp.name) / 'data', self.sessions)
        self.write('Engine/原接口.h', 'int old();\n')
        self.git('init', '-q')
        self.git('config', 'user.email', 'local-test@example.invalid')
        self.git('config', 'user.name', 'Local test')
        self.git('add', '.')
        self.git('commit', '-qm', 'test baseline')

    def test_review_refs_include_exact_round_scope(self):
        scope = {'source': 'round', 'round': 'a' * 20}
        value = {'items': [{'id': 3, 'scope': scope}],
                 'annotations': [{'id': 4, 'scope': scope}]}
        result = _add_review_refs(self.store, value)
        self.assertEqual(result['items'][0]['reviewRef'], 'round:' + 'a' * 20 + ':item:3')
        self.assertEqual(result['annotations'][0]['reviewRef'], 'round:' + 'a' * 20 + ':annotation:4')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True)

    def log(self, output=None):
        patch = '*** Begin Patch\n*** Update File: Engine/原接口.h\n@@\n-int old();\n+int changed();\n*** Add File: Engine/新增.h\n+// 中文\n*** End Patch'
        records = [
            {'type':'session_meta','payload':{'id':'test-session','cwd':str(self.root),'timestamp':'2026-10-25T10:00:00Z'}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'one'}},
            {'type':'turn_context','payload':{'turn_id':'one'}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'保留原接口'}]}},
            {'type':'response_item','payload':{'type':'custom_tool_call','call_id':'c1','name':'exec','input':'text(await tools.apply_patch('+json.dumps(patch)+'));'}},
            {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'c1','output':[{'type':'input_text','text':output or '{}'}]}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'two'}},
            {'type':'turn_context','payload':{'turn_id':'two'}},
            {'type':'response_item','payload':{'type':'custom_tool_call','call_id':'c2','name':'apply_patch','input':'*** Begin Patch\n*** Delete File: Engine/旧文件.h\n*** End Patch'}},
        ]
        p = self.sessions / 'test.jsonl'
        p.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records)+'\n{', encoding='utf-8')
        return self.store.catalog()[0]['id']

    def scope(self, source='git', **extra):
        return {'source':source, **extra}

    def item(self, scope, refs):
        return {'revision':0,'title':'功能组','purpose':'保留接口','validation':'未运行引擎','notes':'第 1 项需要讨论','status':'pending','scope':scope,'refs':refs}

    def approve(self, payload):
        changes = self.store.changes(payload['scope'])
        payload.update(status='approved', confirmApproval=True, seenDiff=changes['fingerprint'],
                       seenHashes={r['path']:r['currentHash'] for r in changes['records']})
        return payload

    def test_session_turns_patches_new_chinese_and_missing_receipt(self):
        sid = self.log()
        s = self.store.session(sid)
        self.assertEqual([t['number'] for t in s['turns']], [1,2])
        self.assertEqual(s['turns'][0]['label'],'保留原接口')
        self.assertEqual(len(s['records']), 3)
        self.assertFalse(s['records'][0]['confirmed'])
        q = self.scope('session',session=sid,start=1,end=1)
        changes = self.store.changes(q)
        self.assertEqual(len(changes['records']),2)
        self.assertTrue(any(r['action']=='Add' and r['path']=='Engine/新增.h' for r in changes['records']))
        self.assertEqual(self.store.changes({**q,'attempts':False})['records'],[])

    def test_success_receipt_and_failed_attempt(self):
        sid = self.log('Success. Updated the following files:\nM Engine/原接口.h\nA Engine/新增.h')
        self.assertTrue(self.store.session(sid)['records'][0]['confirmed'])
        sid = self.log('Error: Failed to find expected lines')
        self.assertFalse(self.store.session(sid)['records'][0]['confirmed'])

    def test_literal_parser_does_not_execute_or_interpolate(self):
        self.assertEqual(list(patch_literals('await tools.apply_patch("a\\nb")')),['a\nb'])
        self.assertEqual(list(patch_literals("tools.apply_patch('中文\\n行')")),['中文\n行'])
        self.assertEqual(list(patch_literals('tools.apply_patch(`hello ${danger()}`)')),[])

    def test_git_includes_index_worktree_new_and_rename(self):
        self.write('Engine/原接口.h', 'int staged();\n')
        self.git('add','.')
        self.write('Engine/原接口.h', 'int working();\n')
        self.write('Engine/中文 新文件.h','// 新文件\n')
        r = self.store.changes(self.scope())['records']
        self.assertEqual(len(r),2)
        self.assertIn('-int old();',r[1]['patch'] if r[1]['path'].endswith('原接口.h') else r[0]['patch'])
        self.assertTrue(any('+int working();' in x['patch'] for x in r))
        self.git('mv','Engine/原接口.h','Engine/重命名.h')
        r = self.store.changes(self.scope())['records']
        self.assertTrue(any(x['path']=='Engine/原接口.h' and x['action']=='Delete' for x in r))
        self.assertTrue(any(x['path']=='Engine/重命名.h' and x['action']=='Add' for x in r))

    def test_filter_thirdparty_outputs_and_chinese(self):
        self.write('Engine/ThirdParty/vendor.cpp','noise')
        self.write('Build/output.cpp','noise')
        self.write('Engine/中文.h','hello')
        r = self.store.changes(self.scope())['records']
        self.assertEqual([x['path'] for x in r],['Engine/中文.h'])
        self.assertEqual(self.store.changes(self.scope(include='Engine',exclude='*中文*'))['records'],[])
        self.assertEqual(len(self.store.changes(self.scope(defaults=False))['records']),3)

    def test_snapshot_without_git_and_deleted_new_file(self):
        snap = self.store.capture('修改前')
        self.write('Engine/新增.h','// 新增\n')
        (self.root/'Engine/原接口.h').unlink()
        r = self.store.changes(self.scope('snapshot',snapshot=snap['id']))['records']
        self.assertEqual({x['action'] for x in r},{'Add','Delete'})
        self.assertEqual(self.store.snapshot(snap['id'])['name'],'修改前')

    def test_approval_expires_notes_preserve_old_fingerprint_restart_export(self):
        self.write('Engine/原接口.h','int changed();\n')
        p = self.approve(self.item(self.scope(),[{'path':'Engine/原接口.h'}]))
        state = self.store.save_item(p)
        i=state['items'][0]
        self.assertFalse(i['stale'])
        old=i['approvedFingerprint']
        self.write('Engine/原接口.h','int changedAgain();\n')
        self.assertTrue(self.store.review_view()['items'][0]['stale'])
        p={'revision':state['revision'],'id':i['id'],'notes':'保留原接口'}
        state=self.store.save_item(p)
        self.assertEqual(state['items'][0]['approvedFingerprint'],old)
        self.assertTrue(state['items'][0]['stale'])
        other=ReviewStore(self.root,self.store.data,self.sessions)
        self.assertTrue(other.review_view()['items'][0]['stale'])
        text=other.export()
        self.assertIn('第 1 项',text);self.assertIn('已过期',text);self.assertIn('保留原接口',text)
        self.assertEqual((other.data/'review-export.md').read_text(encoding='utf-8'),text)
        p={**self.approve(self.item(self.scope(),[{'path':'Engine/原接口.h'}])),'revision':state['revision'],'id':1}
        self.assertFalse(other.save_item(p)['items'][0]['stale'])

    def test_edit_between_load_and_approval_rejected(self):
        self.write('Engine/原接口.h','int changed();\n')
        p=self.approve(self.item(self.scope(),[{'path':'Engine/原接口.h'}]))
        self.write('Engine/原接口.h','int unseen();\n')
        with self.assertRaisesRegex(ValueError,'代码已变化'):
            self.store.save_item(p)
        self.assertEqual(self.store.state()['items'],[])

    def test_revision_conflict_does_not_overwrite(self):
        p=self.item(self.scope(),[])
        self.store.save_item(p)
        with self.assertRaisesRegex(ValueError,'其他窗口'):
            self.store.save_item(p)
        self.assertEqual(len(self.store.state()['items']),1)

    def test_batch_saves_defaults_and_rolls_back_on_invalid_item(self):
        self.write('Engine/原接口.h','int changed();\n')
        first=self.approve(self.item(self.scope(),[{'path':'Engine/原接口.h'}]))
        second=copy.deepcopy(first);second['title']='第二组';second['status']='invalid'
        with self.assertRaisesRegex(ValueError,'状态无效'):
            self.store.save_batch({'revision':0,'items':[first,second]})
        self.assertEqual(self.store.state()['items'],[])
        second['status']='approved'
        state=self.store.save_batch({'revision':0,'items':[first,second]})
        self.assertEqual([i['id'] for i in state['items']],[1,2])
        self.assertTrue(all(i['status']=='approved' and not i['stale'] for i in state['items']))
        self.assertEqual(state['revision'],1)

    def test_changed_scope_and_missing_patch_cannot_be_silently_approved(self):
        sid=self.log()
        q=self.scope('session',session=sid)
        p=self.approve(self.item(q,[{'path':'Engine/原接口.h','record':'missing'}]))
        with self.assertRaisesRegex(ValueError,'关联修改块'):
            self.store.save_item(p)

    def test_path_escape_and_encoding_binary_limit(self):
        for path in ('../outside.txt','.git/config',str(self.store.data/'reviews.json')):
            with self.assertRaises(ValueError): self.store.read_file(path)
        self.assertIsNone(decode(b'\x00binary'))
        self.assertEqual(decode('中文'.encode('utf-16')),'中文')
        self.assertEqual(decode('中文'.encode('gb18030')),'中文')
        self.write('Engine/no-newline.h','old')
        self.git('add','.');self.git('commit','-qm','newline test')
        self.write('Engine/no-newline.h','new')
        diff=self.store.changes(self.scope())['records'][0]['patch']
        self.assertIn('-old\n\\ No newline',diff);self.assertIn('+new\n',diff)

    def test_http_loopback_origin_token_and_assets(self):
        app=App(self.store,0)
        thread=threading.Thread(target=app.http.serve_forever,daemon=True);thread.start()
        def request(method,path,headers=None,body=None):
            c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=10)
            c.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers or {})
            r=c.getresponse();data=r.read();status=r.status;c.close();return status,data
        try:
            self.assertEqual(app.http.server_address[0],'127.0.0.1')
            status,data=request('GET','/');self.assertEqual(status,200);self.assertIn(b'Codex',data)
            status,data=request('GET','/api/meta');self.assertEqual(status,200);self.assertEqual(json.loads(data)['token'],app.token)
            self.assertEqual(request('GET','/api/meta',{'Host':'evil.example'})[0],403)
            self.assertEqual(request('POST','/api/changes',body=self.scope())[0],403)
            self.assertEqual(request('POST','/api/changes',{'X-Review-Token':app.token,'Origin':'https://evil.example'},self.scope())[0],403)
            self.assertEqual(request('POST','/api/changes',{'X-Review-Token':app.token},self.scope())[0],200)
            self.assertEqual(request('GET','/api/file?path=../outside.txt')[0],400)
        finally:
            app.http.shutdown();app.http.server_close();thread.join()

    def test_mcp_protocol_and_results(self):
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'review_results','arguments':{'projectRoot':str(self.root)}}},
                  {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'review_results','arguments':{}}}]
        p=subprocess.run([sys.executable,str(BASE/'server.py'),'--mcp','--sessions',str(self.sessions)],input='\n'.join(json.dumps(r) for r in requests)+'\n',capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(p.returncode,0,p.stderr)
        replies=[json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies],[1,2,3,4])
        self.assertEqual(len(replies[1]['result']['tools']),15)
        self.assertTrue(all('projectRoot' in t['inputSchema']['required'] for t in replies[1]['result']['tools']))
        results_tool = next(t for t in replies[1]['result']['tools'] if t['name'] == 'review_results')
        self.assertIn('roundId', results_tool['inputSchema']['required'])
        self.assertIn('error',replies[2])
        self.assertIn('error',replies[3])
        self.assertTrue((self.root/'.code-review/.gitignore').exists())

    def test_full_compare_git_snapshot_new_deleted_and_unchanged_context(self):
        before='\n'.join('context '+str(i) for i in range(160))+'\n'
        self.write('Engine/full.cpp',before)
        self.git('add','.');self.git('commit','-qm','complete baseline')
        snap=self.store.capture('full baseline')
        after=before.replace('context 35\n','changed 35\n').replace('context 120\n','')+'added tail\n'
        self.write('Engine/full.cpp',after)
        for q in [self.scope(),self.scope('snapshot',snapshot=snap['id'])]:
            data=self.store.compare_file(q,'Engine/full.cpp')
            self.assertEqual(data['before']['lines'],before.splitlines())
            self.assertEqual(data['after']['lines'],after.splitlines())
            self.assertEqual(len([o for o in data['ops'] if o[0]!='equal']),3)
            self.assertIn('context 159',data['after']['lines'])
        self.write('Engine/新增比较.h','// 中文新增\n')
        data=self.store.compare_file(self.scope(),'Engine/新增比较.h')
        self.assertFalse(data['before']['exists']);self.assertEqual(data['ops'][0][0],'insert')
        (self.root/'Engine/full.cpp').unlink()
        data=self.store.compare_file(self.scope(),'Engine/full.cpp')
        self.assertFalse(data['after']['exists']);self.assertEqual(data['before']['lines'],before.splitlines())

    def test_full_session_reverse_is_conditional_and_never_fabricates_missing_old_file(self):
        sid=self.log();q=self.scope('session',session=sid,start=1,end=1)
        self.write('Engine/原接口.h','// before untouched\nint changed();\n// tail untouched\n')
        data=self.store.compare_file(q,'Engine/原接口.h')
        self.assertTrue(data['before']['available'])
        self.assertEqual(data['before']['lines'],['// before untouched','int old();','// tail untouched'])
        self.assertIn('不是历史完整快照',''.join(data['warnings']))
        self.write('Engine/原接口.h','int changed();\nint changed();\n')
        data=self.store.compare_file(q,'Engine/原接口.h')
        self.assertFalse(data['before']['available'],'duplicate matches cannot be guessed')
        self.assertEqual(len(data['after']['lines']),2)
        self.assertEqual(data['ops'],[])
        self.write('Engine/新增.h','// 中文\n')
        data=self.store.compare_file(q,'Engine/新增.h')
        self.assertTrue(data['before']['available']);self.assertFalse(data['before']['exists'])
        data=self.store.compare_file(self.scope('session',session=sid),'Engine/旧文件.h')
        self.assertFalse(data['before']['available'],'delete logs do not contain deleted file contents')

    def test_full_session_multiple_chunks_and_sequential_patches(self):
        from review_core import reverse_record
        lines=['head','new A','middle','new B','tail']
        rec={'action':'Update','patch':'@@\n head\n-old A\n+new A\n middle\n@@\n-old B\n+new B\n tail'}
        old,exists=reverse_record(lines,True,rec)
        self.assertTrue(exists);self.assertEqual(old,['head','old A','middle','old B','tail'])
        rec2={'action':'Update','patch':'@@\n-old A\n+newer A'}
        earlier,_=reverse_record(old,True,{'action':'Update','patch':'@@\n-original A\n+old A'})
        self.assertEqual(earlier[1],'original A')
        with self.assertRaises(ValueError):reverse_record(lines,True,rec2)

    def test_independent_annotations_and_function_guide_keep_ids_and_confirmation_boundary(self):
        self.write('Engine/原接口.h','int changed();\n')
        self.write('src/main.rs','fn main() {}\n')
        q=self.scope()
        state=self.store.save_guide(0,q,[{'key':'api','title':'接口兼容','purpose':'样例目的','validation':'未执行编译',
                                        'refs':[{'path':'Engine/原接口.h'},{'path':'src/main.rs'}]}])
        self.assertEqual(state['items'],[],'assistant guide is not user approval')
        note={'text':'保留整个类的接口','target':{'kind':'symbol','label':'类 Item','key':'Item'},
              'scope':q,'refs':[{'path':'Engine/原接口.h'}]}
        state=self.store.save_batch({'revision':state['revision'],'items':[],'annotations':[note]})
        self.assertEqual(state['annotations'][0]['id'],1)
        self.assertEqual(state['annotations'][0]['text'],'保留整个类的接口')
        self.assertFalse(state['annotations'][0]['stale'])
        first=state['annotations'][0]['contextHashes']
        state=self.store.save_guide(state['revision'],q,[{'key':'changed-guide','title':'重新组织导览','refs':[{'path':'src/main.rs'}]}])
        self.assertEqual(state['annotations'][0]['id'],1,'guide regrouping never renumbers feedback')
        self.write('Engine/原接口.h','int changedAgain();\n')
        note.update(id=1,text='保留旧接口参数')
        state=self.store.save_batch({'revision':state['revision'],'items':[],'annotations':[note]})
        self.assertEqual(state['annotations'][0]['contextHashes'],first,'editing notes preserves original context')
        self.assertTrue(state['annotations'][0]['stale'])
        plan=self.store.propose_plan(state['revision'],[],'针对 R1 保留原接口，更新实现。',[1])
        self.assertEqual(plan['annotationIds'],[1])
        self.assertFalse(plan['mayApply'])
        with self.assertRaises(ValueError):self.store.begin_changes(plan['id'])
        self.assertIn('意见 R1',self.store.export())
        self.assertIn('保留旧接口参数',self.store.export())

    def test_annotations_and_receipts_batch_is_atomic_and_mainstream_files_visible(self):
        self.write('src/main.go','package main\n')
        q=self.scope()
        self.assertIn('src/main.go',[r['path'] for r in self.store.changes(q)['records']])
        good={'text':'整个功能需要讨论','target':{'kind':'general','label':'本轮修改'},'scope':q,'refs':[{'path':'src/main.go'}]}
        bad={**good,'refs':[{'path':'../outside.py'}]}
        with self.assertRaises(ValueError):self.store.save_batch({'revision':0,'items':[],'annotations':[good,bad]})
        self.assertEqual(self.store.state()['annotations'],[])
        state=self.store.save_batch({'revision':0,'items':[],'annotations':[good]})
        self.assertEqual(state['annotations'][0]['id'],1)
        with self.assertRaises(ValueError):self.store.save_guide(0,q,[])
        with self.assertRaises(ValueError):self.store.save_guide(state['revision'],q,[{'key':'bad','title':'bad','refs':[{'path':'unknown.cpp'}]}])

    def test_generic_projects_have_isolated_cache_and_sources(self):
        self.log()
        other=self.root.parent/'another-project';other.mkdir()
        projects=Projects(self.sessions)
        try:
            a,b=projects.get(str(self.root)),projects.get(str(other))
            self.assertIs(projects.get(str(self.root)),a)
            self.assertIsNone(a.http, 'MCP mode does not bind an HTTP port')
            self.assertIsNone(b.http)
            self.assertNotEqual(a.token,b.token)
            self.assertEqual(a.store.data,self.root/'.code-review')
            self.assertEqual(b.store.data,other/'.code-review')
            self.assertEqual(len(a.store.catalog()),1)
            self.assertEqual(b.store.catalog(),[])
            a.store.save_item(self.item(self.scope(),[]))
            self.assertEqual(b.store.state()['items'],[])
            self.assertFalse((self.root/'.gitignore').exists())
            self.assertFalse((BASE/'data').exists())
            with self.assertRaises(ValueError):projects.get('relative')
        finally:projects.close()

    def reviewed_item(self):
        self.write('Engine/原接口.h','int reviewed();\n')
        return self.store.save_item(self.approve(self.item(self.scope(),[{'path':'Engine/原接口.h'}])))

    def test_plan_requires_separate_human_confirmation(self):
        state=self.reviewed_item()
        before=(self.root/'Engine/原接口.h').read_bytes()
        with self.assertRaises(ValueError):self.store.begin_changes('no-plan')
        plan=self.store.propose_plan(state['revision'],[1],'第 1 项：保留接口，补充实现；验证接口调用。')
        self.assertEqual(plan['status'],'awaiting_confirmation')
        self.assertFalse(plan['mayApply'])
        with self.assertRaises(ValueError):self.store.begin_changes(plan['id'])
        with self.assertRaises(ValueError):self.store.finish_changes(plan['id'],'result','checks')
        with self.assertRaises(ValueError):self.store.confirm_plan(plan['id'],' ')
        self.assertEqual(self.store.confirm_plan(plan['id'],'确认，按这个方案修改')['status'],'confirmed')
        self.assertIn('用户已确认方案',(self.store.data/'plan.md').read_text(encoding='utf-8'))
        self.assertEqual(self.store.begin_changes(plan['id'])['status'],'applying')
        done=self.store.finish_changes(plan['id'],'已记录结果','仅运行样例测试')
        self.assertEqual(done['status'],'completed')
        self.assertFalse(done['mayApply'])
        self.assertIn('已记录结果',(self.store.data/'plan.md').read_text(encoding='utf-8'))
        self.assertEqual((self.root/'Engine/原接口.h').read_bytes(),before,'workflow never applies code')
        self.assertIn('勾选不是修改授权',self.store.export())

    def test_feedback_changes_invalidate_confirmed_plan(self):
        state=self.reviewed_item()
        p=self.store.propose_plan(state['revision'],[1],'保留接口')
        self.store.confirm_plan(p['id'],'同意此方案')
        self.store.save_item({'revision':state['revision'],'id':1,'notes':'再保留旧参数'})
        self.assertEqual(self.store.workflow()['status'],'invalidated')
        with self.assertRaises(ValueError):self.store.begin_changes(p['id'])
        with self.assertRaises(ValueError):self.store.propose_plan(state['revision'],[1],'旧版本方案')

    def test_code_changes_permanently_invalidate_plan(self):
        state=self.reviewed_item()
        p=self.store.propose_plan(state['revision'],[1],'保留接口')
        self.store.confirm_plan(p['id'],'按这个方案修改')
        self.write('Engine/原接口.h','int newer();\n')
        with self.assertRaises(ValueError):self.store.begin_changes(p['id'])
        self.write('Engine/原接口.h','int reviewed();\n')
        self.assertEqual(self.store.workflow()['status'],'invalidated')
        self.assertIn('方案已过期',(self.store.data/'plan.md').read_text(encoding='utf-8'))

    def test_applying_can_change_code_but_updated_feedback_blocks_finish(self):
        state=self.reviewed_item()
        p=self.store.propose_plan(state['revision'],[1],'更新实现')
        self.store.confirm_plan(p['id'],'确认方案')
        self.store.begin_changes(p['id'])
        self.write('Engine/原接口.h','int changedByAssistant();\n')
        self.assertEqual(self.store.workflow()['status'],'applying')
        self.store.save_item({'revision':state['revision'],'id':1,'notes':'先暂停这里'})
        with self.assertRaises(ValueError):self.store.finish_changes(p['id'],'done','none')


if __name__=='__main__':
    unittest.main()
