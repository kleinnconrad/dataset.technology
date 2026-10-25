"""
Defines the Pydantic models of the records of the tech dataset.

Every stored record is an instance of a subclass of ``RecordBase``. The
vocabularies are ``Literal`` aliases, so that the allowed values are part of
the schema and are checked when a record is created.
"""
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

TechnologyDomain = Literal["ai_ml", "software_development", "data", "it_infrastructure", "networking", "security",
                           "hardware", "emerging_tech"]
TechnologyCategory = Literal[
    "ai_concept", "ai_model_family", "ai_framework", "ai_application",
    "programming_language", "web_framework", "runtime", "developer_tool", "software_practice",
    "database", "data_platform", "data_library",
    "operating_system", "cloud_platform", "container_platform", "infrastructure_as_code", "observability", "ci_cd",
    "web_server", "virtualization", "data_center",
    "network_protocol", "mobile_wireless", "network_architecture",
    "security_technology", "security_threat",
    "processor_architecture", "accelerator", "memory_storage", "semiconductor_manufacturing",
    "quantum_computing", "blockchain", "extended_reality", "robotics",
]
# Domain of every category
CATEGORY_DOMAINS: dict[str, str] = {
    "ai_concept": "ai_ml", "ai_model_family": "ai_ml", "ai_framework": "ai_ml", "ai_application": "ai_ml",
    "programming_language": "software_development", "web_framework": "software_development",
    "runtime": "software_development", "developer_tool": "software_development",
    "software_practice": "software_development",
    "database": "data", "data_platform": "data", "data_library": "data",
    "operating_system": "it_infrastructure", "cloud_platform": "it_infrastructure",
    "container_platform": "it_infrastructure", "infrastructure_as_code": "it_infrastructure",
    "observability": "it_infrastructure", "ci_cd": "it_infrastructure", "web_server": "it_infrastructure",
    "virtualization": "it_infrastructure", "data_center": "it_infrastructure",
    "network_protocol": "networking", "mobile_wireless": "networking", "network_architecture": "networking",
    "security_technology": "security", "security_threat": "security",
    "processor_architecture": "hardware", "accelerator": "hardware", "memory_storage": "hardware",
    "semiconductor_manufacturing": "hardware",
    "quantum_computing": "emerging_tech", "blockchain": "emerging_tech", "extended_reality": "emerging_tech",
    "robotics": "emerging_tech",
}


class RecordBase(BaseModel):
    """Fields that every record of the dataset has."""
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(description="Stable id of the item. Together with revision it identifies a record.")
    revision: int = Field(default=0, description="0 for the first observation; incremented each time the source changes the item.")
    dataset: str = Field(description="Name of the dataset.")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Version of the record schema.")
    observed_at: datetime = Field(description="UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t.")
    source_id: str = Field(description="Id of the source (API or file).")
    source_url: Optional[str] = Field(description="URL of the item or of the API resource it was read from.")
    license: str = Field(description="License or terms of the source.")
    backfilled: bool = Field(description="True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became known.")


class TechnologyRecord(RecordBase):
    """A tracked technology of the registry, recorded when the entry appears or changes."""
    technology_id: str = Field(description="Stable id of the technology; the join key of the technology-related datasets.")
    name: str = Field(description="Display name.")
    domain: TechnologyDomain = Field(description="Domain of the category.")
    category: TechnologyCategory = Field(description="Category of the technology.")
    added: date = Field(description="Day on which the technology was added to the registry; start of coverage.")
    removed: Optional[date] = Field(description="Day on which the technology was removed from the registry; end of coverage.")
    removed_reason: Optional[str] = Field(description="Why the technology was removed.")
    wikidata_qid: Optional[str] = Field(description="Wikidata id.")
    stackoverflow_tags: list[str] = Field(description="Stack Overflow tags counted for the technology.")
    github_repos: list[str] = Field(description="GitHub repositories tracked for the technology (owner/name).")
    packages: list[str] = Field(description="Packages and container images tracked for the technology (ecosystem:name).")
    endoflife_product: Optional[str] = Field(description="Product slug at endoflife.date.")
    hn_terms: list[str] = Field(description="Terms counted in Hacker News stories and comments.")
    github_language: Optional[str] = Field(description="Language name in GitHub search.")


class PageviewRecord(RecordBase):
    """Daily views of the English Wikipedia article of a technology."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    wikipedia_project: str = Field(description="Wikipedia edition, e.g. en.wikipedia.")
    wikipedia_title: str = Field(description="Title of the article at the time of observation.")
    wikidata_qid: Optional[str] = Field(description="Wikidata id of the technology.")
    daily_views: dict[date, int] = Field(description="Views by people (agent type user) per day (UTC), for the days since the previous run.")


class ReleaseCycleRecord(RecordBase):
    """One release cycle of a product at endoflife.date, recorded when it appears or changes."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    product: str = Field(description="Product slug at endoflife.date.")
    cycle: str = Field(description="Name of the release cycle, e.g. '3.13' or '22.04'.")
    codename: Optional[str] = Field(description="Codename of the cycle.")
    release_date: Optional[date] = Field(description="Release date of the cycle.")
    is_lts: Optional[bool] = Field(description="Whether the cycle has long-term support.")
    lts_from: Optional[date] = Field(description="Day from which the cycle has long-term support.")
    active_support_until: Optional[date] = Field(description="End of active support (bug fixes); null if not stated.")
    eol_date: Optional[date] = Field(description="End of life (end of security support); null if not stated.")
    is_eol: Optional[bool] = Field(description="Whether the cycle had reached its end of life at the time of observation.")
    extended_support_until: Optional[date] = Field(description="End of paid extended support; null if not stated.")
    is_maintained: Optional[bool] = Field(description="Whether the cycle was maintained at the time of observation.")
    latest_version: Optional[str] = Field(description="Latest release of the cycle at the time of observation.")
    latest_release_date: Optional[date] = Field(description="Release date of the latest release.")
    is_listed: bool = Field(description="False in the record written when the cycle disappeared from the product page.")


class StackOverflowTagRecord(RecordBase):
    """Weekly counts of new Stack Overflow questions with a tag of a technology, and the total of the tag."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    tag: str = Field(description="Stack Overflow tag.")
    weekly_questions: dict[date, int] = Field(description="Questions created per week (key: Monday of the ISO week, UTC) as counted at the time of observation, for the weeks since the previous run. Deleted questions are not counted.")
    total_questions: Optional[int] = Field(description="Number of questions with the tag at the time of observation.")


class GitHubRepositoryRecord(RecordBase):
    """Popularity and activity of a GitHub repository of a technology at the time of observation."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    repo: str = Field(description="Repository as listed in the registry (owner/name).")
    repo_id: Optional[int] = Field(description="Numeric repository id at GitHub; stays the same when the repository is renamed or transferred.")
    full_name: str = Field(description="Current owner/name of the repository at GitHub.")
    stars: int = Field(description="Number of stars.")
    forks: int = Field(description="Number of forks.")
    open_issues: int = Field(description="Number of open issues and pull requests.")
    watchers: int = Field(description="Number of users watching the repository (subscribers).")
    size_kb: Optional[int] = Field(description="Size of the repository in kilobytes as reported by GitHub.")
    primary_language: Optional[str] = Field(description="Main language of the repository.")
    repo_license: Optional[str] = Field(description="SPDX id of the license of the repository; NOASSERTION if GitHub cannot determine it.")
    topics: list[str] = Field(description="Topics of the repository.")
    created_at: Optional[datetime] = Field(description="Creation time of the repository (UTC).")
    pushed_at: Optional[datetime] = Field(description="Time of the latest push (UTC).")
    archived: bool = Field(description="Whether the repository is archived (read-only).")
    contributors: Optional[int] = Field(description="Number of contributors including anonymous ones; null if GitHub does not list them, e.g. for very large repositories.")
    commits_last_4_weeks: Optional[int] = Field(description="Commits to the default branch in the 4 weeks before the day of observation, without merge commits.")
    commits_last_52_weeks: Optional[int] = Field(description="Commits to the default branch in the 52 weeks before the day of observation, without merge commits.")
    latest_release_tag: Optional[str] = Field(description="Tag of the latest release that is neither a draft nor a prerelease; null if the repository publishes no releases on GitHub.")
    latest_release_at: Optional[datetime] = Field(description="Publication time of the latest release (UTC).")


class PackageDownloadRecord(RecordBase):
    """Daily downloads of a package of a technology from its registry (npm, PyPI or crates.io)."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    ecosystem: Literal["npm", "pypi", "crates"] = Field(description="Package registry.")
    package: str = Field(description="Package name.")
    daily_downloads: dict[date, int] = Field(description="Downloads per day (UTC), for the days since the previous run and days that were not available before. PyPI downloads exclude mirrors.")


class DockerImageRecord(RecordBase):
    """Cumulative pulls and stars of a container image on Docker Hub at the time of observation."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    image: str = Field(description="Image as namespace/repository, e.g. library/postgres.")
    pull_count: int = Field(description="Pulls since the image was created.")
    star_count: Optional[int] = Field(description="Number of stars.")
    last_updated: Optional[datetime] = Field(description="Time of the latest push to the image (UTC).")


class CveDailyCountRecord(RecordBase):
    """Counts of the CVE records that the NVD published on one day, by status, severity, weakness and vendor, as observed after the day ended."""
    published_date: date = Field(description="Day (UTC) on which the NVD published the CVE records.")
    cves: int = Field(description="Number of CVE records published that day, including rejected ones; rejected records count only in cves, rejected and by_status.")
    rejected: int = Field(description="Records with the status Rejected.")
    by_status: dict[str, int] = Field(description="Records per NVD analysis status at the time of observation, e.g. Received, Awaiting Analysis, Deferred, Analyzed.")
    by_cvss_v3_severity: dict[str, int] = Field(description="Records per CVSS v3.x base severity (CRITICAL, HIGH, MEDIUM, LOW, NONE), from the NVD's own score where present, else from the score of another source.")
    by_cvss_v4_severity: dict[str, int] = Field(description="Records per CVSS v4.0 base severity, chosen in the same way.")
    unscored: int = Field(description="Records without a CVSS v3.x or v4.0 score at the time of observation.")
    nvd_scored: int = Field(description="Records with a CVSS score from the NVD itself (source nvd@nist.gov).")
    by_cwe: dict[str, int] = Field(description="Records per weakness (CWE id, e.g. CWE-79); a record with several weaknesses counts for each.")
    by_vendor: dict[str, int] = Field(description="Records per vendor of the affected products, from the CPE configurations of the NVD, or from the free-text CNA data if there are none (including placeholders such as unknown); vendor names in lower case.")
    with_exploit_reference: int = Field(description="Records with a reference tagged Exploit.")
    kev_listed: int = Field(description="Records already in the CISA Known Exploited Vulnerabilities catalog at the time of observation.")


class KnownExploitedVulnerabilityRecord(RecordBase):
    """A vulnerability in the CISA Known Exploited Vulnerabilities catalog, recorded when it is added or changes."""
    cve_id: str = Field(description="CVE id.")
    vendor_project: str = Field(description="Vendor or project as written by CISA.")
    product: str = Field(description="Product as written by CISA.")
    vulnerability_name: str = Field(description="Short name of the vulnerability given by CISA.")
    date_added: date = Field(description="Day on which CISA added the vulnerability to the catalog.")
    due_date: Optional[date] = Field(description="Remediation deadline for US federal agencies.")
    known_ransomware_use: Optional[bool] = Field(description="True if CISA knows of use in ransomware campaigns; false if this is unknown.")
    cwe_ids: list[str] = Field(description="Weaknesses (CWE ids) named by CISA.")
    cve_published_at: Optional[datetime] = Field(description="Publication time of the CVE record in the NVD (UTC).")
    cvss_base_score: Optional[float] = Field(description="CVSS base score at the first observation of the entry (v3.x, else v4.0), from the NVD's own score where present; later revisions repeat it.")
    cvss_version: Optional[str] = Field(description="CVSS version of the score.")
    cvss_source: Optional[Literal["nvd", "other"]] = Field(description="nvd if the score is the NVD's own, other for scores of the CNA or other sources.")
    is_listed: bool = Field(description="False in the record written when the vulnerability disappeared from the catalog.")


class RfcRecord(RecordBase):
    """An RFC in the index of the RFC Editor, recorded when it appears or its status or relations change."""
    rfc_number: int = Field(description="Number of the RFC.")
    title: str = Field(description="Title of the RFC.")
    publication_year: int = Field(description="Year of publication.")
    publication_month: int = Field(description="Month of publication (1 to 12).")
    page_count: Optional[int] = Field(description="Number of pages.")
    author_count: int = Field(description="Number of authors. Author names are not stored.")
    current_status: str = Field(description="Current status, e.g. PROPOSED STANDARD, INFORMATIONAL, HISTORIC.")
    publication_status: str = Field(description="Status at publication.")
    stream: Optional[str] = Field(description="Stream: IETF, IRTF, IAB, INDEPENDENT, Editorial or Legacy.")
    area: Optional[str] = Field(description="IETF area of the working group (the current area of the group, not necessarily the area at publication).")
    working_group: Optional[str] = Field(description="Acronym of the working group; null for documents without a working group.")
    keywords: list[str] = Field(description="Keywords of the RFC.")
    obsoletes: list[str] = Field(description="Documents that this RFC obsoletes, e.g. RFC2616.")
    obsoleted_by: list[str] = Field(description="Documents that obsolete this RFC.")
    updates: list[str] = Field(description="Documents that this RFC updates.")
    updated_by: list[str] = Field(description="Documents that update this RFC.")
    is_also: list[str] = Field(description="Series memberships, e.g. BCP14 or STD7.")
    draft: Optional[str] = Field(description="Name of the Internet-Draft the RFC was published from.")
    has_errata: bool = Field(description="Whether errata are reported for the RFC.")
    doi: Optional[str] = Field(description="DOI of the RFC.")


class IetfDraftActivityRecord(RecordBase):
    """Weekly counts of Internet-Draft submissions in the IETF Datatracker, for all drafts or by group type or area."""
    scope: str = Field(description="all, group_type:<type> (wg, individ, rg) or area:<acronym> (the working groups of an IETF area, e.g. area:sec).")
    weekly_new_drafts: dict[date, int] = Field(description="First versions (revision 00) submitted per week (key: Monday of the ISO week, UTC), for the weeks since the previous run.")
    weekly_revisions: dict[date, int] = Field(description="Draft versions of any revision submitted per week (key: Monday of the ISO week, UTC).")


class WorldBankIndicatorRecord(RecordBase):
    """Values of a World Bank development indicator for one country or aggregate. Revision 0 holds all years; later revisions hold only the years whose values were added or changed."""
    indicator_id: str = Field(description="Indicator code, e.g. IT.NET.USER.ZS.")
    indicator_name: str = Field(description="Name of the indicator.")
    country_code: str = Field(description="ISO 3166-1 alpha-3 code of the country, or the World Bank code of the aggregate.")
    country_name: str = Field(description="Name of the country or aggregate.")
    is_aggregate: bool = Field(description="True for regions, income groups and other aggregates.")
    values: dict[int, float] = Field(description="Value per year; in revisions after the first only the added or changed years.")
    removed_years: list[int] = Field(description="Years whose value was removed since the previous revision.")
    source_last_updated: Optional[date] = Field(description="Last update of the indicator at the World Bank.")


class EurostatIndicatorRecord(RecordBase):
    """Annual values of a Eurostat indicator on the digital economy and society for one country or the EU, recorded when they appear or change."""
    dataset_code: str = Field(description="Eurostat dataset, e.g. isoc_eb_ai.")
    indicator: str = Field(description="Id of the series within the dataset, e.g. E_AI_TANY.")
    indicator_label: str = Field(description="Label of the series.")
    unit: str = Field(description="Unit, e.g. PC_ENT (percentage of enterprises).")
    geo: str = Field(description="Country code used by Eurostat (EL for Greece) or EU27_2020.")
    values: dict[int, float] = Field(description="Value per year.")
    source_updated: Optional[datetime] = Field(description="Last data update of the dataset at Eurostat.")


class IpDelegationRecord(RecordBase):
    """Internet number resources (IPv4, IPv6, AS numbers) of a regional internet registry per country and status, from the combined statistics of the NRO."""
    registry: str = Field(description="Regional internet registry: afrinic, apnic, arin, lacnic or ripencc (iana for IANA records).")
    country: str = Field(description="ISO 3166-1 alpha-2 code of the country in which the resource holders are legally based, or a registry code such as EU or AP; ZZ for resources that are not delegated and for the few delegated ranges without a country.")
    status: Literal["assigned", "available", "reserved"] = Field(description="assigned (allocated or assigned to holders), available (free pool of the registry) or reserved.")
    ipv4_addresses: int = Field(description="Number of IPv4 addresses.")
    ipv4_ranges: int = Field(description="Number of IPv4 ranges.")
    ipv6_slash32_equivalents: float = Field(description="IPv6 address space in units of /32 prefixes.")
    ipv6_prefixes: int = Field(description="Number of IPv6 prefixes.")
    asns: int = Field(description="Number of AS numbers.")
    asn_ranges: int = Field(description="Number of AS number ranges.")
    file_date: date = Field(description="Date of the statistics file.")


class BgpTableRecord(RecordBase):
    """Daily size of the global BGP routing table for IPv4 or IPv6, as seen from the route collector of bgp.potaroo.net."""
    ip_version: Literal[4, 6] = Field(description="IP version of the routing table.")
    daily_prefixes: dict[date, int] = Field(description="Number of announced prefixes per day (last value of the UTC day), for the days since the previous run.")
    daily_ases: dict[date, int] = Field(description="Number of origin autonomous systems per day (last value of the UTC day).")


class Ipv6AdoptionRecord(RecordBase):
    """Daily share of users that are IPv6-capable and that prefer IPv6, measured by APNIC Labs, for an economy or the world."""
    economy: str = Field(description="ISO 3166-1 alpha-2 code of the economy, or XA for the world.")
    daily_capable_pct: dict[date, float] = Field(description="Share of measured users that can use IPv6, in percent per day, for the days since the previous run.")
    daily_preferred_pct: dict[date, float] = Field(description="Share of measured users that use IPv6 when both protocols are available, in percent per day.")
    daily_samples: dict[date, int] = Field(description="Number of measurements (experiments) per day; for the world (XA) a weighted number, rounded.")


class MarketShareRecord(RecordBase):
    """Monthly market shares of operating systems, browsers, search engines, device platforms or AI chatbots measured by StatCounter, for a region."""
    statistic: str = Field(description="Measured statistic: os, desktop_os, mobile_os, browser, search_engine, platform or ai_chatbot.")
    region: str = Field(description="ww for worldwide, or an ISO 3166-1 alpha-2 code.")
    monthly_shares: dict[date, dict[str, float]] = Field(description="Share in percent per category (e.g. Windows, Android, Chrome, Google, ChatGPT) per month (key: first day of the month), for the months since the previous run; the categories are those that StatCounter lists for the period, the rest is Other.")


class SupercomputerListRecord(RecordBase):
    """Aggregates of one edition of the TOP500 list of supercomputers."""
    list_edition: date = Field(description="Edition of the list (first day of the month of publication, June or November).")
    systems: int = Field(description="Number of systems in the list.")
    total_rmax_tflops: float = Field(description="Sum of the measured LINPACK performance (Rmax) of all systems in TFlop/s.")
    rank1_rmax_tflops: float = Field(description="Rmax of the first system in TFlop/s.")
    rank500_rmax_tflops: float = Field(description="Rmax of the last system (entry level of the list) in TFlop/s.")
    rank1_system: str = Field(description="Name of the first system.")
    rank1_country: str = Field(description="Country of the first system.")
    total_cores: int = Field(description="Sum of the cores of all systems.")
    total_power_kw: Optional[float] = Field(description="Sum of the power of the systems that report it, in kW.")
    systems_with_power: int = Field(description="Number of systems that report their power.")
    systems_by_country: dict[str, int] = Field(description="Number of systems per country.")
    rmax_by_country_tflops: dict[str, float] = Field(description="Sum of Rmax per country in TFlop/s.")
    systems_by_manufacturer: dict[str, int] = Field(description="Number of systems per manufacturer.")
    systems_with_accelerator: Optional[int] = Field(description="Number of systems with accelerators or co-processors; null if the list does not state them.")


class WebMetricRecord(RecordBase):
    """Monthly metric of the web from the crawls of the HTTP Archive, for desktop or mobile pages."""
    metric: str = Field(description="Metric of the HTTP Archive reports, e.g. pctHttps (share of requests over HTTPS), h2 (share of requests over HTTP/2), bytesTotal_p50 (median page weight).")
    client: Literal["desktop", "mobile"] = Field(description="Crawled page variant.")
    unit: str = Field(description="Unit of the values, e.g. percent or KB.")
    monthly_values: dict[date, float] = Field(description="Value per crawl (key: crawl date), for the crawls since the previous run.")


class JobPostingRecord(RecordBase):
    """Daily index of job postings on Indeed for a country and an occupational category (1 February 2020 = 100)."""
    country: str = Field(description="ISO 3166-1 alpha-2 code of the country, or EA for the euro area.")
    category: str = Field(description="Occupational category, e.g. Software Development, or total postings and new postings for all categories.")
    seasonally_adjusted: bool = Field(description="Whether the index is seasonally adjusted.")
    daily_index: dict[date, float] = Field(description="Index value per day, for the days since the previous run. Values are stored as first published; later revisions are not tracked.")


class AiJobPostingRecord(RecordBase):
    """Daily share of job postings on Indeed that mention artificial intelligence, for a country."""
    country: str = Field(description="ISO 3166-1 alpha-2 code of the country.")
    daily_share_pct: dict[date, float] = Field(description="Share of postings that mention AI or generative AI terms in percent (7-day trailing average), for the days since the previous run.")


class LlmPriceRecord(RecordBase):
    """List prices and limits of one LLM offering (a model at a provider), recorded when it appears or changes."""
    model_key: str = Field(description="Key of the offering in the LiteLLM price list, e.g. 'gpt-4o' or 'bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0'.")
    provider: str = Field(description="Provider or platform that sells the offering, e.g. openai, anthropic, azure, bedrock, vertex_ai.")
    mode: str = Field(description="Kind of offering: chat, completion, responses or embedding.")
    input_price_usd_per_mtok: Optional[float] = Field(description="Price of one million input tokens in USD.")
    output_price_usd_per_mtok: Optional[float] = Field(description="Price of one million output tokens in USD.")
    cached_input_price_usd_per_mtok: Optional[float] = Field(description="Price of one million cached input tokens (cache read) in USD.")
    batch_input_price_usd_per_mtok: Optional[float] = Field(description="Price of one million input tokens in batch processing in USD.")
    batch_output_price_usd_per_mtok: Optional[float] = Field(description="Price of one million output tokens in batch processing in USD.")
    max_input_tokens: Optional[int] = Field(description="Maximum number of input tokens (context window).")
    max_output_tokens: Optional[int] = Field(description="Maximum number of output tokens per request.")
    supports_function_calling: Optional[bool] = Field(description="Whether the offering supports function (tool) calling.")
    supports_vision: Optional[bool] = Field(description="Whether the offering accepts image input.")
    supports_reasoning: Optional[bool] = Field(description="Whether the offering supports a reasoning mode.")
    supports_prompt_caching: Optional[bool] = Field(description="Whether the offering supports prompt caching.")
    supports_web_search: Optional[bool] = Field(description="Whether the offering supports built-in web search.")
    deprecation_date: Optional[date] = Field(description="Announced deprecation date of the offering.")
    pricing_page: Optional[str] = Field(description="URL of the provider's pricing page that the price list cites.")
    is_listed: bool = Field(description="False in the record written when the offering disappeared from the price list.")


class AiModelRecord(RecordBase):
    """An AI model in the Epoch AI database of AI models, recorded when it appears or changes."""
    model_name: str = Field(description="Name of the model at Epoch AI.")
    organization: Optional[str] = Field(description="Developing organization or organizations, as written by Epoch AI.")
    organization_categories: list[str] = Field(description="Categories of the developers, e.g. Industry or Academia.")
    countries: list[str] = Field(description="Countries of the developing organizations.")
    publication_date: Optional[date] = Field(description="Publication or release date of the model.")
    domains: list[str] = Field(description="Domains of the model, e.g. Language, Vision, Multimodal.")
    tasks: list[str] = Field(description="Tasks of the model.")
    notability_criteria: list[str] = Field(description="Criteria for which Epoch AI counts the model as notable; empty for models that are not notable.")
    frontier_model: Optional[bool] = Field(description="True if Epoch AI counts the model as a frontier model; null otherwise (the source has no false value).")
    parameters: Optional[float] = Field(description="Number of parameters.")
    training_compute_flop: Optional[float] = Field(description="Training compute in FLOP.")
    training_dataset_size: Optional[float] = Field(description="Size of the training dataset (tokens or data points).")
    training_time_hours: Optional[float] = Field(description="Training time in hours.")
    training_hardware: list[str] = Field(description="Hardware used for training.")
    hardware_quantity: Optional[float] = Field(description="Number of hardware units used for training.")
    training_compute_cost_usd_2023: Optional[float] = Field(description="Estimated cost of the training compute in 2023 USD.")
    training_power_draw_watts: Optional[float] = Field(description="Estimated power draw of the training hardware in watts.")
    finetune_compute_flop: Optional[float] = Field(description="Compute of the fine-tuning in FLOP.")
    post_training_compute_flop: Optional[float] = Field(description="Compute of the post-training in FLOP.")
    compute_estimation_method: Optional[str] = Field(description="How the training compute was determined, e.g. Reported.")
    confidence: Optional[str] = Field(description="Confidence of Epoch AI in the estimates.")
    model_accessibility: Optional[str] = Field(description="Access to the model, e.g. API access or Open weights (unrestricted).")
    open_weights: Optional[bool] = Field(description="Whether the model weights are published.")
    training_code_accessibility: Optional[str] = Field(description="Access to the training code.")
    base_model: Optional[str] = Field(description="Model that this model was fine-tuned from.")
    hugging_face_developer_id: Optional[str] = Field(description="Organization id of the developer on the Hugging Face Hub.")
    epoch_last_modified: Optional[datetime] = Field(description="Last modification of the entry at Epoch AI (UTC).")
    is_listed: bool = Field(description="False in the record written when the model disappeared from the database.")


class MlHardwareRecord(RecordBase):
    """A machine learning accelerator in the Epoch AI hardware database, recorded when it appears or changes."""
    hardware_name: str = Field(description="Name of the hardware at Epoch AI.")
    manufacturer: Optional[str] = Field(description="Manufacturer.")
    hardware_type: Optional[str] = Field(description="Type, e.g. GPU or TPU.")
    release_date: Optional[date] = Field(description="Release date.")
    release_price_usd: Optional[float] = Field(description="Price at release in USD.")
    fp64_flops: Optional[float] = Field(description="Double precision performance in FLOP/s.")
    fp32_flops: Optional[float] = Field(description="Single precision performance in FLOP/s.")
    tf32_flops: Optional[float] = Field(description="TensorFloat-32 performance in FLOP/s.")
    fp16_flops: Optional[float] = Field(description="Half precision performance in FLOP/s.")
    tensor_fp16_bf16_flops: Optional[float] = Field(description="Tensor core FP16/BF16 performance in FLOP/s.")
    fp8_flops: Optional[float] = Field(description="FP8 performance in FLOP/s.")
    fp4_flops: Optional[float] = Field(description="FP4 performance in FLOP/s.")
    int8_ops: Optional[float] = Field(description="INT8 performance in OP/s.")
    int4_ops: Optional[float] = Field(description="INT4 performance in OP/s.")
    memory_bytes: Optional[float] = Field(description="Memory capacity in bytes.")
    memory_bandwidth_bytes_per_s: Optional[float] = Field(description="Memory bandwidth in bytes per second.")
    intranode_bandwidth_bytes_per_s: Optional[float] = Field(description="Bandwidth between accelerators of a node in bytes per second.")
    internode_bandwidth_bits_per_s: Optional[float] = Field(description="Bandwidth between nodes in bits per second.")
    tdp_watts: Optional[float] = Field(description="Thermal design power in watts.")
    foundry: Optional[str] = Field(description="Chip manufacturer (foundry), e.g. TSMC.")
    process_size_nm: Optional[float] = Field(description="Process size in nanometers.")
    transistors_millions: Optional[float] = Field(description="Number of transistors in millions.")
    die_size_mm2: Optional[float] = Field(description="Die size in square millimeters.")
    ml_models: list[str] = Field(description="Models in the Epoch AI database trained on this hardware.")
    epoch_last_modified: Optional[datetime] = Field(description="Last modification of the entry at Epoch AI (UTC).")
    is_listed: bool = Field(description="False in the record written when the hardware disappeared from the database.")


class AiCapabilityScoreRecord(RecordBase):
    """The Epoch Capabilities Index (ECI) score of an AI model, recorded when it appears or changes."""
    model_name: str = Field(description="Name of the model at Epoch AI.")
    display_name: Optional[str] = Field(description="Display name of the model.")
    eci: float = Field(description="Epoch Capabilities Index score.")
    eci_ci_low: Optional[float] = Field(description="Lower bound of the confidence interval of the score.")
    eci_ci_high: Optional[float] = Field(description="Upper bound of the confidence interval of the score.")
    model_date: Optional[date] = Field(description="Release date of the model as used by Epoch AI.")
    organization: Optional[str] = Field(description="Developing organization.")
    country: Optional[str] = Field(description="Country of the developing organization.")
    model_accessibility: Optional[str] = Field(description="Access to the model, e.g. API access.")
    accessibility_group: Optional[str] = Field(description="Open weights or closed weights.")
    is_listed: bool = Field(description="False in the record written when the score disappeared from the data.")


class ArxivSubmissionRecord(RecordBase):
    """Weekly counts of new arXiv submissions in a subject category, including cross-lists."""
    category: str = Field(description="arXiv category, e.g. cs.LG.")
    category_name: str = Field(description="Name of the category, e.g. Machine Learning.")
    weekly_submissions: dict[date, int] = Field(description="Number of first versions submitted per week (key: Monday of the ISO week, UTC), for the weeks since the previous run.")


class HackerNewsStoryRecord(RecordBase):
    """A Hacker News story that reached the point threshold, observed at least two days after it was posted."""
    story_id: str = Field(description="Id of the story at Hacker News.")
    created_at: datetime = Field(description="Time the story was posted (UTC).")
    title: str = Field(description="Title of the story.")
    url: Optional[str] = Field(description="Linked URL; null for text posts such as Ask HN.")
    domain: Optional[str] = Field(description="Host of the linked URL without a leading www.")
    story_type: Literal["story", "ask_hn", "show_hn", "launch_hn"] = Field(description="Type of the post.")
    points: int = Field(description="Points at the time of observation.")
    comments: int = Field(description="Number of comments at the time of observation.")
    technology_ids: list[str] = Field(description="Technologies whose Hacker News terms occur in the title as whole words.")


class HackerNewsMentionRecord(RecordBase):
    """Weekly counts of Hacker News stories and comments that mention a term of a technology."""
    technology_id: str = Field(description="Id of the technology in the registry.")
    term: str = Field(description="The counted term; matched as a whole word or phrase, without typo tolerance.")
    weekly_stories: dict[date, int] = Field(description="Stories with the term in the title per week (key: Monday of the ISO week, UTC), for the weeks since the previous run.")
    weekly_comments: dict[date, int] = Field(description="Comments that contain the term per week (key: Monday of the ISO week, UTC).")
    approximate_weeks: list[date] = Field(description="Weeks whose counts the search service reported as not exhaustive.")


class HuggingFaceRepoRecord(RecordBase):
    """Attributes of a model or dataset repository of an organization on the Hugging Face Hub, recorded when it enters the tracked top list or changes."""
    repo_type: Literal["model", "dataset"] = Field(description="Type of the repository.")
    repo_id: str = Field(description="Repository id (organization/name).")
    organization: str = Field(description="Organization that owns the repository.")
    created_at: Optional[datetime] = Field(description="Creation time of the repository (UTC).")
    pipeline_tag: Optional[str] = Field(description="Task of a model, e.g. text-generation; null for datasets.")
    library_name: Optional[str] = Field(description="Library of a model, e.g. transformers; null for datasets.")
    repo_license: Optional[str] = Field(description="License tag of the repository, e.g. apache-2.0; several tags are separated by commas.")
    base_models: list[str] = Field(description="Base models of a fine-tuned, quantized, adapted or merged model; base models of user accounts are left out.")
    base_model_relation: Optional[str] = Field(description="Relation to the base models, e.g. finetune or quantized.")
    parameters: Optional[int] = Field(description="Number of parameters of a model from its safetensors files.")
    gated: Optional[str] = Field(description="Access gating: false, auto or manual.")


class HuggingFaceRepoStatsRecord(RecordBase):
    """Download and like counts of a top model or dataset repository of an organization on the Hugging Face Hub."""
    repo_type: Literal["model", "dataset"] = Field(description="Type of the repository.")
    repo_id: str = Field(description="Repository id (organization/name).")
    rank: int = Field(description="Rank by downloads in the last 30 days among all public repositories of the type, including those of users.")
    downloads_30d: int = Field(description="Downloads in the last 30 days.")
    downloads_all_time: Optional[int] = Field(description="Downloads since the creation of the repository.")
    likes: Optional[int] = Field(description="Number of likes.")
    trending_score: Optional[float] = Field(description="Trending score of the Hub; low scores are fractions.")


class HuggingFaceHubTotalsRecord(RecordBase):
    """Total numbers of models, datasets and Spaces on the Hugging Face Hub."""
    models: Optional[int] = Field(description="Number of public models.")
    datasets: Optional[int] = Field(description="Number of public datasets.")
    spaces: Optional[int] = Field(description="Number of public Spaces.")
    user_repos_in_top_models: Optional[int] = Field(description="Number of repositories of users (not organizations) among the tracked top models.")
    user_repos_in_top_datasets: Optional[int] = Field(description="Number of repositories of users (not organizations) among the tracked top datasets.")


DATASET_MODELS: dict[str, type[RecordBase]] = {
    "technologies": TechnologyRecord,
    "wikipedia_pageviews": PageviewRecord,
    "release_cycles": ReleaseCycleRecord,
    "stackoverflow_questions": StackOverflowTagRecord,
    "github_repositories": GitHubRepositoryRecord,
    "package_downloads": PackageDownloadRecord,
    "docker_image_pulls": DockerImageRecord,
    "cve_daily_counts": CveDailyCountRecord,
    "known_exploited_vulnerabilities": KnownExploitedVulnerabilityRecord,
    "rfcs": RfcRecord,
    "ietf_draft_activity": IetfDraftActivityRecord,
    "world_bank_indicators": WorldBankIndicatorRecord,
    "eurostat_indicators": EurostatIndicatorRecord,
    "ip_address_delegations": IpDelegationRecord,
    "bgp_routing_table": BgpTableRecord,
    "ipv6_adoption": Ipv6AdoptionRecord,
    "market_shares": MarketShareRecord,
    "supercomputer_lists": SupercomputerListRecord,
    "web_metrics": WebMetricRecord,
    "job_postings": JobPostingRecord,
    "ai_job_postings": AiJobPostingRecord,
    "llm_prices": LlmPriceRecord,
    "ai_models": AiModelRecord,
    "ml_hardware": MlHardwareRecord,
    "ai_capability_scores": AiCapabilityScoreRecord,
    "arxiv_submissions": ArxivSubmissionRecord,
    "hackernews_stories": HackerNewsStoryRecord,
    "hackernews_mentions": HackerNewsMentionRecord,
    "huggingface_repos": HuggingFaceRepoRecord,
    "huggingface_repo_stats": HuggingFaceRepoStatsRecord,
    "huggingface_hub_totals": HuggingFaceHubTotalsRecord,
}
