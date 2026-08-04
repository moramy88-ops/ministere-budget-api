from typing import List, Optional, Union, Dict, Any
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
import io
from datetime import datetime

app = FastAPI(title="API Suivi Budgétaire - Gestion Contrôlée de la LFR")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_CONFIG = {
    "dbname": "budget_db",
    "user": "postgres",
    "password": "zougalB613",
    "host": "localhost",
    "port": "5432"
}

def get_db_connection():
    try:
        return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    except Exception:
        return None

# ============================================================
# MODÈLES PYDANTIC
# ============================================================
class LigneBudgetaire(BaseModel):
    id: Optional[int] = None
    label: str
    lfi: float
    ajustement_lfr: float = 0
    engagements: float = 0
    paiements: float = 0

class ExecutionIcp(BaseModel):
    id: Optional[int] = None
    nom: Optional[str] = None
    unite: Optional[str] = "%"
    cible: Optional[float] = 0
    realise: float = 0

class ExecutionVentilation(BaseModel):
    nature: str
    dotation_lfi: float
    ajustement_lfr: float = 0
    engagements: float = 0
    paiements: float = 0

class SaisieTrimestriellePayload(BaseModel):
    programme_id: int
    exercice: int = 2026
    trimestre: str
    lignes: List[LigneBudgetaire]
    icps: List[ExecutionIcp]
    ventilation: Optional[List[ExecutionVentilation]] = []

class LfrTogglePayload(BaseModel):
    exercice: int
    lfr_active: bool

class AdminLoginPayload(BaseModel):
    password: str

LFR_STATUS_BY_EXERCICE = { 2024: False, 2025: False, 2026: False, 2027: False }
HISTORIQUE_EXERCICES = {}
CIBLES_TRIMESTRE = {"T1": 25, "T2": 50, "T3": 75, "T4": 100}

SEUIL_ALERTE = 5
SEUIL_CRITIQUE = 15

# ============================================================
# ENDPOINTS ADMINISTRATION & CONFIGURATION
# ============================================================

@app.get("/api/config-lfr")
def get_config_lfr(exercice: int = 2026):
    return {"exercice": exercice, "lfr_active": LFR_STATUS_BY_EXERCICE.get(exercice, False)}

@app.post("/api/admin/toggle-lfr")
def toggle_lfr(payload: LfrTogglePayload):
    LFR_STATUS_BY_EXERCICE[payload.exercice] = payload.lfr_active
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config_exercice (
                    exercice INT PRIMARY KEY,
                    lfr_active BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                INSERT INTO config_exercice (exercice, lfr_active) 
                VALUES (%s, %s)
                ON CONFLICT (exercice) DO UPDATE SET lfr_active = EXCLUDED.lfr_active;
            """, (payload.exercice, payload.lfr_active))
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            if conn: conn.rollback(); conn.close()
            
    return {"status": "success", "exercice": payload.exercice, "lfr_active": payload.lfr_active}

@app.post("/api/admin/login")
def admin_login(payload: AdminLoginPayload):
    if payload.password == "admin123":
        return {"status": "success", "token": "admin-session-active"}
    raise HTTPException(status_code=401, detail="Mot de passe administrateur incorrect")

# Endpoint d'importation globale (Correction de l'erreur 404)
@app.post("/api/admin/import-base")
def import_base_donnees(payload: List[Dict[str, Any]] = Body(...), exercice: int = 2026):
    """
    Importe les programmes, leurs lignes budgétaires et leurs indicateurs
    pour un exercice donné.
    """
    conn = get_db_connection()
    
    if conn:
        try:
            cur = conn.cursor()
            
            # Création automatique des tables si nécessaires
            cur.execute("""
                CREATE TABLE IF NOT EXISTS programmes (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(10) UNIQUE NOT NULL,
                    nom VARCHAR(255) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lignes_budgetaires (
                    id SERIAL PRIMARY KEY,
                    programme_id INT REFERENCES programmes(id) ON DELETE CASCADE,
                    exercice INT DEFAULT 2026,
                    label TEXT NOT NULL,
                    lfi NUMERIC DEFAULT 0,
                    ajustement_lfr NUMERIC DEFAULT 0,
                    engagements NUMERIC DEFAULT 0,
                    paiements NUMERIC DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS indicateurs (
                    id SERIAL PRIMARY KEY,
                    programme_id INT REFERENCES programmes(id) ON DELETE CASCADE,
                    exercice INT DEFAULT 2026,
                    nom TEXT NOT NULL,
                    unite VARCHAR(50) DEFAULT '%',
                    cible_annuelle NUMERIC DEFAULT 0,
                    realise NUMERIC DEFAULT 0,
                    inverse BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS ventilation_economique (
                    id SERIAL PRIMARY KEY,
                    programme_id INT REFERENCES programmes(id) ON DELETE CASCADE,
                    exercice INT DEFAULT 2026,
                    nature_economique VARCHAR(255) NOT NULL,
                    dotation_lfi NUMERIC DEFAULT 0,
                    ajustement_lfr NUMERIC DEFAULT 0,
                    engagements NUMERIC DEFAULT 0,
                    paiements NUMERIC DEFAULT 0
                );
            """)

            for item in payload:
                code = item.get("code")
                nom = item.get("nom", f"Programme {code}")
                
                # Insertion ou récupération du programme
                cur.execute("""
                    INSERT INTO programmes (code, nom) VALUES (%s, %s)
                    ON CONFLICT (code) DO UPDATE SET nom = EXCLUDED.nom
                    RETURNING id;
                """, (code, nom))
                prog_id = cur.fetchone()['id']

                # Traitement des Lignes Budgétaires
                if "lignes" in item and isinstance(item["lignes"], list):
                    cur.execute("DELETE FROM lignes_budgetaires WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for ligne in item["lignes"]:
                        cur.execute("""
                            INSERT INTO lignes_budgetaires (programme_id, exercice, label, lfi, ajustement_lfr)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, ligne.get("label"), ligne.get("lfi", 0), ligne.get("ajustement_lfr", 0)))

                # Traitement des Indicateurs
                if "indicateurs" in item and isinstance(item["indicateurs"], list):
                    cur.execute("DELETE FROM indicateurs WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for ind in item["indicateurs"]:
                        cur.execute("""
                            INSERT INTO indicateurs (programme_id, exercice, nom, unite, cible_annuelle)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, ind.get("nom"), ind.get("unite", "%"), ind.get("cible", 0)))

                # Traitement de la Ventilation Économique (si présente)
                if "ventilation" in item and isinstance(item["ventilation"], list):
                    cur.execute("DELETE FROM ventilation_economique WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for vent in item["ventilation"]:
                        cur.execute("""
                            INSERT INTO ventilation_economique (programme_id, exercice, nature_economique, dotation_lfi, ajustement_lfr)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, vent.get("nature"), vent.get("dotation_lfi", 0), vent.get("ajustement_lfr", 0)))

            conn.commit()
            cur.close()
            conn.close()
            return {"status": "success", "message": f"Données de l'exercice {exercice} importées avec succès dans la base PostgreSQL."}
        
        except Exception as e:
            if conn:
                conn.rollback()
                conn.close()
            raise HTTPException(status_code=500, detail=f"Erreur lors de l'enregistrement PostgreSQL : {str(e)}")

    # Mode secours (Mémoire) si la BDD n'est pas connectée
    parsed_programmes = []
    for idx, item in enumerate(payload, start=1):
        parsed_programmes.append({
            "id": idx,
            "code": item.get("code"),
            "nom": item.get("nom"),
            "t1": 0, "t2": 0, "t3": 0, "t4": 0,
            "lignes": [
                {
                    "id": 1000 + i,
                    "label": l.get("label"),
                    "lfi": l.get("lfi", 0),
                    "ajustement_lfr": l.get("ajustement_lfr", 0),
                    "engagements": 0,
                    "paiements": 0
                } for i, l in enumerate(item.get("lignes", []))
            ],
            "indicateurs": [
                {
                    "id": 2000 + i,
                    "nom": ind.get("nom"),
                    "unite": ind.get("unite", "%"),
                    "cible": ind.get("cible", 0),
                    "realise": 0,
                    "inverse": False
                } for i, ind in enumerate(item.get("indicateurs", []))
            ],
            "ventilation": [
                {
                    "nature": v.get("nature"),
                    "dotation_lfi": v.get("dotation_lfi", 0),
                    "ajustement_lfr": v.get("ajustement_lfr", 0),
                    "engagements": 0,
                    "paiements": 0
                } for i, v in enumerate(item.get("ventilation", []))
            ]
        })
    
    HISTORIQUE_EXERCICES[exercice] = parsed_programmes
    return {"status": "success", "message": f"Données de l'exercice {exercice} importées en mémoire de secours."}

# ============================================================
# CHARGEMENT DES PROGRAMMES
# ============================================================

def _fetch_programmes_data(exercice: int = 2026, trimestre: str = "T2"):
    conn = get_db_connection()
    lfr_status = LFR_STATUS_BY_EXERCICE.get(exercice, False)

    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT lfr_active FROM config_exercice WHERE exercice = %s;", (exercice,))
            res = cur.fetchone()
            if res:
                lfr_status = res['lfr_active']
                LFR_STATUS_BY_EXERCICE[exercice] = lfr_status
            cur.close()
        except Exception:
            pass

    if not conn:
        if exercice not in HISTORIQUE_EXERCICES:
            HISTORIQUE_EXERCICES[exercice] = [
                {
                    "id": 1, "code": "P1", "nom": "Pilotage, Coordination et Gestion administrative", "t1": 10.0, "t2": 21.0, "t3": 0, "t4": 0,
                    "lignes": [
                        {"id": 101, "label": "Organiser le Pèlerinage aux Lieux saints de l'Islam", "lfi": 1205687000, "ajustement_lfr": 0, "engagements": 300000000, "paiements": 210000000}
                    ],
                    "indicateurs": [{"id": 201, "nom": "Niveau de satisfaction des pèlerins de la Mecque", "unite": "%", "cible": 87, "realise": 80, "inverse": False}],
                    "ventilation": [{"nature": "Dépenses de personnel", "dotation_lfi": 1731712570, "ajustement_lfr": 0, "engagements": 300000000, "paiements": 210000000}]
                },
                {
                    "id": 2, "code": "P2", "nom": "Coopération bilatérale et multilatérale", "t1": 8.5, "t2": 18.5, "t3": 0, "t4": 0,
                    "lignes": [
                        {"id": 103, "label": "Tenir des commissions mixtes", "lfi": 175603000, "ajustement_lfr": 0, "engagements": 50000000, "paiements": 30000000}
                    ],
                    "indicateurs": [{"id": 202, "nom": "Pourcentage de commissions mixtes réalisées", "unite": "%", "cible": 56, "realise": 30, "inverse": False}],
                    "ventilation": [{"nature": "Dépenses de fonctionnement", "dotation_lfi": 30067370806, "ajustement_lfr": 0, "engagements": 1000000000, "paiements": 500000000}]
                }
            ]
        
        data = HISTORIQUE_EXERCICES[exercice]
        for p in data: p['lfr_active'] = lfr_status
        return data

    try:
        cur = conn.cursor()
        cur.execute("SELECT id, code, nom FROM programmes ORDER BY code;")
        programmes = cur.fetchall()

        for p in programmes:
            p['lfr_active'] = lfr_status
            cur.execute("""
                SELECT id, label, COALESCE(lfi, 0) AS lfi, COALESCE(ajustement_lfr, 0) AS ajustement_lfr,
                       COALESCE(engagements, 0) AS engagements, COALESCE(paiements, 0) AS paiements 
                FROM lignes_budgetaires WHERE programme_id = %s AND (exercice = %s OR exercice IS NULL) ORDER BY id;
            """, (p['id'], exercice))
            p['lignes'] = cur.fetchall()

            cur.execute("""
                SELECT id, nom, unite, cible_annuelle AS cible, COALESCE(realise, 0) AS realise, inverse 
                FROM indicateurs WHERE programme_id = %s AND (exercice = %s OR exercice IS NULL) ORDER BY id;
            """, (p['id'], exercice))
            p['indicateurs'] = cur.fetchall()

            cur.execute("""
                SELECT nature_economique AS nature, COALESCE(dotation_lfi, 0) AS dotation_lfi,
                       COALESCE(ajustement_lfr, 0) AS ajustement_lfr, COALESCE(engagements, 0) AS engagements, COALESCE(paiements, 0) AS paiements 
                FROM ventilation_economique WHERE programme_id = %s AND (exercice = %s OR exercice IS NULL);
            """, (p['id'], exercice))
            p['ventilation'] = cur.fetchall()

        cur.close()
        conn.close()
        return programmes
    except Exception:
        if conn: conn.close()
        return HISTORIQUE_EXERCICES.get(exercice, [])

@app.get("/api/programmes")
def get_programmes(exercice: int = 2026, trimestre: str = "T2"):
    return _fetch_programmes_data(exercice, trimestre)

# ============================================================
# CALCUL ET ENDPOINT DES RISQUES
# ============================================================
def _taux_et_assiette(ligne: dict, lfr_active: bool):
    lfi = float(ligne.get("lfi", 0) or 0)
    lfr = float(ligne.get("ajustement_lfr", 0) or 0) if lfr_active else 0.0
    assiette = lfi + lfr
    paiements = float(ligne.get("paiements", 0) or 0)
    taux = (paiements / assiette * 100) if assiette > 0 else 0.0
    return taux, assiette

def _niveau_risque(taux: float, cible: float):
    ecart = taux - cible
    if ecart <= -SEUIL_CRITIQUE: return "Critique"
    if ecart <= -SEUIL_ALERTE: return "Alerte"
    return "Normal"

def _calculer_risques(exercice: int, trimestre: str):
    cible = CIBLES_TRIMESTRE.get(trimestre, 50)
    programmes = _fetch_programmes_data(exercice, trimestre)
    lfr_active = LFR_STATUS_BY_EXERCICE.get(exercice, False)

    lignes_risque = []
    for p in programmes:
        nom_prog = p.get("nom", "Programme")
        for l in (p.get("lignes") or []):
            taux, assiette = _taux_et_assiette(l, lfr_active)
            if assiette <= 0: continue
            niveau = _niveau_risque(taux, cible)
            if niveau == "Normal": continue
            montant_a_risque = assiette - float(l.get("paiements", 0) or 0)
            lignes_risque.append({
                "programme": nom_prog,
                "ligne_id": l.get("id"),
                "label": l.get("label"),
                "montant_prevu": round(assiette, 2),
                "montant_execute": round(float(l.get("paiements", 0) or 0), 2),
                "montant_a_risque": round(max(montant_a_risque, 0), 2),
                "taux_execution": round(taux, 1),
                "cible": cible,
                "ecart": round(taux - cible, 1),
                "niveau_risque": niveau,
            })

    ordre = {"Critique": 0, "Alerte": 1}
    lignes_risque.sort(key=lambda x: (ordre.get(x["niveau_risque"], 2), -x["montant_a_risque"]))
    return lignes_risque

@app.get("/api/risques")
def get_risques(exercice: int = 2026, trimestre: str = "T2"):
    lignes = _calculer_risques(exercice, trimestre)
    return {
        "exercice": exercice,
        "trimestre": trimestre,
        "cible_trimestre": CIBLES_TRIMESTRE.get(trimestre, 50),
        "nb_critique": sum(1 for l in lignes if l["niveau_risque"] == "Critique"),
        "nb_alerte": sum(1 for l in lignes if l["niveau_risque"] == "Alerte"),
        "montant_total_a_risque": round(sum(l["montant_a_risque"] for l in lignes), 2),
        "lignes": lignes,
    }

@app.get("/api/ventilation")
def get_ventilation(exercice: int = 2026, programme_id: Optional[int] = None):
    conn = get_db_connection()
    if not conn:
        return [
            {"nature": "Dépenses de personnel", "dotation_lfi": 1400000000, "ajustement_lfr": 50000000, "engagements": 500000000, "paiements": 321000000},
            {"nature": "Dépenses de fonctionnement", "dotation_lfi": 1700000000, "ajustement_lfr": -20000000, "engagements": 480000000, "paiements": 302000000},
            {"nature": "Transferts courants", "dotation_lfi": 600000000, "ajustement_lfr": 0, "engagements": 150000000, "paiements": 111000000},
            {"nature": "Investissements exécutés par l'État", "dotation_lfi": 3000000000, "ajustement_lfr": 200000000, "engagements": 1200000000, "paiements": 900000000}
        ]

    try:
        cur = conn.cursor()
        query = """
            SELECT nature_economique AS nature, SUM(COALESCE(dotation_lfi, 0)) AS dotation_lfi,
                   SUM(COALESCE(ajustement_lfr, 0)) AS ajustement_lfr, SUM(engagements) AS engagements, SUM(paiements) AS paiements
            FROM ventilation_economique WHERE (exercice = %s OR exercice IS NULL)
        """
        params = [exercice]
        if programme_id:
            query += " AND programme_id = %s"
            params.append(programme_id)
            
        query += " GROUP BY nature_economique;"
        cur.execute(query, tuple(params))
        results = cur.fetchall()
        cur.close()
        conn.close()
        return results if results else []
    except Exception:
        if conn: conn.close()
        return []

@app.post("/api/collecte")
def enregistrer_declaration(payload: SaisieTrimestriellePayload):
    ex = payload.exercice
    if ex in HISTORIQUE_EXERCICES:
        for prog in HISTORIQUE_EXERCICES[ex]:
            if prog["id"] == payload.programme_id:
                for l in prog["lignes"]:
                    match = next((x for x in payload.lignes if x.id == l["id"]), None)
                    if match:
                        l["lfi"] = match.lfi
                        l["ajustement_lfr"] = match.ajustement_lfr
                        l["engagements"] = match.engagements
                        l["paiements"] = match.paiements

    return {"status": "success", "message": f"Saisie {payload.trimestre} - Exercice {ex} enregistrée."}