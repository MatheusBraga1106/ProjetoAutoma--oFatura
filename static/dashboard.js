/* Aba "Dashboards" — visão agregada de todas as distribuidoras: KPIs,
 * comparação por empresa, série mensal e maiores consumos/valores. */
(() => {
    "use strict";

    let iniciado = false;
    const selectEmpresa = document.getElementById("select-empresa-dashboard");

    const formatarMoeda = (valor) =>
        new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 }).format(valor || 0);
    const formatarMoedaCompleta = (valor) =>
        new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(valor || 0);
    const formatarNumero = (valor) =>
        new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 }).format(valor || 0);

    function celula(texto) {
        const td = document.createElement("td");
        td.textContent = texto == null || texto === "" ? "—" : texto;
        return td;
    }

    function preencherTabela(idTbody, linhas, montarCelulas) {
        const tbody = document.getElementById(idTbody);
        tbody.innerHTML = "";
        if (!linhas || linhas.length === 0) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.textContent = "Sem dados.";
            td.colSpan = 4;
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }
        for (const linha of linhas) {
            const tr = document.createElement("tr");
            for (const valor of montarCelulas(linha)) tr.appendChild(celula(valor));
            tbody.appendChild(tr);
        }
    }

    async function carregarEmpresas() {
        const resposta = await fetch("/dados/empresas");
        if (!resposta.ok) return;
        const { empresas } = await resposta.json();
        for (const info of empresas) {
            const opcao = document.createElement("option");
            opcao.value = info.empresa;
            opcao.textContent = info.empresa;
            selectEmpresa.appendChild(opcao);
        }
    }

    const detalhe = document.getElementById("detalhe-dashboard");
    let requisicaoAtual = 0;

    async function buscarResumo() {
        const parametros = selectEmpresa.value ? `?empresa=${encodeURIComponent(selectEmpresa.value)}` : "";
        const resposta = await fetch(`/dashboard/resumo${parametros}`);
        if (!resposta.ok) return null;
        return resposta.json();
    }

    async function carregar() {
        const dados = await buscarResumo();
        if (!dados) return;
        renderizarVisaoGeral(dados);
        renderizarDetalhe(dados);
    }

    // Troca de distribuidora: só a seção de baixo muda. Mantém o render
    // anterior esmaecido enquanto busca (sem piscar), e descarta respostas
    // que chegarem fora de ordem se o usuário trocar rápido.
    async function recarregarDetalhe() {
        const id = ++requisicaoAtual;
        detalhe.classList.add("carregando");
        try {
            const dados = await buscarResumo();
            if (id !== requisicaoAtual || !dados) return;
            renderizarDetalhe(dados);
        } finally {
            if (id === requisicaoAtual) detalhe.classList.remove("carregando");
        }
    }

    function renderizarVisaoGeral(dados) {
        document.getElementById("kpi-total-faturas").textContent = formatarNumero(dados.kpis.total_faturas);
        document.getElementById("kpi-valor-total").textContent = formatarMoeda(dados.kpis.valor_total);
        document.getElementById("kpi-consumo-total").textContent = formatarNumero(dados.kpis.consumo_total);
        document.getElementById("kpi-suspeitas").textContent = formatarNumero(dados.kpis.total_suspeitas);

        const porEmpresaValor = dados.por_empresa.map((e) => ({ rotulo: e.empresa, valor: e.valor_total }));
        const porEmpresaSuspeitas = dados.por_empresa.map((e) => ({ rotulo: e.empresa, valor: e.suspeitas }));

        window.Graficos.criarBarras(
            document.getElementById("grafico-valor-empresa"), porEmpresaValor,
            { formatarValor: formatarMoeda, titulo: "Valor total por distribuidora" }
        );
        window.Graficos.criarBarras(
            document.getElementById("grafico-suspeitas-empresa"), porEmpresaSuspeitas,
            { formatarValor: formatarNumero, titulo: "Faturas suspeitas por distribuidora" }
        );
    }

    function renderizarDetalhe(dados) {
        const serieValor = dados.serie_mensal.map((m) => ({ rotulo: m.mes_ano, valor: m.valor_total }));
        const serieConsumo = dados.serie_mensal.map((m) => ({ rotulo: m.mes_ano, valor: m.consumo_total }));

        const sufixo = selectEmpresa.value ? ` — ${selectEmpresa.value}` : "";
        document.getElementById("titulo-grafico-valor-mensal").textContent = `Valor total por mês (R$)${sufixo}`;
        document.getElementById("titulo-grafico-consumo-mensal").textContent = `Consumo total por mês (m³)${sufixo}`;
        document.getElementById("titulo-top-consumo").textContent = `Maiores consumos${sufixo}`;
        document.getElementById("titulo-top-valor").textContent = `Maiores valores${sufixo}`;

        window.Graficos.criarLinha(
            document.getElementById("grafico-valor-mensal"), serieValor,
            { formatarValor: formatarMoeda, titulo: `Valor total por mês${sufixo}` }
        );
        window.Graficos.criarLinha(
            document.getElementById("grafico-consumo-mensal"), serieConsumo,
            { formatarValor: formatarNumero, titulo: `Consumo total por mês${sufixo}` }
        );

        preencherTabela("tbody-top-consumo", dados.top_consumo, (l) => [
            l.empresa, l.cliente, l.mes_ano, formatarNumero(l.consumo),
        ]);
        preencherTabela("tbody-top-valor", dados.top_valor, (l) => [
            l.empresa, l.cliente, l.mes_ano, formatarMoedaCompleta(l.valor),
        ]);
    }

    selectEmpresa.addEventListener("change", recarregarDetalhe);

    window.Dashboard = {
        iniciar() {
            if (iniciado) return;
            iniciado = true;
            carregarEmpresas();
            carregar();
        },
    };
})();
