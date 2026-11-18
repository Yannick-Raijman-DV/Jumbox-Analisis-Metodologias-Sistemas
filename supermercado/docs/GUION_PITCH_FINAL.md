# Guion de Defensa y Pitch Final — JUMBOX (12 minutos)

> Alineado punto por punto con la rúbrica de `Presentacion final pitch.pptx` y `Parcial2.docx` sobre la presentación armada en `Presentacion_Final_Jumbox_Pitch.pptx`.

---

## 1. Distribución de tiempos y roles (12 min + 3–5 min de preguntas)

| Tramo | Tiempo | Quién expone | Qué cuenta y muestra |
|---|---|---|---|
| **1. Problema y usuario** | 1 min | **Yannick Raijman** | Supermercados minoristas de cercanía (Cajero, Operario, Admin). Cómo operaban antes (Java Swing + MySQL local, Excel/WhatsApp) y por qué perdían **12%–18%** mensual por vencimientos no alertados. Sin mencionar Flask ni BD todavía. |
| **2. Del 1er parcial al producto** | 1,5 min | **Yannick / Valentino** | Coherencia SRS, Casos de uso, Diagrama de Clases/E-R (14 tablas migradas de `supermercado.sql` + 4 nuevas) y Mockups → Web responsive. Qué se mantuvo, qué cambió y por qué (ej. Open Food Facts descartado porque las fotos propias viven en S3). |
| **3. Demo en vivo (Render)** | 3 min | **Francisco Luzzi** | Un único recorrido feliz de punta a punta en **Render** (ventana de incógnito), narrando cada integración en voz alta. |
| **4. Integraciones y arquitectura** | 2,5 min | **Nicolás De Cabo / Valentino** | Qué mejora al usuario cada integración (Google Auth, AWS S3 privado, IA Qwen2.5, Geoapify, Mercado Pago y DolarAPI) y cómo está organizado el backend (8 Blueprints, `routes → services → models → extensions`, `.env`, `s3-check`, 101 tests). |
| **5. Benchmarking y valor** | 2 min | **Yannick / Francisco** | Comparativa real del 1er parcial (**Loyverse POS**, **Odoo POS**, **Holded ERP** vs **Jumbox**), mapa de posicionamiento y propuesta de valor. |
| **6. Modelo económico y cierre** | 2 min | **Yannick Raijman** | Suscripción SaaS por sucursal + implementación corporativa, costos cloud reales (**USD 37/mes**), punto de equilibrio (**2 sucursales**), **ROI 58,4%** (Payback Mes 4), resultados, desafíos y roadmap v2 (**Supabase + FEFO**). |

---

## 2. Guion paso a paso de la Demo en Vivo (3–4 minutos)

### Preparación 5 minutos antes de exponer
1. Abrir la URL de **Render** en una **ventana de incógnito** para despertar el servidor (cold start del plan Free).
2. Tener a mano los datos de prueba:
   - Cuenta de Google habilitada o usuario demo (`administrador` / `1234`, `cajero` / `1234`, `operario` / `1234`).
   - Comprador de prueba Mercado Pago (`test_user_652089629429375203@testuser.com`) y tarjeta de prueba: **`5031 7557 3453 0604`**, CVV **`123`**, vto **`11/30`**, titular **`APRO`**, DNI **`12345678`**.
   - Plan B listo: captura o video corto de 2 minutos grabado previamente por si falla el proyector o internet.

### Recorrido feliz en vivo
1. **Login con Google y vistas por rol (30 s):**
   - En `/login`, mostrar el botón **Continuar con Google** (o mostrar la pantalla `/auth/elegir-rol` para cuentas nuevas entre Operario y Cajero).
   - Recalcar en voz alta: *"Acá entra **Google OAuth**: el empleado ingresa en 1 clic sin recordar contraseñas nuevas y ve un inicio adaptado a su rol."*
2. **Caja POS + Delivery Geoapify + DolarAPI + Mercado Pago (1 min 30 s):**
   - Ir a **Caja / POS** (`/pos`). Agregar 2 productos al ticket (`CocaCola`, `Lays`).
   - Tildar **Enviar a domicilio**, escribir `Av. Cabildo 1540, CABA` y tocar **Calcular**: mostrar que **Geoapify** calcula km y costo y **lo suma automáticamente al total**.
   - (Opcional rápido) Tildar **Ver conversión / pagar en dólares (USD)** para mostrar que **DolarAPI** consulta bajo demanda y congela el tipo de cambio en el ticket.
   - Elegir medio de pago **Mercado Pago** → **Cobrar y emitir ticket** → en el ticket tocar **Pagar con Mercado Pago**.
   - Pagar como invitado / comprador de prueba (`APRO`). Al regresar al ticket mostrar el badge **Pagado (acreditado)**. Recalcar: *"El estado real se valida del lado servidor contra `GET /v1/orders/{id}`, nunca confiando en la URL."*
3. **Stock con AWS S3 privado + Envíos (45 s):**
   - Entrar a **Inventario** (`/stock`). Mostrar que todas las fotos de productos cargan desde el bucket privado **`jumbox`** (`sa-east-1`).
   - (Demostración rápida de seguridad S3) Clic derecho en una imagen → abrir en pestaña nueva (se ve porque trae `X-Amz-Signature`) → borrar desde el `?` en adelante y recargar → AWS devuelve **`403 AccessDenied`**.
   - Entrar rápido a **Envíos** (`/envios`) y mostrar el mapa proxy (`/envios/mapa.png`) sin `apiKey` expuesta.
4. **IA Local de Reposición en acción (45 s):**
   - Entrar a **IA Reposición** (`/ia/reposicion`). Mostrar cómo Python priorizó los candidatos (qué producto vencido va a Mermas con `?producto_id=...`, cuál trasladar de Depósito a Góndola y cuál pedir al proveedor) junto con el informe redactado por la IA y el botón **Emitir Pedido Automático** en 1 clic.

---

## 3. Respuestas rápidas para las preguntas del jurado

- **¿Por qué Flask modular y no FastAPI o un único `app.py`?**
  Porque la cátedra exige Flask con Application Factory y Blueprints (`routes → models → extensions`). Dividir en 8 Blueprints y capa `services/` permite que el código sea testeable (`101 tests`) y mantenible sin acoplar rutas con lógica de negocio.
- **¿Por qué eligieron AWS S3 si usan SQLite local?**
  En Render el sistema de archivos es efímero: cada deploy recrea el disco y borraría fotos o comprobantes subidos. Al guardar solo `foto_key` en la base y el archivo en el bucket privado `jumbox` (con `sincronizar_fotos_s3()`), los activos persisten y son accesibles desde cualquier dispositivo mediante URLs prefirmadas.
- **¿Cómo evitan que una LLM pequeña (`Qwen2.5-0.5B`) invente precios o cantidades?**
  Separando cálculo y redacción: `inventario.calcular_candidatos_reposicion()` calcula en Python las cantidades y montos exactos; la LLM solo redacta el dictamen ejecutivo como *singleton* y pasa por `_es_respuesta_precisa()`. Si alucina o en Render (`LLM_ENABLED=false`) hay tope de 512 MB RAM, actúa automáticamente el fallback determinístico.
- **¿Cómo aseguran que las API keys no se filtren al navegador?**
  Viven exclusivamente en `.env` y en las variables de Render. Mercado Pago recibe `Bearer` desde Flask (`services/mercadopago.py`) y Geoapify pasa por la ruta proxy `/envios/mapa.png`.
- **¿Cuál es la evolución técnica v2?**
  Apuntar `SQLALCHEMY_DATABASE_URI` (`DATABASE_URL`) a **Supabase / PostgreSQL** para persistencia permanente en la nube y agregar la entidad `Lote` para trazabilidad FEFO (*First Expired, First Out*).
