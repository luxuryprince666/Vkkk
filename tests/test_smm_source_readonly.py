# -*- coding: utf-8 -*-
"""Static no-order source inspection; no real network in these tests."""
import json
import unittest
from unittest.mock import patch

from tools import ci_smm_source_readonly as probe


class SourceInspectionTests(unittest.TestCase):
    def test_source_analysis_never_publishes_inline_values_or_queries(self):
        html = '''<html><script src="https://smmnakrutka.ru/static/site.js?token=private-token"></script>
        <script>const secret='private-short-secret';
        function verify(){ fetch('https://private.example/secret', {method:'POST',
        body:{permissionKey:secret,sessionToken:'some-private-value',rand:'123'}}); }
        </script></html>'''
        result = probe.analyze(html)
        text = json.dumps(result)
        self.assertIn('permissionKey', text)
        self.assertIn('sessionToken', text)
        for secret in ('private-token', 'private-short-secret', 'some-private-value', 'private.example', '123'):
            self.assertNotIn(secret, text)
        self.assertEqual(result['script_sources'][0]['path'], '/static/site.js')
        self.assertTrue(all(w in probe.SAFE_WORDS
                            for x in result['marker_matches'] for w in x['nearby_code_words']))
        self.assertNotIn('private', probe.safe_structure("fetch('private-short-secret')"))

    def test_only_fixed_public_endpoint_hosts_are_exposed_without_query(self):
        text = ('https://smmbakparse.ru/php/countTimePermission.php?token=secret '
                'https://other.example/php/untrusted.php?email=private')
        self.assertEqual(probe.public_endpoints(text),
                         [{'host': 'smmbakparse.ru', 'path': '/php/countTimePermission.php'}])

    def test_public_path_is_sanitized_and_cross_protocol_rejected(self):
        self.assertEqual(probe.public_script_url('/js/' + 'x' * 40 + '.js?key=secret')['path'], '/js/[redacted]')
        self.assertIsNone(probe.public_script_url('http://example.org/a.js'))
        self.assertIsNone(probe.public_script_url('https://user:secret@example.org/a.js'))
        self.assertIsNone(probe.public_script_url('javascript:alert(1)'))

    def test_runner_blocks_unscoped_local_execution_before_network(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}), \
             patch.object(probe.network, 'http_request') as request:
            with self.assertRaises(RuntimeError):
                probe.run()
        request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
