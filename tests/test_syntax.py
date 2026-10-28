from html import unescape
import re
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syntax import ALIASES, LEXERS, highlight_lines, supported_filename


class SyntaxTests(unittest.TestCase):
    def test_multiline_cpp_tokens_keep_original_lines_and_escape_html(self):
        lines = ['/* comment starts', 'still comment */',
                 'class Sample {', 'const char* name = "<script>alert(1)</script>";',
                 'int value() { return 42; }', '};', '']
        html, language = highlight_lines('中文.cpp', lines)
        self.assertEqual(language, 'cpp')
        self.assertEqual(len(html), len(lines))
        self.assertTrue(all('tok-comment' in html[i] for i in (0, 1)))
        self.assertIn('tok-type', html[2])
        self.assertIn('tok-string', html[3])
        self.assertIn('tok-function', html[4])
        self.assertNotIn('<script>', ''.join(html))
        restored = [unescape(re.sub(r'</?span[^>]*>', '', s)) for s in html]
        self.assertEqual(restored, lines)

    def test_common_languages_and_unknown_fallback(self):
        for ext in ALIASES:
            with self.subTest(ext=ext):
                html, _ = highlight_lines('sample' + ext, ['x = 1', ''])
                self.assertEqual(len(html), 2)
        html, language = highlight_lines('custom.unknown', ['<b>literal</b>'])
        self.assertEqual(language, 'text')
        self.assertEqual(html, ['&lt;b&gt;literal&lt;/b&gt;'])

    def test_raw_string_state_spans_multiple_lines(self):
        html, _ = highlight_lines('sample.cpp', ['auto s = R"tag(first', 'second)tag";'])
        self.assertIn('tok-string', html[0])
        self.assertIn('tok-string', html[1])

    def test_const_cpp_member_function_is_colored(self):
        html, _ = highlight_lines('sample.cpp', ['class Item {', 'int value() const { return 1; }', '};'])
        self.assertIn('tok-function', html[1])

    def test_registry_covers_mainstream_languages_and_special_filenames(self):
        samples = {'main.rs': 'fn main() { let value = 42; }', 'main.go': 'package main\nfunc main() {}',
                   'Main.java': 'public class Main { int value = 1; }', 'main.kt': 'fun main() { val value = 42 }',
                   'main.swift': 'let value = 42', 'main.rb': 'class Item\nend', 'main.php': '<?php echo "hello";',
                   'main.dart': 'void main() { print("hello"); }', 'query.sql': 'SELECT id FROM items;',
                   'main.lua': 'local value = 42', 'main.scala': 'object Main { val value = 42 }',
                   'main.R': 'value <- 42', 'main.ps1': '$value = 42', 'Makefile': 'all:\n\techo hello',
                   'Dockerfile': 'FROM python:3.12\nRUN echo hello'}
        self.assertGreater(len(LEXERS), 500)
        for filename, text in samples.items():
            with self.subTest(filename=filename):
                html, language = highlight_lines(filename, text.split('\n'))
                self.assertNotEqual(language, 'text')
                self.assertTrue(supported_filename(filename))
                self.assertTrue(any('tok-' in line for line in html))
                self.assertEqual([unescape(re.sub(r'</?span[^>]*>', '', s)) for s in html], text.split('\n'))
        self.assertEqual(highlight_lines('script', ['#!/usr/bin/env python3','print(1)'])[1], 'python')


if __name__ == '__main__':
    unittest.main()
