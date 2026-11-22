#!/usr/bin/env python3
"""Comprueba y renueva una sesión Moodle mediante su formulario web normal."""
import argparse
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
import json
import os
import stat
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

try:
    from .moodle_download import DEFAULT_COURSE, ROOT, Document, NoRedirect, same_origin, is_login
except ImportError:
    from moodle_download import DEFAULT_COURSE, ROOT, Document, NoRedirect, same_origin, is_login


class AuthError(Exception):
    """Error controlado sin cuerpos, URL firmadas ni credenciales."""


def private_json(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise AuthError("No se pudo abrir el archivo privado sin seguir enlaces.") from None
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid():
            raise AuthError("El secreto debe ser regular, propio y tener permisos 600.")
        try:
            value = json.load(source)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise AuthError("Formato de secreto no válido.") from None


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise AuthError("No se permite guardar secretos sobre enlaces.")
    fd, temporary = tempfile.mkstemp(prefix=".moodle-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def make_cookie(name, value, host, path="/", expires=None):
    if not isinstance(name, str) or not isinstance(value, str) or any(c in name + value for c in "\r\n;\x00"):
        raise AuthError("Cookie no válida.")
    return Cookie(0, name, value, None, False, host, False, False, path, True, True, expires, expires is None, None, None, {}, False)


class WebSession:
    def __init__(self, course_url, session_file):
        if urlsplit(course_url).scheme != "https" or urlsplit(course_url).username or urlsplit(course_url).password:
            raise AuthError("Moodle debe usar un origen HTTPS sin credenciales en la URL.")
        self.course_url, self.session_file = course_url, Path(session_file)
        self.host = urlsplit(course_url).hostname
        self.jar = CookieJar(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrict))
        if self.session_file.exists() or self.session_file.is_symlink():
            data = private_json(self.session_file)
            cookies = data.get("cookies", [{"name": data.get("name"), "value": data.get("value")}])
            for item in cookies:
                if item.get("domain", self.host).lstrip(".") != self.host:
                    raise AuthError("Cookie de otro dominio rechazada.")
                self.jar.set_cookie(make_cookie(item.get("name"), item.get("value"), self.host, item.get("path", "/"), item.get("expires")))
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.jar))

    def request(self, url, data=None):
        for _ in range(8):
            if not same_origin(url, self.course_url):
                raise AuthError("Redirección fuera de Moodle bloqueada; se requiere acceso interactivo.")
            req = Request(url, data=data, headers={"User-Agent": "MCP-ASIR-session/1.0"})
            try:
                response = self.opener.open(req, timeout=30)
            except HTTPError as error:
                code = error.code
                location = error.headers.get("Location")
                error.close()
                if code in {301, 302, 303, 307, 308} and location:
                    if data is not None and code in {307, 308}:
                        raise AuthError("Redirección de credenciales no admitida.") from None
                    url, data = urljoin(url, location), None
                    continue
                raise AuthError("Moodle rechazó la petición; revisa el acceso o inténtalo más tarde.") from None
            except (URLError, TimeoutError, OSError):
                raise AuthError("No se pudo conectar con Moodle por red/TLS.") from None
            with response:
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise AuthError("Respuesta de acceso demasiado grande.")
                return url, Document(body.decode("utf-8", errors="replace"))
        raise AuthError("Demasiadas redirecciones durante el acceso.")

    def check(self):
        url, doc = self.request(self.course_url)
        nodes = list(doc.root.walk())
        authenticated = any(n.tag == "a" and "/login/logout.php" in n.attrs.get("href", "") for n in nodes)
        return not is_login(url, doc) and authenticated

    def renew(self, credentials):
        url, doc = self.request(urljoin(self.course_url, "/login/index.php"))
        forms = [n for n in doc.root.walk() if n.tag == "form" and any(c.attrs.get("name") == "password" and c.attrs.get("type", "").lower() == "password" for c in n.walk())]
        if len(forms) != 1:
            raise AuthError("Acceso web no compatible: se requiere acceso interactivo (SSO/MFA/CAPTCHA).")
        form = forms[0]
        inputs = [n for n in form.walk() if n.tag == "input"]
        names = {n.attrs.get("name") for n in inputs}
        if not {"username", "password", "logintoken"} <= names or any("captcha" in str(n.attrs).lower() for n in form.walk()):
            raise AuthError("Formulario no compatible: se requiere acceso interactivo (SSO/MFA/CAPTCHA).")
        action = urljoin(url, form.attrs.get("action") or url)
        if not same_origin(action, self.course_url) or form.attrs.get("method", "get").lower() != "post":
            raise AuthError("Formulario de acceso no seguro.")
        fields = {n.attrs["name"]: n.attrs.get("value", "") for n in inputs if n.attrs.get("type", "").lower() == "hidden" and n.attrs.get("name")}
        if not fields.get("logintoken"):
            raise AuthError("Falta el token CSRF de Moodle.")
        if not all(isinstance(credentials.get(k), str) and credentials[k] for k in ("username", "password")):
            raise AuthError("Credenciales incompletas.")
        fields.update(username=credentials["username"], password=credentials["password"])
        self.request(action, urlencode(fields).encode())
        if not self.check():
            raise AuthError("No se confirmó el acceso: credenciales rechazadas o intervención requerida.")

    def save(self):
        self.jar.clear_expired_cookies()
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": c.expires} for c in self.jar if c.domain.lstrip(".") == self.host]
        sessions = [c for c in cookies if "MoodleSession" in c["name"]]
        if len(sessions) != 1:
            raise AuthError("No se ha encontrado una cookie de sesión Moodle inequívoca.")
        save_private(self.session_file, {"name": sessions[0]["name"], "value": sessions[0]["value"], "cookies": cookies})
        return sessions[0]["name"] + "=" + sessions[0]["value"]


def ensure_session(session_file, credentials_file=None, course_url=DEFAULT_COURSE, force=False):
    session = WebSession(course_url, session_file)
    if force or not session.check():
        if credentials_file is None:
            raise AuthError("Sesión caducada; configura credenciales privadas o renueva la cookie interactivamente.")
        session.renew(private_json(credentials_file))
    return session.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "renew"))
    parser.add_argument("--session-file", type=Path, default=ROOT / ".private/moodle-session.json")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--course-url", default=DEFAULT_COURSE)
    args = parser.parse_args()
    try:
        ensure_session(args.session_file, args.credentials_file, args.course_url, args.command == "renew")
    except (AuthError, OSError, TypeError, KeyError, ValueError):
        print("No se pudo validar/renovar la sesión. Revisa red, permisos y acceso interactivo; el secreto no se ha mostrado.")
        return 1
    print("Sesión validada y guardada; el secreto no se ha mostrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
