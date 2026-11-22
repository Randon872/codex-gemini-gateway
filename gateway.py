"""Private loopback gateway: native OpenAI identity and separate Gemini auth."""
import json
import hmac
import http.client
import ssl
import threading
import time
import uuid
import copy
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent / "runtime"
GEMINI_MODELS = frozenset(('gemini-4-neon', 'gemini-4-hydrogen'))
GEMINI_REASONING_PREFIX = 'rs_raygemini_'
HOP_HEADERS = frozenset(('host', 'connection', 'proxy-connection', 'keep-alive',
                         'transfer-encoding', 'te', 'trailer', 'upgrade',
                         'content-length', 'x-ray-gateway-key', 'cookie', 'accept-encoding'))


def route_for(model, openai_models):
    if model in GEMINI_MODELS:
        return 'gemini'
    if isinstance(model, str) and model in openai_models:
        return 'openai'
    raise ValueError('Unknown model')


def prepare_headers(incoming, route, gemini_key):
    if route == 'gemini':
        return {'Authorization': 'Bearer ' + gemini_key,
                'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
    headers = {k: v for k, v in incoming.items() if k.lower() not in HOP_HEADERS}
    headers['Accept-Encoding'] = 'identity'
    return headers


def load_gemini_catalog():
    return json.loads((ROOT / 'gemini-models.json').read_text())


def combine_catalog(original, extra):
    result = dict(original)
    names = {m['slug'] for m in extra['models']}
    result['models'] = [m for m in original['models'] if m.get('slug') not in names]
    result['models'].extend(extra['models'])
    return result


def canonical_path(path):
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise ValueError('Invalid path')
    for prefix in ('', '/v1', '/backend-api/codex'):
        for endpoint in ('/models', '/responses', '/responses/compact', '/responses/lite',
                         '/responses/input_tokens'):
            if parsed.path == prefix + endpoint:
                return endpoint
    raise ValueError('Unknown endpoint')


def prepare_gemini_body(request):
    """Use the current toolset rather than replaying a previous model's tools."""
    if not isinstance(request.get('input'), list):
        return request
    result = copy.deepcopy(request)
    tools = result.setdefault('tools', [])
    current_toolset_provided = 'tools' in request
    retained = []

    def tool_key(tool):
        kind = tool.get('type')
        if kind in ('function', 'custom'):
            return ('callable', tool.get('name'))
        return (kind, tool.get('name'))

    def merge(target, additions):
        indexed = {tool_key(tool): tool for tool in target}
        for tool in additions:
            key = tool_key(tool)
            if key not in indexed:
                target.append(tool)
                indexed[key] = tool
            elif tool.get('type') == 'namespace':
                merge(indexed[key].setdefault('tools', []), tool.get('tools', []))

    for item in result['input']:
        if isinstance(item, dict) and item.get('type') == 'additional_tools':
            if not current_toolset_provided:
                merge(tools, item.get('tools', []))
        elif isinstance(item, dict) and item.get('type') == 'reasoning' and not str(item.get('id', '')).startswith(GEMINI_REASONING_PREFIX):
            continue
        else:
            retained.append(item)
    result['input'] = retained
    return result


def prepare_openai_body(request):
    if not isinstance(request.get('input'), list):
        return request
    result = dict(request)
    result['input'] = [item for item in request['input'] if not (
        isinstance(item, dict) and item.get('type') == 'reasoning' and
        str(item.get('id', '')).startswith(GEMINI_REASONING_PREFIX))]
    return result


def tag_gemini_event(value):
    if isinstance(value, list):
        return [tag_gemini_event(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ('id', 'item_id') and isinstance(item, str) and item.startswith('rs_') and not item.startswith(GEMINI_REASONING_PREFIX):
            result[key] = GEMINI_REASONING_PREFIX + item[3:]
        else:
            result[key] = tag_gemini_event(item)
    return result


def rewrite_gemini_stream(chunks):
    pending = b''

    def rewrite(line):
        if line.startswith(b'data:'):
            payload = line[5:].strip()
            if payload and payload != b'[DONE]':
                return b'data: ' + json.dumps(tag_gemini_event(json.loads(payload))).encode() + b'\n'
        return line

    for chunk in chunks:
        pending += chunk
        if len(pending) > 8 * 1024 * 1024:
            raise ValueError('Upstream SSE line exceeds size bound')
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            yield rewrite(line + b'\n')
    if pending:
        yield rewrite(pending)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, settings):
        super().__init__(address, Handler)
        self.settings = settings
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(12)
        self.audit = logging.Logger('ray-model-gateway')
        self.audit.addHandler(RotatingFileHandler(str(ROOT / 'routes.jsonl'),
                                                 maxBytes=5 * 1024 * 1024, backupCount=2))
        data = json.loads((ROOT / 'openai-fallback-models.json').read_text())
        self.openai_models = {m['slug'] for m in data['models']}

    def server_close(self):
        super().server_close()
        for handler in self.audit.handlers:
            handler.close()

    def record(self, **event):
        # Deliberately exclude headers, tokens, prompts, responses and account IDs.
        event['time'] = time.time()
        with self.lock:
            self.audit.info(json.dumps(event))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(180)

    def error_json(self, code, message):
        self.close_connection = True
        data = json.dumps({'error': {'message': message, 'type': 'gateway_error'}}).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        self.response_started = False
        expected = 'Bearer ' + self.server.settings['gateway_key']
        supplied = self.headers.get('X-Ray-Gateway-Key', '')
        if not hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
            self.error_json(401, 'Local gateway authentication required')
            return
        if self.headers.get('Origin'):
            self.error_json(403, 'Browser-origin requests are not accepted')
            return
        if not self.headers.get('Authorization', '').startswith('Bearer '):
            self.error_json(401, 'OpenAI sign-in credential required')
            return
        if not self.server.slots.acquire(blocking=False):
            self.error_json(429, 'Local gateway is busy')
            return
        try:
            self.dispatch()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            self.server.record(event='transport_error', error=type(error).__name__)
            # If streaming has started, close it. Never synthesize a successful end.
            if getattr(self, 'response_started', False):
                self.close_connection = True
            else:
                self.error_json(502, 'Upstream connection failed; inspect the redacted gateway log')
        finally:
            self.server.slots.release()

    def dispatch(self):
        try:
            endpoint = canonical_path(self.path)
        except ValueError:
            self.error_json(404, 'Unsupported endpoint')
            return
        if endpoint == '/models' and self.command == 'GET':
            self.catalog()
            return
        if self.command != 'POST' or endpoint == '/models':
            self.error_json(405, 'Unsupported method')
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
            self.error_json(400, 'One Content-Length header is required')
            return
        try:
            length = int(lengths[0])
            if not 0 < length <= 64 * 1024 * 1024:
                raise ValueError()
        except ValueError:
            self.error_json(413, 'Invalid request size')
            return
        body = self.rfile.read(length)
        try:
            if len(body) != length:
                raise ValueError()
            request = json.loads(body)
            model = request.get('model')
            with self.server.lock:
                route = route_for(model, self.server.openai_models)
        except (ValueError, AttributeError, TypeError):
            self.error_json(400, 'Invalid JSON or unknown model')
            return
        if route == 'gemini' and endpoint != '/responses':
            self.error_json(400, 'This Gemini route supports the standard Responses endpoint only')
            return
        query = urlsplit(self.path).query
        path = endpoint + ('?' + query if query else '')
        identifier = uuid.uuid4().hex
        self.server.record(event='request', request_id=identifier, model=model, upstream=route,
                           endpoint=endpoint)
        if route == 'gemini':
            normalized = prepare_gemini_body(request)
            body = json.dumps(normalized).encode()
            creds = json.loads(Path(self.server.settings['gemini_credential_file']).read_text())
            headers = prepare_headers(self.headers, route, creds['api_key'])
            connection = http.client.HTTPConnection('127.0.0.1', self.server.settings.get('gemini_port', 2048), timeout=180)
            target = '/v1' + path
        else:
            body = json.dumps(prepare_openai_body(request)).encode()
            headers = prepare_headers(self.headers, route, '')
            connection = http.client.HTTPSConnection('chatgpt.com', context=ssl.create_default_context(), timeout=180)
            target = '/backend-api/codex' + path
        try:
            connection.request('POST', target, body=body, headers=headers)
            response = connection.getresponse()
            self.server.record(event='upstream_status', request_id=identifier, model=model,
                               upstream=route, status=response.status)
            self.response_started = True
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and key.lower() not in ('server', 'date', 'set-cookie'):
                    self.send_header(key, value)
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            chunks = iter(lambda: response.read1(65536), b'')
            if route == 'gemini':
                if response.getheader('Content-Type', '').startswith('text/event-stream'):
                    chunks = rewrite_gemini_stream(chunks)
                else:
                    payload = response.read(64 * 1024 * 1024 + 1)
                    if len(payload) > 64 * 1024 * 1024:
                        raise ValueError('Upstream JSON response exceeds size bound')
                    chunks = iter([json.dumps(tag_gemini_event(json.loads(payload))).encode()])
            for chunk in chunks:
                self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        finally:
            connection.close()

    def catalog(self):
        query = urlsplit(self.path).query
        connection = http.client.HTTPSConnection('chatgpt.com', context=ssl.create_default_context(), timeout=45)
        headers = prepare_headers(self.headers, 'openai', '')
        try:
            connection.request('GET', '/backend-api/codex/models' + ('?' + query if query else ''), headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                self.server.record(event='catalog_error', status=response.status)
                self.error_json(response.status, 'OpenAI model catalog request failed')
                return
            original = json.loads(response.read(8 * 1024 * 1024 + 1))
            extra = load_gemini_catalog()
            result = combine_catalog(original, extra)
            with self.server.lock:
                self.server.openai_models = {m['slug'] for m in original['models']}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
            self.server.record(event='catalog_served', models=len(result['models']))
        finally:
            connection.close()


if __name__ == '__main__':
    import os
    os.umask(0o077)
    settings = json.loads((ROOT / 'settings.json').read_text())
    server = GatewayServer(('127.0.0.1', settings['port']), settings)
    server.serve_forever()
