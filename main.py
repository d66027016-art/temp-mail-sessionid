# ============================================================
# TEMP MAIL SYSTEM - WITH CUSTOM DOMAIN OPTION
# Users can create: anything@damxd.shop
# ============================================================

import os
import json
import sqlite3
import smtplib
import imaplib
import email
import hashlib
import secrets
import threading
import time
import random
import string
import asyncio
import re
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import contextmanager
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash
import dns.resolver
import requests

# Ensure Windows console supports UTF-8 for printing emojis
try:
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================================================
# CONFIGURATION
# ============================================================

# Load environment variables from .env file
load_dotenv()

CUSTOM_DOMAIN = os.getenv("CUSTOM_DOMAIN", "damxd.shop")

def get_db_path():
    db_path = os.getenv("DB_PATH")
    if db_path:
        return db_path
    
    # Explicitly check for serverless environments (Vercel / AWS Lambda)
    is_serverless = (
        os.environ.get('VERCEL') == '1' or 
        'AWS_LAMBDA_FUNCTION_NAME' in os.environ or
        os.path.abspath(__file__).startswith('/var/task') or
        os.path.abspath(__file__).startswith('/var/lang') or
        os.environ.get('NOW_REGION') is not None
    )
    if is_serverless:
        import tempfile
        return os.path.join(tempfile.gettempdir(), "temp_mail.db")
    return "temp_mail.db"
SMTP_HOST = os.getenv("SMTP_HOST", "0.0.0.0")
SMTP_PORT = int(os.getenv("SMTP_PORT", 2525))
API_PORT = int(os.getenv("API_PORT", 5000))

# Flask Session Secret Key
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)

# ============================================================
# DATABASE SETUP & UTILITIES
# ============================================================

@contextmanager
def get_db():
    """Context manager for SQLite database connections to ensure proper closing."""
    conn = sqlite3.connect(get_db_path())
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        
        # Users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            session_id TEXT UNIQUE,
            is_premium INTEGER DEFAULT 0
        )''')
        
        # Add is_premium column to users table if not exists (for existing databases)
        c.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in c.fetchall()]
        if 'is_premium' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
        
        # Domains table
        c.execute('''CREATE TABLE IF NOT EXISTS domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT UNIQUE,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_custom INTEGER DEFAULT 0
        )''')
        
        # Insert default domain
        c.execute("INSERT OR IGNORE INTO domains (domain, is_custom) VALUES (?, 0)", (CUSTOM_DOMAIN,))
        
        # Insert custom domains from env
        custom_domains_env = os.getenv("CUSTOM_DOMAINS", "")
        if custom_domains_env:
            domains_list = [d.strip() for d in custom_domains_env.split(",") if d.strip()]
            for d in domains_list:
                if re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', d):
                    c.execute("INSERT OR IGNORE INTO domains (domain, is_custom) VALUES (?, 1)", (d,))
        
        # Inboxes table
        c.execute('''CREATE TABLE IF NOT EXISTS inboxes (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            session_id TEXT,
            email TEXT UNIQUE,
            local_part TEXT,
            domain TEXT,
            custom_domain_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            is_used INTEGER DEFAULT 0,
            is_custom INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions (session_id),
            FOREIGN KEY (custom_domain_id) REFERENCES domains (id)
        )''')
        
        # Sessions table
        c.execute('''CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )''')
        
        # Emails table
        c.execute('''CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id TEXT,
            message_id TEXT,
            sender TEXT,
            subject TEXT,
            body TEXT,
            html_body TEXT,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_read INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            attachment_count INTEGER DEFAULT 0,
            FOREIGN KEY (inbox_id) REFERENCES inboxes (id)
        )''')
        
        # Forwarding rules
        c.execute('''CREATE TABLE IF NOT EXISTS forwarding_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id TEXT,
            forward_to TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (inbox_id) REFERENCES inboxes (id)
        )''')
        
        # SMS logs
        c.execute('''CREATE TABLE IF NOT EXISTS sms_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT,
            message TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            method TEXT
        )''')
        
        # Create performance optimization indexes
        c.execute('CREATE INDEX IF NOT EXISTS idx_inboxes_session_id ON inboxes(session_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_inboxes_email ON inboxes(email)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_emails_inbox_id ON emails(inbox_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_session_id ON users(session_id)')
        
        conn.commit()
    print(f"✅ Database initialized with domain: @{CUSTOM_DOMAIN}")

# ============================================================
# SESSION MANAGER
# ============================================================

class SessionManager:
    def __init__(self):
        self.session_lifetime = 7 * 24 * 60 * 60
    
    def create_session(self, user_id=None, ip_address=None, user_agent=None):
        # Generate session_id in the format: damxd + 7 random lowercase letters/digits (total 12 chars)
        random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=7))
        session_id = f"damxd{random_suffix}"
        expires_at = datetime.now() + timedelta(seconds=self.session_lifetime)
        
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO sessions 
                   (session_id, user_id, ip_address, user_agent, expires_at) 
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, user_id, ip_address, user_agent, expires_at.isoformat())
            )
            conn.commit()
        return session_id
    
    def get_session(self, session_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND is_active = 1 AND expires_at > datetime('now')",
                (session_id,)
            )
            row = c.fetchone()
        
        if not row:
            return None
        
        self.update_session(session_id)
        
        return {
            "session_id": row[0],
            "user_id": row[1],
            "ip_address": row[2],
            "user_agent": row[3],
            "created_at": row[4],
            "last_active": row[5],
            "expires_at": row[6],
            "is_active": row[7]
        }
    
    def update_session(self, session_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE sessions SET last_active = datetime('now') WHERE session_id = ?",
                (session_id,)
            )
            conn.commit()
    
    def delete_session(self, session_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET is_active = 0 WHERE session_id = ?", (session_id,))
            conn.commit()
    
    def get_inboxes_by_session(self, session_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                """SELECT id, email, local_part, domain, created_at, expires_at, is_active, is_custom 
                   FROM inboxes WHERE session_id = ? AND is_active = 1
                   ORDER BY created_at DESC""",
                (session_id,)
            )
            rows = c.fetchall()
        
        return [{
            "id": row[0],
            "email": row[1],
            "local_part": row[2],
            "domain": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "is_active": row[6],
            "is_custom": bool(row[7])
        } for row in rows]
    
    def cleanup_expired_sessions(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE sessions SET is_active = 0 WHERE expires_at < datetime('now')"
            )
            conn.commit()

# ============================================================
# USER MANAGER
# ============================================================

class UserManager:
    def register_user(self, username, password):
        username = username.strip()
        if not username or not password:
            return {"success": False, "error": "Username and password are required."}
        
        # Check if username exists
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM users WHERE username = ?", (username,))
            if c.fetchone():
                return {"success": False, "error": "Username is already taken."}
            
            password_hash = generate_password_hash(password)
            try:
                c.execute(
                    "INSERT INTO users (username, password_hash, is_premium) VALUES (?, ?, 0)",
                    (username, password_hash)
                )
                conn.commit()
                user_id = c.lastrowid
                return {"success": True, "user_id": user_id}
            except Exception as e:
                return {"success": False, "error": f"Registration failed: {str(e)}"}

    def authenticate_user(self, username, password):
        username = username.strip()
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, username, password_hash, is_premium FROM users WHERE username = ?", (username,))
            row = c.fetchone()
            if not row:
                return None
            
            user_id, db_username, db_hash, is_premium = row
            if check_password_hash(db_hash, password):
                c.execute("UPDATE users SET last_login = datetime('now') WHERE id = ?", (user_id,))
                conn.commit()
                return {"id": user_id, "username": db_username, "is_premium": bool(is_premium)}
            return None

    def get_user_by_id(self, user_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, username, is_premium FROM users WHERE id = ?", (user_id,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "is_premium": bool(row[2])}
            return None

    def set_premium_status(self, user_id, is_premium):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET is_premium = ? WHERE id = ?", (1 if is_premium else 0, user_id))
            conn.commit()
            return True

# ============================================================
# DOMAIN MANAGER
# ============================================================

class DomainManager:
    def get_all_domains(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT domain, is_custom FROM domains WHERE active = 1")
            rows = c.fetchall()
        return [{"domain": row[0], "is_custom": bool(row[1])} for row in rows]
    
    def get_default_domain(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT domain FROM domains WHERE active = 1 AND is_custom = 0 LIMIT 1")
            row = c.fetchone()
        return row[0] if row else CUSTOM_DOMAIN
    
    def get_custom_domains(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT domain FROM domains WHERE active = 1 AND is_custom = 1")
            rows = c.fetchall()
        return [row[0] for row in rows]
    
    def add_custom_domain(self, domain):
        """Add a custom domain (must be verified)"""
        # Validate domain format
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain):
            return {"success": False, "error": "Invalid domain format"}
        
        with get_db() as conn:
            c = conn.cursor()
            
            # Check if exists
            c.execute("SELECT id FROM domains WHERE domain = ?", (domain,))
            if c.fetchone():
                return {"success": False, "error": "Domain already exists"}
            
            c.execute(
                "INSERT INTO domains (domain, is_custom) VALUES (?, 1)",
                (domain,)
            )
            conn.commit()
        return {"success": True, "domain": domain}
    
    def remove_custom_domain(self, domain):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE domains SET active = 0 WHERE domain = ? AND is_custom = 1", (domain,))
            conn.commit()
        return {"success": True}
    
    def validate_domain(self, domain):
        """Check if domain is active and can receive email"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM domains WHERE domain = ? AND active = 1", (domain,))
            row = c.fetchone()
        return row is not None

# ============================================================
# TEMP MAIL ENGINE
# ============================================================

def generate_readable_local_part():
    adjectives = [
        "happy", "lucky", "swift", "clever", "brave", "bright", "cosmic", "cyber", "digital", "epic",
        "super", "magic", "wild", "gentle", "smart", "quick", "cool", "funky", "silent", "shadow",
        "golden", "silver", "frosty", "sunny", "mighty", "hyper", "neon", "crypto", "stellar"
    ]
    nouns = [
        "panda", "koala", "tiger", "falcon", "fox", "wolf", "otter", "eagle", "hawk", "coder",
        "ninja", "geek", "user", "agent", "pilot", "wizard", "hero", "knight", "ranger", "scout",
        "pixel", "orbit", "rocket", "comet", "nebula", "matrix", "vector", "delta", "proton"
    ]
    adj = random.choice(adjectives)
    noun = random.choice(nouns)
    num = random.randint(10, 99)
    return f"{adj}-{noun}-{num}"

class TempMail:
    def __init__(self):
        self.session_manager = SessionManager()
        self.domain_manager = DomainManager()
    
    def get_available_domains(self):
        return self.domain_manager.get_all_domains()
    
    def get_default_domain(self):
        return self.domain_manager.get_default_domain()
    
    def generate_inbox(self, session_id=None, custom_local=None, custom_domain=None, username=None):
        """Generate a new temporary email inbox with custom domain support"""
        # Determine domain
        if custom_domain:
            domain = custom_domain
            # Validate domain exists
            if not self.domain_manager.validate_domain(domain):
                domain = self.domain_manager.get_default_domain()
                is_custom = 0
            else:
                is_custom = 1
        else:
            domain = self.domain_manager.get_default_domain()
            is_custom = 0
        
        with get_db() as conn:
            c = conn.cursor()
            
            # Generate local part
            if custom_local:
                local_part = custom_local.lower().strip()
                local_part = re.sub(r'[^a-zA-Z0-9._-]', '', local_part)
                
                # Check restricted names (exact match on base alphanumeric name)
                base_part = local_part.strip('._-')
                restricted_names = {
                    'admin', 'administrator', 'admins',
                    'operator', 'op', 'ops',
                    'owner', 'owners',
                    'ceo', 'cfo', 'coo', 'cto',
                    'root', 'system', 'sys', 'sysadmin',
                    'support', 'help', 'helpdesk', 'info', 'contact',
                    'webmaster', 'hostmaster', 'postmaster',
                    'staff', 'moderator', 'mod', 'mods',
                    'official', 'security', 'legal',
                    'manager', 'founder', 'president', 'director'
                }
                if base_part in restricted_names:
                    if username != 'damxd':
                        raise ValueError("This custom name is reserved and not allowed.")
                
                if len(local_part) < 3:
                    local_part = generate_readable_local_part()
                
                # Check if taken
                c.execute("SELECT id FROM inboxes WHERE email = ? AND is_active = 1", (f"{local_part}@{domain}",))
                if c.fetchone():
                    local_part = f"{local_part}{random.randint(100,999)}"
            else:
                # Generate a readable random email address
                local_part = generate_readable_local_part()
            
            email_addr = f"{local_part}@{domain}"
            inbox_id = secrets.token_hex(8)
            
            # Expires in 24 hours
            expires_at = datetime.now() + timedelta(hours=24)
            
            # If session doesn't exist, create one
            if not session_id:
                session_id = self.session_manager.create_session()
            
            c.execute(
                """INSERT INTO inboxes 
                   (id, user_id, session_id, email, local_part, domain, expires_at, is_custom) 
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
                (inbox_id, session_id, email_addr, local_part, domain, expires_at.isoformat(), is_custom)
            )
            conn.commit()
        
        return {
            "id": inbox_id,
            "email": email_addr,
            "local_part": local_part,
            "domain": domain,
            "is_custom": bool(is_custom),
            "expires_at": expires_at.isoformat(),
            "session_id": session_id,
            "web_url": f"http://localhost:{API_PORT}/inbox/{inbox_id}",
            "session_url": f"http://localhost:{API_PORT}/session/{session_id}"
        }
    
    def get_inbox(self, inbox_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM inboxes WHERE id = ? AND is_active = 1", (inbox_id,))
            row = c.fetchone()
        
        if not row:
            return None
        
        return {
            "id": row[0],
            "user_id": row[1],
            "session_id": row[2],
            "email": row[3],
            "local_part": row[4],
            "domain": row[5],
            "created_at": row[6],
            "expires_at": row[7],
            "is_active": row[8],
            "is_used": row[9],
            "is_custom": bool(row[10])
        }
    
    def get_inboxes_by_session(self, session_id):
        return self.session_manager.get_inboxes_by_session(session_id)
    
    def get_session(self, session_id):
        return self.session_manager.get_session(session_id)
    
    def get_inbox_by_email(self, email_addr):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM inboxes WHERE email = ? AND is_active = 1", (email_addr,))
            row = c.fetchone()
        return row[0] if row else None
    
    def receive_email(self, sender, recipients, subject, body, html_body=None, message_id=None):
        stored = 0
        with get_db() as conn:
            c = conn.cursor()
            for recipient in recipients:
                c.execute(
                    "SELECT id FROM inboxes WHERE email = ? AND is_active = 1",
                    (recipient,)
                )
                row = c.fetchone()
                if row:
                    inbox_id = row[0]
                    c.execute("UPDATE inboxes SET is_used = 1 WHERE id = ?", (inbox_id,))
                    c.execute(
                        """INSERT INTO emails 
                           (inbox_id, message_id, sender, subject, body, html_body)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (inbox_id, message_id, sender, subject, body, html_body)
                    )
                    stored += 1
                    
                    c.execute(
                        "SELECT forward_to FROM forwarding_rules WHERE inbox_id = ? AND is_active = 1",
                        (inbox_id,)
                    )
                    forward_row = c.fetchone()
                    if forward_row:
                        self.forward_email(forward_row[0], sender, subject, body, html_body)
            conn.commit()
        return stored
    
    def forward_email(self, to_email, sender, subject, body, html_body):
        try:
            print(f"📤 Forwarding to: {to_email}")
        except Exception as e:
            print(f"❌ Forwarding failed: {e}")
    
    def list_emails(self, inbox_id, limit=50):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                """SELECT id, sender, subject, received_at, is_read, attachment_count
                FROM emails WHERE inbox_id = ? AND is_deleted = 0
                ORDER BY received_at DESC LIMIT ?""",
                (inbox_id, limit)
            )
            rows = c.fetchall()
        
        return [{
            "id": row[0],
            "sender": row[1],
            "subject": row[2] or "(No Subject)",
            "received_at": row[3],
            "is_read": bool(row[4]),
            "attachments": row[5]
        } for row in rows]
    
    def get_email(self, email_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT sender, subject, body, html_body, received_at FROM emails WHERE id = ? AND is_deleted = 0",
                (email_id,)
            )
            row = c.fetchone()
        
        if not row:
            return None
        
        self.mark_read(email_id)
        
        return {
            "sender": row[0],
            "subject": row[1] or "(No Subject)",
            "body": row[2] or "No content",
            "html_body": row[3],
            "received_at": row[4]
        }
    
    def mark_read(self, email_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE emails SET is_read = 1 WHERE id = ?", (email_id,))
            conn.commit()
    
    def delete_email(self, email_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE emails SET is_deleted = 1 WHERE id = ?", (email_id,))
            conn.commit()
    
    def delete_inbox(self, inbox_id):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE inboxes SET is_active = 0 WHERE id = ?", (inbox_id,))
            c.execute("DELETE FROM emails WHERE inbox_id = ?", (inbox_id,))
            conn.commit()
    
    def cleanup_expired(self):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM inboxes WHERE expires_at < datetime('now') AND is_active = 1"
            )
            expired = c.fetchall()
            
            if expired:
                ids = [row[0] for row in expired]
                placeholders = ','.join(['?'] * len(ids))
                c.execute(f"DELETE FROM emails WHERE inbox_id IN ({placeholders})", ids)
                c.execute(f"UPDATE inboxes SET is_active = 0 WHERE id IN ({placeholders})", ids)
                conn.commit()
        return len(expired)
    
    def add_forwarding_rule(self, inbox_id, forward_to):
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO forwarding_rules (inbox_id, forward_to) VALUES (?, ?)",
                (inbox_id, forward_to)
            )
            conn.commit()
    
    def get_stats(self):
        with get_db() as conn:
            c = conn.cursor()
            
            c.execute("SELECT COUNT(*) FROM inboxes WHERE is_active = 1")
            active_inboxes = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM emails WHERE is_deleted = 0")
            total_emails = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM sessions WHERE is_active = 1")
            active_sessions = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM domains WHERE is_custom = 1 AND active = 1")
            custom_domains = c.fetchone()[0]
            
        return {
            "active_inboxes": active_inboxes,
            "total_emails": total_emails,
            "active_sessions": active_sessions,
            "custom_domains": custom_domains
        }

# ============================================================
# MODERN PYTHON 3.12+ ASYNC SMTP SERVER
# ============================================================

class AsyncSMTPServer:
    def __init__(self, host, port, mail_handler):
        self.host = host
        self.port = port
        self.mail_handler = mail_handler

    async def handle_client(self, reader, writer):
        try:
            writer.write(b"220 temp-mail-server SMTP ready\r\n")
            await writer.drain()
            
            mail_from = ""
            rcpt_tos = []
            data_mode = False
            data_buffer = []

            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break
                
                if data_mode:
                    # Look for the end of DATA indicator (a single dot on its own line)
                    if line_bytes in (b".\r\n", b".\n"):
                        data_mode = False
                        raw_data = b"".join(data_buffer)
                        try:
                            self.mail_handler(mail_from, rcpt_tos, raw_data)
                            writer.write(b"250 OK: Message accepted for delivery\r\n")
                        except Exception as e:
                            print(f"❌ Error handling mail: {e}")
                            writer.write(b"451 Requested action aborted: local error in processing\r\n")
                        await writer.drain()
                        data_buffer = []
                    else:
                        # Handle dot-stuffing: If the line starts with a dot, remove it
                        if line_bytes.startswith(b"."):
                            line_bytes = line_bytes[1:]
                        data_buffer.append(line_bytes)
                    continue

                line = line_bytes.decode("utf-8", errors="ignore").strip()
                upper_line = line.upper()
                
                if not line:
                    continue
                
                if upper_line.startswith("EHLO") or upper_line.startswith("HELO"):
                    writer.write(b"250-temp-mail-server Hello\r\n250-SIZE 10485760\r\n250 8BITMIME\r\n")
                elif upper_line.startswith("MAIL FROM:"):
                    mail_from = line[10:].strip("<> ")
                    writer.write(b"250 2.1.0 OK\r\n")
                elif upper_line.startswith("RCPT TO:"):
                    rcpt = line[8:].strip("<> ")
                    rcpt_tos.append(rcpt)
                    writer.write(b"250 2.1.5 OK\r\n")
                elif upper_line == "DATA":
                    if not mail_from or not rcpt_tos:
                        writer.write(b"503 Bad sequence of commands\r\n")
                    else:
                        data_mode = True
                        writer.write(b"354 Start mail input; end with <CRLF>.<CRLF>\r\n")
                elif upper_line == "QUIT":
                    writer.write(b"221 2.0.0 Bye\r\n")
                    await writer.drain()
                    break
                elif upper_line == "RSET":
                    mail_from = ""
                    rcpt_tos = []
                    data_mode = False
                    data_buffer = []
                    writer.write(b"250 2.0.0 OK\r\n")
                elif upper_line == "NOOP":
                    writer.write(b"250 2.0.0 OK\r\n")
                else:
                    writer.write(b"500 5.5.1 Command unrecognized\r\n")
                await writer.drain()
        except Exception as e:
            print(f"Connection error in SMTP server: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except:
                pass

    async def start(self):
        try:
            server = await asyncio.start_server(self.handle_client, self.host, self.port)
            print(f"📨 SMTP Server starting on {self.host}:{self.port} (asyncio)")
            async with server:
                await server.serve_forever()
        except OSError as e:
            print(f"\n❌ Failed to start SMTP Server on {self.host}:{self.port}: {e}")
            if self.port == 25:
                print("   Note: Port 25 usually requires administrator/root privileges.")
            else:
                print(f"   Note: Check if another service is already using port {self.port}.")

def handle_incoming_email(mailfrom, rcpttos, raw_data):
    try:
        msg = email.message_from_bytes(raw_data)
        
        subject = msg.get('Subject', 'No Subject')
        from_addr = msg.get('From', 'Unknown')
        message_id = msg.get('Message-ID')
        
        if subject:
            try:
                from email.header import decode_header
                decoded = decode_header(subject)
                subject = ' '.join([str(t[0], t[1] or 'utf-8') if isinstance(t[0], bytes) else t[0] for t in decoded])
            except:
                pass
        
        body = ""
        html_body = None
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition', ''))
                
                if 'attachment' not in content_disposition:
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            if content_type == 'text/plain':
                                body = payload.decode('utf-8', errors='ignore')
                            elif content_type == 'text/html':
                                html_body = payload.decode('utf-8', errors='ignore')
                    except:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='ignore')
            except:
                body = str(msg.get_payload())
        
        # Get all active domains
        domain_mgr = DomainManager()
        active_domains = [d['domain'] for d in domain_mgr.get_all_domains()]
        
        valid_recipients = [r for r in rcpttos if any(r.endswith(f'@{d}') for d in active_domains)]
        
        if valid_recipients:
            print(f"\n📧 Received email")
            print(f"   From: {from_addr}")
            print(f"   To: {valid_recipients}")
            print(f"   Subject: {subject}")
            
            temp_mail = TempMail()
            stored = temp_mail.receive_email(
                from_addr, valid_recipients, subject, body, html_body, message_id
            )
            print(f"   ✅ Stored in {stored} inbox(es)")
        else:
            print(f"⚠️ No valid recipients for any active domain: {rcpttos}")
            
    except Exception as e:
        print(f"❌ Error processing email: {e}")

def run_smtp():
    def start_smtp_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = AsyncSMTPServer(SMTP_HOST, SMTP_PORT, handle_incoming_email)
        try:
            loop.run_until_complete(server.start())
        except Exception as e:
            print(f"❌ SMTP Server event loop error: {e}")
            
    smtp_thread = threading.Thread(target=start_smtp_server)
    smtp_thread.daemon = True
    smtp_thread.start()

# ============================================================
# FLASK WEB INTERFACE & ROUTES
# ============================================================

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = SECRET_KEY
temp_mail = TempMail()
domain_manager = DomainManager()
user_manager = UserManager()

# Initialize database tables on first request (prevents crashes during serverless module load/compilation)
@app.before_request
def initialize_database():
    if not getattr(app, '_db_initialized', False):
        init_db()
        app._db_initialized = True

import traceback
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({
        "error": str(e),
        "db_path": get_db_path(),
        "traceback": traceback.format_exc()
    }), 500

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required."}), 400
    
    result = user_manager.register_user(username, password)
    if result["success"]:
        # Auto-login after registration
        session['user_id'] = result["user_id"]
        return jsonify({"success": True, "message": "Registered and logged in successfully."})
    return jsonify(result), 400

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required."}), 400
    
    user = user_manager.authenticate_user(username, password)
    if user:
        session['user_id'] = user['id']
        return jsonify({"success": True, "user": user})
    return jsonify({"success": False, "error": "Invalid username or password."}), 401

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    return jsonify({"success": True, "message": "Logged out successfully."})

@app.route('/api/auth/status')
def auth_status():
    user_id = session.get('user_id')
    if user_id:
        user = user_manager.get_user_by_id(user_id)
        if user:
            return jsonify({"success": True, "logged_in": True, "user": user})
    return jsonify({"success": True, "logged_in": False})

@app.route('/api/auth/upgrade', methods=['POST'])
def upgrade_user():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"success": False, "error": "Authentication required."}), 401
    
    user = user_manager.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404
        
    # Toggle premium for demo purposes
    new_status = not user['is_premium']
    user_manager.set_premium_status(user_id, new_status)
    return jsonify({"success": True, "is_premium": new_status})

@app.route('/')
def index():
    response = make_response(render_template('index.html', domain=CUSTOM_DOMAIN))
    response.set_cookie('session_id', '', expires=0)
    return response

@app.route('/inbox/<inbox_id>')
def inbox_page(inbox_id):
    inbox = temp_mail.get_inbox(inbox_id)
    if inbox:
        response = make_response(render_template('index.html', domain=CUSTOM_DOMAIN))
        response.set_cookie('session_id', inbox['session_id'], max_age=7*24*60*60, httponly=True)
        return response
    return "Inbox not found", 404

@app.route('/session/<session_id>')
def session_page(session_id):
    session_data = temp_mail.get_session(session_id)
    if session_data:
        response = make_response(render_template('index.html', domain=CUSTOM_DOMAIN))
        response.set_cookie('session_id', session_id, max_age=7*24*60*60, httponly=True)
        return response
    return "Session not found", 404

@app.route('/api/inbox/generate', methods=['POST'])
def generate_inbox():
    local = request.args.get('local')
    
    # Enforce premium check on custom local prefix
    if local:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({"success": False, "error": "Authentication required. Please log in to create custom names."}), 403
        user = user_manager.get_user_by_id(user_id)
        if not user or not user['is_premium']:
            return jsonify({"success": False, "error": "Premium account required to use custom names."}), 403

    custom_domain = request.args.get('domain')
    
    try:
        user_id = session.get('user_id')
        username = None
        if user_id:
            user = user_manager.get_user_by_id(user_id)
            if user:
                username = user['username']

        inbox = temp_mail.generate_inbox(
            session_id=None, 
            custom_local=local, 
            custom_domain=custom_domain,
            username=username
        )
        session_data = temp_mail.get_session(inbox['session_id'])
        
        response = jsonify({
            'success': True,
            'inbox': inbox,
            'session': {
                'session_id': session_data['session_id'],
                'created_at': session_data['created_at'],
                'expires_at': session_data['expires_at'],
                'inboxes': temp_mail.get_inboxes_by_session(session_data['session_id'])
            }
        })
        
        response.set_cookie('session_id', inbox['session_id'], max_age=7*24*60*60, httponly=True)
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/session/<session_id>')
def get_session(session_id):
    session_data = temp_mail.get_session(session_id)
    if session_data:
        inboxes = temp_mail.get_inboxes_by_session(session_id)
        response = jsonify({
            'success': True,
            'session': session_data,
            'inboxes': inboxes
        })
        response.set_cookie('session_id', session_id, max_age=7*24*60*60, httponly=True)
        return response
    return jsonify({'success': False, 'error': 'Session not found'})

@app.route('/api/inbox/<inbox_id>')
def get_inbox(inbox_id):
    inbox = temp_mail.get_inbox(inbox_id)
    if inbox:
        return jsonify({'success': True, 'inbox': inbox})
    return jsonify({'success': False, 'error': 'Inbox not found'})

@app.route('/api/inbox/<inbox_id>/emails')
def list_emails(inbox_id):
    emails = temp_mail.list_emails(inbox_id)
    return jsonify({'success': True, 'emails': emails})

@app.route('/api/email/<int:email_id>')
def get_email(email_id):
    email_data = temp_mail.get_email(email_id)
    if email_data:
        return jsonify({'success': True, 'email': email_data})
    return jsonify({'success': False, 'error': 'Email not found'})

@app.route('/api/inbox/<inbox_id>/delete', methods=['DELETE'])
def delete_inbox(inbox_id):
    temp_mail.delete_inbox(inbox_id)
    return jsonify({'success': True})

@app.route('/api/domains')
def get_domains():
    domains = temp_mail.get_available_domains()
    return jsonify({'success': True, 'domains': domains})

@app.route('/api/domain/add', methods=['POST'])
def add_domain():
    data = request.json
    domain = data.get('domain')
    if not domain:
        return jsonify({'success': False, 'error': 'Domain required'})
    result = domain_manager.add_custom_domain(domain)
    return jsonify(result)

@app.route('/api/domain/remove', methods=['POST'])
def remove_domain():
    data = request.json
    domain = data.get('domain')
    if not domain:
        return jsonify({'success': False, 'error': 'Domain required'})
    result = domain_manager.remove_custom_domain(domain)
    return jsonify(result)

@app.route('/api/stats')
def get_stats():
    return jsonify({'success': True, 'stats': temp_mail.get_stats()})

@app.route('/api/cleanup', methods=['POST'])
def cleanup():
    count = temp_mail.cleanup_expired()
    return jsonify({'success': True, 'cleaned': count})

# ============================================================
# ENTRY POINT
# ============================================================

def main():
    print("="*60)
    print("📧 DAMXDMAIL SYSTEM - CUSTOM DOMAINS")
    print(f"   Default Domain: @{CUSTOM_DOMAIN}")
    print("="*60)
    
    init_db()
    
    run_smtp()
    
    print(f"\n🌐 Web Interface: http://localhost:{API_PORT}")
    print(f"📧 Emails to *@damxd.shop and custom domains")
    print("\n💡 Features:")
    print("   - Create any email: anything@damxd.shop")
    print("   - Add custom domains: yourdomain.com")
    print("   - Session restore with Session ID")
    print("   - One unique inbox per session ID key")
    print("\nPress Ctrl+C to stop\n")
    
    app.run(host='0.0.0.0', port=API_PORT, debug=False, threaded=True)

if __name__ == "__main__":
    main()
