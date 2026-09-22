# Deploy no Dokploy (Docker Compose)

Passo a passo para colocar o extrator no ar na VPS Hetzner. O porquê de cada escolha está em [`decisao-armazenamento.md`](decisao-armazenamento.md). O que sobe (tudo em `docker-compose.yml`):

```
Internet ─HTTPS─> Traefik (Dokploy) ─dokploy-network─> faturas-app :8000
                                                          │
                          rede "interno" (internal: true) │
   faturas-worker ──────────────┬─────────────────────────┤
                                ▼                         ▼
                    faturas-db :5432            faturas-storage :9000 (S3) / :9001 (console)
   one-shots a cada deploy: faturas-storage-init (bucket + usuário S3)  e  faturas-migrar (python -m banco.migrar)
```

Nenhum serviço publica porta no host. Só a UI sai, e sai pelo domínio.

## 0. Pré-requisitos

- [ ] A camada de persistência já está no repositório: `banco/migrar.py`, `worker.py` e as dependências no `requirements.txt` (`sqlalchemy`, `psycopg[binary]`, `boto3`). Sem isso, `faturas-migrar` falha e **nada sobe**, e esse é o comportamento esperado.
- [ ] Um subdomínio (ex.: `faturas.<seu-domínio>`) com registro **A** apontando para o IP da VPS.
- [ ] Dokploy atualizado (testado contra a documentação e o código da v0.30.x).
- [ ] Se builds de outros projetos na VPS já funcionam, o DNS do Docker está ok. Se o build travar em `apt-get`/`pip` com "could not resolve host", é o problema conhecido dos resolvers da Hetzner. A correção é adicionar `"dns": ["1.1.1.1", "8.8.8.8"]` em `/etc/docker/daemon.json` e reiniciar o Docker ([doc do Dokploy](https://docs.dokploy.com/docs/core/troubleshooting/networking)). Reiniciar o Docker reinicia todos os projetos da VPS.

## 1. Gerar os segredos (na sua máquina)

```powershell
py -c "import secrets; [print(n, secrets.token_hex(24)) for n in ('POSTGRES_PASSWORD','STORAGE_ROOT_PASSWORD','S3_SECRET_KEY')]"
```

Hex puro (`0-9a-f`) é de propósito: a senha do Postgres entra no meio do `DATABASE_URL`, e `@ : / ? #` quebrariam a URL. Guarde os três num gerenciador de senhas.

## 2. Criar o projeto e o serviço

1. Dokploy → **Projects → Create Project**, com nome `Faturas`.
2. Dentro do projeto: **Create Service → Compose**.
   - Compose Type: **Docker Compose**, não *Stack*. O modo Stack não suporta `build:`.
   - Provider: GitHub (o mesmo acesso já usado no FindBus), repositório `MatheusBraga1106/ProjetoAutoma--oFatura`, branch `main`.
   - Compose Path: `./docker-compose.yml`.
3. **Não** ative *Isolated Deployments* nem *Randomize*. O compose já isola banco e storage na rede `interno`, e os nomes `faturas-*` evitam colisão de DNS na `dokploy-network`.

## 3. Variáveis de ambiente

Aba **Environment**: copie o `.env.example` inteiro e preencha com os valores reais:

```
POSTGRES_DB=faturas
POSTGRES_USER=faturas
POSTGRES_PASSWORD=<hex do passo 1>
STORAGE_ROOT_USER=faturas-admin
STORAGE_ROOT_PASSWORD=<hex do passo 1>
S3_ACCESS_KEY=faturas-app
S3_SECRET_KEY=<hex do passo 1>
S3_BUCKET=faturas-agua
S3_REGION=us-east-1
PORT=8000
TESSERACT_CMD=
```

`DATABASE_URL`, `S3_ENDPOINT_URL` e `CONTAS_JSON_PATH` **não** vão aqui. O compose monta esses três. Se faltar alguma obrigatória, o deploy falha com `required variable X is missing a value: defina X`.

> O Postgres só lê `POSTGRES_USER`/`POSTGRES_PASSWORD` na **primeira** inicialização do volume. Se trocar a senha depois, rode também, dentro do banco, `ALTER USER faturas PASSWORD '...'`. Senão a app para de conectar.

## 4. Montar o `contas.json` (antes do primeiro deploy)

1. Na sua máquina, confira se o JSON é válido e copie para a área de transferência:
   ```powershell
   py -c "import json; d=json.load(open('contas.json',encoding='utf-8')); print(len(d),'contas ok')"
   Get-Content contas.json -Raw -Encoding UTF8 | Set-Clipboard
   ```
2. No serviço Compose: **Advanced → Mounts → Add Mount → File Mount**.
   - File Path: `contas.json`
   - Content: cole o conteúdo.
3. Salve. O arquivo passa a existir em `/etc/dokploy/compose/<appName>/files/contas.json`, que é o `../files/contas.json` que o compose monta read-only em `/app/contas.json`.

**Se pular este passo**, o Docker cria um *diretório* vazio chamado `contas.json` e o enriquecimento quebra. Para consertar: apague o diretório no host (`rm -r /etc/dokploy/compose/<appName>/files/contas.json`), crie o File Mount e faça redeploy.

## 5. Domínio

Aba **Domains → Add Domain**:

| Campo | Valor |
|---|---|
| Service Name | `faturas-app` |
| Host | `faturas.<seu-domínio>` |
| Path | `/` |
| Container Port | `8000` |
| HTTPS | ligado, Certificate: Let's Encrypt |

O Dokploy injeta as labels do Traefik e a `traefik.docker.network=dokploy-network` nesse serviço. Com **Preview Compose** dá para ver o arquivo final antes de subir. Não adicione domínio para nenhum outro serviço.

## 6. Primeiro deploy

Clique em **Deploy** e acompanhe os logs. A sequência esperada:

1. Build da imagem. Nos logs precisa aparecer a lista de idiomas do Tesseract com **`por`** (o build **falha** se não tiver) e a versão do `pdftotext`.
2. `faturas-db` e `faturas-storage` ficam *healthy*.
3. `faturas-storage-init` termina com `storage pronto: bucket faturas-agua, usuario faturas-app` e **sai com código 0**. Na tela, ele aparece como *exited*, e isso é normal.
4. `faturas-migrar` roda `python -m banco.migrar` e **sai com código 0** (também aparece *exited*).
5. `faturas-app` e `faturas-worker` sobem e ficam *healthy* em até ~1 min.

Se o passo 3 ou o 4 falhar, app e worker não sobem. Veja o log do one-shot que falhou.

## 7. Verificação pós-deploy

Pela sua máquina:

```bash
curl -fsS https://faturas.<seu-domínio>/health
# {"status":"ok"}

curl -fsS https://faturas.<seu-domínio>/pipeline/status-sistema
# esperar: contas.json presente, tesseract_resolvido=true, pdftotext_resolvido=true
```

**Teste de OCR de ponta a ponta.** `POST /extrair` extrai e devolve JSON sem persistir. Use uma fatura que **só funciona por OCR** (as da BURITI ALEGRE são escaneadas, com ~5 s por página):

```bash
curl -fsS -X POST https://faturas.<seu-domínio>/extrair \
  -F "arquivos=@FATURA Nº 185278 - BURITI ALEGRE AMBIENTAL - JANEIRO.2025.pdf"
# esperar status "ok" com valor_total preenchido; "erro" = OCR/extrator com problema
```

Na VPS (SSH), onde `<app>` é o *App Name* mostrado no Dokploy:

```bash
docker ps --filter "label=com.docker.compose.project=<app>" --format "table {{.Names}}\t{{.Status}}"

# Tesseract com português, dentro do worker (roda como usuário não-root)
docker exec <app>-faturas-worker-1 tesseract --list-langs
docker exec <app>-faturas-worker-1 id            # uid=10001(app)

# Postgres: tabelas criadas pela migração
docker exec <app>-faturas-db-1 psql -U faturas -d faturas -c '\dt'

# Storage: a credencial restrita enxerga o bucket
docker exec <app>-faturas-storage-1 sh -c \
  'mcli alias set app http://127.0.0.1:9000 faturas-app "<S3_SECRET_KEY>" >/dev/null && mcli ls app/faturas-agua'

# Nada publicado no host (a coluna PORTS deve mostrar só portas internas, sem 0.0.0.0:)
docker ps --filter "label=com.docker.compose.project=<app>" --format "{{.Names}} {{.Ports}}"
```

**Console web do storage (opcional).** Ele não tem porta no host de propósito. Quando precisar, suba um túnel temporário e acesse por SSH:

```bash
# na VPS
docker run --rm -d --name console-tunel --network <app>_interno -p 127.0.0.1:9001:9001 \
  alpine/socat tcp-listen:9001,fork,reuseaddr tcp-connect:faturas-storage:9001
# na sua máquina
ssh -L 9001:127.0.0.1:9001 root@<ip-da-vps>     # abrir http://localhost:9001 (login = STORAGE_ROOT_*)
# ao terminar, na VPS
docker stop console-tunel
```

## 8. Backups (fazer no mesmo dia do primeiro deploy)

1. **Cloudflare R2**: crie o bucket `faturas-backups` e um *API token* com permissão **Object Read & Write só nesse bucket**. Anote o endpoint `https://<account-id>.r2.cloudflarestorage.com`.
2. Dokploy → **Settings → Destinations → Add**: provider Cloudflare (ou S3 genérico), endpoint acima, região `auto`, bucket `faturas-backups`, access/secret do token. Use **Test** para validar.
3. Serviço Compose → **Backups → Create Backup** (tipo banco):
   - Service Name `faturas-db`, Database Type **Postgres**, Database Name `faturas`, Database User `faturas`
   - Destination `faturas-backups`, Prefix `postgres/`, cron `0 3 * * *`, Keep latest **14**
   - Clique **Test** e confira se apareceu um `.sql.gz` no R2.
4. **Volume Backups → Create**: volume `<app>_faturas-storage-dados`, destination `faturas-backups`, prefix `storage/`, cron `0 4 * * 0` (domingo), keep latest **4**, **Turn off container during backup: ligado**.
5. **`contas.json`**: não entra nos backups acima (é bind mount). Guarde uma cópia cifrada fora da VPS sempre que editar o arquivo.

**Teste de restore do banco** (uma vez, antes de apagar qualquer coisa local):

```bash
docker exec <app>-faturas-db-1 psql -U faturas -d faturas -c 'CREATE DATABASE faturas_restore_teste'
```

Depois, na aba **Backups → Restore**, escolha o arquivo do R2 e use como Database Name `faturas_restore_teste`. Por fim, compare as contagens e apague o banco de teste:

```bash
docker exec <app>-faturas-db-1 psql -U faturas -d faturas_restore_teste -c '\dt+'
docker exec <app>-faturas-db-1 psql -U faturas -d faturas -c 'DROP DATABASE faturas_restore_teste'
```

## 9. Reextração completa (apagar a saída atual e refazer)

> O comando exato para enfileirar depende da interface que a camada de persistência e o worker expuserem (upload pela UI gravando no S3 + tabela `jobs`, ou um comando de importação em lote). Ajuste o item 3 quando isso estiver pronto.

1. **Ponto de partida limpo**: banco recém-migrado, sem faturas. Se já houver faturas de teste, ou apague o volume antes do primeiro uso real, ou use o mecanismo de versão da camada de persistência. Não reaproveite os `.txt` locais: a ideia é regerar tudo (pdftotext + OCR) no servidor.
2. **Mantenha as cópias locais** (`dados_entrada/`, `dados_saida/`, `dados_saida_backup_*`) até o passo 5.
3. **Enviar os PDFs** (1.117 arquivos, 344 MB). O caminho mais simples é a aba *Processar* → selecionar a pasta `dados_entrada/` inteira. O upload por pasta preserva o caminho relativo, e é dele que `identificar_distribuidora` depende (a DEMAE, por exemplo, só é reconhecida pelo nome da pasta).
4. **Acompanhar**: `docker logs -f <app>-faturas-worker-1`. Estimativa: 657 páginas de OCR a ~5 s cada (medido num Ryzen 5 5600X), algo como **1–2 h** na VPS. O resto (pdftotext + regex) leva minutos. O worker tem teto de 4 CPUs e 4 GB, então os outros projetos continuam respondendo.
5. **Conferir contra o baseline local** antes de dar como pronto: cerca de 10,1 mil faturas no total (9.593 da SANEAGO) e 63 suspeitos (40 IPAMERI, 21 SANEAGO, 1 SAAE_CORUMBA, 1 SAE), conforme `PROXIMOS_PASSOS.md`. Diferença grande indica OCR diferente entre Windows e Linux (Tesseract 5.5 na imagem) ou falha de identificação por pasta.
6. Só depois de 5 **e** do teste de restore (seção 8): apagar as saídas locais.

## 10. Deploys seguintes e manutenção

- **Push na `main`** dispara um novo deploy (se *Autodeploy* estiver ligado), que faz rebuild e roda `faturas-storage-init` e `faturas-migrar` de novo. Os dois são idempotentes. Volumes e o File Mount continuam.
- **Atualizar o Silo**: troque a tag `pgsty/silo:RELEASE...` nos dois serviços (`faturas-storage` e `faturas-storage-init`) depois de ler as notas de release. Nunca use `latest`.
- **Trocar a senha do storage da app**: mude `S3_SECRET_KEY` e faça redeploy. O `faturas-storage-init` atualiza o usuário.
- **Editar `contas.json`**: edite o File Mount e reinicie `faturas-app` e `faturas-worker` (ou faça redeploy).
- **Cuidado ao apagar o serviço Compose no Dokploy**: se marcar a remoção de volumes, banco e PDFs vão junto. Confira antes se há backup recente no R2.

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `network dokploy-network declared as external, but could not be found` | Deploy fora de um host Dokploy. Na VPS essa rede sempre existe |
| App e worker não sobem, `faturas-migrar` com exit ≠ 0 | Erro de migração ou `DATABASE_URL`. Veja o log do `faturas-migrar` |
| `faturas-storage-init` falha em `alias set` | `STORAGE_ROOT_*` diferente do usado na **primeira** subida do volume do storage |
| Enriquecimento vazio / erro lendo `contas.json` | File Mount não criado, e o Docker criou um diretório (ver seção 4) |
| Worker *unhealthy* | Postgres fora do ar, ou o Tesseract perdeu o `por` (imagem errada). Veja o `docker inspect` do healthcheck |
| 404 / certificado inválido no domínio | Registro A ainda não propagou, ou domínio ligado a outro serviço que não o `faturas-app` |
