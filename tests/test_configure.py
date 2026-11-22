import json
import os
import stat
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
from configure import build_runtime


class ConfigureTests(unittest.TestCase):
    def test_cli_backs_up_without_modifying_codex_and_creates_valid_restore(self):
        import configure
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            (project / 'examples').mkdir(parents=True)
            (project / 'examples/gemini-models.json').write_text(json.dumps({'models': [
                {'slug': 'gemini-4-neon'}, {'slug': 'gemini-4-hydrogen'}]}))
            catalog = root / 'catalog.json'
            catalog.write_text(json.dumps({'models': [{'slug': 'gpt-test'}]}))
            codex_home = root / 'codex'
            codex_home.mkdir()
            original = 'model = "gpt-test"\n[plugins]\n'
            (codex_home / 'config.toml').write_text(original)
            with patch.object(configure, 'PROJECT', project), patch('getpass.getpass', return_value='fake-local-key'), patch('sys.argv', ['configure.py', '--openai-catalog', str(catalog)]), patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), patch('builtins.print'):
                configure.main()
            self.assertEqual((codex_home / 'config.toml').read_text(), original)
            self.assertEqual((project / 'runtime/config-before.toml').read_text(), original)
            subprocess.run(['/bin/sh', '-n', str(project / 'runtime/restore.sh')], check=True)

    def test_generated_config_keeps_secrets_out_of_toml_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            original = {'models': [{'slug': 'gpt-test', 'context_window': 12345}]}
            extra = {'models': [{'slug': 'gemini-4-neon'}, {'slug': 'gemini-4-hydrogen'}]}
            build_runtime(runtime, original, extra, 'example-gemini-key', '/usr/bin/python3', root)
            settings = json.loads((runtime / 'settings.json').read_text())
            snippet = (runtime / 'codex-settings.snippet.toml').read_text()
            self.assertNotIn('example-gemini-key', snippet)
            self.assertNotIn(settings['gateway_key'], snippet)
            self.assertIn('requires_openai_auth = true', snippet)
            self.assertIn('credential_export', snippet)
            self.assertEqual(json.loads((runtime / 'models.json').read_text())['models'][0], original['models'][0])
            for filename in ('settings.json', 'gemini-credentials.json', 'models.json'):
                self.assertEqual(stat.S_IMODE((runtime / filename).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)

    def test_existing_runtime_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / 'runtime'
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                build_runtime(existing, {'models': [{'slug': 'gpt-test'}]}, {'models': []}, 'fake', '/usr/bin/python3', root)

    def test_invalid_ports_or_unrecognized_gemini_models_do_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for port in (-1, 0, 65536):
                with self.assertRaises(ValueError):
                    build_runtime(root / 'runtime', {'models': [{'slug': 'gpt-test'}]}, {'models': []}, 'fake', '/usr/bin/python3', root, port=port)
            self.assertFalse((root / 'runtime').exists())
