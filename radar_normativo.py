#!/usr/bin/env python3
"""
Radar normativo · Transporte y logística
=========================================

Recopila novedades normativas y de cumplimiento para empresas de transporte
y logística en España, las puntúa por relevancia y genera un HTML autónomo
que se actualiza en cada ejecución.

Fuentes (configurables en fuentes.json):
  * BOE         -> API oficial de datos abiertos (sumario diario en JSON)
  * RSS         -> asociaciones (FENADISMER, CETM, ASTIC, UNO), AEAT, DGT, prensa sectorial
  * HTML        -> sala de prensa del Ministerio de Transportes (best effort)

Persistencia: SQLite (histórico, deduplicación y detección de "nuevos").

Uso:
    python radar_normativo.py                 # recoge + genera HTML
    python radar_normativo.py --solo-html     # regenera HTML desde la BD, sin red
    python radar_normativo.py --dias-boe 30   # fuerza ventana BOE
    python radar_normativo.py -v              # log detallado

Variables de entorno opcionales:
    RADAR_WEBHOOK_URL   Webhook (Teams/Slack/Google Chat) para avisar de novedades de prioridad alta.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("radar")

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
RE_FECHA_LARGA = re.compile(
    r"\b(\d{1,2})\s+de\s+(" + "|".join(MESES) + r")(?:\s+de|,)?\s+(?:del?\s+)?(\d{4})\b", re.IGNORECASE
)
RE_FECHA_CORTA = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b")


# --------------------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------------------
@dataclass
class Noticia:
    fuente_id: str
    fuente: str
    tipo_fuente: str
    titulo: str
    url: str
    resumen: str = ""
    fecha_pub: str | None = None  # ISO yyyy-mm-dd
    extra: str = ""  # contexto adicional (p.ej. departamento BOE)
    bonus: float = 0.0  # bonus de relevancia propio de la fuente
    peso: float = 1.0
    filtrar: bool = True
    generalista: bool = False  # fuente no sectorial: algunos temas exigen contexto de transporte
    puntua_resumen: bool = True

    # calculados
    puntuacion: float = 0.0
    categoria: str = ""
    categorias: list[str] = field(default_factory=list)
    senales: list[str] = field(default_factory=list)
    fechas_clave: list[dict] = field(default_factory=list)
    prioridad: str = "baja"

    @property
    def uid(self) -> str:
        clave = self.url.split("?qfr=")[0].strip().lower() or (self.fuente_id + self.titulo)
        return hashlib.sha1(clave.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------
def normaliza(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texto.lower()).strip()


def limpia_html(texto: str, max_len: int = 600) -> str:
    if not texto:
        return ""
    txt = BeautifulSoup(texto, "html.parser").get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt)
    # quita colas tipo "La entrada X apareció primero en Y" / "The post X appeared first on Y"
    txt = re.split(r"\s(?:La entrada|The post)\s.+?(?:apareci[oó] primero|appeared first)", txt)[0]
    txt = re.sub(r"\s*(Ampliar noticia|Leer más)\s*…?\s*$", "", txt).strip()
    return (txt[: max_len - 1] + "…") if len(txt) > max_len else txt


def fecha_iso(struct) -> str | None:
    if not struct:
        return None
    try:
        return datetime(*struct[:6], tzinfo=timezone.utc).date().isoformat()
    except Exception:
        return None


def hoy() -> date:
    return datetime.now().date()


class Http:
    def __init__(self, user_agent: str, timeout: int = 25, reintentos: int = 2):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "es-ES,es;q=0.9",
        })
        self.timeout = timeout
        self.reintentos = reintentos

    def get(self, url: str, **kw) -> requests.Response:
        ultimo_error: Exception | None = None
        for intento in range(self.reintentos + 1):
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                if r.status_code in (429, 500, 502, 503, 504) and intento < self.reintentos:
                    time.sleep(2 * (intento + 1))
                    continue
                return r
            except requests.RequestException as e:  # red caída, timeout...
                ultimo_error = e
                time.sleep(2 * (intento + 1))
        raise ultimo_error or RuntimeError(f"Sin respuesta de {url}")


# --------------------------------------------------------------------------------------
# Recolectores
# --------------------------------------------------------------------------------------
def _como_lista(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def recoge_boe(http: Http, cfg: dict, desde: date, hasta: date) -> list[Noticia]:
    """Sumario diario del BOE vía API de datos abiertos (JSON). Días sin boletín devuelven 404."""
    secciones = set(cfg.get("secciones", ["1", "3"]))
    bonus_dep = {k.upper(): v for k, v in cfg.get("departamentos_bonus", {}).items()}
    bonus_sec = {str(k): float(v) for k, v in cfg.get("bonus_seccion", {}).items()}
    out: list[Noticia] = []
    dia = desde
    while dia <= hasta:
        url = f"https://www.boe.es/datosabiertos/api/boe/sumario/{dia:%Y%m%d}"
        r = http.get(url, headers={"Accept": "application/json"})
        if r.status_code == 404:
            log.debug("BOE %s: sin publicación", dia)
        elif r.ok:
            try:
                diarios = _como_lista(r.json()["data"]["sumario"]["diario"])
            except (ValueError, KeyError) as e:
                log.warning("BOE %s: respuesta inesperada (%s)", dia, e)
                diarios = []
            for diario in diarios:
                for sec in _como_lista(diario.get("seccion")):
                    if str(sec.get("codigo")) not in secciones:
                        continue
                    for dep in _como_lista(sec.get("departamento")):
                        nombre_dep = (dep.get("nombre") or "").strip()
                        items = list(_como_lista(dep.get("item")))
                        for epi in _como_lista(dep.get("epigrafe")):
                            items.extend(_como_lista(epi.get("item")))
                        for it in items:
                            titulo = (it.get("titulo") or "").strip()
                            if not titulo:
                                continue
                            out.append(Noticia(
                                fuente_id="boe",
                                fuente="BOE",
                                tipo_fuente="Oficial",
                                titulo=titulo,
                                url=it.get("url_html") or "",
                                resumen=f"{sec.get('nombre', '')} · {nombre_dep}",
                                fecha_pub=dia.isoformat(),
                                extra=nombre_dep,
                                bonus=bonus_dep.get(nombre_dep.upper(), 0.0)
                                + bonus_sec.get(str(sec.get("codigo")), 0.0),
                                peso=1.4,
                                generalista=True,
                                puntua_resumen=False,  # el resumen es sección + departamento
                                # Disposiciones generales de Transportes: siempre se muestran
                                filtrar=not (str(sec.get("codigo")) == "1"
                                             and "TRANSPORTES" in nombre_dep.upper()),
                            ))
        else:
            log.warning("BOE %s: HTTP %s", dia, r.status_code)
        dia += timedelta(days=1)
        time.sleep(0.3)  # cortesía con el servidor
    return out


def recoge_rss(http: Http, fuente: dict) -> list[Noticia]:
    r = http.get(fuente["url"])
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Feed no válido: {feed.bozo_exception}")
    out = []
    for e in feed.entries:
        resumen = e.get("summary") or ""
        if not resumen and e.get("content"):
            resumen = e.content[0].get("value", "")
        out.append(Noticia(
            fuente_id=fuente["id"],
            fuente=fuente["nombre"],
            tipo_fuente=fuente.get("tipo", ""),
            titulo=limpia_html(e.get("title", ""), 300),
            url=e.get("link", ""),
            resumen=limpia_html(resumen),
            fecha_pub=fecha_iso(e.get("published_parsed") or e.get("updated_parsed")),
            extra=" ".join(t.get("term", "") for t in e.get("tags", [])),
            peso=float(fuente.get("peso", 1.0)),
            filtrar=fuente.get("filtrar", True),
            generalista=fuente.get("generalista", False),
        ))
    return out


def recoge_html_listado(http: Http, fuente: dict) -> list[Noticia]:
    """Scraper genérico de listados de noticias: busca enlaces que contienen `patron_enlace`
    dentro de un encabezado y la fecha dd-mm-aaaa más cercana en su bloque."""
    r = http.get(fuente["url"], headers={"Accept": "text/html"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    patron = fuente.get("patron_enlace", "")
    vistos, out = set(), []
    for a in soup.select("h2 a[href], h3 a[href], h4 a[href]"):
        href = a.get("href", "")
        if patron and patron not in href:
            continue
        url = urljoin(fuente["url"], href)
        if url in vistos:
            continue
        vistos.add(url)
        # sube por el DOM hasta encontrar un bloque con fecha y texto
        bloque, fecha, resumen = a, None, ""
        for _ in range(5):
            bloque = bloque.parent
            if bloque is None:
                break
            texto = bloque.get_text(" ", strip=True)
            m = RE_FECHA_CORTA.search(texto)
            if m:
                d, mth, y = map(int, m.groups())
                try:
                    fecha = date(y, mth, d).isoformat()
                except ValueError:
                    fecha = None
                parrafos = [p.get_text(" ", strip=True) for p in bloque.find_all("p")]
                resumen = max(parrafos, key=len) if parrafos else ""
                break
        out.append(Noticia(
            fuente_id=fuente["id"], fuente=fuente["nombre"], tipo_fuente=fuente.get("tipo", ""),
            titulo=a.get_text(" ", strip=True), url=url, resumen=limpia_html(resumen),
            fecha_pub=fecha, peso=float(fuente.get("peso", 1.0)), filtrar=fuente.get("filtrar", True),
        ))
    if not out:
        raise ValueError("No se encontraron noticias: ¿ha cambiado la estructura de la página?")
    return out


# --------------------------------------------------------------------------------------
# Puntuación y extracción de plazos
# --------------------------------------------------------------------------------------
class Clasificador:
    def __init__(self, cfg: dict):
        self.umbral = float(cfg.get("umbral_relevancia", 3))
        self.umbral_alta = float(cfg.get("umbral_prioridad_alta", 8))
        self.categorias = {
            nombre: [(self._re(t), float(p)) for t, p in c["terminos"].items()]
            for nombre, c in cfg["categorias"].items()
        }
        self.requiere_contexto = {n for n, c in cfg["categorias"].items() if c.get("requiere_contexto")}
        self.contexto = [self._re(t) for t in cfg.get("contexto_sector", [])]
        self.senales = [(t, self._re(t), float(p)) for t, p in cfg.get("senales_obligacion", {}).items()]
        self.excluir = [self._re(t) for t in cfg.get("terminos_excluir", [])]

    @staticmethod
    def _re(termino: str) -> re.Pattern:
        t = re.escape(normaliza(termino))
        return re.compile(rf"(?<![a-z0-9]){t}(?![a-z0-9])")

    def evalua(self, n: Noticia) -> Noticia:
        titulo = normaliza(n.titulo)
        cuerpo = normaliza(f"{n.resumen} {n.extra}") if n.puntua_resumen else ""
        con_contexto = (not n.generalista) or any(rx.search(titulo) or rx.search(cuerpo) for rx in self.contexto)
        puntos_cat: dict[str, float] = {}
        for cat, terminos in self.categorias.items():
            if cat in self.requiere_contexto and not con_contexto:
                continue
            s = 0.0
            for rx, peso in terminos:
                if rx.search(titulo):
                    s += peso * 1.5  # en el título pesa más
                elif rx.search(cuerpo):
                    s += peso
            if s:
                puntos_cat[cat] = s
        senales = [t for t, rx, _ in self.senales if rx.search(titulo) or rx.search(cuerpo)]
        p_senales = min(sum(p for t, _, p in self.senales if t in senales), 6.0)

        base = sum(puntos_cat.values())
        # señales y bonus de departamento solo suman si hay materia relevante
        score = (base + p_senales + n.bonus) * n.peso if base else 0.0
        if any(rx.search(titulo) for rx in self.excluir):
            score *= 0.2

        n.puntuacion = round(score, 1)
        n.categorias = sorted(puntos_cat, key=puntos_cat.get, reverse=True)
        n.categoria = n.categorias[0] if n.categorias else "Otros"
        n.senales = senales[:6]
        n.fechas_clave = extrae_fechas(f"{n.titulo}. {n.resumen}", n.fecha_pub)
        if score >= self.umbral_alta or (score >= self.umbral and n.fechas_clave and senales):
            n.prioridad = "alta"
        elif score >= self.umbral:
            n.prioridad = "media"
        else:
            n.prioridad = "media" if not n.filtrar else "baja"
        return n

    def es_relevante(self, n: Noticia) -> bool:
        return (not n.filtrar) or n.puntuacion >= self.umbral


def extrae_fechas(texto: str, fecha_pub: str | None) -> list[dict]:
    """Detecta fechas futuras citadas en el texto (p.ej. 'a partir del 1 de enero de 2027')."""
    ref = date.fromisoformat(fecha_pub) if fecha_pub else hoy()
    encontradas: dict[str, dict] = {}

    def ventana(m: re.Match) -> str:
        ini = max(0, m.start() - 90)
        frag = texto[ini: m.end() + 40]
        frag = frag.split(". ")[-1] if ". " in texto[ini:m.start()] else frag
        return re.sub(r"\s+", " ", frag).strip(" .,;")

    for m in RE_FECHA_LARGA.finditer(texto):
        d, mes, y = int(m.group(1)), MESES[m.group(2).lower()], int(m.group(3))
        try:
            f = date(y, mes, d)
        except ValueError:
            continue
        if f >= ref and f <= ref + timedelta(days=365 * 4):
            encontradas.setdefault(f.isoformat(), {"fecha": f.isoformat(), "contexto": ventana(m)})
    return sorted(encontradas.values(), key=lambda x: x["fecha"])[:4]


# --------------------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------------------
ESQUEMA = """
CREATE TABLE IF NOT EXISTS noticias (
    uid TEXT PRIMARY KEY,
    fuente_id TEXT, fuente TEXT, tipo_fuente TEXT,
    titulo TEXT, url TEXT, resumen TEXT, fecha_pub TEXT,
    categoria TEXT, categorias TEXT, senales TEXT, fechas_clave TEXT,
    puntuacion REAL, prioridad TEXT,
    primera_vez TEXT, ultima_vez TEXT, ejecucion_alta INTEGER
);
CREATE TABLE IF NOT EXISTS ejecuciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT, inicio TEXT, fin TEXT, nuevas INTEGER
);
CREATE TABLE IF NOT EXISTS estado_fuentes (
    ejecucion INTEGER, fuente_id TEXT, fuente TEXT, ok INTEGER, recibidas INTEGER,
    relevantes INTEGER, error TEXT, segundos REAL
);
CREATE INDEX IF NOT EXISTS ix_fecha ON noticias(fecha_pub);
"""


class Almacen:
    def __init__(self, ruta: Path):
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(ruta)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(ESQUEMA)

    def nueva_ejecucion(self) -> int:
        cur = self.db.execute("INSERT INTO ejecuciones(inicio) VALUES (?)", (datetime.now().isoformat(timespec="seconds"),))
        self.db.commit()
        return cur.lastrowid

    def cierra_ejecucion(self, ej: int, nuevas: int):
        self.db.execute("UPDATE ejecuciones SET fin=?, nuevas=? WHERE id=?",
                        (datetime.now().isoformat(timespec="seconds"), nuevas, ej))
        self.db.commit()

    def ultima_ejecucion_completa(self) -> datetime | None:
        row = self.db.execute("SELECT MAX(fin) AS f FROM ejecuciones WHERE fin IS NOT NULL").fetchone()
        return datetime.fromisoformat(row["f"]) if row and row["f"] else None

    def guarda(self, n: Noticia, ej: int) -> bool:
        """Inserta o actualiza. Devuelve True si es nueva."""
        ahora = datetime.now().isoformat(timespec="seconds")
        existe = self.db.execute("SELECT uid FROM noticias WHERE uid=?", (n.uid,)).fetchone()
        if not existe and self.db.execute(  # misma noticia republicada con otra URL
                "SELECT 1 FROM noticias WHERE fuente_id=? AND titulo=?", (n.fuente_id, n.titulo)).fetchone():
            return False
        valores = (n.fuente_id, n.fuente, n.tipo_fuente, n.titulo, n.url, n.resumen, n.fecha_pub,
                   n.categoria, json.dumps(n.categorias, ensure_ascii=False),
                   json.dumps(n.senales, ensure_ascii=False), json.dumps(n.fechas_clave, ensure_ascii=False),
                   n.puntuacion, n.prioridad)
        if existe:
            self.db.execute("""UPDATE noticias SET fuente_id=?, fuente=?, tipo_fuente=?, titulo=?, url=?, resumen=?,
                fecha_pub=COALESCE(?, fecha_pub), categoria=?, categorias=?, senales=?, fechas_clave=?,
                puntuacion=?, prioridad=?, ultima_vez=? WHERE uid=?""", valores + (ahora, n.uid))
            return False
        self.db.execute("""INSERT INTO noticias(fuente_id, fuente, tipo_fuente, titulo, url, resumen, fecha_pub,
            categoria, categorias, senales, fechas_clave, puntuacion, prioridad, uid, primera_vez, ultima_vez, ejecucion_alta)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", valores + (n.uid, ahora, ahora, ej))
        return True

    def estado_fuente(self, ej, fuente_id, fuente, ok, recibidas, relevantes, error, seg):
        self.db.execute("INSERT INTO estado_fuentes VALUES (?,?,?,?,?,?,?,?)",
                        (ej, fuente_id, fuente, int(ok), recibidas, relevantes, error, round(seg, 1)))

    def commit(self):
        self.db.commit()

    def noticias_ventana(self, dias: int) -> list[dict]:
        limite = (hoy() - timedelta(days=dias)).isoformat()
        rows = self.db.execute("""SELECT * FROM noticias
            WHERE COALESCE(fecha_pub, substr(primera_vez,1,10)) >= ?
            ORDER BY COALESCE(fecha_pub, substr(primera_vez,1,10)) DESC, puntuacion DESC""", (limite,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("categorias", "senales", "fechas_clave"):
                d[k] = json.loads(d[k] or "[]")
            out.append(d)
        return out

    def estado_ultima(self) -> tuple[int | None, list[dict]]:
        row = self.db.execute("SELECT MAX(id) AS id FROM ejecuciones WHERE fin IS NOT NULL").fetchone()
        if not row or row["id"] is None:
            return None, []
        ej = row["id"]
        filas = [dict(r) for r in self.db.execute("SELECT * FROM estado_fuentes WHERE ejecucion=?", (ej,))]
        return ej, filas


# --------------------------------------------------------------------------------------
# Notificación opcional
# --------------------------------------------------------------------------------------
def notifica(nuevas_altas: list[Noticia]):
    url = os.environ.get("RADAR_WEBHOOK_URL")
    if not url or not nuevas_altas:
        return
    lineas = [f"• [{n.categoria}] {n.titulo} ({n.fuente}) {n.url}" for n in nuevas_altas[:15]]
    texto = f"🚛 Radar normativo: {len(nuevas_altas)} novedad(es) de prioridad alta\n" + "\n".join(lineas)
    try:
        requests.post(url, json={"text": texto}, timeout=15)
        log.info("Webhook enviado (%d novedades)", len(nuevas_altas))
    except requests.RequestException as e:
        log.warning("No se pudo enviar el webhook: %s", e)


# --------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------
def genera_html(almacen: Almacen, cfg: dict, plazos: list[dict], salida: Path):
    plantilla = (BASE_DIR / "plantilla.html").read_text(encoding="utf-8")
    ej, estado = almacen.estado_ultima()
    noticias = [n for n in almacen.noticias_ventana(int(cfg.get("ventana_dias_html", 60)))
                if n["prioridad"] in ("alta", "media")]
    for n in noticias:
        n["nuevo"] = (ej is not None and n["ejecucion_alta"] == ej)
    datos = {
        "generado": datetime.now().isoformat(timespec="minutes"),
        "hoy": hoy().isoformat(),
        "ventana_dias": cfg.get("ventana_dias_html", 60),
        "colores": {k: v.get("color", "#64748b") for k, v in cfg["categorias"].items()},
        "noticias": noticias,
        "plazos": plazos,
        "fuentes": estado,
    }
    js = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    html = plantilla.replace("/*__DATOS__*/null", js)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(html, encoding="utf-8")
    log.info("HTML generado: %s (%d noticias)", salida, len(noticias))


# --------------------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------------------
def ejecuta(args) -> int:
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    plazos = json.loads(Path(args.plazos).read_text(encoding="utf-8")).get("plazos", []) if Path(args.plazos).exists() else []
    almacen = Almacen(Path(args.db))

    if args.solo_html:
        genera_html(almacen, cfg, plazos, Path(args.salida))
        return 0

    http = Http(cfg.get("user_agent", "RadarNormativo/1.0"))
    clasif = Clasificador(cfg)
    ultima = almacen.ultima_ejecucion_completa()
    ej = almacen.nueva_ejecucion()

    tareas: list[tuple[str, str, callable]] = []
    boe_cfg = cfg.get("boe", {})
    if boe_cfg.get("activo", True):
        if args.dias_boe:
            dias = args.dias_boe
        elif ultima:
            dias = min((hoy() - ultima.date()).days + 2, int(boe_cfg.get("dias_atras_max", 15)))
        else:
            dias = int(boe_cfg.get("dias_atras_primera_ejecucion", 30))
        tareas.append(("boe", "BOE", lambda: recoge_boe(http, boe_cfg, hoy() - timedelta(days=dias), hoy())))
    for f in cfg.get("rss", []):
        if f.get("activo", True):
            tareas.append((f["id"], f["nombre"], lambda f=f: recoge_rss(http, f)))
    for f in cfg.get("html", []):
        if f.get("activo", True):
            tareas.append((f["id"], f["nombre"], lambda f=f: recoge_html_listado(http, f)))

    nuevas: list[Noticia] = []
    for fid, nombre, fn in tareas:
        t0 = time.time()
        try:
            brutas = fn()
            relevantes = [n for n in (clasif.evalua(x) for x in brutas) if clasif.es_relevante(n)]
            for n in relevantes:
                if almacen.guarda(n, ej):
                    nuevas.append(n)
            almacen.estado_fuente(ej, fid, nombre, True, len(brutas), len(relevantes), None, time.time() - t0)
            log.info("%-28s OK  %4d recibidas · %3d relevantes", nombre, len(brutas), len(relevantes))
        except Exception as e:  # una fuente caída no tumba el resto
            almacen.estado_fuente(ej, fid, nombre, False, 0, 0, f"{type(e).__name__}: {e}"[:300], time.time() - t0)
            log.warning("%-28s ERROR %s", nombre, e)
        almacen.commit()

    almacen.cierra_ejecucion(ej, len(nuevas))
    log.info("Novedades nuevas en esta ejecución: %d (alta: %d)",
             len(nuevas), sum(n.prioridad == "alta" for n in nuevas))
    if ultima:  # en la primera ejecución no avisamos de todo el histórico
        notifica([n for n in nuevas if n.prioridad == "alta"])
    genera_html(almacen, cfg, plazos, Path(args.salida))
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Radar normativo para transporte y logística")
    p.add_argument("--config", default=str(BASE_DIR / "fuentes.json"))
    p.add_argument("--plazos", default=str(BASE_DIR / "plazos.json"))
    p.add_argument("--db", default=str(BASE_DIR / "data" / "radar.sqlite"))
    p.add_argument("--salida", default=str(BASE_DIR / "docs" / "index.html"))
    p.add_argument("--dias-boe", type=int, default=None, help="Días de BOE a revisar (por defecto, desde la última ejecución)")
    p.add_argument("--solo-html", action="store_true", help="No consulta fuentes; solo regenera el HTML")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    return ejecuta(args)


if __name__ == "__main__":
    sys.exit(main())
