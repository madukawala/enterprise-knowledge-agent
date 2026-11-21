from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ingestion.chunker import (
    INTRO_SECTION,
    ChunkingConfig,
    chunk_document,
    embedding_text,
    estimate_tokens,
    split_sections,
)
from ingestion.loader import FrontMatter, SourceDocument
from rag.schema import Collection, Department, DocumentType
from security.roles import AccessLevel


def make_document(body: str) -> SourceDocument:
    meta = FrontMatter(
        doc_id="RB-900",
        title="Sample runbook",
        department=Department.PAYMENTS,
        document_type=DocumentType.RUNBOOK,
        access_level=AccessLevel.INTERNAL,
        created_date=date(2026, 1, 1),
    )
    return SourceDocument(
        meta=meta, collection=Collection.RUNBOOKS, body=body, source_path="x/RB-900.md"
    )


def test_sections_follow_headings_and_name_their_parents() -> None:
    body = (
        "# Sample runbook\n\nIntro text.\n\n## Checks\n\nCheck the pool.\n\n"
        "## Discussion\n\n### Card payments\n\nCards were fine.\n"
    )
    sections = split_sections(body)
    assert [s.heading for s in sections] == [INTRO_SECTION, "Checks", "Discussion > Card payments"]
    # "Discussion" has no text of its own, so it is not a section.


def test_headings_inside_code_blocks_are_ignored() -> None:
    body = "## Steps\n\n```\n# not a heading\nkubectl get pods\n```\n"
    sections = split_sections(body)
    assert len(sections) == 1
    assert "# not a heading" in sections[0].text


def test_a_short_section_becomes_one_chunk() -> None:
    chunks = chunk_document(make_document("## Root cause\n\nThe certificate expired."))
    assert len(chunks) == 1
    assert chunks[0].section == "Root cause"
    assert chunks[0].chunk_index == 0


def test_a_long_section_is_split_under_the_limit_with_overlap() -> None:
    paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(12))
    config = ChunkingConfig(max_tokens=120, overlap_tokens=20)
    chunks = chunk_document(make_document(f"## Timeline\n\n{paragraphs}"), config)

    assert len(chunks) > 1
    assert all(estimate_tokens(c.text) <= config.max_tokens for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # The start of each chunk repeats the end of the one before.
    assert chunks[1].text.split()[0] in chunks[0].text


def test_a_long_table_is_split_by_lines() -> None:
    rows = "\n".join(
        f"| 09:{i:02d} | Step number {i} done by the on-call engineer |" for i in range(60)
    )
    config = ChunkingConfig(max_tokens=100, overlap_tokens=0)
    chunks = chunk_document(make_document(f"## Timeline\n\n{rows}"), config)
    assert len(chunks) > 1
    assert all(estimate_tokens(c.text) <= config.max_tokens for c in chunks)
    assert all(line.startswith("|") for c in chunks for line in c.text.splitlines())


def test_one_huge_sentence_falls_back_to_word_windows() -> None:
    config = ChunkingConfig(max_tokens=60, overlap_tokens=0)
    chunks = chunk_document(make_document("## Notes\n\n" + "alpha " * 400), config)
    assert len(chunks) > 1
    assert sum(len(c.text.split()) for c in chunks) == 400


def test_embedding_text_adds_title_and_section() -> None:
    document = make_document("## Checks\n\nCheck the pool.")
    chunk = chunk_document(document)[0]
    assert embedding_text(document, chunk) == "Sample runbook - Checks\n\nCheck the pool."


@pytest.mark.parametrize(("max_tokens", "overlap"), [(10, 0), (100, 60), (100, -1)])
def test_bad_chunking_settings_are_rejected(max_tokens: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="must be"):
        ChunkingConfig(max_tokens=max_tokens, overlap_tokens=overlap)


words = st.text(alphabet="abcdefghij .\n", min_size=1, max_size=2000)


@settings(max_examples=50)
@given(words)
def test_no_words_are_lost(text: str) -> None:
    config = ChunkingConfig(max_tokens=60, overlap_tokens=10)
    chunks = chunk_document(make_document("## Body\n\n" + text), config)
    found = {word for c in chunks for word in c.text.split()}
    assert set(text.split()) <= found
