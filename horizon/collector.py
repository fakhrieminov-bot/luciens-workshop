"""OpenAlex collection, durable deduplication, and portable JSONL export."""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class CollectionError(Exception):
    """Safe error message without request URLs or credentials."""


def normalize_doi(value):
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip().lower()) or None


def normalize(work, query):
    title = (work.get("title") or "").strip()
    source_id = work.get("id")
    if not title or not isinstance(source_id, str) or not source_id.startswith("https://openalex.org/W"):
        raise CollectionError("OpenAlex returned a work without a valid title or identifier")
    index = work.get("abstract_inverted_index") or {}
    abstract = " ".join(word for _, word in sorted((pos, word) for word, positions in index.items() for pos in positions)) or None
    location = work.get("primary_location") or {}
    return dict(source="openalex", source_id=source_id, doi=normalize_doi(work.get("doi")),
                title=title, year=work.get("publication_year"), publication_date=work.get("publication_date"),
                authors=[a.get("author", {}).get("display_name") for a in work.get("authorships", [])],
                venue=(location.get("source") or {}).get("display_name"),
                url=location.get("landing_page_url") or source_id, abstract=abstract,
                cited_by_count=work.get("cited_by_count"), open_access=work.get("open_access"),
                retrieved_at=datetime.now(timezone.utc).isoformat(), queries=[query])


class OpenAlex:
    def __init__(self, key=None, interval=1.1, attempts=3, opener=urlopen, sleep=time.sleep):
        self.key, self.interval, self.attempts = key, interval, attempts
        self.opener, self.sleep = opener, sleep

    def search(self, query, limit=25, since=None):
        # A single bounded page deliberately avoids unbounded cursor traversal.
        params = {"search": query, "per_page": limit, "sort": "publication_date:desc"}
        if since:
            params["filter"] = "from_publication_date:" + since
        headers = {"User-Agent": "HorizonResearchLab/0.1", "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = Request("https://api.openalex.org/works?" + urlencode(params), headers=headers)
        for attempt in range(self.attempts):
            self.sleep(self.interval)
            try:
                with self.opener(request, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    raise CollectionError("Invalid OpenAlex response")
                return [normalize(work, query) for work in payload["results"]]
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.attempts - 1:
                    raise CollectionError(f"OpenAlex HTTP {exc.code}; collection stopped") from None
                retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        delay = 2 ** (attempt + 1)
                if delay > 60:
                    raise CollectionError("OpenAlex requested a long cooldown; retry a later run") from None
                self.sleep(max(0, delay))
            except URLError:
                if attempt == self.attempts - 1:
                    raise CollectionError("OpenAlex network request failed") from None
                self.sleep(2 ** (attempt + 1))
            except (ValueError, TypeError):
                raise CollectionError("Invalid OpenAlex JSON response") from None


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS papers (id INTEGER PRIMARY KEY, doi TEXT UNIQUE, title_key TEXT, year INTEGER, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS aliases (source_id TEXT PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS title_year ON papers(title_key, year)")

    def put(self, paper):
        title_key = " ".join(re.findall(r"\w+", paper["title"].casefold()))
        row = self.db.execute("SELECT p.id, p.data FROM papers p JOIN aliases a ON a.paper_id=p.id WHERE a.source_id=?", (paper["source_id"],)).fetchone()
        if row is None and paper["doi"]:
            row = self.db.execute("SELECT id, data FROM papers WHERE doi=?", (paper["doi"],)).fetchone()
        # Missing year cannot safely support title fallback. Conflicting DOIs stay separate.
        if row is None and paper["year"] is not None:
            candidates = self.db.execute("SELECT id, data FROM papers WHERE title_key=? AND year=? AND (doi IS NULL OR doi=?)", (title_key, paper["year"], paper["doi"])).fetchall() if paper["doi"] else self.db.execute("SELECT id, data FROM papers WHERE title_key=? AND year=?", (title_key, paper["year"])).fetchall()
            if len(candidates) == 1:
                row = candidates[0]
        if row:
            old = json.loads(row[1])
            paper = dict(paper)
            paper["queries"] = sorted(set(old["queries"] + paper["queries"]))
            paper["first_seen_at"] = old["first_seen_at"]
            paper["doi"] = paper["doi"] or old["doi"]
            paper["source_ids"] = sorted(set(old["source_ids"] + [paper["source_id"]]))
            self.db.execute("UPDATE papers SET doi=?, title_key=?, year=?, data=? WHERE id=?", (paper["doi"], title_key, paper["year"], json.dumps(paper), row[0]))
            paper_id = row[0]
        else:
            paper = dict(paper, first_seen_at=paper["retrieved_at"], source_ids=[paper["source_id"]])
            paper_id = self.db.execute("INSERT INTO papers(doi,title_key,year,data) VALUES (?,?,?,?)", (paper["doi"], title_key, paper["year"], json.dumps(paper))).lastrowid
        self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (paper["source_id"], paper_id))

    def export(self, path):
        target = Path(path)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for (data,) in self.db.execute("SELECT data FROM papers ORDER BY id"):
                stream.write(json.dumps(json.loads(data), ensure_ascii=False, sort_keys=True) + "\n")
        temporary.replace(target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date().isoformat())
    parser.add_argument("--output", default="data")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 1 <= len(args.query) <= 10 or any(not q.strip() or len(q) > 500 for q in args.query):
        parser.error("Use 1–10 nonempty queries (max 500 characters), with --limit 1–100")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output / "horizon.sqlite3")
    try:
        client = OpenAlex(os.getenv("OPENALEX_API_KEY"))
        # Fetch first: failed requests do not partially update durable state.
        papers = [paper for query in args.query for paper in client.search(query, args.limit, args.since)]
        with store.db:
            for paper in papers:
                store.put(paper)
        store.export(output / "papers.jsonl")
        print(f"Collected {len(papers)} source results; exported deduplicated library")
    except CollectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.db.close()
    return 0
