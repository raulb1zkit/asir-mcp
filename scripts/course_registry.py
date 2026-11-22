#!/usr/bin/env python3
"""Registro validado de cursos y exclusiones para la adquisición diaria."""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / 'config/courses.json'
ORIGIN = os.environ.get("ASIR_MOODLE_ORIGIN", "https://moodle.example.edu")


@dataclass(frozen=True)
class Exclusion:
    url: str
    reason: str


@dataclass(frozen=True)
class SourceSpec:
    name: str
    kind: str
    manifest: str
    chunks: str


@dataclass(frozen=True)
class Course:
    slug: str
    subject: str
    course_url: str
    exclusions: tuple[Exclusion, ...] = ()
    preserve_sources: tuple[SourceSpec, ...] = ()

    def downloader_args(self, output_root, session_file, mode='download'):
        return downloader_args(self, output_root, session_file, mode)


def _url(value, course=False):
    if not isinstance(value, str):
        raise ValueError('registry_invalid_url')
    u = urlsplit(value)
    if u.scheme != 'https' or u.netloc != urlsplit(ORIGIN).netloc or u.fragment:
        raise ValueError('registry_invalid_origin')
    query = parse_qsl(u.query, keep_blank_values=True)
    if course:
        valid = u.path == '/course/view.php' and len(query) == 1 and query[0][0] == 'id' and query[0][1].isdigit()
    else:
        valid = ((re.fullmatch(r'/mod/[a-z]+/view\.php', u.path) and len(query) == 1 and query[0][0] == 'id' and query[0][1].isdigit())
                 or (u.path.startswith('/pluginfile.php/') and not query and '..' not in u.path.split('/')))
    if not valid:
        raise ValueError('registry_invalid_resource_url')
    return value


def _relative(value, slug):
    if not isinstance(value, str) or '\\' in value:
        raise ValueError('registry_invalid_source_path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != slug:
        raise ValueError('registry_invalid_source_path')
    return value


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError('registry_invalid_label')
    return value


def load_registry(path=DEFAULT_REGISTRY):
    """Carga metadatos públicos; falla ante duplicados, URL o rutas no permitidas."""
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('version') != 1 or not isinstance(raw.get('courses'), list) or not raw['courses']:
        raise ValueError('registry_invalid_format')
    courses = []
    seen_slugs, seen_subjects, seen_urls = set(), set(), set()
    for item in raw['courses']:
        if not isinstance(item, dict):
            raise ValueError('registry_invalid_course')
        slug = item.get('slug')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', slug):
            raise ValueError('registry_invalid_slug')
        subject = _label(item.get('subject'))
        url = _url(item.get('course_url'), course=True)
        if slug in seen_slugs or subject in seen_subjects or url in seen_urls:
            raise ValueError('registry_duplicate_course')
        exclusions, sources = [], []
        for entry in item.get('exclusions', []):
            exclusions.append(Exclusion(_url(entry['url']), _label(entry['reason'])))
        if len({e.url for e in exclusions}) != len(exclusions):
            raise ValueError('registry_duplicate_exclusion')
        for entry in item.get('preserve_sources', []):
            if entry.get('kind') not in ('archives', 'manuals'):
                raise ValueError('registry_invalid_source_kind')
            sources.append(SourceSpec(_label(entry.get('name')), entry['kind'], _relative(entry.get('manifest'), slug), _relative(entry.get('chunks'), slug)))
        courses.append(Course(slug, subject, url, tuple(exclusions), tuple(sources)))
        seen_slugs.add(slug)
        seen_subjects.add(subject)
        seen_urls.add(url)
    return courses


def downloader_args(course, output_root, session_file, mode='download'):
    """Argumentos sin shell, cookie ni --resume: cada ejecución comprueba el remoto."""
    if mode not in ('download', 'inventory'):
        raise ValueError('registry_invalid_mode')
    args = [mode, '--course-url', course.course_url, '--subject', course.subject,
            '--output', str(Path(output_root) / course.slug / 'moodle'), '--session-file', str(session_file)]
    for exclusion in course.exclusions:
        args.extend(['--exclude-url', exclusion.url])
    return args
