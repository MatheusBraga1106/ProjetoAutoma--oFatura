import os
import tempfile
import unicodedata
import uuid
from typing import List

import pandas as pd
from fastapi import FastAPI, UploadFile, File

from extratores.saneago import extrair_saneago
from extratores.saneago_analitica import extrair_saneago_analitica
from extratores.sae import extrair_sae
from extratores.codego import extrair_codego
from extratores.aguas_ipameri import extrair_aguas_ipameri
from extratores.buriti_alegre import extrair_buriti_alegre
from extratores.demae import extrair_demae
from extratores.saae_abadiania import extrair_saae_abadiania
from extratores.saae_corumba import extrair_saae_corumba
from extratores.saae_mineiros import extrair_saae_mineiros
from extratores.ocr_fallback import eh_texto_util, gerar_txt_via_ocr
from extratores.pdftotext_fallback import converter_pdf_para_txt

app = FastAPI(title="Extrator de Faturas de Água")

MANIFEST = {
    "id": "faturas-agua",
    "nome": "Extrator de Faturas de Água",
    "dominio": "agua",
    "versao": "1.0.0",
    "formatos_aceitos": ["pdf"],
    "campos_saida": [
        "CONCESSIONARIA", "NUM_FATURA", "MES_ANO_REF", "VENCIMENTO", "CONTA_DV",
        "NUM_HIDROMETRO", "NOME_CLIENTE", "LOGRADOURO", "CONSUMO_M3",
        "VALOR_AGUA", "VALOR_ESGOTO", "VALOR_TAXAS_EXTRAS", "VALOR_TOTAL",
    ],
}

# Ordem importa: "saneago"+"analitica" tem que vir antes de "saneago" sozinho,
# senão o arquivo analítico nunca bate na primeira condição.
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

EXTRATORES_POR_EMPRESA = {
    "SANEAGO_ANALITICA": lambda caminho, is_ocr: extrair_saneago_analitica(caminho),
    "SANEAGO": lambda caminho, is_ocr: extrair_saneago(caminho, is_ocr=is_ocr),
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


def normalizar_texto(texto: str) -> str:
    texto = str(texto).lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def identificar_distribuidora(nome_arquivo: str) -> str | None:
    alvo = normalizar_texto(nome_arquivo)
    for palavras, empresa in PADROES_DISTRIBUIDORA:
        if all(p in alvo for p in palavras):
            return empresa
    if "sae" in alvo and "saae" not in alvo:
        return "SAE"
    return None


def texto_de(caminho_pdf: str) -> tuple[str, bool]:
    """Garante um .txt pareado, tentando pdftotext e caindo pro OCR se precisar.
    Devolve (caminho_txt, veio_de_ocr)."""
    caminho_txt = os.path.splitext(caminho_pdf)[0] + ".txt"
    converter_pdf_para_txt(caminho_pdf, caminho_txt)
    if eh_texto_util(caminho_txt):
        return caminho_txt, False
    gerar_txt_via_ocr(caminho_pdf, caminho_txt)
    return caminho_txt, True


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


def mesclar_saneago_com_analitica(df_saneago: pd.DataFrame, df_analitica: pd.DataFrame) -> pd.DataFrame:
    """Mesma lógica de cruzamento do Main.py: usa a fatura analítica (hidrômetros)
    pra preencher NUM_HIDROMETRO das faturas SANEAGO normais, casando por
    CONTA_DV + MES_ANO_REF, com fallback pro hidrômetro mais recente da conta
    quando não há analítica exata daquele mês."""
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/manifest")
def manifest():
    return MANIFEST


@app.post("/extrair")
def extrair(arquivos: List[UploadFile] = File(...)):
    resultados = []
    lotes_saneago = []  # [(nome_arquivo, dataframe)]
    lotes_analitica = []  # [(nome_arquivo, dataframe)]

    with tempfile.TemporaryDirectory() as pasta_tmp:
        for arquivo in arquivos:
            empresa = identificar_distribuidora(arquivo.filename)

            if empresa is None:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": "Não foi possível identificar a distribuidora deste arquivo.",
                })
                continue

            if empresa in EXTRATORES_NAO_IMPLEMENTADOS:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": f"Extrator para {empresa} ainda não foi implementado.",
                })
                continue

            caminho_pdf = os.path.join(pasta_tmp, f"{uuid.uuid4().hex}_{arquivo.filename}")
            with open(caminho_pdf, "wb") as f:
                f.write(arquivo.file.read())

            try:
                caminho_txt, veio_de_ocr = texto_de(caminho_pdf)
                df = EXTRATORES_POR_EMPRESA[empresa](caminho_txt, veio_de_ocr)
            except Exception as e:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": str(e),
                })
                continue

            if df is None or df.empty:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": "Nenhum dado reconhecido neste arquivo.",
                })
                continue

            if empresa == "SANEAGO_ANALITICA":
                lotes_analitica.append((arquivo.filename, df))
            elif empresa == "SANEAGO":
                lotes_saneago.append((arquivo.filename, df))
            else:
                for _, linha in df.iterrows():
                    resultados.append({
                        "arquivo_origem": arquivo.filename,
                        "status": "ok",
                        "dados": linha_para_dados(linha),
                    })

        if lotes_saneago:
            df_saneago = pd.concat([df for _, df in lotes_saneago], ignore_index=True)
            nomes_saneago = [nome for nome, df in lotes_saneago for _ in range(len(df))]

            if lotes_analitica:
                df_analitica = pd.concat([df for _, df in lotes_analitica], ignore_index=True)
                df_saneago = mesclar_saneago_com_analitica(df_saneago, df_analitica)

            for posicao, (_, linha) in enumerate(df_saneago.iterrows()):
                resultados.append({
                    "arquivo_origem": nomes_saneago[posicao],
                    "status": "ok",
                    "dados": linha_para_dados(linha),
                })
        elif lotes_analitica:
            # Sem fatura SANEAGO no mesmo lote pra cruzar — devolve os dados
            # do hidrômetro isolados mesmo assim (não descarta o arquivo).
            for nome, df in lotes_analitica:
                for _, linha in df.iterrows():
                    resultados.append({
                        "arquivo_origem": nome,
                        "status": "ok",
                        "dados": linha_para_dados(linha),
                    })

    return {"resultados": resultados}
