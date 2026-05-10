import os
import re
import numpy as np
import onnxruntime as ort


class QwenLogic:
    """
    M5: Qwen-0.5B를 사용한 고급 위험도 평가 엔진
    
    Qwen2-0.5B-Instruct ONNX 모델을 활용하여 M1-M4(낙상, 생체신호, 활동, 음성)
    의 결과를 분석하고 상황에 맞는 위험도 점수를 생성합니다.
    
    역할:
    - M1-M4 전문가 모델의 출력을 통합 분석
    - 시간 시리즈 맥락 반영
    - 응급 상황 감지 및 위험도 평가
    """
    
    def __init__(self, model_path):
        """
        Args:
            model_path: Qwen ONNX 모델 경로
                       - 폴더면: model.onnx, config.json, tokenizer.json 포함
                       - 파일면: ONNX 모델 파일 경로
        """
        self.model_path = model_path
        self.session = None
        self.tokenizer = None
        
        # 폴더인지 파일인지 확인
        if os.path.isdir(self.model_path):
            onnx_file = os.path.join(self.model_path, "model.onnx")
        else:
            onnx_file = self.model_path
        
        # ONNX 모델 로드
        if os.path.exists(onnx_file):
            self._load_model(onnx_file, self.model_path if os.path.isdir(self.model_path) else None)
    
    def _load_model(self, onnx_path, model_dir=None):
        """ONNX 모델 및 토크나이저 로드"""
        try:
            # ONNX Runtime 세션 생성
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            session_opts = ort.SessionOptions()
            session_opts.intra_op_num_threads = 4
            session_opts.inter_op_num_threads = 2
            
            self.session = ort.InferenceSession(
                onnx_path,
                providers=providers,
                sess_options=session_opts
            )
            print(f"[Qwen] ONNX model loaded: {onnx_path}")
            
            # 토크나이저 로드
            if model_dir and os.path.exists(os.path.join(model_dir, "tokenizer.json")):
                try:
                    from transformers import AutoTokenizer
                    self.tokenizer = AutoTokenizer.from_pretrained(
                        model_dir,
                        trust_remote_code=True
                    )
                    print(f"[Qwen] Tokenizer loaded from {model_dir}")
                except Exception as e:
                    print(f"[Qwen] Tokenizer load failed: {e}, using fallback logic")
                    self.tokenizer = None
            
        except Exception as e:
            print(f"[Qwen] Model loading failed: {e}")
            self.session = None
    
    def _build_analysis_prompt(self, expert_results, context_window=None):
        """
        M1-M4 결과를 Qwen이 이해하는 분석 프롬프트로 변환
        """
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        activity = expert_results.get("activity", {})
        occupancy = expert_results.get("occupancy", {})
        
        # 타임스탐프
        timestamp = ""
        if context_window and context_window.get("current_time"):
            timestamp = f"[{context_window['current_time']}] "
        
        # 프롬프트 구성
        prompt = f"""{timestamp}센서 데이터 분석 요청:

[현재 상황 정보]
- 낙상 감지: {fall.get('fall_detected', False)} (신뢰도: {fall.get('fall_score', 0):.1%})
- 심박수: {vital.get('heart_rate', 0):.0f} bpm (정상: 60-100)
- 호흡수: {vital.get('breathing_rate', 0):.0f} bpm (정상: 12-20)
- 활동 상태: {activity.get('activity', 'unknown')} 
- 점유 신호: {occupancy.get('occupancy_score', 0):.1%}

[질문]
위 정보를 바탕으로 현재 상황의 응급 정도를 0(정상)부터 1(긴급)까지 점수로 답해줘.
응답은 반드시 0.xx 형태의 숫자 하나만 제공해줘."""
        
        return prompt
    
    def _extract_risk_score(self, response_text):
        """
        응답에서 위험도 점수 추출
        """
        # 첫 번째: 0~1 사이의 소수 찾기
        match = re.search(r'0\.\d+|1\.0|1', response_text.strip())
        if match:
            try:
                score = float(match.group())
                return float(np.clip(score, 0.0, 1.0))
            except:
                pass
        
        # 두 번째: 텍스트 기반 휴리스틱
        text_lower = response_text.lower()
        if "긴급" in text_lower or "응급" in text_lower or "즉시" in text_lower:
            return 0.85
        elif "경고" in text_lower or "주의" in text_lower or "주의필요" in text_lower:
            return 0.65
        elif "정상" in text_lower or "안전" in text_lower or "이상없" in text_lower:
            return 0.2
        
        # 기본값
        return 0.5
    
    def _evaluate_with_qwen(self, prompt_text):
        """
        Qwen ONNX 모델을 사용한 응답 생성 (간단한 버전)
        
        참고: 실제 LLM 추론은 복잡하므로, 여기서는 규칙 기반 폴백 사용
        """
        if not self.session or not self.tokenizer:
            return None
        
        try:
            # 토크나이징
            inputs = self.tokenizer(
                prompt_text,
                return_tensors="np",
                truncation=True,
                max_length=512
            )
            
            input_ids = inputs["input_ids"].astype(np.int64)
            
            # 추론 (단일 forward pass)
            input_name = self.session.get_inputs()[0].name
            outputs = self.session.run(None, {input_name: input_ids})
            
            logits = outputs[0]
            # 마지막 토큰의 최대 확률 토큰 선택
            next_token_id = np.argmax(logits[0, -1, :])
            
            # 디코딩
            response = self.tokenizer.decode([next_token_id], skip_special_tokens=True)
            return response
            
        except Exception as e:
            print(f"[Qwen] Inference error: {e}")
            return None
    
    def _evaluate_fallback(self, expert_results):
        """
        Qwen 모델이 없을 때 사용할 규칙 기반 평가
        """
        fall = expert_results.get("fall", {})
        vital = expert_results.get("vital", {})
        activity = expert_results.get("activity", {})
        
        risk = 0.0
        
        # 낙상 감지: 매우 높은 위험
        if fall.get("fall_detected", False):
            risk = max(risk, 0.9)
        else:
            risk += fall.get("fall_score", 0.0) * 0.3
        
        # 생체신호 이상: 중간~높은 위험
        hr = float(vital.get("heart_rate", 70.0))
        rr = float(vital.get("breathing_rate", 16.0))
        
        if hr < 50 or hr > 120 or rr < 10 or rr > 30:
            risk = max(risk, 0.75)
        elif hr < 60 or hr > 100 or rr < 12 or rr > 25:
            risk = max(risk, 0.55)
        
        # 활동 상태: 보조 지표
        activity_type = activity.get("activity", "unknown")
        if activity_type == "lying":
            risk = max(risk, risk + 0.1)  # 누워있는 상태면 약간 상향
        
        return float(np.clip(risk, 0.0, 1.0))
    
    def _apply_context_window(self, risk_score, context_window):
        """
        시간 시리즈 맥락 적용
        
        최근 연속 경고/긴급 상태를 고려하여 위험도 조정
        """
        if not context_window:
            return risk_score
        
        critical_count = int(context_window.get("recent_critical_count", 0))
        warning_count = int(context_window.get("recent_warning_count", 0))
        
        # 최근 긴급 상태가 연속이면 높음
        if critical_count > 0:
            risk_score = max(risk_score, 0.85)
        elif critical_count > 1:
            risk_score = max(risk_score, 0.9)
        
        # 최근 경고가 3번 이상 연속이면 위험도 상향
        if warning_count >= 3:
            risk_score = min(1.0, risk_score + 0.1)
        
        return float(np.clip(risk_score, 0.0, 1.0))
    
    def evaluate(self, expert_results, context_window=None):
        """
        최종 위험도 평가
        
        Args:
            expert_results: M1-M4 전문가 모델의 결과
            context_window: 시간 시리즈 맥락 (최근 경고/긴급 카운트 등)
        
        Returns:
            {
                "emergency": bool,
                "risk_level": "normal" | "warning" | "critical",
                "risk_score": float (0-1),
                "experts": dict,
                "context_used": bool,
                "qwen_response": str (optional)
            }
        """
        
        # Qwen 또는 폴백 규칙으로 위험도 계산
        if self.session and self.tokenizer:
            # Qwen 모델 사용
            prompt = self._build_analysis_prompt(expert_results, context_window)
            qwen_response = self._evaluate_with_qwen(prompt)
            
            if qwen_response:
                risk_score = self._extract_risk_score(qwen_response)
            else:
                # Qwen 추론 실패시 폴백
                risk_score = self._evaluate_fallback(expert_results)
                qwen_response = None
        else:
            # Qwen 없으면 규칙 기반 평가
            risk_score = self._evaluate_fallback(expert_results)
            qwen_response = None
        
        # 시간 맥락 적용
        risk_score = self._apply_context_window(risk_score, context_window)
        
        # 위험 레벨 분류
        if risk_score >= 0.85:
            level = "critical"
        elif risk_score >= 0.6:
            level = "warning"
        else:
            level = "normal"
        
        # 응답 구성
        result = {
            "emergency": risk_score >= 0.6,
            "risk_level": level,
            "risk_score": round(risk_score, 4),
            "experts": expert_results,
            "context_used": bool(context_window),
        }
        
        if qwen_response:
            result["qwen_response"] = qwen_response
        
        return result
