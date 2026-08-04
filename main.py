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
# MODÈLES & AUTHENTIFICATION
# ============================================================
MOTS_DE_PASSE_DEFAUT = {
    "P1": "p1_resp2026",
    "P2": "p2_resp2026",
    "P3": "p3_resp2026",
    "P4": "p4_resp2026"
}

# Stockage dynamique des mots de passe en mémoire si DB non connectée
MOTS_DE_PASSE_ACTUELS = MOTS_DE_PASSE_DEFAUT.copy()

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

# --- Initialisation de la table des mots de passe DB ---
def init_auth_table():
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS responsable_passwords (
                    programme_code VARCHAR(20) PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    is_default BOOLEAN DEFAULT TRUE
                );
            """)
            for code, pwd in MOTS_DE_PASSE_DEFAUT.items():
                cur.execute("""
                    INSERT INTO responsable_passwords (programme_code, password_hash, is_default)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (programme_code) DO NOTHING;
                """, (code, pwd))
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            if conn: conn.rollback(); conn.close()

init_auth_table()

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
        except Exception as e:
            if conn: conn.rollback(); conn.close()
            raise HTTPException(status_code=500, detail="Erreur lors du changement de mot de passe")

    if old_pwd != pwd_actuel:
        raise HTTPException(status_code=401, detail="Ancien mot de passe incorrect")

    MOTS_DE_PASSE_ACTUELS[code] = new_pwd
    return {"status": "success", "message": "Mot de passe modifié avec succès (mémoire)"}

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