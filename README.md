# Jev FOLIO Recursive Classifier

Classifies OCR'd legal documents through the recursive `rdfs:subClassOf` hierarchy below FOLIO's **Document Types / Transactional Document / Agreements** node.

The script uses a local FOLIO OWL cache, asks TypeSafe Jev a Choice question at each level, and retains a beam of candidate paths so an uncertain early decision can be recovered later. All beam candidates at a level are sent as questions in one TypeSafe request. FOLIO nodes with more than 255 direct children are handled through temporary sibling buckets because TypeSafe Choice questions support at most 255 options.

## Requirements

- Python 3.10 or newer
- A TypeSafe API key
- Network access to `api.typesafe.ai`; FOLIO GitHub access is needed only when refreshing the cache

No API key is stored by this project. Set it only in your local shell, or let the script prompt for it without echoing:

```bash
export TYPESAFE_API_KEY='your-key'
```

## FOLIO cache

The default caches are `.cache/FOLIO.owl` and `.cache/agreements-tree.json`. Normal runs load the extracted tree cache directly and do not fetch FOLIO over the network. The caches are ignored by Git; the OWL snapshot is about 17 MB, while the extracted Agreements tree is much smaller. Populate or update both explicitly:

```bash
python3 folio_classifier.py --refresh-folio --document agreement.md
```

Use `--folio-cache` to select a different local snapshot. Use `--folio-owl-url` only together with `--refresh-folio` when refreshing from a different source.

## Run

Save OCR'd markdown as a local file, then run:

```bash
python3 folio_classifier.py --document agreement.md
```

The CLI sends the first approximately 2,000 document tokens by default. Change the limit with `--max-document-tokens`, or use `0` to send the complete document:

```bash
python3 folio_classifier.py --document agreement.md --max-document-tokens 4000
python3 folio_classifier.py --document agreement.md --max-document-tokens 0
```

The token count is a deterministic approximation based on words and punctuation; the output reports both the original and sent estimates under `document_context`.

Or pipe the document without creating a persistent input file:

```bash
pbpaste | python3 folio_classifier.py
```

The output contains the selected FOLIO label and IRI, the complete path from `Document Types`, the beam width, and a length-normalized `path_score`. This is a traversal score, not TypeSafe's single-question confidence value.

## Context-length benchmark

Run the classifier seven times against the same document using 1,000, 2,500, 5,000, 10,000, 25,000, 50,000, and all approximate document tokens:

```bash
python3 -m pip install -r requirements.txt
python3 benchmark_context_lengths.py --document agreement.md
```

The script writes `context-benchmark.json` with each classification, FOLIO path, path score, request metadata, and estimated cost. It writes `context-benchmark.png`, plotting sent token length against path score and annotating each point with the resulting classification. The generated files are ignored by Git.

Useful options:

The default maximum traversal depth is five levels below `Agreements`. Override it explicitly when needed:

```bash
python3 folio_classifier.py --document agreement.md --beam-width 5 --max-depth 8
```

The JSON output includes `api_metadata` with per-request latency, request count, HTTP status, input tokens, and output tokens. To include an estimated cost, provide the current model rates in USD per 1,000 tokens:

```bash
export TYPESAFE_INPUT_COST_PER_1K='0.00'
export TYPESAFE_OUTPUT_COST_PER_1K='0.00'
python3 folio_classifier.py --document agreement.md
```

You can also pass `--input-cost-per-1k` and `--output-cost-per-1k`. The script does not guess or hardcode pricing; when rates are omitted, `estimated_cost_usd` is `null`.

## Smoke test with CUAD

The CUAD sample used during development is a commercial Services Agreement:

```bash
curl -L 'https://huggingface.co/datasets/theatticusproject/cuad/resolve/main/CUAD_v1/full_contract_txt/Part_I/ABILITYINC_06_15_2020-EX-4.25-SERVICES%20AGREEMENT.txt?download=true' -o /tmp/cuad-services-agreement.txt
python3 folio_classifier.py --document /tmp/cuad-services-agreement.txt
```

The expected broad category is `Transactional Document`, though the recursive result may select a more specific descendant if the current FOLIO hierarchy contains one that matches the agreement.

## Security

- Do not put `TYPESAFE_API_KEY` in source files, `.env` files committed to Git, shell transcripts, or issue reports.
- The API key is read from the environment or a hidden terminal prompt only.
- Document contents are sent to TypeSafe and should be handled according to your data-sharing requirements.

## Sources

- [FOLIO](https://github.com/alea-institute/FOLIO), CC BY 4.0
- [TypeSafe System One API](https://docs.typesafe.ai/api)
- [TypeSafe hierarchical classification cookbook](https://docs.typesafe.ai/cookbooks/hierarchical_classification)