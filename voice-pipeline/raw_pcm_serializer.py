"""Minimal Pipecat FrameSerializer for a plain browser client speaking raw
linear16 PCM over a WebSocket — no wrapper protocol.

Pipecat's built-in serializers (e.g. TwilioFrameSerializer, Protobuf) assume
a specific vendor wire format. Our widget talks a much simpler protocol
(binary PCM16 in, binary PCM16 out — the same minimal scheme used by the
English /voice/en relay), so FastAPIWebsocketTransport needs this instead of
`serializer=None`, which silently discards every incoming message.

2026-09-29: also forwards `OutputTransportMessageFrame` as a JSON text
message — added so `conversation_router.py` can tell the browser which
topic it just switched the voice to, for the topic-avatar "speaking" glow
(widget/index.html's `setActiveTopic`/`setSpeakingTopic`). That glow used
to be driven by Deepgram's `ConversationText` JSON events; the Sarvam
pipeline never sent any JSON at all before this (binary audio only), which
is why the glow silently stopped working when English moved off Deepgram —
not a regression in the glow code itself, just its one input source
disappearing. Binary audio frames are unaffected; this is a second,
independent case in the same serializer, not a replacement.
"""
import json

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class RawPCMSerializer(FrameSerializer):
    def __init__(self, sample_rate: int = 16000):
        super().__init__()
        self._sample_rate = sample_rate

    async def serialize(self, frame: Frame):
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, OutputTransportMessageFrame):
            return json.dumps(frame.message)
        if isinstance(frame, InterruptionFrame):
            # Barge-in: FastAPIWebsocketOutputTransport hands the serializer
            # every InterruptionFrame. The server has already stopped the
            # LLM/TTS, but it sends audio at up to 2x real time, so the browser
            # is still holding queued chunks — this tells the widget to drop
            # them (its existing 'UserStartedSpeaking' handler -> stopPlayback()).
            return json.dumps({"type": "UserStartedSpeaking"})
        return None

    async def deserialize(self, data):
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(
                audio=bytes(data),
                sample_rate=self._sample_rate,
                num_channels=1,
            )
        return None
