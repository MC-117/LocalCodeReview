"""Local review storage and read-only source adapters. Python 3.10+, no dependencies."""
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from syntax import highlight_lines, supported_filename
from contextlib import contextmanager
from datetime import datetime, timezone
from round_store import RoundStore
from session_literals import patch_literals

MAX_FILE = 1024 * 1024
EXTENSIONS = {'.h', '.hpp', '.hxx', '.c', '.cpp', '.cc', '.cs', '.py', '.js', '.mjs',
              '.ts', '.tsx', '.jsx', '.json', '.md', '.txt', '.xml', '.ini', '.yaml',
              '.yml', '.bat', '.ps1', '.html', '.css', '.glsl', '.hlsl', '.mat', '.sh',
              '.vcxproj', '.filters', '.cmake', '.sln'}
EXCLUDED = {'.git', '.vs', '.idea', 'node_modules', '__pycache__', 'thirdparty',
            'binaries', 'build', 'intermediate', 'debug', 'release', 'x64', 'x86',
            'resource', '.shdtmp', 'localreview', 'local-code-review', '.code-review', '.codex', '.agents',
            '_vendor', '.test-artifacts'}


def scope_identity(query):
    value = {'source': query.get('source', 'session'), 'include': query.get('include') or '',
             'exclude': query.get('exclude') or '', 'defaults': query.get('defaults', True)}
    if value['source'] == 'session':
        value.update(session=query.get('session') or '', start=str(query.get('start') or ''),
                     end=str(query.get('end') or ''), attempts=query.get('attempts', True),
                     turnId=query.get('turnId') or '')
    if value['source'] == 'snapshot':
        value['snapshot'] = query.get('snapshot') or ''
    if value['source'] == 'round':
        value['round'] = query.get('round') or ''
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def unified(before, after, path):
    lines = difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/' + path, tofile='b/' + path)
    return ''.join(s if s.endswith('\n') else s + '\n\\ No newline at end of file\n' for s in lines)


def decode(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return data.decode('utf-16')
        except UnicodeError:
            return None
    if b'\x00' in data:
        return None
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    return None


def patch_chunks(patch):
    """Read apply_patch chunks without executing or guessing missing context."""
    chunks, old, new = [], [], []
    changed = False
    for line in patch.splitlines():
        if line.startswith('@@'):
            if changed:
                chunks.append((old, new))
            old, new, changed = [], [], False
        elif line.startswith('***'):
            if line.startswith('*** Move to:'):
                raise ValueError('移动补丁缺少完整历史文件，请使用快照比较')
        elif line.startswith((' ', '+', '-')):
            if line[0] != '+': old.append(line[1:])
            if line[0] != '-': new.append(line[1:])
            changed = changed or line[0] in '+-'
        elif line:
            raise ValueError('补丁上下文格式无法可靠还原')
    if changed:
        chunks.append((old, new))
    return chunks


def unique_match(lines, needle):
    if not needle:
        return None
    found = None
    for i in range(len(lines) - len(needle) + 1):
        if lines[i:i + len(needle)] == needle:
            if found is not None:
                return None
            found = i
    return found


def reverse_record(lines, exists, record):
    if record['action'] == 'Add':
        added = [s[1:] for s in record['patch'].splitlines() if s.startswith('+')]
        if not exists or added != lines:
            raise ValueError('新增文件已发生后续变化，不能还原完整旧侧')
        return [], False
    if record['action'] != 'Update' or not exists:
        raise ValueError('会话未保存被删除文件的完整原文')
    chunks = patch_chunks(record['patch'])
    if not chunks:
        raise ValueError('没有可定位的修改上下文')
    locations, end = [], 0
    for old, new in chunks:
        pos = unique_match(lines, new)
        if pos is None or pos < end:
            raise ValueError('补丁与当前文件不再唯一匹配，不能可靠反推旧侧')
        locations.append((pos, len(new), old))
        end = pos + len(new)
    lines = list(lines)
    for pos, count, old in reversed(locations):
        lines[pos:pos + count] = old
    return lines, True


class ReviewStore(RoundStore):
    def __init__(self, root, data=None, sessions=None):
        self.root = Path(root).resolve()
        self.data = Path(data or self.root / '.code-review').resolve()
        self.sessions = Path(sessions or Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions')
        self.data.mkdir(parents=True, exist_ok=True)
        ignore = self.data / '.gitignore'
        if not ignore.exists():
            ignore.write_text('*\n', encoding='utf-8')
        self.lock = threading.RLock()
        self.session_cache = {}
        self._migrate_history_storage()

    def _migrate_history_storage(self):
        """Move development-era inline histories out of the current-state index."""
        path = self.data / 'reviews.json'
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if state.get('historySchema') == 1:
            return
        changed = False
        for kind in ('items', 'annotations'):
            for entry in state.get(kind, []):
                history = entry.pop('history', None)
                if history:
                    history_path = self.data / 'history' / kind / (str(entry['id']) + '.jsonl')
                    history_path.parent.mkdir(parents=True, exist_ok=True)
                    history_path.write_text('\n'.join(json.dumps({'migrated': True, **row}, ensure_ascii=False) for row in history) + '\n', encoding='utf-8')
                    entry['historyCount'] = len(history)
                    entry['historyFile'] = f'history/{kind}/{entry["id"]}.jsonl'
                    changed = True
                elif 'history' in entry:
                    changed = True
        state['historySchema'] = 1
        if changed or 'historySchema' not in state:
            atomic_json(path, state)

    def _append_history(self, records):
        for record in records:
            path = self.data / 'history' / record['kind'] / (str(record['id']) + '.jsonl')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record['entry'], ensure_ascii=False) + '\n')

    def history(self, kind, identifier, limit=100):
        if kind not in ('items', 'annotations'):
            raise ValueError('历史类型无效')
        path = self.data / 'history' / kind / (str(identifier) + '.jsonl')
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows[-max(1, min(int(limit or 100), 500)):]

    @contextmanager
    def writing(self):
        """Serialize independent MCP/UI processes without overwriting revisions."""
        with self.lock, (self.data / 'write.lock').open('a+b') as f:
            if f.tell() == 0:
                f.write(b'0')
                f.flush()
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                deadline = time.monotonic() + 10
                while True:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise ValueError('另一个进程正在保存，请稍后重试')
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                f.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def relative(self, value):
        value = str(value).replace('\\', '/')
        p = Path(value)
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if not p.is_relative_to(self.root) or p == self.root:
            raise ValueError('路径必须位于项目目录内')
        if any(part.lower() == '.git' for part in p.relative_to(self.root).parts) or p.is_relative_to(self.data):
            raise ValueError('不允许读取 Git 内部或审查数据')
        return p.relative_to(self.root).as_posix()

    def read_file(self, value):
        path = self.root / self.relative(value)
        if not path.exists():
            return {'exists': False, 'hash': 'missing', 'text': '', 'note': '文件已不存在'}
        if not path.is_file():
            raise ValueError('路径不是文件')
        if path.stat().st_size > MAX_FILE:
            # Hash large files in chunks so review expiry still works.
            h = hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda: f.read(65536), b''):
                    h.update(block)
            return {'exists': True, 'hash': h.hexdigest(), 'text': None, 'note': '超过 1 MiB，仅显示摘要'}
        raw = path.read_bytes()
        text = decode(raw)
        return {'exists': True, 'hash': digest(raw), 'text': text,
                'note': '' if text is not None else '二进制或无法识别的文本编码'}

    def git(self, *args, check=True):
        p = subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=self.root,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if check and p.returncode:
            raise ValueError(p.stderr.decode('utf-8', errors='replace')[:500])
        return p

    def catalog(self, thread_id=None):
        result = []
        if not self.sessions.exists():
            return result
        for path in self.sessions.rglob('*.jsonl'):
            try:
                with path.open(encoding='utf-8') as f:
                    first = json.loads(f.readline())
                meta = first.get('payload', {})
                if first.get('type') != 'session_meta':
                    continue
                if Path(meta.get('cwd', '')).resolve() != self.root and not (thread_id and meta.get('id') == thread_id):
                    continue
                sid = digest(str(path))[:20]
                result.append({'id': sid, 'label': path.stem, 'time': meta.get('timestamp', ''),
                               'path': str(path), 'cwd': meta.get('cwd'), 'bytes': path.stat().st_size, 'threadId': meta.get('id')})
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda x: x['time'], reverse=True)

    def review_scope(self, thread_id=None, query=None, reason=None, standalone=False):
        """Resolve assistant-selected scope without guessing another conversation."""
        if query is not None and not isinstance(query, dict):
            raise ValueError('query 必须是比较范围对象')
        if thread_id and query and query.get('threadId') and query['threadId'] != thread_id:
            raise ValueError('范围不能切换到其他对话')
        thread_id = thread_id or (query or {}).get('threadId')
        sessions = self.catalog(thread_id)
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
            raise ValueError('threadId 必须是可信上下文中的当前对话编号')
        if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
            raise ValueError('范围说明必须是 2000 字以内的文本')
        if query is not None and not isinstance(query, dict):
            raise ValueError('query 必须是比较范围对象')
        q = dict(query or {})
        allowed = {'source','session','snapshot','start','end','include','exclude','defaults','attempts',
                   'threadId','turnId','round'}
        if set(q) - allowed:
            raise ValueError('比较范围包含不支持的字段')
        latest = next((r for r in self.rounds(thread_id) if r['status'] == 'completed' and r['changedFiles']), None) if thread_id else None
        q.setdefault('source', 'round' if latest else 'session')
        if q['source'] not in ('session','snapshot','git','round'):
            raise ValueError('比较来源无效')
        selected_round = None
        if q['source'] == 'round':
            if not q.get('round') or q['round'] == 'latest':
                if not latest:
                    raise ValueError('指定对话还没有已完成且有修改的单轮快照')
                q['round'] = latest['id']
            selected_round, _, _ = self.round_endpoints(q)
            if thread_id and selected_round['threadId'] != thread_id:
                raise ValueError('单轮审查不属于当前对话')
        if q['source'] == 'session':
            if thread_id:
                if q.get('threadId') and q['threadId'] != thread_id:
                    raise ValueError('范围不能切换到其他对话')
                q['threadId'] = thread_id
            if q.get('turnId'):
                turn = self.find_turn(thread_id, q['turnId'])
                if q.get('session') and q['session'] != turn['session']:
                    raise ValueError('指定轮次不属于所选日志段')
                q.update(session=turn['session'], start=str(turn['number']), end=str(turn['number']))
            if not q.get('session'):
                match = next((s for s in sessions if s.get('threadId') == thread_id), None) if thread_id else None
                if match:
                    q['session'] = match['id']
                elif thread_id:
                    return {'query': None, 'threadId': thread_id, 'origin': 'unbound',
                            'reason': '当前对话的本地修改记录尚不可用；未切换到其他对话。请稍后刷新或由助手指定快照。'}
                elif standalone and sessions:
                    q['session'] = sessions[0]['id']
                else:
                    return {'query': None, 'threadId': thread_id, 'origin': 'unbound',
                            'reason': '尚未绑定审查范围，请让助手根据当前对话设置；也可在选项中手动选择。'}
            if not any(s['id'] == q['session'] for s in sessions):
                raise ValueError('会话不存在或不属于当前项目')
            if thread_id and not any(s['id'] == q['session'] and s.get('threadId') == thread_id for s in sessions):
                raise ValueError('会话不属于指定对话')
            for key in ('start','end'):
                value = q.get(key)
                if value not in (None, ''):
                    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
                        raise ValueError('轮次必须是正整数')
                    q[key] = str(int(value))
            if q.get('start') and q.get('end') and int(q['start']) > int(q['end']):
                raise ValueError('起始轮次不能大于结束轮次')
        elif q['source'] == 'snapshot':
            self.snapshot(q.get('snapshot', ''))
        for key in ('include','exclude'):
            if key in q and not isinstance(q[key], str):
                raise ValueError('路径过滤必须是文本')
        for key in ('defaults','attempts'):
            if key in q and not isinstance(q[key], bool):
                raise ValueError('过滤选项必须是布尔值')
        q.setdefault('defaults', True)
        q.setdefault('attempts', True)
        origin = 'assistant' if query is not None else 'current-chat' if thread_id else 'recent-session'
        description = reason or ('已选择最近有修改的一轮：' + selected_round['label'] if selected_round else None) or {'assistant':'助手根据对话上下文设置的比较范围。',
                                'current-chat':'已自动绑定当前对话，显示该对话的全部记录。',
                                'recent-session':'独立页面暂用项目最近的对话；不代表当前 Codex 对话。'}[origin]
        return {'query': q, 'threadId': thread_id, 'origin': origin, 'reason': description, 'round': selected_round}

    def session(self, sid, thread_id=None):
        entry = next((s for s in self.catalog(thread_id) if s['id'] == sid), None)
        if entry is None:
            raise ValueError('会话不存在或不属于当前项目')
        path = Path(entry['path'])
        signature = (path.stat().st_size, path.stat().st_mtime_ns)
        cached = self.session_cache.get(sid)
        if cached and cached[0] == signature:
            return cached[1]
        calls, outputs, turns, turn, warnings = [], {}, [], 0, []
        turn_ids = {}
        with path.open(encoding='utf-8') as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # An active JSONL may end in a partial record.
                p = event.get('payload', {})
                if (event.get('type') == 'event_msg' and p.get('type') == 'task_started') or event.get('type') == 'turn_context':
                    tid = p.get('turn_id')
                    if tid and tid not in turn_ids:
                        turn += 1
                        turn_ids[tid] = turn
                        turns.append({'number': turn, 'id': tid, 'label': '', 'time': event.get('timestamp', '')})
                    elif tid:
                        turn = turn_ids[tid]
                if event.get('type') == 'event_msg' and p.get('type') == 'user_message' and not turn_ids:
                    turn += 1
                    turns.append({'number': turn, 'label': p.get('message', '')[:180], 'time': event.get('timestamp', '')})
                if event.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') == 'user' and turns:
                    label = ''.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
                    if not label.lstrip().startswith(('<environment_context>', '<send_user_message_question_reply>')):
                        turns[-1]['label'] = label[:180]
                if event.get('type') != 'response_item':
                    continue
                typ = p.get('type')
                if typ in ('custom_tool_call_output', 'function_call_output'):
                    outputs[p.get('call_id')] = p.get('output', '')
                elif typ in ('custom_tool_call', 'function_call'):
                    source = p.get('input', p.get('arguments', ''))
                    if not isinstance(source, str):
                        continue
                    if p.get('name', '').split('__')[-1] == 'apply_patch':
                        patches = [source]
                        if source.startswith('{'):
                            try:
                                a = json.loads(source)
                                patches = [a.get('patch', a.get('input', ''))]
                            except ValueError:
                                pass
                    else:
                        patches = list(patch_literals(source))
                    if patches:
                        calls.append((p.get('call_id'), turn, event.get('timestamp', ''), patches))
                    elif re.search(r'write_text\(|write_bytes\(|Set-Content|WriteAllText|writeFileSync|apply_patch', source):
                        warnings.append(f'第 {turn} 轮存在未解码的潜在写入；日志无法保证覆盖脚本或外部编辑')
        records = []
        for call_id, turn, time, patches in calls:
            output = outputs.get(call_id, '')
            if isinstance(output, list):
                output = '\n'.join(c.get('text', '') for c in output if isinstance(c, dict))
            elif not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            # Fail closed: an attempted patch is not an applied patch.
            success = len(patches) == 1 and 'Success. Updated the following files' in output and not re.search(r'(?i)failed|error|rejected', output)
            for patch in patches:
                for m in re.finditer(r'^\*\*\* (Update|Add|Delete) File: (.+)\r?\n([\s\S]*?)(?=^\*\*\* (?:Update|Add|Delete) File: |^\*\*\* End Patch|\Z)', patch, re.M):
                    action, file, body = m.groups()
                    try:
                        original = Path(file.strip())
                        file = self.relative(original if original.is_absolute() else Path(entry['cwd']) / original)
                    except ValueError:
                        continue
                    # Keep the precise historical patch, rather than inventing a net diff.
                    rec = {'path': file, 'action': action, 'patch': body.rstrip('\r\n'), 'turn': turn,
                           'time': time, 'confirmed': success, 'call': call_id,
                           'evidence': '成功回执' if success else '日志未提供可确认的逐文件回执'}
                    rec['id'] = digest([call_id, file, body])[:24]
                    records.append(rec)
        value = {'entry': entry, 'turns': turns, 'records': records,
                 'warnings': sorted(set(warnings)), 'coverage': '解析 apply_patch 字面量及同次调用内的顶层 const 字符串引用；不执行日志代码。动态表达式、脚本、外部编辑、压缩日志不保证覆盖。'}
        for t in turns:
            t['patchCount'] = sum(r['turn'] == t['number'] for r in records)
        self.session_cache[sid] = signature, value
        return value

    def allowed(self, path, includes='', excludes='', defaults=True):
        parts = Path(path).parts
        if defaults and (any(p.lower() in EXCLUDED for p in parts[:-1]) or
                         (Path(path).suffix.lower() not in EXTENSIONS and not supported_filename(path))):
            return False
        patterns = lambda text: [s.strip().replace('\\', '/') for s in re.split('[,;\n]', text) if s.strip()]
        def match(pattern):
            return fnmatch.fnmatch(path.lower(), pattern.lower()) or path.lower().startswith(pattern.rstrip('/').lower() + '/')
        return (not includes or any(match(s) for s in patterns(includes))) and not any(match(s) for s in patterns(excludes))

    def snapshots(self):
        result = []
        for p in (self.data / 'snapshots').glob('*.json'):
            m = json.loads(p.read_text(encoding='utf-8'))
            result.append({k: m[k] for k in ('id', 'name', 'time', 'count', 'skipped')})
        return sorted(result, key=lambda x: x['time'], reverse=True)

    def capture(self, name):
        with self.writing():
            return self._capture(name)

    def _capture(self, name):
        files, skipped, total, unavailable = {}, [], 0, {}
        for base, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED and
                       not (Path(base) / d).is_symlink() and not (Path(base) / d).is_junction()]
            for file in names:
                p = Path(base) / file
                rel = p.relative_to(self.root).as_posix()
                if p.is_symlink() or not self.allowed(rel):
                    continue
                try:
                    content = self.read_file(rel)
                    if content['text'] is None:
                        skipped.append(rel)
                        unavailable[rel] = content
                        continue
                    total += len(content['text'].encode('utf-8'))
                    if total > 64 * MAX_FILE:
                        raise ValueError('快照超过 64 MiB，未保存；请缩小项目范围')
                    h = content['hash']
                    blob = self.data / 'blobs' / (h + '.json')
                    if not blob.exists():
                        atomic_json(blob, content)
                    # A file can disappear while a live workspace is being captured.
                    if content['exists']:
                        files[rel] = h
                except OSError:
                    skipped.append(rel)
        sid = digest([now(), files])[:20]
        manifest = {'id': sid, 'name': str(name)[:100] or '工作区快照', 'time': now(),
                    'files': files, 'count': len(files), 'skipped': skipped, 'unavailable': unavailable}
        atomic_json(self.data / 'snapshots' / (sid + '.json'), manifest)
        return manifest

    def snapshot(self, sid):
        if not re.fullmatch('[a-f0-9]{20}', sid):
            raise ValueError('快照编号无效')
        p = self.data / 'snapshots' / (sid + '.json')
        if not p.exists():
            raise ValueError('快照不存在')
        return json.loads(p.read_text(encoding='utf-8'))

    def workspace_paths(self):
        # NUL-delimited paths handle Chinese, whitespace and rename destinations.
        changed = self.git('diff', '--name-only', '-z', 'HEAD', '--').stdout
        new = self.git('ls-files', '--others', '--exclude-standard', '-z').stdout
        return sorted(set(p.decode('utf-8') for p in (changed + new).split(b'\x00') if p))

    def changes(self, query):
        source = query.get('source', 'session')
        if source == 'round':
            return self.round_changes(query)
        includes, excludes = query.get('include', ''), query.get('exclude', '')
        defaults = query.get('defaults', True)
        records, warnings = [], []
        if source == 'session':
            s = self.session(query['session'], query.get('threadId'))
            lo, hi = int(query.get('start') or 0), int(query.get('end') or 999999)
            if query.get('turnId'):
                selected = next((t for t in s['turns'] if t.get('id') == query['turnId']), None)
                if selected is None:
                    raise ValueError('所选日志段中没有该轮次')
                lo = hi = selected['number']
            records = [dict(r) for r in s['records'] if lo <= r['turn'] <= hi and
                       (r['confirmed'] or query.get('attempts', True))]
            warnings = s['warnings'] + [s['coverage'], '会话补丁是历史操作，不等于当前工作区净差异，也不证明仍未提交。']
        elif source in ('git', 'snapshot'):
            manifest = self.snapshot(query['snapshot']) if source == 'snapshot' else None
            paths = set(manifest['files']) if manifest else set()
            if manifest:
                for base, dirs, names in os.walk(self.root):
                    dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED and not (Path(base) / d).is_symlink() and not (Path(base) / d).is_junction()]
                    paths.update((Path(base) / n).relative_to(self.root).as_posix() for n in names if self.allowed((Path(base) / n).relative_to(self.root).as_posix()))
                warnings = ['快照比较不依赖 Git；仅覆盖快照规则内的文本。', f"基线略过 {len(manifest['skipped'])} 个不可展示文件。"]
            else:
                paths = self.workspace_paths()
                warnings = ['HEAD → 当前工作区（包含暂存、未暂存和未跟踪文件）；不区分会话归属。']
            for file in sorted(paths):
                if not self.allowed(file, includes, excludes, defaults):
                    continue
                current = self.read_file(file)
                if manifest:
                    h = manifest['files'].get(file)
                    before = json.loads((self.data / 'blobs' / (h + '.json')).read_text(encoding='utf-8')) if h else {'exists': False, 'text': '', 'hash': 'missing'}
                else:
                    p = self.git('show', 'HEAD:' + file, check=False)
                    before = {'exists': p.returncode == 0, 'text': decode(p.stdout) if not p.returncode else '',
                              'hash': digest(p.stdout) if not p.returncode else 'missing'}
                if before['hash'] == current['hash']:
                    continue
                text = unified(before['text'] or '', current['text'] or '', file) if before['text'] is not None and current['text'] is not None else '二进制或过大文件，仅提供内容摘要。'
                records.append({'id': digest([source, file])[:24], 'path': file,
                                'action': 'Delete' if not current['exists'] else 'Update' if before['exists'] else 'Add',
                                'patch': text, 'confirmed': True, 'turn': None, 'time': '',
                                'beforeHash': before['hash'], 'afterHash': current['hash']})
        else:
            raise ValueError('未知比较来源')
        records = [r for r in records if self.allowed(r['path'], includes, excludes, defaults)]
        current = {}
        for r in records:
            if r['path'] not in current:
                current[r['path']] = self.read_file(r['path'])['hash']
            r['currentHash'] = current[r['path']]
        return {'records': records, 'warnings': warnings, 'fingerprint': digest(records), 'query': query}

    def compare_file(self, query, file, record_ids=None):
        if query.get('source') == 'round':
            return self.round_compare(query, file)
        file = self.relative(file)
        current = self.read_file(file)
        after = {'lines': (current['text'] or '').splitlines(), 'exists': current['exists'],
                 'available': current['text'] is not None, 'label': '当前文件', 'note': current['note']}
        before = {'lines': [], 'exists': True, 'available': True, 'label': '修改前', 'note': ''}
        records, warnings, anchors = [], [], []
        source = query.get('source', 'session')
        if source == 'snapshot':
            manifest = self.snapshot(query['snapshot'])
            if file in manifest['skipped']:
                before.update(available=False, note='该文件在快照中被略过，缺少旧侧原文')
            else:
                h = manifest['files'].get(file)
                content = json.loads((self.data/'blobs'/(h+'.json')).read_text(encoding='utf-8')) if h else {'exists': False, 'text': ''}
                before.update(lines=(content['text'] or '').splitlines(), exists=content['exists'])
            before['label'] = '快照 · ' + manifest['name']
        elif source == 'git':
            p = self.git('show', 'HEAD:' + file, check=False)
            content = decode(p.stdout) if not p.returncode and len(p.stdout) <= MAX_FILE else '' if p.returncode else None
            before.update(lines=(content or '').splitlines(), exists=not p.returncode,
                          available=content is not None, label='HEAD', note='旧侧为二进制或超过 1 MiB' if content is None else '')
        elif source == 'session':
            records = [r for r in self.changes(query)['records'] if r['path'] == file and (not record_ids or r['id'] in record_ids)]
            before['label'] = '按所选补丁反推的旧侧'
            warnings.append('右侧为当前文件。旧侧仅从当前文本反推所选补丁，不是历史完整快照；补丁外内容沿用当前文件。')
            if any(not r['confirmed'] for r in records):
                warnings.append('包含无成功回执的日志记录，匹配文本不证明补丁曾成功应用。')
            try:
                if not after['available']:
                    raise ValueError(current['note'])
                lines, exists = after['lines'], after['exists']
                for record in reversed(records):
                    lines, exists = reverse_record(lines, exists, record)
                before.update(lines=lines, exists=exists)
            except ValueError as e:
                before.update(available=False, note=str(e))
                # Keep the entire current file, with only uniquely matched historical anchors.
                for record in records:
                    try:
                        for old, new in patch_chunks(record['patch']):
                            pos = unique_match(after['lines'], new)
                            if pos is not None:
                                anchors.append({'start': pos, 'end': pos + len(new)})
                    except ValueError:
                        pass
                warnings.append('旧侧不可还原。当前页仅标记唯一匹配的历史补丁上下文，不代表当前净差异；原始补丁仍可展开查看。')
        else:
            raise ValueError('未知比较来源')
        ops = []
        if before['available'] and after['available']:
            ops = [list(op) for op in difflib.SequenceMatcher(None, before['lines'], after['lines']).get_opcodes()]
        for side in (before, after):
            side['htmlLines'], side['language'] = highlight_lines(file, side['lines']) if side['available'] else ([], 'text')
        return {'path': file, 'before': before, 'after': after, 'ops': ops, 'anchors': anchors,
                'warnings': warnings, 'records': records, 'currentHash': current['hash']}

    def state(self, scope=None, round_id=None):
        index = self._review_index()
        state = {**index, 'items': [], 'annotations': [], 'guides': []}
        entries = index['scopes']
        if round_id is not None:
            entries = [entry for entry in entries
                       if entry.get('scope', {}).get('source') == 'round'
                       and entry['scope'].get('round') == round_id]
        elif scope is not None:
            key = self._scope_key(scope)
            entries = [entry for entry in entries if entry['key'] == key]
        state['scopes'] = entries
        for entry in entries:
            path = self.data / entry['file']
            if not path.exists():
                continue
            shard = json.loads(path.read_text(encoding='utf-8'))
            for key in ('items', 'annotations', 'guides'):
                state[key].extend(shard.get(key, []))
        return state

    def _review_index(self):
        index_path = self.data / 'reviews.json'
        raw = json.loads(index_path.read_text(encoding='utf-8')) if index_path.exists() else {}
        if raw.get('storageSchema') != 1:
            raw = self._migrate_review_storage(raw)
        return {'version': 2, 'storageSchema': 1, 'revision': raw.get('revision', 0),
                'nextId': raw.get('nextId', 1), 'nextAnnotationId': raw.get('nextAnnotationId', 1),
                'scopes': raw.get('scopes', [])}

    def _migrate_review_storage(self, raw):
        """Split development-era review content into one shard per comparison scope."""
        groups = {}
        for kind in ('items', 'annotations', 'guides'):
            for entry in raw.get(kind, []):
                scope = entry.get('scope') or {}
                key = digest(scope_identity(scope))[:24]
                group = groups.setdefault(key, {'key': key, 'scope': scope,
                                                'file': f'reviews/{key}.json',
                                                'items': [], 'annotations': [], 'guides': []})
                clean = dict(entry)
                clean.pop('history', None)
                group[kind].append(clean)
        scopes = []
        for group in groups.values():
            shard = {key: group[key] for key in ('scope', 'items', 'annotations', 'guides')}
            atomic_json(self.data / group['file'], shard)
            scopes.append({'key': group['key'], 'scope': group['scope'], 'file': group['file'],
                           'itemCount': len(group['items']), 'annotationCount': len(group['annotations'])})
        index = {'version': 2, 'storageSchema': 1, 'revision': raw.get('revision', 0),
                 'nextId': raw.get('nextId', 1), 'nextAnnotationId': raw.get('nextAnnotationId', 1),
                 'scopes': scopes}
        atomic_json(self.data / 'reviews.json', index)
        return index

    def _scope_key(self, scope):
        return digest(scope_identity(scope))[:24]

    def review_scope_for_key(self, key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{24}', key):
            raise ValueError('审查范围编号无效')
        entry = next((s for s in self._review_index()['scopes'] if s.get('key') == key), None)
        if entry is None:
            raise ValueError('审查范围不存在')
        return entry['scope']

    def _save_scope(self, state, scope):
        index_path = self.data / 'reviews.json'
        index = json.loads(index_path.read_text(encoding='utf-8'))
        key = self._scope_key(scope)
        entry = next((s for s in index['scopes'] if s['key'] == key), None)
        if entry is None:
            entry = {'key': key, 'scope': scope, 'file': f'reviews/{key}.json'}
            index['scopes'].append(entry)
        entry.update(scope=scope, itemCount=len(state['items']),
                     annotationCount=len(state['annotations']))
        shard = {'scope': scope, 'items': state['items'],
                 'annotations': state['annotations'], 'guides': state['guides']}
        atomic_json(self.data / entry['file'], shard)
        index.update(revision=state['revision'], nextId=state['nextId'],
                     nextAnnotationId=state['nextAnnotationId'])
        atomic_json(index_path, index)

    def save_guide(self, revision, query, groups):
        with self.writing():
            state = self.state(scope=query)
            if revision != state['revision']:
                raise ValueError('审查数据已更新，请重新读取后整理导览')
            changes = self.changes(query)
            allowed = {r['path'] for r in changes['records']}
            if not isinstance(groups, list) or len(groups) > 200:
                raise ValueError('功能分组数量无效')
            keys, prepared = set(), []
            for group in groups:
                key = str(group.get('key', '')).strip()
                if not key or key in keys or not str(group.get('title', '')).strip():
                    raise ValueError('功能分组必须有唯一 key 和标题')
                keys.add(key)
                refs = [{'path': self.relative(r['path'])} for r in group.get('refs', [])]
                if not refs or any(r['path'] not in allowed for r in refs):
                    raise ValueError('功能分组必须关联当前比较范围中的文件')
                prepared.append({'key': key, 'title': str(group['title'])[:200],
                                 'purpose': str(group.get('purpose', '')), 'validation': str(group.get('validation', '未记录验证')),
                                 'refs': refs, 'scope': query, 'updated': now()})
            state['guides'] = [g for g in state['guides'] if scope_identity(g['scope']) != scope_identity(query)] + prepared
            state['revision'] += 1
            self._save_scope(state, query)
            return self.review_view(locked=True, scope=query)

    def _merge_annotation(self, state, payload, history_records):
        text = str(payload.get('text', '')).strip()
        if len(text) > 100000:
            raise ValueError('批注过长')
        target = payload.get('target', {})
        if target.get('kind') not in ('feature', 'directory', 'file', 'symbol', 'line', 'general'):
            raise ValueError('批注对象无效')
        if not isinstance(target.get('label'), str) or not target['label'].strip():
            raise ValueError('批注对象缺少名称')
        refs = payload.get('refs', [])
        if len(refs) > 1000:
            raise ValueError('批注关联文件过多')
        for ref in refs:
            ref['path'] = self.relative(ref['path'])
            for key in ('line', 'end'):
                if key in ref and (not isinstance(ref[key], int) or ref[key] < 1):
                    raise ValueError('批注行号无效')
        query = payload['scope']
        key = digest([query, target])
        note = next((n for n in state['annotations'] if n['id'] == payload.get('id')), None)
        if payload.get('id') is not None and note is None:
            raise ValueError('批注不存在')
        if note is not None and note['targetKey'] != key:
            raise ValueError('已有批注不能静默更换对象')
        if note is None:
            note = next((n for n in state['annotations'] if n['targetKey'] == key), None)
        if note is None:
            if not text: return
            note = {'id': state['nextAnnotationId'], 'created': now()}
            state['nextAnnotationId'] += 1
            state['annotations'].append(note)
        prior = {k: note.get(k) for k in ('text', 'updated', 'contextHashes', 'status', 'target', 'refs', 'scope')}
        # Editing text never makes an old location current again.
        hashes = dict(note.get('contextHashes', {}))
        for ref in refs:
            if ref['path'] not in hashes:
                hashes[ref['path']] = payload.get('seenHashes', {}).get(ref['path'], self.read_file(ref['path'])['hash'])
        if target['kind'] == 'general' and 'contextDiff' not in note:
            note['contextDiff'] = self.changes(query)['fingerprint']
        note.update(text=text, target=target, targetKey=key, refs=refs, scope=query,
                    contextHashes=hashes, updated=now(), status='open' if text else 'withdrawn')
        history_records.append({'kind': 'annotations', 'id': note['id'],
                                'entry': {'time': now(), 'previous': prior}})
        note['historyCount'] = note.get('historyCount', 0) + 1
        note['historyFile'] = f"history/annotations/{note['id']}.jsonl"

    def select(self, selection):
        value = {'path': self.relative(selection['path']) if selection.get('path') else None,
                 'itemId': selection.get('itemId'), 'itemTitle': str(selection.get('itemTitle', ''))[:200],
                 'annotationId': selection.get('annotationId'), 'target': selection.get('target'),
                 'scope': selection.get('scope', {}), 'updated': now()}
        with self.writing():
            atomic_json(self.data / 'selection.json', value)
        return value

    def context(self, item_id=None, history_limit=20, scope=None, round_id=None):
        path = self.data / 'selection.json'
        selection = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        state = self.review_view(round_id=round_id, scope=scope or selection.get('scope') or {})
        item = next((i for i in state['items'] if i['id'] == (item_id or selection.get('itemId'))), None)
        file = selection.get('path') if item_id is None else None
        if not file and item and item.get('refs'):
            file = item['refs'][0]['path']
        note = next((n for n in state['annotations'] if n['id'] == selection.get('annotationId')), None)
        try:
            history_limit = max(1, min(50, int(history_limit or 20)))
        except (TypeError, ValueError):
            raise ValueError('historyLimit 必须在 1 到 50 之间')
        selected_history = (
            self.history('items', item['id'], history_limit) if item else
            self.history('annotations', note['id'], history_limit) if note else []
        )
        return {'selection': selection, 'item': item, 'annotation': note,
                'history': selected_history,
                'nativeFile': str(self.root / self.relative(file)) if file else None,
                'instruction': '若需查看代码，使用 Codex open_in_codex，target={type:file,path:nativeFile}，placement=right。审查清单由 review_open 的 MCP Apps 界面展示；仅在明确需要网页回退时打开浏览器。只读查看，不将自检冒充用户意见。'}

    def item_fingerprint(self, item, require_records=False, result=None):
        result = result if result is not None else self.changes(item['scope'])
        refs = item.get('refs', [])
        if not refs:
            return None
        selected = []
        for ref in refs:
            path = self.relative(ref['path'])
            records = [r for r in result['records'] if r['path'] == path and (not ref.get('record') or r['id'] == ref['record'])]
            if require_records and ref.get('record') and not records:
                raise ValueError('关联修改块已不在该比较范围，请刷新后重新关联')
            selected.append([ref, records, self.read_file(path)['hash']])
        return digest(selected)

    def review_view(self, locked=False, round_id=None, scope=None):
        state = self.state(scope=scope, round_id=round_id)
        target_round = None
        if round_id is not None:
            target_round = self.round(round_id)
            if target_round.get('status') != 'completed':
                raise ValueError('目标审查轮尚未完成')
        scopes = {}
        for item in state['items']:
            try:
                key = digest(item['scope'])
                if key not in scopes:
                    scopes[key] = self.changes(item['scope'])
                current = self.item_fingerprint(item, result=scopes[key])
                item['stale'] = item['status'] == 'approved' and current != item.get('approvedFingerprint')
                if item['status'] == 'approved' and item['scope'].get('source') == 'round':
                    paths = {r['path'] for r in item['refs']}
                    item['stale'] = item['stale'] or any(r['currentHash'] != r['afterHash'] for r in scopes[key]['records'] if r['path'] in paths)
                item['error'] = ''
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                item['stale'] = item['status'] == 'approved'
                item['error'] = str(e)
        for note in state['annotations']:
            try:
                note['stale'] = any(self.read_file(p)['hash'] != h for p, h in note['contextHashes'].items())
                if note.get('contextDiff'):
                    key = digest(note['scope'])
                    if key not in scopes:
                        scopes[key] = self.changes(note['scope'])
                    note['stale'] = note['stale'] or scopes[key]['fingerprint'] != note['contextDiff']
                note['error'] = ''
            except (OSError, ValueError) as e:
                note['stale'], note['error'] = True, str(e)
        workflow = self.workflow(locked=locked)
        # A scoped panel must never inherit a plan from another comparison
        # scope. Round panels also expose their immutable target metadata.
        if target_round or (scope and scope.get('source')):
            item_ids = {i['id'] for i in state['items']}
            annotation_ids = {n['id'] for n in state['annotations']}
            plan_items = set(workflow.get('itemIds', []))
            plan_annotations = set(workflow.get('annotationIds', []))
            plan_ids = plan_items | plan_annotations
            if not plan_ids or not plan_items.issubset(item_ids) or not plan_annotations.issubset(annotation_ids):
                workflow = {'status': 'feedback_ready' if item_ids or annotation_ids else 'no_feedback',
                            'mayApply': False,
                            'instruction': '读取意见后先给出具体修改方案，等待用户明确确认。'}
            state = {'revision': state['revision'], 'items': state['items'],
                     'annotations': state['annotations'], 'guides': state['guides'],
                     'workflow': workflow}
            if target_round:
                state['targetRound'] = self._round_summary(target_round)
        else:
            state['workflow'] = workflow
        state['instructions'] = '审查意见不是改代码授权。先提出修改方案，等用户在聊天中明确确认后再修改。'
        return state

    def _save_workflow(self, plan):
        plan.pop('mayApply', None)
        atomic_json(self.data / 'workflow.json', plan)
        labels = {'awaiting_confirmation': '等待用户确认；尚未授权修改代码',
                  'confirmed': '用户已确认方案', 'applying': '正在执行已确认方案',
                  'completed': '本轮修改已完成', 'invalidated': '方案已过期，需重新提出并确认'}
        lines = [f"# 修改方案 {plan['id']}", '', '状态：' + labels[plan['status']],
                 f"审查版本：{plan['reviewRevision']}", '涉及审查编号：' + ', '.join(map(str, plan['itemIds'])),
                 '涉及意见编号：' + ', '.join('R' + str(i) for i in plan.get('annotationIds', [])),
                 '', plan['text'], '', '审查勾选不是改代码授权。以 workflow.json / review_results 的当前状态为准。']
        for key, label in [('reason', '失效原因'), ('userConfirmation', '用户确认原话（助手记录）'),
                           ('summary', '实际结果'), ('validation', '实际验证')]:
            if plan.get(key):
                lines += ['', label + '：' + plan[key]]
        path = self.data / 'plan.md'
        tmp = path.with_suffix('.tmp')
        tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(tmp, path)

    def workflow(self, locked=False):
        if not locked:
            with self.writing():
                return self.workflow(locked=True)
        path = self.data / 'workflow.json'
        if not path.exists():
            index = self._review_index()
            has_feedback = any(s.get('itemCount', 0) or s.get('annotationCount', 0) for s in index['scopes'])
            return {'status': 'feedback_ready' if has_feedback else 'no_feedback', 'mayApply': False,
                    'instruction': '读取意见后先给出修改方案，等待用户明确确认。'}
        plan = json.loads(path.read_text(encoding='utf-8'))
        if plan['status'] not in ('completed', 'invalidated'):
            reason = ''
            if plan['reviewRevision'] != self._review_index()['revision']:
                reason = '审查意见已更新，需要重新提出并确认方案。'
            elif plan['status'] != 'applying' and any(self.read_file(p)['hash'] != h for p, h in plan['codeHashes'].items()):
                reason = '方案涉及的代码已变化，需要重新提出并确认方案。'
            if reason:
                plan['status'] = 'invalidated'
                plan['reason'] = reason
                self._save_workflow(plan)
        plan['mayApply'] = plan['status'] in ('confirmed', 'applying')
        return plan

    def propose_plan(self, review_revision, item_ids, text, annotation_ids=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('修改方案不能为空')
        with self.writing():
            state = self.state()
            if review_revision != state['revision']:
                raise ValueError('审查意见已更新，请重新读取后提出方案')
            annotation_ids = annotation_ids or []
            items = [i for i in state['items'] if i['id'] in item_ids]
            notes = [n for n in state['annotations'] if n['id'] in annotation_ids and n['status'] != 'withdrawn']
            if (not items and not notes) or len(items) != len(set(item_ids)) or len(notes) != len(set(annotation_ids)):
                raise ValueError('请提供存在的审查项编号')
            files = sorted({r['path'] for i in items + notes for r in i['refs']})
            plan = {'id': digest([now(), text, review_revision])[:20], 'status': 'awaiting_confirmation',
                    'reviewRevision': review_revision, 'itemIds': sorted(set(item_ids)), 'annotationIds': sorted(set(annotation_ids)), 'text': text.strip(),
                    'codeHashes': {p: self.read_file(p)['hash'] for p in files}, 'proposedAt': now()}
            self._save_workflow(plan)
            return self.workflow(locked=True)

    def confirm_plan(self, plan_id, user_confirmation):
        if not isinstance(user_confirmation, str) or not user_confirmation.strip():
            raise ValueError('必须记录用户在方案之后明确确认的原话，不能使用审查勾选代替')
        with self.writing():
            plan = self.workflow(locked=True)
            if plan.get('id') != plan_id or plan['status'] != 'awaiting_confirmation':
                raise ValueError('方案不存在、已过期或不在等待确认状态')
            plan.update(status='confirmed', confirmedAt=now(), userConfirmation=user_confirmation.strip(),
                        confirmationSource='用户聊天确认原话，由助手记录')
            plan.pop('mayApply', None)
            self._save_workflow(plan)
            return self.workflow(locked=True)

    def begin_changes(self, plan_id):
        with self.writing():
            plan = self.workflow(locked=True)
            if plan.get('id') != plan_id or plan['status'] != 'confirmed':
                raise ValueError('未获得该方案的有效用户确认，不能开始修改')
            plan.update(status='applying', startedAt=now())
            plan.pop('mayApply', None)
            self._save_workflow(plan)
            return self.workflow(locked=True)

    def finish_changes(self, plan_id, summary, validation):
        with self.writing():
            plan = self.workflow(locked=True)
            if plan.get('id') != plan_id or plan['status'] != 'applying':
                raise ValueError('方案未开始执行或意见已更新，不能记为已完成')
            plan.update(status='completed', completedAt=now(), summary=str(summary), validation=str(validation))
            plan.pop('mayApply', None)
            self._save_workflow(plan)
            return self.workflow(locked=True)

    def save_item(self, payload):
        if 'scope' not in payload and payload.get('id') is not None:
            existing = next((item for item in self.state()['items'] if item['id'] == payload['id']), None)
            if existing is None:
                raise ValueError('审查项不存在')
            payload = {**payload, 'scope': existing['scope']}
        return self.save_batch({'revision': payload.get('revision'), 'items': [payload]})

    def save_batch(self, payload):
        with self.writing():
            items = payload.get('items')
            annotations = payload.get('annotations', [])
            if not isinstance(items, list) or len(items) > 1000:
                raise ValueError('审查项数量无效')
            if not isinstance(annotations, list) or len(annotations) > 200:
                raise ValueError('批注数量无效')
            entries = items + annotations
            if not entries or any(not isinstance(entry.get('scope'), dict) for entry in entries):
                raise ValueError('每次保存必须属于一个明确的比较范围')
            scope = entries[0]['scope']
            if any(scope_identity(entry['scope']) != scope_identity(scope) for entry in entries):
                raise ValueError('一次保存不能混合多个审查范围')
            state = self.state(scope=scope)
            if payload.get('revision') != state['revision']:
                raise ValueError('审查数据已被其他窗口或助手修改，请刷新后重试（本次未覆盖）')
            if not (items or annotations):
                raise ValueError('审查项数量无效')
            scopes, history_records = {}, []
            for entry in items:
                self._merge_item(state, entry, scopes, history_records)
            for entry in annotations:
                self._merge_annotation(state, entry, history_records)
            state['revision'] += 1
            self._append_history(history_records)
            self._save_scope(state, scope)
            return self.review_view(locked=True, scope=scope)

    def _merge_item(self, state, payload, scopes, history_records):
        item = next((i for i in state['items'] if i['id'] == payload.get('id')), None)
        if item is None and payload.get('id') is not None:
            raise ValueError('审查项不存在')
        if item is None:
            item = {'id': state['nextId'], 'created': now()}
            state['nextId'] += 1
            state['items'].append(item)
        prior = {k: item.get(k) for k in ('title', 'purpose', 'validation', 'notes', 'status', 'refs', 'scope', 'approvedFingerprint')}
        for key in ('title', 'purpose', 'validation', 'notes', 'status', 'refs', 'scope', 'groupKey'):
            if key in payload:
                item[key] = payload[key]
        if item.get('status') not in ('pending', 'approved', 'changes'):
            raise ValueError('状态无效')
        if not isinstance(item.get('title'), str) or not item['title'].strip():
            raise ValueError('请填写审查项标题')
        if len(item.get('refs', [])) > 500:
            raise ValueError('关联位置过多')
        for ref in item.get('refs', []):
            ref['path'] = self.relative(ref['path'])
            if ref.get('line') and (not isinstance(ref['line'], int) or ref['line'] < 1):
                raise ValueError('行号必须是正整数')
        if item['status'] == 'approved':
            if not item.get('refs'):
                raise ValueError('至少关联一个文件或修改块后才能通过')
            old_binding = [prior.get('refs'), prior.get('scope')]
            new_binding = [item.get('refs'), item.get('scope')]
            if payload.get('confirmApproval'):
                seen = payload.get('seenHashes', {})
                for ref in item['refs']:
                    if seen.get(ref['path']) != self.read_file(ref['path'])['hash']:
                        raise ValueError('代码已变化，请刷新修改列表并重新查看后再通过')
                key = digest(item['scope'])
                if key not in scopes:
                    scopes[key] = self.changes(item['scope'])
                if item['scope'].get('source') == 'round':
                    paths = {r['path'] for r in item['refs']}
                    if any(r['currentHash'] != r['afterHash'] for r in scopes[key]['records'] if r['path'] in paths):
                        raise ValueError('工作区已不同于本轮结束快照，请审查最新修改轮次，不能用旧端点通过新代码')
                if payload.get('seenDiff') != scopes[key]['fingerprint']:
                    raise ValueError('比较内容已变化，请刷新修改列表后再通过')
                item['approvedFingerprint'] = self.item_fingerprint(item, require_records=True, result=scopes[key])
                item['approvedAt'] = now()
            elif prior.get('status') != 'approved' or old_binding != new_binding:
                raise ValueError('通过或变更通过范围需要显式重新确认')
        item['updated'] = now()
        history_records.append({'kind': 'items', 'id': item['id'],
                                'entry': {'time': now(), 'previous': prior}})
        item['historyCount'] = item.get('historyCount', 0) + 1
        item['historyFile'] = f"history/items/{item['id']}.jsonl"

    def export(self):
        state = self.review_view()
        labels = {'pending': '待审', 'approved': '通过', 'changes': '需修改'}
        lines = ['# 本地代码审查结果', f'项目：{self.root}', f'导出：{now()}',
                 '编号为本地持久编号；过期的通过结论不能作为当前修改已通过的依据。',
                 '助手收到意见后必须先给出具体修改方案，等用户在聊天中明确确认该方案后再改代码。审查勾选不是修改授权。',
                 '方案状态：' + state['workflow']['status'], '']
        for n in state['annotations']:
            if n['status'] == 'withdrawn': continue
            lines += [f"## 意见 R{n['id']}：{n['target']['label']}",
                      '对象：' + n['target']['kind'], '意见：' + n['text'],
                      '定位状态：' + ('代码已变化，请结合当前上下文复核' if n['stale'] else '原始上下文未变'),
                      '比较范围：' + json.dumps(n['scope'], ensure_ascii=False)]
            for ref in n['refs']:
                lines.append('- ' + ref['path'] + (':' + str(ref['line']) if ref.get('line') else ''))
            lines.append('')
        for guide in state['guides']:
            lines += ['## 助手导览：' + guide['title'], '说明（不代表用户通过）：' + guide.get('purpose', ''),
                      '验证：' + guide.get('validation', '未记录验证'),
                      '关联文件：' + ', '.join(r['path'] for r in guide['refs']), '']
        for i in state['items']:
            status = '通过（已过期，需复审）' if i['stale'] else labels[i['status']]
            heading = f"## 文件：{i['title']} [{status}]" if i.get('groupKey', '').startswith('file-receipt:') else f"## 第 {i['id']} 项：{i['title']} [{status}]"
            lines += [heading, f"目的：{i.get('purpose', '')}",
                      f"验证：{i.get('validation', '')}", f"意见：{i.get('notes', '')}",
                      '比较范围：' + json.dumps(i['scope'], ensure_ascii=False),
                      f"通过时指纹：{i.get('approvedFingerprint') or '无'}"]
            for ref in i.get('refs', []):
                lines.append(f"- {ref['path']}" + (f":{ref['line']}" if ref.get('line') else '') + (f" · 修改块 {ref['record']}" if ref.get('record') else ' · 整个文件'))
            if i.get('error'):
                lines.append('刷新错误：' + i['error'])
            lines.append('')
        content = '\n'.join(lines)
        with self.writing():
            path = self.data / 'review-export.md'
            tmp = path.with_suffix('.tmp')
            tmp.write_text(content, encoding='utf-8')
            os.replace(tmp, path)
        return content
