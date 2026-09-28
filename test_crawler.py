"""Self-contained local-server integration test; no public websites contacted."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from crawler import Crawler, normalize_url


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        pages = {
            '/robots.txt': ('text/plain', 'User-agent: *\nDisallow: /private\n'),
            '/': ('text/html', '<title>Home</title><a href="/a">A</a><a href="/copy">Copy</a><a href="/private">Private</a><a href="https://example.org/else">External</a>'),
            '/a': ('text/html', '<title>A</title><a href="/a#fragment">self</a>'),
            '/copy': ('text/html', '<title>A</title><a href="/a#fragment">self</a>'),
            '/private': ('text/html', '<title>Forbidden</title>'),
            '/redirect-private': ('redirect', '/private'),
        }
        if self.path not in pages:
            self.send_error(404)
            return
        kind, body = pages[self.path]
        if kind == 'redirect':
            self.send_response(302)
            self.send_header('Location', body)
            self.end_headers()
            return
        encoded = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        return


class Tests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_url('HTTPS://EXAMPLE.COM:443/foo#x'), 'https://example.com/foo')
        self.assertIsNone(normalize_url('mailto:someone@example.com'))

    def test_crawl(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with tempfile.TemporaryDirectory() as directory:
                c = Crawler([base + '/', base + '/redirect-private'], directory,
                            max_pages=10, max_depth=2, workers=3, delay=0,
                            timeout=5, max_bytes=20000)
                c.run()
                records = [json.loads(s) for s in Path(directory, 'results.jsonl').read_text().splitlines()]
                statuses = {r['url']: r['status'] for r in records}
                self.assertEqual(statuses[base + '/private'], 'blocked_by_robots')
                self.assertEqual(statuses[base + '/redirect-private'], 'error')
                self.assertEqual(statuses[base + '/'], 'ok')
                self.assertEqual(statuses[base + '/a'], 'ok')
                self.assertEqual(statuses[base + '/copy'], 'ok')
                self.assertEqual(len(list(Path(directory, 'pages').glob('*.html'))), 2)
                self.assertEqual(len(records), 5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
