"""Loads an evaluation question set from YAML and checks its shape."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rag.filters import SearchFilters
from rag.retrieval import SearchRequest
from rag.schema import Collection
from security.roles import Role

RETRIEVAL_SET = Path(__file__).parent / "datasets" / "retrieval.yaml"


class RetrievalCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    question: str
    role: Role
    expected: tuple[str, ...] = Field(min_length=1)
    collections: tuple[Collection, ...] = tuple(Collection)
    filters: SearchFilters = SearchFilters()

    def request(self, *, alpha: float | None = None, top_k: int | None = None) -> SearchRequest:
        return SearchRequest(
            query=self.question,
            collections=self.collections,
            filters=self.filters,
            alpha=alpha,
            top_k=top_k,
        )


class RetrievalSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[RetrievalCase, ...] = Field(min_length=1)


def load_retrieval_set(path: Path = RETRIEVAL_SET) -> RetrievalSet:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    dataset = RetrievalSet.model_validate(data)
    ids = [case.id for case in dataset.cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} has two questions with the same id.")
    return dataset
