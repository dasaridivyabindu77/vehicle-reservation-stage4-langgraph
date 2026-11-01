import json
import statistics
import time
from pathlib import Path

from app.database import Database
from app.vector_store import StaticVectorStore

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

EVALUATION_QUERIES = [
    ("Where is the parking facility?", {"location"}),
    ("What vehicle parking zones are available?", {"parking-zones"}),
    ("How do I make a reservation?", {"booking-process"}),
    ("What is the cancellation policy?", {"cancellation-policy"}),
    ("How can I pay for parking?", {"payment"}),
    ("Is the facility secure?", {"security"}),
]


def precision_at_k(relevant, retrieved, k):
    retrieved_k = retrieved[:k]
    return sum(1 for item in retrieved_k if item in relevant) / k


def recall_at_k(relevant, retrieved, k):
    return sum(1 for item in retrieved[:k] if item in relevant) / len(relevant)


def run_evaluation():
    evaluation_dir = DATA_DIR / "evaluation"
    store = StaticVectorStore(evaluation_dir / "chroma", DATA_DIR / "static_knowledge.json")
    latencies = []
    recalls = []
    precisions = []
    for query, expected in EVALUATION_QUERIES:
        start = time.perf_counter()
        hits = store.search(query, top_k=3)
        latencies.append((time.perf_counter() - start) * 1000)
        retrieved = {hit["id"] for hit in hits}
        recalls.append(recall_at_k(expected, list(retrieved), 3))
        precisions.append(precision_at_k(expected, list(retrieved), 3))

    report = {
        "query_count": len(EVALUATION_QUERIES),
        "Recall@3": round(statistics.mean(recalls), 4),
        "Precision@3": round(statistics.mean(precisions), 4),
        "mean_latency_ms": round(statistics.mean(latencies), 2),
        "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2),
    }
    (DATA_DIR / "evaluation").mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "evaluation" / "report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, indent=2, fp=handle)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    run_evaluation()
