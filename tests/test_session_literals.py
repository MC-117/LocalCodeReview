import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_core import ReviewStore, patch_literals


class SessionLiteralTests(unittest.TestCase):
    def test_const_binding_and_windows_chinese_path(self):
        patch = '*** Begin Patch\n*** Add File: C:\\项目 中文\\新增.py\n+value = 1\n*** End Patch'
        for quote in [json.dumps(patch, ensure_ascii=False), '`' + patch.replace('\\', '\\\\') + '`']:
            source = 'const patch = ' + quote + '; text(await tools.apply_patch(patch));'
            self.assertEqual(list(patch_literals(source)), [patch])

    def test_direct_literals_and_const_values_keep_execution_order(self):
        source = 'const a = "first"; const b = "second"; text(await tools.apply_patch(a)); await tools.apply_patch("inline"); text(await tools.apply_patch(b));'
        self.assertEqual(list(patch_literals(source)), ['first', 'inline', 'second'])
        self.assertEqual(list(patch_literals("tools.apply_patch('a\\x0ab')")), ['a\nb'])

    def test_source_strings_comments_and_unused_constants_are_not_tool_calls(self):
        source = 'const unused = "decoy"; const quoted = "tools.apply_patch(\\\"fake\\\")"; /* tools.apply_patch("comment") */ // tools.apply_patch("line")\n tools.apply_patch("real");'
        self.assertEqual(list(patch_literals(source)), ['real'])
        self.assertEqual(list(patch_literals('const unused = "text";')), [])

    def test_dynamic_mutable_shadowed_and_computed_values_fail_closed(self):
        unsupported = [
            'let patch = "old"; patch = getNew(); tools.apply_patch(patch);',
            'const patch = "literal" + other; tools.apply_patch(patch);',
            'const patch = getPatch(); tools.apply_patch(patch);',
            'const patch = `hello ${danger()}`; tools.apply_patch(patch);',
            'tools.apply_patch("literal" + other);',
            'const patch = "outer"; function f(patch) { tools.apply_patch(patch); }',
            'const patch = "outer"; { const patch = "inner"; tools.apply_patch(patch); }',
            'const patch = "old"; patch = "new"; tools.apply_patch(patch);',
            'const patch = "old"; const patch = "new"; tools.apply_patch(patch);',
            'const r = /tools.apply_patch("fake")/;',
            'const patch = "unterminated; tools.apply_patch(patch);',
        ]
        for source in unsupported:
            with self.subTest(source=source):
                self.assertEqual(list(patch_literals(source)), [])

    def test_five_files_across_cwd_and_turn_filter_without_executing_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, logs, other = base / '工具 中文', base / 'sessions', base / '引擎目录'
            for path in (root, logs, other):
                path.mkdir()
            paths = ['round_store.py', 'review_core.py', 'tests/test_storage.py', 'README.md', 'SKILL.md']
            events = [
                {'type': 'session_meta', 'payload': {'id': 'chat', 'cwd': str(other)}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'edited-turn'}},
            ]
            for index, path in enumerate(paths):
                patch = f'*** Begin Patch\n*** Add File: {root / path}\n+new text\n*** End Patch'
                source = 'const patch = ' + json.dumps(patch, ensure_ascii=False) + '; text(await tools.apply_patch(patch));'
                events.extend([
                    {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': str(index), 'input': source}},
                    {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': str(index), 'output': '{}'}},
                ])
            # A later empty test turn must not hide the preceding editing turn.
            events.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'test-turn'}})
            (logs / 'test.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events), encoding='utf-8')
            store = ReviewStore(root, sessions=logs)
            sid = store.catalog('chat')[0]['id']
            query = {'source': 'session', 'session': sid, 'threadId': 'chat', 'turnId': 'edited-turn'}
            changes = store.changes(query)
            self.assertEqual({r['path'] for r in changes['records']}, set(paths))
            self.assertTrue(all(not r['confirmed'] for r in changes['records']))
            self.assertEqual(store.changes({**query, 'attempts': False})['records'], [])
            self.assertEqual(store.changes({**query, 'turnId': 'test-turn'})['records'], [])
            self.assertFalse((root / 'README.md').exists())  # Parsing never applies a logged patch.


if __name__ == '__main__':
    unittest.main()
