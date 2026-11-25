# Plan: revivir poly-bot como bot de inversión simulada (Colombia + global)

> Contexto para retomar el proyecto en una sesión nueva de Claude Code. Solo dinero ficticio (paper trading). No es asesoría financiera.

## Decisiones tomadas (2026-11-24)

- Fase paper corre **en la Mac** con `docker compose` (+ Cloudflare Tunnel para ver el dashboard desde el iPhone). Costo $0.
- Al pasar el semáforo → mismo `docker compose` a **AWS Lightsail micro_3_0 ($7/mes)** con Caddy (HTTPS, subdominio de `chauxdev.co`) y deploy por GitHub Actions vía SSH.
- Railway/Render se eliminan (`railway.json`, `nixpacks.toml`, `render.yaml`).
- LLM: `claude -p` con la suscripción (sin API keys). Notificaciones: ntfy primero.
- Cambio de modo basado en evidencia (~4–6 semanas paper + backtest), no en 3 meses fijos.

## Estado actual (abril 2026)

- Bot de Polymarket (mercados de predicción) en Python 3.12 + uv, FastAPI + WebSocket dashboard, SQLite, structlog.
- Estrategias: `ai_research` (LLM vía OpenRouter free + DuckDuckGo), `mean_reversion`, `momentum` (off).
- Resultados en `data/poly_bot.db`: 66 fills (46 mean_reversion, 20 ai_research) entre 2026-03-26 y 2026-04-03, P&L realizado 0, win rate muy bajo (commit `24e5373` menciona 3.9%). Último snapshot: 2026-04-03.
- Desplegado antes en Railway/Render; hay `com.polybot.plist` para launchd local.
- Problemas conocidos: modelos free de OpenRouter con rate-limit/404, Polymarket CLOB bloqueado por IP en Railway, señales de baja calidad.

## Objetivo nuevo

Bot que **sugiere y simula inversiones** en:
- **Colombia**: acciones BVC (Ecopetrol, Bancolombia/PF Bancolombia, ISA, GEB, Grupo Aval, Cementos Argos, etc.), ETF COLCAP, ADRs en NYSE (`EC`, `CIB`, `AVAL`), ETF `GXG`, dólar `COP=X`.
- **Global**: ETFs índice (`SPY`, `QQQ`, `VT`, `ACWI`), acciones grandes, bonos (`TLT`, `BND`), oro (`GLD`), cripto (`BTC-USD`, `ETH-USD`).
- Portafolio ficticio en COP y USD (ej. 10.000.000 COP + 5.000 USD), con conversión por TRM.

## Restricciones del usuario

- **Sin API keys de LLM.** Usar la suscripción de Claude Code CLI (o Antigravity CLI):
  - Local / servidor: `claude -p "<prompt>" --output-format json` en modo headless.
  - En servidor sin navegador: `claude setup-token` en la Mac → exportar `CLAUDE_CODE_OAUTH_TOKEN` en el servidor.
  - Alternativa sin servidor: routines de Claude Code (`/schedule`) para el análisis diario.
  - Consume los mismos límites de uso de la suscripción → pocas llamadas por día (ej. 1 análisis diario + 1 semanal), no por tick.
- Costo de infra ~$0: correr en la Mac (launchd) u Oracle Cloud Free Tier. La infra AWS vieja se borró el 2026-11-24.

## Qué reusar vs cambiar

| Reusar | Cambiar |
|---|---|
| `strategies/base.py` (Strategy ABC + Signal) | `market_data/*` → nuevo proveedor de precios (yfinance / Stooq / CoinGecko / datos.gov.co para TRM) |
| `execution/paper_executor.py` (adaptar: fill al precio de cierre/último + comisión simulada) | Quitar CLOB/order book de Polymarket, `live_executor.py` |
| `risk/manager.py` (límites por posición/exposición) | Añadir límites por clase de activo, país y moneda |
| `portfolio/*`, `store/*` (SQLite) | Multi-moneda (COP/USD), dividendos |
| `web/api.py` + dashboard | Convertir en PWA con Web Push (ver notificaciones) |
| `ai_research.py` patrón research→decisión→evento | Reemplazar OpenRouter por `claude -p`; frecuencia diaria |

## Estrategias propuestas (evaluables con backtest)

1. **Asignación base (core)**: portafolio diversificado con rebalanceo mensual (ej. 60% global, 20% Colombia, 10% bonos, 10% oro/cripto). Benchmark contra `VT` y COLCAP.
2. **Momentum / tendencia** con medias móviles (50/200) y filtros de volatilidad — clásico, fácil de backtestear.
3. **Dividendos Colombia**: seguimiento de dividend yield de acciones BVC.
4. **Analista LLM (diario)**: Claude lee precios, noticias (DuckDuckGo/RSS de La República, Portafolio, Valora Analitik, Reuters) y la cartera; devuelve JSON `{acciones: [{ticker, accion: BUY|SELL|HOLD, peso, confianza, razon}]}`. El risk manager valida antes de ejecutar.
5. **Backtesting obligatorio** antes de habilitar una estrategia (lección del 3.9% win rate): `vectorbt` o backtest propio sobre histórico de yfinance; métricas: CAGR, Sharpe, max drawdown vs benchmark.

## Notificaciones al iPhone

Orden recomendado:
1. **ntfy.sh** (empezar aquí): app iOS gratis, se publica con un `POST https://ntfy.sh/<topic-secreto>`. Cero cuentas, 10 minutos de trabajo. Mensajes: resumen diario, señal nueva, stop-loss, cambio fuerte de TRM.
2. **PWA + Web Push**: el dashboard FastAPI instalado en la pantalla de inicio (iOS 16.4+ soporta push para PWAs). Sin pagar Apple Developer. Requiere HTTPS (ej. Cloudflare Tunnel o Vercel para el frontend).
3. **App nativa** (SwiftUI o Expo/React Native): solo si se quiere de verdad; requiere Apple Developer ($99/año) para APNs y distribución.
- Alternativa: bot de Telegram (gratis, permite comandos como `/cartera`, `/comprar`).

## Fases sugeridas

1. **MVP (1–2 sesiones)**: proveedor de precios (yfinance), portafolio paper multi-moneda, estrategia core + rebalanceo, resumen diario a ntfy.
2. **Analista LLM** con `claude -p` 1 vez al día, guardando razonamiento en `agent_events`.
3. **Backtester + métricas vs benchmark** en el dashboard.
4. **PWA + Web Push** (o Telegram con comandos).
5. Opcional: app nativa.

## Graduación: simulado → asesor → real

El objetivo a futuro es usarlo con dinero real, pero solo después de que el bot demuestre buen comportamiento. Tres modos, controlados por config (`mode: paper | advisor | live`):

1. **paper** (inicio): todo simulado. Se mide desempeño.
2. **advisor**: el bot propone operaciones concretas (ticker, monto, razón, stop-loss) y notifica al iPhone; **el usuario ejecuta manualmente** en su broker y confirma (`/ejecute`, `/ignore`). Se registra qué habría pasado si se seguía cada consejo.
3. **live** (opcional, último): ejecución automática vía API de broker, con límites duros.

### Criterios para pasar de paper a advisor (todos)
- Decisión basada en **evidencia, no en calendario**: ~4–6 semanas de paper en vivo **más** backtest + walk-forward histórico. El paper en vivo solo confirma que el backtest se reproduce con datos reales.
- Muestra mínima: ≥ 30 decisiones cerradas (o ≥ 20 rebalanceos/operaciones) — con menos, el resultado es ruido.
- Retorno ajustado por riesgo (Sharpe) ≥ benchmark (`VT` global, COLCAP Colombia), neto de comisiones simuladas e impuestos aproximados.
- Max drawdown ≤ benchmark o ≤ 15%.
- Backtest de ≥ 5 años consistente con el paper en vivo (sin overfitting).
- Razonamientos del LLM auditables y sin errores factuales graves en revisión manual de una muestra.

### Dashboard de decisión ("¿está listo?")
Vista principal para decidir el cambio de modo, sin tener que interpretar logs:
- **Scorecard semáforo** por criterio (verde/amarillo/rojo): Sharpe vs benchmark, max drawdown, nº de decisiones, acierto, consistencia backtest vs vivo. Botón "pasar a advisor" habilitado solo con todo en verde.
- **Curva de capital vs benchmarks** (VT, COLCAP, CDT/tasa libre de riesgo en COP) en la misma gráfica, en COP y USD.
- **Drawdown** (área bajo cero) y volatilidad móvil.
- **Bitácora de decisiones**: cada recomendación con razón del LLM, precio de entrada, resultado a 1/7/30 días y si fue acierto.
- **Acierto por estrategia y por activo** (barras), y asignación actual (por país, clase de activo, moneda).
- **Backtest vs en vivo**: superponer ambas curvas para detectar sobreajuste.
- Responsive / PWA para verlo bien en el iPhone.

### Criterios para pasar de advisor a live
- ~1–2 meses en advisor (ejecución manual con montos pequeños) con consejos que superen el benchmark.
- Capital inicial pequeño (dinero que se puede perder) y límites duros en código: monto máx por orden, pérdida diaria/mensual máx (kill switch), lista blanca de activos (preferir ETFs líquidos), sin apalancamiento, sin opciones/derivados.
- Toda orden > umbral requiere confirmación desde el iPhone.

### Brokers con API (verificar disponibilidad para residentes en Colombia)
- **Interactive Brokers** (TWS API / Client Portal API): acepta residentes en Colombia, acceso a mercados globales. Opción más probable.
- **Alpaca**: API muy simple; verificar si abre cuentas internacionales para Colombia.
- Brokers locales (Trii, comisionistas BVC): normalmente sin API pública → para Colombia usar modo advisor (ejecución manual) o ADRs/`GXG` vía broker internacional.
- Cripto: exchanges con API (Binance, etc.) — verificar regulación vigente.

### Legal / responsabilidad
- Uso **personal** con dinero propio: sin problema regulatorio especial.
- Ofrecer asesoría a **terceros** en Colombia requiere registro ante la SFC / AMV (RNPMV). No convertirlo en servicio público sin asesoría legal.
- Impuestos: ganancias ocasionales/renta y declaración de inversiones en el exterior (DIAN / Banco de la República). Que el bot registre todo para el reporte anual.

## Despliegue en AWS (precios consultados 2026-11-24, us-east-1)

La cuenta no tiene créditos free tier (plan PAID, $0 créditos). Requisitos: Python bot (~150 MB RAM) + Claude CLI Node (~300–500 MB) → mínimo 1 GB, cómodo 2 GB.

| Opción | Costo/mes | Notas |
|---|---|---|
| **Lightsail micro_3_0** (1 GB, 2 vCPU, 40 GB, IPv4, transfer incluido) | **$7** | Recomendado. Precio fijo, añadir swap 1 GB |
| Lightsail small_3_0 (2 GB) | $12 | Si 1 GB se queda corto |
| Lightsail micro IPv6-only | $5 | Riesgo: algunas APIs/redes sin IPv6 |
| EC2 t4g.micro ($0.0084/h) + EBS 8 GB + IPv4 | ~$10.40 | Más caro que Lightsail por la IP pública ($3.65) |
| EC2 t4g.small ($0.0168/h) + EBS + IPv4 | ~$16.70 | |
| Serverless: Lambda (contenedor con Claude CLI) + EventBridge + DynamoDB + S3/CloudFront | ~$0–1 | Más barato, pero requiere rediseño: dashboard estático, sin WebSocket |

Extras: HTTPS con Caddy + Let's Encrypt (gratis), dominio `chauxdev.co` ya existe (DNS en Vercel), ntfy gratis, LLM incluido en la suscripción de Claude. Backups: snapshot Lightsail ~$0.05/GB-mes.

Alternativa $0 durante la fase paper: correr en la Mac con launchd (`com.polybot.plist`) + Cloudflare Tunnel para ver el dashboard desde el iPhone.

## Verificar antes de implementar

- Sufijo de Yahoo Finance para tickers BVC (probar `ECOPETROL.CL`, `PFBCOLOM.CL`, etc.) y si hay datos confiables; si no, usar ADRs + `GXG`, o scraping de la BVC.
- Licencia/uso de datos de cada fuente.
- Formato estable de salida JSON de `claude -p` (`--output-format json`, `--json-schema` si está disponible en la versión instalada).
