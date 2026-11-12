"""Evaluation harness for the CityPark RAG system.

Measures two things the spec asks for:

* **Retrieval accuracy** — Recall@K, Precision@K, Hit@K and MRR against a small
  labelled dataset (``eval_dataset.json``), computed on the raw top-K retrieval.
* **Grounding behaviour** — how often on-topic questions are (wrongly) refused
  and how often off-topic questions are (correctly) refused by the gate.
* **Performance** — mean and p95 per-turn latency.

Runs fully offline by default (deterministic hashing embeddings + in-memory
store) so it is reproducible and CI-friendly. Writes ``eval/reports/report.md``
and ``eval/reports/report.json``.

Usage::

    python -m eval.evaluate            # offline, reproducible
    python -m eval.evaluate --online   # use DIAL if a key is configured
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from time import perf_counter
from typing import Dict, List

from app import relevance
from app.chatbot import Chatbot
from app.config import Settings, get_settings

_DATASET = Path(__file__).parent / "eval_dataset.json"
_REPORT_DIR = Path(__file__).parent / "reports"


def _retrieval_metrics(bot: Chatbot, cases: List[dict], k: int) -> Dict[str, float]:
    recalls, precisions, rr, hits = [], [], [], []
    for case in cases:
        query = case["query"]
        relevant = set(case["relevant"])
        hits_raw = bot.store.search(query, k=k)
        retrieved = [doc.metadata.get("doc_id", "") for doc, _ in hits_raw]
        retrieved_relevant = [d for d in retrieved if d in relevant]

        recalls.append(len(set(retrieved_relevant)) / max(len(relevant), 1))
        precisions.append(len(retrieved_relevant) / max(len(retrieved), 1))
        hits.append(1.0 if retrieved_relevant else 0.0)
        first = next((i for i, d in enumerate(retrieved, 1) if d in relevant), None)
        rr.append(1.0 / first if first else 0.0)

    return {
        f"recall@{k}": round(statistics.mean(recalls), 3),
        f"precision@{k}": round(statistics.mean(precisions), 3),
        f"hit@{k}": round(statistics.mean(hits), 3),
        "mrr": round(statistics.mean(rr), 3),
    }


def _grounding_metrics(bot: Chatbot, data: dict) -> Dict[str, float]:
    latencies: List[float] = []

    false_refusals = 0
    for case in data["on_topic"]:
        t = perf_counter()
        resp = bot.chat(case["query"], session_id="eval")
        latencies.append((perf_counter() - t) * 1000)
        if resp.refused:
            false_refusals += 1

    correct_refusals = 0
    for case in data["off_topic"]:
        t = perf_counter()
        resp = bot.chat(case["query"], session_id="eval")
        latencies.append((perf_counter() - t) * 1000)
        if resp.refused:
            correct_refusals += 1

    n_on = len(data["on_topic"])
    n_off = len(data["off_topic"])
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    return {
        "false_refusal_rate": round(false_refusals / max(n_on, 1), 3),
        "offtopic_refusal_rate": round(correct_refusals / max(n_off, 1), 3),
        "latency_ms_mean": round(statistics.mean(latencies), 2),
        "latency_ms_p95": round(p95, 2),
    }


def run(online: bool = False) -> dict:
    data = json.loads(_DATASET.read_text(encoding="utf-8"))
    if online:
        settings = get_settings()
    else:
        settings = Settings(dial_api_key="", sqlite_path="eval_tmp.db")

    bot = Chatbot(settings=settings, prefer_weaviate=False)
    k = settings.top_k
    try:
        metrics = {
            "mode": "online(dial)" if settings.llm_configured else "offline",
            "vector_backend": bot.backend,
            "k": k,
            "n_on_topic": len(data["on_topic"]),
            "n_off_topic": len(data["off_topic"]),
            **_retrieval_metrics(bot, data["on_topic"], k),
            **_grounding_metrics(bot, data),
        }
    finally:
        bot.close()
    return metrics


def _write_report(metrics: dict) -> None:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "report.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    k = metrics["k"]
    recall = metrics[f"recall@{k}"]
    precision = metrics[f"precision@{k}"]
    hit = metrics[f"hit@{k}"]
    lines = [
        "# CityPark RAG — Evaluation Report",
        "",
        f"- Mode: `{metrics['mode']}`  |  Vector backend: `{metrics['vector_backend']}`  |  K = {k}",
        f"- Dataset: {metrics['n_on_topic']} on-topic + {metrics['n_off_topic']} off-topic queries",
        "",
        "## Retrieval accuracy",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Recall@{k} | {recall} |",
        f"| Precision@{k} | {precision} |",
        f"| Hit@{k} | {hit} |",
        f"| MRR | {metrics['mrr']} |",
        "",
        "## Grounding gate",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| False-refusal rate (on-topic wrongly refused) | {metrics['false_refusal_rate']} |",
        f"| Off-topic refusal rate (correctly refused) | {metrics['offtopic_refusal_rate']} |",
        "",
        "## Performance",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Mean latency (ms) | {metrics['latency_ms_mean']} |",
        f"| p95 latency (ms) | {metrics['latency_ms_p95']} |",
        "",
    ]
    (_REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the CityPark RAG system")
    parser.add_argument("--online", action="store_true", help="use DIAL if configured")
    args = parser.parse_args()

    metrics = run(online=args.online)
    _write_report(metrics)
    print(json.dumps(metrics, indent=2))
    print(f"\nReport written to {_REPORT_DIR / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
