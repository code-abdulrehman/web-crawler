#!/usr/bin/env python3
"""A polite, bounded, multi-site HTML crawler (Python 3.10+, stdlib only)."""

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

USER_AGENT = "LearningWebCrawler/1.0 (+https://example.org/crawler-info)"


def normalize_url(url: str) -> str | None:
    """Normalize an absolute http(s) URL, dropping its fragment."""
    try:
        url, _fragment = urldefrag(url.strip())
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        scheme = parts.scheme.lower()
        host = parts.hostname.lower().rstrip(".")
        # urlsplit.hostname strips IPv6 brackets, so restore them for the netloc.
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        path = parts.path or "/"
        return urlunsplit((scheme, host, path, parts.query, ""))
    except (ValueError, AttributeError):
        return None


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join(" ".join(self.title_parts).split())


class ScopedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed, check_robots=None):
        self.allowed = allowed
        self.check_robots = check_robots

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        normalized = normalize_url(newurl)
        if not normalized or not self.allowed(normalized):
            raise ValueError(f"Redirect outside crawl scope: {newurl}")
        if self.check_robots and not self.check_robots(normalized):
            raise ValueError(f"Redirect blocked by robots.txt: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, normalized)


class Crawler:
    def __init__(self, seeds, output, *, max_pages, max_depth, workers, delay, timeout, max_bytes,
                 include_subdomains=False):
        normalized_seeds = [normalize_url(u) for u in seeds]
        if not all(normalized_seeds):
            raise ValueError("Seed URLs must be absolute http(s) URLs")
        self.seeds = list(dict.fromkeys(normalized_seeds))
        self.seed_hosts = {urlsplit(u).hostname for u in self.seeds}
        self.include_subdomains = include_subdomains
        self.output = Path(output)
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.workers = workers
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.frontier = deque((u, 0) for u in self.seeds)
        self.seen = set(self.seeds)
        self._robots: dict[str, RobotFileParser] = {}
        self._robots_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_next: dict[str, float] = {}
        self._host_map_lock = threading.Lock()
        self._robot_opener = build_opener(ScopedRedirectHandler(self.in_scope))
        self._opener = build_opener(ScopedRedirectHandler(self.in_scope, self.robots_allowed))

    def in_scope(self, url):
        host = urlsplit(url).hostname
        return host in self.seed_hosts or (self.include_subdomains and any(
            host.endswith("." + seed) for seed in self.seed_hosts
        ))

    def robots_allowed(self, url):
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        with self._robots_lock:
            if origin not in self._robots:
                robot_url = origin + "/robots.txt"
                robot = RobotFileParser()
                robot.set_url(robot_url)
                try:
                    request = Request(robot_url, headers={"User-Agent": USER_AGENT})
                    with self._robot_opener.open(request, timeout=self.timeout) as response:
                        content = response.read(256_001)
                        if len(content) > 256_000:
                            robot.disallow_all = True  # Do not parse unbounded robots.txt files.
                        else:
                            robot.parse(content.decode("utf-8", errors="replace").splitlines())
                except HTTPError as exc:
                    if exc.code in (404, 410):
                        robot.allow_all = True
                    else:
                        robot.disallow_all = True  # Conservative for 401/403/429/5xx.
                except (URLError, OSError, ValueError):
                    robot.disallow_all = True
                self._robots[origin] = robot
            return self._robots[origin].can_fetch(USER_AGENT, url)

    def fetch(self, url):
        """Serialize requests per host, with a delay between request completions."""
        host = urlsplit(url).netloc
        with self._host_map_lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            remaining = self._host_next.get(host, 0) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            try:
                req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
                with self._opener.open(req, timeout=self.timeout) as response:
                    final_url = normalize_url(response.geturl())
                    media_type = response.headers.get_content_type()
                    if media_type not in ("text/html", "application/xhtml+xml"):
                        return {"status": "skipped_non_html", "url": url, "content_type": media_type}, []
                    content = response.read(self.max_bytes + 1)
                    if len(content) > self.max_bytes:
                        return {"status": "skipped_too_large", "url": url}, []
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        html = content.decode(charset, errors="replace")
                    except LookupError:
                        html = content.decode("utf-8", errors="replace")
                    parser = PageParser()
                    parser.feed(html)
                    digest = hashlib.sha256(content).hexdigest()
                    self.output.joinpath("pages").mkdir(parents=True, exist_ok=True)
                    filename = f"pages/{digest}.html"
                    try:
                        with self.output.joinpath(filename).open("xb") as handle:
                            handle.write(content)
                        duplicate_content = False
                    except FileExistsError:
                        duplicate_content = True
                    links = []
                    for href in parser.links:
                        candidate = normalize_url(urljoin(final_url, href))
                        if candidate and self.in_scope(candidate):
                            links.append(candidate)
                    return {
                        "status": "ok", "url": url, "final_url": final_url,
                        "title": parser.title, "depth": None, "sha256": digest,
                        "duplicate_content": duplicate_content, "file": filename,
                        "links_found_in_scope": len(links),
                    }, links
            except (HTTPError, URLError, OSError, ValueError, UnicodeError) as exc:
                return {"status": "error", "url": url, "error": str(exc)}, []
            finally:
                self._host_next[host] = time.monotonic() + self.delay

    def process(self, url, depth):
        if not self.robots_allowed(url):
            return {"status": "blocked_by_robots", "url": url, "depth": depth}, []
        record, links = self.fetch(url)
        record["depth"] = depth
        return record, links

    def run(self):
        self.output.mkdir(parents=True, exist_ok=True)
        count = 0
        pending = {}
        with self.output.joinpath("results.jsonl").open("w", encoding="utf-8") as log, \
             ThreadPoolExecutor(max_workers=self.workers) as pool:
            while self.frontier or pending:
                while self.frontier and len(pending) < self.workers and count + len(pending) < self.max_pages:
                    url, depth = self.frontier.popleft()
                    pending[pool.submit(self.process, url, depth)] = (url, depth)
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    url, depth = pending.pop(future)
                    try:
                        record, links = future.result()
                    except Exception as exc:
                        record, links = {"status": "error", "url": url, "depth": depth, "error": str(exc)}, []
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()
                    count += 1
                    print(f"[{count}/{self.max_pages}] {record['status']}: {url}")
                    if depth < self.max_depth:
                        for link in links:
                            if link not in self.seen:
                                self.seen.add(link)
                                self.frontier.append((link, depth + 1))
        print(f"Finished. Processed {count} URL(s). Saved results to {self.output / 'results.jsonl'}")


def main():
    parser = argparse.ArgumentParser(description="Polite same-host web crawler")
    parser.add_argument("seeds", nargs="+", help="One or more starting HTTP(S) URLs")
    parser.add_argument("--output", default="crawl-output")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests to a host")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    parser.add_argument("--include-subdomains", action="store_true")
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_depth < 0 or args.workers < 1 or args.delay < 0 or args.timeout <= 0 or args.max_bytes < 1:
        parser.error("Use positive limits, nonnegative max-depth, and a nonnegative delay")
    try:
        crawler = Crawler(args.seeds, args.output, max_pages=args.max_pages,
                          max_depth=args.max_depth, workers=args.workers, delay=args.delay,
                          timeout=args.timeout, max_bytes=args.max_bytes,
                          include_subdomains=args.include_subdomains)
    except ValueError as exc:
        parser.error(str(exc))
    crawler.run()


if __name__ == "__main__":
    main()
