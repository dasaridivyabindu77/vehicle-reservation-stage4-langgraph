"""Terminal chat loop. Run with ``python -m app.cli``."""

from __future__ import annotations

import sys

from .chatbot import Chatbot


def main() -> int:
    bot = Chatbot()
    mode = "DIAL" if bot.settings.llm_configured else "offline"
    print(f"CityPark assistant — backend: vector={bot.backend}, llm={mode}")
    print("Type your question, or 'quit' to exit.\n")
    try:
        while True:
            try:
                message = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not message:
                continue
            if message.lower() in {"quit", "exit", ":q"}:
                break
            resp = bot.chat(message)
            print(f"bot> {resp.reply}")
            if resp.sources:
                cites = ", ".join(s["doc_id"] for s in resp.sources)
                print(f"     (sources: {cites} | {resp.latency_ms} ms)")
            print()
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
