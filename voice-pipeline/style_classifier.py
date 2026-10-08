"""Deterministic reply-style classifier: EN / TA / TA_EN.

TA_EN is how people in Chennai actually talk or type: Tamil grammar with English
words mixed in, usually written in English letters ("fees evlo bro?",
"hostel iruka?"). It is not the same as English, and it is not formal Tamil in
Tamil script either, so it gets its own style.

No network call and no model: Tamil Unicode, a list of romanized Tamil words,
and a count of plain English words. It runs on every turn, so it has to be cheap
and has to give the same answer for the same text.

Copy of rag-backend/language_style.py (keep in step). The two services share no
code, so the file is copied rather than imported; if you change the word lists
in one, change the other.

First pass, not reviewed by a native speaker — the word lists want one.
"""
import re
from typing import Literal

Style = Literal["EN", "TA", "TA_EN"]

_TAMIL_SCRIPT = re.compile(r"[஀-௿]")
_LATIN_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
_TAMIL_WORD = re.compile(r"[஀-௿]+")
# "hostel-la", "branch-ku": an English word with a Tamil case ending attached.
_TANGLISH_SUFFIX = re.compile(r"\b[a-z]+-(?:la|ku|kku|ukku|oda|aa|ah|kitta|ta)\b")

# Romanized Tamil words that are not English words. A single one is enough
# evidence for TA_EN.
_STRONG = frozenset(
    "evlo evvalo evalavu epdi eppadi epo eppo enga engae inga ingae anga angae "
    "iruka irukka iruku irukku irukkum irukaa irukkaa eruka erukka erukku illa illai illaya "
    "venum vendam vennum venaam panna pannu pannunga pannalaam pannalama pannanum "
    "panrathu pannrathu panren pannuven pannitu pannitten pannidalaam "
    "sollu solunga sollunga sollidunga kelunga ketta paaru paarunga paakalaam paakureenga "
    "theriyuma theriyum therila theriyala puriyala puriyuthu purinjuthu "
    "seri sari aama aamam aamaa nalla romba konjam rombha "
    "enakku ennaku unakku ungalukku ungaluku avlo ivlo ippo ippa appuram apram "
    "vanthu vaanga vanga ponga poga kudukka kidaikum kedaikum kidaikkum mudiyuma mudiyum "
    "edhu ethu entha endha yaar yaaru yen yaen ethuku edhukku ethana ethanai evalo "
    "padikka padikalaam padikkalaam padikkanum sera serkka serunga "
    "nandri vanakkam sema machan macha thambi thangachi "
    "neenga neengal nee naanga nanga namma unga ungal ungalukku enga "
    "irundha irundhaa irukkanum".split()
)
# Words people say in English too ("bro", "anna university", "naan bread"). On
# their own they are not enough; they only count when a strong word is also there
# or when the session is already in TA_EN.
_WEAK = frozenset("bro anna akka da di pa ma sir naan naa ji machan".split())

# Plain English words. Used for the English-or-not decision and for the ratio.
_ENGLISH_WORDS = frozenset(
    "a an the is are am was were be do does did can could will would should have has had "
    "i me my you your we our it its they them this that these those there here "
    "what which who when where why how to of in on at for from with about and or but not no "
    "yes if so any some tell want need know please hello hi hey thanks thank okay ok "
    "fee fees course courses admission admissions apply application hostel campus college "
    "placement placements department branch cutoff seat seats counselling".split()
)

# Acknowledgements and fillers that say nothing about language. They keep
# whatever style the call is already in.
_NEUTRAL = frozenset(
    "ok okay k yes yeah yep no hmm hm mm mhm right sure fine cool nice thanks thank "
    "hi hello hey bye alright".split()
)


def _words(text: str) -> list[str]:
    return _LATIN_WORD.findall(text.lower())


def detect_style(text: str) -> Style | None:
    """The style the text is written in, or None when it carries no evidence
    either way ("okay", "B.Tech", a bare number)."""
    if not text or not text.strip():
        return None
    lower = text.lower()

    if _TAMIL_SCRIPT.search(text):
        tamil = len(_TAMIL_WORD.findall(text))
        latin = len(_words(text))
        # Tamil script with mostly English words around it reads as a mix.
        return "TA_EN" if latin > tamil else "TA"

    words = _words(lower)
    strong = sum(1 for w in words if w in _STRONG) + len(_TANGLISH_SUFFIX.findall(lower))
    if strong:
        return "TA_EN"

    if any(w in _WEAK for w in words):
        # "bro" / "anna" alone: could be an English speaker. Leave it to the
        # caller's current style.
        return None

    english = sum(1 for w in words if w in _ENGLISH_WORDS or len(w) > 2)
    if len(words) >= 3 and english >= 2:
        return "EN"
    if words and all(w in _NEUTRAL for w in words):
        return None
    if len(words) >= 3:
        return "EN"
    return None


def classify_style(text: str, previous: Style = "EN") -> Style:
    """detect_style, falling back to `previous` when the text has no evidence."""
    return detect_style(text) or previous
