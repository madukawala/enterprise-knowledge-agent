"""Read the Markdown documents and check their front matter.

Layout: one folder per collection, one file per document, named after its ID:

    data/sample_docs/incidents/INC-2026-003.md

Each file starts with YAML front matter (see data/README.md):

    ---
    doc_id: INC-2026-003
    title: Salary-day payment failures in PayHub
    department: payments
    document_type: incident
    access_level: confidential
    created_date: 2026-01-30
    ---

The checks are strict on purpose. A missing or wrong `access_level` would
show a document to the wrong people, so ingestion stops instead of guessing.
All problems are collected and reported together, so they can be fixed in
one go.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rag.schema import COLLECTION_BY_TYPE, Collection, Department, DocumentType, Severity
from security.roles import AccessLevel

# Larger files are almost certainly not hand-written documents.
MAX_FILE_BYTES = 256 * 1024

_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z", re.DOTALL)
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


class FrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Upper-case letters, digits and dashes. No "#", which separates the
    # document ID from the chunk number in record IDs.
    doc_id: str = Field(pattern=r"^[A-Z][A-Z0-9-]{2,40}$")
    title: str = Field(min_length=3, max_length=200)
    department: Department
    document_type: DocumentType
    access_level: AccessLevel
    created_date: date
    owner: str | None = None
    tags: tuple[str, ...] = ()
    severity: Severity | None = None


@dataclass(frozen=True)
class SourceDocument:
    meta: FrontMatter
    collection: Collection
    body: str
    # Relative to the repository root, with forward slashes, for citations.
    source_path: str


@dataclass(frozen=True)
class DocumentProblem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class CorpusError(Exception):
    """One or more documents could not be loaded."""

    def __init__(self, problems: list[DocumentProblem]) -> None:
        self.problems = problems
        super().__init__(f"{len(problems)} document problem(s) found")


def _describe(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "front matter"
        parts.append(f"{field}: {item['msg']}")
    return "; ".join(parts)


def parse_document(text: str, *, source_path: str, collection: Collection) -> SourceDocument:
    """Parse one file. Raises ValueError with a readable message if it is not valid."""
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        raise ValueError("the file must start with front matter between two '---' lines")

    raw_front_matter, body = match.groups()
    try:
        # safe_load only builds plain values (text, numbers, dates, lists),
        # never Python objects, so a document cannot run code.
        data = yaml.safe_load(raw_front_matter)
    except yaml.YAMLError as error:
        raise ValueError(
            f"the front matter is not valid YAML ({error.__class__.__name__})"
        ) from None
    if not isinstance(data, dict):
        raise ValueError("the front matter must be a list of 'key: value' lines")

    try:
        meta = FrontMatter.model_validate(data)
    except ValidationError as error:
        raise ValueError(_describe(error)) from None

    expected_collection = COLLECTION_BY_TYPE[meta.document_type]
    if expected_collection is not collection:
        raise ValueError(
            f"document_type '{meta.document_type}' belongs in the "
            f"'{expected_collection}' folder, not '{collection}'"
        )
    if meta.severity is not None and meta.document_type is not DocumentType.INCIDENT:
        raise ValueError("only incident reports have a severity")
    if not body.strip():
        raise ValueError("the document has no text after the front matter")

    heading = _H1_RE.search(body)
    if heading is not None and heading.group(1).strip() != meta.title:
        raise ValueError(
            f"the '# ' heading '{heading.group(1).strip()}' does not match the title '{meta.title}'"
        )

    return SourceDocument(meta=meta, collection=collection, body=body, source_path=source_path)


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def load_documents(docs_dir: Path, *, base_dir: Path | None = None) -> list[SourceDocument]:
    """Load every document under `docs_dir`, sorted by collection and ID.

    `base_dir` is what `source_path` is made relative to (default: the
    current folder, which is the repository root when run through make).
    Raises CorpusError listing every problem found.
    """
    base = (base_dir or Path.cwd()).resolve()
    root = docs_dir.resolve()
    if not root.is_dir():
        raise CorpusError([DocumentProblem(str(docs_dir), "folder not found")])

    problems: list[DocumentProblem] = []
    documents: list[SourceDocument] = []
    known_collections = {collection.value for collection in Collection}

    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_file():
            # README files and the like at the top level are documentation, not data.
            continue
        if entry.name not in known_collections:
            problems.append(
                DocumentProblem(
                    _relative(entry, base),
                    "unknown collection folder; expected one of: "
                    + ", ".join(sorted(known_collections)),
                )
            )
            continue

        collection = Collection(entry.name)
        for path in sorted(entry.iterdir()):
            if path.name.startswith("."):
                continue
            shown = _relative(path, base)
            # A symbolic link could point anywhere on the machine. Only files
            # that really live inside the documents folder are read.
            if not path.resolve().is_relative_to(root):
                problems.append(DocumentProblem(shown, "points outside the documents folder"))
                continue
            if path.suffix != ".md" or not path.is_file():
                problems.append(DocumentProblem(shown, "only .md files are allowed here"))
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                problems.append(DocumentProblem(shown, f"larger than {MAX_FILE_BYTES} bytes"))
                continue
            try:
                document = parse_document(
                    path.read_text(encoding="utf-8"), source_path=shown, collection=collection
                )
            except UnicodeDecodeError:
                problems.append(DocumentProblem(shown, "not valid UTF-8 text"))
                continue
            except ValueError as error:
                problems.append(DocumentProblem(shown, str(error)))
                continue
            if path.stem != document.meta.doc_id:
                problems.append(
                    DocumentProblem(shown, f"the file name must be '{document.meta.doc_id}.md'")
                )
                continue
            documents.append(document)

    seen: dict[str, str] = {}
    for document in documents:
        first = seen.setdefault(document.meta.doc_id, document.source_path)
        if first != document.source_path:
            problems.append(
                DocumentProblem(
                    document.source_path, f"doc_id {document.meta.doc_id} is also used by {first}"
                )
            )

    if problems:
        raise CorpusError(problems)
    if not documents:
        raise CorpusError([DocumentProblem(_relative(root, base), "no documents found")])
    return documents
