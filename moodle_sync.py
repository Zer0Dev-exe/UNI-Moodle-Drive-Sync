"""
Sincroniza todo el contenido de un Moodle con Google Drive.

- Ficheros nuevos en Moodle      -> se suben a Drive.
- Ficheros modificados en Moodle -> se actualiza el mismo fichero en Drive
                                    (Drive guarda la versión anterior en su historial).
- Ficheros borrados en Moodle    -> se MANTIENEN en Drive (se anota en su descripción).
- Ficheros movidos/renombrados   -> se mueven/renombran en Drive.

Pensado para ejecutarse con Docker (ver README). Opciones:
    --loop MIN    repetir cada MIN minutos (o variable SYNC_INTERVAL_MINUTES)
    --dry-run     mostrar qué haría sin tocar Drive
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MOODLE_URL = os.environ.get("MOODLE_URL", "").rstrip("/")
DRIVE_ROOT = os.environ.get("DRIVE_ROOT_FOLDER", "Moodle")
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"

log = logging.getLogger("moodle_sync")


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def clean(name: str, maxlen: int = 150) -> str:
    name = html.unescape(name or "").strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name)
    return (name[:maxlen].strip() or "sin_nombre")


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- Moodle

class MoodleError(Exception):
    pass


class Moodle:
    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.http = requests.Session()
        r = self.http.post(f"{url}/login/token.php", timeout=60, data={
            "username": user, "password": password, "service": "moodle_mobile_app"}).json()
        if "token" not in r:
            raise MoodleError(f"Login fallido en Moodle: {r.get('error')} ({r.get('errorcode')})")
        self.token = r["token"]
        self.site = self.call("core_webservice_get_site_info")
        self.userid = self.site["userid"]
        self._credentials = (user, password)
        self._web = None

    def web(self) -> requests.Session:
        """Sesión web normal (algunas cosas, como las subpestañas, no salen en la API)."""
        if self._web is None:
            s = requests.Session()
            page = s.get(f"{self.url}/login/index.php", timeout=60).text
            tok = re.search(r'name="logintoken" value="([^"]+)"', page)
            r = s.post(f"{self.url}/login/index.php", timeout=60, data={
                "username": self._credentials[0], "password": self._credentials[1],
                "logintoken": tok.group(1) if tok else ""})
            if "notloggedin" in r.text:
                raise MoodleError("Login web fallido")
            self._web = s
        return self._web

    def top_level_sections(self, courseid: int) -> set[int]:
        """IDs de sección que son pestañas principales en el formato 'onetopic'.
        Las demás son subpestañas de la pestaña principal anterior. Vacío = sin jerarquía."""
        try:
            page = self.web().get(f"{self.url}/course/view.php?id={courseid}", timeout=120).text
        except (requests.RequestException, MoodleError) as e:
            log.warning("No se pudo leer la jerarquía de pestañas: %s", e)
            return set()
        return {int(x) for x in re.findall(r'class="[^"]*\btab_level_0\b[^"]*"\s+id="onetabid-(\d+)"', page)}

    def call(self, function: str, **params):
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}
        data.update(flatten_params(params))
        for attempt in range(3):
            try:
                r = self.http.post(f"{self.url}/webservice/rest/server.php", data=data, timeout=120)
                r.raise_for_status()
                res = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    raise MoodleError(f"{function}: {e}")
                time.sleep(5 * (attempt + 1))
        if isinstance(res, dict) and res.get("exception"):
            raise MoodleError(f"{function}: {res.get('message')} ({res.get('errorcode')})")
        return res

    def download(self, fileurl: str, dest: str):
        if "/webservice/pluginfile.php" not in fileurl:
            fileurl = fileurl.replace("/pluginfile.php", "/webservice/pluginfile.php")
        sep = "&" if "?" in fileurl else "?"
        with self.http.get(f"{fileurl}{sep}token={self.token}", stream=True, timeout=300) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if "application/json" in ctype:
            with open(dest, "rb") as f:
                head = f.read(2000)
            if b'"error"' in head or b'"exception"' in head:
                raise MoodleError(f"Error descargando {fileurl}: {head[:300]!r}")


def flatten_params(obj, prefix=""):
    """Convierte dicts/listas al formato de Moodle: courseids[0]=1, options[0][name]=x ..."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten_params(v, f"{prefix}[{k}]" if prefix else k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(flatten_params(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = int(obj) if isinstance(obj, bool) else obj
    return out


# --------------------------------------------------------------------------- Drive

class Drive:
    def __init__(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                client_id, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
                if not client_id or not secret:
                    raise SystemExit("Faltan GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET en .env. Mira el README.")
                flow = InstalledAppFlow.from_client_config({"installed": {
                    "client_id": client_id.strip(), "client_secret": secret.strip(),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"]}}, SCOPES)
                # Primera vez: se imprime un enlace; Google redirige a localhost:8765, que Docker
                # reenvía al contenedor. Fuera de Docker se abre el navegador directamente.
                in_docker = os.path.exists("/.dockerenv")
                creds = flow.run_local_server(
                    port=8765, bind_addr="0.0.0.0" if in_docker else None, open_browser=not in_docker,
                    authorization_prompt_message="\n>>> Abre este enlace en el navegador para autorizar Google Drive:\n{url}\n",
                    success_message="Autorizado. Ya puedes cerrar esta ventana.")
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.folders: dict[tuple, str] = {}
        self.existing_ids: set[str] = set()

    def load_existing(self):
        """IDs de todos los ficheros/carpetas creados por esta app que no están en la papelera."""
        ids, token = set(), None
        while True:
            res = self.svc.files().list(q="trashed=false", spaces="drive", pageSize=1000,
                                        fields="nextPageToken, files(id)", pageToken=token).execute(num_retries=5)
            ids.update(f["id"] for f in res.get("files", []))
            token = res.get("nextPageToken")
            if not token:
                break
        self.existing_ids = ids

    def folder(self, path: list[str]) -> str:
        parent, key = "root", ()
        for name in path:
            key = key + (name,)
            if key not in self.folders:
                esc = name.replace("\\", "\\\\").replace("'", "\\'")
                q = (f"name='{esc}' and '{parent}' in parents and mimeType='{FOLDER_MIME}' and trashed=false")
                found = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute(num_retries=5)["files"]
                if found:
                    self.folders[key] = found[0]["id"]
                else:
                    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
                    self.folders[key] = self.svc.files().create(body=meta, fields="id").execute(num_retries=5)["id"]
                    self.existing_ids.add(self.folders[key])
            parent = self.folders[key]
        return parent

    def remove_empty_folders(self, folder_id: str, path: str = "", is_root: bool = True) -> bool:
        """Manda a la papelera las subcarpetas vacías (p. ej. tras reorganizar). Devuelve si quedó vacía."""
        children, token = [], None
        while True:
            res = self.svc.files().list(q=f"'{folder_id}' in parents and trashed=false", pageToken=token,
                                        fields="nextPageToken, files(id, name, mimeType)",
                                        pageSize=1000).execute(num_retries=5)
            children += res.get("files", [])
            token = res.get("nextPageToken")
            if not token:
                break
        remaining = 0
        for ch in children:
            if ch["mimeType"] == FOLDER_MIME and self.remove_empty_folders(ch["id"], f"{path}/{ch['name']}", False):
                continue
            remaining += 1
        if remaining == 0 and not is_root:
            self.svc.files().update(fileId=folder_id, body={"trashed": True}).execute(num_retries=5)
            self.folders = {k: v for k, v in self.folders.items() if v != folder_id}
            log.info("Carpeta vacía a la papelera: %s", path)
            return True
        return False

    @staticmethod
    def _media(local: str, name: str):
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return MediaFileUpload(local, mimetype=mime, resumable=os.path.getsize(local) > 5 * 1024 * 1024)

    def upload(self, local: str, name: str, parent: str, description: str = "") -> str:
        meta = {"name": name, "parents": [parent], "description": description}
        fid = self.svc.files().create(body=meta, media_body=self._media(local, name),
                                      fields="id").execute(num_retries=5)["id"]
        self.existing_ids.add(fid)
        return fid

    def update_content(self, fid: str, local: str, name: str):
        self.svc.files().update(fileId=fid, body={"name": name}, media_body=self._media(local, name),
                                fields="id").execute(num_retries=5)

    def move(self, fid: str, name: str, old_parent: str, new_parent: str):
        kw = {}
        if old_parent != new_parent:
            kw = {"addParents": new_parent, "removeParents": old_parent}
        self.svc.files().update(fileId=fid, body={"name": name}, fields="id", **kw).execute(num_retries=5)

    def set_description(self, fid: str, text: str):
        self.svc.files().update(fileId=fid, body={"description": text}, fields="id").execute(num_retries=5)


# --------------------------------------------------------------------------- Sync

@dataclass
class Item:
    key: str            # identificador estable en Moodle
    path: list          # carpetas en Drive (bajo la raíz)
    name: str           # nombre del fichero en Drive
    version: str        # cambia cuando cambia el fichero en Moodle
    fetch: Callable     # fetch(dest_path) -> descarga/genera el fichero
    course: int


class Syncer:
    def __init__(self, moodle: Moodle, drive: Drive, dry_run: bool = False):
        self.m, self.d, self.dry = moodle, drive, dry_run
        self.state = self._load_state()
        self.seen: set[str] = set()
        self.ok_courses: set[int] = set()
        self.changes: list[tuple] = []
        self.stats = {"nuevos": 0, "actualizados": 0, "movidos": 0, "sin_cambios": 0,
                      "borrados_en_moodle": 0, "errores": 0}

    # ---------- estado
    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        return {"files": {}}

    def save_state(self):
        if self.dry:
            return
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)

    # ---------- un fichero
    def sync_item(self, it: Item):
        self.seen.add(it.key)
        files = self.state["files"]
        st = files.get(it.key)
        try:
            if self.dry:
                action = "nuevo" if not st else ("actualizar" if st["version"] != it.version else
                                                 "mover" if (st.get("path"), st.get("name")) != ("/".join(it.path), it.name)
                                                 else "=")
                if action != "=":
                    log.info("[dry-run] %s: %s", action, "/".join(it.path + [it.name]))
                return
            parent = self.d.folder([DRIVE_ROOT] + it.path)
            alive = st and st.get("drive_id") in self.d.existing_ids

            if alive and st["version"] == it.version:
                if st.get("parent") != parent or st.get("name") != it.name:
                    self.d.move(st["drive_id"], it.name, st.get("parent"), parent)
                    st.update(parent=parent, name=it.name, path="/".join(it.path))
                    self.stats["movidos"] += 1
                    self.change("MOVIDO", it.path, it.name)
                else:
                    self.stats["sin_cambios"] += 1
                self._undelete(st, it.key)
                return

            with tempfile.TemporaryDirectory() as tmpdir:
                local = os.path.join(tmpdir, "f")
                it.fetch(local)
                md5 = md5_file(local)
                if alive:
                    if md5 != st.get("md5"):
                        if st.get("parent") != parent:
                            self.d.move(st["drive_id"], it.name, st.get("parent"), parent)
                        self.d.update_content(st["drive_id"], local, it.name)
                        self.stats["actualizados"] += 1
                        self.change("ACTUALIZADO", it.path, it.name)
                    else:
                        self.stats["sin_cambios"] += 1
                    st.update(version=it.version, md5=md5, parent=parent, name=it.name,
                              path="/".join(it.path), updated=now_iso())
                    self._undelete(st, it.key)
                else:
                    fid = self.d.upload(local, it.name, parent, f"Moodle: {it.key}")
                    files[it.key] = {"drive_id": fid, "version": it.version, "md5": md5, "parent": parent,
                                     "name": it.name, "path": "/".join(it.path), "course": it.course,
                                     "created": now_iso(), "updated": now_iso()}
                    self.stats["nuevos"] += 1
                    self.change("RE-SUBIDO" if st else "NUEVO", it.path, it.name)
            self.save_state()
        except Exception as e:
            self.stats["errores"] += 1
            log.error("Error con %s (%s): %s", it.name, it.key, e)

    def change(self, kind, path, name):
        """Registra un cambio: asignatura, dónde está y qué documento."""
        course, where = (path[0] if path else "?"), " / ".join(path[1:]) or "(raíz del curso)"
        self.changes.append((kind, course, where, name))
        log.info("%s | Asignatura: %s | Sección: %s | Documento: %s", kind, course, where, name)

    def report(self):
        if not self.changes:
            log.info("Sin cambios (%d ficheros revisados%s)", self.stats["sin_cambios"],
                     f", {self.stats['errores']} errores" if self.stats["errores"] else "")
            return
        lines = [f"===== {len(self.changes)} cambio(s) en Moodle ====="]
        for course in dict.fromkeys(c[1] for c in self.changes):
            lines.append(f"  {course}:")
            lines += [f"    - {k}: {n}   [{w}]" for k, c, w, n in self.changes if c == course]
        if self.stats["errores"]:
            lines.append(f"  ({self.stats['errores']} errores, mira las líneas ERROR)")
        log.info("\n".join(lines))

    def _undelete(self, st, key):
        if st.pop("deleted_in_moodle", None):
            self.d.set_description(st["drive_id"], f"Moodle: {key}")

    def mark_deleted(self):
        """Lo que estaba en Moodle y ya no está se conserva en Drive; solo se anota."""
        for key, st in self.state["files"].items():
            if key in self.seen or st.get("deleted_in_moodle") or st.get("course") not in self.ok_courses:
                continue
            st["deleted_in_moodle"] = now_iso()
            self.stats["borrados_en_moodle"] += 1
            self.change("BORRADO EN MOODLE (se conserva en Drive)", (st.get("path") or "").split("/"), st.get("name"))
            if not self.dry and st.get("drive_id") in self.d.existing_ids:
                try:
                    self.d.set_description(st["drive_id"], f"Eliminado de Moodle el {st['deleted_in_moodle']}")
                except HttpError as e:
                    log.warning("No se pudo anotar %s: %s", st.get("name"), e)
        self.save_state()

    # ---------- recorrer Moodle
    def run(self):
        if not self.dry:
            self.d.load_existing()
        courses = self.m.call("core_enrol_get_users_courses", userid=self.m.userid)
        log.debug("Usuario: %s | %d cursos", self.m.site.get("fullname"), len(courses))
        for c in courses:
            try:
                self.sync_course(c)
                self.ok_courses.add(c["id"])
            except Exception as e:
                self.stats["errores"] += 1
                log.error("Error en el curso %s: %s", c.get("fullname"), e)
        self.mark_deleted()
        if not self.dry and not self.stats["errores"]:
            self.d.remove_empty_folders(self.d.folder([DRIVE_ROOT]), DRIVE_ROOT)
        self.report()

    def file_item(self, key, path, name, f, course) -> Item:
        version = f"{f.get('timemodified')}-{f.get('filesize')}"
        return Item(key, path, clean(name), version, lambda dest, url=f["fileurl"]: self.m.download(url, dest), course)

    def text_item(self, key, path, name, text, course) -> Item:
        data = text.encode("utf-8")

        def write(dest):
            with open(dest, "wb") as fh:
                fh.write(data)
        return Item(key, path, clean(name), hashlib.md5(data).hexdigest(), write, course)

    def sync_course(self, c):
        cid = c["id"]
        cname = clean(c.get("fullname") or c.get("shortname"))
        log.debug("== Curso: %s", cname)
        sections = self.m.call("core_course_get_contents", courseid=cid)
        index = [f"<h1>{html.escape(cname)}</h1>", c.get("summary") or ""]
        top = self.m.top_level_sections(cid) if c.get("format") == "onetopic" else set()
        self.cm_paths = {}  # cmid -> carpeta de su sección (para tareas y foros)

        parent, n_top, n_sub = [cname], -1, 0
        for s in sections:
            title = s.get("name") or "Sección"
            # pestaña principal (o la sección 0 / cursos sin subpestañas) -> carpeta del curso;
            # subpestaña -> carpeta dentro de la pestaña principal anterior
            if not top or s["section"] == 0 or s["id"] in top or n_top < 0:
                n_top, n_sub = n_top + 1, 0
                spath = [cname, clean(f"{n_top:02d} - {title}")]
                parent = spath
            else:
                n_sub += 1
                spath = parent + [clean(f"{n_sub:02d} - {title}")]
            index.append(f"<h{2 if spath is parent else 3}>{html.escape(title)}</h{2 if spath is parent else 3}>"
                         f"{s.get('summary') or ''}")
            for mod in s.get("modules", []):
                self.cm_paths[mod["id"]] = spath
                mname = clean(mod.get("name"))
                index.append(f"<h4>[{mod.get('modname')}] {html.escape(mod.get('name') or '')}</h4>"
                             f"{mod.get('description') or ''}"
                             + (f"<p><a href='{html.escape(mod['url'])}'>{html.escape(mod['url'])}</a></p>"
                                if mod.get("url") else ""))
                contents = mod.get("contents") or []
                files = [f for f in contents if f.get("type") == "file" and f.get("fileurl")]
                urls = [f for f in contents if f.get("type") == "url" and f.get("fileurl")]

                # recurso con un único fichero -> directamente en la carpeta de la sección
                base = spath if (mod["modname"] == "resource" and len(files) == 1) else spath + [mname]
                for f in files:
                    sub = [clean(p) for p in (f.get("filepath") or "/").strip("/").split("/") if p]
                    key = f"c{cid}/m{mod['id']}{f.get('filepath') or '/'}{f.get('filename')}"
                    self.sync_item(self.file_item(key, base + sub, f["filename"], f, cid))
                for u in urls:
                    index.append(f"<p>Enlace: <a href='{html.escape(u['fileurl'])}'>{html.escape(u['fileurl'])}</a></p>")
                    if mod["modname"] == "url":
                        self.sync_item(self.text_item(f"c{cid}/m{mod['id']}/url", spath, f"{mname}.url",
                                                      f"[InternetShortcut]\r\nURL={u['fileurl']}\r\n", cid))

        self.sync_assignments(cid, cname)
        self.sync_forums(cid, cname)
        self.sync_grades(cid, cname)
        page = ("<html><head><meta charset='utf-8'><title>" + html.escape(cname) + "</title></head><body>"
                + "\n".join(index) + "</body></html>")
        self.sync_item(self.text_item(f"c{cid}/index", [cname], "_Indice del curso.html", page, cid))

    def sync_assignments(self, cid, cname):
        try:
            res = self.m.call("mod_assign_get_assignments", courseids=[cid])
        except MoodleError as e:
            log.warning("Tareas no disponibles en %s: %s", cname, e)
            return
        for course in res.get("courses", []):
            for a in course.get("assignments", []):
                apath = self.cm_paths.get(a.get("cmid"), [cname]) + ["Tareas", clean(a["name"])]
                due = (dt.datetime.fromtimestamp(a["duedate"]).strftime("%Y-%m-%d %H:%M")
                       if a.get("duedate") else "-")
                desc = (f"<html><head><meta charset='utf-8'></head><body><h1>{html.escape(a['name'])}</h1>"
                        f"<p>Fecha de entrega: {due}</p>{a.get('intro') or ''}</body></html>")
                self.sync_item(self.text_item(f"c{cid}/assign{a['id']}/enunciado", apath, "_Enunciado.html", desc, cid))
                for f in a.get("introattachments", []):
                    self.sync_item(self.file_item(f"c{cid}/assign{a['id']}/intro{f.get('filepath', '/')}{f['filename']}",
                                                  apath, f["filename"], f, cid))
                try:
                    status = self.m.call("mod_assign_get_submission_status", assignid=a["id"])
                except MoodleError as e:
                    log.debug("Sin estado de entrega %s: %s", a["name"], e)
                    continue
                groups = [("Mi entrega", (status.get("lastattempt") or {}).get("submission") or {}),
                          ("Feedback", status.get("feedback") or {})]
                for label, obj in groups:
                    for plugin in obj.get("plugins", []):
                        for area in plugin.get("fileareas", []):
                            for f in area.get("files", []):
                                key = f"c{cid}/assign{a['id']}/{label}/{area.get('area')}{f.get('filepath', '/')}{f['filename']}"
                                self.sync_item(self.file_item(key, apath + [label], f["filename"], f, cid))

    def sync_forums(self, cid, cname):
        try:
            forums = self.m.call("mod_forum_get_forums_by_courses", courseids=[cid])
        except MoodleError as e:
            log.warning("Foros no disponibles en %s: %s", cname, e)
            return
        for fo in forums:
            fpath = self.cm_paths.get(fo.get("cmid"), [cname]) + ["Foros", clean(fo["name"])]
            page = 0
            while True:
                try:
                    res = self.m.call("mod_forum_get_forum_discussions", forumid=fo["id"], page=page, perpage=50)
                except MoodleError as e:
                    log.warning("Foro %s: %s", fo["name"], e)
                    break
                discussions = res.get("discussions", [])
                for d in discussions:
                    self.sync_discussion(cid, fpath, d)
                if len(discussions) < 50:
                    break
                page += 1

    def sync_discussion(self, cid, fpath, d):
        did = d["discussion"]
        try:
            posts = self.m.call("mod_forum_get_discussion_posts", discussionid=did,
                                sortby="created", sortdirection="ASC")["posts"]
        except MoodleError as e:
            log.warning("Discusión %s: %s", d.get("name"), e)
            return
        dname = clean(d.get("name") or d.get("subject"), 100)
        body = [f"<h1>{html.escape(d.get('name') or '')}</h1>"]
        for p in posts:
            when = dt.datetime.fromtimestamp(p.get("timecreated", 0)).strftime("%Y-%m-%d %H:%M")
            author = (p.get("author") or {}).get("fullname", "")
            body.append(f"<hr><h3>{html.escape(p.get('subject') or '')}</h3>"
                        f"<p><i>{html.escape(author)} - {when}</i></p>{p.get('message') or ''}")
            for f in p.get("attachments", []) or []:
                key = f"c{cid}/disc{did}/post{p['id']}/{f['filename']}"
                self.sync_item(self.file_item(key, fpath + [dname], f["filename"], f, cid))
        page = "<html><head><meta charset='utf-8'></head><body>" + "\n".join(body) + "</body></html>"
        self.sync_item(self.text_item(f"c{cid}/disc{did}", fpath, f"{dname}.html", page, cid))

    def sync_grades(self, cid, cname):
        try:
            res = self.m.call("gradereport_user_get_grade_items", courseid=cid, userid=self.m.userid)
        except MoodleError as e:
            log.debug("Notas no disponibles en %s: %s", cname, e)
            return
        rows = []
        for ug in res.get("usergrades", []):
            for g in ug.get("gradeitems", []):
                rows.append("<tr>" + "".join(f"<td>{html.escape(str(x if x is not None else ''))}</td>" for x in (
                    g.get("itemname") or g.get("itemtype"), g.get("gradeformatted"), g.get("rangeformatted"),
                    g.get("percentageformatted"))) + f"<td>{g.get('feedback') or ''}</td></tr>")
        if rows:
            page = ("<html><head><meta charset='utf-8'></head><body>"
                    f"<h1>Calificaciones - {html.escape(cname)}</h1><table border=1 cellpadding=4>"
                    "<tr><th>Elemento</th><th>Nota</th><th>Rango</th><th>%</th><th>Comentario</th></tr>"
                    + "".join(rows) + "</table></body></html>")
            self.sync_item(self.text_item(f"c{cid}/grades", [cname], "_Calificaciones.html", page, cid))


# --------------------------------------------------------------------------- main

def setup_logging(verbose: bool):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(os.path.join(DATA_DIR, "sync.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("googleapiclient", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)  # reintentos automáticos ante 500


def sync_once(dry_run: bool):
    user, password = os.environ.get("MOODLE_USER"), os.environ.get("MOODLE_PASSWORD")
    if not MOODLE_URL or not user or not password:
        raise SystemExit("Define MOODLE_URL, MOODLE_USER y MOODLE_PASSWORD en .env")
    moodle = Moodle(MOODLE_URL, user, password)
    drive = None if dry_run else Drive()
    Syncer(moodle, drive, dry_run).run()


def main():
    ap = argparse.ArgumentParser(description="Sincroniza Moodle con Google Drive")
    ap.add_argument("--loop", type=int, metavar="MIN", default=int(os.environ.get("SYNC_INTERVAL_MINUTES") or 0),
                    help="repetir cada MIN minutos (por defecto: SYNC_INTERVAL_MINUTES o una sola vez)")
    ap.add_argument("--dry-run", action="store_true", help="solo mostrar qué haría, sin tocar Drive")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)
    while True:
        try:
            sync_once(args.dry_run)
        except MoodleError as e:
            log.error("%s", e)
        except Exception:
            log.exception("Fallo inesperado")
        if not args.loop:
            break
        log.debug("Próxima sincronización en %d min", args.loop)
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
