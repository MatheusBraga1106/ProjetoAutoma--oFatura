/* Gráficos em SVG puro (sem lib externa) — barra de hue único sequencial e
 * linha de série única, seguindo as specs do skill dataviz do projeto:
 * marca fina, extremidade arredondada, grid recessivo, hover com tooltip,
 * rótulo nunca na cor da série (sempre token de texto). */
(() => {
    "use strict";

    const NS = "http://www.w3.org/2000/svg";

    function corToken(nome, respaldo) {
        const valor = getComputedStyle(document.documentElement).getPropertyValue(nome).trim();
        return valor || respaldo;
    }

    function el(tag, atributos) {
        const no = document.createElementNS(NS, tag);
        for (const [chave, valor] of Object.entries(atributos || {})) {
            no.setAttribute(chave, valor);
        }
        return no;
    }

    function textoSvg(x, y, conteudo, classe) {
        const texto = el("text", { x, y });
        if (classe) texto.setAttribute("class", classe);
        texto.textContent = conteudo; // nunca innerHTML — rótulos vêm de dados extraídos (não confiáveis)
        return texto;
    }

    function criarTooltip(container) {
        const tooltip = document.createElement("div");
        tooltip.className = "grafico-tooltip";
        tooltip.hidden = true;
        container.style.position = "relative";
        container.appendChild(tooltip);
        return {
            mostrar(xPixel, yPixel, linhas) {
                tooltip.innerHTML = "";
                for (const linha of linhas) {
                    const p = document.createElement("div");
                    p.textContent = linha;
                    tooltip.appendChild(p);
                }
                tooltip.style.left = `${xPixel}px`;
                tooltip.style.top = `${yPixel}px`;
                tooltip.hidden = false;
            },
            esconder() {
                tooltip.hidden = true;
            },
        };
    }

    function formatoPadrao(valor) {
        return new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 }).format(valor);
    }

    // Estimativa de largura de texto sem medir DOM (evita reflow por rótulo);
    // fator calibrado pra a fonte sans do projeto em ~11px/~10px.
    function larguraEstimada(texto, tamanhoFonte = 11) {
        return String(texto).length * tamanhoFonte * 0.56;
    }

    function estadoVazio(container, mensagem) {
        container.innerHTML = "";
        const p = document.createElement("p");
        p.className = "grafico-vazio";
        p.textContent = mensagem || "Sem dados suficientes.";
        container.appendChild(p);
    }

    /**
     * criarBarras: comparação de magnitude entre categorias — hue sequencial
     * único (nunca uma cor por barra). dados: [{rotulo, valor}].
     */
    function criarBarras(container, dados, opts = {}) {
        container.innerHTML = "";
        if (!dados || dados.length === 0) {
            estadoVazio(container);
            return;
        }

        const formatar = opts.formatarValor || formatoPadrao;
        const largura = container.clientWidth || 480;
        const altura = opts.altura || 280;
        const valorMaximo = Math.max(...dados.map((d) => d.valor), 0) || 1;
        const margem = {
            topo: 24, direita: 16, baixo: 70,
            esquerda: Math.max(16, larguraEstimada(formatar(valorMaximo), 10) + 10),
        };
        const areaLargura = largura - margem.esquerda - margem.direita;
        const areaAltura = altura - margem.topo - margem.baixo;

        const serie = corToken("--serie-1", "#2a78d6");
        const ink = corToken("--cor-texto-suave", "#5b6472");
        const inkMuted = corToken("--cor-texto-suave", "#5b6472");
        const grid = corToken("--cor-borda", "#dfe4ea");

        const larguraBanda = areaLargura / dados.length;
        const larguraBarra = Math.min(larguraBanda * 0.6, 24);

        const svg = el("svg", {
            viewBox: `0 0 ${largura} ${altura}`,
            width: "100%",
            height: altura,
            role: "img",
            "aria-label": opts.titulo || "Gráfico de barras",
        });

        // Eixo Y: 3 faixas com valor arredondado — carrega a escala pros
        // casos em que o rótulo direto no topo da barra não coube (ver
        // marks-and-anatomy.md: manter os ticks enquanto nem todo valor
        // estiver rotulado diretamente).
        for (let i = 0; i <= 2; i++) {
            const y = margem.topo + (areaAltura / 2) * i;
            const valorEixo = valorMaximo * (1 - i / 2);
            svg.appendChild(el("line", {
                x1: margem.esquerda, x2: largura - margem.direita, y1: y, y2: y,
                stroke: grid, "stroke-width": 1,
            }));
            if (i < 2) {
                const rotuloEixo = textoSvg(margem.esquerda - 6, y - 4, formatar(valorEixo), "grafico-rotulo-eixo");
                rotuloEixo.setAttribute("text-anchor", "end");
                rotuloEixo.setAttribute("fill", inkMuted);
                svg.appendChild(rotuloEixo);
            }
        }

        const tooltip = criarTooltip(container);

        dados.forEach((item, indice) => {
            const alturaBarra = valorMaximo > 0 ? (item.valor / valorMaximo) * areaAltura : 0;
            const xCentro = margem.esquerda + larguraBanda * indice + larguraBanda / 2;
            const xBarra = xCentro - larguraBarra / 2;
            const yBarra = margem.topo + areaAltura - alturaBarra;
            const raio = Math.min(4, alturaBarra);

            const grupo = el("g", { class: "grafico-barra-grupo", tabindex: "0" });

            // Retângulo com cantos arredondados só no topo: path manual.
            const caminho = alturaBarra > raio
                ? `M${xBarra},${yBarra + raio}
                   a${raio},${raio} 0 0 1 ${raio},-${raio}
                   h${larguraBarra - 2 * raio}
                   a${raio},${raio} 0 0 1 ${raio},${raio}
                   v${alturaBarra - raio}
                   h${-larguraBarra}
                   Z`
                : `M${xBarra},${yBarra} h${larguraBarra} v${alturaBarra} h${-larguraBarra} Z`;

            const barra = el("path", { d: caminho, fill: serie });
            grupo.appendChild(barra);

            // Área de hover maior que a marca (spec de interaction.md)
            const hit = el("rect", {
                x: margem.esquerda + larguraBanda * indice, y: margem.topo,
                width: larguraBanda, height: areaAltura, fill: "transparent",
            });
            grupo.appendChild(hit);

            // Rótulo de valor no topo só quando cabe nesta barra específica
            // (medir antes de desenhar — nunca truncar/deixar colidir, ver
            // marks-and-anatomy.md). Fora daí o valor mora no tooltip.
            const textoValor = formatar(item.valor);
            if (larguraEstimada(textoValor, 11) < larguraBanda - 4) {
                const rotuloValor = textoSvg(xCentro, yBarra - 6, textoValor, "grafico-rotulo-valor");
                rotuloValor.setAttribute("text-anchor", "middle");
                rotuloValor.setAttribute("fill", ink);
                grupo.appendChild(rotuloValor);
            }

            const yRotuloCategoria = margem.topo + areaAltura + 14;
            const rotuloCategoria = textoSvg(xCentro, yRotuloCategoria, item.rotulo, "grafico-rotulo-eixo");
            rotuloCategoria.setAttribute("fill", inkMuted);
            if (larguraBanda < 90) {
                rotuloCategoria.setAttribute("transform", `rotate(-40 ${xCentro} ${yRotuloCategoria})`);
                rotuloCategoria.setAttribute("text-anchor", "end");
            } else {
                rotuloCategoria.setAttribute("text-anchor", "middle");
            }
            grupo.appendChild(rotuloCategoria);

            const mostrarTooltip = () => {
                grupo.classList.add("ativo");
                tooltip.mostrar(xCentro, yBarra - 8, [item.rotulo, formatar(item.valor)]);
            };
            const esconderTooltip = () => {
                grupo.classList.remove("ativo");
                tooltip.esconder();
            };
            grupo.addEventListener("pointermove", mostrarTooltip);
            grupo.addEventListener("pointerleave", esconderTooltip);
            grupo.addEventListener("focus", mostrarTooltip);
            grupo.addEventListener("blur", esconderTooltip);

            svg.appendChild(grupo);
        });

        container.appendChild(svg);
    }

    /**
     * criarLinha: uma métrica ao longo do tempo — uma série só (nunca
     * dual-axis). dados: [{rotulo, valor}].
     */
    function criarLinha(container, dados, opts = {}) {
        container.innerHTML = "";
        if (!dados || dados.length < 2) {
            estadoVazio(container, "Dados insuficientes para uma série temporal.");
            return;
        }

        const formatar = opts.formatarValor || formatoPadrao;
        const largura = container.clientWidth || 480;
        const altura = opts.altura || 260;
        const margem = { topo: 20, direita: 16, baixo: 32, esquerda: 16 };
        const areaLargura = largura - margem.esquerda - margem.direita;
        const areaAltura = altura - margem.topo - margem.baixo;

        const serie = corToken("--serie-1", "#2a78d6");
        const superficie = corToken("--cor-cartao", "#ffffff");
        const ink = corToken("--cor-texto-suave", "#5b6472");
        const grid = corToken("--cor-borda", "#dfe4ea");

        const valores = dados.map((d) => d.valor);
        const valorMaximo = Math.max(...valores, 0) || 1;
        const valorMinimo = Math.min(...valores, 0);

        const passoX = areaLargura / (dados.length - 1);
        const escalaY = (valor) => {
            const proporcao = (valor - valorMinimo) / (valorMaximo - valorMinimo || 1);
            return margem.topo + areaAltura - proporcao * areaAltura;
        };
        const pontos = dados.map((d, i) => ({
            x: margem.esquerda + passoX * i,
            y: escalaY(d.valor),
            ...d,
        }));

        const svg = el("svg", {
            viewBox: `0 0 ${largura} ${altura}`,
            width: "100%",
            height: altura,
            role: "img",
            "aria-label": opts.titulo || "Gráfico de linha",
        });

        // Gridlines horizontais (3 faixas)
        for (let i = 0; i <= 2; i++) {
            const y = margem.topo + (areaAltura / 2) * i;
            svg.appendChild(el("line", {
                x1: margem.esquerda, x2: largura - margem.direita, y1: y, y2: y,
                stroke: grid, "stroke-width": 1,
            }));
        }

        const caminho = pontos.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
        svg.appendChild(el("path", {
            d: caminho, fill: "none", stroke: serie, "stroke-width": 2,
            "stroke-linejoin": "round", "stroke-linecap": "round",
        }));

        // Rótulos do eixo X: distribuídos por igual (sempre incluindo o
        // primeiro e o último ponto), pulando qualquer um que ficaria perto
        // demais do último já desenhado — nunca deixa colidir.
        const larguraRotuloX = larguraEstimada("00/0000", 10);
        let xUltimoRotulo = -Infinity;
        const passoAlvo = Math.max(1, Math.round((larguraRotuloX + 12) / passoX));
        for (let i = 0; i < pontos.length; i += passoAlvo) {
            const p = pontos[i];
            if (p.x - xUltimoRotulo < larguraRotuloX + 8 && i !== 0) continue;
            const rotulo = textoSvg(p.x, altura - 8, p.rotulo, "grafico-rotulo-eixo");
            rotulo.setAttribute("fill", ink);
            rotulo.setAttribute("text-anchor", "middle");
            svg.appendChild(rotulo);
            xUltimoRotulo = p.x;
        }
        // Último ponto: só rotula se não colidir com o rótulo anterior.
        const ultimoPonto = pontos[pontos.length - 1];
        if (ultimoPonto.x - xUltimoRotulo >= larguraRotuloX + 8) {
            const rotulo = textoSvg(ultimoPonto.x, altura - 8, ultimoPonto.rotulo, "grafico-rotulo-eixo");
            rotulo.setAttribute("fill", ink);
            rotulo.setAttribute("text-anchor", "end");
            svg.appendChild(rotulo);
        }

        // Marcador + rótulo no último ponto (valor na ponta, ver marks-and-anatomy)
        const ultimo = pontos[pontos.length - 1];
        const marcadorFinal = el("circle", { cx: ultimo.x, cy: ultimo.y, r: 4, fill: serie, stroke: superficie, "stroke-width": 2 });
        svg.appendChild(marcadorFinal);
        const rotuloFinal = textoSvg(ultimo.x, ultimo.y - 10, formatar(ultimo.valor), "grafico-rotulo-valor");
        rotuloFinal.setAttribute("text-anchor", "end");
        rotuloFinal.setAttribute("fill", ink);
        svg.appendChild(rotuloFinal);

        // Crosshair + ponto ativo (hover)
        const linhaCrosshair = el("line", { y1: margem.topo, y2: margem.topo + areaAltura, stroke: grid, "stroke-width": 1, visibility: "hidden" });
        const pontoAtivo = el("circle", { r: 5, fill: serie, stroke: superficie, "stroke-width": 2, visibility: "hidden" });
        svg.appendChild(linhaCrosshair);
        svg.appendChild(pontoAtivo);

        const areaHover = el("rect", {
            x: margem.esquerda, y: margem.topo, width: areaLargura, height: areaAltura, fill: "transparent",
        });
        svg.appendChild(areaHover);

        const tooltip = criarTooltip(container);

        function aoMover(evento) {
            const retangulo = svg.getBoundingClientRect();
            const xRelativo = ((evento.clientX - retangulo.left) / retangulo.width) * largura;
            let indiceMaisProximo = Math.round((xRelativo - margem.esquerda) / passoX);
            indiceMaisProximo = Math.max(0, Math.min(pontos.length - 1, indiceMaisProximo));
            const ponto = pontos[indiceMaisProximo];

            linhaCrosshair.setAttribute("x1", ponto.x);
            linhaCrosshair.setAttribute("x2", ponto.x);
            linhaCrosshair.setAttribute("visibility", "visible");
            pontoAtivo.setAttribute("cx", ponto.x);
            pontoAtivo.setAttribute("cy", ponto.y);
            pontoAtivo.setAttribute("visibility", "visible");

            tooltip.mostrar(ponto.x, ponto.y - 12, [ponto.rotulo, formatar(ponto.valor)]);
        }
        function aoSair() {
            linhaCrosshair.setAttribute("visibility", "hidden");
            pontoAtivo.setAttribute("visibility", "hidden");
            tooltip.esconder();
        }

        areaHover.addEventListener("pointermove", aoMover);
        areaHover.addEventListener("pointerleave", aoSair);

        container.appendChild(svg);
    }

    window.Graficos = { criarBarras, criarLinha };
})();
