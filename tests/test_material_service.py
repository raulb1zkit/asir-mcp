import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from scripts.material_service import MaterialError, MaterialService, Settings, MAX_OUTPUT_BYTES, compact_locations, size_bytes
from scripts.semantic_search import Retriever
from test_search_material import build_fixture, chunk


def material(text, method="native", subject="ASO", section="UD1"):
    value = chunk(text, subject=subject, section=section, method=method)
    value["citations"][0]["source"]["sha256"] = "a" * 64
    value["citations"][0]["source"]["local_path"] = "/private/original.pdf"
    return value


class MaterialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.first = material("crontab prioridad " + "a" * 600)
        self.second = material("renice prioridad de procesos")
        self.ocr = material("crontab texto reconocido", method="ocr_supplement")
        citation = self.first["citations"][0]
        citation["next_chunk_id"] = self.second["id"]
        citation["previous_chunk_id"] = self.ocr["id"]
        segment = citation["segments"][0]
        segment["end"] = 300
        other = copy.deepcopy(segment)
        other.update(start=300, end=len(self.first["text"]))
        other["locator"] = {"block": 2}
        citation["segments"].append(other)
        self.foreign = material("crontab otras clases", subject="FOL")
        self.db = build_fixture(root, [self.first, self.second, self.ocr, self.foreign])
        self.before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.settings = Settings(database=self.db)
        self.service = MaterialService(self.settings)
        await self.service.start()
        self.addAsyncCleanup(self.service.close)

    async def test_catalog_and_search_filter_scope_and_remove_disk_paths(self):
        catalog = await self.service.catalog()
        self.assertEqual([s["name"] for s in catalog["subjects"]], ["ASO"])
        self.assertEqual(catalog["subjects"][0]["sources"], 1)
        found = await self.service.search("crontab", mode="lexical", native_only=True)
        self.assertEqual(len(found["results"]), 1)
        encoded = json.dumps(found)
        self.assertNotIn("local_path", encoded)
        self.assertNotIn("/private/", encoded)
        self.assertEqual(found["answerability"], "not_assessed")
        self.assertTrue(found["data_is_untrusted"])
        self.assertEqual(found["results"][0]["references"][0]["locations"], [{"kind": "block", "start": 1, "end": 2}])

    async def test_review_flags_survive_search_and_read(self):
        found = await self.service.search("reconocido", mode="lexical")
        self.assertTrue(found["results"][0]["requires_review"])
        read = await self.service.read(self.ocr["id"])
        self.assertEqual(read["fragment"]["warnings"], ["ocr_requires_review"])
        with self.assertRaisesRegex(MaterialError, "fragment_not_available"):
            await self.service.read(self.ocr["id"], native_only=True)

    async def test_neighbors_stay_in_same_source_and_method(self):
        result = await self.service.read(self.first["id"])
        self.assertEqual([c["fragment_id"] for c in result["context"]], [self.second["id"]])
        self.assertEqual(result["context"][0]["relation"], "next")
        self.assertEqual((await self.service.read(self.first["id"], neighbors=0))["context"], [])

    async def test_pagination_and_reference_offsets(self):
        first = (await self.service.read(self.first["id"], characters=256, neighbors=0))["fragment"]
        self.assertEqual(first["next_offset"], 256)
        self.assertEqual(first["references"][0]["locations"], [{"kind": "block", "start": 1, "end": 1}])
        tail = (await self.service.read(self.first["id"], start=300, neighbors=0))["fragment"]
        self.assertIsNone(tail["next_offset"])
        self.assertEqual(tail["text"], self.first["text"][300:])
        self.assertEqual(tail["references"][0]["locations"], [{"kind": "block", "start": 2, "end": 2}])
        with self.assertRaisesRegex(MaterialError, "offset_out_of_range"):
            await self.service.read(self.first["id"], start=len(self.first["text"]))

    async def test_scope_cannot_be_overridden_by_caller(self):
        with self.assertRaisesRegex(MaterialError, "subject_not_available"):
            await self.service.search("crontab", subject="FOL", mode="lexical")
        with self.assertRaisesRegex(MaterialError, "fragment_not_available"):
            await self.service.read(self.foreign["id"])
        with self.assertRaisesRegex(MaterialError, "fragment_not_available"):
            await self.service.read(self.first["id"], unit="UD9")

    async def test_path_and_argument_rejection(self):
        for value in ["../../.private/moodle-session.json", "file:///etc/passwd", "chunk:" + "0" * 63]:
            with self.assertRaisesRegex(MaterialError, "invalid_fragment_id"):
                await self.service.read(value)
        with self.assertRaisesRegex(MaterialError, "fragment_not_available"):
            await self.service.read("chunk:" + "0" * 64)
        for kwargs in [{"limit": 9}, {"query": "a" * 2001}, {"unit": "a" * 201}]:
            with self.assertRaises(MaterialError):
                await self.service.search(**({"query": "cron", "mode": "lexical"} | kwargs))

    async def test_concurrent_calls_keep_sqlite_affinity_and_index_unchanged(self):
        results = await asyncio.gather(*(self.service.search("crontab", mode="lexical") for _ in range(8)))
        self.assertTrue(all(r["results"] for r in results))
        await self.service.close()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.before)
        with self.assertRaisesRegex(MaterialError, "service_closed"):
            await self.service.catalog()

    async def test_large_reference_is_rejected_instead_of_unbounded_output(self):
        await self.service.close()
        large = material("crontab")
        large["citations"][0]["source"]["title"] = "ñ" * 30000
        db = build_fixture(Path(self.tmp.name), [large])
        service = MaterialService(Settings(database=db))
        self.addAsyncCleanup(service.close)
        with self.assertRaisesRegex(MaterialError, "search_response_too_large"):
            await service.search("crontab", mode="lexical")
        with self.assertRaisesRegex(MaterialError, "read_response_too_large"):
            await service.read(large["id"])

    async def test_context_character_budget(self):
        await self.service.close()
        long = material("x" * 17000)
        self.first["citations"][0]["next_chunk_id"] = long["id"]
        db = build_fixture(Path(self.tmp.name), [self.first, long])
        service = MaterialService(Settings(database=db))
        self.addAsyncCleanup(service.close)
        result = await service.read(self.first["id"])
        self.assertEqual(result["context"], [])
        self.assertEqual(result["context_omitted"], 1)
        self.assertLessEqual(size_bytes(result), MAX_OUTPUT_BYTES)

    async def test_token_subjects_limit_search_read_and_catalog(self):
        service = MaterialService(Settings(database=self.db, allowed_subjects=frozenset({"ASO", "FOL"})))
        self.addAsyncCleanup(service.close)
        with self.assertRaisesRegex(MaterialError, "subject_not_available"):
            await service.search("crontab", subject="FOL", mode="lexical", subjects={"ASO"})
        catalog = await service.catalog(subjects={"ASO"})
        self.assertEqual([item["name"] for item in catalog["subjects"]], ["ASO"])
        with self.assertRaisesRegex(MaterialError, "subject_not_available"):
            await service.read(self.foreign["id"], subject="FOL", subjects={"ASO"})

    async def test_token_cannot_expand_instance_subjects(self):
        with self.assertRaisesRegex(MaterialError, "subject_not_available"):
            await self.service.search("crontab", subject="FOL", mode="lexical", subjects={"ASO", "FOL"})
        catalog = await self.service.catalog(subjects={"ASO", "FOL"})
        self.assertEqual([item["name"] for item in catalog["subjects"]], ["ASO"])

    async def test_subject_aliases_resolve_before_authorization(self):
        service = MaterialService(Settings(database=self.db, allowed_subjects=frozenset({"ASO", "Bases de Datos", "Digitalizacion"})))
        self.addAsyncCleanup(service.close)
        for alias in ("Xestión de Bases de Datos", "Gestión de Bases de Datos", "BBDD"):
            self.assertEqual(service.authorize(alias, {"Bases de Datos"}), "Bases de Datos")
            with self.assertRaisesRegex(MaterialError, "subject_not_available"):
                service.authorize(alias, {"ASO"})
        self.assertEqual(service.authorize("Digitalización", {"Digitalizacion"}), "Digitalizacion")

    async def test_catalog_uses_metadata_aggregation_when_available(self):
        engine = self.service.retriever.engine
        allowed_seen = []
        engine.source_catalog = lambda allowed: allowed_seen.append(allowed) or [{"name": "ASO", "units": ["UD1"], "sources": 1}]
        result = await self.service.catalog(subjects={"ASO", "FOL"})
        self.assertEqual(allowed_seen, [frozenset({"ASO"})])
        self.assertEqual(result["subjects"][0]["sources"], 1)

    async def test_pending_limit_rejects_instead_of_unbounded_queue(self):
        class Slow:
            class Engine:
                metadata = {"corpus_sha256": "x" * 64}

            engine = Engine()

            def search(self, *args):
                time.sleep(0.4)
                return []

            def close(self):
                pass

        service = MaterialService(Settings(database=self.db, max_pending=1), retriever_factory=lambda: Slow())
        self.addAsyncCleanup(service.close)
        await service.start()
        first = asyncio.create_task(service.search("cron", mode="lexical"))
        await asyncio.sleep(0.05)
        with self.assertRaisesRegex(MaterialError, "server_busy"):
            await service.search("cron", mode="lexical")
        await first


class LocationTests(unittest.TestCase):
    def test_compact_ranges_do_not_claim_gaps_or_other_pages(self):
        segments = [{"start": n * 10, "end": n * 10 + 10, "locator": {"page": page}} for n, page in enumerate([1, 2, 4])]
        self.assertEqual(compact_locations(segments), [{"kind": "page", "start": 1, "end": 2}, {"kind": "page", "start": 4, "end": 4}])
        self.assertEqual(compact_locations(segments, 10, 20), [{"kind": "page", "start": 2, "end": 2}])
