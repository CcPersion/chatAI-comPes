import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "outputs" / "backend"))
from voxcpm_worker import GenerationRegistry, VoxCPMRuntime, profile_spec
from voxcpm_client import VoxCPMClient


def test_health_and_version_observe_profile_model_and_sample_rate():
    runtime = VoxCPMRuntime("/missing", "VoxCPM2", "balanced-v2", configured_sample_rate=48000)
    health = runtime.health()
    version = runtime.version()
    assert health["protocol_version"] == "tts.v2"
    assert health["model"]["id"] == "VoxCPM2"
    assert health["model"]["sample_rate"] == 48000
    assert version["model_id"] == "VoxCPM2"
    assert version["sample_rate"] == 48000
    assert version["local_files_only"] is True


def test_ndjson_generation_events_are_ordered_without_gpu():
    runtime = VoxCPMRuntime("/missing", "VoxCPM2", "balanced-v2", configured_sample_rate=48000)
    runtime.model = type("FakeModel", (), {"generate_streaming": lambda self, **_: iter([np.array([0.0, 0.5], dtype=np.float32)]), "tts_model": type("T", (), {"sample_rate": 48000})()})()
    runtime.actual_sample_rate = 48000
    events = list(runtime.synthesize("hello", "req", "conv", "gen", runtime.registry.start("gen")))
    runtime.registry.finish("gen")
    assert [event["type"] for event in events] == ["generation.started", "audio.chunk", "generation.completed"]
    assert events[1]["sequence"] == 0 and events[1]["is_first"] is True and events[1]["is_last"] is True
    assert events[1]["audio_format"] == "pcm_s16le_mono"
    assert events[1]["sample_rate"] == events[2]["sample_rate"] == 48000


def test_voxcpm2_uses_isolated_reference_without_prompt_continuation(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-test")
    captured = {}

    class FakeModel:
        tts_model = type("T", (), {"sample_rate": 48000})()

        def generate_streaming(self, **kwargs):
            captured.update(kwargs)
            return iter([np.array([0.0, 0.1], dtype=np.float32)])

    runtime = VoxCPMRuntime(
        "/missing",
        "VoxCPM2",
        "balanced-v2",
        ref_wav=str(reference),
        ref_text="reference transcript",
    )
    runtime.model = FakeModel()
    runtime.actual_sample_rate = 48000

    list(runtime.synthesize("hello", "req", "conv", "gen", runtime.registry.start("gen")))
    runtime.registry.finish("gen")

    assert captured["reference_wav_path"] == str(reference.resolve())
    assert "prompt_wav_path" not in captured
    assert "prompt_text" not in captured


def test_voxcpm15_keeps_prompt_continuation_contract(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-test")
    captured = {}

    class FakeModel:
        tts_model = type("T", (), {"sample_rate": 44100})()

        def generate_streaming(self, **kwargs):
            captured.update(kwargs)
            return iter([np.array([0.0, 0.1], dtype=np.float32)])

    runtime = VoxCPMRuntime(
        "/missing",
        "VoxCPM1.5",
        "safe-v15",
        ref_wav=str(reference),
        ref_text="reference transcript",
    )
    runtime.model = FakeModel()
    runtime.actual_sample_rate = 44100

    list(runtime.synthesize("hello", "req", "conv", "gen", runtime.registry.start("gen")))
    runtime.registry.finish("gen")

    assert captured["prompt_wav_path"] == str(reference.resolve())
    assert captured["prompt_text"] == "reference transcript"
    assert "reference_wav_path" not in captured


def test_cancel_is_generation_scoped_and_idempotent_for_finished_generation():
    registry = GenerationRegistry()
    registry.start("gen-old")
    assert registry.cancel("gen-other") == "already_finished"
    assert registry.cancel("gen-old") == "cancelling"
    registry.finish("gen-old")
    assert registry.cancel("gen-old") == "already_finished"


def test_profiles_keep_native_sample_rates_separate():
    assert profile_spec("balanced-v2", "VoxCPM2")["sample_rate"] == 48000
    assert profile_spec("safe-v15", "VoxCPM1.5")["sample_rate"] == 44100


def test_reference_audio_update_is_local_and_changes_reference_id(tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-test")
    runtime = VoxCPMRuntime("/missing", "VoxCPM2", "balanced-v2")
    assert runtime.reference_id() is None
    runtime.update_reference(str(reference), "exact prompt")
    assert runtime.ref_wav == str(reference.resolve())
    assert runtime.ref_text == "exact prompt"
    assert runtime.reference_id()


def test_reference_audio_update_rejects_paths_outside_upload_root(tmp_path):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF-test")
    runtime = VoxCPMRuntime("/missing", "VoxCPM2", "balanced-v2", reference_root=str(upload_root))
    try:
        runtime.update_reference(str(outside))
    except PermissionError:
        pass
    else:
        raise AssertionError("reference path outside upload root must be rejected")


def test_client_applies_native_style_prompt_without_changing_empty_text():
    client = VoxCPMClient(
        "http://worker.test",
        style_prompt="（语速稍慢，停顿清晰）",
    )
    assert client.prepare_text(" 你好。 ") == "(语速稍慢，停顿清晰)你好。"
    assert client.prepare_text("   ") == ""
