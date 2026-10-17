# HALLMARK v1.2 public regression corpus

These are byte-for-byte copies of `data/v1.2/dev_public.jsonl` (1,119 records) and
`data/v1.2/test_public.jsonl` (831 records) from
[HALLMARK](https://github.com/rpatrik96/hallmark/tree/f774fa40675daa83eca6201637a94c4536b7bb3e),
commit `f774fa40675daa83eca6201637a94c4536b7bb3e`. `provenance.json` pins the upstream
paths, SHA-256 digests and counts. No checkout, HALLMARK package, evaluator or dataset download is
needed by these tests. The original labels, explanations and relabel history are preserved.

HALLMARK is by **Patrik Reizinger and Wieland Brendel**. The copied data is distributed under its
MIT license; the original copyright and permission notice are in [LICENSE](LICENSE). Please cite
[HALLMARK: Diagnosing Three Failure Modes in LLM Citation Verifiers](https://arxiv.org/abs/2607.18360)
when using this corpus in research:

```bibtex
@misc{reizinger2026hallmarkdiagnosingfailuremodes,
  title={HALLMARK: Diagnosing Three Failure Modes in LLM Citation Verifiers},
  author={Patrik Reizinger and Wieland Brendel},
  year={2026},
  eprint={2607.18360},
  archivePrefix={arXiv},
  primaryClass={cs.CR},
  url={https://arxiv.org/abs/2607.18360},
}
```

`tests/test_hallmark_corpus.py` exercises every example offline, both on its own and in a complete
split. Nine upstream records have truncated, unbalanced field values (seven dev, two test).
They must be reported with a reason, preserve neighbouring entries, and remain not evaluated;
their bibliographic label does not make them parseable. Twelve additional regressions check reviewed
cases against independent source records, including the actual owners of six cited DOIs. Venue
comparisons replay Pydantic field judgments and verify that an ungrounded judgment cannot turn a
different version's venue into proof of non-publication. These tests verify escalation, evidence
guards and reporting, rather than measuring a live LLM's accuracy.

Run the full audit separately:

```bash
REFERENCE_AUDIT_HALLMARK_LIVE=1 uv run pytest tests/test_hallmark_live.py -q -s
# Selecting a key audits only the selected records:
REFERENCE_AUDIT_HALLMARK_LIVE=1 uv run pytest tests/test_hallmark_live.py -k eae03804fd48 -q
```

This uses the repository's `.env`, `~/.config/reference-audit/config.toml` and the environment, requires an enabled LLM and key, and may take an
hour or more and incur LLM charges for the whole corpus. Sources use the configured `SOURCE_BACKEND`.
The tests honor the configured `PARTIAL_AUTHORS` policy, force field checking on, and turn citation
alignment off. Records are
audited in concurrent batches with labels stripped before the pipeline. The cache is temporary by
default; `REFERENCE_AUDIT_HALLMARK_CACHE=/path/to/cache.db` reuses an existing source/LLM cache,
while whole-entry verdicts remain gated by pipeline version/model/backend. Pipeline 0.27 invalidates
the previous venue policy's cached verdicts.

Each case checks reference-audit's native reporting contract. `exactly_one` with unverified metadata,
`multiple`, or a failed/unresolved audit with explicit reasons are legitimate outcomes. Bare null
verdicts, silent findings, unsupported venue errors and violations of independent native
expectations fail. `audits.jsonl` retains the outcomes and `diagnostics.json` records upstream labels,
local label reviews, native expectations and the strict binary mapping without asserting agreement.
With `-s`, the run prints native outcome counts and output paths. The live suite is skipped by default;
the offline suite always runs. A passing contract test does not mean every reference was verified.

## Label review policy

Upstream labels are provenance and benchmark diagnostics, never native test expectations.
[label_reviews.json](label_reviews.json) retains twelve historical proposed label corrections and
one dispute, with reasons and links. Duplicate reviews, missing keys and changed upstream labels fail
loudly. [native_expectations.json](native_expectations.json) separately specifies independently
grounded identity and field expectations for the twelve reviewed cases. Metadata uncertainty is
allowed; a definite negative identity result contradicting an independently established identity or
a clean field contradicting the reviewed discrepancy fails. Author-corrupted references may fail
correspondence even though the underlying paper is real. Other records get consistency/reporting
checks and diagnostics until an independent oracle is reviewed.

Neither HALLMARK's labels, its automated relabel notes, nor the tool's own predictions justify
inventing an oracle. The external benchmark harness still uses upstream labels for comparable metrics.

| Split | Key | Reviewed discrepancy / historical label correction proposal |
| --- | --- | --- |
| dev_public | `d0f7f9c72c33` | IBRNet cites another paper's DOI |
| dev_public | `f802800935ef` | ImageBind cites another paper's DOI |
| dev_public | `ff2931c3228f` | Self-supervised vision transformers cites another paper's DOI |
| dev_public | `ded9f5844e90` | TensoRF cites another paper's DOI |
| dev_public | `a24129d1c5e5` | Flamingo names unrelated authors |
| dev_public | `e9e08922a057` | PaLM names unrelated authors and an incorrect venue |
| dev_public | `b939e55d7555` | Claimed ICML venue differs from the recorded JMLR version |
| dev_public | `c65faf378a95` | OPT names unrelated authors and an incorrect venue |
| test_public | `eae03804fd48` | Mip-NeRF cites Baking Neural Radiance Fields' DOI |
| test_public | `e8e1379d3939` | Masked Autoencoders cites Motion-from-Blur's DOI |
| test_public | `d87ce678eb11` | Llama 2 names unrelated authors |
| test_public | `e7e5083fa94f` | Claimed CVPR venue differs from the recorded ICLR version |

The eight dev corrections were already investigated in
[hallmark_label_errors.md](../../../../benchmarks/hallmark_label_errors.md). The test corrections
use DBLP records `conf/iccv/BarronMTHMS21`, `conf/cvpr/HeCXLDG22`,
`journals/corr/abs-2307-09288`, and `conf/iclr/Zhu0SLE24`, plus Crossref DOI ownership,
the Llama 2 author list and MiniGPT-4's ICLR proceedings (linked in each review).

For the two venue-only proposals, these records establish a real work and a differently published
version, not absence from every other venue. Native field-check regressions require `unverifiable`
from this evidence alone. A publisher/registrant record for the specific cited published DOI, or a
complete catalogue covering the claimed venue/year/track, would be needed to establish a venue error.
Current top-k adapters do not provide complete catalogues; automatic exhaustive retrieval is not
implemented. `SourceRecord.venue_catalogues` accepts explicit, typed inventories from source adapters.

`dev_public/a0527a7c2d1b` remains a historical-title dispute in diagnostics, with no guessed
replacement label and no `xfail` exemption from the native contract. Title punctuation, author-order
and year-tolerance disagreements also remain visible without requiring HALLMARK's binary philosophy.
These twelve reviews are not exhaustive independent verification of all 1,950 labels.

## Independent source snapshots

[recorded_sources.json](recorded_sources.json) projects factual metadata from successful DBLP
ClickHouse-mirror and Crossref API responses recorded on 2026-10-08/09 in the existing benchmark
source cache. It preserves provider IDs, titles, author lists, venues, years and identifiers, with
the capture time and public evidence URL. These records were not synthesized from HALLMARK labels,
its citation fields or reference-audit's predictions. DBLP metadata is maintained by Schloss Dagstuhl
and released under [CC0 1.0](https://dblp.org/db/about/copyright.html); Crossref DOI metadata is
credited through the linked work records. Raw responses and abstracts are omitted.

When refreshing, choose an explicit upstream commit, copy the JSONL files and license unchanged,
update provenance, and re-review all corrections and parsing exceptions. Do not silently follow
upstream `main`, fit expectations to current predictions, or import HALLMARK to load the records.

Pipeline 0.28 adds `contradiction`: the six reviewed DOI/title+author conflicts and four
author-corrupted arXiv citations now require a resolved ID target with contradictory field
evidence. The copied upstream JSONL and label reviews remain provenance records; native
expectations describe the auditor’s current contract. Six recorded DOI cases exercise the
full contradiction path offline.
