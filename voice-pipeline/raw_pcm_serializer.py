"""Minimal Pipecat FrameSerializer for a plain browser client speaking raw
linear16 PCM over a WebSocket — no wrapper protocol.

Pipecat's built-in serializers (e.g. TwilioFrameSerializer, Protobuf) assume
a specific vendor wire format. Our widget talks a much simpler protocol
(binary PCM16 in, binary PCM16 out — the same minimal scheme used by the
English /voice/en relay), so FastAPIWebsocketTransport needs this instead of
`serializer=None`, which silently discards every incoming message.
"""
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer


class RawPCMSerializer(FrameSerializer):
    def __init__(self, sample_rate: int = 16000):
        super().__init__()
        self._sample_rate = sample_rate

    async def serialize(self, frame: Frame):
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data):
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(
                audio=bytes(data),
                sample_rate=self._sample_rate,
                num_channels=1,
            )
        return None
