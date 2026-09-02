"""R-BOT-DEFINITIVE Fase 2 — tests del autodiagnostico.

Lo que estos tests protegen no es el formato de un mensaje: es la regla que
hace que la fase sirva para algo.

    · un bloque que falla tiene que DECIR que fallo, nunca desaparecer
      (un campo ausente se lee como "no aplica", que es como un subsistema
      roto se vuelve invisible)
    · `None` no puede renderizarse igual que `True`
    · el self-test tiene que ser SILENCIOSO cuando esta sano — si manda un
      "todo ok" periodico, entrena a ignorar el dia que mande "algo falla"
    · y tiene que avisar UNA vez por problema, no una por corrida

El ultimo test es el mas importante de todos y nacio de un bug real que se
encontro escribiendolos: `run_selftest` llamaba a `should_emit` con una firma
que no existe. La excepcion caia en un `except` que loguea y emite igual, asi
que el resultado no habria sido "no avisa" — habria sido avisar CADA corrida,
para siempre. Un dedup roto que falla hacia el lado ruidoso es tan silencioso
como cualquier otra degradacion: nadie lo reporta, la gente solo deja de leer
las alertas.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def diag(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # alert_dedup fija su DB_PATH al importarse, asi que sin esto los tests
    # comparten el mismo almacen de dedup y se pisan entre ellos: el segundo
    # que use la misma firma no emite y el test falla por contaminacion, no
    # por el codigo. Cada test tiene su propio almacen.
    import modules.alert_dedup as ad
    monkeypatch.setattr(ad, "DB_PATH", str(tmp_path / "alert_dedup.db"))
    import modules.diagnostics as dg
    importlib.reload(dg)
    return dg


# ── robustez: el diagnostico no puede tumbar al proceso que diagnostica ────
def test_un_bloque_que_explota_no_tumba_el_diagnostico(diag, monkeypatch):
    def _bomba():
        raise RuntimeError("kaboom")

    monkeypatch.setitem(diag.BLOQUES, "volumen", _bomba)
    d = diag.full_diagnosis()
    assert "volumen" in d
    assert "kaboom" in d["volumen"]["_error"]
    assert "RuntimeError" in d["volumen"]["_error"]


def test_un_bloque_que_falla_no_desaparece_del_render(diag, monkeypatch):
    """El modo de fallo peligroso no es la excepcion: es la omision. Un bloque
    ausente se lee como 'no aplica' y el problema se vuelve invisible."""
    monkeypatch.setitem(diag.BLOQUES, "feeds",
                        lambda: (_ for _ in ()).throw(OSError("db ilegible")))
    d = diag.full_diagnosis()
    assert d["feeds"]["_error"]
    out = diag.format_diagnosis(d)
    assert "Feeds" in out and "db ilegible" in out


def test_sin_datos_no_se_pinta_como_sano(diag):
    assert diag._tick(True) == "\u2705"
    assert diag._tick(False) == "\u274c"
    assert diag._tick(None) == "\u2754", (
        "None es 'no se sabe'; si se pinta con tilde verde volvemos a que la "
        "ausencia de datos parezca ausencia de problemas")


def test_full_diagnosis_trae_identidad_del_deploy(diag):
    d = diag.full_diagnosis(incluir=["volumen"])
    for k in ("commit", "deploy_id", "servicio", "uptime_segundos", "generado_utc"):
        assert k in d, f"falta {k}: /health tiene que poder decir QUE version corre"
    assert isinstance(d["ok"], bool)


# ── que cuenta como problema ───────────────────────────────────────────────
def test_diagnostico_limpio_no_reporta_nada(diag):
    assert diag.detectar_problemas({}) == []


def test_subsistema_degradado_en_money_path_se_marca_como_money(diag):
    p = diag.detectar_problemas({"subsistemas": {
        "degraded": ["ledger"],
        "subsystems": {"ledger": {"label": "Ledger de cierres", "money": True,
                                  "detail": "sync incompleto"}},
    }})
    assert len(p) == 1
    assert p[0].startswith("MONEY ")
    assert "sync incompleto" in p[0]


def test_registro_de_salud_roto_es_un_problema_en_si_mismo(diag):
    p = diag.detectar_problemas(
        {"subsistemas": {"degraded": [], "registry_self_errors": 2}})
    assert any("no puede garantizar lo que reporta" in x for x in p)


def test_invariante_roto_y_recomputo_discrepante_se_reportan(diag):
    p = diag.detectar_problemas({"invariantes": {
        "invariantes": ["NET != gross - fees + funding en fill 44"],
        "recomputo": ["wallet 0xab: 120.50 vs 118.00"],
    }})
    assert any("invariante roto" in x for x in p)
    assert any("recomputo no coincide" in x for x in p)


def test_volumen_sobre_umbral_avisa(diag):
    p = diag.detectar_problemas(
        {"volumen": {"sobre_umbral": True, "usado_pct": 91.2,
                     "umbral_alerta_pct": 85}})
    assert any("91.2%" in x for x in p)


def test_dependencia_con_fallback_silencioso_ausente_avisa(diag):
    """Es el bug de rapidfuzz: faltaba en requirements, el dedup caia a un
    matcher peor, y nadie se entero hasta que alguien fue a mirar."""
    p = diag.detectar_problemas({"dependencias": {"faltan": ["rapidfuzz"]}})
    assert any("rapidfuzz" in x for x in p)


# ── backup: los cuatro estados posibles, y ninguno es ambiguo ──────────────
def test_backup_no_restaurable_avisa_con_el_motivo(diag):
    p = diag.detectar_problemas({"backup": {
        "ok": True, "horas": 3, "verificado_alguna_vez": True,
        "verificacion": {"ok": False,
                         "problemas": ["intel_memory.db: integrity_check dice 'corrupto'"]},
    }})
    assert any("NO es restaurable" in x and "corrupto" in x for x in p)


def test_backup_que_existe_pero_nunca_se_verifico_avisa(diag):
    """Es el estado en el que estuvo el fondo hasta esta ronda: habia
    tarballs todos los dias y nadie habia probado jamas restaurar uno."""
    p = diag.detectar_problemas({"backup": {
        "ok": True, "horas": 5, "verificado_alguna_vez": False,
        "verificacion": None}})
    assert any("nunca se verifico" in x for x in p)


def test_backup_verificado_y_ok_no_avisa_nada(diag):
    p = diag.detectar_problemas({"backup": {
        "ok": True, "horas": 5, "verificado_alguna_vez": True,
        "verificacion_horas": 5,
        "verificacion": {"ok": True, "problemas": []},
        "cobertura": {"sin_clasificar": []}}})
    assert p == []


def test_nunca_haber_corrido_un_backup_no_puede_pasar_desapercibido(diag):
    """`horas is None` significa que el job NUNCA corrio. Si solo se alertara
    por 'backup viejo', ese caso no dispararia jamas — el agujero mas facil
    de dejar abierto."""
    p = diag.detectar_problemas({"backup": {"ok": False, "horas": None},
                                 "uptime_segundos": 5 * 86400})
    assert any("NUNCA corrio un backup" in x for x in p)
    # y al reves: recien arrancado, no molesta
    assert diag.detectar_problemas(
        {"backup": {"ok": False, "horas": None}, "uptime_segundos": 600}) == []


def test_db_sin_clasificar_avisa(diag):
    """Fase 5.2: una DB nueva que nadie declaro critica ni descartable es una
    decision pendiente, no un detalle."""
    p = diag.detectar_problemas({"backup": {
        "ok": True, "horas": 1, "verificado_alguna_vez": True,
        "verificacion": {"ok": True},
        "cobertura": {"sin_clasificar": ["algo_nuevo.db"]}}})
    assert any("sin clasificar" in x and "algo_nuevo.db" in x for x in p)


# ── feeds ──────────────────────────────────────────────────────────────────
def test_solo_avisan_los_feeds_que_antes_funcionaban(diag):
    """Una fuente que nunca anduvo generaria una alerta eterna, y una alerta
    eterna es como se entrena a la gente a ignorar las alertas."""
    p = diag.detectar_problemas({"feeds": {
        "muertos": ["nunca_anduvo"], "muertos_accionables": []}})
    assert p == []
    p2 = diag.detectar_problemas({"feeds": {
        "muertos": ["asxn"], "muertos_accionables": ["ASXN: 14 runs caida"]}})
    assert any("ASXN" in x for x in p2)


def test_si_el_registro_de_feeds_no_responde_se_cae_a_la_lista_cruda(diag):
    """Quedarse sin ninguna lista seria el silencio que la ronda elimina."""
    p = diag.detectar_problemas({"feeds": {"muertos": ["asxn"]}})
    assert any("asxn" in x for x in p)


# ── el self-test: silencioso si esta sano, una vez por problema si no ──────
class _Espia:
    def __init__(self):
        self.mensajes: list[str] = []

    def __call__(self, texto: str) -> None:
        self.mensajes.append(texto)


def test_sano_no_manda_ni_un_mensaje(diag, monkeypatch):
    monkeypatch.setattr(diag, "full_diagnosis",
                        lambda **kw: {"ok": True, "problemas": []})
    espia = _Espia()
    res = diag.run_selftest(notificar=espia)
    assert res["ok"] is True and res["alerto"] is False
    assert espia.mensajes == [], (
        "un chequeo que saluda cuando todo esta bien entrena a ignorarlo")


def test_problema_avisa_una_vez_y_se_calla_en_la_corrida_siguiente(diag, monkeypatch):
    monkeypatch.setattr(diag, "full_diagnosis", lambda **kw: {
        "ok": False, "problemas": ["ledger incompleto en 0xab"]})
    espia = _Espia()
    r1 = diag.run_selftest(notificar=espia)
    r2 = diag.run_selftest(notificar=espia)
    assert r1["alerto"] is True
    assert r2["alerto"] is False, (
        "el mismo problema aviso dos veces; asi un problema de una semana "
        "manda siete mensajes y se vuelve ruido")
    assert len(espia.mensajes) == 1
    assert "ledger incompleto" in espia.mensajes[0]


def test_un_problema_nuevo_si_vuelve_a_avisar(diag, monkeypatch):
    problemas = ["ledger incompleto en 0xab"]
    monkeypatch.setattr(diag, "full_diagnosis",
                        lambda **kw: {"ok": False, "problemas": list(problemas)})
    espia = _Espia()
    diag.run_selftest(notificar=espia)
    problemas.append("volumen al 92%")
    r = diag.run_selftest(notificar=espia)
    assert r["alerto"] is True, (
        "aparecio un problema NUEVO mientras el viejo seguia y no aviso")
    assert len(espia.mensajes) == 2


def test_resolverse_limpia_el_dedup_para_que_la_proxima_vez_avise(diag, monkeypatch):
    estado = {"malo": True}
    monkeypatch.setattr(diag, "full_diagnosis", lambda **kw: (
        {"ok": False, "problemas": ["volumen al 92%"]} if estado["malo"]
        else {"ok": True, "problemas": []}))
    espia = _Espia()
    diag.run_selftest(notificar=espia)          # avisa
    estado["malo"] = False
    diag.run_selftest(notificar=espia)          # se resuelve, limpia
    estado["malo"] = True
    r = diag.run_selftest(notificar=espia)      # vuelve: TIENE que avisar
    assert r["alerto"] is True, (
        "el problema volvio y quedo tapado por la entrada vieja del dedup; "
        "asi es como una alerta desaparece para siempre")
    assert len(espia.mensajes) == 2


def test_el_dedup_se_llama_con_una_firma_que_existe(diag, monkeypatch):
    """EL test de esta fase.

    `run_selftest` envuelve la llamada al dedup en un try/except que, si algo
    falla, emite igual. Eso significa que llamar mal a `should_emit` no se
    manifiesta como 'no avisa' sino como 'avisa SIEMPRE', cada corrida, para
    siempre — y nadie reporta un bug de alertas que llegan de mas: dejan de
    leerlas. Este test exige que la llamada de verdad funcione.
    """
    from modules import alert_dedup

    llamadas: list[tuple] = []
    real = alert_dedup.should_emit

    def _espia(*a, **kw):
        llamadas.append((a, kw))
        return real(*a, **kw)          # firma real, sin mocks permisivos

    monkeypatch.setattr(alert_dedup, "should_emit", _espia)
    monkeypatch.setattr(diag, "full_diagnosis",
                        lambda **kw: {"ok": False, "problemas": ["x"]})
    diag.run_selftest(notificar=_Espia())
    assert llamadas, "no se consulto el dedup"
    args, kwargs = llamadas[0]
    assert len(args) == 3, "should_emit exige (atype, entity, state) posicionales"
    assert "cooldown_hours" in kwargs


# ── integracion con /health y con el comando ───────────────────────────────
def test_health_payload_incluye_el_autodiagnostico(diag):
    from modules.version_info import health_payload
    p = health_payload(1)
    a = p.get("autodiagnostico")
    assert isinstance(a, dict)
    assert "problemas" in a
    for bloque in ("subsistemas", "feeds", "volumen", "backup",
                   "dependencias", "autoactualizacion"):
        assert bloque in a, f"/health no expone {bloque}"


def test_health_no_finge_estar_sano_si_el_diagnostico_explota(monkeypatch):
    """Si el bloque fallara y devolviera {}, /health seguiria contestando 200
    y pareceria sano. Tiene que devolver ok=None (no se sabe) y decirlo."""
    import modules.diagnostics as dg
    import modules.version_info as vi
    monkeypatch.setattr(dg, "full_diagnosis",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    a = vi._autodiagnostico_safe()
    assert a["ok"] is None
    assert "boom" in a["_error"]
    assert a["problemas"]


def test_el_comando_diagnostico_esta_registrado_y_existe():
    import bot
    from commands_registry import COMMANDS
    cmd = next((c for c in COMMANDS if c.command == "diagnostico"), None)
    assert cmd is not None, "/diagnostico no figura en el registro de comandos"
    assert hasattr(bot, cmd.handler_name), (
        f"falta el handler {cmd.handler_name} en bot.py")


def test_autoactualizacion_nunca_expone_un_valor_de_secreto(diag, monkeypatch):
    """Fase 0.3: /health dice QUE variable falta, jamas cuanto vale."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secretito_que_no_debe_salir")
    monkeypatch.setenv("GITHUB_REPO", "blackcatdefi/pear-monitor-bot")
    out = diag._b_autoactualizacion()
    plano = repr(out)
    assert "ghp_secretito_que_no_debe_salir" not in plano
    assert out["github_token_var"] == "GITHUB_TOKEN"
    assert out["puede_pushear"] is True
