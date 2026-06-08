"""
app.py — Servidor KAIROS (motor causal honesto)
================================================
Expone el motor honesto por HTTP para que el front (Vercel) le pegue.
Reemplaza al backend que fabrica. Se despliega en Render como Web Service.

Endpoints:
  GET  /health   -> chequeo de vida (Render lo usa para saber si arrancó)
  POST /causal   -> corre el motor; devuelve estimacion O negacion honesta

El cuerpo de /causal es el MISMO contrato que ya tenias:
  {
    "treatment": "ph",
    "outcome": "lambda_max",
    "confounders": ["ratio_acido_urea", "temp_reflujo"],
    "data": [ {fila}, {fila}, ... ],   # o "csv_text": "col1,col2\n..."
    "graph": "digraph {...}",          # opcional; si falta se arma uno minimo
    "n_boot": 200                      # opcional; menos = mas rapido
  }
"""
import io
import os
import json
import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any

from kairos_causal import estimate_causal_effect

app = FastAPI(title="KAIROS - Motor Causal Honesto", version="1.0")

# --- CORS: solo tus dominios. Cambia/agrega los que uses. -------------------
# Podes setear KAIROS_ORIGINS en Render como "https://kairos-ai.lat,https://..."
_default_origins = "https://kairos-ai.lat,https://www.kairos-ai.lat"
ALLOWED = [o.strip() for o in os.getenv("KAIROS_ORIGINS", _default_origins).split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class CausalRequest(BaseModel):
    treatment: str
    outcome: str
    confounders: list[str] = []
    data: Optional[list[dict[str, Any]]] = None
    csv_text: Optional[str] = None
    graph: Optional[str] = None
    n_boot: int = 200


@app.get("/health")
def health():
    return {"status": "ok", "engine": "honest", "version": "1.0"}


@app.post("/causal")
def causal(req: CausalRequest):
    # Construir el dataframe desde data (lista de dicts) o csv_text
    if req.data is not None:
        df = pd.DataFrame(req.data)
    elif req.csv_text is not None:
        df = pd.read_csv(io.StringIO(req.csv_text))
    else:
        return {"status": "refused",
                "refusal": {"gate": "datos",
                            "reason": "No mandaste datos: falta 'data' o 'csv_text'."}}

    if len(df) == 0:
        return {"status": "refused",
                "refusal": {"gate": "datos", "reason": "El dataset llego vacio."}}

    result = estimate_causal_effect(
        df=df,
        treatment=req.treatment,
        outcome=req.outcome,
        confounders=req.confounders,
        graph=req.graph,
        n_boot=max(50, min(req.n_boot, 500)),  # cota de seguridad
    )
    return result


# Permite correr local con: python app.py
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
