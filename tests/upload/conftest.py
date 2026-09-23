"""Fixtures da suíte de upload.

- `servidor`: uma instância compartilhada (porta 8021) com saída isolada,
  pra testes que não dependem do estado acumulado dos CSVs.
- `servidor_limpo`: instância nova por teste (portas 8022–8029), com pasta
  de saída vazia — pra testes de duplicata, concorrência e contagem.

Nenhuma das duas toca em dados_saida/ nem em erros_reportados.db reais.
Variáveis úteis: FATURAS_APP_DIR (rodar o servidor a partir de outro
diretório/worktree), FATURAS_DADOS_ENTRADA (outra pasta de PDFs reais).
"""

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import utilitarios as u  # noqa: E402

_portas_limpas = itertools.cycle(range(8022, 8030))


def pytest_configure(config):
    config.addinivalue_line("markers", "lento: lotes grandes / OCR / limites (minutos)")
    config.addinivalue_line("markers", "dados_reais: usa PDFs de dados_entrada/ (pulado se a pasta não existir)")


def pytest_collection_modifyitems(config, items):
    if not u.DADOS_ENTRADA.is_dir():
        pular = pytest.mark.skip(reason=f"dados reais ausentes em {u.DADOS_ENTRADA}")
        for item in items:
            if "dados_reais" in item.keywords:
                item.add_marker(pular)


@pytest.fixture(scope="session")
def servidor(tmp_path_factory):
    # 8021 é a porta combinada; se estiver ocupada (outra instância/agente), usa 8030-8039.
    porta = next((p for p in [8021, *range(8030, 8040)] if u.porta_livre(p)), None)
    if porta is None:
        pytest.fail("nenhuma porta livre para o servidor de teste (8021, 8030-8039)")
    s = u.novo_servidor(tmp_path_factory.mktemp("servidor_compartilhado"), porta).iniciar()
    yield s
    s.parar()


@pytest.fixture
def servidor_limpo(tmp_path):
    for _ in range(8):
        porta = next(_portas_limpas)
        if u.porta_livre(porta):
            break
    s = u.novo_servidor(tmp_path / "srv", porta).iniciar()
    yield s
    s.parar()


@pytest.fixture(scope="session")
def fatura():
    """Acesso a PDFs reais por distribuidora: fatura(EMPRESA) -> (nome_webkit, bytes)."""
    return amostra_real


# Uma fatura "boa" (extrai >= 1 linha no commit 67cebcf) por distribuidora
# implementada: (empresa, pasta selecionada no navegador, trecho do caminho).
AMOSTRAS = {
    "SANEAGO": (lambda: u.SANEAGO_BORDERO, "SANEAGO - NOVEMBRO.2023"),
    "SANEAGO_ANALITICA": (lambda: u.SANEAGO_ANALITICA, "2023" + "\\" + "FATURA ANALÍTICA.pdf"),
    "BURITI_ALEGRE": (lambda: u.pasta_distribuidora("BURITI ALEGRE"), "FATURA Nº 11317"),
    "DEMAE": (lambda: u.pasta_distribuidora("DEMAE CALDAS"), "215449 (1).pdf"),
    "SAAE_ABADIANIA": (lambda: u.pasta_distribuidora("ABADIANIA"), "260039549"),
    "SAAE_CORUMBA": (lambda: u.pasta_distribuidora("CORUMBA"), "605732"),
    "SAAE_MINEIROS": (lambda: u.pasta_distribuidora("MINEIROS"), "9753419"),
    "SAE": (lambda: u.pasta_distribuidora("SAE CATALAO"), "2021" + "\\" + "11723-4.pdf"),
    "IPAMERI": (lambda: u.pasta_distribuidora("IPAMERI"), "119044"),
}


def amostra_real(empresa: str) -> tuple:
    pasta_fn, trecho = AMOSTRAS[empresa]
    pasta = pasta_fn()
    caminho = u.achar_pdf(pasta, trecho)
    return u.nome_webkit(caminho, pasta), u.ler_bytes(caminho)
