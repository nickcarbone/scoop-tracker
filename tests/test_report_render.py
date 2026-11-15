import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from main import write_html


class ReportRenderTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        articles = [
            {
                "link": "https://example.com/outlet-a/bridge-report",
                "source": "Outlet A",
                "title": "City audit finds hidden safety report on bridge repairs",
                "summary": "Documents show officials concealed an independent bridge inspection report before the repairs contract was awarded.",
                "published": "",
                "published_parsed": None,
                "byline_count": 3,
                "author_names": ["Reporter One", "Reporter Two", "Reporter Three"],
                "date_published": "",
                "word_count": None,
                "found_jsonld": False,
                "keyword_score": 9,
                "byline_bonus": 4,
                "total_score": 13,
                "score_hits": [
                    {"category": "sourcing_depth", "pattern": "documents obtained by", "weight": 4},
                    {"category": "primary_source", "pattern": "inspection report", "weight": 3},
                    {"category": "headline_label", "pattern": "^exclusive:", "weight": 3},
                ],
            },
            {
                "link": "https://example.com/outlet-b/bridge-audit",
                "source": "Outlet B",
                "title": "Exclusive: City hid bridge safety report before repairs contract",
                "summary": "Documents show officials concealed an independent bridge inspection report before the repairs contract was awarded.",
                "published": "",
                "published_parsed": None,
                "byline_count": 1,
                "author_names": ["Reporter Four"],
                "date_published": "",
                "word_count": None,
                "found_jsonld": False,
                "keyword_score": 6,
                "byline_bonus": 0,
                "total_score": 6,
                "score_hits": [
                    {"category": "headline_label", "pattern": "^exclusive:", "weight": 3},
                    {"category": "primary_source", "pattern": "inspection report", "weight": 3},
                ],
            },
        ]
        db.upsert_articles(self.conn, articles)

    def tearDown(self):
        self.conn.close()

    def test_report_contains_triage_controls_and_story_explanations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scoop_report.html"
            write_html(
                self.conn,
                [("Outlet A", "OK (10 items)"), ("Outlet B", "OK (8 items)")],
                path=str(path),
                new_urls={"https://example.com/outlet-b/bridge-audit"},
            )
            html = path.read_text(encoding="utf-8")

        self.assertIn("story-search", html)
        self.assertIn("data-view=\"saved\"", html)
        self.assertIn("data-signal=\"sourcing_depth\"", html)
        self.assertIn("Why it surfaced", html)
        self.assertIn("assets/report.css", html)
        self.assertIn("assets/report.js", html)
        self.assertIn("Grouped automatically by similarity", html)
        self.assertIn("NEW", html)
        self.assertEqual(html.count("data-story-card"), 1)
        self.assertIn("Outlet B", html)
        self.assertIn("Outlet A", html)

    def test_rss_markup_is_not_rendered_as_markup(self):
        article = {
            "link": "https://example.com/story",
            "source": "Outlet C",
            "title": "Safety report published",
            "summary": "<script>alert('not executable')</script>Public records explain the report.",
            "published_parsed": None,
            "first_seen_at": None,
            "byline_count": 1,
            "author_names": [],
            "total_score": 3,
            "matched_categories": ["primary_source"],
            "_cluster_others": [],
        }
        from main import render_rows
        rendered = render_rows([article])
        self.assertNotIn("<script>", rendered)
        self.assertIn("alert(&#x27;not executable&#x27;)", rendered)


if __name__ == "__main__":
    unittest.main()
