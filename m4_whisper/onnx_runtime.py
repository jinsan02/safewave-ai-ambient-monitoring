"""Offline ONNX CPU ASR with an explicit, window-local repetition policy.

This is an ONNX decoding adapter, not a numerical reproduction of CT2 decoding.
"""

import argparse
from copy import deepcopy
import importlib.metadata
import json
import math
from pathlib import Path
import threading
import time
import zlib

from .guard import detect_suspicious_repetition, select_repetition_aware_candidate
from .runtime import sha256_file, validate_audio

TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
MODES = ('off', 'selection', 'selection_regenerate')


def make_candidate(text, tokens, avg_logprob, no_speech_prob, temperature,
                   cap_reached, candidate_id):
    raw = text.encode('utf-8')
    compression = len(raw) / len(zlib.compress(raw))
    return {
        'candidate_id': candidate_id, 'text': text, 'token_ids': list(tokens),
        'avg_logprob': float(avg_logprob), 'no_speech_prob': float(no_speech_prob),
        'temperature': temperature, 'compression_ratio': compression,
        'compression_pass': compression <= 2.4, 'logprob_pass': avg_logprob >= -1.0,
        'probable_silence': no_speech_prob > 0.6 and avg_logprob < -1.0,
        'empty': not bool(text.strip()), 'generation_cap_reached': bool(cap_reached),
        'repetition': detect_suspicious_repetition(text, tokens, special_token_start=50257),
    }


def finish_candidates(candidates, stock, mode, regenerate):
    if mode not in MODES:
        raise ValueError(f'Unknown policy: {mode}')
    event = {'mode': mode, 'additional_calls': 0, 'regeneration_success': False,
             'rescued_low_logprob': False, 'selection_reason': 'off',
             'unresolved_repetition': stock['repetition']['suspected']}
    chosen = stock
    if stock['probable_silence']:
        event['selection_reason'] = 'preserve_no_speech_skip'
    elif mode != 'off':
        decision = select_repetition_aware_candidate(candidates, stock)
        chosen = decision['candidate']
        event.update(selection_reason=decision['reason'],
                     rescued_low_logprob=decision['rescued_low_logprob'])
        if decision['needs_regeneration'] and mode == 'selection_regenerate':
            replacement = regenerate()
            event.update(additional_calls=1, regeneration_candidate=replacement)
            eligible = (not replacement['empty'] and not replacement['generation_cap_reached']
                        and not replacement['probable_silence'] and replacement['compression_pass']
                        and not replacement['repetition']['suspected'])
            if eligible:
                chosen = replacement
                event.update(regeneration_success=True, selection_reason='one_time_ngram_3_regeneration')
    event['unresolved_repetition'] = chosen['repetition']['suspected']
    event['selected_candidate_id'] = chosen['candidate_id']
    return chosen, event


def restore_sampling_scores(scores, temperature):
    return tuple(score * temperature for score in scores) if temperature > 0 else scores


def run_fallback(generate, mode):
    pool, best_per_attempt, attempts = [], [], []
    stock_stop_after = None
    for attempt, temperature in enumerate(TEMPERATURES):
        current, returned = generate(temperature, 0, f'fallback-{attempt}')
        pool.extend(returned)
        best_per_attempt.append(current)
        attempts.append({'kind': 'fallback', 'temperature': temperature,
                         'candidates': returned, 'best_id': current['candidate_id']})
        stock = current
        passes = current['compression_pass'] and current['logprob_pass']
        if stock_stop_after is None and (passes or current['probable_silence']):
            stock_stop_after = len(attempts)
        if current['probable_silence']:
            break
        if passes and (mode == 'off' or not current['repetition']['suspected']):
            break
        if mode != 'off' and current['repetition']['suspected']:
            if select_repetition_aware_candidate(pool, current)['changed']:
                break
    else:
        allowed = [c for c in best_per_attempt if c['compression_pass']]
        stock = max(allowed or best_per_attempt, key=lambda c: c['avg_logprob'])

    def regenerate():
        new, returned = generate(0.0, 3, 'regenerate')
        attempts.append({'kind': 'regeneration', 'temperature': 0.0,
                         'candidates': returned, 'best_id': new['candidate_id']})
        return new

    selected, guard = finish_candidates(pool, stock, mode, regenerate)
    guard['generation_calls'] = len(attempts)
    guard['calls_after_first_stock_stop'] = (
        len(attempts) - stock_stop_after if stock_stop_after is not None else guard['additional_calls'])
    return selected, guard, attempts


class OnnxSpeechRecognizer:
    """Load once, share captured mono float32 windows, serialize inference per instance."""

    def __init__(self, model_dir, cpu_threads=6, repetition_policy='selection_regenerate'):
        if repetition_policy not in MODES:
            raise ValueError(f'Unknown policy: {repetition_policy}')
        if not isinstance(cpu_threads, int) or cpu_threads < 1:
            raise ValueError('cpu_threads must be a positive integer')
        for name, expected in [('transformers', '4.57.6'), ('optimum', '2.1.0'),
                               ('optimum-onnx', '0.1.0'), ('onnxruntime', '1.23.2')]:
            actual = importlib.metadata.version(name)
            if actual != expected:
                raise RuntimeError(f'{name}=={expected} required; found {actual}')
        from .export_onnx_int8 import SOURCE_SHA256, GRAPH_NAMES
        model_dir = Path(model_dir).resolve()
        manifest = json.loads((model_dir / 'onnx_manifest.json').read_text(encoding='utf-8'))
        if manifest.get('source_weight_sha256') != SOURCE_SHA256:
            raise ValueError('Wrong model lineage')
        for name in (*GRAPH_NAMES, 'config.json', 'generation_config.json',
                     'tokenizer.json', 'tokenizer_config.json', 'preprocessor_config.json'):
            if sha256_file(model_dir / name) != manifest['files'][name]['sha256']:
                raise ValueError(f'Model asset hash mismatch: {name}')
        import onnxruntime as ort
        import torch
        from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
        from transformers import WhisperProcessor
        options = ort.SessionOptions()
        options.intra_op_num_threads = cpu_threads
        options.inter_op_num_threads = 1
        self.model = ORTModelForSpeechSeq2Seq.from_pretrained(
            model_dir, local_files_only=True, provider='CPUExecutionProvider',
            session_options=options, use_cache=True, use_merged=False, use_io_binding=False)
        self.processor = WhisperProcessor.from_pretrained(model_dir, local_files_only=True)
        self.policy = repetition_policy
        self.model_id = manifest['model_id']
        self.lock = threading.Lock()
        self.torch = torch
        t = self.processor.tokenizer
        self.prefix = [t.convert_tokens_to_ids(x) for x in
                       ['<|startoftranscript|>', '<|ko|>', '<|transcribe|>', '<|notimestamps|>']]
        self.no_speech_id = t.convert_tokens_to_ids('<|nocaptions|>')
        if self.no_speech_id != 50362:
            raise ValueError('Unexpected Whisper no-speech token ID')
        self.eos = self.model.config.eos_token_id
        self.max_length = self.model.config.max_target_positions

    def _generate(self, hidden, no_speech, temperature, ngram, run_id):
        from transformers.generation.utils import GenerationMixin
        from transformers.modeling_outputs import BaseModelOutput
        torch = self.torch
        config = deepcopy(self.model.generation_config)
        config.forced_decoder_ids = None
        config.forced_eos_token_id = None
        config.max_length = self.max_length
        config.max_new_tokens = None
        config.use_cache = True
        config.do_sample = temperature > 0
        config.temperature = temperature if config.do_sample else 1.0
        config.num_beams = 1 if config.do_sample else 5
        config.num_return_sequences = 5 if config.do_sample else 1
        config.top_k = 0 if config.do_sample else 50
        config.top_p = 1.0
        config.repetition_penalty = 1.0
        config.no_repeat_ngram_size = ngram
        config.length_penalty = 1.0
        # This handoff emits window-level text, not timestamp-token segmentation.
        config.suppress_tokens = sorted(set(config.suppress_tokens or []) |
                                        set(range(50364, self.model.config.vocab_size)))
        config.begin_suppress_tokens = [220, self.eos]
        result = GenerationMixin.generate(
            self.model, encoder_outputs=BaseModelOutput(last_hidden_state=hidden.clone()),
            attention_mask=torch.ones((1, hidden.shape[1] * 2), dtype=torch.long),
            decoder_attention_mask=torch.ones((1, len(self.prefix)), dtype=torch.long),
            decoder_input_ids=torch.tensor([self.prefix], dtype=torch.long),
            generation_config=config, return_dict_in_generate=True, output_scores=True)
        scores = self.model.compute_transition_scores(
            result.sequences, restore_sampling_scores(result.scores, temperature),
            getattr(result, 'beam_indices', None),
            normalize_logits=True)
        candidates = []
        for index, seq in enumerate(result.sequences):
            tokens = seq[len(self.prefix):].tolist()
            eos_seen = self.eos in tokens
            if eos_seen:
                tokens = tokens[:tokens.index(self.eos) + 1]
            score = float(scores[index, :len(tokens)].mean()) if tokens else float('-inf')
            text = self.processor.tokenizer.decode(tokens, skip_special_tokens=True)
            item = make_candidate(text, tokens, score, no_speech, temperature,
                                  not eos_seen and len(seq) >= self.max_length,
                                  candidate_id=f'{run_id}:{index}')
            item.update(eos_observed=eos_seen, termination='eos' if eos_seen else 'length_limit',
                        no_repeat_ngram_size=ngram, num_beams=config.num_beams,
                        num_return_sequences=config.num_return_sequences)
            candidates.append(item)
        return max(candidates, key=lambda c: c['avg_logprob']), candidates

    def transcribe(self, audio, sample_rate=16000, *, window_id=None, start_seconds=0.0):
        waveform = validate_audio(audio, sample_rate)
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError('start_seconds must be finite and nonnegative')
        with self.lock, self.torch.inference_mode():
            started = time.perf_counter()
            features = self.processor.feature_extractor(waveform, sampling_rate=16000,
                                                        return_tensors='pt').input_features
            hidden = self.model.encoder(input_features=features, attention_mask=None).last_hidden_state
            # Observe the actual no-speech token at SOT, before language/task context.
            first = self.model.decoder(input_ids=self.torch.tensor([[self.prefix[0]]]),
                                       encoder_hidden_states=hidden, use_cache=False)
            no_speech = float(first.logits[0, 0].softmax(-1)[self.no_speech_id])
            selected, guard, attempts = run_fallback(
                lambda temp, ngram, run_id: self._generate(hidden, no_speech, temp, ngram, run_id),
                self.policy)
            # Silence is the existing two-score rule, never a repetition workaround.
            skipped = selected['probable_silence']
            text = '' if skipped else selected['text']
            return {
                'schema_version': 1, 'component': 'M4_speech', 'model': self.model_id,
                'engine': 'ONNX Runtime / Optimum', 'device': 'cpu',
                'quantization': 'dynamic_int8_matmul', 'language': 'ko',
                'status': 'no_speech' if skipped else 'ok' if text.strip() else 'no_text',
                'window_id': window_id, 'window_start_seconds': start_seconds,
                'window_end_seconds': start_seconds + len(waveform) / sample_rate,
                'sample_rate_hz': sample_rate, 'text': text, 'transcript_ko': text,
                'timestamps': None, 'confidence': None,
                'avg_logprob': selected['avg_logprob'], 'no_speech_prob': no_speech,
                'raw_selected_candidate': selected, 'skipped_as_no_speech': skipped,
                'guard': guard, 'attempts': attempts,
                'elapsed_seconds': time.perf_counter() - started,
                'ct2_benchmark_applies': False,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cpu-threads', type=int, default=6)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--repetition-policy', choices=MODES, default='selection_regenerate')
    args = parser.parse_args()
    import soundfile as sf
    import torch
    torch.set_num_threads(args.cpu_threads)
    torch.manual_seed(args.seed)
    waveform, rate = sf.read(args.audio, dtype='float32')
    recognizer = OnnxSpeechRecognizer(args.model_dir, args.cpu_threads, args.repetition_policy)
    result = recognizer.transcribe(waveform, rate, window_id=args.audio.stem)
    result['input_sha256'] = sha256_file(args.audio)
    result['seed'] = args.seed
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()
