# Data License

This license applies to the datasets in the `data/` folder of this repository. The code of the repository is licensed under the MIT License (see [LICENSE](../LICENSE) in the repository root).

## Table of Contents
- [License](#license)
- [Attribution](#attribution)
- [Third-Party Content](#third-party-content)
- [Obligations of Reusers](#obligations-of-reusers)

## License

The Tech Dataset is made available under the Open Database License: https://opendatacommons.org/licenses/odbl/1-0/. Any rights in individual contents of the database are licensed under the Database Contents License: https://opendatacommons.org/licenses/dbcl/1-0/, unless the field `license` of a record names other terms of its source.

Copyright (c) 2026 Conrad Kleinn.

## Attribution

The datasets contain data from the following sources. The field `license` of every record names the license or terms of its source.

| Source | Datasets | License or terms |
|---|---|---|
| Wikimedia Foundation, Pageviews API (https://wikimedia.org/api/rest_v1/) | `wikipedia_pageviews` | CC0 1.0 |
| Wikidata (https://www.wikidata.org/) | article titles of `wikipedia_pageviews` | CC0 1.0 |
| Stack Exchange API (https://api.stackexchange.com/) | `stackoverflow_questions` | Counts derived from Stack Overflow; content of the Stack Exchange Network is licensed under CC BY-SA. Source: Stack Exchange Network. |
| GitHub REST API (https://docs.github.com/rest) | `github_repositories` | GitHub Terms of Service; public, non-personal repository metadata |
| npm public registry API (https://api.npmjs.org/) | `package_downloads` | npm Open-Source Terms |
| pypistats.org (https://pypistats.org/) | `package_downloads` | PyPI download statistics provided by pypistats.org |
| crates.io API (https://crates.io/) | `package_downloads` | crates.io data access policy |
| Docker Hub API (https://hub.docker.com/) | `docker_image_pulls` | Docker Terms of Service |
| endoflife.date (https://endoflife.date/) | `release_cycles` | MIT License |
| LiteLLM model price list (https://github.com/BerriAI/litellm) | `llm_prices` | MIT License |
| Epoch AI (https://epoch.ai/data) | `ai_models`, `ml_hardware`, `ai_capability_scores` | CC BY 4.0. Source: Epoch AI. |
| Hugging Face Hub API (https://huggingface.co/) | `huggingface_repos`, `huggingface_repo_stats`, `huggingface_hub_totals` | Hugging Face Terms of Service |
| arXiv API (https://info.arxiv.org/help/api/) | `arxiv_submissions` | Metadata under CC0 1.0. Thank you to arXiv for use of its open access interoperability. |
| Hacker News Search API by Algolia (https://hn.algolia.com/api) | `hackernews_stories`, `hackernews_mentions` | Hacker News content (Y Combinator); titles remain subject to the rights of their authors |
| National Vulnerability Database, NIST (https://nvd.nist.gov/) | `cve_daily_counts`, NVD fields of `known_exploited_vulnerabilities` | Public domain. This product uses data from the NVD API but is not endorsed or certified by the NVD. |
| CISA Known Exploited Vulnerabilities catalog (https://www.cisa.gov/known-exploited-vulnerabilities-catalog) | `known_exploited_vulnerabilities` | CC0 1.0 |
| RFC Editor (https://www.rfc-editor.org/) | `rfcs` | IETF Trust; RFC index metadata |
| IETF Datatracker (https://datatracker.ietf.org/) | `ietf_draft_activity` | IETF Trust |
| World Bank, World Development Indicators (https://data.worldbank.org/) | `world_bank_indicators` | CC BY 4.0. Source: World Bank. |
| Eurostat (https://ec.europa.eu/eurostat) | `eurostat_indicators` | Reuse authorised provided the source is acknowledged. Source: Eurostat. |
| Indeed Hiring Lab (https://github.com/hiring-lab) | `job_postings`, `ai_job_postings` | CC BY 4.0. Source: Indeed Hiring Lab. |
| Number Resource Organization and the regional internet registries AFRINIC, APNIC, ARIN, LACNIC and RIPE NCC (https://www.nro.net/) | `ip_address_delegations` | Counts derived from the publicly available extended delegated statistics |
| bgp.potaroo.net, Geoff Huston (https://bgp.potaroo.net/) | `bgp_routing_table` | No license published; daily values derived from the published series |
| APNIC Labs (https://stats.labs.apnic.net/ipv6/) | `ipv6_adoption` | (C) APNIC Pty Ltd; re-use with attribution permitted |
| StatCounter Global Stats (https://gs.statcounter.com/) | `market_shares` | CC BY-SA 3.0. Source: StatCounter Global Stats. |
| TOP500 (https://www.top500.org/) | `supercomputer_lists` | Aggregates derived from the TOP500 lists, copyright TOP500.org |
| HTTP Archive (https://httparchive.org/) | `web_metrics` | Free use; attribution requested. Source: HTTP Archive. |

The registry entries in `technologies` were compiled by this project.

## Third-Party Content

`hackernews_stories` contains titles and URLs of Hacker News submissions, and `known_exploited_vulnerabilities` contains short vulnerability names given by CISA. No full texts, descriptions or comments are stored. Titles remain subject to the rights of their authors; the licenses above cover the selection, the structure and the derived variables of this database, not these contents.

## Obligations of Reusers

In summary, and without replacing the license text:

- **Attribution:** state that the data comes from this dataset and keep the attributions of the sources listed above, in particular for the CC BY and CC BY-SA sources (Epoch AI, World Bank, Indeed Hiring Lab, Eurostat, StatCounter) and for APNIC Labs and the HTTP Archive.
- **Share-alike:** a database that is derived from this dataset and used publicly must be published under the ODbL.
- **Produced works:** charts, model predictions or articles created from the data require a notice that names the dataset and the ODbL, but do not have to be published under the ODbL.
- **No technical restrictions:** a derived database that is published with technical restrictions must also be offered without them.
