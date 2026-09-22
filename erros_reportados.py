"""Banco de erros reportados pela aba "Erros" da UI — SQLite simples, sem
dependência nova (sqlite3 é builtin), seguindo o mesmo padrão do hub-faturas
(banco de arquivo local, sem precisar de Docker/Postgres pra rodar). Guarda
só o relato do usuário (o que ele acha que está errado numa fatura já
extraída) + a referência da fatura, se ele souber; não altera nada nos CSVs
de dados_saida/ — é puramente uma fila de revisão manual."""

import os
import sqlite3
from datetime import datetime

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
CAMINHO_DB = os.environ.get("ERROS_DB_PATH", os.path.join(PASTA_BASE, "erros_reportados.db"))

STATUS_VALIDOS = {"aberto", "resolvido"}


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(CAMINHO_DB)
    conn.row_factory = sqlite3.Row
    return conn


def inicializar_db() -> None:
    with _conectar() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS erros_reportados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concessionaria TEXT,
                num_fatura TEXT,
                conta_dv TEXT,
                mes_ano_ref TEXT,
                mensagem TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'aberto',
                data_criacao TEXT NOT NULL
            )
        """)


def criar_erro(mensagem: str, concessionaria: str = "", num_fatura: str = "",
               conta_dv: str = "", mes_ano_ref: str = "") -> dict:
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conectar() as conn:
        cursor = conn.execute(
            """INSERT INTO erros_reportados
               (concessionaria, num_fatura, conta_dv, mes_ano_ref, mensagem, status, data_criacao)
               VALUES (?, ?, ?, ?, ?, 'aberto', ?)""",
            (concessionaria.strip(), num_fatura.strip(), conta_dv.strip(), mes_ano_ref.strip(),
             mensagem.strip(), agora),
        )
        novo_id = cursor.lastrowid
    return obter_erro(novo_id)


def obter_erro(erro_id: int) -> "dict | None":
    with _conectar() as conn:
        linha = conn.execute("SELECT * FROM erros_reportados WHERE id = ?", (erro_id,)).fetchone()
    return dict(linha) if linha else None


def listar_erros(status: "str | None" = None) -> list[dict]:
    with _conectar() as conn:
        if status:
            linhas = conn.execute(
                "SELECT * FROM erros_reportados WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            linhas = conn.execute("SELECT * FROM erros_reportados ORDER BY id DESC").fetchall()
    return [dict(linha) for linha in linhas]


def atualizar_status(erro_id: int, status: str) -> "dict | None":
    if status not in STATUS_VALIDOS:
        raise ValueError(f"status inválido: {status!r} (esperado um de {STATUS_VALIDOS})")
    with _conectar() as conn:
        conn.execute("UPDATE erros_reportados SET status = ? WHERE id = ?", (status, erro_id))
    return obter_erro(erro_id)
