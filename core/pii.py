"""Finds personal data and secrets in text, and masks them.

Used in three places: the answers users see (agents/nodes/validator.py and
the streamed preview), every log line (core/logging.py) and the traces sent
to LangSmith (agents/tracing.py). It lives in `core` because logging is in
`core` too, and `core` imports nothing from the other packages.

Each kind of data has a pattern, and the ones that have a checksum are only
masked when the checksum is right, so ordinary numbers (dates, incident
numbers, amounts) are left alone:

| Kind | Example | Extra check |
|---|---|---|
| EMAIL | jane.doe@example.com | |
| CARD | 4111 1111 1111 1111 | Luhn checksum |
| IBAN | GB82 WEST 1234 5698 7654 32 | ISO 7064 mod-97 |
| UK_SORT_CODE_ACCOUNT | 12-34-56 12345678 | |
| UK_NI_NUMBER | QQ 12 34 56 C | the official prefix rules |
| PHONE | +44 20 7946 0958, 020 7946 0958 | 10 to 15 digits |
| IP_ADDRESS | 203.0.113.7 | each part 0-255 |
| SECRET | sk-..., AIza..., ghp_..., Bearer eyJ... | |

A masked value becomes its kind in square brackets, for example `[EMAIL]`.
This is a set of rules, not a language model: it does not find names or
addresses. ADR-0016 explains why, and what a production system would add.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
# 13 to 19 digits, optionally in groups split by spaces or dashes.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_SORT_CODE_ACCOUNT = re.compile(r"\b\d{2}-\d{2}-\d{2}[ ,/]*\d{8}\b")
_NI_NUMBER = re.compile(
    r"\b(?!BG|GB|KN|NK|NT|TN|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"
)
_PHONE = re.compile(r"(?<![\w-])(?:\+\d{1,3}[ .-]?)?\(?\d{2,5}\)?(?:[ .-]?\d{2,4}){2,4}(?![\w-])")
_IP = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,}"
    r"|pcsk_[A-Za-z0-9_-]{20,}|lsv2_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"\.[A-Za-z0-9_-]{10,})"
)


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def luhn_ok(number: str) -> bool:
    digits = [int(d) for d in _digits(number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            digit -= 9 if digit > 9 else 0
        total += digit
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    compact = value.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        number = int("".join(str(int(char, 36)) for char in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


def _phone_ok(value: str) -> bool:
    digits = _digits(value)
    if not 10 <= len(digits) <= 15:
        return False
    # Dates and times written with separators are not phone numbers.
    return not re.fullmatch(r"\d{4}[-./]\d{2}[-./]\d{2}.*", value.strip())


# Order matters: the more specific kinds go first, so a card number is
# masked as a card and not as a phone number.
_RULES: tuple[tuple[str, re.Pattern[str], Callable[[str], bool] | None], ...] = (
    ("SECRET", _SECRET, None),
    ("EMAIL", _EMAIL, None),
    ("IBAN", _IBAN, iban_ok),
    ("CARD", _CARD, luhn_ok),
    ("UK_SORT_CODE_ACCOUNT", _SORT_CODE_ACCOUNT, None),
    ("UK_NI_NUMBER", _NI_NUMBER, None),
    ("IP_ADDRESS", _IP, None),
    ("PHONE", _PHONE, _phone_ok),
)

KINDS = tuple(kind for kind, _, _ in _RULES)


@dataclass(frozen=True)
class Masked:
    text: str
    # How many values of each kind were masked, e.g. {"EMAIL": 2}.
    found: Counter[str] = field(default_factory=Counter)

    @property
    def changed(self) -> bool:
        return bool(self.found)


def mask(text: str, *, keep_email_domains: tuple[str, ...] = ()) -> Masked:
    """Replace every personal value and secret in `text` with its kind.

    E-mail addresses at `keep_email_domains` are left as they are (the
    bank's own work addresses, in answers to staff). Logs and traces never
    pass any, so every address is masked there.
    """
    found: Counter[str] = Counter()
    keep = tuple(domain.lower() for domain in keep_email_domains)
    for kind, pattern, valid in _RULES:

        def replace(match: re.Match[str], kind: str = kind, valid: object = valid) -> str:
            value = match.group(0)
            if callable(valid) and not valid(value):
                return value
            if kind == "EMAIL" and value.lower().rsplit("@", 1)[-1] in keep:
                return value
            found[kind] += 1
            return f"[{kind}]"

        text = pattern.sub(replace, text)
    return Masked(text=text, found=found)


def mask_text(text: str) -> str:
    return mask(text).text
