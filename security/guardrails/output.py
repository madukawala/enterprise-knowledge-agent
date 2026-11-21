"""Checks on an answer before the user sees it.

Two kinds of check:

- **Problems** make the validator reject the draft. It is written again
  once, and if it still fails, the user gets the list of sources instead:
  - it repeats the internal instructions (system prompt leakage);
  - it shows a document label above the user's role, such as
    "Classification: Restricted" for a viewer;
  - it gives advice the bank does not give: investment, tax or legal
    advice, or promises of returns (brand rules).
- **Fixes** are applied to every answer that is shown:
  - images are removed (a Markdown image to an outside address would send
    data to it as soon as the answer is drawn);
  - links to outside sites are turned into plain text;
  - personal data and secrets are masked (core/pii.py).
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.pii import mask
from security.guardrails.text import for_scanning
from security.roles import AccessLevel, Role, allowed_access_levels

# Eight words in a row from the instructions is not a coincidence.
LEAK_SHINGLE_WORDS = 8

# Links to these hosts are kept; everything else becomes plain text.
ALLOWED_LINK_HOSTS = ("helix.example", "localhost")
# Staff work addresses at the bank's own domain stay readable in answers
# (the employee directory is for staff); every other address is masked.
STAFF_EMAIL_DOMAINS = ("helix.example",)

_LABEL = re.compile(
    r"\b(?:classification|access[ _]level|sensitivity|marking)\s*[:=]?\s*\**\s*"
    r"(public|internal|confidential|restricted)\b",
    re.IGNORECASE,
)
_BRACKET_LABEL = re.compile(r"\[(confidential|restricted)\]", re.IGNORECASE)

_ADVICE = (
    r"\byou should (?:buy|sell|invest|purchase|short)\b",
    r"\b(?:i|we) (?:recommend|advise|suggest) "
    r"(?:buying|selling|investing|that you (?:buy|sell|invest))\b",
    r"\bguaranteed (?:returns?|profits?|gains?)\b",
    r"\b(?:this|it) is a (?:good|great|safe) investment\b",
    r"\b(?:as|for) (?:tax|legal) advice\b",
)
_ADVICE_RE = tuple(re.compile(pattern) for pattern in _ADVICE)

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)[^)]*\)")
_BARE_URL = re.compile(r"\bhttps?://[^\s)\]>]+")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", for_scanning(text))


def _shingles(words: list[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


@dataclass(frozen=True)
class PromptFingerprint:
    """Every run of eight words in the internal instructions."""

    shingles: frozenset[tuple[str, ...]]

    @classmethod
    def of(cls, instructions: Iterable[str]) -> "PromptFingerprint":
        shingles: set[tuple[str, ...]] = set()
        for text in instructions:
            shingles |= _shingles(_words(text), LEAK_SHINGLE_WORDS)
        return cls(frozenset(shingles))

    def leaked_in(self, answer: str) -> bool:
        return bool(_shingles(_words(answer), LEAK_SHINGLE_WORDS) & self.shingles)


@dataclass(frozen=True)
class Problem:
    # prompt_leak, restricted_label or advice: for metrics and the security log.
    category: str
    # For the response agent's rewrite notes.
    message: str


def problems(answer: str, *, role: Role, fingerprint: PromptFingerprint | None) -> list[Problem]:
    """What is wrong with an answer, in words the response agent can act on."""
    issues = []
    if fingerprint is not None and fingerprint.leaked_in(answer):
        issues.append(
            Problem(
                "prompt_leak",
                "It repeats the assistant's internal instructions. Answer the question only.",
            )
        )

    allowed = allowed_access_levels(role)
    labels = {m.group(1).lower() for m in _LABEL.finditer(answer)}
    labels |= {m.group(1).lower() for m in _BRACKET_LABEL.finditer(answer)}
    above = sorted(label for label in labels if AccessLevel(label) not in allowed)
    if above:
        issues.append(
            Problem(
                "restricted_label",
                f"It shows a document label the user may not see ({', '.join(above)}). "
                "Leave labels out.",
            )
        )

    plain = for_scanning(answer)
    if any(pattern.search(plain) for pattern in _ADVICE_RE):
        issues.append(
            Problem(
                "advice",
                "It gives investment, tax or legal advice, which the bank's assistant must not do.",
            )
        )
    return issues


@dataclass(frozen=True)
class Fixed:
    text: str
    masked: Counter[str] = field(default_factory=Counter)
    images_removed: int = 0
    links_removed: int = 0

    @property
    def notes(self) -> list[str]:
        notes = []
        if self.masked:
            count = sum(self.masked.values())
            notes.append(
                f"Masked {count} personal or secret value{'s' if count != 1 else ''} "
                f"({', '.join(sorted(self.masked))})."
            )
        if self.images_removed:
            notes.append("Removed images from the answer.")
        if self.links_removed:
            notes.append("Turned links to outside sites into plain text.")
        return notes


def _allowed_host(url: str) -> bool:
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_LINK_HOSTS)


def fix(answer: str) -> Fixed:
    """The answer with images removed, outside links made plain, and personal data masked."""
    images = 0
    links = 0

    def drop_image(match: re.Match[str]) -> str:
        nonlocal images
        images += 1
        return match.group(1)

    def plain_link(match: re.Match[str]) -> str:
        nonlocal links
        if _allowed_host(match.group(2)):
            return match.group(0)
        links += 1
        return match.group(1)

    def plain_url(match: re.Match[str]) -> str:
        nonlocal links
        if _allowed_host(match.group(0)):
            return match.group(0)
        links += 1
        return "[link removed]"

    text = _IMAGE.sub(drop_image, answer)
    text = _LINK.sub(plain_link, text)
    text = _BARE_URL.sub(plain_url, text)
    masked = mask(text, keep_email_domains=STAFF_EMAIL_DOMAINS)
    return Fixed(text=masked.text, masked=masked.found, images_removed=images, links_removed=links)
