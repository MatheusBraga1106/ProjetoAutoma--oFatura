"""Ingestão no banco: onde moram as regras de deduplicação e reextração.

Fluxo de um arquivo (o worker chama `processar_item` pra cada item do job):

  1. ARQUIVO — identidade = sha256 do conteúdo (`registrar_arquivo`). O blob
     vai pro storage uma vez só; cada caminho/nome por onde o mesmo conteúdo
     chegou vira uma linha em `arquivo_origens` (o caminho é o que identifica
     a distribuidora, então nenhum é jogado fora).
  2. PULO — se esse conteúdo já foi processado com SUCESSO pela MESMA versão
     do extrator (e com o mesmo texto), não faz nada: reenviar o mesmo PDF
     sem mudar código não custa extração nem gera linha nova.
  3. TEXTO — reaproveita o texto em cache no storage (pdftotext/OCR já feito
     antes); só roda pdftotext -> OCR se não houver cache (ou se pedido).
  4. EXTRAÇÃO — extrator da distribuidora (inalterado) + filtrar_linhas_vazias.
  5. SINCRONIZAÇÃO — cada linha ganha uma chave natural (ver
     `chave_natural`) e é gravada em `faturas` com upsert:
        - chave nova ................ fatura nova (versão 1)
        - mesma chave, mesmo conteúdo  nada muda ("duplicada")
        - mesma chave, conteúdo novo . nova versão (histórico em
                                       fatura_versoes, com a versão do código)
        - chave que ESTE arquivo produzia e deixou de produzir (ex.: extrator
          corrigido passou a ler o mês) -> a ligação arquivo->fatura é
          desativada; se nenhum outro arquivo ainda produz aquela fatura, ela
          vira inativa (obsoleta) — some das telas, fica no histórico.
     SANEAGO_ANALITICA não gera fatura: vai pra `hidrometros_analitica`, e as
     faturas SANEAGO afetadas (de qualquer lote) têm o hidrômetro refeito.
  6. DERIVADOS — hidrômetro, contas.json (enriquecer_com_contas_json) e
     SUSPEITO (marcar_suspeitas) são calculados a partir de `dados_extraidos`
     e gravados em `dados`; `recalcular_desatualizados` refaz só isso (sem
     reextrair) quando contas.json ou pipeline.py mudam.

Tudo do passo 5/6 + o status do item + o evento de progresso vão numa
transação só: ou o arquivo inteiro entrou, ou nada entrou (retomável).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import pipeline
from banco.config import PASTA_BASE, caminho_contas_json
from banco.modelos import (
    ArquivoOrigem,
    ArquivoUpload,
    Fatura,
    FaturaOrigem,
    FaturaVersao,
    HidrometroAnalitica,
    Job,
    JobArquivo,
    JobEvento,
    agora,
)
from banco.sessao import sessao
from banco.storage import chave_pdf, chave_texto, obter_armazenamento

# Campos que o extrator preenche mas que não são "conteúdo da fatura" — ficam
# fora do hash, senão toda reextração viraria versão nova só pela data.
CAMPOS_VOLATEIS = {"DATA_PROCESSAMENTO", "ARQUIVO_ORIGEM", "PASTA_ORIGEM"}

# Suba quando mudar a lógica de derivados daqui (hidrômetro / ALERTA_CHAVE)
# — força recalcular_desatualizados a refazer tudo.
VERSAO_DERIVADOS = "1"

COLUNAS_CSV = [
    "CONCESSIONARIA", "NUM_FATURA", "MES_ANO_REF", "VENCIMENTO", "COD_ORGAO_AGRUPADOR",
    "COD_ORGAO_PAGADOR", "NOME_ORGAO_AGRUPADOR", "CONTA_DV", "NUM_HIDROMETRO", "NOME_CLIENTE",
    "LOGRADOURO", "CONSUMO_M3", "VALOR_AGUA", "VALOR_ESGOTO", "BASE_CALCULO", "VALOR_TAXAS_EXTRAS",
    "VALOR_OUTRAS_TAXAS", "VALOR_TOTAL", "DATA_PROCESSAMENTO", "ARQUIVO_ORIGEM", "PASTA_ORIGEM",
    "UNIDADE JUDICIÁRIA", "ENDEREÇO", "DISTRIBUIDORA", "AGUA", "ESGOTO", "SMRSU", "SUSPEITO",
    "ALERTA_CHAVE",
]

MAX_TENTATIVAS_ITEM = 3
TAMANHO_LOTE_SQL = 500


class LeasePerdida(Exception):
    """O job deixou de ser deste worker (foi dado como morto e retomado por
    outro). Aborta sem gravar nada."""


@dataclass
class Opcoes:
    forcar: bool = False          # reprocessa mesmo com a mesma versão do extrator
    refazer_texto: bool = False   # ignora o texto em cache e roda pdftotext/OCR de novo

    @classmethod
    def de_parametros(cls, parametros: "dict | None") -> "Opcoes":
        p = parametros or {}
        return cls(forcar=bool(p.get("forcar")), refazer_texto=bool(p.get("refazer_texto")))


# =========================================================
# Normalização / chave natural
# =========================================================

def _texto(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and math.isnan(valor):
        return ""
    return str(valor).strip()


def normalizar_caminho_origem(caminho: str) -> str:
    c = unicodedata.normalize("NFC", str(caminho or "")).replace("\\", "/")
    c = re.sub(r"/+", "/", c).strip()
    if c.startswith("//?/"):
        c = c[4:]
    return c.lstrip("/") or "arquivo.pdf"


def normalizar_num_fatura(valor) -> str:
    s = re.sub(r"\s+", "", _texto(valor)).upper()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def normalizar_mes_ano(valor) -> str:
    s = _texto(valor)
    m = re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{4}|\d{2})", s)
    if m:
        mes, ano = int(m.group(1)), int(m.group(2))
        if ano < 100:
            ano += 2000
        if 1 <= mes <= 12:
            return f"{mes:02d}/{ano:04d}"
    return s.upper()


def competencia_de(mes_normalizado: str) -> "str | None":
    m = re.fullmatch(r"(\d{2})/(\d{4})", mes_normalizado or "")
    return f"{m.group(2)}-{m.group(1)}" if m else None


def chave_eh_completa(empresa: str, num_fatura: str, mes: str, conta: str) -> bool:
    """Quando a chave identifica UMA fatura sem ambiguidade.

    SANEAGO: o NUM_FATURA é do borderô inteiro (~140 contas), então a conta
    é obrigatória, mais NUM_FATURA ou mês. Demais: pelo menos 2 dos 3
    (NUM_FATURA, mês, conta) — só um deles (típico de OCR ruim que só leu a
    conta do canhoto) não basta pra dizer que duas linhas são a mesma fatura.
    """
    if empresa == "SANEAGO":
        return bool(conta) and bool(num_fatura or mes)
    return sum(1 for x in (num_fatura, mes, conta) if x) >= 2


def chaves_do_arquivo(empresa: str, registros: list[dict], sha_arquivo: str) -> list[tuple[str, bool, str]]:
    """Chave natural de cada linha extraída de UM arquivo.
    Devolve [(chave, completa, alerta)], alerta em {"", "incompleta",
    "repetida_no_arquivo"}.

    - Chave completa: EMPRESA|NUM_FATURA|MM/AAAA|CONTA (normalizados). A
      mesma chave vinda de outro arquivo é a MESMA fatura (dedup entre
      lotes/arquivos).
    - Chave incompleta: a linha é mantida (nunca descartada), mas com a
      chave escopada ao arquivo — EMPRESA|INCOMPLETA|<sha do arquivo>|
      <partes>|<n> — então reenviar o mesmo arquivo não duplica, e ela nunca
      é fundida em silêncio com a linha incompleta de outro arquivo.
      Aparece com ALERTA_CHAVE=incompleta.
    - Chave completa repetida DENTRO do mesmo arquivo: as duas linhas são
      mantidas (a 2ª ganha sufixo #2) e marcadas — não dá pra saber se é
      repetição do layout ou cobrança real em dobro, então não se perde valor.
    """
    resultado = []
    vistas: dict[str, int] = {}
    for reg in registros:
        nf = normalizar_num_fatura(reg.get("NUM_FATURA"))
        mes = normalizar_mes_ano(reg.get("MES_ANO_REF"))
        conta = pipeline.normalizar_conta_dv(_texto(reg.get("CONTA_DV")))
        completa = chave_eh_completa(empresa, nf, mes, conta)
        if completa:
            base = f"{empresa}|{nf}|{mes}|{conta}"
        else:
            base = f"{empresa}|INCOMPLETA|{sha_arquivo[:16]}|{nf}|{mes}|{conta}"
        n = vistas.get(base, 0) + 1
        vistas[base] = n
        if completa:
            chave, alerta = (base, "") if n == 1 else (f"{base}#{n}", "repetida_no_arquivo")
        else:
            chave, alerta = f"{base}|{n}", "incompleta"
        resultado.append((chave, completa, alerta))
    return resultado


def _limpar_valor(valor):
    if valor is None:
        return None
    if hasattr(valor, "item") and not isinstance(valor, (str, bytes)):
        try:
            valor = valor.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(valor, float):
        return None if (math.isnan(valor) or math.isinf(valor)) else valor
    if isinstance(valor, (bool, int, str)):
        return valor
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    try:
        return float(valor)  # Decimal
    except (TypeError, ValueError):
        return str(valor)


def registro_limpo(linha: "pd.Series | dict") -> dict:
    itens = linha.items() if hasattr(linha, "items") else linha
    return {str(k): _limpar_valor(v) for k, v in itens}


def hash_conteudo(registro: dict) -> str:
    payload = {k: v for k, v in registro.items() if k not in CAMPOS_VOLATEIS}
    bruto = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _sha_arquivo_disco(caminho: str) -> str:
    try:
        with open(caminho, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "ausente"


def hash_derivados_atual() -> str:
    """Muda quando muda qualquer coisa que entra no cálculo dos derivados:
    o contas.json, o pipeline.py (enriquecimento/suspeitas) ou as regras
    daqui (VERSAO_DERIVADOS)."""
    partes = [
        _sha_arquivo_disco(caminho_contas_json()),
        _sha_arquivo_disco(os.path.join(PASTA_BASE, "pipeline.py")),
        VERSAO_DERIVADOS,
    ]
    return hashlib.sha256("|".join(partes).encode()).hexdigest()


# =========================================================
# Registro de arquivos e jobs
# =========================================================

def armazenar_blob(conteudo: bytes, armazenamento=None) -> str:
    """Grava o PDF no storage (endereçado por conteúdo) e devolve o sha256.
    Pode rodar fora de transação: gravar de novo o mesmo blob é no-op."""
    armazenamento = armazenamento or obter_armazenamento()
    sha = hashlib.sha256(conteudo).hexdigest()
    chave = chave_pdf(sha)
    if not armazenamento.existe(chave):
        armazenamento.salvar(chave, conteudo, "application/pdf")
    return sha


def registrar_arquivo(s, sha256: str, tamanho: int, caminho_origem: str) -> tuple[ArquivoUpload, ArquivoOrigem]:
    """Acha ou cria o arquivo (por sha256) e a origem (por caminho). O blob
    já precisa estar no storage (armazenar_blob)."""
    arquivo = s.scalar(select(ArquivoUpload).where(ArquivoUpload.sha256 == sha256))
    if arquivo is None:
        try:
            with s.begin_nested():
                arquivo = ArquivoUpload(sha256=sha256, tamanho_bytes=tamanho, chave_storage=chave_pdf(sha256))
                s.add(arquivo)
        except IntegrityError:  # outro processo registrou o mesmo conteúdo agora
            arquivo = s.scalar(select(ArquivoUpload).where(ArquivoUpload.sha256 == sha256))

    caminho = normalizar_caminho_origem(caminho_origem)
    origem = s.scalar(select(ArquivoOrigem).where(
        ArquivoOrigem.arquivo_id == arquivo.id, ArquivoOrigem.caminho_origem == caminho))
    if origem is None:
        try:
            with s.begin_nested():
                origem = ArquivoOrigem(
                    arquivo_id=arquivo.id,
                    caminho_origem=caminho,
                    nome_arquivo=caminho.rsplit("/", 1)[-1],
                    pasta_origem=caminho.rsplit("/", 1)[0] if "/" in caminho else "",
                    empresa_identificada=pipeline.identificar_distribuidora(caminho),
                )
                s.add(origem)
        except IntegrityError:
            origem = s.scalar(select(ArquivoOrigem).where(
                ArquivoOrigem.arquivo_id == arquivo.id, ArquivoOrigem.caminho_origem == caminho))
    return arquivo, origem


def registrar_texto_existente(s, arquivo: ArquivoUpload, conteudo_txt: bytes, via_ocr: bool,
                              armazenamento=None, substituir: bool = False) -> bool:
    """Usa um .txt que já existe ao lado do PDF (gerado por uma rodada
    anterior do Main.py) como cache de texto — evita refazer horas de OCR na
    reextração completa. Não sobrescreve um cache existente, salvo pedido."""
    if arquivo.texto_chave_storage and not substituir:
        return False
    armazenamento = armazenamento or obter_armazenamento()
    chave = chave_texto(arquivo.sha256)
    armazenamento.salvar(chave, conteudo_txt, "text/plain; charset=utf-8")
    arquivo.texto_chave_storage = chave
    arquivo.texto_sha256 = hashlib.sha256(conteudo_txt).hexdigest()
    arquivo.texto_via_ocr = via_ocr
    arquivo.texto_fonte = "txt_existente"
    arquivo.texto_versao = None  # desconhecida: não fomos nós que geramos
    return True


def criar_job(s, itens: list[tuple[ArquivoUpload, ArquivoOrigem, str]], tipo: str = "extracao",
              parametros: "dict | None" = None) -> Job:
    """itens = [(arquivo, origem, nome_como_recebido)] na ordem de envio."""
    import uuid

    job = Job(id=uuid.uuid4().hex, tipo=tipo, status="pendente", parametros=parametros or {},
              total_arquivos=len(itens))
    s.add(job)
    s.flush()
    for indice, (arquivo, origem, nome) in enumerate(itens, start=1):
        s.add(JobArquivo(job_id=job.id, indice=indice, arquivo_id=arquivo.id, origem_id=origem.id,
                         caminho_origem=nome or origem.caminho_origem))
    return job


def registrar_evento(s, job_id: str, tipo: str, dados: dict) -> None:
    s.add(JobEvento(job_id=job_id, tipo=tipo, dados=dados))


# =========================================================
# Texto do PDF (pdftotext -> OCR), com cache no storage
# =========================================================

def nome_seguro(nome: str) -> str:
    """Nome de arquivo pra pasta temporária. Mantém o nome original (o
    extrator da analítica tira o mês/ano do NOME do arquivo quando o texto
    não tem REFERÊNCIA), só troca caractere proibido e limita o tamanho."""
    base = os.path.splitext(os.path.basename(str(nome).replace("\\", "/").split("/")[-1]))[0]
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return (base or "arquivo")[:120]


@dataclass
class TextoPreparado:
    caminho_txt: str
    via_ocr: bool
    sha256: str
    novo: bool          # True = gerado agora (grava no cache/arquivo)
    fonte: str
    versao: "str | None"
    chave_storage: str


def preparar_texto(arquivo_sha: str, cache: dict, nome_base: str, pasta_tmp: str,
                   armazenamento, refazer: bool) -> TextoPreparado:
    from extratores.ocr_fallback import eh_texto_ocr, eh_texto_util, gerar_txt_via_ocr
    from extratores.pdftotext_fallback import converter_pdf_para_txt

    caminho_txt = os.path.join(pasta_tmp, nome_base + ".txt")
    if cache.get("texto_chave_storage") and not refazer:
        conteudo = armazenamento.ler(cache["texto_chave_storage"])
        with open(caminho_txt, "wb") as f:
            f.write(conteudo)
        via_ocr = cache.get("texto_via_ocr")
        if via_ocr is None:
            via_ocr = eh_texto_ocr(caminho_txt)
        return TextoPreparado(caminho_txt, bool(via_ocr), hashlib.sha256(conteudo).hexdigest(), False,
                              cache.get("texto_fonte") or "cache", cache.get("texto_versao"),
                              cache["texto_chave_storage"])

    caminho_pdf = os.path.join(pasta_tmp, nome_base + ".pdf")
    with open(caminho_pdf, "wb") as f:
        f.write(armazenamento.ler(chave_pdf(arquivo_sha)))
    converter_pdf_para_txt(caminho_pdf, caminho_txt)
    if eh_texto_util(caminho_txt):
        fonte, via_ocr = "pdftotext", False
    else:
        gerar_txt_via_ocr(caminho_pdf, caminho_txt, forcar=True)
        fonte, via_ocr = "ocr", True
    conteudo = b""
    if os.path.exists(caminho_txt):
        with open(caminho_txt, "rb") as f:
            conteudo = f.read()
    else:
        with open(caminho_txt, "wb"):
            pass
    chave = chave_texto(arquivo_sha)
    armazenamento.salvar(chave, conteudo, "text/plain; charset=utf-8")
    return TextoPreparado(caminho_txt, via_ocr, hashlib.sha256(conteudo).hexdigest(), True, fonte,
                          pipeline.versao_texto(), chave)


def extrair_texto_de_pdf(caminho_pdf: str) -> tuple[str, bool]:
    """Mesma cascata pdftotext -> OCR, direto em disco (usado pelo /extrair
    avulso, que não persiste nada). Devolve (caminho_txt, veio_de_ocr)."""
    from extratores.ocr_fallback import eh_texto_util, gerar_txt_via_ocr
    from extratores.pdftotext_fallback import converter_pdf_para_txt

    caminho_txt = os.path.splitext(caminho_pdf)[0] + ".txt"
    converter_pdf_para_txt(caminho_pdf, caminho_txt)
    if eh_texto_util(caminho_txt):
        return caminho_txt, False
    gerar_txt_via_ocr(caminho_pdf, caminho_txt, forcar=True)
    return caminho_txt, True


# =========================================================
# Sincronização de faturas / hidrômetros
# =========================================================

def _em_lotes(itens: list, tamanho: int = TAMANHO_LOTE_SQL):
    for i in range(0, len(itens), tamanho):
        yield itens[i:i + tamanho]


def sincronizar_faturas(s, arquivo: ArquivoUpload, empresa: str, registros: list[dict], versao: str,
                        job_id: "str | None") -> tuple[dict, set[int]]:
    """Aplica as linhas extraídas de UM arquivo no banco (ver docstring do
    módulo). Devolve (contadores, ids de faturas tocadas)."""
    cont = {"adicionadas": 0, "atualizadas": 0, "duplicadas": 0, "removidas": 0}
    chaves = chaves_do_arquivo(empresa, registros, arquivo.sha256)

    existentes: dict[str, Fatura] = {}
    for lote in _em_lotes(list({c for c, _, _ in chaves})):
        for f in s.scalars(select(Fatura).where(Fatura.chave_natural.in_(lote))):
            existentes[f.chave_natural] = f
    origens_deste: dict[int, FaturaOrigem] = {
        fo.fatura_id: fo for fo in s.scalars(select(FaturaOrigem).where(FaturaOrigem.arquivo_id == arquivo.id))
    }
    agora_ = agora()
    tocadas: set[int] = set()
    produzidas: set[int] = set()

    for reg, (chave, completa, _alerta) in zip(registros, chaves):
        h = hash_conteudo(reg)
        mes = normalizar_mes_ano(reg.get("MES_ANO_REF"))
        f = existentes.get(chave)
        if f is None:
            f = Fatura(
                chave_natural=chave, empresa=empresa,
                num_fatura=normalizar_num_fatura(reg.get("NUM_FATURA")), mes_ano_ref=mes,
                conta_dv=_texto(reg.get("CONTA_DV")),
                conta_normalizada=pipeline.normalizar_conta_dv(_texto(reg.get("CONTA_DV"))),
                competencia=competencia_de(mes), chave_completa=completa, ativa=True, versao=1,
                versao_extrator=versao, hash_conteudo=h, arquivo_id=arquivo.id, dados_extraidos=reg,
                dados={}, criado_em=agora_, atualizado_em=agora_,
            )
            s.add(f)
            s.flush()
            existentes[chave] = f
            s.add(FaturaVersao(fatura_id=f.id, versao=1, arquivo_id=arquivo.id, job_id=job_id,
                               versao_extrator=versao, hash_conteudo=h, dados_extraidos=reg, motivo="nova"))
            s.add(FaturaOrigem(fatura_id=f.id, arquivo_id=arquivo.id, hash_conteudo=h, versao_extrator=versao,
                               ativa=True, atualizado_em=agora_))
            cont["adicionadas"] += 1
        else:
            fo = origens_deste.get(f.id)
            saida_deste_arquivo_mudou = fo is None or fo.hash_conteudo != h or not fo.ativa
            if f.hash_conteudo == h:
                if not f.ativa:
                    f.ativa = True
                    f.atualizado_em = agora_
                    cont["atualizadas"] += 1
                else:
                    cont["duplicadas"] += 1
                if f.arquivo_id == arquivo.id:
                    f.versao_extrator = versao
            elif saida_deste_arquivo_mudou:
                # Conteúdo novo pra esta fatura: reextração que mudou valor,
                # ou outro arquivo trazendo a mesma fatura com valor
                # diferente. O conteúdo mais recente vence; o anterior fica
                # em fatura_versoes. (Se ESTE arquivo continua produzindo o
                # mesmo que já produzia, não troca — evita "pingue-pongue"
                # de versão entre dois arquivos divergentes a cada rodada.)
                motivo = "reextracao" if fo is not None else "outra_origem"
                f.versao += 1
                f.hash_conteudo = h
                f.dados_extraidos = reg
                f.versao_extrator = versao
                f.arquivo_id = arquivo.id
                f.ativa = True
                f.atualizado_em = agora_
                s.add(FaturaVersao(fatura_id=f.id, versao=f.versao, arquivo_id=arquivo.id, job_id=job_id,
                                   versao_extrator=versao, hash_conteudo=h, dados_extraidos=reg, motivo=motivo))
                cont["atualizadas"] += 1
            else:
                cont["duplicadas"] += 1

            if fo is None:
                fo = FaturaOrigem(fatura_id=f.id, arquivo_id=arquivo.id, hash_conteudo=h, versao_extrator=versao,
                                  ativa=True, atualizado_em=agora_)
                s.add(fo)
                origens_deste[f.id] = fo
            else:
                fo.hash_conteudo, fo.versao_extrator, fo.ativa, fo.atualizado_em = h, versao, True, agora_
        tocadas.add(f.id)
        produzidas.add(f.id)

    # Chaves que este arquivo produzia e não produz mais.
    for fatura_id, fo in origens_deste.items():
        if fatura_id in produzidas or not fo.ativa:
            continue
        fo.ativa = False
        fo.atualizado_em = agora_
        f = s.get(Fatura, fatura_id)
        outras = s.scalars(select(FaturaOrigem).where(
            FaturaOrigem.fatura_id == fatura_id, FaturaOrigem.ativa.is_(True),
            FaturaOrigem.arquivo_id != arquivo.id,
        ).order_by(FaturaOrigem.atualizado_em.desc())).all()
        if not outras:
            if f.ativa:
                f.ativa = False
                f.atualizado_em = agora_
                cont["removidas"] += 1
        elif f.arquivo_id == arquivo.id:
            # O conteúdo vigente vinha deste arquivo; passa a valer o de
            # outro arquivo que ainda produz a fatura.
            outra = outras[0]
            versao_outra = s.scalar(select(FaturaVersao).where(
                FaturaVersao.fatura_id == fatura_id, FaturaVersao.hash_conteudo == outra.hash_conteudo,
            ).order_by(FaturaVersao.versao.desc()).limit(1))
            if versao_outra is not None and versao_outra.hash_conteudo != f.hash_conteudo:
                f.versao += 1
                f.hash_conteudo = versao_outra.hash_conteudo
                f.dados_extraidos = versao_outra.dados_extraidos
                f.versao_extrator = outra.versao_extrator
                s.add(FaturaVersao(fatura_id=f.id, versao=f.versao, arquivo_id=outra.arquivo_id, job_id=job_id,
                                   versao_extrator=outra.versao_extrator, hash_conteudo=f.hash_conteudo,
                                   dados_extraidos=f.dados_extraidos, motivo="outra_origem"))
            f.arquivo_id = outra.arquivo_id
            f.atualizado_em = agora_
        tocadas.add(fatura_id)
    return cont, tocadas


def sincronizar_hidrometros(s, arquivo: ArquivoUpload, df: "pd.DataFrame | None", versao: str) -> set[str]:
    """Grava/atualiza as linhas da analítica deste arquivo. Devolve as contas
    (normalizadas) cujo hidrômetro pode ter mudado."""
    novos: dict[tuple[str, str], dict] = {}
    if df is not None and not df.empty:
        for _, linha in df.iterrows():
            conta_dv = _texto(linha.get("CONTA_DV"))
            conta = pipeline.normalizar_conta_dv(conta_dv)
            num = _texto(linha.get("NUM_HIDROMETRO_EXTRAIDO"))
            if not conta or not num:
                continue
            mes = normalizar_mes_ano(linha.get("MES_ANO_REF"))
            novos.setdefault((conta, mes), {"conta_dv": conta_dv, "num": num})  # keep="first", igual ao mesclar

    existentes = {(h.conta_normalizada, h.mes_ano_ref): h for h in s.scalars(
        select(HidrometroAnalitica).where(HidrometroAnalitica.arquivo_id == arquivo.id))}
    afetadas: set[str] = set()
    for (conta, mes), info in novos.items():
        h = existentes.get((conta, mes))
        if h is None:
            s.add(HidrometroAnalitica(arquivo_id=arquivo.id, conta_dv=info["conta_dv"], conta_normalizada=conta,
                                      mes_ano_ref=mes, competencia=competencia_de(mes), num_hidrometro=info["num"],
                                      versao_extrator=versao, ativa=True))
            afetadas.add(conta)
        else:
            if h.num_hidrometro != info["num"] or not h.ativa:
                afetadas.add(conta)
            h.num_hidrometro, h.conta_dv, h.versao_extrator, h.ativa = info["num"], info["conta_dv"], versao, True
    for chave, h in existentes.items():
        if chave not in novos and h.ativa:
            h.ativa = False
            afetadas.add(chave[0])
    s.flush()
    return afetadas


def _mapa_hidrometros(s, contas: set[str]) -> dict[str, list]:
    mapa: dict[str, list] = {}
    for lote in _em_lotes(sorted(contas)):
        for h in s.scalars(select(HidrometroAnalitica).where(
                HidrometroAnalitica.conta_normalizada.in_(lote), HidrometroAnalitica.ativa.is_(True))):
            mapa.setdefault(h.conta_normalizada, []).append(h)
    for lista in mapa.values():
        lista.sort(key=lambda h: h.id)
    return mapa


def hidrometro_para(mapa: dict[str, list], conta: str, mes: str) -> "str | None":
    """Mesma regra do pipeline.mesclar_saneago_com_analitica, agora entre
    lotes: analítica da mesma conta e mesmo mês; senão, o hidrômetro mais
    recente daquela conta (mês mais novo; empate -> o que chegou primeiro)."""
    candidatos = mapa.get(conta)
    if not candidatos:
        return None
    if mes:
        for h in candidatos:
            if h.mes_ano_ref == mes:
                return h.num_hidrometro
    com_data = [h for h in candidatos if h.competencia]
    if com_data:
        maior = max(h.competencia for h in com_data)
        return next(h.num_hidrometro for h in com_data if h.competencia == maior)
    return candidatos[0].num_hidrometro


def atualizar_hidrometros_saneago(s, contas: set[str]) -> int:
    """Depois que a analítica de algumas contas mudou: refaz o NUM_HIDROMETRO
    das faturas SANEAGO dessas contas (de qualquer lote). Só mexe nas que
    mudaram de fato. Devolve quantas mudaram."""
    if not contas:
        return 0
    mapa = _mapa_hidrometros(s, contas)
    mudaram = 0
    for lote in _em_lotes(sorted(contas)):
        faturas = s.scalars(select(Fatura).where(
            Fatura.empresa == "SANEAGO", Fatura.ativa.is_(True), Fatura.conta_normalizada.in_(lote))).all()
        for f in faturas:
            novo = hidrometro_para(mapa, f.conta_normalizada, f.mes_ano_ref)
            if novo is None:
                novo = _texto((f.dados_extraidos or {}).get("NUM_HIDROMETRO")) or None
            if (novo or None) != (f.num_hidrometro or None):
                f.num_hidrometro = novo
                dados = dict(f.dados or {})
                dados["NUM_HIDROMETRO"] = novo if novo is not None else ""
                f.dados = dados
                f.atualizado_em = agora()
                mudaram += 1
    return mudaram


# =========================================================
# Derivados: hidrômetro + contas.json + SUSPEITO
# =========================================================

def _alerta_da_chave(chave: str) -> str:
    if "|INCOMPLETA|" in chave:
        return "incompleta"
    if re.search(r"#\d+$", chave):
        return "repetida_no_arquivo"
    return ""


def _origem_principal(s, arquivo_id: int, empresa: str) -> "ArquivoOrigem | None":
    origens = s.scalars(select(ArquivoOrigem).where(ArquivoOrigem.arquivo_id == arquivo_id)
                        .order_by(ArquivoOrigem.id)).all()
    for o in origens:
        if o.empresa_identificada == empresa:
            return o
    return origens[0] if origens else None


def recalcular_derivados(s, fatura_ids) -> dict:
    """Recalcula `dados` (a linha exibida/exportada) das faturas indicadas a
    partir de `dados_extraidos` — reaproveita enriquecer_com_contas_json e
    marcar_suspeitas do pipeline, por distribuidora, exatamente como o
    pipeline CSV fazia por lote. Devolve info do enriquecimento."""
    info = {"contas_json_encontrado": os.path.exists(caminho_contas_json()), "linhas_sem_correspondencia": 0}
    ids = sorted(set(fatura_ids))
    if not ids:
        return info
    hash_atual = hash_derivados_atual()
    cache_origem: dict[tuple[int, str], "ArquivoOrigem | None"] = {}

    for lote in _em_lotes(ids, 2000):
        faturas = s.scalars(select(Fatura).where(Fatura.id.in_(lote))).all()
        por_empresa: dict[str, list[Fatura]] = {}
        for f in faturas:
            por_empresa.setdefault(f.empresa, []).append(f)

        for empresa, lista in por_empresa.items():
            mapa = _mapa_hidrometros(s, {f.conta_normalizada for f in lista}) if empresa == "SANEAGO" else {}
            registros = []
            for f in lista:
                reg = dict(f.dados_extraidos or {})
                chave_origem = (f.arquivo_id, empresa)
                if chave_origem not in cache_origem:
                    cache_origem[chave_origem] = _origem_principal(s, f.arquivo_id, empresa)
                origem = cache_origem[chave_origem]
                reg["ARQUIVO_ORIGEM"] = origem.nome_arquivo if origem else ""
                reg["PASTA_ORIGEM"] = origem.pasta_origem if origem else ""
                if empresa == "SANEAGO":
                    h = hidrometro_para(mapa, f.conta_normalizada, f.mes_ano_ref)
                    if h is not None:
                        reg["NUM_HIDROMETRO"] = h
                reg["_ID"] = f.id
                registros.append(reg)

            df = pd.DataFrame(registros)
            for col in ("VALOR_AGUA", "VALOR_ESGOTO", "VALOR_TAXAS_EXTRAS", "VALOR_TOTAL"):
                if col not in df.columns:
                    df[col] = 0.0
            if "CONTA_DV" not in df.columns:
                df["CONTA_DV"] = ""
            df, info_lote = pipeline.enriquecer_com_contas_json(df, caminho_contas_json())
            info["linhas_sem_correspondencia"] += int(info_lote["linhas_sem_correspondencia"])
            df = pipeline.marcar_suspeitas(df)

            por_id = {f.id: f for f in lista}
            for _, linha in df.iterrows():
                dados = registro_limpo(linha)
                f = por_id[int(dados.pop("_ID"))]
                dados["SUSPEITO"] = bool(dados.get("SUSPEITO") or False)
                dados["ALERTA_CHAVE"] = _alerta_da_chave(f.chave_natural)
                f.dados = dados
                f.valor_total = _num(dados.get("VALOR_TOTAL"))
                f.consumo_m3 = _num(dados.get("CONSUMO_M3"))
                f.num_hidrometro = _texto(dados.get("NUM_HIDROMETRO")) or None
                f.suspeito = dados["SUSPEITO"]
                f.texto_busca = "\n".join(
                    _texto(dados.get(c)).lower() for c in ("NOME_CLIENTE", "CONTA_DV", "NUM_FATURA", "ARQUIVO_ORIGEM")
                )
                f.hash_derivados = hash_atual
    s.flush()
    return info


def _num(valor) -> "float | None":
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def recalcular_desatualizados(s, todos: bool = False) -> int:
    """Recalcula os derivados das faturas ativas cujo cálculo ficou velho
    (contas.json / pipeline.py mudaram). `todos=True` refaz tudo."""
    consulta = select(Fatura.id).where(Fatura.ativa.is_(True))
    if not todos:
        h = hash_derivados_atual()
        consulta = consulta.where((Fatura.hash_derivados != h) | Fatura.hash_derivados.is_(None))
    ids = list(s.scalars(consulta))
    if ids:
        recalcular_derivados(s, ids)
    return len(ids)


# =========================================================
# Processamento de um item de job
# =========================================================

def _verificar_dono(s, job_id: str, worker_id: "str | None") -> Job:
    job = s.get(Job, job_id)
    if worker_id is not None and (job is None or job.worker_id != worker_id or job.status != "processando"):
        raise LeasePerdida(job_id)
    return job


def iniciar_item(item_id: int, worker_id: "str | None") -> "JobArquivo | None":
    """Conta a tentativa ANTES de começar (commit próprio) — se o processo
    morrer no meio deste arquivo, a retomada sabe que ele já foi tentado."""
    with sessao(escrita=True) as s:
        item = s.get(JobArquivo, item_id)
        if item is None or item.status != "pendente":
            return None
        _verificar_dono(s, item.job_id, worker_id)
        item.tentativas = (item.tentativas or 0) + 1
        return item


def processar_item(item_id: int, worker_id: "str | None" = None, opcoes: "Opcoes | None" = None,
                   armazenamento=None) -> "dict | None":
    """Processa UM arquivo de um job e grava tudo numa transação.
    Devolve o evento de progresso gravado (ou None se o item já não estava
    pendente)."""
    opcoes = opcoes or Opcoes()
    armazenamento = armazenamento or obter_armazenamento()
    item = iniciar_item(item_id, worker_id)
    if item is None:
        return None

    with sessao() as s:
        job = s.get(Job, item.job_id)
        arquivo = s.get(ArquivoUpload, item.arquivo_id)
        origem = s.get(ArquivoOrigem, item.origem_id)
        origens = s.scalars(select(ArquivoOrigem).where(ArquivoOrigem.arquivo_id == arquivo.id)
                            .order_by(ArquivoOrigem.id)).all()
        total = job.total_arquivos
        cache_texto = {
            "texto_chave_storage": arquivo.texto_chave_storage, "texto_via_ocr": arquivo.texto_via_ocr,
            "texto_fonte": arquivo.texto_fonte, "texto_versao": arquivo.texto_versao,
        }
        estado_arquivo = (arquivo.status, arquivo.versao_extrator, arquivo.empresa,
                          arquivo.texto_sha256, arquivo.texto_sha256_processado)

    base_evento = {"arquivo": item.caminho_origem, "indice": item.indice, "total": total}

    if (item.tentativas or 0) > MAX_TENTATIVAS_ITEM:
        return _gravar_item(item, worker_id, None, base_evento, erro=(
            f"arquivo abandonado após {MAX_TENTATIVAS_ITEM} tentativas (o worker caiu no meio dele)"),
            status_arquivo="erro")

    # --- Distribuidora ---
    empresa_origem = origem.empresa_identificada
    empresa = estado_arquivo[2] or empresa_origem or next(
        (o.empresa_identificada for o in origens if o.empresa_identificada), None)
    aviso = None
    if estado_arquivo[2] and empresa_origem and empresa_origem != estado_arquivo[2]:
        aviso = (f"o caminho sugere {empresa_origem}, mas este mesmo conteúdo já foi identificado "
                 f"como {estado_arquivo[2]}")

    if empresa is None:
        return _gravar_item(item, worker_id, None, base_evento,
                            erro="Não foi possível identificar a distribuidora deste arquivo.",
                            detalhe_evento="distribuidora não identificada", status_arquivo="nao_identificado")
    if empresa in pipeline.EXTRATORES_NAO_IMPLEMENTADOS:
        return _gravar_item(item, worker_id, empresa, base_evento,
                            erro=f"Extrator para {empresa} ainda não foi implementado.",
                            detalhe_evento=f"extrator {empresa} não implementado", status_arquivo="nao_implementado")

    versao = pipeline.versao_extrator(empresa)

    # --- Pulo: mesmo conteúdo, mesma versão do código, mesmo texto ---
    status_ant, versao_ant, empresa_ant, texto_sha, texto_sha_proc = estado_arquivo
    if (not opcoes.forcar and not opcoes.refazer_texto and status_ant in ("ok", "sem_dados")
            and versao_ant == versao and empresa_ant == empresa and texto_sha and texto_sha == texto_sha_proc):
        return _gravar_item(item, worker_id, empresa, base_evento, inalterado=True,
                            status_anterior=status_ant, aviso=aviso)

    # --- Texto + extração (fora de transação: pode demorar minutos no OCR) ---
    origem_nome = next((o for o in origens if o.empresa_identificada == empresa), origem)
    pasta_tmp = tempfile.mkdtemp(prefix="fatura_")
    try:
        texto = preparar_texto(arquivo.sha256, cache_texto, nome_seguro(origem_nome.nome_arquivo), pasta_tmp,
                               armazenamento, refazer=opcoes.refazer_texto)
        df = pipeline.EXTRATORES_POR_EMPRESA[empresa](texto.caminho_txt, texto.via_ocr)
    except Exception as e:  # noqa: BLE001 — erro do extrator vira erro do arquivo, não do job
        return _gravar_item(item, worker_id, empresa, base_evento, erro=str(e) or e.__class__.__name__,
                            status_arquivo="erro")
    finally:
        shutil.rmtree(pasta_tmp, ignore_errors=True)

    descartadas = 0
    if df is not None and not df.empty and empresa != "SANEAGO_ANALITICA":
        df, descartadas = pipeline.filtrar_linhas_vazias(df)

    return _gravar_item(item, worker_id, empresa, base_evento, df=df, versao=versao, texto=texto,
                        descartadas=descartadas, aviso=aviso)


def _gravar_item(item: JobArquivo, worker_id, empresa, base_evento: dict, *, erro: "str | None" = None,
                 detalhe_evento: "str | None" = None, status_arquivo: "str | None" = None,
                 inalterado: bool = False, status_anterior: "str | None" = None, df=None,
                 versao: "str | None" = None, texto: "TextoPreparado | None" = None, descartadas: int = 0,
                 aviso: "str | None" = None) -> dict:
    """Parte transacional do processamento de um item (com retry em conflito
    de chave única, que só acontece com dois workers gravando a mesma
    fatura ao mesmo tempo)."""
    for tentativa in range(3):
        try:
            with sessao(escrita=True) as s:
                job = _verificar_dono(s, item.job_id, worker_id)
                it = s.get(JobArquivo, item.id)
                if it.status != "pendente":
                    return {}
                arquivo = s.get(ArquivoUpload, it.arquivo_id)
                resultado_item: dict = {"descartadas": descartadas}
                evento = dict(base_evento)
                agora_ = agora()

                if erro is not None:
                    arquivo.status = status_arquivo or "erro"
                    arquivo.erro_mensagem = erro
                    arquivo.processado_em = agora_
                    it.status, it.detalhe, it.empresa = "erro", erro, empresa
                    evento.update({"status": "erro", "detalhe": detalhe_evento or erro})
                elif inalterado:
                    ativas = s.scalar(select(func.count()).select_from(FaturaOrigem).where(
                        FaturaOrigem.arquivo_id == arquivo.id, FaturaOrigem.ativa.is_(True))) or 0
                    if status_anterior == "sem_dados":
                        it.status, it.detalhe, it.empresa = "erro", "Nenhum dado reconhecido neste arquivo.", empresa
                        evento.update({"status": "erro", "detalhe": "nenhum dado reconhecido"})
                    else:
                        resultado_item.update({"adicionadas": 0, "atualizadas": 0, "duplicadas": int(ativas),
                                               "removidas": 0, "inalterado": True})
                        it.status, it.empresa = "ok", empresa
                        it.detalhe = "já processado com esta versão do extrator (nada a fazer)"
                        evento.update({"status": "ok", "empresa": empresa, "detalhe": it.detalhe})
                    resultado_item["linhas"] = arquivo.linhas_extraidas or 0
                else:
                    if arquivo.empresa is None:
                        arquivo.empresa = empresa
                    if texto is not None and texto.novo:
                        arquivo.texto_chave_storage = texto.chave_storage
                        arquivo.texto_sha256 = texto.sha256
                        arquivo.texto_via_ocr = texto.via_ocr
                        arquivo.texto_fonte = texto.fonte
                        arquivo.texto_versao = texto.versao
                    vazio = df is None or df.empty
                    if empresa == "SANEAGO_ANALITICA":
                        contas = sincronizar_hidrometros(s, arquivo, df, versao)
                        resultado_item["hidrometros_alterados"] = atualizar_hidrometros_saneago(s, contas)
                        linhas = 0 if vazio else len(df)
                    else:
                        registros = [] if vazio else [registro_limpo(l) for _, l in df.iterrows()]
                        cont, tocadas = sincronizar_faturas(s, arquivo, empresa, registros, versao, it.job_id)
                        info = recalcular_derivados(s, tocadas)
                        resultado_item.update(cont)
                        resultado_item.update(info)
                        linhas = len(registros)
                    resultado_item["linhas"] = linhas
                    arquivo.versao_extrator = versao
                    arquivo.texto_sha256_processado = texto.sha256 if texto else arquivo.texto_sha256
                    arquivo.linhas_extraidas = linhas
                    arquivo.processado_em = agora_
                    arquivo.erro_mensagem = None
                    if vazio:
                        arquivo.status = "sem_dados"
                        it.status, it.detalhe, it.empresa = "erro", "Nenhum dado reconhecido neste arquivo.", empresa
                        evento.update({"status": "erro", "detalhe": "nenhum dado reconhecido"})
                    else:
                        arquivo.status = "ok"
                        it.status, it.empresa = "ok", empresa
                        evento.update({"status": "ok", "empresa": empresa})

                if aviso:
                    resultado_item["aviso"] = aviso
                it.resultado = resultado_item
                it.processado_em = agora_
                job.arquivos_processados = (job.arquivos_processados or 0) + 1
                job.heartbeat_em = agora_
                registrar_evento(s, job.id, "progresso", evento)
                return evento
        except IntegrityError:
            if tentativa == 2:
                raise
    return {}


# =========================================================
# Fechamento do job
# =========================================================

def resumo_do_job(s, job_id: str) -> dict:
    itens = s.scalars(select(JobArquivo).where(JobArquivo.job_id == job_id).order_by(JobArquivo.indice)).all()
    arquivos = []
    por_empresa: dict[str, dict] = {}
    arquivos_por_empresa: dict[str, set[int]] = {}
    for it in itens:
        r = it.resultado or {}
        if it.status == "ok":
            arquivos.append({"arquivo_origem": it.caminho_origem, "status": "ok", "empresa": it.empresa,
                             "linhas": r.get("linhas", 0)})
            if it.empresa and it.empresa != "SANEAGO_ANALITICA":
                acc = por_empresa.setdefault(it.empresa, {
                    "adicionadas": 0, "atualizadas": 0, "duplicadas": 0, "removidas": 0, "descartadas": 0})
                for k in acc:
                    acc[k] += int(r.get(k, 0) or 0)
                arquivos_por_empresa.setdefault(it.empresa, set()).add(it.arquivo_id)
        else:
            arquivos.append({"arquivo_origem": it.caminho_origem, "status": "erro",
                             "erro": it.detalhe or "erro"})

    contas_ok = os.path.exists(caminho_contas_json())
    for empresa, acc in por_empresa.items():
        vistas: dict[int, tuple[bool, dict]] = {}
        for lote in _em_lotes(sorted(arquivos_por_empresa[empresa])):
            for fid, sus, dados in s.execute(
                select(Fatura.id, Fatura.suspeito, Fatura.dados)
                .join(FaturaOrigem, FaturaOrigem.fatura_id == Fatura.id)
                .where(FaturaOrigem.arquivo_id.in_(lote), FaturaOrigem.ativa.is_(True), Fatura.ativa.is_(True))
            ):
                vistas[fid] = (bool(sus), dados or {})
        acc["suspeitas"] = sum(1 for sus, _ in vistas.values() if sus)
        acc["contas_json_encontrado"] = contas_ok
        acc["linhas_sem_correspondencia"] = sum(1 for _, d in vistas.values() if not d.get("UNIDADE JUDICIÁRIA"))
    return {"arquivos": arquivos, "empresas": por_empresa}


def finalizar_job(job_id: str, worker_id: "str | None") -> dict:
    with sessao(escrita=True) as s:
        job = _verificar_dono(s, job_id, worker_id)
        recalcular_desatualizados(s)
        resultado = resumo_do_job(s, job_id)
        job.resultado = resultado
        job.status = "concluido"
        job.concluido_em = agora()
        registrar_evento(s, job_id, "concluido", resultado)
        return resultado


def marcar_job_erro(job_id: str, worker_id: "str | None", mensagem: str) -> None:
    with sessao(escrita=True) as s:
        job = s.get(Job, job_id)
        if job is None or job.status in ("concluido", "erro"):
            return
        if worker_id is not None and job.worker_id not in (None, worker_id):
            return
        job.status = "erro"
        job.erro_mensagem = mensagem
        job.resultado = {"erro": mensagem}
        job.concluido_em = agora()
        registrar_evento(s, job_id, "erro", {"erro": mensagem})


# =========================================================
# Criação de jobs em lote (reextração completa — usado pelo Main.py)
# =========================================================

def _caminho_longo(caminho: str) -> str:
    caminho = os.path.abspath(caminho)
    if os.name == "nt" and not caminho.startswith("\\\\?\\"):
        caminho = "\\\\?\\" + caminho
    return caminho


def listar_pdfs_pasta(pasta: str) -> list[tuple[str, str]]:
    """[(caminho_absoluto, caminho_origem)] de todos os PDFs sob `pasta`
    (recursivo, ordenado). caminho_origem = <nome da pasta>/<relativo>,
    com "/" — o mesmo formato do upload de pasta da UI."""
    base = _caminho_longo(pasta)
    raiz_nome = os.path.basename(os.path.normpath(pasta))
    achados = []
    for raiz, _dirs, arquivos in os.walk(base):
        for nome in arquivos:
            if nome.lower().endswith(".pdf"):
                completo = os.path.join(raiz, nome)
                rel = os.path.relpath(completo, base).replace("\\", "/")
                achados.append((completo, f"{raiz_nome}/{rel}"))
    return sorted(achados, key=lambda x: x[1])


def _texto_irmao_util(caminho_pdf: str) -> "tuple[bytes, bool] | None":
    from extratores.ocr_fallback import eh_texto_ocr, eh_texto_util

    caminho_txt = os.path.splitext(caminho_pdf)[0] + ".txt"
    if not eh_texto_util(caminho_txt):
        return None
    with open(_caminho_longo(caminho_txt), "rb") as f:
        return f.read(), eh_texto_ocr(caminho_txt)


def job_de_arquivos(fontes, usar_txt_existente: bool, parametros: dict, tipo: str = "reextracao",
                    progresso=None) -> "str | None":
    """fontes: iterável de (caminho_origem, ler_pdf(), ler_txt_irmao() | None).
    Sobe os blobs pro storage, registra arquivos/origens e cria UM job com
    tudo. Devolve o job_id (None se não houver PDF)."""
    armazenamento = obter_armazenamento()
    registrados = []  # (sha, tamanho, caminho_origem, txt)
    for i, (caminho_origem, ler_pdf, ler_txt) in enumerate(fontes, start=1):
        conteudo = ler_pdf()
        sha = armazenar_blob(conteudo, armazenamento)
        txt = ler_txt() if (usar_txt_existente and ler_txt is not None) else None
        registrados.append((sha, len(conteudo), caminho_origem, txt))
        if progresso and i % 100 == 0:
            progresso(f"   {i} arquivo(s) enviados ao storage...")
    if not registrados:
        return None
    with sessao(escrita=True) as s:
        itens = []
        for sha, tamanho, caminho_origem, txt in registrados:
            arq, origem = registrar_arquivo(s, sha, tamanho, caminho_origem)
            if txt is not None and not parametros.get("refazer_texto"):
                registrar_texto_existente(s, arq, txt[0], txt[1], armazenamento)
            itens.append((arq, origem, caminho_origem))
        job = criar_job(s, itens, tipo=tipo, parametros=parametros)
        return job.id


def job_de_pasta(pasta: str, usar_txt_existente: bool = True, parametros: "dict | None" = None,
                 progresso=None) -> "str | None":
    def fontes():
        for completo, caminho_origem in listar_pdfs_pasta(pasta):
            def ler_pdf(p=completo):
                with open(p, "rb") as f:
                    return f.read()
            yield caminho_origem, ler_pdf, (lambda p=completo: _texto_irmao_util(p))
    return job_de_arquivos(fontes(), usar_txt_existente, parametros or {}, progresso=progresso)


def job_de_prefixo_storage(prefixo: str, usar_txt_existente: bool = True, parametros: "dict | None" = None,
                           progresso=None) -> "str | None":
    """Mesma coisa, lendo de um prefixo do próprio bucket (ex.: a pasta
    dados_entrada/ copiada pra s3://<bucket>/entrada/ com `mc cp -r`) — é o
    caminho pra reextração completa dentro do container, onde os PDFs não
    estão em disco. caminho_origem = chave sem o prefixo."""
    from extratores.ocr_fallback import MARCADOR_OCR

    armazenamento = obter_armazenamento()
    prefixo = prefixo.lstrip("/")
    chaves = armazenamento.listar(prefixo)
    conjunto = set(chaves)

    def ler_txt_de(chave_txt):
        conteudo = armazenamento.ler(chave_txt)
        texto = conteudo.decode("utf-8", errors="replace")
        if len(texto.strip()) < 50 or "�" in texto:
            return None
        return conteudo, texto.split("\n", 1)[0].strip() == MARCADOR_OCR

    def fontes():
        for chave in sorted(chaves):
            if not chave.lower().endswith(".pdf"):
                continue
            chave_txt = chave[:-4] + ".txt"
            ler_txt = (lambda c=chave_txt: ler_txt_de(c)) if chave_txt in conjunto else None
            yield chave[len(prefixo):].lstrip("/"), (lambda c=chave: armazenamento.ler(c)), ler_txt
    return job_de_arquivos(fontes(), usar_txt_existente, parametros or {}, progresso=progresso)


def job_reprocessar_banco(empresa: "str | None" = None, parametros: "dict | None" = None) -> "str | None":
    """Job com TODOS os arquivos já no banco (opcionalmente só de uma
    distribuidora). Sem --forcar, só os arquivos cujo extrator mudou de
    versão são reextraídos de fato; o resto é pulado na hora."""
    with sessao(escrita=True) as s:
        consulta = select(ArquivoUpload).order_by(ArquivoUpload.id)
        if empresa:
            consulta = consulta.where(ArquivoUpload.empresa == empresa)
        itens = []
        for arq in s.scalars(consulta):
            origem = _origem_principal(s, arq.id, arq.empresa) if arq.empresa else s.scalar(
                select(ArquivoOrigem).where(ArquivoOrigem.arquivo_id == arq.id).order_by(ArquivoOrigem.id).limit(1))
            if origem is not None:
                itens.append((arq, origem, origem.caminho_origem))
        if not itens:
            return None
        return criar_job(s, itens, tipo="reextracao", parametros=parametros or {}).id


# =========================================================
# Leitura (API / exportação)
# =========================================================

def dataframe_empresa(s, empresa: str) -> "pd.DataFrame | None":
    linhas = [d for (d,) in s.execute(
        select(Fatura.dados).where(Fatura.empresa == empresa, Fatura.ativa.is_(True)).order_by(Fatura.id))]
    if not linhas:
        return None
    df = pd.DataFrame(linhas)
    for col in df.columns:
        if col.startswith("VALOR_") or col in ("CONSUMO_M3", "BASE_CALCULO"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "SUSPEITO" in df.columns:
        df["SUSPEITO"] = df["SUSPEITO"].fillna(False).astype(bool)
    return df


def ordenar_colunas(colunas) -> list[str]:
    presentes = list(dict.fromkeys(colunas))
    fixas = [c for c in COLUNAS_CSV if c in presentes]
    return fixas + sorted(c for c in presentes if c not in COLUNAS_CSV)


def exportar_csv_empresa(s, empresa: str) -> "bytes | None":
    df = dataframe_empresa(s, empresa)
    if df is None:
        return None
    df = df[ordenar_colunas(df.columns)]
    return df.to_csv(sep=";", index=False).encode("utf-8-sig")
