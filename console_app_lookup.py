"""
console_app_lookup.py
======================
Konsolen-Tool, das fiori_core.py (dasselbe Modul, das auch app.py/die
Web-UI nutzt) verwendet, um Infos zu einer oder mehreren Fiori-App-IDs
aus der SAP Fiori Apps Library zu holen und am Ende folgende Felder
auszugeben:

    App-ID, Rolle, App-Beschreibung, Organisationsebene, Berechtigung,
    Vorschlag PFCG-Rollen-Freitext (max. 16 Zeichen),
    Vorschlag BC-Freitext (max. 16 Zeichen)

Es wird NICHTS in Excel geschrieben - reine Konsolenausgabe.

Voraussetzung: dieses Skript liegt im selben Ordner wie fiori_core.py
(also im "fiori_webapp"-Ordner), da es "import fiori_core" nutzt.

Ausfuehren:
    pip install -r requirements.txt
    python console_app_lookup.py

Hinweis zu "Rolle" / "Berechtigung" / Freitext-Vorschlaegen:
- "Rolle": es wird zuerst gefragt, ob eine Rolle manuell eingegeben wird.
  Bleibt die Eingabe leer, wird automatisch die von der Bibliothek
  zurueckgegebene Rolle ("SAP Role Template" / role_name) uebernommen.
  Ist auch das leer, wird das entsprechend vermerkt.
- "Berechtigung" ist NICHT frei eingebbar, sondern wird - wie in
  fiori_core.infer_maintain_display() - aus dem App Type der Bibliothek
  abgeleitet (Maintain / Display / Unconfirmed).
- "Organisationsebene" liefert die Bibliothek nicht, daher weiterhin
  manuelle Eingabe (wie im vorherigen Skript).
- Die Freitext-Vorschlaege (PFCG/BC) basieren primaer auf der
  App-Beschreibung/App-Titel aus Fiori (die beschreibt den Task/Zweck der
  App), nicht auf Rollenname/Katalogbeschreibung. Aktionsverben wie
  "Manage"/"Display" werden entfernt, gaengige lange Begriffe abgekuerzt
  (z.B. Management -> MGMNT). Statt einfach von links nach rechts
  abzuschneiden, wird jedes Wort nach Aussagekraft bewertet (bekannter
  Fachbegriff > langes/spezifisches Wort > generisches Fuellwort) und die
  wichtigsten Woerter werden ausgewaehlt, die noch in 16 Zeichen passen
  (z.B. "ALLOC_SETTLEMENT", "MGMNT_ACCOUNTING"). Es wird nie mitten im
  Wort geschnitten.

WICHTIG (siehe fiori_core.py-Docstring): Die Feldnamen in
FIELD_CANDIDATES sind unverifiziert (keine Netzwerkverbindung beim
Bau dieses Tools). Vor produktivem Einsatz: eine bekannte App-ID
abrufen und die Rohdaten (siehe Ausgabe unten, Abschnitt "Rohdaten")
mit FIELD_CANDIDATES in fiori_core.py abgleichen.
"""

import fiori_core


def fetch_and_collect(app_id: str, cache: dict) -> dict:
    """Ruft die Fiori-Library-Daten fuer eine App-ID ab und stellt die vom
    Nutzer angefragten Felder zusammen (ohne Rolle/Organisationsebene, die
    werden separat in main() behandelt, da "Rolle" manuell ueberschreibbar
    ist und "Organisationsebene" nicht aus der Bibliothek kommt)."""
    raw_results, status = fiori_core.get_app_info(app_id, refresh=False, cache=cache)

    # Wichtig: status "ok"/"cached" mit LEEREM raw_results heisst, der
    # HTTP-Aufruf war erfolgreich, aber die Bibliothek hat 0 Zeilen fuer
    # diese App-ID zurueckgegeben. Das ist ein anderer Fall als ein
    # Verbindungsfehler - meist bedeutet es, dass die App-ID nicht (unter
    # diesem Namen) in der Fiori Apps Library registriert ist (z.B. ein
    # klassischer Transaktionscode ohne 1:1-Fiori-App-Eintrag).
    if status in ("ok", "cached") and not raw_results:
        status = "empty: Abruf erfolgreich, aber 0 Ergebnisse fuer diese App-ID - moeglicherweise ist die App-ID nicht in der Bibliothek registriert"

    app_titles = fiori_core._collect_all(raw_results, "app_title")
    role_names = fiori_core._collect_all(raw_results, "role_name")
    app_types = fiori_core._collect_all(raw_results, "app_type")
    catalog_descrs = fiori_core._collect_all(raw_results, "business_catalog_descr")

    # Rohwerte - fuer Freitext-Vorschlaege und die "kein manueller Wert ->
    # automatisch uebernehmen"-Logik in main(). app_titles bleibt eine Liste
    # (nicht zusammengefuegt!), da suggest_free_text mehrere Titel-Varianten
    # derselben App-ID (z.B. bei MD06) einzeln analysieren muss.
    app_titles_list = app_titles  # Liste, kann mehrere Varianten enthalten
    role_name_raw = "; ".join(role_names) if role_names else None
    catalog_descr_raw = catalog_descrs[0] if catalog_descrs else None

    activity_label, activity_flag = fiori_core.infer_maintain_display(app_types)

    return {
        "app_id": app_id,
        "status": status,
        "raw_results": raw_results,
        "app_titles_list": app_titles_list,
        "role_name_raw": role_name_raw,
        "catalog_descr_raw": catalog_descr_raw,
        "app_description": "; ".join(app_titles) if app_titles else "Nicht von der Bibliothek zurueckgegeben",
        "authorization": f"{activity_label} ({activity_flag})",
    }


def print_result(result: dict, org_level: str) -> None:
    print("\n" + "=" * 60)
    print(f"  App-ID:               {result['app_id']}")
    if result["status"] not in ("ok", "cached"):
        print(f"  ACHTUNG - Abruf-Status: {result['status']}")
    print(f"  Rolle:                {result['role']}  ({result['role_source']})")
    print(f"  App-Beschreibung:     {result['app_description']}")
    print(f"  Organisationsebene:   {org_level}")
    print(f"  Berechtigung:         {result['authorization']}")
    print(f"  Vorschlag PFCG-Freitext (max {fiori_core.FREE_TEXT_MAX_LEN} Zeichen): {result['pfcg_free_text']}")
    print(f"  Vorschlag BC-Freitext  (max {fiori_core.FREE_TEXT_MAX_LEN} Zeichen): {result['bc_free_text']}")
    print("=" * 60)


def main():
    raw_input_ids = input(
        "App-ID(s) eingeben (bei mehreren mit Komma trennen): "
    ).strip()
    app_ids = [a.strip() for a in raw_input_ids.split(",") if a.strip()]

    if not app_ids:
        print("Keine App-ID eingegeben. Abgebrochen.")
        return

    cache = fiori_core.load_cache()
    results = []

    for app_id in app_ids:
        print(f"\nRufe Daten fuer '{app_id}' aus der Fiori Apps Library ab ...")
        result = fetch_and_collect(app_id, cache)

        # Rolle: zuerst pruefen, ob manuell ein Wert eingegeben wird. Wird
        # nichts eingegeben, wird automatisch die von Fiori zurueckgegebene
        # Rolle uebernommen.
        manual_role = input(
            f"Rolle fuer '{app_id}' (Enter = automatisch aus der Fiori "
            f"Library uebernehmen): "
        ).strip()

        if manual_role:
            result["role"] = manual_role
            result["role_source"] = "manuell eingegeben"
        elif result["role_name_raw"]:
            result["role"] = result["role_name_raw"]
            result["role_source"] = "automatisch aus Fiori Library uebernommen"
        else:
            result["role"] = "Nicht angegeben und nicht von der Bibliothek zurueckgegeben"
            result["role_source"] = "nicht verfuegbar"

        # Freitext-Vorschlaege: primaer aus den App-Titel-Varianten aus
        # Fiori (die beschreiben den TASK der App - bei mehreren Varianten
        # fuer dieselbe App-ID zaehlt, welche Woerter sich wiederholen).
        # Rollenname/Katalogbeschreibung nur als Fallback, falls Fiori
        # keinen Titel liefert. WICHTIG: hier NUR echte Werte verwenden
        # (result["role"] kann ein Platzhaltertext wie "Nicht angegeben
        # und nicht von der Bibliothek zurueckgegeben" sein - der darf
        # NICHT als Freitext-Quelle verwendet werden, sonst entsteht daraus
        # Muell wie "ZURUECKGEGEBEN").
        role_for_suggestions = manual_role or result["role_name_raw"]  # kann None sein
        result["pfcg_free_text"] = fiori_core.suggest_free_text(
            result["app_titles_list"], role_for_suggestions
        ) or "(kein Vorschlag moeglich - keine Quelltexte von Fiori vorhanden)"
        result["bc_free_text"] = fiori_core.suggest_free_text(
            result["app_titles_list"], result["catalog_descr_raw"]
        ) or "(kein Vorschlag moeglich - keine Quelltexte von Fiori vorhanden)"

        org_level = input(f"Organisationsebene fuer '{app_id}': ").strip()
        results.append((result, org_level))

    fiori_core.save_cache(cache)

    print("\n\nERGEBNIS")
    for result, org_level in results:
        print_result(result, org_level)

    # Optional: Rohdaten zur Kontrolle der FIELD_CANDIDATES-Zuordnung
    show_raw = input(
        "\nRohdaten der Bibliothek anzeigen, um FIELD_CANDIDATES zu "
        "pruefen? (j/N): "
    ).strip().lower()
    if show_raw == "j":
        for result, _ in results:
            print(f"\n--- Rohdaten fuer {result['app_id']} ---")
            if result["raw_results"]:
                for row in result["raw_results"]:
                    for key, value in row.items():
                        print(f"  {key}: {value}")
                    print("  ---")
            else:
                print("  (keine Rohdaten - Abruf leer oder fehlgeschlagen)")


if __name__ == "__main__":
    main()
