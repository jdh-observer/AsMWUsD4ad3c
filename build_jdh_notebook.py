#!/usr/bin/env python3
"""
build_jdh_notebook.py — Word (Chicago footnotes) -> notebook JDH conforme.

Cet article n'a PAS de citations Zotero « live » : ce sont des notes de bas de page
Chicago (Notes-Bibliography). Le JDH, lui, est auteur-date (<cite> + magasin Zotero).
Ce script :
  1. lit le markdown pandoc (corps + notes [^N]),
  2. parse les notes -> bibliographie CSL-JSON (best-effort, dédupliquée),
  3. rend l'auteur-date via citation-js (render.js) -> même moteur que le JDH,
  4. remplace [^N] par des grappes <cite> auteur-date JDH,
  5. PRÉSERVE les notes originales (archives + timecodes) dans une section « Notes »,
  6. structure le notebook avec les tags JDH (title, contributor, abstract, ...),
  7. écrit le magasin metadata["citation-manager"]["items"]["zotero"].
"""
import json, re, subprocess, hashlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = "/tmp/jdh-pipeline"  # render.js
LIB = "88888888"  # espace de noms "import chicago" (chiffres, conforme regex frontend)

# ---------- parsing des notes Chicago ----------
def clean(s):
    for a, b in [(r'\"', '"'), (r"\'", "'"), (r"\[", "["), (r"\]", "]"),
                 (r"\<", "<"), (r"\>", ">"), ("---", "—"), ("--", "–")]:
        s = s.replace(a, b)
    s = re.sub(r"<(https?://[^>\s]+)>", r"\1", s)   # autolinks pandoc <url> -> url nue (sinon vu comme balise HTML)
    return re.sub(r"\s+", " ", s).strip()

YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
YEAR_IN_PAREN = re.compile(r"\([^)]*?(1[89]\d\d|20\d\d)[^)]*?\)")
QUOTED = re.compile(r'"([^"]+)"')
ITALIC = re.compile(r"\*([^*]+)\*")

def split_top(text, sep=";"):
    """Découpe sur `sep` hors parenthèses ET hors italique (titres type *A; B* ou (Ville; Éd.))."""
    out, depth, ital, cur = [], 0, False, []
    for ch in text:
        if ch == "*":
            ital = not ital
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0 and not ital:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out

def note_chunks(text):
    """Fragments d'une note : d'abord les lignes (une note peut lister 2 œuvres sur 2 lignes),
    puis découpage sur `;` hors parenthèses/italiques."""
    chunks = []
    for line in re.split(r"\n+", text):
        line = line.strip()
        if line:
            chunks.extend(split_top(line, ";"))
    return chunks

# marqueurs de source PRIMAIRE / d'archives -> jamais une référence bibliographique
ARCH_STRONG = ["records", "digital collection", "incorporated", "newsreel",
               "public library", "library of congress"]
PARTICLES = {"le", "la", "van", "von", "de", "du", "da", "del", "della", "di"}
TIMECODE = re.compile(r"\d{1,2}[:.]\d{2}")

def is_archival(chunk):
    """Source PRIMAIRE / d'archives (pas une réf biblio). Un simple URL ne suffit PAS :
    un chapitre savant peut porter un DOI/URL. On exige un marqueur fort (timecode, fonds)."""
    low = clean(chunk).lower()
    return (TIMECODE.search(low) or "video essay" in low
            or any(k in low for k in ARCH_STRONG))

def first_author(head):
    """(surname, given) du 1er auteur d'un en-tête Chicago ; gère 'X and Y' et 'Le Corbusier'."""
    head = re.sub(r"\*", "", head).strip()
    head = re.split(r"\s+and\s+|\s*&\s*", head)[0].strip()   # 1er auteur seulement
    toks = head.split()
    if not toks or not re.match(r"^[A-Z]", toks[0]) or len(toks) > 4:
        return None
    if len(toks) >= 2 and toks[-2].lower() in PARTICLES:      # « Le Corbusier »
        return " ".join(toks[-2:]), " ".join(toks[:-2])
    return toks[-1], " ".join(toks[:-1])

def title_word(c):
    qt, it = QUOTED.search(c), ITALIC.search(c)
    src = qt.group(1) if qt else (it.group(1) if it else "")
    return re.sub(r"[^a-z]", "", src.lower())[:12]

def parse_ref(chunk):
    """Extrait un item CSL-JSON (item, surname, title_word) d'un fragment, ou None."""
    c = clean(chunk)
    if not c or "Featured in the video" in c or is_archival(c):
        return None                          # source d'archives / timecode -> pas une réf
    pin = YEAR_IN_PAREN.findall(c)
    ally = YEAR.findall(c)
    year = int(pin[0]) if pin else (int(ally[0]) if ally else None)   # 1re année (chapitre avbefore livre)
    qt, it = QUOTED.search(c), ITALIC.search(c)
    fa = first_author(re.split(r",", c, 1)[0])
    if qt:                                   # article / chapitre
        title, container, typ = qt.group(1), (it.group(1) if it else None), "article-journal"
    elif it:                                 # livre
        title, container, typ = it.group(1), None, "book"
    else:
        return None
    if not fa or not year:
        return None                          # short-form (pas d'année) : résolu plus tard
    surname, given = fa
    key = f"{re.sub(r'[^A-Za-z]','',surname)}{year}".upper()
    item = {"id": key, "type": typ, "title": clean(title),
            "author": [{"family": surname, "given": given}],
            "issued": {"date-parts": [[year]]}}
    if container:
        item["container-title"] = clean(container)
    return item, surname.lower(), title_word(c)

def try_shortform(chunk, by_sur, by_surtw):
    """Rattache une forme courte Chicago (Auteur, "titre", pages — sans année) à une entrée connue."""
    c = clean(chunk)
    if not c or is_archival(c) or not (QUOTED.search(c) or ITALIC.search(c)):
        return None
    fa = first_author(re.split(r",", c, 1)[0])
    if not fa:
        return None
    sur, tw = fa[0].lower(), title_word(c)
    if (sur, tw) in by_surtw:
        return by_surtw[(sur, tw)]
    cand = by_sur.get(sur, [])
    return cand[0] if len(cand) == 1 else None

def parse_note(text):
    items = []
    for chunk in split_top(text, ";"):
        r = parse_ref(chunk)
        if r:
            items.append(r)
    return items, clean(text)

def is_content(chunk):
    """True si le fragment est du contenu NON bibliographique à conserver
    (source d'archive, URL, timecode, prose) — pas une réf ni une forme courte."""
    c = clean(chunk)
    if not c:
        return False
    if is_archival(c):
        return True                       # archive / URL / timecode -> garder
    if QUOTED.search(c) or ITALIC.search(c):
        return False                      # forme courte bibliographique -> jeter
    if re.fullmatch(r'[\dp.,\s–\-]+', c):
        return False                      # simple pagination -> jeter
    return len(c) > 40                    # prose substantielle -> garder

def note_residual(text):
    """Retourne le contenu non-bibliographique d'une note (chunks conservés), ou []."""
    return [clean(ch) for ch in note_chunks(text) if is_content(ch)]

# ---------- rendu auteur-date via citation-js ----------
def render(bib, clusters):
    if not clusters:
        return []
    p = subprocess.run(["node", os.path.join(PIPE, "render.js")],
                       input=json.dumps({"bib": bib, "clusters": clusters}),
                       capture_output=True, text=True)
    if p.returncode:
        sys.exit("render.js: " + p.stderr)
    return json.loads(p.stdout)["labels"]

def cid(x): return hashlib.sha1(x.encode()).hexdigest()[:5]

# ---------- construction ----------
def main():
    md = open(os.path.join(HERE, "_converted.md")).read()
    m = re.search(r"^\[\^\d+\]:", md, re.M)
    body, notesblock = md[:m.start()], md[m.start():]
    notes = {}
    for nm in re.finditer(r"^\[\^(\d+)\]:\s*(.*?)(?=^\[\^\d+\]:|\Z)", notesblock, re.M | re.S):
        notes[int(nm.group(1))] = nm.group(2).strip()

    # --- passe 1 : extraire toutes les références COMPLÈTES + construire l'index ---
    bib_by_id, dedup, by_sur, by_surtw = {}, {}, {}, {}
    for n in sorted(notes):
        for ch in note_chunks(notes[n]):
            r = parse_ref(ch)
            if not r:
                continue
            item, sur, tw = r
            if (sur, tw) in dedup:
                continue
            key = item["id"]
            while key in bib_by_id and bib_by_id[key]["title"] != item["title"]:
                key += "B"
            item["id"] = key
            bib_by_id[key] = item
            dedup[(sur, tw)] = key
            by_surtw[(sur, tw)] = key
            by_sur.setdefault(sur, [])
            if key not in by_sur[sur]:
                by_sur[sur].append(key)

    # --- passe 2 : grappe par note = réfs complètes + formes courtes résolues ---
    note_clusters = {}
    for n in sorted(notes):
        ids = []
        for ch in note_chunks(notes[n]):
            r = parse_ref(ch)
            k = dedup[(r[1], r[2])] if r else try_shortform(ch, by_sur, by_surtw)
            if k and k not in ids:
                ids.append(k)
        note_clusters[n] = ids

    anchors = {k: f"{LIB}/{k}" for k in bib_by_id}
    bib_relabeled = [dict(bib_by_id[k], id=anchors[k]) for k in bib_by_id]
    ordered_notes = [n for n in sorted(note_clusters) if note_clusters[n]]
    clusters = [[anchors[k] for k in note_clusters[n]] for n in ordered_notes]
    labels = render(bib_relabeled, clusters)
    label_of = dict(zip(ordered_notes, labels))

    # une note est CONSERVÉE en section Notes si elle a du contenu non-biblio,
    # ou si aucune œuvre n'a pu être extraite (filet : jamais d'ancre morte)
    kept_notes = {n for n in notes if note_residual(notes[n]) or not note_clusters[n]}

    cell_citations = {}
    def repl(mobj):
        n = int(mobj.group(1))
        ids = note_clusters.get(n, [])
        out = ""
        if ids:
            loc = cid(f"n{n}")
            href = "#zotero%7C" + anchors[ids[0]].replace("/", "%2F")
            cell_citations[loc] = [{"id": anchors[k], "source": "zotero"} for k in ids]
            out = f'<cite id="{loc}"><a href="{href}">{label_of[n]}</a></cite>'
        if n in kept_notes:                 # renvoi vers la section Notes (invariant sup<->note)
            out += f'<sup>[{n}]</sup>'
        return out
    body_conv = re.sub(r"\[\^(\d+)\]", repl, body)

    lines = [clean(l) for l in body_conv.split("\n")]
    cells = []
    def md_cell(src, tags=None):
        c = {"cell_type": "markdown", "id": cid(src[:40] + str(len(cells))),
             "metadata": ({"tags": tags} if tags else {}), "source": src}
        cells.append(c); return c

    md_cell("# The City of Tomorrow: Urban Visions at the New York World's Fair of 1939", ["title"])
    md_cell("### Mara Oliva [https://orcid.org/0000-0001-7444-5203](https://orcid.org/0000-0001-7444-5203)\nUniversity of Reading", ["contributor"])
    md_cell("[![cc-by](https://licensebuttons.net/l/by/4.0/88x31.png)](https://creativecommons.org/licenses/by/4.0/)  \n©Mara Oliva. Published by De Gruyter in cooperation with the University of Luxembourg Centre for Contemporary and Digital History. Open Access under [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/).", ["copyright"])
    cells.append({"cell_type": "code", "id": cid("cover"), "metadata": {"tags": ["cover"]},
                  "execution_count": None, "outputs": [],
                  "source": 'from IPython.display import VimeoVideo\nVimeoVideo("1065468810", h="da976f44be", width=640)'})
    md_cell("Video essay, Archives, Animation, Film, Sound, Text, New York World's Fair, Urban planning, Utopia", ["keywords"])
    md_cell("*The City of Tomorrow* is a dynamic audiovisual project that explores urban visions presented at the New York World's Fair of 1939. This accompanying statement addresses the historiography of the Fair and its relationship to urban planning, and reflects on the videographic strategies of the essay itself — where the video is not an illustration of the research but the research in its own right.", ["abstract"])

    fig_re = re.compile(r"^[:.]\s*(.+?)\s*Featured in the video essay at:\s*(.+)$")
    para, figno = [], 0
    def flush():
        if para:
            txt = " ".join(para).strip()
            if txt:
                md_cell(txt)
            para.clear()
    for ln in lines:
        if not ln:
            flush(); continue
        if ln.strip("* ").upper() == "INTRODUCTION":
            flush(); md_cell("## Introduction"); continue
        if ln.startswith("**") and ln.endswith("**") and len(ln) < 80:
            flush(); md_cell("## " + ln.strip("* ")); continue
        fm = fig_re.match(ln)
        if fm:
            flush(); figno += 1
            cap = clean(fm.group(1)); tc = clean(fm.group(2))
            md_cell(f'![figure](media/placeholder.png)\n*Fig. {figno}. {cap} (video essay, {tc})*', [f"figure-{figno}"])
            continue
        para.append(ln)
    flush()

    for c in cells:
        if c["cell_type"] == "markdown" and "<cite" in c["source"]:
            loc_here = {loc: refs for loc, refs in cell_citations.items() if f'id="{loc}"' in c["source"]}
            if loc_here:
                c["metadata"]["citation-manager"] = {"citations": loc_here}

    # --- intégration des cellules de code de l'article.ipynb ORIGINAL ---
    orig = json.load(open(os.path.join(HERE, "article_original.ipynb")))
    code_cells = [c for c in orig["cells"]
                  if c["cell_type"] == "code"
                  and "".join(c["source"]).strip()
                  and "Vimeo" not in "".join(c["source"])]     # le cover est déjà géré proprement
    if code_cells:
        md_cell("## Code")
        for c in code_cells:
            cells.append({"cell_type": "code", "id": cid("code" + "".join(c["source"])[:20]),
                          "metadata": c.get("metadata", {}), "execution_count": None,
                          "outputs": [], "source": "".join(c["source"]) if isinstance(c["source"], list) else c["source"]})

    # --- section Notes : notes conservées (contenu non biblio, ou note non convertie) ---
    #   invariant : n ∈ kept_notes  <=>  un <sup>[n]</sup> existe dans le corps
    notes_md = ["## Notes", ""]
    for n in sorted(kept_notes):
        residual = note_residual(notes[n])
        notes_md.append(f"{n}. {'; '.join(residual) if residual else clean(notes[n])}")
    if len(notes_md) > 2:
        md_cell("\n".join(notes_md))

    biblines = ["## Bibliography", "", '<!-- BIBLIOGRAPHY START -->']
    for k in sorted(bib_by_id, key=lambda x: bib_by_id[x]["author"][0]["family"]):
        it = bib_by_id[k]; au = it["author"][0]
        cont = f' *{it.get("container-title","")}*' if it.get("container-title") else ""
        biblines.append(f'- {au["family"]}, {au.get("given","")}. ({it["issued"]["date-parts"][0][0]}). *{it["title"]}*.{cont}')
    biblines.append('<!-- BIBLIOGRAPHY END -->')
    md_cell("\n".join(biblines))

    store = {anchors[k]: dict(bib_by_id[k], id=anchors[k], system_id="zotero|" + anchors[k]) for k in bib_by_id}
    nb = {"cells": cells,
          "metadata": {"citation-manager": {"items": {"zotero": store}},
                       "kernelspec": {"display_name": "Python 3 (ipykernel)", "language": "python", "name": "python3"},
                       "language_info": {"name": "python", "version": "3.11"}},
          "nbformat": 4, "nbformat_minor": 5}
    out = os.path.join(HERE, "article_jdh.ipynb")
    json.dump(nb, open(out, "w"), ensure_ascii=False, indent=1)
    print(f"OK -> {out}")
    print(f"  {len(bib_by_id)} references extraites | {len([c for c in clusters if c])} notes -> citations <cite>")
    print(f"  {len(cells)} cellules | {len(cell_citations)} appels de citation")

if __name__ == "__main__":
    main()
