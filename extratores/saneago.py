import pandas as pd
import re
import os
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation


def carregar_mapa_contas(caminho_json="contas.json"):
    """Mapa 'dígitos concatenados' -> 'CONTA_DV formatado', a partir do
    contas.json. Ex: {"16278852": "1627885 2"}. Usado só como rede de
    recuperação pra OCR, onde o Tesseract às vezes cola o dígito
    verificador junto do número da conta (sem o espaço)."""
    if not os.path.exists(caminho_json):
        return {}
    with open(caminho_json, encoding="utf-8") as f:
        registros = json.load(f)
    mapa = {}
    for r in registros:
        conta = r.get("CONTA")
        conta_dv = r.get("CONTA_DV")
        if conta is not None and conta_dv:
            mapa[str(conta)] = conta_dv
    return mapa


def extrair_saneago(caminho_txt, is_ocr=False, contas_conhecidas=None):
    print("  Iniciando extração: SANEAGO PRINCIPAL" + (" (texto via OCR)" if is_ocr else ""))

    if is_ocr and contas_conhecidas is None:
        contas_conhecidas = carregar_mapa_contas()

    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        linhas = f.readlines()

    dados_finais = []
    data_lote = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Memória do Cabeçalho
    meta_n_fatura = ""
    meta_mes_ano = ""
    meta_vencimento = ""
    meta_nome_agrupador = ""
    meta_cod_agrupador = ""
    meta_cod_pagador = ""
    
    # Estados da Máquina
    dentro_da_tabela = False
    capturando_orgao = False
    buffer_nome_orgao = []

    # Posição (fim da string, na linha de cabeçalho) de cada coluna de valor.
    # Recalculadas toda vez que um novo cabeçalho de tabela aparece, porque o
    # relatório repete o cabeçalho a cada página/bloco e o espaçamento entre
    # colunas pode variar levemente de um bloco pro outro.
    col_agua_fim = None
    col_esgoto_fim = None
    col_smrsu_fim = None

    def formatar_para_sql(valor_str):
        if not valor_str: return Decimal('0.00')
        try:
            valor_limpo = str(valor_str).replace('.', '').replace(',', '.')
            return Decimal(valor_limpo)
        except (ValueError, InvalidOperation):
            return Decimal('0.00')

    for i, linha in enumerate(linhas):
        linha_bruta = linha.rstrip('\n')

        if is_ocr:
            # O Tesseract costuma ler o dígito verificador "0" como a letra
            # "O" maiúscula quando ele aparece sozinho logo após o número
            # da conta (ex: "1650402 O" em vez de "1650402 0"). Sem essa
            # correção, TODA conta cujo DV é 0 fica de fora da extração.
            linha_bruta = re.sub(r'^(\s*\d{4,}\s+)O(\s)', r'\g<1>0\2', linha_bruta)

        linha_limpa = linha_bruta.strip()
        linha_upper = linha_limpa.upper()
        
        # =========================================================
        # 1. CAPTURA DE METADADOS
        # =========================================================
        match_fatura = re.search(r'N[º°Oo]?\s*FATURA:\s*(\d+)', linha_bruta, re.IGNORECASE)
        if match_fatura: meta_n_fatura = match_fatura.group(1)

        match_mes = re.search(r'M[ÊE]S/ANO\s*REF:?\s*([\d/]+)', linha_bruta, re.IGNORECASE)
        if match_mes: meta_mes_ano = match_mes.group(1)

        match_venc = re.search(r'VENCIMENTO:\s*([\d/]+)', linha_bruta, re.IGNORECASE)
        if match_venc: meta_vencimento = match_venc.group(1)

        # Mesmo motivo do gatilho da tabela: alguns motores de pdftotext colam
        # "NOME" e "DO" sem espaço ("NOMEDO ÓRGÃO AGRUPADOR").
        if re.search(r'NOME\s*DO\s*ÓRGÃO\s*AGRUPADOR', linha_upper):
            capturando_orgao = True
            buffer_nome_orgao = []
            continue

        if capturando_orgao:
            if not linha_limpa or "CÓD." in linha_upper or "ÓRGÃO" in linha_upper:
                continue
            match_orgao = re.search(r'^(.*?)\s+(\d{3,})\s+(\d{3,})\s*$', linha_limpa)
            if match_orgao:
                buffer_nome_orgao.append(match_orgao.group(1).strip())
                meta_nome_agrupador = " ".join(buffer_nome_orgao).replace("  ", " ")
                meta_cod_agrupador = match_orgao.group(2)
                meta_cod_pagador = match_orgao.group(3)
                capturando_orgao = False
            else:
                buffer_nome_orgao.append(linha_limpa)

        # =========================================================
        # 2. GATILHO E MÁQUINA DE ESTADOS
        # =========================================================
        # GATILHO INFALÍVEL: Procura pelas colunas exatas da mesma linha.
        # Espaçamento entre "CONTA"/"-"/"DV" e "NOME"/"CLIENTE" varia
        # conforme o motor do pdftotext (o xpdf do Git for Windows funde
        # "CONTA-DV" e "NOMECLIENTE" sem espaço; outros motores e o OCR
        # preservam o espaço), então aceitamos zero ou mais espaços ali.
        if re.search(r'CONTA\s*-\s*DV', linha_upper) and re.search(r'NOME\s*CLIENTE', linha_upper):
            dentro_da_tabela = True

            # Formato novo (a partir de ~11/2025): 1 coluna de consumo + 3 de
            # valor (ÁGUA, ESGOTO, SMRSU). Formato antigo: 2 colunas de
            # consumo + 2 de valor, ambas rotuladas "VALOR R$" (a 1ª é água,
            # a 2ª é esgoto — os rótulos "ÁGUA"/"ESGOTO" nesse formato ficam
            # em cima das colunas de CONSUMO, não das de valor).
            #
            # Em texto de OCR não dá pra confiar na posição de caractere:
            # o Tesseract não preserva alinhamento fixo de coluna (uma linha
            # com NOME_CLIENTE mais longo empurra os números pra outra
            # posição de caractere mesmo estando na mesma coluna visual da
            # tabela). Por isso pulamos o cálculo de posição pra OCR e
            # deixamos a atribuição cair na rede de segurança por ordem.
            # As posições de coluna precisam ser medidas na linha BRUTA (sem
            # strip), porque é nela que a posição dos valores é procurada
            # mais abaixo (linha_bruta.find(val, ...)). Medir no texto
            # stripado desalinha tudo quando o cabeçalho tem um recuo
            # diferente da linha de dados (comum entre motores de pdftotext).
            linha_bruta_upper = linha_bruta.upper()

            # Em alguns arquivos os rótulos "ÁGUA ESGOTO SMRSU" saem numa
            # linha à parte (não na mesma linha de "CONTA - DV"), então
            # verificamos também a linha seguinte antes de desistir e cair
            # no formato antigo (2 colunas).
            linha_rotulos_upper = linha_bruta_upper
            if "SMRSU" not in linha_upper and i + 1 < len(linhas):
                proxima_bruta = linhas[i + 1].rstrip('\n')
                if "SMRSU" in proxima_bruta.upper():
                    linha_rotulos_upper = proxima_bruta.upper()

            if is_ocr:
                col_agua_fim = col_esgoto_fim = col_smrsu_fim = None
            elif "SMRSU" in linha_rotulos_upper:
                m_agua = re.search(r'ÁGUA', linha_rotulos_upper)
                m_esgoto = re.search(r'ESGOTO', linha_rotulos_upper)
                m_smrsu = re.search(r'SMRSU', linha_rotulos_upper)
                col_agua_fim = m_agua.end() if m_agua else None
                col_esgoto_fim = m_esgoto.end() if m_esgoto else None
                col_smrsu_fim = m_smrsu.end() if m_smrsu else None
            else:
                posicoes_valor = [m.end() for m in re.finditer(r'VALOR R\$', linha_bruta_upper)]
                col_agua_fim = posicoes_valor[0] if len(posicoes_valor) > 0 else None
                col_esgoto_fim = posicoes_valor[1] if len(posicoes_valor) > 1 else None
                col_smrsu_fim = None
            continue

        if dentro_da_tabela:
            # Desliga ao encontrar os rodapés
            if "TOTAL" in linha_upper or "INFORMAÇÕES" in linha_upper or "RETENÇÕES" in linha_upper or "FATURADO" in linha_upper:
                dentro_da_tabela = False
                continue

            match_conta = re.match(r'^(\d+\s\d)', linha_limpa)
            conta_dv_ocr_recuperada = None

            if not match_conta and is_ocr and contas_conhecidas:
                # O Tesseract às vezes "cola" o dígito verificador no fim do
                # número da conta (perde o espaço). Sem o espaço não dá pra
                # saber por regex onde a conta termina e o DV começa — mas
                # dá pra conferir contra o contas.json, que guarda o mesmo
                # número concatenado no campo "CONTA".
                match_colado = re.match(r'^(\d{5,})\s', linha_limpa)
                if match_colado and match_colado.group(1) in contas_conhecidas:
                    conta_dv_ocr_recuperada = contas_conhecidas[match_colado.group(1)]
                    match_conta = match_colado

            if match_conta:
                conta_dv = conta_dv_ocr_recuperada or match_conta.group(1).strip()
                resto_linha = linha_limpa[match_conta.end():].strip()

                # =========================================================
                # CAUDA NUMÉRICA: captura a sequência final de "espaço(s) +
                # número" na linha, seja qual for a largura do espaçamento.
                # O texto nativo do pdftotext usa espaçamento duplo/largo
                # entre colunas; texto vindo de OCR (Tesseract) normaliza
                # tudo pra espaço único, então não dá pra depender da
                # contagem de espaços aqui — só da posição (é sempre o
                # finalzinho da linha).
                # =========================================================
                match_cauda = re.search(r'((?:\s+[\d.,]+)+)\s*$', resto_linha)
                if match_cauda:
                    texto_bruto = resto_linha[:match_cauda.start()].strip()
                    numeros = match_cauda.group(1).split()
                else:
                    texto_bruto = resto_linha
                    numeros = []

                # Nome/Logradouro: melhor esforço via quebra de espaço duplo.
                # Funciona bem no texto nativo (nome e endereço são
                # separados por um vão largo na tabela); em texto de OCR,
                # sem esse vão preservado, os dois acabam saindo juntos em
                # NOME_CLIENTE — aceitável, o que importa mais são os
                # valores financeiros, tratados à parte acima.
                texto_parts = re.split(r'\s{2,}', texto_bruto) if texto_bruto else []

                if len(texto_parts) == 1:
                    nome = texto_parts[0]
                    logradouro = ""
                elif len(texto_parts) >= 2:
                    nome = texto_parts[0]
                    logradouro = " ".join(texto_parts[1:])
                else:
                    nome = ""
                    logradouro = ""

                # =========================================================
                # INTELIGÊNCIA FINANCEIRA (Heurística da Vírgula)
                # =========================================================
                valores_financeiros = [n for n in numeros if ',' in n]
                valores_inteiros = [n for n in numeros if ',' not in n]

                # O Consumo é o primeiro número inteiro (Sem Vírgula)
                val_consumo = valores_inteiros[0] if len(valores_inteiros) > 0 else "0"

                # =========================================================
                # ATRIBUIÇÃO POR POSIÇÃO DE COLUNA (não por ordem de aparição)
                # Cada valor financeiro é comparado à posição (fim) de cada
                # coluna do cabeçalho mais recente, e vai pra categoria mais
                # próxima. Isso evita que uma coluna em branco no meio (ex:
                # conta sem esgoto, só água+SMRSU) desloque os valores
                # seguintes pra categoria errada.
                # =========================================================
                colunas_valor = [
                    (nome_col, fim) for nome_col, fim in [
                        ('AGUA', col_agua_fim),
                        ('ESGOTO', col_esgoto_fim),
                        ('SMRSU', col_smrsu_fim),
                    ] if fim is not None
                ]

                val_agua, val_esgoto, val_smrsu = "0.00", "0.00", "0.00"

                if colunas_valor:
                    cursor_busca = 0
                    for val in valores_financeiros:
                        pos = linha_bruta.find(val, cursor_busca)
                        if pos == -1:
                            continue
                        pos_fim = pos + len(val)
                        cursor_busca = pos_fim
                        categoria = min(colunas_valor, key=lambda c: abs(pos_fim - c[1]))[0]
                        if categoria == 'AGUA':
                            val_agua = val
                        elif categoria == 'ESGOTO':
                            val_esgoto = val
                        elif categoria == 'SMRSU':
                            val_smrsu = val
                else:
                    # Cabeçalho não reconhecido: mantém o comportamento antigo
                    # (ordem de aparição) como rede de segurança.
                    val_agua = valores_financeiros[0] if len(valores_financeiros) > 0 else "0.00"
                    val_esgoto = valores_financeiros[1] if len(valores_financeiros) > 1 else "0.00"
                    val_smrsu = valores_financeiros[2] if len(valores_financeiros) > 2 else "0.00"

                # Matemática Absoluta
                consumo_f = formatar_para_sql(val_consumo)
                agua_f = formatar_para_sql(val_agua)
                esgoto_f = formatar_para_sql(val_esgoto)
                smrsu_f = formatar_para_sql(val_smrsu)
                
                base_calculo_f = agua_f + esgoto_f
                valor_total_f = agua_f + esgoto_f + smrsu_f
                
                dados_finais.append({
                    "CONCESSIONARIA": "SANEAGO", 
                    "NUM_FATURA": meta_n_fatura,
                    "MES_ANO_REF": meta_mes_ano,
                    "VENCIMENTO": meta_vencimento,
                    "COD_ORGAO_AGRUPADOR": meta_cod_agrupador,
                    "COD_ORGAO_PAGADOR": meta_cod_pagador,
                    "NOME_ORGAO_AGRUPADOR": meta_nome_agrupador,
                    "CONTA_DV": conta_dv,
                    "NUM_HIDROMETRO": "", 
                    "NOME_CLIENTE": nome,
                    "LOGRADOURO": logradouro,
                    "CONSUMO_M3": float(consumo_f),
                    "VALOR_AGUA": float(agua_f),
                    "VALOR_ESGOTO": float(esgoto_f),
                    "BASE_CALCULO": float(base_calculo_f), 
                    "VALOR_TAXAS_EXTRAS": float(smrsu_f), 
                    "VALOR_TOTAL": float(valor_total_f),
                    "DATA_PROCESSAMENTO": data_lote
                })

    if dados_finais:
        df = pd.DataFrame(dados_finais)
        print(f"✅ SANEAGO: Extração concluída com sucesso! ({len(df)} faturas)")
        return df
    else:
        print("❌ SANEAGO: Nenhum dado encontrado no arquivo.")
        return None