"""Utilitários dos testes de upload: sobe uma instância isolada do servidor
(uvicorn em subprocesso, com DADOS_SAIDA_DIR / ERROS_DB_PATH / TEMP próprios)
e fala com ela SÓ pelo contrato HTTP — POST /pipeline/jobs, SSE de eventos,
snapshot do job, download de CSV, /dados. Nada aqui importa api.py ou
pipeline.py: a ideia é que esta suíte continue valendo como regressão depois
da reescrita da camada de persistência."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
# Diretório de onde o servidor sob teste é executado (cwd do uvicorn). Por
# padrão é o próprio repositório; aponte FATURAS_APP_DIR para um `git
# worktree` de outro commit pra testar uma versão estável enquanto o código
# do repo está sendo editado. Os dados de entrada vêm sempre do repo.
APP_DIR = Path(os.environ.get("FATURAS_APP_DIR", str(REPO)))
DADOS_ENTRADA = Path(os.environ.get("FATURAS_DADOS_ENTRADA", str(REPO / "dados_entrada")))
OUTRAS = DADOS_ENTRADA / "Faturas-Outras-Distribuídoras"
SANEAGO_BORDERO = DADOS_ENTRADA / "Faturas-Saneago-Borderô"
SANEAGO_ANALITICA = DADOS_ENTRADA / "Faturas-Saneago-Analítica"

TIMEOUT_JOB_PADRAO = 600  # s — OCR de fatura-imagem pode demorar


# =========================================================
# Caminhos longos (> 260) no Windows
# =========================================================

def caminho_longo(p: "Path | str") -> str:
    s = os.path.abspath(str(p))
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def ler_bytes(p: "Path | str") -> bytes:
    with open(caminho_longo(p), "rb") as f:
        return f.read()


def listar_pdfs(pasta: Path) -> list[Path]:
    """PDFs (qualquer caixa de extensão) sob `pasta`, recursivo, ordenados.
    Usa os.walk com prefixo \\\\?\\ porque há caminhos > 260 caracteres."""
    base = caminho_longo(pasta)
    achados = []
    for raiz, _dirs, arquivos in os.walk(base):
        for nome in arquivos:
            if nome.lower().endswith(".pdf"):
                completo = os.path.join(raiz, nome)
                achados.append(Path(str(pasta)) / os.path.relpath(completo, base))
    return sorted(achados, key=lambda p: str(p))


def nome_webkit(caminho: Path, pasta_selecionada: Path) -> str:
    """Filename que a UI manda no multipart num upload de pasta: é o
    `webkitRelativePath` do navegador — nome da pasta selecionada + caminho
    relativo, sempre com "/"."""
    rel = os.path.relpath(str(caminho), str(pasta_selecionada)).replace("\\", "/")
    return f"{pasta_selecionada.name}/{rel}"


def pasta_distribuidora(nome: str) -> Path:
    """Acha a subpasta de Faturas-Outras-Distribuídoras cujo nome contém
    `nome` (comparação sem acento/caixa)."""
    alvo = _sem_acento(nome)
    for p in sorted(OUTRAS.iterdir()):
        if p.is_dir() and alvo in _sem_acento(p.name):
            return p
    raise FileNotFoundError(nome)


def achar_pdf(pasta: Path, trecho: str) -> Path:
    """Primeiro PDF sob `pasta` cujo caminho contém `trecho`."""
    for p in listar_pdfs(pasta):
        if trecho in str(p):
            return p
    raise FileNotFoundError(f"{trecho!r} em {pasta}")


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


# =========================================================
# Servidor isolado
# =========================================================

def porta_livre(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", porta)) != 0


@dataclass
class Servidor:
    porta: int
    pasta_saida: Path
    caminho_db: Path
    pasta_temp: Path
    log: Path
    processo: "subprocess.Popen | None" = None
    _log_fh: object = field(default=None, repr=False)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    def iniciar(self, timeout: float = 60) -> "Servidor":
        if not porta_livre(self.porta):
            raise RuntimeError(f"porta {self.porta} já está em uso")
        for p in (self.pasta_saida, self.pasta_temp):
            p.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "DADOS_SAIDA_DIR": str(self.pasta_saida),
            "ERROS_DB_PATH": str(self.caminho_db),
            # uploads vão pra tempfile.mkdtemp — isolar TEMP permite checar
            # vazamento de arquivos temporários (ex.: restart no meio do job)
            "TEMP": str(self.pasta_temp),
            "TMP": str(self.pasta_temp),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        self._log_fh = open(self.log, "ab")
        self.processo = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(self.porta),
             "--log-level", "warning"],
            cwd=str(APP_DIR), env=env, stdout=self._log_fh, stderr=subprocess.STDOUT,
        )
        limite = time.time() + timeout
        while time.time() < limite:
            if self.processo.poll() is not None:
                raise RuntimeError(f"servidor morreu ao subir (veja {self.log})")
            try:
                if httpx.get(self.url + "/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        self.parar()
        raise RuntimeError("servidor não respondeu /health a tempo")

    def parar(self):
        if self.processo and self.processo.poll() is None:
            self.processo.kill()  # simula queda abrupta também (sem shutdown gracioso)
            self.processo.wait(timeout=30)
        self.processo = None
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    @property
    def pid(self) -> int:
        return self.processo.pid


def novo_servidor(base: Path, porta: int) -> Servidor:
    base.mkdir(parents=True, exist_ok=True)
    return Servidor(
        porta=porta,
        pasta_saida=base / "saida",
        caminho_db=base / "erros.db",
        pasta_temp=base / "temp",
        log=base / "servidor.log",
    )


# =========================================================
# Cliente do contrato HTTP
# =========================================================

Arquivo = tuple  # (nome_no_multipart, bytes)


def criar_job(url: str, arquivos: list[Arquivo], timeout: float = 600) -> httpx.Response:
    partes = [("arquivos", (nome, conteudo, "application/pdf")) for nome, conteudo in arquivos]
    return httpx.post(url + "/pipeline/jobs", files=partes, timeout=timeout)


@dataclass
class EventoSSE:
    tipo: str
    dados: dict
    recebido_em: float
    id: "str | None" = None


def ler_eventos(url: str, job_id: str, timeout: float = TIMEOUT_JOB_PADRAO, parar_apos: "int | None" = None,
                cabecalhos: "dict | None" = None) -> list[EventoSSE]:
    """Consome o SSE de /pipeline/jobs/{id}/eventos até o servidor fechar o
    stream (ou até `parar_apos` eventos, simulando desconexão do cliente)."""
    eventos: list[EventoSSE] = []
    with httpx.stream("GET", f"{url}/pipeline/jobs/{job_id}/eventos", headers=cabecalhos or {},
                      timeout=httpx.Timeout(timeout, connect=10)) as resp:
        resp.raise_for_status()
        assert resp.headers["content-type"].startswith("text/event-stream")
        tipo, dados, ident = "message", [], None
        for linha in resp.iter_lines():
            if linha == "":
                if dados:
                    eventos.append(EventoSSE(tipo, json.loads("\n".join(dados)), time.time(), ident))
                    if parar_apos is not None and len(eventos) >= parar_apos:
                        return eventos
                tipo, dados, ident = "message", [], None
                continue
            if linha.startswith(":"):
                continue
            campo, _, valor = linha.partition(":")
            valor = valor[1:] if valor.startswith(" ") else valor
            if campo == "event":
                tipo = valor
            elif campo == "data":
                dados.append(valor)
            elif campo == "id":
                ident = valor
    return eventos


def snapshot(url: str, job_id: str) -> httpx.Response:
    return httpx.get(f"{url}/pipeline/jobs/{job_id}", timeout=30)


def aguardar_job(url: str, job_id: str, timeout: float = TIMEOUT_JOB_PADRAO) -> dict:
    limite = time.time() + timeout
    while time.time() < limite:
        r = snapshot(url, job_id)
        r.raise_for_status()
        corpo = r.json()
        if corpo["status"] in ("concluido", "erro"):
            return corpo
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} não terminou em {timeout}s")


def rodar_job(url: str, arquivos: list[Arquivo], timeout: float = TIMEOUT_JOB_PADRAO) -> tuple[dict, list[EventoSSE]]:
    """Envia o lote, consome o SSE inteiro e devolve (snapshot_final, eventos)."""
    r = criar_job(url, arquivos)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    eventos = ler_eventos(url, job_id, timeout=timeout)
    return aguardar_job(url, job_id, timeout=60), eventos


def total_dados(url: str, empresa: str) -> int:
    r = httpx.get(f"{url}/dados/{empresa}", params={"tamanho_pagina": 1}, timeout=60)
    if r.status_code == 404:
        return 0
    r.raise_for_status()
    return r.json()["total"]


def todas_linhas(url: str, empresa: str) -> list[dict]:
    linhas, pagina = [], 1
    while True:
        r = httpx.get(f"{url}/dados/{empresa}", params={"pagina": pagina, "tamanho_pagina": 500}, timeout=60)
        if r.status_code == 404:
            return linhas
        r.raise_for_status()
        corpo = r.json()
        linhas.extend(corpo["linhas"])
        if len(linhas) >= corpo["total"] or not corpo["linhas"]:
            return linhas
        pagina += 1


def chave_fatura(linha: dict) -> str:
    return "|".join(str(linha.get(c) or "") for c in ("NUM_FATURA", "MES_ANO_REF", "CONTA_DV"))


# =========================================================
# Validadores do contrato (formato dos eventos e do resultado)
# =========================================================

STATUS_JOB = {"pendente", "processando", "concluido", "erro"}
CAMPOS_RESUMO_EMPRESA = {"adicionadas", "duplicadas", "suspeitas", "descartadas",
                         "contas_json_encontrado", "linhas_sem_correspondencia"}


def validar_sequencia_sse(eventos: list[EventoSSE], nomes_enviados: list[str]) -> dict:
    """Contrato do SSE: exatamente um `progresso` por PDF, na ordem de envio,
    `indice` 1..N contínuo, `total` = N, `arquivo` = filename do multipart;
    depois um único evento terminal (`concluido` ou `erro`), que é o último.
    Devolve os dados do evento terminal."""
    progresso = [e for e in eventos if e.tipo == "progresso"]
    terminais = [e for e in eventos if e.tipo in ("concluido", "erro")]
    assert len(terminais) == 1, [e.tipo for e in eventos]
    assert eventos[-1] is terminais[0], "evento terminal não foi o último"
    assert len(progresso) == len(nomes_enviados)
    for i, (ev, nome) in enumerate(zip(progresso, nomes_enviados), start=1):
        d = ev.dados
        assert d["indice"] == i
        assert d["total"] == len(nomes_enviados)
        assert d["arquivo"] == nome
        assert d["status"] in ("ok", "erro")
        if d["status"] == "ok":
            assert isinstance(d["empresa"], str) and d["empresa"]
        else:
            assert isinstance(d["detalhe"], str) and d["detalhe"]
    return terminais[0].dados


def validar_resultado(resultado: dict, nomes_enviados: list[str]):
    """Contrato de `resultado` de um job concluído."""
    assert set(resultado) >= {"arquivos", "empresas"}
    arquivos = resultado["arquivos"]
    assert [a["arquivo_origem"] for a in arquivos] == nomes_enviados
    for a in arquivos:
        assert a["status"] in ("ok", "erro")
        if a["status"] == "ok":
            assert isinstance(a["empresa"], str)
            assert isinstance(a["linhas"], int) and a["linhas"] >= 1
        else:
            assert isinstance(a["erro"], str) and a["erro"]
    for empresa, resumo in resultado["empresas"].items():
        assert empresa == empresa.upper()
        assert CAMPOS_RESUMO_EMPRESA <= set(resumo), (empresa, resumo)
        for campo in ("adicionadas", "duplicadas", "suspeitas", "descartadas"):
            assert isinstance(resumo[campo], int) and resumo[campo] >= 0


def pdf_minimo(texto: str = "Documento de teste sem fatura") -> bytes:
    """PDF válido de uma página com camada de texto (gerado com PyMuPDF,
    dependência do próprio projeto)."""
    import pymupdf
    doc = pymupdf.open()
    pagina = doc.new_page()
    pagina.insert_text((72, 72), texto)
    dados = doc.tobytes()
    doc.close()
    return dados


def pdf_imagem_de(pdf_original: bytes, dpi: int = 200) -> bytes:
    """Rasteriza cada página de um PDF real num PDF só-imagem (sem camada de
    texto) — força o caminho de OCR com conteúdo de fatura de verdade."""
    import pymupdf
    origem = pymupdf.open(stream=pdf_original, filetype="pdf")
    destino = pymupdf.open()
    for pagina in origem:
        pix = pagina.get_pixmap(dpi=dpi)
        nova = destino.new_page(width=pagina.rect.width, height=pagina.rect.height)
        nova.insert_image(nova.rect, pixmap=pix)
    dados = destino.tobytes()
    origem.close()
    destino.close()
    return dados
