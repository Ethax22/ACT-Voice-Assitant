# voice-pipeline

Pipecat pipeline composing Sarvam Saaras (STT) + the shared rag-backend + Sarvam Bulbul (TTS),
serving both English and Tamil (renamed from `tamil-voice-pipeline/` once generalized — see
CLAUDE.md's locked-in decision #1). `/voice/en` and `/voice/ta` both run the same pipeline,
parameterized by language in `pipeline.py`.

See ../docs/Implementation_Plan_API_Based.md, Phase 3, for the full spec.
