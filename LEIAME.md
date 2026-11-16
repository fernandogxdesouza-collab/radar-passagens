# Radar de Passagens

Robô que lê preços de passagens 4 vezes ao dia, compara cada preço com a mediana da própria rota e avisa no Telegram quando algo cai muito abaixo do habitual. Gera também um painel com filtros de região, origem, data e preço.

## O que ele faz

| Camada | Fonte | Situação |
|---|---|---|
| Preço em dinheiro (ida e volta, econômica) | Google Flights, via biblioteca `faster-flights` | Testado em 15/11/2026 |
| Promoções e tarifas erro | RSS do Melhores Destinos, Passageiro de Primeira e Fly4free | Testado |
| Voo de posicionamento | Mediana observada de CNF até GRU e GIG, somada ao preço do hub | Testado |
| Milhas | Calculadora: compara o preço com a tabela fixa LATAM e aponta parceiras da Azul | Sete regiões preenchidas com a tabela de 23/11/2026 |
| Qualquer destino do mundo | API de cache da Aviasales (Travelpayouts) | Opcional, código ainda não testado com token real |

O que ele **não** faz: ver disponibilidade de assento Award. Isso só existe em ferramenta paga (Seats.aero Pro) ou raspando site de companhia aérea. Cada alerta traz o link do Seats.aero para você conferir a vaga à mão.

## Como decide o que é oportunidade

- Cada rota acumula os preços dos últimos 90 dias. Com 6 observações ou mais, calcula a mediana.
- Preço 35% abaixo da mediana vira alerta. 50% abaixo vira "imperdível", com aviso de possível tarifa erro.
- A mesma rota fica 3 dias em silêncio depois de um alerta, salvo se cair mais 5%.
- Nos primeiros 2 a 3 dias não sai alerta de preço: o robô ainda está formando o histórico. As promoções dos blogs chegam desde a segunda execução.

São 98 rotas e 8 meses à frente. Com 80 consultas por execução, o ciclo completo leva cerca de 2 dias e meio.

## Instalação (uns 20 minutos, uma vez só)

### 1. Bot do Telegram
1. No Telegram, abra uma conversa com **@BotFather**, envie `/newbot` e siga as perguntas. Guarde o token que ele devolve.
2. Abra a conversa com o seu bot novo e envie qualquer mensagem.
3. No navegador, abra `https://api.telegram.org/botSEU_TOKEN/getUpdates` e copie o número em `"chat":{"id": ...}`.

### 2. Repositório no GitHub
1. Crie uma conta gratuita em github.com e um repositório **público** chamado `radar-passagens`, vazio. Público porque o GitHub só dá execução ilimitada e página gratuita para repositório público. Ele guarda apenas preços de passagens; os tokens ficam em Secrets, fora do código.
2. Nesta pasta, rode um comando por vez:

```bash
git init -b main
```
```bash
git add .
```
```bash
git commit -m "Radar de passagens"
```
```bash
git remote add origin https://github.com/SEU_USUARIO/radar-passagens.git
```
```bash
git push -u origin main
```

### 3. Segredos e painel
1. No repositório: **Settings > Secrets and variables > Actions > New repository secret**. Crie `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.
2. **Settings > Pages**: em Source escolha "Deploy from a branch", branch `main`, pasta `/docs`. O painel fica em `https://SEU_USUARIO.github.io/radar-passagens/`.
3. **Actions > Radar de passagens > Run workflow** para a primeira execução manual. Depois ele roda sozinho.

### 4. Testar o Telegram no PC (opcional)

```powershell
$env:TELEGRAM_TOKEN="..."; $env:TELEGRAM_CHAT_ID="..."; python radar.py --teste
```

## Ajustes

Tudo em [config.json](config.json):
- `destinos`: acrescente ou remova aeroportos no formato `"IATA": ["Cidade", "Região"]`.
- `desconto_alerta` e `desconto_imperdivel`: suba se chegar alerta demais, desça se chegar de menos.
- `estadia_dias`: duração da viagem simulada por região.
- `feeds`: `filtro` é a lista de palavras que o título precisa conter.

Em [milhas.json](milhas.json):
- `valor_milheiro_reais`: quanto você paga por mil pontos LATAM. O padrão de R$ 25 é um chute inicial.
- `latam_tabela_fixa`: pontos por trecho saindo do Brasil, com o reajuste de 20% vigente em 23/11/2026. Os valores vêm de duas publicações que batem entre si, não do comunicado oficial; confirme no site do LATAM Pass antes de emitir e atualize aqui a cada novo reajuste (foram dois em 2026).

## Cobertura mundial (opcional)

A API de dados da Aviasales devolve os preços mais baratos encontrados para **qualquer** destino a partir de uma origem. Exige cadastro gratuito no Travelpayouts e adesão ao programa Aviasales. Com o token em mãos, crie o secret `TRAVELPAYOUTS_TOKEN` e o robô passa a usá-lo. Duas ressalvas: o cache vem de buscas de usuários da Aviasales, que são poucos no Brasil, então a cobertura a partir de CNF pode ser rala; e esse trecho do código precisa de um teste real na primeira execução.

## Problemas

- **Aviso "4 execuções seguidas sem conseguir ler preços"**: o Google pode recusar os servidores do GitHub ou ter mudado o formato. Primeiro tente atualizar a versão em `requirements.txt`. Se persistir, rode no PC pelo Agendador de Tarefas do Windows com `python radar.py` (o uso de memória é baixo).
- **Preço do alerta diferente do site**: o robô pega o menor entre os primeiros resultados do Google, no instante da coleta. Sempre confirme no link antes de comprar.
- **Rota sempre em alerta**: a mediana se ajusta sozinha em algumas semanas; se incomodar, suba `desconto_alerta`.

## Estratégias que ficam por sua conta

O alerta mostra onde olhar. Estas continuam manuais:
- **Tabela fixa LATAM**: ache a vaga Award no site da parceira ou no Seats.aero, anote voo e data, emita pelo WhatsApp do LATAM Pass.
- **Azul pelo Mundo**: confira a vaga Saver na United ou na Air Canada e repita a busca no portal da Azul.
- **Ponto de venda estrangeiro**: para voo interno em outro país, compare o preço no site local da companhia, em moeda local.
- **Hidden city (Skiplagged)**: só bagagem de mão, só ida, sem número de fidelidade.
- **ITA Matrix e FlightConnections**: para montar rotas específicas depois que o alerta apontar a região barata.
