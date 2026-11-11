"""Funções auxiliares: dinheiro, vídeos e validações."""
import re
from urllib.parse import parse_qs, urlparse


def reais(centavos):
    """1990 -> 'R$ 19,90'"""
    if centavos is None:
        return "—"
    valor = centavos / 100
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def para_centavos(texto):
    """'49,90' ou '49.90' ou 'R$ 1.249,90' -> 4990. Vazio -> None. Inválido -> ValueError."""
    texto = (texto or "").strip().replace("R$", "").replace(" ", "")
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    valor = round(float(texto) * 100)
    if valor < 100:
        raise ValueError("O preço mínimo é R$ 1,00.")
    return valor


def video_embed(url):
    """Converte links do YouTube/Vimeo em endereço de player. Outros links: None."""
    if not url or not url.startswith("https://"):
        return None
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    video_id = None
    if host == "youtube.com" and u.path == "/watch":
        video_id = parse_qs(u.query).get("v", [None])[0]
    elif host == "youtu.be":
        video_id = u.path.lstrip("/")
    elif host == "youtube.com" and u.path.startswith(("/embed/", "/shorts/")):
        video_id = u.path.split("/")[2]
    if video_id and re.fullmatch(r"[\w-]{6,20}", video_id):
        return f"https://www.youtube-nocookie.com/embed/{video_id}"
    if host == "vimeo.com" and re.fullmatch(r"/\d+", u.path):
        return f"https://player.vimeo.com/video{u.path}"
    return None


def iniciais(texto):
    palavras = (texto or "?").split()
    sigla = (palavras[0][0] + (palavras[-1][0] if len(palavras) > 1 else "")).upper()
    if len(sigla) == 2 and sigla[0] == sigla[1]:  # "Programação com Python" -> "Py", "Inglês Iniciante" -> "In"
        sigla = palavras[0][0].upper() + palavras[-1][1:2].lower()  # "Py", "In"
    return sigla


TONS = ["indigo", "verde", "ambar", "rosa"]


def tom(numero):
    return TONS[(numero - 1) % len(TONS)]


METODOS = {"pix": "Pix", "cartao": "Cartão", "boleto": "Boleto", "mercadopago": "Mercado Pago", "kiwify": "Kiwify", "manual": "Manual"}


def link_checkout_valido(url):
    """Aceita só links https (ex.: https://pay.kiwify.com.br/AbC123)."""
    if not url:
        return True
    u = urlparse(url)
    return u.scheme == "https" and bool(u.netloc) and " " not in url


# chave: (rótulo, mínimo em centavos, máximo em centavos) — compara com o menor preço do curso
FAIXAS_PRECO = {
    "ate50": ("Até R$ 50", 0, 5000),
    "50a150": ("R$ 50 a R$ 150", 5000, 15000),
    "150a300": ("R$ 150 a R$ 300", 15000, 30000),
    "300mais": ("Acima de R$ 300", 30000, 999999999),
}

ORDENS = [("relevancia", "Mais procurados"), ("novos", "Mais novos"), ("menor", "Menor preço"),
          ("maior", "Maior preço"), ("az", "Nome (A–Z)")]

CATEGORIAS = ["Tecnologia", "Negócios", "Idiomas", "Design", "Marketing", "Saúde", "Finanças", "Outros"]
