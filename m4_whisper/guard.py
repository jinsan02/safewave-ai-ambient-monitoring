import unicodedata
import zlib
from typing import Any, Sequence


class RepetitionDetectorSettings:
    def __init__(
        self,
        min_repetitions: int = 3,
        single_item_min_repetitions: int = 4,
        min_repeated_fraction: float = 0.6,
        trailing_min_repetitions: int = 4,
        token_min_repetitions: int = 4,
        max_trailing_gap_items: int = 1,
        max_text_unit_chars: int = 12,
        max_token_unit_length: int = 8,
    ) -> None:
        self.min_repetitions = min_repetitions
        self.single_item_min_repetitions = single_item_min_repetitions
        self.min_repeated_fraction = min_repeated_fraction
        self.trailing_min_repetitions = trailing_min_repetitions
        self.token_min_repetitions = token_min_repetitions
        self.max_trailing_gap_items = max_trailing_gap_items
        self.max_text_unit_chars = max_text_unit_chars
        self.max_token_unit_length = max_token_unit_length

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class RepetitionGuardSettings:
    MODES = {"off", "selection", "selection_regenerate"}

    def __init__(
        self,
        mode: str = "off",
        detector: RepetitionDetectorSettings | None = None,
        regeneration_no_repeat_ngram_size: int = 4,
        generation_cap_tokens: int = 224,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"Unsupported repetition guard mode: {mode}")
        if regeneration_no_repeat_ngram_size < 2:
            raise ValueError("regeneration_no_repeat_ngram_size must be at least 2")
        self.mode = mode
        self.detector = detector or RepetitionDetectorSettings()
        self.regeneration_no_repeat_ngram_size = regeneration_no_repeat_ngram_size
        self.generation_cap_tokens = generation_cap_tokens

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def regeneration_enabled(self) -> bool:
        return self.mode == "selection_regenerate"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "detector": self.detector.as_dict(),
            "regeneration_no_repeat_ngram_size": self.regeneration_no_repeat_ngram_size,
            "generation_cap_tokens": self.generation_cap_tokens,
        }


def _minimum_repetitions(unit_length: int, settings: RepetitionDetectorSettings) -> int:
    if unit_length == 1:
        return settings.single_item_min_repetitions
    return settings.min_repetitions


def _find_repeat_regions(
    items: Sequence[Any],
    source: str,
    max_unit_length: int,
    settings: RepetitionDetectorSettings,
    minimum_repetitions_override: int | None = None,
) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    item_count = len(items)
    for unit_length in range(1, min(max_unit_length, item_count // 2) + 1):
        required = (
            minimum_repetitions_override
            if minimum_repetitions_override is not None
            else _minimum_repetitions(unit_length, settings)
        )
        if unit_length * required > item_count:
            continue
        for start in range(item_count - unit_length * required + 1):
            unit = list(items[start : start + unit_length])
            repeat_count = 1
            cursor = start + unit_length
            while cursor + unit_length <= item_count:
                if list(items[cursor : cursor + unit_length]) != unit:
                    break
                repeat_count += 1
                cursor += unit_length
            if repeat_count < required:
                continue
            repeated_items = repeat_count * unit_length
            coverage = repeated_items / item_count if item_count else 0.0
            trailing_gap = item_count - cursor
            trailing = trailing_gap == 0
            near_tail = trailing_gap <= settings.max_trailing_gap_items
            qualifies = coverage >= settings.min_repeated_fraction or (
                near_tail and repeat_count >= settings.trailing_min_repetitions
            )
            if not qualifies:
                continue
            regions.append(
                {
                    "source": source,
                    "unit": unit,
                    "unit_length": unit_length,
                    "repeat_count": repeat_count,
                    "start": start,
                    "end": cursor,
                    "repeated_items": repeated_items,
                    "total_items": item_count,
                    "coverage": coverage,
                    "trailing": trailing,
                    "trailing_gap": trailing_gap,
                    "reason": (
                        "repeated_fraction"
                        if coverage >= settings.min_repeated_fraction
                        else "trailing_repetition"
                        if trailing
                        else "near_tail_repetition"
                    ),
                }
            )
    return regions


def _compact_text(text: str) -> tuple[list[str], list[int]]:
    characters = []
    original_indexes = []
    for index, character in enumerate(text):
        category = unicodedata.category(character)
        if category.startswith("L") or category.startswith("N"):
            characters.append(character.lower())
            original_indexes.append(index)
    return characters, original_indexes


def _region_rank(region: dict[str, Any]) -> tuple[float, int, int, int]:
    return (
        region["coverage"],
        region["repeated_items"],
        region["repeat_count"],
        region["unit_length"],
    )


def detect_suspicious_repetition(
    text: str,
    token_ids: Sequence[int] | None = None,
    special_token_start: int | None = None,
    settings: RepetitionDetectorSettings | None = None,
) -> dict[str, Any]:
    settings = settings or RepetitionDetectorSettings()
    compact, original_indexes = _compact_text(text)
    regions = _find_repeat_regions(
        compact, "text", settings.max_text_unit_chars, settings
    )
    for region in regions:
        region["unit"] = "".join(region["unit"])
        region["original_start"] = original_indexes[region["start"]]
        region["original_end"] = original_indexes[region["end"] - 1] + 1
        region["original_region"] = text[
            region["original_start"] : region["original_end"]
        ]

    generated_tokens = [int(token) for token in (token_ids or [])]
    if special_token_start is None:
        speech_tokens = generated_tokens
    else:
        speech_tokens = [token for token in generated_tokens if token < special_token_start]
    if speech_tokens:
        regions.extend(
            _find_repeat_regions(
                speech_tokens,
                "token",
                settings.max_token_unit_length,
                settings,
                settings.token_min_repetitions,
            )
        )

    best = max(regions, key=_region_rank) if regions else None
    return {
        "input_text": text,
        "generated_token_ids": generated_tokens,
        "speech_token_ids": speech_tokens,
        "suspected": best is not None,
        "best": best,
        "regions": sorted(regions, key=_region_rank, reverse=True),
        "settings": settings.as_dict(),
    }


def _valid_non_repetitive_candidate(candidate: dict[str, Any]) -> bool:
    return bool(
        not candidate.get("empty")
        and not candidate.get("generation_cap_reached")
        and not candidate.get("probable_silence")
        and candidate.get("compression_pass", True)
        and not candidate["repetition"]["suspected"]
    )


def select_repetition_aware_candidate(
    candidates: list[dict[str, Any]], stock_candidate: dict[str, Any]
) -> dict[str, Any]:
    if not stock_candidate["repetition"]["suspected"]:
        return {
            "candidate": stock_candidate,
            "changed": False,
            "rescued_low_logprob": False,
            "needs_regeneration": False,
            "reason": "stock_candidate_not_repetitive",
        }

    non_repetitive = [
        candidate for candidate in candidates if _valid_non_repetitive_candidate(candidate)
    ]
    if non_repetitive:
        selected = max(non_repetitive, key=lambda item: item["avg_logprob"])
        return {
            "candidate": selected,
            "changed": selected is not stock_candidate,
            "rescued_low_logprob": not selected.get("logprob_pass", True),
            "needs_regeneration": False,
            "reason": "rescued_non_repetitive_candidate",
        }

    return {
        "candidate": stock_candidate,
        "changed": False,
        "rescued_low_logprob": False,
        "needs_regeneration": True,
        "reason": "no_valid_non_repetitive_candidate",
    }


def _compression_ratio(text: str) -> float:
    encoded = text.encode("utf-8")
    return len(encoded) / len(zlib.compress(encoded))


def _candidate_for_result(
    result: Any,
    temperature: float,
    tokenizer: Any,
    options: Any,
    guard: RepetitionGuardSettings,
    candidate_id: int,
    source: str = "fallback",
) -> dict[str, Any]:
    tokens = [int(token) for token in result.sequences_ids[0]]
    sequence_length = len(tokens)
    cumulative_logprob = float(result.scores[0]) * (
        sequence_length ** options.length_penalty
    )
    avg_logprob = cumulative_logprob / (sequence_length + 1)
    text = tokenizer.decode(tokens).strip()
    compression_ratio = _compression_ratio(text)
    compression_pass = (
        options.compression_ratio_threshold is None
        or compression_ratio <= options.compression_ratio_threshold
    )
    logprob_pass = (
        options.log_prob_threshold is None
        or avg_logprob >= options.log_prob_threshold
    )
    probable_silence = bool(
        options.no_speech_threshold is not None
        and float(result.no_speech_prob) > options.no_speech_threshold
        and options.log_prob_threshold is not None
        and avg_logprob < options.log_prob_threshold
    )
    needs_fallback = (not compression_pass or not logprob_pass) and not probable_silence
    return {
        "candidate_id": candidate_id,
        "source": source,
        "result": result,
        "actual_temperature": float(temperature),
        "text": text,
        "token_ids": tokens,
        "avg_logprob": avg_logprob,
        "compression_ratio": compression_ratio,
        "compression_pass": compression_pass,
        "logprob_pass": logprob_pass,
        "probable_silence": probable_silence,
        "needs_fallback": needs_fallback,
        "empty": not bool(text),
        "generation_cap_reached": sequence_length >= guard.generation_cap_tokens,
        "repetition": detect_suspicious_repetition(
            text,
            tokens,
            special_token_start=int(tokenizer.eot),
            settings=guard.detector,
        ),
    }


def _candidate_log(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if key != "result"}


def _generation_kwargs(
    options: Any,
    prompt_length: int,
    temperature: float,
    no_repeat_ngram_size: int,
    model_max_length: int,
    time_precision: float,
) -> dict[str, Any]:
    max_initial_timestamp_index = int(
        round(options.max_initial_timestamp / time_precision)
    )
    if options.max_new_tokens is not None:
        max_length = prompt_length + options.max_new_tokens
    else:
        max_length = model_max_length
    if max_length > model_max_length:
        raise ValueError(
            f"The combined prompt and generation length {max_length} exceeds "
            f"the Whisper model max_length {model_max_length}."
        )
    if temperature > 0:
        search = {
            "beam_size": 1,
            "num_hypotheses": options.best_of,
            "sampling_topk": 0,
            "sampling_temperature": temperature,
        }
    else:
        search = {"beam_size": options.beam_size, "patience": options.patience}
    return {
        "length_penalty": options.length_penalty,
        "repetition_penalty": options.repetition_penalty,
        "no_repeat_ngram_size": no_repeat_ngram_size,
        "max_length": max_length,
        "return_scores": True,
        "return_no_speech_prob": True,
        "suppress_blank": options.suppress_blank,
        "suppress_tokens": options.suppress_tokens,
        "max_initial_timestamp_index": max_initial_timestamp_index,
        **search,
    }


def _return_tuple(candidate: dict[str, Any], returned_temperature: float | None = None) -> tuple[Any, float, float, float]:
    return (
        candidate["result"],
        candidate["avg_logprob"],
        candidate["actual_temperature"] if returned_temperature is None else returned_temperature,
        candidate["compression_ratio"],
    )


def make_repetition_guard_model_class(base_class: type) -> type:
    class RepetitionGuardWhisperModel(base_class):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.repetition_guard = kwargs.pop(
                "repetition_guard", RepetitionGuardSettings()
            )
            super().__init__(*args, **kwargs)
            self.repetition_guard_events: list[dict[str, Any]] = []
            self.repetition_guard_context: dict[str, Any] = {}

        def generate_with_fallback(
            self, encoder_output: Any, prompt: list[int], tokenizer: Any, options: Any
        ) -> tuple[Any, float, float, float]:
            guard = self.repetition_guard
            if not guard.enabled:
                return super().generate_with_fallback(
                    encoder_output, prompt, tokenizer, options
                )

            candidates: list[dict[str, Any]] = []
            compression_passes: list[dict[str, Any]] = []
            original_selected: dict[str, Any] | None = None
            original_returned_temperature: float | None = None
            original_stopped_early = False
            selected_non_repetitive: dict[str, Any] | None = None
            last_temperature = float(options.temperatures[-1])

            for candidate_id, temperature_value in enumerate(options.temperatures, start=1):
                temperature = float(temperature_value)
                kwargs = _generation_kwargs(
                    options,
                    len(prompt),
                    temperature,
                    options.no_repeat_ngram_size,
                    self.max_length,
                    self.time_precision,
                )
                result = self.model.generate(
                    encoder_output, [prompt], **kwargs
                )[0]
                candidate = _candidate_for_result(
                    result, temperature, tokenizer, options, guard, candidate_id
                )
                candidates.append(candidate)
                if candidate["compression_pass"]:
                    compression_passes.append(candidate)

                if candidate["probable_silence"]:
                    original_selected = candidate
                    original_returned_temperature = temperature
                    original_stopped_early = True
                    break

                if not candidate["needs_fallback"]:
                    if original_selected is None:
                        original_selected = candidate
                        original_returned_temperature = temperature
                        original_stopped_early = True
                    if not candidate["repetition"]["suspected"]:
                        selected_non_repetitive = candidate
                        break
                    # Stop where stock faster-whisper would stop. The guard may
                    # choose an earlier candidate or add its one deterministic
                    # regeneration, but it must not consume extra fallback samples.
                    break

            if original_selected is None:
                original_selected = max(
                    compression_passes or candidates,
                    key=lambda item: item["avg_logprob"],
                )
                original_returned_temperature = last_temperature

            event = {
                **self.repetition_guard_context,
                "mode": guard.mode,
                "settings": guard.as_dict(),
                "prompt_token_ids": [int(token) for token in prompt],
                "attempts": [_candidate_log(candidate) for candidate in candidates],
                "original_selected_candidate_id": original_selected["candidate_id"],
                "original_selected_text": original_selected["text"],
                "original_selected_actual_temperature": original_selected["actual_temperature"],
                "original_returned_temperature": original_returned_temperature,
                "regeneration_attempted": False,
                "regeneration_suppressed_repetition": False,
                "selection_additional_generation_calls": (
                    len(candidates) - original_selected["candidate_id"]
                    if original_stopped_early
                    else 0
                ),
                "additional_generation_calls": (
                    len(candidates) - original_selected["candidate_id"]
                    if original_stopped_early
                    else 0
                ),
            }

            if original_selected["probable_silence"]:
                event.update(
                    {
                        "selection_reason": "probable_silence_preserved",
                        "selected_candidate_id": original_selected["candidate_id"],
                        "selected_text": original_selected["text"],
                        "selected_actual_temperature": original_selected["actual_temperature"],
                        "rescued_low_logprob": False,
                        "unresolved_repetition": False,
                    }
                )
                self.repetition_guard_events.append(event)
                return _return_tuple(original_selected, original_returned_temperature)

            if selected_non_repetitive is not None:
                changed = selected_non_repetitive is not original_selected
                event.update(
                    {
                        "selection_reason": (
                            "accepted_non_repetitive_after_repeated_candidate"
                            if changed
                            else "existing_non_repetitive_candidate_preserved"
                        ),
                        "selected_candidate_id": selected_non_repetitive["candidate_id"],
                        "selected_text": selected_non_repetitive["text"],
                        "selected_actual_temperature": selected_non_repetitive["actual_temperature"],
                        "rescued_low_logprob": not selected_non_repetitive["logprob_pass"],
                        "unresolved_repetition": False,
                    }
                )
                self.repetition_guard_events.append(event)
                return _return_tuple(selected_non_repetitive)

            decision = select_repetition_aware_candidate(candidates, original_selected)
            selected = decision["candidate"]
            if decision["changed"]:
                event.update(
                    {
                        "selection_reason": decision["reason"],
                        "selected_candidate_id": selected["candidate_id"],
                        "selected_text": selected["text"],
                        "selected_actual_temperature": selected["actual_temperature"],
                        "rescued_low_logprob": decision["rescued_low_logprob"],
                        "unresolved_repetition": False,
                    }
                )
                self.repetition_guard_events.append(event)
                return _return_tuple(selected)

            if decision["needs_regeneration"] and guard.regeneration_enabled:
                regeneration_kwargs = _generation_kwargs(
                    options,
                    len(prompt),
                    0.0,
                    guard.regeneration_no_repeat_ngram_size,
                    self.max_length,
                    self.time_precision,
                )
                event["regeneration_generation_kwargs"] = dict(regeneration_kwargs)
                regenerated_result = self.model.generate(
                    encoder_output, [prompt], **regeneration_kwargs
                )[0]
                regenerated = _candidate_for_result(
                    regenerated_result,
                    0.0,
                    tokenizer,
                    options,
                    guard,
                    len(candidates) + 1,
                    source="limited_regeneration",
                )
                event["regeneration_attempted"] = True
                event["additional_generation_calls"] += 1
                event["regeneration_candidate"] = _candidate_log(regenerated)
                if _valid_non_repetitive_candidate(regenerated):
                    event.update(
                        {
                            "selection_reason": "limited_regeneration_suppressed_repetition",
                            "selected_candidate_id": regenerated["candidate_id"],
                            "selected_text": regenerated["text"],
                            "selected_actual_temperature": 0.0,
                            "rescued_low_logprob": not regenerated["logprob_pass"],
                            "regeneration_suppressed_repetition": True,
                            "unresolved_repetition": False,
                        }
                    )
                    self.repetition_guard_events.append(event)
                    return _return_tuple(regenerated)

            unresolved = bool(original_selected["repetition"]["suspected"])
            event.update(
                {
                    "selection_reason": (
                        "limited_regeneration_failed_original_preserved"
                        if event["regeneration_attempted"]
                        else decision["reason"]
                    ),
                    "selected_candidate_id": original_selected["candidate_id"],
                    "selected_text": original_selected["text"],
                    "selected_actual_temperature": original_selected["actual_temperature"],
                    "rescued_low_logprob": False,
                    "unresolved_repetition": unresolved,
                }
            )
            self.repetition_guard_events.append(event)
            return _return_tuple(original_selected, original_returned_temperature)

    RepetitionGuardWhisperModel.__name__ = "RepetitionGuardWhisperModel"
    return RepetitionGuardWhisperModel
