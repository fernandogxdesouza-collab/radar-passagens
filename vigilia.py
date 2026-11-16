"""Vigília da madrugada: mede os MESMOS voos várias vezes por noite para flagrar promoções relâmpago.

Uso: python vigilia.py
Cada execução consulta a cesta fixa de itinerários (config.json > vigilia) e grava em dados/vigilia.csv.
Na primeira execução depois das 7h, compara a noite inteira, registra as quedas em dados/quedas.csv
(base para achar padrão de dia, hora e companhia) e manda um resumo pelo ntfy.
"""
import csv
import json
import random
import time
from datetime import date, datetime, timedelta, timezone

import radar

CFG = radar.CFG["vigilia"]
MEDIDAS = radar.DADOS / "vigilia.csv"
QUEDAS = radar.DADOS / "quedas.csv"
CAMPOS = ["coletado_em", "origem", "destino", "ida", "volta", "preco", "cia", "faixa_baixa"]
CAMPOS_QUEDA = ["noite", "dia_semana", "hora_minimo", "origem", "destino", "ida", "volta", "cia",
                "referencia", "minimo", "queda", "manha", "persistiu", "faixa_verde"]
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
BRT = timezone(timedelta(hours=-3))


def cesta():
    """Itinerários fixos da semana: sexta a segunda, a N semanas de distância.

    As datas só mudam na virada da semana, para que cada voo seja medido muitas vezes.
    """
    segunda = date.today() - timedelta(days=date.today().weekday())
    itens = []
    for semanas in CFG["semanas_a_frente"]:
        ida = segunda + timedelta(weeks=semanas, days=4)
        volta = ida + timedelta(days=CFG["estadia_dias"])
        itens += [(o, d, ida.isoformat(), volta.isoformat()) for o, d in CFG["rotas"]]
    return itens


def ler(caminho):
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def gravar(caminho, campos, linhas):
    radar.DADOS.mkdir(exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=campos, restval="", extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(linhas)


def medir(agora):
    linhas, falhas = [], 0
    itens = cesta()
    random.shuffle(itens)  # se o tempo acabar, não são sempre as mesmas rotas que ficam de fora
    for origem, destino, ida, volta in itens:
        if radar.sem_tempo():
            break
        try:
            achado = radar.consultar_google(origem, destino, ida, volta)
        except Exception as erro:
            achado, falhas = None, falhas + 1
            print(f"  falha {origem}-{destino} {ida}: {type(erro).__name__}: {str(erro)[:100]}")
        if achado:
            linhas.append({"coletado_em": agora, "origem": origem, "destino": destino, "ida": ida, "volta": volta,
                           "preco": achado["preco"], "cia": achado["cia"], "faixa_baixa": achado["faixa_baixa"]})
        time.sleep(random.uniform(*radar.CFG["pausa_entre_consultas_seg"]))
    return linhas, falhas


def quedas_da_noite(medidas, agora_dt):
    """Compara, voo a voo, o preço do começo da noite com o menor preço visto até de manhã."""
    inicio = (agora_dt - timedelta(days=1)).replace(hour=CFG["noite_comeca_hora"], minute=0).strftime("%Y-%m-%d %H:%M")
    por_voo = {}
    for m in medidas:
        if m["coletado_em"] >= inicio:
            por_voo.setdefault((m["origem"], m["destino"], m["ida"], m["volta"]), []).append(m)
    achadas = []
    for (origem, destino, ida, volta), serie in por_voo.items():
        serie.sort(key=lambda m: m["coletado_em"])
        if len(serie) < 2:
            continue
        referencia, manha = int(serie[0]["preco"]), int(serie[-1]["preco"])
        menor = min(serie[1:], key=lambda m: int(m["preco"]))
        minimo = int(menor["preco"])
        queda = 1 - minimo / referencia
        if queda < CFG["queda_minima"]:
            continue
        piso = menor.get("faixa_baixa")
        achadas.append({"noite": inicio[:10], "dia_semana": DIAS[date.fromisoformat(inicio[:10]).weekday()],
                        "hora_minimo": menor["coletado_em"][11:13], "origem": origem, "destino": destino,
                        "ida": ida, "volta": volta, "cia": menor["cia"], "referencia": referencia,
                        "minimo": minimo, "queda": round(queda, 3), "manha": manha,
                        "persistiu": "sim" if manha <= minimo * 1.03 else "não",
                        "faixa_verde": "sim" if piso and minimo <= int(piso) else "não"})
    return sorted(achadas, key=lambda q: -q["queda"])


def texto_resumo(quedas, medidos):
    if not quedas:
        return (f"<b>🌙 Madrugada sem promoção</b>\nNenhum dos {medidos} voos vigiados caiu "
                f"{CFG['queda_minima']:.0%} ou mais desde ontem à noite.")
    linhas = [f"<b>🌙 Madrugada: {len(quedas)} queda(s) de preço</b>"]
    for q in quedas[:6]:
        fim = "ainda vale" if q["persistiu"] == "sim" else f"voltou a {radar.brl(q['manha'])}"
        verde = ", faixa verde" if q["faixa_verde"] == "sim" else ""
        linhas.append(f"• {q['origem']}→{radar.cidade_de(q['destino'])} {q['ida'][8:]}/{q['ida'][5:7]}: "
                      f"{radar.brl(q['referencia'])} → {radar.brl(q['minimo'])} às {q['hora_minimo']}h "
                      f"(-{q['queda']:.0%}, {q['cia']}{verde}); {fim}")
    if len(quedas) > 6:
        linhas.append(f"e mais {len(quedas) - 6} no painel.")
    return "\n".join(linhas)


def main():
    agora_dt = datetime.now(BRT)
    agora = agora_dt.strftime("%Y-%m-%d %H:%M")
    estado = radar.ler_json(radar.ESTADO) or {}

    novas, falhas = medir(agora)
    corte = (date.today() - timedelta(days=CFG["guardar_dias"])).isoformat()
    medidas = [m for m in ler(MEDIDAS) if m["coletado_em"][:10] >= corte] + novas
    gravar(MEDIDAS, CAMPOS, medidas)

    resumo_feito = False
    inicio_noite = (agora_dt - timedelta(days=1)).replace(hour=CFG["noite_comeca_hora"], minute=0).strftime("%Y-%m-%d %H:%M")
    rodadas_da_noite = len({m["coletado_em"] for m in medidas if m["coletado_em"] >= inicio_noite})
    if (agora_dt.hour >= CFG["resumo_apos_hora"] and estado.get("vigilia_resumo") != agora[:10]
            and rodadas_da_noite >= 3):  # sem pelo menos 3 medições não há o que comparar
        quedas = quedas_da_noite(medidas, agora_dt)
        gravar(QUEDAS, CAMPOS_QUEDA, ler(QUEDAS) + quedas)
        voos = len({(m["origem"], m["destino"], m["ida"]) for m in novas})
        radar.telegram(texto_resumo(quedas, voos), prioridade=3 if quedas else 2)
        estado["vigilia_resumo"] = agora[:10]
        resumo_feito = True

    historico = radar.ler_historico()
    radar.gerar_painel(historico, radar.medianas(historico), {"promos": [], **estado}, agora)
    radar.ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{agora}: vigília mediu {len(novas)} voos, {falhas} falhas"
          + (", resumo da madrugada enviado." if resumo_feito else "."))


if __name__ == "__main__":
    main()
