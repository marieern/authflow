"""
fiori_core.py
=============
Core logic used by app.py (the web UI) and console_app_lookup.py. Handles:
  - fetching + caching Fiori Apps Library data for one App ID
  - inferring App Type / Maintain-vs-Display
  - inferring the SAP Module (2-letter code, e.g. MM/SD/FI) from the
    Business Catalog's Line-of-Business prefix or, failing that, keywords
  - a task-oriented free-text suggestion (e.g. "MRP_MAT_COVERAGE"), shared
    by app.py's generated Role Description AND console_app_lookup.py's
    PFCG/BC free-text suggestions, so both stay in sync
  - detecting shared Semantic Object+Action (a real signal of shared
    target mapping / tile dependency) across whatever apps you've loaded
    in the current session
  - a heuristic (editable) "possibly critical" suggestion
  - generating PFCG single-role name/description and Business
    Catalog/Space/Page name/description from configurable templates

IMPORTANT — verify before trusting in bulk:
This environment has no network access, so none of the API field-name
assumptions below have been tested against a live response. Run the app,
fetch one known App ID, and check the "Raw fields returned" panel to
confirm the field names match FIELD_CANDIDATES. Adjust the lists if not.

The Module inference (LOB_TO_MODULE / MODULE_KEYWORDS) and the free-text
suggestion (suggest_free_text / ABBREVIATIONS) are both best-effort
heuristics, not an official SAP classification — treat their output as a
suggestion to confirm, not a final answer. That's why Module gets flagged
in "Flags/Notes" whenever it was auto-inferred rather than typed in.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

CACHE_FILE = Path(__file__).parent / "fiori_app_cache.json"
BASE_URL = "https://fioriappslibrary.hana.ondemand.com/sap/fix/externalViewer/services/SingleApp.xsodata"

# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def load_cache() -> Dict[str, Any]:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARNUNG: Cache-Datei '{CACHE_FILE}' konnte nicht gelesen "
                  f"werden ({e}). Es wird ohne Cache fortgefahren.")
            return {}
    return {}


def save_cache(cache: Dict[str, Any]) -> None:
    """Best-effort write. Caching is an optimization, not a requirement, so a
    failure here (e.g. Windows file-locking issues) must never crash the
    fetch — it just means the next run re-fetches instead of using the cache."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)
    except OSError as e:
        print(f"WARNUNG: Cache-Datei '{CACHE_FILE}' konnte nicht geschrieben "
              f"werden ({e}). Die Ergebnisse dieses Laufs werden nicht "
              f"zwischengespeichert, das ist aber unkritisch.")


# --------------------------------------------------------------------------
# Matrix import helpers
# --------------------------------------------------------------------------

def normalize_matrix_rows(source_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse imported rows to one row per App ID and combine end users."""
    aliases = {
        "app_id": {
            "appid", "applicationid", "fioriappid", "fioriapp", "application", "app",
            "anwendungsid", "applikationsid", "transaktionappid", "transaktion", "transaction",
        },
        "end_users": {"enduser", "endusers", "user", "users", "endbenutzer", "benutzer"},
        "business_user": {"businessuser", "businessusers", "businessrole", "usergroup", "geschaeftsbenutzer", "geschaftsbenutzer"},
        "pcg_role": {"pcgrole", "pfcgrole", "pcgrolepfcg", "pfcgrolepfcg", "masterrolepfcg", "role"},
        "master_role": {
            "masterrole", "masterrolepfcg", "pfcgmasterrole", "rollenname", "rollennamepfcg",
        },
        "derived_role": {
            "derivedrole", "derivedrolepfcg", "pfcgderivedrole", "abgeleiteterolle",
            "abgeleiteterollepfcg",
        },
        "business_catalog": {
            "businesscatalog", "businesscataloge", "businesscatalogname", "businesscatalogename",
            "custombusinesscatalog", "catalog", "customcatalog",
        },
        "space": {"space", "spacename"},
        "page": {"page", "pagename"},
    }
    normalized = {}
    order = []

    def key_for(label: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(label or "").strip().lower())

    def split_values(value: Any) -> List[str]:
        return [part.strip() for part in re.split(r"[,;\n]+", str(value or "")) if part.strip()]

    for source in source_rows:
        fields = {key_for(key): value for key, value in source.items()}
        app_id = next((str(fields[name]).strip() for name in aliases["app_id"]
                       if fields.get(name) not in (None, "")), "")
        if not app_id:
            continue
        dedupe_key = app_id.casefold()
        if dedupe_key not in normalized:
            normalized[dedupe_key] = {
                "App ID": app_id, "End User(s)": "", "Business User": "", "PCG Role (PFCG)": "",
                "Master Role (PFCG)": "", "Derived Role (PFCG)": "",
                "Custom Business Catalog": "", "Space": "", "Page": "",
            }
            order.append(dedupe_key)
        target = normalized[dedupe_key]
        for field, output_name in (
            ("end_users", "End User(s)"),
            ("business_user", "Business User"),
            ("pcg_role", "PCG Role (PFCG)"),
            ("master_role", "Master Role (PFCG)"),
            ("derived_role", "Derived Role (PFCG)"),
            ("business_catalog", "Custom Business Catalog"),
            ("space", "Space"),
            ("page", "Page"),
        ):
            values = []
            for alias in aliases[field]:
                if fields.get(alias) not in (None, ""):
                    values.extend(split_values(fields[alias]))
            existing = split_values(target[output_name])
            target[output_name] = "; ".join(dict.fromkeys(existing + values))
        for key, value in source.items():
            field_name = str(key).strip()
            if value in (None, "") or field_name == "App ID":
                continue
            if field_name not in target:
                target[field_name] = value
            elif str(target[field_name]) != str(value) and field_name not in {
                "End User(s)", "Business User", "PCG Role (PFCG)"
            }:
                target[field_name] = "; ".join(dict.fromkeys(
                    split_values(target[field_name]) + split_values(value)
                ))

    return [normalized[key] for key in order]


def is_app_id_header(label: Any) -> bool:
    """Return whether a spreadsheet label plausibly identifies an App ID column."""
    compact = re.sub(r"[^a-z0-9]", "", str(label or "").strip().lower())
    return compact in {
        "appid", "applicationid", "fioriappid", "fioriapp", "application", "app",
        "anwendungsid", "applikationsid", "transaktionappid", "transaktion", "transaction",
    }


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def _clean_key(key: str) -> str:
    return key.strip()


def fetch_app_info_raw(app_id: str) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/InputFilterParam(InpFilterValue='{app_id}')/Results"
    response = requests.get(url, headers={"Accept": "application/json"}, timeout=30)
    response.raise_for_status()
    data = response.json()
    results = data.get("d", {}).get("results", [])
    return [{_clean_key(k): v for k, v in row.items() if not k.startswith("__")} for row in results]


def get_app_info(app_id: str, refresh: bool = False, cache: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], str]:
    """Returns (raw_results, status). status is 'ok' | 'error: ...' | 'cached'."""
    owns_cache = cache is None
    if owns_cache:
        cache = load_cache()

    # WICHTIG: nur einen Cache-Treffer vertrauen, der auch tatsaechlich
    # Ergebnisse enthaelt. Ein fruehere Abruf koennte mit HTTP 200 aber 0
    # Zeilen zurueckgekommen sein (z.B. durch einen kurzen Aussetzer/Rate-
    # Limit bei der Bibliothek) und waere sonst als "status: ok" dauerhaft
    # als leeres Ergebnis zwischengespeichert geblieben - jeder weitere
    # Aufruf haette dann IMMER dieses leere Ergebnis zurueckgegeben, ohne
    # es je erneut zu versuchen.
    if (
        not refresh
        and app_id in cache
        and cache[app_id].get("status") == "ok"
        and cache[app_id].get("results")
    ):
        return cache[app_id]["results"], "cached"

    try:
        results = fetch_app_info_raw(app_id)
        cache[app_id] = {"results": results, "status": "ok"}
        status = "ok"
    except requests.exceptions.RequestException as e:
        cache[app_id] = {"results": [], "status": f"error: {e}"}
        results, status = [], f"error: {e}"
    except (KeyError, json.JSONDecodeError) as e:
        cache[app_id] = {"results": [], "status": f"parse_error: {e}"}
        results, status = [], f"parse_error: {e}"

    if owns_cache:
        save_cache(cache)

    return results, status


# --------------------------------------------------------------------------
# Field extraction — candidate key lists (UNVERIFIED, see module docstring)
# --------------------------------------------------------------------------

FIELD_CANDIDATES = {
    "app_title": ["AppNameAll", "AppName", "AppLauncherTitleCombined"],
    "app_type": ["AppType", "ApplicationType", "FioriAppType"],
    "business_catalog": ["BusinessCatalog"],
    "business_catalog_descr": ["BusinessCatalogDescr", "BusinessCatalogDescription", "BusinessCatalogText"],
    "role_name": ["RoleName", "BusinessRoleNameCombined"],
    "role_description": ["RoleDescription"],
    "release_group": ["releaseGroupText"],
    "semantic_object": ["SemanticObject", "SemObject"],
    "semantic_action": ["SemanticAction", "SemAction"],
    "related_apps": ["RelatedApps", "RelatedApp", "TargetMapping", "NavigationTargetMapping"],
}


def _first_present(row: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    for key in candidates:
        if row.get(key):
            return row[key]
    return None


def _collect_all(raw_results: List[Dict[str, Any]], field_key: str) -> List[str]:
    vals = set()
    for r in raw_results:
        v = _first_present(r, FIELD_CANDIDATES[field_key])
        if v:
            vals.add(str(v))
    return sorted(vals)


# --------------------------------------------------------------------------
# Inference: App Type -> Maintain / Display
# --------------------------------------------------------------------------

DISPLAY_TYPES = {"analytical", "factsheet", "fact sheet", "overview page"}
MAINTAIN_TYPES = {"transactional"}


def infer_maintain_display(app_types: List[str]) -> Tuple[str, str]:
    """Returns (activity_label, flag). activity_label in {'Maintain','Display','Unconfirmed'}."""
    if not app_types:
        return "Unconfirmed", "No App Type returned — set Maintain/Display manually"

    lowered = {t.lower() for t in app_types}
    if lowered & MAINTAIN_TYPES:
        return "Maintain", "OK — inferred from App Type = Transactional"
    if lowered & DISPLAY_TYPES:
        return "Display", "OK — inferred from App Type"
    return "Unconfirmed", f"Unrecognized App Type value(s): {', '.join(app_types)} — set manually"


# --------------------------------------------------------------------------
# Inference: SAP Module (2-letter code) from Business Catalog LoB / keywords
# --------------------------------------------------------------------------

# SAP Fiori Business Catalogs are usually named "SAP_<LOB>_BC_...", where
# <LOB> is a Line-of-Business code (S/4HANA concept). This maps the LoB
# codes seen in practice to the classic 2-letter module code that most
# role-naming conventions still use (e.g. PRC -> MM, per SAP_PRC_BC_...).
# UNVERIFIED beyond the examples actually observed (PRC, SCM) — extend this
# dict as you encounter more Business Catalog prefixes in real data.
LOB_TO_MODULE = {
    "FIN": "FI", "R2R": "FI", "A2R": "FI",
    "SLS": "SD", "O2C": "SD",
    "PRC": "MM", "P2P": "MM", "SRC": "MM",
    "MFG": "PP", "PPL": "PP",
    "SCM": "PP",
    "QLM": "QM",
    "EAM": "PM",
    "HCM": "HR",
    "PSV": "PS", "PPM": "PS",
    "LOG": "LE", "WHS": "WM",
    "TRM": "TR",
}

# Fallback keyword heuristic when no recognizable Business Catalog LoB is
# available — matched (case-insensitive) against App Title + Role Template
# + Role Description. UNVERIFIED / heuristic, not an official SAP
# classification — extend the keyword lists as needed for your app mix.
MODULE_KEYWORDS = {
    "MM": ["purchase", "purchasing", "procurement", "vendor", "supplier",
           "material", "materials", "inventory", "goods receipt",
           "goods issue", "stock", "requisition"],
    "SD": ["sales", "customer", "delivery", "shipping", "billing",
           "sales order", "quotation", "returns"],
    "FI": ["accounting", "ledger", "invoice", "payment", "accounts payable",
           "accounts receivable", "financial", "asset accounting", "bank",
           "tax"],
    "CO": ["cost center", "profit center", "controlling", "cost accounting",
           "internal order", "profitability"],
    "PP": ["production", "mrp", "bill of material", "bom", "work order",
           "routing", "capacity planning", "shop floor", "production planner"],
    "QM": ["quality", "inspection", "quality notification"],
    "PM": ["maintenance", "equipment", "maintenance order", "breakdown"],
    "HR": ["employee", "personnel", "payroll", "recruiting", "workforce",
           "leave request"],
    "WM": ["warehouse", "bin", "putaway", "picking"],
    "PS": ["project", "wbs", "milestone"],
    "TR": ["treasury", "cash management", "bank statement"],
}


def infer_module(app_title: Optional[str], role_names: List[str], role_descrs: List[str],
                  business_catalogs: List[str]) -> Tuple[str, str]:
    """Returns (module_code, note). module_code is '' if nothing could be
    confidently inferred — the caller should then leave it blank for manual
    entry rather than guessing.

    Strategy (in order):
    1. Parse Business Catalog IDs shaped like SAP_<LOB>_BC_... and map the
       LOB code via LOB_TO_MODULE. This is the most reliable signal, since
       it comes from Fiori's own classification rather than guessing from
       free text.
    2. If that yields nothing (no catalog returned, or an unrecognized LOB
       code), fall back to scoring MODULE_KEYWORDS against App Title +
       Role Template + Role Description combined.
    3. If still nothing, return ('', ...) so it's flagged for manual entry.
    """
    lob_matches = []
    for catalog in business_catalogs:
        parts = catalog.split("_")
        if len(parts) >= 2 and parts[0].upper() == "SAP":
            lob = parts[1].upper()
            if lob in LOB_TO_MODULE:
                lob_matches.append(LOB_TO_MODULE[lob])

    if lob_matches:
        module = max(set(lob_matches), key=lob_matches.count)
        return module, "Auto-inferred from Business Catalog Line-of-Business code — CONFIRM"

    text = " ".join(filter(None, [app_title] + list(role_names) + list(role_descrs))).lower()
    scores = {}
    for module, keywords in MODULE_KEYWORDS.items():
        hits = [kw for kw in keywords if kw in text]
        if hits:
            scores[module] = len(hits)

    if scores:
        best_module = max(scores, key=scores.get)
        return best_module, "Auto-inferred (heuristic keyword match, no usable Business Catalog) — CONFIRM"

    return "", "Could not auto-infer Module — set manually"


# --------------------------------------------------------------------------
# Task-oriented free-text suggestion (shared by app.py's generated Role
# Description AND console_app_lookup.py's PFCG/BC free-text suggestions -
# lives here once so both stay in sync instead of drifting apart).
# --------------------------------------------------------------------------

FREE_TEXT_MAX_LEN = 16

# Aktions-/CRUD-Verben, die den TASK nicht beschreiben (der wird schon durch
# "Maintain/Display" abgedeckt) - werden aus dem Freitext entfernt.
ACTION_VERBS = {
    "MANAGE", "MAINTAIN", "DISPLAY", "CREATE", "EDIT", "VIEW", "MONITOR",
    "PROCESS", "POST", "REVIEW", "APPROVE", "CHECK", "ANALYZE", "PLAN",
    "SCHEDULE", "RUN", "EXECUTE", "CONFIGURE", "DEFINE", "ASSIGN", "TRACK",
    "SHOW", "LIST", "SEARCH", "FIND", "RELEASE", "CONFIRM", "CANCEL",
}

# Reine Fuellwoerter ohne inhaltlichen Wert - werden komplett entfernt
# (im Gegensatz zu GENERIC_FILLER_WORDS, die noch als letzte Wahl infrage
# kommen, wenn sonst nichts anderes mehr reinpasst).
STOPWORDS = {
    "OF", "THE", "FOR", "AND", "TO", "IN", "ON", "BY", "WITH", "A", "AN",
    "PER", "AS", "FROM", "AT", "IS", "ARE", "VERSION", "VERSIONS",
}

# Generische Fuellwoerter, die kaum etwas ueber den Task aussagen - werden bei
# der Auswahl zuletzt beruecksichtigt (fliegen zuerst raus, wenn's eng wird).
GENERIC_FILLER_WORDS = {
    "OVERVIEW", "DATA", "DETAILS", "DETAIL", "INFORMATION", "INFO",
    "ITEMS", "ITEM", "ENTRIES", "ENTRY", "ALL", "APP", "OBJECT", "OBJECTS",
    "VALUES", "VALUE", "TYPE", "TYPES", "ISSUES", "ISSUE",
}

# Kurze, aber trotzdem hoch aussagekraeftige SAP-Fachbegriffe/Akronyme -
# werden wie ein ABBREVIATIONS-Treffer behandelt, obwohl sie < 5 Zeichen
# sind (z.B. "MRP" ist keine generische Abkuerzung, sondern DER Fachbegriff).
RECOGNIZED_SHORT_TERMS = {
    "MRP", "BOM", "ATP", "PO", "SO", "GR", "GI", "FI", "CO", "HR", "SD",
    "MM", "WM", "EWM", "APO", "PP", "QM", "PM", "EAM", "GL", "AR", "AP",
    "CRM", "SRM",
}

# Haeufige lange Begriffe -> gaengige SAP-Kurzform. Bei Bedarf erweitern -
# das ist eine Heuristik, keine offizielle SAP-Abkuerzungsliste.
ABBREVIATIONS = {
    "MANAGEMENT": "MGMNT", "ACCOUNTING": "ACCOUNTING", "ALLOCATION": "ALLOC",
    "ALLOCATIONS": "ALLOC", "SETTLEMENT": "SETTLEMENT", "SETTLEMENTS": "SETTLEMENT",
    "PURCHASE": "PURCH", "PURCHASING": "PURCH",
    "ORDER": "ORD", "ORDERS": "ORD", "INVOICE": "INV", "INVOICES": "INV",
    "CUSTOMER": "CUST", "CUSTOMERS": "CUST", "SUPPLIER": "SUPPL",
    "SUPPLIERS": "SUPPL", "VENDOR": "VEND", "VENDORS": "VEND",
    "FINANCIAL": "FIN", "FINANCE": "FIN", "MATERIAL": "MAT",
    "MATERIALS": "MAT", "DOCUMENT": "DOC", "DOCUMENTS": "DOC",
    "DELIVERY": "DELIV", "DELIVERIES": "DELIV", "PRODUCTION": "PROD",
    "PROCUREMENT": "PROCURE", "REQUISITION": "REQ", "REQUISITIONS": "REQ",
    "PAYMENT": "PYMT", "PAYMENTS": "PYMT", "REPORT": "RPT", "REPORTS": "RPT",
    "ANALYTICS": "ANLYT", "MONITORING": "MON", "PLANNING": "PLNG",
    "SCHEDULING": "SCHED", "WAREHOUSE": "WH", "INVENTORY": "INV",
    "TRANSPORTATION": "TRANSP", "PROJECT": "PROJ", "EMPLOYEE": "EMP",
    "EMPLOYEES": "EMP", "APPROVAL": "APPR", "APPROVALS": "APPR",
    "GENERAL": "GEN", "COST": "COST", "CENTER": "CTR", "CENTERS": "CTR",
}


def _task_words(text: str) -> List[Tuple[str, str]]:
    """Zerlegt einen Text in Woerter (Grossbuchstaben, unterstrichgetrennt),
    entfernt Aktionsverben, reine Zahlen (z.B. aus "Version 2") und
    Stopwoerter, und wendet die ABBREVIATIONS-Kurzformen an. Gibt eine Liste
    von (original_wort, abgekuerztes_wort) in der urspruenglichen
    Lesereihenfolge zurueck."""
    slug = _slug(text or "")
    words = [w for w in slug.split("_") if w]
    filtered = [
        w for w in words
        if w not in ACTION_VERBS and w not in STOPWORDS and not w.isdigit()
    ]
    if not filtered:
        # Falls der ganze Text nur aus Verben/Fuellwoertern/Zahlen bestand,
        # lieber die Originalwoerter behalten als nichts zurueckzugeben.
        filtered = [w for w in words if not w.isdigit()] or words
    return [(w, ABBREVIATIONS.get(w, w)) for w in filtered]


def _word_priority(original_word: str, abbreviated_word: str) -> int:
    """Bewertet, wie aussagekraeftig ein Wort fuer den Task vermutlich ist.
    Hoeher = wichtiger = wird bevorzugt in den Freitext aufgenommen.

    2 = bekannter Fachbegriff (in ABBREVIATIONS oder RECOGNIZED_SHORT_TERMS,
        z.B. "MRP" - kurz, aber hoch spezifisch)
    1 = unbekanntes, aber vermutlich spezifisches Wort (>= 5 Zeichen, z.B.
        "Inbound", "Outbound", Produktnamen, ...)
    0 = generisches Fuellwort oder sehr kurzes unbekanntes Wort
    """
    if original_word in GENERIC_FILLER_WORDS:
        return 0
    if original_word in ABBREVIATIONS or original_word in RECOGNIZED_SHORT_TERMS:
        return 2
    if len(abbreviated_word) >= 5:
        return 1
    return 0


def suggest_free_text(*sources, max_len: int = FREE_TEXT_MAX_LEN) -> str:
    """Findet einen Freitext-Vorschlag, der beschreibt, WORUM es bei der App
    geht (nicht nur irgendwelche Woerter von links nach rechts).

    Jede "source" kann entweder ein einzelner String sein, oder eine Liste
    von Strings (z.B. mehrere Titel-Varianten derselben App-ID aus der Fiori
    Library - MD06 hat z.B. 10 verschiedene Tile-Titel fuer dieselbe App-ID).
    Die erste nicht-leere Quelle wird verwendet.

    Bei mehreren Titel-Varianten wird gezaehlt, in wie vielen Varianten ein
    Wort vorkommt (pro Variante nur einmal gezaehlt). Woerter, die sich ueber
    mehrere Varianten wiederholen (z.B. "Material", "Coverage", "MRP" bei
    MD06), beschreiben das eigentliche, gemeinsame Thema der App besser als
    Woerter, die nur in einer einzigen Titel-Variante auftauchen.

    Auswahl-Reihenfolge: Haeufigkeit ueber die Varianten > Fachbegriff-
    Prioritaet (_word_priority) > Wortlaenge. Ausgewaehlt wird, was in
    max_len Zeichen passt; Ergebnis wird in der urspruenglichen
    Lesereihenfolge zusammengesetzt (z.B. "MRP_MAT_COVERAGE").

    Das ist eine SUGGESTION, kein Endergebnis - immer pruefen/anpassen.
    """
    for source in sources:
        variants = [source] if isinstance(source, str) else list(source or [])
        variants = [v for v in variants if v]
        if not variants:
            continue

        freq: Dict[str, int] = {}
        first_seen_pos: Dict[str, int] = {}
        original_of: Dict[str, str] = {}
        position_counter = 0

        for variant in variants:
            pairs = _task_words(variant)
            seen_in_variant = set()
            for original, abbr in pairs:
                if abbr not in seen_in_variant:
                    seen_in_variant.add(abbr)
                    freq[abbr] = freq.get(abbr, 0) + 1
                if abbr not in first_seen_pos:
                    first_seen_pos[abbr] = position_counter
                    original_of[abbr] = original
                position_counter += 1

        if not freq:
            continue

        def score(abbr: str) -> int:
            base = _word_priority(original_of[abbr], abbr)
            repetition_bonus = min(freq[abbr] - 1, 3)
            return base + repetition_bonus

        ranked = sorted(
            freq.keys(),
            key=lambda a: (-score(a), -len(a), first_seen_pos[a]),
        )

        selected = []
        total_len = 0
        for abbr in ranked:
            added_len = len(abbr) + (1 if selected else 0)
            if total_len + added_len <= max_len:
                selected.append(abbr)
                total_len += added_len

        if selected:
            selected.sort(key=lambda a: first_seen_pos[a])
            return "_".join(selected)

        best = ranked[0]
        return best[:max_len].rstrip("_")
    return ""


def suggest_function_group(group_rows: List[Dict[str, Any]]) -> str:
    """Ask Gemini for a concise work-area label, with a local fallback."""
    descriptions = "\n".join(
        str(row.get("App Title") or row.get("SAP Role Template") or "").strip()
        for row in group_rows
    )
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key and descriptions:
        try:
            response = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                params={"key": api_key},
                json={"contents": [{"parts": [{"text":
                    "Create one concise SAP function-group description for these app descriptions. "
                    "Return only 2-3 uppercase words joined with underscores, maximum 16 characters.\n"
                    + descriptions
                }]}]},
                timeout=10,
            )
            response.raise_for_status()
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            candidate = re.sub(r"[^A-Z0-9_]", "", text.upper().strip()).strip("_")
            if candidate and len(candidate) <= FREE_TEXT_MAX_LEN:
                return candidate
        except (requests.RequestException, KeyError, TypeError, ValueError):
            pass

    source_text = " ".join(descriptions.splitlines())
    source_lower = source_text.lower()
    if "workflow" in source_lower and any(word in source_lower for word in ("purchase", "purchasing", "procurement")):
        return "WORKFLOW_PURCH"
    return suggest_free_text(source_text) or "SHARED_PROCESS"


def synchronize_shared_user_groups(rows: List[Dict[str, Any]], templates: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Give apps used by the same users one function group and master role."""
    groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for row in rows:
        end_users = [value.strip() for value in re.split(r"[,;\n]+", str(row.get("End User(s)") or "")) if value.strip()]
        users = tuple(sorted({re.sub(r"\s+", " ", value).casefold() for value in end_users}))
        if users:
            groups.setdefault(users, []).append(row)

    used_function_groups = set()
    used_master_roles = set()
    for group_rows in groups.values():
        has_descriptions = any(row.get("App Title") or row.get("SAP Role Template") for row in group_rows)
        if has_descriptions:
            function_group = suggest_function_group(group_rows)
        else:
            function_group = next((str(row.get("Function Group") or "").strip() for row in group_rows
                                   if str(row.get("Function Group") or "").strip()), "")
        function_group_conflict = function_group.casefold() in used_function_groups
        if function_group_conflict:
            regenerated = suggest_function_group(group_rows)
            if regenerated.casefold() != function_group.casefold():
                function_group = regenerated
        base_function_group = function_group
        suffix = 2
        while function_group.casefold() in used_function_groups:
            function_group = f"{base_function_group}_{suffix}"
            suffix += 1
        used_function_groups.add(function_group.casefold())
        source = group_rows[0]
        generated = generate_names(
            str(source.get("Module") or ""), function_group, str(source.get("Persona") or ""),
            str(source.get("App Title") or ""), str(source.get("Maintain/Display") or "Unconfirmed"),
            templates or {}, str(source.get("Derivation") or ""),
        )
        master_role = generated["master_role_name"]
        used_master_roles.add(master_role.casefold())
        for row in group_rows:
            row["Function Group"] = function_group
            row_derivation = str(row.get("Derivation") or "").strip()
            row["Derivation"] = row_derivation
            row_generated = generate_names(
                str(row.get("Module") or ""), function_group, str(row.get("Persona") or ""),
                str(row.get("App Title") or ""), str(row.get("Maintain/Display") or "Unconfirmed"),
                templates or {}, row_derivation,
            )
            if master_role:
                row["Master Role (PFCG)"] = master_role
                row["PCG Role (PFCG)"] = master_role
                row["Derived Role (PFCG)"] = row_generated["derived_role_name"] if row_derivation else ""
                row["Role Description (generated)"] = row_generated["role_description"]
                row["Custom Business Catalog"] = row_generated["catalog_name"]
                row["Custom Catalog Description"] = row_generated["catalog_description"]
                row["Space"] = row_generated["space_name"]
                row["Space Description"] = row_generated["space_description"]
                row["Page"] = row_generated["page_name"]
                row["Page Description"] = row_generated["page_description"]
    return rows


# --------------------------------------------------------------------------
# Target mapping / tile dependency — cross-reference across loaded apps
# --------------------------------------------------------------------------

def find_shared_target_mapping(app_id: str, semantic_object: Optional[str], semantic_action: Optional[str],
                                all_apps_index: Dict[str, Dict[str, Any]]) -> str:
    """all_apps_index: {app_id: {'semantic_object':..., 'semantic_action':...}, ...}
    for every app loaded in the current session (including this one)."""
    if not semantic_object or not semantic_action:
        return "Unconfirmed — no Semantic Object/Action returned; check the app's Configuration tab in the Fiori Library manually"

    matches = [
        other_id for other_id, info in all_apps_index.items()
        if other_id != app_id
        and info.get("semantic_object") == semantic_object
        and info.get("semantic_action") == semantic_action
    ]
    if matches:
        return f"Shares target mapping ({semantic_object}-{semantic_action}) with: {', '.join(sorted(matches))}"
    return "No shared target mapping found among loaded apps"


# --------------------------------------------------------------------------
# Critical-app heuristic (suggestion only — always user-editable)
# --------------------------------------------------------------------------

_CRITICAL_KEYWORDS = [
    "delete", "reverse", "cancel", "mass", "release", "post", "approve",
    "write-off", "write off", "close period", "reclassify",
]


def suggest_critical(app_title: str, role_description: str, activity_label: str) -> str:
    text = f"{app_title or ''} {role_description or ''}".lower()
    hits = [kw for kw in _CRITICAL_KEYWORDS if kw in text]
    if activity_label == "Maintain" and hits:
        return f"Possibly critical (heuristic — keywords: {', '.join(hits)}) — CONFIRM"
    return "Not flagged (heuristic) — CONFIRM"


# --------------------------------------------------------------------------
# Naming/description generation — fully template-driven
# names["master_role_name"]
# --------------------------------------------------------------------------

DEFAULT_TEMPLATES = {
    "activity_code_maintain": "M",
    "activity_code_display": "D",
    "activity_code_unconfirmed": "X",
    "master_role_name": "T:0000_{module}_{activity_code}_{function_descr}",
    "derived_role_name": "T:{derivation}_{module}_{activity_code}_{function_descr}",
    "role_description": "{module}-{derivation}-{descr}-{activity_word}",
    "catalog_name": "T_BC_{module}_{function_descr}",
    "catalog_description": "{descr}",
    "space_name": "Z_SP_{module}_{function_descr}",
    "space_description": "{descr}",
    "page_name": "Z_PG_{module}_{function_descr}",
    "page_description": "{module} - {function_group} Page",
}

MODULE_LONG_NAMES = {
    "FI": "Finance", "CO": "Controlling", "MM": "Materials Management",
    "SD": "Sales and Distribution", "PP": "Production Planning",
    "QM": "Quality Management", "PM": "Plant Maintenance", "HR": "Human Resources",
    "WM": "Warehouse Management", "PS": "Project System", "TR": "Treasury",
    "LE": "Logistics Execution",
}
FUNCTION_GROUP_LONG_WORDS = {
    "ORD": "Order", "PURCH": "Purchasing", "PROC": "Procurement", "INV": "Inventory",
    "MAT": "Material", "MGMT": "Management", "DELIV": "Delivery", "PROD": "Production",
    "SCHED": "Scheduling",
}


def _slug(value: str) -> str:
    value = (value or "").strip().upper()
    value = re.sub(r"[^A-Z0-9]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_")


def _long_function_group(value: str) -> str:
    return " ".join(FUNCTION_GROUP_LONG_WORDS.get(word.upper(), word.capitalize())
                     for word in re.split(r"[_\s-]+", value) if word)


def generate_names(module: str, function_group: str, persona: str, app_title: str,
                    activity_label: str, templates: Dict[str, str], derivation: str = "") -> Dict[str, str]:
    t = {**DEFAULT_TEMPLATES, **(templates or {})}

    activity_code = {
        "Maintain": t["activity_code_maintain"],
        "Display": t["activity_code_display"],
    }.get(activity_label, t["activity_code_unconfirmed"])

    ctx = {
        "module": _slug(module) or "MOD",
        "function_group": _slug(function_group) or "ERROR",
        "function_descr": _slug(function_group) or "ERROR",
        "persona": _slug(persona) or "PERSONA",
        "derivation": _slug(derivation) or "ROOT",
        "activity_code": activity_code,
        "activity_word": activity_label,
        "app_title": app_title or "",
    }
    description_ctx = {**ctx, "module": MODULE_LONG_NAMES.get(ctx["module"], ctx["module"]),
                       "descr": _long_function_group(ctx["function_group"])}

    def fmt(key: str) -> str:
        try:
            return t[key].format(**(description_ctx if key in {
                "role_description", "catalog_description", "space_description",
            } else ctx))
        except (KeyError, IndexError):
            return t[key]

    return {
        "master_role_name": fmt("master_role_name"),
        "derived_role_name": fmt("derived_role_name"),
        "role_description": fmt("role_description"),
        "catalog_name": fmt("catalog_name"),
        "catalog_description": fmt("catalog_description"),
        "space_name": fmt("space_name"),
        "space_description": fmt("space_description"),
        "page_name": fmt("page_name"),
        "page_description": fmt("page_description"),
    }


# --------------------------------------------------------------------------
# Row builder — combines everything into one flat dict for the UI/export
# --------------------------------------------------------------------------

def build_row(app_id: str, raw_results: List[Dict[str, Any]], status: str,
              module: str, function_group: str, persona: str,
              all_apps_index: Dict[str, Dict[str, Any]], templates: Dict[str, str],
              end_users: str = "", business_user: str = "", pcg_role: str = "",
              derivation: str = "") -> Dict[str, Any]:

    app_titles = _collect_all(raw_results, "app_title")
    app_types = _collect_all(raw_results, "app_type")
    catalogs = _collect_all(raw_results, "business_catalog")
    catalog_descrs = _collect_all(raw_results, "business_catalog_descr")
    role_names = _collect_all(raw_results, "role_name")
    role_descrs = _collect_all(raw_results, "role_description")

    semantic_object = raw_results[0].get("SemanticObject") if raw_results else None
    semantic_action = raw_results[0].get("SemanticAction") if raw_results else None
    # fall back to candidate keys if exact key missing
    if raw_results and not semantic_object:
        semantic_object = _first_present(raw_results[0], FIELD_CANDIDATES["semantic_object"])
    if raw_results and not semantic_action:
        semantic_action = _first_present(raw_results[0], FIELD_CANDIDATES["semantic_action"])

    activity_label, activity_flag = infer_maintain_display(app_types)
    app_title = "; ".join(app_titles) if app_titles else None  # full list, for the "App Title" column
    # short, task-oriented abbreviation for the GENERATED role description
    # (same style/logic as the PFCG/BC free-text suggestions, e.g.
    # "MRP_MAT_COVERAGE") — falls back to role_names if no title returned
    title_free_text = suggest_free_text(app_titles, role_names)
    role_descr = "; ".join(role_descrs) if role_descrs else None

    # Module: a manually-typed value (from the UI form) always wins and is
    # used as-is. Only auto-infer when the user left it blank.
    module_note = None
    if module and module.strip():
        module_value = module.strip()
    else:
        module_value, module_note = infer_module(app_title, role_names, role_descrs, catalogs)

    target_mapping_note = find_shared_target_mapping(app_id, semantic_object, semantic_action, all_apps_index)
    critical_suggestion = suggest_critical(app_title, role_descr, activity_label)

    derivation_value = derivation.strip()
    names = generate_names(module_value, function_group, persona, app_title or "", activity_label, templates, derivation_value)

    flags = []
    if status not in ("ok", "cached"):
        flags.append(f"FETCH ERROR: {status}")
    if not raw_results:
        flags.append("No data returned for this App ID")
    flags.append(activity_flag)
    if module_note:
        flags.append(f"Module: {module_note}")
    if len(catalogs) > 1:
        flags.append(f"{len(catalogs)} business catalogs returned — confirm which applies")

    return {
        "App ID": app_id,
        "Status": status,
        "App Title": app_title,
        "App Type (raw)": "; ".join(app_types) if app_types else None,
        "Maintain/Display": activity_label,
        "Semantic Object": semantic_object,
        "Semantic Action": semantic_action,
        "Target Mapping Note": target_mapping_note,
        "SAP Business Catalog": "; ".join(catalogs) if catalogs else None,
        "SAP Business Catalog Descr": "; ".join(catalog_descrs) if catalog_descrs else None,
        "SAP Role Template": "; ".join(role_names) if role_names else None,
        "SAP Role Description": role_descr,
        "Module": module_value,
        "Function Group": function_group,
        "Persona": persona,
        "Derivation": derivation_value,
        "End User(s)": end_users,
        "Business User": business_user,
        "Critical (suggested)": critical_suggestion,
        "Critical (confirmed)": "",
        "Master Role (PFCG)": pcg_role or names["master_role_name"],
        "PCG Role (PFCG)": pcg_role or names["master_role_name"],
        "Derived Role (PFCG)": names["derived_role_name"] if derivation_value else "",
        "Role Description (generated)": names["role_description"],
        "Custom Business Catalog": names["catalog_name"],
        "Custom Catalog Description": names["catalog_description"],
        "Space": names["space_name"],
        "Space Description": names["space_description"],
        "Page": names["page_name"],
        "Page Description": names["page_description"],
        "Flags/Notes": "; ".join(flags) if flags else "OK",
    }
