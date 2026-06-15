"""
kairos_causal.py
----------------
El motor causal HONESTO de KAIROS.

A diferencia del motor que esta vivo hoy (que fabrica datos y siempre responde),
este orquestador:
  1. Identifica el efecto sobre un DAG que VOS especificas (Gatillo A)
  2. Chequea poder estadistico ANTES de estimar (Gatillo C)
  3. Estima el ATE por backdoor + regresion lineal, con CI por bootstrap
  4. Chequea precision del intervalo (Gatillo C)
  5. Corre los tres refutation tests (Gatillo B)
  6. Devuelve UNA estimacion con CI y refutaciones, O una NEGACION con motivo.

Nunca inventa. Si los datos no alcanzan, se niega y dice por que.

Pensado para correr sobre datos reales: tu export de MercadoLibre via
kairos_loader.py, o tu CSV de lotes de sintesis de CQDs. Es agnostico al dominio.
"""

import json
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from dowhy import CausalModel
import kairos_refusal as kr


# ---------------------------------------------------------------------------
# Construccion del grafo
# ---------------------------------------------------------------------------
def build_graph(treatment: str, outcome: str, confounders: list[str]) -> str:
    """
    Grafo minimo en formato DOT: cada confusor apunta al tratamiento y al
    outcome, y el tratamiento apunta al outcome. Para DAGs mas ricos (mediadores,
    instrumentos) pasa tu propio string DOT a estimate_effect(graph=...).
    """
    edges = [f'"{treatment}" -> "{outcome}";']
    for c in confounders:
        edges.append(f'"{c}" -> "{treatment}";')
        edges.append(f'"{c}" -> "{outcome}";')
    return "digraph {" + " ".join(edges) + "}"


def _bootstrap_ci(df, treatment, outcome, graph, n_boot=300, alpha=0.05, seed=42):
    """CI por bootstrap sobre el ATE (percentil). Reconstruye el modelo en cada
    remuestreo con los mismos parametros explicitos (sin tocar internos de DoWhy)."""
    rng = np.random.default_rng(seed)
    n = len(df)
    effects = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        boot = df.iloc[idx].reset_index(drop=True)
        try:
            bm = CausalModel(data=boot, treatment=treatment, outcome=outcome, graph=graph)
            ident = bm.identify_effect(proceed_when_unidentifiable=True)
            est = bm.estimate_effect(ident, method_name="backdoor.linear_regression")
            effects.append(float(est.value))
        except Exception:
            continue
    if len(effects) < n_boot * 0.5:
        return None, None
    lo = float(np.percentile(effects, 100 * alpha / 2))
    hi = float(np.percentile(effects, 100 * (1 - alpha / 2)))
    return lo, hi


def _run_refutations(model, identified_estimand, estimate) -> dict:
    """Corre los tres refuters de DoWhy y devuelve {nombre: new_effect o None}."""
    out = {}
    specs = [
        ("placebo_treatment", "placebo_treatment_refuter",
         {"placebo_type": "permute", "num_simulations": 20}),
        ("random_common_cause", "random_common_cause",
         {"num_simulations": 20}),
        ("data_subset", "data_subset_refuter",
         {"subset_fraction": 0.8, "num_simulations": 20}),
    ]
    for key, method, kwargs in specs:
        try:
            r = model.refute_estimate(identified_estimand, estimate,
                                      method_name=method, **kwargs)
            out[key] = float(r.new_effect) if np.isscalar(r.new_effect) \
                else float(np.mean(r.new_effect))
        except Exception:
            out[key] = None
    return out


def _sensitivity(model, identified_estimand, estimate) -> dict:
    """Analisis de sensibilidad a confusor NO observado. No detecta el confusor:
    reporta cuanto se movent el ATE si existiera uno de fuerza moderada. Sirve
    para ser honesto sobre la fragilidad de la conclusion, NO para validarla."""
    try:
        r = model.refute_estimate(
            identified_estimand, estimate,
            method_name="add_unobserved_common_cause",
            confounders_effect_on_treatment="linear",
            confounders_effect_on_outcome="linear",
            effect_strength_on_treatment=0.1,
            effect_strength_on_outcome=abs(estimate.value) * 0.5 + 1e-9,
        )
        new = float(r.new_effect) if np.isscalar(r.new_effect) else float(np.mean(r.new_effect))
        return {"ate_con_confusor_moderado": round(new, 6),
                "nota": ("Esto NO detecta confusion oculta. Asume que el DAG esta completo. "
                         "Si omitiste un confusor fuerte, el ATE de arriba ya esta sesgado "
                         "y ningun refuter lo va a avisar.")}
    except Exception:
        return {"ate_con_confusor_moderado": None}




def _dose_response(df, treatment, outcome, confounders, n_points=9):
    """Curva dosis-respuesta CAUSAL, honesta. Solo numpy (sin dependencias nuevas).
    Outcome esperado bajo do(treatment=x), confusores en su media, rango percentil
    10-90 de lo medido (no extrapola). Minimos cuadrados: misma matematica que
    backdoor.linear_regression, asi la pendiente ES el ATE. Devuelve dict o None."""
    try:
        cols = [treatment] + list(confounders)
        Xraw = df[cols].astype(float).values
        n = Xraw.shape[0]
        X = np.column_stack([np.ones(n), Xraw])
        y = df[outcome].astype(float).values
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        t = df[treatment].astype(float).values
        lo, hi = float(np.percentile(t, 10)), float(np.percentile(t, 90))
        xs = np.linspace(lo, hi, n_points)
        conf_means = [float(df[c].astype(float).mean()) for c in confounders]
        ys = []
        for xv in xs:
            row = [1.0, float(xv)] + conf_means
            ys.append(float(np.dot(beta, row)))
        return {
            "x": [round(float(v), 4) for v in xs],
            "y": [round(float(v), 6) for v in ys],
            "xlabel": treatment,
            "ylabel": outcome,
            "nota": ("Outcome esperado bajo do(%s=x), confusores en su media. "
                     "Rango percentil 10-90 de lo medido; no extrapola. "
                     "Recta porque el estimador es lineal; su pendiente es el ATE." % treatment),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------
def estimate_causal_effect(df: pd.DataFrame,
                           treatment: str,
                           outcome: str,
                           confounders: list[str],
                           graph: str | None = None,
                           n_boot: int = 300) -> dict:
    """
    Devuelve un dict con status 'estimate' o 'refused'.
    Aplica los gatillos en orden: A (identif.) -> C (poder) -> estima ->
    C (precision) -> B (robustez).
    """
    result = {
        "query": {"treatment": treatment, "outcome": outcome,
                  "confounders": confounders, "n_obs": int(len(df))},
        "status": None,
        "refusal": None,
        "estimate": None,
        "gates": [],
    }

    # Validacion basica de columnas
    faltan = [c for c in [treatment, outcome] + confounders if c not in df.columns]
    if faltan:
        result["status"] = "refused"
        result["refusal"] = {"gate": "datos",
                             "reason": f"Faltan columnas en los datos: {faltan}."}
        return result

    df = df.dropna(subset=[treatment, outcome] + confounders).reset_index(drop=True)
    graph = graph or build_graph(treatment, outcome, confounders)

    model = CausalModel(data=df, treatment=treatment, outcome=outcome, graph=graph)

    # --- Gatillo A: identificabilidad ---
    identified = model.identify_effect(proceed_when_unidentifiable=True)
    g_id = kr.gate_identifiability(identified)
    result["gates"].append(g_id.__dict__)
    if not g_id.passed:
        result["status"] = "refused"
        result["refusal"] = {"gate": g_id.gate, "reason": g_id.reason, "detail": g_id.detail}
        return result

    # --- Gatillo C: poder estructural (antes de estimar) ---
    g_pow = kr.gate_power_structural(len(df), len(confounders))
    result["gates"].append(g_pow.__dict__)
    if not g_pow.passed:
        result["status"] = "refused"
        result["refusal"] = {"gate": g_pow.gate, "reason": g_pow.reason, "detail": g_pow.detail}
        return result

    # --- Estimacion ---
    estimate = model.estimate_effect(identified,
                                     method_name="backdoor.linear_regression")
    ate = float(estimate.value)
    ci_low, ci_high = _bootstrap_ci(df, treatment, outcome, graph, n_boot=n_boot)
    if ci_low is None:
        result["status"] = "refused"
        result["refusal"] = {"gate": "estimacion",
                             "reason": "El bootstrap no convergio: la estimacion es inestable."}
        return result

    # --- Gatillo C: precision ---
    g_prec = kr.gate_precision(ate, ci_low, ci_high)
    result["gates"].append(g_prec.__dict__)
    if not g_prec.passed:
        result["status"] = "refused"
        result["refusal"] = {"gate": g_prec.gate, "reason": g_prec.reason, "detail": g_prec.detail}
        return result

    # --- Gatillo B: robustez ---
    refutations = _run_refutations(model, identified, estimate)
    g_rob = kr.gate_robustness(ate, refutations)
    result["gates"].append(g_rob.__dict__)
    if not g_rob.passed:
        result["status"] = "refused"
        result["refusal"] = {"gate": g_rob.gate, "reason": g_rob.reason, "detail": g_rob.detail}
        return result

    # --- Pasaron todos: devolver estimacion ---
    result["status"] = "estimate"
    result["estimate"] = {
        "ate": round(ate, 6),
        "ci_low": round(ci_low, 6),
        "ci_high": round(ci_high, 6),
        "n_obs": int(len(df)),
        "adjustment_set": confounders,
        "refutations": refutations,
        "sensibilidad": _sensitivity(model, identified, estimate),
        "dose_response": _dose_response(df, treatment, outcome, confounders),
        "supuesto_critico": ("La validez de este numero depende de que el DAG este "
                             "completo: que hayas medido TODOS los confusores. KAIROS "
                             "verifica identificabilidad, poder y robustez, pero no puede "
                             "verificar que tu grafo este bien. Eso lo aportas vos."),
        "interpretacion": (f"Efecto causal estimado de '{treatment}' sobre '{outcome}': "
                           f"{ate:.4g} (IC95% [{ci_low:.4g}, {ci_high:.4g}]). "
                           f"Identificado por backdoor, robusto a los tres refutation tests."),
    }
    return result


if __name__ == "__main__":
    import sys
    payload = json.loads(sys.stdin.read())
    df = pd.DataFrame(payload["data"]) if "data" in payload \
        else pd.read_csv(payload["csv_path"])
    out = estimate_causal_effect(
        df=df,
        treatment=payload["treatment"],
        outcome=payload["outcome"],
        confounders=payload.get("confounders", []),
        graph=payload.get("graph"),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
