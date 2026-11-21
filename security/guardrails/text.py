"""Cleaning text before it is scanned or shown to the language model.

Attackers hide instructions in ways a person reading the page would not
notice: zero-width characters inside words ("ig​nore"), right-to-left
override characters, HTML comments, white text in HTML, letters from other
alphabets that look like Latin ones (an "ignore" whose "i" is Cyrillic), or
full-width letters. `for_scanning()` undoes all of that, so the injection
rules see what the model would see.

`clean_retrieved()` is applied to every retrieved chunk before it reaches
the model: it removes markup and hidden characters but keeps the readable
text, so the answer can still quote it.
"""

import html
import re
import unicodedata

# Letters from other alphabets that look like Latin ones (Cyrillic, Greek,
# Armenian), written as code points so the source shows no look-alikes.
# Not complete, but it covers the ones used in published attacks.
_LOOKALIKES = str.maketrans(
    {
        "\u0430": "a",
        "\u0441": "c",
        "\u0435": "e",
        "\u0456": "i",
        "\u0458": "j",
        "\u043e": "o",
        "\u0440": "p",
        "\u0455": "s",
        "\u0445": "x",
        "\u0443": "y",
        "\u0501": "d",
        "\u0261": "g",
        "\u04bb": "h",
        "\u04cf": "l",
        "\u0578": "n",
        "\u057d": "u",
        "\u03bd": "v",
        "\u03bf": "o",
        "\u03b1": "a",
        "\u03b5": "e",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u0410": "A",
        "\u0412": "B",
        "\u0421": "C",
        "\u0415": "E",
        "\u041d": "H",
        "\u0406": "I",
        "\u041a": "K",
        "\u041c": "M",
        "\u041e": "O",
        "\u0420": "P",
        "\u0422": "T",
        "\u0425": "X",
        "\u04ae": "Y",
    }
)
# Leetspeak, used to slip past word lists: "1gn0re", "pr0mpt".
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HIDDEN_HTML = re.compile(
    r"<(\w+)[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|"
    r"color\s*:\s*(?:white|#fff\b|#ffffff\b))[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z][^>]{0,200}>")
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# A retrieved chunk is cut to this length. Chunks are about 1,500 characters,
# so this only ever trims something that was padded on purpose.
MAX_CHUNK_CHARS = 4000


def remove_invisible(text: str) -> str:
    """Drop control and format characters (zero-width, bidi overrides); keep new lines and tabs."""
    return "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co"}
    )


def for_scanning(text: str) -> str:
    """The text the injection rules look at: one plain, lower-case form.

    Full-width and other compatibility forms become ordinary letters (NFKC),
    invisible characters are removed, look-alike letters become Latin ones,
    and runs of spaces become one. A second form with leetspeak undone is
    added after a separator, so "1gn0re" is caught too.
    """
    plain = remove_invisible(unicodedata.normalize("NFKC", text)).translate(_LOOKALIKES)
    plain = _SPACES.sub(" ", plain.replace("\n", " ")).casefold().strip()
    unleet = plain.translate(_LEET)
    return plain if unleet == plain else f"{plain} ‖ {unleet}"


def clean_retrieved(text: str) -> str:
    """A retrieved chunk as the model should see it: readable text only."""
    text = _HTML_COMMENT.sub(" ", text)
    text = _HIDDEN_HTML.sub(" ", text)
    text = _SCRIPT_OR_STYLE.sub(" ", text)
    text = _MARKDOWN_IMAGE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = remove_invisible(unicodedata.normalize("NFKC", text))
    text = _BLANK_LINES.sub("\n\n", _SPACES.sub(" ", text))
    return text.strip()[:MAX_CHUNK_CHARS]
