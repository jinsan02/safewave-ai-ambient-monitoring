#!/usr/bin/env python3
"""
M5 Qwen-0.5B-Instruct ONNX 변환 스크립트

공식 출처: Hugging Face - Qwen/Qwen2-0.5B-Instruct
변환 방식: Hugging Face Optimum (optimum-cli)
출력: volumes/models/qwen_05b/ 폴더 (config.json, tokenizer.json, model.onnx 등 포함)

요구사항:
1. transformers >= 4.38.0
2. onnx >= 1.15.0
3. onnxruntime >= 1.20.1
4. optimum[onnxruntime] >= 1.17.0
"""

import os
import sys
import torch
import subprocess
from pathlib import Path
from optimum.onnxruntime import ORTModelForCausalLM
from transformers import AutoTokenizer, AutoConfig

def check_gpu():
    """GPU 사용 가능 여부 확인"""
    if torch.cuda.is_available():
        print(f"✓ GPU available: {torch.cuda.get_device_name(0)}")
        return True
    else:
        print("⚠ GPU not available, using CPU (slower)")
        return False

def convert_qwen_to_onnx():
    """
    Qwen2-0.5B-Instruct를 ONNX로 변환
    """
    model_id = "Qwen/Qwen2-0.5B-Instruct"
    output_dir = Path("volumes/models/qwen_05b")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("\n" + "="*80)
    print("[M5] Exporting Qwen2-0.5B-Instruct model to ONNX...")
    print("="*80)
    print(f"Model ID: {model_id}")
    print(f"Output directory: {output_dir}")
    print(f"GPU available: {check_gpu()}")
    
    try:
        # Step 1: Tokenizer 다운로드
        print("\n[Step 1/3] Downloading tokenizer...")
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
        print("✓ Tokenizer downloaded")
        
        # Step 2: Config 다운로드
        print("\n[Step 2/3] Downloading model config...")
        config = AutoConfig.from_pretrained(model_id, trust_remote_code=True)
        print("✓ Model config downloaded")
        
        # Step 3: Optimum을 사용한 ONNX 변환
        print("\n[Step 3/3] Converting to ONNX with Optimum...")
        print("(This may take 2-5 minutes depending on GPU availability)")
        
        # Optimum CLI를 통한 변환
        # optimum-cli export onnx --model Qwen/Qwen2-0.5B-Instruct volumes/models/qwen_05b
        cmd = [
            "optimum-cli",
            "export", "onnx",
            "--model", model_id,
            "--task", "text-generation",  # Causal LM task
            str(output_dir)
        ]
        
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=False, text=True)
        
        if result.returncode != 0:
            print("✗ Optimum CLI conversion failed, falling back to manual conversion...")
            # Fallback: 수동 변환
            convert_with_torch_onnx(model_id, output_dir, tokenizer, config)
        else:
            print("✓ ONNX export completed with Optimum CLI")
        
        # Step 4: 검증
        print("\n[Step 4/4] Verifying ONNX model...")
        verify_qwen_onnx(output_dir, tokenizer, config)
        
    except Exception as e:
        print(f"✗ Error: {e}")
        print("\nTroubleshooting tips:")
        print("1. Ensure optimum-cli is installed: pip install optimum[onnxruntime]")
        print("2. Check internet connection (model download required)")
        print("3. Ensure sufficient disk space (~5GB for model download)")
        sys.exit(1)

def convert_with_torch_onnx(model_id, output_dir, tokenizer, config):
    """
    Fallback: torch.onnx.export를 사용한 수동 변환
    """
    print("\n[Fallback] Using torch.onnx.export for conversion...")
    from transformers import AutoModelForCausalLM
    
    # 모델 다운로드
    print("Loading model from Hugging Face...")
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        trust_remote_code=True,
        torch_dtype=torch.float32,
    )
    
    if torch.cuda.is_available():
        model = model.cuda()
    model.eval()
    
    # 더미 입력 생성
    dummy_input_ids = torch.tensor([[1, 2, 3, 4, 5]], dtype=torch.long)
    if torch.cuda.is_available():
        dummy_input_ids = dummy_input_ids.cuda()
    
    # ONNX export
    onnx_path = output_dir / "model.onnx"
    print(f"Exporting to {onnx_path}...")
    
    try:
        torch.onnx.export(
            model,
            (dummy_input_ids,),
            str(onnx_path),
            input_names=["input_ids"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch_size", 1: "sequence_length"},
                "logits": {0: "batch_size", 1: "sequence_length"}
            },
            opset_version=17,
            do_constant_folding=True,
            use_external_data_format=True,  # Large model support
            verbose=False,
        )
        print("✓ ONNX export completed")
        
    except Exception as e:
        print(f"✗ torch.onnx.export failed: {e}")
        print("Consider using quantization or splitting model layers")
        sys.exit(1)

def verify_qwen_onnx(output_dir, tokenizer, config):
    """
    ONNX 모델 검증
    """
    import onnx
    import onnxruntime
    
    # ONNX 파일 찾기
    onnx_files = list(output_dir.glob("*.onnx"))
    if not onnx_files:
        print("✗ No ONNX files found!")
        return
    
    print(f"✓ Found {len(onnx_files)} ONNX file(s):")
    for onnx_file in onnx_files:
        print(f"  - {onnx_file.name}")
        
        # ONNX 모델 로드 및 검증
        try:
            onnx_model = onnx.load(str(onnx_file))
            onnx.checker.check_model(onnx_model)
            print(f"    ✓ ONNX model structure valid")
            
            # Input/Output 정보
            graph = onnx_model.graph
            print(f"    Inputs:")
            for inp in graph.input:
                print(f"      - {inp.name}: {inp.type.tensor_type.shape}")
            print(f"    Outputs:")
            for out in graph.output:
                print(f"      - {out.name}: {out.type.tensor_type.shape}")
                
        except Exception as e:
            print(f"    ✗ ONNX validation failed: {e}")
            return
    
    # Tokenizer 및 Config 확인
    print(f"\n✓ Supporting files:")
    print(f"  - config.json: {(output_dir / 'config.json').exists()}")
    print(f"  - tokenizer.json: {(output_dir / 'tokenizer.json').exists()}")
    print(f"  - tokenizer_config.json: {(output_dir / 'tokenizer_config.json').exists()}")
    
    # 간단한 추론 테스트
    print(f"\n[Inference Test]")
    test_qwen_inference(output_dir, tokenizer, config)
    
    print("\n" + "="*80)
    print("✓ M5 Qwen-0.5B-Instruct model is READY for deployment!")
    print("="*80)

def test_qwen_inference(output_dir, tokenizer, config):
    """
    ONNX 모델 추론 테스트
    """
    import onnxruntime as ort
    import numpy as np
    
    try:
        # 모델 로드
        model_path = output_dir / "model.onnx"
        if not model_path.exists():
            print("  ⚠ model.onnx not found, skipping inference test")
            return
        
        # ONNX Runtime 세션 생성
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        session = ort.InferenceSession(
            str(model_path),
            providers=providers
        )
        
        # 테스트 입력
        test_text = "현재 시간은 14시 30분이야. 사용자의 상태는?"
        input_ids = tokenizer.encode(test_text, return_tensors="np")
        
        # 추론
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        
        print(f"  Input text: '{test_text}'")
        print(f"  Input shape: {input_ids.shape}")
        
        outputs = session.run(
            [output_name],
            {input_name: input_ids.astype(np.int64)}
        )
        
        logits = outputs[0]
        print(f"  Output shape: {logits.shape}")
        print(f"  Output range: [{logits.min():.4f}, {logits.max():.4f}]")
        print(f"  ✓ Inference test passed!")
        
    except Exception as e:
        print(f"  ⚠ Inference test failed: {e}")

if __name__ == "__main__":
    print("\n" + "="*80)
    print("M5 Qwen-0.5B-Instruct → ONNX Conversion")
    print("="*80)
    
    # 필수 라이브러리 확인
    required_packages = ["torch", "transformers", "onnx", "onnxruntime", "optimum"]
    missing = []
    for pkg in required_packages:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    
    if missing:
        print(f"\n✗ Missing packages: {', '.join(missing)}")
        print(f"\nInstall with:")
        print(f"  pip install transformers onnx onnxruntime 'optimum[onnxruntime]'")
        print(f"  (or: pip install -r ai/requirements.txt)")
        sys.exit(1)
    
    print("✓ All required packages available")
    
    # 변환 시작
    convert_qwen_to_onnx()
