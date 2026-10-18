#!/usr/bin/env python3
"""Map FASTA UniProt accessions to current UniProtKB organism taxonomy.

Only exact primary/secondary accession evidence is accepted. No free-text,
sequence-similarity, organism-lineage, or historical-taxonomy inference is used.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urlparse

import requests
from Bio import SeqIO

VERSION = "1.0.0"
API_URL = "https://rest.uniprot.org/uniprotkb/search"
SOURCE_PRIMARY = "UniProtKB REST: exact accession + primaryAccession verification"
SOURCE_SECONDARY = "UniProtKB REST: exact sec_acc + secondaryAccessions verification"
# Official 6/10-character UniProtKB accession syntax; isoform suffixes are NOT removed.
ACCESSION_RE = re.compile(r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\Z")
CONFIRMED = frozenset({"primary_match", "secondary_match"})
CACHEABLE = CONFIRMED | {"unmapped", "ambiguous"}
STATUSES = CACHEABLE | {"invalid_id", "api_error", "interrupted"}
LOG = logging.getLogger("fasta_taxonomy_mapper")


class MapperError(Exception):
    """Input, API protocol, or persistent-storage error."""


class APIError(MapperError):
    """A query could not be completed and must not be treated as unmapped."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FastaRecord:
    record_index: int
    original_id: str
    accession: str
    parse_error: str = ""


@dataclass(frozen=True)
class Candidate:
    matched_accession: str
    taxonomy_id: int
    organism_name: str


@dataclass(frozen=True)
class QueryMetadata:
    query_date_utc: str
    uniprot_release: str = ""
    uniprot_release_date: str = ""


@dataclass(frozen=True)
class MappingResult:
    accession: str
    mapping_status: str
    mapping_source: str
    query_date_utc: str
    uniprot_release: str = ""
    uniprot_release_date: str = ""
    taxonomy_id: int | None = None
    organism_name: str = ""
    matched_accession: str = ""
    candidates: tuple[Candidate, ...] = ()
    error: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MappingResult:
        """Validate cached data, including invariants used by FASTA filtering."""
        raw = dict(data)
        raw["candidates"] = tuple(Candidate(**item) for item in raw["candidates"])
        result = cls(**raw)
        if not ACCESSION_RE.fullmatch(result.accession) or result.mapping_status not in STATUSES:
            raise ValueError("Invalid cached accession/status")
        if datetime.fromisoformat(result.query_date_utc).utcoffset() is None:
            raise ValueError("Cache timestamp must include a timezone")
        for candidate in result.candidates:
            if (not ACCESSION_RE.fullmatch(candidate.matched_accession)
                    or type(candidate.taxonomy_id) is not int or candidate.taxonomy_id <= 0
                    or not isinstance(candidate.organism_name, str) or not candidate.organism_name.strip()):
                raise ValueError("Invalid cached candidate")
        if result.mapping_status in CONFIRMED:
            expected = Candidate(result.matched_accession, result.taxonomy_id, result.organism_name)
            if len(result.candidates) != 1 or result.candidates[0] != expected:
                raise ValueError("Confirmed cache entry lacks unique taxonomy evidence")
            if (result.mapping_status == "primary_match") != (result.accession == result.matched_accession):
                raise ValueError("Inconsistent primary/secondary cache entry")
        elif result.taxonomy_id is not None or result.matched_accession or result.organism_name:
            raise ValueError("Unresolved cache entry contains definitive mapping fields")
        if result.mapping_status == "ambiguous" and len(result.candidates) < 2:
            raise ValueError("Ambiguous cache entry lacks multiple candidates")
        if result.mapping_status == "unmapped" and result.candidates:
            raise ValueError("Unmapped cache entry contains candidates")
        return result


def parse_accession(original_id: str) -> tuple[str, str]:
    """Return normalized accession and an explicit error for unsupported IDs."""
    token = original_id.strip()
    if "|" in token:
        parts = token.split("|")
        if len(parts) != 3 or parts[0].lower() not in {"sp", "tr"} or not parts[2]:
            return token, "Unsupported pipe-delimited FASTA ID; expected sp|ACCESSION|NAME or tr|ACCESSION|NAME"
        token = parts[1]
    accession = token.upper()
    if not ACCESSION_RE.fullmatch(accession):
        return accession, "Not a canonical UniProt accession (entry names, versions and isoform suffixes are not inferred)"
    return accession, ""


def iter_fasta_blocks(path: Path) -> Iterator[bytes]:
    """Stream original FASTA records without changing headers, sequences or wrapping."""
    block: list[bytes] = []
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, 1):
            check = line.removeprefix(b"\xef\xbb\xbf") if line_number == 1 else line
            if check.startswith(b">"):
                if block:
                    yield b"".join(block)
                block = [line]
            elif block:
                block.append(line)
            elif check.strip():
                raise MapperError(f"{path}:{line_number}: content before the first FASTA header")
        if block:
            yield b"".join(block)


def read_fasta(path: Path) -> list[FastaRecord]:
    records: list[FastaRecord] = []
    for index, raw in enumerate(iter_fasta_blocks(path), 1):
        try:
            text = raw.decode("utf-8-sig")
            if not text.splitlines()[0][1:].strip():
                raise ValueError("Empty FASTA header")
            record = SeqIO.read(io.StringIO(text), "fasta")
        except (ValueError, UnicodeError) as exc:
            raise MapperError(f"{path}: FASTA record {index}: {exc}") from exc
        accession, error = parse_accession(record.id)
        records.append(FastaRecord(index, record.id, accession, error))
    if not records:
        raise MapperError(f"No FASTA records found in {path}")
    return records


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class MappingCache:
    """SQLite transactions preserve completed batches across crashes and Ctrl+C."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        try:
            tables = {row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables and tables != {"mapper_metadata", "mapping_results"}:
                raise MapperError(f"Refusing unrelated SQLite database as mapper cache: {path}")
            if tables:
                schema = self.connection.execute(
                    "SELECT value FROM mapper_metadata WHERE key='schema_version'"
                ).fetchone()
                if schema != ("1",):
                    raise MapperError("Unsupported cache schema; choose a new --cache path")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.connection:
                self.connection.execute("CREATE TABLE IF NOT EXISTS mapper_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self.connection.execute("INSERT OR IGNORE INTO mapper_metadata VALUES ('schema_version', '1')")
                self.connection.execute("CREATE TABLE IF NOT EXISTS mapping_results (accession TEXT PRIMARY KEY, result_json TEXT NOT NULL)")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> MappingCache:
        return self

    def __exit__(self, *args: Any) -> None:
        self.connection.close()

    def get(self, accession: str, max_age_days: float) -> MappingResult | None:
        row = self.connection.execute("SELECT result_json FROM mapping_results WHERE accession=?", (accession,)).fetchone()
        if row is None:
            return None
        try:
            result = MappingResult.from_json(json.loads(row[0]))
            if result.accession != accession:
                raise ValueError("Cache key and stored accession differ")
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            LOG.warning("Ignoring corrupt cache row for %s: %s", accession, exc)
            return None
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(result.query_date_utc)).total_seconds() / 86400
        if result.mapping_status not in CACHEABLE or age_days < 0 or age_days > max_age_days:
            return None
        return result

    def put_many(self, results: Sequence[MappingResult]) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT INTO mapping_results VALUES (?, ?) ON CONFLICT(accession) DO UPDATE SET result_json=excluded.result_json",
                [(item.accession, json.dumps(asdict(item), ensure_ascii=False)) for item in results],
            )


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(value)))
    except (ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(value)
            if date.utcoffset() is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def parse_api_entries(
    entries: Sequence[dict[str, Any]], requested: Sequence[str], secondary: bool,
) -> dict[str, tuple[Candidate, ...]]:
    """Reject unverified hits; preserve one-to-many secondary mappings as ambiguity."""
    requested_set = set(requested)
    matches: dict[str, dict[str, Candidate]] = {accession: {} for accession in requested}
    for entry in entries:
        if not isinstance(entry, dict):
            raise APIError("API results contain a non-object entry")
        primary = entry.get("primaryAccession")
        if not isinstance(primary, str) or not ACCESSION_RE.fullmatch(primary) or entry.get("inactiveReason"):
            raise APIError("API returned a missing/invalid primary accession or inactive entry")
        if secondary:
            accessions = entry.get("secondaryAccessions", [])
            if not isinstance(accessions, list) or not all(isinstance(item, str) for item in accessions):
                raise APIError("API returned invalid secondaryAccessions")
            verified = requested_set.intersection(accessions)
        else:
            verified = {primary} & requested_set
        if not verified:
            raise APIError("API hit lacks exact requested accession evidence; refusing to infer a match")
        organism = entry.get("organism")
        if not isinstance(organism, dict):
            raise APIError(f"Missing organism for {primary}")
        taxonomy_id = organism.get("taxonId")
        name = organism.get("scientificName")
        if type(taxonomy_id) is not int or taxonomy_id <= 0 or not isinstance(name, str) or not name.strip():
            raise APIError(f"Missing/invalid taxonomy ID or scientific name for {primary}")
        candidate = Candidate(primary, taxonomy_id, name)
        for accession in verified:
            old = matches[accession].get(primary)
            if old is not None and old != candidate:
                raise APIError(f"Inconsistent duplicate API entry for {primary}")
            matches[accession][primary] = candidate
    return {key: tuple(sorted(value.values(), key=lambda item: item.matched_accession)) for key, value in matches.items()}


def result_from_candidates(
    accession: str, candidates: tuple[Candidate, ...], metadata: QueryMetadata, secondary: bool,
) -> MappingResult:
    source = SOURCE_SECONDARY if secondary else SOURCE_PRIMARY
    shared = dict(accession=accession, mapping_source=source, **asdict(metadata), candidates=candidates)
    if not candidates:
        return MappingResult(mapping_status="unmapped", **shared)
    if len(candidates) > 1:
        return MappingResult(mapping_status="ambiguous", error="Multiple current entries; no unique accession mapping", **shared)
    candidate = candidates[0]
    return MappingResult(mapping_status="secondary_match" if secondary else "primary_match", **asdict(candidate), **shared)


class UniProtClient:
    def __init__(
        self, timeout: float = 30, retries: int = 3, request_interval: float = 0.25,
        page_size: int = 500, session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.request_interval = request_interval
        self.page_size = page_size
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": f"fasta-taxonomy-mapper/{VERSION}", "Accept": "application/json"})
        self.sleep = sleep
        self.last_request = 0.0
        self.request_count = 0
        self.retry_count = 0

    def close(self) -> None:
        self.session.close()

    def _get(self, url: str, params: dict[str, Any] | None) -> requests.Response:
        for attempt in range(self.retries + 1):
            wait = self.request_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                self.sleep(wait)
            self.last_request = time.monotonic()
            self.request_count += 1
            response = None
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 200:
                    return response
                message = f"HTTP {response.status_code}: {response.text[:400]}"
                retryable = response.status_code in {408, 429, 500, 502, 503, 504}
                delay = retry_after_seconds(response.headers.get("Retry-After"))
            except requests.RequestException as exc:
                message = f"{type(exc).__name__}: {exc}"
                retryable, delay = True, 0.0
            finally:
                if response is not None and response.status_code != 200:
                    response.close()
            if not retryable or attempt == self.retries:
                raise APIError(f"UniProt request failed after {attempt + 1} attempt(s): {message}")
            self.retry_count += 1
            delay = max(delay, min(60.0, 2.0 ** min(attempt, 6)) + random.uniform(0, 0.25))
            LOG.warning("%s; retry %s/%s in %.2fs", message, attempt + 1, self.retries, delay)
            self.sleep(delay)
        raise AssertionError("unreachable")

    def query(self, accessions: Sequence[str], secondary: bool = False) -> tuple[dict[str, tuple[Candidate, ...]], QueryMetadata]:
        field = "sec_acc" if secondary else "accession"
        if not accessions or any(not ACCESSION_RE.fullmatch(item) for item in accessions):
            raise ValueError("query() requires valid canonical UniProt accessions")
        query = "active:true AND (" + " OR ".join(f"{field}:{item}" for item in accessions) + ")"
        params: dict[str, Any] | None = {"query": query, "format": "json", "size": self.page_size}
        if not secondary:
            params["fields"] = "accession,organism_id,organism_name"
        # sec_acc is a QUERY field, not a supported RETURN field. Full JSON is
        # necessary here to inspect secondaryAccessions; other annotations are discarded.
        url: str | None = API_URL
        seen_urls: set[str] = set()
        metadata: QueryMetadata | None = None
        expected_total: int | None = None
        entries: list[dict[str, Any]] = []
        seen_accessions: set[str] = set()
        while url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "rest.uniprot.org" or parsed.path != "/uniprotkb/search":
                raise APIError("Unexpected API pagination URL")
            if url in seen_urls:
                raise APIError("Repeated pagination URL")
            seen_urls.add(url)
            response = self._get(url, params)
            try:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise APIError("UniProt returned invalid JSON") from exc
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    raise APIError("UniProt response lacks a results array")
                release = response.headers.get("X-UniProt-Release", "")
                release_date = response.headers.get("X-UniProt-Release-Date", "")
                if metadata is None:
                    metadata = QueryMetadata(utc_now(), release, release_date)
                elif (release, release_date) != (metadata.uniprot_release, metadata.uniprot_release_date):
                    raise APIError("UniProt release changed during pagination; rerun the query")
                total_header = response.headers.get("X-Total-Results")
                if total_header is not None:
                    try:
                        total = int(total_header)
                    except ValueError as exc:
                        raise APIError("Invalid X-Total-Results") from exc
                    if total < 0 or (expected_total is not None and total != expected_total):
                        raise APIError("Inconsistent X-Total-Results across pages")
                    expected_total = total
                for entry in payload["results"]:
                    primary = entry.get("primaryAccession") if isinstance(entry, dict) else None
                    if isinstance(primary, str):
                        if primary in seen_accessions:
                            raise APIError("Duplicate entry across API results; pagination completeness is unverified")
                        seen_accessions.add(primary)
                entries.extend(payload["results"])
                if expected_total is not None and len(entries) > expected_total:
                    raise APIError("API returned more entries than X-Total-Results")
                url = response.links.get("next", {}).get("url")
                if response.headers.get("Link") and "rel=\"next\"" in response.headers["Link"] and not url:
                    raise APIError("Malformed next-page Link header")
            finally:
                response.close()
            params = None  # Cursor links already contain every query parameter.
        if expected_total is None:
            raise APIError("Missing X-Total-Results; pagination completeness is unverified")
        if len(entries) != expected_total:
            raise APIError(f"Incomplete pagination: expected {expected_total} entries, received {len(entries)}")
        assert metadata is not None
        return parse_api_entries(entries, accessions, secondary), metadata


def map_accessions(
    accessions: Sequence[str], cache: MappingCache, client: UniProtClient, batch_size: int,
    max_age_days: float = 30, refresh: bool = False,
) -> tuple[dict[str, MappingResult], int, bool]:
    results: dict[str, MappingResult] = {}
    pending: list[str] = []
    cache_hits = 0
    interrupted = False
    try:
        for accession in dict.fromkeys(accessions):
            cached = None if refresh else cache.get(accession, max_age_days)
            if cached is None:
                pending.append(accession)
            else:
                results[accession] = cached
                cache_hits += 1
        LOG.info("%d unique valid accessions: %d cache hits, %d to query", len(results) + len(pending), cache_hits, len(pending))
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            unresolved = batch
            try:
                matches, primary_metadata = client.query(batch)
                primary_results = [result_from_candidates(key, matches[key], primary_metadata, False) for key in batch if matches[key]]
                cache.put_many(primary_results)
                results.update((item.accession, item) for item in primary_results)
                unresolved = [key for key in batch if not matches[key]]
                if unresolved:
                    secondary_matches, metadata = client.query(unresolved, secondary=True)
                    if (metadata.uniprot_release, metadata.uniprot_release_date) != (primary_metadata.uniprot_release, primary_metadata.uniprot_release_date):
                        raise APIError("UniProt release changed between primary and secondary lookup")
                    secondary_results = [result_from_candidates(key, secondary_matches[key], metadata, True) for key in unresolved]
                    # An unmapped result means BOTH exact queries completed successfully.
                    secondary_results = [
                        replace(item, mapping_source=SOURCE_PRIMARY + "; " + SOURCE_SECONDARY)
                        if item.mapping_status == "unmapped" else item for item in secondary_results
                    ]
                    cache.put_many(secondary_results)
                    results.update((item.accession, item) for item in secondary_results)
            except APIError as exc:
                LOG.error("Batch beginning at %s: %s", batch[0], exc)
                failures = [MappingResult(key, "api_error", "UniProtKB REST: incomplete query", utc_now(), error=str(exc)) for key in unresolved if key not in results]
                cache.put_many(failures)  # Errors are saved for audit but never reused.
                results.update((item.accession, item) for item in failures)
            LOG.info("Completed batch %d/%d", start // batch_size + 1, math.ceil(len(pending) / batch_size))
    except KeyboardInterrupt:
        interrupted = True
        LOG.warning("Interrupted; saving completed results and marking unfinished accessions")
    for accession in accessions:
        if accession not in results:
            results[accession] = MappingResult(accession, "interrupted", "not_completed", utc_now(), error="Run interrupted before a complete query")
    return results, cache_hits, interrupted


@contextmanager
def atomic_writer(path: Path, binary: bool = False) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    options = {} if binary else {"encoding": "utf-8", "newline": ""}
    handle = tempfile.NamedTemporaryFile(mode="wb" if binary else "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False, **options)
    temporary = Path(handle.name)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_result(record: FastaRecord, results: dict[str, MappingResult]) -> MappingResult:
    if record.parse_error:
        return MappingResult(record.accession, "invalid_id", "local_id_validation", "", error=record.parse_error)
    return results[record.accession]


def is_target(result: MappingResult, taxon: int) -> bool:
    return result.mapping_status in CONFIRMED and result.taxonomy_id == taxon


TSV_FIELDS = [
    "original_id", "accession", "taxonomy_id", "organism_name", "matched_accession",
    "mapping_status", "mapping_source", "record_index", "query_date_utc",
    "uniprot_release", "uniprot_release_date", "taxonomy_context", "candidate_matches", "error",
    "target_taxonomy_id", "taxon_filter_status",
]


def write_tables(
    records: Sequence[FastaRecord], results: dict[str, MappingResult], output: Path,
    unmatched: Path, taxon: int | None = None,
) -> None:
    with atomic_writer(output) as all_handle, atomic_writer(unmatched) as unresolved_handle:
        writers = [csv.DictWriter(handle, fieldnames=TSV_FIELDS, delimiter="\t", lineterminator="\n") for handle in (all_handle, unresolved_handle)]
        for writer in writers:
            writer.writeheader()
        for record in records:
            result = record_result(record, results)
            row = asdict(result)
            row["candidate_matches"] = json.dumps(row.pop("candidates"), ensure_ascii=False, separators=(",", ":"))
            row.update(original_id=record.original_id, record_index=record.record_index, taxonomy_context="current_uniprotkb")
            row["target_taxonomy_id"] = taxon
            row["taxon_filter_status"] = (
                "not_requested" if taxon is None else
                "unresolved" if result.mapping_status not in CONFIRMED else
                "target_taxon" if is_target(result, taxon) else "other_taxon"
            )
            writers[0].writerow(row)
            if result.mapping_status not in CONFIRMED:
                writers[1].writerow(row)


def write_filtered_fasta(
    input_path: Path, output: Path, records: Sequence[FastaRecord], results: dict[str, MappingResult], taxon: int,
) -> int:
    selected = 0
    seen = 0
    with atomic_writer(output, binary=True) as handle:
        for index, block in enumerate(iter_fasta_blocks(input_path)):
            if index >= len(records):
                raise MapperError("Input FASTA changed while running")
            original_id = SeqIO.read(io.StringIO(block.decode("utf-8-sig")), "fasta").id
            if original_id != records[index].original_id:
                raise MapperError("Input FASTA record order/IDs changed while running")
            if is_target(record_result(records[index], results), taxon):
                handle.write(block)
                selected += 1
            seen += 1
        if seen != len(records):
            raise MapperError("Input FASTA record count changed while running")
    return selected


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return number


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Input UTF-8 FASTA")
    parser.add_argument("--output", required=True, type=Path, help="Complete mapping TSV (all input records)")
    parser.add_argument("--cache", type=Path, help="SQLite cache; default: OUTPUT stem + .cache.sqlite3")
    parser.add_argument("--batch-size", type=int, default=50, help="Accessions per query; 1..100 (tool URL-length safeguard)")
    parser.add_argument("--timeout", type=positive_float, default=30, help="Per-request connect/read timeout, seconds")
    parser.add_argument("--retries", type=int, default=3, help="Retries after the initial request")
    parser.add_argument("--taxon", type=int, help="Exact NCBI taxonomy ID; no descendant matching")
    parser.add_argument("--filtered-fasta", type=Path, help="Target-species FASTA; requires --taxon")
    parser.add_argument("--unmatched-output", type=Path, help="All unresolved rows; default: OUTPUT stem + .unmatched.tsv")
    parser.add_argument("--report", type=Path, help="JSON statistics; default: OUTPUT stem + .report.json")
    parser.add_argument("--request-interval", type=nonnegative_float, default=0.25, help="Minimum seconds between request starts")
    parser.add_argument("--page-size", type=int, default=500, help="UniProt page size, 1..500")
    parser.add_argument("--cache-max-age-days", type=nonnegative_float, default=30, help="Reuse successful cached lookups for this many days")
    parser.add_argument("--refresh-cache", action="store_true", help="Requery all accessions; refresh incrementally")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 100 or not 1 <= args.page_size <= 500:
        parser.error("--batch-size must be 1..100; --page-size must be 1..500")
    if args.retries < 0 or (args.taxon is not None and args.taxon <= 0):
        parser.error("--retries must be non-negative; --taxon must be positive")
    if args.filtered_fasta and args.taxon is None:
        parser.error("--filtered-fasta requires --taxon")
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    args.cache = (args.cache or args.output.with_suffix(".cache.sqlite3")).expanduser().resolve()
    args.unmatched_output = (args.unmatched_output or args.output.with_suffix(".unmatched.tsv")).expanduser().resolve()
    args.report = (args.report or args.output.with_suffix(".report.json")).expanduser().resolve()
    if args.filtered_fasta:
        args.filtered_fasta = args.filtered_fasta.expanduser().resolve()
    paths = [args.input, args.output, args.cache, args.unmatched_output, args.report]
    if args.filtered_fasta:
        paths.append(args.filtered_fasta)
    if len(set(paths)) != len(paths):
        parser.error("Input, cache and every output path must be distinct")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = utc_now()
    client = UniProtClient(args.timeout, args.retries, args.request_interval, args.page_size)
    try:
        input_hash = file_sha256(args.input)
        records = read_fasta(args.input)
        unique_accessions = list(dict.fromkeys(record.accession for record in records if not record.parse_error))
        with MappingCache(args.cache) as cache:
            results, cache_hits, interrupted = map_accessions(unique_accessions, cache, client, args.batch_size, args.cache_max_age_days, args.refresh_cache)
        if file_sha256(args.input) != input_hash:
            raise MapperError("Input FASTA changed during mapping; cached queries retained, outputs not written")
        write_tables(records, results, args.output, args.unmatched_output, args.taxon)
        selected = sum(is_target(record_result(record, results), args.taxon) for record in records) if args.taxon else None
        if args.filtered_fasta:
            selected = write_filtered_fasta(args.input, args.filtered_fasta, records, results, args.taxon)
        record_counts = Counter(record_result(record, results).mapping_status for record in records)
        unique_counts = Counter(item.mapping_status for item in results.values())
        versions = sorted({item.uniprot_release for item in results.values() if item.uniprot_release})
        if len(versions) > 1:
            LOG.warning("Output combines cached/API results from multiple UniProt releases: %s; use --refresh-cache for a new snapshot", versions)
        report = {
            "tool_version": VERSION, "started_at_utc": started, "finished_at_utc": utc_now(),
            "input": str(args.input), "input_sha256": input_hash,
            "output": str(args.output), "unmatched_output": str(args.unmatched_output),
            "cache": str(args.cache), "filtered_fasta": str(args.filtered_fasta) if args.filtered_fasta else None,
            "total_records": len(records), "unique_original_ids": len({item.original_id for item in records}),
            "unique_valid_accessions": len(unique_accessions), "status_counts_records": dict(record_counts),
            "status_counts_unique_valid_accessions": dict(unique_counts), "cache_hits_unique_accessions": cache_hits,
            "http_requests_including_retries": client.request_count, "http_retries": client.retry_count,
            "target_taxonomy_id": args.taxon, "target_records": selected,
            "confirmed_other_taxa_records": sum(record_result(record, results).mapping_status in CONFIRMED and record_result(record, results).taxonomy_id != args.taxon for record in records) if args.taxon else None,
            "unresolved_records": sum(count for status, count in record_counts.items() if status not in CONFIRMED),
            "uniprot_releases": versions, "mixed_uniprot_releases": len(versions) > 1,
            "taxonomy_context": "current_uniprotkb",
            "historical_taxonomy": "Not inferred. Current database taxonomy does not establish taxonomy at dataset creation.",
            "query_dates_utc": sorted({item.query_date_utc for item in results.values() if item.query_date_utc}),
            "interrupted": interrupted,
            "parameters": {key: getattr(args, key) for key in ("batch_size", "timeout", "retries", "request_interval", "page_size", "cache_max_age_days", "refresh_cache")},
        }
        with atomic_writer(args.report) as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        LOG.info("Wrote %d mapping rows; statuses=%s; target=%s; unresolved=%d", len(records), dict(record_counts), selected, report["unresolved_records"])
        if interrupted:
            return 130
        return 2 if record_counts["api_error"] else 0
    except KeyboardInterrupt:
        LOG.error("Interrupted outside mapping; committed cache remains reusable")
        return 130
    except (MapperError, OSError, sqlite3.Error, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
