import json
import os
import sqlite3
import uuid
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "banco.db")
DATA_DIR = os.path.join(BASE_DIR, "data")


@contextmanager
def conectar():
    conexao = sqlite3.connect(DB_PATH, timeout=20)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def _json(valor):
    return json.dumps(valor, ensure_ascii=False)


def inicializar_banco():
    os.makedirs(DATA_DIR, exist_ok=True)
    with conectar() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS series (
                slug TEXT PRIMARY KEY,
                dados TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alunos (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                id TEXT NOT NULL UNIQUE,
                serie_slug TEXT,
                dados TEXT NOT NULL,
                FOREIGN KEY (serie_slug) REFERENCES series(slug)
                    ON UPDATE CASCADE ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS cadernos (
                email TEXT NOT NULL,
                chave TEXT NOT NULL,
                dados TEXT NOT NULL,
                PRIMARY KEY (email, chave),
                FOREIGN KEY (email) REFERENCES alunos(email)
                    ON UPDATE CASCADE ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS agenda (
                email TEXT NOT NULL,
                evento_id TEXT NOT NULL,
                dados TEXT NOT NULL,
                PRIMARY KEY (email, evento_id),
                FOREIGN KEY (email) REFERENCES alunos(email)
                    ON UPDATE CASCADE ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS progresso (
                email TEXT PRIMARY KEY,
                dados TEXT NOT NULL,
                FOREIGN KEY (email) REFERENCES alunos(email)
                    ON UPDATE CASCADE ON DELETE CASCADE
            );
        """)
        # Importação única: nunca sobrescreve um banco já existente.
        if db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0:
            caminho = os.path.join(DATA_DIR, "vagas.json")
            if os.path.exists(caminho):
                with open(caminho, encoding="utf-8") as f:
                    origem = json.load(f)
                for serie in origem.get("series", []):
                    db.execute("INSERT OR IGNORE INTO series VALUES (?, ?)",
                               (serie["slug"], _json(serie)))
                for aluno in origem.get("alunos", []):
                    aluno = dict(aluno)
                    aluno.setdefault("id", uuid.uuid4().hex)
                    aluno["email"] = aluno["email"].strip().lower()
                    db.execute(
                        "INSERT OR IGNORE INTO alunos(email,id,serie_slug,dados) VALUES(?,?,?,?)",
                        (aluno["email"], aluno["id"], aluno.get("serie"), _json(aluno))
                    )
        _importar_cadernos(db)
        _importar_agenda(db)
        _importar_progresso(db)


def _ler_origem(nome, padrao):
    caminho = os.path.join(DATA_DIR, nome)
    if not os.path.exists(caminho):
        return padrao
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return padrao


def _importar_cadernos(db):
    if db.execute("SELECT COUNT(*) FROM cadernos").fetchone()[0]:
        return
    origem = _ler_origem("cadernos.json", {})
    for email, materias in origem.items():
        for chave, anotacoes in materias.items():
            db.execute("INSERT OR IGNORE INTO cadernos VALUES(?,?,?)",
                       (email.lower(), chave, _json(anotacoes)))


def _importar_agenda(db):
    if db.execute("SELECT COUNT(*) FROM agenda").fetchone()[0]:
        return
    origem = _ler_origem("agenda.json", {})
    for email, eventos in origem.items():
        for evento in eventos:
            evento_id = str(evento.get("id") or uuid.uuid4().hex)
            evento["id"] = evento_id
            db.execute("INSERT OR IGNORE INTO agenda VALUES(?,?,?)",
                       (email.lower(), evento_id, _json(evento)))


def _importar_progresso(db):
    if db.execute("SELECT COUNT(*) FROM progresso").fetchone()[0]:
        return
    origem = _ler_origem("progresso.json", {}).get("usuarios", {})
    for email, dados in origem.items():
        db.execute("INSERT OR IGNORE INTO progresso VALUES(?,?)",
                   (email.lower(), _json(dados)))


def carregar_vagas():
    with conectar() as db:
        series = [_parse(r["dados"]) for r in db.execute("SELECT dados FROM series ORDER BY rowid")]
        alunos = [_parse(r["dados"]) for r in db.execute("SELECT dados FROM alunos ORDER BY rowid")]
    return {"series": series, "alunos": alunos}


def salvar_vagas(dados):
    with conectar() as db:
        # Séries primeiro para respeitar a chave estrangeira.
        for serie in dados.get("series", []):
            db.execute("INSERT INTO series(slug,dados) VALUES(?,?) "
                       "ON CONFLICT(slug) DO UPDATE SET dados=excluded.dados",
                       (serie["slug"], _json(serie)))
        for aluno in dados.get("alunos", []):
            aluno = dict(aluno)
            aluno.setdefault("id", uuid.uuid4().hex)
            email = aluno["email"].strip().lower()
            aluno["email"] = email
            db.execute("INSERT INTO alunos(email,id,serie_slug,dados) VALUES(?,?,?,?) "
                       "ON CONFLICT(email) DO UPDATE SET id=excluded.id, "
                       "serie_slug=excluded.serie_slug, dados=excluded.dados",
                       (email, aluno["id"], aluno.get("serie"), _json(aluno)))
        emails = [a["email"].strip().lower() for a in dados.get("alunos", [])]
        if emails:
            marks = ",".join("?" for _ in emails)
            db.execute(f"DELETE FROM alunos WHERE email NOT IN ({marks})", emails)
        else:
            db.execute("DELETE FROM alunos")


def carregar_cadernos():
    saida = {}
    with conectar() as db:
        for r in db.execute("SELECT email,chave,dados FROM cadernos"):
            saida.setdefault(r["email"], {})[r["chave"]] = _parse(r["dados"])
    return saida


def salvar_cadernos(dados):
    with conectar() as db:
        db.execute("DELETE FROM cadernos")
        for email, materias in dados.items():
            for chave, valor in materias.items():
                db.execute("INSERT INTO cadernos VALUES(?,?,?)",
                           (email.lower(), chave, _json(valor)))


def carregar_agenda():
    saida = {}
    with conectar() as db:
        for r in db.execute("SELECT email,evento_id,dados FROM agenda ORDER BY rowid"):
            saida.setdefault(r["email"], []).append(_parse(r["dados"]))
    return saida


def salvar_agenda(dados):
    with conectar() as db:
        db.execute("DELETE FROM agenda")
        for email, eventos in dados.items():
            for evento in eventos:
                evento_id = str(evento.get("id") or uuid.uuid4().hex)
                evento["id"] = evento_id
                db.execute("INSERT INTO agenda VALUES(?,?,?)",
                           (email.lower(), evento_id, _json(evento)))


def carregar_progresso():
    saida = {"usuarios": {}}
    with conectar() as db:
        for r in db.execute("SELECT email,dados FROM progresso"):
            saida["usuarios"][r["email"]] = _parse(r["dados"])
    return saida


def salvar_progresso(dados):
    with conectar() as db:
        db.execute("DELETE FROM progresso")
        for email, usuario in dados.get("usuarios", {}).items():
            db.execute("INSERT INTO progresso VALUES(?,?)",
                       (email.lower(), _json(usuario)))


def _parse(valor):
    return json.loads(valor)


inicializar_banco()
