# Learning Web Crawler

A small but usable Python 3.10+ crawler using only the standard library. It follows links within the seed host(s), reads `robots.txt`, delays requests to each host, deduplicates URLs and identical HTML, and saves HTML plus JSONL metadata.

## Run

```bash
python crawler.py https://example.com --max-pages 30 --max-depth 2 --workers 4 --delay 1.0 --output crawl-output
```

- Provide multiple seed URLs to crawl multiple hosts concurrently.
- `--include-subdomains` also permits subdomains of seed hosts.
- `--max-pages` limits processed URL tasks, not necessarily successful downloads.
- `--max-depth 0` only crawls seed URLs.
- `--delay` inserts a pause **after each request** to the same host, and at most one request per host is active at a time.
- Robots-file errors other than 404/410 conservatively block crawling for that origin. Redirects cannot leave the configured scope.
- Content larger than `--max-bytes` and non-HTML responses are skipped. A `pages/<sha256>.html` file is stored only once per byte-identical page.
- Results are written to `crawl-output/results.jsonl` (one JSON object per processed URL).

This is an educational crawler; it does not render JavaScript, authenticate, retry temporary errors, persist the queue across restarts, or implement a distributed URL frontier. Crawl only where permitted and keep limits modest.
