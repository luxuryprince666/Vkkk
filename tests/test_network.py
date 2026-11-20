# -*- coding: utf-8 -*-
"""Изолированные сетевые тесты: никаких запросов к внешним площадкам."""
import importlib.util
import os
import socket
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from core import network


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        payload = self.server.payload
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def start_http(payload):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.paths = []
    server.payload = payload
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class SocksHandler(socketserver.BaseRequestHandler):
    def read_n(self, n):
        out = b''
        while len(out) < n:
            chunk = self.request.recv(n - len(out))
            if not chunk:
                raise EOFError('short SOCKS request')
            out += chunk
        return out

    def handle(self):
        self.request.settimeout(5)
        ver, nmethods = self.read_n(2)
        if ver != 5:
            return
        self.read_n(nmethods)
        self.request.sendall(b'\x05\x00')
        ver, cmd, _reserved, atyp = self.read_n(4)
        if ver != 5 or cmd != 1:
            return
        if atyp == 3:
            name = self.read_n(self.read_n(1)[0]).decode('ascii')
        elif atyp == 1:
            name = socket.inet_ntoa(self.read_n(4))
        else:
            name = socket.inet_ntop(socket.AF_INET6, self.read_n(16))
        port = int.from_bytes(self.read_n(2), 'big')
        self.server.destinations.append((name, port))
        self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50')
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data += chunk
        self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nsocks')


class ThreadedSocks(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class NetworkTests(unittest.TestCase):
    def _proxy_test(self, use_curl):
        proxy, pt = start_http(b'from-proxy')
        target, tt = start_http(b'direct')
        try:
            target_url = 'http://127.0.0.1:%d/target' % target.server_port
            proxy_url = 'http://127.0.0.1:%d' % proxy.server_port
            env = {'NO_PROXY': '*', 'no_proxy': '*', 'HTTP_PROXY': proxy_url, 'http_proxy': proxy_url,
                   'ALL_PROXY': proxy_url, 'all_proxy': proxy_url}
            with patch.dict(os.environ, env), patch.object(network, '_curl_cffi_available', return_value=use_curl):
                proxied = network.http_request(target_url, proxy=proxy_url, timeout=4)
                direct = network.http_request(target_url, timeout=4)
            self.assertEqual((proxied['ok'], proxied['text']), (True, 'from-proxy'), proxied)
            self.assertEqual((direct['ok'], direct['text']), (True, 'direct'), direct)
            self.assertEqual(proxied['backend'], 'curl_cffi' if use_curl else 'urllib')
            self.assertEqual(direct['backend'], 'curl_cffi' if use_curl else 'urllib')
            self.assertEqual(len(proxy.paths), 1)
            self.assertTrue(proxy.paths[0].startswith('http://'))
            self.assertEqual(target.paths, ['/target'])
        finally:
            proxy.shutdown(); proxy.server_close(); pt.join()
            target.shutdown(); target.server_close(); tt.join()

    def test_explicit_http_proxy_ignores_no_proxy_urllib(self):
        self._proxy_test(False)

    @unittest.skipUnless(network._curl_cffi_available(), 'curl_cffi не установлен')
    def test_explicit_http_proxy_ignores_no_proxy_curl(self):
        self._proxy_test(True)

    def _socks_test(self, use_curl):
        srv = ThreadedSocks(('127.0.0.1', 0), SocksHandler)
        srv.destinations = []
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            proxy = 'socks5h://127.0.0.1:%d' % srv.server_address[1]
            with patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'}), \
                 patch.object(network, '_curl_cffi_available', return_value=use_curl):
                result = network.http_request('http://unresolvable.invalid/probe', proxy=proxy, timeout=4)
            self.assertEqual((result['ok'], result['text']), (True, 'socks'), result)
            self.assertEqual(result['backend'], 'curl_cffi' if use_curl else 'urllib')
            self.assertEqual(srv.destinations, [('unresolvable.invalid', 80)])
        finally:
            srv.shutdown(); srv.server_close(); thread.join()

    @unittest.skipUnless(importlib.util.find_spec('socks'), 'PySocks не установлен')
    def test_socks_urllib_uses_remote_dns(self):
        self._socks_test(False)

    @unittest.skipUnless(network._curl_cffi_available(), 'curl_cffi не установлен')
    def test_socks_curl_uses_remote_dns_and_ignores_no_proxy(self):
        self._socks_test(True)

    def test_subscription_dns_rejects_local_multicast_and_mixed_answers(self):
        def addresses(*values):
            return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in values]
        for ips in [('127.0.0.1',), ('10.0.0.4',), ('169.254.169.254',), ('224.0.0.1',),
                    ('::1',), ('::ffff:127.0.0.1',), ('8.8.8.8', '192.168.1.2')]:
            with self.subTest(ips=ips), patch.object(socket, 'getaddrinfo', return_value=addresses(*ips)):
                self.assertEqual(network._public_addresses('example.com'), [])
        with patch.object(socket, 'getaddrinfo', return_value=addresses('8.8.8.8')):
            self.assertEqual(network._public_addresses('example.com'), ['8.8.8.8'])

    def test_subscription_pins_public_ip_and_refuses_redirect(self):
        instances = []
        class Response:
            status = 302
        class Connection:
            def __init__(self, hostname, address, timeout):
                self.args = (hostname, address, timeout); self.closed = False; instances.append(self)
            def request(self, method, path, headers):
                self.requested = (method, path)
            def getresponse(self):
                return Response()
            def close(self):
                self.closed = True
        with patch.object(network, '_public_addresses', return_value=['8.8.8.8']), \
             patch.object(network, '_PinnedHTTPSConnection', Connection), \
             patch.object(network, 'parse_proxy_entries') as parse:
            endpoints, note = network.fetch_subscription('https://example.com/private?token=secret')
        parse.assert_not_called()
        self.assertEqual(endpoints, [])
        self.assertIn('перенаправления запрещены', note)
        self.assertNotIn('secret', note)
        self.assertEqual(instances[0].args[:2], ('example.com', '8.8.8.8'))
        self.assertTrue(instances[0].closed)

    def test_invalid_subscription_rejected_before_network(self):
        values = ['http://example.com/sub', 'https://user:secret@example.com/x',
                  'https://example.com:8443/sub', 'https://example.com/a\r\nBad: header']
        for value in values:
            with self.subTest(value=value), patch.object(network, '_public_addresses') as dns:
                endpoints, _note = network.fetch_subscription(value)
                dns.assert_not_called()
                self.assertFalse(endpoints)

    def test_diagnostics_does_not_use_env_when_rotation_disabled(self):
        with patch.dict(os.environ, {'VKFREE_PROXIES': 'vless://secret@example.test'}), \
             patch.object(network, 'parse_proxy_entries', return_value=[]) as parse:
            network.diagnostics('', autodetect_local=False, include_env=False)
        parse.assert_called_once_with('')

    def test_proxy_password_is_never_in_masked_output(self):
        self.assertEqual(network.mask_proxy_url('socks5://alice:secret@example.com:1080'),
                         'socks5://***@example.com:1080')
        self.assertNotIn('secret', network.mask_proxy_url('vless://secret'))


if __name__ == '__main__':
    unittest.main()
