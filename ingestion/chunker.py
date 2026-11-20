"""Split documents into chunks that each belong to one section.

1. Split by Markdown headings, so every chunk knows its section ("Root
   cause", "Timeline") and citations point to a real part of the document.
2. If a section is too long, pack its paragraphs into chunks of up to
   `max_tokens`, with a small overlap so a sentence near the cut is not lost.
   A paragraph that is too long on its own is split by lines (tables and
   lists), then by sentences, then by words.

Token counts are estimates. The embedding model allows 512 tokens and its
tokenizer is not installed here, so we count words and punctuation marks
and add a third. That errs on the high side, and the embedding call cuts any
chunk that is still too long instead of failing (ADR-0004).
"""

import math
import re
from dataclasses import dataclass

from ingestion.loader import SourceDocument

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_PIECE_RE = re.compile(r"\w+|[^\w\s]")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")

INTRO_SECTION = "Introduction"


def estimate_tokens(text: str) -> int:
    """A cautious estimate of the embedding model's token count for `text`."""
    return math.ceil(len(_PIECE_RE.findall(text)) * 4 / 3)


@dataclass(frozen=True)
class ChunkingConfig:
    # Under the model's 512-token limit, leaving room for the title and
    # section that are added in front of each chunk before embedding.
    max_tokens: int = 450
    overlap_tokens: int = 50

    def __post_init__(self) -> None:
        if self.max_tokens < 50:
            raise ValueError("max_tokens must be at least 50")
        if not 0 <= self.overlap_tokens < self.max_tokens // 2:
            raise ValueError("overlap_tokens must be at least 0 and under half of max_tokens")


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    chunk_index: int
    section: str
    text: str


@dataclass(frozen=True)
class _Section:
    heading: str
    text: str


def split_sections(body: str) -> list[_Section]:
    """Split a Markdown body at its headings.

    The document title (`#`) is not a section of its own; text right under
    it is called "Introduction". A `###` heading is named together with its
    parent, for example "Discussion > Card payments". Headings inside code
    blocks are ignored. Sections with no text are dropped.
    """
    sections: list[_Section] = []
    trail: list[tuple[int, str]] = []
    current = INTRO_SECTION
    lines: list[str] = []
    in_code = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append(_Section(heading=current, text=text))
        lines.clear()

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_code = not in_code
        heading = None if in_code else _HEADING_RE.match(line)
        if heading is None:
            lines.append(line)
            continue
        flush()
        level = len(heading.group(1))
        if level == 1:
            trail = []
            current = INTRO_SECTION
            continue
        trail = [(lvl, name) for lvl, name in trail if lvl < level]
        trail.append((level, heading.group(2).strip()))
        current = " > ".join(name for _, name in trail)
    flush()
    return sections


def _split_long(text: str, max_tokens: int) -> list[str]:
    """Break text that is over the limit into pieces that fit."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    # Tables and lists: one line at a time. Prose: one sentence at a time.
    lines = [line for line in text.splitlines() if line.strip()]
    parts = lines if len(lines) > 1 else _SENTENCE_END_RE.split(text)
    if len(parts) > 1:
        return [piece for part in parts for piece in _split_long(part, max_tokens)]
    # One very long sentence or line: fall back to fixed-size word windows.
    words = text.split()
    words_per_piece = max(1, max_tokens * 3 // 4 // 2)
    return [
        " ".join(words[start : start + words_per_piece])
        for start in range(0, len(words), words_per_piece)
    ]


def _tail(text: str, overlap_tokens: int) -> str:
    """The last words of a chunk, about `overlap_tokens` long."""
    if overlap_tokens == 0:
        return ""
    words = text.split()
    taken: list[str] = []
    for word in reversed(words):
        if estimate_tokens(" ".join([word, *taken])) > overlap_tokens:
            break
        taken.insert(0, word)
    return " ".join(taken)


# A piece of text and what joins it to the piece before it: a blank line
# between paragraphs, a line break between table rows or list items, and a
# space between sentences of the same paragraph.
_Piece = tuple[str, str]


def _pieces(text: str, max_tokens: int) -> list[_Piece]:
    pieces: list[_Piece] = []
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        joiner = "\n" if "\n" in paragraph else " "
        for position, part in enumerate(_split_long(paragraph, max_tokens)):
            pieces.append(("\n\n" if position == 0 else joiner, part))
    return pieces


def _join(pieces: list[_Piece]) -> str:
    return "".join(part if i == 0 else joiner + part for i, (joiner, part) in enumerate(pieces))


def _overlap(pieces: list[_Piece], overlap_tokens: int) -> list[_Piece]:
    """The end of a finished chunk, repeated at the start of the next one.

    Whole rows or sentences are reused when they fit, so a table row is never
    cut in half. Otherwise the last few words are used.
    """
    taken: list[_Piece] = []
    for piece in reversed(pieces):
        if estimate_tokens(_join([piece, *taken])) > overlap_tokens:
            break
        taken.insert(0, piece)
    if taken:
        return taken
    tail = _tail(pieces[-1][1], overlap_tokens)
    return [("\n\n", tail)] if tail else []


def _pack(text: str, config: ChunkingConfig) -> list[str]:
    """Group paragraphs into chunks of up to max_tokens, with overlap."""
    chunks: list[str] = []
    current: list[_Piece] = []
    for piece in _pieces(text, config.max_tokens):
        if current and estimate_tokens(_join([*current, piece])) > config.max_tokens:
            chunks.append(_join(current))
            carried = _overlap(current, config.overlap_tokens)
            # Skip the overlap if it would push the new chunk over the limit.
            if estimate_tokens(_join([*carried, piece])) > config.max_tokens:
                carried = []
            current = [*carried, piece]
        else:
            current.append(piece)
    if current:
        chunks.append(_join(current))
    return chunks


def chunk_document(document: SourceDocument, config: ChunkingConfig | None = None) -> list[Chunk]:
    """Split one document into numbered chunks, in reading order."""
    settings = config or ChunkingConfig()
    chunks: list[Chunk] = []
    for section in split_sections(document.body):
        for text in _pack(section.text, settings):
            chunks.append(
                Chunk(
                    doc_id=document.meta.doc_id,
                    chunk_index=len(chunks),
                    section=section.heading,
                    text=text,
                )
            )
    return chunks


def embedding_text(document: SourceDocument, chunk: Chunk) -> str:
    """The text that is turned into vectors: title and section, then the chunk.

    The title and section give a short chunk such as "Rolled back at 09:40"
    the context it needs to be found. Only the chunk itself is shown to users.
    """
    return f"{document.meta.title} - {chunk.section}\n\n{chunk.text}"
