#!/usr/bin/env python3
"""Genera el registro de identidades estables de recursos (mapa del núcleo RAG).

Lee chunks.jsonl (las fuentes tal como entran en el manifiesto, campos
filename/mime/moodle_active/path/section/sha256/subject/title/url) y los
manifiestos de Moodle y de expand_archives, y produce un JSON plano
`{clave: identidad}` que `rag_versions.resource_id` consume por `source_key`
o por URL. Nunca usa sha, título ni ruta local del host como identidad.

Reglas de identidad:
- Curso: `moodle:c<id>` desde `/course/view.php?id=N` del manifiesto del curso.
- Módulo: `moodle:c<curso>:<mod>:<cmid>` desde `/mod/<mod>/view.php?id=M`.
- Adjunto: `moodle:c<curso>:pf:<contextid>:<ruta filtrada>` desde
  `/pluginfile.php/<contextid>/...`; se descartan los segmentos interiores
  puramente numéricos (revisiones) y se conserva el último (nombre de archivo).
- Miembro de comprimido: identidad del contenedor + `#` + `filename`, que es la
  ruta completa del miembro dentro del ZIP/7z escrita por expand_archives.
  Todos los miembros comparten la URL del contenedor, así que la URL nunca es
  un alias válido para ellos; solo reciben clave por source_key individual.
  Las revisiones se resuelven por miembro lógico (asignatura, URL del
  contenedor y filename): se retiene la revisión inactiva anterior y se exige
  como máximo una revisión activa; dos o más activas son ambigüedad fail-closed.
  El sha nunca forma parte de la identidad.
- Externo: `external:<id>` únicamente si el operador lo declara en
  --external-file con la URL exacta.

Alias por URL solo cuando es inequívoco en todo el corpus (una sola identidad).
Clasificación por RUTA (mod/*/view.php, pluginfile.php) o por el host del curso
registrado; nunca por un literal del dominio. Todo lo ambiguo o sin identidad
deducible falla cerrado: escribe diagnóstico privado 0600 con URLs y hashes y
por stdout solo motivos, recuentos y hashes; ningún título ni contenido.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

try:
    from .import_supabase import source_key
except ImportError:
    from import_supabase import source_key

ARCHIVE_MIMES = {'application/g-zip', 'application/zip', 'application/x-7z-compressed'}
COURSE_RE = re.compile(r'/course/view\.php\?id=(\d+)\Z')
MODULE_RE = re.compile(r'/mod/([a-z0-9_-]+)/view\.php\?id=(\d+)\Z')
SOURCE_FIELDS = ('filename', 'mime', 'moodle_active', 'path', 'section', 'sha256', 'subject', 'title', 'url')


class RegistryError(ValueError):
    pass


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RegistryError('registry_input_unreadable') from error


def _path_query(split):
    return split.path + ('?' + split.query if split.query else '')


def _course_identity(course_url):
    match = COURSE_RE.search(_path_query(urlsplit(course_url or '')))
    if not match:
        raise RegistryError('course_identity_unresolvable')
    return 'c' + match.group(1)


def _pluginfile_identity(course, url):
    segments = urlsplit(url).path.strip('/').split('/')
    # pluginfile.php/<contextid>/<component>/<filearea>/.../<filename>
    if (len(segments) < 4 or segments[0] != 'pluginfile.php' or not segments[1].isdigit()
            or not segments[-1]):
        raise RegistryError('pluginfile_route_unrecognised')
    context = segments[1]
    interior = [segment for segment in segments[2:-1] if not segment.isdigit()]
    return 'moodle:%s:pf:%s:%s' % (course, context, '/'.join([*interior, segments[-1]]))


def _course_url_identity(course, url):
    """Identidad de una URL del host del curso; None si la ruta no es de módulo."""
    split = urlsplit(url)
    if split.scheme not in {'http', 'https'} or not split.netloc:
        raise RegistryError('url_not_absolute')
    target = _path_query(split)
    module = MODULE_RE.search(target)
    if module:
        return 'moodle:%s:%s:%s' % (course, module.group(1), module.group(2))
    if split.path.startswith('/pluginfile.php/'):
        return _pluginfile_identity(course, url)
    if COURSE_RE.search(target):
        raise RegistryError('course_url_used_as_resource')
    return None


class Registry:
    def __init__(self):
        self.subject_course = {}                # subject -> (curso, host)
        self.items_by_url = defaultdict(list)   # (subject, url) -> items de manifiestos
        self.external = {}                      # url -> external:<id>
        self.conflicts = defaultdict(list)      # motivo -> registros seguros

    def _register_subject(self, subject, course_url):
        if not isinstance(subject, str) or not subject:
            raise RegistryError('manifest_subject_missing')
        course = _course_identity(course_url)
        host = urlsplit(course_url or '').netloc.lower()
        known = self.subject_course.get(subject)
        if known and known != (course, host):
            raise RegistryError('subject_maps_to_two_courses')
        self.subject_course[subject] = (course, host)

    def load_course_manifest(self, path):
        manifest = _read_json(path)
        self._register_subject(manifest.get('subject'), manifest.get('course_url'))
        subject = manifest['subject']
        for item in manifest.get('items', []):
            url, sha = item.get('url'), item.get('sha256')
            if not url or not sha:
                continue
            self.items_by_url[(subject, url)].append({'sha256': sha, 'mime': item.get('mime')})

    def load_archive_manifest(self, path):
        """Confirma contenedores; opcional pero recomendable para validar."""
        manifest = _read_json(path)
        self._register_subject(manifest.get('subject'), manifest.get('course_url'))
        subject = manifest['subject']
        archives = {entry.get('sha256') for entry in manifest.get('archives', []) if entry.get('sha256')}
        for item in manifest.get('items', []):
            url, sha = item.get('url'), item.get('sha256')
            if not url or not sha:
                continue
            self.items_by_url[(subject, url)].append({'sha256': sha, 'mime': item.get('mime'), 'archive': item.get('archive_sha256') in archives})

    def load_external(self, path):
        values = _read_json(path)
        if not isinstance(values, dict):
            raise RegistryError('external_file_invalid')
        for url, identity in values.items():
            if (not isinstance(identity, str) or not identity.startswith('external:')
                    or len(identity) <= len('external:')):
                raise RegistryError('external_identity_invalid')
            self.external[url] = identity

    def _is_archive_url(self, subject, url):
        return any(item.get('mime') in ARCHIVE_MIMES for item in self.items_by_url.get((subject, url), []))

    def _identity(self, source):
        """Identidad estable de una fuente del corpus; falla cerrado si es ambigua."""
        subject, url = source.get('subject'), source.get('url')
        if not isinstance(subject, str) or not isinstance(url, str):
            raise RegistryError('source_subject_or_url_missing')
        known = self.subject_course.get(subject)
        if not known:
            self.conflicts['subject_without_registered_course'].append({'subject': subject, 'sha256': source.get('sha256')})
            raise RegistryError('subject_without_registered_course')
        course, host = known
        split = urlsplit(url)
        if split.netloc.lower() != host:
            identity = self.external.get(url)
            if identity is None:
                self.conflicts['external_resource_unregistered'].append({'subject': subject, 'url': url, 'sha256': source.get('sha256')})
                raise RegistryError('external_resource_unregistered')
            return identity
        parent = _course_url_identity(course, url)
        if parent is None:
            self.conflicts['course_host_route_unrecognised'].append({'subject': subject, 'url': url, 'sha256': source.get('sha256')})
            raise RegistryError('course_host_route_unrecognised')
        filename = source.get('filename')
        if self._is_archive_url(subject, url):
            if not isinstance(filename, str) or not filename:
                raise RegistryError('archive_member_filename_missing')
            return parent + '#' + filename
        return parent

    def build(self, chunks_paths):
        """Devuelve (mapa, resumen) o falla cerrado; diagnóstico en write_conflicts."""
        by_source = {}
        url_identities = defaultdict(set)   # url -> identidades distintas (alias)
        member_contents = defaultdict(lambda: {'active': set(), 'inactive': set()})
        source_count = 0
        for chunks_path in chunks_paths:
            try:
                lines = Path(chunks_path).read_text(encoding='utf-8').splitlines()
            except OSError as error:
                raise RegistryError('registry_input_unreadable') from error
            for line in lines:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                for citation in chunk.get('citations', []):
                    source = {k: citation['source'].get(k) for k in SOURCE_FIELDS}
                    key = source_key(source)
                    identity = self._identity(source)
                    previous = by_source.get(key)
                    if previous and previous != identity:
                        self.conflicts['source_key_maps_to_two_identities'].append({'source_key': key, 'sha256': source.get('sha256')})
                        raise RegistryError('source_key_maps_to_two_identities')
                    by_source[key] = identity
                    if self._is_archive_url(source['subject'], source['url']):
                        state = 'active' if source.get('moodle_active') is True else 'inactive'
                        member_contents[(source['subject'], source['url'], source.get('filename'))][state].add(source.get('sha256'))
                    else:
                        url_identities[source['url']].add(identity)
                    source_count += 1
        for (subject, url, filename), revisions in member_contents.items():
            active = revisions['active']
            if len(active) > 1:
                self.conflicts['ambiguous_archive_member'].append({'subject': subject, 'url': url, 'filename': filename, 'sha256': sorted(revisions['active'])[0]})
                raise RegistryError('ambiguous_archive_member')
        mapping = {key: identity for key, identity in sorted(by_source.items())}
        for url, identities in sorted(url_identities.items()):
            if len(identities) == 1:
                mapping[url] = next(iter(identities))
        summary = {'sources': source_count, 'source_keys': len(by_source), 'url_aliases': len(mapping) - len(by_source)}
        return mapping, summary

    def write_conflicts(self, output):
        """Diagnóstico privado para el operador; nunca se publica ni sube a Git."""
        if not self.conflicts:
            return None
        path = output.with_name(output.name + '.conflicts.json')
        payload = {'generated_at': datetime.now(timezone.utc).isoformat(), 'conflicts': self.conflicts}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        return path


def check(existing_path, mapping):
    """Compara un mapa existente con la reconstrucción actual del corpus."""
    existing = _read_json(existing_path)
    if not isinstance(existing, dict):
        raise RegistryError('registry_map_invalid')
    missing = sorted(set(mapping) - set(existing))
    changed = sorted(key for key in set(mapping) & set(existing) if existing[key] != mapping[key])
    return {'missing_keys': missing, 'changed_keys': changed, 'expected_keys': len(mapping), 'existing_keys': len(existing)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chunks', type=Path, action='append', required=True, help='chunks.jsonl del corpus (repetible).')
    parser.add_argument('--course-manifest', type=Path, action='append', help='moodle/manifest.json por curso (repetible).')
    parser.add_argument('--archive-manifest', type=Path, action='append', help='manifest.json de expand_archives (repetible, opcional).')
    parser.add_argument('--external-file', type=Path, help='JSON privado {url: "external:<id>"} para recursos fuera del curso.')
    parser.add_argument('--output', type=Path, required=True, help='Mapa nuevo; el archivo no debe existir.')
    parser.add_argument('--check', type=Path, help='Valida un mapa existente en lugar de escribir; salida 0 si coincide.')
    args = parser.parse_args()
    registry = Registry()
    try:
        for path in args.course_manifest or []:
            registry.load_course_manifest(path)
        for path in args.archive_manifest or []:
            registry.load_archive_manifest(path)
        if args.external_file:
            registry.load_external(args.external_file)
        mapping, summary = registry.build(args.chunks)
        if args.check:
            result = check(args.check, mapping)
            result.update(summary)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if not result['missing_keys'] and not result['changed_keys'] else 1
        if args.output.exists():
            raise RegistryError('registry_output_exists')
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(mapping, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        result = {**summary, 'entries': len(mapping)}
        diagnostics = registry.write_conflicts(args.output)
        if diagnostics:
            result['diagnostics'] = str(diagnostics)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RegistryError as error:
        result = {'error': str(error)}
        for reason, entries in registry.conflicts.items():
            result[reason] = len(entries)
        try:
            diagnostics = registry.write_conflicts(args.output) if args.output else None
            if diagnostics:
                result['diagnostics'] = str(diagnostics)
        except OSError:
            pass
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
