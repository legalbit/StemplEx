#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 https://github.com/legalbit
#
# stemplex.py darf von jedem frei verwendet, kopiert, verändert und
# weitergegeben werden (MIT-Lizenz, siehe Datei LICENSE).
#
# Benötigt pikepdf (MPL-2.0), das bei der Installation (pip install pikepdf)
# separat bezogen wird und unter seiner eigenen Lizenz steht.

"""
stemplex.py – sucht positionierte XObjects und Annotationen in PDF-Dateien
und blendet sie aus (Standard) oder löscht sie (--delete).

Erfasst:
- Image-XObjects
- Form-XObjects
- verschachtelte Image- und Form-XObjects
- Anmerkungen, Links, Kommentare, Widgets/Formularfelder

Koordinaten:
- PDF-Ursprung: untere linke Seitenecke
- 72 Points = 25,4 mm

Installation:
    python -m pip install pikepdf

Vorgehen von minimal/nicht zerstörerisch bis maximal/unumkehrbar:
    --dry-run   nur suchen, nichts schreiben
    (Standard)  Stempel ausblenden (PDF-Ebene, wieder einblendbar)
    --delete    Stempel endgültig löschen

Ausblenden ist der Standardfall. Ohne --output wird automatisch
"<input>_bereinigt.pdf" im selben Ordner erzeugt:
    python stemplex.py original_scan.pdf
    -> original_scan_bereinigt.pdf

Eigener Ausgabename:
    python stemplex.py original_scan.pdf \
      --output original_scan_bereinigt.pdf

Nur suchen, nichts verändern (Dry-Run):
    python stemplex.py original_scan.pdf --dry-run

Mit ausführlicher Zusammenfassung in der Konsole:
    python stemplex.py original_scan.pdf --verbose

Stempel endgültig löschen statt ausblenden (nicht umkehrbar):
    python stemplex.py original_scan.pdf --delete

JSON-Bericht schreiben (standardmäßig AUS):
    python stemplex.py original_scan.pdf --report bericht.json

Suchkriterium (Position der oberen linken Ecke des Objekts):
- Abstand von der linken Kante der SICHTBAREN Seite: 20 mm
- Abstand von der oberen Kante der SICHTBAREN Seite: 20 mm
- Toleranz: +/- 15 mm (Standard, per --tolerance änderbar)
- Die Größe des Objekts spielt keine Rolle (Stempel sind nicht einheitlich).

Die Abstände werden pro Seite berechnet. Dadurch greift das Kriterium auf
jedem Seitenformat (A4, A5, ...) und auch bei Seiten mit /Rotate 90/180/270,
bei denen die sichtbare obere linke Ecke eine andere Ecke der unrotierten
Seite ist.

Optimierungen gegenüber der Ursprungsversion
---------------------------------------------
1. SHA256-Hash wird nur noch für tatsächliche Treffer berechnet (nicht
   mehr für jedes einzelne Bild-XObject auf jeder Seite). Bei großen
   Scans mit hochauflösenden Bildern spart das massiv I/O und CPU-Zeit.
   Der Hash ist nur mit --hash-compare aktiv.
2. Content-Streams werden pro Objekt nur EINMAL geparst und in einem
   Cache gehalten. Die Such- und die Entfernungsphase greifen auf
   denselben Cache zu, statt den Stream zweimal zu parsen/unparsen.
3. Die Zyklen-Erkennung (active_objects) kopiert nicht mehr bei jeder
   Rekursionsstufe das komplette Set, sondern nutzt klassisches
   Backtracking (hinzufügen -> rekursieren -> wieder entfernen).
4. Die Suchkriterien (mm) werden einmalig in Points umgerechnet.
   matches_target() vergleicht direkt in Points, ohne bei jedem Aufruf
   Divisionen durchzuführen.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pikepdf


PT_PER_MM = 72.0 / 25.4

# Suchposition: obere linke Ecke des Objekts, gemessen von der oberen linken
# Ecke der SICHTBAREN Seite (Rotation und Seitengröße werden pro Seite
# berücksichtigt). Hier im Code anpassbar.
TARGET_LEFT_MM = 20.0
TARGET_TOP_MM = 20.0

# Zulässige Abweichung vom Zielwert in mm, in beide Richtungen und für beide
# Achsen. Lässt sich pro Lauf mit --tolerance überschreiben.
DEFAULT_TOLERANCE_MM = 15.0

# Suffix für die automatisch erzeugte Ausgabedatei, wenn --output fehlt.
# Wird vor die Dateiendung gehängt, z. B. "scan.pdf" -> "scan_bereinigt.pdf".
# Hier im Code anpassbar.
DEFAULT_OUTPUT_SUFFIX = "_bereinigt"

# Name der PDF-Ebene, in die ausgeblendete Stempel (Standardmodus) gelegt werden
# (erscheint im Ebenen-Panel des PDF-Readers). Hier im Code anpassbar.
HIDE_LAYER_NAME = "Stempel (ausgeblendet)"

# Optimierung 4: Schwellwerte einmalig in Points umrechnen, damit
# matches_target() nicht bei jedem Aufruf mm-Konvertierungen durchführt.
TARGET_LEFT_PT = TARGET_LEFT_MM * PT_PER_MM
TARGET_TOP_PT = TARGET_TOP_MM * PT_PER_MM


def pt_to_mm(value):
    return value / PT_PER_MM


def non_negative_float(text):
    """argparse-Typ für --tolerance: Zahl >= 0 (Komma wird akzeptiert)."""
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{text}' ist keine Zahl."
        )

    if value < 0:
        raise argparse.ArgumentTypeError(
            "Die Toleranz darf nicht negativ sein."
        )

    return value


def obj_id(obj):
    """Liefert Objektnummer und Generation eines indirekten PDF-Objekts."""
    try:
        objnum, generation = obj.objgen
        return {
            "objnum": objnum,
            "generation": generation,
            "id": f"{objnum} {generation} R",
        }
    except Exception:
        return None


def objgen_key(obj):
    """
    Liefert einen hashbaren Schlüssel für ein PDF-Objekt, der als Cache-
    und Gruppierungsschlüssel dient. Fällt auf id() zurück, falls das
    Objekt kein indirektes Objekt ist (z. B. lose erzeugte Dictionaries).
    """
    try:
        return ("objgen", tuple(obj.objgen))
    except Exception:
        return ("id", id(obj))


def get_inherited(page_obj, key):
    """
    Ermittelt vererbte Einträge des Page Tree, insbesondere:
    /Resources, /MediaBox und /CropBox.
    """
    current = page_obj

    while current is not None:
        if key in current:
            return current[key]

        current = current.get("/Parent")

    return None


def get_dict_value(pdf_dict, key, default=None):
    """
    Robuster Zugriff auf Dictionary-Einträge, auch wenn key als PDF-Name
    oder als String übergeben wird.
    """
    try:
        return pdf_dict.get(key, default)
    except Exception:
        try:
            return pdf_dict.get(pikepdf.Name(key), default)
        except Exception:
            return default


def rect(value):
    """Wandelt ein PDF-Rechteck [x0 y0 x1 y1] in ein Dictionary um."""
    if not value or len(value) != 4:
        return None

    x0, y0, x1, y1 = [float(item) for item in value]

    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "width": x1 - x0,
        "height": y1 - y0,
    }


def rect_with_mm(value):
    """Ergänzt Point-Koordinaten um Millimeterwerte."""
    if not value:
        return None

    result = dict(value)

    for key in ("x0", "y0", "x1", "y1", "width", "height"):
        result[f"{key}_mm"] = round(pt_to_mm(value[key]), 3)

    return result


def matrix_identity():
    return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def matrix_multiply(left, right):
    """
    Multipliziert zwei PDF-Transformationsmatrizen.

    Matrixformat: [a b c d e f]
    """
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right

    return [
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    ]


def transform_point(matrix, x, y):
    """Transformiert einen Punkt mit einer PDF-Matrix."""
    a, b, c, d, e, f = matrix

    return {
        "x": a * x + c * y + e,
        "y": b * x + d * y + f,
    }


def transform_bbox(matrix, bbox):
    """
    Transformiert ein Rechteck und berechnet die umschließende Bounding Box.
    """
    points = [
        transform_point(matrix, bbox["x0"], bbox["y0"]),
        transform_point(matrix, bbox["x0"], bbox["y1"]),
        transform_point(matrix, bbox["x1"], bbox["y0"]),
        transform_point(matrix, bbox["x1"], bbox["y1"]),
    ]

    xs = [point["x"] for point in points]
    ys = [point["y"] for point in points]

    x0 = min(xs)
    y0 = min(ys)
    x1 = max(xs)
    y1 = max(ys)

    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "width": x1 - x0,
        "height": y1 - y0,
    }


def relative_to_page(absolute_bbox, page_bbox):
    """
    Rechnet Koordinaten auf die untere linke Ecke der sichtbaren Seite um.
    """
    return {
        "x0": absolute_bbox["x0"] - page_bbox["x0"],
        "y0": absolute_bbox["y0"] - page_bbox["y0"],
        "x1": absolute_bbox["x1"] - page_bbox["x0"],
        "y1": absolute_bbox["y1"] - page_bbox["y0"],
        "width": absolute_bbox["width"],
        "height": absolute_bbox["height"],
    }


def normalize_rotation(value):
    """
    Normalisiert /Rotate auf 0, 90, 180 oder 270 (Uhrzeigersinn).

    Die PDF-Spezifikation erlaubt nur Vielfache von 90; fehlerhafte Werte
    werden auf das nächste Vielfache gerundet statt das Skript abzubrechen.
    """
    try:
        degrees = int(value)
    except (TypeError, ValueError):
        return 0

    return (round(degrees / 90) * 90) % 360


def build_page_geometry(page_bbox, rotation):
    """
    Fasst die Maße einer Seite zusammen, die für die Umrechnung auf die
    sichtbare obere linke Ecke gebraucht werden.

    page_bbox:  CropBox (sonst MediaBox) der unrotierten Seite
    rotation:   /Rotate, bereits normalisiert (0/90/180/270)

    "width"/"height" sind die Maße der UNROTIERTEN Seite, "visual_width"/
    "visual_height" die der angezeigten Seite (bei 90/270 vertauscht).
    """
    width = page_bbox["width"]
    height = page_bbox["height"]
    swapped = rotation in (90, 270)

    return {
        "bbox": page_bbox,
        "rotation": rotation,
        "width": width,
        "height": height,
        "visual_width": height if swapped else width,
        "visual_height": width if swapped else height,
    }


def visual_position(relative_bbox, page_geometry):
    """
    Rechnet eine Bounding Box (Points, Ursprung unten links der unrotierten
    CropBox) auf die SICHTBARE Seite um und liefert:

        left   Abstand der linken Objektkante von der linken Seitenkante
        top    Abstand der oberen Objektkante von der oberen Seitenkante
        width  sichtbare Breite des Objekts
        height sichtbare Höhe des Objekts

    Alles in Points. Das ist die obere linke Ecke des Objekts, so wie man
    sie im PDF-Reader sieht, unabhängig von Seitengröße und Rotation.

    Herleitung (W/H = Breite/Höhe der unrotierten Seite, x/y relativ zur
    unteren linken Ecke, y wächst nach oben):

        0 Grad:   left = x_min       top = H - y_max
        90 Grad:  left = y_min       top = x_min
        180 Grad: left = W - x_max   top = y_min
        270 Grad: left = H - y_max   top = W - x_max

    Bei 0 Grad ist der Abstand von links also einfach x (keine Umrechnung
    nötig), und der Abstand von oben ist Seitenhöhe minus OBERE Objektkante.
    """
    x_min = min(relative_bbox["x0"], relative_bbox["x1"])
    x_max = max(relative_bbox["x0"], relative_bbox["x1"])
    y_min = min(relative_bbox["y0"], relative_bbox["y1"])
    y_max = max(relative_bbox["y0"], relative_bbox["y1"])

    page_width = page_geometry["width"]
    page_height = page_geometry["height"]
    rotation = page_geometry["rotation"]

    if rotation == 90:
        left, top = y_min, x_min
    elif rotation == 180:
        left, top = page_width - x_max, y_min
    elif rotation == 270:
        left, top = page_height - y_max, page_width - x_max
    else:
        left, top = x_min, page_height - y_max

    if rotation in (90, 270):
        width, height = y_max - y_min, x_max - x_min
    else:
        width, height = x_max - x_min, y_max - y_min

    return {
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def visual_position_mm(position):
    """Rundet eine visual_position() in Millimetern für den Bericht."""
    return {
        f"{key}_mm": round(pt_to_mm(value), 3)
        for key, value in position.items()
    }


def matches_target(position, tolerance_mm):
    """
    Prüft, ob die obere linke Ecke eines Objekts (visual_position) im
    Suchfenster um TARGET_LEFT_MM / TARGET_TOP_MM liegt.

    Die Größe des Objekts wird bewusst ignoriert.
    """
    tolerance_pt = tolerance_mm * PT_PER_MM

    left_matches = abs(position["left"] - TARGET_LEFT_PT) <= tolerance_pt
    top_matches = abs(position["top"] - TARGET_TOP_PT) <= tolerance_pt

    return left_matches and top_matches


def stream_hash(obj):
    """Erzeugt einen Hash der unveränderten Streamdaten eines Objekts."""
    try:
        raw = obj.read_raw_bytes()
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return None


def load_known_hashes(items):
    """
    Lädt bekannte SHA256-Hashes aus den übergebenen --hash-compare-Werten.

    Jeder Wert ist entweder:
    - ein SHA256-Hexwert direkt auf der Kommandozeile, oder
    - der Pfad zu einer Textdatei mit einem Hash pro Zeile
      (leere Zeilen und Zeilen, die mit # beginnen, werden ignoriert).
    """
    known_hashes = set()

    for item in items or []:
        candidate_path = Path(item)

        if candidate_path.is_file():
            with candidate_path.open("r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()

                    if not line or line.startswith("#"):
                        continue

                    known_hashes.add(line.lower())
        else:
            known_hashes.add(item.strip().lower())

    return known_hashes


def xobject_bbox(xobject, current_ctm):
    """
    Berechnet die Bounding Box eines XObjects in Seitenkoordinaten.

    Image:
        Standardgröße 0/0 bis 1/1.

    Form:
        Eigene /BBox und gegebenenfalls /Matrix.
    """
    subtype = str(get_dict_value(xobject, "/Subtype", ""))

    if subtype == "/Image":
        local_bbox = {
            "x0": 0.0,
            "y0": 0.0,
            "x1": 1.0,
            "y1": 1.0,
            "width": 1.0,
            "height": 1.0,
        }

        return transform_bbox(current_ctm, local_bbox)

    if subtype == "/Form":
        form_bbox = rect(get_dict_value(xobject, "/BBox"))

        if not form_bbox:
            return None

        raw_matrix = get_dict_value(
            xobject,
            "/Matrix",
            matrix_identity()
        )

        try:
            form_matrix = [float(value) for value in raw_matrix]
        except Exception:
            form_matrix = matrix_identity()

        effective_ctm = matrix_multiply(current_ctm, form_matrix)

        return transform_bbox(effective_ctm, form_bbox)

    return None


def get_cached_instructions(container, stream_cache):
    """
    Parst den Content-Stream eines Containers (Seite oder Form-XObject)
    nur beim ersten Zugriff und hält das Ergebnis im stream_cache.

    Optimierung 2: Vermeidet doppeltes Parsen/Unparsen desselben Streams
    in der Such- und der Entfernungsphase.
    """
    key = objgen_key(container)

    if key in stream_cache:
        return stream_cache[key]

    try:
        instructions = list(pikepdf.parse_content_stream(container))
    except Exception:
        instructions = None

    stream_cache[key] = instructions
    return instructions


def find_xobjects_recursive(
    container,
    inherited_resources,
    page_number,
    page_geometry,
    tolerance_mm,
    stream_cache,
    hash_compare_enabled=False,
    known_hashes=None,
    base_ctm=None,
    path=None,
    depth=0,
    active_objects=None,
):
    """
    Durchsucht einen Seiteninhalt oder Form-XObject-Inhalt rekursiv.

    Beispielpfad:
        /Page -> /X1 -> /Fm12 -> /Im3

    Das ist erforderlich, weil ein PDF-Stempel häufig nicht unmittelbar
    auf der Seite platziert wird, sondern in einem seitenfüllenden
    Form-XObject wie /X1 verschachtelt ist.
    """
    if base_ctm is None:
        base_ctm = matrix_identity()

    if path is None:
        path = ["/Page"]

    # Optimierung 3: active_objects wird über Backtracking gepflegt statt
    # bei jeder Rekursionsstufe kopiert zu werden.
    if active_objects is None:
        active_objects = set()

    if depth > 20:
        return []

    container_key = objgen_key(container)
    added_to_active = False

    # Schutz gegen fehlerhafte zyklische Form-Referenzen
    if container_key in active_objects:
        return []

    active_objects.add(container_key)
    added_to_active = True

    try:
        own_resources = get_dict_value(container, "/Resources")
        resources = own_resources if own_resources is not None else inherited_resources

        if not resources:
            return []

        xobjects = get_dict_value(
            resources,
            "/XObject",
            pikepdf.Dictionary()
        )

        instructions = get_cached_instructions(container, stream_cache)

        if instructions is None:
            return []

        findings = []
        current_ctm = base_ctm.copy()
        saved_ctms = []

        for instruction_index, instruction in enumerate(instructions):
            operands, operator = instruction
            operator = str(operator)

            if operator == "q":
                saved_ctms.append(current_ctm.copy())
                continue

            if operator == "Q":
                if saved_ctms:
                    current_ctm = saved_ctms.pop()
                continue

            if operator == "cm":
                try:
                    cm_matrix = [float(value) for value in operands]

                    if len(cm_matrix) == 6:
                        current_ctm = matrix_multiply(
                            current_ctm,
                            cm_matrix
                        )
                except Exception:
                    pass

                continue

            # /Name Do: XObject zeichnen
            if operator != "Do" or not operands:
                continue

            resource_name = str(operands[0])
            xobject = get_dict_value(xobjects, resource_name)

            if xobject is None:
                continue

            subtype = str(get_dict_value(xobject, "/Subtype", ""))
            absolute_bbox = xobject_bbox(xobject, current_ctm)

            if absolute_bbox is None:
                continue

            page_relative_bbox = relative_to_page(
                absolute_bbox,
                page_geometry["bbox"]
            )

            position = visual_position(page_relative_bbox, page_geometry)
            is_match = matches_target(position, tolerance_mm)

            finding = {
                "page": page_number,
                "kind": "xobject",
                "subtype": subtype,
                "resource_name": resource_name,
                "object": obj_id(xobject),
                "container_object": obj_id(container),
                "container_instruction_index": instruction_index,
                "xobject_path": path + [resource_name],
                "bbox_points": page_relative_bbox,
                "bbox_mm": rect_with_mm(page_relative_bbox),
                "visual_mm": visual_position_mm(position),
                "matches_target": is_match,

                # Nur intern für die Entfernung; wird vor JSON-Ausgabe entfernt.
                "_container_ref": container,
                "_instruction_index": instruction_index,
                "_resources_ref": resources,
            }

            # Hashing ist standardmäßig AUS. Nur wenn --hash-compare gesetzt
            # ist, wird für Treffer der SHA256 berechnet (und, falls
            # bekannte Hashes übergeben wurden, direkt gegen diese geprüft).
            if hash_compare_enabled and is_match:
                finding["stream_sha256"] = stream_hash(xobject)

                if known_hashes:
                    finding["hash_known"] = (
                        finding["stream_sha256"] in known_hashes
                    )

            if subtype == "/Image":
                finding["pixel_width"] = int(
                    get_dict_value(xobject, "/Width", 0)
                )
                finding["pixel_height"] = int(
                    get_dict_value(xobject, "/Height", 0)
                )
                finding["filter"] = str(
                    get_dict_value(xobject, "/Filter", "")
                )

            findings.append(finding)

            # Rekursiv in Form-XObjects steigen
            if subtype == "/Form":
                raw_form_matrix = get_dict_value(
                    xobject,
                    "/Matrix",
                    matrix_identity()
                )

                try:
                    form_matrix = [
                        float(value)
                        for value in raw_form_matrix
                    ]
                except Exception:
                    form_matrix = matrix_identity()

                child_base_ctm = matrix_multiply(
                    current_ctm,
                    form_matrix
                )

                findings.extend(
                    find_xobjects_recursive(
                        container=xobject,
                        inherited_resources=resources,
                        page_number=page_number,
                        page_geometry=page_geometry,
                        tolerance_mm=tolerance_mm,
                        stream_cache=stream_cache,
                        hash_compare_enabled=hash_compare_enabled,
                        known_hashes=known_hashes,
                        base_ctm=child_base_ctm,
                        path=path + [resource_name],
                        depth=depth + 1,
                        active_objects=active_objects,
                    )
                )

        return findings
    finally:
        if added_to_active:
            active_objects.discard(container_key)


def find_annotations(page, page_number, page_geometry, tolerance_mm):
    """
    Sucht Annotationen, darunter:
    - Text-Kommentare
    - Links
    - Stempel-Annotationen
    - Formularfelder / Widgets
    """
    findings = []
    annotations = list(get_dict_value(page.obj, "/Annots", []))

    for annotation_index, annotation in enumerate(annotations):
        absolute_bbox = rect(get_dict_value(annotation, "/Rect"))

        if absolute_bbox is None:
            continue

        page_relative_bbox = relative_to_page(
            absolute_bbox,
            page_geometry["bbox"]
        )

        position = visual_position(page_relative_bbox, page_geometry)

        finding = {
            "page": page_number,
            "kind": "annotation",
            "subtype": str(
                get_dict_value(annotation, "/Subtype", "")
            ),
            "object": obj_id(annotation),
            "annotation_index": annotation_index,
            "author": str(get_dict_value(annotation, "/T", "")),
            "contents": str(get_dict_value(annotation, "/Contents", "")),
            "subject": str(get_dict_value(annotation, "/Subj", "")),
            "bbox_points": page_relative_bbox,
            "bbox_mm": rect_with_mm(page_relative_bbox),
            "visual_mm": visual_position_mm(position),
            "matches_target": matches_target(position, tolerance_mm),

            # Nur intern für Entfernung
            "_page_ref": page.obj,
            "_annotation_index": annotation_index,
        }

        findings.append(finding)

    return findings


def remove_do_instructions(pdf, removal_targets, stream_cache):
    """
    Entfernt passende /Do-Anweisungen.

    Die Zielobjekte können Seiteninhaltsströme oder Form-XObjects sein.
    Nutzt den bereits in der Suchphase gefüllten stream_cache, um den
    Content-Stream nicht ein zweites Mal zu parsen (Optimierung 2).
    """
    grouped = defaultdict(lambda: {
        "container": None,
        "instruction_indices": set(),
    })

    for target in removal_targets:
        container = target["_container_ref"]
        instruction_index = target["_instruction_index"]
        key = objgen_key(container)

        grouped[key]["container"] = container
        grouped[key]["instruction_indices"].add(instruction_index)

    removal_report = []

    for group in grouped.values():
        container = group["container"]
        indices_to_remove = group["instruction_indices"]

        instructions = get_cached_instructions(container, stream_cache)

        if instructions is None:
            removal_report.append({
                "container": obj_id(container),
                "removed": 0,
                "error": "Content-Stream konnte nicht geparst werden.",
            })
            continue

        kept_instructions = [
            instruction
            for index, instruction in enumerate(instructions)
            if index not in indices_to_remove
        ]

        data = pikepdf.unparse_content_stream(
            kept_instructions
        )

        container_type = str(
            get_dict_value(container, "/Type", "")
        )

        try:
            if container_type == "/Page":
                container["/Contents"] = pikepdf.Stream(
                    pdf,
                    data
                )
            else:
                # Bei einem Form-XObject den Stream selbst überschreiben
                container.write(data)

            removal_report.append({
                "container": obj_id(container),
                "removed": len(indices_to_remove),
            })

        except Exception as exc:
            removal_report.append({
                "container": obj_id(container),
                "removed": 0,
                "error": str(exc),
            })

    return removal_report


def remove_annotations(annotation_targets):
    """Entfernt passende Annotationen aus /Annots."""
    grouped = defaultdict(lambda: {
        "page": None,
        "annotation_indices": set(),
    })

    for target in annotation_targets:
        page_obj = target["_page_ref"]
        annotation_index = target["_annotation_index"]
        key = objgen_key(page_obj)

        grouped[key]["page"] = page_obj
        grouped[key]["annotation_indices"].add(annotation_index)

    removal_report = []

    for group in grouped.values():
        page_obj = group["page"]
        indices_to_remove = group["annotation_indices"]

        annotations = list(get_dict_value(page_obj, "/Annots", []))

        remaining = [
            annotation
            for index, annotation in enumerate(annotations)
            if index not in indices_to_remove
        ]

        if remaining:
            page_obj["/Annots"] = pikepdf.Array(remaining)
        elif "/Annots" in page_obj:
            del page_obj["/Annots"]

        removal_report.append({
            "page_object": obj_id(page_obj),
            "removed": len(indices_to_remove),
        })

    return removal_report


def create_hidden_layer(pdf, layer_name):
    """
    Legt eine Optional-Content-Gruppe (PDF-Ebene) an, die in der
    Standardkonfiguration des Dokuments AUSGESCHALTET ist.

    Die Ebene erscheint in PDF-Readern im Ebenen-Panel und lässt sich dort
    wieder einblenden. Der Inhalt bleibt in der Datei erhalten.
    """
    ocg = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name("/OCG"),
            Name=pikepdf.String(layer_name),
        )
    )

    root = pdf.Root

    if "/OCProperties" not in root:
        root["/OCProperties"] = pikepdf.Dictionary()

    oc_properties = root["/OCProperties"]

    if "/OCGs" not in oc_properties:
        oc_properties["/OCGs"] = pikepdf.Array()

    oc_properties["/OCGs"].append(ocg)

    if "/D" not in oc_properties:
        oc_properties["/D"] = pikepdf.Dictionary()

    default_config = oc_properties["/D"]

    if "/OFF" not in default_config:
        default_config["/OFF"] = pikepdf.Array()

    default_config["/OFF"].append(ocg)

    # /Order bestimmt, was Reader im Ebenen-Panel anzeigen. Manche Reader
    # zeigen eine Ebene NUR an, wenn sie dort gelistet ist, auch wenn sie
    # bereits in /OCGs und /OFF steht. Deshalb immer anlegen, nicht nur
    # wenn /Order schon existierte.
    if "/Order" not in default_config:
        default_config["/Order"] = pikepdf.Array()

    default_config["/Order"].append(ocg)

    return ocg


def unique_property_name(properties, base="/StemplexOC"):
    """Liefert einen noch freien Namen im /Properties-Dictionary."""
    if base not in properties:
        return base

    counter = 1

    while f"{base}{counter}" in properties:
        counter += 1

    return f"{base}{counter}"


def hide_do_instructions(pdf, targets, stream_cache, ocg):
    """
    Blendet passende /Do-Anweisungen aus, statt sie zu löschen.

    Jede betroffene Zeichenanweisung wird in einen Marked-Content-Block
    eingefasst:

        /OC /StemplexOC BDC
        /Name Do
        EMC

    /StemplexOC verweist über /Properties der Resources auf die
    ausgeschaltete Ebene. Der Inhalt bleibt in der Datei erhalten.
    """
    grouped = defaultdict(lambda: {
        "container": None,
        "resources": None,
        "instruction_indices": set(),
    })

    for target in targets:
        container = target["_container_ref"]
        key = objgen_key(container)

        grouped[key]["container"] = container
        grouped[key]["resources"] = target["_resources_ref"]
        grouped[key]["instruction_indices"].add(target["_instruction_index"])

    hide_report = []

    for group in grouped.values():
        container = group["container"]
        resources = group["resources"]
        indices_to_hide = group["instruction_indices"]

        instructions = get_cached_instructions(container, stream_cache)

        if instructions is None or resources is None:
            hide_report.append({
                "container": obj_id(container),
                "hidden": 0,
                "error": "Content-Stream oder Resources nicht verfügbar.",
            })
            continue

        try:
            if "/Properties" not in resources:
                resources["/Properties"] = pikepdf.Dictionary()

            properties = resources["/Properties"]
            property_name = unique_property_name(properties)
            properties[property_name] = ocg

            new_instructions = []

            for index, instruction in enumerate(instructions):
                if index in indices_to_hide:
                    new_instructions.append((
                        [pikepdf.Name("/OC"), pikepdf.Name(property_name)],
                        pikepdf.Operator("BDC"),
                    ))
                    new_instructions.append(instruction)
                    new_instructions.append(([], pikepdf.Operator("EMC")))
                else:
                    new_instructions.append(instruction)

            data = pikepdf.unparse_content_stream(new_instructions)

            container_type = str(
                get_dict_value(container, "/Type", "")
            )

            if container_type == "/Page":
                container["/Contents"] = pikepdf.Stream(pdf, data)
            else:
                container.write(data)

            hide_report.append({
                "container": obj_id(container),
                "hidden": len(indices_to_hide),
            })

        except Exception as exc:
            hide_report.append({
                "container": obj_id(container),
                "hidden": 0,
                "error": str(exc),
            })

    return hide_report


def hide_annotations(annotation_targets, ocg):
    """
    Blendet Annotationen aus, indem /OC auf dieselbe Optional-Content-
    Gruppe (Ebene) gesetzt wird wie bei den XObjects.

    WICHTIG: Nicht über das Hidden-Flag (/F, Bit 2) ausblenden. Das Flag
    hat in Standard-Readern keinen UI-Umschalter und lässt sich nicht
    über ein Ebenen-Panel rückgängig machen – die Annotation wäre dann
    zwar in der Datei vorhanden, aber faktisch dauerhaft unsichtbar.
    /OC macht die Annotation hingegen über dieselbe Ebene steuerbar wie
    ausgeblendete Bilder/Formulare, reversibel im selben Panel.
    """
    hide_report = []

    for target in annotation_targets:
        page_obj = target["_page_ref"]
        annotation_index = target["_annotation_index"]

        try:
            annotation = list(get_dict_value(page_obj, "/Annots", []))[
                annotation_index
            ]
            annotation["/OC"] = ocg

            hide_report.append({
                "annotation": obj_id(annotation),
                "hidden": 1,
            })
        except Exception as exc:
            hide_report.append({
                "annotation": None,
                "hidden": 0,
                "error": str(exc),
            })

    return hide_report


def public_record(record):
    """Entfernt interne Python-Referenzen vor der JSON-Ausgabe."""
    return {
        key: value
        for key, value in record.items()
        if not key.startswith("_")
    }


def inspect_pdf(
    input_path,
    remove=False,
    output_path=None,
    tolerance_mm=DEFAULT_TOLERANCE_MM,
    hash_compare_enabled=False,
    known_hashes=None,
    hide=False,
):
    report = {
        "file": str(input_path),
        "criteria_mm": {
            "reference": "obere linke Ecke der sichtbaren Seite",
            "target_left": TARGET_LEFT_MM,
            "target_top": TARGET_TOP_MM,
            "tolerance": tolerance_mm,
        },
        "matches": [],
        "pages": [],
        "mode": "hide" if hide else "remove",
        "removed_xobjects": [],
        "removed_annotations": [],
        "hidden_xobjects": [],
        "hidden_annotations": [],
    }

    xobject_removal_targets = []
    annotation_removal_targets = []

    # Optimierung 2: Ein Cache pro Lauf, geteilt zwischen Such- und
    # Entfernungsphase sowie über alle Seiten hinweg (wichtig, falls ein
    # Form-XObject von mehreren Seiten referenziert wird).
    stream_cache = {}

    with pikepdf.Pdf.open(input_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_box_array = (
                get_inherited(page.obj, "/CropBox")
                or get_inherited(page.obj, "/MediaBox")
            )

            page_bbox = rect(page_box_array)

            if page_bbox is None:
                report["pages"].append({
                    "page": page_number,
                    "error": "Keine CropBox oder MediaBox vorhanden.",
                })
                continue

            page_resources = get_inherited(
                page.obj,
                "/Resources"
            )

            # /Rotate ist vererbbar (steht bei manchen Dateien nur im
            # Seitenbaum-Knoten), deshalb get_inherited statt Direktzugriff.
            rotation = normalize_rotation(
                get_inherited(page.obj, "/Rotate")
            )

            page_geometry = build_page_geometry(page_bbox, rotation)

            xobject_findings = find_xobjects_recursive(
                container=page.obj,
                inherited_resources=page_resources,
                page_number=page_number,
                page_geometry=page_geometry,
                tolerance_mm=tolerance_mm,
                stream_cache=stream_cache,
                hash_compare_enabled=hash_compare_enabled,
                known_hashes=known_hashes,
            )

            annotation_findings = find_annotations(
                page=page,
                page_number=page_number,
                page_geometry=page_geometry,
                tolerance_mm=tolerance_mm,
            )

            all_findings = xobject_findings + annotation_findings
            matches = [
                finding
                for finding in all_findings
                if finding["matches_target"]
            ]

            for finding in matches:
                report["matches"].append(
                    public_record(finding)
                )

                if finding["kind"] == "xobject":
                    xobject_removal_targets.append(finding)
                elif finding["kind"] == "annotation":
                    annotation_removal_targets.append(finding)

            report["pages"].append({
                "page": page_number,
                "rotation": rotation,
                "visual_size_mm": {
                    "width_mm": round(
                        pt_to_mm(page_geometry["visual_width"]), 3
                    ),
                    "height_mm": round(
                        pt_to_mm(page_geometry["visual_height"]), 3
                    ),
                },
                "all_positioned_objects": [
                    public_record(finding)
                    for finding in all_findings
                ],
                "matches": [
                    public_record(finding)
                    for finding in matches
                ],
            })

        report["output_written"] = False

        if remove and report["matches"]:
            if hide:
                ocg = create_hidden_layer(pdf, HIDE_LAYER_NAME)

                report["hidden_xobjects"] = hide_do_instructions(
                    pdf,
                    xobject_removal_targets,
                    stream_cache,
                    ocg,
                )

                report["hidden_annotations"] = hide_annotations(
                    annotation_removal_targets,
                    ocg,
                )

                # Optional Content erfordert mindestens PDF 1.5
                pdf.save(output_path, min_version="1.5")
            else:
                report["removed_xobjects"] = remove_do_instructions(
                    pdf,
                    xobject_removal_targets,
                    stream_cache,
                )

                report["removed_annotations"] = remove_annotations(
                    annotation_removal_targets
                )

                pdf.save(output_path)

            report["output_written"] = True

    return report


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Durchsucht PDF-Seiten und verschachtelte Form-XObjects "
            "nach positionierten Objekten."
        )
    )

    parser.add_argument(
        "input_pdf",
        help="Zu untersuchende PDF-Datei"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Nur suchen, nichts verändern und keine Ausgabedatei "
            "schreiben. Standardmäßig blendet das Skript passende "
            "Treffer aus; --dry-run schaltet das ab."
        )
    )

    parser.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Treffer endgültig aus der Datei löschen, statt sie "
            "auszublenden. NICHT umkehrbar. Standardmäßig werden Treffer "
            "nur ausgeblendet (XObjects und Annotationen gemeinsam über "
            "eine ausgeschaltete PDF-Ebene), der Inhalt bleibt erhalten "
            "und lässt sich im Ebenen-Panel des Readers wieder "
            "einblenden. Ohne Wirkung bei --dry-run."
        )
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Zusammenfassung (Trefferliste, ausgeblendete/entfernte Objekte) in der "
            "Konsole ausgeben. Ohne diese Option läuft das Skript still "
            "und schreibt nur den JSON-Bericht."
        )
    )

    parser.add_argument(
        "--force-output",
        action="store_true",
        help=(
            "Erzwingt das Überschreiben, falls die Ausgabedatei bereits "
            "existiert. Standardmäßig bricht das Skript in diesem Fall "
            "ohne jede Änderung ab."
        )
    )

    parser.add_argument(
        "--output",
        help=(
            "Ausgabedatei für die bereinigte PDF. Optional: fehlt "
            f"--output, wird automatisch '<input>{DEFAULT_OUTPUT_SUFFIX}"
            ".pdf' im selben Ordner wie die Eingabedatei verwendet. "
            "Bei --dry-run wird dieser Wert ignoriert."
        )
    )

    parser.add_argument(
        "--report",
        help=(
            "Pfad für die JSON-Berichtsdatei. Ohne diese Option wird "
            "KEIN Bericht geschrieben."
        )
    )

    parser.add_argument(
        "--tolerance",
        type=non_negative_float,
        default=DEFAULT_TOLERANCE_MM,
        metavar="MM",
        help=(
            "Zulässige Abweichung in mm vom Zielwert "
            f"({TARGET_LEFT_MM:g} mm von links, {TARGET_TOP_MM:g} mm von "
            "oben, gemessen an der oberen linken Ecke der sichtbaren "
            "Seite), in beide Richtungen und für beide Achsen. "
            f"Standard: {DEFAULT_TOLERANCE_MM:g} mm."
        )
    )

    parser.add_argument(
        "--hash-compare",
        nargs="*",
        metavar="HASH_ODER_DATEI",
        help=(
            "Schaltet die SHA256-Berechnung ein (standardmäßig AUS, "
            "spart Zeit). Ohne weitere Werte wird der Hash für Treffer "
            "nur im Bericht ausgegeben (Feld 'stream_sha256'). Werden "
            "zusätzlich ein oder mehrere SHA256-Hexwerte und/oder Pfade "
            "zu Textdateien mit je einem Hash pro Zeile übergeben, wird "
            "jeder Treffer außerdem gegen diese bekannten Stempel-Hashes "
            "geprüft (Feld 'hash_known')."
        )
    )

    args = parser.parse_args()

    input_path = Path(args.input_pdf)

    if not input_path.is_file():
        raise SystemExit(
            f"Datei nicht gefunden: {input_path}"
        )

    remove = not args.dry_run

    output_path = Path(args.output) if args.output else None

    if remove and output_path is None:
        output_path = input_path.with_name(
            input_path.stem + DEFAULT_OUTPUT_SUFFIX + input_path.suffix
        )

    # Überschreib-Schutz: VOR allem anderen prüfen (vor Hash-Laden, vor
    # dem eigentlichen Durchsuchen der PDF), damit im Konfliktfall
    # garantiert nichts angefasst wird.
    if remove and output_path is not None and output_path.exists() and not args.force_output:
        raise SystemExit(
            f"Ausgabedatei existiert bereits: {output_path}\n"
            "Abgebrochen, um sie nicht versehentlich zu überschreiben. "
            "Mit --force-output kann das Überschreiben erzwungen werden."
        )

    hash_compare_enabled = args.hash_compare is not None
    known_hashes = load_known_hashes(args.hash_compare)

    report_path = Path(args.report) if args.report else None

    report = inspect_pdf(
        input_path=input_path,
        remove=remove,
        output_path=output_path,
        tolerance_mm=args.tolerance,
        hash_compare_enabled=hash_compare_enabled,
        known_hashes=known_hashes,
        hide=not args.delete,
    )

    if report_path is not None:
        with report_path.open("w", encoding="utf-8") as file:
            json.dump(
                report,
                file,
                ensure_ascii=False,
                indent=2
            )

    if not report["matches"]:
        print("Keine Stempel gefunden. Nichts verändert.")

        if args.verbose and report_path is not None:
            print(f"Bericht: {report_path}")

        return

    if args.verbose:
        if report_path is not None:
            print(f"Bericht: {report_path}")
        else:
            print("Bericht: (nicht geschrieben, --report nicht angegeben)")

        print(f"Passende Objekte: {len(report['matches'])}")

        for match in report["matches"]:
            position = match["visual_mm"]

            line = (
                f"Seite {match['page']} | "
                f"{match['kind']} | "
                f"{match['subtype']} | "
                f"Pfad: {' -> '.join(match.get('xobject_path', []))} | "
                f"links={position['left_mm']} mm | "
                f"oben={position['top_mm']} mm | "
                f"B={position['width_mm']} mm | "
                f"H={position['height_mm']} mm"
            )

            if match.get("stream_sha256"):
                line += f" | SHA256: {match['stream_sha256']}"

                if "hash_known" in match:
                    status = "bekannt" if match["hash_known"] else "unbekannt"
                    line += f" ({status})"

            print(line)

        if remove and report["output_written"]:
            if not args.delete:
                number_xobjects = sum(
                    item.get("hidden", 0)
                    for item in report["hidden_xobjects"]
                )

                number_annotations = sum(
                    item.get("hidden", 0)
                    for item in report["hidden_annotations"]
                )

                print(
                    f"Ausgeblendete XObject-Platzierungen: {number_xobjects}"
                )
                print(f"Ausgeblendete Annotationen: {number_annotations}")
            else:
                number_xobjects = sum(
                    item.get("removed", 0)
                    for item in report["removed_xobjects"]
                )

                number_annotations = sum(
                    item.get("removed", 0)
                    for item in report["removed_annotations"]
                )

                print(
                    f"Entfernte XObject-Platzierungen: {number_xobjects}"
                )
                print(f"Entfernte Annotationen: {number_annotations}")

            print(f"Bereinigte Datei: {output_path}")


if __name__ == "__main__":
    main()
