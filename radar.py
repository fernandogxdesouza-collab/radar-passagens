"""Radar de passagens: coleta preços, compara com o histórico da rota e avisa no Telegram.

Uso:  python radar.py            (execução normal)
      python radar.py --teste    (manda uma mensagem de teste ao Telegram e sai)
Variáveis de ambiente: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TRAVELPAYOUTS_TOKEN (opcional),
RADAR_CONSULTAS (opcional, sobrepõe consultas_por_execucao).
"""
import csv
import html
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).parent
DADOS = RAIZ / "dados"
PRECOS = DADOS / "precos.csv"
ESTADO = DADOS / "estado.json"
PAINEL_MODELO = RAIZ / "painel_modelo.html"
PAINEL = RAIZ / "docs" / "index.html"
CAMPOS = ["coletado_em", "origem", "destino", "ida", "volta", "preco", "cia", "paradas", "fonte"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) radar-passagens"


def ler_json(caminho, padrao=None):
    if not caminho.exists():
        return padrao
    return json.loads(caminho.read_text(encoding="utf-8"))


CFG = ler_json(RAIZ / "config.json")
MILHAS = ler_json(RAIZ / "milhas.json")


def brl(valor):
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def baixar(url, dados=None, espera=25):
    req = urllib.request.Request(url, data=dados, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=espera) as resp:
        return resp.read()


# ---------------------------------------------------------------- rotas e datas

def regiao_de(destino):
    return CFG["destinos"].get(destino, ["", "Outros"])[1]


def cidade_de(codigo):
    return CFG["destinos"].get(codigo, [codigo])[0]


def montar_rotas():
    casa, hubs = CFG["casa"], CFG["hubs"]
    rotas = [(casa, d) for d in CFG["destinos"] if d != casa]
    for hub in hubs:
        rotas += [(hub, d) for d, (_, reg) in CFG["destinos"].items() if reg != "Brasil"]
    return rotas


def datas_da_tarefa(indice_rota, mes, ciclo, regiao):
    estadia = CFG["estadia_dias"].get(regiao, CFG["estadia_dias"]["padrao"])
    ida = date.today() + timedelta(days=21 + 30 * mes + (ciclo * 3 + indice_rota) % 7)
    return ida.isoformat(), (ida + timedelta(days=estadia)).isoformat()


# ---------------------------------------------------------------- fontes de preço

def consultar_google(origem, destino, ida, volta):
    """Menor preço ida e volta em reais no Google Flights, ou None."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    consulta = create_query(
        flights=[
            FlightQuery(date=ida, from_airport=origem, to_airport=destino),
            FlightQuery(date=volta, from_airport=destino, to_airport=origem),
        ],
        trip="round-trip", seat="economy", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL",
    )
    voos = [v for v in get_flights(consulta) if getattr(v, "price", 0)]
    if not voos:
        return None
    melhor = min(voos, key=lambda v: v.price)
    return {
        "preco": int(melhor.price),
        "cia": " + ".join(melhor.airlines or []),
        "paradas": max(len(melhor.flights or []) - 1, 0),
    }


def coletar_google(estado, agora):
    rotas = montar_rotas()
    meses = CFG["meses_a_frente"]
    tarefas = [(i, r, m) for m in range(meses) for i, r in enumerate(rotas)]
    limite = int(os.environ.get("RADAR_CONSULTAS", CFG["consultas_por_execucao"]))
    linhas, falhas = [], 0
    for _ in range(min(limite, len(tarefas))):
        if estado["cursor"] >= len(tarefas):
            estado["cursor"], estado["ciclo"] = 0, estado["ciclo"] + 1
        i, (origem, destino), mes = tarefas[estado["cursor"]]
        estado["cursor"] += 1
        ida, volta = datas_da_tarefa(i, mes, estado["ciclo"], regiao_de(destino))
        try:
            achado = consultar_google(origem, destino, ida, volta)
        except Exception as erro:  # bloqueio, mudança de formato, rede
            achado, falhas = None, falhas + 1
            print(f"  falha {origem}-{destino} {ida}: {type(erro).__name__}: {str(erro)[:120]}")
        if achado:
            linhas.append({"coletado_em": agora, "origem": origem, "destino": destino, "ida": ida,
                           "volta": volta, "fonte": "google", **achado})
        if falhas >= 10 and not linhas:
            print("  Google Flights recusou as 10 primeiras consultas; interrompendo esta execução.")
            break
        time.sleep(random.uniform(*CFG["pausa_entre_consultas_seg"]))
    return linhas, falhas


def coletar_aviasales(agora):
    """Preços em cache da Aviasales (Travelpayouts) para QUALQUER destino. Opcional.

    Só roda com TRAVELPAYOUTS_TOKEN definido. Ainda não foi testado com um token real.
    """
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return []
    linhas = []
    for origem in [CFG["casa"], *CFG["hubs"]]:
        params = urllib.parse.urlencode({
            "origin": origem, "currency": "brl", "period_type": "year", "one_way": "false",
            "limit": 1000, "sorting": "price", "token": token,
        })
        try:
            resposta = json.loads(baixar("https://api.travelpayouts.com/aviasales/v3/get_latest_prices?" + params))
            for item in resposta.get("data", []):
                if not item.get("return_date") or item["depart_date"] <= date.today().isoformat():
                    continue
                linhas.append({"coletado_em": agora, "origem": origem, "destino": item["destination"],
                               "ida": item["depart_date"], "volta": item["return_date"],
                               "preco": int(item["value"]), "cia": item.get("gate", ""),
                               "paradas": item.get("number_of_changes", ""), "fonte": "aviasales"})
        except Exception as erro:
            print(f"  Aviasales {origem}: {type(erro).__name__}: {str(erro)[:120]}")
    return linhas


# ---------------------------------------------------------------- histórico

def ler_historico():
    if not PRECOS.exists():
        return []
    corte = (date.today() - timedelta(days=CFG["janela_historico_dias"])).isoformat()
    with PRECOS.open(encoding="utf-8", newline="") as f:
        linhas = [l for l in csv.DictReader(f) if l["coletado_em"][:10] >= corte]
    for l in linhas:
        l["preco"] = int(l["preco"])
    return linhas


def gravar_historico(linhas):
    DADOS.mkdir(exist_ok=True)
    with PRECOS.open("w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=CAMPOS)
        escritor.writeheader()
        escritor.writerows(linhas)


def medianas(historico):
    grupos = {}
    for l in historico:
        grupos.setdefault((l["origem"], l["destino"], l["fonte"]), []).append(l["preco"])
    minimo = CFG["min_observacoes"]
    return {k: statistics.median(v) for k, v in grupos.items() if len(v) >= minimo}


def posicionamento(hub, meds):
    """Custo estimado de ida e volta da casa até o hub: mediana observada ou valor padrão."""
    return meds.get((CFG["casa"], hub, "google"), CFG["posicionamento_padrao_reais"])


def avaliar(linha, meds):
    """Acrescenta à observação a mediana da rota, o desconto e o custo total saindo de casa."""
    med = meds.get((linha["origem"], linha["destino"], linha["fonte"]))
    extra = 0 if linha["origem"] == CFG["casa"] else round(posicionamento(linha["origem"], meds))
    return {**linha, "mediana": round(med) if med else None,
            "desconto": round(1 - linha["preco"] / med, 3) if med else None,
            "posicionamento": extra, "total_casa": linha["preco"] + extra,
            "cidade": cidade_de(linha["destino"]), "regiao": regiao_de(linha["destino"])}


# ---------------------------------------------------------------- milhas e links

def nota_milhas(regiao, preco):
    tabela = MILHAS["latam_tabela_fixa"]
    faixa = tabela["regioes"].get(regiao)
    notas = []
    if faixa and faixa["economica"]:
        custo = faixa["economica"] * 2 / 1000 * MILHAS["valor_milheiro_reais"]
        veredito = "milhas saem mais baratas" if custo < preco else "dinheiro sai mais barato"
        pontos = f"{faixa['economica']:,}".replace(",", ".")
        notas.append(f"Tabela fixa LATAM ({faixa['parceira']}): {pontos} pts por trecho"
                     + f", cerca de {brl(custo)} ida e volta com milheiro a {brl(MILHAS['valor_milheiro_reais'])}, mais taxas. Aqui {veredito}.")
    elif faixa:
        notas.append(f"Tabela fixa LATAM ({faixa['parceira']}): valor vigente não conferido, veja {tabela['link_oficial']}")
    parceiras_azul = MILHAS["azul_award"].get(regiao)
    if parceiras_azul:
        notas.append(f"Azul pelo Mundo: confira vaga Award em {parceiras_azul}.")
    return notas


def links(o):
    busca = f"Flights from {o['origem']} to {o['destino']} on {o['ida']} through {o['volta']}"
    google = "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=" + urllib.parse.quote(busca)
    seats = f"https://seats.aero/search?origin={o['origem']}&destination={o['destino']}&date={o['ida']}"
    return google, seats


# ---------------------------------------------------------------- alertas

def ntfy(texto, topico):
    """Envia pelo ntfy.sh (app gratuito, sem conta). O nome do tópico funciona como senha."""
    corpo = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"[\2](\1)", texto)
    corpo = html.unescape(re.sub(r"</?b>", "**", corpo))
    titulo, _, resto = corpo.partition("\n")
    dados = json.dumps({"topic": topico, "title": titulo.replace("**", "")[:200],
                        "message": (resto or titulo)[:3800], "markdown": True}).encode()
    try:
        baixar("https://ntfy.sh/", dados)
        return True
    except Exception as erro:
        print(f"  ntfy falhou: {type(erro).__name__}: {str(erro)[:120]}")
        return False


def telegram(texto):
    """Avisa pelo canal configurado: ntfy (NTFY_TOPICO) e/ou Telegram."""
    topico = os.environ.get("NTFY_TOPICO")
    enviado = ntfy(texto, topico) if topico else False
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        if not topico:
            print("[Nenhum canal de alerta configurado] " + re.sub(r"<[^>]+>", "", texto))
        return enviado
    corpo = urllib.parse.urlencode({"chat_id": chat, "text": texto[:4000], "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    try:
        baixar(f"https://api.telegram.org/bot{token}/sendMessage", corpo)
        return True
    except Exception as erro:
        print(f"  Telegram falhou: {type(erro).__name__}: {str(erro)[:120]}")
        return False


def texto_oportunidade(o):
    imperdivel = o["desconto"] >= CFG["desconto_imperdivel"]
    titulo = "🔥 IMPERDÍVEL" if imperdivel else "✈️ Oportunidade"
    google, seats = links(o)
    partes = [
        f"<b>{titulo}: {cidade_de(o['origem'])} ({o['origem']}) → {o['cidade']} ({o['destino']})</b>",
        f"{brl(o['preco'])} ida e volta, {o['desconto']:.0%} abaixo da mediana da rota ({brl(o['mediana'])})",
        f"{o['ida']} a {o['volta']} · {o['cia'] or 'cia não informada'} · {o['paradas']} parada(s)",
    ]
    if o["posicionamento"]:
        partes.append(f"Saindo de {CFG['casa']}: some cerca de {brl(o['posicionamento'])} de posicionamento, total {brl(o['total_casa'])}")
    partes += nota_milhas(o["regiao"], o["preco"])
    if imperdivel:
        partes.append("Pode ser tarifa erro: compre logo e espere de 7 a 15 dias antes de reservar hotel não reembolsável.")
    partes.append(f'<a href="{google}">Ver no Google Flights</a> · <a href="{seats}">Vagas Award (Seats.aero)</a>')
    return "\n".join(partes)


def detectar(novas, meds, estado):
    # Uma rota já avisada fica 3 dias em silêncio, salvo se o preço cair mais 5%.
    hoje = date.today().isoformat()
    corte = (date.today() - timedelta(days=3)).isoformat()
    estado["alertados"] = {k: v for k, v in estado["alertados"].items() if v[1] >= corte}
    achadas = []
    for linha in sorted(novas, key=lambda l: l["preco"]):
        o = avaliar(linha, meds)
        if o["desconto"] is None or o["desconto"] < CFG["desconto_alerta"]:
            continue
        chave = f"{o['origem']}-{o['destino']}"
        anterior = estado["alertados"].get(chave)
        if anterior and o["preco"] > anterior[0] * 0.95:
            continue
        estado["alertados"][chave] = [o["preco"], hoje]
        achadas.append(o)
    achadas.sort(key=lambda o: -o["desconto"])
    return achadas[: CFG["max_alertas_por_execucao"]]


# ---------------------------------------------------------------- promoções (RSS)

def ler_feeds(estado):
    primeira_vez = not estado["feeds_vistos"]
    vistos, novos = set(estado["feeds_vistos"]), []
    for feed in CFG["feeds"]:
        try:
            raiz = ET.fromstring(baixar(feed["url"]))
        except Exception as erro:
            print(f"  feed {feed['nome']}: {type(erro).__name__}: {str(erro)[:100]}")
            continue
        for item in raiz.iter("item"):
            titulo = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not link or link in vistos:
                continue
            vistos.add(link)
            estado["feeds_vistos"].append(link)
            if re.search(feed["filtro"], titulo, re.I):
                novos.append({"fonte": feed["nome"], "titulo": titulo, "link": link,
                              "visto_em": date.today().isoformat()})
    estado["feeds_vistos"] = estado["feeds_vistos"][-800:]
    estado["promos"] = (novos + estado["promos"])[:40]
    return [] if primeira_vez else novos  # na primeira execução só memoriza, sem enxurrada


# ---------------------------------------------------------------- painel

def gerar_painel(historico, meds, estado, agora):
    hoje = date.today().isoformat()
    recente = (date.today() - timedelta(days=10)).isoformat()
    ultimas = {}
    for l in historico:
        if l["ida"] > hoje and l["coletado_em"][:10] >= recente:
            ultimas[(l["origem"], l["destino"], l["ida"], l["fonte"])] = l
    ofertas = []
    for l in ultimas.values():
        o = avaliar(l, meds)
        o["link"] = links(o)[0]
        ofertas.append(o)
    dados = {"atualizado_em": agora, "casa": CFG["casa"], "ofertas": ofertas, "promos": estado["promos"],
             "limiar": CFG["desconto_alerta"]}
    pagina = PAINEL_MODELO.read_text(encoding="utf-8").replace(
        "/*DADOS*/null", json.dumps(dados, ensure_ascii=False).replace("</", "<\\/"))
    PAINEL.parent.mkdir(exist_ok=True)
    PAINEL.write_text(pagina, encoding="utf-8")


# ---------------------------------------------------------------- execução

def main():
    if "--teste" in sys.argv:
        ok = telegram("✅ Radar de passagens conectado. Os alertas vão chegar aqui.")
        sys.exit(0 if ok else 1)

    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    estado = {"cursor": 0, "ciclo": 0, "alertados": {}, "feeds_vistos": [], "promos": [],
              **(ler_json(ESTADO) or {})}

    novas, falhas = coletar_google(estado, agora)
    novas += coletar_aviasales(agora)
    historico = ler_historico() + novas
    gravar_historico(historico)
    meds = medianas(historico)

    oportunidades = detectar(novas, meds, estado)
    if oportunidades:
        telegram("\n\n".join(texto_oportunidade(o) for o in oportunidades))

    promos = ler_feeds(estado)
    if promos:
        linhas = [f'• <a href="{p["link"]}">{html.escape(p["titulo"])}</a> ({p["fonte"]})' for p in promos[:12]]
        telegram("<b>📰 Promoções publicadas</b>\n" + "\n".join(linhas))

    if falhas and not novas:
        estado["falhas_seguidas"] = estado.get("falhas_seguidas", 0) + 1
        if estado["falhas_seguidas"] == 4:
            telegram("⚠️ Radar: 4 execuções seguidas sem conseguir ler preços. Veja o LEIAME, seção Problemas.")
    else:
        estado["falhas_seguidas"] = 0

    gerar_painel(historico, meds, estado, agora)
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{agora}: {len(novas)} preços novos, {falhas} falhas, {len(oportunidades)} oportunidades, "
          f"{len(promos)} promoções, {len(meds)} rotas com histórico suficiente.")


if __name__ == "__main__":
    main()
