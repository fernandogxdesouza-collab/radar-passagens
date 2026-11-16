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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).parent
DADOS = RAIZ / "dados"
PRECOS = DADOS / "precos.csv"
ESTADO = DADOS / "estado.json"
PAINEL_MODELO = RAIZ / "painel_modelo.html"
PAINEL = RAIZ / "docs" / "index.html"
CAMPOS = ["coletado_em", "origem", "destino", "ida", "volta", "preco", "cia", "paradas", "fonte",
          "faixa_baixa", "faixa_alta", "cidade", "regiao"]
EXPLORAR_URL = ("https://www.google.com/_/FlightsFrontendUi/data/travel.frontend.flights."
                "FlightsFrontendService/GetExploreDestinations?hl=pt-BR&gl=BR&curr=BRL")
DURACOES = {1: "fim de semana", 2: "1 semana", 3: "2 semanas"}  # opções de "datas flexíveis" do Google
# Termômetro de preços do Google: [nível, [preço atual], [..], [..], [piso da faixa normal], [teto]
TERMOMETRO = re.compile(r"\[(\d),\[null,(\d+)\],(?:\[null,-?\d+\]|null),(?:\[null,-?\d+\]|null),"
                        r"\[null,(\d+)\],\[null,(\d+)\],")
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
    if codigo == CFG["casa"]:
        return CFG.get("cidade_casa", codigo)
    return CFG["destinos"].get(codigo, [codigo])[0]


def montar_rotas():
    casa, hubs = CFG["casa"], CFG["hubs"]
    rotas = [(casa, d) for d in CFG["destinos"] if d != casa]
    for hub in hubs:
        rotas += [(hub, d) for d, (_, reg) in CFG["destinos"].items() if reg != "Brasil"]
    return rotas


def datas_da_tarefa(indice_rota, fatia, ciclo, regiao):
    """Espalha as datas de ida pelo horizonte; a cada ciclo o dia muda, para cobrir datas diferentes."""
    opcoes = CFG["estadias_grade_dias"]  # durações que o "Explorar" do Google não oferece
    estadia = opcoes[(ciclo + indice_rota + fatia) % len(opcoes)]
    passo = CFG["horizonte_dias"] // CFG["datas_por_rota"]
    dias = 7 + passo * fatia + (ciclo * 3 + indice_rota) % passo
    ida = date.today() + timedelta(days=min(dias, CFG["horizonte_dias"]))
    return ida.isoformat(), (ida + timedelta(days=estadia)).isoformat()


# ---------------------------------------------------------------- fontes de preço

def consultar_google(origem, destino, ida, volta):
    """Menor preço ida e volta em reais no Google Flights, ou None."""
    from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
    from fast_flights.parser import parse

    consulta = create_query(
        flights=[
            FlightQuery(date=ida, from_airport=origem, to_airport=destino),
            FlightQuery(date=volta, from_airport=destino, to_airport=origem),
        ],
        trip="round-trip", seat="economy", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL",
    )
    pagina = fetch_flights_html(consulta)
    resultado = parse(pagina)
    voos = [v for v in resultado if getattr(v, "price", 0)]
    if not voos:
        return None
    melhor = min(voos, key=lambda v: v.price)
    preco = int(melhor.price)
    try:  # o Google informa o menor preço da busca, às vezes abaixo dos voos listados
        menor = resultado.metadata.shopping.cheapest_price
        if menor and 0 < menor < preco:
            preco = int(menor)
    except AttributeError:
        pass
    achado = {"preco": preco, "cia": " + ".join(melhor.airlines or []),
              "paradas": max(len(melhor.flights or []) - 1, 0), "faixa_baixa": "", "faixa_alta": ""}
    termometro = TERMOMETRO.search(pagina)
    if termometro and 0 < int(termometro[3]) < int(termometro[4]):
        achado.update(faixa_baixa=int(termometro[3]), faixa_alta=int(termometro[4]))
    return achado


def coletar_google(estado, agora):
    rotas = montar_rotas()
    tarefas = [(i, r, f) for f in range(CFG["datas_por_rota"]) for i, r in enumerate(rotas)]
    limite = int(os.environ.get("RADAR_CONSULTAS", CFG["consultas_por_execucao"]))
    linhas, falhas = [], 0
    for _ in range(min(limite, len(tarefas))):
        if estado["cursor"] >= len(tarefas):
            estado["cursor"], estado["ciclo"] = 0, estado["ciclo"] + 1
        i, (origem, destino), fatia = tarefas[estado["cursor"]]
        estado["cursor"] += 1
        ida, volta = datas_da_tarefa(i, fatia, estado["ciclo"], regiao_de(destino))
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


def regiao_por_coordenada(pais, lat, lon):
    """Região aproximada de um destino qualquer do mundo, pela posição no mapa."""
    if pais == "Brasil":
        return "Brasil"
    if lon < -30:
        if lon < -120 and lat < 15:
            return "Oceania"
        if pais in ("Colômbia", "Venezuela"):
            return "América do Sul"
        if 7 <= lat <= 23.5 and -93 <= lon <= -59:
            return "América Central e Caribe"
        return "América do Norte" if lat > 13 else "América do Sul"
    if lat > 35 and lon < 45:
        return "Europa"
    if 12 <= lat <= 42 and 34 <= lon <= 60:
        return "Oriente Médio"
    if (lon <= 52 and lat <= 37) or (lon <= 60 and lat <= 12):
        return "África"
    if lat < -8 and lon > 110:
        return "Oceania"
    return "Ásia"


def explorar(origem, duracao):
    """Busca "Explorar" do Google com datas flexíveis nos próximos seis meses.

    Devolve, para cada destino do mundo que o Google sugere, a data mais barata do período.
    """
    from fast_flights.fetcher import _build_default_client

    local = [[[origem, 0]]]
    pedido = [[], None, None,
              [None, None, 1, None, [0, duracao], 1, [1, 0, 0, 0], None, None, None, None, None, None,
               [[local, [], None, 0], [[], local, None, 0]],
               None, None, None, 0, None, None, None, None, None, None, None, 1],
              None, 1, None, 0, None, 0, [1164, 595], 2]
    corpo = "f.req=" + urllib.parse.quote(json.dumps([None, json.dumps(pedido, separators=(",", ":"))],
                                                     separators=(",", ":")))
    resposta = _build_default_client(proxy=None).post(
        EXPLORAR_URL, content=corpo.encode(), timeout=90,
        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8", "x-same-domain": "1"})
    if resposta.status_code != 200:
        raise RuntimeError(f"Explorar devolveu HTTP {resposta.status_code}")

    lugares, precos = {}, {}

    def varrer(no):
        if not isinstance(no, list):
            return
        try:
            if isinstance(no[0], str) and no[0].startswith("/") and len(no) > 12:
                if isinstance(no[1], list) and len(no[1]) == 2 and isinstance(no[1][0], float) and isinstance(no[2], str):
                    lugares[no[0]] = no
                elif isinstance(no[1], list) and isinstance(no[1][0], list) and isinstance(no[1][0][1], int):
                    precos[no[0]] = no
        except (IndexError, TypeError):
            pass
        for filho in no:
            varrer(filho)

    for linha in resposta.text.split("\n"):
        if "wrb.fr" in linha:
            for item in json.loads(linha):
                if item and item[0] == "wrb.fr" and item[2]:
                    varrer(json.loads(item[2]))

    achados = []
    for chave, lugar in lugares.items():
        try:
            voo = precos[chave][6]
            achados.append({"origem": origem, "destino": voo[5], "ida": lugar[11], "volta": lugar[12],
                            "preco": int(precos[chave][1][0][1]), "cia": voo[1], "paradas": voo[2],
                            "cidade": lugar[2], "duracao": duracao,
                            "regiao": regiao_por_coordenada(lugar[4], lugar[1][0], lugar[1][1])})
        except (KeyError, IndexError, TypeError):
            continue
    return [a for a in achados if a["destino"] and a["ida"] and a["volta"]]


def coletar_explorar(estado, agora):
    """Varre o mundo pelo "Explorar" e confere no termômetro os candidatos mais promissores."""
    candidatos, falhas = [], 0
    for origem in [CFG["casa"], *CFG["hubs"]]:
        for duracao in CFG["explorar_duracoes"]:
            try:
                achados = explorar(origem, duracao)
                if not achados:  # o Google às vezes devolve erro passageiro; tenta mais uma vez
                    time.sleep(6)
                    achados = explorar(origem, duracao)
                if not achados:
                    raise RuntimeError("resposta vazia")
                candidatos += achados
            except Exception as erro:
                falhas += 1
                print(f"  explorar {origem} {DURACOES.get(duracao)}: {type(erro).__name__}: {str(erro)[:100]}")
            time.sleep(random.uniform(*CFG["pausa_entre_consultas_seg"]))
    linhas = [{"coletado_em": agora, "fonte": "explorar", "faixa_baixa": "", "faixa_alta": "", **c}
              for c in candidatos]

    # Prioridade da conferência: quem caiu 10% desde a última vez, depois quem está há mais tempo sem conferir.
    memoria = estado.setdefault("explorar", {})

    def prioridade(c):
        ultimo_preco, conferido_em = memoria.get(f"{c['origem']}-{c['destino']}-{c['duracao']}", [None, ""])
        caiu = bool(ultimo_preco) and c["preco"] <= ultimo_preco * 0.9
        return (not caiu, conferido_em)

    for c in sorted(candidatos, key=prioridade)[: CFG["explorar_conferencias"]]:
        chave = f"{c['origem']}-{c['destino']}-{c['duracao']}"
        memoria[chave] = [c["preco"], agora]
        try:
            achado = consultar_google(c["origem"], c["destino"], c["ida"], c["volta"])
        except Exception as erro:
            achado, falhas = None, falhas + 1
            print(f"  falha {chave} {c['ida']}: {type(erro).__name__}: {str(erro)[:100]}")
        if achado:
            linhas.append({"coletado_em": agora, "origem": c["origem"], "destino": c["destino"], "ida": c["ida"],
                           "volta": c["volta"], "fonte": "google", "cidade": c["cidade"], "regiao": c["regiao"],
                           **achado})
        time.sleep(random.uniform(*CFG["pausa_entre_consultas_seg"]))
    return linhas, falhas, len(candidatos)


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
        for campo in ("faixa_baixa", "faixa_alta"):
            l[campo] = int(l[campo]) if l.get(campo) else ""
    return linhas


def gravar_historico(linhas):
    DADOS.mkdir(exist_ok=True)
    with PRECOS.open("w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=CAMPOS, restval="", extrasaction="ignore")
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
    """Classifica a observação e calcula o custo total saindo de casa.

    Referência = piso da faixa normal do termômetro do Google (abaixo dele é a faixa verde).
    Sem termômetro, usa a mediana da rota menos desconto_alerta. folga >= 0 é oportunidade.
    """
    med = meds.get((linha["origem"], linha["destino"], linha["fonte"]))
    extra = 0 if linha["origem"] == CFG["casa"] else round(posicionamento(linha["origem"], meds))
    if linha.get("faixa_baixa"):
        base, ref = "termometro", linha["faixa_baixa"]
    elif med:
        base, ref = "mediana", med * (1 - CFG["desconto_alerta"])
    else:
        base, ref = None, None
    folga = round(1 - linha["preco"] / ref, 3) if ref else None
    return {**linha, "mediana": round(med) if med else None, "base": base, "folga": folga,
            "oportunidade": folga is not None and folga >= 0,
            "imperdivel": folga is not None and folga >= CFG["folga_imperdivel"],
            "posicionamento": extra, "total_casa": linha["preco"] + extra,
            "cidade": linha.get("cidade") or cidade_de(linha["destino"]),
            "regiao": linha.get("regiao") or regiao_de(linha["destino"])}


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

def ntfy(texto, topico, prioridade=3):
    """Envia pelo ntfy.sh (app gratuito, sem conta). O nome do tópico funciona como senha."""
    corpo = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"[\2](\1)", texto)
    corpo = html.unescape(re.sub(r"</?b>", "**", corpo))
    titulo, _, resto = corpo.partition("\n")
    dados = json.dumps({"topic": topico, "title": titulo.replace("**", "")[:200],
                        "message": (resto or titulo)[:3800], "markdown": True,
                        "priority": prioridade}).encode()
    try:
        baixar("https://ntfy.sh/", dados)
        return True
    except Exception as erro:
        print(f"  ntfy falhou: {type(erro).__name__}: {str(erro)[:120]}")
        return False


def whatsapp(texto, telefone, chave):
    """Envia ao próprio WhatsApp pelo CallMeBot (serviço gratuito de terceiros, uso pessoal).

    Ainda não testado com uma chave real. O telefone e a chave vão na URL, como a API exige.
    """
    corpo = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2: \1", texto)
    corpo = html.unescape(re.sub(r"</?b>", "*", corpo))
    params = urllib.parse.urlencode({"phone": telefone, "text": corpo[:3500], "apikey": chave})
    try:
        baixar("https://api.callmebot.com/whatsapp.php?" + params)
        return True
    except Exception as erro:
        print(f"  WhatsApp falhou: {type(erro).__name__}: {str(erro)[:80]}")
        return False


def telefone_whatsapp():
    return os.environ.get("CALLMEBOT_TELEFONE"), os.environ.get("CALLMEBOT_CHAVE")


def telegram(texto, prioridade=3):
    """Avisa por todos os canais configurados: ntfy, WhatsApp (CallMeBot) e Telegram."""
    topico = os.environ.get("NTFY_TOPICO")
    enviado = ntfy(texto, topico, prioridade) if topico else False
    fone, chave = telefone_whatsapp()
    if fone and chave:
        enviado = whatsapp(texto, fone, chave) or enviado
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        if not (topico or (fone and chave)):
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
    imperdivel = o["imperdivel"]
    titulo = "🔥 IMPERDÍVEL" if imperdivel else "🟢 Preço baixo"
    if o["base"] == "termometro":
        motivo = (f"faixa verde do Google, {o['folga']:.0%} abaixo do piso normal "
                  f"(normal: {brl(o['faixa_baixa'])} a {brl(o['faixa_alta'])})")
    else:
        motivo = (f"{1 - o['preco'] / o['mediana']:.0%} abaixo da mediana da rota ({brl(o['mediana'])}); "
                  "sem termômetro do Google")
    google, seats = links(o)
    partes = [
        f"<b>{titulo}: {cidade_de(o['origem'])} ({o['origem']}) → {o['cidade']} ({o['destino']})</b>",
        f"{brl(o['preco'])} ida e volta: {motivo}",
        f"{o['ida']} a {o['volta']} · {o['cia'] or 'cia não informada'} · {o['paradas']} parada(s)",
    ]
    if o["posicionamento"]:
        partes.append(f"Saindo de {CFG['casa']}: some cerca de {brl(o['posicionamento'])} de posicionamento, total {brl(o['total_casa'])}")
    partes += nota_milhas(o["regiao"], o["preco"])
    if o["folga"] >= 0.4:
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
        if not o["oportunidade"] or linha["fonte"] == "explorar":  # "explorar" só vale depois de conferido
            continue
        chave = f"{o['origem']}-{o['destino']}"
        anterior = estado["alertados"].get(chave)
        if anterior and o["preco"] > anterior[0] * 0.95:
            continue
        estado["alertados"][chave] = [o["preco"], hoje]
        achadas.append(o)
    achadas.sort(key=lambda o: -o["folga"])
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
    for l in sorted(historico, key=lambda l: (l["fonte"] == "google", l["coletado_em"])):
        if l["ida"] > hoje and l["coletado_em"][:10] >= recente:  # preço conferido prevalece sobre o do "explorar"
            ultimas[(l["origem"], l["destino"], l["ida"], l["volta"])] = l
    ofertas = []
    for l in ultimas.values():
        o = avaliar(l, meds)
        o["link"] = links(o)[0]
        ofertas.append(o)
    dados = {"atualizado_em": agora, "casa": CFG["casa"], "ofertas": ofertas, "promos": estado["promos"]}
    pagina = PAINEL_MODELO.read_text(encoding="utf-8").replace(
        "/*DADOS*/null", json.dumps(dados, ensure_ascii=False).replace("</", "<\\/"))
    PAINEL.parent.mkdir(exist_ok=True)
    PAINEL.write_text(pagina, encoding="utf-8")


# ---------------------------------------------------------------- execução

def main():
    if "--teste" in sys.argv:
        ok = telegram("✅ Radar de passagens conectado. Os alertas vão chegar aqui.")
        sys.exit(0 if ok else 1)

    agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M")  # horário de Brasília
    estado = {"cursor": 0, "ciclo": 0, "alertados": {}, "feeds_vistos": [], "promos": [],
              **(ler_json(ESTADO) or {})}

    novas, falhas = coletar_google(estado, agora)
    exploradas, falhas_explorar, candidatos = coletar_explorar(estado, agora)
    novas += exploradas
    falhas += falhas_explorar
    novas += coletar_aviasales(agora)
    historico = ler_historico() + novas
    gravar_historico(historico)
    meds = medianas(historico)

    oportunidades = detectar(novas, meds, estado)
    if oportunidades:  # com som; "imperdível" sai com prioridade alta
        telegram("\n\n".join(texto_oportunidade(o) for o in oportunidades),
                 prioridade=4 if any(o["imperdivel"] for o in oportunidades) else 3)

    # Promoções dos blogs: acumulam e saem uma vez por dia, em silêncio.
    promos = ler_feeds(estado)
    estado["promos_fila"] = promos + estado.get("promos_fila", [])
    inicio, fim = CFG["promos_janela_horas"]
    if estado["promos_fila"] and inicio <= int(agora[11:13]) < fim:
        linhas = [f'• <a href="{p["link"]}">{html.escape(p["titulo"])}</a> ({p["fonte"]})'
                  for p in estado["promos_fila"][:15]]
        telegram("<b>📰 Promoções do dia</b>\n" + "\n".join(linhas), prioridade=2)
        estado["promos_fila"] = []

    if falhas and not novas:
        estado["falhas_seguidas"] = estado.get("falhas_seguidas", 0) + 1
        if estado["falhas_seguidas"] == 4:
            telegram("⚠️ Radar: 4 execuções seguidas sem conseguir ler preços. Veja o LEIAME, seção Problemas.")
    else:
        estado["falhas_seguidas"] = 0

    gerar_painel(historico, meds, estado, agora)
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{agora}: {candidatos} destinos no explorar, {len(novas)} preços novos, {falhas} falhas, {len(oportunidades)} oportunidades, "
          f"{len(promos)} promoções, {len(meds)} rotas com histórico suficiente.")


if __name__ == "__main__":
    main()
