import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(BASE))
from review_core import ReviewStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / '项目 中文'
        self.root.mkdir()
        (self.root / 'main.py').write_text('value = 1\n', encoding='utf-8')
        self.store = ReviewStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_current_index_excludes_history_and_context_loads_item_history(self):
        state = self.store.save_batch({
            'revision': 0,
            'items': [{'title': '初次审查', 'status': 'pending', 'refs': [], 'scope': {}}]
        })
        state = self.store.save_item({
            'revision': state['revision'], 'id': 1, 'title': '更新后的审查',
            'status': 'changes', 'refs': [], 'scope': {}, 'notes': '需要调整'
        })
        raw = json.loads((self.store.data / 'reviews.json').read_text(encoding='utf-8'))
        self.assertNotIn('history', raw)
        self.assertNotIn('history', raw['items'][0])
        history_path = self.store.data / 'history' / 'items' / '1.jsonl'
        self.assertTrue(history_path.exists())
        self.assertGreaterEqual(len(history_path.read_text(encoding='utf-8').splitlines()), 2)
        self.assertNotIn('history', state['items'][0])
        self.assertNotIn('history', self.store.review_view()['items'][0])
        self.assertEqual(len(self.store.context(item_id=1)['history']), 2)
        self.assertEqual(len(self.store.history('items', 1, limit=1)), 1)

    def test_annotation_history_is_separate_and_selected_context_is_explicit(self):
        state = self.store.save_batch({'revision': 0, 'items': [], 'annotations': [{
            'text': '保留接口', 'target': {'kind': 'file', 'label': 'main.py'},
            'refs': [], 'scope': {}
        }]})
        state = self.store.save_batch({'revision': state['revision'], 'items': [], 'annotations': [{
            'id': 1, 'text': '重新确认接口', 'target': {'kind': 'file', 'label': 'main.py'},
            'refs': [], 'scope': {}
        }]})
        raw = json.loads((self.store.data / 'reviews.json').read_text(encoding='utf-8'))
        self.assertNotIn('history', raw['annotations'][0])
        self.assertTrue((self.store.data / 'history' / 'annotations' / '1.jsonl').exists())
        self.assertNotIn('history', self.store.review_view()['annotations'][0])
        self.store.select({'annotationId': 1})
        self.assertEqual(len(self.store.context()['history']), 2)

    def test_development_inline_history_is_split_on_first_open(self):
        data = self.root / '.code-review'
        legacy = {
            'version': 2, 'revision': 3, 'nextId': 2, 'nextAnnotationId': 2,
            'items': [{'id': 1, 'title': '旧项', 'status': 'pending', 'refs': [], 'scope': {},
                       'history': [{'time': 't1', 'previous': {'status': 'pending'}}]}],
            'annotations': [{'id': 1, 'text': '旧意见', 'target': {'kind': 'file', 'label': 'x'},
                             'targetKey': 'k', 'refs': [], 'scope': {}, 'status': 'open',
                             'history': [{'time': 't2', 'previous': {'text': '旧意见'}}]}]
        }
        (data / 'reviews.json').write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
        migrated = ReviewStore(self.root)
        current = json.loads((data / 'reviews.json').read_text(encoding='utf-8'))
        self.assertEqual(current['historySchema'], 1)
        self.assertNotIn('history', current['items'][0])
        self.assertNotIn('history', current['annotations'][0])
        self.assertEqual(len(migrated.history('items', 1)), 1)
        self.assertEqual(len(migrated.history('annotations', 1)), 1)

    def test_round_index_has_summaries_and_detail_is_separate(self):
        # Round behavior is exercised by test_rounds; this checks the storage split directly.
        rounds = self.store.data / 'rounds'
        rounds.mkdir(parents=True)
        detail = {'id': 'a' * 20, 'threadId': 'chat', 'turnId': 'turn', 'startedAt': 't',
                  'status': 'completed', 'coverage': 'long explanation', 'before': 'b', 'after': 'a'}
        (rounds / 'index.json').write_text(json.dumps([{
            'id': detail['id'], 'threadId': 'chat', 'turnId': 'turn', 'startedAt': 't',
            'status': 'completed', 'before': 'b', 'after': 'a'
        }]), encoding='utf-8')
        (rounds / (detail['id'] + '.json')).write_text(json.dumps(detail), encoding='utf-8')
        self.assertNotIn('coverage', self.store.rounds()[0])
        self.assertEqual(self.store.round(detail['id'])['coverage'], 'long explanation')


if __name__ == '__main__':
    unittest.main()
