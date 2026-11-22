"""Generate private runtime files; never modify Codex config automatically."""
import argparse
import getpass
import json
import os
import plistlib
import secrets
import shlex
import sys
from pathlib import Path

from gateway import GEMINI_MODELS, combine_catalog

PROJECT = Path(__file__).resolve().parent


def build_runtime(destination, original, extra, gemini_key, python, project,
                  port=2052, gemini_port=2048):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Runtime already exists; retain it and review before changing anything')
    if not all(isinstance(value, int) and 0 < value < 65536 for value in (port, gemini_port)):
        raise ValueError('Ports must be between 1 and 65535')
    if port == gemini_port:
        raise ValueError('Gateway and backend ports must be different')
    if not isinstance(gemini_key, str) or not gemini_key.strip():
        raise ValueError('A nonempty local backend API key is required')
    models = original.get('models', [])
    if not models or any(not isinstance(m.get('slug'), str) for m in models):
        raise ValueError('Expected an OpenAI catalog with nonempty models and slug fields')
    if {m['slug'] for m in extra.get('models', [])} != GEMINI_MODELS:
        raise ValueError('This release supports the two Gemini IDs from the supplied example only')
    destination.mkdir(mode=0o700)

    def write(name, text):
        path = destination / name
        with path.open('x') as file:
            file.write(text)
        path.chmod(0o600)

    def write_json(name, value):
        write(name, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    write_json('settings.json', {
        'port': port, 'gemini_port': gemini_port,
        'gateway_key': secrets.token_urlsafe(32),
        'gemini_credential_file': str(destination / 'gemini-credentials.json')})
    write_json('gemini-credentials.json', {'api_key': gemini_key})
    write_json('openai-fallback-models.json', original)
    write_json('gemini-models.json', extra)
    write_json('models.json', combine_catalog(original, extra))
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    snippet = f'''# Merge these ROOT settings before the first TOML table.
# Keep your existing model and model_reasoning_effort settings.
model_provider = "codex-gemini-gateway"
model_catalog_json = {quote(destination / 'models.json')}

# Append these provider tables after your existing tables.
[model_providers.codex-gemini-gateway]
name = "GPT + Gemini (local gateway)"
base_url = "http://127.0.0.1:{port}/backend-api/codex"
model_catalog_url = "http://127.0.0.1:{port}/backend-api/codex/models"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = 180000

[model_providers.codex-gemini-gateway.gateway_oauth]
delivery = {{ kind = "header", name = "X-Ray-Gateway-Key", scheme = "Bearer" }}

[model_providers.codex-gemini-gateway.gateway_oauth.credential_export]
command = {quote(python)}
args = ["-B", {quote(project / 'gateway_token.py')}]
timeout_ms = 5000
'''
    write('codex-settings.snippet.toml', snippet)
    agent = {
        'Label': 'local.codex-gemini-gateway',
        'ProgramArguments': [str(python), '-B', str(project / 'gateway.py')],
        'WorkingDirectory': str(project), 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 5, 'Umask': 0o077,
        'StandardOutPath': str(destination / 'service.stdout.log'),
        'StandardErrorPath': str(destination / 'service.stderr.log')}
    write('local.codex-gemini-gateway.plist', plistlib.dumps(agent).decode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openai-catalog', required=True, type=Path)
    parser.add_argument('--port', type=int, default=2052)
    parser.add_argument('--gemini-port', type=int, default=2048)
    args = parser.parse_args()
    os.umask(0o077)
    original = json.loads(args.openai_catalog.read_text())
    extra = json.loads((PROJECT / 'examples/gemini-models.json').read_text())
    key = getpass.getpass('Existing local Gemini backend API key (hidden): ')
    runtime = PROJECT / 'runtime'
    build_runtime(runtime, original, extra, key, Path(sys.executable).resolve(), PROJECT,
                  port=args.port, gemini_port=args.gemini_port)
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()
    target = codex_home / 'config.toml'
    backup = runtime / 'config-before.toml'
    backup.write_bytes(target.read_bytes() if target.exists() else b'')
    backup.chmod(0o600)
    restore = f'''#!/bin/sh
set +e
if [ "$(/usr/bin/id -u)" = 0 ]; then
 echo 'Run this restore as the same normal user; no sudo is needed.'
 exit 1
fi
/bin/launchctl bootout "gui/$(/usr/bin/id -u)/local.codex-gemini-gateway" 2>/dev/null
if [ -f {shlex.quote(str(backup))} ]; then
 /bin/cp {shlex.quote(str(backup))} {shlex.quote(str(target))} || exit 1
 /bin/chmod 600 {shlex.quote(str(target))}
 echo 'Original full Codex config restored. Restart Codex and create a new GPT chat.'
else
 echo 'Backup missing; config was not changed.'
 exit 1
fi
'''
    (runtime / 'restore.sh').write_text(restore)
    (runtime / 'restore.sh').chmod(0o600)
    print('Private runtime generated. Codex configuration was NOT changed.')
    print('Review runtime/codex-settings.snippet.toml and the README before merging.')
    print('Offline full-config rollback: sh ' + shlex.quote(str(runtime / 'restore.sh')))


if __name__ == '__main__':
    main()
