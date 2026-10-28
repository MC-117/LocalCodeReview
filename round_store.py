"""Immutable review endpoints for one assistant execution turn, independent of Git."""
import difflib
import json
from pathlib import Path
import re


class RoundStore:
    @staticmethod
    def _round_summary(value):
        """Keep the list index cheap; endpoint metadata stays in one detail file."""
        fields = ('id', 'threadId', 'turnId', 'turnNumber', 'session', 'label',
                  'request', 'turnTime', 'startedAt', 'status', 'completedAt',
                  'changedFiles', 'before', 'after')
        return {key: value[key] for key in fields if key in value}

    def _round_index(self):
        path = self.data / 'rounds' / 'index.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def _save_round_index(self, values):
        from review_core import atomic_json
        atomic_json(self.data / 'rounds' / 'index.json', [self._round_summary(value) for value in values])

    def _round_detail(self, round_id):
        path = self.data / 'rounds' / (round_id + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def rounds(self, thread_id=None):
        values = self._round_index()
        return sorted([r for r in values if not thread_id or r['threadId'] == thread_id],
                      key=lambda r: r['startedAt'], reverse=True)

    def round(self, round_id):
        if not isinstance(round_id, str) or not re.fullmatch('[a-f0-9]{20}', round_id):
            raise ValueError('单轮审查编号无效')
        value = next((r for r in self.rounds() if r['id'] == round_id), None)
        if value is None:
            raise ValueError('单轮审查不存在')
        return self._round_detail(round_id) or value

    def find_turn(self, thread_id, turn_id=None):
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError('必须指定当前对话 threadId')
        matches = []
        for entry in self.catalog(thread_id):
            if entry.get('threadId') != thread_id:
                continue
            for turn in self.session(entry['id'], thread_id)['turns']:
                if turn.get('id') and (not turn_id or turn['id'] == turn_id):
                    matches.append({**turn, 'session': entry['id']})
        if not matches:
            raise ValueError('指定对话/轮次的日志尚不可用；不能猜测其他对话')
        return max(matches, key=lambda t: t.get('time', ''))

    def begin_round(self, thread_id, turn_id=None, label=None):
        from review_core import atomic_json, digest, now
        turn = self.find_turn(thread_id, turn_id)
        with self.writing():
            existing = next((r for r in self.rounds(thread_id) if r['turnId'] == turn['id']), None)
            if existing:
                return self.round(existing['id'])  # Retrying must not replace the original baseline.
            title = str(label or turn['label'] or '本轮修改')[:180]
            before = self._capture('轮前 · ' + title[:80])
            value = {'id': digest([thread_id, turn['id']])[:20], 'threadId': thread_id,
                     'turnId': turn['id'], 'turnNumber': turn['number'], 'session': turn['session'],
                     'label': title, 'request': turn['label'], 'turnTime': turn['time'],
                     'startedAt': now(), 'status': 'recording', 'before': before['id'],
                     'after': None, 'changedFiles': None,
                     'coverage': '助手记录的修改前后文本快照；仅覆盖快照规则内文件，不是文件系统事务。'}
            atomic_json(self.data / 'rounds' / (value['id'] + '.json'), value)
            self._save_round_index(self.rounds() + [value])
            return value

    def finish_round(self, round_id):
        from review_core import atomic_json, now
        with self.writing():
            value = self.round(round_id)
            if value['status'] == 'completed':
                return value  # Immutable after endpoint, including after a retry/restart.
            after = self._capture('轮后 · ' + value['label'][:80])
            value.update(after=after['id'], status='completed', completedAt=now())
            result = self.round_changes({'source': 'round', 'round': round_id}, value=value)
            value['changedFiles'] = len(result['records'])
            atomic_json(self.data / 'rounds' / (round_id + '.json'), value)
            values = [value if r['id'] == round_id else r for r in self.rounds()]
            self._save_round_index(values)
            return value

    def round_content(self, manifest, file):
        file = self.relative(file)
        if file in manifest.get('unavailable', {}):
            return manifest['unavailable'][file]
        if file in manifest.get('skipped', []):
            return {'exists': True, 'hash': 'unavailable', 'text': None, 'note': '快照未保存此文件原文'}
        h = manifest['files'].get(file)
        if not h:
            return {'exists': False, 'hash': 'missing', 'text': '', 'note': ''}
        return json.loads((self.data / 'blobs' / (h + '.json')).read_text(encoding='utf-8'))

    def round_endpoints(self, query, value=None):
        value = value or self.round(query.get('round'))
        if value['status'] != 'completed' or not value.get('after'):
            raise ValueError('本轮尚未记录结束快照，不能展示完整单轮对比')
        if query.get('threadId') and query['threadId'] != value['threadId']:
            raise ValueError('单轮审查不属于指定对话')
        return value, self.snapshot(value['before']), self.snapshot(value['after'])

    def round_changes(self, query, value=None):
        from review_core import digest, unified
        value, before, after = self.round_endpoints(query, value)
        paths = set(before['files']) | set(after['files']) | set(before['skipped']) | set(after['skipped'])
        records = []
        for file in sorted(paths):
            if not self.allowed(file, query.get('include', ''), query.get('exclude', ''), query.get('defaults', True)):
                continue
            old, new = self.round_content(before, file), self.round_content(after, file)
            if old['hash'] == new['hash'] and old['hash'] != 'unavailable':
                continue
            patch = unified(old['text'], new['text'], file) if old['text'] is not None and new['text'] is not None else '快照略过此文件原文，仅提供摘要；不能推断完整代码差异。'
            records.append({'id': digest([value['id'], file])[:24], 'path': file,
                            'action': 'Delete' if not new['exists'] else 'Update' if old['exists'] else 'Add',
                            'patch': patch, 'confirmed': True, 'turn': value['turnNumber'],
                            'turnId': value['turnId'], 'time': value['completedAt'],
                            'beforeHash': old['hash'], 'afterHash': new['hash'],
                            'currentHash': self.read_file(file)['hash']})
        return {'records': records, 'fingerprint': digest(records), 'query': query, 'round': value,
                'warnings': ['固定轮前 → 轮后快照；后续工作区修改不会混入两侧代码。', value['coverage'],
                             f"轮前略过 {len(before['skipped'])} 文件；轮后略过 {len(after['skipped'])} 文件。"]}

    def round_compare(self, query, file):
        from syntax import highlight_lines
        value, before, after = self.round_endpoints(query)
        file = self.relative(file)
        sides = []
        for manifest, label in [(before, '本轮修改前'), (after, '本轮修改后')]:
            content = self.round_content(manifest, file)
            lines = (content['text'] or '').splitlines()
            html, language = highlight_lines(file, lines) if content['text'] is not None else ([], 'text')
            sides.append({'lines': lines, 'htmlLines': html, 'language': language,
                          'exists': content['exists'], 'available': content['text'] is not None,
                          'hash': content['hash'], 'label': label + ' · ' + manifest['time'], 'note': content['note']})
        old, new = sides
        ops = [list(op) for op in difflib.SequenceMatcher(None, old['lines'], new['lines']).get_opcodes()] if old['available'] and new['available'] else []
        current_hash = self.read_file(file)['hash']
        warnings = ['两侧为固定历史快照；“原生文件”打开的是工作区当前文件。']
        if current_hash != new['hash']:
            warnings.append('工作区已不同于本轮结束快照；此处仅显示历史轮次，旧通过结论不能用于当前代码。')
        return {'path': file, 'before': old, 'after': new, 'ops': ops, 'anchors': [], 'records': [],
                'warnings': warnings, 'currentHash': current_hash, 'round': value}
