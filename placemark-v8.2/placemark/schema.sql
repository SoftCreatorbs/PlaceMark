-- Marketplace de cursos — esquema do banco
-- Valores em dinheiro são guardados em CENTAVOS (inteiro) para evitar erros de arredondamento.

-- Tabelas criadas pelas atualizações (db.py → MIGRACOES) também são apagadas aqui
DROP TABLE IF EXISTS saques;
DROP TABLE IF EXISTS tentativas;
DROP TABLE IF EXISTS auditoria;
DROP TABLE IF EXISTS assinaturas_vendedor;
DROP TABLE IF EXISTS acessos;
DROP TABLE IF EXISTS pedidos;
DROP TABLE IF EXISTS aulas;
DROP TABLE IF EXISTS cursos;
DROP TABLE IF EXISTS empresas;
DROP TABLE IF EXISTS usuarios;
DROP TABLE IF EXISTS configuracoes;
DROP TABLE IF EXISTS planos_vendedor;

CREATE TABLE usuarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nome        TEXT    NOT NULL,
    email       TEXT    NOT NULL UNIQUE,
    senha_hash  TEXT    NOT NULL,
    telefone    TEXT,
    papel       TEXT    NOT NULL DEFAULT 'aluno' CHECK (papel IN ('aluno', 'empresa', 'admin')),
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Planos que os VENDEDORES pagam para poder vender (definidos pelo admin)
CREATE TABLE planos_vendedor (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nome           TEXT    NOT NULL,
    preco_mensal   INTEGER NOT NULL,           -- centavos
    limite_cursos  INTEGER,                    -- NULL = ilimitado
    descricao      TEXT    NOT NULL DEFAULT '',
    checkout_url   TEXT,                       -- link do checkout externo (ex.: Kiwify)
    ativo          INTEGER NOT NULL DEFAULT 1,
    ordem          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE empresas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id    INTEGER NOT NULL UNIQUE REFERENCES usuarios(id) ON DELETE CASCADE,
    nome_fantasia TEXT    NOT NULL,
    documento     TEXT    NOT NULL,           -- CNPJ ou CPF
    descricao     TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pendente'
                  CHECK (status IN ('pendente', 'aprovada', 'rejeitada', 'bloqueada')),
    oficial       INTEGER NOT NULL DEFAULT 0,  -- 1 = loja da própria plataforma (sem plano)
    plano_id      INTEGER REFERENCES planos_vendedor(id),
    plano_expira_em TEXT,
    -- Conta Mercado Pago do vendedor (conectada por OAuth)
    mp_user_id       TEXT,
    mp_access_token  TEXT,
    mp_refresh_token TEXT,
    mp_public_key    TEXT,
    mp_expira_em     TEXT,
    mp_conectado_em  TEXT,
    mp_teste         INTEGER NOT NULL DEFAULT 0,
    criado_em     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE cursos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id       INTEGER NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
    titulo           TEXT    NOT NULL,
    descricao        TEXT    NOT NULL,
    categoria        TEXT    NOT NULL DEFAULT 'Outros',
    carga_horas      INTEGER NOT NULL DEFAULT 0,
    preco_mensal     INTEGER,                  -- centavos; NULL = não vende mensal
    preco_vitalicio  INTEGER,                  -- centavos; NULL = não vende vitalício
    status           TEXT    NOT NULL DEFAULT 'rascunho'
                     CHECK (status IN ('rascunho', 'em_analise', 'publicado', 'rejeitado')),
    motivo_rejeicao  TEXT,
    criado_em        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (preco_mensal IS NOT NULL OR preco_vitalicio IS NOT NULL)
);

CREATE TABLE aulas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    curso_id   INTEGER NOT NULL REFERENCES cursos(id) ON DELETE CASCADE,
    titulo     TEXT    NOT NULL,
    video_url  TEXT,
    conteudo   TEXT    NOT NULL DEFAULT '',
    ordem      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE pedidos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    aluno_id       INTEGER NOT NULL REFERENCES usuarios(id),
    curso_id       INTEGER NOT NULL REFERENCES cursos(id),
    empresa_id     INTEGER NOT NULL REFERENCES empresas(id),
    plano          TEXT    NOT NULL CHECK (plano IN ('mensal', 'vitalicio')),
    valor          INTEGER NOT NULL,           -- total pago pelo aluno
    comissao       INTEGER NOT NULL,           -- parte da plataforma
    valor_empresa  INTEGER NOT NULL,           -- parte da empresa
    metodo         TEXT    NOT NULL,           -- pix / cartao / boleto / mercadopago
    status         TEXT    NOT NULL DEFAULT 'pago'
                   CHECK (status IN ('pendente', 'pago', 'recusado', 'reembolsado')),
    mp_preferencia_id TEXT,                    -- cobrança criada no Mercado Pago
    mp_pagamento_id   TEXT,                    -- pagamento confirmado no Mercado Pago
    atualizado_em  TEXT,
    criado_em      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pedidos_mp ON pedidos (mp_pagamento_id);

CREATE TABLE acessos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    aluno_id   INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    curso_id   INTEGER NOT NULL REFERENCES cursos(id) ON DELETE CASCADE,
    plano      TEXT    NOT NULL CHECK (plano IN ('mensal', 'vitalicio')),
    expira_em  TEXT,                           -- NULL = vitalício
    criado_em  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (aluno_id, curso_id)
);

-- Pagamentos de plano feitos pelos vendedores (receita da plataforma)
CREATE TABLE assinaturas_vendedor (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id  INTEGER NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
    plano_id    INTEGER NOT NULL REFERENCES planos_vendedor(id),
    valor       INTEGER NOT NULL,
    metodo      TEXT    NOT NULL,
    criado_em   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Segurança: tentativas de login e registro de atividades
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

CREATE TABLE configuracoes (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);

INSERT INTO configuracoes (chave, valor) VALUES ('comissao_percentual', '7');

INSERT INTO planos_vendedor (nome, preco_mensal, limite_cursos, descricao, ordem) VALUES
 ('Standard', 5000, 1, 'Para começar: 1 curso à venda.', 1),
 ('Pro', 12000, 5, 'Para quem já tem alguns cursos: até 5 à venda.', 2),
 ('Premium', 25000, NULL, 'Cursos ilimitados à venda.', 3);
