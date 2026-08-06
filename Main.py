import os
import pandas as pd
import unicodedata

# Importando os extratores prontos
from extratores.saneago import extrair_saneago
from extratores.saneago_analitica import extrair_saneago_analitica
from extratores.sae import extrair_sae
from extratores.codego import extrair_codego

# Importando TODOS os outros extratores (ativados)
from extratores.aguas_ipameri import extrair_aguas_ipameri
from extratores.buriti_alegre import extrair_buriti_alegre
from extratores.demae import extrair_demae
from extratores.saae_abadiania import extrair_saae_abadiania
from extratores.saae_corumba import extrair_saae_corumba
from extratores.saae_mineiros import extrair_saae_mineiros
# from extratores.sanesc import extrair_sanesc
# from extratores.chesp import extrair_chesp


def normalizar_texto(texto):
    """Remove acentos e coloca em minúsculas para facilitar a correspondência no IF"""
    texto = str(texto).lower()
    return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')


def rastrear_e_processar_pastas(pasta_raiz, pasta_saida):
    os.makedirs(pasta_saida, exist_ok=True)
    
    # Gavetas de armazenamento em memória atualizadas
    dados_por_empresa = {
        "SANEAGO": [],
        "SANEAGO_ANALITICA": [], 
        "SAE": [],
        "CODEGO": [],
        "IPAMERI": [],
        "BURITI_ALEGRE": [],
        "DEMAE": [],
        "SAAE_ABADIANIA": [],
        "SAAE_CORUMBA": [],
        "SAAE_MINEIROS": [],
        "SANESC": [],
        "CHESP": []
    }

    print(f"🕷️ A iniciar rastreamento na pasta raiz: '{pasta_raiz}'\n")

    for diretorio_atual, subpastas, arquivos in os.walk(pasta_raiz):
        arquivos_txt = [f for f in arquivos if f.lower().endswith('.txt')]
        
        for arquivo in arquivos_txt:
            # Cria o caminho absoluto (C:\...)
            caminho_completo = os.path.abspath(os.path.join(diretorio_atual, arquivo))
            
            # 🛡️ TRUQUE ANTI-LIMITE DO WINDOWS (MAX_PATH > 260 caracteres)
            if os.name == 'nt' and not caminho_completo.startswith('\\\\?\\'):
                caminho_completo = '\\\\?\\' + caminho_completo
            
            # Normalizamos o nome da pasta e do ficheiro
            nome_arquivo_norm = normalizar_texto(arquivo)
            nome_pasta_norm = normalizar_texto(diretorio_atual)
            
            # Junta os dois para facilitar a procura da palavra-chave
            alvo_busca = f"{nome_arquivo_norm} {nome_pasta_norm}"
            
            df_extraido = None
            empresa_identificada = None

            print(f" Lendo: {arquivo} (Pasta: {os.path.basename(diretorio_atual)})")

            # =========================================================
            # ROTEADOR INTELIGENTE (TOTALMENTE ATIVADO)
            # =========================================================
            if "saneago" in alvo_busca and "analitica" in alvo_busca:
                df_extraido = extrair_saneago_analitica(caminho_completo)
                empresa_identificada = "SANEAGO_ANALITICA"
                
            elif "saneago" in alvo_busca and "analitica" not in alvo_busca:
                df_extraido = extrair_saneago(caminho_completo)
                empresa_identificada = "SANEAGO"
                
            elif "codego" in alvo_busca:
                df_extraido = extrair_codego(caminho_completo)
                empresa_identificada = "CODEGO"
                
            elif "ipameri" in alvo_busca:
                df_extraido = extrair_aguas_ipameri(caminho_completo)
                empresa_identificada = "IPAMERI"
                
            elif "buriti alegre" in alvo_busca:
                df_extraido = extrair_buriti_alegre(caminho_completo)
                empresa_identificada = "BURITI_ALEGRE"
                
            elif "demae" in alvo_busca:
                df_extraido = extrair_demae(caminho_completo)
                empresa_identificada = "DEMAE"
                
            elif "abadiania" in alvo_busca:
                df_extraido = extrair_saae_abadiania(caminho_completo)
                empresa_identificada = "SAAE_ABADIANIA"
                
            elif "corumba" in alvo_busca:
                df_extraido = extrair_saae_corumba(caminho_completo)
                empresa_identificada = "SAAE_CORUMBA"
                
            elif "mineiros" in alvo_busca:
                df_extraido = extrair_saae_mineiros(caminho_completo)
                empresa_identificada = "SAAE_MINEIROS"
                
            elif "sanesc" in alvo_busca:
                print(" ⏭ Extrator SANESC ainda não criado. A saltar...")
                # df_extraido = extrair_sanesc(caminho_completo)
                empresa_identificada = "SANESC"
                
            elif "chesp" in alvo_busca:
                print(" ⏭ Extrator CHESP ainda não criado. A saltar...")
                # df_extraido = extrair_chesp(caminho_completo)
                empresa_identificada = "CHESP"
                
            elif "sae" in alvo_busca and "saae" not in alvo_busca:
                df_extraido = extrair_sae(caminho_completo)
                empresa_identificada = "SAE"
                
            else:
                print(f" ⏭ Ignorado: Não foi possível identificar a empresa.")
                continue

            # =========================================================
            # GUARDA O RESULTADO NA GAVETA CORRETA
            # =========================================================
            if df_extraido is not None and not df_extraido.empty:
                df_extraido['ARQUIVO_ORIGEM'] = arquivo
                df_extraido['PASTA_ORIGEM'] = diretorio_atual
                dados_por_empresa[empresa_identificada].append(df_extraido)


    # =========================================================
    # RELACIONAMENTO 1: SANEAGO NORMAL + SANEAGO ANALÍTICA (HIDRÔMETROS)
    # =========================================================
    if len(dados_por_empresa["SANEAGO"]) > 0 and len(dados_por_empresa["SANEAGO_ANALITICA"]) > 0:
        print("\n🔗 Relacionando faturas da SANEAGO com hidrômetros analíticos...")
        
        df_saneago = pd.concat(dados_por_empresa["SANEAGO"], ignore_index=True)
        df_analitica = pd.concat(dados_por_empresa["SANEAGO_ANALITICA"], ignore_index=True)
        
        # Garante que a coluna MES_ANO_REF exista na analítica mesmo se nenhum arquivo tiver data
        if 'MES_ANO_REF' not in df_analitica.columns:
            df_analitica['MES_ANO_REF'] = ""
            
        # Padroniza as chaves removendo espaços extras
        df_saneago['CONTA_DV'] = df_saneago['CONTA_DV'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
        df_analitica['CONTA_DV'] = df_analitica['CONTA_DV'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
        
        df_saneago['MES_ANO_REF'] = df_saneago['MES_ANO_REF'].astype(str).str.strip()
        df_analitica['MES_ANO_REF'] = df_analitica['MES_ANO_REF'].astype(str).str.strip()

        # ---------------------------------------------------------
        # CRIANDO O "PLANO B" (Mapa do Hidrômetro Mais Recente)
        # ---------------------------------------------------------
        df_analitica['DATA_ORDENACAO'] = pd.to_datetime(df_analitica['MES_ANO_REF'], format='%m/%Y', errors='coerce')
        df_analitica_ordenada = df_analitica.sort_values(by=['CONTA_DV', 'DATA_ORDENACAO'], ascending=[True, False])
        hidrometros_recentes = df_analitica_ordenada.drop_duplicates(subset=['CONTA_DV'], keep='first')
        
        mapa_hidrometros = hidrometros_recentes.set_index('CONTA_DV')['NUM_HIDROMETRO_EXTRAIDO'].to_dict()
        mapa_arquivos = hidrometros_recentes.set_index('CONTA_DV')['ARQUIVO_ORIGEM'].to_dict()
        
        df_analitica = df_analitica.drop(columns=['DATA_ORDENACAO'])

        # ---------------------------------------------------------
        # CRUZAMENTO EXATO (TENTATIVA 1 - Conta + Mês/Ano)
        # ---------------------------------------------------------
        df_analitica = df_analitica.drop_duplicates(subset=['CONTA_DV', 'MES_ANO_REF'])
        df_cruzado = pd.merge(df_saneago, df_analitica, on=['CONTA_DV', 'MES_ANO_REF'], how='left')
        df_cruzado['NUM_HIDROMETRO'] = df_cruzado['NUM_HIDROMETRO_EXTRAIDO']
        
        # ---------------------------------------------------------
        # PREENCHIMENTO INTELIGENTE (TENTATIVA 2 - Apenas pela Conta)
        # ---------------------------------------------------------
        vazios = df_cruzado['NUM_HIDROMETRO'].isna() | (df_cruzado['NUM_HIDROMETRO'] == "") | (df_cruzado['NUM_HIDROMETRO'] == "nan")
        df_cruzado.loc[vazios, 'NUM_HIDROMETRO'] = df_cruzado.loc[vazios, 'CONTA_DV'].map(mapa_hidrometros)
        df_cruzado.loc[vazios, 'ARQUIVO_ORIGEM_y'] = df_cruzado.loc[vazios, 'CONTA_DV'].map(mapa_arquivos)

        # ---------------------------------------------------------
        # LIMPEZA FINAL DAS COLUNAS GERADAS PELO MERGE
        # ---------------------------------------------------------
        df_cruzado = df_cruzado.drop(columns=['NUM_HIDROMETRO_EXTRAIDO'])
        
        if 'ARQUIVO_ORIGEM_y' in df_cruzado.columns:
            df_cruzado = df_cruzado.drop(columns=['ARQUIVO_ORIGEM_y', 'PASTA_ORIGEM_y'], errors='ignore')
            df_cruzado = df_cruzado.rename(columns={'ARQUIVO_ORIGEM_x': 'ARQUIVO_ORIGEM', 'PASTA_ORIGEM_x': 'PASTA_ORIGEM'})
        
        # Atualiza a gaveta da Saneago e limpa a Analítica
        dados_por_empresa["SANEAGO"] = [df_cruzado]
        dados_por_empresa["SANEAGO_ANALITICA"] = []


    # =========================================================
    # EXPORTAÇÃO INCREMENTAL E ENRIQUECIMENTO VIA JSON
    # =========================================================
    print("\n" + "="*70)
    print("🛡️ A SALVAR NA BASE DE DADOS E ENRIQUECER COM JSON")
    print("="*70)

    for empresa, lista_de_dfs in dados_por_empresa.items():
        if len(lista_de_dfs) > 0 and empresa != "SANEAGO_ANALITICA":
            df_novo_lote = pd.concat(lista_de_dfs, ignore_index=True)
            
            # =========================================================
            # RELACIONAMENTO 2: ENRIQUECIMENTO DE DADOS COM O JSON
            # =========================================================
            caminho_json = "contas.json" 
            if os.path.exists(caminho_json):
                df_info_contas = pd.read_json(caminho_json)

                colunas_json = [
                    "CONTA_DV", "UNIDADE JUDICIÁRIA", "ENDEREÇO", "DISTRIBUIDORA","OPERANTE - FILTRO"
                ]
                colunas_existentes = [col for col in colunas_json if col in df_info_contas.columns]
                df_info_contas = df_info_contas[colunas_existentes]

                df_info_contas['CONTA_DV'] = df_info_contas['CONTA_DV'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
                df_novo_lote['CONTA_DV'] = df_novo_lote['CONTA_DV'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()

                df_novo_lote = pd.merge(df_novo_lote, df_info_contas, on="CONTA_DV", how="left")


                # =========================================================
                # 🛡️ NOVA CAMADA DE CONFERÊNCIA: REDISTRIBUIÇÃO DE VALORES
                # =========================================================
                def corrigir_distribuicao_financeira(row):
                    valores_encontrados = []
                    if pd.notna(row.get('VALOR_AGUA')) and row['VALOR_AGUA'] > 0: valores_encontrados.append(row['VALOR_AGUA'])
                    if pd.notna(row.get('VALOR_ESGOTO')) and row['VALOR_ESGOTO'] > 0: valores_encontrados.append(row['VALOR_ESGOTO'])
                    if pd.notna(row.get('VALOR_TAXAS_EXTRAS')) and row['VALOR_TAXAS_EXTRAS'] > 0: valores_encontrados.append(row['VALOR_TAXAS_EXTRAS'])
                    
                    if not (row.get('AGUA', False) or row.get('ESGOTO', False) or row.get('SMRSU', False)):
                        return row.get('VALOR_AGUA', 0.0), row.get('VALOR_ESGOTO', 0.0), row.get('VALOR_TAXAS_EXTRAS', 0.0)
                        
                    agua, esgoto, smrsu = 0.0, 0.0, 0.0
                    idx = 0
                    
                    if row.get('AGUA', False) and idx < len(valores_encontrados):
                        agua = valores_encontrados[idx]
                        idx += 1
                    if row.get('ESGOTO', False) and idx < len(valores_encontrados):
                        esgoto = valores_encontrados[idx]
                        idx += 1
                    if row.get('SMRSU', False) and idx < len(valores_encontrados):
                        smrsu = valores_encontrados[idx]
                        idx += 1
                        
                    return agua, esgoto, smrsu

                df_novo_lote[['VALOR_AGUA', 'VALOR_ESGOTO', 'VALOR_TAXAS_EXTRAS']] = df_novo_lote.apply(
                    lambda row: pd.Series(corrigir_distribuicao_financeira(row)), axis=1
                )
                
                df_novo_lote['BASE_CALCULO'] = (df_novo_lote['VALOR_AGUA'] + df_novo_lote['VALOR_ESGOTO']).round(2)
            
            # =========================================================
            # LÓGICA ANTI-DUPLICATA E SALVAMENTO CSV
            # =========================================================
            caminho_csv = os.path.join(pasta_saida, f"banco_dados_{empresa.lower()}.csv")
            arquivo_existe = os.path.exists(caminho_csv)

            if arquivo_existe:
                df_antigo = pd.read_csv(caminho_csv, sep=";", dtype={'NUM_FATURA': str, 'MES_ANO_REF': str, 'CONTA_DV': str})
                
                cols_chave = ['NUM_FATURA', 'MES_ANO_REF', 'CONTA_DV']
                df_antigo[cols_chave] = df_antigo[cols_chave].fillna("")
                df_novo_lote[cols_chave] = df_novo_lote[cols_chave].fillna("")

                chaves_antigas = set(
                    df_antigo['NUM_FATURA'].astype(str) + "|" + 
                    df_antigo['MES_ANO_REF'].astype(str) + "|" + 
                    df_antigo['CONTA_DV'].astype(str)
                )

                df_novo_lote['CHAVE_TEMP'] = (
                    df_novo_lote['NUM_FATURA'].astype(str) + "|" + 
                    df_novo_lote['MES_ANO_REF'].astype(str) + "|" + 
                    df_novo_lote['CONTA_DV'].astype(str)
                )

                df_inedito = df_novo_lote[~df_novo_lote['CHAVE_TEMP'].isin(chaves_antigas)].copy()
                df_inedito.drop(columns=['CHAVE_TEMP'], inplace=True)
                
                qtd_duplicadas = len(df_novo_lote) - len(df_inedito)

                if len(df_inedito) > 0:
                    df_inedito.to_csv(caminho_csv, mode='a', index=False, sep=";", encoding="utf-8-sig", header=False)
                    print(f"➕ {empresa}: {len(df_inedito)} faturas ADICIONADAS. (Ignoradas {qtd_duplicadas} duplicatas)")
                else:
                    print(f"⏩ {empresa}: Nenhuma fatura nova. (Ignoradas {qtd_duplicadas} faturas)")

            else:
                df_novo_lote.to_csv(caminho_csv, mode='w', index=False, sep=";", encoding="utf-8-sig", header=True)
                print(f"✨ {empresa}: {len(df_novo_lote)} faturas gravadas. (Novo ficheiro criado)")

    print("="*70 + "\n")

if __name__ == "__main__":
    PASTA_RAIZ_DADOS = "./dados_entrada" 
    PASTA_SAIDA = "./dados_saida"
    
    rastrear_e_processar_pastas(PASTA_RAIZ_DADOS, PASTA_SAIDA)