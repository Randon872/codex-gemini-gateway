import unittest
import json
import http.client
import threading
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from gateway import (route_for, prepare_headers, combine_catalog, canonical_path, GatewayServer,
                     Handler, prepare_gemini_body, prepare_openai_body, tag_gemini_event,
                     rewrite_gemini_stream)


class BoundaryTests(unittest.TestCase):
    def test_gemini_uses_only_its_own_credential(self):
        incoming = {'Authorization': 'Bearer openai-private', 'ChatGPT-Account-ID': 'workspace-private',
                    'Cookie': 'private-cookie', 'X-Ray-Gateway-Key': 'gateway-private',
                    'Content-Type': 'application/json', 'x-oai-attestation': 'private-attestation'}
        headers = prepare_headers(incoming, 'gemini', 'gemini-private')
        self.assertEqual(headers['Authorization'], 'Bearer gemini-private')
        self.assertNotIn('openai-private', json.dumps(headers))
        self.assertNotIn('workspace-private', json.dumps(headers))
        self.assertNotIn('private-cookie', json.dumps(headers))
        self.assertNotIn('gateway-private', json.dumps(headers))
        self.assertNotIn('private-attestation', json.dumps(headers))

    def test_openai_preserves_its_identity_and_discards_gateway_key(self):
        headers = prepare_headers({'Authorization': 'Bearer openai-private',
                                   'ChatGPT-Account-ID': 'account', 'X-Ray-Gateway-Key': 'gateway-private',
                                   'Host': '127.0.0.1:2052'}, 'openai', 'gemini-private')
        self.assertEqual(headers['Authorization'], 'Bearer openai-private')
        self.assertEqual(headers['ChatGPT-Account-ID'], 'account')
        self.assertNotIn('gateway-private', json.dumps(headers))
        self.assertNotIn('gemini-private', json.dumps(headers))
        self.assertNotIn('Host', headers)

    def test_catalog_compression_header_has_one_authoritative_value(self):
        headers = prepare_headers({'accept-encoding': 'gzip, zstd'}, 'openai', '')
        values = [v for k, v in headers.items() if k.lower() == 'accept-encoding']
        self.assertEqual(values, ['identity'])

    def test_unknown_models_cannot_escape_to_a_default_route(self):
        for name in ['', None, 'https://evil.example', 'gemini-4-carbon', 'gpt-made-up']:
            with self.assertRaises(ValueError):
                route_for(name, {'gpt-6.1-sol'})

    def test_model_routes_are_explicit(self):
        self.assertEqual(route_for('gemini-4-neon', set()), 'gemini')
        self.assertEqual(route_for('gemini-4-hydrogen', set()), 'gemini')
        self.assertEqual(route_for('gpt-6.1-sol', {'gpt-6.1-sol'}), 'openai')

    def test_catalog_preserves_original_metadata_without_duplicates(self):
        original = {'models': [{'slug': 'gpt-6.1-sol', 'private_metadata': {'a': 1}},
                               {'slug': 'gemini-4-neon', 'wrong': True}], 'cache_tag': 'retained'}
        extra = {'models': [{'slug': 'gemini-4-neon', 'display_name': 'Neon'},
                            {'slug': 'gemini-4-hydrogen', 'display_name': 'Hydrogen'}]}
        merged = combine_catalog(original, extra)
        self.assertEqual(merged['cache_tag'], 'retained')
        self.assertEqual(merged['models'][0], original['models'][0])
        self.assertEqual(len(merged['models']), 3)
        self.assertEqual(merged['models'][1], extra['models'][0])

    def test_only_documented_endpoints_are_accepted(self):
        for prefix in ['', '/v1', '/backend-api/codex']:
            self.assertEqual(canonical_path(prefix + '/responses?x=1'), '/responses')
            self.assertEqual(canonical_path(prefix + '/models'), '/models')
        for value in ['/../models', '//evil.example/models', '/responses/evil', '/settings.json']:
            with self.assertRaises(ValueError):
                canonical_path(value)

    def test_new_request_failure_is_not_hidden_by_a_previous_response(self):
        handler = object.__new__(Handler)
        handler.response_started = True
        handler.headers = {'X-Ray-Gateway-Key': 'Bearer test', 'Authorization': 'Bearer example'}
        handler.server = SimpleNamespace(settings={'gateway_key': 'test'},
                                         slots=threading.BoundedSemaphore(1), record=lambda **event: None)
        statuses = []
        handler.error_json = lambda code, message: statuses.append(code)
        def fail():
            raise OSError('fake upstream failure')
        handler.dispatch = fail
        handler.handle_request()
        self.assertEqual(statuses, [502])

    def test_replayed_tool_updates_do_not_duplicate_current_tools(self):
        current = {'type': 'function', 'name': 'request_user_input', 'description': 'current'}
        previous = {'type': 'function', 'name': 'request_user_input', 'description': 'previous'}
        extra = {'type': 'function', 'name': 'additional_lookup'}
        request = {'model': 'gemini-4-neon', 'tools': [current], 'input': [
            {'type': 'additional_tools', 'tools': [previous, extra]},
            {'role': 'user', 'content': 'Keep this message'},
            {'type': 'function_call_output', 'call_id': 'keep-id', 'output': 'keep-result'}]}
        result = prepare_gemini_body(request)
        self.assertEqual(result['tools'], [current])
        self.assertEqual(result['input'], request['input'][1:])
        self.assertEqual(request['input'][0]['type'], 'additional_tools')

    def test_historical_namespace_updates_cannot_expand_the_current_toolset(self):
        a = {'type': 'function', 'name': 'a', 'description': 'current'}
        b = {'type': 'function', 'name': 'b'}
        result = prepare_gemini_body({'tools': [{'type': 'namespace', 'name': 'tools', 'tools': [a]}],
                                     'input': [{'type': 'additional_tools', 'tools': [
                                         {'type': 'namespace', 'name': 'tools', 'tools': [
                                             {'type': 'function', 'name': 'a', 'description': 'old'}, b]}]}]})
        self.assertEqual(result['tools'][0]['tools'], [a])
        self.assertEqual(result['input'], [])

    def test_input_only_tool_declarations_are_still_supported(self):
        tool = {'type': 'function', 'name': 'lookup'}
        result = prepare_gemini_body({'input': [{'type': 'additional_tools', 'tools': [tool]}]})
        self.assertEqual(result['tools'], [tool])

    def test_openai_receives_only_its_own_reasoning_items(self):
        own = {'type': 'reasoning', 'id': 'rs_openai', 'encrypted_content': 'openai-opaque'}
        foreign = {'type': 'reasoning', 'id': 'rs_raygemini_call_1', 'encrypted_content': 'gemini-opaque'}
        call = {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'preserved'}
        result = prepare_openai_body({'input': [own, foreign, call]})
        self.assertEqual(result['input'], [own, call])

    def test_gemini_receives_only_tagged_gemini_reasoning(self):
        own = {'type': 'reasoning', 'id': 'rs_raygemini_call_1', 'encrypted_content': 'gemini-opaque'}
        foreign = {'type': 'reasoning', 'id': 'rs_openai', 'encrypted_content': 'openai-opaque'}
        result = prepare_gemini_body({'input': [foreign, own], 'tools': []})
        self.assertEqual(result['input'], [own])

    def test_reasoning_provenance_does_not_change_call_ids_or_signatures(self):
        event = {'item_id': 'rs_call_1', 'item': {'type': 'reasoning', 'id': 'rs_call_1',
                                               'encrypted_content': 'opaque'},
                 'call': {'id': 'fc_1', 'call_id': 'call_1'}}
        result = tag_gemini_event(event)
        self.assertEqual(result['item_id'], 'rs_raygemini_call_1')
        self.assertEqual(result['item']['id'], result['item_id'])
        self.assertEqual(result['item']['encrypted_content'], 'opaque')
        self.assertEqual(result['call'], event['call'])

    def test_sse_transformation_handles_arbitrary_chunk_boundaries(self):
        event = {'type': 'response.output_item.added', 'item': {'type': 'reasoning', 'id': 'rs_call_9'}}
        raw = ('event: response.output_item.added\ndata: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()
        chunks = [raw[i:i+3] for i in range(0, len(raw), 3)]
        transformed = b''.join(rewrite_gemini_stream(chunks))
        self.assertIn(b'rs_raygemini_call_9', transformed)
        self.assertTrue(transformed.endswith(b'data: [DONE]\n\n'))


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'openai-fallback-models.json').write_text(json.dumps({'models': [{'slug': 'gpt-test'}]}))
        self.root_patch = patch('gateway.ROOT', self.root)
        self.root_patch.start()
        self.server = GatewayServer(('127.0.0.1', 0), {'gateway_key': 'test-access'})
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.root_patch.stop()
        self.directory.cleanup()

    def request(self, path, headers, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request('POST' if body is not None else 'GET', path, body=body, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            return response.status, data
        finally:
            connection.close()

    def test_local_runtime_catalog_is_used_without_sibling_application(self):
        from gateway import load_gemini_catalog
        (self.root / 'gemini-models.json').write_text(json.dumps({'models': [{'slug': 'gemini-4-neon'}]}))
        self.assertEqual(load_gemini_catalog()['models'][0]['slug'], 'gemini-4-neon')

    def test_no_gateway_credential_cannot_reach_any_upstream(self):
        status, body = self.request('/models', {'Authorization': 'Bearer fake-openai'})
        self.assertEqual(status, 401)
        self.assertNotIn('fake-openai', json.dumps(body))

    def test_non_ascii_gateway_credential_is_rejected_cleanly(self):
        status, _ = self.request('/models', {'X-Ray-Gateway-Key': 'Bearer caf\u00e9'})
        self.assertEqual(status, 401)

    def test_browser_origin_cannot_use_local_credentials(self):
        status, _ = self.request('/models', {'X-Ray-Gateway-Key': 'Bearer test-access',
                                           'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_unknown_model_fails_without_fallback(self):
        status, _ = self.request('/responses', {'X-Ray-Gateway-Key': 'Bearer test-access',
                                              'Authorization': 'Bearer fake-openai'},
                                 json.dumps({'model': 'unknown'}))
        self.assertEqual(status, 400)

    def test_unknown_path_does_not_serve_private_files(self):
        status, _ = self.request('/settings.json', {'X-Ray-Gateway-Key': 'Bearer test-access',
                                                 'Authorization': 'Bearer fake-openai'})
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
