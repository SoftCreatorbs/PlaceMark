"""Conexão com o banco SQLite, criação/atualização automática, auxiliares e comandos."""
import os
import shutil
import sqlite3
from pathlib import Path

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf8"))
    db.commit()
    for migracao in MIGRACOES:
        migracao(db)
    db.execute(f"PRAGMA user_version = {len(MIGRACOES)}")
    db.commit()


# ---------- Onde o banco fica ----------
def caminho_padrao():
    """O banco fica FORA da pasta do projeto, para não se perder ao atualizar o site.
    Windows: C:\\Users\\<você>\\PlacemarkDados\\placemark.sqlite
    Dá para trocar com a variável de ambiente PLACEMARK_DB."""
    return os.environ.get("PLACEMARK_DB") or str(Path.home() / "PlacemarkDados" / "placemark.sqlite")


# ---------- Migrações: cada versão nova do site adiciona um item aqui ----------
def _m1_checkout_url(con):
    colunas = {r[1] for r in con.execute("PRAGMA table_info(planos_vendedor)")}
    if "checkout_url" not in colunas:
        con.execute("ALTER TABLE planos_vendedor ADD COLUMN checkout_url TEXT")


def _m2_seguranca(con):
    con.executescript("""
CREATE TABLE IF NOT EXISTS tentativas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chave      TEXT NOT NULL,                    -- ex.: "login-email:fulano@x.com"
    criado_em  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tentativas_chave ON tentativas (chave, criado_em);

CREATE TABLE IF NOT EXISTS auditoria (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id  INTEGER,
    acao        TEXT NOT NULL,
    detalhe     TEXT NOT NULL DEFAULT '',
    ip          TEXT,
    criado_em   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
""")


def _m3_mercado_pago(con):
    """Pedidos ganham os status pendente/recusado e o vínculo com o Mercado Pago;
    vendedores ganham os campos da conta Mercado Pago conectada."""
    colunas_emp = {r[1] for r in con.execute("PRAGMA table_info(empresas)")}
    for nome, tipo in (("mp_user_id", "TEXT"), ("mp_access_token", "TEXT"), ("mp_refresh_token", "TEXT"),
                       ("mp_public_key", "TEXT"), ("mp_expira_em", "TEXT"), ("mp_conectado_em", "TEXT"),
                       ("mp_teste", "INTEGER NOT NULL DEFAULT 0")):
        if nome not in colunas_emp:
            con.execute(f"ALTER TABLE empresas ADD COLUMN {nome} {tipo}")

    colunas_ped = {r[1] for r in con.execute("PRAGMA table_info(pedidos)")}
    if "mp_pagamento_id" in colunas_ped:
        return
    # O SQLite não deixa mudar a regra de status: recria a tabela copiando tudo
    con.commit()
    con.executescript("""
BEGIN;
CREATE TABLE pedidos_novo (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    aluno_id       INTEGER NOT NULL REFERENCES usuarios(id),
    curso_id       INTEGER NOT NULL REFERENCES cursos(id),
    empresa_id     INTEGER NOT NULL REFERENCES empresas(id),
    plano          TEXT    NOT NULL CHECK (plano IN ('mensal', 'vitalicio')),
    valor          INTEGER NOT NULL,
    comissao       INTEGER NOT NULL,
    valor_empresa  INTEGER NOT NULL,
    metodo         TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pago'
                   CHECK (status IN ('pendente', 'pago', 'recusado', 'reembolsado')),
    mp_preferencia_id TEXT,
    mp_pagamento_id   TEXT,
    atualizado_em  TEXT,
    criado_em      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
INSERT INTO pedidos_novo (id, aluno_id, curso_id, empresa_id, plano, valor, comissao, valor_empresa, metodo, status, criado_em)
    SELECT id, aluno_id, curso_id, empresa_id, plano, valor, comissao, valor_empresa, metodo, status, criado_em FROM pedidos;
DROP TABLE pedidos;
ALTER TABLE pedidos_novo RENAME TO pedidos;
CREATE INDEX IF NOT EXISTS idx_pedidos_mp ON pedidos (mp_pagamento_id);
COMMIT;
""")


SAQUES_SQL = """
CREATE TABLE IF NOT EXISTS saques (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id         INTEGER NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
    valor              INTEGER NOT NULL,       -- sai do saldo do vendedor (centavos)
    taxa               INTEGER NOT NULL,       -- taxa de saque que fica com a Placemark
    valor_liquido      INTEGER NOT NULL,       -- o que o vendedor recebe no Pix
    pix_tipo           TEXT    NOT NULL,       -- CPF / CNPJ / EMAIL / PHONE / PIX_CODE
    pix_chave          TEXT    NOT NULL,
    titular_tipo       TEXT    NOT NULL,       -- CPF ou CNPJ do dono da conta Mercado Pago conectada
    titular_documento  TEXT    NOT NULL,
    mp_user_id         TEXT,                   -- conta Mercado Pago conectada no momento do pedido
    status             TEXT    NOT NULL DEFAULT 'solicitado'
                       CHECK (status IN ('solicitado', 'processando', 'pago', 'falhou', 'cancelado')),
    envio              TEXT    NOT NULL DEFAULT 'automatico' CHECK (envio IN ('automatico', 'manual')),
    idempotencia       TEXT    NOT NULL UNIQUE, -- impede o Mercado Pago de pagar o mesmo saque duas vezes
    mp_transacao_id    TEXT,
    comprovante        TEXT,
    erro               TEXT,
    tentativas         INTEGER NOT NULL DEFAULT 0,
    criado_em          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    atualizado_em      TEXT,
    pago_em            TEXT
);
CREATE INDEX IF NOT EXISTS idx_saques_empresa ON saques (empresa_id, status);
INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES
    ('mp_modo_recebimento', 'carteira'),
    ('saque_taxa_fixa', '200'),
    ('saque_taxa_percentual', '0'),
    ('saque_minimo', '2000'),
    ('saque_dias_liberacao', '7'),
    ('saque_envio', 'automatico');
"""


def _m4_carteira_saques(con):
    """Carteira Placemark: as vendas caem na conta da Placemark e o vendedor saca por Pix."""
    colunas_ped = {r[1] for r in con.execute("PRAGMA table_info(pedidos)")}
    if "mp_conta" not in colunas_ped:
        # placemark = dinheiro caiu na conta da Placemark (vira saldo do vendedor)
        # vendedor  = split antigo: o dinheiro já caiu direto na conta do vendedor
        con.execute("ALTER TABLE pedidos ADD COLUMN mp_conta TEXT NOT NULL DEFAULT 'placemark'")
        con.execute("""UPDATE pedidos SET mp_conta = 'vendedor' WHERE mp_preferencia_id IS NOT NULL
                       AND empresa_id IN (SELECT id FROM empresas WHERE oficial = 0)""")
    colunas_emp = {r[1] for r in con.execute("PRAGMA table_info(empresas)")}
    for nome in ("mp_documento", "mp_documento_tipo", "mp_email", "mp_nome", "pix_tipo", "pix_chave"):
        if nome not in colunas_emp:
            con.execute(f"ALTER TABLE empresas ADD COLUMN {nome} TEXT")
    con.executescript(SAQUES_SQL)


MIGRACOES = [_m1_checkout_url, _m2_seguranca, _m3_mercado_pago, _m4_carteira_saques]


def preparar_banco(app):
    """Roda ao ligar o site: cria o banco se não existir e aplica as atualizações pendentes."""
    caminho = app.config["DATABASE"]
    os.makedirs(os.path.dirname(caminho), exist_ok=True)

    # Traz o banco da versão antiga (pasta instance) na primeira vez
    antigo = os.path.join(app.instance_path, "marketplace.sqlite")
    if not os.path.exists(caminho) and os.path.exists(antigo):
        shutil.copy2(antigo, caminho)

    novo = not os.path.exists(caminho)
    con = sqlite3.connect(caminho)
    try:
        if novo:
            with app.open_resource("schema.sql") as f:
                con.executescript(f.read().decode("utf8"))
            app.logger.info("Banco criado em %s", caminho)
        # Toda migração é segura de repetir: confere antes de mudar. Por isso rodam sempre.
        for migracao in MIGRACOES:
            migracao(con)
        con.execute(f"PRAGMA user_version = {len(MIGRACOES)}")
        con.commit()
    finally:
        con.close()


def existe_admin():
    return get_db().execute("SELECT 1 FROM usuarios WHERE papel = 'admin' LIMIT 1").fetchone() is not None


# ---------- Auxiliares usados em vários lugares ----------
def config(chave, padrao=None):
    linha = get_db().execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    return linha["valor"] if linha else padrao


def tem_acesso(aluno_id, curso_id):
    """Retorna a linha de acesso se o aluno pode assistir ao curso agora."""
    return get_db().execute(
        """SELECT * FROM acessos
           WHERE aluno_id = ? AND curso_id = ?
             AND (expira_em IS NULL OR expira_em > datetime('now', 'localtime'))""",
        (aluno_id, curso_id),
    ).fetchone()


def plano_da_empresa(empresa):
    """Plano ativo do vendedor (dict) ou None. A loja oficial tem plano ilimitado sem custo."""
    if empresa is None:
        return None
    if empresa["oficial"]:
        return {"id": None, "nome": "Oficial", "limite_cursos": None, "preco_mensal": 0, "expira_em": None}
    linha = get_db().execute(
        """SELECT p.*, e.plano_expira_em AS expira_em,
                  CAST(julianday(e.plano_expira_em) - julianday('now', 'localtime') AS INTEGER) AS dias_restantes
           FROM empresas e JOIN planos_vendedor p ON p.id = e.plano_id
           WHERE e.id = ? AND e.plano_expira_em > datetime('now', 'localtime')""",
        (empresa["id"],),
    ).fetchone()
    return dict(linha) if linha else None


def cursos_ocupando_vaga(empresa_id):
    """Cursos que contam no limite do plano: publicados e em análise."""
    return get_db().execute(
        "SELECT COUNT(*) FROM cursos WHERE empresa_id = ? AND status IN ('publicado', 'em_analise')",
        (empresa_id,),
    ).fetchone()[0]


# Só aparecem no catálogo cursos publicados de vendedores aprovados COM plano ativo
CURSOS_PUBLICOS_SQL = """
    SELECT c.*, e.nome_fantasia AS empresa_nome, e.oficial AS empresa_oficial,
           (SELECT COUNT(*) FROM aulas a WHERE a.curso_id = c.id) AS total_aulas,
           (SELECT COUNT(*) FROM acessos x WHERE x.curso_id = c.id) AS total_alunos
    FROM cursos c JOIN empresas e ON e.id = c.empresa_id
    WHERE c.status = 'publicado' AND e.status = 'aprovada'
      AND (e.oficial = 1 OR e.plano_expira_em > datetime('now', 'localtime'))
"""


# ---------- Comandos ----------
@click.command("init-db")
@click.confirmation_option(prompt="Isso APAGA todos os dados (usuários, cursos, vendas). Continuar?")
def init_db_command():
    """Apaga e recria o banco do zero. Normalmente NÃO é preciso: o site cria o banco sozinho."""
    init_db()
    click.echo("Banco de dados recriado do zero.")


@click.command("create-admin")
@click.option("--nome", prompt="Nome")
@click.option("--email", prompt="E-mail")
@click.option("--senha", prompt="Senha", hide_input=True, confirmation_prompt=True)
def create_admin_command(nome, email, senha):
    """Cria um usuário administrador (só pelo terminal, nunca pelo site)."""
    db = get_db()
    try:
        db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, papel) VALUES (?, ?, ?, 'admin')",
            (nome, email.strip().lower(), generate_password_hash(senha)),
        )
        db.commit()
        click.echo(f"Admin {email} criado.")
    except sqlite3.IntegrityError:
        click.echo("Já existe um usuário com esse e-mail.")


@click.command("seed-demo")
def seed_demo_command():
    """Cria uma empresa de exemplo aprovada, com cursos e aulas, para testar o site."""
    click.echo("Dados de exemplo criados. Vendedor (plano Pro): empresa@demo.com / Demo1234"
               if criar_dados_exemplo() else "Os dados de exemplo já existem.")


def criar_dados_exemplo():
    db = get_db()
    if db.execute("SELECT 1 FROM usuarios WHERE email = 'empresa@demo.com'").fetchone():
        return False

    cur = db.execute(
        "INSERT INTO usuarios (nome, email, senha_hash, papel) VALUES (?, ?, ?, 'empresa')",
        ("Ana Souza", "empresa@demo.com", generate_password_hash("Demo1234")),
    )
    empresa_id = db.execute(
        """INSERT INTO empresas (usuario_id, nome_fantasia, documento, descricao, status, plano_id, plano_expira_em)
           VALUES (?, ?, ?, ?, 'aprovada', 2, datetime('now', 'localtime', '+30 days'))""",
        (cur.lastrowid, "TechLab Cursos", "12.345.678/0001-90", "Cursos práticos de tecnologia e carreira."),
    ).lastrowid

    cursos = [
        ("Informática Básica", "Windows, internet, e-mail, Word e Excel para quem está começando do zero.", "Tecnologia", 40, 2990, 19900),
        ("Excel Avançado", "Fórmulas, tabelas dinâmicas, gráficos e automação para o mercado de trabalho.", "Negócios", 30, 3990, 29700),
        ("Inglês Iniciante", "Conversação e gramática básica para usar no dia a dia e no trabalho.", "Idiomas", 60, 4990, None),
        ("Programação com Python", "Lógica de programação e seus primeiros projetos reais em Python.", "Tecnologia", 50, None, 49700),
    ]
    for titulo, desc, cat, horas, mensal, vitalicio in cursos:
        curso_id = db.execute(
            """INSERT INTO cursos (empresa_id, titulo, descricao, categoria, carga_horas,
                                   preco_mensal, preco_vitalicio, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'publicado')""",
            (empresa_id, titulo, desc, cat, horas, mensal, vitalicio),
        ).lastrowid
        for i, aula in enumerate(["Boas-vindas e visão geral", "Primeiros passos", "Exercício prático"], 1):
            db.execute(
                "INSERT INTO aulas (curso_id, titulo, video_url, conteudo, ordem) VALUES (?, ?, ?, ?, ?)",
                (curso_id, aula, "", f"Conteúdo da aula {i} de {titulo}.", i),
            )
    db.commit()
    return True


def init_app(app):
    preparar_banco(app)
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(create_admin_command)
    app.cli.add_command(seed_demo_command)
