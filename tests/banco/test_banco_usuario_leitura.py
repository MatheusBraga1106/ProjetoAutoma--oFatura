"""Usuário só de leitura pro DBeaver: validação de nome/senha (entram direto
no DDL) e comportamento fora do Postgres. A criação em si só roda no
Postgres real (deploy)."""

import pytest
from sqlalchemy import create_engine

from banco.migrar import garantir_usuario_leitura, validar_usuario_leitura


def test_aceita_nome_e_senha_seguros():
    validar_usuario_leitura("leitura", "a1b2c3d4e5f60718293a4b5c")


@pytest.mark.parametrize("usuario", ["Leitura", "leitura; drop table faturas", "1leitura", "", "a" * 64])
def test_recusa_nome_que_precisaria_de_escape(usuario):
    with pytest.raises(ValueError):
        validar_usuario_leitura(usuario, "a1b2c3d4e5f60718293a4b5c")


@pytest.mark.parametrize("senha", ["curta123", "a1b2c3d4e5f6071' OR '1", "a1b2c3d4 e5f60718293a"])
def test_recusa_senha_curta_ou_com_caractere_especial(senha):
    with pytest.raises(ValueError):
        validar_usuario_leitura("leitura", senha)


def test_sqlite_nao_se_aplica(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    assert garantir_usuario_leitura(engine, "leitura", "a1b2c3d4e5f60718293a4b5c") is False
