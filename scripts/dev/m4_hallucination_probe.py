#!/usr/bin/env python3
"""M4 환각("MBC 뉴스 ○○○입니다" 등) 재현·원인 조사 — 운영과 같은 M4 경로(WhisperSmallModel.infer)에
무음·약한 잡음·실제 생활 소음·실제 음성을 넣고, 전사와 함께 두 지표를 기록한다.
  no_speech_prob : Whisper가 첫 위치에서 '말 없음'(<|nocaptions|>) 토큰에 준 확률
  avg_logprob    : 생성한 토큰 로그확률 평균(= 실제 인식 확신도 후보)
생활 소음 구간은 sensing과 같은 규칙(peak < 0.12면 peak 0.85로 증폭)으로 증폭본도 넣는다.
ai-experts 이미지 안에서 실행:
  docker run --rm --gpus all -e ORT_USE_GPU=1 -v C:/rp5:/repo -v C:/rp5/volumes/models:/app/models -w /repo \\
    --entrypoint python3 rp5-ai-experts-gpu scripts/dev/m4_hallucination_probe.py --model whisper_onnx_int8_ft_svc
"""
import argparse
import csv
import glob
import json
import os
import random
import sys
import wave
from collections import Counter

sys.path.insert(0, os.getenv("APP_DIR", "/app"))
import numpy as np  # noqa: E402
import torch  # noqa: E402

from experts.m4_whisper_small import WhisperSmallModel  # noqa: E402

SR = 16000


def load_wav(path):
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def amplify_like_sensing(x, target=0.85, below=0.12):
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak <= 1e-9 or peak >= below:
        return x
    return np.clip(x * (target / peak), -1.0, 1.0).astype(np.float32)


def dbfs(x):
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return round(20 * np.log10(peak), 1) if peak > 0 else -120.0


class Probe:
    def __init__(self, model_dir):
        self.m4 = WhisperSmallModel(model_dir)
        self.m4._init_asr_pipeline()
        pipe = self.m4.asr_pipe
        self.model, self.fe, self.tok = pipe.model, pipe.feature_extractor, pipe.tokenizer
        self.sot = self.tok.convert_tokens_to_ids("<|startoftranscript|>")
        ids = [self.tok.convert_tokens_to_ids(t) for t in ("<|nocaptions|>", "<|nospeech|>")]
        self.nospeech = next(i for i in ids if i is not None and i != self.tok.unk_token_id)

    def run(self, x):
        out = self.m4.infer({"waveform": x, "sample_rate": SR})     # 운영과 같은 경로
        feats = self.fe(x, sampling_rate=SR, return_tensors="pt").input_features.to(self.model.device)
        with torch.no_grad():
            first = self.model(input_features=feats,
                               decoder_input_ids=torch.tensor([[self.sot]], device=self.model.device))
            p_nospeech = float(torch.softmax(first.logits[0, -1].float(), -1)[self.nospeech])
            gen = self.model.generate(feats, language="ko", task="transcribe", num_beams=1,
                                      max_new_tokens=48, return_dict_in_generate=True, output_scores=True)
            scores = self.model.compute_transition_scores(gen.sequences, gen.scores, normalize_logits=True)
            lp = scores[0][torch.isfinite(scores[0])]
            avg_lp = float(lp.mean()) if lp.numel() else float("nan")
        return out["transcript_ko"], round(p_nospeech, 4), round(avg_lp, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="whisper_onnx_int8_ft_svc")
    ap.add_argument("--ambient", default="data/m3_eval_v34/ambient/20260911")
    ap.add_argument("--speech", default="data/m4_eval_2398")
    ap.add_argument("--n", type=int, default=40, help="생활 소음 구간 수(길이별)")
    ap.add_argument("--out", default="reports/laptop/m4_hallu_probe")
    args = ap.parse_args()
    rng = random.Random(924)
    probe = Probe(os.path.join(os.getenv("MODEL_PATH", "/app/models"), args.model))

    clips = []   # (set, variant, source, waveform)
    for dur in (0.4, 1.5, 3.0):
        n = int(SR * dur)
        clips.append(("synthetic", "silence", f"{dur}s", np.zeros(n, np.float32)))
        noise = np.random.default_rng(1).normal(0, 10 ** (-60 / 20), n).astype(np.float32)
        clips.append(("synthetic", "noise_-60dB_raw", f"{dur}s", noise))
        clips.append(("synthetic", "noise_-60dB_amplified", f"{dur}s", amplify_like_sensing(noise)))
    amb = sorted(glob.glob(os.path.join(args.ambient, "*.wav")))
    for dur in (0.4, 1.5):
        n = int(SR * dur)
        for _ in range(args.n):
            path = rng.choice(amb)
            x = load_wav(path)
            s = rng.randrange(0, len(x) - n)
            seg = x[s:s + n]
            src = f"{os.path.basename(path)}@{s / SR:.1f}s/{dur}s"
            clips.append(("ambient", "raw", src, seg))
            clips.append(("ambient", "amplified", src, amplify_like_sensing(seg)))
    rows = list(csv.DictReader(open(os.path.join(args.speech, "manifest.csv"), encoding="utf-8-sig")))
    for label in ("help_direct", "fall_related"):
        for r in rng.sample([r for r in rows if r["label"] == label], 20):
            clips.append(("speech", label, r["transcript"], load_wav(os.path.join(args.speech, r["wav_path"]))))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    results = []
    with open(args.out + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["set", "variant", "source", "dur_s", "peak_dbfs", "transcript", "no_speech_prob", "avg_logprob"])
        for st, var, src, x in clips:
            text, pns, alp = probe.run(x)
            row = [st, var, src, round(len(x) / SR, 2), dbfs(x), text, pns, alp]
            w.writerow(row)
            results.append(row)
    summary = {}
    for st, var in sorted({(r[0], r[1]) for r in results}):
        sub = [r for r in results if r[0] == st and r[1] == var]
        nonempty = [r for r in sub if r[5].strip()]
        summary[f"{st}/{var}"] = {
            "n": len(sub), "nonempty": len(nonempty),
            "no_speech_prob_median": float(np.median([r[6] for r in sub])),
            "avg_logprob_median": float(np.nanmedian([r[7] for r in sub])),
            "top_texts": Counter(r[5] for r in nonempty).most_common(6),
        }
    json.dump(summary, open(args.out + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for k, v in summary.items():
        print(k, json.dumps(v, ensure_ascii=False))


if __name__ == "__main__":
    main()
