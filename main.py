import os
from typing import List, Optional, Union, Dict, Any
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="API Suivi Budgétaire - Gestion Sécurisée")

# Activation du CORS pour autoriser l'accès depuis le frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Récupération de l'URL de la base de données PostgreSQL de Render
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        print("⚠️ Aucune variable DATABASE_URL détectée dans l'environnement.")
        return None
        
    try:
        url = DATABASE_URL
        # 1. Correction du préfixe postgres:// exigé par les versions récentes de psycopg2 / SQLAlchemy
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
            
        # 2. Sécurisation de la connexion SSL requise par Render
        if "sslmode" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}sslmode=require"
            
        return psycopg2.connect(url, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"❌ Erreur de connexion à PostgreSQL Render : {e}")
        return None

# ============================================================
# CONFIGURATIONS ET MOTS DE PASSE PAR DÉFAUT
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

# --- MODÈLES PYDANTIC ---
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

class ToggleLfrPayload(BaseModel):
    exercice: int
    lfr_active: bool

# ============================================================
# AUTHENTIFICATION
# ============================================================
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS responsable_passwords (
                    programme_code VARCHAR(50) PRIMARY KEY,
                    password_hash VARCHAR(255) NOT NULL,
                    is_default BOOLEAN DEFAULT TRUE
                );
            """)
            conn.commit()
            
            cur.execute("SELECT password_hash, is_default FROM responsable_passwords WHERE programme_code = %s;", (code,))
            res = cur.fetchone()
            cur.close()
            conn.close()
            if res:
                pwd_attendu = res['password_hash']
                is_default = res['is_default']
        except Exception as e:
            print(f"Erreur vérification mot de passe DB : {e}")
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
        except Exception as e:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail=f"Erreur changement mot de passe : {str(e)}")

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
        except Exception as e:
            if conn: conn.rollback(); conn.close()

    MOTS_DE_PASSE_ACTUELS[code] = pwd_defaut
    return {
        "status": "success", 
        "message": f"Mot de passe de {code} réinitialisé avec succès", 
        "default_password": pwd_defaut
    }

@app.post("/api/admin/toggle-lfr")
def toggle_lfr(payload: ToggleLfrPayload):
    LFR_STATUS_BY_EXERCICE[payload.exercice] = payload.lfr_active
    return {"status": "success", "exercice": payload.exercice, "lfr_active": payload.lfr_active}

# ============================================================
# ENDPOINTS IMPORTATION & ENREGISTREMENT POSTGRESQL
# ============================================================

@app.post("/api/admin/import-base")
def import_base_donnees(payload: Any = Body(...), exercice: int = 2026):
    conn = get_db_connection()
    items = payload if isinstance(payload, list) else [payload]
    HISTORIQUE_EXERCICES[exercice] = items

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
                code = str(item.get("code") or item.get("programme_code") or item.get("Code") or "P1").strip()
                nom = str(item.get("nom") or item.get("programme_nom") or item.get("label") or f"Programme {code}").strip()
                
                cur.execute("""
                    INSERT INTO programmes (code, nom) VALUES (%s, %s)
                    ON CONFLICT (code) DO UPDATE SET nom = EXCLUDED.nom
                    RETURNING id;
                """, (code, nom))
                prog_id = cur.fetchone()['id']

                lignes = item.get("lignes") or []
                if isinstance(lignes, list) and len(lignes) > 0:
                    cur.execute("DELETE FROM lignes_budgetaires WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for ligne in lignes:
                        lbl = ligne.get("label") or ligne.get("ligne_label") or "Ligne Budgétaire"
                        lfi_val = float(ligne.get("lfi") or ligne.get("dotation_lfi") or 0)
                        lfr_val = float(ligne.get("ajustement_lfr") or 0)
                        cur.execute("""
                            INSERT INTO lignes_budgetaires (programme_id, exercice, label, lfi, ajustement_lfr)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, lbl, lfi_val, lfr_val))

                indicateurs = item.get("indicateurs") or item.get("icps") or []
                if isinstance(indicateurs, list) and len(indicateurs) > 0:
                    cur.execute("DELETE FROM indicateurs WHERE programme_id = %s AND exercice = %s;", (prog_id, exercice))
                    for ind in indicateurs:
                        i_nom = ind.get("nom") or ind.get("icp_nom") or "Indicateur"
                        i_unite = ind.get("unite") or "%"
                        i_cible = float(ind.get("cible") or ind.get("cible_annuelle") or 0)
                        cur.execute("""
                            INSERT INTO indicateurs (programme_id, exercice, nom, unite, cible_annuelle)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (prog_id, exercice, i_nom, i_unite, i_cible))

            conn.commit()
            cur.close()
            conn.close()
            return {"status": "success", "message": f"Structure {exercice} enregistrée dans PostgreSQL !"}
        
        except Exception as e:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail=f"Erreur PostgreSQL : {str(e)}")

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
                    nature = vent.get("nature") or vent.get("nature_economique") or "Dépense"
                    lfi = float(vent.get("dotation_lfi") or vent.get("dotation") or 0)
                    lfr = float(vent.get("ajustement_lfr") or 0)
                    cur.execute("""
                        INSERT INTO ventilation_economique (programme_id, exercice, nature_economique, dotation_lfi, ajustement_lfr)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (prog_id, exercice, nature, lfi, lfr))

            conn.commit()
            cur.close()
            conn.close()
            return {"status": "success", "message": f"Ventilation {exercice} enregistrée dans PostgreSQL !"}
            
        except Exception as e:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail=f"Erreur PostgreSQL : {str(e)}")

    return {"status": "success", "message": f"Ventilation {exercice} enregistrée en mémoire de secours."}

# ============================================================
# CONSULTATION & COLLECTE
# ============================================================

@app.get("/api/programmes")
def get_programmes(exercice: int = 2026, trimestre: str = "T2"):
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
    except Exception as e:
        if conn: conn.close()
        return HISTORIQUE_EXERCICES.get(exercice, [])

@app.get("/api/ventilation")
def get_ventilation(exercice: int = 2026, programme_id: Optional[int] = None):
    conn = get_db_connection()
    natures_default = [
        "Dépenses de personnel",
        "Dépenses de fonctionnement",
        "Transferts courants",
        "Investissements exécutés par l'État"
    ]
    
    if not conn:
        return [{"nature": n, "dotation_lfi": 0, "ajustement_lfr": 0, "engagements": 0, "paiements": 0} for n in natures_default]

    try:
        cur = conn.cursor()
        query = """
            SELECT nature_economique AS nature, 
                   COALESCE(SUM(dotation_lfi), 0) AS dotation_lfi,
                   COALESCE(SUM(ajustement_lfr), 0) AS ajustement_lfr, 
                   COALESCE(SUM(engagements), 0) AS engagements, 
                   COALESCE(SUM(paiements), 0) AS paiements
            FROM ventilation_economique 
            WHERE (exercice = %s OR exercice IS NULL)
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

        if not results:
            return [{"nature": n, "dotation_lfi": 0, "ajustement_lfr": 0, "engagements": 0, "paiements": 0} for n in natures_default]

        return results

    except Exception:
        if conn: conn.close()
        return [{"nature": n, "dotation_lfi": 0, "ajustement_lfr": 0, "engagements": 0, "paiements": 0} for n in natures_default]

@app.post("/api/collecte")
def enregistrer_collecte(payload: Dict[str, Any] = Body(...)):
    conn = get_db_connection()
    if not conn:
        return {"status": "success", "message": "Enregistré en mémoire temporaire"}

    try:
        cur = conn.cursor()
        prog_id = payload.get("programme_id")
        exercice = payload.get("exercice", 2026)

        for l in payload.get("lignes", []):
            if "id" in l:
                cur.execute("""
                    UPDATE lignes_budgetaires 
                    SET engagements = %s, paiements = %s 
                    WHERE id = %s AND exercice = %s;
                """, (l.get("engagements", 0), l.get("paiements", 0), l["id"], exercice))

        for ic in payload.get("icps", []):
            if "id" in ic:
                cur.execute("""
                    UPDATE indicateurs 
                    SET realise = %s 
                    WHERE id = %s AND exercice = %s;
                """, (ic.get("realise", 0), ic["id"], exercice))

        conn.commit()
        cur.close()
        conn.close()
        return {"status": "success", "message": "Déclaration enregistrée dans PostgreSQL !"}
    except Exception as e:
        if conn: conn.rollback(); conn.close()
        raise HTTPException(status_code=500, detail=f"Erreur d'enregistrement : {str(e)}")

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

@app.get("/api/db-test")
def db_test():
    if not DATABASE_URL:
        return {"status": "erreur", "detail": "La variable d'environnement DATABASE_URL est introuvable."}
    
    try:
        conn = get_db_connection()
        if conn is None:
            return {"status": "erreur", "detail": "Impossible d'établir la connexion (connexion renvoie None)."}
        
        cur = conn.cursor()
        cur.execute("SELECT version();")
        version = cur.fetchone()
        cur.close()
        conn.close()
        
        return {
            "status": "succès",
            "message": "Connexion PostgreSQL réussie !",
            "version_postgres": version
        }
    except Exception as e:
        return {
            "status": "erreur",
            "exception": str(e)
        }