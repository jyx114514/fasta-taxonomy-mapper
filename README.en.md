# FASTA UniProt to NCBI Taxonomy Mapper

English | [简体中文](README.md)

`fasta_taxonomy_mapper.py` reads FASTA files with Biopython and uses the official UniProt REST API to map accessions to organisms in the current database. Each unique accession is queried once, while the output retains every original FASTA record, including duplicates, in its original order.

## Requirements and execution

- Python 3.11 or later.
- Only `requests`, `biopython`, and the Python standard library are required. Dependency ranges are listed in `requirements-fasta-taxonomy.txt`.
- The script does not automatically install dependencies, rebuild virtual environments, or upgrade Python.
- Validated with Python 3.11.9, requests 2.34.2, and Biopython 1.88.

All examples use relative paths. They do not depend on a personal username, drive letter, or fixed project location. Run the commands from the project root using an existing project virtual environment. The current layout is:

```text
project-root/
├── .venv/
├── 重写/
│   ├── fasta_taxonomy_mapper.py
│   ├── README.md
│   └── README.en.md
├── 配置文件/
│   └── requirements-fasta-taxonomy.txt
├── examples/
│   └── taxonomy_smoke.fasta
├── tests/
└── validation/
```

Show help in Windows PowerShell. Each command is on one line and requires no line-continuation characters:

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --help
```

Map the bundled small FASTA file:

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --input examples/taxonomy_smoke.fasta --output results/taxonomy_mapping.tsv
```

The script uses `pathlib.Path`. Relative paths are resolved against the directory from which the command is launched, rather than the script directory. If you reorganize the files, adjust the relative script, input, and output paths accordingly.

On Linux or WSL, use a project environment created for that platform, for example:

```bash
.venv/bin/python ./重写/fasta_taxonomy_mapper.py --input examples/taxonomy_smoke.fasta --output results/taxonomy_mapping.tsv
```

## Usage examples

`data/proteins.fasta` below is a generic input example. Replace it with the path to your FASTA file.

Build mappings for all species:

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv --cache results/uniprot_taxonomy.cache.sqlite3
```

Filter human proteins using the exact NCBI Taxonomy ID `9606`:

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv --cache results/uniprot_taxonomy.cache.sqlite3 --batch-size 50 --timeout 30 --retries 3 --taxon 9606 --filtered-fasta results/human.fasta
```

`--output` always contains the complete mapping table. With `--taxon`, the table records the target-species decision and the report includes target counts. `--filtered-fasta` creates a separate FASTA containing only confirmed target records. Chimpanzee `9598`, bonobo `9597`, and other primates are not included in a `9606` filter.

Filtering copies the original FASTA record bytes, preserving headers, sequences, line endings, wrapping, and duplicate records. API sequences, headers, and GO annotations never replace the input. The tool does not read or rewrite historical GO label files.

## Official API and matching rules

The implementation was checked against official documentation and actual API requests:

- [UniProtKB query fields](https://www.uniprot.org/help/query-fields): `accession:` searches current primary accessions; `sec_acc:` searches historical secondary accessions.
- [Queries and pagination](https://www.uniprot.org/help/api_queries), [pagination details](https://www.uniprot.org/help/pagination): the tool uses `/uniprotkb/search` and follows the response header `Link: ...; rel="next"`.
- [Accession rules, merges, and splits](https://www.uniprot.org/help/accession_numbers): a historical accession can correspond to multiple current entries.
- [Official return-field configuration](https://rest.uniprot.org/configure/uniprotkb/result-fields).

The query workflow is:

1. Extract `record.id` using Biopython. Preserve the original ID, normalize accessions to uppercase, and deduplicate queries in first-occurrence order.
2. Reuse unexpired local cache entries first.
3. Send batched GET requests to `https://rest.uniprot.org/uniprotkb/search` with parameters such as:

   ```text
   query=active:true AND (accession:P69905 OR accession:P68871)
   format=json
   fields=accession,organism_id,organism_name
   size=500
   ```

4. Verify that JSON `primaryAccession` exactly equals the input accession. Validate `organism.taxonId` and `organism.scientificName`. Query `sec_acc:` only for IDs without a primary match after the primary query completes.
5. Omit `fields` for secondary queries and check the full JSON to confirm that the input accession is present in `secondaryAccessions`. `sec_acc` is a valid query field, but `fields=sec_acc` returns HTTP 400. Other annotations are discarded and are not written to the results or cache.
6. Accept a secondary mapping only when it has exactly one current candidate. Multiple candidates remain ambiguous, even if their species are identical.
7. Assign `unmapped` only after both exact queries have completed successfully and completely without a match.

Matches require accession-specific queries and verified JSON evidence. Free-text rankings and similar names are not used to infer matches. No species restriction is applied during API lookup, because doing so could hide secondary mappings across species. Filtering happens afterward through exact integer taxonomy-ID comparison.

The selected endpoint uses synchronous pagination and requires no asynchronous job polling. The official maximum page size is 500. The tool queries 50 accessions per batch by default and allows 1–100; this is a conservative tool limit on GET URL length, not an official limit on the total number of accessions. The default 0.25-second request interval is a tool policy, not a guaranteed official rate quota.

Each query checks `X-Total-Results`, duplicate entries, repeated pagination links, and release consistency. Missing result counts or incomplete pagination produce `api_error`, not a definitive unmapped result. `X-UniProt-Release` and `X-UniProt-Release-Date` are recorded as returned when available. Missing release information remains blank rather than being inferred.

## Supported headers and mapping statuses

Supported header formats include `>P12345`, `>sp|P12345|PROTEIN_HUMAN ...`, and `>tr|A0A024RBG1|ENTRY ...`. `original_id` is Biopython's `record.id`: the ID before the first whitespace in the header. Full headers remain in the original and filtered FASTA files. `record_index` starts at 1 and distinguishes repeated IDs.

Accessions are validated against the official 6- or 10-character syntax. The pipe-delimited format of `tr|A0A1234567|...` can be parsed, but the illustrative accession `A0A1234567` does not follow the official 10-character pattern. It is retained as `invalid_id` and is not sent to the API. `A0A024RBG1` is a valid 10-character format example. The tool does not strip isoform suffixes such as `-2`, strip version suffixes such as `.1`, or infer accessions from entry names such as `PROTEIN_HUMAN`.

| mapping_status | Meaning | Eligible for species filtering |
|---|---|---|
| `primary_match` | Exact match to a current primary accession | Yes |
| `secondary_match` | Exact secondary mapping to one current entry | Yes; the original accession and mapping relationship are retained |
| `ambiguous` | A secondary accession corresponds to multiple current entries | No |
| `unmapped` | No exact match after complete queries against active UniProtKB | No |
| `invalid_id` | Unsupported header ID or invalid accession syntax | No |
| `api_error` | HTTP, network, response-content, or pagination validation failure | No; queried again on the next run |
| `interrupted` | Query not completed before interruption | No; queried again on the next run |

For ambiguous, unmapped, and error records, `taxonomy_id`, `organism_name`, and `matched_accession` remain blank. All ambiguity candidates are retained in `candidate_matches` JSON. The tool neither selects the first candidate nor accepts a unique human match merely because one of several candidates is human.

## Output files

For `--output results/taxonomy_mapping.tsv`, the default files are:

| File | Contents |
|---|---|
| `taxonomy_mapping.tsv` | All input records in the original FASTA order |
| `taxonomy_mapping.unmatched.tsv` | All unresolved records: unmapped, ambiguous, invalid_id, api_error, and interrupted |
| `taxonomy_mapping.report.json` | Status counts for records and unique accessions, request/retry counts, cache hits, releases, input SHA-256, query times, and parameters |
| `taxonomy_mapping.cache.sqlite3` | Incremental cache; use `--cache` to specify a shared path |

The first seven TSV columns are `original_id`, `accession`, `taxonomy_id`, `organism_name`, `matched_accession`, `mapping_status`, and `mapping_source`.

Additional columns:

- `record_index`: original FASTA record number.
- `query_date_utc`: actual lookup time as a timezone-aware ISO 8601 UTC timestamp. Cache reads preserve it; locally invalid IDs have no query date.
- `uniprot_release`, `uniprot_release_date`: release metadata returned by the API.
- `taxonomy_context`: `current_uniprotkb`, meaning the active database at the time of lookup.
- `candidate_matches`: a JSON array of candidate current accessions, taxonomy IDs, and scientific names.
- `error`: a specific explanation of an invalid ID, ambiguity, or request failure.
- `target_taxonomy_id`: the requested `--taxon`, or blank when absent.
- `taxon_filter_status`: `target_taxon`, `other_taxon`, `unresolved`, or `not_requested`. Uncertain records remain `unresolved` rather than being labeled as other species.

TSV files use UTF-8 and standard CSV quoting. For programmatic reading, use `csv.DictReader(..., delimiter="\t")`.

Current database taxonomy does not establish the taxonomy assigned when a historical dataset was created. `secondary_match` establishes an accession relationship; it does not prove that the historical FASTA sequence is identical to the current entry sequence. Historical assignments require separate checks against original dataset metadata or historical database releases. This tool does not infer those assignments or change dataset labels.

## Options and recovery

| Option | Default or constraint |
|---|---|
| `--input`, `--output` | Required |
| `--cache` | Output stem followed by `.cache.sqlite3` |
| `--batch-size` | 50; range 1–100 |
| `--timeout` | 30 seconds per requests connection/read timeout; not a deadline for the entire task |
| `--retries` | Up to 3 retries after the initial attempt |
| `--taxon` | No species restriction by default; must be a positive integer when supplied |
| `--filtered-fasta` | Optional; requires `--taxon` |
| `--request-interval` | 0.25 seconds; non-negative |
| `--page-size` | 500; range 1–500; use 1 to exercise pagination |
| `--cache-max-age-days` | 30 days; use 0 to requery |
| `--refresh-cache` | Force fresh queries while continuing incremental cache commits |
| `--unmatched-output`, `--report` | Override the default unresolved TSV and JSON report paths |
| `--verbose`, `--version` | Debug logging or tool version |

Network failures and HTTP 408, 429, 500, 502, 503, and 504 receive bounded retries. Request errors such as HTTP 400 are recorded directly. Retry delays use exponential backoff with small random jitter and wait at least as long as `Retry-After`, whether expressed as seconds or an HTTP date. TLS certificate verification remains enabled.

The cache uses SQLite WAL, full synchronization, and transactions. Completed primary and secondary batches are committed separately. Committed results survive interruption; unfinished batches may be requested again. Query errors can be stored for auditing but are never reused as cache hits. A corrupt cache row triggers a warning and requery. A corrupt database produces an explicit error and is not automatically deleted or overwritten.

Rerun the same command to resume. Cache entries retain their original query dates and releases. If results combine multiple releases, the log and report flag this explicitly. Use `--refresh-cache` to obtain a newer database snapshot. The active database can change between dates, so retain complete mapping tables, reports, and caches for auditing.

Each output file is written through a temporary file and an atomic replacement. The complete mapping table and unresolved records are retained. Absence from a filtered FASTA is not sufficient evidence that a record belongs to another species.

Exit codes: `0` means the query workflow completed, potentially with ambiguous, unmapped, or invalid IDs; `1` indicates input, file, cache, or similar errors; `2` indicates API errors or invalid command-line arguments; `130` indicates interruption. Ctrl+C during mapping attempts to write completed results and mark pending records as `interrupted`. After forced process termination, recovery relies on committed cache entries.

## Tests and small live validation

Tests are in `tests/`, while the script is in `重写/`. Before running tests from the project root, add the script directory to the current PowerShell session's module search path. This preserves the existing `PYTHONPATH` and does not change system settings or virtual environment files:

```powershell
$env:PYTHONPATH = (Resolve-Path './重写').Path + [IO.Path]::PathSeparator + $env:PYTHONPATH
```

Run the offline regression suite without network access:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_fasta_taxonomy_mapper.py' -v
```

Coverage includes ID parsing, duplicates and order, cache round trips and expiry, requerying cached errors, secondary evidence and ambiguity, HTTP 429/timeouts, pagination completeness, Ctrl+C, TSV/report output, exact species filtering, and preservation of original FASTA bytes.

Run the explicit live check, which queries only the 7 unique valid accessions in `examples/taxonomy_smoke.fasta`:

```powershell
.\.venv\Scripts\python.exe -B tests/live_smoke_test.py
```

The check forces one result per page and verifies all 9 original records, 4 human records, secondary mapping, ambiguity across species, and byte-for-byte preservation of selected records. A second run must make zero HTTP requests. Outputs are saved in `validation/taxonomy_smoke/`: complete and unresolved TSV files, human FASTA, reports from both runs, the cache, and `verification.json`.

Live-test reference expectations were checked against UniProt `2026_03`. Future database changes may alter mappings and require reviewing those expectations. Example sequences are truncated test strings intended for checking ID mapping and file preservation, not biological analysis.

Baseline validation on **2026-10-18** passed all 29 offline tests. The first live run made 9 requests and the repeated run made 0; `verification.json` recorded `passed`. The 9 records comprised 4 primary matches, 1 secondary match, 2 ambiguous records, 1 unmapped record, and 1 invalid ID. Exact filtering selected 4 human records. Invocation from another working directory and complete mapping without `--taxon` were also verified with exit code 0.
