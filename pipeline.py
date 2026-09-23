"""Lógica de pipeline compartilhada entre o modo CLI (Main.py, varredura de
pastas), a API/UI (api.py) e o worker (worker.py, via ingestao.py). Mantém
num único lugar o roteamento por distribuidora, o cruzamento
SANEAGO+analítica, o enriquecimento via contas.json, a marcação de
suspeitas e a versão de código de cada extrator — pra nenhum ponto de
entrada divergir no que já foi corrigido aqui (ver CONTA_DV normalizado).
A gravação em si (banco + storage, dedup, versões) fica em ingestao.py."""

import functools
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime

import pandas as pd

from banco.config import caminho_contas_json
from extratores.saneago import carregar_flags_contas, carregar_mapa_contas, extrair_saneago
from extratores.saneago_analitica import extrair_saneago_analitica
from extratores.sae import extrair_sae
from extratores.codego import extrair_codego
from extratores.aguas_ipameri import extrair_aguas_ipameri
from extratores.buriti_alegre import extrair_buriti_alegre
from extratores.demae import extrair_demae
from extratores.saae_abadiania import extrair_saae_abadiania
from extratores.saae_corumba import extrair_saae_corumba
from extratores.saae_mineiros import extrair_saae_mineiros

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))

# =========================================================
# ROTEAMENTO POR DISTRIBUIDORA
# =========================================================

# Ordem importa: "saneago"+"analitica" tem que vir antes de "saneago"
# sozinho, senão o arquivo analítico nunca bate na primeira condição.
PADROES_DISTRIBUIDORA = [
    (("saneago", "analitica"), "SANEAGO_ANALITICA"),
    (("saneago",), "SANEAGO"),
    (("codego",), "CODEGO"),
    (("ipameri",), "IPAMERI"),
    (("buriti alegre",), "BURITI_ALEGRE"),
    (("demae",), "DEMAE"),
    (("abadiania",), "SAAE_ABADIANIA"),
    (("corumba",), "SAAE_CORUMBA"),
    (("mineiros",), "SAAE_MINEIROS"),
    (("sanesc",), "SANESC"),
    (("chesp",), "CHESP"),
]

def _extrair_saneago_com_contas(caminho, is_ocr):
    """Chama extrair_saneago passando o cadastro de contas lido de
    CONTAS_JSON_PATH — sem isso o extrator cai no default "contas.json"
    relativo ao cwd (que no container/worker não é a raiz do projeto). Só o
    caminho OCR usa esses mapas; a lógica de extração não muda."""
    if not is_ocr:
        return extrair_saneago(caminho, is_ocr=False)
    caminho_json = caminho_contas_json()
    return extrair_saneago(
        caminho,
        is_ocr=True,
        contas_conhecidas=carregar_mapa_contas(caminho_json),
        flags_contas=carregar_flags_contas(caminho_json),
    )


EXTRATORES_POR_EMPRESA = {
    "SANEAGO_ANALITICA": lambda caminho, is_ocr: extrair_saneago_analitica(caminho),
    "SANEAGO": _extrair_saneago_com_contas,
    "CODEGO": lambda caminho, is_ocr: extrair_codego(caminho),
    "IPAMERI": lambda caminho, is_ocr: extrair_aguas_ipameri(caminho),
    "BURITI_ALEGRE": lambda caminho, is_ocr: extrair_buriti_alegre(caminho),
    "DEMAE": lambda caminho, is_ocr: extrair_demae(caminho),
    "SAAE_ABADIANIA": lambda caminho, is_ocr: extrair_saae_abadiania(caminho),
    "SAAE_CORUMBA": lambda caminho, is_ocr: extrair_saae_corumba(caminho),
    "SAAE_MINEIROS": lambda caminho, is_ocr: extrair_saae_mineiros(caminho),
    "SAE": lambda caminho, is_ocr: extrair_sae(caminho),
}

EXTRATORES_NAO_IMPLEMENTADOS = {"SANESC", "CHESP"}

EMPRESAS_CONHECIDAS = list(EXTRATORES_POR_EMPRESA) + sorted(EXTRATORES_NAO_IMPLEMENTADOS)

# Módulo-fonte de cada extrator — é o conteúdo desse arquivo que define a
# "versão do extrator" gravada em cada fatura (ver versao_extrator).
MODULO_POR_EMPRESA = {
    "SANEAGO_ANALITICA": "saneago_analitica",
    "SANEAGO": "saneago",
    "CODEGO": "codego",
    "IPAMERI": "aguas_ipameri",
    "BURITI_ALEGRE": "buriti_alegre",
    "DEMAE": "demae",
    "SAAE_ABADIANIA": "saae_abadiania",
    "SAAE_CORUMBA": "saae_corumba",
    "SAAE_MINEIROS": "saae_mineiros",
    "SAE": "sae",
}

# Suba este número quando mudar uma regra GENÉRICA que altera o resultado da
# extração de todas as distribuidoras (filtrar_linhas_vazias, normalização
# da chave da fatura em ingestao.py...). Mudança dentro de um extrator não
# precisa: o hash do arquivo do extrator já muda sozinho.
VERSAO_REGRAS_EXTRACAO = "1"


@functools.lru_cache(maxsize=64)
def _sha_arquivo(caminho: str, _mtime: float, _tamanho: int) -> str:
    with open(caminho, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _sha_de(caminho: str) -> str:
    try:
        st = os.stat(caminho)
    except OSError:
        return "ausente"
    return _sha_arquivo(caminho, st.st_mtime, st.st_size)


def _sha_contas_para_saneago(caminho_json: str) -> str:
    """Hash só das colunas do contas.json que o extrator SANEAGO usa (no
    caminho OCR: CONTA/CONTA_DV e flags) — editar endereço ou unidade não
    deve forçar reextração."""
    if not os.path.exists(caminho_json):
        return "sem-contas"
    st = os.stat(caminho_json)
    return _sha_contas_cache(caminho_json, st.st_mtime, st.st_size)


@functools.lru_cache(maxsize=8)
def _sha_contas_cache(caminho_json: str, _mtime: float, _tamanho: int) -> str:
    with open(caminho_json, encoding="utf-8") as f:
        registros = json.load(f)
    projecao = sorted(
        (str(r.get("CONTA", "")), str(r.get("CONTA_DV", "")), bool(r.get("AGUA", False)),
         bool(r.get("ESGOTO", False)), bool(r.get("SMRSU", False)))
        for r in registros
    )
    return hashlib.sha256(json.dumps(projecao).encode()).hexdigest()


def versao_extrator(empresa: str) -> str:
    """Identificador da versão do código que extrai `empresa`: hash do
    arquivo-fonte do extrator + VERSAO_REGRAS_EXTRACAO. Muda sozinho a cada
    correção num extrator (não depende de git, que não vai pra imagem
    Docker). Pra SANEAGO inclui também o hash do trecho do contas.json que o
    extrator consulta no caminho OCR, já que o resultado depende dele."""
    modulo = MODULO_POR_EMPRESA[empresa]
    caminho = os.path.join(PASTA_BASE, "extratores", f"{modulo}.py")
    base = hashlib.sha256(f"{_sha_de(caminho)}|{VERSAO_REGRAS_EXTRACAO}".encode()).hexdigest()[:12]
    versao = f"{modulo}:{base}"
    if empresa == "SANEAGO":
        versao += "+contas:" + _sha_contas_para_saneago(caminho_contas_json())[:8]
    return versao


def versao_texto() -> str:
    """Versão da camada de texto (pdftotext + OCR) — gravada junto do texto
    em cache, pra saber quais textos vieram de um código de OCR antigo."""
    pasta = os.path.join(PASTA_BASE, "extratores")
    partes = [_sha_de(os.path.join(pasta, n)) for n in ("pdftotext_fallback.py", "ocr_fallback.py")]
    return hashlib.sha256("|".join(partes).encode()).hexdigest()[:12]


def normalizar_texto(texto: str) -> str:
    """Remove acentos e coloca em minúsculas para facilitar a correspondência."""
    texto = str(texto).lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def identificar_distribuidora(alvo_busca: str) -> str | None:
    """Identifica a distribuidora a partir de um texto-alvo (nome do arquivo,
    opcionalmente concatenado com o caminho da pasta — é o chamador que
    decide o que entra nesse texto)."""
    alvo = normalizar_texto(alvo_busca)
    for palavras, empresa in PADROES_DISTRIBUIDORA:
        if all(p in alvo for p in palavras):
            return empresa
    if "sae" in alvo and "saae" not in alvo:
        return "SAE"
    return None


# =========================================================
# CRUZAMENTO SANEAGO + ANALÍTICA (HIDRÔMETROS)
# =========================================================

def mesclar_saneago_com_analitica(df_saneago: pd.DataFrame, df_analitica: pd.DataFrame) -> pd.DataFrame:
    """Usa a fatura analítica (hidrômetros) pra preencher NUM_HIDROMETRO das
    faturas SANEAGO normais, casando por CONTA_DV + MES_ANO_REF, com
    fallback pro hidrômetro mais recente da conta quando não há analítica
    exata daquele mês."""
    df_saneago = df_saneago.copy()
    df_analitica = df_analitica.copy()
    if "MES_ANO_REF" not in df_analitica.columns:
        df_analitica["MES_ANO_REF"] = ""

    for df in (df_saneago, df_analitica):
        df["CONTA_DV"] = df["CONTA_DV"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
        df["MES_ANO_REF"] = df["MES_ANO_REF"].astype(str).str.strip()

    df_analitica["DATA_ORDENACAO"] = pd.to_datetime(df_analitica["MES_ANO_REF"], format="%m/%Y", errors="coerce")
    hidrometros_recentes = (
        df_analitica.sort_values(by=["CONTA_DV", "DATA_ORDENACAO"], ascending=[True, False])
        .drop_duplicates(subset=["CONTA_DV"], keep="first")
    )
    mapa_hidrometros = hidrometros_recentes.set_index("CONTA_DV")["NUM_HIDROMETRO_EXTRAIDO"].to_dict()

    df_analitica = df_analitica.drop(columns=["DATA_ORDENACAO"]).drop_duplicates(subset=["CONTA_DV", "MES_ANO_REF"])
    df_cruzado = pd.merge(
        df_saneago,
        df_analitica[["CONTA_DV", "MES_ANO_REF", "NUM_HIDROMETRO_EXTRAIDO"]],
        on=["CONTA_DV", "MES_ANO_REF"],
        how="left",
    )
    df_cruzado["NUM_HIDROMETRO"] = df_cruzado["NUM_HIDROMETRO_EXTRAIDO"]

    vazios = df_cruzado["NUM_HIDROMETRO"].isna() | df_cruzado["NUM_HIDROMETRO"].isin(["", "nan"])
    df_cruzado.loc[vazios, "NUM_HIDROMETRO"] = df_cruzado.loc[vazios, "CONTA_DV"].map(mapa_hidrometros)

    return df_cruzado.drop(columns=["NUM_HIDROMETRO_EXTRAIDO"])


# =========================================================
# ENRIQUECIMENTO VIA contas.json
# =========================================================

COLUNAS_ENRIQUECIMENTO = ["UNIDADE JUDICIÁRIA", "ENDEREÇO", "DISTRIBUIDORA"]
COLUNAS_FLAG = ["AGUA", "ESGOTO", "SMRSU"]


def normalizar_conta_dv(valor) -> str:
    """Chave canônica de CONTA_DV pra casar com o contas.json mesmo quando o
    extrator usa separador diferente do "número espaço DV" do JSON (ex.:
    "9945-7" da Ipameri, "0001255.2" da SAAE Abadiânia, "030285" com zero à
    esquerda da SAAE Mineiros). Fica só nos dígitos e tira zeros à esquerda
    — nunca é usada como CONTA_DV exibido/exportado, só como chave de merge.
    """
    digitos = re.sub(r"\D", "", str(valor) if valor is not None else "")
    if not digitos:
        return ""
    return digitos.lstrip("0") or "0"


def enriquecer_com_contas_json(df: pd.DataFrame, caminho_json: str) -> tuple[pd.DataFrame, dict]:
    """Cruza df com contas.json (metadados da unidade + flags AGUA/ESGOTO/
    SMRSU) e redistribui os valores financeiros conforme essas flags —
    conservador: nunca desloca um valor de uma categoria pra outra (isso é
    responsabilidade do extrator), só zera um valor que caiu numa categoria
    que a conta não tem cadastrada.

    Sempre devolve o DataFrame com o conjunto completo de colunas de
    enriquecimento (mesmo se o contas.json não existir, ou a conta não tiver
    correspondência) — é isso que evita o desalinhamento de colunas no CSV
    incremental (salvar_incremental) quando o JSON está ausente.

    Retorna (df_enriquecido, info), onde info tem "contas_json_encontrado" e
    "linhas_sem_correspondencia".
    """
    df = df.copy()
    contas_json_encontrado = os.path.exists(caminho_json)
    sem_correspondencia = len(df)

    df["_CHAVE_CONTA"] = df["CONTA_DV"].map(normalizar_conta_dv)

    if contas_json_encontrado:
        with open(caminho_json, encoding="utf-8") as f:
            registros = json.load(f)
        df_info = pd.DataFrame(registros)
        colunas_presentes = [c for c in ["CONTA_DV", *COLUNAS_ENRIQUECIMENTO, *COLUNAS_FLAG] if c in df_info.columns]
        df_info = df_info[colunas_presentes].copy()

        if "CONTA_DV" in df_info.columns:
            df_info["_CHAVE_CONTA"] = df_info["CONTA_DV"].map(normalizar_conta_dv)
            df_info = df_info.drop(columns=["CONTA_DV"]).drop_duplicates(subset=["_CHAVE_CONTA"], keep="first")
            df = pd.merge(df, df_info, on="_CHAVE_CONTA", how="left")
            if COLUNAS_ENRIQUECIMENTO[0] in df_info.columns:
                sem_correspondencia = int(df[COLUNAS_ENRIQUECIMENTO[0]].isna().sum())

    df = df.drop(columns=["_CHAVE_CONTA"])

    for col in COLUNAS_ENRIQUECIMENTO:
        df[col] = df[col].fillna("") if col in df.columns else ""
    for col in COLUNAS_FLAG:
        df[col] = df[col].fillna(False).astype(bool) if col in df.columns else False

    # VALOR_OUTRAS_TAXAS nunca é tocada por flag nenhuma (ver comentário na
    # função abaixo) — só garante que a coluna sempre existe, com o mesmo
    # motivo do fillna de COLUNAS_ENRIQUECIMENTO/COLUNAS_FLAG acima.
    if "VALOR_OUTRAS_TAXAS" not in df.columns:
        df["VALOR_OUTRAS_TAXAS"] = 0.0
    df["VALOR_OUTRAS_TAXAS"] = df["VALOR_OUTRAS_TAXAS"].fillna(0.0)

    def corrigir_distribuicao_financeira(row):
        if not (row.get("AGUA", False) or row.get("ESGOTO", False) or row.get("SMRSU", False)):
            return row.get("VALOR_AGUA", 0.0), row.get("VALOR_ESGOTO", 0.0), row.get("VALOR_TAXAS_EXTRAS", 0.0)
        agua = row.get("VALOR_AGUA", 0.0) if row.get("AGUA", False) else 0.0
        esgoto = row.get("VALOR_ESGOTO", 0.0) if row.get("ESGOTO", False) else 0.0
        # A flag SMRSU do contas.json só é confiável pra SANEAGO, cuja
        # fatura tem uma coluna "SMRSU" própria e nomeada (extrair_saneago
        # captura o valor exato dessa coluna). Os outros extratores usam
        # VALOR_TAXAS_EXTRAS/VALOR_OUTRAS_TAXAS como resíduo genérico
        # (total - água - esgoto) pra representar qualquer taxa fixa da
        # fatura que não seja água/esgoto (ex: "Tarifa Básica Operacional"
        # da SAAE_CORUMBA, "SERVIÇO BÁSICO ÁGUA" da SAE, "TARIFA BÁSICA" da
        # BURITI_ALEGRE/IPAMERI) — não necessariamente SMRSU. Zerar esse
        # resíduo pela flag SMRSU=False descartava dinheiro real dessas
        # distribuidoras. Por isso só SANEAGO zera por essa flag; as demais
        # sempre mantêm VALOR_OUTRAS_TAXAS como veio do extrator.
        smrsu = row.get("VALOR_TAXAS_EXTRAS", 0.0) if row.get("SMRSU", False) else 0.0
        return agua, esgoto, smrsu

    df[["VALOR_AGUA", "VALOR_ESGOTO", "VALOR_TAXAS_EXTRAS"]] = df.apply(
        lambda row: pd.Series(corrigir_distribuicao_financeira(row)), axis=1
    )
    df["BASE_CALCULO"] = (df["VALOR_AGUA"] + df["VALOR_ESGOTO"]).round(2)

    info = {
        "contas_json_encontrado": contas_json_encontrado,
        "linhas_sem_correspondencia": sem_correspondencia,
    }
    return df, info


def filtrar_linhas_vazias(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Remove linhas quase vazias (sem número de fatura, sem valor total e
    sem consumo) — mitigação genérica pro bloco residual que o corte "à
    tesoura" de alguns extratores (AUTENTICAÇÃO NO VERSO etc.) às vezes
    deixa passar contendo só a CONTA_DV do canhoto. Retorna (df, descartadas).
    """
    tem_fatura = df.get("NUM_FATURA", pd.Series([""] * len(df), index=df.index)).astype(str).str.strip().ne("")
    tem_valor = df.get("VALOR_TOTAL", pd.Series([0.0] * len(df), index=df.index)).fillna(0) != 0
    tem_consumo = df.get("CONSUMO_M3", pd.Series([0.0] * len(df), index=df.index)).fillna(0) != 0
    mantem = tem_fatura | tem_valor | tem_consumo
    descartadas = int((~mantem).sum())
    return df[mantem].reset_index(drop=True), descartadas


def marcar_suspeitas(df: pd.DataFrame) -> pd.DataFrame:
    """Marca SUSPEITO=True quando as taxas (SMRSU ou outras) saíram negativas
    ou a soma ÁGUA+ESGOTO+TAXAS_EXTRAS+OUTRAS_TAXAS diverge do TOTAL em mais
    de 5 centavos — indício de que algum regex do extrator não capturou o
    valor certo naquela fatura. Só sinaliza pra revisão manual, não corrige
    nada."""
    df = df.copy()
    outras_taxas = df.get("VALOR_OUTRAS_TAXAS", 0.0)
    esperado = df["VALOR_AGUA"] + df["VALOR_ESGOTO"] + df["VALOR_TAXAS_EXTRAS"] + outras_taxas
    diverge = (esperado - df["VALOR_TOTAL"]).abs() > 0.05
    negativo = (df["VALOR_TAXAS_EXTRAS"] < 0) | (outras_taxas < 0)
    df["SUSPEITO"] = (diverge | negativo).fillna(False)
    return df


# =========================================================
# GRAVAÇÃO INCREMENTAL ANTI-DUPLICATA — LEGADO (CSV)
# A persistência oficial agora é o banco (ingestao.py). salvar_incremental
# e as leituras de CSV abaixo ficam só pra comparar/importar a saída antiga
# em dados_saida/; nenhum ponto de entrada (Main/api/worker) grava CSV mais.
# =========================================================

DTYPES_CSV = {"NUM_FATURA": str, "MES_ANO_REF": str, "CONTA_DV": str}


def salvar_incremental(df_novo_lote: pd.DataFrame, caminho_csv: str) -> dict:
    """Grava df_novo_lote em caminho_csv, ignorando faturas já salvas (chave
    NUM_FATURA|MES_ANO_REF|CONTA_DV). Se o arquivo já existir, reindexa o
    lote novo pras mesmas colunas (mesma ordem) do cabeçalho já gravado —
    trava de segurança contra desalinhamento de colunas no append, mesmo que
    algum passo anterior da pipeline mude o conjunto de colunas produzidas.
    Retorna {"adicionadas", "duplicadas", "arquivo_novo"}.
    """
    df_novo_lote = df_novo_lote.copy()
    cols_chave = ["NUM_FATURA", "MES_ANO_REF", "CONTA_DV"]
    arquivo_existe = os.path.exists(caminho_csv)

    if not arquivo_existe:
        df_novo_lote.to_csv(caminho_csv, mode="w", index=False, sep=";", encoding="utf-8-sig", header=True)
        return {"adicionadas": len(df_novo_lote), "duplicadas": 0, "arquivo_novo": True}

    df_antigo = pd.read_csv(caminho_csv, sep=";", dtype=DTYPES_CSV)
    df_antigo[cols_chave] = df_antigo[cols_chave].fillna("")
    df_novo_lote[cols_chave] = df_novo_lote[cols_chave].fillna("")

    chaves_antigas = set(
        df_antigo["NUM_FATURA"].astype(str) + "|" +
        df_antigo["MES_ANO_REF"].astype(str) + "|" +
        df_antigo["CONTA_DV"].astype(str)
    )
    chaves_novas = (
        df_novo_lote["NUM_FATURA"].astype(str) + "|" +
        df_novo_lote["MES_ANO_REF"].astype(str) + "|" +
        df_novo_lote["CONTA_DV"].astype(str)
    )
    df_inedito = df_novo_lote[~chaves_novas.isin(chaves_antigas)].copy()
    qtd_duplicadas = len(df_novo_lote) - len(df_inedito)

    for col in df_antigo.columns:
        if col not in df_inedito.columns:
            df_inedito[col] = ""
    df_inedito = df_inedito[df_antigo.columns.tolist()]

    if len(df_inedito) > 0:
        df_inedito.to_csv(caminho_csv, mode="a", index=False, sep=";", encoding="utf-8-sig", header=False)

    return {"adicionadas": len(df_inedito), "duplicadas": qtd_duplicadas, "arquivo_novo": False}


# =========================================================
# LEITURA DOS CSVs CONSOLIDADOS (aba "Dados" / "Dashboards" da UI)
# =========================================================

def linha_para_dados(linha: pd.Series) -> dict:
    """Converte uma linha de DataFrame pra um dict serializável em JSON
    (valores numpy/NaN não são aceitos pelo encoder do FastAPI como estão)."""
    dados = {}
    for chave, valor in linha.items():
        if pd.isna(valor):
            dados[chave] = None
        elif hasattr(valor, "item"):
            dados[chave] = valor.item()
        else:
            dados[chave] = valor
    return dados


def listar_empresas_com_dados(pasta_saida: str) -> list[dict]:
    """Lista as distribuidoras que já têm CSV consolidado em pasta_saida,
    com contagem de linhas e data da última modificação — alimenta o
    seletor de empresa da aba "Dados" da UI."""
    empresas = []
    if not os.path.isdir(pasta_saida):
        return empresas
    for empresa in EMPRESAS_CONHECIDAS:
        caminho_csv = os.path.join(pasta_saida, f"banco_dados_{empresa.lower()}.csv")
        if not os.path.exists(caminho_csv):
            continue
        with open(caminho_csv, encoding="utf-8-sig") as f:
            total_linhas = max(sum(1 for _ in f) - 1, 0)  # -1 do cabeçalho
        empresas.append({
            "empresa": empresa,
            "linhas": total_linhas,
            "ultima_modificacao": datetime.fromtimestamp(os.path.getmtime(caminho_csv)).isoformat(timespec="seconds"),
        })
    return empresas


def carregar_dataframe_empresa(pasta_saida: str, empresa: str) -> "pd.DataFrame | None":
    """Lê o CSV consolidado de uma distribuidora. CSVs gravados antes da
    coluna SUSPEITO existir não a têm no arquivo — calcula na hora com
    marcar_suspeitas em vez de exigir migração do CSV. Retorna None se o
    arquivo não existir."""
    caminho_csv = os.path.join(pasta_saida, f"banco_dados_{empresa.lower()}.csv")
    if not os.path.exists(caminho_csv):
        return None
    df = pd.read_csv(caminho_csv, sep=";", dtype=DTYPES_CSV, encoding="utf-8-sig")
    if "SUSPEITO" in df.columns:
        df["SUSPEITO"] = df["SUSPEITO"].fillna(False).astype(bool)
    else:
        df = marcar_suspeitas(df)
    return df
