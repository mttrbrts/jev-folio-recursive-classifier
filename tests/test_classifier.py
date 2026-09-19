import json
import tempfile
import unittest
from pathlib import Path

from folio_classifier import (
  FOLIO_DOCUMENT_TYPES_IRI,
  FolioHierarchy,
  TypeSafeClient,
  classify,
  estimate_tokens,
  limit_document_context,
)


OWL = b'''<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:skos="http://www.w3.org/2004/02/skos/core#">
  <owl:Class rdf:about="https://folio.openlegalstandard.org/RBkL8I5saFF7mqpLTI7GxSh">
    <rdfs:label>Document Types</rdfs:label>
  </owl:Class>
  <owl:Class rdf:about="https://folio.example/transactional">
    <rdfs:subClassOf rdf:resource="https://folio.openlegalstandard.org/RBkL8I5saFF7mqpLTI7GxSh"/>
    <rdfs:label>Transactional Document</rdfs:label>
    <skos:definition>A document memorializing a transaction.</skos:definition>
  </owl:Class>
  <owl:Class rdf:about="https://folio.example/services">
    <rdfs:subClassOf rdf:resource="https://folio.example/transactional"/>
    <rdfs:label>Services Agreement</rdfs:label>
  </owl:Class>
</rdf:RDF>'''


class FakeClient(TypeSafeClient):
    def __init__(self):
        self.request_metrics = []
        self.input_cost_per_1k = None
        self.output_cost_per_1k = None

    def evaluate(self, state, questions):
        answers = {}
        for question_id, question in questions.items():
            option = next(
                key
                for key, value in question["criteria"].items()
                if "Transactional Document" in value or "Services Agreement" in value
            )
            answers[question_id] = {"probabilities": {option: 1.0}, "choice": option}
        self.request_metrics.append(
            {"latency_ms": 12.5, "status": 200, "input_tokens": 100, "output_tokens": 10}
        )
        return {"answers": answers}


class ClassifierTests(unittest.TestCase):
    def test_limits_document_context_to_first_tokens(self):
        document = "One two three four five six"
        limited, metadata = limit_document_context(document, 3)
        self.assertEqual(limited, "One two three")
        self.assertEqual(metadata["sent_token_estimate"], 3)
        self.assertEqual(estimate_tokens(document), 6)

    def test_zero_document_limit_preserves_context(self):
        document = "One two three"
        limited, metadata = limit_document_context(document, 0)
        self.assertEqual(limited, document)
        self.assertEqual(metadata["max_document_tokens"], 0)

    def test_loads_recursive_subclasses(self):
        hierarchy = FolioHierarchy.from_owl(OWL)
        self.assertEqual(hierarchy.direct_children(FOLIO_DOCUMENT_TYPES_IRI), ("https://folio.example/transactional",))
        self.assertEqual(hierarchy.direct_children("https://folio.example/transactional"), ("https://folio.example/services",))

    def test_loads_from_local_cache_without_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "FOLIO.owl"
            tree_cache_path = Path(directory) / "agreements-tree.json"
            cache_path.write_bytes(OWL)
            hierarchy = FolioHierarchy.load_cached_or_refresh(
                cache_path=cache_path,
                tree_cache_path=tree_cache_path,
                root_iri=FOLIO_DOCUMENT_TYPES_IRI,
            )
            self.assertEqual(len(hierarchy.nodes), 3)
            cache_path.unlink()
            hierarchy = FolioHierarchy.load_cached_or_refresh(
                cache_path=cache_path,
                tree_cache_path=tree_cache_path,
                root_iri=FOLIO_DOCUMENT_TYPES_IRI,
            )
            self.assertEqual(len(hierarchy.nodes), 3)

    def test_returns_leaf_path(self):
        hierarchy = FolioHierarchy.from_owl(OWL)
        result = classify(
          FakeClient(),
          hierarchy,
          "services agreement",
          root_iri=FOLIO_DOCUMENT_TYPES_IRI,
          root_path_iris=(FOLIO_DOCUMENT_TYPES_IRI,),
        )
        self.assertEqual(result["iri"], "https://folio.example/services")
        self.assertEqual([item["label"] for item in result["path"]], ["Document Types", "Transactional Document", "Services Agreement"])
        self.assertEqual(result["api_metadata"]["request_count"], 2)
        self.assertEqual(result["api_metadata"]["input_tokens"], 200)
        self.assertEqual(result["api_metadata"]["total_latency_ms"], 25.0)


if __name__ == "__main__":
    unittest.main()