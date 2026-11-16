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

- **Faixa verde do Google.** Cada busca traz o termômetro de preços do Google Flights (piso e teto da faixa normal para aquela rota e data). O alerta sai quando o preço fica abaixo do piso, que é a faixa verde.
- **Imperdível** é 20% ou mais abaixo do piso (`folga_imperdivel`). Sai com prioridade alta e aviso de possível tarifa erro.
- **Sem termômetro** (o Google não mostra para toda rota): vale a regra antiga, preço 35% abaixo da mediana da própria rota, com 6 observações ou mais.
- A mesma rota fica 3 dias em silêncio depois de um alerta, salvo se cair mais 5%.
- **Promoções dos blogs** saem uma vez por dia, na rodada das 11h, sem som.

## Onde ele procura

1. **Explorar do Google, datas flexíveis.** A cada rodada, para CNF, GRU e GIG, pede ao Google os destinos mais baratos do mundo nos próximos seis meses, em três durações: fim de semana, 1 semana e 2 semanas (são as únicas que o filtro de datas flexíveis oferece). Vêm cerca de 70 a 90 destinos por consulta, cada um com a data mais barata do período.
2. **Conferência no termômetro.** Os 50 candidatos mais promissores da rodada (quem caiu 10% desde a última vez, depois quem está há mais tempo sem conferir) são consultados na data exata para ler a faixa verde. Só preço conferido gera alerta.
3. **Grade complementar.** As rotas do `config.json` são consultadas em datas espalhadas pelos próximos 180 dias com estadias de 3, 4, 5 e 10 dias (`estadias_grade_dias`), que o Explorar não cobre.

4. **Outras classes e só ida.** A cada rodada, para uma origem em rodízio (CNF, GRU, GIG), o Explorar também é consultado em econômica premium, executiva, primeira e só ida (1 semana). Esses preços aparecem no painel, sem alerta.

O Explorar recusa mais da metade das chamadas com um erro passageiro; o robô insiste até 6 vezes em cada uma.

## Painel

- **Viagem:** tipo (ida e volta ou só ida), classe e passageiros. O preço mostrado é o de 1 adulto multiplicado pelos pagantes (adultos e crianças de 2 a 11), portanto estimado; o botão "Abrir no Google Flights" leva a busca com classe, tipo e passageiros exatos.
- **Filtros:** região, origem, duração, datas, preço total máximo e "só oportunidades".
- **Como economizar mais:** em cada cartão, dicas montadas a partir dos preços coletados (voo de posicionamento com a conta feita, só ida contra ida e volta, milhas contra dinheiro pela tabela fixa) e das estratégias gerais (dia da semana, stopover, aeroportos vizinhos, moeda local, Skiplagged, tarifa erro).
- Preços vindos só do Explorar ainda não foram conferidos na data exata e aparecem "sem referência".

## Vigília da madrugada

Robô à parte ([vigilia.py](vigilia.py), workflow `vigilia.yml`) para flagrar promoção relâmpago e achar padrão.

- **O que mede:** uma cesta fixa de 38 voos (19 rotas nacionais e Buenos Aires e Santiago, sexta a segunda, a 4 e a 8 semanas de distância). As datas só mudam na virada da semana, para que o mesmo voo seja medido muitas vezes.
- **Quando:** por volta de 22h, 0h, 1h30, 3h, 4h30, 6h e 8h. O GitHub atrasa e às vezes pula agendamentos, então a hora registrada é a da medição real.
- **Resumo da manhã:** na primeira medição depois das 7h, compara cada voo com o preço do começo da noite. Queda de 12% ou mais (`queda_minima`) entra no resumo do ntfy, com a hora do menor preço e se ainda valia de manhã.
- **Padrão:** cada queda fica em `dados/quedas.csv` (noite, dia da semana, hora, rota, companhia, tamanho da queda, se persistiu). O painel mostra a tabela e as contagens por hora, dia e companhia. Só dá para falar em padrão depois de 2 a 4 semanas de noites medidas.
- **Ajuste:** a cesta e os limites ficam em `config.json`, bloco `vigilia`.

## Instalação (uns 20 minutos, uma vez só)

### 1. Bot do Telegram
1. No Telegram, abra uma conversa com **@BotFather**, envie `/newbot` e siga as perguntas. Guarde o token que ele devolve.
2. Abra a conversa com o seu bot novo e envie qualquer mensagem.
3. No navegador, abra `https://api.telegram.org/botSEU_TOKEN/getUpdates` e copie o número em `"chat":{"id": ...}`.

**Alternativa sem bot (ntfy):** se o BotFather recusar a criação do bot, instale o app **ntfy** no celular, toque em "+" e assine um tópico com nome longo e difícil de adivinhar (quem souber o nome consegue ler os alertas). Cadastre esse nome no GitHub como secret `NTFY_TOPICO`. O robô usa ntfy, Telegram ou os dois, conforme os secrets existentes.

**WhatsApp (CallMeBot):** serviço gratuito de terceiros que manda texto para o seu próprio número. Em 16/11/2026 o cadastro estava fechado ("bot is full"); o número do bot aparece em callmebot.com/blog/free-api-whatsapp-messages quando abrir vaga. Passos: salvar o número do bot nos contatos, enviar a ele pelo WhatsApp a frase `I allow callmebot to send me messages`, receber a chave e cadastrar os secrets `CALLMEBOT_TELEFONE` (com +55 e DDD) e `CALLMEBOT_CHAVE`. Seu telefone fica conhecido por esse serviço. Envio ainda não testado com chave real.

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
- `folga_imperdivel`: quanto abaixo do piso verde vira "imperdível". `desconto_alerta` só vale para rotas sem termômetro.
- `horizonte_dias` e `datas_por_rota`: até quando olhar e quantas datas por rota.
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
