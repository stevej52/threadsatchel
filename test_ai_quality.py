"""Synthetic optional-AI evidence checks; no live archive or model is used.

The 16 questions below are a reviewable evaluation corpus, not a claim that a
mock proves a live model's reasoning accuracy. Injected responses let the tests
check which evidence the implementation accepts, retains, or rejects.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ai_memory
from import_memory import import_file


def case(name, question, text, subject, key, value, kind="observed", *,
         project="Atlas", conversation="atlas-design", quote=None):
    facts = [] if value is None else [{"subject": subject, "key": key,
        "value": value, "kind": kind, "quote": quote or text}]
    return {"name": name, "question": question, "text": text,
        "project": project, "conversation": conversation,
        "analysis": {"summary": text, "keywords": [subject, key],
            "questions": [question], "facts": facts}}


QUESTION_CASES = (
    case("owner", "Who owns the Atlas cache work?",
         "SYNTHETIC Atlas: Mira owns the cache implementation.",
         "Atlas", "cache owner", "Mira"),
    case("retention", "How long should Atlas retain diagnostics?",
         "SYNTHETIC Atlas decision: retain diagnostics for 30 days.",
         "Atlas", "diagnostic retention", "30 days", "decided"),
    case("controller_decision", "Which controller was approved for Atlas?",
         "SYNTHETIC Atlas decision: use the Raspberry Pi 5 controller.",
         "Atlas", "controller", "Raspberry Pi 5", "decided"),
    case("controller_proposal", "Did the later Jetson suggestion become a decision?",
         "SYNTHETIC Atlas later proposal: consider Jetson Orin Nano; no change is approved.",
         "Atlas", "controller", "Jetson Orin Nano", "proposed"),
    case("old_storage", "Which Atlas storage choice was explicitly replaced?",
         "SYNTHETIC Atlas: the microSD storage decision is superseded by eMMC.",
         "Atlas", "storage", "microSD", "superseded"),
    case("new_storage", "What explicitly replaced microSD for Atlas?",
         "SYNTHETIC Atlas decision: eMMC replaces the previous microSD storage decision.",
         "Atlas", "storage", "eMMC", "decided"),
    case("no_answer", "What is the confirmed Atlas launch date?",
         "SYNTHETIC Atlas: the launch date is not set; when can testing finish?",
         "Atlas", "launch date", None),
    case("part_number", "What is the exact Atlas shoulder servo part number?",
         "SYNTHETIC Atlas shoulder servo part number: XM430-W350-R.",
         "Atlas", "shoulder servo", "XM430-W350-R"),
    case("power_synonym", "What happens to Atlas after a power interruption?",
         "SYNTHETIC Atlas: brownouts reset the controller and erase its volatile counters.",
         "Atlas", "brownout behavior", "controller resets; volatile counters erased"),
    case("diagnostic_synonym", "Where can I find Atlas crash reports?",
         "SYNTHETIC Atlas: failure diagnostics are written to the local service journal.",
         "Atlas", "failure diagnostics", "local service journal"),
    case("other_project", "Who owns the Borealis cache work?",
         "SYNTHETIC Borealis: Ivo owns the cache implementation.",
         "Borealis", "cache owner", "Ivo", project="Borealis",
         conversation="borealis-design"),
    case("other_part", "Which servo belongs to Borealis rather than Atlas?",
         "SYNTHETIC Borealis shoulder servo part number: XL430-W250-T.",
         "Borealis", "shoulder servo", "XL430-W250-T", project="Borealis",
         conversation="borealis-design"),
    case("conversation_source", "Which Atlas review recorded the enclosure material?",
         "SYNTHETIC Atlas review decision: the enclosure uses aluminum.",
         "Atlas", "enclosure material", "aluminum", "decided",
         conversation="atlas-mechanical-review"),
    case("literal_quote", "What exact words state the Atlas freeze date?",
         "SYNTHETIC Atlas: the agreed freeze date is 2030-04-12; testing follows.",
         "Atlas", "freeze date", "2030-04-12", "decided",
         quote="the agreed freeze date is 2030-04-12"),
    case("unresolved", "Was the Atlas network transport selected?",
         "SYNTHETIC Atlas: transport remains uncertain between Ethernet and Wi-Fi.",
         "Atlas", "network transport", "Ethernet or Wi-Fi", "uncertain"),
    case("open_question", "Which Atlas question remains open about cooling?",
         "SYNTHETIC Atlas open question: is a passive heatsink sufficient?",
         "Atlas", "cooling", "is a passive heatsink sufficient?", "question"),
)


class SyntheticChat:
    """Deterministic endpoint replacement, deliberately without HTTP code."""
    def __init__(self, available=True):
        self.is_available = available
        self.calls = []

    def available(self):
        return self.is_available

    def analyze(self, record, project):
        if not self.is_available:
            raise AssertionError("Unavailable model must not be called")
        self.calls.append(record["id"])
        for item in QUESTION_CASES:
            if record["text"] == item["text"]:
                return deepcopy(item["analysis"])
        raise AssertionError("Unexpected synthetic source")


class EvidenceValidationTests(unittest.TestCase):
    def record(self, item):
        return {"id": "synthetic-" + item["name"], "text": item["text"],
            "title": "SYNTHETIC " + item["project"], "source": "SYNTHETIC fixture",
            "created_at": "2030-01-01T00:00:00Z"}

    def test_sixteen_question_evidence_contract(self):
        self.assertEqual(len(QUESTION_CASES), 16)
        for item in QUESTION_CASES:
            with self.subTest(question=item["question"]):
                accepted = ai_memory.validate_analysis(deepcopy(item["analysis"]), self.record(item))
                self.assertEqual(len(accepted["facts"]), len(item["analysis"]["facts"]))
                for actual, expected in zip(accepted["facts"], item["analysis"]["facts"]):
                    self.assertEqual(actual["kind"], expected["kind"])
                    self.assertEqual(actual["value"], expected["value"])
                    self.assertIn(actual["quote"], item["text"])

    def test_fabricated_and_foreign_source_quotes_are_rejected(self):
        item = QUESTION_CASES[0]
        for quote in ("Mira owns the release budget.", QUESTION_CASES[10]["text"], ""):
            with self.subTest(quote=quote):
                raw = deepcopy(item["analysis"])
                raw["facts"][0]["quote"] = quote
                with self.assertRaises(ValueError):
                    ai_memory.validate_analysis(raw, self.record(item))

    def test_exact_part_number_cannot_be_cited_with_changed_characters(self):
        item = next(c for c in QUESTION_CASES if c["name"] == "part_number")
        raw = deepcopy(item["analysis"])
        raw["facts"][0]["quote"] = item["text"].replace("XM430-W350-R", "XM430-W350-T")
        with self.assertRaises(ValueError):
            ai_memory.validate_analysis(raw, self.record(item))

    def test_unsupported_fact_kind_is_rejected(self):
        item = QUESTION_CASES[0]
        raw = deepcopy(item["analysis"])
        raw["facts"][0]["kind"] = "model-authoritative"
        with self.assertRaises(ValueError):
            ai_memory.validate_analysis(raw, self.record(item))

    def test_source_fingerprint_tracks_identity_text_and_metadata(self):
        source = self.record(QUESTION_CASES[0])
        initial = ai_memory.source_fingerprint(source)
        self.assertEqual(initial, ai_memory.source_fingerprint(dict(source)))
        for field in ("id", "text", "title", "source", "created_at"):
            with self.subTest(field=field):
                changed = dict(source)
                changed[field] += " changed"
                self.assertNotEqual(initial, ai_memory.source_fingerprint(changed))
        changed = dict(source, source_ids={"conversation_id": "synthetic-another-thread"})
        self.assertNotEqual(initial, ai_memory.source_fingerprint(changed))

    def test_chat_endpoint_rejects_external_hosts_and_redirects(self):
        for endpoint in ("https://127.0.0.1:8090", "http://example.invalid:8090",
                         "http://localhost:8090", "http://127.0.0.1:8090/remote",
                         "http://user:password@127.0.0.1:8090"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    ai_memory.LocalChat(dict(ai_memory.DEFAULTS, endpoint=endpoint))
        with self.assertRaises(ValueError):
            ai_memory.NoRedirect().redirect_request(None, None, 302, "redirect", {},
                "http://example.invalid/")


class OptionalAIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-quality-synthetic-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "memory.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)")
            db.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title,tokenize='unicode61')")
        groups = {}
        for item in QUESTION_CASES:
            groups.setdefault(item["conversation"], []).append(item)
        for conversation, items in groups.items():
            packet = {"format": "threadsatchel/1", "kind": "excerpt",
                "title": "SYNTHETIC " + items[0]["project"],
                "conversation_id": conversation,
                "messages": [{"message_id": item["name"], "speaker": "user",
                    "source_order": index, "source_date": f"2030-01-{index + 1:02d}T00:00:00Z",
                    "text": item["text"]} for index, item in enumerate(items)]}
            path = self.root / (conversation + ".json")
            path.write_text(json.dumps(packet), encoding="utf-8")
            import_file(path, self.db_path)
        self.config = ai_memory.load_config(self.root)
        self.config.update(enabled=True, embeddings=False, rerank=False,
            projects={"Atlas": ["Atlas"], "Borealis": ["Borealis"]},
            include_unassigned=False)
        self.write_config()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.ids = dict(db.execute("SELECT e.message_id, m.id FROM memories m JOIN import_revisions r ON r.memory_id=m.id JOIN import_entities e ON e.id=r.entity_id"))
        self.archive_digest = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self):
        (self.root / "memory-ai.json").write_text(json.dumps(self.config), encoding="utf-8")

    def process(self, client=None):
        return ai_memory.process(self.root, max_records=32, budget_seconds=20,
            chat_client=client or SyntheticChat())

    def brief(self, project="Atlas"):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            return ai_memory.project_brief(self.root, db, project)

    def test_disabled_feature_never_calls_network_or_model(self):
        self.config["enabled"] = False
        self.write_config()
        with patch("socket.socket", side_effect=AssertionError("Network forbidden when disabled")):
            client = SyntheticChat()
            report = self.process(client)
            self.assertFalse(report["enabled"])
            self.assertEqual(report["state"], "off")
            self.assertEqual(client.calls, [])
            self.brief()
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), self.archive_digest)

    def test_missing_local_config_defaults_off_without_creating_cache(self):
        (self.root / "memory-ai.json").unlink()
        with patch("socket.socket", side_effect=AssertionError("Default-off code cannot use network")):
            client = SyntheticChat()
            report = self.process(client)
            self.assertFalse(report["enabled"])
            self.assertEqual(client.calls, [])
        self.assertFalse((self.root / ".ai-cache").exists())

    def test_qwen_unavailable_preserves_canonical_archive(self):
        with patch("socket.socket", side_effect=AssertionError("Synthetic tests cannot use network")):
            report = self.process(SyntheticChat(available=False))
        self.assertIn(report["state"], ("deferred", "partial"))
        self.assertEqual(report["analyzed_chunks"], 0)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), self.archive_digest)

    def test_brief_keeps_decisions_proposals_and_source_evidence(self):
        report = self.process()
        self.assertGreater(report["analyzed_chunks"], 0)
        brief = self.brief()
        facts = brief["facts"]
        self.assertTrue(brief["generated"])
        self.assertTrue(any(f["kind"] == "decided" and f["value"] == "Raspberry Pi 5" for f in facts))
        self.assertTrue(any(f["kind"] == "proposed" and f["value"] == "Jetson Orin Nano" for f in facts))
        self.assertTrue(any(f["kind"] == "superseded" and f["value"] == "microSD" for f in facts))
        self.assertTrue(any(f["kind"] == "decided" and f["value"] == "eMMC" for f in facts))
        self.assertFalse(any(f["key"] == "launch date" for f in facts))
        self.assertFalse(any(f["subject"] == "Borealis" for f in facts))
        with closing(sqlite3.connect(self.db_path)) as db:
            originals = dict(db.execute("SELECT id,text FROM memories"))
            db.row_factory = sqlite3.Row
            fingerprints = {record["id"]: ai_memory.source_fingerprint(record)
                            for record in ai_memory.source_records(db)}
        for fact in facts:
            self.assertIn(fact["memory_id"], originals)
            self.assertIn(fact["source_quote"], originals[fact["memory_id"]])
        for evidence in [*facts, *brief["summaries"]]:
            self.assertEqual(evidence["source_fingerprint"], fingerprints[evidence["memory_id"]])
        enclosure = next(f for f in facts if f["key"] == "enclosure material")
        self.assertEqual(brief["project"], "Atlas")
        self.assertEqual(enclosure["source_ids"]["conversation_id"], "atlas-mechanical-review")
        self.assertEqual(enclosure["source_ids"]["message_id"], "conversation_source")
        self.assertEqual(enclosure["memory_id"], self.ids["conversation_source"])
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), self.archive_digest)

    def test_related_wording_uses_derived_questions_to_find_original_sources(self):
        self.process()
        for query, expected in (("power interruption", "power_synonym"),
                                ("crash reports", "diagnostic_synonym"),
                                ("XM430-W350-R", "part_number")):
            with self.subTest(query=query):
                with closing(sqlite3.connect(self.db_path)) as db:
                    db.row_factory = sqlite3.Row
                    hits = ai_memory.enhance_search(self.root, db, query, [], 5)
                self.assertIn(self.ids[expected], [hit["id"] for hit in hits])
                hit = next(h for h in hits if h["id"] == self.ids[expected])
                self.assertEqual(hit["text"], next(c["text"] for c in QUESTION_CASES if c["name"] == expected))

    def test_changed_source_invalidates_derived_fact_without_rewriting_it(self):
        self.process()
        before = self.brief()
        self.assertTrue(any(f["value"] == "Mira" for f in before["facts"]))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE memories SET text=? WHERE id=?", (
                "SYNTHETIC Atlas: cache ownership has not been assigned.", self.ids["owner"]))
        after = self.brief()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            _, current_fingerprint = ai_memory.current_sources(db, self.config)
        self.assertNotEqual(before["source_fingerprint"], current_fingerprint)
        self.assertFalse(any(f["value"] == "Mira" for f in after.get("facts", [])))
        self.assertFalse(after["generated"])
        self.assertIn(after["state"], ("pending", "partial"))

    def test_expired_processing_budget_cannot_republish_stale_source_facts(self):
        self.process()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE memories SET text=? WHERE id=?", (
                "SYNTHETIC Atlas: cache ownership has not been assigned.", self.ids["owner"]))
        report = ai_memory.process(self.root, max_records=32, budget_seconds=0,
            chat_client=SyntheticChat())
        self.assertEqual(report["state"], "partial")
        self.assertFalse(any(f["value"] == "Mira" for f in self.brief().get("facts", [])))

    def test_model_configuration_change_invalidates_analysis(self):
        self.process()
        self.assertTrue(self.brief()["facts"])
        self.config["model"] = str(self.config["model"]) + "-synthetic-new-version"
        self.write_config()
        after = self.brief()
        self.assertEqual(after.get("facts", []), [])
        self.assertFalse(after["generated"])
        self.assertIn(after["state"], ("pending", "partial"))

    def test_new_source_reuses_unchanged_evidence_as_explicitly_incomplete(self):
        self.process()
        before = self.brief()
        self.assertTrue(before["facts"])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            values = ("synthetic-later-source", "SYNTHETIC Atlas decision: Ivo now owns cache implementation, replacing Mira.",
                      "SYNTHETIC fixture", "SYNTHETIC Atlas", "2030-02-01")
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?)", values)
            db.execute("INSERT INTO memory_fts VALUES(?,?,?,?)", values[:4])
        after = self.brief()
        self.assertEqual(after["state"], "partial")
        self.assertTrue(after["generated"])
        self.assertEqual(after["freshness"], "source_updates_pending")
        self.assertTrue(after["coverage"]["work_pending"])
        self.assertEqual(after["source_fingerprint"], before["source_fingerprint"])
        self.assertNotEqual(after["current_source_fingerprint"], before["source_fingerprint"])
        self.assertEqual(after["facts"], before["facts"])
        self.assertEqual(after["coverage"]["analyzed_chunks"], before["coverage"]["analyzed_chunks"])
        self.assertEqual(after["coverage"]["current_source_records"], before["coverage"]["total_records"] + 1)
        self.assertNotIn("synthetic-later-source", [item["memory_id"] for item in [*after["facts"], *after["summaries"]]])
        self.assertIn("may contradict", after["caution"])
        self.assertIn("does not establish the latest decisions", after["caution"])

    def test_removed_referenced_source_hides_the_cached_brief(self):
        self.process()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM memory_fts WHERE id=?", (self.ids["owner"],))
            db.execute("DELETE FROM memories WHERE id=?", (self.ids["owner"],))
        after = self.brief()
        self.assertEqual(after["state"], "pending")
        self.assertFalse(after["generated"])
        self.assertEqual(after.get("facts", []), [])

    def test_changed_source_identity_metadata_hides_the_cached_brief(self):
        self.process()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE import_entities SET speaker='assistant' WHERE message_id='owner'")
        after = self.brief()
        self.assertEqual(after["state"], "pending")
        self.assertFalse(after["generated"])
        self.assertEqual(after.get("facts", []), [])

    def test_changed_summary_only_source_hides_the_cached_brief(self):
        self.process()
        before = self.brief()
        summary_id = before["summaries"][0]["memory_id"]
        before["facts"] = [fact for fact in before["facts"] if fact["memory_id"] != summary_id]
        with closing(sqlite3.connect(self.root / ".ai-cache" / "index.sqlite3")) as cache, cache:
            cache.execute("UPDATE responses SET result=? WHERE key='brief:Atlas'", (json.dumps(before),))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE memories SET text=text || ' SYNTHETIC corrected source.' WHERE id=?", (summary_id,))
        after = self.brief()
        self.assertEqual(after["state"], "pending")
        self.assertFalse(after["generated"])
        self.assertEqual(after.get("summaries", []), [])

    def test_distinct_facts_with_the_same_supporting_quote_are_preserved(self):
        class TwoFactsChat(SyntheticChat):
            def analyze(inner_self, record, project):
                result = super().analyze(record, project)
                if "retain diagnostics for 30 days" in record["text"]:
                    result["facts"].append({"subject": "Atlas", "key": "retained dataset",
                        "value": "diagnostics", "kind": "decided", "quote": record["text"]})
                return result
        self.process(TwoFactsChat())
        retained = [f for f in self.brief()["facts"] if f["memory_id"] == self.ids["retention"]]
        self.assertEqual({f["key"] for f in retained}, {"diagnostic retention", "retained dataset"})

    def test_a_new_model_can_retry_previously_held_analysis(self):
        class InvalidOwnerChat(SyntheticChat):
            def analyze(inner_self, record, project):
                result = super().analyze(record, project)
                if "Mira owns the cache" in record["text"]:
                    result["facts"][0]["quote"] = "A fabricated quotation."
                return result
        for _ in range(3):
            self.process(InvalidOwnerChat())
        self.assertFalse(any(f["value"] == "Mira" for f in self.brief().get("facts", [])))
        self.config["model"] = str(self.config["model"]) + "-synthetic-corrected-version"
        self.write_config()
        self.process(SyntheticChat())
        self.assertTrue(any(f["value"] == "Mira" for f in self.brief()["facts"]))


if __name__ == "__main__":
    unittest.main()
