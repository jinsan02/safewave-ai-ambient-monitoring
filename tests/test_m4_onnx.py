import numpy as np
import pytest
import torch
from transformers import WhisperConfig, WhisperForConditionalGeneration

from m4_whisper.export_onnx import EncoderGraph, DecoderGraph, export_graphs


def tiny_model():
    config = WhisperConfig(vocab_size=32, num_mel_bins=4, d_model=8,
                           encoder_layers=1, decoder_layers=1,
                           encoder_attention_heads=2, decoder_attention_heads=2,
                           encoder_ffn_dim=16, decoder_ffn_dim=16,
                           max_source_positions=8, max_target_positions=24,
                           pad_token_id=0, bos_token_id=1, eos_token_id=2,
                           decoder_start_token_id=1, suppress_tokens=[],
                           begin_suppress_tokens=[])
    config._attn_implementation = 'eager'
    torch.manual_seed(42)
    return WhisperForConditionalGeneration(config).eval()


def test_graph_wrapper_matches_original_forward():
    model = tiny_model()
    features = torch.randn(1, 4, 16)
    ids = torch.tensor([[1, 3, 4, 5]])
    with torch.no_grad():
        encoded = EncoderGraph(model)(features)
        logits = DecoderGraph(model)(ids, encoded)
        expected = model(input_features=features, decoder_input_ids=ids,
                         use_cache=False).logits[:, -1, :]
    torch.testing.assert_close(logits, expected)


def test_onnx_cpu_matches_pytorch_at_multiple_prefix_lengths(tmp_path):
    ort = pytest.importorskip('onnxruntime')
    pytest.importorskip('onnx')
    model = tiny_model()
    export_graphs(model, tmp_path)
    encoder = ort.InferenceSession(str(tmp_path / 'encoder.onnx'),
                                   providers=['CPUExecutionProvider'])
    decoder = ort.InferenceSession(str(tmp_path / 'decoder.onnx'),
                                   providers=['CPUExecutionProvider'])
    features = np.random.default_rng(42).normal(size=(1, 4, 16)).astype(np.float32)
    hidden = encoder.run(None, {'input_features': features})[0]
    with torch.no_grad():
        expected_hidden = EncoderGraph(model)(torch.from_numpy(features)).numpy()
    np.testing.assert_allclose(hidden, expected_hidden, atol=1e-5, rtol=1e-4)
    for length in [1, 4, 9, 23]:
        ids = (np.arange(length)[None, :] % 20 + 1).astype(np.int64)
        actual = decoder.run(None, {'input_ids': ids, 'encoder_hidden_states': hidden})[0]
        with torch.no_grad():
            expected = DecoderGraph(model)(torch.from_numpy(ids), torch.from_numpy(hidden)).numpy()
        np.testing.assert_allclose(actual, expected, atol=1e-5, rtol=1e-4)
        assert actual.shape == (1, 32)
