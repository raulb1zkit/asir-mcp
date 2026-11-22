from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from scripts import sync_materials as sync
from scripts.course_registry import Course


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command_for(commands, script):
    return next(argv for argv in commands if Path(argv[1]).name == script)


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def write_corpus(directory, data, output_sha256=None):
    """Directorio con chunks.jsonl y un informe completo que lo describe."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chunks.jsonl").write_bytes(data)
    write_json(directory / "report.json", {"complete": True, "issues": [], "errors": [], "output_sha256": output_sha256 or hashlib.sha256(data).hexdigest()})
    return directory


class DerivationCommandTests(unittest.TestCase):
    def test_archive_commands_render_html_and_reuse_every_previous_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "previous"
            for relative in ("archives-extracted/all/report.json", "archives-ocr/all/report.json", "archives-chunks/report.json"):
                write_json(previous / relative, {"complete": True})
            ocr = previous / "archives-ocr/all/report.json"
            commands = sync.derivation_commands(root / "moodle/manifest.json", root / "run/iaw", "IAW", root / "tessdata", archives=True, reuse_report=ocr, previous_root=previous)

            extract = command_for(commands, "extract_text.py")
            self.assertIn("--render-html", extract)
            self.assertEqual(option(extract, "--reuse-report"), str(previous / "archives-extracted/all/report.json"))
            self.assertEqual(option(command_for(commands, "ocr_pages.py"), "--reuse-report"), str(ocr))
            chunks = command_for(commands, "chunk_text.py")
            self.assertEqual(option(chunks, "--reuse-report"), str(previous / "archives-chunks/report.json"))
            self.assertIn("--archive", command_for(commands, "audit_derived.py"))

    def test_native_commands_do_not_render_html_but_reuse_native_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "previous"
            for relative in ("extracted/all/report.json", "chunks/report.json"):
                write_json(previous / relative, {"complete": True})
            commands = sync.derivation_commands(root / "moodle/manifest.json", root / "run/aso", "ASO", root / "tessdata", previous_root=previous)

            extract = command_for(commands, "extract_text.py")
            self.assertNotIn("--render-html", extract)
            self.assertEqual(option(extract, "--reuse-report"), str(previous / "extracted/all/report.json"))
            chunks = command_for(commands, "chunk_text.py")
            self.assertEqual(option(chunks, "--reuse-report"), str(previous / "chunks/report.json"))
            self.assertNotIn("--reuse-report", command_for(commands, "ocr_pages.py"))
            self.assertNotIn("--archive", command_for(commands, "audit_derived.py"))

    def test_absent_previous_reports_do_not_add_reuse_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty-previous"
            empty.mkdir()
            commands = sync.derivation_commands(root / "moodle/manifest.json", root / "run", "IAW", root / "tessdata", archives=True, previous_root=empty)
            self.assertNotIn("--reuse-report", command_for(commands, "extract_text.py"))
            self.assertNotIn("--reuse-report", command_for(commands, "chunk_text.py"))
            self.assertNotIn("--reuse-report", command_for(commands, "ocr_pages.py"))


class UnchangedCorpusTests(unittest.TestCase):
    def test_different_output_hash_returns_false_before_touching_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = write_corpus(root / "previous/all/chunks", b'{"id": "old"}\n')
            current = write_corpus(root / "current/all/chunks", b'{"id": "new"}\n')
            with patch("scripts.semantic_search.Retriever", side_effect=AssertionError("no debe abrir el índice si cambió el corpus")):
                self.assertFalse(sync.unchanged_corpus(current, root / "previous", root / "model"))

    def test_missing_previous_candidate_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = write_corpus(root / "current/all/chunks", b'{"id": "same"}\n')
            self.assertFalse(sync.unchanged_corpus(current, None, root / "model"))

    def test_identical_corpus_validates_the_previous_index_without_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'{"id": "same"}\n'
            previous = write_corpus(root / "previous/all/chunks", data)
            current = write_corpus(root / "current/all/chunks", data)
            calls = {}

            class FakeRetriever:
                def __init__(self, database, model, index):
                    calls["arguments"] = (Path(database), Path(model), Path(index))

                def load_semantic(self):
                    calls["loaded"] = True

                def encode(self, *args, **kwargs):
                    raise AssertionError("no debe volver a codificar un corpus sin cambios")

                def close(self):
                    calls["closed"] = True

            with patch("scripts.semantic_search.Retriever", FakeRetriever):
                self.assertTrue(sync.unchanged_corpus(current, root / "previous", root / "model"))
            self.assertTrue(calls["loaded"])
            self.assertTrue(calls["closed"])
            self.assertEqual(calls["arguments"], (root / "previous/all/search/lexical.sqlite", root / "model", root / "previous/all/search/semantic.npz"))

    def test_corrupted_current_chunks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'{"id": "same"}\n'
            previous = write_corpus(root / "previous/all/chunks", data)
            current = write_corpus(root / "current/all/chunks", data)
            (current / "chunks.jsonl").write_bytes(b'{"id": "corrupted"}\n')
            with self.assertRaisesRegex(sync.SyncFailure, "hash_mismatch"):
                sync.unchanged_corpus(current, root / "previous", root / "model")

    def test_corrupted_previous_chunks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'{"id": "same"}\n'
            previous = write_corpus(root / "previous/all/chunks", data)
            current = write_corpus(root / "current/all/chunks", data)
            (previous / "chunks.jsonl").write_bytes(b'{"id": "tampered"}\n')
            with self.assertRaisesRegex(sync.SyncFailure, "hash_mismatch"):
                sync.unchanged_corpus(current, root / "previous", root / "model")

    def test_incomplete_previous_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'{"id": "same"}\n'
            previous = write_corpus(root / "previous/all/chunks", data)
            current = write_corpus(root / "current/all/chunks", data)
            write_json(previous / "report.json", {"complete": False, "output_sha256": hashlib.sha256(data).hexdigest()})
            with self.assertRaises(sync.SyncFailure):
                sync.unchanged_corpus(current, root / "previous", root / "model")


class MainUnchangedTests(unittest.TestCase):
    def test_unchanged_run_marks_date_and_never_stages_publishes_or_builds_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            runs = state_dir / "runs"
            published = runs / "20261115T020000000000Z"
            published.mkdir(parents=True)
            previous = root / "bootstrap"
            previous.mkdir()
            state_path = state_dir / "status.json"
            token = root / "probe-token"
            token.write_text("token")
            write_json(state_path, {"published_candidate": str(published), "previous_candidate": str(previous), "successful_date": "2026-11-15"})
            chunks = root / "current/all/chunks"
            write_json(chunks / "report.json", {"complete": True, "issues": [], "errors": [], "output_sha256": "same-corpus"})

            course = Course("iaw", "IAW", "https://moodle.example.edu/course/view.php?id=1001")
            args = ["sync_materials.py", "--execute", "--rag-delta-publish", "--staging", "--publish", "--probe-token-file", str(token), "--state-dir", str(state_dir), "--data-root", str(root / "data"), "--model", str(root / "model")]
            before = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
            with patch("scripts.course_registry.load_registry", return_value=[course]), patch.object(sync, "build", return_value=chunks) as build, patch.object(sync, "unchanged_corpus", return_value=True) as unchanged, patch.object(sync, "build_vectors") as build_vectors, patch.object(sync, "validate_staging") as validate_staging, patch.object(sync, "publish_candidate") as publish_candidate, patch.object(sync, "clear_staging") as clear_staging, patch.object(sync, "space_preflight", return_value={"allowed": True, "warning": False}), patch.object(sync.Runner, "run"), patch.object(sync, "smoke_remote", new=AsyncMock()), patch("sys.argv", args), redirect_stdout(io.StringIO()):
                self.assertEqual(sync.main(), 0)
            after = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()

            build.assert_called_once()
            unchanged.assert_called_once()
            build_vectors.assert_not_called()
            validate_staging.assert_not_called()
            publish_candidate.assert_not_called()
            clear_staging.assert_not_called()

            status = sync.read_json(state_path)
            self.assertTrue(status["complete"])
            self.assertEqual(status["step"], "unchanged")
            self.assertFalse(status["publication"])
            self.assertEqual(status["published_candidate"], str(published))
            self.assertEqual(status["candidate"], str(published))
            self.assertEqual(status["new_windows"], 0)
            self.assertIn(status["published_date"], {before, after})
            self.assertEqual(status["successful_date"], status["published_date"])


class MergeStabilityTests(unittest.TestCase):
    def chunk(self, text, citations, warnings=None, requires_review=False):
        sha = hashlib.sha256(text.encode()).hexdigest()
        return {"id": "chunk:" + sha, "text": text, "text_sha256": sha, "citations": citations, "warnings": warnings or [], "requires_review": requires_review}

    def citation(self, letter):
        return {"source": {"url": "https://moodle.test/" + letter, "title": letter.upper()}, "unit_id": letter * 8, "locator": {"block": 1}}

    def write_input(self, directory, chunks, reverse_keys=False, reverse_lines=False):
        lines = []
        for chunk in (reversed(chunks) if reverse_lines else chunks):
            if reverse_keys:
                chunk = dict(reversed(list(chunk.items())))
            lines.append(json.dumps(chunk, ensure_ascii=False))
        data = ("\n".join(lines) + "\n").encode("utf-8")
        write_json(directory / "report.json", {"complete": True, "output_sha256": hashlib.sha256(data).hexdigest()})
        (directory / "chunks.jsonl").write_bytes(data)
        return directory

    def test_input_chunk_citation_and_key_order_produce_the_same_hash(self):
        from scripts.merge_corpora import merge

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alpha = self.chunk("alpha content", [self.citation("a"), self.citation("b")], warnings=["note"], requires_review=True)
            beta = self.chunk("beta content", [self.citation("c")])
            alpha_again = self.chunk("alpha content", [self.citation("b"), self.citation("d"), self.citation("a")], warnings=["warning"], requires_review=False)
            first = self.write_input(root / "first", [alpha, beta])
            second = self.write_input(root / "second", [alpha_again], reverse_keys=True, reverse_lines=True)

            with redirect_stdout(io.StringIO()):
                merge([first, second], root / "forward")
                merge([second, first], root / "reverse")

            forward = (root / "forward/chunks.jsonl").read_bytes()
            reverse = (root / "reverse/chunks.jsonl").read_bytes()
            self.assertEqual(forward, reverse)
            forward_report = json.loads((root / "forward/report.json").read_text())
            reverse_report = json.loads((root / "reverse/report.json").read_text())
            self.assertEqual(forward_report["output_sha256"], reverse_report["output_sha256"])
            self.assertEqual(forward_report["output_sha256"], hashlib.sha256(forward).hexdigest())

            lines = forward.decode("utf-8").splitlines()
            parsed = [json.loads(line) for line in lines]
            self.assertEqual([chunk["id"] for chunk in parsed], sorted(chunk["id"] for chunk in parsed))
            for line, chunk in zip(lines, parsed):
                self.assertEqual(line, json.dumps(chunk, ensure_ascii=False, sort_keys=True))
            alpha_chunk = next(chunk for chunk in parsed if chunk["text"] == "alpha content")
            self.assertEqual(alpha_chunk["citations"], sorted(alpha_chunk["citations"], key=lambda citation: json.dumps(citation, sort_keys=True, ensure_ascii=False)))
            self.assertEqual([citation["source"]["url"] for citation in alpha_chunk["citations"]], ["https://moodle.test/a", "https://moodle.test/a", "https://moodle.test/b", "https://moodle.test/b", "https://moodle.test/d"])
            self.assertEqual(alpha_chunk["warnings"], ["note", "warning"])
            self.assertTrue(alpha_chunk["requires_review"])

            summary = forward_report["summary"]
            self.assertEqual(summary["chunks"], 2)
            self.assertEqual(summary["input_chunks"], 3)
            self.assertEqual(summary["duplicates_merged"], 1)
            self.assertEqual(summary["citations"], 6)


if __name__ == "__main__":
    unittest.main()
