/* Aba "Dados" — navega os CSVs consolidados em dados_saida/, por
 * distribuidora, com busca e paginação server-side (SANEAGO sozinho já
 * passa de 8 mil linhas, então nunca carregamos tudo de uma vez). */
(() => {
    "use strict";

    const selectEmpresa = document.getElementById("select-empresa");
    const inputBusca = document.getElementById("input-busca-dados");
    const linkCsv = document.getElementById("link-baixar-csv");
    const tbody = document.getElementById("tbody-dados");
    const textoVazio = document.getElementById("texto-vazio-dados");
    const tabelaScroll = document.querySelector("#aba-dados .tabela-scroll");
    const botaoAnterior = document.getElementById("botao-pagina-anterior");
    const botaoProxima = document.getElementById("botao-pagina-proxima");
    const textoPaginacao = document.getElementById("texto-paginacao");

    const TAMANHO_PAGINA = 50;

    let empresaAtual = null;
    let paginaAtual = 1;
    let totalAtual = 0;
    let buscaDebounce = null;
    let iniciado = false;

    const formatarMoeda = (valor) =>
        new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(valor || 0);
    const formatarNumero = (valor) =>
        new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 }).format(valor || 0);

    function celula(texto) {
        const td = document.createElement("td");
        td.textContent = texto == null || texto === "" ? "—" : texto;
        return td;
    }

    async function carregarEmpresas() {
        const resposta = await fetch("/dados/empresas");
        if (!resposta.ok) return;
        const { empresas } = await resposta.json();

        selectEmpresa.innerHTML = "";
        for (const info of empresas) {
            const opcao = document.createElement("option");
            opcao.value = info.empresa;
            opcao.textContent = `${info.empresa} (${info.linhas})`;
            selectEmpresa.appendChild(opcao);
        }

        if (empresas.length > 0) {
            empresaAtual = empresas[0].empresa;
            selectEmpresa.value = empresaAtual;
            await carregarPagina(1);
        }
    }

    async function carregarPagina(pagina) {
        if (!empresaAtual) return;
        paginaAtual = pagina;

        const parametros = new URLSearchParams({
            pagina: String(pagina),
            tamanho_pagina: String(TAMANHO_PAGINA),
            busca: inputBusca.value.trim(),
        });

        const resposta = await fetch(`/dados/${encodeURIComponent(empresaAtual)}?${parametros}`);
        if (!resposta.ok) {
            tbody.innerHTML = "";
            textoVazio.hidden = false;
            textoVazio.textContent = "Não foi possível carregar os dados dessa distribuidora.";
            return;
        }

        const dados = await resposta.json();
        totalAtual = dados.total;
        renderizarTabela(dados.linhas);
        renderizarPaginacao();
        linkCsv.href = `/dados/${encodeURIComponent(empresaAtual)}/csv`;
    }

    function renderizarTabela(linhas) {
        tbody.innerHTML = "";
        tabelaScroll.hidden = linhas.length === 0;
        textoVazio.hidden = linhas.length > 0;

        for (const linha of linhas) {
            const tr = document.createElement("tr");
            tr.appendChild(celula(linha.NUM_FATURA));
            tr.appendChild(celula(linha.MES_ANO_REF));
            tr.appendChild(celula(linha.CONTA_DV));
            tr.appendChild(celula(linha.NOME_CLIENTE));
            tr.appendChild(celula(linha["UNIDADE JUDICIÁRIA"]));
            tr.appendChild(celula(formatarNumero(linha.CONSUMO_M3)));
            tr.appendChild(celula(formatarMoeda(linha.VALOR_AGUA)));
            tr.appendChild(celula(formatarMoeda(linha.VALOR_ESGOTO)));
            tr.appendChild(celula(formatarMoeda(linha.VALOR_TAXAS_EXTRAS)));
            tr.appendChild(celula(formatarMoeda(linha.VALOR_OUTRAS_TAXAS)));
            tr.appendChild(celula(formatarMoeda(linha.VALOR_TOTAL)));

            const tdSuspeita = document.createElement("td");
            if (linha.SUSPEITO) {
                const badge = document.createElement("span");
                badge.className = "badge-suspeita";
                badge.textContent = "⚠ suspeita";
                tdSuspeita.appendChild(badge);
            }
            tr.appendChild(tdSuspeita);

            const tdReportar = document.createElement("td");
            const botaoReportar = document.createElement("button");
            botaoReportar.className = "botao secundario botao-reportar-erro";
            botaoReportar.type = "button";
            botaoReportar.textContent = "Reportar erro";
            botaoReportar.addEventListener("click", () => reportarErro(linha));
            tdReportar.appendChild(botaoReportar);
            tr.appendChild(tdReportar);

            tbody.appendChild(tr);
        }
    }

    function renderizarPaginacao() {
        const totalPaginas = Math.max(1, Math.ceil(totalAtual / TAMANHO_PAGINA));
        textoPaginacao.textContent = `Página ${paginaAtual} de ${totalPaginas} — ${totalAtual} fatura(s)`;
        botaoAnterior.disabled = paginaAtual <= 1;
        botaoProxima.disabled = paginaAtual >= totalPaginas;
    }

    function reportarErro(linha) {
        window.Erros.preencher({
            concessionaria: empresaAtual,
            numFatura: linha.NUM_FATURA,
            contaDv: linha.CONTA_DV,
            mesAno: linha.MES_ANO_REF,
        });
        document.querySelector('.aba-botao[data-aba="erros"]').click();
    }

    selectEmpresa.addEventListener("change", () => {
        empresaAtual = selectEmpresa.value;
        carregarPagina(1);
    });

    inputBusca.addEventListener("input", () => {
        clearTimeout(buscaDebounce);
        buscaDebounce = setTimeout(() => carregarPagina(1), 300);
    });

    botaoAnterior.addEventListener("click", () => carregarPagina(paginaAtual - 1));
    botaoProxima.addEventListener("click", () => carregarPagina(paginaAtual + 1));

    window.Dados = {
        iniciar() {
            if (iniciado) return;
            iniciado = true;
            carregarEmpresas();
        },
    };
})();
