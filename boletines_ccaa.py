"""
Lectores de boletines oficiales autonómicos para el radar normativo.

Cada lector recibe (http, desde, hasta) y devuelve una lista de dicts con:
    titulo, enlace, fecha (ISO yyyy-mm-dd), seccion, organismo
El radar los convierte en noticias, los puntúa y los filtra igual que el resto de fuentes.

* Los que admiten fecha recorren cada día de la ventana (sirven para recuperar histórico).
* Los que solo ofrecen RSS del último boletín traen lo del día (sin histórico).
Un lector que falla no tumba el resto: el radar lo registra en "Estado de las fuentes".
"""
from __future__ import annotations

import io
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup

PAUSA = 0.4  # segundos entre peticiones al mismo boletín


# --------------------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------------------
def _dias(desde: date, hasta: date, laborables: tuple[int, ...] = (0, 1, 2, 3, 4, 5)) -> list[date]:
    """Días de la ventana (más reciente primero). Por defecto de lunes a sábado."""
    out, d = [], hasta
    while d >= desde:
        if d.weekday() in laborables:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _txt(x) -> str:
    if x is None:
        return ""
    if hasattr(x, "get_text"):
        x = x.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", str(x)).strip()


def _fecha_rss(e) -> str | None:
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return datetime(*e[k][:6]).date().isoformat()
    for k in ("published", "updated"):
        v = e.get(k)
        if not v:
            continue
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v)
        if m:
            return "-".join(m.groups())
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
        if m:
            d, mth, y = map(int, m.groups())
            return date(y, mth, d).isoformat()
        try:
            return parsedate_to_datetime(v).date().isoformat()
        except Exception:
            pass
    return None


def _item(titulo, enlace, fecha, seccion="", organismo="") -> dict:
    return {"titulo": _txt(titulo), "enlace": enlace, "fecha": fecha,
            "seccion": _txt(seccion), "organismo": _txt(organismo)}


def _soup(r, encoding: str | None = None) -> BeautifulSoup:
    contenido = r.content.decode(encoding, errors="replace") if encoding else r.text
    return BeautifulSoup(contenido, "html.parser")


# --------------------------------------------------------------------------------------
# Extremadura · DOE (HTML por fecha; ISO-8859-1)
# --------------------------------------------------------------------------------------
def doe(http, desde: date, hasta: date) -> list[dict]:
    out = []
    for d in _dias(desde, hasta):
        r = http.get(f"https://doe.juntaex.es/ultimosdoe/mostrardoe.php?fecha={d:%Y%m%d}&t=o")
        r.raise_for_status()
        s = _soup(r, "iso-8859-1")
        sec = org = ""
        for el in s.find_all(["span", "div"]):
            cls = el.get("class") or []
            if el.name == "span" and cls and re.fullmatch(r"d\d", cls[0]):
                sec = _txt(el)
            elif el.name == "span" and "DOE2" in cls and el.find_parent("div", class_="justificado") is None:
                org = _txt(el)
            elif el.name == "div" and "justificado" in cls:
                t, a = el.find("span", class_="DOE4"), el.find("a", class_="enlace_dis")
                if t and a and a.get("href"):
                    out.append(_item(t, urljoin("https://doe.juntaex.es/", a["href"]), d.isoformat(), sec, org))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Galicia · DOG (HTML por fecha, castellano)
# --------------------------------------------------------------------------------------
def dog(http, desde: date, hasta: date) -> list[dict]:
    out = []
    for d in _dias(desde, hasta, (0, 1, 2, 3, 4)):
        for n in range(1, 12):
            r = http.get(f"https://www.xunta.gal/dog/Publicados/{d:%Y}/{d:%Y%m%d}/Secciones{n}_es.html")
            if r.status_code == 404:
                break
            r.raise_for_status()
            s = _soup(r)
            sec = org = ""
            for el in s.select("p.dog-toc-nivel-1, p.dog-toc-nivel-2, p.dog-toc-organismo, li.dog-toc-sumario"):
                c = el.get("class")[0]
                if c == "dog-toc-nivel-1":
                    sec = _txt(el)
                elif c == "dog-toc-nivel-2":
                    sec = sec.split(" | ")[0] + " | " + _txt(el)
                elif c == "dog-toc-organismo":
                    org = _txt(el)
                else:
                    a = el.find("a")
                    if a and a.get("href"):
                        out.append(_item(a, urljoin("https://www.xunta.gal", a["href"]), d.isoformat(), sec, org))
            time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Comunidad de Madrid · BOCM (XML por fecha; el XML repite disposiciones -> deduplicar)
# --------------------------------------------------------------------------------------
def bocm(http, desde: date, hasta: date) -> list[dict]:
    out, vistos = [], set()
    for d in _dias(desde, hasta):
        f = f"{d:%Y%m%d}"
        r = http.get(f"https://www.bocm.es/boletin/CM_Orden_BOCM/{d:%Y/%m/%d}/BOCM-{f}-1.xml")
        if r.status_code != 200 or not r.content.lstrip().startswith(b"<"):
            continue  # ese día no hubo boletín
        num = ET.fromstring(r.content).findtext(".//diario_numero")
        if not num:
            continue
        r = http.get(f"https://www.bocm.es/boletin/CM_Boletin_BOCM/{d:%Y/%m/%d}/BOCM-{f}{num}.xml")
        r.raise_for_status()
        raiz = ET.fromstring(r.content)
        for sec in raiz.iter("seccion"):
            for ap in sec.iter("apartado"):
                for org in ap.iter("organismo"):
                    for dsp in org.iter("disposicion"):
                        ident = dsp.findtext("identificador")
                        if not ident or ident in vistos:
                            continue
                        vistos.add(ident)
                        out.append(_item(dsp.findtext("titulo", ""), dsp.findtext("url_html") or "", d.isoformat(),
                                         f"{sec.get('nombre', '')} · {ap.get('nombre', '')}", org.get("nombre", "")))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# País Vasco · BOPV (XML por número; se parte del último y se retrocede hasta cubrir la ventana)
# --------------------------------------------------------------------------------------
def _bopv_fecha(http, yyyy: str, mm: str, yy: str, num: int) -> date | None:
    r = http.get(f"https://www.euskadi.eus/bopv2/datos/{yyyy}/{mm}/s{yy}_{num:04d}.shtml")
    if r.status_code != 200:
        return None
    t = r.content.decode("iso-8859-1", errors="replace")
    # cabecera del sumario: "199, lunes 19 de octubre de 2026" (no confundir con fechas de los títulos)
    t = re.sub(r"<[^>]+>", "", t)  # la cabecera es "<span>199</span>, lunes 19 de octubre de  2026"
    m = re.search(rf"\b{num}\s*,\s*[a-záéíóúñ]+\s+(\d{{1,2}})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{{4}})", t, re.I)
    if not m:
        return None
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre"]
    try:
        return date(int(m.group(3)), meses.index(m.group(2).lower()) + 1, int(m.group(1)))
    except ValueError:
        return None


def bopv(http, desde: date, hasta: date) -> list[dict]:
    r = http.get("https://www.euskadi.eus/bopv2/datos/Ultimo.xml")
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    m = re.search(r"N\D*(\d+)", feed.feed.get("title", ""))
    if not m:
        raise ValueError("BOPV: no se encuentra el número del último boletín")
    num = int(m.group(1))
    out, mes_actual = [], hasta
    for _ in range(400):  # tope de seguridad
        if num < 1:
            break
        # el boletín puede ser de este mes o del anterior (cambio de mes)
        encontrado = None
        for ref in (mes_actual, mes_actual.replace(day=1) - timedelta(days=1)):
            yyyy, mm, yy = f"{ref:%Y}", f"{ref:%m}", f"{ref:%y}"
            rx = http.get(f"https://www.euskadi.eus/bopv2/datos/{yyyy}/{mm}/s{yy}_{num:04d}.xml")
            if rx.status_code == 200 and rx.content.lstrip().startswith(b"<"):
                encontrado = (yyyy, mm, yy, rx)
                break
        if not encontrado:
            num -= 1
            continue
        yyyy, mm, yy, rx = encontrado
        fecha = _bopv_fecha(http, yyyy, mm, yy, num) or date(int(yyyy), int(mm), 1)
        if fecha < desde:
            break
        mes_actual = date(int(yyyy), int(mm), 1)
        raiz = ET.fromstring(rx.content)
        sec = org = tit = ""
        for el in raiz:
            tag, txt = el.tag, (el.text or "").strip()
            if tag == "BOPVSumarioSeccion" and txt:
                sec = txt
            elif tag == "BOPVSumarioOrganismo":
                org = txt
            elif tag == "BOPVSumarioTitulo":
                tit = txt
            elif tag == "BOPVSumarioOrden" and txt.isdigit():
                out.append(_item(tit, f"https://www.euskadi.eus/bopv2/datos/{yyyy}/{mm}/{yy}{int(txt):05d}a.shtml",
                                 fecha.isoformat(), sec, org))
        num -= 1
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Andalucía · BOJA (calendario JSON + HTML de cada sección)
# --------------------------------------------------------------------------------------
BOJA = "https://www.juntadeandalucia.es"
BOJA_SECCIONES = ("s51", "s54", "s57")  # disposiciones generales, otras disposiciones, otros anuncios


def boja(http, desde: date, hasta: date) -> list[dict]:
    # el calendario solo admite fechas relativas a "ahora" (con hora): se pide un día más y se filtra
    n = max((date.today() - desde).days, 0) + 1
    r = http.get(f"{BOJA}/ssdigitales/datasets/contentapi/boja/calendario",
                 params={"fechaDesde": f"now-{n}d", "fechaHasta": "now"})
    r.raise_for_status()
    out = []
    for dia in r.json().get("resultado", []):
        dd, mm, yy = dia["bojaDate"].split("/")
        fecha = f"{yy}-{mm}-{dd}"
        if not (desde.isoformat() <= fecha <= hasta.isoformat()):
            continue
        for b in dia.get("boja", []):
            base = urljoin(BOJA, b["enlace"]).rsplit("/", 1)[0] + "/"
            for s in BOJA_SECCIONES:
                rs = http.get(f"{base}{s}.html")
                if rs.status_code == 404:
                    continue
                rs.raise_for_status()
                cuerpo = _soup(rs).select_one("#cuerpo")
                if cuerpo is None:
                    continue
                sec = org = ""
                for el in cuerpo.find_all(["h2", "h3", "p", "div"]):
                    cls = el.get("class") or []
                    if el.name in ("h2", "h3"):
                        sec = _txt(el)
                    elif el.name == "p" and "h5" in cls:
                        org = _txt(el)
                    elif el.name == "div" and "item" in cls:
                        p, a = el.find("p"), el.select_one("a.item_html")
                        if p and a and a.get("href"):
                            out.append(_item(p, urljoin(BOJA, a["href"]), fecha, sec, org))
                time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Aragón · BOA (datos abiertos JSON por fecha; ISO-8859-1)
# --------------------------------------------------------------------------------------
def boa(http, desde: date, hasta: date) -> list[dict]:
    import json
    out = []
    for d in _dias(desde, hasta, (0, 1, 2, 3, 4)):
        inicio = 1
        while True:
            url = ("https://www.boa.aragon.es/cgi-bin/EBOA/BRSCGI?CMD=VERLST&BASE=BOLE"
                   f"&DOCS={inicio}-{inicio + 199}&SEC=OPENDATABOAJSONAPP&OUTPUTMODE=JSON&SEPARADOR=&PUBL={d:%Y%m%d}")
            r = http.get(url)
            r.raise_for_status()
            txt = r.content.decode("latin-1", errors="replace").strip()
            if not txt.startswith("["):
                break  # sin boletín ese día (devuelve HTML)
            datos = json.loads(txt)
            for it in datos:
                docn = it.get("DOCN")
                enlace = f"https://www.boa.aragon.es/cgi-bin/EBOA/BRSCGI?CMD=VERDOC&BASE=BOLE&DOCN={docn}"
                sec = " · ".join(x for x in (it.get("Seccion"), it.get("Subseccion")) if x)
                out.append(_item(it.get("Titulo", ""), enlace, d.isoformat(), sec, it.get("Emisor", "")))
            if len(datos) < 200:
                break
            inicio += 200
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Asturias · BOPA (sumario diario en PDF; se lee con pdfplumber)
# --------------------------------------------------------------------------------------
_RX_COD = re.compile(r"\[C[óo]d\.\s*(\d{4})\s*[-‑]\s*(\d{5})\]")
_RX_INICIO_TITULO = re.compile(
    r"\b(Resoluci[óo]n|Decreto|Ley|Orden|Anuncio|Acuerdo|Edicto|Extracto|Informaci[óo]n|Correcci[óo]n|"
    r"Notificaci[óo]n|Convenio|Instrucci[óo]n|Circular|Citaci[óo]n|C[ée]dula|Requerimiento|Emplazamiento|"
    r"Aprobaci[óo]n|Bases|Convocatoria)\b", re.I)


def _versalitas(texto: str) -> str:
    """'ReSoluCIón de 1 de...' -> 'Resolución de 1 de...' (el PDF del BOPA usa versalitas)."""
    def arregla(m):
        p = m.group(0)
        return p.capitalize() if re.search(r"[a-záéíóúñ]", p[1:]) and re.search(r"[A-ZÁÉÍÓÚÑ]", p[1:]) else p
    return re.sub(r"\w+", arregla, texto)


def _pdf_texto(contenido: bytes) -> str:
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def bopa(http, desde: date, hasta: date) -> list[dict]:
    out = []
    for d in _dias(desde, hasta):
        r = http.get(f"https://www.asturias.es/bopa/{d:%Y/%m/%d}/{d:%Y%m%d}.pdf")
        if r.status_code == 404 or not r.content.startswith(b"%PDF"):
            continue
        texto = _pdf_texto(r.content).replace("‑", "-").replace("\xad", "")
        sub = ""
        buf: list[str] = []
        for linea in (l.strip() for l in texto.splitlines()):
            if not linea:
                continue
            compacta = re.sub(r"\s", "", linea)
            if re.search(r"^(Lunes|Martes|Mi[ée]rcoles|Jueves|Viernes|S[áa]bado|Domingo),|BOLET[ÍI]NOFICIAL|^SUMARIO$|^N[úu]m\.|ISSN|Dep[óo]sitoLegal|^https?:",
                         compacta, re.I) or re.fullmatch(r"\[\d+p[áa]gs?\.\]", compacta):
                continue
            m = re.match(r"^[•·]{1,2}\s*(.+)$", linea)
            if m:
                sub, buf = _versalitas(m.group(1)).capitalize(), []  # descarta restos de portada
                continue
            if re.match(r"^(Direc:|[A-ZÑÍ]{2,12}$)", linea):  # dirección y texto vertical de la portada
                continue
            buf.append(linea)
            c = _RX_COD.search(linea)
            if c:
                bloque = " ".join(buf)
                buf = []
                org = ""
                mt = _RX_INICIO_TITULO.search(bloque)
                if mt and mt.start() > 0:
                    org, bloque = bloque[:mt.start()].strip(), bloque[mt.start():]
                titulo = _RX_COD.split(bloque)[0].strip()
                titulo = _versalitas(re.sub(r"\[\d+\s*p[áa]gs?\.\]", "", titulo).strip())
                org = _versalitas(org)
                cod = f"{c.group(1)}-{c.group(2)}"
                out.append(_item(titulo, f"https://www.asturias.es/bopa/{d:%Y/%m/%d}/{cod}.pdf", d.isoformat(),
                                 sub, org))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Illes Balears · BOIB (listado mensual -> boletín -> secciones I, III y V)
# --------------------------------------------------------------------------------------
BOIB = "https://www.caib.es"


def boib(http, desde: date, hasta: date) -> list[dict]:
    boletines: list[tuple[date, str]] = []
    mes = date(desde.year, desde.month, 1)
    while mes <= hasta:
        r = http.get(f"{BOIB}/eboibfront/es/{mes.year}/mes/{mes.month - 1}")
        r.raise_for_status()
        for a in _soup(r).find_all("a", href=True, title=True):
            m = re.search(r"del dia (\d{1,2})", a["title"])
            if m and re.search(r"/eboibfront/es/\d{4}/\d+/?$", a["href"]):
                f = date(mes.year, mes.month, int(m.group(1)))
                if desde <= f <= hasta:
                    boletines.append((f, urljoin(BOIB, a["href"])))
        mes = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
        time.sleep(PAUSA)
    out, vistos = [], set()
    for f, url in boletines:
        r = http.get(url)
        r.raise_for_status()
        secciones = {url}
        for a in _soup(r).select("a[href*='/seccion-']"):
            if re.search(r"secci[óo]n[- ](i|iii|v)\b", (a.get("href", "") + " " + _txt(a)).lower()):
                secciones.add(urljoin(BOIB, a["href"]))
        for su in secciones:
            rs = r if su == url else http.get(su)
            rs.raise_for_status()
            s = _soup(rs)
            portada = s.select_one("#info .portada") or s.select_one("#info") or s
            sec = _txt(portada.find("h2"))
            if re.search(r"secci[óo]n (ii|iv)\b", sec.lower()):
                continue  # autoridades y personal / administración de justicia
            for caja in portada.select("div.caja"):
                h3 = caja.select_one("h3.organisme")
                org = _txt(h3)
                for li in caja.select("ul.resolucions > li"):
                    p = li.find("p")
                    a = li.select_one('a[aria-label="Exportar a HTML"]') or li.find("a", href=True)
                    if p and a and a.get("href"):
                        enlace = urljoin(BOIB, a["href"])
                        if enlace not in vistos:
                            vistos.add(enlace)
                            out.append(_item(p, enlace, f.isoformat(), sec, org))
            time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Canarias · BOC (archivo anual -> boletín por número; HTML)
# --------------------------------------------------------------------------------------
BOC_CAN = "https://www.gobiernodecanarias.org"


def boc_canarias(http, desde: date, hasta: date) -> list[dict]:
    boletines = []
    for anio in range(desde.year, hasta.year + 1):
        r = http.get(f"{BOC_CAN}/boc/archivo/{anio}/")
        r.raise_for_status()
        for a in _soup(r).find_all("a", href=True, title=True):
            m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", a["title"])
            if m and "/boc/" in a["href"]:
                f = date.fromisoformat(m.group(1))
                if desde <= f <= hasta:
                    boletines.append((f, urljoin(BOC_CAN, a["href"])))
        time.sleep(PAUSA)
    out = []
    for f, url in boletines:
        r = http.get(url)
        r.raise_for_status()
        sec = org = ""
        for el in _soup(r, "utf-8").find_all(["h4", "h3", "h5", "li"]):
            cls = el.get("class") or []
            if el.name == "h4":
                sec, org = _txt(el), ""
            elif el.name == "h5":
                org = _txt(el)
            elif el.name == "li" and "justificado_boc" in cls:
                enlaces = el.find_all("a", href=True)
                if len(enlaces) < 2:
                    continue
                html = el.find("a", string=lambda x: x and "Versión HTML" in x)
                destino = (html or enlaces[1])["href"]
                out.append(_item(enlaces[1], urljoin(url, destino), f.isoformat(), sec, org))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Castilla y León · BOCYL (HTML por fecha)
# --------------------------------------------------------------------------------------
def bocyl(http, desde: date, hasta: date) -> list[dict]:
    out = []
    for d in _dias(desde, hasta, (0, 1, 2, 3, 4)):
        r = http.get(f"https://bocyl.jcyl.es/boletin.do?fechaBoletin={d:%d/%m/%Y}")
        r.raise_for_status()
        res = _soup(r).find(id="resultados")
        if res is None:
            continue  # ese día no hubo boletín
        sec = sub = org = ""
        pendiente = None
        for el in res.find_all(["h3", "h4", "h5", "p", "ul"]):
            if el.name == "h3":
                sec = _txt(el)
            elif el.name == "h4":
                sub = _txt(el)
            elif el.name == "h5":
                org = _txt(el)
            elif el.name == "p" and el.parent is res:
                pendiente = (_txt(el), f"{sec} · {sub}".strip(" ·"), org)
            elif el.name == "ul" and "descargaBoletin" in (el.get("class") or []) and pendiente:
                enlaces = el.find_all("a", href=True)
                html = next((a for a in enlaces if "html" in a["href"].lower()), None)
                a = html or (enlaces[0] if enlaces else None)
                if a:
                    out.append(_item(pendiente[0], urljoin("https://bocyl.jcyl.es/", a["href"]), d.isoformat(),
                                     pendiente[1], pendiente[2]))
                pendiente = None
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Castilla-La Mancha · DOCM (HTML por fecha; HTML mal formado -> recorrer en orden)
# --------------------------------------------------------------------------------------
def docm(http, desde: date, hasta: date) -> list[dict]:
    base = "https://docm.jccm.es/docm/"
    out, vistos = [], set()
    for d in _dias(desde, hasta, (0, 1, 2, 3, 4)):
        r = http.get(f"{base}cambiarBoletin.do?fecha={d:%Y%m%d}")
        r.raise_for_status()
        s = _soup(r)
        fecha_pag = _txt(s.select_one("td.fechaDiario"))
        if not fecha_pag or "no está disponible" in s.get_text(" ", strip=True)[:3000]:
            continue
        sec = org = ""
        for el in s.select("h3.cabeceraCategoria, h4.tituloOrganismo, p.sumario"):
            if el.name == "h3":
                sec = _txt(el)
            elif el.name == "h4":
                org = _txt(el)
            else:
                a = el.find("a", href=True)
                if not a:
                    continue
                enlace = urljoin(base, a["href"])
                if enlace in vistos:
                    continue
                vistos.add(enlace)
                titulo = re.sub(r"\s*\[NID [^\]]+\]\s*$", "", _txt(el).replace(" ,", ","))
                out.append(_item(titulo, enlace, d.isoformat(), sec, org))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Cataluña · DOGC (API REST de la web, POST; servidor con TLS antiguo)
# --------------------------------------------------------------------------------------
DOGC_API = "https://portaldogc.gencat.cat/eadop-rest/api/dogc/"


def _dogc_sesion(http):
    """El servidor solo negocia TLS 1.2 con AES256-SHA: se habilita únicamente para ese host."""
    import ssl
    from requests.adapters import HTTPAdapter

    class TLSAntiguo(HTTPAdapter):
        def _ctx(self):
            c = ssl.create_default_context()
            c.set_ciphers("DEFAULT:AES256-SHA:@SECLEVEL=1")
            return c

        def init_poolmanager(self, *a, **k):
            k["ssl_context"] = self._ctx()
            return super().init_poolmanager(*a, **k)

        def proxy_manager_for(self, *a, **k):
            k["ssl_context"] = self._ctx()
            return super().proxy_manager_for(*a, **k)

    sesion = http.s
    if not getattr(sesion, "_dogc_tls", False):
        sesion.mount("https://portaldogc.gencat.cat/", TLSAntiguo())
        sesion._dogc_tls = True
    return sesion


def _post(sesion, url, datos, reintentos=3):
    for i in range(reintentos):
        try:
            r = sesion.post(url, data=datos, timeout=60)
            if r.status_code < 500:
                return r
        except Exception:
            if i == reintentos - 1:
                raise
        time.sleep(2 * (i + 1))
    return r


def dogc(http, desde: date, hasta: date) -> list[dict]:
    s = _dogc_sesion(http)
    numeros: list[str] = []
    mes = date(desde.year, desde.month, 1)
    while mes <= hasta:
        r = _post(s, DOGC_API + "calendarDOGC", {"month": mes.month, "year": mes.year, "language": "es"})
        r.raise_for_status()
        for d in r.json().get("calendar", []):
            if not d.get("hasDOGC"):
                continue
            f = datetime.strptime(d["date"], "%d/%m/%Y").date()
            m = re.search(r"numDOGC=(\d+)", d.get("linkDOGC") or "")
            if m and desde <= f <= hasta:
                numeros.append(m.group(1))
        mes = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
    out = []
    for num in numeros:
        r = _post(s, DOGC_API + "summaryDOGC", {"numDOGC": num, "language": "es"})
        r.raise_for_status()
        for sumari in r.json().get("sumaris", []):
            fecha = datetime.strptime(sumari["dateDOGC"], "%d/%m/%Y").date().isoformat()
            for sec in sumari.get("section", []):
                for h in sec.get("header", []):
                    docs = [(h.get("title", ""), d) for d in h.get("document", [])]
                    for sh in h.get("subheader", []):
                        docs += [(f"{h.get('title', '')} · {sh.get('title', '')}", d) for d in sh.get("document", [])]
                    for org, d in docs:
                        pdf = d.get("linkDownloadDocumentPDF") or ""
                        m = re.search(r"documentId=(\d+)", pdf)
                        enlace = f"https://dogc.gencat.cat/es/document-del-dogc/?documentId={m.group(1)}" if m else pdf
                        out.append(_item(d.get("title", ""), enlace, fecha, sec.get("title", ""), org))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Comunitat Valenciana · DOGV (API JSON; algunos días hay sumarios bis/ter)
# --------------------------------------------------------------------------------------
DOGV_API = "https://dogv.gva.es/dogv-portal/dogv"


def dogv(http, desde: date, hasta: date) -> list[dict]:
    out, vistos = [], set()
    for d in _dias(desde, hasta, (0, 1, 2, 3, 4)):
        r = http.get(DOGV_API, params={"date": d.isoformat(), "lang": "es"}, headers={"Accept": "application/json"})
        r.raise_for_status()
        j = r.json()
        if not j.get("cabecera"):
            continue  # sin diario ese día
        num = j["cabecera"]["numeroDogv"]
        rv = http.get(f"{DOGV_API}/{num}/versionesSumario", headers={"Accept": "application/json"})
        versiones = rv.json() if rv.ok else [1]
        for v in versiones or [1]:
            jj = j if v == 1 else http.get(f"{DOGV_API}/{num}", params={"ordenSumario": v, "lang": "es"},
                                           headers={"Accept": "application/json"}).json()
            for disp in jj.get("disposiciones", []):
                cod = disp.get("codigoInsercion")
                if not cod or cod in vistos:
                    continue
                vistos.add(cod)
                sec = " · ".join(x for x in ((disp.get("seccion") or {}).get("descripcion"),
                                             (disp.get("subseccion") or {}).get("descripcion")) if x)
                out.append(_item(disp.get("titulo", ""), f"https://dogv.gva.es/es/resultat-dogv?signatura={cod}",
                                 d.isoformat(), sec, disp.get("organismo") or ""))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Región de Murcia · BORM (JSON del sumario por fecha; si falla, PDF del sumario)
# Parser de PDF: distingue niveles por la fuente (negrita 11 = sección, 10 = subsección,
# 9 = organismo, 8 = suborganismo) y cierra cada anuncio con el nº de página del margen derecho.
# --------------------------------------------------------------------------------------
BORM = "https://www.borm.es"
_BORM_CABECERA = re.compile(r"^Número\s+(\d+)\s+(\w+),\s+(\d{1,2}) de (\w+) de (\d{4})")
_BORM_EXTRA = re.compile(r"^Número\s+Extraordinario\s+(\d+)", re.I)
_BORM_ITEM = re.compile(r"^(\d{1,6})\s+(\S.*)$")
_BORM_PAG = re.compile(r"\s(\d{1,6})$")
_BORM_SECCION = re.compile(r"^[IVX]+\.\s")           # I. Comunidad Autónoma, III. Administración de Justicia
_BORM_SUBSECCION = re.compile(r"^\d\.\s")            # 1. Disposiciones Generales ... 4. Anuncios
_BORM_RUIDO = (re.compile(r"^www\.borm\.es"), re.compile(r"^NPE:"), re.compile(r"^S U M A R I O"),
         re.compile(r"^Página\s+\d+"))
_BORM_MESES = {m: i for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)}


def _borm_estilo(line):
    chars = [c for c in line["chars"] if c["text"].strip()]
    if not chars:
        return False, 0.0
    bold = sum("Bold" in c["fontname"] for c in chars) / len(chars) > 0.6
    size = max(set(round(c["size"]) for c in chars), key=[round(c["size"]) for c in chars].count)
    return bold, float(size)


def _borm_parse_pdf(data: bytes, ano: int | None = None, numero_boletin: int | None = None) -> list[dict]:
    import pdfplumber

    fecha = None
    seccion = subseccion = organismo = suborganismo = None
    items: list[dict] = []
    cur: dict | None = None
    # cabecera en negrita multilínea pendiente de cerrar: [nivel, texto, top, page]
    hdr = None
    sub_abierto = False

    def flush_hdr():
        nonlocal hdr, seccion, subseccion, organismo, suborganismo, sub_abierto
        if not hdr:
            return
        nivel, texto = hdr[0], " ".join(hdr[1].split())
        if nivel == "seccion":
            seccion, subseccion, organismo, suborganismo = texto, None, None, None
        elif nivel == "subseccion":
            subseccion, organismo, suborganismo = texto, None, None
        elif nivel == "organismo":
            organismo, suborganismo = texto, None
        else:
            # dos cabeceras de 3er nivel seguidas sin anuncios entre medias (p. ej.
            # "Instituto Nacional de Estadística" + "Delegación Provincial ..."): se encadenan
            suborganismo = f"{suborganismo} / {texto}" if (suborganismo and sub_abierto) else texto
        sub_abierto = nivel == "suborganismo"
        hdr = None

    def close_item():
        nonlocal cur
        if cur:
            items.append(cur)
            cur = None

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages):
            for line in page.extract_text_lines(return_chars=True):
                txt = line["text"].strip()
                if not txt:
                    continue
                m = _BORM_CABECERA.match(txt)
                if m:
                    numero_boletin = numero_boletin or int(m.group(1))
                    d, mes, a = int(m.group(3)), _BORM_MESES.get(m.group(4).lower()), int(m.group(5))
                    ano = ano or a
                    if mes and not fecha:
                        fecha = f"{d:02d}-{mes:02d}-{a}"
                    continue
                if _BORM_EXTRA.match(txt) or any(p.match(txt) for p in _BORM_RUIDO):
                    continue
                bold, size = _borm_estilo(line)

                if bold:
                    # Cabecera. Clasificar por tamaño de fuente (11 sección, 10 subsección,
                    # 9 organismo, 8 suborganismo) con respaldo por patrón de texto.
                    if _BORM_SECCION.match(txt) or size >= 10.5:
                        nivel = "seccion"
                    elif _BORM_SUBSECCION.match(txt) or size >= 9.5:
                        nivel = "subseccion"
                    elif size >= 8.5:
                        nivel = "organismo"
                    else:
                        nivel = "suborganismo"
                    close_item()
                    # continuación de cabecera multilínea: mismo nivel, línea contigua y la
                    # línea anterior llegaba casi al margen derecho (si era corta, es otra cabecera)
                    if (hdr and hdr[0] == nivel and hdr[3] == pno and line["top"] - hdr[2] < size * 1.6
                            and hdr[4] > page.width * 0.75):
                        hdr[1] += " " + txt
                        hdr[2], hdr[4] = line["top"], line["x1"]
                    else:
                        flush_hdr()
                        hdr = [nivel, txt, line["top"], pno, line["x1"]]
                    continue

                flush_hdr()
                sub_abierto = False
                m = _BORM_ITEM.match(txt)
                esperado = (cur["numero"] + 1) if cur else (items[-1]["numero"] + 1 if items else None)
                es_nuevo = False
                if m:
                    n = int(m.group(1))
                    if esperado is None or n == esperado:
                        es_nuevo = True
                    elif cur is None or cur.get("pagina") is not None:
                        # número no consecutivo pero el anterior ya estaba cerrado con su página
                        es_nuevo = True
                if es_nuevo:
                    close_item()
                    cur = {"numero": int(m.group(1)), "lineas": [m.group(2)], "pagina": None,
                           "seccion": seccion, "subseccion": subseccion,
                           "organismo": organismo, "suborganismo": suborganismo}
                elif cur:
                    cur["lineas"].append(txt)
                else:
                    continue  # texto suelto antes del primer anuncio
                # ¿la línea termina con el número de página? -> el anuncio se cierra.
                # El nº de página va alineado al margen derecho (x1≈553 de 595) mientras que el
                # texto justificado acaba en x1≈510: así no confundimos "del año 2026" con la página.
                pm = _BORM_PAG.search(cur["lineas"][-1])
                if pm and len(cur["lineas"][-1]) > len(pm.group(0)) and line["x1"] > page.width * 0.89:
                    cur["pagina"] = int(pm.group(1))
                    cur["lineas"][-1] = cur["lineas"][-1][: pm.start()].rstrip()
                    close_item()
        close_item()

    out = []
    for it in items:
        # unir líneas; "concurso-" + "oposición" -> "concurso-oposición"
        titulo = ""
        for ln in it["lineas"]:
            ln = ln.strip()
            if titulo.endswith("-") and ln[:1].islower():
                titulo += ln
            else:
                titulo += (" " if titulo else "") + ln
        titulo = " ".join(titulo.split())
        out.append({"numero": it["numero"], "titulo": titulo, "ano": ano,
                    "seccion": " · ".join(x for x in (it["seccion"], it["subseccion"]) if x),
                    "organismo": " / ".join(x for x in (it["organismo"], it["suborganismo"]) if x)})
    return out



def borm(http, desde: date, hasta: date) -> list[dict]:
    out = []
    for d in _dias(desde, hasta):
        f = f"{d:%d-%m-%Y}"
        r = http.get(f"{BORM}/services/boletin/fecha/{f}", headers={"Accept": "application/json"})
        if r.status_code == 404:
            continue  # domingo o festivo
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", ""):
            raise RuntimeError("BORM: respuesta no JSON (¿protección anti-bot?)")
        info = r.json()
        anuncios = []
        rs = http.get(f"{BORM}/services/boletin/fecha/{f}/sumario", headers={"Accept": "application/json"})
        if rs.ok and "json" in rs.headers.get("content-type", ""):
            for a in rs.json().get("anunciosBoletin", []):
                anuncios.append({"numero": a["numero"], "ano": a["ano"], "titulo": a.get("sumario", ""),
                                 "seccion": " · ".join(x for x in (a.get("apartado"), a.get("subApartado")) if x),
                                 "organismo": " / ".join(x for x in (a.get("anunciante"), a.get("subAnunciante")) if x)})
        else:  # plan B: PDF del sumario
            rp = http.get(f"{BORM}/services/boletin/{info['id']}/sumario/pdf")
            rp.raise_for_status()
            anuncios = _borm_parse_pdf(rp.content, ano=info.get("ano") or d.year, numero_boletin=info.get("numero"))
        for a in anuncios:
            enlace = f"{BORM}/services/anuncio/ano/{a.get('ano') or d.year}/numero/{a['numero']}/pdf"
            out.append(_item(a["titulo"], enlace, d.isoformat(), a["seccion"], a["organismo"]))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# La Rioja · BOR (API XML oficial de datos abiertos)
# --------------------------------------------------------------------------------------
BOR_API = "https://ias1.larioja.org/boletin/ExportarBoletinServlet"


def bor(http, desde: date, hasta: date) -> list[dict]:
    dias: list[tuple[date, str]] = []
    mes = date(desde.year, desde.month, 1)
    while mes <= hasta:
        r = http.get(BOR_API, params={"tipo": 3, "mes": mes.month, "anio": mes.year})
        r.raise_for_status()
        for dd in ET.fromstring(r.content).iter("day"):
            f = datetime.strptime(dd.get("value"), "%d/%m/%Y").date()
            if desde <= f <= hasta:
                dias.append((f, dd.get("numero") or ""))
        mes = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
    out = []
    for f, numero in dias:
        params = {"tipo": 1, "fecha": f"{f:%Y/%m/%d}"}
        if numero:
            params["numero"] = numero
        r = http.get(BOR_API, params=params)
        r.raise_for_status()
        raiz = ET.fromstring(r.content)
        if raiz.get("status") != "ok":
            continue
        for rom in raiz.iter("romano"):
            sec = f"{rom.get('valor', '')}. {rom.get('denominacion', '')}".strip(". ")
            for org in rom.iter("organo"):
                for a in org.iter("anuncio"):
                    refs = {c.get("tipo"): (c.text or "").strip() for c in a.findall("contenido")}
                    ref = refs.get("html")
                    if not ref:
                        continue
                    out.append(_item(a.findtext("titulo") or "", f"https://web.larioja.org/bor/boranuncio?n={ref}",
                                     f.isoformat(), sec, org.get("denominacion", "")))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Melilla · BOME (calendario JSON + HTML del boletín)
# --------------------------------------------------------------------------------------
BOME = "https://bomemelilla.es"


def bome(http, desde: date, hasta: date) -> list[dict]:
    r = http.get(f"{BOME}/api/bomes/calendar", params={"start": desde.isoformat(), "end": hasta.isoformat()},
                 headers={"Accept": "application/json"})
    r.raise_for_status()
    out = []
    for b in r.json():
        fecha = (b.get("start") or "")[:10]
        rb = http.get(urljoin(BOME, b["url"]))
        rb.raise_for_status()
        s = _soup(rb)
        for dep in s.select("div.bome-show-departamento"):
            sec = _txt(dep.find("h3"))
            for org_li in dep.select("ul.organismo-list > li"):
                org = _txt(org_li.find("h4"))
                for li in org_li.select("ul.articulo-list > li"):
                    titulo = _txt(li.find("blockquote"))
                    web = next((a for a in li.select("a[href]") if "articulo" in a["href"]), None)
                    if titulo and web:
                        out.append(_item(titulo, urljoin(BOME, web["href"]), fecha, sec, org))
        time.sleep(PAUSA)
    return out


# --------------------------------------------------------------------------------------
# Registro: id del boletín en fuentes.json -> lector
# --------------------------------------------------------------------------------------
LECTORES = {
    "borm": borm,
    "bor": bor,
    "bome": bome,
    "boja": boja,
    "boa": boa,
    "bopa": bopa,
    "boib": boib,
    "boc_canarias": boc_canarias,
    "bocyl": bocyl,
    "docm": docm,
    "dogc": dogc,
    "dogv": dogv,
    "doe": doe,
    "dog": dog,
    "bocm": bocm,
    "bopv": bopv,
}
