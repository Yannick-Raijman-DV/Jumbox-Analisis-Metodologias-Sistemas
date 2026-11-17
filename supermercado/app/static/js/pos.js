// Lógica del Punto de Venta (POS) split-screen 60/40
(() => {
    const carrito = new Map(); // id -> {id, nombre, sku, precio, maxStock, cantidad}
    const itemsContainer = document.getElementById("pos-ticket-items");
    const vacioPlaceholder = document.getElementById("pos-ticket-empty");
    const subtotalEl = document.getElementById("pos-subtotal");
    const totalEl = document.getElementById("pos-total");
    const puntosEl = document.getElementById("pos-puntos");
    const btnCobrar = document.getElementById("pos-btn-cobrar");
    const btnVaciar = document.getElementById("pos-btn-vaciar");
    const inputBuscar = document.getElementById("pos-buscar");
    const selectCliente = document.getElementById("pos-cliente");
    const selectPago = document.getElementById("pos-metodo-pago");

    // Envío a domicilio (suma al total)
    const chkEnvio = document.getElementById("pos-con-envio");
    const panelEnvio = document.getElementById("pos-panel-envio");
    const filaEnvio = document.getElementById("pos-fila-envio");
    const envioCostoEl = document.getElementById("pos-envio-costo");
    const envioInfoEl = document.getElementById("pos-envio-info");
    const btnCotizarEnvio = document.getElementById("pos-btn-cotizar-envio");
    const inputDireccion = document.getElementById("pos-envio-direccion");
    const inputTelefono = document.getElementById("pos-envio-telefono");
    const inputObs = document.getElementById("pos-envio-obs");
    const alertaBox = document.getElementById("pos-alerta");

    // Dólar bajo demanda
    const chkUsd = document.getElementById("pos-ver-usd");
    const filaUsd = document.getElementById("pos-fila-usd");
    const dolarInfo = document.getElementById("pos-dolar-info");
    const totalUsdEl = document.getElementById("pos-total-usd");

    if (!itemsContainer) return;

    let dolarVenta = null;
    let costoEnvio = 0;
    let totalPesos = 0;

    function mostrarAlerta(msg, tipo = "error") {
        alertaBox.textContent = msg;
        alertaBox.className =
            "mb-3 px-4 py-2.5 rounded-xl text-sm font-medium " +
            (tipo === "error"
                ? "bg-error-container text-on-error-container border border-error/30"
                : "bg-secondary-container text-on-secondary-container");
        alertaBox.classList.remove("hidden");
        setTimeout(() => alertaBox.classList.add("hidden"), 5000);
    }

    function formatear(n) {
        return "$" + Number(n).toFixed(2);
    }

    function actualizarUsd() {
        if (!totalUsdEl) return;
        totalUsdEl.textContent = dolarVenta ? "≈ US$ " + (totalPesos / dolarVenta).toFixed(2) : "—";
    }

    async function cargarDolar() {
        if (!dolarInfo) return;
        dolarInfo.textContent = "Consultando cotización…";
        try {
            const resp = await fetch(dolarInfo.dataset.url);
            const d = await resp.json();
            if (!d.ok) throw new Error(d.error);
            dolarVenta = Number(d.venta);
            dolarInfo.textContent = `Dólar ${d.tipo}: $${dolarVenta.toFixed(2)}` + (d.desactualizada ? " (desactualizado)" : "");
        } catch (_e) {
            dolarVenta = null;
            dolarInfo.textContent = "Dólar no disponible (se cobra en pesos)";
        }
        actualizarUsd();
    }

    async function cotizarEnvio() {
        if (!panelEnvio) return;
        const costoBase = Number(panelEnvio.dataset.costoBase || 0);
        const dir = (inputDireccion.value || "").trim();

        if (panelEnvio.dataset.geoOk !== "1" || !dir) {
            costoEnvio = costoBase;
            envioInfoEl.textContent = dir
                ? `Tarifa base de envío: ${formatear(costoBase)}`
                : `Escribí la dirección para calcular distancia (base ${formatear(costoBase)}).`;
            renderizar();
            return;
        }

        if (btnCotizarEnvio) btnCotizarEnvio.disabled = true;
        envioInfoEl.textContent = "Calculando distancia y costo de envío…";
        try {
            const resp = await fetch(panelEnvio.dataset.cotizarUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ direccion: dir }),
            });
            const d = await resp.json();
            if (!d.ok) {
                costoEnvio = costoBase;
                envioInfoEl.textContent = `${d.error || "No se pudo calcular distancia."} Se aplica tarifa base ${formatear(costoBase)}.`;
            } else {
                costoEnvio = Number(d.costo);
                envioInfoEl.textContent = `${d.km} km (~${d.minutos} min) · Envío ${formatear(costoEnvio)}`;
            }
        } catch (_err) {
            costoEnvio = costoBase;
            envioInfoEl.textContent = `Sin conexión con el mapa. Se aplica tarifa base ${formatear(costoBase)}.`;
        } finally {
            if (btnCotizarEnvio) btnCotizarEnvio.disabled = false;
            renderizar();
        }
    }

    function renderizar() {
        itemsContainer.innerHTML = "";
        let subtotal = 0;

        if (carrito.size === 0) {
            vacioPlaceholder.classList.remove("hidden");
            btnCobrar.disabled = true;
        } else {
            vacioPlaceholder.classList.add("hidden");
            btnCobrar.disabled = false;
        }

        carrito.forEach((item) => {
            const sub = item.precio * item.cantidad;
            subtotal += sub;

            const fila = document.createElement("div");
            fila.className = "flex items-center justify-between gap-3 p-3 bg-surface-container-low rounded-xl";
            fila.innerHTML = `
                <div class="min-w-0 flex-1">
                    <p class="font-semibold text-on-surface truncate">${item.nombre}</p>
                    <p class="font-label-sm text-xs text-on-surface-variant">${item.sku} • ${formatear(item.precio)} c/u</p>
                </div>
                <div class="flex items-center gap-1.5">
                    <button type="button" data-accion="restar" data-id="${item.id}" class="w-8 h-8 rounded-full bg-white border border-outline-variant/70 hover:bg-surface-container-high font-bold" aria-label="Quitar uno">−</button>
                    <span class="w-8 text-center font-label-md font-bold">${item.cantidad}</span>
                    <button type="button" data-accion="sumar" data-id="${item.id}" class="w-8 h-8 rounded-full bg-white border border-outline-variant/70 hover:bg-surface-container-high font-bold" aria-label="Agregar uno">+</button>
                </div>
                <div class="w-20 text-right font-label-md font-bold text-primary">${formatear(sub)}</div>
            `;
            itemsContainer.appendChild(fila);
        });

        const envioActivo = chkEnvio && chkEnvio.checked;
        const montoEnvio = envioActivo ? costoEnvio : 0;
        if (filaEnvio && envioCostoEl) {
            filaEnvio.classList.toggle("hidden", !envioActivo);
            envioCostoEl.textContent = formatear(montoEnvio);
        }

        totalPesos = subtotal + montoEnvio;
        actualizarUsd();
        subtotalEl.textContent = formatear(subtotal);
        totalEl.textContent = formatear(totalPesos);
        puntosEl.textContent = "+" + Math.floor(totalPesos / 100) + " pts";
    }

    function agregarProducto(card) {
        const id = Number(card.dataset.id);
        const maxStock = Number(card.dataset.stock);
        const vencido = card.dataset.vencido === "1";

        if (vencido) {
            mostrarAlerta("Este producto está vencido y bloqueado para la venta.");
            return;
        }
        if (maxStock <= 0) {
            mostrarAlerta("Sin stock en góndola. Trasladá unidades desde el depósito.");
            return;
        }

        const existente = carrito.get(id);
        if (existente) {
            if (existente.cantidad + 1 > maxStock) {
                mostrarAlerta(`Stock máximo disponible en góndola: ${maxStock} u.`);
                return;
            }
            existente.cantidad += 1;
        } else {
            carrito.set(id, {
                id,
                nombre: card.dataset.nombre,
                sku: card.dataset.sku,
                precio: Number(card.dataset.precio),
                maxStock,
                cantidad: 1,
            });
        }
        renderizar();
    }

    document.querySelectorAll(".pos-producto-card").forEach((card) => {
        card.addEventListener("click", () => agregarProducto(card));
    });

    itemsContainer.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-accion]");
        if (!btn) return;
        const id = Number(btn.dataset.id);
        const item = carrito.get(id);
        if (!item) return;

        if (btn.dataset.accion === "sumar") {
            if (item.cantidad + 1 > item.maxStock) {
                mostrarAlerta(`Stock máximo disponible en góndola: ${item.maxStock} u.`);
                return;
            }
            item.cantidad += 1;
        } else {
            item.cantidad -= 1;
            if (item.cantidad <= 0) carrito.delete(id);
        }
        renderizar();
    });

    if (btnVaciar) {
        btnVaciar.addEventListener("click", () => {
            carrito.clear();
            renderizar();
        });
    }

    // Filtro por texto / código de barras (Enter agrega directo si hay 1 coincidencia)
    function filtrarCatalogo(enterPresionado = false) {
        const q = (inputBuscar.value || "").trim().toLowerCase();
        const catActiva = document.querySelector(".pos-cat-btn.activo")?.dataset.cat || "todas";
        const visibles = [];

        document.querySelectorAll(".pos-producto-card").forEach((card) => {
            const coincideTexto =
                !q ||
                card.dataset.nombre.toLowerCase().includes(q) ||
                card.dataset.sku.toLowerCase().includes(q);
            const coincideCat = catActiva === "todas" || card.dataset.categoria === catActiva;
            const mostrar = coincideTexto && coincideCat;
            card.classList.toggle("hidden", !mostrar);
            if (mostrar) visibles.push(card);
        });

        if (enterPresionado && visibles.length === 1) {
            agregarProducto(visibles[0]);
            inputBuscar.value = "";
            filtrarCatalogo(false);
        }
    }

    if (inputBuscar) {
        inputBuscar.addEventListener("input", () => filtrarCatalogo(false));
        inputBuscar.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter") {
                ev.preventDefault();
                filtrarCatalogo(true);
            }
        });
    }

    document.querySelectorAll(".pos-cat-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".pos-cat-btn").forEach((b) => {
                b.classList.remove("activo", "bg-primary", "text-white");
                b.classList.add("bg-surface-container-low", "text-on-surface");
            });
            btn.classList.add("activo", "bg-primary", "text-white");
            btn.classList.remove("bg-surface-container-low", "text-on-surface");
            filtrarCatalogo(false);
        });
    });

    // Dólar bajo demanda
    if (chkUsd && filaUsd) {
        chkUsd.addEventListener("change", () => {
            filaUsd.classList.toggle("hidden", !chkUsd.checked);
            if (chkUsd.checked) {
                if (dolarVenta === null) cargarDolar();
                else actualizarUsd();
            }
        });
    }

    // Envío a domicilio + cálculo de costo
    if (chkEnvio && panelEnvio) {
        chkEnvio.addEventListener("change", () => {
            panelEnvio.classList.toggle("hidden", !chkEnvio.checked);
            if (chkEnvio.checked) {
                cotizarEnvio();
            } else {
                costoEnvio = 0;
                renderizar();
            }
        });
    }

    if (btnCotizarEnvio) {
        btnCotizarEnvio.addEventListener("click", cotizarEnvio);
    }
    if (inputDireccion) {
        inputDireccion.addEventListener("blur", () => {
            if (chkEnvio && chkEnvio.checked && inputDireccion.value.trim()) {
                cotizarEnvio();
            }
        });
    }

    if (selectCliente) {
        selectCliente.addEventListener("change", () => {
            const opt = selectCliente.selectedOptions[0];
            if (opt && inputDireccion && !inputDireccion.value) {
                inputDireccion.value = opt.dataset.direccion || "";
            }
            if (opt && inputTelefono && !inputTelefono.value) {
                inputTelefono.value = opt.dataset.telefono || "";
            }
            if (chkEnvio && chkEnvio.checked) cotizarEnvio();
        });
    }

    // Confirmar cobro
    btnCobrar.addEventListener("click", async () => {
        if (carrito.size === 0) return;
        btnCobrar.disabled = true;

        const payload = {
            items: Array.from(carrito.values()).map((i) => ({
                producto_id: i.id,
                cantidad: i.cantidad,
            })),
            cliente_id: selectCliente.value || null,
            metodo_pago: selectPago.value,
            en_usd: Boolean(chkUsd && chkUsd.checked),
            envio: chkEnvio.checked
                ? {
                      direccion: inputDireccion.value,
                      telefono: inputTelefono.value,
                      observaciones: inputObs.value,
                  }
                : null,
        };

        try {
            const resp = await fetch(btnCobrar.dataset.url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (!resp.ok || !data.ok) {
                mostrarAlerta(data.error || "No se pudo registrar la venta.");
                btnCobrar.disabled = false;
                return;
            }
            window.location.href = data.ticket_url;
        } catch (_err) {
            mostrarAlerta("Error de red al procesar la venta.");
            btnCobrar.disabled = false;
        }
    });

    renderizar();
})();
