# KAIROS — Desplegar el motor honesto en Render

Backend probado y funcionando. Estos son los pasos que hacés **vos** en tus cuentas
(yo no puedo tocar tu Render, tu GitHub ni tu Vercel). Todo lo de acá ya está testeado.

## Qué hay en el paquete `kairos-backend/`

| Archivo | Rol |
|---|---|
| `app.py` | Servidor FastAPI. Endpoints `/health` y `/causal`. |
| `kairos_causal.py` | El motor honesto (orquestador + gatillos). |
| `kairos_refusal.py` | Los tres gatillos de negación. |
| `requirements.txt` | Dependencias. |
| `render.yaml` | Config de Render (deploy desde repo). |

## Paso 1 — Subir a un repo

Poné los 5 archivos en un repo de GitHub (puede ser uno nuevo, ej. `kairos-motor`,
o una subcarpeta del que ya tenés). Desde la carpeta:

```bash
cd kairos-backend
git init && git add . && git commit -m "Motor causal honesto"
git remote add origin git@github.com:Gianfranco44/kairos-motor.git
git push -u origin main
```

## Paso 2 — Crear el Web Service en Render

1. En Render → **New** → **Web Service** → conectá el repo.
2. Render detecta `render.yaml`. Si no, configurá a mano:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`

> **OJO con el plan — esto importa.** DoWhy + numba + statsmodels comen bastante RAM.
> El **free tier (512MB) probablemente no arranque** o se cuelgue en el primer request.
> Usá el plan **Starter (~USD 7/mes)**. Descomentá la línea `plan: starter` en `render.yaml`.
> Es el costo real de tener un motor causal de verdad corriendo; no hay versión gratis honesta.

## Paso 3 — Variable de entorno

En Render, en el servicio, agregá:

```
KAIROS_ORIGINS = https://kairos-ai.lat,https://www.kairos-ai.lat
```

(Esto es lo que deja que SOLO tu front le pegue. Sin esto, CORS te bloquea.)

Cuando termine de deployar, Render te da una URL tipo
`https://kairos-motor.onrender.com`. Probala:

```bash
curl https://kairos-motor.onrender.com/health
# -> {"status":"ok","engine":"honest","version":"1.0"}
```

## Paso 4 — Apuntar el front (Vercel) al motor honesto

Esto es lo que **desenchufa** el motor que fabrica. En el código del front, donde hoy
llamás a `/api/causal` (Vercel), cambialo por la URL de Render:

```javascript
const KAIROS_API = "https://kairos-motor.onrender.com";

async function consultarCausal(payload) {
  const res = await fetch(`${KAIROS_API}/causal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),  // {treatment, outcome, confounders, data, n_boot}
  });
  return res.json();
}

const r = await consultarCausal(payload);
if (r.status === "refused") {
  // ESTO ES EL PRODUCTO: mostrá el motivo, NO un número inventado.
  mostrarNegacion(r.refusal.reason);
} else {
  // Mostrá el ATE, el IC, y TAMBIÉN r.estimate.supuesto_critico. No lo escondas.
  mostrarEstimacion(r.estimate);
}
```

Y **borrá** del repo de Vercel los archivos `api/causal_engine.py` (el que fabrica) y la
lógica de datos sintéticos. No los comentes: borralos, para que no haya forma de que el
motor mentiroso vuelva a quedar conectado por accidente.

## Sobre la latencia (los ~73s)

El cuello es el bootstrap. Controlalo con `n_boot` en el payload:
- `n_boot: 200` → más preciso, más lento.
- `n_boot: 100` → buen punto para beta.

Render **no corta** como Vercel, pero un request de >60s igual es mala UX y algunos proxies
lo cortan. Para beta, bajá `n_boot` y mostrá un estado de "calculando…" en el front. Si más
adelante necesitás más, el patrón correcto es un job asíncrono (mandás la consulta, devolvés
un id, el front hace polling). Eso es para después, no para el lanzamiento.

## La línea que no quiero que se te pase

Esto deja el motor honesto **vivo y alcanzable**. No deja KAIROS *validado*. Validar es
correr tus lotes reales de CQDs por este endpoint y ver que no contradice tu física. Deploy
≠ validado. Cuando tengas el CSV de síntesis, le pegás a `/causal` con esos datos y ahí sabés
si el motor es real. Hasta entonces tenés una cañería honesta esperando el primer dato verdadero.
```
