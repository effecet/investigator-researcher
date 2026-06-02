# MCP Hybrid Domain Router — Design Spec

**Date:** 2026-03-31
**Version target:** v2.1.0
**Status:** Draft
**Scope:** `skill/SKILL.md` (single file, ~365 new lines)

---

## Context

The researcher-investigator skill (v2.0) uses WebSearch + WebFetch exclusively for
source discovery and content extraction. This works but has friction:

- **Paywall errors:** WebFetch on PubMed/arXiv URLs frequently returns 403/404/timeout.
  The error budget (5 cumulative) burns fast on paywalled journals.
- **Unstructured data:** WebFetch returns raw HTML. Authors, affiliations, funding, DOIs,
  and sample sizes must be heuristically extracted from page content.
- **No API access:** PubMed's 37M+ indexed articles, Hugging Face's paper search, and
  Context7's live documentation are all available as authenticated MCP servers — but the
  skill doesn't use them.

The MCP Hybrid Domain Router adds a parallel MCP track alongside the existing web pipeline,
controlled by domain profiles. MCP servers are tried first; WebSearch/WebFetch fills gaps.

---

## Design Principles

1. **MCP-first, web fallback** — MCP servers are preferred sources. Web fills uncovered domains.
2. **Pre-MCP pipeline is always the complete fallback** — removing MCP produces identical v2.0 behavior.
3. **Domain profiles control routing** — only profiles with `mcp_sources` use MCP. Others are untouched.
4. **MCP results skip WebFetch** — they arrive already structured and feed directly into Semantic Compression.
5. **Separate error budgets** — MCP errors never count against the web error budget.

---

## Architecture

```
Phase 0 → detect domain → check mcp_sources
                |
        +-------+-------+
        |               |
   Phase 1a          Phase 1b
   MCP Discovery     Web Discovery
   (PubMed, HF,     (WebSearch for
    Context7)         uncovered sources)
        +-------+-------+
                |
           merged ranked list
                |
        +-------+-------+
        |               |
   Phase 2a          Phase 2b
   MCP Extraction    Web Extraction
   (structured,      (Agent dispatch
    no WebFetch)      + WebFetch)
        +-------+-------+
                |
           Phase 2c
           Track Merge
                |
   Phase 2d: Primary Source Chasing
   (PubMed find_related_articles OR WebSearch fallback)
                |
   Phase 2.5+ unchanged (compression, scrutiny, generation)
```

---

## 1. Domain Profile `mcp_sources` Schema

### Schema

```yaml
mcp_sources:                          # optional — omit for web-only profiles
  - server: <mcp-server-name>         # e.g., "pubmed", "huggingface", "context7"
    tools:
      discovery: <tool-name>          # used in Phase 1a
      extraction: <tool-name>         # used in Phase 2a (optional — some tools return content at discovery)
      metadata: <tool-name>           # used for structured metadata enrichment (optional)
      related: <tool-name>            # used in Phase 2d primary source chasing (optional)
      auxiliary: [<tool-name>, ...]   # other tools used opportunistically (optional)
    query_mapping: <instructions>     # how to convert web queries to MCP query format
    activation_condition: <condition> # when to activate this server (optional — default: always)
    fallback_search: <web-modifier>   # WebSearch modifier when MCP is unavailable
```

### Medicine Profile Addition

```yaml
mcp_sources:
  - server: pubmed
    tools:
      discovery: search_articles
      extraction: get_full_text_article
      metadata: get_article_metadata
      related: find_related_articles
      auxiliary: [convert_article_ids, get_copyright_status, lookup_article_by_citation]
    query_mapping: |
      Convert web queries to PubMed syntax:
      - Add [Publication Type] tags: "systematic review" → "systematic review[pt]"
      - Use MeSH terms where applicable
      - Set date_from from profile year_range
      - Set max_results by depth: brief=10, standard=15, deep=25
    fallback_search: "site:pubmed.ncbi.nlm.nih.gov OR site:pmc.ncbi.nlm.nih.gov"
```

### Technology Profile Addition

```yaml
mcp_sources:
  - server: huggingface
    tools:
      discovery: paper_search
      metadata: hub_repo_details
      auxiliary: [hub_repo_search]
    query_mapping: |
      Use semantic natural-language queries (not boolean syntax).
      Set results_limit by depth: brief=5, standard=10, deep=20.
      For papers referencing specific models/datasets, also query hub_repo_search.
    fallback_search: "site:arxiv.org OR site:huggingface.co"
  - server: context7
    tools:
      discovery: resolve-library-id
      extraction: query-docs
    query_mapping: |
      First resolve-library-id to get the library ID.
      Then query-docs with adapted search queries.
    activation_condition: "Topic names a specific library, framework, SDK, or API by name"
    fallback_search: "official documentation"
```

### Profiles Without MCP

History, social-sciences, and general profiles: **no `mcp_sources` field**. Zero behavioral change.

### Custom Profile Schema

The existing custom profile schema gains an optional `mcp_sources` field following the same
schema above. Users may reference any MCP server available in their environment.

---

## 2. MCP Availability and Fallback

**New section in SKILL.md, after "Token-Aware Source Caps" (line ~317).**

### MCP Availability Probe

At the start of Phase 1a, probe each configured MCP server with a minimal call:
- PubMed: `search_articles` with `query="test"`, `max_results=1`
- Hugging Face: `paper_search` with `query="test"`, `results_limit=1`
- Context7: `resolve-library-id` with a known library name

If the probe fails (timeout, error, tool not found):
1. Mark that server as **unavailable** for the entire run
2. Add its `fallback_search` modifier to Phase 1b web queries
3. Increase Phase 1b web query count by 1-2 to compensate
4. Never retry that server during this run
5. Log: `MCP probe failed: {server} — falling back to web for {source_type}`

### Fallback Scenarios

| Scenario | Behavior |
|----------|----------|
| Server unavailable at probe | Skip server, add fallback_search to web queries |
| All MCP servers unavailable | Skip Phase 1a/2a entirely. Pipeline = v2.0 |
| Mid-pipeline failure (extraction) | PubMed: fall back to abstract-only. HF/Context7: fall back to WebFetch on article URL |

### MCP Error Budget (Separate from Web)

- MCP errors are tracked separately and **never** count against the web error budget
- MCP errors do not trigger the 3-consecutive-errors pause
- If an MCP extraction fails, the source is demoted to abstract-only (not dropped)
- MCP errors are logged in the fetch audit log with `channel: mcp:{server}`

---

## 3. Phase 1a — MCP Source Discovery

**New section in SKILL.md, inserted before existing Phase 1 (line ~320).**

### PubMed Discovery (medicine domain)

For each planned search query:

1. Convert to PubMed syntax per `query_mapping`
2. Call `search_articles` with converted query and depth-dependent `max_results`
3. Batch all returned PMIDs into `get_article_metadata` calls (groups of 10)
4. Call `convert_article_ids` to check PMCID availability → sets `has_full_text` boolean
5. For `deep` depth only: call `get_copyright_status` on top candidates to pre-filter
   for open-access full text (skip for `brief`/`standard` to conserve token budget)

Each result captures:
- `pmid`, `pmcid` (if available), `doi`
- `title`, `authors`, `affiliations`, `year`, `journal`
- `abstract`, `mesh_terms`, `funding`
- `has_full_text`: boolean
- `source_channel`: `"mcp:pubmed"`

### Hugging Face Discovery (technology domain)

1. Call `paper_search` with semantic queries, `results_limit` by depth
2. For papers referencing specific models/datasets, call `hub_repo_search`
3. Call `hub_repo_details` for metadata on referenced repositories

Each result captures:
- `paper_id` (arXiv ID), `title`, `authors`, `year`
- `abstract`, `url`
- `related_repos`: list of model/dataset repos (if any)
- `source_channel`: `"mcp:huggingface"`

### Context7 Discovery (technology domain, conditional)

Only activated when topic names a specific library/framework/SDK:

1. Call `resolve-library-id` to find the library ID
2. Call `query-docs` with adapted queries

Each result captures:
- `library_id`, `title`, `url`
- `content` (already extracted — full documentation text)
- `source_channel`: `"mcp:context7"`

Context7 results are **already extracted content** — they skip Phase 2a entirely
and go directly to the Track Merge (Phase 2c).

### MCP Result Aggregation

After all MCP servers return:
1. Deduplicate across servers (DOI match, title+author match)
2. Count total MCP sources
3. Calculate `web_target = depth_source_target - mcp_source_count`
4. If `web_target <= 0`: run only 1-2 diversity-focused web queries in Phase 1b
5. If `web_target > 0`: run full Phase 1b with adjusted query count

---

## 4. Phase 1b — Web Source Discovery (Renamed)

**Modifications to existing Phase 1 (lines 320-359).**

### Changes

1. **Preamble:** "Phase 1b handles web-based source discovery. If MCP sources were
   found in Phase 1a, reduce web queries accordingly."
2. **Exclusion modifiers:** Add `-site:pubmed.ncbi.nlm.nih.gov` (etc.) to web queries
   when PubMed MCP returned results, to avoid duplicate fetching.
3. **Focus remaining queries** on source types MCP cannot cover: government guidelines,
   .edu research pages, news media, think tanks.
4. **Deduplication** now operates on the merged MCP + web list. MCP results take
   priority over web duplicates of the same source.
5. **All existing logic** (ranking, incremental mode, source priority) is unchanged.

---

## 5. Phase 2a — MCP Content Extraction

**New section in SKILL.md, inserted before existing Phase 2 Agent Dispatch (line ~366).**

### PubMed Full-Text Extraction

For PubMed sources with `has_full_text = true`:
- Call `get_full_text_article` with PMCID, in batches of 5
- Returned text is structured (sections, references) — no HTML parsing needed
- Map to the Structured Extraction Schema (see below)

For PubMed sources with `has_full_text = false`:
- Use the abstract from `get_article_metadata`
- Mark as `[abstract-only]` in extraction
- `extraction_depth: "abstract-only"`

### Hugging Face Paper Extraction

- Use the abstract from `paper_search` results
- `extraction_depth: "abstract-only"` (arXiv full text not available via MCP)
- For `deep` depth: if the paper URL points to open-access arXiv (arxiv.org/abs/...),
  WebFetch the HTML version for full text. For `brief`/`standard`: abstract is sufficient

### Context7 Extraction

- Already complete at discovery — content from `query-docs` is final
- `extraction_depth: "full-text"`

### Structured Extraction Schema Mapping

Every MCP result maps into the **same schema** used by web extractions (existing line ~441).
New fields added to the schema:

```yaml
# Existing fields (unchanged)
title: ...
authors: ...
year: ...
url: ...
claims: [...]
numbers: [...]
study_design: ...
sample_size: ...
funding: ...

# New fields (MCP-specific, optional)
source_channel: "mcp:pubmed" | "mcp:huggingface" | "mcp:context7" | "web"
pmid: <string>           # PubMed ID (PubMed only)
pmcid: <string>          # PubMed Central ID (PubMed only)
doi: <string>            # DOI (high confidence from MCP, heuristic from web)
mesh_terms: [<string>]   # MeSH terms (PubMed only)
affiliations: [<string>] # Structured affiliations (PubMed, more reliable than web)
extraction_depth: "full-text" | "abstract-only" | "snippet-only" | "metadata-only"
```

**Key advantage:** DOIs from MCP have HIGH confidence (database-sourced, not scraped).
Authors, affiliations, and funding are structured rather than heuristically extracted.

---

## 6. Phase 2b — Web Content Extraction (Renamed)

**Modifications to existing Phase 2 Agent Dispatch (lines 366-410).**

### Changes

1. **Preamble:** "Phase 2b handles web-based content extraction for URLs from Phase 1b."
2. **URL filter:** Only URLs from Phase 1b (web discovery) enter this track.
   MCP-discovered sources are handled in Phase 2a.
3. **All existing logic unchanged:** Agent dispatch table, partitioning rules,
   same-domain grouping, per-agent WebFetch batching, error budget.

---

## 7. Phase 2c — Track Merge (Expanded)

**Expansion of existing Agent Merge Step (lines 385-410).**

### Merge Process

1. Collect all extractions: MCP (Phase 2a) + Web (Phase 2b) + Context7 (direct from 1a)
2. Deduplicate by DOI/PMID/title+author match — keep MCP version, merge any additional
   web-extracted claims not present in MCP extraction
3. Sum **web errors only** for the global error budget (MCP errors tracked separately)
4. If total verified sources below depth minimum and web error budget allows,
   run supplementary web fetch batch
5. Re-rank merged list by source authority tier (domain profile priority)
6. Build expanded fetch audit log (see Section 9)
7. Calculate source diversity (see Section 10)
8. Pass merged list to Phase 2.5 Semantic Compression

---

## 8. Phase 2d — Primary Source Chasing with MCP

**Modifications to existing Phase 2b / Primary Source Chasing (lines 483-497).**

### When PubMed MCP Is Available

For secondary sources that cite primary biomedical studies:

1. Extract cited DOIs or author+year references from secondary source text
2. For DOIs: `convert_article_ids` (DOI → PMID)
3. For author+year: `lookup_article_by_citation` (→ PMID)
4. For resolved PMIDs: `find_related_articles` with `max_results=5`
   to discover the research cluster around each primary source
5. `get_article_metadata` on all newly found PMIDs
6. `convert_article_ids` to check PMCID availability
7. MCP extraction for full-text sources; abstract-only for the rest

This replaces WebSearch + WebFetch chasing for PubMed-indexed sources.
MCP chases can continue even when the web error budget is exhausted.

### When PubMed MCP Is Unavailable

Existing Phase 2b behavior is unchanged: WebSearch for cited primary studies,
WebFetch top 5 most-cited open-access sources. Full backward compatibility.

### For Non-Biomedical Primary Sources

Existing WebSearch approach is used regardless of MCP availability.

---

## 9. Fetch Audit Log Changes

**Modifications to existing fetch audit section (lines 393-403).**

### New Fields

```yaml
# Per-fetch entry
url: <string>
status: success | 403 | 404 | timeout | cancelled | other-error | abstract-only | mcp-error | not-found
source_type: journal | government | edu | media | other
channel: web | mcp:pubmed | mcp:huggingface | mcp:context7
extraction_depth: full-text | abstract-only | snippet-only | metadata-only
```

### Summary Statistics (Split by Track)

```
MCP track:
  - Servers probed: {count} (available: {available}, unavailable: {unavailable})
  - MCP fetch attempts: {count}
  - Full-text extractions: {count}
  - Abstract-only extractions: {count}
  - MCP errors: {count}

Web track:
  - Total fetch attempts: {count}
  - Success count and rate (%)
  - Failure breakdown by status type
  - Most common failure type
```

---

## 10. PRISMA-Lite Funnel Changes

**Modifications to methodology section (lines 835-857).**

### Updated Funnel

```
Search funnel (PRISMA-lite):
  MCP Discovery (Phase 1a):
    - PubMed articles found: {count} (full-text available: {ft_count})
    - HF papers found: {count}
    - Context7 docs found: {count}
  Web Discovery (Phase 1b):
    - Results found: {total_results}
    - After deduplication: {deduped_count}
  Combined (post-merge):
    - Total unique sources: {total}
    - Fetched/extracted: {fetched_count}
    - Included in analysis: {included_count}
    - Primary sources: {primary_count} | Secondary: {secondary_count}
    - Primary sources chased (MCP): {mcp_chased} | chased (web): {web_chased}
    - Pass 2 gap-filling fetches: {pass2_count}
  Per-track audit:
    - MCP: {mcp_success}/{mcp_attempts} success rate
    - Web: {web_success}/{web_attempts} success rate
```

---

## 11. Source Diversity Changes

**Modifications to source diversity calculation (lines 404-409).**

### Expanded Categories

Source categories expand from 5 to 6:
`journal` | `government` | `edu` | `media` | `mcp-database` | `other`

- `mcp-database`: PubMed sources (via MCP), HF paper sources (via MCP)
- Context7 docs count as `documentation` subcategory of `other`

### Diversity Score

```
diversity_score = unique_categories_represented / 6
```

### Single-Channel Dominance Check

The >60% threshold now covers both MCP dominance and web dominance:
- If >60% of sources are from MCP → trigger 1-2 web-focused diversity queries
- If >60% of sources are from web → trigger additional MCP queries (if available)

### Minimum Web Representation Rule

Even when MCP provides excellent coverage, ensure **at least 2 web-sourced results**
in the final set. MCP databases have their own selection biases (e.g., PubMed indexes
journals selectively; preprints and grey literature may be underrepresented).

---

## 12. Cross-Cutting Changes

### Source Quality Pre-Filter (lines 288-311)

MCP sources bypass the URL-based pre-filter entirely. They are already authenticated
and structured — there is no paywall risk. MCP sources are always "Fetch first."

### Structured Extraction Schema (line ~441)

Add optional fields: `source_channel`, `pmid`, `pmcid`, `doi`, `mesh_terms`,
`affiliations`, `extraction_depth`. See Section 5 for full schema.

### Semantic Compression (Phase 2.5)

No structural changes. The compression step consumes the unified extraction list.
MCP sources provide higher-confidence metadata (DOI, funding, affiliations) which
improves provenance chain quality.

### Statistical Scrutiny (Phase 2.7)

No structural changes. MCP-sourced claims carry study design and sample size from
PubMed metadata, reducing the "missing context" flags.

### Self-Check (Phase 3.5)

Add a check: if MCP servers were available, verify that MCP-sourced citations
include PMID/DOI in the reference (not just a URL).

### Completion Summary (Phase 4)

Add to the completion message:
```
MCP sources used: {count} ({servers_list})
Web sources used: {count}
MCP advantage: {funding_known_mcp}% funding data vs {funding_known_web}% (web)
```

---

## Verification Plan

### Test 1: Medicine Domain (PubMed MCP)

Run a medicine research task (e.g., "sertraline and serotonin syndrome risk factors").
Verify:
- Phase 1a queries PubMed MCP and returns structured results with PMIDs
- Phase 2a extracts full text for PMC-available articles
- Phase 2d uses `find_related_articles` instead of WebSearch
- PRISMA-lite funnel shows MCP Discovery section
- Fetch audit log shows `channel: mcp:pubmed` entries
- References include PMID/DOI

### Test 2: Technology Domain (HF + Context7)

Run a technology research task (e.g., "transformer attention mechanisms in vision models").
Verify:
- Phase 1a queries HF `paper_search` and returns arXiv papers
- Context7 activation condition is checked (should NOT activate — topic is not a specific library)
- PRISMA-lite funnel shows HF Discovery section
- Web queries exclude `site:arxiv.org` when HF returned results

### Test 3: Technology Domain with Context7

Run a task naming a specific library (e.g., "PyTorch 2.0 compile API performance").
Verify:
- Context7 activates (topic names PyTorch)
- `resolve-library-id` finds PyTorch, `query-docs` returns documentation
- Context7 results skip Phase 2a (already extracted)

### Test 4: History Domain (Web-Only Fallback)

Run a history research task (e.g., "causes of the fall of the Roman Republic").
Verify:
- No MCP servers are queried (history has no `mcp_sources`)
- Pipeline behaves identically to v2.0
- No MCP entries in fetch audit log

### Test 5: MCP Unavailability

Simulate MCP unavailability (e.g., tool not found).
Verify:
- Probe fails gracefully, logged
- `fallback_search` modifier added to web queries
- Pipeline completes successfully via web-only path
- No crash or error propagation

---

## Implementation Sequence

| Step | Section | Est. Lines | Dependencies |
|------|---------|------------|--------------|
| 1 | Domain profile `mcp_sources` | +55 | None |
| 2 | MCP Availability and Fallback | +25 | Step 1 |
| 3 | Phase 1a MCP Discovery + Phase 1b rename | +105 | Steps 1-2 |
| 4 | Phase 2a MCP Extraction + Phase 2c Track Merge | +100 | Steps 1-3 |
| 5 | Phase 2d Primary Source Chasing with MCP | +15 | Steps 1-4 |
| 6 | Reporting (audit log, PRISMA-lite, diversity) | +25 | Steps 1-5 |
| 7 | Cross-cutting (schema, pre-filter, self-check) | +13 | Steps 1-6 |
| 8 | Validation (5 test scenarios) | — | Steps 1-7 |
| **Total** | | **+365** | |

---

## Files Modified

| File | Change |
|------|--------|
| `skill/SKILL.md` | All changes (~365 new lines, ~1,157 → ~1,522) |

No other files are modified. The output format, export pipeline, and template are unchanged.
