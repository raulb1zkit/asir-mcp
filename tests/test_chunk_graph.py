"""Checks use synthetic fragments only; never contact Supabase."""
import http.client
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chunk_graph import graph_from_rows, make_handler


def chunk(identifier, text='Synthetic fragment'):
    return {'id': identifier, 'text': text, 'requires_review': False, 'warnings': []}


def citation(identifier, subject='ASO', sha='hash-a', **neighbors):
    return {'chunk_id': identifier, 'subject': subject, 'sha256': sha,
            'title': 'Synthetic document', 'filename': 'synthetic.txt', **neighbors}


class GraphTests(unittest.TestCase):
    def test_repeated_citations_deduplicate_nodes_and_edges(self):
        row = citation('chunk:a')
        graph = graph_from_rows('version', [chunk('chunk:a')], [row, dict(row)])
        self.assertEqual(graph['counts'], {'chunk': 1, 'subject': 1, 'source': 1})
        self.assertEqual(len(graph['links']), 2)
        node = next(n for n in graph['nodes'] if n['kind'] == 'chunk')
        self.assertEqual(node['subjects'], ['ASO'])
        self.assertEqual(node['sources'], ['source:ASO:hash-a'])

    def test_shared_chunk_retains_both_subjects_and_document_memberships(self):
        graph = graph_from_rows('version', [chunk('chunk:a')],
                                [citation('chunk:a'), citation('chunk:a', subject='SRI')])
        self.assertEqual(graph['counts'], {'chunk': 1, 'subject': 2, 'source': 2})
        node = next(n for n in graph['nodes'] if n['id'] == 'chunk:a')
        self.assertEqual(set(node['subjects']), {'ASO', 'SRI'})
        self.assertEqual(len(node['sources']), 2)
        self.assertEqual(len(graph['links']), 4)

    def test_adjacency_is_undirected_deduplicated_and_has_valid_endpoints(self):
        graph = graph_from_rows('version', [chunk('chunk:a'), chunk('chunk:b')], [
            citation('chunk:a', next_chunk_id='chunk:b', previous_chunk_id='missing'),
            citation('chunk:b', previous_chunk_id='chunk:a', next_chunk_id='chunk:b'),
        ])
        adjacent = [edge for edge in graph['links'] if edge['kind'] == 'adjacent']
        self.assertEqual(adjacent, [{'source': 'chunk:a', 'target': 'chunk:b', 'kind': 'adjacent'}])
        ids = {n['id'] for n in graph['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in graph['links']))

    def test_rejects_citation_to_nonexistent_chunk(self):
        with self.assertRaisesRegex(ValueError, 'citation_without_chunk'):
            graph_from_rows('version', [], [citation('missing')])

    def test_document_text_is_json_data(self):
        text = '</script><script>alert(1)</script>'
        graph = graph_from_rows('version', [chunk('chunk:a', text)], [citation('chunk:a')])
        self.assertEqual(json.loads(json.dumps(graph))['nodes'][0]['text'], text)


class GraphHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = graph_from_rows('synthetic-version', [chunk('chunk:a')], [citation('chunk:a')])
        cls.server = SimpleNamespace(server_port=8766)

    def request(self, path, host=None, method='GET'):
        # Exercise the actual BaseHTTPRequestHandler parser and response writer
        # using in-memory transport; sandboxed test runners need no sockets.
        class Transport:
            def __init__(self, payload):
                self.payload = payload
                self.output = bytearray()
            def makefile(self, *args, **kwargs):
                return BytesIO(self.payload)
            def sendall(self, data):
                self.output.extend(data)
        host = f'127.0.0.1:{self.server.server_port}' if host is None else host
        transport = Transport(f'{method} {path} HTTP/1.0\r\nHost: {host}\r\n\r\n'.encode())
        make_handler(self.graph)(transport, ('127.0.0.1', 1), self.server)
        response = http.client.HTTPResponse(Transport(bytes(transport.output)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def test_api_serves_snapshot_without_browser_caching(self):
        code, headers, body = self.request('/api/graph')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['corpus_id'], 'synthetic-version')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(int(headers['Content-Length']), len(body))

    def test_assets_are_local_and_have_csp_and_no_store(self):
        for path, mime in (('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css')):
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                self.assertEqual(code, 200)
                self.assertTrue(headers['Content-Type'].startswith(mime))
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
                self.assertNotIn("'unsafe-inline'", headers['Content-Security-Policy'])
                self.assertGreater(len(body), 0)

    def test_rebinding_hosts_cannot_obtain_corpus(self):
        for host in ('evil.test', '127.0.0.1.evil.test', 'localhost', '127.0.0.1:1'):
            with self.subTest(host=host):
                code, _, body = self.request('/api/graph', host)
                self.assertEqual(code, 403)
                self.assertNotIn(b'synthetic-version', body)
        code, _, _ = self.request('/api/graph', f'localhost:{self.server.server_port}')
        self.assertEqual(code, 200)

    def test_arbitrary_paths_do_not_read_files(self):
        for path in ('/../AGENTS.md', '/.private/session.json', '/%2e%2e/AGENTS.md', '/graph/app.js'):
            with self.subTest(path=path):
                code, _, _ = self.request(path)
                self.assertEqual(code, 404)

    def test_mutation_method_is_not_supported(self):
        code, _, body = self.request('/api/graph', method='POST')
        self.assertEqual(code, 501)
        self.assertNotIn(b'synthetic-version', body)


if __name__ == '__main__':
    unittest.main()
