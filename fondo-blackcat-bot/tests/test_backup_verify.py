"""R-BOT-DEFINITIVE Fase 5.1/5.2 — tests del verificador de backups.

Por que estos tests son el punto entero de la fase
--------------------------------------------------
Un verificador que siempre devuelve "todo bien" es peor que no tener
verificador: agrega una tilde verde a un backup roto. Asi que aca no alcanza
con probar el camino feliz; hay que probar que el verificador SE ROMPE cuando
el backup esta mal. Cada test de abajo construye a mano una de las formas en
que un backup puede ser basura y exige que `verify_latest()` la detecte.

Los cuatro modos de fallo que se prueban son los cuatro que de verdad pasaron
o pudieron pasar:

    1. el tarball no se puede ni abrir           → backup ilegible
    2. el tarball tiene una sqlite sana y VACIA  → el caso mas peligroso,
       porque integrity_check dice "ok" y el archivo pesa lo esperado
    3. falta una DB critica en el tarball        → se respalda lo que no
       importa y se deja afuera lo unico irrecuperable
    4. el backup tiene MAS filas que la DB viva  → la viva perdio datos

Y ademas el test que le pone candado a la causa raiz: `_snapshot_db`.
"""
from __future__ import annotations

import importlib
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest


# ── infraestructura ─────────────────────────────────────────────────────────
@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """DATA_DIR aislado + los dos modulos recargados apuntando ahi."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GITHUB_BACKUP_REPO", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    import modules.backup_volume as bv
    import modules.backup_verify as bk
    importlib.reload(bv)
    importlib.reload(bk)
    return tmp_path, bv, bk


def _db_con_filas(path: Path, tabla: str, n: int, *, wal: bool = True) -> None:
    con = sqlite3.connect(path)
    try:
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute(f'CREATE TABLE IF NOT EXISTS "{tabla}" '
                    f"(id INTEGER PRIMARY KEY, v REAL)")
        con.executemany(f'INSERT INTO "{tabla}" (v) VALUES (?)',
                        [(float(i),) for i in range(n)])
        con.commit()
    finally:
        con.close()


def _reempaquetar(tar_path: Path, mutar) -> None:
    """Extrae el tarball, deja que `mutar(dir)` lo modifique, y lo rearma.

    Sirve para fabricar backups rotos sin tocar el codigo de produccion.
    """
    work = Path(tempfile.mkdtemp())
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(work)
        mutar(work)
        tar_path.unlink()
        with tarfile.open(tar_path, "w:gz") as tar:
            for p in sorted(work.iterdir()):
                if p.is_file():
                    tar.add(p, arcname=p.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── camino feliz ────────────────────────────────────────────────────────────
def test_backup_recien_hecho_se_restaura_y_verifica(entorno):
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 500)
    assert bv.run_backup()["ok"] is True

    res = bk.verify_latest()
    assert res["ok"] is True, res["problemas"]
    assert res["problemas"] == []
    assert res["dbs_verificadas"] == 1
    d = res["detalle"]["intel_memory.db"]
    assert d["integridad"] == "ok"
    assert d["filas_backup"] == 500
    assert d["filas_vivas"] == 500
    assert d["critica"] is True


def test_verificar_deja_registro_leible_por_health(entorno):
    data, bv, bk = entorno
    _db_con_filas(data / "pnl.db", "pnl", 10)
    bv.run_backup()
    assert bk.last_verification() is None, (
        "antes de verificar, /health tiene que poder distinguir 'nunca se "
        "verifico' de 'se verifico y dio bien'")
    bk.verify_latest()
    last = bk.last_verification()
    assert last is not None and last["ok"] is True
    assert bk.horas_desde_verificacion() is not None
    assert "detalle" not in last, "el snapshot en disco debe ser liviano"


def test_nunca_verificado_no_se_lee_como_sano(entorno):
    _data, _bv, bk = entorno
    assert bk.last_verification() is None
    assert bk.horas_desde_verificacion() is None
    out = bk.format_for_telegram()
    assert "nunca" in out.lower()
    assert "✅" not in out, (
        "'nunca se verifico' no puede renderizarse con una tilde verde")


def test_sin_ningun_tarball_no_es_ok(entorno):
    _data, _bv, bk = entorno
    res = bk.verify_latest()
    assert res["ok"] is False
    assert "nunca se corrio un backup" in " ".join(res["problemas"])


# ── modo de fallo 1: tarball ilegible ───────────────────────────────────────
def test_tarball_corrupto_se_detecta(entorno):
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 5)
    bv.run_backup()
    tar = sorted((data / "backup").glob("*.tar.gz"))[-1]
    tar.write_bytes(b"esto no es un gzip ni por casualidad")

    res = bk.verify_latest()
    assert res["ok"] is False
    assert "ilegible" in " ".join(res["problemas"])


# ── modo de fallo 2: sqlite sana pero vacia (el mas peligroso) ─────────────
def test_sqlite_valida_pero_vacia_no_pasa_como_backup_bueno(entorno):
    """integrity_check devuelve 'ok' sobre una base vacia. Si el verificador
    se quedara en ese pragma, un backup de cero filas se reportaria perfecto.
    """
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 400)
    bv.run_backup()
    tar = sorted((data / "backup").glob("*.tar.gz"))[-1]

    def _vaciar(work: Path) -> None:
        p = work / "intel_memory.db"
        p.unlink()
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE ledger_fills (id INTEGER PRIMARY KEY, v REAL)")
        con.commit()
        con.close()

    _reempaquetar(tar, _vaciar)

    res = bk.verify_latest()
    assert res["detalle"]["intel_memory.db"]["integridad"] == "ok", (
        "premisa del test: la base vacia SI pasa integrity_check")
    assert res["ok"] is False, "un backup vacio se reporto como bueno"
    assert "muy por debajo" in " ".join(res["problemas"])


def test_tarball_sin_ninguna_sqlite_se_detecta(entorno):
    data, bv, bk = entorno
    (data / "intel.log").write_text("una linea\n", encoding="utf-8")
    bv.run_backup()
    res = bk.verify_latest()
    assert res["ok"] is False
    assert "no contiene ni una sqlite" in " ".join(res["problemas"])


# ── modo de fallo 3: falta una DB critica ───────────────────────────────────
def test_db_critica_ausente_del_tarball_se_denuncia(entorno):
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 50)
    _db_con_filas(data / "pnl.db", "pnl", 50)
    bv.run_backup()
    tar = sorted((data / "backup").glob("*.tar.gz"))[-1]
    _reempaquetar(tar, lambda work: (work / "pnl.db").unlink())

    res = bk.verify_latest()
    assert res["ok"] is False
    msg = " ".join(res["problemas"])
    assert "criticas fuera del backup" in msg and "pnl.db" in msg


def test_tabla_presente_en_la_viva_y_ausente_en_el_backup(entorno):
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 30)
    bv.run_backup()
    # la viva gana una tabla nueva DESPUES del backup: eso no es un problema
    # del backup, pero el verificador tiene que decirlo igual para que nadie
    # crea que esa tabla esta respaldada.
    _db_con_filas(data / "intel_memory.db", "ledger_funding", 30)

    res = bk.verify_latest()
    assert "le faltan tablas" in " ".join(res["problemas"])
    assert "ledger_funding" in " ".join(res["problemas"])


# ── modo de fallo 4: el backup tiene mas que la viva ────────────────────────
def test_backup_con_mas_filas_que_la_viva_denuncia_perdida_de_datos(entorno):
    """El backup es mas viejo, asi que tener MENOS es normal. Tener MAS
    significa que la DB viva perdio filas — y eso hay que gritarlo."""
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 400)
    bv.run_backup()
    con = sqlite3.connect(data / "intel_memory.db")
    con.execute("DELETE FROM ledger_fills WHERE id > 100")
    con.commit()
    con.close()

    res = bk.verify_latest()
    assert res["ok"] is False
    assert "la viva perdio datos" in " ".join(res["problemas"])


def test_tablas_volatiles_no_disparan_falsas_alarmas(entorno):
    """alert_dedup y compania rotan solas: que el backup tenga menos no es
    una falla. Si contaran, el verificador daria rojo todos los dias y en dos
    semanas nadie lo miraria mas."""
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 100)
    _db_con_filas(data / "alert_dedup.db", "alert_dedup", 5)
    bv.run_backup()
    _db_con_filas(data / "alert_dedup.db", "alert_dedup", 500)

    res = bk.verify_latest()
    assert res["ok"] is True, res["problemas"]


# ── Fase 5.2: cobertura declarada ───────────────────────────────────────────
def test_toda_db_en_disco_esta_clasificada(entorno):
    data, _bv, bk = entorno
    for nombre in list(bk.DBS_CRITICAS)[:3] + list(bk.DBS_DESCARTABLES)[:3]:
        _db_con_filas(data / nombre, "t", 1)
    cob = bk.cobertura()
    assert cob["sin_clasificar"] == []
    assert len(cob["criticas"]) == 3
    assert len(cob["descartables"]) == 3


def test_una_db_desconocida_aparece_como_sin_clasificar(entorno):
    """Si mañana alguien agrega una DB nueva y no dice si es critica o
    descartable, tiene que aparecer en `sin_clasificar` — no desaparecer."""
    data, _bv, bk = entorno
    _db_con_filas(data / "una_db_nueva_de_alguien.db", "t", 1)
    assert "una_db_nueva_de_alguien.db" in bk.cobertura()["sin_clasificar"]


def test_criticas_y_descartables_no_se_solapan_y_todas_se_justifican():
    from modules import backup_verify as bk
    solapadas = set(bk.DBS_CRITICAS) & set(bk.DBS_DESCARTABLES)
    assert not solapadas, f"{solapadas} figuran como criticas Y descartables"
    for nombre, motivo in bk.DBS_DESCARTABLES.items():
        assert len(motivo) > 30, (
            f"{nombre} se declara descartable sin explicar de donde se "
            f"recupera; 'es un cache' sin decir cache de que no alcanza")


# ── candado sobre la causa raiz ─────────────────────────────────────────────
def test_el_backup_toma_snapshot_consistente_y_no_copia_el_db_crudo(entorno):
    """R-BOT-DEFINITIVE Fase 5.1 — el test que impide volver al bug original.

    Con WAL y una conexion VIVA abierta (que es exactamente el estado del bot
    en produccion), copiar el .db crudo — lo que hacia `tar.add()` — produce
    un archivo en el que la tabla ni siquiera existe: las paginas viven en el
    -wal, y el glob '*.db' nunca lo incluia. El backup se creaba sin errores,
    pesaba lo esperado, y era inservible.

    Este test verifica las dos mitades: que la copia cruda efectivamente
    pierde los datos, y que el tarball que produce `run_backup()` NO los
    pierde. Si alguien vuelve a sacar `_snapshot_db`, la segunda mitad falla.
    """
    data, bv, bk = entorno
    p = data / "intel_memory.db"
    con = sqlite3.connect(p)          # se deja ABIERTA a proposito
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE ledger_fills (id INTEGER PRIMARY KEY, v REAL)")
        con.executemany("INSERT INTO ledger_fills (v) VALUES (?)",
                        [(float(i),) for i in range(1000)])
        con.commit()

        # mitad 1: la copia cruda pierde los datos
        crudo = data / "copia_cruda.tmp"
        shutil.copy2(p, crudo)
        con_crudo = sqlite3.connect(crudo)
        with pytest.raises(sqlite3.Error):
            con_crudo.execute("SELECT COUNT(*) FROM ledger_fills").fetchone()
        con_crudo.close()
        crudo.unlink()

        # mitad 2: el backup real NO los pierde
        assert bv.run_backup()["ok"] is True
        res = bk.verify_latest()
        assert res["ok"] is True, res["problemas"]
        assert res["detalle"]["intel_memory.db"]["filas_backup"] == 1000, (
            "el tarball perdio filas que estaban en el WAL: volvio el bug")
    finally:
        con.close()


def test_snapshot_fallido_queda_registrado_y_no_se_reporta_como_perfecto(entorno):
    """Si el snapshot no se puede tomar, el backup sigue (algo es mejor que
    nada) pero el fallo tiene que viajar en el resultado. Un backup degradado
    que se reporta como perfecto es la misma mentira que el bug original."""
    data, bv, _bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 10)
    monkey = getattr(bv, "_snapshot_db")
    try:
        bv._snapshot_db = lambda src, dst: "fallo simulado"
        res = bv.run_backup()
        assert res["ok"] is True
        assert res["db_snapshot_fallos"], (
            "el snapshot fallo y el resultado no lo dice")
        assert "intel_memory.db" in res["db_snapshot_fallos"][0]
    finally:
        bv._snapshot_db = monkey


# ── R-BOT-FINAL (2026-09-02): el conteo tiene que SALIR, no solo calcularse ──
#
# En produccion /diagnostico decia, literal: "15 DBs restauradas". Eso es lo
# mismo que diria una restauracion de 15 sqlites vacios — que es exactamente el
# modo de falla que este modulo existe para detectar y el que motivo la regla
# "un backup que no se restauro no es un backup". El numero que prueba algo es
# cuantas FILAS volvieron, y estaba calculado, guardado en `resumen`... y no
# publicado en ningun lado. Un dato que nadie ve no verifica nada.

def test_la_linea_de_telegram_dice_cuantas_filas_volvieron(entorno):
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 500)
    _db_con_filas(data / "pnl.db", "pnl", 120)
    bv.run_backup()
    res = bk.verify_latest()
    assert res["ok"] is True, res["problemas"]
    assert res["filas_restauradas_total"] == 620
    assert res["filas_vivas_total"] == 620

    linea = bk.format_for_telegram()
    assert "620" in linea, (
        "la verificacion conto 620 filas restauradas y la linea que lee BCD no "
        "las muestra: '15 DBs restauradas' es indistinguible de 15 sqlites "
        "vacios")
    assert "intel_memory" in linea, "no se ve la DB mas grande ni su conteo"


def test_el_conteo_tambien_se_ve_cuando_la_verificacion_falla(entorno):
    """Si algo salio mal, saber cuantas filas volvieron es lo que dice si el
    problema es cosmetico o si el tarball esta vacio."""
    data, bv, bk = entorno
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 400)
    bv.run_backup()
    # La DB viva crece despues del backup: el backup queda por debajo del
    # minimo de cobertura y la verificacion tiene que protestar.
    _db_con_filas(data / "intel_memory.db", "ledger_fills", 4000)
    res = bk.verify_latest()
    assert res["ok"] is False and res["problemas"]
    linea = bk.format_for_telegram()
    assert "400" in linea and "4,400" in linea, linea


def test_una_verificacion_vieja_sin_conteos_no_rompe_la_linea(entorno):
    """Compatibilidad: el JSON escrito por la version anterior no tiene los
    campos nuevos. Tiene que renderizar igual, sin inventar un cero."""
    import json
    data, bv, bk = entorno
    _db_con_filas(data / "pnl.db", "pnl", 10)
    bv.run_backup()
    bk.verify_latest()
    viejo = bk.last_verification()
    viejo.pop("filas_restauradas_total", None)
    viejo.pop("filas_vivas_total", None)
    bk.VERIFY_LAST_PATH.write_text(json.dumps(viejo), encoding="utf-8")
    linea = bk.format_for_telegram()
    assert "Backup verificado" in linea
    assert "0 filas" not in linea, (
        "sin dato hay que callarse, no imprimir un cero que parece un hallazgo")
