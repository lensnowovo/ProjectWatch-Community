"""Public-only, prospective coverage probe. Never opens the private project DB.

Run against the installed candidate; save only status/length/hash, never page text.
The fixed sample must not be edited after observing a run to improve its score.
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from project_watch.feeds import GuardedHTTPS, _pinned_request
from project_watch.host_network import configured_host_python
from project_watch.public_article import ArticleReader, resolve_article

URLS = (
    "https://auto.news18a.com/news/storys_299890.html",
    "https://auto.news18a.com/news/storys_299873.html",
    "https://auto.ifeng.com/c/8wd6YjCBeBg",
    "https://news.qq.com/rain/a/20261004A07UZI00",
    "https://www.sohu.com/a/1083662463_122194300",
    "https://www.sohu.com/a/1082519957_100116740",
    "https://laoyaoba.com/n/1115219",
    "https://news.futunn.com/post/1000374079/goldman-sachs-maintains-amphenol-aphus-with-buy-rating-raises-target",
    "https://www.sohu.com/a/1080452736_122004016",
    "https://www.tradingkey.com/zh-hans/news/market-movers/262178700-market-movers-aph-20260921",
)


def probe():
    rows = []
    for url in URLS:
        attempts = []

        def connect(host, ip, path, *, attempts=attempts):
            attempt = {"host": host, "ip": ip}
            attempts.append(attempt)
            try:
                response = _pinned_request(host, ip, path, accept="text/html, application/xhtml+xml")
                attempt["http_status"] = response[0]
                return response
            except Exception as exc:
                attempt["error_class"] = type(exc).__name__
                attempt["cause_class"] = type(exc.__cause__).__name__ if exc.__cause__ else None
                attempt["stage"] = getattr(exc, "stage", None)
                raise

        started = time.monotonic()
        host_worker = configured_host_python()
        reader = (ArticleReader() if host_worker else
                  ArticleReader(GuardedHTTPS(resolver=resolve_article, connector=connect)))
        result = reader.read(url)
        row = {"url": url, "status": result["status"], "error_code": result["error_code"],
               "characters": len(result["text"]), "body_sha256": result["body_sha256"],
               "seconds": round(time.monotonic()-started, 2), "attempts": attempts,
               "transport": "windows_host_pipe" if host_worker else "direct"}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    usable = sum(row["status"] in {"article_body", "partial_body"} for row in rows)
    return {"sample": "public-coverage-fixed-20261006-b", "measured_at": datetime.now(UTC).isoformat(),
            "usable": usable, "total": len(rows), "target": 8, "passed": usable >= 8, "rows": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive creation preserves each run's receipt, including its failures.
    with args.receipt.open("x", encoding="utf-8") as output:
        receipt = probe()
        json.dump(receipt, output, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in receipt.items() if k != "rows"}))
