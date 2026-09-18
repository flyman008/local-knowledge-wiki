"""Isolated public onboarding smoke test; no models or production data."""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='knowledge-public-smoke-') as td:
        root = Path(td)
        shutil.copytree(repo / '_service', root / '_service', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(repo / 'examples', root / 'examples')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, ANTHROPIC_AUTH_TOKEN='', PYTHONIOENCODING='utf-8')
        with (root / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--app-dir', '_service', '--host', '127.0.0.1', '--port', str(port)], cwd=root, env=env, stdout=log, stderr=log)
            def get(path):
                with urllib.request.urlopen(base + path, timeout=3) as response:
                    return json.load(response)
            def cli(*args):
                # Test-only override; production CLI remains on port 8765.
                code = "import sys; sys.path.insert(0, '_service'); import intake; intake.SERVICE=" + repr(base) + "; sys.exit(intake.main())"
                result = subprocess.run([sys.executable, '-c', code, *args], cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                assert result.returncode == 0, result.stdout + result.stderr
                return result.stdout
            try:
                for _ in range(60):
                    try:
                        assert get('/api/capabilities')['storage'] == 'library'
                        break
                    except OSError:
                        time.sleep(0.25)
                else:
                    raise RuntimeError('Server did not start')
                assert get('/api/status')['worker'] == 'stopped'
                with urllib.request.urlopen(base, timeout=3) as response:
                    assert response.status == 200
                ex = 'examples/first-intake/'
                output = cli('submit', '--intent', 'prepared', '--file', ex+'original.md', '--extract-file', ex+'extract.md', '--wiki-file', ex+'wiki.md', '--source-metadata-file', ex+'source.json', '--title', '演示知识库入门样本', '--kb', 'library', '--scope', 'external', '--tag', 'AI', '--parser', 'repository plain text fixture')
                import re
                receipt = re.search(r'rcp_[a-zA-Z0-9]+', output).group(0)
                status = get('/api/receipt/' + receipt)
                assert all(status['saved_layers'][layer] for layer in ('raw', 'extract', 'wiki'))
                assert status['quality']['automatic_check'] == 'passed'
                cli('status', receipt)
                assert '演示知识库入门样本' in cli('search', '演示知识库入门样本', '--kb', 'library')
                cli('submit', '--intent', 'archive', '--file', ex+'wiki.md', '--title', '演示成稿归档', '--kb', 'library', '--scope', 'external', '--tag', 'AI')
                cli('review', 'list', '--kb', 'library')
                with closing(sqlite3.connect(root / '_local' / 'knowledge.db')) as conn:
                    assert conn.execute('SELECT COUNT(*) FROM usage_log').fetchone()[0] == 0
                print('PASS: no-key startup, homepage HTTP, capabilities, prepared layers, quality, receipt, search, archive, reviews, zero backend usage. Temporary data only.')
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    main()
