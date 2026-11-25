# Invest Bot (ex poly-bot)

Bot de **inversión simulada** (paper trading) para Colombia + global. Sigue ~75 activos
(acciones BVC, ADRs, LatAm, mega caps de EE. UU., ETFs, bonos, oro, cripto), mantiene un
portafolio ficticio en **COP y USD** y manda un **resumen diario al iPhone vía ntfy**.

> Solo dinero ficticio. **No es asesoría financiera.** Plan completo y fases: [`docs/INVEST_BOT_PLAN.md`](docs/INVEST_BOT_PLAN.md).

## Cómo funciona (Fase 1)

Un ciclo por día hábil a las **17:00 hora Bogotá** (después del cierre de BVC y NYSE):

```
yfinance (cierres + dividendos + USD/COP)
  → valoración del portafolio (COP y USD)
  → dividendos abonados (con retención aprox.)
  → estrategias → risk manager → paper executor (cierre ± slippage + comisión)
  → snapshot diario en SQLite → resumen ntfy
```

- **Estrategia `core_allocation`**: pesos fijos (60% global, 20% Colombia, 10% bonos, 10% oro/cripto),
  rebalanceo el primer ciclo de cada mes o si un activo se desvía > 5 pp.
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
