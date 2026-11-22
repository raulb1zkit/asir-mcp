import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import sync_materials as sync
import hashlib
from scripts.course_registry import Course, SourceSpec


class ScheduleTests(unittest.TestCase):
    def test_spring_missing_hour_recovered_at_three(self):
        # 02:00 no existe el 29/03/2026; 01:00Z es 03:00 Madrid.
        self.assertFalse(sync.due({}, datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)))
        self.assertTrue(sync.due({}, datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)))

    def test_autumn_repeated_hour_once(self):
        state = {"successful_date": "2026-10-25"}
        for hour in [0, 1, 2]:
            self.assertFalse(sync.due(state, datetime(2026, 10, 25, hour, 30, tzinfo=timezone.utc)))

    def test_reboot_catchup_and_next_day(self):
        state = {"successful_date": "2026-11-13"}
        self.assertTrue(sync.due(state, datetime(2026, 11, 14, 14, tzinfo=timezone.utc)))

    def test_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with sync.exclusive_lock(path):
                with self.assertRaisesRegex(sync.SyncFailure, "another_sync"):
                    with sync.exclusive_lock(path):
                        pass
            with sync.exclusive_lock(path):
                pass


class DeltaActivationTests(unittest.TestCase):
    def test_publication_requires_explicit_migration_switch_before_build(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            with patch('sys.argv', ['sync_materials.py', '--execute', '--publish', '--state-dir', directory]), patch.object(sync, 'build') as build, contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(sync.main(), 1)
            build.assert_not_called()
            self.assertIn('rag_migration_activation_required', output.getvalue())


class PipelineTests(unittest.TestCase):
    def test_resume_prefers_verified_ocr_from_same_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "runs/current/aso/ocr/all/report.json"
            previous = root / "data/aso/ocr/all/report.json"
            current.parent.mkdir(parents=True)
            previous.parent.mkdir(parents=True)
            current.write_text("{}")
            previous.write_text("{}")
            self.assertEqual(sync.ocr_reuse_report(current.parents[2], root / "data", "aso", True), current)
            self.assertEqual(sync.ocr_reuse_report(current.parents[2], root / "data", "aso", False), previous)

    def test_removal_check_requires_previous_source_to_be_preserved(self):
        old = {"items": [{"url": "old", "sha256": "a"}]}
        with self.assertRaisesRegex(sync.SyncFailure, "preservation_incomplete"):
            sync.check_removals(old, {"items": []})
        sync.check_removals(old, {"items": [{"url": "old", "sha256": "a", "status": "downloaded"}]})

    def test_missing_source_is_copied_and_marked_inactive(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_root, new_root = root / "old/moodle", root / "new/moodle"
            payload = b"preserve this source"
            sha = hashlib.sha256(payload).hexdigest()
            relative = Path("objects") / sha[:2] / sha
            old_object = old_root / relative
            old_object.parent.mkdir(parents=True)
            old_object.write_bytes(payload)
            old_item = {"url": "https://moodle.test/pluginfile.php/old.pdf", "sha256": sha, "path": relative.as_posix(), "bytes": len(payload), "status": "downloaded", "title": "Antiguo", "filename": "old.pdf", "section": "UD1", "mime": "application/pdf", "moodle_active": True}
            new_root.mkdir(parents=True)
            sync.save_json(old_root / "manifest.json", {"complete": True, "issues": [], "items": [old_item]})
            sync.save_json(new_root / "manifest.json", {"complete": True, "issues": [], "items": []})
            sync.save_json(new_root / "inventory.json", {"complete": True, "issues": [], "items": []})
            summary = sync.preserve_missing_sources(old_root / "manifest.json", new_root / "manifest.json", new_root / "inventory.json")
            current = json.loads((new_root / "manifest.json").read_text())
            self.assertEqual(summary["inactive_preserved_sources"], 1)
            self.assertEqual(len(current["items"]), 1)
            self.assertIs(current["items"][0]["moodle_active"], False)
            self.assertTrue(current["items"][0]["preserved_from_previous"])
            self.assertEqual((new_root / relative).read_bytes(), payload)

    def test_changed_file_at_same_url_replaces_consultable_revision(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_root, new_root = root / "old/moodle", root / "new/moodle"
            url = "https://moodle.test/pluginfile.php/doc.pdf"
            old_payload, new_payload = b"old version", b"new version"
            items = []
            for out, payload in ((old_root, old_payload), (new_root, new_payload)):
                sha = hashlib.sha256(payload).hexdigest()
                rel = Path("objects") / sha[:2] / sha
                target = out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                items.append({"url": url, "sha256": sha, "path": rel.as_posix(), "bytes": len(payload), "status": "downloaded", "title": "Guia", "filename": "doc.pdf", "section": "UD1", "mime": "application/pdf"})
            sync.save_json(old_root / "manifest.json", {"complete": True, "issues": [], "items": [items[0]]})
            sync.save_json(new_root / "manifest.json", {"complete": True, "issues": [], "items": [items[1]]})
            sync.save_json(new_root / "inventory.json", {"complete": True, "issues": [], "items": [{"url": url, "status": "discovered"}]})
            sync.preserve_missing_sources(old_root / "manifest.json", new_root / "manifest.json", new_root / "inventory.json")
            report = json.loads((new_root / "manifest.json").read_text())
            self.assertEqual([item["sha256"] for item in report["items"]], [items[1]["sha256"]])
            self.assertTrue((old_root / items[0]["path"]).is_file())
            self.assertFalse((new_root / items[0]["path"]).exists())

    def test_explicitly_excluded_sources_are_not_preserved(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_root, new_root = root / "old/moodle", root / "new/moodle"
            payload = b"excluded"
            sha = hashlib.sha256(payload).hexdigest()
            rel = Path("objects") / sha[:2] / sha
            target = old_root / rel
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            item = {"url": "https://moodle.test/mod/assign/view.php?id=5", "sha256": sha, "path": rel.as_posix(), "bytes": len(payload), "status": "downloaded"}
            new_root.mkdir(parents=True)
            sync.save_json(old_root / "manifest.json", {"complete": True, "issues": [], "items": [item]})
            sync.save_json(new_root / "manifest.json", {"complete": True, "issues": [], "items": [{"url": item["url"], "status": "excluded"}]})
            sync.save_json(new_root / "inventory.json", {"complete": True, "issues": [], "items": []})
            summary = sync.preserve_missing_sources(old_root / "manifest.json", new_root / "manifest.json", new_root / "inventory.json")
            self.assertEqual(summary["excluded_previous_sources"], 1)
            self.assertEqual(summary["preserved_sources"], 0)
            self.assertFalse((new_root / rel).exists())

    def test_partial_report_blocks_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for value in [{"complete": False}, {"complete": True, "issues": ["error"]}, {"complete": True, "errors": ["bad"]}]:
                sync.save_json(path, value)
                with self.assertRaises(sync.SyncFailure):
                    sync.checked_report(path)

    def test_failed_command_stops_and_does_not_log_sensitive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = sync.Runner(Path(directory))
            with patch.object(sync.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, b"secret", b"secret")):
                with self.assertRaisesRegex(sync.SyncFailure, "step_failed"):
                    runner.run(["python", "download.py"])
            logged = (Path(directory) / "steps.json").read_text()
            self.assertNotIn("secret", logged)
            self.assertFalse(json.loads(logged)[0]["complete"])

    def test_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sync.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)):
                with self.assertRaisesRegex(sync.SyncFailure, "step_timeout"):
                    sync.Runner(Path(directory)).run(["python", "download.py"])

    def test_build_stops_before_processing_after_download_failure(self):
        class FailingRunner:
            def __init__(self): self.commands = []
            def run(self, argv):
                self.commands.append(argv)
                raise sync.SyncFailure("download failed")
        course = Course("aso", "ASO", "https://moodle.example.edu/course/view.php?id=1001")
        runner = FailingRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.moodle_auth.ensure_session"):
                with self.assertRaises(sync.SyncFailure):
                    sync.build([course], root / "run", root / "data", root / "session", root / "tessdata", runner)
            self.assertEqual(len(runner.commands), 1)
            self.assertNotIn("--resume", runner.commands[0])

    def test_build_regenerates_archives_and_preserves_manuals(self):
        class FakeRunner:
            def __init__(self): self.commands = []
            def run(self, argv):
                self.commands.append(argv)
                if "moodle_download.py" in argv[1]:
                    out = Path(argv[argv.index("--output") + 1])
                    sync.save_json(out / ("inventory.json" if "inventory" in argv else "manifest.json"), {"complete": True, "items": [{"url": "x", "sha256": "a"}]})
                if "expand_archives.py" in argv[1]:
                    out = Path(argv[argv.index("--output") + 1])
                    sync.save_json(out / "manifest.json", {"complete": True, "items": [{"url": "a", "sha256": "b"}]})
                if "merge_corpora.py" in argv[1]:
                    out = Path(argv[argv.index("--output") + 1])
                    sync.save_json(out / "report.json", {"complete": True})
        course = Course("iaw", "IAW", "https://moodle.example.edu/course/view.php?id=1001", preserve_sources=(SourceSpec("archives", "archives", "iaw/archives/manifest.json", "iaw/archives-chunks"), SourceSpec("manual", "manuals", "iaw/manuals/manifest.json", "iaw/manuals-chunks")))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sync.save_json(root / "data/iaw/manuals-chunks/report.json", {"complete": True})
            runner = FakeRunner()
            with patch("scripts.moodle_auth.ensure_session"):
                sync.build([course], root / "run", root / "data", root / "session", root / "tessdata", runner)
            merge = runner.commands[-1]
            self.assertIn(str(root / "data/iaw/manuals-chunks"), merge)
            self.assertIn(str(root / "run/iaw/archives-chunks"), merge)
            self.assertFalse(any("--resume" in c for c in runner.commands))

class RealDerivationTests(unittest.TestCase):
    def test_plain_document_runs_real_extraction_ocr_chunk_and_audit_clis(self):
        import hashlib
        import shutil
        required = ("pdftotext", "pdfinfo", "pdfimages", "tesseract")
        tessdata = sync.ROOT / "data/aso/tools/tessdata"
        if any(shutil.which(name) is None for name in required) or not (tessdata / "spa.traineddata").exists():
            self.skipTest("local extraction prerequisites absent")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "moodle/object.txt"
            source.parent.mkdir()
            content = b"DNS proporciona resolucion de nombres. Este es un documento de prueba local.\n"
            source.write_bytes(content)
            sync.save_json(root / "moodle/manifest.json", {"complete": True, "issues": [], "items": [{"status": "downloaded", "sha256": hashlib.sha256(content).hexdigest(), "path": "object.txt", "url": "https://example.test/file", "title": "Prueba DNS", "section": "Unidad 1", "mime": "text/plain", "filename": "object.txt", "bytes": len(content)}]})
            runner = sync.Runner(root / "log")
            for argv in sync.derivation_commands(root / "moodle/manifest.json", root, "SRI", tessdata):
                runner.run(argv)
            report = sync.checked_report(root / "chunks/report.json")
            self.assertEqual(report["summary"]["chunks"], 1)
            sync.checked_report(root / "audit.json")
            try:
                import numpy as np
                import onnxruntime
                import tokenizers
            except ImportError:
                return  # CLI pipeline remains checked on minimal stdlib environments.
            model = sync.ROOT / "data/models/multilingual-e5-small"
            if not (model / "model.json").exists():
                return
            first = sync.build_vectors(root / "chunks", root / "index-one", model)
            with np.load(first, allow_pickle=False) as bundle:
                first_metadata = json.loads(str(bundle["metadata"]))
            self.assertGreater(first_metadata["summary"]["new_windows"], 0)
            second = sync.build_vectors(root / "chunks", root / "index-two", model, root / "index-one/lexical.sqlite", first)
            with np.load(second, allow_pickle=False) as bundle:
                metadata = json.loads(str(bundle["metadata"]))
            self.assertGreater(metadata["summary"]["reused_windows"], 0)
            self.assertEqual(metadata["summary"]["new_windows"], 0)
            # La primera edición tras bootstrap legacy conserva su prefijo y
            # reutiliza los vectores en lugar de migrar todo implícitamente.
            legacy = sync.build_vectors(root / 'chunks', root / 'index-legacy', model,
                                        embedding_context='subject-section-title')
            inherited = sync.build_vectors(root / 'chunks', root / 'index-inherited', model,
                                           legacy.parent / 'lexical.sqlite', legacy)
            with np.load(inherited, allow_pickle=False) as bundle:
                inherited_metadata = json.loads(str(bundle['metadata']))
            self.assertEqual(inherited_metadata['context'], 'subject-section-title')
            self.assertEqual(inherited_metadata['summary']['new_windows'], 0)
            self.assertEqual(inherited_metadata['summary']['reused_windows'], first_metadata['summary']['windows'])
            with patch("scripts.semantic_search.LocalEncoder", side_effect=AssertionError("completed index must not load ONNX")):
                resumed = sync.build_vectors(root / "chunks", root / "index-two", model)
            self.assertEqual(resumed, second)
            # Un índice manipulado no puede reutilizarse aunque el vectorial sea válido.
            import shutil
            import sqlite3
            tampered = root / "index-tampered"
            shutil.copytree(second.parent, tampered)
            with sqlite3.connect(tampered / "lexical.sqlite") as database:
                database.execute("UPDATE chunks SET text='alterado' WHERE rowid=1")
            with patch("scripts.semantic_search.LocalEncoder", side_effect=AssertionError("tampered index must be rebuilt")):
                with self.assertRaises(AssertionError):
                    sync.build_vectors(root / "chunks", tampered, model)

class RemoteReadinessTests(unittest.TestCase):
    def setUp(self):
        # Importar el módulo asegura que patch('scripts.check_remote_mcp...')
        # resuelva el atributo del paquete (pkgutil.resolve_name no importa en
        # Python 3.14). Sin httpx2 instalado la prueba se omite: falta de
        # dependencia del entorno, no un fallo de lógica.
        try:
            import scripts.check_remote_mcp  # noqa: F401
        except ImportError as error:
            self.skipTest("sin httpx2 (%s)" % error)

    def test_retries_until_restarted_endpoint_answers(self):
        calls = []

        async def status(url):
            calls.append(url)
            if len(calls) < 3:
                raise ConnectionError("refused")
            return 401

        async def no_sleep(_):
            pass

        with patch("scripts.check_remote_mcp.unauthorized_status", status), patch("scripts.sync_materials.asyncio.sleep", no_sleep):
            asyncio.run(sync.wait_remote_ready("http://127.0.0.1:1/mcp", timeout=5))
        self.assertEqual(len(calls), 3)

    def test_reports_endpoint_that_never_accepts_requests(self):
        async def status(url):
            return 503

        async def no_sleep(_):
            pass

        class Clock:
            value = 0

            def monotonic(self):
                Clock.value += 1
                return Clock.value

        with patch("scripts.check_remote_mcp.unauthorized_status", status), patch("scripts.sync_materials.asyncio.sleep", no_sleep), patch("scripts.sync_materials.time", Clock()):
            with self.assertRaisesRegex(sync.SyncFailure, "remote_mcp_not_ready"):
                asyncio.run(sync.wait_remote_ready("http://127.0.0.1:1/mcp", timeout=1))


class PublishTests(unittest.TestCase):
    def test_postcommit_failure_restores_verified_previous_candidate(self):
        from contextlib import nullcontext
        new = {"report": {"output_sha256": "new"}}
        old = {"report": {"output_sha256": "old"}}
        with patch("scripts.import_supabase.load_local", side_effect=[new, old]), patch("scripts.supabase_db.ingest_config", return_value=object()), patch("scripts.supabase_db.connect", side_effect=lambda *a, **k: nullcontext(object())), patch("scripts.import_supabase.verify_corpus") as verify, patch("scripts.import_supabase.import_corpus") as apply:
            restart = unittest.mock.Mock()
            probe = unittest.mock.Mock(side_effect=RuntimeError("HTTP endpoint failed"))
            with self.assertRaisesRegex(sync.SyncFailure, "previous_corpus_restored"):
                sync.publish_candidate(Path("new"), Path("old"), Path("model"), Path("db"), ["ASO"], restart, probe, legacy_full_publish=True)
            self.assertEqual([call.args[1] for call in apply.call_args_list], [new, old])
            self.assertEqual(restart.call_count, 2)
            self.assertEqual(verify.call_args_list[-1].args[1], old)

    def test_backup_mismatch_does_not_modify_production(self):
        from contextlib import nullcontext
        with patch("scripts.import_supabase.load_local", return_value={"report": {}}), patch("scripts.supabase_db.ingest_config", return_value=object()), patch("scripts.supabase_db.connect", return_value=nullcontext(object())), patch("scripts.import_supabase.verify_corpus", side_effect=ValueError("mismatch")), patch("scripts.import_supabase.import_corpus") as apply:
            with self.assertRaises(ValueError):
                sync.publish_candidate(Path("new"), Path("old"), Path("model"), Path("db"), ["ASO"], lambda: None, lambda: None, legacy_full_publish=True)
            apply.assert_not_called()

    def test_stage_does_not_suppress_publish_same_day(self):
        state = {"staged_date": "2026-11-14", "successful_date": "2026-11-14"}
        now = datetime(2026, 11, 14, 5, tzinfo=timezone.utc)
        self.assertFalse(sync.due(state, now, "staged"))
        self.assertTrue(sync.due(state, now, "published"))


class SensitiveInventoryTests(unittest.TestCase):
    def test_grades_require_review_before_download(self):
        with self.assertRaisesRegex(sync.SyncFailure, "sensitive_resource"):
            sync.review_inventory({"items": [{"title": "Cualificacións da avaliación", "status": "discovered"}]})
        sync.review_inventory({"items": [{"title": "Notas", "status": "excluded"}]})


class FinalizePublicationTests(unittest.TestCase):
    def test_durable_pointer_exists_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            status = {"complete": True, "publication": True, "published_candidate": "/candidate/new", "step": "published"}
            def cleanup():
                saved = sync.read_json(path)
                self.assertEqual(saved["published_candidate"], "/candidate/new")
                self.assertTrue(saved["complete"])
                self.assertEqual(saved["staging_cleanup"]["status"], "pending")
                return {"complete": True, "tables_emptied": 8}
            sync.finish_publication(path, status, cleanup)
            self.assertTrue(sync.read_json(path)["staging_cleanup"]["complete"])

    def test_cleanup_failure_is_warning_and_preserves_published_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            status = {"complete": True, "publication": True, "published_candidate": "/candidate/new", "step": "published"}
            def cleanup():
                raise RuntimeError("sensitive database driver output")
            sync.finish_publication(path, status, cleanup)
            saved = sync.read_json(path)
            self.assertTrue(saved["complete"])
            self.assertTrue(saved["publication"])
            self.assertEqual(saved["step"], "published")
            self.assertEqual(saved["staging_cleanup"], {"complete": False, "status": "warning", "error": "RuntimeError"})
            self.assertNotIn("sensitive", path.read_text())

    def test_no_cleanup_without_completed_publication(self):
        cleaner = unittest.mock.Mock()
        with self.assertRaisesRegex(sync.SyncFailure, "requires_successful"):
            sync.finish_publication(Path("unused"), {"complete": True, "publication": False}, cleaner)
        cleaner.assert_not_called()

    def test_publish_existing_does_not_download_or_rebuild(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            args = ["sync_materials.py", "--execute", "--legacy-full-publish", "--staging", "--publish", "--publish-candidate", str(candidate), "--rollback-candidate", str(root / "bootstrap"), "--probe-token-file", str(root / "token"), "--state-dir", str(root / "state")]
            with patch("sys.argv", args), patch.object(sync, "check_existing_candidate", return_value=candidate) as verify_local, patch.object(sync, "validate_staging", return_value={"complete": True}) as verify_stage, patch.object(sync, "publish_candidate", return_value="hash"), patch.object(sync, "clear_staging", return_value={"complete": True}), patch.object(sync, "space_preflight", return_value={"allowed": True, "warning": False}), patch.object(sync, "build") as build, patch.object(sync, "build_vectors") as vectors, patch.object(sync.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(sync.Runner, "run") as commands, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sync.main(), 0)
            build.assert_not_called()
            vectors.assert_not_called()
            commands.assert_not_called()
            verify_local.assert_called_once()
            self.assertTrue(verify_stage.call_args.kwargs["verify_only"])
            status = sync.read_json(root / "state/status.json")
            self.assertEqual(status["published_candidate"], str(candidate))
            self.assertTrue(status["staging_cleanup"]["complete"])


class ClearStagingTests(unittest.TestCase):
    def test_only_exact_staging_tables_truncated_and_verified(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest("project venv psycopg needed")
        from contextlib import nullcontext
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.side_effect = [{"corpus_sha256": "served"}] + [{"n": 0}] * 8 + [{"corpus_sha256": "served"}]
        connection = unittest.mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch("scripts.supabase_db.ingest_config", return_value=object()), patch("scripts.supabase_db.connect", return_value=nullcontext(connection)):
            result = sync.clear_staging(Path("config"))
        self.assertTrue(result["complete"])
        queries = [call.args[0].as_string() if hasattr(call.args[0], "as_string") else call.args[0] for call in cursor.execute.call_args_list]
        truncate = [q for q in queries if q.startswith("TRUNCATE")]
        self.assertEqual(len(truncate), 1)
        self.assertEqual(truncate[0].count('asir_staging.'), 8)
        self.assertIn("CONTINUE IDENTITY RESTRICT", truncate[0])
        self.assertNotIn("CASCADE", truncate[0])
        self.assertNotIn("TRUNCATE asir.", truncate[0])


class LocalRetentionTests(unittest.TestCase):
    def test_preserves_current_previous_bootstrap_and_three_newest_other_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runs = state / "runs"
            runs.mkdir()
            paths = []
            for day in range(1, 9):
                path = runs / f"202611{day:02d}T020000000000Z"
                path.mkdir()
                (path / "cache").write_text("generated")
                paths.append(path)
            bootstrap = state / "bootstrap"
            bootstrap.mkdir()
            (bootstrap / "original").write_text("recoverable")
            result = sync.prune_runs(state, paths[0], paths[1])
            self.assertEqual(result["deleted_runs"], 3)
            self.assertEqual({p.name for p in runs.iterdir()}, {paths[i].name for i in (0, 1, 5, 6, 7)})
            self.assertEqual((bootstrap / "original").read_text(), "recoverable")

    def test_symlinks_unknown_entries_and_external_targets_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runs = state / "runs"
            runs.mkdir()
            current = runs / "20261114T020000000000Z"
            current.mkdir()
            external = state / "manuals"
            external.mkdir()
            (external / "document").write_text("keep")
            link = runs / "20261101T020000000000Z"
            link.symlink_to(external, target_is_directory=True)
            unknown = runs / "do-not-touch"
            unknown.mkdir()
            disposable = runs / "20261102T020000000000Z"
            disposable.mkdir()
            (disposable / "nested-symlink").symlink_to(external, target_is_directory=True)
            sync.prune_runs(state, current, keep_other=0)
            self.assertTrue(link.is_symlink())
            self.assertTrue(unknown.exists())
            self.assertFalse(disposable.exists())
            self.assertEqual((external / "document").read_text(), "keep")

    def test_rejects_symlinked_runs_directory_and_external_current(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            external = state / "outside"
            external.mkdir()
            (state / "runs").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(sync.SyncFailure, "symlink_forbidden"):
                sync.prune_runs(state, external)
            (state / "runs").unlink()
            (state / "runs").mkdir()
            with self.assertRaisesRegex(sync.SyncFailure, "outside_managed_runs"):
                sync.prune_runs(state, external)

    def test_previous_bootstrap_outside_runs_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            current = state / "runs/20261114T020000000000Z"
            current.mkdir(parents=True)
            bootstrap = state / "bootstrap"
            bootstrap.mkdir()
            result = sync.prune_runs(state, current, bootstrap)
            self.assertTrue(result["complete"])
            self.assertTrue(bootstrap.exists())

    def test_retention_failure_warns_after_durable_current_and_previous_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status.json"
            status = {"complete": True, "publication": True, "published_candidate": "new", "previous_candidate": "old", "step": "published"}
            def fail_retention():
                saved = sync.read_json(state_path)
                self.assertEqual(saved["published_candidate"], "new")
                self.assertEqual(saved["previous_candidate"], "old")
                raise OSError("driver path details")
            sync.finish_publication(state_path, status, lambda: {"complete": True}, fail_retention)
            saved = sync.read_json(state_path)
            self.assertTrue(saved["complete"])
            self.assertEqual(saved["step"], "published")
            self.assertEqual(saved["local_retention"]["status"], "warning")
            self.assertNotIn("driver path details", state_path.read_text())


class ResourceMapRefreshTests(unittest.TestCase):
    """Refresco determinista del registro por candidato en prepare_delta (docs/45)."""

    HOST = "https://campus.example.es"
    COURSE_URL = HOST + "/course/view.php?id=7"

    @staticmethod
    def source_row(url, sha, filename, mime, *, subject="ASO", active=True):
        return {"subject": subject, "url": url, "sha256": sha, "filename": filename, "mime": mime,
                "title": "Guia " + filename, "section": "UD1", "path": "objects/" + sha[:16],
                "moodle_active": active}

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def course_manifest(self, items, subject="ASO"):
        return {"version": 1, "course_url": self.COURSE_URL, "subject": subject, "complete": True, "issues": [], "items": items}

    def write_cycle(self, root, name, manifests, sources):
        """Directorio con manifiestos de curso y corpus fusionado del ciclo."""
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        for slug, manifest in manifests.items():
            self.write_json(directory / slug / "moodle" / "manifest.json", manifest)
        chunks = directory / "all" / "chunks"
        chunks.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"id": "chunk:%d" % n, "citations": [{"source": source}]}, ensure_ascii=False)
                 for n, source in enumerate(sources)]
        (chunks / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return directory

    def refresh(self, root, previous, candidate, *, external=None):
        map_path = root / "resource-registry.json"
        captured = []
        def fake_build_manifest(value, resource_map=None):
            captured.append(dict(resource_map or {}))
            return {"version": "v%d" % (len(captured) + 1)}
        with patch("scripts.import_supabase.load_local", return_value={}), \
             patch("scripts.rag_versions.build_manifest", fake_build_manifest), \
             patch("scripts.rag_versions.write_version", return_value=None), \
             patch("scripts.rag_versions.plan", return_value={"blocked": [], "counts": {}}):
            sync.prepare_delta(candidate, previous, "model", map_path, root / "versioning", external)
        return map_path, captured

    @staticmethod
    def key(source):
        from scripts.import_supabase import source_key
        fields = {name: source.get(name) for name in ("sha256", "subject", "section", "title", "filename", "url", "mime", "path")}
        return source_key(fields)

    def test_new_resource_next_sync_is_registered_and_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "resource-registry.json"
            alias = self.HOST + "/mod/page/view.php?id=101"
            map_path.write_text(json.dumps({alias: "moodle:c7:page:101"}), encoding="utf-8")
            old = self.source_row(alias, "a" * 64, "tema1.html", "text/html")
            new_url = self.HOST + "/mod/page/view.php?id=102"
            new = self.source_row(new_url, "b" * 64, "tema2.html", "text/html")
            previous = self.write_cycle(root, "previous", {}, [])
            candidate = self.write_cycle(root, "candidate", {"aso": self.course_manifest([
                {"url": alias, "sha256": "a" * 64, "filename": "tema1.html", "mime": "text/html"},
                {"url": new_url, "sha256": "b" * 64, "filename": "tema2.html", "mime": "text/html"}])}, [old, new])
            map_path, captured = self.refresh(root, previous, candidate)
            self.assertEqual(captured[1][alias], "moodle:c7:page:101")  # alias estable conservado
            self.assertEqual(captured[1][self.key(new)], "moodle:c7:page:102")  # recurso nuevo registrado
            persisted = json.loads(map_path.read_text())
            self.assertEqual(persisted[self.key(new)], "moodle:c7:page:102")
            self.assertEqual(persisted[alias], "moodle:c7:page:101")
            self.assertEqual(map_path.stat().st_mode & 0o777, 0o600)

    def test_edited_archive_member_keeps_same_identity_across_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "resource-registry.json"
            zip8 = self.HOST + "/pluginfile.php/55/mod_resource/content/8/pack.zip"
            zip9 = self.HOST + "/pluginfile.php/55/mod_resource/content/9/pack.zip"
            identity = "moodle:c7:pf:55:mod_resource/content/pack.zip#docs/uno.md"
            old = self.source_row(zip8, "m1" * 32, "docs/uno.md", "text/plain")
            new = self.source_row(zip9, "m2" * 32, "docs/uno.md", "text/plain")
            map_path.write_text(json.dumps({self.key(old): identity}), encoding="utf-8")
            container = lambda url, sha: {"url": url, "sha256": sha, "filename": "pack.zip", "mime": "application/g-zip"}
            previous = self.write_cycle(root, "previous", {"aso": self.course_manifest([container(zip8, "k" * 64)])}, [old])
            candidate = self.write_cycle(root, "candidate", {"aso": self.course_manifest([container(zip9, "l" * 64)])}, [new])
            map_path, captured = self.refresh(root, previous, candidate)
            # El miembro editado conserva la identidad lógica; los mapas de
            # ciclos separados evitan ver las dos revisiones como activas a la vez.
            self.assertEqual(captured[0][self.key(old)], identity)
            self.assertEqual(captured[1][self.key(new)], identity)
            persisted = json.loads(map_path.read_text())
            self.assertEqual(persisted[self.key(old)], identity)
            self.assertEqual(persisted[self.key(new)], identity)

    def test_stale_identity_conflict_fails_closed_without_rewriting_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "resource-registry.json"
            alias = self.HOST + "/mod/page/view.php?id=101"
            static = {alias: "moodle:c9:page:101"}
            map_path.write_text(json.dumps(static), encoding="utf-8")
            original = map_path.read_bytes()
            stale = self.source_row(alias, "a" * 64, "tema1.html", "text/html")
            previous = self.write_cycle(root, "previous", {}, [])
            candidate = self.write_cycle(root, "candidate", {"aso": self.course_manifest([
                {"url": alias, "sha256": "a" * 64, "filename": "tema1.html", "mime": "text/html"}])}, [stale])
            with self.assertRaisesRegex(sync.SyncFailure, "rag_resource_identity_conflict"):
                self.refresh(root, previous, candidate)
            self.assertEqual(map_path.read_bytes(), original)  # sin escritura al fallar

    def test_legacy_map_only_callers_without_manifests_stay_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "resource-registry.json"
            static = {"clave-legacy": "moodle:c1:x:1"}
            map_path.write_text(json.dumps(static), encoding="utf-8")
            inode = map_path.stat().st_ino
            previous, candidate = root / "previous", root / "candidate"
            previous.mkdir()
            candidate.mkdir()
            map_path, captured = self.refresh(root, previous, candidate)
            self.assertEqual(captured[0], static)
            self.assertEqual(captured[1], static)
            self.assertEqual(map_path.stat().st_ino, inode)  # sin refresco no se reescribe

    def test_configured_refresh_without_course_manifests_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "resource-registry.json"
            map_path.write_text("{}", encoding="utf-8")
            external = root / "external-resources.json"
            external.write_text("{}", encoding="utf-8")  # refresco configurado y desplegado
            previous, candidate = root / "previous", root / "candidate"
            previous.mkdir()
            candidate.mkdir()
            with self.assertRaisesRegex(sync.SyncFailure, "rag_course_manifests_missing"):
                self.refresh(root, previous, candidate, external=external)

    def test_external_sources_need_explicit_url_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "resource-registry.json"
            map_path.write_text("{}", encoding="utf-8")
            drive = "https://drive.google.com/file/d/xyz/view"
            external_source = self.source_row(drive, "d" * 64, "externo.pdf", "application/pdf")
            external_item = {"url": drive, "sha256": "d" * 64, "filename": "externo.pdf", "mime": "application/pdf"}
            previous = self.write_cycle(root, "previous", {"aso": self.course_manifest([external_item])}, [external_source])
            candidate = self.write_cycle(root, "candidate", {"aso": self.course_manifest([external_item])}, [external_source])
            with self.assertRaisesRegex(sync.SyncFailure, "external_resource_unregistered"):
                self.refresh(root, previous, candidate, external=root / "missing.json")
            external = root / "external.json"
            external.write_text(json.dumps({drive: "external:manual-sri-1"}), encoding="utf-8")
            map_path, captured = self.refresh(root, previous, candidate, external=external)
            self.assertEqual(captured[1][drive], "external:manual-sri-1")


class PreservationLogicalIdentityTests(unittest.TestCase):
    """La conservación compara identidad lógica de Moodle, no URL exacta."""

    HOST = "https://campus.example.es"
    COURSE_URL = HOST + "/course/view.php?id=7"

    @staticmethod
    def object_item(manifest_root, url, filename, *, mime="application/pdf"):
        payload = ("contenido de " + filename).encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        relative = Path("objects") / sha[:2] / sha
        target = manifest_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return {"url": url, "sha256": sha, "path": relative.as_posix(), "bytes": len(payload),
                "status": "downloaded", "title": "Guia " + filename, "filename": filename,
                "section": "UD1", "mime": mime, "moodle_active": True}

    @staticmethod
    def reference_item(url, sha, filename, *, mime="application/pdf"):
        return {"url": url, "sha256": sha, "path": "objects/" + sha[:16], "bytes": 10,
                "status": "downloaded", "title": "Guia " + filename, "filename": filename,
                "section": "UD1", "mime": mime, "moodle_active": True}

    def preserve(self, root, previous_items, current_items, inventory_items=None):
        old_root, new_root = root / "old/moodle", root / "new/moodle"
        for manifest_root, items in ((old_root, previous_items), (new_root, current_items)):
            sync.save_json(manifest_root / "manifest.json", {
                "version": 1, "course_url": self.COURSE_URL, "subject": "ASO",
                "complete": True, "issues": [], "items": items})
        sync.save_json(new_root / "inventory.json", {"complete": True, "issues": [], "items": inventory_items or []})
        summary = sync.preserve_missing_sources(old_root / "manifest.json", new_root / "manifest.json", new_root / "inventory.json")
        return summary, json.loads((new_root / "manifest.json").read_text())

    def test_edited_revision_replaces_consultable_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url8 = self.HOST + "/pluginfile.php/55/mod_resource/content/8/guia.pdf"
            url9 = self.HOST + "/pluginfile.php/55/mod_resource/content/9/guia.pdf"
            old = self.object_item(root / "old/moodle", url8, "guia.pdf")
            new = self.reference_item(url9, "n" * 64, "guia.pdf")
            summary, manifest = self.preserve(root, [old], [new])
            self.assertEqual(summary["preserved_sources"], 0)
            # La revisión antigua no se arrastra como inactiva eterna: la
            # identidad lógica del recurso ya está en el ciclo actual.
            self.assertEqual([x["url"] for x in manifest["items"]], [url9])

    def test_disappeared_resource_is_preserved_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = self.HOST + "/pluginfile.php/55/mod_resource/content/8/practica.pdf"
            old = self.object_item(root / "old/moodle", url, "practica.pdf")
            summary, manifest = self.preserve(root, [old], [])
            self.assertEqual(summary["preserved_sources"], 1)
            self.assertEqual(len(manifest["items"]), 1)
            carried = manifest["items"][0]
            self.assertEqual(carried["url"], url)
            self.assertIs(carried["moodle_active"], False)
            self.assertTrue(carried["preserved_from_previous"])
            self.assertTrue((root / "new/moodle" / carried["path"]).is_file())

    def test_replaced_zip_container_spares_untouched_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack8 = self.HOST + "/pluginfile.php/55/mod_resource/content/8/pack.zip"
            pack9 = self.HOST + "/pluginfile.php/55/mod_resource/content/9/pack.zip"
            other = self.HOST + "/pluginfile.php/55/mod_resource/content/3/other.zip"
            old_pack = self.object_item(root / "old/moodle", pack8, "pack.zip", mime="application/g-zip")
            old_other = self.object_item(root / "old/moodle", other, "other.zip", mime="application/g-zip")
            new_pack = self.reference_item(pack9, "l" * 64, "pack.zip", mime="application/g-zip")
            summary, manifest = self.preserve(root, [old_pack, old_other], [new_pack, old_other])
            self.assertEqual(summary["preserved_sources"], 0)
            self.assertEqual(sorted(x["url"] for x in manifest["items"]), sorted([pack9, other]))
            self.assertFalse((root / "new/moodle" / old_pack["path"]).exists())

    def test_removal_check_accepts_logical_revision_replacement(self):
        url8 = self.HOST + "/pluginfile.php/55/mod_resource/content/8/guia.pdf"
        url9 = self.HOST + "/pluginfile.php/55/mod_resource/content/9/guia.pdf"
        old = {"course_url": self.COURSE_URL, "items": [{"url": url8, "sha256": "o" * 64}]}
        current = {"course_url": self.COURSE_URL, "items": [{"url": url9, "sha256": "n" * 64}]}
        sync.check_removals(old, current)  # no raise: sustitución lógica

    def test_removal_check_still_requires_preservation_for_disappearance(self):
        url = self.HOST + "/pluginfile.php/55/mod_resource/content/8/guia.pdf"
        old = {"course_url": self.COURSE_URL, "items": [{"url": url, "sha256": "o" * 64}]}
        current = {"course_url": self.COURSE_URL, "items": []}
        with self.assertRaisesRegex(sync.SyncFailure, "preservation_incomplete"):
            sync.check_removals(old, current)


class SyncFailureStatusTests(unittest.TestCase):
    """Aviso compacto sync_failed en rag-status.json sin borrar lo canónico."""

    @staticmethod
    def write_status(path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_sync_failure_marks_phase_and_preserves_canonical_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rag-status.json"
            self.write_status(path, {"phase": "published", "version": "v2", "published_version": "v2",
                                     "previous_version": "v1", "backup": "ok"})
            sync.record_sync_failure(path, sync.SyncFailure("step_failed:extract_text.py"))
            state = json.loads(path.read_text())
            self.assertEqual(state["phase"], "sync_failed")
            self.assertEqual(state["sync_error"], "step_failed:extract_text.py")
            self.assertEqual(state["published_version"], "v2")
            self.assertEqual(state["previous_version"], "v1")
            self.assertEqual(state["backup"], "ok")
            self.assertNotIn("pending_versions", state)

    def test_unresolved_publication_phase_and_pins_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rag-status.json"
            self.write_status(path, {"phase": "applying", "version": "v3", "published_version": "v2",
                                     "pending_versions": ["v3"]})
            sync.record_sync_failure(path, sync.SyncFailure("step_timeout:moodle_download.py"))
            state = json.loads(path.read_text())
            self.assertEqual(state["phase"], "applying")  # commit sin resolver manda
            self.assertEqual(state["pending_versions"], ["v3"])
            self.assertEqual(state["last_sync_error"], "step_timeout:moodle_download.py")
            self.assertNotIn("sync_error", state)

    def test_generic_exception_records_only_safe_type_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rag-status.json"
            self.write_status(path, {"phase": "published", "published_version": "v1"})
            sync.record_sync_failure(path, ValueError("secreto /var/lib/asir-sync/datos"))
            state = json.loads(path.read_text())
            self.assertEqual(state["phase"], "sync_failed")
            self.assertEqual(state["sync_error"], "ValueError")
            self.assertNotIn("secreto", path.read_text())

    def test_clear_on_unchanged_restores_published_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rag-status.json"
            self.write_status(path, {"phase": "sync_failed", "version": "v9", "sync_error": "step_failed:x",
                                     "published_version": "v2", "previous_version": "v1"})
            sync.clear_sync_failure(path)
            state = json.loads(path.read_text())
            self.assertEqual(state["phase"], "published")
            self.assertEqual(state["version"], "v2")
            self.assertIsNone(state["sync_error"])
            self.assertEqual(state["published_version"], "v2")
            self.assertEqual(state["previous_version"], "v1")

    def test_clear_leaves_other_phases_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rag-status.json"
            self.write_status(path, {"phase": "reverted", "version": "v1", "pending_versions": []})
            sync.clear_sync_failure(path)
            state = json.loads(path.read_text())
            self.assertEqual(state["phase"], "reverted")
            self.assertEqual(state["version"], "v1")


if __name__ == "__main__":
    unittest.main()


class ResumeTests(unittest.TestCase):
    def test_resume_only_recent_physical_managed_attempts(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/"runs";runs.mkdir()
            now=datetime(2026,11,14,18,tzinfo=timezone.utc)
            p=runs/"20261114T170000000000Z";p.mkdir()
            self.assertEqual(sync.validate_resume_run(p,root,now),p)
            with self.assertRaises(sync.SyncFailure):sync.validate_resume_run(p,root,now+timedelta(days=2))
            outside=root/"elsewhere";outside.mkdir()
            with self.assertRaises(sync.SyncFailure):sync.validate_resume_run(outside,root,now)
            link=runs/"20261114T160000000000Z";link.symlink_to(p,target_is_directory=True)
            with self.assertRaises(sync.SyncFailure):sync.validate_resume_run(link,root,now)
