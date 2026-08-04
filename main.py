from typing import List, Optional, Union, Dict, Any
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="API Suivi Budgétaire - Gestion Sécurisée")

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
# AUTHENTIFICATION ET CONFIGURATION DE BASE
# ============================================================
MOTS_DE_PASSE_DEFAUT = {
    "P1": "p1_resp2026",
    "P2": "p2_resp2026",
    "P3": "p3_resp2026",
    "P4": "p4_resp2026"
}

MOTS_DE_PASSE_ACTUELS = MOTS_DE_PASSE_DEFAUT.copy()
LFR_STATUS_BY_EXERCICE = { 2024: False, 2025: False, 2026: False, 2027: False }
HISTORIQUE_EXERCICES = {}
CIBLES_TRIMESTRE = {"T1": 25, "T2": 50, "T3": 75, "T4": 100}

class AdminLoginPayload(BaseModel):
    password: str

class ResponsableLoginPayload(BaseModel):
    programme_code: str
    password: str

class ChangePasswordPayload(BaseModel):
    programme_code: str
    old_password: str
    new_password: str

class ResetPasswordPayload(BaseModel):
    programme_code: str

class LfrTogglePayload(BaseModel):
    exercice: int
    lfr_active: bool

# --- ENDPOINTS AUTHENTIFICATION ---
@app.post("/api/admin/login")
def admin_login(payload: AdminLoginPayload):
    if payload.password.strip() == "admin123":
        return {"status": "success", "token": "admin-session-active", "role": "ADMIN"}
    raise HTTPException(status_code=401, detail="Mot de passe administrateur incorrect")

@app.post("/api/responsable/login")
def responsable_login(payload: ResponsableLoginPayload):
    code = payload.programme_code.upper().strip()
    pwd = payload.password.strip()
    
    conn = get_db_connection()
    pwd_attendu = MOTS_DE_PASSE_ACTUELS.get(code, f"{code.lower()}_resp2026")
    is_default = (pwd == MOTS_DE_PASSE_DEFAUT.get(code, f"{code.lower()}_resp2026"))

    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT password_hash, is_default FROM responsable_passwords WHERE programme_code = %s;", (code,))
            res = cur.fetchone()
            cur.close()
            conn.close()
            if res:
                pwd_attendu = res['password_hash']
                is_default = res['is_default']
        except Exception:
            if conn: conn.close()

    if pwd == pwd_attendu:
        return {
            "status": "success",
            "token": f"resp-session-{code}",
            "role": "RESPONSABLE",
            "programme_code": code,
            "must_change_password": is_default
        }
    
    raise HTTPException(status_code=401, detail="Mot de passe incorrect")

@app.post("/api/responsable/change-password")
def change_password(payload: ChangePasswordPayload):
    code = payload.programme_code.upper().strip()
    old_pwd = payload.old_password.strip()
    new_pwd = payload.new_password.strip()

    if len(new_pwd) < 6:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe doit contenir au moins 6 caractères")

    conn = get_db_connection()
    pwd_actuel = MOTS_DE_PASSE_ACTUELS.get(code, f"{code.lower()}_resp2026")

    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT password_hash FROM responsable_passwords WHERE programme_code = %s;", (code,))
            res = cur.fetchone()
            if res:
                pwd_actuel = res['password_hash']
            
            if old_pwd != pwd_actuel:
                cur.close(); conn.close()
                raise HTTPException(status_code=401, detail="Ancien mot de passe incorrect")

            cur.execute("""
                INSERT INTO responsable_passwords (programme_code, password_hash, is_default)
                VALUES (%s, %s, FALSE)
                ON CONFLICT (programme_code) DO UPDATE 
                SET password_hash = EXCLUDED.password_hash, is_default = FALSE;
            """, (code, new_pwd))
            conn.commit()
            cur.close()
            conn.close()
            MOTS_DE_PASSE_ACTUELS[code] = new_pwd
            return {"status": "success", "message": "Mot de passe modifié avec succès"}
        except HTTPException:
            raise
        except Exception:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail="Erreur lors du changement de mot de passe")

    if old_pwd != pwd_actuel:
        raise HTTPException(status_code=401, detail="Ancien mot de passe incorrect")

    MOTS_DE_PASSE_ACTUELS[code] = new_pwd
    return {"status": "success", "message": "Mot de passe modifié avec succès"}

@app.post("/api/admin/reset-password")
def reset_password(payload: ResetPasswordPayload):
    code = payload.programme_code.upper().strip()
    pwd_defaut = MOTS_DE_PASSE_DEFAUT.get(code, f"{code.lower()}_resp2026")

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO responsable_passwords (programme_code, password_hash, is_default)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (programme_code) DO UPDATE 
                SET password_hash = EXCLUDED.password_hash, is_default = TRUE;
            """, (code, pwd_defaut))
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            if conn: conn.rollback(); conn.close()

    MOTS_DE_PASSE_ACTUELS[code] = pwd_defaut
    return {
        "status": "success", 
        "message": f"Mot de passe de {code} réinitialisé avec succès", 
        "default_password": pwd_defaut
    }

# ============================================================
# ENDPOINTS IMPORTATION DES DONNÉES (STRUCTURE & VENTILATION)
# ============================================================

@app.post("/api/admin/import-base")
def import_base_donnees(payload: Any = Body(...), exercice: int = 2026):
    conn = get_db_connection()
    items = payload if isinstance(payload, list) else [payload]

    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS programmes (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(50) UNIQUE NOT NULL,
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
            """)

            for item in items:
                code = str(item.get("code") or item.get("programme_code") or "PROG").strip()
                nom = str(item.get("nom") or item.get("label") or f"Programme {code}").strip()
                
                cur.execute("""
                    INSERT INTO programmes (code, nom) VALUES (%s, %s)
                    ON CONFLICT (code) DO UPDATE SET nom = EXCLUDED.nom
                    RETURNING id;
                """, (code, nom))
                prog_id = cur.fetchone()['id']

                if "lignes" in item and isinstance(item["lignes"], list):
                    cur.execute("DELETE FROM lignes_budgetaires WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for ligne in item["lignes"]:
                        cur.execute("""
                            INSERT INTO lignes_budgetaires (programme_id, exercice, label, lfi, ajustement_lfr)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, ligne.get("label", "Ligne"), ligne.get("lfi", 0), ligne.get("ajustement_lfr", 0)))

                if "indicateurs" in item and isinstance(item["indicateurs"], list):
                    cur.execute("DELETE FROM indicateurs WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for ind in item["indicateurs"]:
                        cur.execute("""
                            INSERT INTO indicateurs (programme_id, exercice, nom, unite, cible_annuelle)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, ind.get("nom", "Indicateur"), ind.get("unite", "%"), ind.get("cible", 0)))

            conn.commit()
            cur.close()
            conn.close()
            return {"status": "success", "message": f"Structure {exercice} enregistrée dans PostgreSQL."}
        
        except Exception as e:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail=f"Erreur d'importation : {str(e)}")

    HISTORIQUE_EXERCICES[exercice] = items
    return {"status": "success", "message": f"Structure {exercice} enregistrée en mémoire de secours."}

@app.post("/api/admin/import-ventilation")
def import_ventilation(payload: Any = Body(...), exercice: int = 2026):
    conn = get_db_connection()
    items = payload if isinstance(payload, list) else [payload]

    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
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

            for item in items:
                code_prog = str(item.get("programme_code") or item.get("code") or "").strip()
                cur.execute("SELECT id FROM programmes WHERE code = %s;", (code_prog,))
                res = cur.fetchone()
                if not res:
                    continue
                prog_id = res['id']

                ventilation_list = item.get("ventilation", [])
                cur.execute("DELETE FROM ventilation_economique WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                
                for vent in ventilation_list:
                    cur.execute("""
                        INSERT INTO ventilation_economique (programme_id, exercice, nature_economique, dotation_lfi, ajustement_lfr)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (prog_id, exercice, vent.get("nature", "Dépense"), vent.get("dotation_lfi", 0), vent.get("ajustement_lfr", 0)))

            conn.commit()
            cur.close()
            conn.close()
            return {"status": "success", "message": f"Ventilation {exercice} enregistrée dans PostgreSQL."}
            
        except Exception as e:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail=f"Erreur d'importation ventilation : {str(e)}")

    return {"status": "success", "message": f"Ventilation {exercice} enregistrée en mémoire de secours."}

# ============================================================
# ENDPOINTS CONSULTATION ET RAPPORTS
# ============================================================

def _fetch_programmes_data(exercice: int = 2026, trimestre: str = "T2"):
    conn = get_db_connection()
    lfr_status = LFR_STATUS_BY_EXERCICE.get(exercice, False)

    if not conn:
        data = HISTORIQUE_EXERCICES.get(exercice, [])
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

@app.get("/api/ventilation")
def get_ventilation(exercice: int = 2026, programme_id: Optional[int] = None):
    conn = get_db_connection()
    if not conn:
        return []

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

@app.get("/api/risques")
def get_risques(exercice: int = 2026, trimestre: str = "T2"):
    return {
        "exercice": exercice,
        "trimestre": trimestre,
        "cible_trimestre": CIBLES_TRIMESTRE.get(trimestre, 50),
        "nb_critique": 0,
        "nb_alerte": 0,
        "montant_total_a_risque": 0,
        "lignes": []
    }