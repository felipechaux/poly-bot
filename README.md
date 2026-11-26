# Invest Bot (ex poly-bot)

Bot de **inversión simulada** (paper trading) para Colombia + global. Sigue ~75 activos
(acciones BVC, ADRs, LatAm, mega caps de EE. UU., ETFs, bonos, oro, cripto), mantiene un
portafolio ficticio en **COP y USD** y manda un **resumen diario al iPhone vía ntfy**.

> Solo dinero ficticio. **No es asesoría financiera.** Plan completo y fases: [`docs/INVEST_BOT_PLAN.md`](docs/INVEST_BOT_PLAN.md).

## Cómo funciona (Fase 1)

Un ciclo por día hábil a las **8:00 am hora Bogotá** (antes de la apertura; usa los cierres del día anterior):

```
yfinance (cierres + dividendos + USD/COP)
  → valoración del portafolio (COP y USD)
  → dividendos abonados (con retención aprox.)
  → estrategias → risk manager → paper executor (cierre ± slippage + comisión)
  → snapshot diario en SQLite → resumen ntfy
```

- **Estrategia `core_allocation`** (85% de la cartera): pesos fijos (60% global, 20% Colombia, 10% bonos,
  10% oro/cripto), rebalanceo el primer ciclo de cada mes o si un activo se desvía > 5 pp.
- **Analistas IA `llm_claude` y `llm_gemini`** (7,5% de la cartera cada uno): cada mañana reciben los
  mismos datos (indicadores del universo sin los tickers de la core, la cartera, su propio historial de
  aciertos y ~30 titulares RSS) y responden JSON con esquema fijo. Cada uno opera su propia cartera
  satélite y no puede comprar lo que ya tiene el otro, así que el dashboard los compara lado a lado
  (P&L, % de acierto a 1/7/30 días, tokens).
  - Claude: `claude -p`, Opus 5.5, con tu suscripción. El bot elimina `ANTHROPIC_API_KEY` del entorno.
    Medido: ~9k tokens y ~20 s por llamada (~US$ 0,08 equivalente, incluido en el plan).
  - Gemini: `agy -p` (Antigravity CLI), Gemini 4 Neon High, con tu cuenta de Google. El bot elimina
    `GEMINI_API_KEY`/`GOOGLE_API_KEY` y le pasa un entorno mínimo. Como `agy` no permite desactivar sus
    herramientas, cada llamada corre en una carpeta vacía con `--sandbox`, modo plan y reglas que
    niegan leer o escribir archivos, comandos, URLs y MCP. Medido: ~56k tokens y ~170 s por llamada.
  - Límites duros por analista: confianza ≥ 0.70, ≤ 4% por posición, ≤ 3 posiciones, ≤ 2 entradas
    nuevas/día, stop-loss −15%. Cada idea queda en la bitácora con su resultado. El botón de dry run
    nunca llama a ningún modelo.
- **Ejecución a la apertura**: a las 8:00 se decide y las órdenes de BVC y EE. UU. quedan pendientes;
  a las 16:30 se ejecutan al **precio de apertura** de la primera sesión posterior a la decisión (nunca
  a una apertura ya conocida) y llega un push con lo ejecutado, o «sin órdenes» si no hubo nada, para
  que un día tranquilo no se confunda con un bot caído. Cripto (24/7) se ejecuta al momento.
  Una nueva decisión reemplaza las órdenes pendientes de esa estrategia que aún no llegaron a su
  apertura; sin apertura en 3 días hábiles, la orden vence. `execution.fill: close` vuelve al modo
  anterior (todo al último cierre).
- **Escaneos Londres / Nueva York** (`schedule.scans`): a las 08:00 de Londres (≈ 03:00 Bogotá) y a las
  10:00 de Nueva York (solape Londres–NY) solo los analistas vuelven a mirar precios y noticias. Cripto
  se ejecuta en el momento; BVC/EE. UU. quedan para la próxima apertura. Un escaneo solo agrega ideas
  (nunca cancela las órdenes de la mañana) y el tope de entradas nuevas por día cuenta para todas las
  corridas. Solo hay push si deciden algo. Cada escaneo cuesta 1 llamada a Claude + 1 a Gemini.
- **Binance Spot Testnet** (`execution.binance_testnet`): con llaves del testnet en `.env`, las órdenes
  cripto se envían como órdenes market reales a testnet.binance.vision (dinero falso) y se registran
  con el precio, la cantidad y la comisión que devuelve Binance. Si el testnet falla o su precio se
  aleja más de 2% del mercado real, esa orden se simula como antes y el resumen lo avisa.
  `poly-bot binance` revisa las llaves, los precios y los saldos.
- **Semáforo por analista**: cada idea se compara con una canasta equiponderada de los activos que
  pudo escoger, en la misma ventana (exceso = retorno de la idea − canasta). Gris con < 20 ideas a 7
  días; verde o rojo solo con t ≥ 2 a favor o en contra; amarillo en otro caso.
- **Freno automático**: si la cartera de un analista pierde 10% de su tope o su semáforo se pone rojo,
  deja de comprar (sí puede vender y el stop-loss sigue activo) y el resumen diario lo avisa.
- **Costos simulados**: BVC 0,3% (mín. $10.000 COP) + slippage 0,2%, acciones enteras;
  EE. UU. 0,1% (mín. US$1), fraccionadas; cripto 0,1%; spread FX COP↔USD 0,5%.
- **Riesgo**: lista blanca (universo), peso máx. por activo / clase / región, precios desactualizados, sin cortos.
- **Estado**: todo es una transacción en `data/invest_bot.db` (ledger); al reiniciar se reconstruye.
  La base vieja de Polymarket (`data/poly_bot.db`) queda intacta.
- **Benchmarks**: `VT` (global) e `ICOLCAP.CL` (proxy de COLCAP; `^COLCAP` no tiene datos en Yahoo).

Todo se configura en [`config/default.yaml`](config/default.yaml): capital inicial, universo,
pesos objetivo, costos, límites y horario.

## Uso

```bash
cp .env.example .env      # define NTFY_TOPIC (ver abajo)
make setup

make prices REGION=CO     # cotizaciones + tendencia del universo
make dry-run              # simula el ciclo de hoy sin ejecutar nada
make cycle                # ejecuta el ciclo de hoy + notifica
make run                  # scheduler diario + dashboard en http://localhost:8080
make positions / make trades / make notify-test
```

### Docker (recomendado en la Mac)

Docker corre con [Colima](https://github.com/abiosoft/colima) (sin Docker Desktop):

```bash
brew install colima docker docker-compose
brew services start colima        # arranca solo al iniciar sesión
docker compose up -d --build      # bot + túnel
docker compose logs -f invest-bot
make tunnel-url                   # URL pública HTTPS del dashboard (para el iPhone)
```

- Monta `./data` (SQLite) y `./config` (solo lectura): edita el YAML y `docker compose restart invest-bot`.
- El dashboard queda en `http://localhost:8080` (solo local) y por el túnel rápido de Cloudflare
  (`*.trycloudflare.com`, **cambia en cada reinicio del contenedor `tunnel`**). Una URL fija requiere
  un túnel con nombre en una zona DNS gestionada por Cloudflare.
- La Mac debe estar encendida y despierta a las 17:00; si el ciclo se perdió, corre al arrancar.
- Ejecutar un ciclo real por API exige `ADMIN_TOKEN`; el botón de simulación (dry run) no.

### Login de Claude en el servidor (suscripción, sin API key)

Una sola vez, igual que en la Mac:

```bash
ssh -t ubuntu@<servidor> 'cd ~/invest-bot && docker compose exec -it invest-bot claude'
# dentro: /login → abre la URL en tu navegador, autoriza, pega el código → /exit
```

La sesión se guarda en el volumen `claude_auth` (`CLAUDE_CONFIG_DIR`) y sobrevive a reinicios y
deploys. Alternativa: `claude setup-token` en la Mac y `CLAUDE_CODE_OAUTH_TOKEN=...` en `.env`.

### Login de Gemini (Antigravity CLI) en el servidor

```bash
ssh -t ubuntu@<servidor> 'cd ~/invest-bot && docker compose exec -it invest-bot agy'
# abre la URL en tu navegador (únela si sale partida), autoriza, pega el código → /exit
```

La sesión se guarda en el volumen `gemini_auth` (`~/.gemini`). `agy` está fijado a una versión con su
SHA-512 en el `Dockerfile` (el manifest oficial indica cómo actualizarlo).

### Notificaciones al iPhone (ntfy)

1. Instala **ntfy** desde el App Store.
2. Genera un topic secreto: `python3 -c "import secrets; print('invest-' + secrets.token_urlsafe(16))"`.
3. Ponlo en `.env` como `NTFY_TOPIC=...` y suscríbete al mismo topic en la app.
4. `make notify-test`.

## Fuentes de datos

Yahoo Finance vía `yfinance` (uso personal, no comercial). Tickers BVC usan sufijo `.CL`.
Notas verificadas el 2026-11-24: Bancolombia ahora es Grupo Cibest (`CIBEST.CL`, `PFCIBEST.CL`, ADR `CIB`);
`GXG` ahora es `COLO`; las series BVC traen filas duplicadas y festivos que el proveedor filtra.

## Desarrollo

```bash
make test   # pytest (sin red: proveedor de precios falso)
make lint
```
