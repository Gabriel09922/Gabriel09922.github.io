"""
Robô de cotações do Agroshow.

Roda no GitHub Actions (de hora em hora, nos dias úteis). Ele:
  1. busca os preços nos sites de cotação;
  2. grava em cotacoes/atual e acrescenta ao histórico (cotacoes/historico);
  3. confere os alertas de preço de todos os produtores e manda a notificação
     para o celular de quem tiver o alerta atingido.

Itens que o robô não consegue ler continuam com o último preço conhecido,
com a data antiga — nada é inventado.
"""
import datetime as dt
import json
import os
import re
import sys
import unicodedata

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore, messaging

FUSO = dt.timezone(dt.timedelta(hours=-3))
HOJE = dt.datetime.now(FUSO).date()
NA = "https://www.noticiasagricolas.com.br/cotacoes/"
AGRO = "https://www.agrolink.com.br/regional/mg/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; AgroshowBot/1.0; app de gestao rural)"}


# ---------------------------------------------------------------- utilidades
def sem_acento(t):
    return "".join(c for c in unicodedata.normalize("NFD", t or "") if unicodedata.category(c) != "Mn").lower()


def numero(t):
    """'1.611,06' -> 1611.06 ; '+0,32' -> 0.32 ; 's/ cotação' -> None"""
    if t is None:
        return None
    t = t.replace("\xa0", " ").strip()
    m = re.search(r"[-+]?\d[\d.]*(?:,\d+)?|[-+]?\d+(?:\.\d+)?", t.replace("R$", ""))
    if not m or "***" in t or "s/ cot" in t.lower():
        return None
    s = m.group(0)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def data_br(t):
    """Acha a última data dd/mm/aaaa no texto e devolve 'aaaa-mm-dd'."""
    achadas = re.findall(r"(\d{2})/(\d{2})/(\d{4})", t or "")
    if not achadas:
        return None
    d, m, a = achadas[-1]
    return f"{a}-{m}-{d}"


CABECALHO = re.compile(r"^(Data|Pra[cç]a|Munic[ií]pio|Mun[ií]p[ií]cio|Regi[aã]o(/ ?Tipo)?|Estados?|Tipo|Contrato( - M[eê]s)?|Vencimento|Ceasas|Origem/Tipo|Central de Abastecimento/Tipo|M[eê]s|Produto)$", re.I)

_cache = {}


def pagina(url):
    if url not in _cache:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            _cache[url] = BeautifulSoup(r.text, "html.parser")
        except Exception as e:  # noqa: BLE001
            print(f"  ! não abriu {url}: {e}")
            _cache[url] = None
    return _cache[url]


def tabelas(url):
    """Lista de (titulo, linhas, rodape_data) de cada <table> da página."""
    sopa = pagina(url)
    if not sopa:
        return []
    out = []
    for tb in sopa.find_all("table"):
        tit = tb.find_previous(["h1", "h2", "h3", "h4"])
        titulo = tit.get_text(" ", strip=True) if tit else ""
        linhas = []
        for tr in tb.find_all("tr"):
            if tr.find("th") and not tr.find("td"):
                continue  # linha de cabeçalho
            cel = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if cel and not CABECALHO.match(cel[0]):
                linhas.append(cel)
        texto = tb.get_text(" ", strip=True)
        rod = re.search(r"(?:Atualizado em|Fechamento|Valores do dia):?\s*(\d{2}/\d{2}/\d{4})", texto)
        if not rod:
            ant = tb.find_previous(string=re.compile(r"(?:Fechamento|Atualizado em)\D{0,20}\d{2}/\d{2}/\d{4}"))
            rod = re.search(r"(\d{2}/\d{2}/\d{4})", ant) if ant else None
        out.append((titulo, linhas, data_br(rod.group(1)) if rod else None))
    return out


def achar_tabela(url, padrao_titulo):
    rx = re.compile(padrao_titulo, re.I)
    for titulo, linhas, rod in tabelas(url):
        if rx.search(titulo):
            return linhas, rod
    return None, None


def linhas_dados(linhas):
    """Só as linhas que têm algum número (tira cabeçalho, '***', rodapé)."""
    out = []
    for l in linhas:
        txt = " ".join(l)
        if txt.lower().startswith(("atualizado", "fonte", "última", "valores do dia", "semana")):
            continue
        if any(numero(c) is not None for c in l[1:]) or re.search(r"s/ cot", txt, re.I):
            out.append(l)
    return out


MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def mes_contrato(t):
    """'Novembro/26' ou 'Janeiro/2027' -> 'nov/26'"""
    m = re.match(r"\s*([A-Za-zçÇ]{3})[a-zç]*\s*/\s*(\d{2,4})", t or "")
    if not m:
        return t
    return f"{sem_acento(m.group(1))[:3]}/{m.group(2)[-2:]}"


def vencendo(contrato):
    """True quando o contrato é do mês corrente (já está vencendo)."""
    m = re.match(r"([a-z]{3})/(\d{2})", mes_contrato(contrato))
    if not m:
        return False
    return MESES.get(m.group(1)) == HOJE.month and int(m.group(2)) == HOJE.year % 100


# ------------------------------------------------------------- extratores
def ex_primeira(url, titulo, v=1, p=2, div=1.0):
    linhas, rod = achar_tabela(url, titulo)
    if not linhas:
        return None
    dados = linhas_dados(linhas)
    if not dados:
        return None
    l = dados[0]
    val = numero(l[v]) if len(l) > v else None
    if val is None:
        return None
    return {"valor": round(val / div, 4), "variacao": numero(l[p]) if p is not None and len(l) > p else None,
            "dataRef": data_br(l[0]) or rod}


def ex_linha(url, titulo, linha, v=1, p=2, div=1.0, data_col=None):
    linhas, rod = achar_tabela(url, titulo)
    if not linhas:
        return None
    rx = re.compile(linha, re.I)
    for l in linhas_dados(linhas):
        if rx.search(" ".join(l)):
            val = numero(l[v]) if len(l) > v else None
            if val is None:
                return None
            return {"valor": round(val / div, 4), "variacao": numero(l[p]) if p is not None and len(l) > p else None,
                    "dataRef": (data_br(l[data_col]) if data_col is not None else None) or rod}
    return None


def ex_secao(url, titulo, secao, linha, v=1, p=2):
    """Tabelas com blocos ('Ceasa - Belo Horizonte/MG | *** | ***' e depois os tipos)."""
    linhas, rod = achar_tabela(url, titulo)
    if not linhas:
        return None
    dentro = False
    rs, rl = re.compile(secao, re.I), re.compile(linha, re.I)
    for l in linhas:
        txt = " ".join(l)
        if "***" in txt:
            dentro = bool(rs.search(txt))
            continue
        if dentro and rl.search(txt):
            val = numero(l[v]) if len(l) > v else None
            return None if val is None else {"valor": val, "variacao": numero(l[p]) if len(l) > p else None, "dataRef": rod}
    return None


def ex_bolsa(url, titulo, nome, v=1, p=2, div=1.0, pontos=False):
    """Contrato futuro: usa o primeiro vencimento (ou o seguinte, se o primeiro vence neste mês)."""
    linhas, rod = achar_tabela(url, titulo)
    if not linhas:
        return None
    dados = [l for l in linhas_dados(linhas) if re.match(r"\s*[A-Za-zç]{3}", l[0])]
    if not dados:
        return None
    l = dados[1] if len(dados) > 1 and vencendo(dados[0][0]) else dados[0]
    val = numero(l[v])
    if val is None:
        return None
    val = val / div
    var = numero(l[p]) if p is not None and len(l) > p else None
    if pontos and var is not None:  # variação em pontos -> %
        ant = val - var / div
        var = round(var / div / ant * 100, 2) if ant else None
    return {"valor": round(val, 4), "variacao": var, "dataRef": rod or HOJE.isoformat(), "local": f"{nome} · {mes_contrato(l[0])}"}


def ex_agrolink(cidade, produto):
    """Página regional do Agrolink: a primeira linha (mais recente) do produto."""
    sopa = pagina(AGRO + cidade + "/cotacoes")
    if not sopa:
        return None
    rx = re.compile(produto, re.I)
    for tr in sopa.find_all("tr"):
        cel = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cel or not rx.search(" ".join(cel[:2])):
            continue
        txt = " ".join(cel)
        data = data_br(txt)
        precos = [numero(c) for c in cel if re.search(r"\d,\d{2}", c) and "%" not in c and not re.search(r"\d{2}/\d{2}/\d{4}", c)]
        var = next((numero(c) for c in cel if "%" in c), None)
        if var is not None and re.search(r"▼|queda|-", txt.split("%")[0][-8:]):
            var = -abs(var)
        if precos and precos[0] is not None:
            return {"valor": precos[0], "variacao": var, "dataRef": data}
        print(f"  ? {cidade} / {produto}: linha achada sem preço: {cel}")
    return None


def _chave(t):
    return re.sub(r"[^a-z]", "", sem_acento(t))


def ex_ceasa(produto):
    """CeasaMinas, preço mais comum: cada unidade é uma coluna; pega a coluna de Uberlândia."""
    sopa = pagina("https://minas1.ceasa.mg.gov.br/ceasainternet/cst_precosmaiscomumMG/cst_precosmaiscomumMG.php")
    if not sopa:
        return None
    m = re.search(r"Uberl.ndia\s*-\s*cota..o de:?\s*(\d{2}/\d{2}/\d{4})", sopa.get_text(" ", strip=True), re.I)
    data = data_br(m.group(1)) if m else None
    alvo = _chave(produto)
    for tb in sopa.find_all("table"):
        col = None
        for tr in tb.find_all("tr"):
            cel = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if col is None:
                idx = [i for i, c in enumerate(cel) if re.search(r"Uberl.ndia", c, re.I) and len(c) < 20]
                if idx and any(re.search(r"Produto", c, re.I) for c in cel):
                    col = idx[0]
                continue
            if cel and _chave(cel[0]) == alvo and len(cel) > col:
                val = numero(cel[col])
                return None if val is None else {"valor": val, "variacao": None, "dataRef": data}
    return None


def ex_graodireto(cidade):
    """Grão Direto: tabela Data | Cidade | Preço | Variação."""
    sopa = pagina("https://www.graodireto.com.br/ofertas/soja/mg/")
    if not sopa:
        return None
    alvo = _chave(cidade)
    for tr in sopa.find_all("tr"):
        cel = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not any(_chave(c) == alvo for c in cel):
            continue
        preco = next((numero(c) for c in cel if "R$" in c), None)
        var = next((numero(c) for c in cel if "%" in c), None)
        if preco is not None:
            return {"valor": preco, "variacao": var, "dataRef": data_br(" ".join(cel))}
    return None


def ex_girassol():
    """Agrolink: médias mensais (Mês | Estadual | Nacional | Var.) — usa a média nacional mais recente."""
    sopa = pagina("https://www.agrolink.com.br/cotacoes/historico/mt/girassol-comum-sc-60kg")
    if not sopa:
        return None
    for tr in sopa.find_all("tr"):
        cel = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cel) >= 3 and re.match(r"[A-Za-z]{3}/\d{4}$", cel[0]):
            val = numero(cel[2])
            if val is None:
                continue
            mes = f"{sem_acento(cel[0][:3])}/{cel[0][-2:]}"
            return {"valor": val, "variacao": numero(cel[3]) if len(cel) > 3 else None, "dataRef": None,
                    "local": f"Média Brasil · {mes}"}
    return None


def ex_dolar():
    # 1ª opção: AwesomeAPI
    try:
        r = requests.get("https://economia.awesomeapi.com.br/json/last/USD-BRL", headers=UA, timeout=20)
        q = r.json().get("USDBRL")
        if q:
            quando = dt.datetime.fromtimestamp(int(q["timestamp"]), FUSO).date().isoformat()
            return {"valor": round(float(q["bid"]), 4), "variacao": round(float(q["pctChange"]), 2), "dataRef": quando}
        print(f"  ! dólar (AwesomeAPI) respondeu: {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! dólar (AwesomeAPI): {e}")
    # 2ª opção: Yahoo Finanças
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/BRL=X?range=5d&interval=1d", headers=UA, timeout=20)
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        valor = float(meta["regularMarketPrice"])
        ant = meta.get("chartPreviousClose") or meta.get("previousClose")
        var = round((valor / float(ant) - 1) * 100, 2) if ant else None
        quando = dt.datetime.fromtimestamp(int(meta.get("regularMarketTime", 0)) or int(dt.datetime.now().timestamp()), FUSO).date().isoformat()
        return {"valor": round(valor, 4), "variacao": var, "dataRef": quando}
    except Exception as e:  # noqa: BLE001
        print(f"  ! dólar (Yahoo): {e}")
    # 3ª opção: PTAX do Banco Central (fechamento oficial do dia útil mais recente)
    try:
        ini = (HOJE - dt.timedelta(days=7)).strftime("%m-%d-%Y")
        fim = HOJE.strftime("%m-%d-%Y")
        url = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/CotacaoDolarPeriodo(dataInicial=@i,dataFinalCotacao=@f)"
               f"?@i='{ini}'&@f='{fim}'&$format=json")
        vals = requests.get(url, timeout=20).json().get("value", [])
        if vals:
            u = vals[-1]
            ant = vals[-2]["cotacaoVenda"] if len(vals) > 1 else None
            var = round((u["cotacaoVenda"] / ant - 1) * 100, 2) if ant else None
            return {"valor": round(float(u["cotacaoVenda"]), 4), "variacao": var, "dataRef": u["dataHoraCotacao"][:10]}
    except Exception as e:  # noqa: BLE001
        print(f"  ! dólar (Banco Central): {e}")
    return None


# id do item -> como buscar
FONTES = {
    "dolar": ex_dolar,
    # Soja
    "soja-pgua": lambda: ex_primeira(NA + "soja", r"Indicador da Soja.*Paranagu"),
    "soja-cbot": lambda: ex_bolsa(NA + "soja", r"^Soja - Bolsa de Chicago", "Bolsa de Chicago", v=1, p=3),
    "soja-b3": lambda: ex_bolsa(NA + "soja", r"^Soja CME - B3", "Bolsa B3"),
    "soja-udi": lambda: ex_agrolink("uberlandia", r"Soja em Gr"),
    # Milho
    "milho-cepea": lambda: ex_primeira(NA + "milho", r"Indicador do Milho"),
    "milho-b3": lambda: ex_bolsa(NA + "milho", r"^Milho - B3", "Bolsa B3"),
    "milho-cbot": lambda: ex_bolsa(NA + "milho", r"^Milho - Bolsa de Chicago", "Bolsa de Chicago", v=1, p=3),
    "milho-udi": lambda: ex_agrolink("uberlandia", r"Milho Seco"),
    "milho-pdm": lambda: ex_agrolink("patos-de-minas", r"Milho Seco"),
    # Café
    "cafe-cepea": lambda: ex_primeira(NA + "cafe", r"Indicador Caf. Ar.bica"),
    "cafe-ny": lambda: ex_bolsa(NA + "cafe", r"Caf. - Bolsa de Nova Iorque", "Bolsa de Nova York", v=1, p=3, div=100, pontos=True),
    "cafe-b3": lambda: ex_bolsa(NA + "cafe", r"Caf. Ar.bica 4/5 - B3", "Bolsa B3"),
    "robusta-ldn": lambda: ex_bolsa(NA + "cafe", r"Bolsa de Londres", "Bolsa de Londres", v=1, p=2, pontos=True),
    "cafe-ptc-67": lambda: ex_linha(NA + "cafe", r"Tipo 6/7", r"Patroc"),
    "cafe-cxp-67": lambda: ex_linha(NA + "cafe", r"Tipo 6/7", r"Guaxup"),
    "cafe-ptc-6": lambda: ex_linha(NA + "cafe", r"\(Tipo 6 Bebida", r"Patroc"),
    "cafe-cxp-6": lambda: ex_linha(NA + "cafe", r"\(Tipo 6 Bebida", r"Guaxup"),
    "cafe-agi": lambda: ex_linha(NA + "cafe", r"\(Tipo 6 Bebida", r"Araguari"),
    "cafe-ptc-cd": lambda: ex_linha(NA + "cafe", r"Cereja Descascado", r"Patroc"),
    "cafe-cxp-cd": lambda: ex_linha(NA + "cafe", r"Cereja Descascado", r"Guaxup"),
    "conilon-es": lambda: ex_linha(NA + "cafe", r"Conilon", r"7/8"),
    "cafe-pdm": lambda: ex_agrolink("patos-de-minas", r"Caf. Ar.bica Tipo 6(?! Duro)"),
    # Pecuária
    "boi-sp": lambda: ex_primeira(NA + "boi-gordo", r"Indicador do Boi Gordo Esalq"),
    "boi-b3": lambda: ex_bolsa(NA + "boi-gordo", r"^Boi Gordo - B3", "Bolsa B3"),
    "bezerro-ms": lambda: ex_primeira(NA + "boi-gordo", r"^Bezerro Esalq"),
    "boi-tri": lambda: ex_linha(NA + "boi-gordo", r"Scot Consultoria", r"MG Tri", v=1, p=None),
    "vaca-tri": lambda: ex_linha(NA + "boi-gordo", r"Scot Consultoria", r"MG Tri", v=3, p=None),
    "boi-mg": lambda: ex_linha(NA + "boi-gordo", r"^Indicador do Boi$", r"Minas Gerais"),
    "vaca-mg": lambda: ex_linha(NA + "boi-gordo", r"^Indicador da Vaca", r"Minas Gerais"),
    "rep-desmama": lambda: ex_linha(NA + "boi-gordo", r"^Reposi..o Nelore - Macho", r"Minas Gerais", v=1, p=None),
    "rep-bezerro": lambda: ex_linha(NA + "boi-gordo", r"^Reposi..o Nelore - Macho", r"Minas Gerais", v=2, p=None),
    "rep-garrote": lambda: ex_linha(NA + "boi-gordo", r"^Reposi..o Nelore - Macho", r"Minas Gerais", v=3, p=None),
    "rep-boimagro": lambda: ex_linha(NA + "boi-gordo", r"^Reposi..o Nelore - Macho", r"Minas Gerais", v=4, p=None),
    "boi-udi": lambda: ex_agrolink("uberlandia", r"Boi Gordo"),
    "vaca-udi": lambda: ex_agrolink("uberlandia", r"Vaca Gorda"),
    "novilha-udi": lambda: ex_agrolink("uberlandia", r"Novilha Gorda"),
    "leite-udi": lambda: ex_agrolink("uberlandia", r"\bLeite\b"),
    "leite-mg": lambda: ex_linha(NA + "leite", r"Leite - Pre.os ao Produtor", r"^MG\b"),
    "leite-br": lambda: ex_linha(NA + "leite", r"Leite - Pre.os ao Produtor", r"^Brasil\b"),
    # Grãos
    "trigo-pr": lambda: ex_linha(NA + "trigo", r"Pre.o M.dio do Trigo", r"Paran", v=2, p=3, data_col=0),
    "trigo-rs": lambda: ex_linha(NA + "trigo", r"Pre.o M.dio do Trigo", r"Rio Grande", v=2, p=3, data_col=0),
    "sorgo": lambda: ex_linha(NA + "sorgo", r"^Sorgo - Mercado F.sico$", r"Rio Verde"),
    "arroz": lambda: ex_primeira(NA + "arroz", r"Indicador Arroz em Casca"),
    "feijao-tri": lambda: ex_linha(NA + "feijao", r"Peneira 12", r"Tri.ng"),
    "feijao-sp": lambda: ex_linha(NA + "feijao", r"Peneira 12", r"S.o Paulo"),
    "feijao-preto": lambda: ex_linha(NA + "feijao", r"Feij.o Preto Tipo 1", r"Curitiba"),
    # Suínos, aves e ovos
    "suino-mg": lambda: ex_linha(NA + "suinos", r"Indicador do Su.no Vivo", r"MG - posto", v=2, p=3, data_col=0),
    "suino-sp": lambda: ex_linha(NA + "suinos", r"Indicador do Su.no Vivo", r"SP - posto", v=2, p=3, data_col=0),
    "frango-cong": lambda: ex_primeira(NA + "frango", r"Frango Congelado"),
    "frango-vivo": lambda: ex_linha(NA + "frango", r"^Frango vivo", r"Paran"),
    "ovos-brancos": lambda: ex_linha(NA + "ovos", r"Pre.os de Ovos Cepea", r"Branco", v=2, p=3, data_col=0),
    "ovos-vermelhos": lambda: ex_linha(NA + "ovos", r"Pre.os de Ovos Cepea", r"Vermelho", v=2, p=3, data_col=0),
    # Hortifrúti
    "laranja-ind": lambda: ex_primeira(NA + "laranja", r"Laranja Ind.stria"),
    "laranja-pera": lambda: ex_primeira(NA + "laranja", r"Laranja Pera In Natura"),
    "tomate-bh": lambda: ex_secao(NA + "legumes", r"^Tomate - Ceasas", r"Belo Horizonte", r"Longa Vida"),
    "cebola-bh": lambda: ex_secao(NA + "legumes", r"^Cebola - Ceasas", r"Belo Horizonte", r"Amarela"),
    "batata-udi-esp": lambda: ex_agrolink("uberlandia", r"Batata Padr.o Agata Especial"),
    "batata-udi-pri": lambda: ex_agrolink("uberlandia", r"Batata Agata Primeira"),
    # Ceasa de Uberlândia (preço mais comum, por kg)
    "batata-ceasa-udi": lambda: ex_ceasa("BATATA"),
    "tomate-udi": lambda: ex_ceasa("TOMATE"),
    "cebola-udi": lambda: ex_ceasa("CEBOLA"),
    "cenoura-udi": lambda: ex_ceasa("CENOURA"),
    "mandioca-udi": lambda: ex_ceasa("MANDIOCA"),
    "alface-udi": lambda: ex_ceasa("ALFACE"),
    "banana-nanica-udi": lambda: ex_ceasa("BANANA NANICA"),
    "banana-prata-udi": lambda: ex_ceasa("BANANA PRATA"),
    "laranja-udi": lambda: ex_ceasa("LARANJA"),
    # Soja nas praças de Minas (Grão Direto)
    "soja-uba": lambda: ex_graodireto("Uberaba"),
    "soja-ptc": lambda: ex_graodireto("Patrocínio"),
    "soja-pdm": lambda: ex_graodireto("Patos de Minas"),
    "soja-unai": lambda: ex_graodireto("Unaí"),
    "girassol": ex_girassol,
    # Cana e fibras
    "acucar": lambda: ex_primeira(NA + "sucroenergetico", r"Indicador do A..car Cristal Cepea"),
    "etanol": lambda: ex_primeira(NA + "sucroenergetico", r"Indicador Semanal Etanol Hidratado Cepea"),
    "algodao": lambda: ex_primeira(NA + "algodao", r"Algod.o - Indicador Cepea", div=100),
}


# ------------------------------------------------------------------ Firebase
def conectar():
    sa = os.environ.get("FIREBASE_SA")
    if not sa:
        sys.exit("Falta o segredo FIREBASE_SA no GitHub (Settings > Secrets and variables > Actions).")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    return firestore.client()


def carregar_atual(db):
    snap = db.document("cotacoes/atual").get()
    if snap.exists:
        return snap.to_dict()
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cotacoes-inicial.json")
    print("cotacoes/atual ainda não existe: começando de cotacoes-inicial.json")
    with open(base, encoding="utf-8") as f:
        return json.load(f)


REMOVIDOS = {"milho-udi-uba", "feijao-noroeste"}  # vinham de reportagem (BBM): sem leitura confiável


def atualizar_precos(doc):
    doc["itens"] = [it for it in doc["itens"] if it["id"] not in REMOVIDOS]
    ok, falhou = [], []
    for it in doc["itens"]:
        f = FONTES.get(it["id"])
        if not f:
            continue
        try:
            novo = f()
        except Exception as e:  # noqa: BLE001
            print(f"  ! {it['id']}: erro {e}")
            novo = None
        if not novo or novo.get("valor") is None:
            falhou.append(it["id"])
            continue
        it["valor"] = novo["valor"]
        it["variacao"] = novo.get("variacao")
        if novo.get("dataRef"):
            it["dataRef"] = novo["dataRef"]
        if novo.get("local"):
            it["local"] = novo["local"]
        ok.append(it["id"])
    return ok, falhou


def atualizar_historico(db, doc):
    ref = db.document("cotacoes/historico")
    snap = ref.get()
    serie = (snap.to_dict() or {}).get("serie", {}) if snap.exists else {}
    for it in doc["itens"]:
        if it.get("valor") is None or not it.get("dataRef"):
            continue
        s = [p for p in serie.get(it["id"], []) if p and p[0] != it["dataRef"]]
        s.append([it["dataRef"], it["valor"]])
        s.sort(key=lambda p: p[0])
        serie[it["id"]] = s[-120:]
    ref.set({"serie": serie})


def conferir_alertas(db, doc):
    precos = {it["id"]: it for it in doc["itens"]}
    disparos = 0
    for a in db.collection_group("alertas").stream():
        d = a.to_dict()
        if d.get("disparadoEm"):
            continue
        it = precos.get(d.get("itemId"))
        if not it or it.get("valor") is None:
            continue
        v, alvo = float(it["valor"]), float(d.get("alvo", 0))
        bateu = v >= alvo if d.get("cond") == "acima" else v <= alvo
        if not bateu:
            continue
        a.reference.update({"disparadoEm": dt.datetime.now(dt.timezone.utc).isoformat(), "valorDisparo": v})
        disparos += 1
        uid = a.reference.parent.parent.id
        moeda = "US$" if it.get("moeda") == "USD" else "R$"
        valor_txt = f"{moeda} {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        titulo = f"{it['produto']} chegou na sua meta"
        corpo = f"{it['local']}: {valor_txt} por {it['unidade']}."
        notificar(db, uid, titulo, corpo)
    return disparos


def notificar(db, uid, titulo, corpo):
    ref = db.document(f"users/{uid}/config/push")
    snap = ref.get()
    tokens = (snap.to_dict() or {}).get("tokens", []) if snap.exists else []
    if not tokens:
        print(f"  (usuário {uid[:6]}… sem notificações ativadas)")
        return
    ruins = []
    for t in tokens:
        msg = messaging.Message(token=t, webpush=messaging.WebpushConfig(
            notification=messaging.WebpushNotification(title=titulo, body=corpo, icon="icon-192.png"),
            fcm_options=messaging.WebpushFCMOptions(link="/")))
        try:
            messaging.send(msg)
        except Exception as e:  # noqa: BLE001
            print(f"  ! notificação falhou: {e}")
            if "registration-token-not-registered" in str(e) or "not a valid FCM" in str(e) or "Requested entity was not found" in str(e):
                ruins.append(t)
    if ruins:
        ref.set({"tokens": firestore.ArrayRemove(ruins)}, merge=True)


def main():
    db = conectar()
    doc = carregar_atual(db)
    ok, falhou = atualizar_precos(doc)
    doc["atualizadoEm"] = dt.datetime.now(dt.timezone.utc).isoformat()
    db.document("cotacoes/atual").set(doc)
    atualizar_historico(db, doc)
    disparos = conferir_alertas(db, doc)
    print(f"\nAtualizados: {len(ok)} preços.")
    if falhou:
        print(f"Sem preço novo (mantidos como estavam): {len(falhou)} → {', '.join(falhou)}")
    sem_fonte = [it["id"] for it in doc["itens"] if it["id"] not in FONTES]
    if sem_fonte:
        print(f"Sem fonte automática ainda: {', '.join(sem_fonte)}")
    print(f"Alertas atingidos agora: {disparos}")


if __name__ == "__main__":
    main()
