# RAG System Evaluation

## Results

The latest measured results are:

| Metric | Result |
|---|---:|
| Query set size | 6 |
| Mean Recall@3 | 83.33% |
| Mean Precision@3 | 27.78% |
| Mean retrieval latency | 11.66 ms |
| P95 retrieval latency | 4.42 ms |

The JSON report is available at `data/evaluation/report.json`.

## Methodology

Six representative information queries were evaluated against the static knowledge collection. Recall@K measures whether an expected document appears in the top three results; Precision@K measures the proportion of the top three results that are relevant. Retrieval latency is measured around local vector search calls.

Dynamic availability, prices, and reservation capacity are tested separately against SQLite and the reservation flow.

## Interpretations and limitations

- The current deterministic hash embedding is dependency-free and suitable for a local demonstration.
- Precision@3 is lower than Recall@3 because several relevant single-answer queries naturally share the top-k result window with two broader documents.
- Production deployments should replace the hash embedding with a reviewed pretrained or hosted semantic embedding model and expand the labeled evaluation set.
