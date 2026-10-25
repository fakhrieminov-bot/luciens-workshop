import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

from horizon.collector import CollectionError, OpenAlex, Store, main, normalize, normalize_doi


def work(identifier="W1", doi=None, title="A Research Paper", year=2026):
    return {"id": "https://openalex.org/" + identifier, "title": title, "doi": doi,
            "publication_year": year, "abstract_inverted_index": {"world": [1], "Hello": [0]}}


class CollectorTests(unittest.TestCase):
    def test_normalization(self):
        paper = normalize(work(doi="https://doi.org/10.1234/ABC"), "research")
        self.assertEqual(paper["doi"], "10.1234/abc")
        self.assertEqual(paper["abstract"], "Hello world")
        self.assertEqual(normalize_doi(" DOI:10.1234/ABC "), "10.1234/abc")

    def test_invalid_metadata(self):
        with self.assertRaises(CollectionError):
            normalize({"title": ""}, "q")

    def test_dedup_and_provenance(self):
        store = Store(":memory:")
        with store.db:
            store.put(normalize(work(), "one"))
            store.put(normalize(work("W2", "doi:10.1234/a"), "two"))
            store.put(normalize(work("W2", "doi:10.1234/a", "Updated title"), "three"))
        self.assertEqual(store.db.execute("SELECT count(*) FROM papers").fetchone()[0], 1)
        data = json.loads(store.db.execute("SELECT data FROM papers").fetchone()[0])
        self.assertEqual(data["queries"], ["one", "three", "two"])
        self.assertEqual(len(data["source_ids"]), 2)
        store.db.close()

    def test_conflicting_dois_and_missing_year(self):
        store = Store(":memory:")
        for identifier, doi, year in [("W1", "10.1/a", 2026), ("W2", "10.1/b", 2026), ("W3", None, None), ("W4", None, None)]:
            store.put(normalize(work(identifier, doi, year=year), "q"))
        self.assertEqual(store.db.execute("SELECT count(*) FROM papers").fetchone()[0], 4)
        store.db.close()

    def test_durable_rerun_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for _ in range(2):
                store = Store(path / "db")
                with store.db:
                    store.put(normalize(work(), "q"))
                store.export(path / "papers.jsonl")
                store.db.close()
            self.assertEqual(len((path / "papers.jsonl").read_text().splitlines()), 1)

    def test_request_is_bounded_and_key_not_in_url(self):
        opener = Mock(return_value=io.StringIO(json.dumps({"results": [work()]})))
        sleep = Mock()
        result = OpenAlex("secret", opener=opener, sleep=sleep).search("a & b", 5, "2026-01-01")
        request = opener.call_args.args[0]
        self.assertNotIn("secret", request.full_url)
        self.assertIn("per_page=5", request.full_url)
        self.assertEqual(len(result), 1)
        sleep.assert_called_once_with(1.1)

    def test_retry_after_and_retry_budget(self):
        failure = HTTPError("hidden", 429, "rate", {"Retry-After": "4"}, None)
        opener = Mock(side_effect=[failure, io.StringIO('{"results": []}')])
        sleep = Mock()
        self.assertEqual(OpenAlex(opener=opener, sleep=sleep).search("q"), [])
        sleep.assert_any_call(4)
        opener = Mock(side_effect=URLError("credential-bearing-url"))
        with self.assertRaisesRegex(CollectionError, "network request failed"):
            OpenAlex(opener=opener, sleep=Mock()).search("q")
        self.assertEqual(opener.call_count, 3)

    def test_long_cooldown_and_auth_fail_without_retry(self):
        for code, headers in [(429, {"Retry-After": "3600"}), (401, {})]:
            opener = Mock(side_effect=HTTPError("secret", code, "secret", headers, None))
            with self.assertRaises(CollectionError) as caught:
                OpenAlex(opener=opener, sleep=Mock()).search("q")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(opener.call_count, 1)

    def test_invalid_json(self):
        for body in ['no', '{}']:
            with self.assertRaises(CollectionError):
                OpenAlex(opener=Mock(return_value=io.StringIO(body)), sleep=Mock()).search("q")

    def test_cli_limits(self):
        with self.assertRaises(SystemExit):
            main(["--query", "q", "--limit", "101"])


if __name__ == "__main__":
    unittest.main()
