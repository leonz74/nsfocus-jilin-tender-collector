from __future__ import annotations

import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tender_downloader.ai_keys import MacKeyStore, key_account
from tender_downloader.ai_models import list_ai_models, models_endpoint
from tender_downloader.http_client import HttpResult
from tender_downloader.webui.server import ConfigWebApp, create_server


class ModelClient:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def request(self, url, *, headers):
        self.calls.append((url, headers))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return HttpResult(url, 200, {}, json.dumps(page).encode())


class MemoryKeys:
    supported = True
    def __init__(self): self.entries = {}
    def exists(self, account): return account in self.entries
    def get(self, account): return self.entries.get(account, '')
    def save(self, account, key): self.entries[account] = key
    def delete(self, account): self.entries.pop(account, None)


class ModelDiscoveryTests(unittest.TestCase):
    def test_openai_compatible_keeps_new_and_custom_models_and_deduplicates(self):
        client = ModelClient({'data': [{'id': 'new-model-2027'}, {'id': 'ft:custom'}, {'id': 'new-model-2027'}]})
        result = list_ai_models(client, {'endpoint': 'https://api.example/v1/chat/completions'}, 'secret')
        self.assertTrue(result['complete'])
        self.assertEqual(['ft:custom', 'new-model-2027'], [m['id'] for m in result['models']])
        self.assertEqual('https://api.example/v1/models', client.calls[0][0])
        self.assertEqual({'Authorization': 'Bearer secret'}, client.calls[0][1])

    def test_anthropic_fetches_all_pages(self):
        client = ModelClient({'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'},
                             {'data': [{'id': 'b'}], 'has_more': False})
        result = list_ai_models(client, {'protocol': 'anthropic', 'endpoint': 'https://api.example/v1/messages'}, 'secret')
        self.assertEqual(2, len(result['models']))
        self.assertEqual(['a'], parse_qs(urlsplit(client.calls[1][0]).query)['after_id'])
        self.assertEqual('secret', client.calls[0][1]['x-api-key'])
        self.assertNotIn('Authorization', client.calls[0][1])

    def test_gemini_pages_and_non_generation_models_are_not_silently_dropped(self):
        client = ModelClient({'models': [{'name': 'models/gemini-new', 'displayName': 'New'}], 'nextPageToken': 'next+token'},
                             {'models': [{'name': 'models/text-embedding-new'}]})
        result = list_ai_models(client, {'protocol': 'gemini', 'endpoint': 'https://api.example/v1beta/models/{model}:generateContent'}, 'secret')
        self.assertTrue(result['complete'])
        self.assertEqual(2, len(result['models']))
        self.assertEqual(['next+token'], parse_qs(urlsplit(client.calls[1][0]).query)['pageToken'])
        self.assertEqual('secret', client.calls[0][1]['x-goog-api-key'])
        self.assertTrue(all('secret' not in call[0] for call in client.calls))

    def test_later_page_failure_preserves_partial_list_and_redacts_error(self):
        client = ModelClient({'data': [{'id': 'a'}], 'has_more': True}, ValueError('denied secret-api-key'))
        result = list_ai_models(client, {'endpoint': 'https://api.example/chat/completions'}, 'secret-api-key')
        self.assertTrue(result['ok'])
        self.assertFalse(result['complete'])
        self.assertEqual(1, len(result['models']))
        self.assertNotIn('secret-api-key', json.dumps(result))

    def test_repeated_cursor_and_page_limit_are_explicitly_partial(self):
        page = {'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'}
        for limit in (1, 10):
            with self.subTest(limit=limit):
                result = list_ai_models(ModelClient(page, page), {'endpoint': 'https://api.example/v1/messages', 'protocol': 'anthropic'}, 'key', max_pages=limit)
                self.assertFalse(result['complete'])
                self.assertTrue(result['message'])

    def test_empty_unsupported_and_malformed_responses(self):
        for page, ok, complete in [({'data': []}, True, True), ({'error': 'bad'}, False, False),
                                    ({'choices': []}, False, False), ({'data': [{'id': 'a'}, {}]}, True, False)]:
            with self.subTest(page=page):
                result = list_ai_models(ModelClient(page), {'endpoint': 'https://api.example/v1/chat/completions'}, 'key')
                self.assertEqual(ok, result['ok'])
                self.assertEqual(complete, result['complete'])
                self.assertTrue(result['message'])

    def test_model_url_retains_custom_gateway_prefix(self):
        self.assertEqual('https://gateway.example/custom/v3/models', models_endpoint({'endpoint': 'https://gateway.example/custom/v3/chat/completions'}))
        self.assertEqual('https://api.deepseek.com/models', models_endpoint({'endpoint': 'https://api.deepseek.com/chat/completions'}))


class KeyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / 'config.json'
        self.ai = {'enabled': True, 'protocol': 'openai_compatible', 'provider': 'custom',
                   'endpoint': 'https://api.example/v1/chat/completions', 'model': '',
                   'api_key_env': 'TENDER_AI_KEY_TEST_EMPTY', 'timeout_seconds': 5}
        self.config = {'start_date': '2026-01-01', 'end_date': '2026-09-11',
                       'output_dir': 'output', 'database': 'output/state.sqlite3', 'http': {}, 'ai': self.ai,
                       'sources': [{'type': 'jilin_ggzy', 'enabled': True, 'name': 'fixture'}]}
        self.config_path.write_text(json.dumps(self.config))
        self.server, self.app = create_server(self.config_path, port=0)
        self.keys = self.app.ai_keys = MemoryKeys()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def post(self, path, body, csrf=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = {'Content-Type': 'application/json'}
        if csrf: headers['X-CSRF-Token'] = self.app.csrf_token
        conn.request('POST', path, json.dumps(body), headers)
        response = conn.getresponse()
        value = json.loads(response.read())
        status = response.status
        conn.close()
        return status, value

    def test_save_status_restart_delete_and_no_plaintext_disclosure(self):
        secret = 'synthetic-key-123'
        status, response = self.post('/api/ai-key/save', {'ai': self.ai, 'api_key': secret})
        self.assertEqual(200, status)
        self.assertTrue(response['saved'])
        self.assertNotIn(secret, self.config_path.read_text())
        self.assertNotIn(secret, json.dumps(response))
        new = ConfigWebApp(self.config_path)
        new.ai_keys = self.keys
        self.assertTrue(new.ai_key_status()['saved'])
        self.assertEqual(secret, new._resolve_ai_key(self.ai))
        self.assertEqual('override', new._resolve_ai_key(self.ai, 'override'))
        self.assertEqual(200, self.post('/api/ai-key/delete', {})[0])
        self.assertFalse(self.app.ai_key_status()['saved'])
        self.assertEqual('', self.app._resolve_ai_key(self.ai))

    def test_saved_key_is_scoped_to_exact_endpoint_protocol_and_config(self):
        self.app.save_ai_key('one', self.ai)
        for changed in ({'endpoint': 'https://other.example/v1/chat/completions'},
                        {'endpoint': 'https://api.example/other/chat/completions'}, {'protocol': 'anthropic'}):
            ai = {**self.ai, **changed}
            self.assertFalse(self.app.ai_key_status(ai)['saved'])
            self.assertEqual('', self.app._resolve_ai_key(ai))
        self.assertNotEqual(key_account(self.config_path, self.ai), key_account(self.config_path.with_name('other.json'), self.ai))

    def test_saved_key_reaches_models_probe_and_query_child_without_entering_query_json(self):
        ai = {**self.ai, 'model': 'new-model'}
        self.app.save_ai_key('saved-key', ai)
        with patch('tender_downloader.webui.server.list_ai_models', return_value={'ok': True, 'models': []}) as models:
            self.assertEqual(200, self.post('/api/ai-models', {})[0])
            self.assertEqual('saved-key', models.call_args.args[2])
        with patch('tender_downloader.webui.server.test_ai_connection', return_value={'ok': True}) as probe:
            self.assertEqual(200, self.post('/api/ai-test', {})[0])
            self.assertEqual('saved-key', probe.call_args.args[2])
        with patch.object(self.app.runner, 'start') as start:
            code, result = self.post('/api/query', {'criteria': {'start_date': '2026-01-01', 'end_date': '2026-09-11', 'mode': 'ai_recall'}})
            self.assertEqual(202, code)
            self.assertEqual('saved-key', start.call_args.kwargs['api_key'])
            self.assertNotIn('saved-key', json.dumps(result))
            self.assertNotIn('saved-key', json.dumps(start.call_args.kwargs['query_spec']))

    def test_new_routes_require_csrf_and_reject_unsafe_targets_or_keys(self):
        for path in ('/api/ai-models', '/api/ai-key/save', '/api/ai-key/delete', '/api/ai-key/status'):
            self.assertEqual(403, self.post(path, {}, csrf=False)[0])
        for secret in ('', 42, 'one\r\ntwo'):
            self.assertEqual(422, self.post('/api/ai-key/save', {'api_key': secret})[0])
        for endpoint in ('http://public.example/chat/completions', 'https://api.example/chat/completions?key=leak', 'https://u:p@api.example/chat/completions'):
            self.assertEqual(422, self.post('/api/ai-key/save', {'api_key': 'secret', 'ai': {**self.ai, 'endpoint': endpoint}})[0])
        self.assertFalse(self.keys.entries)

    def test_keychain_failure_is_not_reported_as_saved_and_preserves_input_config(self):
        before = self.config_path.read_bytes()
        with patch.object(self.keys, 'save', side_effect=ValueError('Keychain locked')):
            status, response = self.post('/api/ai-key/save', {'api_key': 'secret'})
        self.assertEqual(422, status)
        self.assertNotIn('secret', json.dumps(response))
        self.assertEqual(before, self.config_path.read_bytes())


@unittest.skipUnless(sys.platform == 'darwin', 'Mac Keychain integration')
class NativeKeychainTests(unittest.TestCase):
    def test_saved_key_can_be_read_by_a_new_process_then_updated_and_deleted(self):
        import uuid
        account, key = 'test-' + uuid.uuid4().hex, 'synthetic-' + uuid.uuid4().hex
        store = MacKeyStore()
        try:
            store.save(account, key)
            code = 'import json,sys; from tender_downloader.ai_keys import MacKeyStore; a,k=json.load(sys.stdin); assert MacKeyStore().get(a)==k; print("read verified")'
            result = subprocess.run([sys.executable, '-c', code], input=json.dumps([account, key]), text=True, capture_output=True, timeout=20)
            self.assertEqual(0, result.returncode, 'child Keychain read failed')
            store.save(account, key + '-updated')
            self.assertEqual(key + '-updated', store.get(account))
        finally:
            store.delete(account)
        self.assertFalse(store.exists(account))
