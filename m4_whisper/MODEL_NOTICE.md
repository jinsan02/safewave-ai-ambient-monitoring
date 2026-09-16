# Model Provenance and Use Notice

Owner of this handoff: Lee Daegyeong (M4 speech).

Base: https://huggingface.co/seastar105/whisper-small-ko-zeroth
Upstream model card declares Apache-2.0. The base is a Korean fine-tune of
https://huggingface.co/openai/whisper-small (OpenAI Whisper, MIT).
Copies of the upstream license texts accompany each archive.

Changes: one fresh LoRA run from the local Zeroth checkpoint, followed by adapter
merge, then independent CT2 FP32/INT8 conversions. The additional ONNX FP32
graphs are exported from the HF checkpoints, not from the INT8 model.bin files.
The original base weights are also provided as baseline comparison artifacts.
Source hashes and export manifests identify the exact checkpoints.

No AI Hub audio, personal recordings, original data labels, access tokens or
training checkpoints are included. The AI Hub and recording-consent conditions
remain relevant to downstream use. This handoff does not grant new rights to
the training datasets or assert unrestricted commercial rights to derived data.

Research integration candidate, not a certified emergency or medical device.
Speech may be mistranscribed, hallucinated, repeated, or omitted. No emergency
action should rely solely on this transcription. Guard detection is heuristic;
genuine repeated speech can be mistaken for generated repetition.

The historical CT2 benchmark does not certify the ONNX demo or Raspberry Pi
latency. ONNX graphs do not contain the Python repetition guard.
