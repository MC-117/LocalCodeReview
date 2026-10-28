"""Conservative, read-only decoding of literal apply_patch arguments in JS logs.

This is intentionally not a JavaScript interpreter. Expressions, interpolation,
mutable variables and nested scopes are left for snapshot-based review.
"""
import re


def _string(source, start):
    quote, pos, result = source[start], start + 1, []
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}
    while pos < len(source):
        char = source[pos]
        pos += 1
        if char == quote:
            value = ''.join(result).encode('utf-16-le', 'surrogatepass').decode('utf-16-le')
            return value, pos
        if (quote != '`' and char in '\r\n') or (quote == '`' and char == '$' and source[pos:pos + 1] == '{'):
            raise ValueError('Nonliteral JavaScript string')
        if char != '\\':
            result.append(char)
            continue
        if pos >= len(source):
            break
        char = source[pos]
        pos += 1
        if char in '\r\n':
            if char == '\r' and source[pos:pos + 1] == '\n':
                pos += 1
            continue
        if char in 'xu':
            size = 2 if char == 'x' else 4
            digits = source[pos:pos + size]
            if len(digits) != size or not re.fullmatch('[a-fA-F0-9]+', digits):
                raise ValueError('Unsupported JavaScript escape')
            result.append(chr(int(digits, 16)))
            pos += size
        elif char.isdigit() and (char != '0' or source[pos:pos + 1].isdigit()):
            raise ValueError('Unsupported JavaScript octal escape')
        else:
            result.append(escapes.get(char, char))
    raise ValueError('Unterminated JavaScript string')


def _tokens(source):
    tokens, pos = [], 0
    while pos < len(source):
        char = source[pos]
        if char.isspace():
            pos += 1
        elif source.startswith('//', pos):
            end = source.find('\n', pos + 2)
            pos = len(source) if end < 0 else end + 1
        elif source.startswith('/*', pos):
            end = source.find('*/', pos + 2)
            if end < 0:
                raise ValueError('Unterminated JavaScript comment')
            pos = end + 2
        elif char in '\"\'`':
            value, pos = _string(source, pos)
            tokens.append(('string', value))
        elif char == '/':
            # Division and regexes are outside this small grammar. Never scan
            # regex bodies as tool calls or evaluate arbitrary JS to resolve them.
            raise ValueError('Unsupported JavaScript expression')
        elif char.isalpha() or char in '_$':
            end = pos + 1
            while end < len(source) and (source[end].isalnum() or source[end] in '_$'):
                end += 1
            tokens.append(('name', source[pos:end]))
            pos = end
        else:
            tokens.append(('symbol', char))
            pos += 1
    return tokens


def patch_literals(source):
    """Decode direct literals and top-level const-string bindings, without eval."""
    try:
        tokens = _tokens(source)
    except (ValueError, UnicodeError):
        return
    constants, declared = {}, set()
    braces = parens = brackets = 0
    for index, token in enumerate(tokens):
        kind, value = token
        # Only standalone top-level const NAME = STRING; declarations qualify.
        if token == ('name', 'const') and not (braces or parens or brackets):
            row = tokens[index:index + 5]
            if (index == 0 or tokens[index - 1] == ('symbol', ';')) and len(row) == 5:
                _, name, equals, literal, end = row
                if name[0] == 'name':
                    identifier = name[1]
                    if identifier in declared:
                        constants.pop(identifier, None)
                    elif equals == ('symbol', '=') and literal[0] == 'string' and end == ('symbol', ';'):
                        constants[identifier] = literal[1]
                    declared.add(identifier)
        if kind == 'name' and value in constants and index > 0:
            following = tokens[index + 1:index + 3]
            if following and (following[0] == ('symbol', '=') or following[:2] in
                              [[('symbol', '+'), ('symbol', '+')], [('symbol', '-'), ('symbol', '-')]]):
                # Reject reassignment even in invalid JS rather than guess it ran.
                if tokens[index - 1] != ('name', 'const'):
                    constants.pop(value, None)
        if token == ('name', 'apply_patch') and not braces:
            previous = tokens[max(0, index - 2):index]
            qualified = previous == [('name', 'tools'), ('symbol', '.')]
            bare = index == 0 or tokens[index - 1][1] != '.'
            row = tokens[index + 1:index + 4]
            if (qualified or bare) and len(row) == 3 and row[0] == ('symbol', '(') and row[2] == ('symbol', ')'):
                argument = row[1]
                if argument[0] == 'string':
                    yield argument[1]
                elif argument[0] == 'name' and argument[1] in constants:
                    yield constants[argument[1]]
        if kind == 'symbol':
            braces += (value == '{') - (value == '}')
            parens += (value == '(') - (value == ')')
            brackets += (value == '[') - (value == ']')
            if min(braces, parens, brackets) < 0:
                return
