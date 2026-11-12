"""Carteira Placemark: saldo dos vendedores e saques por Pix.

Como o dinheiro anda (modo carteira, o padrão):
1. O aluno paga no Mercado Pago e o dinheiro inteiro cai na conta Mercado Pago da Placemark.
2. A parte do vendedor (venda menos a comissão) vira SALDO dele aqui no site.
3. Cada venda fica "a liberar" por alguns dias (Admin → Saques → Prazo de liberação). Isso protege
   a Placemark de estornos e chargebacks de cartão depois que o dinheiro já foi repassado.
4. Com a conta Mercado Pago dele conectada, o vendedor pede o saque e confirma com a senha.
5. O site envia um Pix da conta da Placemark para a chave Pix do vendedor pela API de saques do
   Mercado Pago (Money Out), JÁ DESCONTANDO a taxa de saque. A chave Pix precisa ser do mesmo
   CPF/CNPJ da conta Mercado Pago conectada: o Mercado Pago recusa chave de outra pessoa.
6. Se o envio automático não estiver disponível, o saque cai na fila do admin, que paga pelo app
   do Mercado Pago e marca como pago. O saldo nunca é descontado duas vezes.

Segurança:
- O saldo é recalculado a partir das vendas e dos saques toda vez (não existe um número "saldo"
  guardado que alguém possa alterar).
- O pedido de saque roda numa transação exclusiva do banco: dois cliques ao mesmo tempo não sacam
  o mesmo dinheiro duas vezes.
- Cada saque tem uma chave de idempotência: reenviar ao Mercado Pago nunca paga duas vezes.
- Avisos (webhook) do Mercado Pago nunca são confiados: o site sempre consulta a API.
"""
import re
import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash

import db
import mercadopago as mp
import seguranca
from auth import papel_obrigatorio
from empresa import empresa_aprovada

bp = Blueprint("saque", __name__)

FORMATO = "%Y-%m-%d %H:%M:%S"
EM_ANDAMENTO = ("solicitado", "processando")

PADROES = {
    "saque_taxa_fixa": "200",          # centavos (R$ 2,00 por saque)
    "saque_taxa_percentual": "0",      # % sobre o valor sacado
    "saque_minimo": "2000",            # centavos (R$ 20,00)
    "saque_dias_liberacao": "7",       # dias até a venda poder ser sacada
    "saque_envio": "automatico",       # automatico = API do Mercado Pago; manual = fila do admin
}

TIPOS_CHAVE = {
    "CPF": "CPF",
    "CNPJ": "CNPJ",
    "EMAIL": "E-mail",
    "PHONE": "Celular",
    "PIX_CODE": "Chave aleatória",
}


# =====================================================================
# Regras (Admin → Saques)
# =====================================================================
def regra(chave):
    return db.config(chave, PADROES[chave]) or PADROES[chave]


def regra_int(chave):
    try:
        return max(0, int(regra(chave)))
    except ValueError:
        return int(PADROES[chave])


def taxa_percentual():
    try:
        return max(0.0, min(50.0, float(regra("saque_taxa_percentual").replace(",", "."))))
    except ValueError:
        return 0.0


def calcular_taxa(valor):
    """Taxa de saque em centavos para um saque de `valor` centavos."""
    return regra_int("saque_taxa_fixa") + round(valor * taxa_percentual() / 100)


def envio_automatico():
    return regra("saque_envio") == "automatico" and mp.ativo()


# =====================================================================
# Saldo e números do painel
# =====================================================================
def _data_venda():
    # Venda paga pelo Mercado Pago: conta a partir da aprovação. Simulada: da criação.
    return "COALESCE(atualizado_em, criado_em)"


def saldo(empresa_id):
    """Tudo em centavos. Recalculado a partir das vendas e dos saques.
    vendas/bruto/comissao/creditado contam todas as vendas pagas; o saldo só conta as que caíram
    na conta da Placemark (as do modo split já estão na conta do vendedor)."""
    dias = regra_int("saque_dias_liberacao")
    corte = (datetime.now() - timedelta(days=dias)).strftime(FORMATO)
    c = db.get_db()
    v = c.execute(
        f"""SELECT COUNT(*) AS vendas,
                   COALESCE(SUM(valor), 0) AS bruto,
                   COALESCE(SUM(comissao), 0) AS comissao,
                   COALESCE(SUM(valor_empresa), 0) AS creditado,
                   COALESCE(SUM(CASE WHEN mp_conta = 'placemark' AND {_data_venda()} <= ? THEN valor_empresa END), 0) AS liberado,
                   COALESCE(SUM(CASE WHEN mp_conta = 'placemark' AND {_data_venda()} > ? THEN valor_empresa END), 0) AS a_liberar,
                   MIN(CASE WHEN mp_conta = 'placemark' AND {_data_venda()} > ? THEN {_data_venda()} END) AS proxima_venda
            FROM pedidos WHERE empresa_id = ? AND status = 'pago'""",
        (corte, corte, corte, empresa_id)).fetchone()
    s = c.execute(
        """SELECT COALESCE(SUM(CASE WHEN status IN ('solicitado', 'processando') THEN valor END), 0) AS em_andamento,
                  COALESCE(SUM(CASE WHEN status = 'pago' THEN valor END), 0) AS sacado,
                  COALESCE(SUM(CASE WHEN status = 'pago' THEN valor_liquido END), 0) AS recebido,
                  COALESCE(SUM(CASE WHEN status = 'pago' THEN taxa END), 0) AS taxas
           FROM saques WHERE empresa_id = ?""", (empresa_id,)).fetchone()
    proxima = None
    if v["proxima_venda"]:
        proxima = (datetime.strptime(v["proxima_venda"], FORMATO) + timedelta(days=dias)).strftime(FORMATO)
    return {
        "vendas": v["vendas"], "bruto": v["bruto"], "comissao": v["comissao"], "creditado": v["creditado"],
        "liberado": v["liberado"], "a_liberar": v["a_liberar"], "proxima_liberacao": proxima,
        "em_andamento": s["em_andamento"], "sacado": s["sacado"], "recebido": s["recebido"], "taxas": s["taxas"],
        # Pode ficar negativo se uma venda já sacada for estornada: é descontado das próximas vendas.
        "disponivel": v["liberado"] - s["em_andamento"] - s["sacado"],
        "dias_liberacao": dias,
    }


MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def series_vendas(empresa_id):
    """Quanto o vendedor ganhou (já sem comissão) por dia (30 dias) e por mês (12 meses)."""
    c = db.get_db()
    hoje = date.today()
    inicio_dias = hoje - timedelta(days=29)
    por_dia = {r["d"]: r for r in c.execute(
        f"""SELECT substr({_data_venda()}, 1, 10) AS d, COUNT(*) AS n, SUM(valor_empresa) AS v
            FROM pedidos WHERE empresa_id = ? AND status = 'pago' AND {_data_venda()} >= ?
            GROUP BY d""", (empresa_id, inicio_dias.isoformat())).fetchall()}
    dias = []
    for i in range(30):
        d = inicio_dias + timedelta(days=i)
        r = por_dia.get(d.isoformat())
        dias.append({"rotulo": f"{d.day:02d}/{d.month:02d}", "valor": r["v"] if r else 0, "vendas": r["n"] if r else 0})

    primeiro_mes = date(hoje.year - (1 if hoje.month < 12 else 0), (hoje.month % 12) + 1, 1)
    por_mes = {r["m"]: r for r in c.execute(
        f"""SELECT substr({_data_venda()}, 1, 7) AS m, COUNT(*) AS n, SUM(valor_empresa) AS v
            FROM pedidos WHERE empresa_id = ? AND status = 'pago' AND {_data_venda()} >= ?
            GROUP BY m""", (empresa_id, primeiro_mes.isoformat())).fetchall()}
    meses = []
    ano, mes = primeiro_mes.year, primeiro_mes.month
    for _ in range(12):
        r = por_mes.get(f"{ano:04d}-{mes:02d}")
        meses.append({"rotulo": f"{MESES[mes - 1]}/{str(ano)[2:]}", "valor": r["v"] if r else 0, "vendas": r["n"] if r else 0})
        mes += 1
        if mes > 12:
            ano, mes = ano + 1, 1
    return {"dias": dias, "meses": meses}


# =====================================================================
# Chave Pix e titular
# =====================================================================
def normalizar_chave(tipo, valor):
    """Confere e padroniza a chave Pix. Erro -> ValueError com mensagem para o vendedor."""
    valor = (valor or "").strip()
    digitos = re.sub(r"\D", "", valor)
    if tipo == "CPF":
        if len(digitos) != 11:
            raise ValueError("O CPF precisa ter 11 dígitos.")
        return digitos
    if tipo == "CNPJ":
        if len(digitos) != 14:
            raise ValueError("O CNPJ precisa ter 14 dígitos.")
        return digitos
    if tipo == "EMAIL":
        valor = valor.lower()
        if len(valor) > 77 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", valor):
            raise ValueError("Informe um e-mail válido.")
        return valor
    if tipo == "PHONE":
        if digitos.startswith("55") and len(digitos) in (12, 13):
            digitos = digitos[2:]
        if len(digitos) not in (10, 11):
            raise ValueError("Informe o celular com DDD (ex.: 11 91234-5678).")
        return "+55" + digitos
    if tipo == "PIX_CODE":
        valor = valor.lower()
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", valor):
            raise ValueError("A chave aleatória tem o formato 123e4567-e89b-12d3-a456-426614174000.")
        return valor
    raise ValueError("Escolha o tipo da chave Pix.")


def titular(empresa):
    """(tipo, número) do dono da conta Mercado Pago conectada. Cai para o documento do cadastro."""
    if empresa["mp_documento"] and empresa["mp_documento_tipo"]:
        return empresa["mp_documento_tipo"], empresa["mp_documento"]
    digitos = re.sub(r"\D", "", empresa["documento"] or "")
    if len(digitos) == 11:
        return "CPF", digitos
    if len(digitos) == 14:
        return "CNPJ", digitos
    return None, None


def mascarar_chave(tipo, chave):
    if not chave:
        return ""
    if tipo in ("CPF", "CNPJ"):
        return "•••" + chave[3:-2] + "••" if len(chave) > 6 else chave
    if tipo == "EMAIL":
        nome, _, dominio = chave.partition("@")
        return nome[:2] + "•••@" + dominio
    if tipo == "PHONE":
        return chave[:5] + "•••••" + chave[-4:]
    return chave[:8] + "…" + chave[-4:]


# =====================================================================
# Envio pelo Mercado Pago (API de saques / Money Out)
# =====================================================================
ERROS_DA_CHAVE = ("chave", "different owner", "identification", "account is disabled",
                  "unrecognized key", "financial identity", "participant is not available")


def _classificar(erro):
    """incerto = não sabemos se o Pix saiu (reenviar é seguro, mesma chave de idempotência);
    chave = problema na chave Pix do vendedor (saque falha e o saldo volta);
    plataforma = problema do lado da Placemark (credencial, assinatura, saldo da conta): vai para a fila."""
    if erro.status is None or erro.status >= 500 or erro.status in (408, 424):
        return "incerto"
    texto = erro.detalhe.lower()
    if erro.status == 400 and any(t in texto for t in ERROS_DA_CHAVE):
        return "chave"
    return "plataforma"


def _resumo_erro(erro):
    detalhe = re.sub(r"\s+", " ", erro.detalhe)[:180]
    return f"{erro} {detalhe}".strip()


def _corpo_pix(s):
    valor = round(s["valor_liquido"] / 100, 2)
    corpo = {
        "external_reference": f"saque{s['id']}",
        "point_of_interaction": {"type": "PSP_TRANSFER"},
        "transaction": {
            "from": {"accounts": [{"amount": valor}]},
            "to": {"accounts": [{
                "type": "current",
                "amount": valor,
                "chave": {"type": s["pix_tipo"], "value": s["pix_chave"]},
                "owner": {"identification": {"type": s["titular_tipo"], "number": s["titular_documento"]}},
            }]},
            "total_amount": valor,
        },
    }
    if mp.cfg("mp_site_url"):
        corpo["seller_configuration"] = {
            "notification_info": {"notification_url": mp.url_publica("saque.webhook", s=s["id"])}}
    return corpo


def _cabecalhos(s):
    cab = {"X-Idempotency-Key": s["idempotencia"], "X-enforce-signature": "false"}
    if mp.modo_teste():
        cab["X-test-token"] = "true"
    return cab


def _atualizar(saque_id, **campos):
    campos["atualizado_em"] = datetime.now().strftime(FORMATO)
    sets = ", ".join(f"{k} = ?" for k in campos)
    conexao = db.get_db()
    # Só mexe em saque ainda em andamento: um saque pago, falho ou cancelado nunca muda de novo
    mudou = conexao.execute(f"UPDATE saques SET {sets} WHERE id = ? AND status IN ('solicitado', 'processando')",
                            (*campos.values(), saque_id)).rowcount
    conexao.commit()
    return mudou


def aplicar_resultado(saque_id, resposta):
    """Aplica a resposta do Mercado Pago para um saque. Pode rodar várias vezes."""
    status_mp = str(resposta.get("status") or "").lower()
    transacao = str(resposta.get("id") or "") or None
    if status_mp == "approved":
        _atualizar(saque_id, status="pago", mp_transacao_id=transacao, erro=None,
                   pago_em=datetime.now().strftime(FORMATO))
        seguranca.registrar("saque_pago", f"Saque #{saque_id} (MP {transacao})")
        return "pago"
    if status_mp in ("rejected", "failed", "cancelled", "canceled", "error"):
        _atualizar(saque_id, status="falhou", mp_transacao_id=transacao,
                   erro=f"O Mercado Pago recusou a transferência ({status_mp}).")
        return "falhou"
    _atualizar(saque_id, status="processando", mp_transacao_id=transacao, erro=None)
    return "processando"


def enviar(saque_id):
    """Envia (ou reenvia, com segurança) o Pix de um saque pelo Mercado Pago."""
    conexao = db.get_db()
    s = conexao.execute("SELECT * FROM saques WHERE id = ?", (saque_id,)).fetchone()
    if s is None or s["status"] not in EM_ANDAMENTO:
        return s["status"] if s else None
    if not mp.ativo():
        _atualizar(saque_id, status="solicitado", envio="manual",
                   erro="Mercado Pago não configurado: pague pela fila manual.")
        return "solicitado"
    conexao.execute("UPDATE saques SET tentativas = tentativas + 1, envio = 'automatico' WHERE id = ?", (saque_id,))
    conexao.commit()
    try:
        resposta = mp.chamar("POST", "/v1/transaction-intents/process", token=mp.cfg("mp_access_token"),
                             dados=_corpo_pix(s), cabecalhos=_cabecalhos(s))
    except mp.ErroMP as erro:
        tipo = _classificar(erro)
        if tipo == "chave":
            _atualizar(saque_id, status="falhou",
                       erro="O Mercado Pago não aceitou a chave Pix. Confira se ela está cadastrada no "
                            "mesmo CPF/CNPJ da sua conta Mercado Pago. " + _resumo_erro(erro))
            seguranca.registrar("saque_falhou", f"Saque #{saque_id}: chave Pix recusada")
            return "falhou"
        if tipo == "incerto":
            _atualizar(saque_id, status="processando",
                       erro="Sem confirmação do Mercado Pago. Clique em Reenviar: é seguro, não paga duas vezes. "
                            + _resumo_erro(erro))
            return "processando"
        _atualizar(saque_id, status="solicitado", envio="manual", erro=_resumo_erro(erro))
        seguranca.registrar("saque_fila_manual", f"Saque #{saque_id}: {erro.status}")
        return "solicitado"
    return aplicar_resultado(saque_id, resposta)


def consultar(saque_id):
    """Pergunta ao Mercado Pago como está o saque (nunca confia em aviso recebido)."""
    s = db.get_db().execute("SELECT * FROM saques WHERE id = ?", (saque_id,)).fetchone()
    if s is None or s["status"] not in EM_ANDAMENTO:
        return s["status"] if s else None
    if not s["mp_transacao_id"]:
        return enviar(saque_id) if s["envio"] == "automatico" else s["status"]
    resposta = mp.chamar("GET", f"/v1/transaction-intents/{s['mp_transacao_id']}",
                         token=mp.cfg("mp_access_token"), cabecalhos=_cabecalhos(s))
    return aplicar_resultado(saque_id, resposta)


# =====================================================================
# Vendedor: painel financeiro e pedido de saque
# =====================================================================
@bp.route("/empresa/financeiro")
@empresa_aprovada
def painel():
    eid = g.empresa["id"]
    conexao = db.get_db()
    tipo_doc, doc = titular(g.empresa)
    saques = conexao.execute("SELECT * FROM saques WHERE empresa_id = ? ORDER BY id DESC LIMIT 30", (eid,)).fetchall()
    vendas_recentes = conexao.execute(
        """SELECT p.*, c.titulo, COALESCE(p.atualizado_em, p.criado_em) AS pago_em FROM pedidos p JOIN cursos c ON c.id = p.curso_id
            WHERE p.empresa_id = ? AND p.status IN ('pago', 'reembolsado') ORDER BY p.id DESC LIMIT 10""",
        (eid,)).fetchall()
    return render_template(
        "empresa/sacar.html", saldo=saldo(eid), series=series_vendas(eid), saques=saques,
        vendas_recentes=vendas_recentes, carteira=mp.modo_carteira(), conectado=bool(g.empresa["mp_user_id"]),
        oauth_pronto=mp.oauth_pronto(), teste=mp.modo_teste(), comissao=db.config("comissao_percentual", "7"),
        taxa_fixa=regra_int("saque_taxa_fixa"), taxa_pct=taxa_percentual(), minimo=regra_int("saque_minimo"),
        tipos_chave=TIPOS_CHAVE, titular_tipo=tipo_doc, titular_doc=doc, mascarar_chave=mascarar_chave,
        em_andamento=next((s for s in saques if s["status"] in EM_ANDAMENTO), None),
        automatico=envio_automatico())


def _voltar_painel():
    return redirect(url_for("saque.painel") + "#sacar")


@bp.route("/empresa/financeiro/sacar", methods=("POST",))
@empresa_aprovada
def solicitar():
    emp = g.empresa
    if emp["oficial"]:
        flash("As vendas da loja oficial já ficam na conta da Placemark.", "aviso")
        return _voltar_painel()
    if not mp.modo_carteira():
        flash("Seu dinheiro cai direto na sua conta Mercado Pago. Saque pelo app do Mercado Pago.", "aviso")
        return _voltar_painel()
    if not emp["mp_user_id"]:
        flash("Conecte sua conta Mercado Pago para poder sacar.", "erro")
        return _voltar_painel()

    # 1. Senha da conta: quem pegar a sessão aberta não consegue sacar
    espera = seguranca.bloqueado("saque-senha", g.usuario["id"])
    if espera:
        flash(f"Muitas senhas erradas. Aguarde {espera} minuto(s) para tentar sacar de novo.", "erro")
        return _voltar_painel()
    if not check_password_hash(g.usuario["senha_hash"], request.form.get("senha", "")):
        seguranca.registrar_tentativa("saque-senha", g.usuario["id"])
        flash("Senha incorreta. O saque não foi feito.", "erro")
        return _voltar_painel()
    seguranca.zerar_tentativas("saque-senha", g.usuario["id"])

    # 2. Chave Pix e titular
    tipo = request.form.get("pix_tipo", "")
    try:
        chave = normalizar_chave(tipo, request.form.get("pix_chave"))
    except ValueError as erro:
        flash(str(erro), "erro")
        return _voltar_painel()
    titular_tipo, titular_doc = titular(emp)
    if not titular_doc:
        flash("Não conseguimos identificar o CPF/CNPJ da sua conta Mercado Pago. Desconecte e conecte de novo.", "erro")
        return _voltar_painel()
    if tipo in ("CPF", "CNPJ") and chave != titular_doc:
        flash("A chave CPF/CNPJ precisa ser a mesma da sua conta Mercado Pago conectada.", "erro")
        return _voltar_painel()

    # 3. Valor
    try:
        texto = (request.form.get("valor") or "").replace("R$", "").replace(" ", "")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        valor = round(float(texto) * 100)
    except ValueError:
        flash("Valor inválido. Use o formato 150,00.", "erro")
        return _voltar_painel()

    # 4. Reserva o dinheiro numa transação exclusiva (dois pedidos ao mesmo tempo não passam juntos)
    conexao = db.get_db()
    conexao.commit()
    conexao.execute("BEGIN IMMEDIATE")
    try:
        erro = None
        s = saldo(emp["id"])
        minimo = regra_int("saque_minimo")
        taxa = calcular_taxa(valor)
        if conexao.execute("SELECT 1 FROM saques WHERE empresa_id = ? AND status IN ('solicitado', 'processando')",
                           (emp["id"],)).fetchone():
            erro = "Você já tem um saque em andamento. Aguarde ele terminar para pedir outro."
        elif valor <= 0 or valor > s["disponivel"]:
            erro = "O valor precisa ser maior que zero e caber no seu saldo disponível."
        elif valor < minimo:
            erro = f"O saque mínimo é de {_reais(minimo)}."
        elif valor - taxa <= 0:
            erro = f"O valor não cobre a taxa de saque ({_reais(taxa)})."
        if erro:
            conexao.rollback()
            flash(erro, "erro")
            return _voltar_painel()
        saque_id = conexao.execute(
            """INSERT INTO saques (empresa_id, valor, taxa, valor_liquido, pix_tipo, pix_chave, titular_tipo,
                                   titular_documento, mp_user_id, envio, idempotencia)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp["id"], valor, taxa, valor - taxa, tipo, chave, titular_tipo, titular_doc, emp["mp_user_id"],
             "automatico" if envio_automatico() else "manual", secrets.token_hex(16))).lastrowid
        conexao.execute("UPDATE empresas SET pix_tipo = ?, pix_chave = ? WHERE id = ?", (tipo, chave, emp["id"]))
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    seguranca.registrar("saque_solicitado", f"Saque #{saque_id}: {_reais(valor)} (taxa {_reais(taxa)}) para Pix {tipo}")

    # 5. Envia o Pix (ou deixa na fila do admin)
    resultado = enviar(saque_id) if envio_automatico() else "solicitado"
    liquido = _reais(valor - taxa)
    if resultado == "pago":
        flash(f"Saque enviado! {liquido} já foram transferidos para a sua chave Pix.", "sucesso")
    elif resultado == "processando":
        flash(f"Saque de {liquido} enviado ao Mercado Pago. A confirmação costuma levar poucos minutos.", "sucesso")
    elif resultado == "falhou":
        flash("O Mercado Pago recusou a chave Pix. O valor voltou para o seu saldo. Confira a chave e tente de novo.", "erro")
    else:
        flash(f"Saque de {liquido} solicitado! A equipe Placemark faz o Pix em até 1 dia útil.", "sucesso")
    return _voltar_painel()


@bp.route("/empresa/financeiro/saques/<int:saque_id>/cancelar", methods=("POST",))
@empresa_aprovada
def cancelar_meu(saque_id):
    """O vendedor pode desistir enquanto o saque está na fila (ainda não foi enviado)."""
    conexao = db.get_db()
    mudou = conexao.execute(
        """UPDATE saques SET status = 'cancelado', atualizado_em = datetime('now', 'localtime')
           WHERE id = ? AND empresa_id = ? AND status = 'solicitado' AND mp_transacao_id IS NULL
             AND (envio = 'manual' OR tentativas = 0)""",
        (saque_id, g.empresa["id"])).rowcount
    conexao.commit()
    if mudou:
        seguranca.registrar("saque_cancelado", f"Saque #{saque_id} (pelo vendedor)")
        flash("Saque cancelado. O valor voltou para o seu saldo.", "sucesso")
    else:
        flash("Este saque já está sendo processado e não pode mais ser cancelado.", "aviso")
    return _voltar_painel()


@bp.route("/empresa/financeiro/saques/<int:saque_id>/atualizar", methods=("POST",))
@empresa_aprovada
def atualizar_meu(saque_id):
    s = db.get_db().execute("SELECT id FROM saques WHERE id = ? AND empresa_id = ?",
                            (saque_id, g.empresa["id"])).fetchone()
    if s is None:
        abort(404)
    try:
        consultar(saque_id)
    except mp.ErroMP:
        flash("Não deu para falar com o Mercado Pago agora. Tente em alguns minutos.", "aviso")
    return _voltar_painel()


# =====================================================================
# Aviso do Mercado Pago sobre um saque
# =====================================================================
@bp.route("/mercadopago/saque/webhook", methods=("POST", "GET"))
def webhook():
    espera = seguranca.bloqueado("webhook-ip", seguranca.ip())
    if espera:
        return "muitas requisicoes", 429
    seguranca.registrar_tentativa("webhook-ip", seguranca.ip())
    saque_id = request.args.get("s", type=int)
    if saque_id:
        try:
            consultar(saque_id)   # só consulta a API; o conteúdo do aviso é ignorado
        except mp.ErroMP:
            return "tente de novo", 503
    return "ok", 200


# =====================================================================
# Admin: fila de saques e regras
# =====================================================================
def _reais(centavos):
    from util import reais
    return reais(centavos)


def _salvar_regras(form):
    from util import para_centavos
    erros = []
    novos = {}
    try:
        novos["saque_taxa_fixa"] = str(round(float((form.get("saque_taxa_fixa") or "0").replace(".", "").replace(",", ".")) * 100))
    except ValueError:
        erros.append("Taxa fixa inválida. Use o formato 2,00.")
    try:
        pct = float((form.get("saque_taxa_percentual") or "0").replace(",", "."))
        if not 0 <= pct <= 20:
            raise ValueError
        novos["saque_taxa_percentual"] = f"{pct:g}"
    except ValueError:
        erros.append("A taxa percentual precisa ficar entre 0 e 20.")
    try:
        novos["saque_minimo"] = str(para_centavos(form.get("saque_minimo")) or 100)
    except ValueError:
        erros.append("O saque mínimo precisa ser de pelo menos R$ 1,00.")
    try:
        dias = int(form.get("saque_dias_liberacao", ""))
        if not 0 <= dias <= 60:
            raise ValueError
        novos["saque_dias_liberacao"] = str(dias)
    except ValueError:
        erros.append("O prazo de liberação precisa ficar entre 0 e 60 dias.")
    novos["saque_envio"] = "automatico" if form.get("saque_envio") == "automatico" else "manual"
    novos["mp_modo_recebimento"] = "split" if form.get("mp_modo_recebimento") == "split" else "carteira"
    if erros:
        return erros
    conexao = db.get_db()
    for chave, valor in novos.items():
        conexao.execute("INSERT INTO configuracoes (chave, valor) VALUES (?, ?) "
                        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor", (chave, valor))
    conexao.commit()
    seguranca.registrar("regras_saque", ", ".join(f"{k}={v}" for k, v in novos.items()))
    return []


@bp.route("/admin/saques", methods=("GET", "POST"))
@papel_obrigatorio("admin")
def admin_lista():
    if request.method == "POST":
        erros = _salvar_regras(request.form)
        for e in erros:
            flash(e, "erro")
        if not erros:
            flash("Regras de saque salvas.", "sucesso")
        return redirect(url_for("saque.admin_lista"))

    status = request.args.get("status", "fila")
    conexao = db.get_db()
    sql = """SELECT s.*, e.nome_fantasia, e.mp_nome, e.mp_email FROM saques s
             JOIN empresas e ON e.id = s.empresa_id"""
    if status == "fila":
        sql += " WHERE s.status IN ('solicitado', 'processando')"
    elif status in ("pago", "falhou", "cancelado"):
        sql += f" WHERE s.status = '{status}'"
    lista = conexao.execute(sql + " ORDER BY s.id DESC LIMIT 200").fetchall()

    numeros = conexao.execute(
        """SELECT COUNT(CASE WHEN status IN ('solicitado', 'processando') THEN 1 END) AS fila,
                  COALESCE(SUM(CASE WHEN status IN ('solicitado', 'processando') THEN valor_liquido END), 0) AS fila_valor,
                  COALESCE(SUM(CASE WHEN status = 'pago' AND pago_em >= date('now', 'localtime', 'start of month')
                                    THEN valor_liquido END), 0) AS pago_mes,
                  COALESCE(SUM(CASE WHEN status = 'pago' THEN taxa END), 0) AS taxas
           FROM saques""").fetchone()
    # Quanto do dinheiro na conta da Placemark pertence aos vendedores (vendas - saques já pagos)
    devido = conexao.execute(
        """SELECT COALESCE((SELECT SUM(valor_empresa) FROM pedidos p JOIN empresas e ON e.id = p.empresa_id
                            WHERE p.status = 'pago' AND p.mp_conta = 'placemark' AND e.oficial = 0), 0)
                - COALESCE((SELECT SUM(valor) FROM saques WHERE status = 'pago'), 0)""").fetchone()[0]
    regras = {k: regra(k) for k in PADROES}
    return render_template(
        "admin/saques.html", saques=lista, status=status, n=numeros, devido=devido, regras=regras,
        taxa_fixa=regra_int("saque_taxa_fixa"), minimo=regra_int("saque_minimo"),
        carteira=mp.modo_carteira(), mp_ativo=mp.ativo(), teste=mp.modo_teste(), tipos_chave=TIPOS_CHAVE)


def _saque_ou_404(saque_id):
    s = db.get_db().execute("SELECT * FROM saques WHERE id = ?", (saque_id,)).fetchone()
    if s is None:
        abort(404)
    return s


@bp.route("/admin/saques/<int:saque_id>/enviar", methods=("POST",))
@papel_obrigatorio("admin")
def admin_enviar(saque_id):
    s = _saque_ou_404(saque_id)
    if s["status"] not in EM_ANDAMENTO:
        flash("Este saque não está mais na fila.", "aviso")
        return redirect(url_for("saque.admin_lista"))
    if not mp.ativo():
        flash("Configure o Mercado Pago antes de enviar automaticamente.", "erro")
        return redirect(url_for("saque.admin_lista"))
    # Reenvio com a MESMA chave de idempotência: se o Pix já saiu, o Mercado Pago não paga de novo
    resultado = enviar(saque_id)
    mensagens = {"pago": ("Pix enviado e confirmado.", "sucesso"),
                 "processando": ("Enviado. Aguardando a confirmação do Mercado Pago.", "aviso"),
                 "falhou": ("O Mercado Pago recusou a chave Pix. O saldo voltou para o vendedor.", "erro"),
                 "solicitado": ("O Mercado Pago não aceitou o envio. Veja o erro na linha do saque.", "erro")}
    flash(*mensagens.get(resultado, ("Feito.", "aviso")))
    return redirect(url_for("saque.admin_lista"))


@bp.route("/admin/saques/<int:saque_id>/consultar", methods=("POST",))
@papel_obrigatorio("admin")
def admin_consultar(saque_id):
    _saque_ou_404(saque_id)
    try:
        resultado = consultar(saque_id)
        flash(f"Situação atualizada: {resultado}.", "sucesso")
    except mp.ErroMP as erro:
        flash(f"Não deu para consultar: {erro}", "erro")
    return redirect(url_for("saque.admin_lista"))


@bp.route("/admin/saques/<int:saque_id>/pago", methods=("POST",))
@papel_obrigatorio("admin")
def admin_marcar_pago(saque_id):
    """Para quando a equipe fez o Pix pelo app do Mercado Pago (fila manual)."""
    _saque_ou_404(saque_id)
    comprovante = (request.form.get("comprovante") or "").strip()[:120]
    if len(comprovante) < 4:
        flash("Cole o código do comprovante do Pix (ID da transação) para marcar como pago.", "erro")
        return redirect(url_for("saque.admin_lista"))
    if _atualizar(saque_id, status="pago", envio="manual", comprovante=comprovante, erro=None,
                  pago_em=datetime.now().strftime(FORMATO)):
        seguranca.registrar("saque_pago_manual", f"Saque #{saque_id}: comprovante {comprovante}")
        flash("Saque marcado como pago.", "sucesso")
    else:
        flash("Este saque não está mais na fila.", "aviso")
    return redirect(url_for("saque.admin_lista"))


@bp.route("/admin/saques/<int:saque_id>/cancelar", methods=("POST",))
@papel_obrigatorio("admin")
def admin_cancelar(saque_id):
    s = _saque_ou_404(saque_id)
    if s["status"] == "processando" and not request.form.get("confirmo"):
        flash("Este saque pode já ter sido enviado ao Mercado Pago. Consulte antes de cancelar.", "erro")
        return redirect(url_for("saque.admin_lista"))
    motivo = (request.form.get("motivo") or "").strip()[:200] or "Cancelado pela equipe Placemark."
    if _atualizar(saque_id, status="cancelado", erro=motivo):
        seguranca.registrar("saque_cancelado", f"Saque #{saque_id}: {motivo}")
        flash("Saque cancelado. O valor voltou para o saldo do vendedor.", "sucesso")
    return redirect(url_for("saque.admin_lista"))
