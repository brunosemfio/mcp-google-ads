# Google Ads MCP Server

Servidor MCP (Model Context Protocol) que lê e edita contas do Google Ads pela Google Ads API REST (`v25`). As ferramentas de leitura têm os mesmos parâmetros do [googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp). As ferramentas de edição alteram status, orçamento, lances, estratégia de lance, palavras-chave, locais, programação de horários e URL final dos anúncios, criam e desvinculam assets (sitelinks, frases de destaque, snippets estruturados e chamadas) e clonam campanhas de pesquisa, sempre com uma prévia antes da alteração real.

## Ferramentas expostas

### Leitura

| Ferramenta | O que faz |
|---|---|
| `list_accessible_customers` | Lista os IDs das contas que a credencial acessa diretamente. Uma conta gerente (MCC) aparece aqui; as contas dela são consultadas com `search` no recurso `customer_client`. |
| `search` | Executa uma consulta GAQL (`searchStream`) montada a partir de `fields`, `resource`, `conditions`, `orderings` e `limit`. |
| `get_resource_metadata` | Lista os campos selecionáveis, filtráveis e ordenáveis de um recurso, incluindo as métricas e os segmentos compatíveis. |

### Edição

| Ferramenta | O que faz |
|---|---|
| `set_status` | Pausa ou ativa uma campanha, um grupo de anúncios, um anúncio ou uma palavra-chave. Anúncio e palavra-chave exigem `ad_group_id`. |
| `set_campaign_budget` | Altera o orçamento diário de uma campanha. Recusa orçamento compartilhado e orçamento que não seja diário. |
| `set_bid` | Altera o CPC máximo de um grupo de anúncios ou de uma palavra-chave (`keyword_id`). |
| `set_bidding_strategy` | Troca a estratégia de lance padrão (`MANUAL_CPC`, `MAXIMIZE_CLICKS`, `MAXIMIZE_CONVERSIONS`, `MAXIMIZE_CONVERSION_VALUE`) e define a meta de CPA (`target_cpa`) ou de ROAS (`target_roas`). Recusa estratégia de portfólio. |
| `add_keywords` | Adiciona palavras-chave a um grupo de anúncios (`ad_group_id`), palavras-chave negativas a uma campanha (`campaign_id`) ou palavras-chave a uma lista de palavras-chave negativas (`list_id`). |
| `remove_keywords` | Remove palavras-chave pelos `resource_names` que o `search` retorna (`adGroupCriteria`, `campaignCriteria` ou `sharedCriteria`), numa única chamada. |
| `create_negative_keyword_list` | Cria uma lista de palavras-chave negativas com as palavras iniciais, numa única chamada. |
| `link_negative_keyword_list` | Vincula uma lista de palavras-chave negativas a campanhas ou, com `unlink=true`, desvincula. |
| `clone_campaign` | Cria uma cópia pausada de uma campanha de pesquisa, numa única chamada. Ver [Clonar uma campanha](#clonar-uma-campanha). |
| `set_locations` | Substitui os locais segmentados de uma campanha por raios (`points`) e/ou IDs de local do Google (`location_ids`). Os locais excluídos (negativos) não mudam. |
| `set_ad_schedule` | Substitui a programação de horários de uma campanha, em horas inteiras no fuso da conta. Uma lista vazia remove a programação. |
| `set_ad_final_url` | Altera a URL final de um anúncio (`ad_id`). |
| `add_assets` | Cria sitelinks, frases de destaque, snippets estruturados e chamadas e vincula à conta (padrão), a uma campanha (`campaign_id`) ou a um grupo de anúncios (`ad_group_id`), numa única chamada. |
| `remove_assets` | Desvincula assets pelos `resource_names` que o `search` retorna (`customerAssets`, `campaignAssets` ou `adGroupAssets`), numa única chamada. O asset continua na biblioteca da conta. |

As ferramentas de leitura são anotadas com `readOnlyHint=true`. As ferramentas de edição são anotadas com `readOnlyHint=false` e `destructiveHint=true`. Com essas anotações, o Claude Code pede permissão a cada chamada de edição. Não inclua as ferramentas de edição na allowlist de permissões.

## Como a edição funciona

Toda ferramenta de edição segue estas etapas:

1. A ferramenta lê o estado atual do item no Google Ads.
2. Com `confirm=false` (padrão), a ferramenta envia a alteração com `validateOnly=true`. O Google valida a alteração e não aplica nada.
3. Com `confirm=true`, a ferramenta aplica a alteração.

As duas chamadas retornam o mesmo formato:

```json
{
  "applied": false,
  "before": {"campaign": "Campanha Exemplo", "daily_amount": 160.0},
  "after": {"campaign": "Campanha Exemplo", "daily_amount": 170.0},
  "currency": "USD",
  "warnings": []
}
```

Com `confirm=true`, o retorno tem `"applied": true` e a chave `resource_names` com os itens alterados ou criados.

As instruções do servidor orientam o cliente MCP a mostrar a prévia ao usuário e a chamar `confirm=true` somente depois da aprovação explícita do usuário.

Regras aplicadas pelas ferramentas:

- Valores monetários (`daily_amount`, `amount`, `target_cpa`) estão na moeda da conta (`customer.currency_code`), com no máximo duas casas decimais. A ferramenta converte o valor para micros. A prévia informa a moeda em `currency`.
- Um lote de palavras-chave é aplicado inteiro ou não é aplicado (`partialFailure` desativado).
- `add_keywords` ignora palavras-chave que já existem no destino e registra cada uma em `warnings`. A comparação ignora maiúsculas e minúsculas e considera o tipo de correspondência. O `validateOnly` do Google não detecta palavras-chave duplicadas.
- `add_keywords` com `campaign_id` ou `list_id` sempre cria palavras-chave negativas.
- `set_bid` retorna um aviso quando a estratégia da campanha não é `MANUAL_CPC`, porque o Google ignora o CPC máximo nas outras estratégias.
- `set_bidding_strategy` sem meta remove a meta existente. A prévia mostra a meta como `null` em `after`. A troca de estratégia ou de meta reinicia o período de aprendizado da campanha, e a prévia registra esse aviso.
- O Google rejeita estratégias que o tipo de campanha não aceita. A prévia mostra o erro do Google antes de qualquer alteração.
- `set_ad_schedule` descarta os ajustes de lance da programação atual e registra esse aviso.
- `add_assets` ignora assets com o mesmo conteúdo já vinculados no destino e registra cada um em `warnings`. O `validateOnly` do Google não detecta assets duplicados.
- Assets da conta aparecem em todas as campanhas. Assets de um tipo vinculados a uma campanha ou a um grupo de anúncios substituem, ali, os assets desse tipo dos níveis acima. `add_assets` registra esse aviso quando o destino ainda não tinha assets do tipo.
- Erros da API retornam a mensagem do Google e o `Request ID`.

### Clonar uma campanha

`clone_campaign` copia uma campanha de pesquisa para uma campanha nova, criada com status `PAUSED`. A ferramenta copia:

- o orçamento, como um orçamento novo não compartilhado;
- a estratégia de lance e a meta, as redes, a opção de local e as configurações de rastreamento;
- idiomas, locais, raios, programação de horários e palavras-chave negativas da campanha;
- os recursos vinculados à campanha (imagens, logotipo, sitelinks) e as listas de palavras-chave negativas vinculadas;
- os grupos de anúncios, com as palavras-chave e os anúncios responsivos de pesquisa. O nome da campanha de origem é substituído pelo nome novo nos nomes dos grupos.

A ferramenta não copia ajustes de lance por dispositivo, públicos, recursos de grupo de anúncios e anúncios de outros tipos. Cada anúncio ignorado aparece em `warnings`. A ferramenta recusa campanhas que não são de pesquisa e campanhas com estratégia de portfólio.

Para abrir a mesma campanha em outra cidade:

1. Clone a campanha com `clone_campaign`.
2. Troque os locais com `set_locations`.
3. Ajuste os horários ao fuso da cidade com `set_ad_schedule`.
4. Troque a URL final dos anúncios com `set_ad_final_url`.
5. Troque as palavras-chave com nome de cidade com `remove_keywords` e `add_keywords`.
6. Ajuste orçamento e meta com `set_campaign_budget` e `set_bidding_strategy`.
7. Ative a campanha com `set_status`.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/): baixa e executa o servidor. Se a máquina não tiver Python 3.10 ou mais novo, o `uv` baixa um sozinho.
- [git](https://git-scm.com/): o `uvx` usa o git para baixar o código deste repositório.
- [gcloud CLI](https://cloud.google.com/sdk/docs/install): necessário para habilitar a API pelo terminal e para gerar as credenciais.

**macOS**, com [Homebrew](https://brew.sh/):

```bash
brew install uv git
```

**Windows**, no PowerShell:

```powershell
winget install --id=astral-sh.uv -e
winget install --id=Git.Git -e
```

Depois de instalar, feche e abra o PowerShell de novo. Sem isso, o terminal não encontra os comandos `uv` e `git`.

## Configuração

### 1. Habilite a Google Ads API no Google Cloud

No projeto GCP que vai usar, habilite a **Google Ads API** pela [página da API no Console](https://console.cloud.google.com/apis/library/googleads.googleapis.com) ou pelo terminal:

```bash
gcloud services enable googleads.googleapis.com
```

### 2. Developer token

Obtenha o developer token na Central de API da conta gerente, conforme [Obtaining a Developer Token](https://developers.google.com/google-ads/api/docs/get-started/dev-token). O valor vai na variável `GOOGLE_ADS_DEVELOPER_TOKEN`.

O token precisa de um [nível de acesso](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) que permita contas de produção. Se o erro *"The developer token is only approved for use with test accounts"* aparecer, o token só acessa contas de teste.

### 3. Credenciais (Application Default Credentials)

As credenciais precisam do escopo `https://www.googleapis.com/auth/adwords`. O usuário Google ou a service account precisa ter acesso **Padrão** ou **Administrador** na conta gerente para usar as ferramentas de edição. Com acesso **Somente leitura**, as ferramentas de leitura funcionam e as de edição falham.

**Opção A — Credenciais de usuário com um cliente OAuth próprio:**

1. Crie um cliente OAuth do tipo "App para computador" no projeto GCP, conforme [Manage OAuth Clients](https://support.google.com/cloud/answer/15549257), e baixe o JSON do cliente.
2. Gere as credenciais:

   ```bash
   gcloud auth application-default login "--scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform" --client-id-file=/caminho/cliente-oauth.json
   ```

3. Anote o caminho que o gcloud mostra em `Credentials saved to file: [...]`. O caminho vai na variável `GOOGLE_APPLICATION_CREDENTIALS`.

**Opção B — Impersonação de service account:**

```bash
gcloud auth application-default login --impersonate-service-account=SERVICE_ACCOUNT_EMAIL "--scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform"
```

Nessa opção, a service account precisa estar cadastrada como usuário na conta gerente do Google Ads.

As aspas protegem as vírgulas: sem elas, o PowerShell quebra a lista de escopos em vários argumentos.

### 4. Conta gerente (login-customer-id)

Se o acesso às contas passa por uma conta gerente (MCC), defina `GOOGLE_ADS_LOGIN_CUSTOMER_ID` com o ID da conta gerente, só com dígitos. As ferramentas de leitura também aceitam o parâmetro `login_customer_id`, que tem prioridade sobre a variável.

### 5. Arquivo `.env` (opcional)

Para não repetir os valores em cada cliente MCP, grave as variáveis num arquivo `.env`. O servidor lê o primeiro arquivo que encontrar:

1. `$GOOGLE_ADS_ENV_FILE`, se definido;
2. `~/.config/google-ads/.env`;
3. `.env` no diretório de trabalho.

No macOS, no Terminal:

```bash
mkdir -p ~/.config/google-ads
cat > ~/.config/google-ads/.env <<'ENV'
GOOGLE_APPLICATION_CREDENTIALS=/Users/voce/.config/gcloud/application_default_credentials.json
GOOGLE_ADS_DEVELOPER_TOKEN=SEU_DEVELOPER_TOKEN
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890
ENV
chmod 600 ~/.config/google-ads/.env
```

No Windows, no PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\google-ads" | Out-Null
@"
GOOGLE_APPLICATION_CREDENTIALS=C:\Users\voce\AppData\Roaming\gcloud\application_default_credentials.json
GOOGLE_ADS_DEVELOPER_TOKEN=SEU_DEVELOPER_TOKEN
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890
"@ | Set-Content -Encoding utf8 "$HOME\.config\google-ads\.env"
```

Variáveis já definidas no ambiente, inclusive pelo `--env` ou pelo `env` do cliente MCP, têm prioridade sobre o arquivo. O servidor lê o arquivo uma vez por processo, e a ausência do arquivo não é erro. O arquivo aceita `export ` no começo da linha, comentários com `#` e valores entre aspas. Escreva o caminho completo das credenciais: o servidor não expande o `~` em `GOOGLE_APPLICATION_CREDENTIALS`.

## Instalação e uso

### Direto do GitHub (recomendado)

Para testar, rode o comando abaixo, que é igual no macOS e no Windows:

```bash
uvx --from git+https://github.com/brunosemfio/mcp-google-ads.git google-ads-mcp
```

Se o servidor subir sem erro, ele fica aguardando um cliente MCP. Encerre com Ctrl+C.

#### Claude Code

1. Registre o servidor. O `--scope user` deixa o servidor disponível em qualquer pasta.

   **macOS**, no Terminal:

   ```bash
   claude mcp add --scope user google-ads \
     --env GOOGLE_APPLICATION_CREDENTIALS=/Users/voce/.config/gcloud/application_default_credentials.json \
     --env GOOGLE_ADS_DEVELOPER_TOKEN=SEU_DEVELOPER_TOKEN \
     --env GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890 \
     -- uvx --from git+https://github.com/brunosemfio/mcp-google-ads.git google-ads-mcp
   ```

   **Windows**, no PowerShell:

   ```powershell
   claude mcp add --scope user google-ads `
     --env "GOOGLE_APPLICATION_CREDENTIALS=C:\Users\voce\AppData\Roaming\gcloud\application_default_credentials.json" `
     --env "GOOGLE_ADS_DEVELOPER_TOKEN=SEU_DEVELOPER_TOKEN" `
     --env "GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890" `
     -- uvx --from git+https://github.com/brunosemfio/mcp-google-ads.git google-ads-mcp
   ```

   - Troque os valores de `--env` pelo caminho das suas credenciais, pelo seu developer token e pelo ID da sua conta gerente.
   - Sem conta gerente, remova a linha do `GOOGLE_ADS_LOGIN_CUSTOMER_ID`.
   - Com o [arquivo `.env`](#5-arquivo-env-opcional), remova as linhas de `--env`.

2. Rode `claude mcp list`. O `google-ads` deve aparecer como conectado.

3. Numa conversa nova do Claude Code, peça "liste as contas do Google Ads que você acessa". O Claude chama `list_accessible_customers` e mostra as contas. Se der erro, digite `/mcp` dentro do Claude Code para ver o status do servidor.

Para trocar algum valor, remova o servidor com `claude mcp remove --scope user google-ads` e registre de novo pelo passo 1.

#### Claude Desktop

Adicione a entrada abaixo em `mcpServers` no arquivo `claude_desktop_config.json` (**Settings → Developer → Edit Config**). Use o caminho completo do `uvx`, obtido com `which uvx` no macOS ou `where.exe uvx` no Windows.

```json
{
  "mcpServers": {
    "google-ads": {
      "command": "/opt/homebrew/bin/uvx",
      "args": [
        "--from",
        "git+https://github.com/brunosemfio/mcp-google-ads.git",
        "google-ads-mcp"
      ],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/Users/voce/.config/gcloud/application_default_credentials.json",
        "GOOGLE_ADS_DEVELOPER_TOKEN": "SEU_DEVELOPER_TOKEN",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "1234567890"
      }
    }
  }
}
```

No Windows, dobre as barras invertidas nos caminhos, como o JSON exige. Com o [arquivo `.env`](#5-arquivo-env-opcional), remova o bloco `env` inteiro e a vírgula que sobrar no fim do `]` de `args`. Depois de salvar, encerre o Claude Desktop (Cmd+Q no macOS; Sair no ícone da bandeja no Windows) e abra de novo.

O `uvx` faz cache do build: para atualizar após novos commits, rode o comando uma vez com `--refresh`. Para fixar uma versão, aponte para uma tag ou commit: `git+https://...@<tag-ou-sha>`.

### A partir de um clone local

```bash
git clone https://github.com/brunosemfio/mcp-google-ads.git
cd mcp-google-ads
uv sync
uv run google-ads-mcp
```

Para registrar no cliente MCP, use como comando o executável que o `uv sync` cria dentro do clone:

- macOS: `/caminho/do/clone/.venv/bin/google-ads-mcp`
- Windows: `C:\caminho\do\clone\.venv\Scripts\google-ads-mcp.exe`

### Migração do googleads/google-ads-mcp

O servidor usa as mesmas variáveis de ambiente do MCP oficial. A variável `GOOGLE_PROJECT_ID` não é usada. Para migrar, remova o registro antigo com `claude mcp remove --scope user google-ads` e registre este servidor pelo passo 1 de [Claude Code](#claude-code).

Diferenças em relação ao MCP oficial:

- As ferramentas não têm prefixo de namespace: `search_search` passa a ser `search`, `customers_list_accessible_customers` passa a ser `list_accessible_customers` e `metadata_get_resource_metadata` passa a ser `get_resource_metadata`.
- Em `search`, valores int64 (IDs, micros, cliques) retornam como string, por exemplo `"4500000"`. Campos sem valor retornam `null`.
- A descrição de `search` não traz a lista de recursos válidos. Use `get_resource_metadata` para descobrir os campos.
- O servidor só aceita transporte stdio.

## Desenvolvimento

```bash
uv sync --extra dev
uv run pytest
uv run ruff check google_ads_mcp tests
uv run mypy google_ads_mcp
```

Os testes de integração acessam a API real e pulam se `GOOGLE_ADS_DEVELOPER_TOKEN` ou `GOOGLE_ADS_TEST_CUSTOMER_ID` não estiverem definidas. `GOOGLE_ADS_TEST_CUSTOMER_ID` é a conta usada nas consultas e nas prévias:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/caminho/credenciais.json \
GOOGLE_ADS_DEVELOPER_TOKEN=SEU_DEVELOPER_TOKEN \
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890 \
GOOGLE_ADS_TEST_CUSTOMER_ID=1112223333 \
uv run pytest -m integration
```

Os testes de integração executam cada ferramenta de edição somente com `validateOnly=true`. Uma fixture falha o teste se alguma chamada tentar aplicar uma alteração. Nenhum teste altera a conta.

O CI (GitHub Actions, branch `main`) roda ruff, mypy e os testes unitários com cobertura mínima de 65% em Python 3.10 e 3.12.

### Atualizar a versão da API

A versão da API fica na constante `API_VERSION` em `google_ads_mcp/api.py`. O Google desativa cada versão da Google Ads API cerca de um ano após o lançamento ([calendário de versões](https://developers.google.com/google-ads/api/docs/sunset-dates)). Para atualizar, troque o valor da constante e rode os testes de integração.

## Exemplos de pedidos

- "Quanto cada campanha da conta Y gastou em setembro e quantas conversões trouxe?"
- "Pause a campanha X da conta Y."
- "Aumente o orçamento diário da campanha X para R$ 80."
- "Troque a estratégia da campanha X para maximizar conversões com CPA desejado de R$ 25."
- "Adicione 'grátis' e 'emprego' como negativas de frase na campanha X."
- "Crie uma lista de negativas chamada 'Geral' com estas palavras e vincule às campanhas de pesquisa da conta Y."
- "Clone a campanha X para Houston, com raio de 20 km no centro da cidade."
- "Crie as frases de destaque 'Free Delivery' e '24/7 Support' na conta Y."
