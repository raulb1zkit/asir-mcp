import json
from pathlib import Path
import tempfile
import unittest

from scripts.material_service import MaterialError, Settings
from test_search_material import build_fixture, chunk

try:
    from mcp import Client
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    from scripts.mcp_server import create_server, token_subjects
except ImportError:
    Client = None


@unittest.skipIf(Client is None, "Install requirements-mcp.txt to test MCP")
class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.record = chunk("renice cambia la prioridad de procesos. Dato ficticio de prueba.")
        self.db = build_fixture(Path(self.tmp.name), [self.record])
        self.settings = Settings(database=self.db)

    async def test_discovery_schema_resource_and_tool_roundtrip(self):
        async with Client(create_server(self.settings)) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({t.name for t in tools}, {"buscar_material", "leer_fragmento"})
            for tool in tools:
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.open_world_hint)
                self.assertNotIn("ctx", tool.input_schema["properties"])
            resource = await client.read_resource("asir://catalogo")
            self.assertEqual(json.loads(resource.contents[0].text)["subjects"][0]["name"], "ASO")
            found = await client.call_tool("buscar_material", {"consulta": "prioridad", "modo": "lexical"})
            self.assertFalse(found.is_error)
            record_id = found.structured_content["results"][0]["fragment_id"]
            read = await client.call_tool("leer_fragmento", {"fragmento_id": record_id, "vecinos": 0})
            self.assertFalse(read.is_error)
            self.assertEqual(read.structured_content["fragment"]["text"], self.record["text"])

    async def test_protocol_rejects_types_bounds_paths_and_scope(self):
        async with Client(create_server(self.settings)) as client:
            for args in [{"consulta": "cron", "limite": 999}, {"consulta": "cron", "limite": True}, {"consulta": "cron", "solo_nativo": "false"}, {"consulta": "cron", "asignatura": "FOL"}, {"consulta": "x" * 2001}]:
                result = await client.call_tool("buscar_material", args)
                self.assertTrue(result.is_error, args)
            result = await client.call_tool("leer_fragmento", {"fragmento_id": "../../.private/moodle-session.json"})
            self.assertTrue(result.is_error)

    async def test_legacy_client(self):
        async with Client(create_server(self.settings), mode="legacy") as client:
            found = await client.call_tool("buscar_material", {"consulta": "prioridad", "modo": "lexical"})
            self.assertFalse(found.is_error)

    def test_token_subjects_are_scoped_and_missing_claims_are_rejected(self):
        empty = auth_context_var.set(None)
        try:
            self.assertIsNone(token_subjects())
        finally:
            auth_context_var.reset(empty)
        access = AccessToken(token="", client_id="tok", scopes=["asir:read"], claims={"subjects": ["ASO"]})
        token = auth_context_var.set(AuthenticatedUser(access))
        try:
            self.assertEqual(token_subjects(), frozenset({"ASO"}))
        finally:
            auth_context_var.reset(token)
        bad = AccessToken(token="", client_id="tok", scopes=["asir:read"], claims={"subjects": []})
        token = auth_context_var.set(AuthenticatedUser(bad))
        try:
            with self.assertRaisesRegex(MaterialError, "subject_not_available"):
                token_subjects()
        finally:
            auth_context_var.reset(token)

    async def test_unexpected_backend_errors_do_not_expose_local_details(self):
        class Broken:
            def search(self, *args):
                raise RuntimeError("failed at /private/original.pdf")
            def close(self):
                pass
        async with Client(create_server(self.settings, retriever_factory=Broken)) as client:
            result = await client.call_tool("buscar_material", {"consulta": "prioridad"})
            self.assertTrue(result.is_error)
            encoded = json.dumps([c.model_dump() for c in result.content])
            self.assertIn("local_search_unavailable", encoded)
            self.assertNotIn("/private/", encoded)
