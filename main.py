from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
import io
from datetime import datetime

from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml, OxmlElement

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
    id: int
    label: str
    lfi: float
    ajustement_lfr: float = 0
    engagements: float = 0
    paiements: float = 0

class ExecutionIcp(BaseModel):
    id: int
    realise: float

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

# ============================================================
# CHARGEMENT DES DONNÉES DE BASE
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
                    "id": 1, "code": "P01", "nom": "Enseignement Supérieur", "t1": 10.0, "t2": 21.0, "t3": 0, "t4": 0,
                    "lignes": [
                        {"id": 101, "label": "Construction et équipement d'écoles", "lfi": 1000000000, "ajustement_lfr": 150000000, "engagements": 300000000, "paiements": 210000000}
                    ],
                    "indicateurs": [{"id": 201, "nom": "Taux de scolarisation", "unite": "%", "cible": 95, "realise": 88, "inverse": False}],
                    "ventilation": [{"nature": "Dépenses de personnel", "dotation_lfi": 800000000, "ajustement_lfr": 50000000, "engagements": 300000000, "paiements": 210000000}]
                },
                {
                    "id": 2, "code": "P02", "nom": "Santé Publique", "t1": 8.5, "t2": 18.5, "t3": 0, "t4": 0,
                    "lignes": [
                        {"id": 103, "label": "Approvisionnement en médicaments", "lfi": 800000000, "ajustement_lfr": -50000000, "engagements": 250000000, "paiements": 148000000}
                    ],
                    "indicateurs": [{"id": 202, "nom": "Taux de couverture vaccinale", "unite": "%", "cible": 90, "realise": 82, "inverse": False}],
                    "ventilation": [{"nature": "Transferts courants", "dotation_lfi": 600000000, "ajustement_lfr": 0, "engagements": 150000000, "paiements": 111000000}]
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
                SELECT id, label, COALESCE(lfi, ouverts) AS lfi, COALESCE(ajustement_lfr, 0) AS ajustement_lfr,
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
                SELECT nature_economique AS nature, COALESCE(dotation_lfi, dotation) AS dotation_lfi,
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
            SELECT nature_economique AS nature, SUM(COALESCE(dotation_lfi, dotation)) AS dotation_lfi,
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