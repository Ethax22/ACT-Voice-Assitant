"""Structured per-conversation state, so the model is told what the visitor
has already said instead of having to re-derive it from the transcript.

Two sources, both deterministic (no extra LLM call):

- Everything except the lead stage is re-derived from the full message history
  on every request. The history is always passed in full (CLAUDE.md decision
  #3), so there is nothing to keep in sync and nothing lost on a restart.
- "A lead was already saved" is the one fact the history can't show — the
  capture_lead tool round-trip happens inside this backend and never reaches
  the caller's copy of the conversation. That is kept in a small in-memory
  store keyed by session id (widget chat's session_id, or the voice pipeline's
  X-Session-Id header). Lost on restart, which only costs a possible repeat ask.

The patterns are English-first with a few Tamil cues — a first pass that needs
checking against real transcripts, like the voice pipeline's Tamil keywords.
"""
import re
from collections import OrderedDict
from dataclasses import dataclass

from leads import normalize_indian_mobile

_MAX_SESSIONS = 2000
_captured_phones: OrderedDict[str, set[str]] = OrderedDict()

# (label, pattern). Abbreviations are matched case-sensitively so ordinary
# words ("it", "be", "me") don't count as IT / B.E. / M.E.
_COURSES: list[tuple[str, re.Pattern]] = [
    ("Artificial Intelligence and Data Science", re.compile(r"artificial intelligence|data science|\bAI\s*(?:&|and)?\s*DS\b|(?i:\baids\b)")),
    ("Biomedical Engineering", re.compile(r"(?i)bio\s*medical")),
    ("Chemical Engineering", re.compile(r"(?i)\bchemical\b")),
    ("Civil Engineering", re.compile(r"(?i)\bcivil\b")),
    ("Computer Science and Engineering", re.compile(r"(?i:computer science)|\bCSE\b|\bC\.S\.E\b")),
    ("Electrical and Electronics Engineering", re.compile(r"(?i:electrical)|\bEEE\b")),
    ("Electronics and Communication Engineering", re.compile(r"(?i:electronics (?:and|&) communication)|\bECE\b")),
    ("Information Technology", re.compile(r"(?i:information technology)|\bIT\b(?! is| was|'s)")),
    ("Mechanical Engineering", re.compile(r"(?i)\bmech(?:anical)?\b")),
    ("Mechatronics Engineering", re.compile(r"(?i)mechatronics")),
    ("M.E. Structural Engineering", re.compile(r"(?i)structural")),
]

_LEVEL_PG = re.compile(r"(?i:\bpg\b|post\s*graduat|\bmasters?\b|\bm\.?\s?tech\b|\bph\.?d\b)|\bM\.E\b|\bME\b")
_LEVEL_UG = re.compile(r"(?i:\bug\b|under\s*graduat|\bbachelors?\b|\bb\.?\s?tech\b)|\bB\.E\b|\bBE\b")

_ASKER_PARENT = re.compile(
    r"(?i)\bmy (?:son|daughter|child|kid|ward|brother|sister)\b|என் (?:மகன்|மகள்|பையன்|பொண்ணு|தம்பி|தங்கை)"
)
_ASKER_STUDENT = re.compile(
    r"(?i)\bi(?:'m| am) (?:a |currently )?(?:student|studying|doing)\b"
    r"|\bi (?:have |just )?(?:completed|finished|passed|scored|got)\b|நான் படி"
)

_QUALIFICATIONS: list[tuple[str, re.Pattern]] = [
    ("Diploma", re.compile(r"(?i)\bdiploma\b|\bpolytechnic\b|டிப்ளமோ")),
    ("12th standard", re.compile(r"(?i)\b12th\b|\btwelfth\b|\bplus two\b|\+\s?2\b|\bhsc\b|higher secondary|பன்னிரண்டாம்|பிளஸ் டூ")),
]

_INTEREST = re.compile(
    r"(?i)\b(?:apply|applying|application|admission|join|joining|enrol|fees?|eligib|cut\s*-?off|"
    r"counsel+ing|call ?back|seat)\w*|சேர்க்கை|விண்ணப்ப|கட்டணம்|சேர"
)
_PHONE_CANDIDATE = re.compile(r"(?:\+?91[\s-]?)?[6-9](?:[\s-]?\d){9}")


@dataclass
class ConversationState:
    asker: str | None = None
    level: str | None = None
    course: str | None = None
    qualification: str | None = None
    lead_stage: str = "none"  # none -> interested -> details_given -> captured

    def prompt_block(self) -> str:
        """Text appended to the system prompt, or "" when nothing is known."""
        known = [
            f"- Who is asking: {self.asker}" if self.asker else None,
            f"- Level: {self.level}" if self.level else None,
            f"- Course of interest: {self.course}" if self.course else None,
            f"- Qualification so far: {self.qualification}" if self.qualification else None,
        ]
        lines = [k for k in known if k]
        if self.lead_stage == "captured":
            lines.append(
                "- Lead: already saved for admissions in this conversation. Do not ask "
                "for their name, phone or email again, and do not call capture_lead "
                "again unless they give a corrected detail."
            )
        elif self.lead_stage == "details_given":
            lines.append(
                "- Lead: they have given a phone number. If you also have their name, "
                "call capture_lead now; otherwise ask only for the name."
            )
        elif self.lead_stage == "interested":
            lines.append(
                "- Lead: they have shown admission interest. Ask for name and phone "
                "once, when it fits naturally; if they decline, don't ask again."
            )
        if not lines:
            return ""
        return (
            "\n\nKnown about this visitor so far (worked out from this conversation — "
            "use it, and never ask for any of it again):\n" + "\n".join(lines)
        )


def _latest_match(turns: list[str], options: list[tuple[str, re.Pattern]]) -> str | None:
    """Most recent turn wins, so a visitor who changes their mind is followed."""
    for turn in reversed(turns):
        for label, pattern in options:
            if pattern.search(turn):
                return label
    return None


def derive_state(messages: list[dict], session_id: str | None = None) -> ConversationState:
    turns = [
        m["content"] for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    state = ConversationState()
    state.course = _latest_match(turns, _COURSES)
    state.qualification = _latest_match(turns, _QUALIFICATIONS)
    state.level = _latest_match(
        turns, [("Postgraduate", _LEVEL_PG), ("Undergraduate", _LEVEL_UG)]
    )
    state.asker = _latest_match(
        turns,
        [("a parent or family member, asking for someone else", _ASKER_PARENT),
         ("the student themselves", _ASKER_STUDENT)],
    )

    if session_id and _captured_phones.get(session_id):
        state.lead_stage = "captured"
    elif any(
        normalize_indian_mobile(m.group()) for t in turns for m in _PHONE_CANDIDATE.finditer(t)
    ):
        state.lead_stage = "details_given"
    elif any(_INTEREST.search(t) for t in turns):
        state.lead_stage = "interested"
    return state


def lead_already_captured(session_id: str | None, phone: str) -> bool:
    return bool(session_id) and phone in _captured_phones.get(session_id, ())


def mark_lead_captured(session_id: str | None, phone: str) -> None:
    if not session_id:
        return
    _captured_phones.setdefault(session_id, set()).add(phone)
    _captured_phones.move_to_end(session_id)
    while len(_captured_phones) > _MAX_SESSIONS:
        _captured_phones.popitem(last=False)
