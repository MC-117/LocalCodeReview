"""Isolated generic project for browser interaction tests and the demo."""
import json
import difflib
from pathlib import Path
import sys
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from review_core import ReviewStore
from server import App

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding='utf-8')
run = BASE / '.test-artifacts' / 'browser-fixtures' / uuid.uuid4().hex
root, sessions = run / 'sample-project', run / 'sessions'
data = root / '.code-review'
root.mkdir(parents=True)
sessions.mkdir()
paths = ['src/01-core/item.h', 'src/01-core/item.cpp', 'src/02-store/store.cpp',
         'src/03-import/load.h', 'src/04-refs/ref.h', 'src/05-editor/中文 新文件.h']
patch = '*** Begin Patch\n'
for path in paths:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    before = ['// browser fixture'] + [f'// unchanged context {i:03d}' for i in range(1,180)]
    before[10:17] = ['/* multiline comment starts', 'continued comment */',
                     'const char* greeting = "你好 <script>safe</script>";',
                     'class ReviewExample {', 'public:',
                     '    int value() const { return 1; }', '};']
    after = before.copy()
    after[35] = 'int current = 2;'
    after[95:95] = ['// added block', 'int added = 3;']
    del after[145:147]
    text = '\n'.join(after)+'\n'
    file.write_text(text, encoding='utf-8')
    if '中文' in path:
        patch += f'*** Add File: {path}\n'+''.join('+'+line+'\n' for line in after)
    else:
        patch += f'*** Update File: {path}\n'+''.join(list(difflib.unified_diff([s+'\n' for s in before],[s+'\n' for s in after],n=2))[2:])
samples = {'src/06-languages/main.rs': 'fn main() { let message = "你好"; println!("{}", message); }\n',
           'src/06-languages/main.go': 'package main\nfunc main() { println("你好") }\n',
           'src/06-languages/example.py': 'class Example:\n    def greet(self):\n        return "你好"\n'}
for path, text in samples.items():
    file=root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text,encoding='utf-8')
    patch += f'*** Add File: {path}\n'+''.join('+'+line+'\n' for line in text.splitlines())
    paths.append(path)
patch += '*** End Patch'
records = [
    {'type':'session_meta', 'payload':{'id':'browser-fixture', 'cwd':str(root), 'timestamp':'2026-10-25T10:00:00Z'}},
    {'type':'event_msg', 'payload':{'type':'task_started', 'turn_id':'fixture-turn'}},
    {'type':'response_item', 'payload':{'type':'custom_tool_call', 'call_id':'fixture-patch', 'name':'apply_patch', 'input':patch}},
    {'type':'response_item', 'payload':{'type':'custom_tool_call_output', 'call_id':'fixture-patch', 'output':'Success. Updated the following files:\n'+'\n'.join('M '+p for p in paths)}}
]
(sessions/'fixture.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records), encoding='utf-8')
store = ReviewStore(root, data, sessions)
query = {'source':'session','session':store.catalog()[0]['id'],'snapshot':'','start':'','end':'','include':'','exclude':'','defaults':True,'attempts':True}
store.save_guide(0,query,[
    {'key':'interface','title':'接口与加载（样例）','purpose':'独立样例：关联接口与加载文件，演示跨目录导览。','validation':'样例未编译。','refs':[{'path':paths[i]} for i in (1,0,3)]},
    {'key':'storage','title':'存储与引用（样例）','purpose':'独立样例：集中讨论存储和引用边界。','validation':'样例未编译。','refs':[{'path':paths[i]} for i in (2,4)]},
    {'key':'editor','title':'编辑器（样例）','purpose':'演示中文路径。','validation':'样例未编译。','refs':[{'path':paths[5]}]},
    {'key':'languages','title':'多语言示例','purpose':'演示 Rust、Go、Python 的本地语法高亮。','validation':'未执行样例程序。','refs':[{'path':p} for p in samples]}
])
app = App(store, 0)
print(json.dumps({'url':app.url, 'root':str(root), 'data':str(data)}), flush=True)
app.run()
