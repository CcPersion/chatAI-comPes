import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from outputs.backend import app


def test_recent_logs_are_allowlisted_and_capped(tmp_path, monkeypatch):
    log_path = tmp_path / "app.log"
    log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    monkeypatch.setitem(app.LOG_SOURCE_PATHS, "应用", str(log_path))

    output = app.get_recent_logs("应用", limit=2)

    assert "【应用】" in output
    assert "line-1" not in output
    assert "line-2" in output and "line-3" in output


def test_recent_logs_rejects_unknown_source_without_reading_arbitrary_path():
    output = app.get_recent_logs("C:/should-not-be-read", limit=10)

    assert "【应用】" in output
    assert "should-not-be-read" not in output


def test_recent_logs_supports_up_to_one_thousand_lines(tmp_path, monkeypatch):
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "\n".join(f"line-{index}" for index in range(1200)),
        encoding="utf-8",
    )
    monkeypatch.setitem(app.LOG_SOURCE_PATHS, "应用", str(log_path))

    output = app.get_recent_logs("应用", limit=1000)

    assert "line-199\n" not in output
    assert "line-200" in output and "line-1199" in output


def test_render_recent_logs_uses_readable_level_colors_and_escapes_content(tmp_path, monkeypatch):
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "[2026-08-10] INFO ready\n[2026-08-10] WARNING <slow>\n[2026-08-10] ERROR failed\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(app.LOG_SOURCE_PATHS, "搴旂敤", str(log_path))

    rendered = app.render_recent_logs("搴旂敤", limit=10)

    assert "utility-log-info" in rendered
    assert "utility-log-warning" in rendered
    assert "utility-log-error" in rendered
    assert "&lt;slow&gt;" in rendered
    assert "0001" in rendered and "0003" in rendered


def test_runtime_settings_do_not_expose_model_or_service_paths():
    rendered = app.render_runtime_settings(app.get_runtime_settings())

    assert "模型路径" not in rendered
    assert "127.0.0.1:8020" not in rendered
    assert "voice.yaml" not in rendered


def test_status_summary_and_details_include_non_color_state_text():
    status = {
        "overall": "partial",
        "llama": "connected",
        "asr": "ready",
        "tts": "timeout",
        "livetalking": "disconnected",
        "avatar_sync": "ready",
    }

    summary = app.format_status_summary(status)
    details = app.format_status_details(status)

    assert "部分服务异常" in summary
    assert "语音合成" in details and "超时" in details
    assert "数字人口型" in details and "未连接" in details


def test_utility_panel_updates_are_mutually_exclusive():
    drawer, title, settings, logs, status = app.toggle_utility_panel("logs")

    assert drawer["visible"] is True
    assert title == "日志"
    assert settings["visible"] is False
    assert logs["visible"] is True
    assert status["visible"] is False

    closed = app.close_utility_panel()
    assert all(update["visible"] is False for update in closed)


def test_apply_reference_audio_delegates_explicit_application(monkeypatch):
    captured = {}

    def fake_upload(file_path, prompt_text):
        captured.update(file_path=file_path, prompt_text=prompt_text)
        return {"ok": True, "message": "updated"}

    monkeypatch.setattr(app, "handle_audio_upload", fake_upload)

    result = app.apply_reference_audio("/tmp/reference.wav", "spoken reference")

    assert result["ok"] is True
    assert captured == {
        "file_path": "/tmp/reference.wav",
        "prompt_text": "spoken reference",
    }


def test_apply_reference_audio_requires_a_file():
    result = app.apply_reference_audio(None, "")

    assert result["error"]["code"] == "TTS_ERR_002"
    assert "参考音频" in result["error"]["message"]


def test_apply_runtime_settings_updates_live_objects_and_persists(monkeypatch):
    persisted = {}
    for key in app.EDITABLE_RUNTIME_KEYS:
        monkeypatch.setattr(app, key, getattr(app, key))
    monkeypatch.setattr(app, "config", dict(app.config))
    fake_pipeline = SimpleNamespace(
        vad=SimpleNamespace(threshold=0.0, min_silence_ms=0),
        _close_avatar_audio_stream=lambda: persisted.update(stream_closed=True),
    )
    fake_tts = SimpleNamespace(style_prompt="")
    monkeypatch.setattr(app, "pipeline", fake_pipeline)
    monkeypatch.setattr(app, "tts_engine", fake_tts)
    monkeypatch.setattr(app, "_persist_runtime_settings", lambda values: persisted.update(values))

    values = dict(zip(app.EDITABLE_RUNTIME_KEYS, app.get_runtime_form_values()))
    values.update(
        LLM_TEMPERATURE=0.9,
        VOXCPM_STYLE_PROMPT="语速稍慢，停顿自然",
        AVATAR_AUDIO_GAIN=1.6,
    )

    result = app.apply_runtime_settings(values)

    assert result["ok"] is True
    assert app.LLM_TEMPERATURE == 0.9
    assert app.AVATAR_AUDIO_GAIN == 1.6
    assert fake_tts.style_prompt == "语速稍慢，停顿自然"
    assert persisted["stream_closed"] is True
    assert persisted["LOG_LEVEL"] == app.LOG_LEVEL


def test_apply_runtime_settings_rejects_invalid_values(monkeypatch):
    persisted = []
    monkeypatch.setattr(app, "_persist_runtime_settings", lambda values: persisted.append(values))
    values = dict(zip(app.EDITABLE_RUNTIME_KEYS, app.get_runtime_form_values()))
    values["VAD_THRESH"] = 2.0

    result = app.apply_runtime_settings(values)

    assert result["error"]["code"] == "CFG_ERR_002"
    assert persisted == []


def test_persist_runtime_settings_preserves_comments_and_unlisted_values(tmp_path, monkeypatch):
    config_path = tmp_path / "voice.yaml"
    config_path.write_text(
        "# keep this comment\nLLM_TEMPERATURE: 0.7\nLLM_MODEL: qwen-test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app, "CONFIG_PATH", str(config_path))

    app._persist_runtime_settings({"LLM_TEMPERATURE": 0.8})

    saved = config_path.read_text(encoding="utf-8")
    assert "# keep this comment" in saved
    assert "LLM_TEMPERATURE: 0.8" in saved
    assert "LLM_MODEL: qwen-test" in saved
