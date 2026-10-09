# HALLMARK v1.2 `dev_public`: entries labelled VALID that are hallucinated

These eight `dev_public` entries are labelled `VALID` in HALLMARK v1.2 (commit
`f774fa40675daa83eca6201637a94c4536b7bb3e`), but each one cites metadata that does not belong to the
paper. All eight were originally labelled `HALLUCINATED`, and all eight were relabelled `VALID` by
`systematic-relabel-2026-05-30` (the `relabeled_by` field). The relabelling heuristic's own notes, in
`relabel_reason`, show where it went wrong:
- it accepted an author list as "faithful" at a subset ratio below 1 (0.79, 0.80, 0.92);
- it did not check whose DOI was cited;
- it took "venue not refuted" for confirmed when the only record it had was the arXiv copy.

In every case, the original label was right.

reference-audit flags all eight as metadata errors on a real paper, so the `strict` mapping counts
each as HALLUCINATED, and HALLMARK scores it as a false positive.

## How to check

Every claim below can be checked against public records:
- **DOIs:** `https://doi.org/<doi>` resolves to the cited DOI's own paper, and Crossref or OpenAlex
  give its title.
- **Author lists and venues:** the DBLP record keys are given; open them at
  `https://dblp.org/rec/<key>`.

The comparisons here were made with this repository's local OpenAlex and DBLP mirrors. Author names
were compared person by person, ignoring order and the form of the given name (initials, middle
names, common diminutives such as Liam / William). This is the comparison
`reference_audit.matching.names.same_person` makes.

## Summary

| key | cited work | what is wrong |
| --- | --- | --- |
| `d0f7f9c72c33` | IBRNet (CVPR 2021) | The DOI is another CVPR 2021 paper's |
| `f802800935ef` | ImageBind (CVPR 2023) | The DOI is another CVPR 2023 paper's |
| `ff2931c3228f` | An Empirical Study of Training Self-Supervised Vision Transformers (ICCV 2021) | The DOI is another ICCV 2021 paper's |
| `ded9f5844e90` | TensoRF (ECCV 2022) | The DOI is another ECCV 2022 paper's |
| `a24129d1c5e5` | Flamingo (NeurIPS 2022) | 4 of 19 cited authors are not authors of the paper |
| `e9e08922a057` | PaLM, cited as ICML 2022 | 13 of 63 cited authors are not authors of the paper, one author is listed twice, and PaLM did not appear at ICML |
| `b939e55d7555` | PaLM, cited as ICML 2022 | PaLM did not appear at ICML |
| `c65faf378a95` | OPT, cited as ACL 2022 | 3 of 10 named authors are not authors of the paper, and OPT appeared only on arXiv |

## The cited DOI belongs to another paper

The title, authors, venue and year are correct. The DOI identifies a different paper from the same
proceedings, a few numbers away.

**`d0f7f9c72c33`: IBRNet: Learning Multi-View Image-Based Rendering (CVPR 2021)**
- Cited DOI: `10.1109/CVPR46437.2021.00469`, which is "Delving into Localization Errors for Monocular
  3D Object Detection".
- IBRNet's DOI is `10.1109/CVPR46437.2021.00466` (DBLP `conf/cvpr/WangWGSZBMSF21`).

**`f802800935ef`: ImageBind: One Embedding Space To Bind Them All (CVPR 2023)**
- Cited DOI: `10.1109/CVPR52729.2023.02141`, which is "Spatial-Frequency Mutual Learning for Face
  Super-Resolution".
- ImageBind's DOI is `10.1109/CVPR52729.2023.01457` (DBLP `conf/cvpr/GirdharELSAJM23`).

**`ff2931c3228f`: An Empirical Study of Training Self-Supervised Vision Transformers (ICCV 2021)**
- Cited DOI: `10.1109/ICCV48922.2021.00952`, which is "Do Image Classifiers Generalize Across Time?".
- The paper's DOI is `10.1109/ICCV48922.2021.00950` (DBLP `conf/iccv/ChenXH21`).

**`ded9f5844e90`: TensoRF: Tensorial Radiance Fields (ECCV 2022)**
- Cited DOI: `10.1007/978-3-031-19830-4_20`, which is "ESS: Learning Event-Based Semantic Segmentation
  from Still Images".
- TensoRF's DOI is `10.1007/978-3-031-19824-3_20` (DBLP `conf/eccv/ChenXGYS22`).

HALLMARK's relabel notes read "FULLY-CORRECT citation: title exact, authors faithful … venue not
refuted, year_delta=0". Title, authors, venue and year were compared; the DOI was not.

## Authors who are not authors of the paper, or a venue the paper never appeared in

**`a24129d1c5e5`: Flamingo: a Visual Language Model for Few-Shot Learning (NeurIPS 2022)**
- The paper has 27 authors, in both DBLP `conf/nips/AlayracDLMBHLMM22` and
  `journals/corr/abs-2204-14198`.
- The entry cites 19. Four of them are not authors of the paper: Skanda Koppula, Michal Valko,
  Ludovic Denoyer and João Carreira.
- HALLMARK's relabel note: "authors faithful (verdict=faithful, subset=0.7895…)". 15 of 19 is 0.79:
  the four names were seen and accepted.
- (João Carreira escaped reference-audit up to pipeline 0.21: its surname-only fuzzy match took him
  for the paper's Ricardo Barreira. Pipeline 0.22 compares whole names.)

**`e9e08922a057`: PaLM: Scaling Language Modeling with Pathways, cited as ICML 2022**
- PaLM has 67 authors (DBLP `journals/jmlr/ChowdheryNDBMRBCSGSSTMRBTSPRDHPBAI23`, JMLR 2023; also
  `journals/corr/abs-2204-02311`). It was published in JMLR in 2023 and as an arXiv preprint in 2022;
  it did not appear at ICML.
- The entry cites 63 authors. Thirteen are not authors of the paper:
  - near-namesakes of real authors: Peter Schuh (Parker Schuh), Peter Barnes (Parker Barnes), Trevor
    Duke (Toju Duke), Shachi Agrawal (Shivani Agrawal), Trilok Pillai (Thanumalayan Sankaranarayana
    Pillai), Nitish Shirish Keskar Rao (Abhishek Rao), Ekin Dogus Cubuk Moreira (Erica Moreira);
  - people with no counterpart: Armand Joulin, Arman Cohan, Yiming Wang, Mostafa Dehghani, Yonghui Wu,
    Ed Chi.
- Two other names differ from DBLP's, but plausibly name the same people, so they are not counted:
  William Fedus (DBLP: Liam Fedus) and Aleksandar Spiridonov (DBLP: Alexander Spiridonov).
- Noam Shazeer is listed twice.
- HALLMARK's relabel note: "subset=0.9206".

**`b939e55d7555`: PaLM: Scaling Language Modeling with Pathways, cited as ICML 2022**
- A second PaLM entry. Its ten named authors are PaLM's first ten, followed by `and others`.
- It cites the venue as ICML 2022, but PaLM appeared in JMLR (2023) and on arXiv (2022), never at
  ICML (DBLP `journals/jmlr/ChowdheryNDBMRBCSGSSTMRBTSPRDHPBAI23`, `journals/corr/abs-2204-02311`).
  This is HALLMARK's own `wrong_venue` pattern.
- HALLMARK's relabel note: "venue not refuted … via datacite (10.48550/arxiv.2204.02311)". The only
  record it compared the venue with was the arXiv copy, which has no conference.

**`c65faf378a95`: OPT: Open Pre-trained Transformer Language Models, cited as ACL 2022**
- OPT has 19 authors and was published only as an arXiv report (DBLP `journals/corr/abs-2205-01068`;
  DBLP has no ACL record).
- The entry names 10 authors and ends with `and others`. Three of them are not authors of the paper:
  Mona Dewan (the paper has Christopher Dewan), Emily Dinan and Zhiguo Du.
- HALLMARK's relabel note: "subset=0.8, truncated=True".

## Not counted here

Two other `strict` false positives at 0.21 are disagreements of judgement, not label errors:
- `e73343fc5b98` cites its title with "in the" repeated: a typo, flagged as a title error.
- `d3c90ed9a18e` (XCiT) cites "Alaaeldin Ali" for the author DBLP lists as Alaaeldin El-Nouby.
