"""build_pipeline: no profile = the website exactly; a profile changes only what it names."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

for k, v in {"SARVAM_API_KEY": "t", "RAG_BACKEND_PUBLIC_URL": "http://127.0.0.1:1", "RAG_BACKEND_AUTH_KEY": "t",
             "CARTESIA_API_KEY": "t", "CARTESIA_VOICE_ID": "v"}.items():
    os.environ[k] = v
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipecat.transports.websocket.fastapi as fastapi_transport

from pipeline import PipelineProfile, build_pipeline
from raw_pcm_serializer import RawPCMSerializer


def _build(monkeypatch, profile=None):
    seen = {}
    real = fastapi_transport.FastAPIWebsocketTransport

    class Spy(real):
        def __init__(self, websocket, params, **kw):
            seen["params"] = params
            super().__init__(websocket, params, **kw)

    monkeypatch.setattr(fastapi_transport, "FastAPIWebsocketTransport", Spy)
    build_pipeline(MagicMock(), "en", "test-session", profile)
    return seen["params"]


def test_default_profile_is_the_website(monkeypatch):
    params = _build(monkeypatch)
    assert params.audio_in_sample_rate == 16000 and params.audio_out_sample_rate == 24000
    assert isinstance(params.serializer, RawPCMSerializer)


def test_profile_overrides_rates_and_serializer(monkeypatch):
    serializer = RawPCMSerializer(sample_rate=8000)

    async def handler(params):
        pass

    profile = PipelineProfile(serializer=serializer, audio_in_rate=8000, audio_out_rate=8000, tts_rate=8000,
                              llm_headers={"X-Source": "phone"}, function_handlers={"get_x": handler})
    params = _build(monkeypatch, profile)
    assert params.audio_in_sample_rate == 8000 and params.audio_out_sample_rate == 8000
    assert params.serializer is serializer
