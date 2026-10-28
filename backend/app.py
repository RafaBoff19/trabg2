"""Backend do projeto trabg2: API para a Raspberry Pi + interface web.

Rodar:  python app.py   (abre em http://localhost:5000)
Login padrão: admin / admin123  (troque com as variáveis de ambiente abaixo)
"""
import csv
import io
import os
import sqlite3
import uuid
from datetime import date, datetime
from functools import wraps

from flask import (Flask, Response, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TRABG2_DB", os.path.join(BASE_DIR, "trabg2.db"))
API_KEY = os.environ.get("TRABG2_API_KEY", "trabg2-pi-key")
ADMIN_USER = os.environ.get("TRABG2_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("TRABG2_ADMIN_PASS", "admin123")

TIPOS = ("ENTRADA", "SAIDA", "ACESSO_NEGADO", "TENTATIVA_INVASAO")
FMT = "%Y-%m-%d %H:%M:%S"

app = Flask(__name__)
app.secret_key = os.environ.get("TRABG2_SECRET", "troque-esta-chave")


# ---------------------------------------------------------------- banco
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT UNIQUE NOT NULL,
        senha_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS colaboradores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        matricula TEXT UNIQUE NOT NULL,
        tag TEXT UNIQUE NOT NULL,
        cargo TEXT NOT NULL DEFAULT '',
        acesso INTEGER NOT NULL DEFAULT 0,
        ativo INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT UNIQUE NOT NULL,
        tag TEXT NOT NULL,
        colaborador_id INTEGER,
        tipo TEXT NOT NULL,
        timestamp TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos(timestamp);
    """)
    if not db.execute("SELECT 1 FROM usuarios WHERE usuario=?", (ADMIN_USER,)).fetchone():
        db.execute("INSERT INTO usuarios (usuario, senha_hash) VALUES (?, ?)",
                   (ADMIN_USER, generate_password_hash(ADMIN_PASS)))
    db.commit()
    db.close()


# ---------------------------------------------------------------- auth
def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(erro="não autenticado"), 401
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def chave_api_valida():
    enviada = request.headers.get("X-API-Key") or request.args.get("key")
    return enviada == API_KEY


def api_key_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not chave_api_valida():
            return jsonify(erro="chave de API inválida"), 401
        return view(*a, **kw)
    return wrapper


def login_ou_chave(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not (session.get("user") or chave_api_valida()):
            return jsonify(erro="não autenticado"), 401
        return view(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("usuario", "").strip()
        s = request.form.get("senha", "")
        row = get_db().execute("SELECT * FROM usuarios WHERE usuario=?", (u,)).fetchone()
        if row and check_password_hash(row["senha_hash"], s):
            session["user"] = u
            prox = request.args.get("next") or url_for("monitoramento")
            return redirect(prox if prox.startswith("/") else url_for("monitoramento"))
        flash("Usuário ou senha inválidos.", "erro")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- utilidades
def normalizar_ts(valor):
    """Aceita ISO (com 'T' ou espaço) e devolve 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "")).strftime(FMT)
    except (ValueError, TypeError):
        return datetime.now().strftime(FMT)


def inicio_do_dia():
    return date.today().strftime("%Y-%m-%d") + " 00:00:00"


def estado_hoje(db):
    """Quem está dentro agora e quem já entrou hoje (por tag)."""
    rows = db.execute(
        "SELECT tag, tipo FROM eventos WHERE timestamp >= ? "
        "AND tipo IN ('ENTRADA','SAIDA') ORDER BY timestamp, id", (inicio_do_dia(),)).fetchall()
    ultimo, entraram = {}, set()
    for r in rows:
        ultimo[r["tag"]] = r["tipo"]
        if r["tipo"] == "ENTRADA":
            entraram.add(r["tag"])
    dentro = sorted(t for t, tp in ultimo.items() if tp == "ENTRADA")
    return dentro, sorted(entraram)


def colab_dict(r):
    return {"id": r["id"], "nome": r["nome"], "matricula": r["matricula"], "tag": r["tag"],
            "cargo": r["cargo"], "acesso": bool(r["acesso"]), "ativo": bool(r["ativo"])}


# ---------------------------------------------------------------- API da Raspberry Pi
@app.route("/api/colaboradores")
@api_key_required
def api_colaboradores():
    rows = get_db().execute("SELECT * FROM colaboradores ORDER BY nome").fetchall()
    return jsonify([colab_dict(r) for r in rows])


@app.route("/api/estado")
@api_key_required
def api_estado():
    dentro, entraram = estado_hoje(get_db())
    return jsonify(data=date.today().isoformat(), dentro=dentro, entraram_hoje=entraram)


@app.route("/api/eventos", methods=["POST"])
@api_key_required
def api_eventos():
    d = request.get_json(silent=True) or {}
    tag = str(d.get("tag", "")).strip()
    tipo = d.get("tipo")
    if not tag or tipo not in TIPOS:
        return jsonify(erro="campos 'tag' e 'tipo' válidos são obrigatórios", tipos=TIPOS), 400
    uid = str(d.get("uid") or uuid.uuid4())
    ts = normalizar_ts(d.get("timestamp"))
    db = get_db()
    c = db.execute("SELECT id FROM colaboradores WHERE tag=?", (tag,)).fetchone()
    try:
        cur = db.execute(
            "INSERT INTO eventos (uid, tag, colaborador_id, tipo, timestamp) VALUES (?,?,?,?,?)",
            (uid, tag, c["id"] if c else None, tipo, ts))
        db.commit()
    except sqlite3.IntegrityError:  # reenvio da fila offline: já gravado
        return jsonify(status="duplicado"), 200
    return jsonify(status="ok", id=cur.lastrowid), 201


# ---------------------------------------------------------------- painel
def eventos_query(db, where="", params=(), limite=20):
    return db.execute(
        "SELECT e.id, e.timestamp, e.tag, e.tipo, e.colaborador_id, c.nome, c.matricula "
        "FROM eventos e LEFT JOIN colaboradores c ON c.id = e.colaborador_id "
        f"{where} ORDER BY e.timestamp DESC, e.id DESC LIMIT ?", (*params, limite)).fetchall()


@app.route("/api/painel")
@login_required
def api_painel():
    db = get_db()
    dentro_tags, _ = estado_hoje(db)
    dentro = []
    for t in dentro_tags:
        r = db.execute("SELECT nome, matricula FROM colaboradores WHERE tag=?", (t,)).fetchone()
        dentro.append({"nome": r["nome"] if r else f"Tag {t}", "matricula": r["matricula"] if r else ""})
    cont = {t: 0 for t in TIPOS}
    for r in db.execute("SELECT tipo, COUNT(*) n FROM eventos WHERE timestamp >= ? GROUP BY tipo",
                        (inicio_do_dia(),)):
        cont[r["tipo"]] = r["n"]
    ultimos = [dict(r) for r in eventos_query(db, limite=20)]
    alertas = [dict(r) for r in eventos_query(
        db, "WHERE e.tipo IN ('ACESSO_NEGADO','TENTATIVA_INVASAO')", limite=8)]
    return jsonify(agora=datetime.now().strftime(FMT), contadores=cont,
                   dentro=dentro, ultimos=ultimos, alertas=alertas)


@app.route("/")
@login_required
def index():
    return redirect(url_for("monitoramento"))


@app.route("/monitoramento")
@login_required
def monitoramento():
    return render_template("monitoramento.html")


# ---------------------------------------------------------------- colaboradores (CRUD)
@app.route("/colaboradores")
@login_required
def colaboradores():
    rows = get_db().execute("SELECT * FROM colaboradores ORDER BY nome").fetchall()
    return render_template("colaboradores.html", colaboradores=rows)


def ler_form():
    f = request.form
    return {"nome": f.get("nome", "").strip(), "matricula": f.get("matricula", "").strip(),
            "tag": f.get("tag", "").strip(), "cargo": f.get("cargo", "").strip(),
            "acesso": 1 if f.get("acesso") else 0, "ativo": 1 if f.get("ativo") else 0}


@app.route("/colaboradores/novo", methods=["GET", "POST"])
@login_required
def colaborador_novo():
    if request.method == "POST":
        c = ler_form()
        if not (c["nome"] and c["matricula"] and c["tag"]):
            flash("Nome, matrícula e tag são obrigatórios.", "erro")
        else:
            try:
                db = get_db()
                db.execute("INSERT INTO colaboradores (nome, matricula, tag, cargo, acesso, ativo) "
                           "VALUES (:nome,:matricula,:tag,:cargo,:acesso,:ativo)", c)
                db.commit()
                flash("Colaborador cadastrado.", "ok")
                return redirect(url_for("colaboradores"))
            except sqlite3.IntegrityError:
                flash("Matrícula ou tag já cadastrada.", "erro")
        return render_template("colaborador_form.html", c=c, novo=True)
    return render_template("colaborador_form.html", c={"acesso": 1, "ativo": 1}, novo=True)


@app.route("/colaboradores/<int:cid>/editar", methods=["GET", "POST"])
@login_required
def colaborador_editar(cid):
    db = get_db()
    atual = db.execute("SELECT * FROM colaboradores WHERE id=?", (cid,)).fetchone()
    if not atual:
        flash("Colaborador não encontrado.", "erro")
        return redirect(url_for("colaboradores"))
    if request.method == "POST":
        c = ler_form()
        c["id"] = cid
        if not (c["nome"] and c["matricula"] and c["tag"]):
            flash("Nome, matrícula e tag são obrigatórios.", "erro")
        else:
            try:
                db.execute("UPDATE colaboradores SET nome=:nome, matricula=:matricula, tag=:tag, "
                           "cargo=:cargo, acesso=:acesso, ativo=:ativo WHERE id=:id", c)
                db.commit()
                flash("Colaborador atualizado.", "ok")
                return redirect(url_for("colaboradores"))
            except sqlite3.IntegrityError:
                flash("Matrícula ou tag já cadastrada.", "erro")
        return render_template("colaborador_form.html", c=c, novo=False)
    return render_template("colaborador_form.html", c=dict(atual), novo=False)


@app.route("/colaboradores/<int:cid>/excluir", methods=["POST"])
@login_required
def colaborador_excluir(cid):
    db = get_db()
    db.execute("DELETE FROM colaboradores WHERE id=?", (cid,))
    db.commit()
    flash("Colaborador excluído (os eventos antigos continuam no histórico).", "ok")
    return redirect(url_for("colaboradores"))


# ---------------------------------------------------------------- logs e CSV
@app.route("/logs")
@login_required
def logs():
    dia = request.args.get("data", "").strip()
    tipo = request.args.get("tipo", "").strip()
    cond, params = [], []
    if dia:
        cond.append("substr(e.timestamp,1,10) = ?")
        params.append(dia)
    if tipo in TIPOS:
        cond.append("e.tipo = ?")
        params.append(tipo)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    rows = eventos_query(get_db(), where, params, limite=500)
    return render_template("logs.html", eventos=rows, dia=dia, tipo=tipo, tipos=TIPOS)


@app.route("/api/eventos/export.csv")
@login_ou_chave
def exportar_csv():
    dia = request.args.get("data", "").strip()
    where, params = ("WHERE substr(e.timestamp,1,10) = ?", [dia]) if dia else ("", [])
    rows = get_db().execute(
        "SELECT e.id, e.timestamp, e.tag, e.colaborador_id, c.nome, c.matricula, e.tipo "
        "FROM eventos e LEFT JOIN colaboradores c ON c.id = e.colaborador_id "
        f"{where} ORDER BY e.timestamp, e.id", params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "timestamp", "tag", "colaborador_id", "nome", "matricula", "tipo"])
    for r in rows:
        w.writerow([r["id"], r["timestamp"], r["tag"], r["colaborador_id"] or "",
                    r["nome"] or "", r["matricula"] or "", r["tipo"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=eventos.csv"})


init_db()

if __name__ == "__main__":
    # host 0.0.0.0 permite que a Raspberry Pi acesse pelo IP deste computador
    app.run(host="0.0.0.0", port=5000, debug=False)
