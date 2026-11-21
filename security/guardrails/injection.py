"""Rules that spot prompt-injection attempts.

The same rules check two kinds of text:

- **The user's question** (direct injection), in the input guard. A
  question that matches is refused before any model sees it.
- **Each retrieved chunk** (indirect injection: instructions planted in a
  document), in the retrieval agent. A chunk that matches is left out of
  the sources.

The rules look for the shapes attacks take, grouped by what the attacker
wants. Text is first put into one plain form (security/guardrails/text.py),
so hidden characters, look-alike letters, full-width letters and leetspeak
do not get past them. Text that looks like base64 is decoded and checked too.

Rules are not the only defence, and they are not meant to be: the system
prompt marks user and document text as data, the role and the document
access levels are decided in code, and the answer is checked on the way
out. A missed injection can therefore not reach documents or tools above
the user's role (docs/security/PROMPT_INJECTION.md).
"""

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum

from security.guardrails.text import for_scanning


class Category(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    PROMPT_LEAK = "prompt_leak"
    ROLE_ESCALATION = "role_escalation"
    DATA_EXFILTRATION = "data_exfiltration"
    TOOL_ABUSE = "tool_abuse"
    FAKE_DELIMITER = "fake_delimiter"


_IGNORE = r"(?:ignore|disregard|forget|override|bypass|skip|drop|abandon)"
_PREVIOUS = r"(?:all |any |the |your |these |those |my )?(?:previous |prior |above |earlier |preceding |original |system |existing |current )*"
_RULES_WORD = r"(?:instructions?|rules?|prompts?|guidelines?|directives?|policies|constraints|restrictions|guardrails?|context)"

_PATTERNS: dict[Category, tuple[str, ...]] = {
    Category.INSTRUCTION_OVERRIDE: (
        rf"\b{_IGNORE} {_PREVIOUS}{_RULES_WORD}\b",
        r"\b(?:forget|ignore) (?:everything|all of that|what you (?:were|have been) told)\b",
        r"\byou are (?:now|no longer)\b",
        r"\bfrom now on,? (?:you|act|behave|respond|answer)\b",
        r"\b(?:new|updated|real|actual) (?:system )?(?:instructions?|rules|prompt)\s*:",
        r"\b(?:act|behave|respond|pretend) as (?:if you (?:were|are) )?(?:an? )?(?:unrestricted|unfiltered|jailbroken|different|evil|dan)\b",
        r"\b(?:developer|god|jailbreak|debug|admin|sudo) mode\b",
        r"\bdo anything now\b",
        r"\b(?:stop|quit) (?:being|acting as) (?:an? )?(?:assistant|helix)",
    ),
    Category.PROMPT_LEAK: (
        r"\b(?:reveal|show|print|repeat|output|display|tell me|give me|share|dump|leak|recite|spell out|what (?:is|are|were)) (?:me )?(?:your |the |all |any )*(?:full |complete |exact |original |hidden |secret |initial |internal )*(?:system (?:prompt|message|instructions?)|prompts?|instructions you (?:were|have been) given|(?:initial|hidden|secret|internal) (?:instructions?|rules))\b",
        r"\bwhat (?:were|are) you told (?:before|at the start|to do)\b",
        r"\b(?:text|words|everything) (?:above|before) (?:this|my) (?:line|message|question)\b",
    ),
    Category.ROLE_ESCALATION: (
        r"\b(?:i am|i'm|im|as) (?:an?|the) (?:admin|administrator|superuser|root|analyst)\b.{0,40}\b(?:so|therefore|give|show|grant|allow|let)\b",
        r"\b(?:set|change|switch|upgrade|elevate|escalate) (?:my |the user'?s? )?(?:role|access|permissions?|access[_ ]level|clearance)\b",
        r"\b(?:grant|give) me (?:admin|administrator|analyst|full|elevated|restricted) (?:access|rights|role|permissions?)\b",
        r"\btreat me as (?:an? )?(?:admin|administrator|analyst)\b",
        r"\baccess[_ ]level\s*[:=]\s*[\"']?(?:restricted|confidential|admin|all|any|\*)",
        r"\brole\s*[:=]\s*[\"']?(?:admin|administrator|analyst)\b",
    ),
    Category.DATA_EXFILTRATION: (
        r"\b(?:send|post|upload|forward|email|exfiltrate|transmit) (?:this|the|all|every|these|our)? ?(?:data|conversation|documents?|answer|results?|chat|context|history|secrets?|keys?) (?:to|at)\b",
        r"!\[[^\]]*\]\(\s*https?://",
        r"\bhttps?://[^\s)]+[?&](?:q|data|d|payload|secret|token|c)=",
        r"\b(?:list|show|dump|print) (?:all |every )(?:documents?|files?) (?:in|from) (?:the )?(?:index|database|vector store|pinecone)\b",
        r"\b(?:api[_ ]?keys?|passwords?|secrets?|tokens?|credentials) (?:from|in|of) (?:the )?(?:env|environment|config|\.env|system)\b",
    ),
    Category.TOOL_ABUSE: (
        r"\b(?:call|run|use|invoke|execute|trigger) (?:the )?(?:admin|re-?index|reindex|delete|drop) (?:tool|function|command|endpoint)\b",
        r"\b(?:import os|subprocess|os\.system|__import__|eval\(|exec\(|rm -rf)\b",
        r"\bdrop table\b|;\s*--",
    ),
    Category.FAKE_DELIMITER: (
        r"</?(?:system|sources|question|conversation|instructions?|assistant|developer)>",
        r"\[/?(?:inst|system)\]",
        r"<\|(?:im_start|im_end|system|endoftext)\|>",
        r"^#{2,} ?(?:system|instructions?)\b|\s#{2,} ?(?:system|instructions?)\b",
        r"\b(?:end of (?:user )?(?:input|question|document|sources)|begin (?:new )?(?:system|instructions?))\b",
    ),
}

_COMPILED = {
    category: tuple(re.compile(pattern) for pattern in patterns)
    for category, patterns in _PATTERNS.items()
}
# Looks like base64 and is long enough to hold an instruction.
_BASE64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


@dataclass(frozen=True)
class Finding:
    flagged: bool
    categories: tuple[Category, ...] = ()

    @property
    def summary(self) -> str:
        return ", ".join(category.value.replace("_", " ") for category in self.categories)


def _match(text: str) -> set[Category]:
    plain = for_scanning(text)
    return {
        category
        for category, patterns in _COMPILED.items()
        if any(pattern.search(plain) for pattern in patterns)
    }


def _decoded(text: str) -> list[str]:
    """Text hidden as base64 inside `text`, if any."""
    found = []
    for blob in _BASE64.findall(text):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded.isprintable() or "\n" in decoded:
            found.append(decoded)
    return found


def scan(text: str) -> Finding:
    """Which kinds of injection `text` looks like, if any."""
    categories = _match(text)
    for hidden in _decoded(text):
        categories |= _match(hidden)
    ordered = tuple(category for category in Category if category in categories)
    return Finding(flagged=bool(ordered), categories=ordered)
