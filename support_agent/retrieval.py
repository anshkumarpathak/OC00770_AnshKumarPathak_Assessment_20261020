"""Policy retrieval: BM25 search over the markdown knowledge base.

Each '## ' section of a policy file is one chunk with a stable id such as
'access_policy#2', which is what the agent cites as [kb:access_policy#2].
BM25 is used instead of embeddings so the demo runs offline and every score
can be explained; swapping in a vector store only changes this file.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from support_agent import config

STOPWORDS = set("""a an and any are as at be by can do does for from get has have how i if in is it
its know long me much my need of on or our please policy policies take tell the their there this to whether
want was what when where which who will with you your""".split())
# Words on almost every page of an SAP support knowledge base carry no topic.
STOPWORDS |= {"sap"}


def _stem(t: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if len(t) > len(suffix) + 3 and t.endswith(suffix):
            return t[: -len(suffix)]
    return t


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_title: str
    heading: str
    text: str

    @property
    def source_id(self) -> str:
        return f"kb:{self.chunk_id}"


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [_stem(t) for t in tokens if t not in STOPWORDS]


@lru_cache(maxsize=1)
def load_chunks() -> tuple[Chunk, ...]:
    chunks = []
    for path in sorted(config.KB_DIR.glob("*.md")):
        title, sections = path.stem, []
        for block in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M):
            if block.startswith("# "):
                title = block.splitlines()[0][2:].strip()
                continue
            heading, _, body = block.partition("\n")
            sections.append((heading.strip(), " ".join(body.split())))
        for n, (heading, body) in enumerate(sections, start=1):
            chunks.append(Chunk(f"{path.stem}#{n}", title, heading, body))
    return tuple(chunks)


class BM25Index:
    def __init__(self, chunks, k1: float = 1.5, b: float = 0.75):
        self.chunks = list(chunks)
        self.k1, self.b = k1, b
        self.docs = [tokenize(f"{c.doc_title} {c.heading} {c.heading} {c.text}") for c in self.chunks]
        self.avgdl = sum(map(len, self.docs)) / len(self.docs)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 3, min_score: float = config.RETRIEVAL_MIN_SCORE,
               min_coverage: float = config.RETRIEVAL_MIN_COVERAGE):
        """Coverage = share of the query's (idf-weighted) words found in the chunk.
        Words the knowledge base has never seen count at full weight, so a question
        about something we have no policy for ("cryptocurrency") scores low coverage
        even if one common word ("payment") matches."""
        q = tokenize(query)
        max_idf = max(self.idf.values())
        total_weight = sum(self.idf.get(t, max_idf) for t in set(q)) or 1
        scored = []
        for chunk, doc in zip(self.chunks, self.docs):
            tf = Counter(doc)
            score = sum(
                self.idf.get(t, 0) * tf[t] * (self.k1 + 1)
                / (tf[t] + self.k1 * (1 - self.b + self.b * len(doc) / self.avgdl))
                for t in q if t in tf
            )
            coverage = sum(self.idf[t] for t in set(q) if t in tf) / total_weight
            if score >= min_score and coverage >= min_coverage:
                scored.append((round(score, 2), chunk))
        scored.sort(key=lambda s: -s[0])
        return scored[:k]


# The support desk searches its own six runbooks; other agents pass their own list.
SUPPORT_DOCS = ("access_policy", "change_policy", "data_security", "finance_runbook", "procurement_runbook",
                "sla_policy")


@lru_cache(maxsize=None)
def index_for(docs: tuple[str, ...]) -> BM25Index:
    return BM25Index([c for c in load_chunks() if c.chunk_id.split("#")[0] in docs])


def index() -> BM25Index:
    return index_for(SUPPORT_DOCS)


def get_chunk(chunk_id: str) -> Chunk | None:
    return next((c for c in load_chunks() if c.chunk_id == chunk_id), None)
