#!/usr/bin/env python3
"""Classify OCR'd legal documents through the FOLIO Document Types hierarchy."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FOLIO_OWL_URL = "https://raw.githubusercontent.com/alea-institute/FOLIO/main/FOLIO.owl"
FOLIO_DOCUMENT_TYPES_IRI = "https://folio.openlegalstandard.org/RBkL8I5saFF7mqpLTI7GxSh"
FOLIO_AGREEMENTS_IRI = "https://folio.openlegalstandard.org/R88D8i8AcSTUig2X3yPbFHg"
DEFAULT_FOLIO_CACHE_PATH = Path(__file__).with_name(".cache").joinpath("FOLIO.owl")
DEFAULT_FOLIO_TREE_CACHE_PATH = Path(__file__).with_name(".cache").joinpath("agreements-tree.json")
TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
MAX_CHOICE_OPTIONS = 255
EPSILON = 1e-12
DEFAULT_DOCUMENT_MAX_TOKENS = 2000

RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
RDF_RESOURCE = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource"
RDFS_SUBCLASS = "{http://www.w3.org/2000/01/rdf-schema#}subClassOf"
RDFS_LABEL = "{http://www.w3.org/2000/01/rdf-schema#}label"
SKOS_DEFINITION = "{http://www.w3.org/2004/02/skos/core#}definition"
SKOS_PREF_LABEL = "{http://www.w3.org/2004/02/skos/core#}prefLabel"


@dataclass(frozen=True)
class FolioNode:
    iri: str
    label: str
    definition: str


@dataclass(frozen=True)
class Candidate:
    node_iri: str
    path: tuple[str, ...]
    log_probability: float
    decisions: int

    @property
    def score(self) -> float:
        if not self.decisions:
            return 1.0
        return math.exp(self.log_probability / self.decisions)


class TypeSafeApiError(RuntimeError):
    """Raised when the TypeSafe API rejects a request."""


class TypeSafeClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: int = 120,
        input_cost_per_1k: float | None = None,
        output_cost_per_1k: float | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self.request_metrics: list[dict[str, Any]] = []

    def evaluate(self, state: dict[str, str], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        payload = json.dumps(
            {"state": state, "model": self.model, "questions": questions}
        ).encode("utf-8")
        request = urllib.request.Request(
            TYPESAFE_API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started_at = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
                latency_ms = (time.perf_counter() - started_at) * 1000
                usage = result.get("usage", {})
                self.request_metrics.append(
                    {
                        "latency_ms": round(latency_ms, 2),
                        "status": response.status,
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                    }
                )
                return result
        except urllib.error.HTTPError as error:
            latency_ms = (time.perf_counter() - started_at) * 1000
            detail = error.read().decode("utf-8", errors="replace")
            self.request_metrics.append(
                {"latency_ms": round(latency_ms, 2), "status": error.code, "error": True}
            )
            raise TypeSafeApiError(f"TypeSafe API returned HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            latency_ms = (time.perf_counter() - started_at) * 1000
            self.request_metrics.append(
                {"latency_ms": round(latency_ms, 2), "status": None, "error": True}
            )
            raise TypeSafeApiError(f"Could not reach TypeSafe API: {error.reason}") from error

    def metadata(self) -> dict[str, Any]:
        successful = [metric for metric in self.request_metrics if not metric.get("error")]
        input_tokens = sum(metric.get("input_tokens") or 0 for metric in successful)
        output_tokens = sum(metric.get("output_tokens") or 0 for metric in successful)
        metadata: dict[str, Any] = {
            "request_count": len(self.request_metrics),
            "successful_request_count": len(successful),
            "total_latency_ms": round(sum(metric["latency_ms"] for metric in self.request_metrics), 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "requests": self.request_metrics,
        }
        if self.input_cost_per_1k is not None and self.output_cost_per_1k is not None:
            metadata["estimated_cost_usd"] = round(
                input_tokens / 1000 * self.input_cost_per_1k
                + output_tokens / 1000 * self.output_cost_per_1k,
                6,
            )
        else:
            metadata["estimated_cost_usd"] = None
            metadata["cost_note"] = (
                "Provide input and output cost rates per 1,000 tokens to calculate cost."
            )
        return metadata


class FolioHierarchy:
    def __init__(self, nodes: dict[str, FolioNode], children: dict[str, tuple[str, ...]]) -> None:
        self.nodes = nodes
        self.children = children

    @classmethod
    def from_owl(cls, content: bytes) -> "FolioHierarchy":
        root = ET.fromstring(content)
        nodes: dict[str, FolioNode] = {}
        children: dict[str, list[str]] = {}

        for element in root.findall(".//{http://www.w3.org/2002/07/owl#}Class"):
            iri = element.get(RDF_ABOUT)
            if not iri:
                continue
            label = element.findtext(RDFS_LABEL) or element.findtext(SKOS_PREF_LABEL) or iri
            definition = element.findtext(SKOS_DEFINITION) or "No definition supplied by FOLIO."
            nodes[iri] = FolioNode(iri=iri, label=label, definition=definition)
            for parent in element.findall(RDFS_SUBCLASS):
                parent_iri = parent.get(RDF_RESOURCE)
                if parent_iri:
                    children.setdefault(parent_iri, []).append(iri)

        return cls(nodes, {iri: tuple(values) for iri, values in children.items()})

    @classmethod
    def from_tree_cache(cls, content: bytes) -> "FolioHierarchy":
        payload = json.loads(content)
        nodes = {
            iri: FolioNode(
                iri=record["iri"],
                label=record["label"],
                definition=record["definition"],
            )
            for iri, record in payload["nodes"].items()
        }
        children = {
            iri: tuple(child_iris) for iri, child_iris in payload["children"].items()
        }
        return cls(nodes, children)

    def to_tree_cache(self, root_iri: str) -> bytes:
        reachable: set[str] = set()
        pending = [root_iri]
        while pending:
            iri = pending.pop()
            if iri in reachable:
                continue
            reachable.add(iri)
            pending.extend(self.direct_children(iri))
        payload = {
            "root_iri": root_iri,
            "nodes": {
                iri: {
                    "iri": self.node(iri).iri,
                    "label": self.node(iri).label,
                    "definition": self.node(iri).definition,
                }
                for iri in sorted(reachable)
            },
            "children": {
                iri: [child for child in self.direct_children(iri) if child in reachable]
                for iri in sorted(reachable)
                if self.direct_children(iri)
            },
        }
        return json.dumps(payload, indent=2, ensure_ascii=True).encode("utf-8")

    @classmethod
    def load_cached_or_refresh(
        cls,
        cache_path: Path = DEFAULT_FOLIO_CACHE_PATH,
        tree_cache_path: Path = DEFAULT_FOLIO_TREE_CACHE_PATH,
        url: str = FOLIO_OWL_URL,
        root_iri: str = FOLIO_AGREEMENTS_IRI,
        refresh: bool = False,
        timeout: int = 120,
    ) -> "FolioHierarchy":
        if tree_cache_path.exists() and not refresh:
            return cls.from_tree_cache(tree_cache_path.read_bytes())
        if cache_path.exists() and not refresh:
            hierarchy = cls.from_owl(cache_path.read_bytes())
            tree_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tree_cache_path.write_bytes(hierarchy.to_tree_cache(root_iri))
            return cls.from_tree_cache(tree_cache_path.read_bytes())
        if not refresh:
            raise FileNotFoundError(f"FOLIO caches not found. Run with --refresh-folio to create them.")
        request = urllib.request.Request(url, headers={"User-Agent": "folio-jev-classifier/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
        hierarchy = cls.from_owl(content)
        tree_cache_path.parent.mkdir(parents=True, exist_ok=True)
        tree_cache_path.write_bytes(hierarchy.to_tree_cache(root_iri))
        return cls.from_tree_cache(tree_cache_path.read_bytes())

    def node(self, iri: str) -> FolioNode:
        return self.nodes[iri]

    def direct_children(self, iri: str) -> tuple[str, ...]:
        return self.children.get(iri, ())


def _criteria(nodes: FolioHierarchy, iris: tuple[str, ...]) -> dict[str, str]:
    return {
        f"option_{index}": f"{nodes.node(iri).label}: {nodes.node(iri).definition} (FOLIO IRI: {iri})"
        for index, iri in enumerate(iris)
    }


def _choice_question(criteria: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": (
            "Which direct child category best matches this OCR'd legal document? "
            "Use its purpose, legal context, procedural role, and operative content. "
            "Choose only among the supplied FOLIO categories."
        ),
        "criteria": criteria,
    }


def _best_probability(answer: dict[str, Any], option: str) -> float:
    probabilities = answer.get("probabilities", {})
    return float(probabilities.get(option, 0.0))


def estimate_tokens(text: str) -> int:
    """Estimate tokens without adding a tokenizer dependency."""
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def limit_document_context(document_markdown: str, max_tokens: int | None) -> tuple[str, dict[str, int | None]]:
    """Keep the first approximate tokens of the OCR document."""
    original_tokens = estimate_tokens(document_markdown)
    if max_tokens is None or max_tokens <= 0 or original_tokens <= max_tokens:
        return document_markdown, {
            "original_characters": len(document_markdown),
            "sent_characters": len(document_markdown),
            "original_token_estimate": original_tokens,
            "sent_token_estimate": original_tokens,
            "max_document_tokens": max_tokens,
        }

    token_matches = list(re.finditer(r"\w+|[^\w\s]", document_markdown, flags=re.UNICODE))
    end = token_matches[max_tokens - 1].end()
    limited = document_markdown[:end]
    return limited, {
        "original_characters": len(document_markdown),
        "sent_characters": len(limited),
        "original_token_estimate": original_tokens,
        "sent_token_estimate": estimate_tokens(limited),
        "max_document_tokens": max_tokens,
    }


def _candidate_options(
    hierarchy: FolioHierarchy,
    candidate: Candidate,
) -> tuple[tuple[str, ...], dict[str, str]]:
    child_iris = hierarchy.direct_children(candidate.node_iri)
    option_to_iri = {f"option_{index}": iri for index, iri in enumerate(child_iris)}
    return child_iris, option_to_iri


def _expand_from_answer(
    candidate: Candidate,
    option_to_iri: dict[str, str],
    answer: dict[str, Any],
    probability_multiplier: float = 1.0,
) -> list[Candidate]:
    probabilities = answer.get("probabilities", {})
    return [
        Candidate(
            node_iri=iri,
            path=candidate.path + (iri,),
            log_probability=candidate.log_probability
            + math.log(max(probability_multiplier * float(probabilities.get(option, 0.0)), EPSILON)),
            decisions=candidate.decisions + 1,
        )
        for option, iri in sorted(
            option_to_iri.items(), key=lambda item: probabilities.get(item[0], 0.0), reverse=True
        )[: max(3, min(10, len(option_to_iri)))]
    ]


def _expand_candidates(
    client: TypeSafeClient,
    hierarchy: FolioHierarchy,
    document_markdown: str,
    candidates: list[Candidate],
) -> list[Candidate]:
    questions: dict[str, dict[str, Any]] = {}
    normal_options: dict[str, tuple[Candidate, dict[str, str]]] = {}
    bucket_options: dict[str, tuple[Candidate, list[tuple[str, ...]]]] = {}

    for index, candidate in enumerate(candidates):
        child_iris, option_to_iri = _candidate_options(hierarchy, candidate)
        question_id = f"candidate_{index}"
        if len(child_iris) <= MAX_CHOICE_OPTIONS:
            questions[question_id] = _choice_question(_criteria(hierarchy, child_iris))
            normal_options[question_id] = (candidate, option_to_iri)
            continue
        buckets = [
            child_iris[offset : offset + MAX_CHOICE_OPTIONS]
            for offset in range(0, len(child_iris), MAX_CHOICE_OPTIONS)
        ]
        bucket_criteria = {
            f"bucket_{bucket_index}": "A FOLIO sibling group containing: "
            + ", ".join(hierarchy.node(iri).label for iri in bucket)
            for bucket_index, bucket in enumerate(buckets)
        }
        questions[question_id] = _choice_question(bucket_criteria)
        bucket_options[question_id] = (candidate, buckets)

    response = client.evaluate({"document_markdown": document_markdown}, questions)
    expanded: list[Candidate] = []
    for question_id, (candidate, option_to_iri) in normal_options.items():
        expanded.extend(_expand_from_answer(candidate, option_to_iri, response["answers"][question_id]))

    selected_bucket_questions: dict[str, dict[str, Any]] = {}
    selected_bucket_options: dict[str, tuple[Candidate, dict[str, str], float]] = {}
    for question_id, (candidate, buckets) in bucket_options.items():
        bucket_answer = response["answers"][question_id]
        selected_bucket = bucket_answer.get("choice", "bucket_0")
        bucket_index = int(selected_bucket.rsplit("_", 1)[-1])
        selected_children = buckets[min(bucket_index, len(buckets) - 1)]
        option_to_iri = {f"option_{index}": iri for index, iri in enumerate(selected_children)}
        selected_bucket_questions[question_id] = _choice_question(
            _criteria(hierarchy, selected_children)
        )
        selected_bucket_options[question_id] = (
            candidate,
            option_to_iri,
            _best_probability(bucket_answer, selected_bucket),
        )

    if selected_bucket_questions:
        child_response = client.evaluate(
            {"document_markdown": document_markdown}, selected_bucket_questions
        )
        for question_id, (candidate, option_to_iri, bucket_probability) in selected_bucket_options.items():
            expanded.extend(
                _expand_from_answer(
                    candidate,
                    option_to_iri,
                    child_response["answers"][question_id],
                    bucket_probability,
                )
            )
    return expanded


def classify(
    client: TypeSafeClient,
    hierarchy: FolioHierarchy,
    document_markdown: str,
    beam_width: int = 3,
    max_depth: int = 32,
    document_metadata: dict[str, int | None] | None = None,
    root_iri: str = FOLIO_AGREEMENTS_IRI,
    root_path_iris: tuple[str, ...] = (
        FOLIO_AGREEMENTS_IRI,
    ),
) -> dict[str, Any]:
    beam = [Candidate(root_iri, (), 0.0, 0)]
    finished: list[Candidate] = []

    for _ in range(max_depth):
        expandable = [candidate for candidate in beam if hierarchy.direct_children(candidate.node_iri)]
        finished.extend(candidate for candidate in beam if not hierarchy.direct_children(candidate.node_iri))
        if not expandable:
            break
        expanded: list[Candidate] = []
        expanded.extend(_expand_candidates(client, hierarchy, document_markdown, expandable))
        beam = sorted(expanded, key=lambda candidate: candidate.score, reverse=True)[:beam_width]

    finalists = sorted(finished + beam, key=lambda candidate: candidate.score, reverse=True)
    winner = finalists[0]
    path = [
        {"iri": iri, "label": hierarchy.node(iri).label}
        for iri in root_path_iris + winner.path
    ]
    return {
        "label": hierarchy.node(winner.node_iri).label,
        "iri": winner.node_iri,
        "path": path,
        "path_score": winner.score,
        "beam_width": beam_width,
        "depth": len(winner.path),
        "api_metadata": client.metadata(),
        "document_context": document_metadata or {},
    }


def _read_document(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("Provide --document or pipe OCR'd markdown on stdin.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", help="Path to OCR'd markdown/text; stdin is also supported.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--beam-width", type=int, default=3)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument(
        "--max-document-tokens",
        type=int,
        default=DEFAULT_DOCUMENT_MAX_TOKENS,
        help="Approximate maximum OCR document tokens sent as context; use 0 for unlimited.",
    )
    parser.add_argument("--folio-owl-url", default=FOLIO_OWL_URL)
    parser.add_argument(
        "--folio-cache",
        type=Path,
        default=DEFAULT_FOLIO_CACHE_PATH,
        help="Local FOLIO OWL cache path.",
    )
    parser.add_argument(
        "--folio-tree-cache",
        type=Path,
        default=DEFAULT_FOLIO_TREE_CACHE_PATH,
        help="Local extracted Agreements tree cache path.",
    )
    parser.add_argument(
        "--refresh-folio",
        action="store_true",
        help="Fetch FOLIO OWL from --folio-owl-url and replace the local cache.",
    )
    parser.add_argument(
        "--input-cost-per-1k",
        type=float,
        default=float(os.environ["TYPESAFE_INPUT_COST_PER_1K"])
        if os.environ.get("TYPESAFE_INPUT_COST_PER_1K")
        else None,
        help="Model input price in USD per 1,000 tokens.",
    )
    parser.add_argument(
        "--output-cost-per-1k",
        type=float,
        default=float(os.environ["TYPESAFE_OUTPUT_COST_PER_1K"])
        if os.environ.get("TYPESAFE_OUTPUT_COST_PER_1K")
        else None,
        help="Model output price in USD per 1,000 tokens.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        api_key = getpass.getpass("TypeSafe API key (hidden): ")
    if not api_key:
        parser.error("TYPESAFE_API_KEY is not set and no API key was entered.")

    try:
        document_markdown = _read_document(args.document)
        document_markdown, document_metadata = limit_document_context(
            document_markdown, args.max_document_tokens
        )
        hierarchy = FolioHierarchy.load_cached_or_refresh(
            cache_path=args.folio_cache,
            tree_cache_path=args.folio_tree_cache,
            url=args.folio_owl_url,
            refresh=args.refresh_folio,
        )
        result = classify(
            TypeSafeClient(
                api_key,
                model=args.model,
                input_cost_per_1k=args.input_cost_per_1k,
                output_cost_per_1k=args.output_cost_per_1k,
            ),
            hierarchy,
            document_markdown,
            beam_width=args.beam_width,
            max_depth=args.max_depth,
            document_metadata=document_metadata,
        )
    except (OSError, ET.ParseError, TypeSafeApiError, ValueError, KeyError) as error:
        parser.error(str(error))

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())