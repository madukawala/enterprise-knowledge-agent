from datetime import date
from pathlib import Path

import pytest

from ingestion.loader import CorpusError, load_documents, parse_document
from rag.schema import Collection, Department

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sample_docs"

VALID = """---
doc_id: RB-901
title: Example runbook
department: payments
document_type: runbook
access_level: internal
created_date: 2026-02-01
tags: [payhub]
---
# Example runbook

## Steps

Do the thing.
"""


def write(root: Path, folder: str, name: str, text: str) -> Path:
    path = root / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_parse_reads_front_matter_and_body() -> None:
    document = parse_document(
        VALID, source_path="runbooks/RB-901.md", collection=Collection.RUNBOOKS
    )
    assert document.meta.doc_id == "RB-901"
    assert document.meta.department is Department.PAYMENTS
    assert document.meta.created_date == date(2026, 2, 1)
    assert document.meta.tags == ("payhub",)
    assert document.body.startswith("# Example runbook")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# No front matter\n", "must start with front matter"),
        ("---\n: : :\n---\nbody\n", "not valid YAML"),
        ("---\n- a list\n---\nbody\n", "key: value"),
        (VALID.replace("access_level: internal\n", ""), "access_level"),
        (VALID.replace("internal", "secret"), "access_level"),
        (VALID.replace("payments", "marketing"), "department"),
        (VALID.replace("RB-901", "rb 901"), "doc_id"),
        (VALID.replace("tags:", "unexpected:"), "unexpected"),
        (VALID.replace("document_type: runbook", "document_type: incident"), "belongs in"),
        (VALID.replace("tags: [payhub]", "severity: SEV1"), "only incident"),
        (VALID.replace("# Example runbook", "# Another title"), "does not match"),
        (VALID.split("# Example")[0], "no text"),
    ],
)
def test_parse_rejects_bad_documents(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_document(text, source_path="x.md", collection=Collection.RUNBOOKS)


def test_load_reads_every_fixture_document() -> None:
    documents = load_documents(FIXTURES, base_dir=FIXTURES.parent)
    assert [d.meta.doc_id for d in documents] == ["INC-TEST-001", "POL-TEST-001", "RB-TEST-001"]
    assert documents[0].source_path == "sample_docs/incidents/INC-TEST-001.md"
    assert documents[0].meta.severity == "SEV1"


def test_load_ignores_readme_and_hidden_files(tmp_path: Path) -> None:
    write(tmp_path, "runbooks", "RB-901.md", VALID)
    (tmp_path / "README.md").write_text("# About these documents")
    write(tmp_path, "runbooks", ".DS_Store", "junk")
    write(tmp_path, ".git", "config", "junk")
    assert len(load_documents(tmp_path)) == 1


def test_load_reports_every_problem_at_once(tmp_path: Path) -> None:
    write(tmp_path, "runbooks", "RB-901.md", VALID)
    write(tmp_path, "runbooks", "wrong-name.md", VALID.replace("RB-901", "RB-902"))
    write(tmp_path, "runbooks", "notes.txt", "hello")
    write(tmp_path, "runbooks", "RB-903.md", "no front matter")
    write(tmp_path, "unknown-folder", "X.md", VALID)
    write(tmp_path, "runbooks", "latin1.md", "")
    (tmp_path / "runbooks" / "latin1.md").write_bytes(b"\xff\xfe bad")

    with pytest.raises(CorpusError) as caught:
        load_documents(tmp_path)

    messages = " | ".join(str(p) for p in caught.value.problems)
    assert "file name must be 'RB-902.md'" in messages
    assert "only .md files" in messages
    assert "must start with front matter" in messages
    assert "unknown collection folder" in messages
    assert "not valid UTF-8" in messages
    assert len(caught.value.problems) == 5


def test_duplicate_ids_across_folders_are_rejected(tmp_path: Path) -> None:
    write(tmp_path, "runbooks", "RB-901.md", VALID)
    policy = VALID.replace("document_type: runbook", "document_type: policy")
    write(tmp_path, "policies", "RB-901.md", policy)
    with pytest.raises(CorpusError, match="1 document problem"):
        load_documents(tmp_path)


def test_very_large_files_are_rejected(tmp_path: Path) -> None:
    write(tmp_path, "runbooks", "RB-901.md", VALID + "x" * 300_000)
    with pytest.raises(CorpusError) as caught:
        load_documents(tmp_path)
    assert "larger than" in str(caught.value.problems[0])


def test_missing_or_empty_folder_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="1 document problem"):
        load_documents(tmp_path / "missing")
    with pytest.raises(CorpusError) as caught:
        load_documents(tmp_path)
    assert "no documents found" in str(caught.value.problems[0])
