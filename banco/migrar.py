"""Cria/atualiza o schema do banco. Idempotente — pode rodar a cada deploy.

    python -m banco.migrar
    python -m banco.migrar --importar-erros-sqlite erros_reportados.db

Estratégia: `create_all` (cria o que falta, nunca derruba nada) + uma lista
de passos incrementais simples (ADD COLUMN / CREATE INDEX) aplicados só se o
objeto ainda não existir. No Postgres, tudo roda sob um advisory lock, então
app, worker e o job de migração subindo juntos não disputam o DDL.

Sem Alembic de propósito: o schema nasce agora, do zero (a extração vai ser
refeita inteira sobre o banco), e o projeto não tem hoje quem mantenha
migrações geradas. Se o schema começar a mudar com frequência, trocar por
Alembic (mesmo padrão do hub-faturas).
"""

import argparse
import os
import sqlite3
import sys

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from banco.modelos import Base, ErroReportado, Meta

VERSAO_SCHEMA = "1"
_CHAVE_LOCK_PG = 774_201_001  # qualquer inteiro fixo, só identifica o lock

# (tabela, coluna, DDL do tipo) — colunas adicionadas depois da versão 1.
# Vazio por enquanto; exemplo de uso:
#   ("faturas", "observacao", "TEXT")
COLUNAS_INCREMENTAIS: list[tuple[str, str, str]] = []


def migrar(engine: Engine) -> None:
    with engine.begin() as conexao:
        if conexao.dialect.name == "postgresql":
            conexao.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CHAVE_LOCK_PG})
        Base.metadata.create_all(conexao, checkfirst=True)

        inspetor = inspect(conexao)
        for tabela, coluna, tipo in COLUNAS_INCREMENTAIS:
            existentes = {c["name"] for c in inspetor.get_columns(tabela)}
            if coluna not in existentes:
                conexao.execute(text(f'ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}'))

        atual = conexao.execute(select(Meta.valor).where(Meta.chave == "versao_schema")).scalar()
        if atual is None:
            conexao.execute(Meta.__table__.insert().values(chave="versao_schema", valor=VERSAO_SCHEMA))
        elif atual != VERSAO_SCHEMA:
            conexao.execute(
                Meta.__table__.update().where(Meta.chave == "versao_schema").values(valor=VERSAO_SCHEMA)
            )


def importar_erros_sqlite(caminho_db_antigo: str) -> int:
    """Copia a fila da aba Erros do SQLite antigo (erros_reportados.db) pro
    banco novo. Idempotente: pula relato idêntico (mesma data + mensagem +
    referência) já importado. Devolve quantos entraram."""
    from banco.sessao import sessao

    if not os.path.exists(caminho_db_antigo):
        raise FileNotFoundError(caminho_db_antigo)
    conn = sqlite3.connect(caminho_db_antigo)
    conn.row_factory = sqlite3.Row
    try:
        linhas = [dict(l) for l in conn.execute("SELECT * FROM erros_reportados ORDER BY id")]
    finally:
        conn.close()

    importados = 0
    with sessao(escrita=True) as s:
        for l in linhas:
            existe = s.execute(
                select(ErroReportado.id).where(
                    ErroReportado.data_criacao == (l.get("data_criacao") or ""),
                    ErroReportado.mensagem == (l.get("mensagem") or ""),
                    ErroReportado.concessionaria == (l.get("concessionaria") or ""),
                    ErroReportado.num_fatura == (l.get("num_fatura") or ""),
                    ErroReportado.conta_dv == (l.get("conta_dv") or ""),
                )
            ).first()
            if existe:
                continue
            s.add(ErroReportado(
                concessionaria=l.get("concessionaria") or "",
                num_fatura=l.get("num_fatura") or "",
                conta_dv=l.get("conta_dv") or "",
                mes_ano_ref=l.get("mes_ano_ref") or "",
                mensagem=l.get("mensagem") or "",
                status=l.get("status") or "aberto",
                data_criacao=l.get("data_criacao") or "",
            ))
            importados += 1
    return importados


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cria/atualiza o schema do banco (idempotente).")
    parser.add_argument("--importar-erros-sqlite", metavar="CAMINHO",
                        help="copia os erros reportados de um erros_reportados.db antigo")
    args = parser.parse_args(argv)

    from banco.sessao import obter_engine

    engine = obter_engine()
    migrar(engine)  # explícito mesmo com MIGRAR_NA_INICIALIZACAO=0
    url = engine.url.render_as_string(hide_password=True)
    print(f"Schema OK (versão {VERSAO_SCHEMA}) em {url}")

    if args.importar_erros_sqlite:
        n = importar_erros_sqlite(args.importar_erros_sqlite)
        print(f"{n} erro(s) reportado(s) importado(s) de {args.importar_erros_sqlite}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
