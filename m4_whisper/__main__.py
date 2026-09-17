import argparse
import json
from pathlib import Path
import sys

from .runtime import SpeechRecognizer
from .settings import MODELS


def main():
    parser = argparse.ArgumentParser(description='M4 CT2 handoff: strict 16 kHz mono WAV input.')
    parser.add_argument('--variant', choices=MODELS, required=True)
    parser.add_argument('--model-root', type=Path, default=Path('m4_artifacts/ct2'))
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--window-id', default='file')
    parser.add_argument('--start-seconds', type=float, default=0.)
    parser.add_argument('--cpu-threads', type=int, default=6)
    parser.add_argument('--seed', type=int, default=42,
                        help='Set CT2 RNG once for this process, not once per fallback.')
    parser.add_argument('--repetition-policy', choices=['off', 'selection', 'selection_regenerate'],
                        default='selection_regenerate')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        import ctranslate2
        import soundfile as sf
        audio, rate = sf.read(args.audio, dtype='float32')
        ctranslate2.set_random_seed(args.seed)
        recognizer = SpeechRecognizer(args.model_dir or args.model_root / args.variant,
                                      args.variant, args.cpu_threads, args.repetition_policy)
        result = recognizer.transcribe(audio, rate, window_id=args.window_id,
                                       start_seconds=args.start_seconds)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({'schema_version': 1, 'status': 'error',
                          'error_type': type(exc).__name__, 'message': str(exc)},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
