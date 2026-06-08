"""
kairos_refusal.py
-----------------
Los tres gatillos de negacion de KAIROS.

Esta es la parte que hace a KAIROS distinto de un dashboard: en vez de devolver
SIEMPRE un numero, el motor se NIEGA cuando los datos no permiten estimar un
efecto causal honesto. Cada gatillo es una funcion pura y transparente que
devuelve (paso, motivo, detalle). Los umbrales estan arriba, a la vista, para
que puedas discutirlos y calibrarlos. Nada de magia escondida.

Gatillo A  -> identificabilidad : si no hay adjustment set valido -> NIEGA
Gatillo C  -> poder estadistico : si N es insuficiente para el set -> NIEGA
Gatillo B  -> robustez          : si los refutation tests fallan   -> NIEGA
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Umbrales. Todo a la vista. Esto es lo que vas a calibrar con tus lotes reales.
# ---------------------------------------------------------------------------

# Gatillo C - poder estadistico
MIN_OBS_PER_PARAM = 10          # observaciones minimas por parametro del modelo
                                # (params = 1 tratamiento + k confusores + 1 intercepto)

# Gatillo C - precision (sobre el intervalo bootstrap)
MAX_REL_CI_HALFWIDTH = 5.0      # si el semi-ancho del CI supera 5x |ATE| -> no informativo
                                # (el efecto no se distingue de "nada")

# Gatillo B - robustez (refutation tests). Criterios transparentes calculados
# a mano sobre new_effect vs efecto original, sin depender de la semantica de
# p-value de DoWhy (que cambia entre versiones).
PLACEBO_MAX_REL          = 0.15  # placebo: |new| debe colapsar a <15% del original
RANDOM_CAUSE_MAX_REL     = 0.10  # random common cause: el ATE no debe moverse >10%
DATA_SUBSET_MAX_REL      = 0.20  # data subset: el ATE debe ser estable a <20%


@dataclass
class GateResult:
    """Resultado de un gatillo individual."""
    gate: str
    passed: bool
    reason: str
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gatillo A - Identificabilidad
# ---------------------------------------------------------------------------
def gate_identifiability(identified_estimand) -> GateResult:
    """
    Recibe el IdentifiedEstimand de DoWhy (model.identify_effect()).
    NIEGA si no existe ningun estimando usable (backdoor / iv / frontdoor).
    """
    estimands = getattr(identified_estimand, "estimands", {}) or {}

    def usable(key):
        e = estimands.get(key)
        if not e:
            return False
        # DoWhy guarda None o un dict vacio cuando no hay set valido
        if isinstance(e, dict):
            expr = e.get("estimand") or e.get("backdoor_variables")
            return bool(expr)
        return True

    backdoor = usable("backdoor")
    iv = usable("iv")
    frontdoor = usable("frontdoor")

    if backdoor or iv or frontdoor:
        method = "backdoor" if backdoor else ("iv" if iv else "frontdoor")
        return GateResult(
            gate="identificabilidad",
            passed=True,
            reason=f"Efecto identificable por {method}.",
            detail={"metodo": method},
        )

    return GateResult(
        gate="identificabilidad",
        passed=False,
        reason=("El efecto NO es identificable con el grafo y las variables que diste. "
                "No existe un conjunto de ajuste (backdoor), ni instrumento, ni frontdoor "
                "valido. KAIROS no inventa un numero: te falta medir alguna variable o el "
                "DAG no permite separar el efecto causal de la confusion."),
        detail={"estimands_disponibles": list(estimands.keys())},
    )


# ---------------------------------------------------------------------------
# Gatillo C - Poder estadistico (estructural, ANTES de estimar)
# ---------------------------------------------------------------------------
def gate_power_structural(n_obs: int, n_adjustment: int) -> GateResult:
    """
    NIEGA si no hay suficientes observaciones por parametro del modelo.
    params = 1 (tratamiento) + n_adjustment (confusores) + 1 (intercepto)
    """
    n_params = 1 + n_adjustment + 1
    needed = n_params * MIN_OBS_PER_PARAM
    ratio = n_obs / n_params if n_params else 0.0

    if n_obs >= needed:
        return GateResult(
            gate="poder",
            passed=True,
            reason=f"N suficiente: {n_obs} obs para {n_params} parametros "
                   f"({ratio:.1f} obs/param, minimo {MIN_OBS_PER_PARAM}).",
            detail={"n_obs": n_obs, "n_params": n_params, "obs_por_param": round(ratio, 2)},
        )

    return GateResult(
        gate="poder",
        passed=False,
        reason=(f"Poder estadistico insuficiente: {n_obs} observaciones para {n_params} "
                f"parametros ({ratio:.1f} obs/param). KAIROS necesita al menos "
                f"{MIN_OBS_PER_PARAM} obs por parametro ({needed} en total) para estimar "
                f"algo no espurio. Con esta cantidad cualquier numero seria ruido disfrazado "
                f"de resultado."),
        detail={"n_obs": n_obs, "n_params": n_params, "n_necesario": needed,
                "obs_por_param": round(ratio, 2)},
    )


# ---------------------------------------------------------------------------
# Gatillo C - Precision (sobre el intervalo bootstrap, DESPUES de estimar)
# ---------------------------------------------------------------------------
def gate_precision(ate: float, ci_low: float, ci_high: float) -> GateResult:
    """
    NIEGA si el intervalo es tan ancho que el efecto no se distingue de cero
    o de su propio signo. Un ATE con un CI gigante no es una estimacion: es
    una opinion con barras de error.
    """
    halfwidth = (ci_high - ci_low) / 2.0
    crosses_zero = ci_low < 0 < ci_high
    rel = halfwidth / abs(ate) if ate != 0 else float("inf")

    # No informativo si: el CI es enorme relativo al efecto Y ademas cruza cero
    if crosses_zero and rel > MAX_REL_CI_HALFWIDTH:
        return GateResult(
            gate="precision",
            passed=False,
            reason=(f"Estimacion no informativa: ATE={ate:.4g} pero el intervalo "
                    f"[{ci_low:.4g}, {ci_high:.4g}] cruza cero y su semi-ancho es "
                    f"{rel:.1f}x el efecto. El motor no puede afirmar ni el signo. "
                    f"KAIROS no reporta esto como un hallazgo."),
            detail={"ate": ate, "ci": [ci_low, ci_high], "rel_halfwidth": round(rel, 2),
                    "cruza_cero": crosses_zero},
        )

    return GateResult(
        gate="precision",
        passed=True,
        reason=f"Intervalo informativo (semi-ancho {rel:.2f}x el efecto).",
        detail={"ate": ate, "ci": [ci_low, ci_high], "rel_halfwidth": round(rel, 2),
                "cruza_cero": crosses_zero},
    )


# ---------------------------------------------------------------------------
# Gatillo B - Robustez (refutation tests)
# ---------------------------------------------------------------------------
def _rel_change(new: float, base: float) -> float:
    if base == 0:
        return float("inf") if new != 0 else 0.0
    return abs(new - base) / abs(base)


def gate_robustness(original_effect: float, refutations: dict) -> GateResult:
    """
    refutations: dict {nombre_test: new_effect (float) o None si fallo el test}
    Criterios transparentes:
      placebo            -> el efecto debe colapsar hacia cero
      random_common_cause-> el efecto no debe moverse mucho
      data_subset        -> el efecto debe ser estable
    NIEGA si cualquiera falla (o si el test no se pudo correr).
    """
    fallas = []
    detalle = {}

    # placebo: |new| debe ser chico relativo al original
    placebo = refutations.get("placebo_treatment")
    if placebo is None:
        fallas.append("placebo (no se pudo correr)")
    else:
        rel = abs(placebo) / abs(original_effect) if original_effect else float("inf")
        detalle["placebo"] = {"new_effect": placebo, "rel_vs_original": round(rel, 3)}
        if rel > PLACEBO_MAX_REL:
            fallas.append(f"placebo (efecto placebo {rel:.2f}x el real, deberia ser ~0)")

    # random common cause: el ATE no debe moverse
    rcc = refutations.get("random_common_cause")
    if rcc is None:
        fallas.append("random_common_cause (no se pudo correr)")
    else:
        rel = _rel_change(rcc, original_effect)
        detalle["random_common_cause"] = {"new_effect": rcc, "cambio_rel": round(rel, 3)}
        if rel > RANDOM_CAUSE_MAX_REL:
            fallas.append(f"random_common_cause (ATE se movio {rel:.0%})")

    # data subset: estabilidad
    subset = refutations.get("data_subset")
    if subset is None:
        fallas.append("data_subset (no se pudo correr)")
    else:
        rel = _rel_change(subset, original_effect)
        detalle["data_subset"] = {"new_effect": subset, "cambio_rel": round(rel, 3)}
        if rel > DATA_SUBSET_MAX_REL:
            fallas.append(f"data_subset (ATE se movio {rel:.0%} en submuestra)")

    if not fallas:
        return GateResult(
            gate="robustez",
            passed=True,
            reason="Los tres refutation tests pasaron.",
            detail=detalle,
        )

    return GateResult(
        gate="robustez",
        passed=False,
        reason=("Estimacion NO robusta. Fallaron: " + "; ".join(fallas) + ". "
                "El numero existe pero no resiste los chequeos de falsacion, asi que "
                "KAIROS no lo reporta como hallazgo confiable."),
        detail=detalle,
    )
