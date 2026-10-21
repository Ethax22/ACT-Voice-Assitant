"""Lone letters ("B. E.", "M. E.") stay inside one sentence for the voice, in Tamil and English."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sentence_split_fix  # noqa: E402
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator


def _sentences(text: str, language: str, chunk: int = 3) -> list[str]:
    """Feed text in small pieces, as the model streams it, and collect what goes to the voice."""
    async def run():
        agg, out = SimpleTextAggregator(language=language), []
        for i in range(0, len(text), chunk):
            async for a in agg.aggregate(text[i:i + chunk]):
                out.append(a.text)
        last = await agg.flush()
        if last and last.text:
            out.append(last.text)
        return out
    return asyncio.run(run())


def test_tamil_initials_stay_together():
    sentence_split_fix.install()
    reply = ("B. E. CSE course-ல AI, Data Science கத்துக்கலாம். "
             "நம்மகிட்ட IT, ECE அப்புறம் M. E. Structural Engineering இருக்கு. இதுல எந்த course வேணும்?")
    for chunk in (1, 3, 7):
        assert _sentences(reply, "ta", chunk) == [
            "B. E. CSE course-ல AI, Data Science கத்துக்கலாம்.",
            "நம்மகிட்ட IT, ECE அப்புறம் M. E. Structural Engineering இருக்கு.",
            "இதுல எந்த course வேணும்?",
        ]


def test_ordinary_sentences_still_split():
    sentence_split_fix.install()
    assert _sentences("Hello Ravi. We offer B.E. and Ph.D. programs. Want details?", "en") == [
        "Hello Ravi.", "We offer B.E. and Ph.D. programs.", "Want details?"]
    assert sentence_split_fix.match_endofsentence("Hello. N", language="ta") == 6
    assert sentence_split_fix.match_endofsentence("B. E", language="ta") == 0
    assert _sentences("B.E. CSE-ல AI கத்துக்கலாம். சரியா?", "ta") == ["B.E. CSE-ல AI கத்துக்கலாம்.", "சரியா?"]
