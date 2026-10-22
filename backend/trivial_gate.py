"""Trivial-answer gate (card ada-nest-trivial-gate — Nest adaptive-depth).

Last cheap stage of the relay cascade:

    silence detect -> STT -> noise filter (_is_trivial) -> THIS GATE -> Ada turn

Same rule NCM (OPS-0750 / Education 1588) demonstrates: stop when a cheap
stage suffices. The gate answers only what it can answer with certainty —
greetings, farewells, thanks, acknowledgements, time/date asks, persona
small talk — and returns None for everything else so the turn passes
through to Ada untouched.

Depth:

    L0 rules  — anchored intent patterns over the normalized text plus
                canned/deterministic replies. Zero deps, sub-ms, always
                available. Rules only fire on a FULL match, so "thanks,
                can you open the gate" still reaches Ada.
    L1 model  — optional /v1/systemone intent classifier on the local
                model shelf (ADA_TRIVIAL_LLM_URL, falling back to
                ADA_COMMAND_LLM_URL — e.g. a gemma-3-1b llama.cpp lane
                next to ada-stt on idc02). Raises recall on phrasing the
                rules don't know; ADA_TRIVIAL_MIN_CONF decides deflect
                vs pass-through. The model only classifies into canned
                intents — it never free-generates a reply.

Hot-session suppression: bare answer-shaped acks ("yes", "ok", "sure",
"👍") are often replies to a question Ada just asked. When the chat has a
live session that saw traffic within ADA_TRIVIAL_QUIET_S, answerish
intents are suppressed and pass through — a cold "yes" still gets "👍".

Modes (ADA_TRIVIAL_GATE):

    off     — gate disabled; nothing is evaluated.
    enforce — (default) confident trivial turns answered locally.
    shadow  — evaluate fire-and-forget, record what WOULD deflect, never
              answer. Zero added latency; use to measure before trusting.

Every evaluated turn appends a corpus row (ADA_TRIVIAL_CORPUS, default
~/.local/share/ada/trivial-gate-corpus.jsonl) and each decision logs the
running deflection rate — the gate is measurable from the first turn.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("trivial_gate")

_MODE_ENV = "ADA_TRIVIAL_GATE"
_URL_ENV = "ADA_TRIVIAL_LLM_URL"
_MIN_CONF_ENV = "ADA_TRIVIAL_MIN_CONF"
_TIMEOUT_ENV = "ADA_TRIVIAL_TIMEOUT_S"
_MAX_WORDS_ENV = "ADA_TRIVIAL_MAX_WORDS"
_QUIET_ENV = "ADA_TRIVIAL_QUIET_S"
_CORPUS_ENV = "ADA_TRIVIAL_CORPUS"

_DEFAULT_MIN_CONF = 0.75
_DEFAULT_TIMEOUT_S = 2.0
_DEFAULT_MAX_WORDS = 20
_DEFAULT_QUIET_S = 90.0

# Intents a user may send as the ANSWER to Ada's own question ("shall I
# open it?" -> "yes"). They only deflect when the session is cold; mid-
# conversation they must reach Ada.
_ANSWERISH = {"ack", "emoji"}


# --- config ----------------------------------------------------------------

def mode() -> str:
    return os.environ.get(_MODE_ENV, "enforce").strip().lower()


def llm_url() -> str | None:
    url = (os.environ.get(_URL_ENV)
           or os.environ.get("ADA_COMMAND_LLM_URL") or "").strip()
    return url.rstrip("/") or None


def min_conf() -> float:
    try:
        return float(os.environ.get(_MIN_CONF_ENV, _DEFAULT_MIN_CONF))
    except ValueError:
        return _DEFAULT_MIN_CONF


def timeout_s() -> float:
    try:
        return float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def max_words() -> int:
    try:
        return int(os.environ.get(_MAX_WORDS_ENV, _DEFAULT_MAX_WORDS))
    except ValueError:
        return _DEFAULT_MAX_WORDS


def quiet_s() -> float:
    """Mid-conversation window: an open session active within this many
    seconds makes answer-shaped acks pass through to Ada."""
    try:
        return float(os.environ.get(_QUIET_ENV, _DEFAULT_QUIET_S))
    except ValueError:
        return _DEFAULT_QUIET_S


def corpus_path() -> Path:
    return Path(os.environ.get(
        _CORPUS_ENV, os.path.expanduser(
            "~/.local/share/ada/trivial-gate-corpus.jsonl")))


def enabled() -> bool:
    return mode() in ("shadow", "enforce")


# --- L0 rules ----------------------------------------------------------------

_PUNCT_RE = re.compile(r"[\s.,!?…\-—_'\"“”()\[\]{}:;~^*]+")
_WORD_RE = re.compile(r"\w", re.UNICODE)


def _norm(text: str) -> str:
    """Lowercase, punctuation->space, collapse — rules then fullmatch."""
    t = _PUNCT_RE.sub(" ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


@functools.lru_cache(maxsize=8)
def _rules(persona: str) -> list[tuple[re.Pattern, str]]:
    """Anchored intent patterns, evaluated against the normalized text.
    Persona name is a legal optional suffix on greetings/thanks ('hi
    kraut'). Order matters — first fullmatch wins."""
    p = re.escape(_norm(persona) or "ada")
    tail = rf"( {p})?( there| all| everyone| guys| folks)?( ครับ| ค่ะ| คะ)?"
    return [
        (re.compile(
            r"(hi+|hello+|hey+|yo|hiya|howdy|helo|"
            r"good (morning|afternoon|evening|day)|"
            r"morning|evening|afternoon|g day|"
            r"สวัสดี|หวัดดี)" + tail), "greeting"),
        (re.compile(
            r"(thanks|thank you( (very|so) much)?( a lot)?|thx|ty|"
            r"cheers|much appreciated|appreciated|"
            r"ขอบคุณ( มาก)?|ขอบใจ)" + tail), "thanks"),
        (re.compile(
            r"(bye+|good bye|good ?night|nite|gn|"
            r"see (you|ya)( later| soon)?|later|take care|"
            r"ฝันดี|ลาก่อน|บาย)( ครับ| ค่ะ| คะ)?"), "bye"),
        (re.compile(
            r"((what s|what is|what was) the (time|date|day)"
            r"( (is it|is it now|today|now))?|"
            r"what (time|day|date) is (it|today)|"
            r"what day is (it|today)|today s date|"
            r"current (time|date)|time( now| please)?|"
            r"กี่โมง( แล้ว)?|ตอนนี้กี่โมง|วันอะไร|วันที่เท่าไหร่)"),
         "time"),
        (re.compile(
            r"(who are you|what s your name|what is your name|"
            r"your name|who dis|คุณคือใคร|ชื่ออะไร)"), "identity"),
        (re.compile(
            r"(how are you( doing| today)?|how s it going|"
            r"how is it going|how do you do|how are things|"
            r"what s up|whats up|sup|wassup|"
            r"are you (ok|okay|alright|there|awake|alive|up)|"
            r"you (there|ok|okay|alive|awake)|"
            r"สบายดีไหม|เป็นไง( บ้าง)?)"), "chat"),
        (re.compile(
            r"(yes|yeah|yep|yup|ok|okay|kk+|k|sure|nope|no+|nah|"
            r"go ahead|please do|do it|sounds good|noted|"
            r"understood|roger( that)?|wilco|correct|right|"
            r"exactly|absolutely|definitely|of course|deal|"
            r"agreed|on it|"
            r"ใช่|โอเคร?|ครับ|ค่ะ|คะ|จ้า+|จ้ะ|รับทราบ|ได้เลย|เอาเลย)"),
         "ack"),
        (re.compile(
            r"(got it|gotcha|nice|cool|great|awesome|perfect|"
            r"lovely|beautiful|amazing|wow|lol+|ha(ha)+|"
            r"love it|good job|well done|"
            r"เยี่ยม|สุดยอด|ดีมาก|เก่ง(มาก)?)"), "kudos"),
    ]


def _rule_intent(norm: str, persona: str) -> str | None:
    for rx, intent in _rules(persona):
        if rx.fullmatch(norm):
            return intent
    return None


def _is_candidate(text: str) -> bool:
    """Short enough to plausibly be small talk — the L1 cap keeps the
    model hop off long real queries entirely."""
    return 0 < len((text or "").split()) <= max_words()


# --- replies -----------------------------------------------------------------

def _reply_for(intent: str, norm: str, persona: str) -> str:
    persona = (persona or "").strip() or "Ada"
    if intent == "greeting":
        if "morning" in norm:
            return "Good morning!"
        if "afternoon" in norm:
            return "Good afternoon!"
        if "evening" in norm:
            return "Good evening!"
        return "Hey!"
    if intent == "thanks":
        return "You're welcome!"
    if intent == "ack" or intent == "emoji":
        return "👍"
    if intent == "kudos":
        return "Glad it helped!"
    if intent == "bye":
        if re.search(r"night|nite|gn|ฝันดี", norm):
            return "Good night!"
        return "See you!"
    if intent == "time":
        now = time.localtime()
        if re.search(r"date|day|วัน", norm):
            return f"It's {time.strftime('%A, %B %-d, %Y', now)}."
        return f"It's {time.strftime('%-I:%M %p', now)}."
    if intent == "identity":
        return f"I'm {persona}."
    if intent == "chat":
        return "All good here — what's up?"
    return ""


# --- L1 model hop (/v1/systemone intent classifier) ---------------------------

_L1_STATE = (
    "A home-assistant chat bot received this user message: \"{turn}\"")
_L1_QUESTION = {
    "type": "choice",
    "instructions": "Classify the message intent.",
    "criteria": {
        "greeting": "a greeting or hello",
        "thanks": "thanking or expressing gratitude",
        "ack": "a bare acknowledgement or yes/no answer "
               "(ok, sure, noted, yep)",
        "bye": "a farewell or good night",
        "chat": "small talk aimed at the assistant itself "
                "(how are you, who are you)",
        "pass": "a real question, request, command, or anything else",
    },
}
_L1_INTENTS = {"greeting", "thanks", "ack", "bye", "chat"}
# 'time' is deliberately absent from L1 — "what time is it in Bangkok"
# looks like a time ask but wants a timezone answer; the anchored L0
# rule covers the canonical local-time asks only.


def _classify(text: str, url: str) -> dict | None:
    """One POST to /v1/systemone. Sync — callers wrap in to_thread."""
    payload = {"state": _L1_STATE.format(turn=text[:300]),
               "questions": {"q": _L1_QUESTION}}
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/systemone",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(
            urllib.request.urlopen(req, timeout=timeout_s()).read())
        ans = (resp.get("answers") or {}).get("q") or {}
        return {"intent": str(ans.get("choice") or ""),
                "confidence": float(ans.get("confidence") or 0)}
    except Exception as exc:
        logger.info("trivial gate L1 call failed: %s", exc)
        return None


# --- corpus + deflection rate --------------------------------------------------

_STATS = {"evaluated": 0, "deflected": 0, "would_deflect": 0}


def _record(row: dict[str, Any]) -> None:
    try:
        path = corpus_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.info("trivial gate corpus append failed: %s", exc)


def _log_decision(row: dict[str, Any]) -> None:
    n = _STATS["evaluated"]
    served = _STATS["deflected"] + _STATS["would_deflect"]
    logger.info(
        "trivial_gate %s: %s intent=%s layer=%s conf=%.2f ms=%d%s "
        "| deflected %d/%d (%.0f%%)",
        row.get("surface"), row.get("action"), row.get("intent"),
        row.get("layer"), row.get("conf") or 0.0, row.get("ms") or 0,
        f" reason={row['reason']}" if row.get("reason") else "",
        served, n, (100.0 * served / n) if n else 0.0)


# --- public API ------------------------------------------------------------

async def evaluate(text: str, *, surface: str, chat: str = "",
                   session: str = "", persona: str = "",
                   hot: bool = False) -> dict[str, Any]:
    """Full gate evaluation — always records a corpus row and logs the
    decision. Returns the decision dict; 'action' is 'deflect' or
    'pass'."""
    t0 = time.monotonic()
    text = (text or "").strip()[:300]
    norm = _norm(text)
    intent: str | None = None
    layer: str | None = None
    conf = 0.0
    reason = ""
    if norm:
        intent = _rule_intent(norm, persona)
        if intent:
            layer, conf = "rules", 1.0
        elif not _WORD_RE.search(norm) and len(norm) <= 8:
            # emoji/pictograph-only message ("❤️", "👍🏼") — no word
            # chars survive the norm pass but it is still a coherent ack
            intent, layer, conf = "emoji", "rules", 1.0
        elif _is_candidate(text) and llm_url():
            ans = await asyncio.to_thread(_classify, text, llm_url())
            if ans and ans["intent"] in _L1_INTENTS \
                    and ans["confidence"] >= min_conf():
                intent = ans["intent"]
                layer, conf = "model", ans["confidence"]
            elif ans:
                reason = (f"l1={ans['intent']}@{ans['confidence']:.2f}")
    action = "pass"
    if intent:
        if hot and intent in _ANSWERISH:
            reason = "hot-session"
        else:
            action = "deflect"
    ms = int((time.monotonic() - t0) * 1000)
    row: dict[str, Any] = {
        "ts": time.time(), "surface": surface, "chat": chat,
        "session": session, "text": text[:160], "mode": mode(),
        "action": action, "intent": intent, "layer": layer,
        "conf": round(conf, 4), "ms": ms, "hot": hot,
    }
    if reason:
        row["reason"] = reason
    _STATS["evaluated"] += 1
    if action == "deflect":
        if mode() == "enforce":
            _STATS["deflected"] += 1
            row["reply"] = _reply_for(intent, norm, persona)
        else:
            _STATS["would_deflect"] += 1
            row["action"] = "would-deflect"
    _record(row)
    _log_decision(row)
    return row


async def check(text: str, *, surface: str, chat: str = "",
                session: str = "", persona: str = "",
                hot: bool = False) -> dict[str, Any] | None:
    """Gate a turn before it reaches Ada.

    enforce: returns {"reply", "intent", "layer", ...} when the gate
        answers, else None (pass through untouched).
    shadow:  evaluates fire-and-forget for the corpus and returns None
        immediately — zero added latency on real turns.
    off:     returns None without evaluating."""
    if not enabled() or not (text or "").strip():
        return None
    if mode() != "enforce":
        try:
            asyncio.get_running_loop().create_task(evaluate(
                text, surface=surface, chat=chat, session=session,
                persona=persona, hot=hot))
        except RuntimeError:
            pass
        return None
    row = await evaluate(text, surface=surface, chat=chat,
                         session=session, persona=persona, hot=hot)
    if row.get("action") == "deflect" and row.get("reply"):
        return row
    return None


def stats() -> dict[str, int]:
    return dict(_STATS)


def reset() -> None:
    """Test hook — clear the running counters."""
    _STATS.update(evaluated=0, deflected=0, would_deflect=0)
