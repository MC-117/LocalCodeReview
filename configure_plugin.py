"""Generate the per-user Codex MCP configuration for this clone."""
import json
from pathlib import Path
import sys

base = Path(__file__).resolve().parent
if sys.version_info < (3, 12):
    raise SystemExit('Local Code Review requires Python 3.12 or newer.')
config = {'mcpServers': {'local_code_review': {
    'command': sys.executable,
    'args': [str(base / 'server.py'), '--mcp'],
    'cwd': str(base), 'env': {'PYTHONUTF8': '1'}
}}}
(base / 'plugin' / '.mcp.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
print('Configured local-code-review for this clone:')
print('  Python:', sys.executable)
print('  Repository:', base)
print('  MCP config:', base / 'plugin' / '.mcp.json')
