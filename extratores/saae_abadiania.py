import pandas as pd
import re
from datetime import datetime

def extrair_saae_abadiania(caminho_txt):
    print("⚙️  A iniciar extração: SAAE ABADIÂNIA")
    
    with open(caminho_txt, 'r', encoding='utf-8', errors='replace') as f:
        texto_completo = f.read()

    dados_finais = []
    data_lote = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def formatar_para_sql(valor_str):
        if not valor_str: return 0.0
        valor_str = valor_str.strip()
        if valor_str.startswith(','):
            valor_str = '0' + valor_str
        try:
            return float(valor_str.replace('.', '').replace(',', '.'))
        except ValueError:
            return 0.0

    # =========================================================
    # ✂️ A TESOURA SAAE ABADIÂNIA
    # =========================================================
    padrao_quebra = r'AUTENTICAÇ[AÃÂ]O NO VERSO'

    faturas_separadas = re.split(padrao_quebra, texto_completo, flags=re.IGNORECASE)
    print(f"   ✂️ O ficheiro foi dividido em {len(faturas_separadas)} blocos.")

    # =========================================================
    # VALORES DE ÁGUA/ESGOTO — busca no texto inteiro, não por bloco
    # =========================================================
    # Confirmado nos dados reais: "AUTENTICAÇÃO NO VERSO" nunca separa duas
    # faturas de fato (nenhum arquivo do corpus tem mais de 1 canhoto) — ele
    # só separa a via detalhada (tabela TARIFA DE ÁGUA/ESGOTO) da 2ª via/
    # reaviso (só o canhoto compacto, sem a tabela). Buscando por bloco, o
    # canhoto (que só existe na 2ª via) "ganhava" e a tabela de valores (que
    # só existe na 1ª via, sem canhoto) era descartada — água/esgoto saíam
    # 0,00 na maioria das faturas mesmo com o valor certo presente no texto.
    # Algumas faturas também acumulam mais de um mês de tarifa (fatura em
    # atraso) — soma todas as ocorrências em vez de pegar só a 1ª.
    todas_aguas = re.findall(r'TARIFA DE [AÁ]GUA.*?\s([\d\.,]+)\s*\n', texto_completo, re.IGNORECASE)
    todos_esgotos = re.findall(r'TARIFA DE ESGOTO.*?\s([\d\.,]+)\s*\n', texto_completo, re.IGNORECASE)
    agua_total_arquivo = round(sum(formatar_para_sql(v) for v in todas_aguas), 2)
    esgoto_total_arquivo = round(sum(formatar_para_sql(v) for v in todos_esgotos), 2)

    for indice, pagina_texto in enumerate(faturas_separadas, start=1):
        if len(pagina_texto.strip()) < 100:
            continue

        # =========================================================
        # 1. METADADOS SAAE ABADIÂNIA
        # =========================================================
        num_fatura = conta_dv = mes_ano_ref = vencimento = "NÃO ACHOU"
        total_f = 0.0

        # O Regex "Santo Graal": Captura tudo numa única linha do canhoto
        # Ex: 0001255.2     MAR/2021     210006761      23/03/2021 81,45
        match_canhoto = re.search(r'([\d\.]+)\s+([A-Z]{3}/\d{4})\s+(\d{8,})\s+(\d{2}/\d{2}/\d{4})\s+([\d\.,]+)', pagina_texto, re.IGNORECASE)
        
        if match_canhoto:
            conta_dv = match_canhoto.group(1)
            mes_bruto = match_canhoto.group(2).upper()
            num_fatura = match_canhoto.group(3)
            vencimento = match_canhoto.group(4)
            total_f = formatar_para_sql(match_canhoto.group(5))

            # Tradutor de Mês (MAR/2021 -> 03/2021)
            meses_map = {
                "JAN": "01", "FEV": "02", "MAR": "03", "ABR": "04",
                "MAI": "05", "JUN": "06", "JUL": "07", "AGO": "08",
                "SET": "09", "OUT": "10", "NOV": "11", "DEZ": "12"
            }
            if '/' in mes_bruto:
                sigla, ano = mes_bruto.split('/')
                mes_num = meses_map.get(sigla, sigla)
                mes_ano_ref = f"{mes_num}/{ano}"

        # NOME: FORUM DE ABADIANIA
        match_nome = re.search(r'NOME:\s*(.*?)(?=\s+LOCALIZA[ÇC][AÃ]O|\n)', pagina_texto, re.IGNORECASE)
        nome = match_nome.group(1).strip() if match_nome else "NÃO ACHOU"

        # =========================================================
        # 2. VALORES SAAE ABADIÂNIA
        # =========================================================
        # Consumo Faturado (Procura na tabela do histórico, o primeiro valor)
        # Ex: MAR/21       04       000 034
        match_cons = re.search(r'(?:[A-Z]{3}/\d{2})\s+(\d+)\s+\d{3}\s+\d{2,3}', pagina_texto, re.IGNORECASE)
        consumo_f = formatar_para_sql(match_cons.group(1)) if match_cons else 0.0

        # Água/esgoto vêm do texto inteiro do arquivo (ver comentário acima),
        # não deste bloco — este bloco pode ser a via sem a tabela de valores.
        agua_f = agua_total_arquivo
        esgoto_f = esgoto_total_arquivo

        taxas_extras_f = round(total_f - agua_f - esgoto_f, 2)

        if num_fatura != "NÃO ACHOU" or conta_dv != "NÃO ACHOU":
            print(f"   🔎 RAIO-X SAAE ABADIÂNIA (Bloco {indice}):")
            print(f"      ├─ Conta:      {conta_dv}")
            print(f"      ├─ Fatura:     {num_fatura}")
            print(f"      ├─ Mês/Ano:    {mes_ano_ref}")
            print(f"      └─ Total:      R$ {total_f}")
            
            dados_finais.append({
                "CONCESSIONARIA": "SAAE_ABADIANIA",
                "NUM_FATURA": num_fatura if num_fatura != "NÃO ACHOU" else "",
                "MES_ANO_REF": mes_ano_ref if mes_ano_ref != "NÃO ACHOU" else "",
                "VENCIMENTO": vencimento if vencimento != "NÃO ACHOU" else "",
                "COD_ORGAO_AGRUPADOR": "", 
                "COD_ORGAO_PAGADOR": "",
                "NOME_ORGAO_AGRUPADOR": "",
                "CONTA_DV": conta_dv if conta_dv != "NÃO ACHOU" else "",
                "NOME_CLIENTE": nome if nome != "NÃO ACHOU" else "",
                "LOGRADOURO": "", 
                "CONSUMO_M3": consumo_f,
                "VALOR_AGUA": agua_f,
                "VALOR_ESGOTO": esgoto_f,
                "VALOR_TAXAS_EXTRAS": taxas_extras_f,
                "VALOR_TOTAL": total_f,
                "DATA_PROCESSAMENTO": data_lote
            })

    if dados_finais:
        df = pd.DataFrame(dados_finais)
        print(f"   ✅ SAAE ABADIÂNIA: Extração concluída! Foram capturadas {len(df)} faturas deste arquivo.")
        return df
    else:
        print("   ❌ SAAE ABADIÂNIA: O ficheiro não continha faturas válidas.")
        return None