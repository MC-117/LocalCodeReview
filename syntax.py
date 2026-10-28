"""Offline syntax highlighting with the bundled BSD-licensed Pygments."""
from functools import lru_cache
from html import escape
from pathlib import Path
from fnmatch import fnmatchcase
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / '_vendor'))
from pygments import lex
from pygments.lexers import get_lexer_by_name, get_all_lexers
from pygments.token import Comment, Keyword, Name, String, Number, Operator

LEXERS = tuple(get_all_lexers(plugins=False))
# Overrides only resolve common ambiguous extensions; all other filenames use the library registry.
ALIASES = {'.h': 'cpp', '.ts': 'typescript', '.tsx': 'tsx', '.jsx': 'jsx', '.m': 'objective-c'}


@lru_cache(maxsize=2048)
def supported_filename(path):
    name = Path(path).name.lower()
    return any(fnmatchcase(name, pattern.lower()) for _, _, patterns, _ in LEXERS for pattern in patterns)


@lru_cache(maxsize=64)
def language_for(path, sample):
    override = ALIASES.get(Path(path).suffix.lower())
    if override:
        return override
    name = Path(path).name.lower()
    candidates = []
    for _, aliases, patterns, _ in LEXERS:
        matches = [p for p in patterns if fnmatchcase(name, p.lower())]
        if aliases and matches:
            lexer = get_lexer_by_name(aliases[0])
            candidates.append((lexer.analyse_text(sample), lexer.priority,
                               max(len(p.replace('*', '')) for p in matches), aliases[0]))
    if candidates:
        return max(candidates)[-1]
    first = sample.split('\n', 1)[0]
    if first.startswith('#!'):
        for expression, alias in [(r'python', 'python'), (r'node|deno', 'javascript'),
                                  (r'ruby', 'ruby'), (r'perl', 'perl'), (r'(ba|z|k)?sh\b', 'bash')]:
            if re.search(expression, first): return alias
    return 'text'


def token_class(token):
    if token in Comment.Preproc: return 'directive'
    if token in Comment: return 'comment'
    if token in String: return 'string'
    if token in Number: return 'number'
    if token in Keyword.Type or token in Name.Class or token in Name.Exception: return 'type'
    if token in Keyword: return 'keyword'
    if token in Name.Function: return 'function'
    if token in Name.Builtin: return 'builtin'
    if token in Name.Decorator or token in Name.Attribute or token in Name.Tag: return 'meta'
    if token in Name.Variable: return 'variable'
    if token in Operator: return 'operator'
    return ''


@lru_cache(maxsize=24)
def _highlight(alias, text):
    lexer = get_lexer_by_name(alias, stripnl=False, ensurenl=False, tabsize=0)
    lines = ['']
    tokens = list(lex(text, lexer))
    for position, (token, value) in enumerate(tokens):
        cls = token_class(token)
        if not cls and token in Name:
            following = position + 1
            while following < len(tokens) and not tokens[following][1].strip():
                following += 1
            if following < len(tokens) and tokens[following][1].startswith('('):
                cls = 'function'
        for i, part in enumerate(value.split('\n')):
            if i: lines.append('')
            if part:
                safe = escape(part, quote=True)
                symbol = ''
                if token in Name.Class or cls == 'function':
                    kind = 'class' if token in Name.Class else 'function'
                    symbol = f' data-symbol="{escape(value, quote=True)}" data-symbol-kind="{kind}"'
                lines[-1] += f'<span class="tok-{cls}"{symbol}>{safe}</span>' if cls else safe
    return tuple(lines)


def highlight_lines(path, lines):
    alias = language_for(path, '\n'.join(lines)[:8192])
    if not lines: return [], alias
    highlighted = _highlight(alias, '\n'.join(lines))
    # Keep exact source line identity, including an empty final line.
    if len(highlighted) != len(lines):
        return [escape(line, quote=True) for line in lines], 'text'
    return list(highlighted), alias
