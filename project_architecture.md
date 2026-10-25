# Tech Dataset: Architecture

This document describes the structure of the pipeline: scope and decisions, the design rules for a collection period of 15 to 20 years, the technology registry, the datasets and their variables, the prediction targets they serve, the sources, storage and data volume, the run, failure handling, the measurement log and the implementation status. The pipeline adopts the structure of [dataset.politics.germany](https://github.com/kleinnconrad/dataset.politics.germany). Usage and configuration are described in the [README](README.md).

## Table of Contents
- [1. Scope and Decisions](#1-scope-and-decisions)
- [2. Design Rules](#2-design-rules)
- [3. Technology Registry](#3-technology-registry)
- [4. Datasets and Variables](#4-datasets-and-variables)
  - [4.1 Attention and Discussion](#41-attention-and-discussion)
  - [4.2 Developer Activity and Adoption](#42-developer-activity-and-adoption)
  - [4.3 Artificial Intelligence and Machine Learning](#43-artificial-intelligence-and-machine-learning)
  - [4.4 Security](#44-security)
  - [4.5 Standards and Research](#45-standards-and-research)
  - [4.6 Internet Infrastructure and the Web](#46-internet-infrastructure-and-the-web)
  - [4.7 Economy and Society](#47-economy-and-society)
- [5. Prediction Targets](#5-prediction-targets)
- [6. Sources and Fetch Rules](#6-sources-and-fetch-rules)
- [7. Storage, Partitioning and Data Volume](#7-storage-partitioning-and-data-volume)
- [8. Repository Structure](#8-repository-structure)
- [9. Pipeline Run](#9-pipeline-run)
- [10. Failure Handling](#10-failure-handling)
- [11. Measurement Log](#11-measurement-log)
- [12. Implementation Status](#12-implementation-status)

---

## 1. Scope and Decisions

- **Scope:** the technology world in eight domains: AI and machine learning, software development, data, IT infrastructure (operating systems, cloud, containers, infrastructure as code, observability, CI/CD, web servers, virtualization, data centers), networking, security, hardware, and emerging technologies (quantum computing, blockchain, extended reality, robotics). The scope is global; country breakdowns are stored where a source provides them (World Bank, Eurostat, Indeed Hiring Lab).
- **Horizon:** the data is collected for 15 to 20 years and analyzed as long time series. The design favors stable definitions, durable sources and a moderate data volume over the completeness of single items.
- **Repository:** public. The data is published under the ODbL 1.0 (see [data/LICENSE.md](data/LICENSE.md)); every record names the license or terms of its source.
- **Schedule:** one workflow runs the due collectors on Tuesdays and Fridays at 02:07 Europe/Berlin. Collectors of snapshot and change data run weekly or in every run; collectors of series whose sources keep the history run monthly (Section 9). A run takes at most 45 minutes; the history is imported once by a separate `backfill` run.
- **No language model:** all variables are read deterministically from structured sources. Labels produced by a model, such as topics of Hacker News stories, can be added later as a separate dataset keyed by the item id, as in dataset.politics.germany.
- **No paid access:** every source is free. GitHub Actions provides a token for the GitHub API; free keys for the Stack Exchange and NVD APIs are optional and raise the quotas.

---

## 2. Design Rules

1. **Point-in-time correctness.** Every record has `observed_at`, the UTC time at which the pipeline observed it, in addition to its own dates. Filtering `observed_at <= t` reproduces what was known at time t. Items older than the first run of their collector (more than 7 days) have `backfilled: true`.
2. **Four kinds of datasets.**
   - *Series:* one record per entity and run with a map of the periods since the previous run (days or ISO weeks). Every period is stored once. Past periods can be fetched again by a later run, so a failed run loses nothing.
   - *Snapshot:* one record per entity and run with the current value of a measure that the source does not keep as history (stars, pulls, downloads in the last 30 days). A missed run leaves a gap.
   - *Change:* an item is written when it appears and again, with the next `revision`, when a tracked field changes. An item that disappears from a complete source listing gets one record with `is_listed: false`.
   - *Event:* an item is written once when it is observed (Hacker News stories, daily CVE counts).
3. **Snapshot sources first.** Collectors whose values are lost when a run is missed run at the start of each run; series collectors run later and catch up missed periods.
4. **Permanent identifiers.** Technology ids, dataset names, field names and `record_id` formats are never reused for a different meaning. The registry keeps removed technologies with `removed` and `removed_reason`.
5. **Additive schema changes.** New fields get a description and are added without changing existing ones; a changed meaning requires a new field and a new `SCHEMA_VERSION`.
6. **Raw values, derived variables later.** The datasets store counts, prices, scores and attributes as the sources report them. Growth rates, shares, ranks across sources and other features are computed in the analysis.
7. **Aggregation where items are too many.** Where the number of items would make the repository grow too fast, the dataset stores aggregates: CVEs as daily counts instead of 135,000 records per year, World Bank indicators as delta records.
8. **No full texts, no personal data.** Only titles, identifiers, URLs, metadata, counts and derived variables are stored. User names, author names and repositories of individual users are not stored.
9. **Terms of the sources.** Sources whose terms forbid redistribution or automated copying, or that require personal contact data in requests, are not collected (Section 12).
10. **Logged measurement changes.** Every change of a source, endpoint, filter, counting rule or basket is recorded in the measurement log (Section 11), so that structural breaks can be identified.

---

## 3. Technology Registry

The registry [config/technologies.toml](config/technologies.toml) lists the tracked technologies. Every entry has a stable `id`, which is the join key `technology_id` of the technology-related datasets, a `name`, a `category` (34 categories in 8 domains, see `TechnologyCategory` in [src/schema.py](src/schema.py)) and the date `added`. The identifiers of the technology at the sources are optional:

| Key | Identifier | Read by |
|---|---|---|
| `wikidata` | Wikidata id; the English Wikipedia article is resolved at run time, so renamed articles keep their series | `wikipedia_pageviews` |
| `stackoverflow_tags` | Stack Overflow master tags | `stackoverflow_questions` |
| `github_repos` | main repositories as owner/name | `github_repositories` |
| `packages` | `npm:`, `pypi:`, `crates:` packages and `docker:` images that serve as adoption proxies | `package_downloads`, `docker_image_pulls` |
| `endoflife` | product slug at endoflife.date | `release_cycles` |
| `hn_terms` | lower-case terms that refer to the technology without ambiguity | `hackernews_mentions`, `hackernews_stories` |
| `github_language` | language name in GitHub Linguist | reserved for GitHub language statistics (planned) |

Rules:
- Each identifier belongs to one entry, so that no value is counted for two technologies. [src/registry.py](src/registry.py) rejects duplicates, unknown keys and invalid formats.
- Every identifier is verified against the live source before it is committed (CLAUDE.md, section 9). The verification of the initial entries on 2026-10-25 also recorded known limitations: terms that start with a dot (`.net`) are matched in titles but not counted with the full-text search, which also finds host names for them; some languages have no reference repository (C, SQL) or are hosted outside GitHub (Zig, R, Haskell).
- The collector `technologies` writes every new or changed entry to the dataset `technologies`, the slowly changing dimension of the registry. The coverage of a technology starts with `added` and ends with `removed`.
- New technologies are added when they appear, for example new model families. Ids are never renamed; an entry that is replaced by another keeps its id and gets `removed`.
- **Monthly registry review** (`source_review.yml`, [src/source_review.py](src/source_review.py)): checks every identifier against its source, fixes renames, lists missing identifiers and unhealthy collectors, and adds up to five technologies proposed by Gemini with Google Search after verifying their identifiers. The changes arrive as a pull request; findings without changes as an issue.

---

## 4. Datasets and Variables

All fields are defined with descriptions in [src/schema.py](src/schema.py); the README shows the data model and one example record per dataset. Every record has the common fields `record_id`, `revision`, `dataset`, `schema_version`, `observed_at`, `source_id`, `source_url`, `license` and `backfilled`.

The column **History** states what the first run imports and whether past values can still be fetched later.

### 4.1 Attention and Discussion

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `wikipedia_pageviews` | series (days) | monthly | from July 2015; can be fetched later | daily views of the English Wikipedia article of each technology (people only, agent type `user`) |
| `hackernews_stories` | event | every run | 7 days (backfill: 365 days); can be fetched later | stories with at least 100 points: title, domain, type (story, Ask HN, Show HN, Launch HN), points and comments about two to six days after posting, technologies named in the title |
| `hackernews_mentions` | series (ISO weeks) | every run, within a budget of 1,500 requests | 4 weeks (backfill: 52 weeks); can be fetched later | stories with the term in the title and comments with the term, per technology term and week |

Attention variables measure interest, not use. They serve as leading indicators: whether attention precedes adoption, and by how long, is one of the questions the dataset is built to answer.

### 4.2 Developer Activity and Adoption

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `stackoverflow_questions` | series (ISO weeks) | monthly | 4 weeks (backfill: 104 weeks); can be fetched later | new questions per tag and week, total questions of the tag |
| `github_repositories` | snapshot | weekly | none | stars, forks, open issues and pull requests, watchers, contributors, commits in the last 4 and 52 weeks, size, language, license, topics, latest release, archived state |
| `package_downloads` | series (days) | monthly | npm: 365 days (backfill: from 2015); PyPI: 180 days; crates.io: 90 days | daily downloads of npm, PyPI and crates.io packages |
| `docker_image_pulls` | snapshot | weekly | none | cumulative pulls and stars of container images |
| `release_cycles` | change | weekly | all cycles of each product | release date, LTS, end of active support, end of life, extended support, latest version per release cycle |

Variables that follow from these fields, such as weekly pulls (difference of cumulative pulls), release cadence or the support period of a cycle, are computed in the analysis.

### 4.3 Artificial Intelligence and Machine Learning

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `llm_prices` | change | every run | current list only | input, output, cached and batch prices per million tokens, context and output limits, capabilities (tools, vision, reasoning, prompt caching, web search), deprecation date per offering (model at a provider) |
| `ai_models` | change | weekly | the whole Epoch AI database (models since 1950) | organization, country, publication date, domains, tasks, notability, parameters, training compute, dataset size, training time, hardware, cost, power, accessibility, open weights |
| `ml_hardware` | change | weekly | the whole database | release date and price, performance per number format (FP64 to FP4, INT8, INT4), memory and bandwidth, TDP, foundry, process size, transistors |
| `ai_capability_scores` | change | weekly | all scores | Epoch Capabilities Index (ECI) per model with confidence interval, organization, accessibility |
| `huggingface_repos` | change | weekly | none | task, library, license, base models, parameter count, gating of top model and dataset repositories of organizations |
| `huggingface_repo_stats` | snapshot | weekly | none | rank, downloads in the last 30 days, downloads since creation, likes, trending score |
| `huggingface_hub_totals` | snapshot | weekly | none | number of public models, datasets and Spaces |
| `arxiv_submissions` | series (ISO weeks) | monthly | 8 weeks (backfill: 104 weeks); can be fetched later | new submissions per week in 20 categories (cs.AI, cs.LG, cs.CL, cs.CV, cs.CR, cs.DC, cs.NI, cs.SE, quant-ph, ...), including cross-lists; a week is counted 7 days after it ended, when its papers are announced |

### 4.4 Security

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `cve_daily_counts` | event (one record per day) | every run | 7 days (backfill: from 2024); counts by status reflect the time of observation | CVE records published per day by NVD status, CVSS v3 and v4 severity, NVD-scored, unscored, weakness (CWE), vendor, exploit references, KEV listing |
| `known_exploited_vulnerabilities` | change | every run | the whole catalog (from November 2021) | vendor, product, date added, due date, ransomware use, weaknesses, and from the NVD: publication date and CVSS score at the time of listing |

The NVD publishes about 370 CVE records per day. Records per CVE would add about 70 MB per year to the repository; the daily counts keep the trends at about 2 MB per year, and every record can still be fetched from the NVD by its id.

### 4.5 Standards and Research

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `rfcs` | change | weekly | all RFCs (from 1968) | publication month, status at publication and now, stream, area, working group, pages, keywords, relations (obsoletes, updates), errata |
| `ietf_draft_activity` | series (ISO weeks) | monthly | 8 weeks (backfill: 104 weeks); can be fetched later | new Internet-Drafts and draft revisions per week, for all drafts, by group type and by IETF area |

`arxiv_submissions` (Section 4.3) also covers the research side of networking (cs.NI), distributed systems (cs.DC), security (cs.CR) and software engineering (cs.SE).

### 4.6 Internet Infrastructure and the Web

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `ip_address_delegations` | snapshot | quarterly | current file (backfill: first day of each quarter since October 2021) | IPv4 addresses, IPv6 address space (in /32 equivalents), AS numbers and their ranges per registry, country of the holder and status (assigned, available, reserved) |
| `bgp_routing_table` | series (days) | monthly | from 1988 (IPv4) and 2003 (IPv6) | announced prefixes and origin autonomous systems per day in the global routing table |
| `ipv6_adoption` | series (days) | monthly | from October 2013 | shares of IPv6-capable and IPv6-preferring users per day, for the world and 11 economies |
| `market_shares` | series (months) | monthly | from July 2008 | monthly shares of operating systems (all devices, desktop, mobile), browsers, search engines, device platforms and AI chatbots, worldwide and for Germany and the United States |
| `supercomputer_lists` | event (one record per edition) | monthly check | latest edition (backfill: all editions since 1993) | per TOP500 edition: total, first-rank and entry-level performance, cores, power, systems and performance per country, systems per manufacturer (systems with accelerators: the XML lists contain no accelerator element, so the field is null) |
| `web_metrics` | series (crawls) | monthly | from 2010 and 2016 | share of requests over HTTPS, HTTP/2 and HTTP/3 (from 2020), median page weight and median JavaScript bytes of desktop and mobile pages |

The country in `ip_address_delegations` is the country in which the holder of the resources is legally based, not where the addresses are used.

### 4.7 Economy and Society

| Dataset | Kind | Run | History | Variables |
|---|---|---|---|---|
| `job_postings` | series (days) | weekly | from February 2020 | Indeed job postings index (1 February 2020 = 100) for total and new postings in 11 countries and for software development, IT infrastructure, IT systems, data and analytics, research, engineering and banking (baseline) in 6 countries |
| `ai_job_postings` | series (days) | weekly | from January 2019 | share of job postings that mention AI or generative AI terms in 9 countries |
| `world_bank_indicators` | change (delta records) | monthly | all years (from 1960) | 19 indicators for 266 countries and aggregates: internet users, broadband and mobile subscriptions, secure servers, high-technology exports, ICT goods and service trade, R&D expenditure, researchers, patent applications, scientific articles, electricity use, IP charges |
| `eurostat_indicators` | change | monthly | all years | for the EU and its 27 member states: enterprises using AI and generative AI for text, enterprises buying cloud services, digital intensity, ICT specialists, households with internet access, individuals using generative AI |

---

## 5. Prediction Targets

The dataset is designed for questions that need long series of comparable measurements. Examples:

| Target | Labels | Main features |
|---|---|---|
| Adoption curve of a technology: growth, saturation, decline, time to peak | `package_downloads`, `docker_image_pulls`, `github_repositories` (stars, contributors), `stackoverflow_questions` | pageviews, Hacker News mentions, arXiv activity, release cadence, ecosystem size, job postings |
| Lead time between attention and adoption | adoption measures above | `wikipedia_pageviews`, `hackernews_mentions`, `hackernews_stories` |
| End of a technology: abandonment, end of support, replacement | `release_cycles`, `github_repositories` (commits, archived) | declining questions, downloads and mentions, successor technologies in the same category |
| Price per token of language models over time; time until a price level is reached | `llm_prices` | `ml_hardware` (price-performance), `ai_models` (training compute), number of providers per model |
| Growth of the training compute and capabilities of frontier models | `ai_models`, `ai_capability_scores` | `ml_hardware`, `huggingface_*`, arXiv submissions in cs.LG and cs.CL |
| Open versus closed models: gap in capability and adoption | `ai_capability_scores` (accessibility group), `huggingface_repo_stats` | `ai_models` (open weights), `llm_prices` |
| Volume and severity of vulnerabilities; share of exploited vulnerabilities | `cve_daily_counts`, `known_exploited_vulnerabilities` | weaknesses, vendors, CVSS versions, ecosystem growth |
| Labor market for software development under AI adoption | `job_postings` | `ai_job_postings`, `llm_prices`, `stackoverflow_questions`, `eurostat_indicators` |
| Decline of public developer Q&A | `stackoverflow_questions` | pageviews and mentions of AI assistants, `llm_prices`, `huggingface_hub_totals` |
| Diffusion of digital infrastructure and AI by country | `world_bank_indicators`, `eurostat_indicators` | income group, R&D expenditure, ICT trade |
| Standardization activity per area | `rfcs`, `ietf_draft_activity` | arXiv activity in networking, attention to protocols |
| IPv6 adoption per economy; exhaustion of the IPv4 pools | `ipv6_adoption`, `ip_address_delegations` (available pools) | routing table, broadband and mobile subscriptions, income group |
| Growth of the global routing table (memory requirements of routers) | `bgp_routing_table` | delegated address space and AS numbers, IPv6 adoption |
| Performance growth of supercomputers; country shares; power efficiency | `supercomputer_lists` | `ml_hardware`, `ai_models` (training hardware), R&D expenditure |
| Shift between device platforms and from search engines to AI chatbots | `market_shares` | `llm_prices`, pageviews and mentions of AI applications |
| Weight and protocol mix of web pages | `web_metrics` | JavaScript framework downloads, adoption of HTTP/3 and QUIC |

---

## 6. Sources and Fetch Rules

| Source | Access | Rate limit used | License or terms | Datasets |
|---|---|---|---|---|
| Wikimedia Pageviews API, Wikidata API | JSON | 0.1 s (Wikimedia), 1 s (Wikidata) | CC0 | `wikipedia_pageviews` |
| Stack Exchange API 2.3 | JSON, custom filter with totals and quota; optional key | 0.2 s; 300 requests per day and IP without key, 10,000 with key | counts; content CC BY-SA, source attribution required | `stackoverflow_questions` |
| GitHub REST API | JSON, workflow token | 0.25 s; 1,000 requests per hour with the workflow token | GitHub Terms of Service and Acceptable Use Policies | `github_repositories` |
| npm downloads API | JSON | 0.5 s | npm Open-Source Terms | `package_downloads` |
| pypistats.org API | JSON | 1.5 s | PyPI download statistics | `package_downloads` |
| crates.io API | JSON | 1 s (data access policy) | crates.io data access policy | `package_downloads` |
| Docker Hub API | JSON | 1 s | Docker Terms of Service (documented APIs within published limits) | `docker_image_pulls` |
| endoflife.date API v1 | JSON | 0.5 s | MIT | `release_cycles` |
| LiteLLM model price list (GitHub) | JSON file, conditional request | 0.2 s | MIT | `llm_prices` |
| Epoch AI data | CSV and ZIP files, conditional requests | 1 s | CC BY 4.0 | `ai_models`, `ml_hardware`, `ai_capability_scores` |
| Hugging Face Hub API and list pages | JSON; HTML for the totals | 0.7 s; 500 API requests per 5 minutes and IP | Hugging Face Terms of Service | `huggingface_*` |
| arXiv API | Atom | 3 s (terms of use) | metadata CC0 | `arxiv_submissions` |
| HN Search API (Algolia) | JSON | 0.4 s; 10,000 requests per hour and IP | Hacker News content (Y Combinator); metadata only | `hackernews_*` |
| NVD CVE API 2.0 | JSON; optional key | 6.5 s without key, 0.7 s with key | public domain; NVD notice | `cve_daily_counts`, `known_exploited_vulnerabilities` |
| CISA KEV catalog | JSON file | 1 s | CC0 | `known_exploited_vulnerabilities` |
| RFC Editor index | XML file, conditional request | 1 s | IETF Trust | `rfcs` |
| IETF Datatracker API | JSON | 0.5 s | IETF Trust | `ietf_draft_activity` |
| World Bank Indicators API v2 | JSON | 1 s | CC BY 4.0 | `world_bank_indicators` |
| Eurostat dissemination API | JSON-stat 2.0 | 1 s | reuse authorised with acknowledgement | `eurostat_indicators` |
| Indeed Hiring Lab (GitHub) | CSV files, conditional requests | 0.2 s | CC BY 4.0 | `job_postings`, `ai_job_postings` |
| NRO extended delegated statistics (ftp.ripe.net) | text file (57 MB), conditional request | 1 s | no license stated; only derived counts are stored | `ip_address_delegations` |
| bgp.potaroo.net | text files | 10 s (crawl delay of robots.txt) | no license published | `bgp_routing_table` |
| APNIC Labs IPv6 measurement | JSON files (about 4 MB each) | 1 s | re-use with attribution permitted | `ipv6_adoption` |
| StatCounter Global Stats | CSV | 2 s | CC BY-SA 3.0 | `market_shares` |
| TOP500 | XML files | 2 s | copyright TOP500.org; only aggregates are stored | `supercomputer_lists` |
| HTTP Archive reports | JSON | 1 s | free use, attribution requested | `web_metrics` |

**Fetch rules** ([src/web.py](src/web.py)), the same as in dataset.politics.germany:
- Every request identifies the pipeline with the User-Agent `TechDataset/0.1 (+https://github.com/kleinnconrad/dataset.technology)`. No personal data is sent.
- Documented APIs and data files offered for programmatic use are called under their terms and rate limits (`respect_robots=False`). Website requests (Hugging Face list pages, the CISA and RFC Editor files) check robots.txt with the longest-match rule of RFC 9309 and honor crawl delays.
- Text-and-data-mining reservations are respected; bot protection (HTTP 401/403, challenge pages) is never bypassed and no headless browser is used. A refused host gets no further requests in the run, and the collector reports an error, which marks the run as failed and appears in the notification; the next scheduled run tries again.
- An exhausted quota (HTTP 429, or 403 with rate-limit headers) raises `RateLimitError`: the collector stops and continues in the next run.
- Server errors and network errors are retried twice. Large files are requested with `If-None-Match`, so that unchanged files are not downloaded again.

---

## 7. Storage, Partitioning and Data Volume

```text
data/
├── LICENSE.md
├── wikipedia_pageviews/year=2026/month=10/wikipedia_pageviews_2026-10-26.json
├── ...
└── state/
    ├── seen/<dataset>.json   # written items with content hash and revision; large stores in shards
    ├── collectors.json       # first run, last run, failures, cursor per collector
    └── run_log.jsonl         # one line per run
```

- **Partition key:** the UTC day of the run that observed the record. A run writes at most one file per dataset; records are merged by `record_id` and `revision`, so a repeated run on the same day replaces instead of duplicating.
- **Folder names:** `year=` and `month=` let DuckDB, Polars and pyarrow read a dataset as one table with partition pruning.
- **Format:** one JSON list per file with one record per line (compact and readable in git diffs).
- **Expected growth:** the backfill writes about 60 to 90 MB of JSON and about 10 MB of state (a live run of all collectors with 7 technologies wrote 27 MB of records and 7 MB of state, mostly the RFC index, the Epoch AI database, IPv6, LLM prices and job postings); afterwards about 45 MB per year. The largest datasets are `huggingface_repo_stats`, `hackernews_stories`, `github_repositories` and `llm_prices`; git compresses the files to a fraction of their size.
- **Long-term plan:** when the repository approaches 1 GB, the data of completed years moves to a separate data repository with the same layout, or is archived as compressed Parquet files per year and dataset.

---

## 8. Repository Structure

```text
dataset.technology/
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── ci.yml              # lint, tests, commit messages
│       ├── notify.yml          # run summary as a commit comment that mentions the owner
│       ├── pipeline.yml        # due collectors, Tuesdays and Fridays 02:07 Europe/Berlin; backfill by hand
│       └── source_review.yml   # monthly registry review as a pull request
├── config/
│   └── technologies.toml       # registry of the tracked technologies
├── data/                       # datasets, state and data license, committed by the workflow
├── src/
│   ├── main.py                 # entry point
│   ├── pipeline.py             # collector order, host intervals, isolation, run log
│   ├── settings.py             # environment variables
│   ├── web.py                  # HTTP client with the fetch rules
│   ├── schema.py               # record models
│   ├── registry.py             # technology registry
│   ├── storage.py, state.py
│   ├── generate_dataset_overview.py
│   ├── source_review.py        # monthly review of config/technologies.toml
│   └── collectors/             # one module per source family
├── scripts/
│   └── run_summary.py          # run summary for notify.yml
├── tests/                      # offline tests with a fake web
├── pyproject.toml
├── uv.lock
├── LICENSE                     # MIT, for the code
└── README.md
```

---

## 9. Pipeline Run

`src/main.py` starts the run; `src/pipeline.py` runs the collectors in the order of `COLLECTOR_ORDER`:

1. Registry and snapshot sources whose values are lost when a run is missed: `technologies`, `github_repositories`, `docker_hub`, `huggingface`, `llm_prices`.
2. Change and event sources: `vulnerabilities`, `hackernews` (stories every run; mentions within a budget of 1,500 requests per run, the terms with the oldest pending weeks first, the first backlog takes about three runs, afterwards every term is counted every week), `epoch_ai`, `release_cycles`, `rfcs`.
3. Series that can be fetched later: `wikipedia_pageviews`, `stackoverflow`, `package_downloads`, `arxiv`, `ietf_drafts`, `job_postings`.
4. Monthly reference data: `world_bank`, `eurostat`, `nro` (quarterly), `bgp`, `ipv6_adoption`, `statcounter`, `top500`, `httparchive`.
5. The workflow updates the README overview and commits `data/` and `README.md`. `notify.yml` posts a summary of the run as a commit comment that mentions the repository owner.

**Intervals.** A collector with `min_days_between_runs` runs only if its last complete run is at least that many days old: 6 days for weekly collectors, 27 days for monthly ones. Series collectors run monthly because their sources keep the history (except `job_postings`, which runs weekly because its source revises the history every week) (npm 18 months, PyPI 180 days, crates.io 90 days, the others without limit); a monthly record holds the same days or weeks as four weekly records with a quarter of the overhead.

**Run modes.** A regular run (`normal`) has a deadline of 40 minutes (`RUN_DEADLINE_MINUTES`) and a job timeout of 45 minutes. A `backfill` run imports the history of all collectors without the per-run caps and the intervals (deadline 225 minutes, timeout 240 minutes); it is started once by hand. Collectors that do not finish their backfill within the deadline continue in later runs. `shadow` and `dry-run` work on a copy of the state and commit nothing.

---

## 10. Failure Handling

- **A collector fails:** the error is logged, recorded in `collectors.json` and the run log, and the next collector starts. Data already written stays. The run exits with code 1.
- **A source item fails** (one repository, one package): the other items continue; the failure is counted in the collector's counters.
- **Quota or rate limit:** the collector stops with the status `partial` and continues in the next run; series and backfills keep their position, because the seen stores advance only for fetched periods.
- **Run deadline:** after `RUN_DEADLINE_MINUTES` the collectors start no new work. A collector that stops early has the status `partial`; a collector that has not started by the deadline is skipped, which marks the run as failed.
- **Run interrupted:** records and seen stores are written in batches; a later run skips what was written. A cancelled or timed-out workflow commits nothing; the run deadline prevents the timeout.
- **Concurrent pushes:** the workflow pulls with rebase and retries the push up to three times.

---

## 11. Measurement Log

Changes of sources, endpoints, filters, counting rules and baskets, and known breaks in the sources. Add a line for every change.

| Date | Dataset | Change |
|---|---|---|
| 2026-10-25 | all | Design of the pipeline; registry with the initial technologies, all identifiers verified on this day. |
| 2026-10-25 | `stackoverflow_questions` | Counting rule: Stack Exchange API filter `!9n30I5fDgPX4`; weeks from Monday 00:00:00 to Sunday 23:59:59 UTC; deleted questions are not counted; tag synonyms are not resolved (registry tags are master tags). |
| 2026-10-25 | `eurostat_indicators` | Filters: `freq=A`, enterprises `size_emp=GE10` and `nace_r2=C10-S951_X_K`; datasets without an indicator dimension (`isoc_sks_itspt`, `isoc_ci_in_h`) use the indicator id `TOTAL`; digital intensity versions 3 and 4 are not comparable; status flags (e.g. b = break in series) are not stored. |
| 2026-10-25 | `job_postings` | Only seasonally adjusted indices are stored; the sector files contribute only total postings per category; values are stored as first seen (the source revises the history weekly, so the collector runs weekly). |
| 2026-10-25 | `package_downloads` | Counting rules: npm days up to three days before the run, days on which more than half of the npm packages with at least 1,000 daily downloads report 0 are treated as outages and re-requested for 60 days, zeros before the first day with downloads are not stored; PyPI without mirrors; crates.io daily total = downloads of the five newest versions plus all other versions; PyPI and crates.io days up to two days before the run. |
| 2026-10-25 | `release_cycles` | Field mapping of the endoflife.date API v1: eoasFrom = end of active support, eolFrom = end of life, eoesFrom = end of extended support; the record of a cycle that disappeared has `is_listed: false` and null in the other fields except product and cycle. |
| 2026-10-25 | `market_shares` | The categories are the top categories of the requested range (about 20 in the first import, the top categories of one or a few months later); months that StatCounter reports as zeros before a statistic starts are not stored (data from 2009-01; AI chatbots in the United States from 2025-04). |
| 2026-10-25 | `web_metrics` | h2 is 0 in the first crawls of 2016 and h3 is 0 until 2020-05: the start of the measurement, not of the adoption; the HTTP Archive crawled twice a month until 2018. |
| 2026-10-25 | `ip_address_delegations` | Counting rules: status allocated counts as assigned; IPv6 space summed exactly and converted to /32 equivalents; lines without a country count as ZZ; quarterly snapshots. |
| 2026-10-25 | `bgp_routing_table` | Single vantage point of bgp.potaroo.net (AS65000); daily value = last hourly value of the UTC day; a day counts as complete when both files have later measurements. |
| 2026-10-25 | `ipv6_adoption` | Percentages rounded to 4 decimals; days without measurements (seen = 0) are not stored; the number of measurements of the world (XA) is a weighted value. |
| 2026-10-25 | `hackernews_mentions` | Counting rules: Algolia search with `typoTolerance=false`, `queryType=prefixNone`, `restrictSearchableAttributes=title` (stories) or `comment_text` (comments); single words as typed, other terms as quoted phrases (dots belong to the word, hyphens and slashes separate words); terms that start with a dot are not counted; weeks with non-exhaustive counts are split into days and 6-hour windows and listed in `approximate_weeks`. |
| 2026-10-25 | `hackernews_stories` | Stories with at least 100 points, collected at least 48 hours after posting; technologies matched in the title as whole words (spaces, hyphens and slashes interchangeable). |
| 2026-10-25 | `arxiv_submissions` | 20 categories including cross-lists; a week is counted only when it ended at least 7 days before the run, because the API lists papers after their announcement. |
| 2026-10-25 | `huggingface_*` | Top 500 models and top 200 datasets by downloads in the last 30 days; only repositories of organizations are stored (owner checked once per account with the organization endpoint); base models of user accounts are left out; Hugging Face developer ids in `ai_models` are kept only for organizations. |
| 2026-10-25 | `llm_prices` | Offerings of the LiteLLM price list with the modes chat, completion, responses and embedding; prices converted from USD per token to USD per million tokens. |
| 2026-10-25 | `cve_daily_counts` | Counting rules: one record per publication day (UTC), observed after the day ended; score per CVSS version from the NVD's own entry, else the first Primary, else the first Secondary entry; vendors from vulnerable CPE matches, else from the CNA data; rejected records count only in `cves`, `rejected` and `by_status`. |
| 2026-10-25 | `known_exploited_vulnerabilities` | NVD values (publication time, CVSS) are taken at the first observation of an entry; entries added in the last 7 days wait for the next run if the NVD fails; no removal records if the catalog's count header does not match or more than 5 % of the entries vanish at once. |
| 2026-10-25 | `ietf_draft_activity` | Area scopes count the working groups of the area only (`doc__group__type=wg`); weeks in UTC. |
| 2026-10-25 | `github_repositories` | Counting rule: contributors = last page number of the contributors list with `per_page=1&anon=true`; commits from the participation statistics (rolling weeks ending at UTC midnight, without merge commits); watchers = `subscribers_count`. |
| 2020 | `wikipedia_pageviews` | Source break: Wikimedia classifies automated traffic separately since 2020; views of agent type `user` before and after are not fully comparable. |
| 2023 to 2026 | `stackoverflow_questions` | Source development: the number of new questions fell by more than 99 % between 2020 (python: about 5,000 per week) and 2026 (python: about 30 per week). |
| 2024-02 | `cve_daily_counts` | Source break: since February 2024 the NVD analyzes only part of the new CVE records; most records carry scores of other sources (CNAs, CISA) or none. `nvd_scored` and the score selection rule make this visible. |
| before 2026-10 | `job_postings` | Source change (date not documented by the source): Indeed Hiring Lab replaced the categories IT Operations & Helpdesk, Information Design & Documentation and Mathematics by IT Infrastructure, Operations & Support, IT Systems & Solutions and Data & Analytics. |
| 2025-08-15 | `ai_job_postings` | Source change: the separate series of postings that mention generative AI was discontinued; the AI share covers AI and generative AI terms. |

---

## 12. Implementation Status

| Part | Status |
|---|---|
| Technology registry and its dataset | implemented |
| Wikipedia pageviews, Hacker News stories and mentions | implemented |
| Stack Overflow questions, GitHub repositories, package downloads, Docker pulls, release cycles | implemented |
| LLM prices, Epoch AI models, hardware and capability index, Hugging Face Hub, arXiv submissions | implemented |
| CVE daily counts, known exploited vulnerabilities | implemented |
| RFCs, Internet-Draft activity | implemented |
| Job postings and AI job postings, World Bank and Eurostat indicators | implemented |
| IP address delegations, BGP routing table, IPv6 adoption, market shares, TOP500 aggregates, web metrics | implemented |
| Technology adoption on websites (HTTP Archive Tech Report API, origins per technology) | planned |
| Routed versus delegated resources per country (resources.potaroo.net); NRO statistics before October 2021 | planned |
| Counts of internet exchange points, facilities and networks per country (PeeringDB), after permission of PeeringDB | planned |
| GitHub language statistics (repositories created per language and month, search API) | planned |
| Monthly registry review: identifier checks, collector health, new technologies proposed by Gemini with Google Search, as a pull request (`source_review.yml`) | implemented |
| Labels of Hacker News stories (topic, event type) with a language model, as a separate dataset | planned |
| Per-CVE records in a separate data repository; EPSS score distributions (no license stated by FIRST) | planned |
| Advisories per package ecosystem (GitHub Advisory Database, OSV) | planned |
| Backfill of LLM prices from the git history of the LiteLLM price list (since 2023) | planned |
| Backfill of crates.io downloads from the crates.io archive; Stack Overflow history before 2024 with an API key | planned |
| Pageviews of the German Wikipedia | planned |
| Persistent block status per host in the state (a refused host is not requested again until the entry is reset by hand) | planned |

Sources that were checked and are not collected:

| Source | Reason |
|---|---|
| OpenRouter model catalog | The terms forbid automated copying of information from the service; the catalog has no data license. The licensed rankings dataset requires an API key. |
| Azure Retail Prices API | No data license; the Microsoft Terms of Use restrict copying and publishing of information from the services. The price basket was tested (Linux VMs, GPU VMs, Blob storage, egress, Azure OpenAI tokens) and can be added if the terms are clarified. |
| SEC EDGAR XBRL API (company financials) | SEC blocks requests whose User-Agent does not contain contact details; the pipeline sends no personal data. |
| ecosyste.ms (package ecosystem sizes) | The data is CC BY-SA 4.0, but the terms forbid scripts that extract data without consent and limit use to personal purposes. |
| stackoverflow.com and stackexchange.com websites | robots.txt disallows all access; the API is used instead. |
| Google IPv6 statistics | The data files state "All Rights Reserved"; the APNIC Labs measurement, which permits re-use with attribution, is used instead. |
| Cloudflare Radar API (HTTP versions, post-quantum encryption, AI crawlers) | The data is licensed CC BY-NC 4.0, which does not permit the commercial reuse that the ODbL of this dataset allows; an API token is required. |
| PeeringDB | The Acceptable Use Policy forbids reproducing or passing on the data in bulk without prior permission. |
| RIPEstat Data API | The terms forbid re-distributing RIPEstat data. |
