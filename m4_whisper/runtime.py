"""CT2 inference for one 16 kHz mono window; no microphone ownership."""

import hashlib
import importlib.metadata
import math
import threading
import time
from pathlib import Path

import numpy as np

from .guard import detect_suspicious_repetition, make_repetition_guard_model_class
from .settings import MODELS, guard_settings, transcribe_options


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_audio(audio, sample_rate):
    if sample_rate != 16000:
        raise ValueError('Expected 16000 Hz; resample at the shared capture boundary.')
    if not isinstance(audio, np.ndarray) or audio.dtype != np.float32:
        raise TypeError('Expected a numpy float32 waveform, not PCM bytes or int16.')
    if audio.ndim != 1 or not 1 <= audio.size <= 480000:
        raise ValueError('Expected mono shape (N,), with 1 <= N <= 480000 (30 seconds).')
    if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1.0:
        raise ValueError('Audio must be finite and normalized to [-1, 1].')
    return np.array(audio, dtype=np.float32, order='C', copy=True)


def render_result(segments, events, window_id, start_seconds, duration,
                  variant, compute_type, elapsed):
    text = ''.join(item.text for item in segments).strip()
    rows = [{
        'text': item.text,
        'start_seconds': start_seconds + float(item.start),
        'end_seconds': start_seconds + float(item.end),
        'timestamp_outside_window': item.start < 0 or item.end > duration,
        'token_ids': list(item.tokens),
        'avg_logprob': float(item.avg_logprob),
        'no_speech_prob': float(item.no_speech_prob),
        'compression_ratio': float(item.compression_ratio),
        'temperature_metadata': float(item.temperature),
    } for item in segments]
    return {
        'schema_version': 1, 'component': 'M4_speech',
        'status': 'ok' if text else 'no_text', 'window_id': window_id,
        'window_start_seconds': start_seconds,
        'window_end_seconds': start_seconds + duration,
        'sample_rate_hz': 16000, 'model': variant,
        'engine': 'faster-whisper/CTranslate2', 'device': 'cpu',
        'compute_type': compute_type, 'language': 'ko',
        'text': text, 'segments': rows, 'elapsed_seconds': elapsed,
        'guard': {
            'mode': events[0]['mode'] if events and 'mode' in events[0] else None,
            'regenerations': sum(bool(e.get('regeneration_attempted')) for e in events),
            'regeneration_successes': sum(bool(e.get('regeneration_suppressed_repetition')) for e in events),
            'low_logprob_rescues': sum(bool(e.get('rescued_low_logprob')) for e in events),
            'unresolved_candidates': sum(bool(e.get('unresolved_repetition')) for e in events),
            'additional_calls': sum(e.get('additional_generation_calls', 0) for e in events),
            'final_repetition_suspected': detect_suspicious_repetition(
                text, [token for item in segments for token in item.tokens],
                special_token_start=50257,
            )['suspected'],
            'selection_reasons': [e.get('selection_reason') for e in events],
        },
    }


class SpeechRecognizer:
    """Load once per worker; transcribe windows serially on this instance."""

    def __init__(self, model_dir, variant, cpu_threads=6,
                 repetition_policy='selection_regenerate'):
        if variant not in MODELS:
            raise ValueError(f'Unknown model variant: {variant}')
        if not isinstance(cpu_threads, int) or cpu_threads < 1:
            raise ValueError('cpu_threads must be a positive integer.')
        expected_versions = {'faster-whisper': '1.2.1', 'ctranslate2': '4.8.1'}
        for name, version in expected_versions.items():
            actual = importlib.metadata.version(name)
            if actual != version:
                raise RuntimeError(f'{name}=={version} required for this guard, found {actual}.')
        compute_type, expected_hash = MODELS[variant]
        model_dir = Path(model_dir).resolve()
        for name in ['config.json', 'tokenizer.json', 'preprocessor_config.json']:
            if not (model_dir / name).is_file():
                raise FileNotFoundError(f'Missing offline model asset: {model_dir / name}')
        if sha256_file(model_dir / 'model.bin') != expected_hash:
            raise ValueError(f'Model hash mismatch for {variant}; do not mix checkpoints.')
        from faster_whisper import WhisperModel
        cls = make_repetition_guard_model_class(WhisperModel)
        self.model = cls(str(model_dir), device='cpu', compute_type=compute_type,
                         cpu_threads=cpu_threads, num_workers=1,
                         local_files_only=True,
                         repetition_guard=guard_settings(repetition_policy))
        self.variant = variant
        self.compute_type = str(self.model.model.compute_type)
        self.repetition_policy = repetition_policy
        self._lock = threading.Lock()

    def transcribe(self, audio, sample_rate=16000, *, window_id='', start_seconds=0.0):
        snapshot = validate_audio(audio, sample_rate)
        if not isinstance(window_id, str):
            raise TypeError('window_id must be a string.')
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError('start_seconds must be finite and nonnegative.')
        with self._lock:
            self.model.repetition_guard_events.clear()
            self.model.repetition_guard_context = {'window_id': window_id}
            begin = time.perf_counter()
            try:
                stream, _ = self.model.transcribe(snapshot, **transcribe_options())
                segments = list(stream)
                events = list(self.model.repetition_guard_events)
            finally:
                # Per-window traces must not grow forever during streaming.
                self.model.repetition_guard_events.clear()
            result = render_result(segments, events, window_id, float(start_seconds),
                                   snapshot.size / sample_rate, self.variant,
                                   self.compute_type, time.perf_counter() - begin)
            result['guard']['mode'] = self.repetition_policy
            return result
