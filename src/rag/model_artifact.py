"""Pinned public model; validate bytes before ONNX loads, without network."""
import hashlib
import json
from pathlib import Path
from src.core.environment_guard import validate_path

MANIFEST = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))


def verified_artifact(cache):
    folder = validate_path(Path(cache)/MANIFEST['revision'])
    for name, expected in MANIFEST['files'].items():
        file = folder/name
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError('Pinned model missing or changed; run scripts/prepare_model.py explicitly')
    return folder
