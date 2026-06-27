"""SAME-OBJECT clustering: formal relations + SAME_WORK LLM tie-break (the crux of step 1)."""

from reference_audit.config import AuditConfig
from reference_audit.matching.sameobject import cluster_accepted, formal_relation
from reference_audit.models import (
    CandidateAssessment,
    FeatureVector,
    Identifiers,
    SameWorkResult,
    SourceRecord,
)

CFG = AuditConfig(model="t")


def _rec(source="s", doi=None, arxiv=None, title="T", authors=None, pages="", venue=""):
    return SourceRecord(source=source, title=title, authors=authors or ["A. B."], pages=pages,
                        venue=venue, ids=Identifiers(doi=doi, arxiv_id=arxiv))


def _acc(rec):
    return CandidateAssessment(record=rec, features=FeatureVector(), bucket="auto_accept")


class FakeLLM:
    def __init__(self, relation):
        self.relation = relation
        self.calls = 0

    async def structured(self, system, user, schema_model, schema_name):
        self.calls += 1
        return SameWorkResult(relation=self.relation, confidence="high", reason="r")

    async def aclose(self):
        pass


# ── formal relations ────────────────────────────────────────────────────────────
def test_shared_doi_is_same():
    a = _rec(doi="10.1/x"); b = _rec(source="o", doi="10.1/x")
    assert formal_relation(a, b, CFG) == "same"


def test_disjoint_pages_same_venue_is_distinct():
    # laughlin: same venue/year, disjoint pages → distinct (V2), never reaches LLM
    a = _rec(doi="10.1073/pnas.97.1.28", title="The Theory of Everything",
             authors=["Laughlin", "Pines"], pages="28-31", venue="PNAS")
    b = _rec(doi="10.1073/pnas.97.1.32", title="The Middle Way",
             authors=["Laughlin", "Pines"], pages="32-37", venue="PNAS")
    assert formal_relation(a, b, CFG) == "distinct"


def test_prefix_trap_is_distinct():
    a = _rec(doi="10.1/a", title="Multiscale structural complexity of natural patterns",
             authors=["Bagrov", "Katsnelson"])
    b = _rec(doi="10.2/b",
             title="Multiscale structural complexity as a quantitative measure of visual complexity",
             authors=["Kravchenko", "Bagrov", "Katsnelson"])
    assert formal_relation(a, b, CFG) == "distinct"


def test_identical_title_distinct_dois_is_ambiguous():
    # fu2023: one paper, two distinct published DOIs, identical title+authors → LLM tie-break
    t = "DreamSim: Learning New Dimensions of Human Visual Similarity"
    au = ["Stephanie Fu", "Phillip Isola"]
    a = _rec(doi="10.52202/075280-2208", title=t, authors=au)
    b = _rec(doi="10.5555/3666122.3668330", title=t, authors=au)
    assert formal_relation(a, b, CFG) == "ambiguous"


# ── clustering with the SAME_WORK LLM ────────────────────────────────────────────
async def test_ambiguous_pair_merges_when_llm_says_same():
    t = "DreamSim: Learning New Dimensions of Human Visual Similarity"
    au = ["Stephanie Fu", "Phillip Isola"]
    accepted = [_acc(_rec(doi="10.52202/075280-2208", title=t, authors=au)),
                _acc(_rec(doi="10.5555/3666122.3668330", title=t, authors=au))]
    llm = FakeLLM("same_artifact")
    artifacts, errored = await cluster_accepted(accepted, CFG, llm, None)
    assert errored is False
    assert len(artifacts) == 1            # merged → exactly_one
    assert llm.calls == 1


async def test_ambiguous_pair_stays_split_when_llm_says_distinct():
    t = "DreamSim: Learning New Dimensions of Human Visual Similarity"
    au = ["Stephanie Fu", "Phillip Isola"]
    accepted = [_acc(_rec(doi="10.1/a", title=t, authors=au)),
                _acc(_rec(doi="10.2/b", title=t, authors=au))]
    artifacts, _ = await cluster_accepted(accepted, CFG, FakeLLM("distinct_works"), None)
    assert len(artifacts) == 2            # stays multiple


async def test_distinct_pair_never_calls_llm():
    # disjoint pages → formal distinct; LLM must not be consulted
    a = _rec(doi="10.1/a", title="X", pages="1-5", venue="J")
    b = _rec(doi="10.2/b", title="Y different", pages="6-9", venue="J")
    llm = FakeLLM("same_artifact")
    artifacts, _ = await cluster_accepted([_acc(a), _acc(b)], CFG, llm, None)
    assert len(artifacts) == 2
    assert llm.calls == 0


async def test_single_accepted_is_one_artifact():
    artifacts, _ = await cluster_accepted([_acc(_rec(doi="10.1/x"))], CFG, None, None)
    assert len(artifacts) == 1
