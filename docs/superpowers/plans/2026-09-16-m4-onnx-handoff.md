# M4 ONNX INT8 Handoff Implementation Plan

**Goal:** Deliver the fresh Zeroth-derived fine-tuned model as ONNX INT8 on a team branch, with offline CPU inference and an explicit input/output contract.

**Architecture:** Export the existing HF merged checkpoint using Optimum, quantize constant MatMul weights with ONNX Runtime, and run encoder/decoder sessions through Optimum. Reuse the existing repetition detector and candidate selector; implement the ONNX generation adapter separately from CT2. Preserve the team's current default M4 and original source checkpoint.

**Tech Stack:** Python 3.10, Transformers 4.57.6, Optimum 2.1.0 / optimum-onnx 0.1.0, ONNX 1.19.1, ONNX Runtime 1.23.2 CPU.

**Spec:** User requested ONNX handoff in jinsan02/safewave-ai-ambient-monitoring, based on the fresh fine-tuned model from the 2,398-file CT2 comparison.

## Constraints
- No retraining, default service replacement, raw-audio publication, or CT2-to-ONNX renaming.
- Existing CT2 benchmark numbers are not ONNX benchmark numbers.
- Model weights are release assets, not normal Git objects.
- Selection and one n=3 regeneration preserve raw outputs; no string deduplication.

## Tasks
- [x] Export and dynamic-quantize the exact source checkpoint; save hashes, graph checks, operator counts and version provenance.
- [x] Test candidate selection, one-time regeneration, silence handling, input validation and identity verification before implementing the ONNX runtime adapter.
- [x] Run offline CPU inference on a small existing evaluation sample and synthetic silence; save original outputs and distinguish smoke results from corpus accuracy.
- [x] Document the package, download command, JSON contract, model lineage, decoding differences and integration precautions; do not change ai/main.py defaults.
- [ ] Run scoped tests, inspect staged content for weights/audio/secrets, publish one ONNX INT8 archive and push the team branch.
