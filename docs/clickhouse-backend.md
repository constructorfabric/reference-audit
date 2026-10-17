# Local ClickHouse backend

Semantic Scholar, OpenAlex and DBLP can be read from a local [ClickHouse](https://clickhouse.com)
mirror instead of their public APIs: the S2 Academic Graph dump (database `s2ag`), an OpenAlex
snapshot (`openalex`) and the DBLP XML dump (`kb.dblp_publication`). Select it with
`source_backend = "clickhouse"` in the config file (`SOURCE_BACKEND=clickhouse` in `.env`) or
`--backend clickhouse`. The other sources (Crossref, arXiv, Open Library, Google Books, publisher and web fetches) always use their APIs. Those requests stay inside
each API's published limits: they are spaced per source, and Crossref (3 at a time, its polite-pool
limit) and arXiv (1) also cap requests in flight, retries included. arXiv's terms allow one request
every three seconds, so lookups by arXiv id are the slowest step of a large batch. Without these
limits, a batch of a few hundred entries turned about a fifth of them into 429s.

The local sources have no rate limits and no third-party outages. A large batch is then paced by the
API sources that remain: about 45 minutes per thousand references, mostly Crossref's three requests in
flight. On the Semantic Scholar API (1 request/s), the same batch takes about an hour.
The two backends are interchangeable to the rest of the pipeline. The local adapters keep the API
adapters' source names, and shape their rows like the API's JSON before the same normalizers. Their
cached responses are kept apart, and the verdict cache is keyed by backend, so neither backend ever
serves the other's result.

**Title search** needs a full-text index on each searched table. It matches titles that contain
every searchable word of the cited title, shortest title first, then (S2 and OpenAlex, which have
citation counts) most-cited first. When none of those is a near-exact title (and the title has at
least 4 distinct words), it searches again with each word left out in turn, ranked by edit distance
to the cited title. So one misspelt or extra word no longer hides the real paper; two still do.
Before any entry is audited, the run checks that the server answers, that each index exists and is
fully built, and that each snapshot reports a coverage date. If not, it exits rather than scanning
hundreds of millions of rows per reference: for a missing index, with the statements below; for an
index still building, asking you to wait for its `system.mutations` entry to finish.

```sql
ALTER TABLE s2ag.papers           ADD INDEX IF NOT EXISTS idx_title_text title TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));
ALTER TABLE openalex.works_slim   ADD INDEX IF NOT EXISTS idx_title_text title TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));
ALTER TABLE kb.dblp_publication   ADD INDEX IF NOT EXISTS idx_title_text title TYPE text(tokenizer = splitByNonAlpha, preprocessor = lower(title));
ALTER TABLE <each of the above>   MATERIALIZE INDEX idx_title_text;   -- runs in the background; see system.mutations
```

On the reference machine these built in about 6 minutes and take 7.1 GiB (S2), 13.6 GiB (OpenAlex)
and 181 MiB (DBLP). A title lookup then takes 0.1–2 s and an identifier lookup 20–90 ms. A
40-reference HALLMARK sample audits in about 30 s. Identifier
lookups use `s2ag.paper_external_ids` and `openalex.works_slim.doi`, which need no extra index.

What the local data cannot supply, compared with the APIs:

- **Coverage ends at the snapshot.** The run reads each snapshot's end at startup (DBLP's dump date,
  OpenAlex's newest `updated_date`, the newest publication date in the S2 dump) and prints it in the
  report header. On the reference machine: OpenAlex 2026-06-26, DBLP 2026-09-19, S2 current. A work
  newer than its snapshot is not there, so a *no match* for an entry dated in or after a snapshot's
  year carries a note naming the snapshot. Audit very recent papers with the API backend.
- **OpenAlex has no `locations` locally**, so a Work's version links are its primary and best
  open-access locations only. The preprint↔published merge then also rests on the identifier links
  the other sources supply. Pages and the abstract come from the full `openalex.works` table (on
  disk, about 1 s per batch).
- **The DBLP dump table has no landing-page URL and no pages.**
- Title search tolerates one wrong word (see above); the API searches are more forgiving still.
- A cold title search on S2 takes up to about 2 s: the text index is evaluated per part, and the
  largest part holds about 200 M rows. Accepted as is; a finer index granularity could help.
