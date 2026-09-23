"""Resolução das variáveis de ambiente da camada de persistência.

Tudo aqui é lido na hora (não no import), pra que um processo — ou um teste —
possa trocar DATABASE_URL / DADOS_SAIDA_DIR / S3_* antes do primeiro uso sem
precisar reimportar módulo nenhum.

Contrato de env (compartilhado com Dockerfile/docker-compose):
  DATABASE_URL      postgresql+psycopg://... ; ausente -> SQLite em arquivo
                    (<DADOS_SAIDA_DIR>/faturas.db), só pra desenvolvimento.
  S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, S3_REGION
                    object storage S3/MinIO dos PDFs. Sem S3_BUCKET -> pasta
                    local (<DADOS_SAIDA_DIR>/armazenamento), só pra dev.
  CONTAS_JSON_PATH  cadastro de contas (default: contas.json na raiz).
  WORKER_EMBUTIDO   "1" = a própria API processa os jobs numa thread (default
                    quando o banco é SQLite); "0" = só o `python worker.py`.
  MIGRAR_NA_INICIALIZACAO  "0" desliga o create/upgrade automático do schema
                    no primeiro uso do banco (default ligado; é idempotente e,
                    no Postgres, serializado por advisory lock).
"""

import os

PASTA_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _verdadeiro(valor: "str | None", padrao: bool) -> bool:
    if valor is None or valor.strip() == "":
        return padrao
    return valor.strip().lower() in ("1", "true", "sim", "yes", "on")


def pasta_saida() -> str:
    return os.environ.get("DADOS_SAIDA_DIR") or os.path.join(PASTA_BASE, "dados_saida")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Aceita a forma "postgres://" / "postgresql://" que painéis (Dokploy,
        # Heroku...) costumam gerar e força o driver psycopg 3.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    caminho = os.path.join(pasta_saida(), "faturas.db")
    return "sqlite:///" + caminho.replace("\\", "/")


def eh_sqlite(url: "str | None" = None) -> bool:
    return (url or database_url()).startswith("sqlite")


def caminho_contas_json() -> str:
    return os.environ.get("CONTAS_JSON_PATH") or os.path.join(PASTA_BASE, "contas.json")


def worker_embutido() -> bool:
    return _verdadeiro(os.environ.get("WORKER_EMBUTIDO"), padrao=eh_sqlite())


def migrar_na_inicializacao() -> bool:
    return _verdadeiro(os.environ.get("MIGRAR_NA_INICIALIZACAO"), padrao=True)


def lease_job_segundos() -> int:
    """Depois de quanto tempo sem heartbeat um job 'processando' é
    considerado órfão (worker morreu) e pode ser retomado por outro."""
    return int(os.environ.get("JOB_LEASE_SEGUNDOS", "90"))


def max_tentativas_job() -> int:
    return int(os.environ.get("JOB_MAX_TENTATIVAS", "5"))
