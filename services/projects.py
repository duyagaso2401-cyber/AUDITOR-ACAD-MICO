"""
Persistencia de proyectos de auditoría (borradores y trabajos finalizados).

Diseño pensado para la integración con la base de datos institucional:

    * ``ProjectStore`` es la interfaz. Hoy se usa ``SQLiteProjectStore`` (librería
      estándar, cero dependencias). Para PostgreSQL (Render Postgres) basta con
      implementar los mismos 4 métodos; el esquema SQL ya es compatible.
    * El contexto académico (institucion_id, docente_id, periodo, area_asignatura)
      se guarda en COLUMNAS indexadas para poder filtrar por institución, docente,
      periodo o área/asignatura sin abrir el JSON; el estado completo del informe va
      en ``state_json``.
    * ``owner`` identifica a quién pertenece el proyecto. Hoy es el hash de la API
      key o el identificador anónimo del navegador; cuando existan usuarios y roles
      se reemplaza por el ``user_id`` autenticado y se añaden reglas por rol
      (p. ej. un docente ve los proyectos de sus áreas/asignaturas).
    * Control de concurrencia optimista con ``revision``: si un cliente guarda sobre
      una versión más nueva que la suya, se responde 409 en lugar de sobrescribir.

AVISO Render (plan gratuito): el disco es efímero; los archivos se pierden en cada
redeploy o reinicio. Para producción, monte un Persistent Disk en PROJECTS_DB_PATH
o migre a PostgreSQL.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
PROJECT_STATUSES = ("borrador", "auditado", "reescribiendo", "pausado", "finalizado")
CONTEXT_FIELDS = ("institucion_id", "docente_id", "periodo", "area_asignatura")
# Nombres alternativos aceptados en la entrada (compatibilidad y formularios externos).
CONTEXT_ALIASES = {"área/asignatura": "area_asignatura", "area/asignatura": "area_asignatura",
                   "asignatura": "area_asignatura", "area": "area_asignatura", "área": "area_asignatura",
                   "periodo_academico": "periodo", "período": "periodo"}
MAX_PROJECT_BYTES = int(os.getenv("PROJECT_MAX_BYTES", str(4 * 1024 * 1024)))


class ProjectError(ValueError):
    status = 400


class ProjectNotFound(ProjectError):
    status = 404


class ProjectConflict(ProjectError):
    status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_str(value, limit: int = 200) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] or None


def normalize_project(payload: dict) -> dict:
    """Valida y normaliza el modelo de proyecto que envía el frontend o una integración.

    Modelo (v1):
    {
      "id": "uuid" | null,
      "revision": 3,                       # la que el cliente conoce (opcional)
      "status": "borrador|auditado|reescribiendo|pausado|finalizado",
      "title": "...",
      "context": {"institucion_id", "docente_id", "periodo", "area_asignatura"},
      "document": {"text", "title", "author", "norma", ...},
      "report": {... respuesta de /api/v1/audit ...},
      "rewrite": {"mode", "items": {"12": {"status", "original", "rewritten", ...}}, "paused_until"}
    }
    """
    if not isinstance(payload, dict):
        raise ProjectError("El proyecto debe ser un objeto JSON")
    size = len(json.dumps(payload, ensure_ascii=False).encode())
    if size > MAX_PROJECT_BYTES:
        raise ProjectError(f"El proyecto excede el tamaño máximo ({MAX_PROJECT_BYTES // 1024} KB)")

    project_id = payload.get("id")
    if project_id:
        try:
            project_id = str(uuid.UUID(str(project_id)))
        except ValueError:
            raise ProjectError("'id' debe ser un UUID válido") from None

    status = payload.get("status") or "borrador"
    if status not in PROJECT_STATUSES:
        raise ProjectError(f"'status' debe ser uno de: {', '.join(PROJECT_STATUSES)}")

    ctx_in = payload.get("context") or {}
    if not isinstance(ctx_in, dict):
        raise ProjectError("'context' debe ser un objeto")
    ctx_norm = dict(ctx_in)
    for alias, field in CONTEXT_ALIASES.items():
        if ctx_in.get(alias) and not ctx_in.get(field):
            ctx_norm[field] = ctx_in[alias]
    context = {f: _clean_str(ctx_norm.get(f)) for f in CONTEXT_FIELDS}

    document = payload.get("document") or {}
    if not isinstance(document, dict):
        raise ProjectError("'document' debe ser un objeto")

    revision = payload.get("revision")
    try:
        revision = int(revision) if revision is not None else None
    except (TypeError, ValueError):
        raise ProjectError("'revision' debe ser un entero") from None

    return {
        "id": project_id,
        "revision": revision,
        "status": status,
        "title": _clean_str(payload.get("title") or document.get("title"), 300) or "Proyecto sin título",
        "norma": _clean_str(document.get("norma") or (payload.get("report") or {}).get("institution", {}).get("id"), 60),
        "context": context,
        "state": {
            "schema_version": SCHEMA_VERSION,
            "document": document,
            "report": payload.get("report"),
            "rewrite": payload.get("rewrite") or {},
            "client_saved_at": payload.get("saved_at"),
        },
    }


class ProjectStore:
    """Interfaz de almacenamiento. Implementar para Postgres/MySQL con la misma semántica."""

    def save(self, project: dict, owner: str) -> dict: ...
    def load(self, project_id: str, owner: str) -> dict: ...
    def list(self, owner: str, filters: dict | None = None, limit: int = 20) -> list[dict]: ...
    def delete(self, project_id: str, owner: str) -> None: ...


class SQLiteProjectStore(ProjectStore):
    _DDL = """
    CREATE TABLE IF NOT EXISTS projects (
        id                TEXT PRIMARY KEY,
        owner             TEXT NOT NULL,
        revision          INTEGER NOT NULL DEFAULT 1,
        status            TEXT NOT NULL DEFAULT 'borrador',
        title             TEXT,
        norma             TEXT,
        institucion_id    TEXT,
        docente_id        TEXT,
        periodo           TEXT,
        area_asignatura   TEXT,
        state_json        TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_projects_owner   ON projects(owner, updated_at DESC);
    """
    _INDEXES = """
    CREATE INDEX IF NOT EXISTS ix_projects_context ON projects(institucion_id, docente_id, periodo, area_asignatura);
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._DDL)
        self._migrate()
        self._conn.executescript(self._INDEXES)

    def _migrate(self) -> None:
        """Añade columnas nuevas a bases creadas con versiones anteriores del esquema."""
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(projects)")}
        for col in CONTEXT_FIELDS:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT")
        if "asignatura" in existing:        # esquema v1 anterior: copiar asignatura -> area_asignatura
            self._conn.execute("UPDATE projects SET area_asignatura = asignatura "
                               "WHERE area_asignatura IS NULL AND asignatura IS NOT NULL")
        self._conn.commit()

    def save(self, project: dict, owner: str) -> dict:
        now = _now()
        ctx = project["context"]
        state_json = json.dumps(project["state"], ensure_ascii=False)
        with self._lock, self._conn:
            row = None
            if project["id"]:
                row = self._conn.execute("SELECT owner, revision, created_at FROM projects WHERE id=?",
                                         (project["id"],)).fetchone()
            if row is None:
                pid = project["id"] or str(uuid.uuid4())
                self._conn.execute(
                    """INSERT INTO projects (id, owner, revision, status, title, norma, institucion_id,
                       docente_id, periodo, area_asignatura, state_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, owner, 1, project["status"], project["title"], project["norma"],
                     ctx["institucion_id"], ctx["docente_id"], ctx["periodo"], ctx["area_asignatura"],
                     state_json, now, now))
                return {"id": pid, "revision": 1, "created_at": now, "updated_at": now, "created": True}

            if row["owner"] != owner:
                # No se revela la existencia del proyecto a otro propietario.
                raise ProjectNotFound("Proyecto no encontrado")
            if project["revision"] is not None and project["revision"] < row["revision"]:
                raise ProjectConflict(f"El servidor tiene una versión más reciente (revisión {row['revision']})")
            revision = row["revision"] + 1
            self._conn.execute(
                """UPDATE projects SET revision=?, status=?, title=?, norma=?, institucion_id=?, docente_id=?,
                   periodo=?, area_asignatura=?, state_json=?, updated_at=? WHERE id=?""",
                (revision, project["status"], project["title"], project["norma"], ctx["institucion_id"],
                 ctx["docente_id"], ctx["periodo"], ctx["area_asignatura"], state_json, now, project["id"]))
            return {"id": project["id"], "revision": revision, "created_at": row["created_at"],
                    "updated_at": now, "created": False}

    def load(self, project_id: str, owner: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None or row["owner"] != owner:
            raise ProjectNotFound("Proyecto no encontrado")
        state = json.loads(row["state_json"])
        return {
            "id": row["id"], "revision": row["revision"], "status": row["status"], "title": row["title"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "context": {f: row[f] for f in CONTEXT_FIELDS},
            "document": state.get("document") or {}, "report": state.get("report"),
            "rewrite": state.get("rewrite") or {}, "schema_version": state.get("schema_version"),
        }

    def list(self, owner: str, filters: dict | None = None, limit: int = 20) -> list[dict]:
        clauses, params = ["owner=?"], [owner]
        for key, value in (filters or {}).items():
            if key in CONTEXT_FIELDS + ("status", "norma") and value:
                clauses.append(f"{key}=?")
                params.append(value)
        sql = (f"SELECT id, revision, status, title, norma, {', '.join(CONTEXT_FIELDS)}, created_at, updated_at "
               f"FROM projects WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, (*params, max(1, min(limit, 100)))).fetchall()
        return [dict(r) for r in rows]

    def delete(self, project_id: str, owner: str) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM projects WHERE id=? AND owner=?", (project_id, owner))
        if cur.rowcount == 0:
            raise ProjectNotFound("Proyecto no encontrado")


def create_store() -> ProjectStore:
    return SQLiteProjectStore(os.getenv("PROJECTS_DB_PATH", os.path.join("data", "projects.sqlite3")))
