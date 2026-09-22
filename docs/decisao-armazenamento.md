# Decisão: onde guardar os arquivos e os dados tratados

Data: 2026-09-22. Vale para o extrator de faturas de água (este repositório) rodando no Dokploy da VPS Hetzner compartilhada (8 vCPU / 16 GB / 160 GB, já com FindBus, neon-balance-glow e Toca). Fase de teste: o objetivo é funcionar e ser recuperável, não otimizar capacidade.

## Resumo

| O quê | Onde | Serviço no `docker-compose.yml` |
|---|---|---|
| **Arquivos**: PDFs originais + `.txt` derivados (pdftotext/OCR) | Object storage S3-compatível self-hosted na própria VPS: **Silo**, fork mantido do MinIO, fixado por tag | `faturas-storage` (volume `faturas-storage-dados`) |
| **Dados tratados**: faturas extraídas, jobs, erros reportados | **Postgres 17 dedicado**, dentro do mesmo Compose. Separado do Postgres do neon-balance-glow | `faturas-db` (volume `faturas-db-dados`) |
| **Cadastro `contas.json`** | File Mount do Dokploy (fica no host, fora do git e da imagem), montado read-only em `/app/contas.json` | montado em app/worker/migração |
| **Backups** | Fora da VPS: bucket dedicado no Cloudflare R2, usado como *Destination* do Dokploy | Dokploy: backup de banco (compose) e Volume Backups |

Fica mantido o que o Hub (`hub-faturas`) já tinha decidido: S3 para os PDFs, Postgres com colunas fixas mais `jsonb`, fila como tabela `jobs` com worker por polling, nenhuma porta pública, `restart` e healthcheck em tudo, Postgres próprio. **Uma coisa mudou, e a razão é concreta: a imagem do MinIO.** Detalhes abaixo.

## Volume medido (2026-09-22, máquina local)

| Item | Quantidade | Tamanho |
|---|---|---|
| `dados_entrada/` inteiro | 2.242 arquivos | **390,4 MB** |
| PDFs | 1.117 arquivos, 4.901 páginas (máx. 64 págs./PDF) | **344,2 MB** (média 316 KB, maior 26,7 MB) |
| `.txt` já gerados | 1.119 (197 vieram de OCR, somando 657 páginas) | 27,0 MB (média 25 KB, maior 308 KB) |
| Por pasta | Outras Distribuidoras 161,4 MB · SANEAGO Borderô 129,7 MB · SANEAGO Analítica 81,2 MB | |
| `dados_saida/` (8 CSVs) | cerca de 10,1 mil linhas (9.593 da SANEAGO) | 5,3 MB |
| `contas.json` | 178 contas | 158 KB |
| OCR medido localmente (Ryzen 5 5600X, 400 DPI, psm 4, `por`) | 3 faturas de 1 página | **cerca de 5 s por página** |

O que isso significa para o dimensionamento:

- **Storage**: 0,4 GB hoje. Mesmo com 10 vezes mais histórico, dá 4 GB, cerca de 2,5% do disco. Não precisa de Hetzner Volume separado agora.
- **Postgres**: cerca de 10 mil faturas × aproximadamente 1–2 KB (colunas fixas + `jsonb` + índices) dá 20–40 MB por extração completa. Se cada reprocessamento guardar uma versão nova (o modelo do Hub incrementa `versao`), cada reextração total soma mais ~30 MB. Irrelevante para 160 GB.
- **CPU (a única conta que importa)**: são 657 páginas de OCR a ~5 s cada, ou seja, ~55 min num núcleo desta máquina. Numa vCPU compartilhada da Hetzner, conte com 1–2 h para a reextração completa. O worker tem limite de 4 CPUs e 4 GB no compose. Isso não é tuning: é para o OCR não sufocar os outros projetos da VPS.

## Arquivos: alternativas consideradas

| Opção | Prós | Contras | Veredito |
|---|---|---|---|
| **MinIO oficial (`minio/minio`)**, a decisão original do Hub | API S3, console, `mc` | **Não existe mais como imagem utilizável.** Binários e imagens pararam em out/2025, o repositório entrou em modo manutenção em dez/2025 e foi arquivado em 2026. Em 2026-09-22 a API do Docker Hub responde `object not found` para `minio/minio` (conferido nesta sessão). Sobram tags antigas no Quay, sem correção de segurança | **Descartado.** `hub-faturas/docs/minio-dokploy.md` manda usar `minio/minio`, e esse passo vai falhar |
| **Silo (`pgsty/silo`)**, fork do MinIO pela Pigsty | Troca direta: mesmas variáveis `MINIO_*`, rotas `/minio/*`, formato de dados, `mcli` e `curl` na imagem, console restaurado. Release ativo (`RELEASE.2026-09-16T00-00-00Z`). AGPL, sem problema para uso interno | Mantido por uma organização só. Foi renomeado de `pgsty/minio` para `pgsty/silo` em 2026-08-06, o que dá uma ideia de quanto ainda muda | **Escolhido**, com tag fixada. O código só conhece S3 (`S3_ENDPOINT_URL`), então trocar é mudar variável e copiar objetos |
| Garage (`dxflrs/garage`) / RustFS / SeaweedFS | Self-hosted e ativos | Garage exige layout de cluster e criação de chave por CLI, e não tem console. RustFS saiu no 1.0.0 em 2026-09-16 (novo demais). SeaweedFS é mais pesado do que o necessário | Plano B se o Silo parar |
| **Cloudflare R2** | Egress zero, 10 GB grátis/mês, nada para operar, já fica fora da VPS | As faturas de unidades públicas passam a ficar com terceiro. Depende de internet a cada leitura do worker. O argumento do egress aqui é fraco: o worker lê os PDFs na mesma VPS | **Usado como destino de backup**, não como primário |
| Hetzner Object Storage | Gerenciado, mesmo provedor, S3 | Cobra uma taxa base mensal fixa (com 1 TB incluso) para guardar 0,4 GB | Caminho de upgrade se operar storage virar peso |
| Volume local (pasta no container) | O mais simples de todos | App e worker são containers separados. Não segue o contrato S3 do Hub, não tem URL assinada e não tem credencial restrita | Descartado |

### Como organizar o bucket (recomendação para a camada de persistência)

- Chave por conteúdo: `pdfs/<sha256>.pdf`. Isso dá dedupe natural e evita os problemas que os nomes atuais causam: acento, `Nº`, caminhos maiores que 260 caracteres.
- **O caminho relativo original (pasta + nome) vai para o banco, obrigatoriamente.** `identificar_distribuidora` usa o nome da *pasta* para reconhecer várias distribuidoras (ex.: DEMAE). Se só a chave sha256 for guardada, essa informação se perde.
- Texto derivado em `textos/<sha256>.txt`, com o método (`pdftotext`/`ocr`) registrado no banco. É cache do passo caro (OCR): reprocessar a regex não precisa refazer OCR. Na reextração "do zero", ignore esse cache.
- A credencial da app só enxerga o bucket `S3_BUCKET` (política criada pelo `faturas-storage-init`: listar, ler, gravar, apagar e multipart; **sem** criar bucket). Se o código copiar o `garantir_bucket()` do Hub, ele funciona (há `s3:ListAllMyBuckets`), mas o ideal é `head_bucket`. O bucket já existe quando a app sobe.

## Dados tratados: alternativas consideradas

| Opção | Veredito |
|---|---|
| CSV em disco (o que existe hoje) | Descartado: sem transação, sem índice, sem escrita concorrente de app e worker, e perde tudo no redeploy |
| SQLite em volume | Descartado: um escritor só, com dois containers disputando o arquivo. Destoa do contrato (`DATABASE_URL` Postgres) e do Hub |
| Postgres compartilhado com o neon-balance-glow | Descartado (já decidido): um erro num projeto derruba o outro, e as senhas e backups se misturam |
| **Postgres como serviço "Database" separado do Dokploy** | Viável. Tem UI própria de backup e restore e dá para ativar porta externa temporária. Contra: fica fora do arquivo versionado da stack, com um projeto a mais para lembrar, e a app precisa entrar na `dokploy-network` para alcançá-lo |
| **Postgres dentro do Compose** | **Escolhido.** A stack inteira fica num arquivo só versionado no git. O banco fica numa rede `internal: true`, que nenhum outro projeto alcança. O Dokploy (v0.30.x) **também faz backup e restore de banco dentro de Compose**: no código, `backupType: "compose"`, `serviceName` e `keepLatestCount` |

Modelo recomendado (quem implementa é a camada de persistência, não este documento): colunas fixas e indexáveis com o que filtra e deduplica hoje (`concessionaria`, `num_fatura`, `mes_ano_ref`, `conta_dv` normalizada, `valor_total`, `suspeito`, `versao`, `arquivo_id`, `job_id`), mais `dados jsonb` com o resto das colunas do extrator. A chave anti-duplicata atual (`NUM_FATURA|MES_ANO_REF|CONTA_DV` em `salvar_incremental`) vira um índice único, somando a versão.

## Backup

O que o Dokploy oferece (conferido na documentação e no código-fonte, v0.30.7 de 2026-09-18):

1. **Backup de banco para S3**: aba *Backups* do serviço. Pede *Destination* (bucket S3 cadastrado em Settings → Destinations), nome do banco, cron, prefixo e retenção. A retenção é o campo "keep latest" (`keepLatestCount`), e há um botão *Test*. Para Postgres em Compose, informe o *service name* (`faturas-db`) e o usuário do banco. O Dokploy roda `pg_dump -Fc --no-acl --no-owner -h localhost -U <user> <db> | gzip` via `docker exec` no container e sobe com rclone. O restore sai da mesma aba (botão *Restore*, escolhendo o arquivo no bucket).
2. **Volume Backups**: backup de *named volume* (não serve para bind mount / `../files`) para S3, com cron e opção de desligar o container durante a cópia. Funciona com Compose. O nome do volume segue `<appName>_<volume>`.

Plano:

| O quê | Mecanismo | Quando | Retenção | Tamanho estimado |
|---|---|---|---|---|
| Postgres `faturas` | Backup de banco (compose, `faturas-db`) | diário 03:00 | keep latest 14 | poucos MB por dump |
| Volume `faturas-storage-dados` (PDFs + textos) | Volume Backup, **com** "turn off container" (consistência) | semanal, domingo 04:00 | keep latest 4 | ~0,4 GB cada |
| `contas.json` | **Não coberto** (é bind mount). Guardar uma cópia cifrada fora da VPS, e sempre que editar | manual | — | 158 KB |

Destino: um bucket só para backup no **Cloudflare R2** (`faturas-backups`), com token de API restrito a esse bucket. São ~1,7 GB no total, dentro dos 10 GB grátis. Guardar o backup no próprio Silo da VPS não serve: se a VPS cair, backup e original vão juntos.

**Um backup nunca restaurado não conta como backup.** O `deploy-dokploy.md` tem o teste de restore, e ele precisa rodar uma vez antes de apagar as cópias locais.

## Retenção dos dados

- **PDFs originais**: guardar sem prazo nesta fase. São a fonte de verdade, e é por eles que dá para reprocessar quando uma regex mudar. Se houver regra de temporalidade documental do órgão para faturas, quem define é o dono do dado. Aí basta uma regra de expiração no bucket.
- **Textos derivados**: descartáveis, regeneráveis a partir do PDF.
- **Faturas extraídas**: guardar todas as versões durante o teste. Mais tarde, manter só a última e as revisadas manualmente.
- **Jobs**: as linhas de jobs concluídos podem ser apagadas depois de 90 dias. Não é urgente com esse volume.
- **Dados locais atuais** (`dados_saida/`, `dados_saida_backup_*`, `.txt` locais): apagar **só depois** de a reextração no servidor bater com os números atuais (ex.: 63 suspeitos, cerca de 10,1 mil faturas) e de um restore de teste ter funcionado.

## Como o `contas.json` chega em produção sem ir para o git

- Ele continua no `.gitignore` e no `.dockerignore`, então não entra na imagem.
- No Dokploy: **Advanced → Mounts → File Mount**, com caminho `contas.json` e o conteúdo colado. O arquivo fica no host em `/etc/dokploy/compose/<appName>/files/contas.json`, sobrevive a redeploy e ao `git clone` que o Dokploy faz a cada deploy.
- O compose monta `../files/contas.json:/app/contas.json:ro` e define `CONTAS_JSON_PATH=/app/contas.json`. O caminho é `/app` de propósito: `extratores/saneago.py` ainda abre `"contas.json"` relativo ao diretório de trabalho (`/app`), então o caminho explícito e o implícito caem no mesmo arquivo.
- Quem vê o arquivo: root da VPS e administradores do painel Dokploy. É aceitável na fase de teste. O destino natural é virar tabela no Postgres (`unidades_contas` do Hub), editável pela UI.
- Atualizar: editar o File Mount e reiniciar `faturas-app` e `faturas-worker`.

## Riscos aceitos

- **O Silo depende de um mantenedor.** Mitigação: tag fixada, código só-S3 e backup fora da VPS. Para sair: subir Garage, RustFS ou Hetzner Object Storage, `mcli mirror` do bucket e trocar `S3_ENDPOINT_URL` e as credenciais.
- **Tudo numa VPS só** (app, banco, storage). Se a VPS cair, o serviço cai também. A recuperação vem dos backups no R2 (RPO: 1 dia para o banco, 1 semana para os PDFs; os PDFs também existem na origem).
- **Dokploy desliga o storage durante o Volume Backup semanal.** Um job rodando nesse minuto falha. Agende fora do horário de uso.

## Fontes

- MinIO arquivado e imagens removidas: [StableBuild: MinIO images disappeared from Docker Hub](https://www.stablebuild.com/blog/minio-images-disappeared-from-docker-hub), [Vonng: MinIO Is Dead, Long Live MinIO](https://blog.vonng.com/en/db/minio-resurrect/). Conferência direta: `GET hub.docker.com/v2/repositories/minio/minio/tags` → `object not found` (2026-09-22).
- Silo: [github.com/pgsty/silo](https://github.com/pgsty/silo) (README, `Dockerfile.goreleaser`, `dockerscripts/docker-entrypoint.sh`).
- Dokploy backups: [Backups](https://docs.dokploy.com/docs/core/databases/backups), [Restore](https://docs.dokploy.com/docs/core/databases/restore), [Volume Backups](https://docs.dokploy.com/docs/core/volume-backups). Código: [`schema/backups.ts`](https://github.com/Dokploy/dokploy/blob/canary/packages/server/src/db/schema/backups.ts) (`backupType: "compose"`, `keepLatestCount`) e [`utils/backups/utils.ts`](https://github.com/Dokploy/dokploy/blob/canary/packages/server/src/utils/backups/utils.ts) (comando `pg_dump`).
- File Mounts / `../files`: [Docker Compose](https://docs.dokploy.com/docs/core/docker-compose), [Troubleshooting: Volumes & Mounts](https://docs.dokploy.com/docs/core/troubleshooting/volumes-mounts).
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/) (10 GB-mês grátis, egress grátis), [Hetzner Object Storage](https://www.hetzner.com/storage/object-storage/).
