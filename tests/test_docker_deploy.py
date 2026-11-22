"""Exercise installed helpers with synthetic stdin and isolated host paths."""
import io
import http.server
import threading
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/docker'
REF = 'ghcr.io/raulb1zkit/mcp-asir@sha256:' + 'a' * 64


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.marker = self.base / 'maintenance'
        self.env = dict(os.environ, ASIR_PROJECT_DIR=str(self.project),
                        ASIR_MAINTENANCE_MARKER=str(self.marker))

    def archive(self, entries):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w:gz') as tar:
            for name, kind in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = 3
                    tar.addfile(info, io.BytesIO(b'ok\n'))
                else:
                    info.linkname = '/etc/passwd'
                    tar.addfile(info)
        return out.getvalue()

    def sync(self, data):
        # Separate process, real stdin pipe: catches the former heredoc bug.
        return subprocess.run([str(DEPLOY / 'asir-deploy-sync'), str(self.project)],
                              input=data, capture_output=True)

    def test_real_pipe_with_directory_entry(self):
        result = self.sync(self.archive([('bin', tarfile.DIRTYPE),
                                        ('bin/asir-mcp-container', tarfile.REGTYPE),
                                        ('compose.yaml', tarfile.REGTYPE)]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / 'bin/asir-mcp-container').read_bytes(), b'ok\n')
        self.assertEqual((self.project / 'bin/asir-mcp-container').stat().st_mode & 0o777, 0o755)

    def test_malicious_archives_are_rejected_before_writes(self):
        for name, kind in [('compose.yaml', tarfile.SYMTYPE),
                           ('compose.yaml', tarfile.LNKTYPE),
                           ('compose.yaml', tarfile.FIFOTYPE),
                           ('../escape', tarfile.REGTYPE),
                           ('/etc/passwd', tarfile.REGTYPE),
                           ('bin/evil', tarfile.REGTYPE),
                           ('bin/other', tarfile.DIRTYPE),
                           ('asir-deploy.sh', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind):
                result = self.sync(self.archive([('compose.smoke.yaml', tarfile.REGTYPE), (name, kind)]))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'miembro tar no permitido', result.stderr)
                self.assertFalse((self.project / 'compose.smoke.yaml').exists())

    def test_duplicates_and_oversize_rejected(self):
        self.assertNotEqual(self.sync(self.archive([('compose.yaml', tarfile.REGTYPE)] * 2)).returncode, 0)
        self.assertNotEqual(self.sync(b'x' * 1_048_577).returncode, 0)

    def run_helper(self, command):
        return subprocess.run(['bash', str(DEPLOY / 'asir-deploy.sh'), command, REF, 'synthetic'],
                              env=self.env, input="", capture_output=True, text=True)

    def test_production_and_bootstrap_guard_before_docker(self):
        self.marker.touch()
        for command in ['production', 'deploy', 'bootstrap', 'rollback', 'install-unit']:
            with self.subTest(command=command):
                result = self.run_helper(command)
                self.assertEqual(result.returncode, 75, result.stderr)
                self.assertIn('operación NO ejecutada', result.stdout)
        self.assertFalse((self.project / '.env').exists())

    def test_first_deploy_directs_to_bootstrap(self):
        result = self.run_helper('deploy')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('usa bootstrap', result.stdout)

    def test_bootstrap_failure_restores_real_unit_file(self):
        unit_dir = self.base / 'systemd'
        unit_dir.mkdir()
        unit = unit_dir / 'asir-mcp.service'
        original = 'ExecStart=/opt/asir/venv/bin/python original.py\n'
        unit.write_text(original)
        (self.project / 'asir-mcp.service').write_text('ExecStart=synthetic-docker\n')
        tools = self.base / 'bin'
        tools.mkdir()
        # Fail start once; recovery start succeeds. HTTP remains unavailable so
        # recovery is honestly reported as unverified, while file restoration runs.
        (tools / 'systemctl').write_text('#!/bin/sh\n[ "$1" = daemon-reload ] && exit 0\nexit 1\n')
        (tools / 'docker').write_text('#!/bin/sh\ncat >/dev/null\nexit 0\n')
        for tool in tools.iterdir():
            tool.chmod(0o755)
        self.env.update(ASIR_SYSTEMD_DIR=str(unit_dir), ASIR_VENV_PYTHON='/bin/true',
                        ASIR_GHCR_TOKEN='synthetic-only', PATH=str(tools) + ':' + os.environ['PATH'])
        result = self.run_helper('bootstrap')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('recuperación venv NO verificada', result.stdout)
        self.assertEqual(unit.read_text(), original)
        self.assertFalse((self.project / '.env').exists())
        self.assertTrue((self.project / '.env.bootstrap-failed').exists())

    def test_bootstrap_success_keeps_venv_backup(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                self.send_response(401)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        unit_dir = self.base / 'systemd'
        unit_dir.mkdir()
        original = 'ExecStart=/opt/asir/venv/bin/python original.py\n'
        (unit_dir / 'asir-mcp.service').write_text(original)
        (self.project / 'asir-mcp.service').write_text('ExecStart=synthetic-docker\n')
        tools = self.base / 'bin'
        tools.mkdir()
        (tools / 'systemctl').write_text('#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n')
        (tools / 'docker').write_text('#!/bin/sh\ncat >/dev/null\nexit 0\n')
        for tool in tools.iterdir():
            tool.chmod(0o755)
        url = f'http://127.0.0.1:{server.server_port}'
        self.env.update(ASIR_SYSTEMD_DIR=str(unit_dir), ASIR_VENV_PYTHON='/bin/true',
                        ASIR_GHCR_TOKEN='synthetic-only', ASIR_HEALTH_URL=url + '/healthz',
                        ASIR_MCP_URL=url + '/mcp', PATH=str(tools) + ':' + os.environ['PATH'])
        result = self.run_helper('bootstrap')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('BOOTSTRAP OK', result.stdout)
        self.assertEqual((unit_dir / 'asir-mcp.service.venv-previo').read_text(), original)
        self.assertIn(REF, (self.project / '.env').read_text())
        self.assertIn('synthetic-docker', (unit_dir / 'asir-mcp.service').read_text())

    def test_workflow_maps_production_to_deploy(self):
        # El workflow de despliegue no se publica en el repositorio público:
        # si no existe, la comprobación se omite (el diseño queda documentado
        # en docs/docker-ci.md y la mecánica vive en el repositorio privado).
        workflow_path = ROOT / '.github/workflows/deploy.yml'
        if not workflow_path.exists():
            self.skipTest('workflow de despliegue no publicado en el repo público')
        workflow = workflow_path.read_text()
        shell = workflow.split('      - name: Ejecuta el modo solicitado')[1].split('        run: |\n')[1].split('\n      - name:')[0]
        shell = '\n'.join(line[10:] for line in shell.splitlines())
        tools = self.base / 'bin'
        tools.mkdir()
        ssh = tools / 'ssh'
        ssh.write_text('#!/bin/bash\nprintf "%s\\n" "${@: -1}"\ncat >/dev/null\n')
        ssh.chmod(0o755)
        for mode, command in [('production', 'deploy'), ('bootstrap', 'bootstrap'), ('smoke', 'smoke')]:
            with self.subTest(mode=mode):
                result = subprocess.run(['bash', '-c', shell], capture_output=True, text=True,
                                        env=dict(self.env, MODE=mode, IMAGE_REF=REF,
                                                 GHCR_TOKEN='synthetic', GHCR_USER='synthetic', VPS_HOST='test',
                                                 PATH=str(tools) + ':' + os.environ['PATH']))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f'run {command} {REF} synthetic')


if __name__ == '__main__':
    unittest.main()
