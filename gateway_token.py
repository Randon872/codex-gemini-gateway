"""Credential export helper. Only Codex should consume stdout."""
import json
import sys
from pathlib import Path

settings = Path(__file__).resolve().parent / 'runtime/settings.json'
sys.stdout.write(json.loads(settings.read_text())['gateway_key'])
