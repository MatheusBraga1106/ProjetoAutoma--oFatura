"""Engine e sessões do SQLAlchemy 2.0, prontos pra Postgres (psycopg) em
produção e SQLite em arquivo no desenvolvimento/testes.

A engine é criada preguiçosamente e cacheada por URL: trocar DATABASE_URL
(ex.: num teste) e chamar `sessao()` de novo já usa o banco novo.

SQLite: WAL + busy_timeout pra API, worker embutido e heartbeat conviverem
no mesmo arquivo, e `sessao(escrita=True)` abre a transação com
BEGIN IMMEDIATE — sem isso, uma transação que lê e depois escreve pode levar
SQLITE_BUSY na hora de "promover" o lock se outra conexão gravou no meio.
"""

import os
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from banco.config import database_url, eh_sqlite, migrar_na_inicializacao

_engines: dict[str, Engine] = {}
_lock = threading.RLock()


def _criar_engine(url: str) -> Engine:
    if eh_sqlite(url):
        caminho = url.split("sqlite:///", 1)[-1]
        if caminho and caminho != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 60})

        @event.listens_for(engine, "connect")
        def _ao_conectar(conexao_dbapi, _registro):
            # Controle de transação fica com o SQLAlchemy (evento "begin"
            # abaixo), não com o pysqlite — senão BEGIN IMMEDIATE não pega.
            conexao_dbapi.isolation_level = None
            cursor = conexao_dbapi.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _ao_iniciar(conexao):
            if conexao.get_execution_options().get("sqlite_begin_immediate"):
                conexao.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conexao.exec_driver_sql("BEGIN")

        return engine

    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def obter_engine() -> Engine:
    url = database_url()
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            engine = _criar_engine(url)
            _engines[url] = engine
            if migrar_na_inicializacao():
                from banco.migrar import migrar

                migrar(engine)
        return engine


def reiniciar() -> None:
    """Descarta as engines em cache (testes trocam de banco entre casos)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


def nova_sessao(escrita: bool = False) -> Session:
    engine = obter_engine()
    if escrita and engine.dialect.name == "sqlite":
        engine = engine.execution_options(sqlite_begin_immediate=True)
    return Session(bind=engine, expire_on_commit=False, autoflush=True)


@contextmanager
def sessao(escrita: bool = False):
    """Sessão com commit ao sair sem exceção e rollback caso contrário."""
    s = nova_sessao(escrita=escrita)
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()
