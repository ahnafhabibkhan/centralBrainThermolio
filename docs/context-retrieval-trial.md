# Context refresh and retrieval evaluation

## Deployed behavior

`get_context_for_task(query, folder_id, known_revision, limit)` returns fresh search results, a context revision, and an overview when the revision differs. The caller passes its previous revision on later calls. Unchanged overviews are omitted to reduce repeated context. Results remain current even when the overview is unchanged.

The tool compares revisions before and after search. It retries once if the library changes during retrieval, then returns no results and `refresh_required=true` if it cannot obtain a stable view. Agents must replace cached results after a revision change. This remains a pull-based system, not a background notification service for existing chats.

Search groups identical original bytes and identical extracted sections before applying the result limit. `also_in` contains up to ten other accessible locations, with a truncation flag and total accessible count. Grouping neither deletes duplicates nor makes copies synchronize. Independent memory records are not merged. Permissions, approval, expiry, hierarchy visibility, and folder scope apply before grouping. Literal path matching also finds filenames with dots or hyphens and assets without extracted text.

The tool response is bounded by `max_context_chars`. Omitted context, copy locations, or results are explicitly marked. Original sources remain authoritative.

## Reproducible local benchmark

The benchmark uses `scripts/retrieval_fixture.json`, a synthetic corpus of 16 short documents with 48 answerable questions and four unanswerable probes. There are 16 exact keyword queries, 16 English paraphrases, and 16 French questions. These are authored test cases, not real business files or a held-out production evaluation.

The keyword baseline uses PostgreSQL English `websearch_to_tsquery` and `ts_rank_cd`, weighted paths, and literal path matching. Natural-language questions are passed directly, without an assistant rewriting them into keywords. This is a material limitation of the comparison: an assistant that rewrites queries can improve the keyword baseline.

The semantic candidate is [paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2), revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`. It runs locally with normalized embeddings. Hybrid search combines keyword and semantic ranks using reciprocal rank fusion with constant 60 and ten semantic candidates.

| Method | Expected file at rank 1 | Expected file in first 3 | Expected file in first 5 | Local p95 query time |
|---|---:|---:|---:|---:|
| Keyword | 16/48 | 16/48 | 16/48 | 5.70 ms |
| Semantic | 43/48 | 46/48 | 48/48 | 49.33 ms |
| Hybrid | 43/48 | 46/48 | 48/48 | 52.02 ms |

All methods passed the 16 exact queries at rank 1. Semantic retrieval helped with paraphrases and French questions. Hybrid retrieval did not improve the measured outcome beyond semantic retrieval in this small corpus. These latency figures come from a Mac using four CPU threads, not the AWS instance.

All four unanswerable probes scored below the illustrative cosine threshold of 0.45. Four probes are insufficient to validate a production rejection threshold. Semantic retrieval still returns ranked candidates even when an answer does not exist, so grounded answers and calibrated rejection remain necessary.

## Changed-document small-model trial

[Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B), revision `c1899de289a04d12100db370d81485cdf75e47ca`, ran locally on Apple MPS with thinking disabled. It returned a category and an exact source excerpt. The model had no tools and could not modify documents, permissions, approvals, or folders.

The cache keys include document identity, version, path, text, model/prompt version, and scope. A complete supplied snapshot removes absent records. Changed records are regenerated; unchanged records are reused. This module is a local trial facility, not a production ingestion worker.

| Run | Documents processed | Cached documents reused | Cached records removed |
|---|---:|---:|---:|
| Initial 16 documents | 16 | 0 | 0 |
| Identical snapshot | 0 | 16 | 0 |
| One updated document | 1 | 15 | 0 |
| One deleted document | 0 | 15 | 1 |

All initial responses passed JSON and exact-evidence validation. However, only 8/16 categories matched the expected taxonomy. The category names were supplied without detailed definitions, which is a limitation of this initial prompt. Valid formatting and grounded quotations do not prove correct classification. Median generation time was 2.078 seconds per processed document on this Mac.

Automatic classification remains disabled. A future trial should define category boundaries, add held-out examples and adversarial documents, and measure whether generated section context improves retrieval. This result does not justify automatic filing decisions.

## Running the evaluation

Use a separate local environment so model dependencies do not enter the production image:

```sh
python -m venv work/retrieval-venv
work/retrieval-venv/bin/python -m pip install sentence-transformers==5.1.0 transformers==4.56.2 torch==2.8.0 'psycopg[binary,pool]>=3.2,<4' 'python-dotenv>=1,<2'
HF_HUB_DISABLE_PROGRESS_BARS=1 work/retrieval-venv/bin/python scripts/download_trial_models.py
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 work/retrieval-venv/bin/python scripts/benchmark_retrieval.py
```

The script connects only to the local `central_brain_test` database and supplies synthetic records directly to a read-only SQL expression. It does not create tables or alter production data. Downloading public model weights requires internet access; inference runs locally and offline. Model results are saved under `work/retrieval-evaluation/`.

## Deployment decision

Context refresh, filename matching, and permission-filtered duplicate grouping are deployed. Semantic search and the small model remain local evaluation components. No model API calls, additional AWS services, or always-running model processes were added. The next evaluation should use representative, access-approved business questions and compare assistant-rewritten keyword queries with hybrid retrieval before any production semantic rollout.
