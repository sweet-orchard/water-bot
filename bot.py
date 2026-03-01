"""
💧 AquaBot v4.0
===============
Changes from v3:
  - Full onboarding inside a single dashboard message (no stray messages)
  - Chart lines no longer wrap: amounts are right-aligned and truncated
  - Notification snooze has a real "back to notification" flow
  - Premium: free → trial offer (not auto-started) → trial → expired → buy
  - Premium purchase gives rich feedback with a feature showcase
  - Reminders: "Start −/+" instead of "Quiet −/+"
  - Dashboard home shows account badge prominently (Free / Trial / ⭐ Premium)
  - Date shown on home instead of time; star prefix for premium users
  - Manage Data: info icon, description, redirect to settings for account delete
  - 20+ achievements
  - Language changes actually translate ALL UI strings immediately
  - Extensive bug fixes across notification flow, snooze, premium checks, etc.

Requirements:
    pip install python-telegram-bot[job-queue] pytz requests
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def load_env_file(path: str = ".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file()

import pytz
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ.get("WATER_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_FILE     = os.environ.get("AQUABOT_DB", "aquabot.db")
ADMIN_IDS   = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

OWM_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "YOUR_OPENWEATHER_API_KEY_HERE")

DEFAULT_TZ    = "UTC"
PREMIUM_STARS = 1
TRIAL_DAYS    = 3

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("aquabot")

# ─────────────────────────────────────────────────────────────────
#  FSM STATES
# ─────────────────────────────────────────────────────────────────

class State(Enum):
    # Onboarding — all happen inside the single dashboard message
    OB_WELCOME          = auto()
    OB_LANGUAGE         = auto()
    OB_WEIGHT           = auto()
    OB_ACTIVITY         = auto()
    OB_CITY             = auto()
    OB_UNIT             = auto()
    # Normal use
    IDLE                = auto()
    AWAIT_CUSTOM_LOG    = auto()
    AWAIT_FIXED_TIME    = auto()
    AWAIT_RECALC_WEIGHT = auto()
    AWAIT_RECALC_ACT    = auto()
    AWAIT_CUSTOM_GOAL   = auto()
    AWAIT_CITY_UPDATE   = auto()

# ─────────────────────────────────────────────────────────────────
#  CALLBACK CONSTANTS
# ─────────────────────────────────────────────────────────────────

CB_NOOP              = "noop"
CB_NAV_HOME          = "nav:home"
CB_NAV_LOG           = "nav:log"
CB_NAV_STATS         = "nav:stats"
CB_NAV_ACHIEVEMENTS  = "nav:achievements"
CB_NAV_HISTORY       = "nav:history"
CB_NAV_REMINDERS     = "nav:reminders"
CB_NAV_SETTINGS      = "nav:settings"
CB_NAV_DELETE        = "nav:delete"
CB_NAV_PREMIUM       = "nav:premium"
CB_NAV_CHARTS        = "nav:charts"

CB_LOG_UNDO          = "log:undo"
CB_LOG_CUSTOM        = "log:custom"

CB_REM_TOGGLE        = "rem:toggle"
CB_REM_ADD           = "rem:add"
CB_REM_RM            = "rem:rm"

CB_DELETE_TODAY          = "delete:today"
CB_DELETE_DAY_LIST       = "delete:day_list"
CB_DELETE_ALL_CONFIRM    = "delete:all_confirm"
CB_DELETE_ALL_DO         = "delete:all_do"
CB_DELETE_ACCOUNT_CONFIRM= "delete:account_confirm"
CB_DELETE_ACCOUNT_DO     = "delete:account_do"

CB_PREM_BUY              = "prem:buy"
CB_PREM_START_TRIAL      = "prem:start_trial"

CB_CFG_UNIT      = "cfg:unit"
CB_CFG_LANGUAGE  = "cfg:language"
CB_CFG_ACTIVITY  = "cfg:activity"
CB_CFG_RECALC    = "cfg:recalc"

CB_SKIP_TODAY    = "home:skip_today"
CB_UNSKIP_TODAY  = "home:unskip_today"
CB_SNOOZE_MENU   = "snooze:menu"
CB_OB_SETUP_LATER = "ob:setup_later"

# ─────────────────────────────────────────────────────────────────
#  MULTILINGUAL CONTENT
# ─────────────────────────────────────────────────────────────────

STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "lang_name":         "🇬🇧 English",
        "welcome":           "👋 <b>Welcome to AquaBot!</b>\n\nI track your water intake and send smart reminders so you stay hydrated every single day.\n\nLet's set you up — it takes 30 seconds.",
        "ask_weight":        "⚖️ <b>What is your weight?</b>\n\nType a number in kg, for example: <code>70</code>",
        "ask_weight_err":    "⚠️ Please enter your weight as a number in kg (e.g. <code>70</code>). Must be between 20 and 300.",
        "ask_activity":      "🏃 <b>How active are you on a typical day?</b>",
        "ask_city":          "🌍 <b>Which city are you in?</b>\n\nThis lets me adjust your goal on hot days using live weather. Type your city name or tap Skip.",
        "ask_unit":          "📐 <b>Which unit do you prefer for water?</b>",
        "setup_done":        "✅ <b>All set!</b>  Your daily goal: <b>{goal}</b>  ·  Reminders every <b>{interval}</b>",
        "goal_reached":      "🏆 You hit your goal for today! Amazing work! 💙",
        "log_confirm":       "✅ +{amount}  ·  {remaining} still to go",
        "undo_done":         "↩️ Removed {amount}",
        "undo_empty":        "Nothing to undo.",
        "snooze_set":        "⏰ Snoozed for {mins} min.",
        "skip_today":        "😴 Reminders paused for today. See you tomorrow!",
        "unskip_today":      "🔔 Reminders resumed. Let's drink up!",
        "premium_desc":      "Lifetime access · <b>{stars} ⭐</b>",
        "free_account":      "🆓 Free Account",
        "trial_account":     "🎁 Trial  ({days}d left)",
        "premium_account":   "⭐ Premium · Lifetime",
        "trial_expired_msg": "⏰ <b>Your free trial has ended.</b>\n\nYou have already used your 3-day free trial. Upgrade to Lifetime Premium for just {stars} ⭐ to keep all the features you enjoyed.",
        "nav_log":           "💧 <b>Log water</b>\n\n★ Your favorite amounts appear here - shown after you log an amount a few times.\nTap a button or enter a custom amount.",
        "nav_charts":        "📈 <b>Charts</b>\n\nChoose a time range to view your intake history.",
        "nav_delete":        "🗑️ <b>Manage Data</b>\n\nClear today's intake, delete a specific day, wipe all history, or delete your account. The first three keep your account and settings; deleting your account removes everything.",
        "nav_reminders":     "⏰ <b>Reminders</b>",
        # Buttons (keep short to avoid overflow)
        "btn_setup":         "🚀 Set Up",
        "btn_quick":         "⚡ Quick 2L",
        "btn_skip":          "⏭ Skip",
        "btn_back":          "◀ Back",
        "btn_cancel":        "◀ Cancel",
        "btn_home":          "◀ Home",
        "btn_log":           "💧 Log",
        "btn_stats":         "📊 Stats",
        "btn_charts":        "📈 Charts",
        "btn_achievements":  "🏆 Achievements",
        "btn_history":       "📂 History",
        "btn_reminders":     "⏰ Reminders",
        "btn_settings":      "⚙️ Settings",
        "btn_premium":       "⭐ Premium",
        "btn_manage":        "📋 Manage Data",
        "btn_custom":        "✏️ Custom",
        "btn_undo":          "↩️ Undo",
        "btn_7d":            "📊 7d",
        "btn_30d":           "📅 30d",
        "btn_add_fixed":     "📌 Add fixed",
        "btn_remove_last":   "🗑 Remove last",
        "btn_toggle_on":     "🔔 Enable",
        "btn_toggle_off":    "🔕 Disable",
        "btn_activity":      "🏃 Activity",
        "btn_unit":          "📐 ml/oz",
        "btn_language":      "🌍 Language",
        "btn_city":          "📍 Change city",
        "btn_goal_custom":   "✏️ Custom goal",
        "btn_recalc":        "⚖️ Recalc",
        "btn_export":        "📤 Export",
        "btn_delete_account": "Delete Account",
        "btn_clear_today":   "🗑 Clear Today",
        "btn_delete_day":    "🗑 Delete day",
        "btn_wipe_all":      "🗑 Wipe All Days",
        "btn_trial":         "🎁 3-Day Trial",
        "btn_buy":           "Buy Lifetime",
        "btn_upgrade":       "Upgrade",
        "btn_snooze":        "⏰ Snooze",
        "btn_dismiss":       "✅ Dismiss",
        "btn_skip_today":    "😴 Skip Today",
        "btn_back_reminder": "◀ Back",
        "btn_resume_today":  "🔔 Resume",
        "btn_start_minus":   "Start−",
        "btn_start_plus":    "Start+",
        "btn_end_minus":     "End−",
        "btn_end_plus":      "End+",
        "btn_yes_wipe":      "✅ Yes, wipe",
        "btn_delete_account_confirm": "✅ Delete account",
    },
    "es": {
        "lang_name":         "🇪🇸 Español",
        "welcome":           "👋 <b>¡Bienvenido a AquaBot!</b>\n\nRastrearé tu ingesta de agua y te enviaré recordatorios inteligentes.\n\n¡Configuremos tu perfil en 30 segundos!",
        "ask_weight":        "⚖️ <b>¿Cuánto pesas?</b>\n\nEscribe en kg, por ejemplo: <code>70</code>",
        "ask_weight_err":    "⚠️ Escribe tu peso en kg (ej. <code>70</code>). Debe estar entre 20 y 300.",
        "ask_activity":      "🏃 <b>¿Qué tan activo eres en un día típico?</b>",
        "ask_city":          "🌍 <b>¿En qué ciudad estás?</b>\n\nEscribe tu ciudad o pulsa Omitir.",
        "ask_unit":          "📐 <b>¿Qué unidad prefieres?</b>",
        "setup_done":        "✅ <b>¡Listo!</b>  Meta diaria: <b>{goal}</b>  ·  Recordatorios cada <b>{interval}</b>",
        "goal_reached":      "🏆 ¡Meta alcanzada hoy! ¡Increíble! 💙",
        "log_confirm":       "✅ +{amount}  ·  {remaining} restantes",
        "undo_done":         "↩️ Eliminado {amount}",
        "undo_empty":        "Nada que deshacer.",
        "snooze_set":        "⏰ Pospuesto {mins} min.",
        "skip_today":        "😴 Recordatorios pausados hoy.",
        "unskip_today":      "🔔 Recordatorios reanudados.",
        "premium_desc":      "Acceso de por vida · <b>{stars} ⭐</b>",
        "free_account":      "🆓 Cuenta Gratis",
        "trial_account":     "🎁 Prueba  ({days}d restantes)",
        "premium_account":   "⭐ Premium · De por vida",
        "trial_expired_msg": "⏰ <b>Tu prueba gratuita ha terminado.</b>\n\nYa usaste tu prueba de 3 días. Actualiza a Premium de por vida por solo {stars} ⭐.",
        "nav_log":           "💧 <b>Registrar agua</b>\n\n★ Tus cantidades favoritas aparecen aquí - se muestran después de registrar una cantidad varias veces.",
        "nav_charts":        "📈 <b>Gráficos</b>\n\nElige un rango de tiempo.",
        "nav_delete":        "🗑️ <b>Gestionar Datos</b>\n\nBorra el registro de hoy, elimina un día específico, borra todo el historial o elimina tu cuenta. Las tres primeras opciones mantienen tu cuenta y ajustes; eliminar la cuenta lo borra todo.",
        "nav_reminders":     "⏰ <b>Recordatorios</b>",
        "btn_setup":         "🚀 Empezar",
        "btn_quick":         "⚡ Rápido 2L",
        "btn_skip":          "⏭ Omitir",
        "btn_back":          "◀ Atrás",
        "btn_cancel":        "◀ Cancelar",
        "btn_home":          "◀ Inicio",
        "btn_log":           "💧 Registrar",
        "btn_stats":         "📊 Estadísticas",
        "btn_charts":        "📈 Gráficos",
        "btn_achievements":  "🏆 Logros",
        "btn_history":       "📂 Historial",
        "btn_reminders":     "⏰ Recordatorios",
        "btn_settings":      "⚙️ Ajustes",
        "btn_premium":       "⭐ Premium",
        "btn_manage":        "📋 Datos",
        "btn_custom":        "✏️ Personal",
        "btn_undo":          "↩️ Deshacer",
        "btn_7d":            "📊 7d",
        "btn_30d":           "📅 30d",
        "btn_add_fixed":     "📌 Añadir fija",
        "btn_remove_last":   "🗑 Quitar última",
        "btn_toggle_on":     "🔔 Activar",
        "btn_toggle_off":    "🔕 Desactivar",
        "btn_activity":      "🏃 Actividad",
        "btn_unit":          "📐 ml/oz",
        "btn_language":      "🌍 Idioma",
        "btn_city":          "📍 Cambiar ciudad",
        "btn_goal_custom":   "✏️ Meta manual",
        "btn_recalc":        "⚖️ Recalcular",
        "btn_export":        "📤 Exportar",
        "btn_delete_account": "Eliminar cuenta",
        "btn_clear_today":   "🗑 Borrar hoy",
        "btn_delete_day":    "🗑 Borrar día",
        "btn_wipe_all":      "🗑 Borrar días",
        "btn_trial":         "🎁 Prueba 3 días",
        "btn_buy":           "Comprar",
        "btn_upgrade":       "Mejorar",
        "btn_snooze":        "⏰ Posponer",
        "btn_dismiss":       "✅ Cerrar",
        "btn_skip_today":    "😴 Pausar hoy",
        "btn_back_reminder": "◀ Atrás",
        "btn_resume_today":  "🔔 Reanudar",
        "btn_start_minus":   "Inicio−",
        "btn_start_plus":    "Inicio+",
        "btn_end_minus":     "Fin−",
        "btn_end_plus":      "Fin+",
        "btn_yes_wipe":      "✅ Sí, borrar",
        "btn_delete_account_confirm": "✅ Borrar cuenta",
    },
    "de": {
        "lang_name":         "🇩🇪 Deutsch",
        "welcome":           "👋 <b>Willkommen bei AquaBot!</b>\n\nIch verfolge deine Wasseraufnahme und sende smarte Erinnerungen.\n\nLass uns dein Profil einrichten – dauert nur 30 Sekunden!",
        "ask_weight":        "⚖️ <b>Wie viel wiegst du?</b>\n\nZahl in kg eingeben, z.B. <code>70</code>",
        "ask_weight_err":    "⚠️ Bitte gib dein Gewicht in kg ein (z.B. <code>70</code>). Zwischen 20 und 300.",
        "ask_activity":      "🏃 <b>Wie aktiv bist du an einem typischen Tag?</b>",
        "ask_city":          "🌍 <b>In welcher Stadt bist du?</b>\n\nStadt eingeben oder überspringen.",
        "ask_unit":          "📐 <b>Welche Einheit bevorzugst du?</b>",
        "setup_done":        "✅ <b>Fertig!</b>  Tagesziel: <b>{goal}</b>  ·  Alle <b>{interval}</b>",
        "goal_reached":      "🏆 Tagesziel erreicht! Fantastisch! 💙",
        "log_confirm":       "✅ +{amount}  ·  Noch {remaining}",
        "undo_done":         "↩️ {amount} entfernt",
        "undo_empty":        "Nichts rückgängig zu machen.",
        "snooze_set":        "⏰ {mins} Min. verschoben.",
        "skip_today":        "😴 Erinnerungen heute pausiert.",
        "unskip_today":      "🔔 Erinnerungen fortgesetzt.",
        "premium_desc":      "Lebenslanger Zugang · <b>{stars} ⭐</b>",
        "free_account":      "🆓 Kostenloses Konto",
        "trial_account":     "🎁 Testversion  (noch {days}T)",
        "premium_account":   "⭐ Premium · Lebenslang",
        "trial_expired_msg": "⏰ <b>Deine kostenlose Testversion ist abgelaufen.</b>\n\nDu hast deine 3-Tage-Testversion bereits genutzt. Upgrade auf Lifetime für nur {stars} ⭐.",
        "nav_log":           "💧 <b>Wasser eintragen</b>\n\n★ Deine Favoriten-Mengen erscheinen hier - werden angezeigt, nachdem du eine Menge mehrmals eingetragen hast.",
        "nav_charts":        "📈 <b>Diagramme</b>\n\nWähle einen Zeitraum.",
        "nav_delete":        "🗑️ <b>Daten verwalten</b>\n\nLösche den heutigen Eintrag, einen bestimmten Tag, den gesamten Verlauf oder dein Konto. Die ersten drei behalten dein Konto und Einstellungen; Konto löschen entfernt alles.",
        "nav_reminders":     "⏰ <b>Erinnerungen</b>",
        "btn_setup":         "🚀 Start",
        "btn_quick":         "⚡ Schnell 2L",
        "btn_skip":          "⏭ Überspringen",
        "btn_back":          "◀ Zurück",
        "btn_cancel":        "◀ Abbrechen",
        "btn_home":          "◀ Home",
        "btn_log":           "💧 Eintragen",
        "btn_stats":         "📊 Statistik",
        "btn_charts":        "📈 Diagramme",
        "btn_achievements":  "🏆 Erfolge",
        "btn_history":       "📂 Verlauf",
        "btn_reminders":     "⏰ Erinnerungen",
        "btn_settings":      "⚙️ Einstellungen",
        "btn_premium":       "⭐ Premium",
        "btn_manage":        "📋 Daten",
        "btn_custom":        "✏️ Benutzerdef.",
        "btn_undo":          "↩️ Rückgängig",
        "btn_7d":            "📊 7T",
        "btn_30d":           "📅 30T",
        "btn_add_fixed":     "📌 Fixzeit",
        "btn_remove_last":   "🗑 Letzte löschen",
        "btn_toggle_on":     "🔔 An",
        "btn_toggle_off":    "🔕 Aus",
        "btn_activity":      "🏃 Aktivität",
        "btn_unit":          "📐 ml/oz",
        "btn_language":      "🌍 Sprache",
        "btn_city":          "📍 Stadt ändern",
        "btn_goal_custom":   "✏️ Eigenes Ziel",
        "btn_recalc":        "⚖️ Neu berechnen",
        "btn_export":        "📤 Export",
        "btn_delete_account": "Konto löschen",
        "btn_clear_today":   "🗑 Heute löschen",
        "btn_delete_day":    "🗑 Tag löschen",
        "btn_wipe_all":      "🗑 Alle Tage",
        "btn_trial":         "🎁 3-Tage-Test",
        "btn_buy":           "Kaufen",
        "btn_upgrade":       "Upgrade",
        "btn_snooze":        "⏰ Schlummern",
        "btn_dismiss":       "✅ Schließen",
        "btn_skip_today":    "😴 Heute aus",
        "btn_back_reminder": "◀ Zurück",
        "btn_resume_today":  "🔔 Fortsetzen",
        "btn_start_minus":   "Start−",
        "btn_start_plus":    "Start+",
        "btn_end_minus":     "Ende−",
        "btn_end_plus":      "Ende+",
        "btn_yes_wipe":      "✅ Ja, löschen",
        "btn_delete_account_confirm": "✅ Konto löschen",
    },
    "fr": {
        "lang_name":         "🇫🇷 Français",
        "welcome":           "👋 <b>Bienvenue sur AquaBot!</b>\n\nJe suis ton hydratation et t'envoie des rappels intelligents.\n\nConfigurons ton profil en 30 secondes !",
        "ask_weight":        "⚖️ <b>Quel est ton poids?</b>\n\nTape en kg, ex. <code>70</code>",
        "ask_weight_err":    "⚠️ Tape ton poids en kg (ex. <code>70</code>). Entre 20 et 300.",
        "ask_activity":      "🏃 <b>Quel est ton niveau d'activité typique?</b>",
        "ask_city":          "🌍 <b>Dans quelle ville es-tu?</b>\n\nTape le nom ou passe.",
        "ask_unit":          "📐 <b>Quelle unité préfères-tu?</b>",
        "setup_done":        "✅ <b>C'est parti!</b>  Objectif: <b>{goal}</b>  ·  Toutes les <b>{interval}</b>",
        "goal_reached":      "🏆 Objectif du jour atteint! Bravo! 💙",
        "log_confirm":       "✅ +{amount}  ·  {remaining} restant",
        "undo_done":         "↩️ {amount} supprimé",
        "undo_empty":        "Rien à annuler.",
        "snooze_set":        "⏰ Reporté {mins} min.",
        "skip_today":        "😴 Rappels suspendus aujourd'hui.",
        "unskip_today":      "🔔 Rappels repris.",
        "premium_desc":      "Accès à vie · <b>{stars} ⭐</b>",
        "free_account":      "🆓 Compte Gratuit",
        "trial_account":     "🎁 Essai  ({days}j restants)",
        "premium_account":   "⭐ Premium · À vie",
        "trial_expired_msg": "⏰ <b>Ton essai gratuit est terminé.</b>\n\nTu as déjà utilisé ton essai de 3 jours. Passe à Premium à vie pour seulement {stars} ⭐.",
        "nav_log":           "💧 <b>Enregistrer de l'eau</b>\n\n★ Tes quantités préférées apparaissent ici - s'affichent après avoir enregistré une quantité plusieurs fois.",
        "nav_charts":        "📈 <b>Graphiques</b>\n\nChoisis une période.",
        "nav_delete":        "🗑️ <b>Gérer les données</b>\n\nSupprime l'historique d'aujourd'hui, un jour précis, tout l'historique ou ton compte. Les trois premières options gardent ton compte et paramètres; supprimer le compte supprime tout.",
        "nav_reminders":     "⏰ <b>Rappels</b>",
        "btn_setup":         "🚀 Démarrer",
        "btn_quick":         "⚡ Rapide 2L",
        "btn_skip":          "⏭ Passer",
        "btn_back":          "◀ Retour",
        "btn_cancel":        "◀ Annuler",
        "btn_home":          "◀ Accueil",
        "btn_log":           "💧 Journal",
        "btn_stats":         "📊 Stats",
        "btn_charts":        "📈 Graphiques",
        "btn_achievements":  "🏆 Succès",
        "btn_history":       "📂 Historique",
        "btn_reminders":     "⏰ Rappels",
        "btn_settings":      "⚙️ Réglages",
        "btn_premium":       "⭐ Premium",
        "btn_manage":        "📋 Données",
        "btn_custom":        "✏️ Perso",
        "btn_undo":          "↩️ Annuler",
        "btn_7d":            "📊 7j",
        "btn_30d":           "📅 30j",
        "btn_add_fixed":     "📌 Ajouter fixe",
        "btn_remove_last":   "🗑 Supprimer",
        "btn_toggle_on":     "🔔 Activer",
        "btn_toggle_off":    "🔕 Désactiver",
        "btn_activity":      "🏃 Activité",
        "btn_unit":          "📐 ml/oz",
        "btn_language":      "🌍 Langue",
        "btn_city":          "📍 Changer ville",
        "btn_goal_custom":   "✏️ But perso",
        "btn_recalc":        "⚖️ Recalculer",
        "btn_export":        "📤 Exporter",
        "btn_delete_account": "Supprimer compte",
        "btn_clear_today":   "🗑 Effacer aujourd'hui",
        "btn_delete_day":    "🗑 Supprimer jour",
        "btn_wipe_all":      "🗑 Effacer jours",
        "btn_trial":         "🎁 Essai 3j",
        "btn_buy":           "Acheter",
        "btn_upgrade":       "Passer",
        "btn_snooze":        "⏰ Rappel + tard",
        "btn_dismiss":       "✅ Fermer",
        "btn_skip_today":    "😴 Pause jour",
        "btn_back_reminder": "◀ Retour",
        "btn_resume_today":  "🔔 Reprendre",
        "btn_start_minus":   "Début−",
        "btn_start_plus":    "Début+",
        "btn_end_minus":     "Fin−",
        "btn_end_plus":      "Fin+",
        "btn_yes_wipe":      "✅ Oui, effacer",
        "btn_delete_account_confirm": "✅ Supprimer",
    },
    "ru": {
        "lang_name":         "🇷🇺 Русский",
        "welcome":           "👋 <b>Добро пожаловать в AquaBot!</b>\n\nЯ помогу следить за водой и буду напоминать вовремя.\n\nНастроим профиль — это 30 секунд.",
        "ask_weight":        "⚖️ <b>Укажи вес в кг</b>\n\nНапример: <code>70</code>",
        "ask_weight_err":    "⚠️ Введи вес числом в кг (20–300), например <code>70</code>.",
        "ask_activity":      "🏃 <b>Насколько ты активен в обычный день?</b>",
        "ask_city":          "🌍 <b>Город?</b>\n\nНужен для погоды. Напиши город или нажми Пропустить.",
        "ask_unit":          "📐 <b>В какой единице вести воду?</b>",
        "setup_done":        "✅ <b>Готово!</b>  Цель: <b>{goal}</b>  ·  Напоминания каждые <b>{interval}</b>",
        "goal_reached":      "🏆 Цель на сегодня достигнута! Отлично! 💙",
        "log_confirm":       "✅ +{amount}  ·  осталось {remaining}",
        "undo_done":         "↩️ Удалено {amount}",
        "undo_empty":        "Нечего отменять.",
        "snooze_set":        "⏰ Отложено на {mins} мин.",
        "skip_today":        "😴 Напоминания отключены до завтра.",
        "unskip_today":      "🔔 Напоминания снова включены.",
        "premium_desc":      "Пожизненный доступ · <b>{stars} ⭐</b>",
        "free_account":      "🆓 Бесплатный аккаунт",
        "trial_account":     "🎁 Пробный период  ({days} дн.)",
        "premium_account":   "⭐ Премиум · Навсегда",
        "trial_expired_msg": "⏰ <b>Пробный период закончился.</b>\n\n3 дня уже использованы. Купи пожизненный Премиум за {stars} ⭐, чтобы сохранить все функции.",
        "nav_log":           "💧 <b>Запись воды</b>\n\n★ Твои любимые объёмы появляются здесь - показываются после записи объёма несколько раз.",
        "nav_charts":        "📈 <b>Графики</b>\n\nВыбери период.",
        "nav_delete":        "🗑️ <b>Управление данными</b>\n\nОчисти сегодня, удали день, весь журнал или удали аккаунт. Первые три сохраняют аккаунт и настройки; удаление аккаунта удаляет всё.",
        "nav_reminders":     "⏰ <b>Напоминания</b>",
        "btn_setup":         "🚀 Начать",
        "btn_quick":         "⚡ Быстро 2Л",
        "btn_skip":          "⏭ Пропустить",
        "btn_back":          "◀ Назад",
        "btn_cancel":        "◀ Отмена",
        "btn_home":          "◀ Домой",
        "btn_log":           "💧 Внести",
        "btn_stats":         "📊 Статистика",
        "btn_charts":        "📈 Графики",
        "btn_achievements":  "🏆 Достижения",
        "btn_history":       "📂 История",
        "btn_reminders":     "⏰ Напоминания",
        "btn_settings":      "⚙️ Настройки",
        "btn_premium":       "⭐ Премиум",
        "btn_manage":        "📋 Данные",
        "btn_custom":        "✏️ Своя",
        "btn_undo":          "↩️ Отменить",
        "btn_7d":            "📊 7д",
        "btn_30d":           "📅 30д",
        "btn_add_fixed":     "📌 Добавить",
        "btn_remove_last":   "🗑 Удалить",
        "btn_toggle_on":     "🔔 Вкл",
        "btn_toggle_off":    "🔕 Выкл",
        "btn_activity":      "🏃 Активность",
        "btn_unit":          "📐 мл/унц",
        "btn_language":      "🌍 Язык",
        "btn_city":          "📍 Сменить город",
        "btn_goal_custom":   "✏️ Своя цель",
        "btn_recalc":        "⚖️ Пересчитать",
        "btn_export":        "📤 Экспорт",
        "btn_delete_account": "Удалить аккаунт",
        "btn_clear_today":   "🗑 Очистить сегодня",
        "btn_delete_day":    "🗑 Удалить день",
        "btn_wipe_all":      "🗑 Стереть дни",
        "btn_trial":         "🎁 Пробные 3 дня",
        "btn_buy":           "Купить",
        "btn_upgrade":       "Улучшить",
        "btn_snooze":        "⏰ Отложить",
        "btn_dismiss":       "✅ Закрыть",
        "btn_skip_today":    "😴 Пауза сегодня",
        "btn_back_reminder": "◀ Назад",
        "btn_resume_today":  "🔔 Возобновить",
        "btn_start_minus":   "Старт−",
        "btn_start_plus":    "Старт+",
        "btn_end_minus":     "Конец−",
        "btn_end_plus":      "Конец+",
        "btn_yes_wipe":      "✅ Да, стереть",
        "btn_delete_account_confirm": "✅ Удалить",
    },
    "uk": {
        "lang_name":         "🇺🇦 Українська",
        "welcome":           "👋 <b>Ласкаво просимо до AquaBot!</b>\n\nЯ відстежую ваше споживання води та надсилаю розумні нагадування, щоб ви залишались здоровими кожного дня.\n\nНалаштуємо все — це займе 30 секунд.",
        "ask_weight":        "⚖️ <b>Яка ваша вага?</b>\n\nВведіть число в кг, наприклад: <code>70</code>",
        "ask_weight_err":    "⚠️ Введіть вагу числом в кг (наприклад <code>70</code>). Має бути від 20 до 300.",
        "ask_activity":      "🏃 <b>Наскільки ви активні протягом дня?</b>",
        "ask_city":          "🌍 <b>В якому місті ви перебуваєте?</b>\n\nЦе дозволяє мені коригувати вашу мету в спекотні дні, використовуючи погоду. Введіть назву міста або натисніть Пропустити.",
        "ask_unit":          "📐 <b>Яку одиницю ви використовуєте для води?</b>",
        "setup_done":        "✅ <b>Готово!</b>  Ваша денна мета: <b>{goal}</b>  ·  Нагадування кожні <b>{interval}</b>",
        "goal_reached":      "🏆 Ви досягли мети на сьогодні! Чудова робота! 💙",
        "log_confirm":       "✅ +{amount}  ·  залишилось {remaining}",
        "undo_done":         "↩️ Видалено {amount}",
        "undo_empty":        "Нічого відкочувати.",
        "snooze_set":        "⏰ Відкладено на {mins} хв.",
        "skip_today":        "😴 Нагадування призупинені на сьогодні. До завтра!",
        "unskip_today":      "🔔 Нагадування відновлено. Пиймо!",
        "premium_desc":      "Пожиттєвий доступ · <b>{stars} ⭐</b>",
        "free_account":      "🆓 Безкоштовний акаунт",
        "trial_account":     "🎁 Пробний період  ({days} днів лишилось)",
        "premium_account":   "⭐ Преміум · Назавжди",
        "trial_expired_msg": "⏰ <b>Ваш безкоштовний пробний період закінчився.</b>\n\nВи вже використали 3-денний пробний період. Оновіть до Преміум за {stars} ⭐, щоб зберегти всі функції.",
        "nav_log":           "💧 <b>Записати воду</b>\n\n★ Ваші улюблені обсяги з'являються тут - показуються після запису обсягу кілька разів.\nНатисніть кнопку або введіть власну кількість.",
        "nav_charts":        "📈 <b>Графіки</b>\n\nОберіть період для перегляду історії споживання.",
        "nav_delete":        "🗑️ <b>Керування даними</b>\n\nОчистіть сьогодні, видаліть конкретний день, видаліть всю історію або видаліть акаунт. Перші три зберігають акаунт і налаштування; видалення акаунту видаляє все.",
        "nav_reminders":     "⏰ <b>Нагадування</b>",
        "btn_setup":         "🚀 Почати",
        "btn_quick":         "⚡ Швидко 2Л",
        "btn_skip":          "⏭ Пропустити",
        "btn_back":          "◀ Назад",
        "btn_cancel":        "◀ Скасувати",
        "btn_home":          "◀ Головна",
        "btn_log":           "💧 Записати",
        "btn_stats":         "📊 Статистика",
        "btn_charts":        "📈 Графіки",
        "btn_achievements":  "🏆 Досягнення",
        "btn_history":       "📂 Історія",
        "btn_reminders":     "⏰ Нагадування",
        "btn_settings":      "⚙️ Налаштування",
        "btn_premium":       "⭐ Преміум",
        "btn_manage":        "📋 Дані",
        "btn_custom":        "✏️ Своя",
        "btn_undo":          "↩️ Відкотити",
        "btn_7d":            "📊 7д",
        "btn_30d":           "📊 30д",
        "btn_90d":           "📊 90д",
        "btn_all":           "📊 Все",
        "btn_add_fixed":     "➕ Додати час",
        "btn_remove_last":   "🗑️ Видалити останнє",
        "btn_toggle_on":     "🔔 Увімкнути",
        "btn_toggle_off":    "🔕 Вимкнути",
        "btn_activity":      "🏃 Активність",
        "btn_unit":          "📐 мл/унц",
        "btn_language":      "🌍 Мова",
        "btn_city":          "📍 Змінити місто",
        "btn_goal_custom":   "✏️ Своя мета",
        "btn_recalc":        "⚖️ Перерахувати",
        "btn_export":        "📤 Експорт",
        "btn_delete_account": "Видалити акаунт",
        "btn_clear_today":   "🗑 Очистити сьогодні",
        "btn_delete_day":    "🗑 Видалити день",
        "btn_wipe_all":      "🗑 Стерти дні",
        "btn_trial":         "🎁 Пробні 3 дні",
        "btn_buy":           "Купити",
        "btn_upgrade":       "Оновити",
        "btn_snooze":        "⏰ Відкласти",
        "btn_dismiss":       "✅ Закрити",
        "btn_skip_today":    "😴 Пауза сьогодні",
        "btn_back_reminder": "◀ Назад",
        "btn_resume_today":  "🔔 Відновити",
        "btn_start_minus":   "🌙 Початок −1",
        "btn_start_plus":    "🌙 Початок +1",
        "btn_end_minus":     "🌙 Кінець −1",
        "btn_end_plus":      "🌙 Кінець +1",
        "btn_save":          "💾 Зберегти",
        "btn_yes_wipe":      "✅ Так, стерти",
        "btn_delete_account_confirm": "✅ Видалити",
        "btn_deletion_confirm": "✅ Так, видалити",
        "btn_deletion_cancel": "✅ Скасувати",
        "btn_undo_confirm":  "✅ Так, відкотити",
    },
}

WELCOME_AFTER_LANG: Dict[str, str] = {
    "en": (
        "💧 <b>Hey there! Welcome to AquaBot!</b> 🎉\n"
        "\n"
        "I'm your personal hydration buddy. Every day I'll cheer you on, "
        "remind you to sip, and celebrate when you hit your goal. 🥳\n"
        "\n"
        "Staying hydrated is one of the simplest things you can do for your "
        "energy, focus, and mood — and I'm here to make it effortless.\n"
        "\n"
        "To get the most out of me, a quick 30-second setup helps me calculate "
        "your personal daily water goal based on your weight and activity. "
        "Or if you're in a rush, just jump straight in — you can always "
        "customise later from Settings. 😊\n"
        "\n"
        "<i>What would you like to do?</i>"
    ),
    "es": (
        "💧 <b>¡Hola! ¡Bienvenido a AquaBot!</b> 🎉\n"
        "\n"
        "Soy tu compañero de hidratación personal. Cada día te animaré, "
        "te recordaré que bebas y celebraré contigo cuando alcances tu meta. 🥳\n"
        "\n"
        "Mantenerte hidratado es una de las cosas más sencillas que puedes hacer "
        "por tu energía, concentración y ánimo — y estoy aquí para hacerlo "
        "sin esfuerzo.\n"
        "\n"
        "Para sacarme el máximo partido, una configuración rápida de 30 segundos "
        "me ayuda a calcular tu objetivo diario de agua según tu peso y actividad. "
        "O si tienes prisa, entra directamente — siempre puedes personalizar "
        "más adelante desde Ajustes. 😊\n"
        "\n"
        "<i>¿Qué quieres hacer?</i>"
    ),
    "de": (
        "💧 <b>Hey! Willkommen bei AquaBot!</b> 🎉\n"
        "\n"
        "Ich bin dein persönlicher Hydrations-Buddy. Jeden Tag werde ich dich "
        "anfeuern, dich ans Trinken erinnern und mit dir feiern, wenn du dein "
        "Ziel erreichst. 🥳\n"
        "\n"
        "Gut hydratisiert zu bleiben ist eines der einfachsten Dinge, die du "
        "für deine Energie, Konzentration und Stimmung tun kannst — und ich bin "
        "hier, um es mühelos zu machen.\n"
        "\n"
        "Für das Beste aus mir braucht es nur 30 Sekunden Setup: Ich berechne "
        "dein persönliches Tagesziel anhand von Gewicht und Aktivität. "
        "Oder spring direkt rein — du kannst später jederzeit in den "
        "Einstellungen anpassen. 😊\n"
        "\n"
        "<i>Was möchtest du tun?</i>"
    ),
    "fr": (
        "💧 <b>Salut ! Bienvenue sur AquaBot !</b> 🎉\n"
        "\n"
        "Je suis ton compagnon d'hydratation personnel. Chaque jour je vais "
        "t'encourager, te rappeler de boire et célébrer avec toi quand tu "
        "atteins ton objectif. 🥳\n"
        "\n"
        "Rester bien hydraté est l'une des choses les plus simples que tu puisses "
        "faire pour ton énergie, ta concentration et ton humeur — et je suis là "
        "pour le rendre sans effort.\n"
        "\n"
        "Pour tirer le meilleur de moi, une configuration rapide de 30 secondes "
        "m'aide à calculer ton objectif hydrique personnel selon ton poids et "
        "ton activité. Ou si tu es pressé, plonge directement — tu pourras "
        "toujours tout personnaliser plus tard dans les Réglages. 😊\n"
        "\n"
        "<i>Que veux-tu faire ?</i>"
    ),
    "ru": (
        "💧 <b>Привет! Добро пожаловать в AquaBot!</b> 🎉\n"
        "\n"
        "Я твой личный помощник по гидратации. Каждый день я буду тебя "
        "поддерживать, напоминать сделать глоток и праздновать вместе с тобой, "
        "когда ты достигнешь цели. 🥳\n"
        "\n"
        "Поддерживать водный баланс — одна из самых простых вещей, которые ты "
        "можешь сделать для своей энергии, концентрации и настроения. "
        "И я здесь, чтобы сделать это без усилий.\n"
        "\n"
        "Чтобы я работал максимально точно, быстрая настройка на 30 секунд "
        "поможет мне рассчитать твою личную дневную цель по весу и активности. "
        "Или если спешишь — сразу начни, всё можно настроить позже в "
        "Настройках. 😊\n"
        "\n"
        "<i>Что хочешь сделать?</i>"
    ),
    "uk": (
        "💧 <b>Привіт! Ласкаво просимо до AquaBot!</b> 🎉\n"
        "\n"
        "Я ваш персональний помічник з гідратації. Кожного дня я буду вас "
        "підтримувати, нагадувати зробити ковток та святкувати разом з вами, "
        "коли ви досягнете мети. 🥳\n"
        "\n"
        "Підтримувати водний баланс — одна з найпростіших речей, яку ви "
        "можете зробити для своєї енергії, концентрації та настрою. "
        "І я тут, щоб зробити це без зусиль.\n"
        "\n"
        "Щоб я працював максимально точно, швидке налаштування за 30 секунд "
        "допоможе мені розрахувати вашу особисту денну мету за вагою та активністю. "
        "Або якщо поспішаєте — одразу починайте, все можна налаштувати пізніше в "
        "Налаштуваннях. 😊\n"
        "\n"
        "<i>Що хочете зробити?</i>"
    ),
}

WELCOME_BUTTONS: Dict[str, Dict[str, str]] = {
    "en": {
        "customise": "⚙️ Customise (30 sec)",
        "quick":     "⚡ Quick Start",
        "later":     "⏭ Set up later",
    },
    "es": {
        "customise": "⚙️ Personalizar (30 seg)",
        "quick":     "⚡ Inicio rápido",
        "later":     "⏭ Configurar después",
    },
    "de": {
        "customise": "⚙️ Einrichten (30 Sek.)",
        "quick":     "⚡ Schnellstart",
        "later":     "⏭ Später einrichten",
    },
    "fr": {
        "customise": "⚙️ Personnaliser (30 sec)",
        "quick":     "⚡ Démarrage rapide",
        "later":     "⏭ Configurer plus tard",
    },
    "ru": {
        "customise": "⚙️ Настроить (30 сек)",
        "quick":     "⚡ Быстрый старт",
        "later":     "⏭ Настрою позже",
    },
    "uk": {
        "customise": "⚙️ Налаштувати (30 сек)",
        "quick":     "⚡ Швидкий старт",
        "later":     "⏭ Налаштую пізніше",
    },
}

SETUP_LATER_MSG: Dict[str, str] = {
    "en": (
        "👍 <b>No problem at all!</b>\n"
        "\n"
        "I've set your daily goal to a standard <b>2 litres</b> for now. "
        "You can fine-tune everything anytime from the ⚙️ Settings button "
        "on your dashboard.\n"
        "\n"
        "Let's get you started! 💧"
    ),
    "es": (
        "👍 <b>¡Sin problema!</b>\n"
        "\n"
        "He establecido tu meta diaria en un estándar de <b>2 litros</b> por ahora. "
        "Puedes ajustar todo en cualquier momento desde el botón ⚙️ Ajustes "
        "en tu panel.\n"
        "\n"
        "¡Empecemos! 💧"
    ),
    "de": (
        "👍 <b>Kein Problem!</b>\n"
        "\n"
        "Ich habe dein Tagesziel vorerst auf standardmäßige <b>2 Liter</b> gesetzt. "
        "Du kannst alles jederzeit über den ⚙️ Einstellungen-Button "
        "in deinem Dashboard anpassen.\n"
        "\n"
        "Lass uns loslegen! 💧"
    ),
    "fr": (
        "👍 <b>Pas de problème !</b>\n"
        "\n"
        "J'ai fixé ton objectif quotidien à <b>2 litres</b> pour l'instant. "
        "Tu peux tout ajuster à tout moment depuis le bouton ⚙️ Réglages "
        "sur ton tableau de bord.\n"
        "\n"
        "C'est parti ! 💧"
    ),
    "ru": (
        "👍 <b>Без проблем!</b>\n"
        "\n"
        "Я установил твою дневную цель на стандартные <b>2 литра</b>. "
        "Ты можешь настроить всё в любое время через кнопку ⚙️ Настройки "
        "на дашборде.\n"
        "\n"
        "Поехали! 💧"
    ),
    "uk": (
        "👍 <b>Без проблем!</b>\n"
        "\n"
        "Я встановив вашу денну мету на стандартні <b>2 літри</b>. "
        "Ви можете налаштувати все в будь-який час через кнопку ⚙️ Налаштування "
        "на дашборді.\n"
        "\n"
        "Поїхали! 💧"
    ),
}

TIPS: Dict[str, List[str]] = {
    "en": [
        "Even 2% dehydration impairs focus and mood.",
        "Your body is ~60% water — keep it topped up!",
        "Drinking water before meals aids digestion.",
        "Morning hydration kick-starts your metabolism.",
        "Sipping consistently beats chugging all at once.",
        "Feeling tired? Dehydration is often the cause.",
        "Hydration keeps your joints lubricated.",
        "Well-hydrated skin looks clearer and healthier.",
        "Water helps flush toxins from your kidneys.",
        "Drinking water can reduce headache frequency.",
        "Even mild dehydration can affect your memory.",
    ],
    "es": [
        "Solo 2% de deshidratación afecta tu concentración.",
        "Tu cuerpo es ~60% agua. ¡Mantenlo hidratado!",
        "Beber agua antes de comer mejora la digestión.",
        "La hidratación matutina activa tu metabolismo.",
    ],
    "de": [
        "2% Dehydrierung beeinträchtigt Konzentration und Stimmung.",
        "Dein Körper ist ~60% Wasser – halte ihn aufgefüllt!",
        "Wasser vor dem Essen fördert die Verdauung.",
        "Morgendliche Hydration kurbelt deinen Stoffwechsel an.",
    ],
    "fr": [
        "2% de déshydratation nuit à la concentration.",
        "Ton corps est ~60% d'eau — garde-le hydraté!",
        "Boire avant les repas aide la digestion.",
        "L'hydratation matinale active ton métabolisme.",
    ],
    "ru": [
        "Даже 2% обезвоживания снижают концентрацию и настроение.",
        "Твое тело примерно на 60% состоит из воды.",
        "Стакан воды перед едой помогает пищеварению.",
        "Утренняя вода помогает быстрее проснуться.",
    ],
    "uk": [
        "Навіть 2% зневоднення знижують концентрацію та настрій.",
        "Ваше тіло приблизно на 60% складається з води.",
        "Склянка води перед їжею допомагає травленню.",
        "Ранкова вода допомагає швидше прокинутися.",
    ],
}

ACTIVITY_LEVELS = {
    "sedentary": {"en": "🪑 Sedentary",  "es": "🪑 Sedentario", "de": "🪑 Sitzend",  "fr": "🪑 Sédentaire", "ru": "🪑 Малоподвижно", "uk": "🪑 Малорухливо", "mult": 1.0},
    "light":     {"en": "🚶 Light",       "es": "🚶 Ligero",     "de": "🚶 Leicht",    "fr": "🚶 Léger",      "ru": "🚶 Легкая",       "uk": "🚶 Легка",        "mult": 1.15},
    "moderate":  {"en": "🏃 Moderate",    "es": "🏃 Moderado",   "de": "🏃 Moderat",   "fr": "🏃 Modéré",     "ru": "🏃 Умеренная",   "uk": "🏃 Помірна",     "mult": 1.3},
    "intense":   {"en": "💪 Intense",     "es": "💪 Intenso",    "de": "💪 Intensiv",  "fr": "💪 Intense",    "ru": "💪 Интенсивная", "uk": "💪 Інтенсивна",  "mult": 1.5},
    "athlete":   {"en": "🏅 Athlete",     "es": "🏅 Atleta",     "de": "🏅 Athlet",    "fr": "🏅 Athlète",    "ru": "🏅 Спортсмен",   "uk": "🏅 Спортсмен",   "mult": 1.7},
}

ACHIEVEMENTS = {
    # First actions
    "first_sip":      {"icon": "🥇", "name": "First Sip",       "desc": "Log your very first water entry"},
    "first_goal":     {"icon": "🎯", "name": "Goal Getter",      "desc": "Hit your daily goal for the first time"},
    "early_bird":     {"icon": "🌅", "name": "Early Bird",       "desc": "Log water before 8 AM"},
    "night_owl":      {"icon": "🦉", "name": "Night Owl",        "desc": "Log water after 10 PM"},
    # Streaks
    "streak_3":       {"icon": "🔥", "name": "3-Day Streak",     "desc": "Hit your goal 3 days in a row"},
    "streak_7":       {"icon": "🌊", "name": "Week Warrior",     "desc": "7-day goal streak"},
    "streak_14":      {"icon": "💫", "name": "Two-Week Titan",   "desc": "14-day goal streak"},
    "streak_30":      {"icon": "💎", "name": "Monthly Master",   "desc": "30-day goal streak"},
    "streak_60":      {"icon": "🚀", "name": "60-Day Legend",    "desc": "60-day goal streak"},
    "streak_100":     {"icon": "👑", "name": "Hydration King",   "desc": "100-day goal streak — truly elite"},
    # Volume milestones
    "litre_5":        {"icon": "💧", "name": "5 Litres",         "desc": "Log 5 litres total"},
    "litre_10":       {"icon": "💦", "name": "10 Litres",        "desc": "Log 10 litres total"},
    "litre_50":       {"icon": "🌊", "name": "50 Litres",        "desc": "Log 50 litres total"},
    "litre_100":      {"icon": "🏊", "name": "100 Litres",       "desc": "Log 100 litres total"},
    "litre_365":      {"icon": "🏔️", "name": "365 Litres",       "desc": "Log 365 litres total"},
    "litre_1000":     {"icon": "🌏", "name": "1000 Litres",      "desc": "Log 1000 litres total — a true legend"},
    # Performance
    "overachiever":   {"icon": "⚡", "name": "Overachiever",     "desc": "Exceed 125% of your daily goal"},
    "double_goal":    {"icon": "🔥", "name": "Double Down",      "desc": "Reach 200% of your daily goal"},
    "perfect_week":   {"icon": "🌟", "name": "Perfect Week",     "desc": "Hit goal every day for 7 days straight"},
    # Variety
    "five_logs":      {"icon": "📝", "name": "Consistent",       "desc": "Log water 5 times in one day"},
    "ten_logs":       {"icon": "🏆", "name": "Log Champion",     "desc": "Log water 10 times in one day"},
    "morning_hero":   {"icon": "☀️", "name": "Morning Hero",     "desc": "Log 500ml before 9 AM"},
    "hydro_rush":     {"icon": "⚡", "name": "Hydro Rush",       "desc": "Log 1 litre within a single hour"},
    # Social / settings
    "city_setter":    {"icon": "🌍", "name": "Local Weather",    "desc": "Set your city for weather integration"},
    "customizer":     {"icon": "🎨", "name": "Customizer",       "desc": "Change your goal or unit"},
}

ACHIEVEMENT_I18N: Dict[str, Dict[str, Dict[str, str]]] = {
    "es": {
        "first_sip": {"name": "Primer Sorbo", "desc": "Registra tu primera entrada de agua"},
        "first_goal": {"name": "Meta Cumplida", "desc": "Alcanza tu meta diaria por primera vez"},
        "early_bird": {"name": "Madrugador", "desc": "Registra agua antes de las 8:00"},
        "night_owl": {"name": "Nocturno", "desc": "Registra agua después de las 22:00"},
        "streak_3": {"name": "Racha de 3 Días", "desc": "Cumple la meta 3 días seguidos"},
        "streak_7": {"name": "Guerrero Semanal", "desc": "Racha de 7 días"},
        "streak_14": {"name": "Titán Quincenal", "desc": "Racha de 14 días"},
        "streak_30": {"name": "Maestro Mensual", "desc": "Racha de 30 días"},
        "streak_60": {"name": "Leyenda 60 Días", "desc": "Racha de 60 días"},
        "streak_100": {"name": "Rey de Hidratación", "desc": "Racha de 100 días"},
        "litre_5": {"name": "5 Litros", "desc": "Registra 5 litros en total"},
        "litre_10": {"name": "10 Litros", "desc": "Registra 10 litros en total"},
        "litre_50": {"name": "50 Litros", "desc": "Registra 50 litros en total"},
        "litre_100": {"name": "100 Litros", "desc": "Registra 100 litros en total"},
        "litre_365": {"name": "365 Litros", "desc": "Registra 365 litros en total"},
        "litre_1000": {"name": "1000 Litros", "desc": "Registra 1000 litros en total"},
        "overachiever": {"name": "Sobresaliente", "desc": "Supera 125% de tu meta diaria"},
        "double_goal": {"name": "Doble Meta", "desc": "Alcanza 200% de tu meta diaria"},
        "perfect_week": {"name": "Semana Perfecta", "desc": "Cumple la meta 7 días seguidos"},
        "five_logs": {"name": "Constante", "desc": "Registra agua 5 veces en un día"},
        "ten_logs": {"name": "Campeón de Registros", "desc": "Registra agua 10 veces en un día"},
        "morning_hero": {"name": "Héroe de la Mañana", "desc": "Registra 500ml antes de las 9:00"},
        "hydro_rush": {"name": "Hydro Rush", "desc": "Registra 1 litro en una sola hora"},
        "city_setter": {"name": "Clima Local", "desc": "Configura tu ciudad para integrar el clima"},
        "customizer": {"name": "Personalizador", "desc": "Cambia tu meta o unidad"},
    },
    "de": {
        "first_sip": {"name": "Erster Schluck", "desc": "Trage deinen ersten Wassereintrag ein"},
        "first_goal": {"name": "Zielstarter", "desc": "Erreiche dein Tagesziel zum ersten Mal"},
        "early_bird": {"name": "Frühaufsteher", "desc": "Trage Wasser vor 8 Uhr ein"},
        "night_owl": {"name": "Nachteule", "desc": "Trage Wasser nach 22 Uhr ein"},
        "streak_3": {"name": "3-Tage-Serie", "desc": "Erreiche das Ziel 3 Tage in Folge"},
        "streak_7": {"name": "Wochenkrieger", "desc": "7-Tage-Zielserie"},
        "streak_14": {"name": "Zwei-Wochen-Titan", "desc": "14-Tage-Zielserie"},
        "streak_30": {"name": "Monatsmeister", "desc": "30-Tage-Zielserie"},
        "streak_60": {"name": "60-Tage-Legende", "desc": "60-Tage-Zielserie"},
        "streak_100": {"name": "Hydration-König", "desc": "100-Tage-Zielserie"},
        "litre_5": {"name": "5 Liter", "desc": "5 Liter insgesamt eintragen"},
        "litre_10": {"name": "10 Liter", "desc": "10 Liter insgesamt eintragen"},
        "litre_50": {"name": "50 Liter", "desc": "50 Liter insgesamt eintragen"},
        "litre_100": {"name": "100 Liter", "desc": "100 Liter insgesamt eintragen"},
        "litre_365": {"name": "365 Liter", "desc": "365 Liter insgesamt eintragen"},
        "litre_1000": {"name": "1000 Liter", "desc": "1000 Liter insgesamt eintragen"},
        "overachiever": {"name": "Überflieger", "desc": "125% deines Tagesziels übertreffen"},
        "double_goal": {"name": "Doppelziel", "desc": "200% deines Tagesziels erreichen"},
        "perfect_week": {"name": "Perfekte Woche", "desc": "7 Tage in Folge Ziel erreicht"},
        "five_logs": {"name": "Konstant", "desc": "5 Wassereinträge an einem Tag"},
        "ten_logs": {"name": "Eintrags-Champion", "desc": "10 Wassereinträge an einem Tag"},
        "morning_hero": {"name": "Morgenheld", "desc": "500ml vor 9 Uhr eintragen"},
        "hydro_rush": {"name": "Hydro Rush", "desc": "1 Liter innerhalb einer Stunde eintragen"},
        "city_setter": {"name": "Lokales Wetter", "desc": "Stadt für Wetterintegration setzen"},
        "customizer": {"name": "Anpasser", "desc": "Ziel oder Einheit ändern"},
    },
    "fr": {
        "first_sip": {"name": "Premier Gorgée", "desc": "Enregistre ta première entrée d'eau"},
        "first_goal": {"name": "Objectif Atteint", "desc": "Atteins ton objectif pour la première fois"},
        "early_bird": {"name": "Lève-Tôt", "desc": "Enregistre de l'eau avant 8h"},
        "night_owl": {"name": "Oiseau de Nuit", "desc": "Enregistre de l'eau après 22h"},
        "streak_3": {"name": "Série 3 Jours", "desc": "Atteins l'objectif 3 jours d'affilée"},
        "streak_7": {"name": "Guerrier Hebdo", "desc": "Série d'objectif de 7 jours"},
        "streak_14": {"name": "Titan 2 Semaines", "desc": "Série d'objectif de 14 jours"},
        "streak_30": {"name": "Maître Mensuel", "desc": "Série d'objectif de 30 jours"},
        "streak_60": {"name": "Légende 60 Jours", "desc": "Série d'objectif de 60 jours"},
        "streak_100": {"name": "Roi de l'Hydratation", "desc": "Série d'objectif de 100 jours"},
        "litre_5": {"name": "5 Litres", "desc": "Enregistre 5 litres au total"},
        "litre_10": {"name": "10 Litres", "desc": "Enregistre 10 litres au total"},
        "litre_50": {"name": "50 Litres", "desc": "Enregistre 50 litres au total"},
        "litre_100": {"name": "100 Litres", "desc": "Enregistre 100 litres au total"},
        "litre_365": {"name": "365 Litres", "desc": "Enregistre 365 litres au total"},
        "litre_1000": {"name": "1000 Litres", "desc": "Enregistre 1000 litres au total"},
        "overachiever": {"name": "Surperformeur", "desc": "Dépasse 125% de l'objectif quotidien"},
        "double_goal": {"name": "Double Objectif", "desc": "Atteins 200% de l'objectif quotidien"},
        "perfect_week": {"name": "Semaine Parfaite", "desc": "Objectif atteint 7 jours de suite"},
        "five_logs": {"name": "Régulier", "desc": "Enregistre l'eau 5 fois en un jour"},
        "ten_logs": {"name": "Champion des Logs", "desc": "Enregistre l'eau 10 fois en un jour"},
        "morning_hero": {"name": "Héros du Matin", "desc": "Enregistre 500ml avant 9h"},
        "hydro_rush": {"name": "Hydro Rush", "desc": "Enregistre 1 litre en une heure"},
        "city_setter": {"name": "Météo Locale", "desc": "Définis ta ville pour la météo"},
        "customizer": {"name": "Personnaliseur", "desc": "Change ton objectif ou unité"},
    },
    "ru": {
        "first_sip": {"name": "Первый Глоток", "desc": "Сделай первую запись воды"},
        "first_goal": {"name": "Цель Взята", "desc": "Выполни дневную цель впервые"},
        "early_bird": {"name": "Ранняя Пташка", "desc": "Запиши воду до 8:00"},
        "night_owl": {"name": "Ночная Сова", "desc": "Запиши воду после 22:00"},
        "streak_3": {"name": "Серия 3 Дня", "desc": "Выполняй цель 3 дня подряд"},
        "streak_7": {"name": "Воин Недели", "desc": "Серия цели 7 дней"},
        "streak_14": {"name": "Титан 2 Недель", "desc": "Серия цели 14 дней"},
        "streak_30": {"name": "Мастер Месяца", "desc": "Серия цели 30 дней"},
        "streak_60": {"name": "Легенда 60 Дней", "desc": "Серия цели 60 дней"},
        "streak_100": {"name": "Король Гидрации", "desc": "Серия цели 100 дней"},
        "litre_5": {"name": "5 Литров", "desc": "Набери всего 5 литров"},
        "litre_10": {"name": "10 Литров", "desc": "Набери всего 10 литров"},
        "litre_50": {"name": "50 Литров", "desc": "Набери всего 50 литров"},
        "litre_100": {"name": "100 Литров", "desc": "Набери всего 100 литров"},
        "litre_365": {"name": "365 Литров", "desc": "Набери всего 365 литров"},
        "litre_1000": {"name": "1000 Литров", "desc": "Набери всего 1000 литров"},
        "overachiever": {"name": "Сверхцель", "desc": "Превысь 125% дневной цели"},
        "double_goal": {"name": "Двойная Цель", "desc": "Достигни 200% дневной цели"},
        "perfect_week": {"name": "Идеальная Неделя", "desc": "Выполняй цель 7 дней подряд"},
        "five_logs": {"name": "Стабильность", "desc": "Запиши воду 5 раз за день"},
        "ten_logs": {"name": "Чемпион Записей", "desc": "Запиши воду 10 раз за день"},
        "morning_hero": {"name": "Герой Утра", "desc": "Запиши 500мл до 9:00"},
        "hydro_rush": {"name": "Гидро Рывок", "desc": "Запиши 1 литр в течение часа"},
        "city_setter": {"name": "Локальная Погода", "desc": "Укажи город для погодных данных"},
        "customizer": {"name": "Кастомайзер", "desc": "Измени цель или единицу"},
    },
    "uk": {
        "first_sip": {"name": "Перший Ковток", "desc": "Зроби перший запис води"},
        "first_goal": {"name": "Ціль Взята", "desc": "Виконай денну мету вперше"},
        "early_bird": {"name": "Рання Пташка", "desc": "Запиши воду до 8:00"},
        "night_owl": {"name": "Нічна Сова", "desc": "Запиши воду після 22:00"},
        "streak_3": {"name": "Серія 3 Дні", "desc": "Виконуй мету 3 дні поспіль"},
        "streak_7": {"name": "Воїн Тижня", "desc": "Серія мети 7 днів"},
        "streak_14": {"name": "Титан 2 Тижнів", "desc": "Серія мети 14 днів"},
        "streak_30": {"name": "Майстер Місяця", "desc": "Серія мети 30 днів"},
        "streak_60": {"name": "Легенда 60 Днів", "desc": "Серія мети 60 днів"},
        "streak_100": {"name": "Король Гідрації", "desc": "Серія мети 100 днів"},
        "litre_5": {"name": "5 Літрів", "desc": "Набери загалом 5 літрів"},
        "litre_10": {"name": "10 Літрів", "desc": "Набери загалом 10 літрів"},
        "litre_50": {"name": "50 Літрів", "desc": "Набери загалом 50 літрів"},
        "litre_100": {"name": "100 Літрів", "desc": "Набери загалом 100 літрів"},
        "litre_365": {"name": "365 Літрів", "desc": "Набери загалом 365 літрів"},
        "litre_1000": {"name": "1000 Літрів", "desc": "Набери загалом 1000 літрів"},
        "overachiever": {"name": "Надціль", "desc": "Перевищ 125% денної мети"},
        "double_goal": {"name": "Подвійна Мета", "desc": "Досягни 200% денної мети"},
        "perfect_week": {"name": "Ідеальний Тиждень", "desc": "Виконуй мету 7 днів поспіль"},
        "five_logs": {"name": "Стабільність", "desc": "Запиши воду 5 разів за день"},
        "ten_logs": {"name": "Чемпіон Записів", "desc": "Запиши воду 10 разів за день"},
        "morning_hero": {"name": "Герой Ранку", "desc": "Запиши 500мл до 9:00"},
        "hydro_rush": {"name": "Гідро Ривок", "desc": "Запиши 1 літр протягом години"},
        "city_setter": {"name": "Локальна Погода", "desc": "Вкажи місто для погодних даних"},
        "customizer": {"name": "Кастомайзер", "desc": "Зміни мету або одиницю"},
    },
}

# ─────────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id                INTEGER PRIMARY KEY,
            language                   TEXT    DEFAULT 'en',
            unit                       TEXT    DEFAULT 'ml',
            weight_kg                  REAL    DEFAULT 70.0,
            activity_level             TEXT    DEFAULT 'moderate',
            city                       TEXT    DEFAULT '',
            timezone                   TEXT    DEFAULT 'UTC',
            daily_goal_ml              INTEGER DEFAULT 2000,
            reminder_interval_mins     INTEGER DEFAULT 60,
            reminders_enabled          INTEGER DEFAULT 1,
            quiet_start_hour           INTEGER DEFAULT 22,
            quiet_end_hour             INTEGER DEFAULT 7,
            skip_today                 INTEGER DEFAULT 0,
            streak_days                INTEGER DEFAULT 0,
            best_streak                INTEGER DEFAULT 0,
            total_ml_ever              INTEGER DEFAULT 0,
            achievements               TEXT    DEFAULT '[]',
            state                      TEXT    DEFAULT 'OB_WELCOME',
            is_premium                 INTEGER DEFAULT 0,
            premium_expiry             TEXT    DEFAULT '',
            trial_used                 INTEGER DEFAULT 0,
            trial_expiry               TEXT    DEFAULT '',
            subscription_active        INTEGER DEFAULT 0,
            dashboard_message_id       INTEGER DEFAULT 0,
            dashboard_chat_id          INTEGER DEFAULT 0,
            last_date_str              TEXT    DEFAULT '',
            log_amounts_json           TEXT    DEFAULT '[]',
            fixed_reminders            TEXT    DEFAULT '[]',
            snooze_until               TEXT    DEFAULT '',
            created_at                 TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            date_str  TEXT    NOT NULL,
            time_str  TEXT    NOT NULL,
            amount_ml INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(telegram_id)
        );
        CREATE INDEX IF NOT EXISTS idx_logs_user_date ON logs(user_id, date_str);
    """)
    # Safe migrations for existing DBs
    for sql in [
        "ALTER TABLE users ADD COLUMN trial_expiry TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN snooze_until TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN premium_expiry TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_reminded TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass
    conn.close()
    logger.info("DB ready: %s", DB_FILE)

# ─────────────────────────────────────────────────────────────────
#  DATA MODELS
# ─────────────────────────────────────────────────────────────────

@dataclass
class FixedReminder:
    hour: int
    minute: int
    enabled: bool = True

    def label(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def to_dict(self) -> dict:
        return {"hour": self.hour, "minute": self.minute, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "FixedReminder":
        return cls(hour=d["hour"], minute=d["minute"], enabled=d.get("enabled", True))


@dataclass
class UserProfile:
    telegram_id: int = 0
    language: str = "en"
    unit: str = "ml"
    weight_kg: float = 70.0
    activity_level: str = "moderate"
    city: str = ""
    timezone: str = DEFAULT_TZ
    daily_goal_ml: int = 2000
    reminder_interval_mins: int = 60
    reminders_enabled: bool = True
    quiet_start_hour: int = 22
    quiet_end_hour: int = 7
    skip_today: bool = False
    streak_days: int = 0
    best_streak: int = 0
    total_ml_ever: int = 0
    achievements: List[str] = field(default_factory=list)
    state: State = State.OB_LANGUAGE
    is_premium: bool = False
    premium_expiry: str = ""          # "lifetime" or date str
    trial_used: bool = False
    trial_expiry: str = ""            # date str
    subscription_active: bool = False
    dashboard_message_id: int = 0
    dashboard_chat_id: int = 0
    last_date_str: str = ""
    log_amounts: List[int] = field(default_factory=list)
    fixed_reminders: List[FixedReminder] = field(default_factory=list)
    snooze_until: str = ""
    last_reminded: str = ""  # ISO timestamp of last reminder sent

    def fmt(self, ml: int) -> str:
        """Format ml value with correct unit, always compact."""
        if self.unit == "oz":
            val = ml / 29.5735
            return f"{val:.0f}oz"
        return f"{ml}ml"

    def fmt_goal(self) -> str:
        return self.fmt(self.daily_goal_ml)

    def favourite_amounts(self) -> List[int]:
        if not self.log_amounts:
            return [200, 350, 500]
        counts = Counter(self.log_amounts[-40:])
        top = [amt for amt, _ in counts.most_common(3)]
        for d in [200, 350, 500]:
            if d not in top and len(top) < 3:
                top.append(d)
        return sorted(top[:3])

    @property
    def feature_smart_reminders(self) -> bool:
        return is_premium_active(self)

    @property
    def feature_weather(self) -> bool:
        return is_premium_active(self) and bool(self.city)

    @property
    def feature_catchup(self) -> bool:
        return is_premium_active(self)

    @property
    def feature_weekly_report(self) -> bool:
        return is_premium_active(self)


# ─────────────────────────────────────────────────────────────────
#  DB ACCESS
# ─────────────────────────────────────────────────────────────────

_profile_cache: Dict[int, UserProfile] = {}


def load_profile(telegram_id: int) -> UserProfile:
    if telegram_id in _profile_cache:
        return _profile_cache[telegram_id]
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    if not row:
        p = UserProfile(telegram_id=telegram_id)
        save_profile(p)
        _profile_cache[telegram_id] = p
        logger.info("New user: %d", telegram_id)
        return p

    def _get(key: str, default=None):
        try:
            return row[key] if row[key] is not None else default
        except (IndexError, KeyError):
            return default

    state_name = _get("state", "OB_LANGUAGE")
    if state_name not in State.__members__:
        state_name = "OB_LANGUAGE"

    p = UserProfile(
        telegram_id=telegram_id,
        language=_get("language", "en"),
        unit=_get("unit", "ml"),
        weight_kg=_get("weight_kg", 70.0),
        activity_level=_get("activity_level", "moderate"),
        city=_get("city", "") or "",
        timezone=_get("timezone", DEFAULT_TZ) or DEFAULT_TZ,
        daily_goal_ml=_get("daily_goal_ml", 2000),
        reminder_interval_mins=_get("reminder_interval_mins", 60),
        reminders_enabled=bool(_get("reminders_enabled", 1)),
        quiet_start_hour=_get("quiet_start_hour", 22),
        quiet_end_hour=_get("quiet_end_hour", 7),
        skip_today=bool(_get("skip_today", 0)),
        streak_days=_get("streak_days", 0),
        best_streak=_get("best_streak", 0),
        total_ml_ever=_get("total_ml_ever", 0),
        achievements=json.loads(_get("achievements", "[]") or "[]"),
        state=State[state_name],
        is_premium=bool(_get("is_premium", 0)),
        premium_expiry=_get("premium_expiry", "") or "",
        trial_used=bool(_get("trial_used", 0)),
        trial_expiry=_get("trial_expiry", "") or "",
        subscription_active=bool(_get("subscription_active", 0)),
        dashboard_message_id=_get("dashboard_message_id", 0),
        dashboard_chat_id=_get("dashboard_chat_id", 0),
        last_date_str=_get("last_date_str", "") or "",
        log_amounts=json.loads(_get("log_amounts_json", "[]") or "[]"),
        fixed_reminders=[FixedReminder.from_dict(d) for d in json.loads(_get("fixed_reminders", "[]") or "[]")],
        snooze_until=_get("snooze_until", "") or "",
        last_reminded=_get("last_reminded", "") or "",
    )
    _profile_cache[telegram_id] = p
    return p


def save_profile(p: UserProfile) -> None:
    conn = db_connect()
    conn.execute("""
        INSERT INTO users (
            telegram_id, language, unit, weight_kg, activity_level, city, timezone,
            daily_goal_ml, reminder_interval_mins, reminders_enabled, quiet_start_hour,
            quiet_end_hour, skip_today, streak_days, best_streak, total_ml_ever,
            achievements, state, is_premium, premium_expiry, trial_used, trial_expiry,
            subscription_active, dashboard_message_id, dashboard_chat_id,
            last_date_str, log_amounts_json, fixed_reminders, snooze_until, last_reminded
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            language=excluded.language, unit=excluded.unit, weight_kg=excluded.weight_kg,
            activity_level=excluded.activity_level, city=excluded.city, timezone=excluded.timezone,
            daily_goal_ml=excluded.daily_goal_ml,
            reminder_interval_mins=excluded.reminder_interval_mins,
            reminders_enabled=excluded.reminders_enabled,
            quiet_start_hour=excluded.quiet_start_hour, quiet_end_hour=excluded.quiet_end_hour,
            skip_today=excluded.skip_today, streak_days=excluded.streak_days,
            best_streak=excluded.best_streak, total_ml_ever=excluded.total_ml_ever,
            achievements=excluded.achievements, state=excluded.state,
            is_premium=excluded.is_premium, premium_expiry=excluded.premium_expiry,
            trial_used=excluded.trial_used, trial_expiry=excluded.trial_expiry,
            subscription_active=excluded.subscription_active,
            dashboard_message_id=excluded.dashboard_message_id,
            dashboard_chat_id=excluded.dashboard_chat_id, last_date_str=excluded.last_date_str,
            log_amounts_json=excluded.log_amounts_json, fixed_reminders=excluded.fixed_reminders,
            snooze_until=excluded.snooze_until, last_reminded=excluded.last_reminded
    """, (
        p.telegram_id, p.language, p.unit, p.weight_kg, p.activity_level, p.city, p.timezone,
        p.daily_goal_ml, p.reminder_interval_mins, int(p.reminders_enabled),
        p.quiet_start_hour, p.quiet_end_hour, int(p.skip_today),
        p.streak_days, p.best_streak, p.total_ml_ever,
        json.dumps(p.achievements), p.state.name, int(p.is_premium),
        p.premium_expiry, int(p.trial_used), p.trial_expiry,
        int(p.subscription_active), p.dashboard_message_id, p.dashboard_chat_id,
        p.last_date_str, json.dumps(p.log_amounts[-40:]),
        json.dumps([fr.to_dict() for fr in p.fixed_reminders]),
        p.snooze_until, p.last_reminded,
    ))
    conn.commit()
    conn.close()


def get_day_ml(uid: int, date_str: str) -> int:
    conn = db_connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_ml),0) as t FROM logs WHERE user_id=? AND date_str=?",
        (uid, date_str)
    ).fetchone()
    conn.close()
    return row["t"] if row else 0


def get_day_entry_count(uid: int, date_str: str) -> int:
    conn = db_connect()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM logs WHERE user_id=? AND date_str=?",
        (uid, date_str)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def get_day_entries(uid: int, date_str: str) -> List[Tuple[str, int]]:
    conn = db_connect()
    rows = conn.execute(
        "SELECT time_str, amount_ml FROM logs WHERE user_id=? AND date_str=? ORDER BY rowid",
        (uid, date_str)
    ).fetchall()
    conn.close()
    return [(r["time_str"], r["amount_ml"]) for r in rows]


def insert_log(uid: int, date_str: str, time_str: str, ml: int) -> None:
    conn = db_connect()
    conn.execute("INSERT INTO logs (user_id,date_str,time_str,amount_ml) VALUES (?,?,?,?)",
                 (uid, date_str, time_str, ml))
    conn.commit()
    conn.close()


def undo_last_log(uid: int, date_str: str) -> Optional[int]:
    conn = db_connect()
    row = conn.execute(
        "SELECT id,amount_ml FROM logs WHERE user_id=? AND date_str=? ORDER BY id DESC LIMIT 1",
        (uid, date_str)
    ).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("DELETE FROM logs WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return row["amount_ml"]


def get_history_totals(uid: int, days: int = 90) -> Dict[str, int]:
    conn = db_connect()
    rows = conn.execute(
        "SELECT date_str, SUM(amount_ml) as t FROM logs WHERE user_id=? "
        "GROUP BY date_str ORDER BY date_str DESC LIMIT ?",
        (uid, days)
    ).fetchall()
    conn.close()
    return {r["date_str"]: r["t"] for r in rows}


def clear_day(uid: int, date_str: str) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM logs WHERE user_id=? AND date_str=?", (uid, date_str))
    conn.commit()
    conn.close()


def clear_all_logs(uid: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM logs WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()


def all_active_users() -> List[int]:
    conn = db_connect()
    rows = conn.execute("SELECT telegram_id FROM users WHERE state='IDLE'").fetchall()
    conn.close()
    return [r["telegram_id"] for r in rows]


# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────

def get_tz(name: str) -> pytz.BaseTzInfo:
    try:
        return pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        return pytz.utc


def today_str(tz: pytz.BaseTzInfo) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


def now_hhmm(tz: pytz.BaseTzInfo) -> str:
    return datetime.now(tz).strftime("%H:%M")


def now_date_label(p: UserProfile, tz: pytz.BaseTzInfo) -> str:
    """Localized short date label, e.g. Thu 19 Feb 2026."""
    dt = datetime.now(tz)
    lang = lang_code(p)
    days = {
        "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "es": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
        "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
        "fr": ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"],
        "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "uk": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"],
    }
    months = {
        "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "es": ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
        "de": ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"],
        "fr": ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Aoû", "Sep", "Oct", "Nov", "Déc"],
        "ru": ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"],
        "uk": ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"],
    }
    d = days.get(lang, days["en"])[dt.weekday()]
    m = months.get(lang, months["en"])[dt.month - 1]
    return f"{d} {dt.day:02d} {m} {dt.year}"


def s(p: UserProfile, key: str, **kw) -> str:
    lang = p.language if p.language in STRINGS else "en"
    tmpl = STRINGS[lang].get(key, STRINGS["en"].get(key, key))
    return tmpl.format(**kw) if kw else tmpl


def get_tip(p: UserProfile) -> str:
    lang = p.language if p.language in TIPS else "en"
    return random.choice(TIPS[lang])


def calc_goal(kg: float, activity: str) -> int:
    base = kg * 35
    mult = ACTIVITY_LEVELS.get(activity, ACTIVITY_LEVELS["moderate"])["mult"]
    return int(round(base * mult / 50) * 50)


def pbar(current: int, total: int, length: int = 10) -> str:
    pct = min(1.0, current / max(1, total))
    filled = int(round(pct * length))
    return "█" * filled + "░" * (length - filled)


def mins_label(mins: int) -> str:
    h, m = divmod(mins, 60)
    if h and m: return f"{h}h{m}m"
    return f"{h}h" if h else f"{m}m"


def is_premium_active(p: UserProfile) -> bool:
    """True if user has active premium or trial."""
    if p.is_premium and p.premium_expiry == "lifetime":
        return True
    # Check trial expiry
    expiry_str = p.trial_expiry or p.premium_expiry
    if expiry_str and expiry_str not in ("", "lifetime"):
        try:
            return datetime.utcnow() < datetime.strptime(expiry_str, "%Y-%m-%d")
        except ValueError:
            pass
    return False


def trial_days_left(p: UserProfile) -> int:
    """Returns days left on trial (0 if not in trial or expired)."""
    if p.is_premium and p.premium_expiry == "lifetime":
        return 0
    exp = p.trial_expiry or p.premium_expiry
    if exp and exp != "lifetime":
        try:
            diff = (datetime.strptime(exp, "%Y-%m-%d") - datetime.utcnow()).days + 1
            return max(0, diff)
        except ValueError:
            pass
    return 0


def is_snoozed(p: UserProfile) -> bool:
    if not p.snooze_until:
        return False
    try:
        until = datetime.fromisoformat(p.snooze_until)
        if datetime.utcnow() < until:
            return True
        p.snooze_until = ""
        return False
    except ValueError:
        p.snooze_until = ""
        return False


def is_quiet(p: UserProfile) -> bool:
    tz = get_tz(p.timezone)
    h = datetime.now(tz).hour
    qs, qe = p.quiet_start_hour, p.quiet_end_hour
    if qs == qe:
        return False
    if qs > qe:
        return h >= qs or h < qe
    else:
        return qs <= h < qe


def run_reset(p: UserProfile) -> bool:
    """Resets daily state if date has changed. Returns True if reset happened."""
    tz = get_tz(p.timezone)
    today = today_str(tz)
    if not p.last_date_str:
        p.last_date_str = today
        save_profile(p)
        return False
    if p.last_date_str == today:
        return False
    prev_ml = get_day_ml(p.telegram_id, p.last_date_str)
    if prev_ml >= p.daily_goal_ml:
        p.streak_days += 1
        p.best_streak = max(p.best_streak, p.streak_days)
        _check_streak_ach(p)
    else:
        p.streak_days = 0
    p.last_date_str = today
    p.skip_today = False
    save_profile(p)
    return True


def _check_streak_ach(p: UserProfile) -> None:
    for key, thresh in [("streak_3", 3), ("streak_7", 7), ("streak_14", 14),
                        ("streak_30", 30), ("streak_60", 60), ("streak_100", 100)]:
        if p.streak_days >= thresh and key not in p.achievements:
            p.achievements.append(key)
    # Perfect week: 7 consecutive
    if p.streak_days == 7 and "perfect_week" not in p.achievements:
        p.achievements.append("perfect_week")


def check_log_ach(p: UserProfile, today: str, tz: pytz.BaseTzInfo) -> List[str]:
    earned: List[str] = []
    day_ml = get_day_ml(p.telegram_id, today)
    entry_count = get_day_entry_count(p.telegram_id, today)
    hour = datetime.now(tz).hour

    def grant(k: str) -> None:
        if k not in p.achievements:
            p.achievements.append(k)
            earned.append(k)

    if p.total_ml_ever > 0:             grant("first_sip")
    if day_ml >= p.daily_goal_ml:
        grant("first_goal")
        grant("day_complete") if "day_complete" not in ACHIEVEMENTS else None
    if day_ml >= p.daily_goal_ml * 1.25: grant("overachiever")
    if day_ml >= p.daily_goal_ml * 2.0:  grant("double_goal")
    if hour < 8:                          grant("early_bird")
    if hour >= 22:                        grant("night_owl")
    if entry_count >= 5:                  grant("five_logs")
    if entry_count >= 10:                 grant("ten_logs")

    # Morning hero: 500ml before 9am
    if hour < 9:
        entries_today = get_day_entries(p.telegram_id, today)
        morning_total = sum(ml for t, ml in entries_today if int(t.split(":")[0]) < 9)
        if morning_total >= 500:
            grant("morning_hero")

    tl = p.total_ml_ever / 1000
    for k, thr in [("litre_5", 5), ("litre_10", 10), ("litre_50", 50),
                   ("litre_100", 100), ("litre_365", 365), ("litre_1000", 1000)]:
        if tl >= thr:
            grant(k)

    if p.city:
        grant("city_setter")

    return earned


# ─────────────────────────────────────────────────────────────────
#  WEATHER
# ─────────────────────────────────────────────────────────────────

_weather_cache: Dict[str, Tuple[int, float, str, datetime]] = {}


def get_weather(city: str) -> Tuple[int, float, str]:
    if not city:
        return 0, 0.0, ""
    cached = _weather_cache.get(city.lower())
    if cached and (datetime.utcnow() - cached[3]).seconds < 1800:
        return cached[0], cached[1], cached[2]
    try:
        # Add UK suffix for British cities to avoid US duplicates
        city_query = city
        if city.lower() in ["bristol", "london", "manchester", "birmingham", "leeds", "glasgow", "liverpool"]:
            city_query = city + ",UK"
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city_query, "appid": OWM_API_KEY, "units": "metric"},
            timeout=5
        )
        data = r.json()
        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"].capitalize()
        if temp >= 35:   bonus = 600
        elif temp >= 30: bonus = 400
        elif temp >= 25: bonus = 200
        else:            bonus = 0
        _weather_cache[city.lower()] = (bonus, temp, desc, datetime.utcnow())
        return bonus, temp, desc
    except Exception:
        return 0, 0.0, ""


# ─────────────────────────────────────────────────────────────────
#  TEXT CHARTS
# ─────────────────────────────────────────────────────────────────

def text_chart(p: UserProfile, days_back: int) -> str:
    """
    Horizontal bar chart. Each row fits on one Telegram line.
    Amounts are compact (no space between number and unit).
    """
    tz = get_tz(p.timezone)
    now = datetime.now(tz)
    dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back - 1, -1, -1)]
    history = get_history_totals(p.telegram_id, days_back + 5)
    values = [history.get(d, 0) for d in dates]
    goal = p.daily_goal_ml

    lang = lang_code(p)
    titles = {
        "en": ("📊  7-Day Chart", "📅  30-Day Chart"),
        "es": ("📊  Gráfico 7 días", "📅  Gráfico 30 días"),
        "de": ("📊  7-Tage-Chart", "📅  30-Tage-Chart"),
        "fr": ("📊  Graphique 7 jours", "📅  Graphique 30 jours"),
        "ru": ("📊  График 7 дней", "📅  График 30 дней"),
        "uk": ("📊  Графік 7 днів", "📅  Графік 30 днів"),
    }[lang]
    title = titles[0] if days_back == 7 else titles[1]
    max_val = max(max(values) if values else 0, goal, 1)
    BAR_LEN = 10  # Keep bar shorter so amount fits on same line

    total = sum(values)
    goals_hit = sum(1 for v in values if v >= goal)
    avg = total // days_back if days_back else 0
    best = max(values) if values else 0
    best_idx = values.index(best) if best else 0

    goal_lbl = {"en": "goal", "es": "meta", "de": "ziel", "fr": "objectif", "ru": "цель", "uk": "мета"}[lang]
    lines = [f"<b>{title}</b>  {goal_lbl}:{p.fmt(goal)}", ""]

    for i, (d, v) in enumerate(zip(dates, values)):
        dt = datetime.strptime(d, "%Y-%m-%d")
        label = dt.strftime("%a%d") if days_back == 7 else dt.strftime("%d%b")
        filled = int(round(v / max_val * BAR_LEN))
        empty  = BAR_LEN - filled

        if v >= goal:
            bar = "█" * filled + "░" * empty
            status = "✅"
        elif v > 0:
            bar = "▓" * filled + "░" * empty
            status = "·"
        else:
            bar = "░" * BAR_LEN
            status = "○"

        # Compact amount — no space between number and unit
        amt = p.fmt(v) if v > 0 else "—"
        # Right-pad amount to fixed 6 chars max to keep alignment
        amt_str = amt[:7].ljust(7)

        lines.append(f"<code>{label} {bar} {amt_str}{status}</code>")

    # Goal marker ruler
    goal_pos = int(round(goal / max_val * BAR_LEN))
    ruler = "      " + "·" * goal_pos + "▲" + "·" * (BAR_LEN - goal_pos)
    lines.append(f"<code>{ruler} {goal_lbl}</code>")
    lines.append("")

    # Summary block
    summary_lbl = {"en": "Summary", "es": "Resumen", "de": "Übersicht", "fr": "Résumé", "ru": "Итог", "uk": "Підсумок"}[lang]
    total_lbl = {"en": "Total", "es": "Total", "de": "Gesamt", "fr": "Total", "ru": "Всего", "uk": "Всього"}[lang]
    avg_lbl = {"en": "Avg/day", "es": "Prom/día", "de": "Ø/Tag", "fr": "Moy/jour", "ru": "Ср/день", "uk": "Сер/день"}[lang]
    goals_lbl = {"en": "Goals", "es": "Metas", "de": "Ziele", "fr": "Objectifs", "ru": "Цели", "uk": "Цілі"}[lang]
    best_lbl = {"en": "Best", "es": "Mejor", "de": "Best", "fr": "Meilleur", "ru": "Лучший", "uk": "Кращий"}[lang]
    trend_lbl = {"en": "Trend", "es": "Tendencia", "de": "Trend", "fr": "Tendance", "ru": "Тренд", "uk": "Тренд"}[lang]
    trend_up = {"en": "📈 up", "es": "📈 sube", "de": "📈 hoch", "fr": "📈 hausse", "ru": "📈 вверх", "uk": "📈 вгору"}[lang]
    trend_down = {"en": "📉 down", "es": "📉 baja", "de": "📉 runter", "fr": "📉 baisse", "ru": "📉 вниз", "uk": "📉 вниз"}[lang]
    trend_flat = {"en": "➡️ steady", "es": "➡️ estable", "de": "➡️ stabil", "fr": "➡️ stable", "ru": "➡️ ровно", "uk": "➡️ рівно"}[lang]

    lines.append(f"┌── <b>{summary_lbl}</b>")
    lines.append(f"│ {total_lbl:<8} <b>{p.fmt(total)}</b>")
    lines.append(f"│ {avg_lbl:<8} <b>{p.fmt(avg)}</b>")
    lines.append(f"│ {goals_lbl:<8} <b>{goals_hit}/{days_back}</b>")

    if best > 0:
        best_dt = datetime.strptime(dates[best_idx], "%Y-%m-%d")
        best_label = best_dt.strftime("%a %d %b") if days_back <= 7 else best_dt.strftime("%d %b")
        lines.append(f"│ {best_lbl:<8} <b>{p.fmt(best)}</b>  {best_label}")

    if days_back >= 6:
        half = days_back // 2
        first_avg = sum(values[:half]) // half if half else 0
        second_avg = sum(values[half:]) // (days_back - half) if (days_back - half) else 0
        if second_avg > first_avg:   trend = trend_up
        elif second_avg < first_avg: trend = trend_down
        else:                        trend = trend_flat
        lines.append(f"│ {trend_lbl:<8} <b>{trend}</b>")

    lines.append("└────────────────")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
#  ACCOUNT BADGE HELPERS
# ─────────────────────────────────────────────────────────────────

def account_badge(p: UserProfile) -> str:
    """Returns a compact badge string for the account type."""
    if p.is_premium and p.premium_expiry == "lifetime":
        return s(p, "premium_account")
    d = trial_days_left(p)
    if d > 0:
        return s(p, "trial_account", days=d)
    return s(p, "free_account")


def reminder_times_text(p: UserProfile, tz: pytz.BaseTzInfo) -> str:
    lang = lang_code(p)
    now = datetime.now(tz)
    
    if p.last_reminded:
        try:
            last_dt = datetime.fromisoformat(p.last_reminded)
            last_str = last_dt.strftime("%H:%M")
        except ValueError:
            last_labels = {
                "en": "not yet",
                "es": "aún no",
                "de": "noch nicht",
                "fr": "pas encore",
                "ru": "пока нет",
                "uk": "ще ні",
            }
            last_str = last_labels.get(lang, last_labels["en"])
    else:
        last_labels = {
            "en": "not yet",
            "es": "aún no",
            "de": "noch nicht",
            "fr": "pas encore",
            "ru": "пока нет",
            "uk": "ще ні",
        }
        last_str = last_labels.get(lang, last_labels["en"])
    
    next_min = (now.hour * 60 + now.minute) + p.reminder_interval_mins
    next_h = (next_min // 60) % 24
    next_m = next_min % 24
    next_str = f"{next_h:02d}:{next_m:02d}"
    
    last_labels = {
        "en": "Last reminder",
        "es": "Último recordatorio",
        "de": "Letzte Erinnerung",
        "fr": "Dernier rappel",
        "ru": "Последнее напоминание",
        "uk": "Останнє нагадування",
    }
    next_labels = {
        "en": "Next reminder",
        "es": "Próximo recordatorio",
        "de": "Nächste Erinnerung",
        "fr": "Prochain rappel",
        "ru": "Следующее напоминание",
        "uk": "Наступне нагадування",
    }
    
    last_lbl = last_labels.get(lang, last_labels["en"])
    next_lbl = next_labels.get(lang, next_labels["en"])
    
    return f"   {last_lbl}: {last_str}\n   {next_lbl}: {next_str}"


def account_header_line(p: UserProfile) -> str:
    """Status first, then bot title with a single constant icon."""
    badge = account_badge(p)
    return f"{badge}\n\n💧 <b>AquaBot</b>"


# ─────────────────────────────────────────────────────────────────
#  TEXT BUILDERS
# ─────────────────────────────────────────────────────────────────

def home_text(p: UserProfile, today: str) -> str:
    tz = get_tz(p.timezone)
    drank = get_day_ml(p.telegram_id, today)
    goal = p.daily_goal_ml
    remaining = max(0, goal - drank)
    pct = min(100, int(drank / max(1, goal) * 100))
    streak_icon = "🔥" if p.streak_days >= 1 else "💤"
    best_icon = "🏆"

    weather_line = ""
    if p.feature_weather:
        bonus, temp, desc = get_weather(p.city)
        if desc:
            weather_line = f"\n🌤 {p.city}: {temp:.0f}°C, {desc}"
            if bonus:
                weather_line += f"  (+{p.fmt(bonus)} suggested)"

    skip_note = "\n😴 <i>Reminders paused for today</i>" if p.skip_today else ""
    rem_status = "" if p.reminders_enabled else f"  · {ui(p, 'line_off')}"

    last_reminder_line = ""
    if p.reminders_enabled and not p.skip_today:
        last_reminder_line = reminder_times_text(p, tz)

    year_str = datetime.now(tz).strftime("%Y")
    lang = lang_code(p)
    lines = [
        f"{pbar(drank, goal)}  <b>{pct}%</b>",
        f"<b>{p.fmt(drank)}</b> / {p.fmt_goal()}"
        + (f"  ·  {ui(p, 'line_remaining', remaining=p.fmt(remaining))}" if remaining else f"  {ui(p, 'line_done')}"),
        "",
        account_badge(p),
        f"💧 <b>AquaBot</b>  ·  {year_str}",
        "",
        f"{streak_icon} <b>{ui(p, 'line_streak', days=p.streak_days)}</b>",
        f"{best_icon} <b>{ui(p, 'line_best', days=p.best_streak)}</b>",
        "",
        f"⏰ {ui(p, 'line_every', interval=mins_label(p.reminder_interval_mins))}{rem_status}",
    ]
    if last_reminder_line:
        lines.append(last_reminder_line)
    if weather_line:
        lines.append(weather_line)
    if skip_note:
        lines.append(skip_note)
    return "\n".join(lines)


def t(p: UserProfile, key: str, default: str = "") -> str:
    lang = STRINGS.get(p.language, STRINGS["en"])
    return lang.get(key, STRINGS["en"].get(key, default or key))


UI_STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "line_remaining": "{remaining} left",
        "line_done": "✅ Done!",
        "line_streak": "Streak: {days} days",
        "line_best": "Best: {days} days",
        "line_every": "Reminder every {interval}",
        "line_off": "🔕 OFF",
        "choose_language": "🌍 <b>Choose your language:</b>",
        "settings_title": "⚙️ <b>Settings</b>",
        "settings_goal": "Goal",
        "settings_weight": "Weight",
        "settings_activity": "Activity",
        "settings_unit": "Unit",
        "settings_language": "Language",
        "settings_timezone": "Timezone",
        "settings_city": "City",
        "settings_plan": "Plan",
        "settings_not_set": "not set",
        "settings_tz_hint": "Use /settz Region/City to change timezone",
        "stats_title": "📊 <b>Statistics</b>",
        "stats_today": "Today",
        "stats_week": "This Week",
        "stats_month": "This Month",
        "stats_all_time": "All Time",
        "stats_goals": "{n}/7 goals",
        "stats_total": "Total",
        "stats_avg": "Avg",
        "stats_badges": "badges",
        "history_title": "📂 <b>History</b>",
        "history_empty": "Nothing yet — start logging!",
        "history_subtitle": "(last 14 days)",
        "rem_on": "ON ✅",
        "rem_off": "OFF ❌",
        "rem_interval": "⏱ Interval: every <b>{interval}</b>\n   How often you get reminded to drink water",
        "rem_quiet": "🌙 Quiet hours: <b>{start} → {end}</b>\n   No reminders during this time (e.g. nighttime)",
        "rem_fixed": "📌 Fixed times: <b>{fixed}</b>\n   Remind at specific times of day",
        "rem_none": "none",
        "snoozed_until": "⏸ <i>Snoozed until {time} UTC</i>",
        "log_removed": "removed",
        "nothing_undo": "Nothing to undo",
        "custom_log_prompt": "💧 <b>Log water</b>\n\n✏️ Type your amount in ml (or oz if you use oz)\ne.g. <code>330</code>",
        "custom_log_err": "⚠️ <i>Enter a number between 1 and 5000</i>",
        "fixed_time_prompt": "📌 <b>Type the time as HH:MM</b>  e.g. <code>08:30</code>",
        "fixed_time_err": "⚠️ <i>Format: HH:MM  e.g. <code>08:30</code></i>",
        "recalc_prompt": "⚖️ <b>Enter your current weight in kg</b>  e.g. <code>75</code>\n\nI will recalculate your daily water goal.",
        "choose_day_delete": "🗑️ <b>Select a day to delete:</b>",
        "day_deleted": "<i>{day} deleted.</i>",
        "today_cleared": "✅ Today's intake cleared.",
        "all_wiped": "✅ All history wiped.",
        "no_history_delete": "No history to delete.",
        "no_fixed_remove": "No fixed reminders to remove.",
        "wipe_all_confirm": "⚠️ <b>Wipe ALL drinking history?</b>\n\nThis will permanently delete all your logged intake, streaks, and achievements.\nYour account, settings, and goal will be kept.\n\n<b>This cannot be undone.</b>",
        "delete_account_confirm": "⚠️ <b>Delete your account entirely?</b>\n\nThis removes <b>everything</b>: all logs, your settings, goal, streak, achievements.\nYou will be back to a blank slate. Send /start to sign up again.\n\n<b>This cannot be undone.</b>",
        "account_deleted": "✅ Account deleted. All your data has been removed.\n\nSend /start to create a fresh account anytime.",
        "account_deleting": "⏳ Deleting account...",
        "choose_language_short": "🌍 <b>Choose language:</b>",
        "trial_used": "You have already used your free trial.",
        "payment_open_error": "Could not open payment. Please try again.",
        "setup_complete": "✅ <b>Setup complete!</b>",
        "trial_cta": "Tap ⭐ <b>Premium</b> to start your free 3-day trial.",
        "export_caption": "📤 Your AquaBot data export. Keep this file safe!",
    },
    "es": {
        "line_remaining": "faltan {remaining}",
        "line_done": "✅ Hecho",
        "line_streak": "Racha: {days} días",
        "line_best": "Mejor: {days} días",
        "line_every": "Recordatorio cada {interval}",
        "line_off": "🔕 OFF",
        "choose_language": "🌍 <b>Elige tu idioma:</b>",
        "settings_title": "⚙️ <b>Ajustes</b>",
        "settings_goal": "Meta",
        "settings_weight": "Peso",
        "settings_activity": "Actividad",
        "settings_unit": "Unidad",
        "settings_language": "Idioma",
        "settings_timezone": "Zona horaria",
        "settings_city": "Ciudad",
        "settings_plan": "Plan",
        "settings_not_set": "sin definir",
        "settings_tz_hint": "Usa /settz Región/Ciudad para cambiar la zona horaria",
        "stats_title": "📊 <b>Estadísticas</b>",
        "stats_today": "Hoy",
        "stats_week": "Esta semana",
        "stats_month": "Este mes",
        "stats_all_time": "Todo el tiempo",
        "stats_goals": "{n}/7 metas",
        "stats_total": "Total",
        "stats_avg": "Promedio",
        "stats_badges": "insignias",
        "history_title": "📂 <b>Historial</b>",
        "history_empty": "Aún no hay datos. Empieza a registrar.",
        "history_subtitle": "(últimos 14 días)",
        "rem_on": "ON ✅",
        "rem_off": "OFF ❌",
        "rem_interval": "⏱ Intervalo: cada <b>{interval}</b>\n   Con qué frecuencia te recuerda beber agua",
        "rem_quiet": "🌙 Horas silenciosas: <b>{start} → {end}</b>\n   Sin recordatorios durante este tiempo (ej. noche)",
        "rem_fixed": "📌 Horas fijas: <b>{fixed}</b>\n   Recordatorios a horas específicas del día",
        "rem_none": "ninguna",
        "snoozed_until": "⏸ <i>Pospuesto hasta {time} UTC</i>",
        "log_removed": "eliminado",
        "nothing_undo": "Nada que deshacer",
        "custom_log_prompt": "💧 <b>Registrar agua</b>\n\n✏️ Escribe la cantidad en ml (u oz si usas oz)\nej. <code>330</code>",
        "custom_log_err": "⚠️ <i>Introduce un número entre 1 y 5000</i>",
        "fixed_time_prompt": "📌 <b>Escribe la hora en HH:MM</b>  ej. <code>08:30</code>",
        "fixed_time_err": "⚠️ <i>Formato: HH:MM  ej. <code>08:30</code></i>",
        "recalc_prompt": "⚖️ <b>Escribe tu peso actual en kg</b>  ej. <code>75</code>\n\nRecalcularé tu meta diaria.",
        "choose_day_delete": "🗑️ <b>Elige un día para borrar:</b>",
        "day_deleted": "<i>{day} eliminado.</i>",
        "today_cleared": "✅ Registro de hoy borrado.",
        "all_wiped": "✅ Historial borrado completo.",
        "no_history_delete": "No hay historial para borrar.",
        "no_fixed_remove": "No hay recordatorios fijos para eliminar.",
        "wipe_all_confirm": "⚠️ <b>¿Borrar TODO el historial?</b>\n\nEsto eliminará para siempre tus registros, racha y logros.\nTu cuenta, ajustes y meta se conservarán.\n\n<b>No se puede deshacer.</b>",
        "delete_account_confirm": "⚠️ <b>¿Eliminar tu cuenta por completo?</b>\n\nSe borrará <b>todo</b>: registros, ajustes, meta, racha y logros.\nVolverás a empezar desde cero. Envía /start para crearla de nuevo.\n\n<b>No se puede deshacer.</b>",
        "account_deleted": "✅ Cuenta eliminada. Todos tus datos se borraron.\n\nEnvía /start para crear una cuenta nueva.",
        "account_deleting": "⏳ Eliminando cuenta...",
        "choose_language_short": "🌍 <b>Elige idioma:</b>",
        "trial_used": "Ya usaste tu prueba gratuita.",
        "payment_open_error": "No se pudo abrir el pago. Inténtalo de nuevo.",
        "setup_complete": "✅ <b>¡Configuración completa!</b>",
        "trial_cta": "Pulsa ⭐ <b>Premium</b> para iniciar tu prueba gratis de 3 días.",
        "export_caption": "📤 Exportación de datos de AquaBot. Guarda este archivo.",
    },
    "de": {
        "line_remaining": "{remaining} übrig",
        "line_done": "✅ Erledigt",
        "line_streak": "Serie: {days} Tage",
        "line_best": "Beste: {days} Tage",
        "line_every": "Erinnerung alle {interval}",
        "line_off": "🔕 AUS",
        "choose_language": "🌍 <b>Sprache wählen:</b>",
        "settings_title": "⚙️ <b>Einstellungen</b>",
        "settings_goal": "Ziel",
        "settings_weight": "Gewicht",
        "settings_activity": "Aktivität",
        "settings_unit": "Einheit",
        "settings_language": "Sprache",
        "settings_timezone": "Zeitzone",
        "settings_city": "Stadt",
        "settings_plan": "Plan",
        "settings_not_set": "nicht gesetzt",
        "settings_tz_hint": "Nutze /settz Region/Stadt, um die Zeitzone zu ändern",
        "stats_title": "📊 <b>Statistiken</b>",
        "stats_today": "Heute",
        "stats_week": "Diese Woche",
        "stats_month": "Dieser Monat",
        "stats_all_time": "Gesamt",
        "stats_goals": "{n}/7 Ziele",
        "stats_total": "Gesamt",
        "stats_avg": "Durchschn.",
        "stats_badges": "Badges",
        "history_title": "📂 <b>Verlauf</b>",
        "history_empty": "Noch nichts da. Starte mit dem Eintragen.",
        "history_subtitle": "(letzte 14 Tage)",
        "rem_on": "AN ✅",
        "rem_off": "AUS ❌",
        "rem_interval": "⏱ Intervall: alle <b>{interval}</b>\n   Wie oft du an Wassertrinken erinnert wirst",
        "rem_quiet": "🌙 Ruhezeit: <b>{start} → {end}</b>\n   Keine Erinnerungen zu dieser Zeit (z.B. nachts)",
        "rem_fixed": "📌 Feste Zeiten: <b>{fixed}</b>\n   Erinnerung zu bestimmten Tageszeiten",
        "rem_none": "keine",
        "snoozed_until": "⏸ <i>Verschoben bis {time} UTC</i>",
        "log_removed": "entfernt",
        "nothing_undo": "Nichts rückgängig zu machen",
        "custom_log_prompt": "💧 <b>Wasser eintragen</b>\n\n✏️ Menge in ml eingeben (oder oz, wenn du oz nutzt)\nz. B. <code>330</code>",
        "custom_log_err": "⚠️ <i>Gib eine Zahl zwischen 1 und 5000 ein</i>",
        "fixed_time_prompt": "📌 <b>Zeit im Format HH:MM eingeben</b>  z. B. <code>08:30</code>",
        "fixed_time_err": "⚠️ <i>Format: HH:MM  z. B. <code>08:30</code></i>",
        "recalc_prompt": "⚖️ <b>Gib dein aktuelles Gewicht in kg ein</b>  z. B. <code>75</code>\n\nIch berechne dein Tagesziel neu.",
        "choose_day_delete": "🗑️ <b>Tag zum Löschen wählen:</b>",
        "day_deleted": "<i>{day} gelöscht.</i>",
        "today_cleared": "✅ Heutiger Eintrag gelöscht.",
        "all_wiped": "✅ Gesamter Verlauf gelöscht.",
        "no_history_delete": "Kein Verlauf zum Löschen.",
        "no_fixed_remove": "Keine festen Erinnerungen zum Entfernen.",
        "wipe_all_confirm": "⚠️ <b>Kompletten Trinkverlauf löschen?</b>\n\nDadurch werden alle Einträge, Serien und Erfolge dauerhaft entfernt.\nKonto, Einstellungen und Ziel bleiben erhalten.\n\n<b>Das kann nicht rückgängig gemacht werden.</b>",
        "delete_account_confirm": "⚠️ <b>Konto vollständig löschen?</b>\n\nDamit wird <b>alles</b> gelöscht: Einträge, Einstellungen, Ziel, Serie, Erfolge.\nDu startest bei Null. Sende /start für ein neues Konto.\n\n<b>Das kann nicht rückgängig gemacht werden.</b>",
        "account_deleted": "✅ Konto gelöscht. Alle Daten wurden entfernt.\n\nSende /start, um jederzeit neu zu beginnen.",
        "account_deleting": "⏳ Konto wird gelöscht...",
        "choose_language_short": "🌍 <b>Sprache wählen:</b>",
        "trial_used": "Du hast deine kostenlose Testversion bereits genutzt.",
        "payment_open_error": "Zahlung konnte nicht geöffnet werden. Bitte erneut versuchen.",
        "setup_complete": "✅ <b>Einrichtung abgeschlossen!</b>",
        "trial_cta": "Tippe auf ⭐ <b>Premium</b>, um die kostenlose 3-Tage-Testversion zu starten.",
        "export_caption": "📤 Dein AquaBot-Datenexport. Bitte sicher aufbewahren!",
    },
    "fr": {
        "line_remaining": "reste {remaining}",
        "line_done": "✅ Fait",
        "line_streak": "Série: {days} jours",
        "line_best": "Meilleure: {days} jours",
        "line_every": "Rappel toutes les {interval}",
        "line_off": "🔕 OFF",
        "choose_language": "🌍 <b>Choisis ta langue :</b>",
        "settings_title": "⚙️ <b>Réglages</b>",
        "settings_goal": "Objectif",
        "settings_weight": "Poids",
        "settings_activity": "Activité",
        "settings_unit": "Unité",
        "settings_language": "Langue",
        "settings_timezone": "Fuseau",
        "settings_city": "Ville",
        "settings_plan": "Plan",
        "settings_not_set": "non définie",
        "settings_tz_hint": "Utilise /settz Région/Ville pour changer le fuseau",
        "stats_title": "📊 <b>Statistiques</b>",
        "stats_today": "Aujourd'hui",
        "stats_week": "Cette semaine",
        "stats_month": "Ce mois-ci",
        "stats_all_time": "Depuis le début",
        "stats_goals": "{n}/7 objectifs",
        "stats_total": "Total",
        "stats_avg": "Moyenne",
        "stats_badges": "badges",
        "history_title": "📂 <b>Historique</b>",
        "history_empty": "Rien pour l'instant. Commence à enregistrer.",
        "history_subtitle": "(14 derniers jours)",
        "rem_on": "ON ✅",
        "rem_off": "OFF ❌",
        "rem_interval": "⏱ Intervalle : toutes les <b>{interval}</b>\n   Fréquence des rappels pour boire de l'eau",
        "rem_quiet": "🌙 Heures calmes : <b>{start} → {end}</b>\n   Pas de rappels pendant ce temps (ex. nuit)",
        "rem_fixed": "📌 Heures fixes : <b>{fixed}</b>\n   Rappels à des heures spécifiques",
        "rem_none": "aucune",
        "snoozed_until": "⏸ <i>Reporté jusqu'à {time} UTC</i>",
        "log_removed": "supprimé",
        "nothing_undo": "Rien à annuler",
        "custom_log_prompt": "💧 <b>Enregistrer de l'eau</b>\n\n✏️ Saisis la quantité en ml (ou oz si tu utilises oz)\nex. <code>330</code>",
        "custom_log_err": "⚠️ <i>Entre un nombre entre 1 et 5000</i>",
        "fixed_time_prompt": "📌 <b>Saisis l'heure au format HH:MM</b>  ex. <code>08:30</code>",
        "fixed_time_err": "⚠️ <i>Format : HH:MM  ex. <code>08:30</code></i>",
        "recalc_prompt": "⚖️ <b>Entre ton poids actuel en kg</b>  ex. <code>75</code>\n\nJe recalculerai ton objectif quotidien.",
        "choose_day_delete": "🗑️ <b>Choisis un jour à supprimer :</b>",
        "day_deleted": "<i>{day} supprimé.</i>",
        "today_cleared": "✅ Données d'aujourd'hui supprimées.",
        "all_wiped": "✅ Historique entièrement effacé.",
        "no_history_delete": "Aucun historique à supprimer.",
        "no_fixed_remove": "Aucun rappel fixe à supprimer.",
        "wipe_all_confirm": "⚠️ <b>Effacer TOUT l'historique ?</b>\n\nCela supprimera définitivement toutes tes entrées, séries et réussites.\nTon compte, tes réglages et ton objectif seront conservés.\n\n<b>Action irréversible.</b>",
        "delete_account_confirm": "⚠️ <b>Supprimer complètement ton compte ?</b>\n\nCela supprime <b>tout</b> : entrées, réglages, objectif, série, réussites.\nTu repartiras de zéro. Envoie /start pour recommencer.\n\n<b>Action irréversible.</b>",
        "account_deleted": "✅ Compte supprimé. Toutes tes données ont été effacées.\n\nEnvoie /start pour créer un nouveau compte.",
        "account_deleting": "⏳ Suppression du compte...",
        "choose_language_short": "🌍 <b>Choisir la langue :</b>",
        "trial_used": "Tu as déjà utilisé ton essai gratuit.",
        "payment_open_error": "Impossible d'ouvrir le paiement. Réessaie.",
        "setup_complete": "✅ <b>Configuration terminée !</b>",
        "trial_cta": "Appuie sur ⭐ <b>Premium</b> pour démarrer l'essai gratuit de 3 jours.",
        "export_caption": "📤 Export AquaBot de tes données. Garde ce fichier en lieu sûr.",
    },
    "ru": {
        "line_remaining": "осталось {remaining}",
        "line_done": "✅ Готово",
        "line_streak": "Серия: {days} дней",
        "line_best": "Лучшая: {days} дней",
        "line_every": "Напоминание каждые {interval}",
        "line_off": "🔕 ВЫКЛ",
        "choose_language": "🌍 <b>Выбери язык:</b>",
        "settings_title": "⚙️ <b>Настройки</b>",
        "settings_goal": "Цель",
        "settings_weight": "Вес",
        "settings_activity": "Активность",
        "settings_unit": "Единица",
        "settings_language": "Язык",
        "settings_timezone": "Часовой пояс",
        "settings_city": "Город",
        "settings_plan": "План",
        "settings_not_set": "не задан",
        "settings_tz_hint": "Используй /settz Регион/Город для смены часового пояса",
        "stats_title": "📊 <b>Статистика</b>",
        "stats_today": "Сегодня",
        "stats_week": "Эта неделя",
        "stats_month": "Этот месяц",
        "stats_all_time": "За все время",
        "stats_goals": "{n}/7 целей",
        "stats_total": "Всего",
        "stats_avg": "Среднее",
        "stats_badges": "наград",
        "history_title": "📂 <b>История</b>",
        "history_empty": "Пока пусто. Начни вносить воду.",
        "history_subtitle": "(последние 14 дней)",
        "rem_on": "ВКЛ ✅",
        "rem_off": "ВЫКЛ ❌",
        "rem_interval": "⏱ Интервал: каждые <b>{interval}</b>\n   Как часто получать напоминания пить воду",
        "rem_quiet": "🌙 Тихие часы: <b>{start} → {end}</b>\n   Без напоминаний в это время (например ночью)",
        "rem_fixed": "📌 Фикс. время: <b>{fixed}</b>\n   Напоминания в конкретное время дня",
        "rem_none": "нет",
        "snoozed_until": "⏸ <i>Отложено до {time} UTC</i>",
        "log_removed": "удалено",
        "nothing_undo": "Нечего отменять",
        "custom_log_prompt": "💧 <b>Запись воды</b>\n\n✏️ Введи объем в мл (или oz, если используешь унции)\nнапример: <code>330</code>",
        "custom_log_err": "⚠️ <i>Введи число от 1 до 5000</i>",
        "fixed_time_prompt": "📌 <b>Введи время в формате HH:MM</b>  напр. <code>08:30</code>",
        "fixed_time_err": "⚠️ <i>Формат: HH:MM  напр. <code>08:30</code></i>",
        "recalc_prompt": "⚖️ <b>Введи текущий вес в кг</b>  напр. <code>75</code>\n\nЯ пересчитаю дневную цель.",
        "choose_day_delete": "🗑️ <b>Выбери день для удаления:</b>",
        "day_deleted": "<i>{day} удален.</i>",
        "today_cleared": "✅ Запись за сегодня очищена.",
        "all_wiped": "✅ Вся история удалена.",
        "no_history_delete": "Нет истории для удаления.",
        "no_fixed_remove": "Нет фиксированных напоминаний для удаления.",
        "wipe_all_confirm": "⚠️ <b>Стереть ВСЮ историю воды?</b>\n\nЭто навсегда удалит записи, серии и достижения.\nАккаунт, настройки и цель сохранятся.\n\n<b>Это нельзя отменить.</b>",
        "delete_account_confirm": "⚠️ <b>Удалить аккаунт полностью?</b>\n\nБудет удалено <b>все</b>: записи, настройки, цель, серия, достижения.\nТы начнешь с нуля. Отправь /start, чтобы зарегистрироваться снова.\n\n<b>Это нельзя отменить.</b>",
        "account_deleted": "✅ Аккаунт удален. Все данные стерты.\n\nОтправь /start, чтобы создать новый аккаунт.",
        "account_deleting": "⏳ Удаляю аккаунт...",
        "choose_language_short": "🌍 <b>Выбери язык:</b>",
        "trial_used": "Ты уже использовал бесплатный пробный период.",
        "payment_open_error": "Не удалось открыть оплату. Попробуй еще раз.",
        "setup_complete": "✅ <b>Настройка завершена!</b>",
        "trial_cta": "Нажми ⭐ <b>Премиум</b>, чтобы начать бесплатный пробный период на 3 дня.",
        "export_caption": "📤 Экспорт данных AquaBot. Сохрани этот файл.",
    },
    "uk": {
        "line_remaining": "залишилось {remaining}",
        "line_done": "✅ Готово!",
        "line_streak": "Серія: {days} днів",
        "line_best": "Найкраща: {days} днів",
        "line_every": "Нагадування кожні {interval}",
        "line_off": "🔕 ВИМК",
        "choose_language": "🌍 <b>Оберіть мову:</b>",
        "settings_title": "⚙️ <b>Налаштування</b>",
        "settings_goal": "Мета",
        "settings_weight": "Вага",
        "settings_activity": "Активність",
        "settings_unit": "Одиниця",
        "settings_language": "Мова",
        "settings_timezone": "Часовий пояс",
        "settings_city": "Місто",
        "settings_plan": "План",
        "settings_not_set": "не встановлено",
        "settings_tz_hint": "Використовуйте /settz Місто для зміни часового поясу",
        "stats_title": "📊 <b>Статистика</b>",
        "stats_today": "Сьогодні",
        "stats_week": "Цей тиждень",
        "stats_month": "Цей місяць",
        "stats_all_time": "За весь час",
        "stats_goals": "{n}/7 цілей",
        "stats_total": "Всього",
        "stats_avg": "Середнє",
        "stats_badges": "бейджів",
        "history_title": "📂 <b>Історія</b>",
        "history_empty": "Поки нічого — почніть записувати!",
        "history_subtitle": "(останні 14 днів)",
        "rem_on": "УВІМК ✅",
        "rem_off": "ВИМК ❌",
        "rem_interval": "⏱ Інтервал: кожні <b>{interval}</b>\n   Як часто отримувати нагадування пити воду",
        "rem_quiet": "🌙 Тихі години: <b>{start} → {end}</b>\n   Без нагадувань в цей час (наприклад вночі)",
        "rem_fixed": "📌 Фіксовані часи: <b>{fixed}</b>\n   Нагадування в конкретний час дня",
        "rem_none": "немає",
        "snoozed_until": "⏸ <i>Відкладено до {time} UTC</i>",
        "log_removed": "видалено",
        "nothing_undo": "Нічого відкочувати",
        "custom_log_prompt": "💧 <b>Записати воду</b>\n\n✏️ Введіть кількість в мл (або унціях якщо використовуєте)\nнаприклад: <code>330</code>",
        "custom_log_err": "⚠️ <i>Введіть число від 1 до 5000</i>",
        "fixed_time_prompt": "📌 <b>Введіть час у форматі ГГ:ХХ</b>  наприклад: <code>08:30</code>",
        "fixed_time_err": "⚠️ <i>Формат: ГГ:ХХ  наприклад: <code>08:30</code></i>",
        "recalc_prompt": "⚖️ <b>Введіть вагу в кг</b>  наприклад: <code>75</code>\n\nЯ перерахую вашу денну норму води.",
        "choose_day_delete": "🗑️ <b>Оберіть день для видалення:</b>",
        "day_deleted": "<i>{day} видалено.</i>",
        "today_cleared": "✅ Споживання за сьогодні очищено.",
        "all_wiped": "✅ Вся історія видалена.",
        "no_history_delete": "Немає історії для видалення.",
        "no_fixed_remove": "Немає фіксованих нагадувань для видалення.",
        "wipe_all_confirm": "⚠️ <b>Видалити ВСЮ історію споживання води?</b>\n\nЦе назавжди видалить весь записаний обсяг, серію та досягнення.\nВаш акаунт, налаштування та мета будуть збережені.\n\n<b>Це не можна відкоригувати.</b>",
        "delete_account_confirm": "⚠️ <b>Видалити весь акаунт?</b>\n\nЦе видаляє <b>все</b>: всі записи, налаштування, мету, серію, досягнення.\nВи знову почнете з чистого аркуша. Надішліть /start, щоб зареєструватися знову.\n\n<b>Це не можна відкоригувати.</b>",
        "account_deleted": "✅ Акаунт видалено. Всі ваші дані видалено.\n\nНадішліть /start, щоб створити новий акаунт будь-коли.",
        "account_deleting": "⏳ Видалення акаунту...",
        "choose_language_short": "🌍 <b>Оберіть мову:</b>",
        "trial_used": "Ви вже використали безкоштовний пробний період.",
        "payment_open_error": "Не вдалося відкрити оплату. Спробуйте ще раз.",
        "setup_complete": "✅ <b>Налаштування завершено!</b>",
        "trial_cta": "Натисніть ⭐ <b>Преміум</b>, щоб почати безкоштовний пробний період на 3 дні.",
        "export_caption": "📤 Експорт даних AquaBot. Збережіть цей файл.",
    },
}


def ui(p: UserProfile, key: str, **kw) -> str:
    lang = p.language if p.language in UI_STRINGS else "en"
    tmpl = UI_STRINGS[lang].get(key, UI_STRINGS["en"].get(key, key))
    return tmpl.format(**kw) if kw else tmpl


def lang_code(p: UserProfile) -> str:
    return p.language if p.language in STRINGS else "en"


def ach_text(p: UserProfile, key: str, field: str) -> str:
    lang = lang_code(p)
    if lang != "en":
        loc = ACHIEVEMENT_I18N.get(lang, {}).get(key, {})
        if field in loc:
            return loc[field]
    return ACHIEVEMENTS[key][field]


def stats_text(p: UserProfile, today: str) -> str:
    tz = get_tz(p.timezone)
    now = datetime.now(tz)
    drank = get_day_ml(p.telegram_id, today)
    goal = p.daily_goal_ml
    history = get_history_totals(p.telegram_id, 30)

    w_dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    m_dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)]
    w_vals = [history.get(d, 0) for d in w_dates]
    m_vals = [history.get(d, 0) for d in m_dates]
    w_total = sum(w_vals)
    m_total = sum(m_vals)
    w_hit = sum(1 for v in w_vals if v >= goal)
    m_hit = sum(1 for v in m_vals if v >= goal)
    w_avg = w_total // 7
    m_avg = m_total // 30

    entries = get_day_entries(p.telegram_id, today)
    entry_lines = ""
    if entries:
        shown = entries[-5:]
        entry_lines = "\n" + "\n".join(f"  <code>{t}</code>  +{p.fmt(ml)}" for t, ml in shown)
        if len(entries) > 5:
            entry_lines = f"\n  <i>({len(entries)-5} earlier…)</i>" + entry_lines

    pct = min(100, int(drank / max(1, goal) * 100))
    return "\n".join([
        ui(p, "stats_title"),
        "",
        f"<b>{ui(p, 'stats_today')}</b>   {pbar(drank, goal, 10)}  {pct}%",
        f"  {p.fmt(drank)} of {p.fmt_goal()}" + entry_lines,
        "",
        f"<b>{ui(p, 'stats_week')}</b>",
        f"  {pbar(w_total, goal * 7, 10)}  {ui(p, 'stats_goals', n=w_hit)}",
        f"  {ui(p, 'stats_total')}: {p.fmt(w_total)}  ·  {ui(p, 'stats_avg')}: {p.fmt(w_avg)}/day",
        "",
        f"<b>{ui(p, 'stats_month')}</b>",
        f"  {m_hit}/30  ·  {ui(p, 'stats_avg')}: {p.fmt(m_avg)}/day",
        f"  {ui(p, 'stats_total')}: {p.fmt(m_total)}",
        "",
        f"<b>{ui(p, 'stats_all_time')}</b>",
        f"  {p.fmt(p.total_ml_ever)}  ·  {ui(p, 'line_best', days=p.best_streak)}  ·  {len(p.achievements)} {ui(p, 'stats_badges')}",
    ])


def reminders_text(p: UserProfile) -> str:
    status = ui(p, "rem_on") if p.reminders_enabled else ui(p, "rem_off")
    qs = f"{p.quiet_start_hour:02d}:00"
    qe = f"{p.quiet_end_hour:02d}:00"
    iv = mins_label(p.reminder_interval_mins)
    fixed = ", ".join(fr.label() for fr in p.fixed_reminders if fr.enabled) or ui(p, "rem_none")
    snooze_note = ""
    if is_snoozed(p):
        try:
            until = datetime.fromisoformat(p.snooze_until)
            snooze_note = "\n" + ui(p, "snoozed_until", time=until.strftime("%H:%M"))
        except Exception:
            pass
    return "\n".join(filter(None, [
        s(p, "nav_reminders") + f"  {status}",
        "",
        ui(p, "rem_interval", interval=iv),
        ui(p, "rem_quiet", start=qs, end=qe),
        ui(p, "rem_fixed", fixed=fixed),
        snooze_note,
    ]))


def achievements_text(p: UserProfile) -> str:
    header, earned_word = {
        "en": ("🏆 <b>Achievements</b>", "earned"),
        "es": ("🏆 <b>Logros</b>", "obtenidos"),
        "de": ("🏆 <b>Erfolge</b>", "erreicht"),
        "fr": ("🏆 <b>Succès</b>", "obtenus"),
        "ru": ("🏆 <b>Достижения</b>", "получено"),
        "uk": ("🏆 <b>Досягнення</b>", "отримано"),
    }[lang_code(p)]
    lines = [header, f"<i>{len(p.achievements)}/{len(ACHIEVEMENTS)} {earned_word}</i>", ""]
    for key, data in ACHIEVEMENTS.items():
        icon = "✅" if key in p.achievements else "🔒"
        lines.append(f"{icon} {data['icon']} <b>{ach_text(p, key, 'name')}</b>")
        lines.append(f"   <i>{ach_text(p, key, 'desc')}</i>")
    return "\n".join(lines)


def history_text(p: UserProfile) -> str:
    history = get_history_totals(p.telegram_id, 14)
    if not history:
        return f"{ui(p, 'history_title')}\n\n{ui(p, 'history_empty')}"
    goal = p.daily_goal_ml
    lines = [f"{ui(p, 'history_title')}  {ui(p, 'history_subtitle')}", ""]
    for d in sorted(history.keys(), reverse=True):
        ml = history[d]
        check = "✅" if ml >= goal else ("·" if ml > 0 else "○")
        lines.append(f"{check} <code>{d}</code>  {p.fmt(ml)}  {pbar(ml, goal, 6)}")
    return "\n".join(lines)


def settings_text(p: UserProfile) -> str:
    lang_name = STRINGS.get(p.language, STRINGS["en"])["lang_name"]
    plan = account_badge(p)
    return "\n".join([
        ui(p, "settings_title"),
        "",
        f"{ui(p, 'settings_goal')}: <b>{p.fmt_goal()}</b>",
        f"{ui(p, 'settings_weight')}: <b>{p.weight_kg} kg</b>",
        f"{ui(p, 'settings_activity')}: <b>{ACTIVITY_LEVELS[p.activity_level].get(p.language, ACTIVITY_LEVELS[p.activity_level]['en'])}</b>",
        f"{ui(p, 'settings_unit')}: <b>{p.unit.upper()}</b>",
        f"{ui(p, 'settings_language')}: <b>{lang_name}</b>",
        f"{ui(p, 'settings_timezone')}: <b>{p.timezone}</b>",
        f"{ui(p, 'settings_city')}: <b>{p.city or ui(p, 'settings_not_set')}</b>",
        f"{ui(p, 'settings_plan')}: <b>{plan}</b>",
        "",
        ui(p, "settings_tz_hint"),
    ])


def premium_text(p: UserProfile) -> str:
    lang = lang_code(p)
    is_prem = is_premium_active(p)
    is_trial = is_prem and (p.premium_expiry != "lifetime")
    trial_expired = p.trial_used and not is_prem and p.premium_expiry != "lifetime"

    # ─────────────────────────────────────────────────────────────
    #  STATE 1: LIFETIME PREMIUM OWNER
    # ─────────────────────────────────────────────────────────────
    if is_prem and not is_trial:
        data = {
            "en": {
                "title": "⭐ <b>Premium Active — Lifetime</b>",
                "sub": "<i>You own Premium forever. No renewals, no charges, ever.</i>",
                "lines": [
                    "",
                    "Here's everything that's active for you:\n",
                    "🧠 <b>Smart Reminders</b>\n"
                    "   Instead of the same message every hour, reminders change based on "
                    "how your day is going. At 30% before noon → casual nudge. At 20% by "
                    "8pm with a 7-day streak → urgent warning. Goal nearly done → "
                    "encouraging push. The bot reads the situation.",
                    "",
                    "🌤 <b>Weather-Adjusted Goals</b>\n"
                    "   On hot days your body needs more water than usual. When the "
                    "temperature in your city rises above 25°C, your daily target "
                    "automatically increases — +200ml at 25°C, +400ml at 30°C, +600ml "
                    "at 35°C+. Set your city in Settings to keep this active.",
                    "",
                    "⚡ <b>Catch-Up Mode</b>\n"
                    "   If it's 3pm and you've only hit 30% of your goal, the bot "
                    "shortens the reminder gap to help you recover before the day ends. "
                    "It returns to normal once you're back on track. No action needed.",
                    "",
                    "📊 <b>Weekly Report (every Sunday)</b>\n"
                    "   Every Sunday evening your dashboard updates with a full breakdown: "
                    "total intake, daily average, how many goal days you hit, your best "
                    "day, and whether you're trending up or down vs last week.",
                    "",
                    "📈 <b>Charts — 7-Day & 30-Day</b>\n"
                    "   Tap Charts from the home screen. You'll see a horizontal bar "
                    "chart — one row per day, scaled to your goal. ✅ = goal hit, "
                    "· = partial, ○ = nothing logged. Trend indicator shows if your "
                    "intake is improving week over week.",
                    "",
                    "🏆 <b>All Achievements</b>\n"
                    "   Every badge and milestone tracks normally — streaks, total litres, "
                    "early bird, night owl, and more.",
                ],
            },
            "es": {
                "title": "⭐ <b>Premium Activo — De por vida</b>",
                "sub": "<i>Tienes Premium para siempre. Sin renovaciones ni cobros adicionales.</i>",
                "lines": [
                    "",
                    "Todo lo que está activo para ti:\n",
                    "🧠 <b>Recordatorios Inteligentes</b>\n"
                    "   En lugar del mismo mensaje cada hora, los recordatorios cambian "
                    "según cómo va tu día. Al 30% antes del mediodía → aviso suave. "
                    "Al 20% a las 8pm con racha de 7 días → alerta urgente. Meta casi "
                    "completa → empuje motivador. El bot lee la situación.",
                    "",
                    "🌤 <b>Meta Ajustada por Clima</b>\n"
                    "   En días calurosos tu cuerpo necesita más agua. Cuando la "
                    "temperatura en tu ciudad sube de 25°C, el objetivo diario sube "
                    "automáticamente — +200ml a 25°C, +400ml a 30°C, +600ml a 35°C+. "
                    "Configura tu ciudad en Ajustes para mantenerlo activo.",
                    "",
                    "⚡ <b>Modo Recuperación</b>\n"
                    "   Si son las 3pm y solo has alcanzado el 30% de tu meta, el bot "
                    "acorta el intervalo de recordatorios para ayudarte a recuperarte "
                    "antes de que termine el día. Vuelve a la normalidad cuando estés "
                    "al día. No necesitas hacer nada.",
                    "",
                    "📊 <b>Reporte Semanal (cada domingo)</b>\n"
                    "   Cada domingo por la tarde tu panel se actualiza con un resumen "
                    "completo: ingesta total, promedio diario, cuántos días cumpliste la "
                    "meta, tu mejor día y si estás mejorando o empeorando vs la semana "
                    "anterior.",
                    "",
                    "📈 <b>Gráficos — 7 y 30 días</b>\n"
                    "   Pulsa Gráficos desde la pantalla principal. Verás un gráfico de "
                    "barras horizontales — una fila por día, escalada a tu meta. "
                    "✅ = meta cumplida, · = parcial, ○ = nada registrado. El "
                    "indicador de tendencia muestra si tu ingesta mejora semana a semana.",
                    "",
                    "🏆 <b>Todos los Logros</b>\n"
                    "   Cada insignia y hito se registra normalmente — rachas, litros "
                    "totales, madrugador, nocturno y más.",
                ],
            },
            "de": {
                "title": "⭐ <b>Premium Aktiv — Lebenslang</b>",
                "sub": "<i>Du besitzt Premium dauerhaft. Keine Verlängerung, keine weiteren Kosten.</i>",
                "lines": [
                    "",
                    "Alles, was für dich aktiv ist:\n",
                    "🧠 <b>Smarte Erinnerungen</b>\n"
                    "   Statt immer der gleichen Nachricht jede Stunde passen sich die "
                    "Erinnerungen deinem Tagesverlauf an. Bei 30% vor Mittag → sanfter "
                    "Hinweis. Bei 20% um 20 Uhr mit 7-Tage-Streak → dringende Warnung. "
                    "Ziel fast erreicht → motivierender Schubs. Der Bot liest die Lage.",
                    "",
                    "🌤 <b>Wetterbasiertes Tagesziel</b>\n"
                    "   An heißen Tagen braucht dein Körper mehr Wasser. Wenn die "
                    "Temperatur in deiner Stadt über 25°C steigt, erhöht sich dein "
                    "Tagesziel automatisch — +200ml bei 25°C, +400ml bei 30°C, "
                    "+600ml bei 35°C+. Setze deine Stadt in den Einstellungen, "
                    "damit es aktiv bleibt.",
                    "",
                    "⚡ <b>Aufholmodus</b>\n"
                    "   Wenn es 15 Uhr ist und du nur 30% deines Ziels erreicht hast, "
                    "verkürzt der Bot den Erinnerungsabstand, damit du vor Tagesende "
                    "aufholen kannst. Sobald du wieder auf Kurs bist, kehrt er zur "
                    "normalen Frequenz zurück. Kein Eingriff nötig.",
                    "",
                    "📊 <b>Wochenbericht (jeden Sonntag)</b>\n"
                    "   Jeden Sonntagabend aktualisiert sich dein Dashboard mit einer "
                    "vollständigen Übersicht: Gesamtaufnahme, Tagesdurchschnitt, wie "
                    "viele Zieltage du erreicht hast, dein bester Tag und ob du im "
                    "Vergleich zur Vorwoche aufwärts oder abwärts trendest.",
                    "",
                    "📈 <b>Diagramme — 7 & 30 Tage</b>\n"
                    "   Tippe auf Diagramme im Startbildschirm. Du siehst ein "
                    "horizontales Balkendiagramm — eine Zeile pro Tag, skaliert auf "
                    "dein Ziel. ✅ = Ziel erreicht, · = teilweise, ○ = nichts "
                    "eingetragen. Der Trendindikator zeigt, ob deine Aufnahme von "
                    "Woche zu Woche besser wird.",
                    "",
                    "🏆 <b>Alle Erfolge</b>\n"
                    "   Jedes Abzeichen und jeder Meilenstein wird normal verfolgt — "
                    "Streaks, Gesamtliter, Frühaufsteher, Nachteule und mehr.",
                ],
            },
            "fr": {
                "title": "⭐ <b>Premium Actif — À vie</b>",
                "sub": "<i>Tu as Premium à vie. Aucun renouvellement, aucun frais supplémentaire.</i>",
                "lines": [
                    "",
                    "Tout ce qui est actif pour toi :\n",
                    "🧠 <b>Rappels Intelligents</b>\n"
                    "   Au lieu du même message toutes les heures, les rappels changent "
                    "selon comment se passe ta journée. À 30% avant midi → nudge "
                    "décontracté. À 20% à 20h avec une série de 7 jours → alerte "
                    "urgente. Objectif presque atteint → poussée motivante. "
                    "Le bot lit la situation.",
                    "",
                    "🌤 <b>Objectif Ajusté à la Météo</b>\n"
                    "   Par temps chaud, ton corps a besoin de plus d'eau. Quand la "
                    "température dans ta ville dépasse 25°C, ton objectif journalier "
                    "augmente automatiquement — +200ml à 25°C, +400ml à 30°C, "
                    "+600ml à 35°C+. Renseigne ta ville dans les Réglages pour "
                    "garder cette fonction active.",
                    "",
                    "⚡ <b>Mode Rattrapage</b>\n"
                    "   S'il est 15h et que tu n'as atteint que 30% de ton objectif, "
                    "le bot réduit l'intervalle entre les rappels pour t'aider à "
                    "rattraper ton retard avant la fin de la journée. Il revient à "
                    "la normale dès que tu es dans les clous. Aucune action requise.",
                    "",
                    "📊 <b>Rapport Hebdomadaire (chaque dimanche)</b>\n"
                    "   Chaque dimanche soir ton tableau de bord se met à jour avec un "
                    "bilan complet : apport total, moyenne journalière, combien de jours "
                    "tu as atteint l'objectif, ton meilleur jour, et si tu es en "
                    "progression ou en recul par rapport à la semaine précédente.",
                    "",
                    "📈 <b>Graphiques — 7 & 30 jours</b>\n"
                    "   Appuie sur Graphiques depuis l'écran d'accueil. Tu verras un "
                    "graphique à barres horizontales — une ligne par jour, mise à "
                    "l'échelle de ton objectif. ✅ = objectif atteint, · = partiel, "
                    "○ = rien enregistré. L'indicateur de tendance montre si ton "
                    "apport s'améliore de semaine en semaine.",
                    "",
                    "🏆 <b>Tous les Succès</b>\n"
                    "   Chaque badge et jalon est suivi normalement — séries, litres "
                    "totaux, lève-tôt, couche-tard et plus encore.",
                ],
            },
            "ru": {
                "title": "⭐ <b>Премиум активен — Навсегда</b>",
                "sub": "<i>Премиум куплен навсегда. Никаких продлений и дополнительных платежей.</i>",
                "lines": [
                    "",
                    "Всё, что сейчас работает для тебя:\n",
                    "🧠 <b>Умные напоминания</b>\n"
                    "   Вместо одного и того же сообщения каждый час напоминания "
                    "меняются в зависимости от твоего прогресса. 30% до полудня → "
                    "мягкий намёк. 20% в 20:00 при серии 7 дней → срочное "
                    "предупреждение. Цель почти достигнута → мотивирующий толчок. "
                    "Бот читает ситуацию.",
                    "",
                    "🌤 <b>Цель по погоде</b>\n"
                    "   В жаркие дни телу нужно больше воды. Когда температура в "
                    "твоём городе поднимается выше 25°C, дневная цель автоматически "
                    "растёт — +200мл при 25°C, +400мл при 30°C, +600мл при 35°C+. "
                    "Укажи город в Настройках, чтобы функция работала.",
                    "",
                    "⚡ <b>Режим догонки</b>\n"
                    "   Если в 15:00 ты выполнил только 30% цели, бот сокращает "
                    "интервал напоминаний, чтобы помочь наверстать до конца дня. "
                    "Как только ты вернёшься в норму — возвращается обычный ритм. "
                    "Ничего делать не нужно.",
                    "",
                    "📊 <b>Недельный отчёт (каждое воскресенье)</b>\n"
                    "   Каждое воскресенье вечером дашборд обновляется с полной "
                    "сводкой: общий объём, дневное среднее, сколько дней цель была "
                    "выполнена, лучший день и тренд по сравнению с прошлой неделей.",
                    "",
                    "📈 <b>Графики — 7 и 30 дней</b>\n"
                    "   Нажми Графики на главном экране. Откроется горизонтальная "
                    "столбчатая диаграмма — одна строка на день, масштабирована по "
                    "цели. ✅ = цель выполнена, · = частично, ○ = ничего не записано. "
                    "Индикатор тренда показывает, улучшается ли потребление воды "
                    "неделя за неделей.",
                    "",
                    "🏆 <b>Все достижения</b>\n"
                    "   Каждый значок и этап отслеживается в обычном режиме — серии, "
                     "общие литры, ранняя пташка, ночная сова и многое другое.",
                ],
            },
            "uk": {
                "title": "⭐ <b>Преміум активний — Назавжди</b>",
                "sub": "<i>Преміум куплено назавжди. Жодних продовжень та додаткових платежів.</i>",
                "lines": [
                    "",
                    "Все, що зараз працює для вас:\n",
                    "🧠 <b>Розумні нагадування</b>\n"
                    "   Замість одного й того самого повідомлення кожну годину нагадування "
                    "змінюються залежно від вашого прогресу. 30% до полудня → "
                    "м'який натяк. 20% о 20:00 при серії 7 днів → термінове "
                    "попередження. Мета майже досягнута → мотивуючий поштовх. "
                    "Бот читає ситуацію.",
                    "",
                    "🌤 <b>Мета за погодою</b>\n"
                    "   У спекотні дні тілу потрібно більше води. Коли температура у "
                    "вашому місті піднімається вище 25°C, денна мета автоматично "
                    "зростає — +200мл при 25°C, +400мл при 30°C, +600мл при 35°C+. "
                    "Вкажіть місто в Налаштуваннях, щоб функція працювала.",
                    "",
                    "⚡ <b>Режим наздоганяння</b>\n"
                    "   Якщо о 15:00 ви виконали лише 30% мети, бот скорочує "
                    "інтервал нагадувань, щоб допомогти надолужити до кінця дня. "
                    "Як тільки ви повернетеся в норму — повертається звичайний ритм. "
                    "Нічого робити не потрібно.",
                    "",
                    "📊 <b>Тижневий звіт (кожну неділю)</b>\n"
                    "   Кожної неділі ввечері дашборд оновлюється з повною "
                    "зведенням: загальний обсяг, денне середнє, скільки днів мета була "
                    "виконана, кращий день і тренд порівняно з минулим тижнем.",
                    "",
                    "📈 <b>Графіки — 7 і 30 днів</b>\n"
                    "   Натисніть Графіки на головному екрані. Відкриється горизонтальна "
                    "стовпчаста діаграма — один рядок на день, масштабована за "
                    "метою. ✅ = мета виконана, · = частково, ○ = нічого не записано. "
                    "Індикатор тренду показує, чи покращується споживання води "
                    "тиждень за тижнем.",
                    "",
                    "🏆 <b>Всі досягнення</b>\n"
                    "   Кожен значок та етап відстежується у звичайному режимі — серії, "
                    "загальні літри, рання пташка, нічна сова та багато іншого.",
                ],
            },
        }[lang]
        return "\n".join([data["title"], data["sub"]] + data["lines"])

    # ─────────────────────────────────────────────────────────────
    #  STATE 2: TRIAL ACTIVE
    # ─────────────────────────────────────────────────────────────
    elif is_trial:
        days_left = trial_days_left(p)
        data = {
            "en": {
                "title": f"🎁 <b>Free Trial Active — {days_left} day(s) left</b>",
                "sub": "<i>You have full Premium access right now. Explore everything before it ends.</i>",
                "lines": [
                    "",
                    "What's unlocked during your trial:\n",
                    "🧠 <b>Smart Reminders — already on</b>\n"
                    "   Your next reminder won't just say 'drink water'. It'll know "
                    "what time it is, how far you are from your goal, and whether your "
                    "streak is at risk. The message adapts every time.",
                    "",
                    "🌤 <b>Weather Goals — on if city is set</b>\n"
                    "   If you've set your city, your goal already adjusts on hot days. "
                    "Haven't set it? Go to Settings → the bot will start using live "
                    "weather from tomorrow.",
                    "",
                    "⚡ <b>Catch-Up Mode — fully active</b>\n"
                    "   Forgot to drink in the morning? After 3pm, if you're significantly "
                    "behind, reminders will come more often automatically — no setup needed.",
                    "",
                    "📊 <b>Weekly Report — arrives Sunday</b>\n"
                    "   You'll get your first full report this Sunday evening. It'll "
                    "show your intake, consistency score, best day of the week, and "
                    "a trend compared to the week before.",
                    "",
                    "📈 <b>Charts — tap Charts on Home</b>\n"
                    "   Try it now: tap the Charts button. You'll see a 7-day bar "
                    "chart of your daily intake. Switch to 30-day for a wider view. "
                    "This closes when your trial ends on free plan.",
                    "",
                    "🔒 <b>After trial:</b> Smart reminders, weather, catch-up, "
                    "reports and charts all go back to locked on the free plan.",
                ],
                "cta": f"Keep everything forever — just <b>{PREMIUM_STARS}⭐ once</b>, no subscription.",
            },
            "es": {
                "title": f"🎁 <b>Prueba activa — {days_left} día(s) restante(s)</b>",
                "sub": "<i>Tienes acceso Premium completo ahora mismo. Explora todo antes de que termine.</i>",
                "lines": [
                    "",
                    "Qué está desbloqueado durante tu prueba:\n",
                    "🧠 <b>Recordatorios Inteligentes — ya activos</b>\n"
                    "   Tu próximo recordatorio no solo dirá 'bebe agua'. Sabrá qué "
                    "hora es, cuánto te falta para la meta y si tu racha está en riesgo. "
                    "El mensaje se adapta cada vez.",
                    "",
                    "🌤 <b>Meta por Clima — activa si tienes ciudad configurada</b>\n"
                    "   Si configuraste tu ciudad, tu meta ya se ajusta en días "
                    "calurosos. ¿No lo hiciste? Ve a Ajustes → el bot empezará a usar "
                    "el clima en directo desde mañana.",
                    "",
                    "⚡ <b>Modo Recuperación — completamente activo</b>\n"
                    "   ¿Olvidaste beber por la mañana? Después de las 3pm, si vas "
                    "muy atrasado, los recordatorios aumentarán automáticamente — "
                    "sin configuración necesaria.",
                    "",
                    "📊 <b>Reporte Semanal — llega el domingo</b>\n"
                    "   Recibirás tu primer reporte completo este domingo. Mostrará "
                    "tu ingesta, puntuación de constancia, mejor día de la semana y "
                    "tendencia comparada con la semana anterior.",
                    "",
                    "📈 <b>Gráficos — pulsa Gráficos en inicio</b>\n"
                    "   Pruébalo ahora: toca el botón Gráficos. Verás un gráfico de "
                    "barras de 7 días. Cambia a 30 días para una vista más amplia. "
                    "Esto se bloquea cuando termine la prueba en el plan gratuito.",
                    "",
                    "🔒 <b>Después de la prueba:</b> Recordatorios inteligentes, clima, "
                    "recuperación, reportes y gráficos vuelven a estar bloqueados.",
                ],
                "cta": f"Mantén todo para siempre — solo <b>{PREMIUM_STARS}⭐ una vez</b>, sin suscripción.",
            },
            "de": {
                "title": f"🎁 <b>Test aktiv — noch {days_left} Tag(e)</b>",
                "sub": "<i>Du hast jetzt vollen Premium-Zugriff. Erkunde alles, bevor der Test endet.</i>",
                "lines": [
                    "",
                    "Was in deinem Test freigeschaltet ist:\n",
                    "🧠 <b>Smarte Erinnerungen — bereits aktiv</b>\n"
                    "   Deine nächste Erinnerung sagt nicht einfach nur 'Wasser trinken'. "
                    "Sie weiß, wie spät es ist, wie weit du von deinem Ziel entfernt "
                    "bist und ob dein Streak gefährdet ist. Die Nachricht passt sich "
                    "jedes Mal an.",
                    "",
                    "🌤 <b>Wetterziel — aktiv wenn Stadt gesetzt</b>\n"
                    "   Wenn du deine Stadt gesetzt hast, passt sich dein Ziel an "
                    "heißen Tagen bereits an. Noch nicht gesetzt? Geh zu Einstellungen "
                    "→ der Bot nutzt das Live-Wetter ab morgen.",
                    "",
                    "⚡ <b>Aufholmodus — voll aktiv</b>\n"
                    "   Morgens vergessen zu trinken? Nach 15 Uhr, wenn du deutlich "
                    "zurückliegt, kommen Erinnerungen automatisch häufiger — "
                    "keine Einrichtung nötig.",
                    "",
                    "📊 <b>Wochenbericht — kommt Sonntag</b>\n"
                    "   Deinen ersten vollständigen Bericht erhältst du diesen "
                    "Sonntagabend. Er zeigt deine Aufnahme, Konstanzpunktzahl, "
                    "besten Wochentag und Trend im Vergleich zur Vorwoche.",
                    "",
                    "📈 <b>Diagramme — tippe auf Diagramme im Startbildschirm</b>\n"
                    "   Probier's jetzt: Tippe auf den Diagramme-Button. Du siehst ein "
                    "7-Tage-Balkendiagramm deiner täglichen Aufnahme. Wechsle zu "
                    "30 Tage für eine breitere Ansicht. Dies wird nach dem Test im "
                    "Free-Plan gesperrt.",
                    "",
                    "🔒 <b>Nach dem Test:</b> Smarte Erinnerungen, Wetter, Aufholmodus, "
                    "Berichte und Charts werden im Free-Plan wieder gesperrt.",
                ],
                "cta": f"Behalte alles dauerhaft — nur <b>{PREMIUM_STARS}⭐ einmal</b>, kein Abo.",
            },
            "fr": {
                "title": f"🎁 <b>Essai actif — {days_left} jour(s) restant(s)</b>",
                "sub": "<i>Tu as l'accès Premium complet en ce moment. Explore tout avant la fin.</i>",
                "lines": [
                    "",
                    "Ce qui est débloqué pendant ton essai :\n",
                    "🧠 <b>Rappels Intelligents — déjà actifs</b>\n"
                    "   Ton prochain rappel ne dira pas juste 'bois de l'eau'. Il saura "
                    "quelle heure il est, où tu en es par rapport à ton objectif et si "
                    "ta série est en danger. Le message s'adapte à chaque fois.",
                    "",
                    "🌤 <b>Objectif Météo — actif si ville renseignée</b>\n"
                    "   Si tu as renseigné ta ville, ton objectif s'ajuste déjà les jours "
                    "chauds. Pas encore fait ? Va dans Réglages → le bot utilisera "
                    "la météo en direct dès demain.",
                    "",
                    "⚡ <b>Mode Rattrapage — pleinement actif</b>\n"
                    "   Oublié de boire le matin ? Après 15h, si tu es nettement en "
                    "retard, les rappels s'intensifient automatiquement — aucune "
                    "configuration requise.",
                    "",
                    "📊 <b>Rapport Hebdomadaire — arrive dimanche</b>\n"
                    "   Tu recevras ton premier rapport complet ce dimanche soir. Il "
                    "montrera ton apport, ton score de régularité, ton meilleur jour "
                    "et la tendance par rapport à la semaine précédente.",
                    "",
                    "📈 <b>Graphiques — appuie sur Graphiques depuis l'accueil</b>\n"
                    "   Essaie maintenant : appuie sur le bouton Graphiques. Tu verras "
                    "un graphique à barres sur 7 jours. Passe à 30 jours pour une "
                    "vue plus large. Cela se verrouille à la fin de l'essai.",
                    "",
                    "🔒 <b>Après l'essai :</b> Rappels intelligents, météo, rattrapage, "
                    "rapports et graphiques repassent en verrouillé sur la version gratuite.",
                ],
                "cta": f"Garde tout à vie — seulement <b>{PREMIUM_STARS}⭐ une fois</b>, sans abonnement.",
            },
            "ru": {
                "title": f"🎁 <b>Пробный период активен — осталось {days_left} дн.</b>",
                "sub": "<i>Прямо сейчас у тебя полный доступ к Premium. Попробуй всё до окончания.</i>",
                "lines": [
                    "",
                    "Что разблокировано во время пробного периода:\n",
                    "🧠 <b>Умные напоминания — уже работают</b>\n"
                    "   Следующее напоминание не просто скажет «пей воду». Оно будет "
                    "знать, который час, насколько ты близок к цели и под угрозой ли "
                    "твоя серия. Сообщение каждый раз адаптируется.",
                    "",
                    "🌤 <b>Цель по погоде — работает если город указан</b>\n"
                    "   Если ты указал город, цель уже корректируется в жаркие дни. "
                    "Не указал? Зайди в Настройки → бот начнёт использовать "
                    "погоду в реальном времени с завтрашнего дня.",
                    "",
                    "⚡ <b>Режим догонки — полностью активен</b>\n"
                    "   Забыл пить утром? После 15:00, если ты сильно отстаёшь, "
                    "напоминания будут приходить чаще автоматически — "
                    "никаких настроек не нужно.",
                    "",
                    "📊 <b>Недельный отчёт — придёт в воскресенье</b>\n"
                    "   Первый полный отчёт ты получишь в это воскресенье вечером. "
                    "В нём будет: общий объём, оценка стабильности, лучший день "
                    "недели и тренд по сравнению с прошлой неделей.",
                    "",
                    "📈 <b>Графики — нажми Графики на главном экране</b>\n"
                    "   Попробуй прямо сейчас: нажми кнопку Графики. Увидишь "
                    "горизонтальную диаграмму за 7 дней. Переключись на 30 дней "
                    "для более широкого обзора. После пробного периода на "
                    "бесплатном плане это закроется.",
                    "",
                    "🔒 <b>После пробного:</b> умные напоминания, погода, режим догонки, "
                    "отчёты и графики снова заблокируются на бесплатном плане.",
                ],
                "cta": f"Оставь всё навсегда — всего <b>{PREMIUM_STARS}⭐ один раз</b>, без подписки.",
            },
            "uk": {
                "title": f"🎁 <b>Пробний період активний — залишилось {days_left} дн.</b>",
                "sub": "<i>Прямо зараз у вас повний доступ до Premium. Спробуйте все до закінчення.</i>",
                "lines": [
                    "",
                    "Що розблоковано під час пробного періоду:\n",
                    "🧠 <b>Розумні нагадування — вже працюють</b>\n"
                    "   Наступне нагадування не просто скаже «пий воду». Воно буде "
                    "знати, котра година, наскільки ви близькі до мети і чи під загрозою "
                    "ваша серія. Повідомлення кожного разу адаптується.",
                    "",
                    "🌤 <b>Мета за погодою — працює якщо місто вказане</b>\n"
                    "   Якщо ви вказали місто, мета вже коригується у спекотні дні. "
                    "Не вказали? Зайдіть у Налаштування → бот почне використовувати "
                    "погоду в реальному часі з завтрашнього дня.",
                    "",
                    "⚡ <b>Режим наздоганяння — повністю активний</b>\n"
                    "   Забули пити вранці? Після 15:00, якщо ви сильно відстаєте, "
                    "нагадування будуть приходити частіше автоматично — "
                    "жодних налаштувань не потрібно.",
                    "",
                    "📊 <b>Тижневий звіт — прийде в неділю</b>\n"
                    "   Перший повний звіт ви отримаєте цієї неділі ввечері. "
                    "В ньому буде: загальний обсяг, оцінка стабільності, кращий день "
                    "тижня та тренд порівняно з минулим тижнем.",
                    "",
                    "📈 <b>Графіки — натисніть Графіки на головному екрані</b>\n"
                    "   Спробуйте прямо зараз: натисніть кнопку Графіки. Побачите "
                    "горизонтальну діаграму за 7 днів. Переключіться на 30 днів "
                    "для ширшого огляду. Після пробного періоду на "
                    "безкоштовному плані це закриється.",
                    "",
                    "🔒 <b>Після пробного:</b> розумні нагадування, погода, режим наздоганяння, "
                    "звіти та графіки знову заблокуються на безкоштовному плані.",
                ],
                "cta": f"Залишіть все назавжди — всього <b>{PREMIUM_STARS}⭐ один раз</b>, без підписки.",
            },
        }[lang]
        return "\n".join([data["title"], data["sub"]] + data["lines"] + ["", data["cta"]])

    # ─────────────────────────────────────────────────────────────
    #  STATE 3: TRIAL EXPIRED
    # ─────────────────────────────────────────────────────────────
    elif trial_expired:
        data = {
            "en": {
                "msg": "⏰ <b>Your free trial has ended.</b>\n"
                       "<i>You've seen what Premium can do — here's what you're missing:</i>",
                "lost": [
                    "🧠 Smart reminders <i>(now back to generic hourly pings)</i>",
                    "🌤 Weather goal boosts <i>(no longer adjusting on hot days)</i>",
                    "⚡ Catch-up mode <i>(reminders won't speed up if you fall behind)</i>",
                    "📊 Weekly reports <i>(Sunday summaries are off)</i>",
                    "📈 Charts <i>(7-day and 30-day views are locked)</i>",
                ],
                "cta": f"<b>Unlock everything again — {PREMIUM_STARS}⭐ once. Yours forever.</b>\n"
                       "No subscription. No monthly fees. Pay once, keep forever.",
            },
            "es": {
                "msg": "⏰ <b>Tu prueba gratuita ha terminado.</b>\n"
                       "<i>Ya viste lo que Premium puede hacer — esto es lo que te falta:</i>",
                "lost": [
                    "🧠 Recordatorios inteligentes <i>(vuelven a ser avisos genéricos)</i>",
                    "🌤 Ajuste de meta por clima <i>(ya no se adapta en días calurosos)</i>",
                    "⚡ Modo recuperación <i>(los recordatorios no se acelerarán si vas tarde)</i>",
                    "📊 Reportes semanales <i>(resúmenes del domingo desactivados)</i>",
                    "📈 Gráficos <i>(vistas de 7 y 30 días bloqueadas)</i>",
                ],
                "cta": f"<b>Desbloquea todo de nuevo — {PREMIUM_STARS}⭐ una vez. Para siempre.</b>\n"
                       "Sin suscripción. Sin cargos mensuales. Paga una vez, conserva para siempre.",
            },
            "de": {
                "msg": "⏰ <b>Dein kostenloser Test ist beendet.</b>\n"
                       "<i>Du hast gesehen, was Premium kann — das fehlt dir jetzt:</i>",
                "lost": [
                    "🧠 Smarte Erinnerungen <i>(wieder generische stündliche Pings)</i>",
                    "🌤 Wetterzielanpassung <i>(passt sich nicht mehr an heißen Tagen an)</i>",
                    "⚡ Aufholmodus <i>(Erinnerungen beschleunigen sich nicht mehr bei Rückstand)</i>",
                    "📊 Wochenberichte <i>(Sonntagszusammenfassungen deaktiviert)</i>",
                    "📈 Diagramme <i>(7- und 30-Tage-Ansichten gesperrt)</i>",
                ],
                "cta": f"<b>Schalte alles wieder frei — {PREMIUM_STARS}⭐ einmal. Für immer.</b>\n"
                       "Kein Abo. Keine monatlichen Gebühren. Einmal zahlen, für immer behalten.",
            },
            "fr": {
                "msg": "⏰ <b>Ton essai gratuit est terminé.</b>\n"
                       "<i>Tu as vu ce que Premium peut faire — voilà ce qui te manque :</i>",
                "lost": [
                    "🧠 Rappels intelligents <i>(retour aux pings horaires génériques)</i>",
                    "🌤 Ajustement météo <i>(ne s'adapte plus les jours chauds)</i>",
                    "⚡ Mode rattrapage <i>(les rappels ne s'accélèrent plus si tu es en retard)</i>",
                    "📊 Rapports hebdos <i>(résumés du dimanche désactivés)</i>",
                    "📈 Graphiques <i>(vues 7 et 30 jours verrouillées)</i>",
                ],
                "cta": f"<b>Déverrouille tout à nouveau — {PREMIUM_STARS}⭐ une fois. À vie.</b>\n"
                       "Pas d'abonnement. Pas de frais mensuels. Un paiement, pour toujours.",
            },
            "ru": {
                "msg": "⏰ <b>Твой бесплатный пробный период закончился.</b>\n"
                       "<i>Ты видел, на что способен Premium — вот что теперь недоступно:</i>",
                "lost": [
                    "🧠 Умные напоминания <i>(снова обычные уведомления раз в час)</i>",
                    "🌤 Цель по погоде <i>(больше не корректируется в жаркие дни)</i>",
                    "⚡ Режим догонки <i>(напоминания не ускорятся при отставании)</i>",
                    "📊 Недельные отчёты <i>(воскресные сводки отключены)</i>",
                    "📈 Графики <i>(виды за 7 и 30 дней заблокированы)</i>",
                ],
                "cta": f"<b>Разблокируй всё снова — {PREMIUM_STARS}⭐ один раз. Навсегда.</b>\n"
                       "Никаких подписок. Никаких ежемесячных платежей. Платишь раз — владеешь вечно.",
            },
            "uk": {
                "msg": "⏰ <b>Ваш безкоштовний пробний період закінчився.</b>\n"
                       "<i>Ви бачили, на що здатний Premium — ось що тепер недоступно:</i>",
                "lost": [
                    "🧠 Розумні нагадування <i>(знову звичайні сповіщення раз на годину)</i>",
                    "🌤 Мета за погодою <i>(більше не коригується у спекотні дні)</i>",
                    "⚡ Режим наздоганяння <i>(нагадування не прискоряться при відставанні)</i>",
                    "📊 Тижневі звіти <i>(недільні зведення вимкнено)</i>",
                    "📈 Графіки <i>(перегляд за 7 і 30 днів заблоковано)</i>",
                ],
                "cta": f"<b>Розблокуйте все знову — {PREMIUM_STARS}⭐ один раз. Назавжди.</b>\n"
                       "Жодних підписок. Жодних щомісячних платежів. Платите раз — володієте вічно.",
            },
        }[lang]
        return "\n".join([data["msg"], ""] + data["lost"] + ["", data["cta"]])

    # ─────────────────────────────────────────────────────────────
    #  STATE 4: FREE USER (never trialed)
    # ─────────────────────────────────────────────────────────────
    else:
        data = {
            "en": {
                "title": "⭐ <b>AquaBot Premium — Lifetime Access</b>",
                "sub": f"<b>{PREMIUM_STARS}⭐ once · No subscription · No renewal</b>",
                "free_header": "✅ <b>Free plan includes:</b>",
                "free": [
                    "  · Log water in one tap",
                    "  · Interval reminders (every X minutes)",
                    "  · Basic stats (today, week, month, all-time)",
                    "  · History (last 14 days)",
                    "  · Achievements and streaks",
                    "  · Settings, units, language",
                    "  🔒 Charts are locked on free",
                ],
                "premium_header": "\n⭐ <b>Premium adds:</b>",
                "premium": [
                    "🧠 <b>Smart Reminders</b>\n"
                    "   Free reminders say the same thing every time. Premium reminders "
                    "read your situation: how far you are from your goal, what time it "
                    "is, whether your streak is under threat. At 7pm with 80% done → "
                    "encouraging. At 9pm with 20% done and a 14-day streak → urgent. "
                    "The message always fits the moment.",
                    "",
                    "🌤 <b>Weather-Adjusted Goals</b>\n"
                    "   Your body needs more water when it's hot outside. With a city "
                    "set in Settings, the bot checks live weather daily. Above 25°C → "
                    "+200ml added to your goal. Above 30°C → +400ml. Above 35°C → "
                    "+600ml. It resets automatically the next day.",
                    "",
                    "⚡ <b>Catch-Up Mode</b>\n"
                    "   Had a busy morning and barely drank anything? After 3pm, if you're "
                    "significantly behind your daily goal, reminders automatically come "
                    "more often — every 30 minutes instead of your usual interval. "
                    "Once you're back on track, it returns to normal. Fully automatic.",
                    "",
                    "📊 <b>Weekly Report — every Sunday evening</b>\n"
                    "   A detailed breakdown sent to your dashboard every Sunday: "
                    "total water for the week, your daily average vs goal, how many "
                    "days you hit the target, your best day with volume, and a trend "
                    "— whether you improved or declined compared to last week.",
                    "",
                    "📈 <b>Charts — 7-day and 30-day</b>\n"
                    "   Visual progress right inside the dashboard — no image files. "
                    "Each day is a horizontal bar scaled to your goal. Locked on free. "
                    "Available immediately when you upgrade or during trial.",
                ],
                "trial": "\n🎁 <b>Try Premium free for 3 days</b> — all features on, no payment needed.\n"
                         "Tap the button below to start your trial now.",
            },
            "es": {
                "title": "⭐ <b>AquaBot Premium — Acceso de por vida</b>",
                "sub": f"<b>{PREMIUM_STARS}⭐ una vez · Sin suscripción · Sin renovación</b>",
                "free_header": "✅ <b>El plan gratuito incluye:</b>",
                "free": [
                    "  · Registrar agua en un toque",
                    "  · Recordatorios por intervalo (cada X minutos)",
                    "  · Estadísticas básicas (hoy, semana, mes, total)",
                    "  · Historial (últimos 14 días)",
                    "  · Logros y rachas",
                    "  · Ajustes, unidades, idioma",
                    "  🔒 Los gráficos están bloqueados en la versión gratuita",
                ],
                "premium_header": "\n⭐ <b>Premium añade:</b>",
                "premium": [
                    "🧠 <b>Recordatorios Inteligentes</b>\n"
                    "   Los recordatorios gratuitos dicen lo mismo cada vez. Los de "
                    "Premium leen tu situación: cuánto te falta para la meta, qué hora "
                    "es, si tu racha está en peligro. A las 7pm con 80% → motivador. "
                    "A las 9pm con 20% y una racha de 14 días → urgente. El mensaje "
                    "siempre encaja con el momento.",
                    "",
                    "🌤 <b>Meta Ajustada por Clima</b>\n"
                    "   Tu cuerpo necesita más agua cuando hace calor. Con una ciudad "
                    "configurada en Ajustes, el bot revisa el clima en directo a diario. "
                    "Más de 25°C → +200ml en tu meta. Más de 30°C → +400ml. "
                    "Más de 35°C → +600ml. Se reinicia automáticamente al día siguiente.",
                    "",
                    "⚡ <b>Modo Recuperación</b>\n"
                    "   ¿Mañana ocupada y casi no bebiste nada? Después de las 3pm, si "
                    "vas muy por detrás de tu meta diaria, los recordatorios aumentan "
                    "automáticamente — cada 30 minutos en lugar de tu intervalo habitual. "
                    "Una vez al día, vuelve a la normalidad. Totalmente automático.",
                    "",
                    "📊 <b>Reporte Semanal — cada domingo por la tarde</b>\n"
                    "   Un resumen detallado enviado a tu panel cada domingo: agua total "
                    "de la semana, promedio diario vs meta, cuántos días cumpliste el "
                    "objetivo, tu mejor día con volumen y una tendencia — si mejoraste "
                    "o empeoraste respecto a la semana anterior.",
                    "",
                    "📈 <b>Gráficos — 7 y 30 días</b>\n"
                    "   Progreso visual directamente en el panel — sin archivos de imagen. "
                    "Cada día es una barra horizontal escalada a tu meta. Bloqueado en "
                    "la versión gratuita. Disponible inmediatamente al actualizar o "
                    "durante la prueba.",
                ],
                "trial": "\n🎁 <b>Prueba Premium gratis durante 3 días</b> — todas las funciones, sin pago.\n"
                         "Pulsa el botón de abajo para iniciar tu prueba ahora.",
            },
            "de": {
                "title": "⭐ <b>AquaBot Premium — Lebenslanger Zugang</b>",
                "sub": f"<b>{PREMIUM_STARS}⭐ einmal · Kein Abo · Keine Verlängerung</b>",
                "free_header": "✅ <b>Free-Plan enthält:</b>",
                "free": [
                    "  · Wasser mit einem Tipp eintragen",
                    "  · Intervallerinnerungen (alle X Minuten)",
                    "  · Basis-Statistiken (heute, Woche, Monat, gesamt)",
                    "  · Verlauf (letzte 14 Tage)",
                    "  · Erfolge und Streaks",
                    "  · Einstellungen, Einheiten, Sprache",
                    "  🔒 Charts sind im Free-Plan gesperrt",
                ],
                "premium_header": "\n⭐ <b>Premium fügt hinzu:</b>",
                "premium": [
                    "🧠 <b>Smarte Erinnerungen</b>\n"
                    "   Free-Erinnerungen sagen jedes Mal dasselbe. Premium-Erinnerungen "
                    "lesen deine Situation: wie weit du vom Ziel entfernt bist, wie spät "
                    "es ist, ob dein Streak gefährdet ist. Um 19 Uhr mit 80% → "
                    "ermutigend. Um 21 Uhr mit 20% und 14-Tage-Streak → dringend. "
                    "Die Nachricht passt immer zum Moment.",
                    "",
                    "🌤 <b>Wetterbasiertes Tagesziel</b>\n"
                    "   Dein Körper braucht mehr Wasser, wenn es draußen heiß ist. "
                    "Mit einer in den Einstellungen gesetzten Stadt prüft der Bot "
                    "täglich das Live-Wetter. Über 25°C → +200ml zu deinem Ziel. "
                    "Über 30°C → +400ml. Über 35°C → +600ml. Setzt sich am "
                    "nächsten Tag automatisch zurück.",
                    "",
                    "⚡ <b>Aufholmodus</b>\n"
                    "   Stressiger Morgen und kaum etwas getrunken? Nach 15 Uhr, wenn "
                    "du deutlich hinter deinem Tagesziel liegst, kommen Erinnerungen "
                    "automatisch häufiger — alle 30 Minuten statt deines üblichen "
                    "Intervalls. Sobald du wieder auf Kurs bist, kehrt es zur Normalität "
                    "zurück. Vollautomatisch.",
                    "",
                    "📊 <b>Wochenbericht — jeden Sonntagabend</b>\n"
                    "   Eine detaillierte Aufschlüsselung, die jeden Sonntag an dein "
                    "Dashboard gesendet wird: Gesamtwasser der Woche, täglicher "
                    "Durchschnitt vs. Ziel, wie viele Tage du das Ziel erreicht hast, "
                    "dein bester Tag mit Volumen und ein Trend — ob du dich im "
                    "Vergleich zur Vorwoche verbessert oder verschlechtert hast.",
                    "",
                    "📈 <b>Diagramme — 7 und 30 Tage</b>\n"
                    "   Visueller Fortschritt direkt im Dashboard — keine Bilddateien. "
                    "Jeder Tag ist ein horizontaler Balken, der auf dein Ziel skaliert "
                    "ist. Im Free-Plan gesperrt. Sofort verfügbar nach dem Upgrade "
                    "oder während des Tests.",
                ],
                "trial": "\n🎁 <b>Premium 3 Tage kostenlos testen</b> — alle Features aktiv, keine Zahlung.\n"
                         "Tippe den Button unten, um deinen Test jetzt zu starten.",
            },
            "fr": {
                "title": "⭐ <b>AquaBot Premium — Accès à vie</b>",
                "sub": f"<b>{PREMIUM_STARS}⭐ une fois · Sans abonnement · Sans renouvellement</b>",
                "free_header": "✅ <b>Le plan gratuit inclut :</b>",
                "free": [
                    "  · Enregistrer l'eau en un tap",
                    "  · Rappels par intervalle (toutes les X minutes)",
                    "  · Statistiques de base (aujourd'hui, semaine, mois, total)",
                    "  · Historique (14 derniers jours)",
                    "  · Succès et séries",
                    "  · Réglages, unités, langue",
                    "  🔒 Les graphiques sont verrouillés sur la version gratuite",
                ],
                "premium_header": "\n⭐ <b>Premium ajoute :</b>",
                "premium": [
                    "🧠 <b>Rappels Intelligents</b>\n"
                    "   Les rappels gratuits disent la même chose à chaque fois. Les "
                    "rappels Premium lisent ta situation : où tu en es par rapport à "
                    "l'objectif, l'heure qu'il est, si ta série est menacée. À 19h "
                    "avec 80% → encourageant. À 21h avec 20% et une série de 14 jours "
                    "→ urgent. Le message correspond toujours au moment.",
                    "",
                    "🌤 <b>Objectif Ajusté à la Météo</b>\n"
                    "   Ton corps a besoin de plus d'eau quand il fait chaud. Avec une "
                    "ville renseignée dans les Réglages, le bot vérifie la météo en "
                    "direct chaque jour. Au-dessus de 25°C → +200ml à ton objectif. "
                    "Au-dessus de 30°C → +400ml. Au-dessus de 35°C → +600ml. "
                    "Se remet à zéro automatiquement le lendemain.",
                    "",
                    "⚡ <b>Mode Rattrapage</b>\n"
                    "   Matinée chargée et tu as à peine bu ? Après 15h, si tu es "
                    "nettement en retard sur ton objectif quotidien, les rappels "
                    "s'intensifient automatiquement — toutes les 30 minutes au lieu "
                    "de ton intervalle habituel. Dès que tu es de nouveau dans les "
                    "clous, ça revient à la normale. Entièrement automatique.",
                    "",
                    "📊 <b>Rapport Hebdomadaire — chaque dimanche soir</b>\n"
                    "   Un bilan détaillé envoyé à ton tableau de bord chaque dimanche : "
                    "eau totale de la semaine, moyenne journalière vs objectif, combien "
                    "de jours tu as atteint la cible, ton meilleur jour avec volume, "
                    "et une tendance — si tu t'es amélioré ou non vs la semaine d'avant.",
                    "",
                    "📈 <b>Graphiques — 7 et 30 jours</b>\n"
                    "   Progrès visuels directement dans le tableau de bord — pas de "
                    "fichiers image. Chaque jour est une barre horizontale mise à "
                    "l'échelle de ton objectif. Verrouillé sur la version gratuite. "
                    "Disponible immédiatement après la mise à niveau ou pendant l'essai.",
                ],
                "trial": "\n🎁 <b>Essaie Premium gratuitement pendant 3 jours</b> — toutes les fonctions, sans paiement.\n"
                         "Appuie sur le bouton ci-dessous pour démarrer ton essai maintenant.",
            },
            "ru": {
                "title": "⭐ <b>AquaBot Premium — Доступ навсегда</b>",
                "sub": f"<b>{PREMIUM_STARS}⭐ один раз · Без подписки · Без продления</b>",
                "free_header": "✅ <b>Бесплатный план включает:</b>",
                "free": [
                    "  · Запись воды одним нажатием",
                    "  · Напоминания по интервалу (каждые X минут)",
                    "  · Базовая статистика (сегодня, неделя, месяц, всё время)",
                    "  · История (последние 14 дней)",
                    "  · Достижения и серии",
                    "  · Настройки, единицы измерения, язык",
                    "  🔒 Графики заблокированы в бесплатном плане",
                ],
                "premium_header": "\n⭐ <b>Premium добавляет:</b>",
                "premium": [
                    "🧠 <b>Умные напоминания</b>\n"
                    "   Бесплатные напоминания говорят одно и то же каждый раз. "
                    "Premium-напоминания читают твою ситуацию: насколько ты близок "
                    "к цели, который час, под угрозой ли серия. В 19:00 при 80% → "
                    "ободряющее. В 21:00 при 20% и серии 14 дней → срочное. "
                    "Сообщение всегда соответствует моменту.",
                    "",
                    "🌤 <b>Цель по погоде</b>\n"
                    "   Твоему телу нужно больше воды в жару. Если город указан в "
                    "Настройках, бот ежедневно проверяет погоду в реальном времени. "
                    "Выше 25°C → +200мл к цели. Выше 30°C → +400мл. "
                    "Выше 35°C → +600мл. На следующий день автоматически сбрасывается.",
                    "",
                    "⚡ <b>Режим догонки</b>\n"
                    "   Напряжённое утро и почти ничего не выпил? После 15:00, если "
                    "ты значительно отстаёшь от дневной цели, напоминания приходят "
                    "чаще автоматически — каждые 30 минут вместо обычного интервала. "
                    "Как только вернёшься в норму — всё возвращается к обычному "
                    "ритму. Полностью автоматически.",
                    "",
                    "📊 <b>Недельный отчёт — каждое воскресенье вечером</b>\n"
                    "   Подробная сводка отправляется на дашборд каждое воскресенье: "
                    "общий объём воды за неделю, дневное среднее vs цель, сколько дней "
                    "цель была выполнена, лучший день с объёмом и тренд — улучшился "
                    "ли ты по сравнению с прошлой неделей.",
                    "",
                    "📈 <b>Графики — 7 и 30 дней</b>\n"
                    "   Визуальный прогресс прямо в дашборде — без картинок. Каждый "
                    "день — горизонтальная полоса, масштабированная по цели. "
                    "Заблокировано на бесплатном плане. Доступно сразу после "
                    "обновления или во время пробного периода.",
                ],
                "trial": "\n🎁 <b>Попробуй Premium бесплатно 3 дня</b> — все функции включены, оплаты нет.\n"
                         "Нажми кнопку ниже, чтобы начать пробный период прямо сейчас.",
            },
            "uk": {
                "title": "⭐ <b>AquaBot Premium — Доступ назавжди</b>",
                "sub": f"<b>{PREMIUM_STARS}⭐ один раз · Без підписки · Без продовження</b>",
                "free_header": "✅ <b>Безкоштовний план включає:</b>",
                "free": [
                    "  · Запис води одним натисканням",
                    "  · Нагадування за інтервалом (кожні X хвилин)",
                    "  · Базова статистика (сьогодні, тиждень, місяць, весь час)",
                    "  · Історія (останні 14 днів)",
                    "  · Досягнення та серії",
                    "  · Налаштування, одиниці, мова",
                    "  🔒 Графіки заблоковані на безкоштовному плані",
                ],
                "premium_header": "\n⭐ <b>Premium додає:</b>",
                "premium": [
                    "🧠 <b>Розумні нагадування</b>\n"
                    "   Безкоштовні нагадування говорять одне й те саме кожного разу. "
                    "Premium-нагадування читають вашу ситуацію: наскільки ви близькі "
                    "до мети, котра година, чи під загрозою серія. О 19:00 при 80% → "
                    "підбадьорливе. О 21:00 при 20% та серії 14 днів → термінове. "
                    "Повідомлення завжди відповідає моменту.",
                    "",
                    "🌤 <b>Мета за погодою</b>\n"
                    "   Вашому тілу потрібно більше води у спеку. Якщо місто вказано в "
                    "Налаштуваннях, бот щоденно перевіряє погоду в реальному часі. "
                    "Вище 25°C → +200мл до мети. Вище 30°C → +400мл. "
                    "Вище 35°C → +600мл. Наступного дня автоматично скидається.",
                    "",
                    "⚡ <b>Режим наздоганяння</b>\n"
                    "   Напружений ранок і майже нічого не випили? Після 15:00, якщо "
                    "ви значно відстаєте від денної мети, нагадування приходять "
                    "частіше автоматично — кожні 30 хвилин замість звичайного інтервалу. "
                    "Як тільки повернетеся в норму — все повертається до звичайного "
                    "ритму. Повністю автоматично.",
                    "",
                    "📊 <b>Тижневий звіт — кожної неділі ввечері</b>\n"
                    "   Детальна зведена відправляється на дашборд кожної неділі: "
                    "загальний обсяг води за тиждень, денне середнє vs мета, скільки днів "
                    "мета була виконана, кращий день з обсягом та тренд — покращились "
                    "ви порівняно з минулим тижнем чи ні.",
                    "",
                    "📈 <b>Графіки — 7 і 30 днів</b>\n"
                    "   Візуальний прогрес прямо в дашборді — без картинок. Кожний "
                    "день — горизонтальна смуга, масштабована за метою. "
                    "Заблоковано на безкоштовному плані. Доступно одразу після "
                    "оновлення або під час пробного періоду.",
                ],
                "trial": "\n🎁 <b>Спробуйте Premium безкоштовно 3 дні</b> — всі функції увімкнені, оплати немає.\n"
                         "Натисніть кнопку нижче, щоб почати пробний період прямо зараз.",
            },
        }[lang]
        return "\n".join(
            [data["title"], data["sub"], "", data["free_header"]]
            + data["free"]
            + [data["premium_header"]]
            + data["premium"]
            + [data["trial"]]
        )


def premium_activated_text(p: UserProfile) -> str:
    lang = lang_code(p)
    data = {
        "en": {
            "msg": "⭐ <b>Welcome to AquaBot Premium!</b>\n"
                   "<i>Lifetime access confirmed. Here's what's now on:</i>",
            "city_set":   f"🌤 Weather goals active for <b>{p.city}</b>",
            "city_unset": "🌤 <i>Tip: Set your city in Settings to enable weather-adjusted goals.</i>",
            "features": [
                "🧠 Smart reminders — context-aware every time",
                "⚡ Catch-up mode — auto-intensifies if you fall behind",
                "📊 Weekly report — arrives every Sunday evening",
                "📈 Charts — tap Charts on the home screen anytime",
            ],
            "footer": "Thank you for supporting AquaBot! 💙",
        },
        "es": {
            "msg": "⭐ <b>¡Bienvenido a AquaBot Premium!</b>\n"
                   "<i>Acceso de por vida confirmado. Esto es lo que está activo:</i>",
            "city_set":   f"🌤 Meta por clima activa para <b>{p.city}</b>",
            "city_unset": "🌤 <i>Consejo: Configura tu ciudad en Ajustes para metas por clima.</i>",
            "features": [
                "🧠 Recordatorios inteligentes — se adaptan cada vez",
                "⚡ Modo recuperación — se activa solo si vas atrasado",
                "📊 Reporte semanal — llega cada domingo por la tarde",
                "📈 Gráficos — pulsa Gráficos en inicio cuando quieras",
            ],
            "footer": "¡Gracias por apoyar AquaBot! 💙",
        },
        "de": {
            "msg": "⭐ <b>Willkommen bei AquaBot Premium!</b>\n"
                   "<i>Lebenslanger Zugang bestätigt. Das ist jetzt aktiv:</i>",
            "city_set":   f"🌤 Wetterziele aktiv für <b>{p.city}</b>",
            "city_unset": "🌤 <i>Tipp: Setze deine Stadt in den Einstellungen für wetterbasierte Ziele.</i>",
            "features": [
                "🧠 Smarte Erinnerungen — jedes Mal kontextbewusst",
                "⚡ Aufholmodus — aktiviert sich automatisch bei Rückstand",
                "📊 Wochenbericht — kommt jeden Sonntagabend",
                "📈 Diagramme — jederzeit auf Diagramme im Startbildschirm tippen",
            ],
            "footer": "Danke, dass du AquaBot unterstützt! 💙",
        },
        "fr": {
            "msg": "⭐ <b>Bienvenue sur AquaBot Premium !</b>\n"
                   "<i>Accès à vie confirmé. Voici ce qui est maintenant actif :</i>",
            "city_set":   f"🌤 Objectifs météo actifs pour <b>{p.city}</b>",
            "city_unset": "🌤 <i>Astuce : renseigne ta ville dans Réglages pour les objectifs météo.</i>",
            "features": [
                "🧠 Rappels intelligents — contextuels à chaque fois",
                "⚡ Mode rattrapage — s'intensifie automatiquement si retard",
                "📊 Rapport hebdo — arrive chaque dimanche soir",
                "📈 Graphiques — appuie sur Graphiques depuis l'accueil quand tu veux",
            ],
            "footer": "Merci de soutenir AquaBot ! 💙",
        },
        "ru": {
            "msg": "⭐ <b>Добро пожаловать в AquaBot Premium!</b>\n"
                   "<i>Пожизненный доступ подтверждён. Вот что теперь работает:</i>",
            "city_set":   f"🌤 Погодные цели активны для <b>{p.city}</b>",
            "city_unset": "🌤 <i>Совет: укажи город в Настройках для погодных целей.</i>",
            "features": [
                "🧠 Умные напоминания — каждый раз адаптируются к ситуации",
                "⚡ Режим догонки — автоматически включается при отставании",
                "📊 Недельный отчёт — приходит каждое воскресенье вечером",
                "📈 Графики — нажми Графики на главном экране в любое время",
            ],
            "footer": "Спасибо за поддержку AquaBot! 💙",
        },
        "uk": {
            "msg": "⭐ <b>Ласкаво просимо до AquaBot Premium!</b>\n"
                   "<i>Довічний доступ підтверджено. Ось що тепер працює:</i>",
            "city_set":   f"🌤 Погодні цілі активні для <b>{p.city}</b>",
            "city_unset": "🌤 <i>Порада: вкажіть місто в Налаштуваннях для погодних цілей.</i>",
            "features": [
                "🧠 Розумні нагадування — кожного разу адаптуються до ситуації",
                "⚡ Режим наздоганяння — автоматично вмикається при відставанні",
                "📊 Тижневий звіт — приходить кожної неділі ввечері",
                "📈 Графіки — натисніть Графіки на головному екрані в будь-який час",
            ],
            "footer": "Дякуємо за підтримку AquaBot! 💙",
        },
    }[lang]
    city_line = data["city_set"] if p.city else data["city_unset"]
    return "\n".join([data["msg"], "", city_line, ""] + data["features"] + ["", data["footer"]])


def trial_activated_text(p: UserProfile, expiry: str) -> str:
    lang = lang_code(p)
    data = {
        "en": {
            "title": "🎁 <b>3-Day Premium Trial Started!</b>",
            "expiry": f"<i>Full access until <b>{expiry}</b> — then free plan resumes.</i>",
            "intro": "Everything is on right now. Here's what to try first:",
            "tips": [
                "📈 <b>Tap Charts on the home screen</b> — see your 7-day intake as a visual bar chart",
                "🌤 <b>Set your city in Settings</b> — the bot will boost your goal on hot days automatically",
                "🧠 <b>Wait for your next reminder</b> — it'll feel different. Context-aware, not generic.",
                "📊 <b>If Sunday is coming up</b> — you'll receive your first weekly report automatically",
            ],
            "cta": f"After trial: upgrade for <b>{PREMIUM_STARS}⭐ once</b> — no subscription, yours forever.",
        },
        "es": {
            "title": "🎁 <b>¡Prueba Premium de 3 días iniciada!</b>",
            "expiry": f"<i>Acceso completo hasta <b>{expiry}</b> — luego vuelve el plan gratuito.</i>",
            "intro": "Todo está activo ahora mismo. Esto es lo que debes probar primero:",
            "tips": [
                "📈 <b>Pulsa Gráficos en la pantalla principal</b> — ve tu ingesta de 7 días en barras visuales",
                "🌤 <b>Configura tu ciudad en Ajustes</b> — el bot ajustará tu meta automáticamente en días calurosos",
                "🧠 <b>Espera tu próximo recordatorio</b> — se sentirá diferente. Contextual, no genérico.",
                "📊 <b>Si el domingo está cerca</b> — recibirás tu primer reporte semanal automáticamente",
            ],
            "cta": f"Tras la prueba: actualiza por <b>{PREMIUM_STARS}⭐ una vez</b> — sin suscripción, para siempre.",
        },
        "de": {
            "title": "🎁 <b>3-Tage-Premium-Test gestartet!</b>",
            "expiry": f"<i>Voller Zugang bis <b>{expiry}</b> — danach kehrt Free zurück.</i>",
            "intro": "Alles ist jetzt aktiv. Das solltest du zuerst ausprobieren:",
            "tips": [
                "📈 <b>Tippe auf Diagramme im Startbildschirm</b> — sieh deine 7-Tage-Aufnahme als Balkendiagramm",
                "🌤 <b>Setze deine Stadt in den Einstellungen</b> — der Bot erhöht dein Ziel automatisch an heißen Tagen",
                "🧠 <b>Warte auf deine nächste Erinnerung</b> — sie wird sich anders anfühlen. Kontextbewusst, nicht generisch.",
                "📊 <b>Wenn Sonntag naht</b> — du erhältst deinen ersten Wochenbericht automatisch",
            ],
            "cta": f"Nach dem Test: Upgrade für <b>{PREMIUM_STARS}⭐ einmal</b> — kein Abo, dauerhaft deins.",
        },
        "fr": {
            "title": "🎁 <b>Essai Premium de 3 jours lancé !</b>",
            "expiry": f"<i>Accès complet jusqu'au <b>{expiry}</b> — ensuite, retour au plan gratuit.</i>",
            "intro": "Tout est actif maintenant. Voici quoi essayer en premier :",
            "tips": [
                "📈 <b>Appuie sur Graphiques depuis l'accueil</b> — vois ton apport sur 7 jours en barres visuelles",
                "🌤 <b>Renseigne ta ville dans Réglages</b> — le bot boostera ton objectif automatiquement les jours chauds",
                "🧠 <b>Attends ton prochain rappel</b> — il sera différent. Contextuel, pas générique.",
                "📊 <b>Si dimanche approche</b> — tu recevras ton premier rapport hebdo automatiquement",
            ],
            "cta": f"Après l'essai : passe à Premium pour <b>{PREMIUM_STARS}⭐ une fois</b> — sans abonnement, à vie.",
        },
        "ru": {
            "title": "🎁 <b>Пробный Premium на 3 дня активирован!</b>",
            "expiry": f"<i>Полный доступ до <b>{expiry}</b> — затем вернётся бесплатный план.</i>",
            "intro": "Всё уже включено. Вот что попробовать в первую очередь:",
            "tips": [
                "📈 <b>Нажми Графики на главном экране</b> — увидишь потребление воды за 7 дней в виде диаграммы",
                "🌤 <b>Укажи город в Настройках</b> — бот будет автоматически увеличивать цель в жаркие дни",
                "🧠 <b>Дождись следующего напоминания</b> — оно будет другим. Контекстное, не шаблонное.",
                "📊 <b>Если воскресенье скоро</b> — первый недельный отчёт придёт автоматически",
            ],
            "cta": f"После пробного: обновись за <b>{PREMIUM_STARS}⭐ один раз</b> — без подписки, навсегда твоё.",
        },
        "uk": {
            "title": "🎁 <b>Пробний Premium на 3 дні активовано!</b>",
            "expiry": f"<i>Повний доступ до <b>{expiry}</b> — потім повернеться безкоштовний план.</i>",
            "intro": "Все вже увімкнено. Ось що спробувати в першу чергу:",
            "tips": [
                "📈 <b>Натисніть Графіки на головному екрані</b> — побачите споживання води за 7 днів у вигляді діаграми",
                "🌤 <b>Вкажіть місто в Налаштуваннях</b> — бот буде автоматично збільшувати мету у спекотні дні",
                "🧠 <b>Зачекайте наступного нагадування</b> — воно буде іншим. Контекстне, не шаблонне.",
                "📊 <b>Якщо неділя скоро</b> — перший тижневий звіт прийде автоматично",
            ],
            "cta": f"Після пробного: оновіться за <b>{PREMIUM_STARS}⭐ один раз</b> — без підписки, назавжди ваше.",
        },
    }[lang]
    tips_block = "\n".join(f"  {tip}" for tip in data["tips"])
    return "\n\n".join([
        data["title"],
        data["expiry"],
        data["intro"] + "\n" + tips_block,
        data["cta"],
    ])


def weekly_report_text(p: UserProfile) -> str:
    tz = get_tz(p.timezone)
    now = datetime.now(tz)
    history = get_history_totals(p.telegram_id, 8)
    goal = p.daily_goal_ml
    vals = [history.get((now - timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(6, -1, -1)]
    total = sum(vals)
    avg = total // 7 if total else 0
    goals_hit = sum(1 for v in vals if v >= goal)
    best = max(vals) if vals else 0
    best_idx = vals.index(best) if best else 0
    best_day = (now - timedelta(days=6 - best_idx)).strftime("%A")

    lang = lang_code(p)
    if lang == "es":
        if goals_hit == 7: insight = "🌟 Semana perfecta."
        elif goals_hit >= 5: insight = f"🔥 Muy bien: {goals_hit}/7 metas."
        elif goals_hit >= 3: insight = f"📈 Bien: {goals_hit}/7."
        else: insight = f"💡 Semana difícil: {goals_hit}/7."
    elif lang == "de":
        if goals_hit == 7: insight = "🌟 Perfekte Woche."
        elif goals_hit >= 5: insight = f"🔥 Stark: {goals_hit}/7 Ziele."
        elif goals_hit >= 3: insight = f"📈 Solide: {goals_hit}/7."
        else: insight = f"💡 Harte Woche: {goals_hit}/7."
    elif lang == "fr":
        if goals_hit == 7: insight = "🌟 Semaine parfaite."
        elif goals_hit >= 5: insight = f"🔥 Solide : {goals_hit}/7 objectifs."
        elif goals_hit >= 3: insight = f"📈 Correct : {goals_hit}/7."
        else: insight = f"💡 Semaine difficile : {goals_hit}/7."
    elif lang == "ru":
        if goals_hit == 7: insight = "🌟 Идеальная неделя."
        elif goals_hit >= 5: insight = f"🔥 Отлично: {goals_hit}/7 целей."
        elif goals_hit >= 3: insight = f"📈 Неплохо: {goals_hit}/7."
        else: insight = f"💡 Сложная неделя: {goals_hit}/7."
    elif lang == "uk":
        if goals_hit == 7: insight = "🌟 Ідеальний тиждень."
        elif goals_hit >= 5: insight = f"🔥 Чудово: {goals_hit}/7 цілей."
        elif goals_hit >= 3: insight = f"📈 Непогано: {goals_hit}/7."
        else: insight = f"💡 Складний тиждень: {goals_hit}/7."
    else:
        if goals_hit == 7: insight = "🌟 Perfect week."
        elif goals_hit >= 5: insight = f"🔥 Strong — {goals_hit}/7 goals. Keep it up!"
        elif goals_hit >= 3: insight = f"📈 Decent — {goals_hit}/7."
        else: insight = f"💡 Tough week: {goals_hit}/7."

    chart = text_chart(p, 7)
    title = {
        "en": "📊 <b>Weekly Report</b>",
        "es": "📊 <b>Reporte Semanal</b>",
        "de": "📊 <b>Wochenbericht</b>",
        "fr": "📊 <b>Rapport Hebdo</b>",
        "ru": "📊 <b>Недельный Отчет</b>",
        "uk": "📊 <b>Тижневий Звіт</b>",
    }[lang]
    lbl_total = {"en": "Total", "es": "Total", "de": "Gesamt", "fr": "Total", "ru": "Всего", "uk": "Всього"}[lang]
    lbl_avg = {"en": "Daily avg", "es": "Promedio diario", "de": "Tagesdurchschn.", "fr": "Moyenne/jour", "ru": "Среднее/день", "uk": "Середнє/день"}[lang]
    lbl_goals = {"en": "Goals hit", "es": "Metas cumplidas", "de": "Ziele erreicht", "fr": "Objectifs atteints", "ru": "Целей выполнено", "uk": "Цілей виконано"}[lang]
    lbl_best = {"en": "Best day", "es": "Mejor día", "de": "Bester Tag", "fr": "Meilleur jour", "ru": "Лучший день", "uk": "Кращий день"}[lang]
    lbl_streak = {"en": "Streak", "es": "Racha", "de": "Serie", "fr": "Série", "ru": "Серия", "uk": "Серія"}[lang]
    return "\n".join([
        title,
        f"<i>{(now - timedelta(days=6)).strftime('%d %b')} – {now.strftime('%d %b %Y')}</i>",
        "",
        f"{lbl_total}:     <b>{p.fmt(total)}</b>",
        f"{lbl_avg}: <b>{p.fmt(avg)}</b>  (goal: {p.fmt_goal()})",
        f"{lbl_goals}: <b>{goals_hit}/7</b>",
        f"{lbl_best}:  <b>{best_day}  {p.fmt(best)}</b>",
        f"{lbl_streak}:    🔥 <b>{p.streak_days}d</b>",
        "",
        insight,
        "",
        chart,
    ])


# ─────────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────────

def kb_home(p: UserProfile) -> InlineKeyboardMarkup:
    charts_label = t(p, "btn_charts")
    if not is_premium_active(p):
        charts_label = f"{charts_label}  🔒"

    skip_btn = (
        InlineKeyboardButton(t(p, "btn_resume_today"), callback_data=CB_UNSKIP_TODAY)
        if p.skip_today else
        InlineKeyboardButton(t(p, "btn_skip_today"),   callback_data=CB_SKIP_TODAY)
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(p, "btn_log"),     callback_data=CB_NAV_LOG),
         InlineKeyboardButton(t(p, "btn_stats"),   callback_data=CB_NAV_STATS),
         InlineKeyboardButton(charts_label,         callback_data=CB_NAV_CHARTS)],
        [InlineKeyboardButton(t(p, "btn_achievements"),  callback_data=CB_NAV_ACHIEVEMENTS),
         InlineKeyboardButton(t(p, "btn_history"),       callback_data=CB_NAV_HISTORY)],
        [InlineKeyboardButton(t(p, "btn_reminders"),     callback_data=CB_NAV_REMINDERS),
         InlineKeyboardButton(t(p, "btn_settings"),      callback_data=CB_NAV_SETTINGS)],
        [InlineKeyboardButton(t(p, "btn_premium"),       callback_data=CB_NAV_PREMIUM),
         InlineKeyboardButton(t(p, "btn_manage"),        callback_data=CB_NAV_DELETE)],
        [skip_btn],
    ])


def kb_log(p: UserProfile) -> InlineKeyboardMarkup:
    favs = p.favourite_amounts()
    fav_row = [InlineKeyboardButton(p.fmt(ml), callback_data=f"log:{ml}") for ml in favs]
    other = [ml for ml in [100, 150, 200, 250, 300, 350, 400, 500, 600, 750, 1000] if ml not in favs]
    rows = [fav_row]
    row: list = []
    for ml in other:
        row.append(InlineKeyboardButton(p.fmt(ml), callback_data=f"log:{ml}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows += [
        [InlineKeyboardButton(t(p, "btn_custom"), callback_data=CB_LOG_CUSTOM),
         InlineKeyboardButton(t(p, "btn_undo"),   callback_data=CB_LOG_UNDO)],
        [InlineKeyboardButton(t(p, "btn_back"),   callback_data=CB_NAV_HOME)],
    ]
    return InlineKeyboardMarkup(rows)


def kb_stats(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_HOME)]])


def kb_charts(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(p, "btn_7d"),  callback_data="chart:7"),
         InlineKeyboardButton(t(p, "btn_30d"), callback_data="chart:30")],
        [InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_HOME)],
    ])


def kb_reminders(p: UserProfile) -> InlineKeyboardMarkup:
    toggle = t(p, "btn_toggle_off") if p.reminders_enabled else t(p, "btn_toggle_on")
    iv = mins_label(p.reminder_interval_mins)
    qs = f"{p.quiet_start_hour:02d}:00"
    qe = f"{p.quiet_end_hour:02d}:00"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏱ {iv}", callback_data=CB_NOOP),
         InlineKeyboardButton("−15m", callback_data="rem:int:-15"),
         InlineKeyboardButton("+15m", callback_data="rem:int:15")],
        [InlineKeyboardButton(f"🌙 {qs}→{qe}", callback_data=CB_NOOP)],
        [InlineKeyboardButton(t(p, "btn_start_minus"), callback_data="rem:qs:-1"),
         InlineKeyboardButton(t(p, "btn_start_plus"),  callback_data="rem:qs:1"),
         InlineKeyboardButton(t(p, "btn_end_minus"),    callback_data="rem:qe:-1"),
         InlineKeyboardButton(t(p, "btn_end_plus"),    callback_data="rem:qe:1")],
        [InlineKeyboardButton(t(p, "btn_add_fixed"), callback_data=CB_REM_ADD),
         InlineKeyboardButton(t(p, "btn_remove_last"), callback_data=CB_REM_RM)],
        [InlineKeyboardButton(toggle if p.reminders_enabled else toggle, callback_data=CB_REM_TOGGLE),
         InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_HOME)],
    ])


def kb_settings(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎯 {p.fmt_goal()}", callback_data=CB_NOOP)],
        [InlineKeyboardButton(t(p, "btn_goal_custom"), callback_data="cfg:goal_custom")],
        [InlineKeyboardButton(t(p, "btn_activity"),    callback_data=CB_CFG_ACTIVITY),
         InlineKeyboardButton(t(p, "btn_unit"),        callback_data=CB_CFG_UNIT)],
        [InlineKeyboardButton(t(p, "btn_language"),    callback_data=CB_CFG_LANGUAGE),
         InlineKeyboardButton(t(p, "btn_city"),        callback_data="cfg:city")],
        [InlineKeyboardButton(t(p, "btn_recalc"),      callback_data=CB_CFG_RECALC)],
        [InlineKeyboardButton(t(p, "btn_back"),        callback_data=CB_NAV_HOME)],
    ])


def kb_delete(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(p, "btn_clear_today"),    callback_data=CB_DELETE_TODAY)],
        [InlineKeyboardButton(t(p, "btn_delete_day"),     callback_data=CB_DELETE_DAY_LIST)],
        [InlineKeyboardButton(t(p, "btn_wipe_all"),       callback_data=CB_DELETE_ALL_CONFIRM)],
        [InlineKeyboardButton(f"⚠️ {t(p, 'btn_delete_account')}", callback_data=CB_DELETE_ACCOUNT_CONFIRM)],
        [InlineKeyboardButton(t(p, "btn_back"),           callback_data=CB_NAV_HOME)],
    ])


def kb_premium(p: UserProfile) -> InlineKeyboardMarkup:
    rows = []
    is_prem = is_premium_active(p)
    is_trial = is_prem and p.premium_expiry != "lifetime"
    trial_expired = p.trial_used and not is_prem and p.premium_expiry != "lifetime"

    buy_label = f"{t(p, 'btn_buy')} {PREMIUM_STARS}⭐"
    upgrade_label = f"{t(p, 'btn_upgrade')} {PREMIUM_STARS}⭐"

    if not is_prem and not p.trial_used:
        rows.append([InlineKeyboardButton(t(p, "btn_trial"), callback_data=CB_PREM_START_TRIAL)])
        rows.append([InlineKeyboardButton(buy_label, callback_data=CB_PREM_BUY)])
    elif is_trial:
        rows.append([InlineKeyboardButton(buy_label, callback_data=CB_PREM_BUY)])
    elif trial_expired:
        rows.append([InlineKeyboardButton(upgrade_label, callback_data=CB_PREM_BUY)])

    rows.append([InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_HOME)])
    return InlineKeyboardMarkup(rows)


def kb_activity(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(data.get(lang, data["en"]), callback_data=f"activity:{key}")]
        for key, data in ACTIVITY_LEVELS.items()
    ])


def kb_language(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(data["lang_name"], callback_data=f"lang:{code}")] for code, data in STRINGS.items()]
        + [[InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_SETTINGS)]]
    )


def kb_back(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_HOME)]])


def kb_snooze(p: UserProfile) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5 min",   callback_data="snooze:5"),
         InlineKeyboardButton("15 min",  callback_data="snooze:15"),
         InlineKeyboardButton("30 min",  callback_data="snooze:30"),
         InlineKeyboardButton("1 hour",  callback_data="snooze:60")],
        [InlineKeyboardButton(t(p, "btn_skip_today"), callback_data=CB_SKIP_TODAY)],
        [InlineKeyboardButton(t(p, "btn_back_reminder"), callback_data="snooze:back")],
    ])


# ─────────────────────────────────────────────────────────────────
#  ONBOARDING — all within the single dashboard message
# ─────────────────────────────────────────────────────────────────

def ob_text_and_kb(p: UserProfile) -> Tuple[str, InlineKeyboardMarkup]:
    """Return (text, keyboard) for the current onboarding step."""
    state = p.state

    if state == State.OB_WELCOME:
        # Skip legacy welcome step and start onboarding from language selection.
        p.state = State.OB_LANGUAGE
        save_profile(p)
        state = State.OB_LANGUAGE

    if state == State.OB_LANGUAGE:
        return (
            ui(p, "choose_language"),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(d["lang_name"], callback_data=f"ob_lang:{c}")] for c, d in STRINGS.items()]
            )
        )

    if state == State.OB_WEIGHT:
        return (
            s(p, "ask_weight"),
            InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_back"), callback_data="ob:back_to_lang")]])
        )

    if state == State.OB_ACTIVITY:
        return (
            s(p, "ask_activity"),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(data.get(p.language, data["en"]), callback_data=f"ob_act:{key}")]
                 for key, data in ACTIVITY_LEVELS.items()]
                 + [[InlineKeyboardButton(t(p, "btn_back"), callback_data="ob:back_to_weight")]]
            )
        )

    if state == State.OB_CITY:
        return (
            s(p, "ask_city"),
            InlineKeyboardMarkup([
                [InlineKeyboardButton(t(p, "btn_skip"), callback_data="ob:skip_city")],
                [InlineKeyboardButton(t(p, "btn_back"), callback_data="ob:back_to_activity")],
            ])
        )

    if state == State.OB_UNIT:
        return (
            s(p, "ask_unit"),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💧 ml", callback_data="ob:unit:ml"),
                 InlineKeyboardButton("🥤 oz", callback_data="ob:unit:oz")],
                [InlineKeyboardButton(t(p, "btn_back"), callback_data="ob:back_to_city")],
            ])
        )

    # Fallback
    return (s(p, "welcome"), InlineKeyboardMarkup([]))


# ─────────────────────────────────────────────────────────────────
#  DASHBOARD — always last message
# ─────────────────────────────────────────────────────────────────

async def _delete_old_dashboard(context: ContextTypes.DEFAULT_TYPE, p: UserProfile) -> None:
    if p.dashboard_message_id and p.dashboard_chat_id:
        try:
            await context.bot.delete_message(
                chat_id=p.dashboard_chat_id,
                message_id=p.dashboard_message_id,
            )
        except Exception:
            pass
    p.dashboard_message_id = 0
    p.dashboard_chat_id = 0


async def send_dashboard(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    p: UserProfile,
    today: str,
    text: Optional[str] = None,
    kb: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Send a fresh dashboard at the bottom of the chat, deleting old one first."""
    await _delete_old_dashboard(context, p)
    final_text = text if text is not None else home_text(p, today)
    final_kb   = kb   if kb   is not None else kb_home(p)
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=final_text,
            reply_markup=final_kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.error("send_dashboard failed for %d: %s", chat_id, e)
        return
    p.dashboard_message_id = sent.message_id
    p.dashboard_chat_id = chat_id
    save_profile(p)
async def edit_dashboard(
    context: ContextTypes.DEFAULT_TYPE,
    p: UserProfile,
    today: str,
    text: Optional[str] = None,
    kb: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Edit existing dashboard in-place; falls back to send_dashboard."""
    final_text = text if text is not None else home_text(p, today)
    final_kb   = kb   if kb   is not None else kb_home(p)

    if not p.dashboard_message_id or not p.dashboard_chat_id:
        chat_id = p.dashboard_chat_id or p.telegram_id
        await send_dashboard(context, chat_id, p, today, final_text, final_kb)
        return
    try:
        await context.bot.edit_message_text(
            chat_id=p.dashboard_chat_id,
            message_id=p.dashboard_message_id,
            text=final_text,
            reply_markup=final_kb,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            pass
        elif any(x in err for x in ["message to edit not found", "message_id_invalid",
                                     "chat not found", "bot was blocked"]):
            logger.info("edit_dashboard: message not found, sending new dashboard")
            await send_dashboard(context, p.telegram_id, p, today, final_text, final_kb)
        else:
            logger.warning("edit_dashboard BadRequest: %s", e)
            await send_dashboard(context, p.telegram_id, p, today, final_text, final_kb)
    except TelegramError as e:
        logger.warning("edit_dashboard TelegramError: %s", e)
        await send_dashboard(context, p.telegram_id, p, today, final_text, final_kb)


# ─────────────────────────────────────────────────────────────────
#  SMART REMINDERS
# ─────────────────────────────────────────────────────────────────

def smart_reminder_text(p: UserProfile, today: str) -> str:
    drank = get_day_ml(p.telegram_id, today)
    goal = p.daily_goal_ml
    remaining = max(0, goal - drank)
    pct = min(100, int(drank / max(1, goal) * 100))
    tz = get_tz(p.timezone)
    hour = datetime.now(tz).hour
    lang = lang_code(p)

    heads = {
        "en": {
            "none": "💧 <b>You haven't had any water yet today!</b>",
            "low": "💧 <b>Only {pct}% done — time to hydrate!</b>",
            "mid": "💧 <b>Halfway there at {pct}% — keep going!</b>",
            "high": "💧 <b>Almost there — {remaining} to go!</b>",
            "top": "💧 <b>So close at {pct}%! Finish strong! 💪</b>",
            "risk1": "🔥 <i>Your {days}-day streak is at risk tonight!</i>",
            "risk2": "🔥 <i>{days}-day streak on the line — finish it!</i>",
            "weather": "🌡️ <i>{temp}°C in {city} — consider +{bonus} extra today</i>",
        },
        "es": {
            "none": "💧 <b>¡Aún no has bebido agua hoy!</b>",
            "low": "💧 <b>Solo {pct}% — ¡hora de hidratarte!</b>",
            "mid": "💧 <b>Vas por {pct}% — ¡sigue así!</b>",
            "high": "💧 <b>Casi llegas — faltan {remaining}!</b>",
            "top": "💧 <b>¡Muy cerca con {pct}%! 💪</b>",
            "risk1": "🔥 <i>Tu racha de {days} días está en riesgo hoy.</i>",
            "risk2": "🔥 <i>Tu racha de {days} días está en juego.</i>",
            "weather": "🌡️ <i>{temp}°C en {city} — considera +{bonus} hoy</i>",
        },
        "de": {
            "none": "💧 <b>Du hast heute noch kein Wasser getrunken!</b>",
            "low": "💧 <b>Erst {pct}% — jetzt trinken!</b>",
            "mid": "💧 <b>{pct}% geschafft — weiter so!</b>",
            "high": "💧 <b>Fast geschafft — noch {remaining}!</b>",
            "top": "💧 <b>Schon {pct}%! Zieh durch! 💪</b>",
            "risk1": "🔥 <i>Deine {days}-Tage-Serie ist heute in Gefahr.</i>",
            "risk2": "🔥 <i>{days}-Tage-Serie steht auf dem Spiel.</i>",
            "weather": "🌡️ <i>{temp}°C in {city} — heute +{bonus} extra einplanen</i>",
        },
        "fr": {
            "none": "💧 <b>Tu n'as pas encore bu d'eau aujourd'hui !</b>",
            "low": "💧 <b>Seulement {pct}% — il est temps de boire !</b>",
            "mid": "💧 <b>Déjà {pct}% — continue !</b>",
            "high": "💧 <b>Presque fini — encore {remaining} !</b>",
            "top": "💧 <b>Tu es à {pct}% ! Finis en beauté 💪</b>",
            "risk1": "🔥 <i>Ta série de {days} jours est en danger ce soir.</i>",
            "risk2": "🔥 <i>Ta série de {days} jours est en jeu.</i>",
            "weather": "🌡️ <i>{temp}°C à {city} — pense à +{bonus} aujourd'hui</i>",
        },
        "ru": {
            "none": "💧 <b>Ты еще не пил воду сегодня!</b>",
            "low": "💧 <b>Только {pct}% — пора пить воду!</b>",
            "mid": "💧 <b>Уже {pct}% — продолжай!</b>",
            "high": "💧 <b>Почти у цели — осталось {remaining}!</b>",
            "top": "💧 <b>Уже {pct}%! Дожми! 💪</b>",
            "risk1": "🔥 <i>Серия {days} дней сегодня под угрозой.</i>",
            "risk2": "🔥 <i>Серия {days} дней на кону.</i>",
            "weather": "🌡️ <i>{temp}°C в {city} — добавь еще +{bonus} сегодня</i>",
        },
        "uk": {
            "none": "💧 <b>Ви ще не пили воду сьогодні!</b>",
            "low": "💧 <b>Лише {pct}% — час пити воду!</b>",
            "mid": "💧 <b>Вже {pct}% — продовжуйте!</b>",
            "high": "💧 <b>Майже у мети — залишилось {remaining}!</b>",
            "top": "💧 <b>Вже {pct}%! Дотисніть! 💪</b>",
            "risk1": "🔥 <i>Серія {days} днів сьогодні під загрозою.</i>",
            "risk2": "🔥 <i>Серія {days} днів на кону.</i>",
            "weather": "🌡️ <i>{temp}°C в {city} — додайте ще +{bonus} сьогодні</i>",
        },
    }[lang]

    if pct == 0:       headline = heads["none"]
    elif pct < 30:     headline = heads["low"].format(pct=pct)
    elif pct < 60:     headline = heads["mid"].format(pct=pct)
    elif pct < 90:     headline = heads["high"].format(remaining=p.fmt(remaining))
    else:              headline = heads["top"].format(pct=pct)

    streak_note = ""
    if p.streak_days >= 3 and pct < 50 and hour >= 18:
        streak_note = "\n" + heads["risk1"].format(days=p.streak_days)
    elif p.streak_days >= 7 and pct < 80 and hour >= 20:
        streak_note = "\n" + heads["risk2"].format(days=p.streak_days)

    weather_note = ""
    if p.feature_weather:
        bonus, temp, desc = get_weather(p.city)
        if bonus:
            weather_note = "\n" + heads["weather"].format(temp=f"{temp:.0f}", city=p.city, bonus=p.fmt(bonus))

    progress = f"{pbar(drank, goal)}  {pct}%\n{p.fmt(drank)} / {p.fmt_goal()}"
    return f"{headline}\n\n{progress}{streak_note}\n\n<i>{get_tip(p)}</i>{weather_note}"


def build_notification_kb(p: UserProfile) -> InlineKeyboardMarkup:
    """Keyboard attached to reminder notification messages."""
    favs = p.favourite_amounts()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(p.fmt(ml), callback_data=f"notif_log:{ml}") for ml in favs],
        [InlineKeyboardButton(t(p, "btn_snooze"),  callback_data="notif_snooze"),
         InlineKeyboardButton(t(p, "btn_dismiss"), callback_data="notif_dismiss")],
    ])


async def _send_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, p: UserProfile, today: str) -> None:
    tz = get_tz(p.timezone)
    p.last_reminded = datetime.now(tz).isoformat()
    save_profile(p)
    
    if p.feature_smart_reminders:
        text = smart_reminder_text(p, today)
    else:
        drank = get_day_ml(p.telegram_id, today)
        goal = p.daily_goal_ml
        remaining = max(0, goal - drank)
        pct = min(100, int(drank / max(1, goal) * 100))
        lang = lang_code(p)
        title = {
            "en": "💧 <b>Time to hydrate!</b>",
            "es": "💧 <b>¡Hora de hidratarte!</b>",
            "de": "💧 <b>Zeit zu trinken!</b>",
            "fr": "💧 <b>Il est temps de boire !</b>",
            "ru": "💧 <b>Пора пить воду!</b>",
            "uk": "💧 <b>Час пити воду!</b>",
        }[lang]
        left_word = {
            "en": "left",
            "es": "restante",
            "de": "übrig",
            "fr": "restant",
            "ru": "осталось",
            "uk": "залишилось",
        }[lang]
        text = (
            f"{title}\n\n"
            f"{pbar(drank, goal)}  {pct}%\n"
            f"{p.fmt(drank)} / {p.fmt_goal()} · {p.fmt(remaining)} {left_word}\n\n"
            f"<i>{get_tip(p)}</i>"
        )
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=build_notification_kb(p),
            parse_mode=ParseMode.HTML
        )
        # Store the notification message id so snooze can reference it
        context.bot_data.setdefault("notif_msgs", {})[chat_id] = sent.message_id
        logger.info("Reminder sent to %d (msg_id=%d)", chat_id, sent.message_id)
    except TelegramError as e:
        logger.warning("Reminder failed for %d: %s", chat_id, e)


# ─────────────────────────────────────────────────────────────────
#  GLOBAL SCHEDULER
# ─────────────────────────────────────────────────────────────────

async def _global_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for uid in all_active_users():
        try:
            p = load_profile(uid)
            if not p.reminders_enabled or p.skip_today or is_quiet(p) or is_snoozed(p):
                continue
            run_reset(p)
            tz = get_tz(p.timezone)
            today = today_str(tz)
            drank = get_day_ml(uid, today)
            if drank >= p.daily_goal_ml:
                continue
            now = datetime.now(tz)
            cur_min = now.hour * 60 + now.minute

            eff_interval = p.reminder_interval_mins
            if p.feature_catchup:
                pct = drank / max(1, p.daily_goal_ml)
                if now.hour >= 15 and pct < 0.4:
                    eff_interval = min(eff_interval, 30)
                elif now.hour >= 18 and pct < 0.7:
                    eff_interval = min(eff_interval, 45)

            fired = False
            for fr in p.fixed_reminders:
                if fr.enabled and now.hour == fr.hour and now.minute == fr.minute:
                    await _send_reminder(context, uid, p, today)
                    fired = True
                    break

            if not fired and eff_interval > 0 and cur_min % eff_interval == 0:
                await _send_reminder(context, uid, p, today)
        except Exception as e:
            logger.warning("Scheduler error for %d: %s", uid, e)


async def _weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for uid in all_active_users():
        try:
            p = load_profile(uid)
            if not p.feature_weekly_report:
                continue
            tz = get_tz(p.timezone)
            today = today_str(tz)
            report = weekly_report_text(p)
            await send_dashboard(context, uid, p, today, report, kb_back(p))
        except Exception as e:
            logger.warning("Weekly report error for %d: %s", uid, e)


# ─────────────────────────────────────────────────────────────────
#  LOG HELPER
# ─────────────────────────────────────────────────────────────────

async def do_log(p: UserProfile, ml: int, today: str, tz: pytz.BaseTzInfo) -> str:
    insert_log(p.telegram_id, today, now_hhmm(tz), ml)
    p.total_ml_ever += ml
    p.log_amounts.append(ml)
    if len(p.log_amounts) > 40:
        p.log_amounts = p.log_amounts[-40:]
    p.state = State.IDLE
    new_ach = check_log_ach(p, today, tz)
    save_profile(p)

    drank = get_day_ml(p.telegram_id, today)
    remaining = max(0, p.daily_goal_ml - drank)
    just_hit = drank >= p.daily_goal_ml and drank - ml < p.daily_goal_ml
    msg = s(p, "goal_reached") if just_hit else s(p, "log_confirm",
                                                    amount=p.fmt(ml), remaining=p.fmt(remaining))
    unlocked_word = {
        "en": "unlocked",
        "es": "desbloqueado",
        "de": "freigeschaltet",
        "fr": "débloqué",
        "ru": "открыто",
        "uk": "відкрито",
    }[lang_code(p)]
    for k in new_ach:
        a = ACHIEVEMENTS[k]
        msg += f"\n🏅 {a['icon']} <b>{ach_text(p, k, 'name')}</b> {unlocked_word}!"
    return msg


# ─────────────────────────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    cleanup_map = context.bot_data.setdefault("cleanup_after_delete", {})
    pending_ids = cleanup_map.pop(chat_id, [])
    for msg_id in pending_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    p = load_profile(chat_id)

    if p.state != State.IDLE:
        # User is in onboarding — show or resend the onboarding dashboard
        ob_t, ob_kb = ob_text_and_kb(p)
        if p.dashboard_message_id and p.dashboard_chat_id:
            # Already have a dashboard message — just refresh it
            await edit_dashboard(context, p, today_str(get_tz(p.timezone)), ob_t, ob_kb)
        else:
            await send_dashboard(context, chat_id, p, today_str(get_tz(p.timezone)), ob_t, ob_kb)
        if update.message:
            try:
                await update.message.delete()
            except Exception:
                pass
        return

    run_reset(p)
    tz = get_tz(p.timezone)
    today = today_str(tz)
    # Avoid visible delete+resend flicker/lag when dashboard already exists.
    if p.dashboard_message_id and p.dashboard_chat_id:
        await edit_dashboard(context, p, today)
    else:
        await send_dashboard(context, chat_id, p, today)
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass


async def cmd_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    p = load_profile(chat_id)

    # If user is still onboarding, keep onboarding flow first.
    if p.state != State.IDLE:
        await cmd_start(update, context)
        return

    run_reset(p)
    tz = get_tz(p.timezone)
    today = today_str(tz)
    # Same anti-flicker logic as /start: edit existing dashboard if possible.
    if p.dashboard_message_id and p.dashboard_chat_id:
        await edit_dashboard(context, p, today, premium_text(p), kb_premium(p))
    else:
        await send_dashboard(context, chat_id, p, today, premium_text(p), kb_premium(p))
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass


async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    p = load_profile(chat_id)
    if p.state != State.IDLE:
        await cmd_start(update, context)
        return
    run_reset(p)
    tz = get_tz(p.timezone)
    today = today_str(tz)
    await send_dashboard(context, chat_id, p, today, s(p, "nav_log"), kb_log(p))


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    p = load_profile(chat_id)
    lang = lang_code(p)
    if not context.args:
        usage = {
            "en": "Usage: <code>/settz Region/City</code>\nExample: <code>/settz Europe/London</code>",
            "es": "Uso: <code>/settz Región/Ciudad</code>\nEjemplo: <code>/settz Europe/Madrid</code>",
            "de": "Nutzung: <code>/settz Region/Stadt</code>\nBeispiel: <code>/settz Europe/Berlin</code>",
            "fr": "Usage : <code>/settz Région/Ville</code>\nExemple : <code>/settz Europe/Paris</code>",
            "ru": "Использование: <code>/settz Регион/Город</code>\nПример: <code>/settz Europe/Moscow</code>",
            "uk": "Використання: <code>/settz Регіон/Місто</code>\nПриклад: <code>/settz Europe/Kyiv</code>",
        }[lang]
        await update.message.reply_text(
            usage,
            parse_mode=ParseMode.HTML)
        return
    tz_str = context.args[0]
    try:
        pytz.timezone(tz_str)
    except pytz.UnknownTimeZoneError:
        unknown = {
            "en": "❌ Unknown timezone",
            "es": "❌ Zona horaria desconocida",
            "de": "❌ Unbekannte Zeitzone",
            "fr": "❌ Fuseau horaire inconnu",
            "ru": "❌ Неизвестный часовой пояс",
            "uk": "❌ Невідомий часовий пояс",
        }[lang]
        await update.message.reply_text(f"{unknown}: <code>{tz_str}</code>", parse_mode=ParseMode.HTML)
        return
    p.timezone = tz_str
    run_reset(p)
    save_profile(p)
    tz = get_tz(p.timezone)
    today = today_str(tz)
    try:
        await update.message.delete()
    except Exception:
        pass
    await send_dashboard(context, chat_id, p, today, settings_text(p), kb_settings(p))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    p = load_profile(update.effective_chat.id)
    lang = lang_code(p)
    text = {
        "en": "💧 <b>AquaBot Commands</b>\n\n/start — Open dashboard\n/water — Quick-open log screen\n/settz Region/City — Change timezone\n  e.g. <code>/settz Europe/Berlin</code>\n/help — This message",
        "es": "💧 <b>Comandos de AquaBot</b>\n\n/start — Abrir panel\n/water — Abrir registro rápido\n/settz Región/Ciudad — Cambiar zona horaria\n  ej. <code>/settz Europe/Madrid</code>\n/help — Este mensaje",
        "de": "💧 <b>AquaBot Befehle</b>\n\n/start — Dashboard öffnen\n/water — Log-Bereich öffnen\n/settz Region/Stadt — Zeitzone ändern\n  z. B. <code>/settz Europe/Berlin</code>\n/help — Diese Nachricht",
        "fr": "💧 <b>Commandes AquaBot</b>\n\n/start — Ouvrir le tableau de bord\n/water — Ouvrir l'écran d'enregistrement\n/settz Région/Ville — Changer le fuseau\n  ex. <code>/settz Europe/Paris</code>\n/help — Ce message",
        "ru": "💧 <b>Команды AquaBot</b>\n\n/start — Открыть панель\n/water — Быстро открыть запись\n/settz Регион/Город — Сменить часовой пояс\n  напр. <code>/settz Europe/Moscow</code>\n/help — Это сообщение",
        "uk": "💧 <b>Команди AquaBot</b>\n\n/start — Відкрити панель\n/water — Швидко відкрити запис\n/settz Регіон/Місто — Змінити часовий пояс\n  напр. <code>/settz Europe/Kyiv</code>\n/help — Це повідомлення",
    }[lang]
    sent = await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await asyncio.sleep(20)
    for msg in [sent, update.message]:
        try:
            await msg.delete()
        except Exception:
            pass


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("❌ Access denied.")
        return
    uids = all_active_users()
    conn = db_connect()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    prem = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
    conn.close()
    if update.message:
        await update.message.reply_text(
            f"🛠 <b>Admin</b>\n\nTotal: <b>{total}</b>  Active: <b>{len(uids)}</b>  Premium: <b>{prem}</b>\n\n"
            "/broadcast msg\n/grant_premium uid [days|lifetime]\n/user_info uid",
            parse_mode=ParseMode.HTML,
        )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg_text = " ".join(context.args)
    sent_count = 0
    for uid in all_active_users():
        try:
            p = load_profile(uid)
            tz = get_tz(p.timezone)
            today = today_str(tz)
            await send_dashboard(context, uid, p, today,
                                 f"📢 <b>AquaBot News</b>\n\n{msg_text}", kb_back(p))
            sent_count += 1
        except Exception:
            pass
    if update.message:
        await update.message.reply_text(f"✅ Broadcast sent to {sent_count} users.")


async def cmd_grant_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /grant_premium uid [days|lifetime]")
        return
    try:
        uid = int(context.args[0])
        pp = load_profile(uid)
        pp.is_premium = True
        if len(context.args) >= 2 and context.args[1] == "lifetime":
            pp.premium_expiry = "lifetime"
        else:
            days = int(context.args[1]) if len(context.args) >= 2 else 30
            expiry = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
            pp.premium_expiry = expiry
            pp.trial_expiry = expiry
        save_profile(pp)
        if update.message:
            await update.message.reply_text(f"✅ Premium granted to {uid}: {pp.premium_expiry}")
    except Exception as e:
        if update.message:
            await update.message.reply_text(f"Error: {e}")


async def cmd_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        return
    try:
        uid = int(context.args[0])
        pp = load_profile(uid)
        if update.message:
            await update.message.reply_text(
                f"👤 {uid}\nGoal: {pp.daily_goal_ml}ml  Streak: {pp.streak_days}\n"
                f"Premium: {is_premium_active(pp)} ({pp.premium_expiry})\n"
                f"Trial used: {pp.trial_used}  Trial expiry: {pp.trial_expiry}\n"
                f"State: {pp.state.name}",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        if update.message:
            await update.message.reply_text(f"Error: {e}")


# ─────────────────────────────────────────────────────────────────
#  TEXT HANDLER
# ─────────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    p = load_profile(chat_id)
    text = (update.message.text or "").strip()
    tz = get_tz(p.timezone)
    today = today_str(tz)

    # Always delete the user's text message to keep the chat clean
    # (the dashboard is the one source of truth)
    try:
        await update.message.delete()
    except Exception:
        pass

    # ── Onboarding text inputs ──────────────────────────────────

    if p.state == State.OB_WEIGHT:
        try:
            w = float(text.replace(",", "."))
            assert 20 <= w <= 300
            p.weight_kg = w
            p.state = State.OB_ACTIVITY
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await edit_dashboard(context, p, today, ob_t, ob_kb)
        except (ValueError, AssertionError):
            ob_t, ob_kb = ob_text_and_kb(p)
            await edit_dashboard(context, p, today,
                ob_t + f"\n\n{s(p, 'ask_weight_err')}", ob_kb)
        return

    if p.state == State.OB_CITY:
        p.city = text[:64]  # Limit city name length
        p.state = State.OB_UNIT
        save_profile(p)
        ob_t, ob_kb = ob_text_and_kb(p)
        await edit_dashboard(context, p, today, ob_t, ob_kb)
        return

    # ── Post-onboarding text inputs ─────────────────────────────

    if p.state == State.AWAIT_CUSTOM_LOG:
        try:
            ml = int(re.sub(r"[^\d]", "", text))
            assert 1 <= ml <= 5000
            run_reset(p)
            msg_text = await do_log(p, ml, today, tz)
            await edit_dashboard(context, p, today,
                home_text(p, today) + f"\n\n<i>{msg_text}</i>", kb_home(p))
        except (ValueError, AssertionError):
            await edit_dashboard(context, p, today,
                home_text(p, today) + f"\n\n{ui(p, 'custom_log_err')}",
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_HOME)]]))
        return

    if p.state == State.AWAIT_FIXED_TIME:
        try:
            parts = text.strip().replace(".", ":").split(":")
            h, m = int(parts[0]), int(parts[1])
            assert 0 <= h <= 23 and 0 <= m <= 59
            p.fixed_reminders.append(FixedReminder(hour=h, minute=m))
            p.state = State.IDLE
            save_profile(p)
            await edit_dashboard(context, p, today, reminders_text(p), kb_reminders(p))
        except Exception:
            await edit_dashboard(context, p, today,
                reminders_text(p) + f"\n\n{ui(p, 'fixed_time_err')}",
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_REMINDERS)]]))
        return

    if p.state == State.AWAIT_RECALC_WEIGHT:
        try:
            w = float(text.replace(",", "."))
            assert 20 <= w <= 300
            p.weight_kg = w
            p.state = State.AWAIT_RECALC_ACT
            save_profile(p)
            await edit_dashboard(context, p, today, s(p, "ask_activity"), kb_activity(p.language))
        except (ValueError, AssertionError):
            await edit_dashboard(context, p, today,
                settings_text(p) + "\n\n⚠️ <i>Enter valid weight in kg (20–300)</i>",
                kb_settings(p))
        return

    if p.state == State.AWAIT_CUSTOM_GOAL:
        try:
            goal = int(re.sub(r"[^\d]", "", text))
            assert 500 <= goal <= 6000
            p.daily_goal_ml = int(round(goal / 50) * 50)
            p.state = State.IDLE
            if "customizer" not in p.achievements:
                p.achievements.append("customizer")
            save_profile(p)
            await edit_dashboard(context, p, today, settings_text(p), kb_settings(p))
        except Exception:
            await edit_dashboard(context, p, today,
                settings_text(p) + "\n\n⚠️ <i>Enter goal between 500 and 6000 ml</i>",
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_SETTINGS)]]))
        return

    if p.state == State.AWAIT_CITY_UPDATE:
        p.city = text[:64]
        p.state = State.IDLE
        if p.city and "city_setter" not in p.achievements:
            p.achievements.append("city_setter")
        save_profile(p)
        await edit_dashboard(context, p, today, settings_text(p), kb_settings(p))
        return

    unrecognized_msgs = {
        "en": "Sorry, I didn't understand that message. Please use the buttons or commands.",
        "es": "Lo siento, no entendí ese mensaje. Usa los botones o comandos.",
        "de": "Entschuldigung, diese Nachricht habe ich nicht verstanden. Bitte nutze die Buttons oder Befehle.",
        "fr": "Désolé, je n'ai pas compris ce message. Utilise les boutons ou les commandes.",
        "ru": "Извините, я не понял это сообщение. Используйте кнопки или команды.",
        "uk": "Вибачте, я не зрозумів це повідомлення. Використовуйте кнопки або команди.",
    }
    lang = lang_code(p)
    unrecognized_msg = unrecognized_msgs.get(lang, unrecognized_msgs["en"])
    await edit_dashboard(context, p, today,
        home_text(p, today) + f"\n\n⚠️ <i>{unrecognized_msg}</i>",
        InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_HOME)]]))


# ─────────────────────────────────────────────────────────────────
#  CALLBACK ROUTER
# ─────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    p = load_profile(chat_id)

    # Only call run_reset if user is past onboarding
    if p.state == State.IDLE:
        run_reset(p)

    tz = get_tz(p.timezone)
    today = today_str(tz)
    d: str = query.data or ""

    if not d or d == CB_NOOP:
        return

    async def dash(text=None, kb=None):
        await edit_dashboard(context, p, today, text, kb)

    # ── Notification callbacks ───────────────────────────────────

    if d.startswith("notif_log:"):
        try:
            ml = int(d.split(":")[1])
            run_reset(p)
            msg_text = await do_log(p, ml, today, tz)
            # Delete the notification message
            try:
                await query.delete_message()
            except Exception:
                pass
            await edit_dashboard(context, p, today,
                home_text(p, today) + f"\n\n<i>{msg_text}</i>", kb_home(p))
        except Exception as e:
            logger.warning("notif_log error: %s", e)
        return

    if d == "notif_dismiss":
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if d == "notif_snooze":
        # Transform the notification message into a snooze picker
        # (edit in-place so user is still "looking at the reminder")
        drank = get_day_ml(p.telegram_id, today)
        goal = p.daily_goal_ml
        pct = min(100, int(drank / max(1, goal) * 100))
        lang = lang_code(p)
        snooze_title = {
            "en": "⏰ <b>Snooze reminders?</b>",
            "es": "⏰ <b>¿Posponer recordatorios?</b>",
            "de": "⏰ <b>Erinnerungen schlummern?</b>",
            "fr": "⏰ <b>Reporter les rappels ?</b>",
            "ru": "⏰ <b>Отложить напоминания?</b>",
            "uk": "⏰ <b>Відкласти нагадування?</b>",
        }[lang]
        snooze_hint = {
            "en": "Choose how long to snooze, or skip the whole day.",
            "es": "Elige cuánto posponer o pausa todo el día.",
            "de": "Wähle die Dauer oder pausiere den ganzen Tag.",
            "fr": "Choisis la durée ou mets en pause toute la journée.",
            "ru": "Выбери время паузы или пропусти весь день.",
            "uk": "Оберіть час паузи або пропустіть весь день.",
        }[lang]
        snooze_text = (
            f"{snooze_title}\n\n"
            f"{pbar(drank, goal)}  {pct}%\n"
            f"{p.fmt(drank)} / {p.fmt_goal()}\n\n"
            f"<i>{snooze_hint}</i>"
        )
        try:
            await query.edit_message_text(
                snooze_text,
                reply_markup=kb_snooze(p),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    # ── Snooze actions ───────────────────────────────────────────

    if d.startswith("snooze:"):
        action = d.split(":", 1)[1]

        if action == "back":
            # Rebuild the notification message in-place (go back from snooze to notification)
            drank = get_day_ml(p.telegram_id, today)
            goal = p.daily_goal_ml
            pct = min(100, int(drank / max(1, goal) * 100))
            if p.feature_smart_reminders:
                notif_text = smart_reminder_text(p, today)
            else:
                remaining = max(0, goal - drank)
                lang = lang_code(p)
                title = {
                    "en": "💧 <b>Time to hydrate!</b>",
                    "es": "💧 <b>¡Hora de hidratarte!</b>",
                    "de": "💧 <b>Zeit zu trinken!</b>",
                    "fr": "💧 <b>Il est temps de boire !</b>",
                    "ru": "💧 <b>Пора пить воду!</b>",
                    "uk": "💧 <b>Час пити воду!</b>",
                }[lang]
                left_word = {
                    "en": "left",
                    "es": "restante",
                    "de": "übrig",
                    "fr": "restant",
                    "ru": "осталось",
                    "uk": "залишилось",
                }[lang]
                notif_text = (
                    f"{title}\n\n"
                    f"{pbar(drank, goal)}  {pct}%\n"
                    f"{p.fmt(drank)} / {p.fmt_goal()} · {p.fmt(remaining)} {left_word}\n\n"
                    f"<i>{get_tip(p)}</i>"
                )
            try:
                await query.edit_message_text(
                    notif_text,
                    reply_markup=build_notification_kb(p),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        try:
            mins = int(action)
            until = datetime.utcnow() + timedelta(minutes=mins)
            p.snooze_until = until.isoformat()
            save_profile(p)
            lang = lang_code(p)
            snooze_line = {
                "en": "⏰ <i>Snoozed for {mins} min — reminders resume at {time} UTC</i>",
                "es": "⏰ <i>Pospuesto {mins} min — vuelve a las {time} UTC</i>",
                "de": "⏰ <i>{mins} Min. verschoben — weiter um {time} UTC</i>",
                "fr": "⏰ <i>Reporté de {mins} min — reprise à {time} UTC</i>",
                "ru": "⏰ <i>Отложено на {mins} мин — продолжим в {time} UTC</i>",
                "uk": "⏰ <i>Відкладено на {mins} хв — продовжимо о {time} UTC</i>",
            }[lang]
            snooze_confirmation = (
                snooze_line.format(mins=mins, time=until.strftime("%H:%M"))
            )
            # Dismiss the snooze/notification message
            try:
                await query.delete_message()
            except Exception:
                pass
            # Show confirmation on the dashboard
            await edit_dashboard(context, p, today,
                home_text(p, today) + f"\n\n{snooze_confirmation}", kb_home(p))
        except (ValueError, TypeError):
            pass
        return

    # ── Skip / Unskip today ──────────────────────────────────────

    if d == CB_SKIP_TODAY:
        p.skip_today = True
        save_profile(p)
        await query.answer()
        await edit_dashboard(context, p, today, home_text(p, today), kb_home(p))
        return

    if d == CB_UNSKIP_TODAY:
        p.skip_today = False
        save_profile(p)
        await query.answer()
        await edit_dashboard(context, p, today, home_text(p, today), kb_home(p))
        return

    # ── Onboarding callbacks ─────────────────────────────────────

    if d.startswith("ob_lang:"):
        await query.answer()
        code = d.split(":")[1]
        if code in STRINGS:
            p.language = code
        # Stay in OB_LANGUAGE state — don't advance to OB_WEIGHT yet.
        # The user is shown the warm welcome + 3 choices.
        save_profile(p)

        lang = p.language
        welcome_txt = WELCOME_AFTER_LANG.get(lang, WELCOME_AFTER_LANG["en"])
        btns = WELCOME_BUTTONS.get(lang, WELCOME_BUTTONS["en"])

        await query.edit_message_text(
            welcome_txt,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(btns["customise"], callback_data="ob:start")],
                [InlineKeyboardButton(btns["quick"],     callback_data="ob:quick")],
                [InlineKeyboardButton(btns["later"],     callback_data=CB_OB_SETUP_LATER)],
            ]),
        )
        return

    if d.startswith("ob_act:"):
        await query.answer()
        key = d.split(":")[1]
        if key in ACTIVITY_LEVELS:
            p.activity_level = key
        p.daily_goal_ml = calc_goal(p.weight_kg, p.activity_level)
        p.state = State.OB_CITY
        save_profile(p)
        ob_t, ob_kb = ob_text_and_kb(p)
        await dash(ob_t, ob_kb)
        return

    if d.startswith("ob:"):
        action = d.split(":", 1)[1]

        if action == "start":
            await query.answer()
            p.state = State.OB_WEIGHT
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await dash(ob_t, ob_kb)

        elif action == "quick":
            await query.answer()
            p.weight_kg = 70
            p.activity_level = "moderate"
            p.daily_goal_ml = 2000
            p.unit = "ml"
            p.city = ""
            await finish_onboard(context, chat_id, p, query.message)

        elif action == "skip_city":
            await query.answer()
            p.city = ""
            p.state = State.OB_UNIT
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await dash(ob_t, ob_kb)

        elif action.startswith("unit:"):
            await query.answer()
            p.unit = action.split(":")[1]
            await finish_onboard(context, chat_id, p, query.message)

        elif action == "setup_later":
            await query.answer()
            # Skip all setup — use sensible defaults and go straight to dashboard
            p.weight_kg = 70.0
            p.activity_level = "moderate"
            p.daily_goal_ml = 2000
            p.unit = "ml"
            p.city = ""
            # Finish onboarding directly in dashboard (no extra chat messages)
            await finish_onboard(context, chat_id, p, query.message)

        # Back navigation
        elif action == "back_to_lang":
            await query.answer()
            p.state = State.OB_LANGUAGE
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await dash(ob_t, ob_kb)

        elif action == "back_to_weight":
            await query.answer()
            p.state = State.OB_WEIGHT
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await dash(ob_t, ob_kb)

        elif action == "back_to_activity":
            await query.answer()
            p.state = State.OB_ACTIVITY
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await dash(ob_t, ob_kb)

        elif action == "back_to_city":
            await query.answer()
            p.state = State.OB_CITY
            save_profile(p)
            ob_t, ob_kb = ob_text_and_kb(p)
            await dash(ob_t, ob_kb)

        return

    if d.startswith("activity:"):
        key = d.split(":")[1]
        if key in ACTIVITY_LEVELS:
            p.activity_level = key
        if p.state == State.AWAIT_RECALC_ACT:
            p.state = State.IDLE
            p.daily_goal_ml = calc_goal(p.weight_kg, p.activity_level)
            if "customizer" not in p.achievements:
                p.achievements.append("customizer")
            save_profile(p)
            await dash(settings_text(p), kb_settings(p))
        else:
            p.daily_goal_ml = calc_goal(p.weight_kg, p.activity_level)
            save_profile(p)
            await dash(settings_text(p), kb_settings(p))
        return

    if d.startswith("lang:"):
        code = d.split(":")[1]
        if code in STRINGS:
            p.language = code
        save_profile(p)
        # Refresh settings page in the newly selected language
        await dash(settings_text(p), kb_settings(p))
        return

    # ── Navigation ───────────────────────────────────────────────

    if d.startswith("nav:"):
        dest = d.split(":", 1)[1]
        # Clean up any pending invoice
        if dest != "premium":
            inv_id = context.user_data.pop("invoice_msg_id", None)
            if inv_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=inv_id)
                except Exception:
                    pass

        # Gate premium-only sections
        if dest == "charts" and not is_premium_active(p):
            await dash(premium_text(p), kb_premium(p))
            return

        nav_map = {
            "home":         (lambda: home_text(p, today),          lambda: kb_home(p)),
            "log":          (lambda: s(p, "nav_log"),              lambda: kb_log(p)),
            "stats":        (lambda: stats_text(p, today),         lambda: kb_stats(p)),
            "charts":       (lambda: s(p, "nav_charts"),           lambda: kb_charts(p)),
            "achievements": (lambda: achievements_text(p),         lambda: kb_back(p)),
            "history":      (lambda: history_text(p),              lambda: kb_back(p)),
            "reminders":    (lambda: reminders_text(p),            lambda: kb_reminders(p)),
            "settings":     (lambda: settings_text(p),             lambda: kb_settings(p)),
            "delete":       (lambda: s(p, "nav_delete"),           lambda: kb_delete(p)),
            "premium":      (lambda: premium_text(p),              lambda: kb_premium(p)),
        }
        if dest in nav_map:
            txt_fn, kb_fn = nav_map[dest]
            await dash(txt_fn(), kb_fn())
        return

    # ── Logging ──────────────────────────────────────────────────

    if d.startswith("log:"):
        action = d.split(":", 1)[1]
        if action == "undo":
            removed = undo_last_log(p.telegram_id, today)
            if removed:
                p.total_ml_ever = max(0, p.total_ml_ever - removed)
                save_profile(p)
                await dash(home_text(p, today) + f"\n\n↩️ <i>{p.fmt(removed)} {ui(p, 'log_removed')}</i>", kb_home(p))
            else:
                await dash(home_text(p, today) + f"\n\n<i>{ui(p, 'nothing_undo')}</i>", kb_home(p))
            return
        if action == "custom":
            p.state = State.AWAIT_CUSTOM_LOG
            save_profile(p)
            await dash(
                ui(p, "custom_log_prompt"),
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_LOG)]])
            )
            return
        try:
            ml = int(action)
        except ValueError:
            return
        run_reset(p)
        msg_text = await do_log(p, ml, today, tz)
        await dash(home_text(p, today) + f"\n\n<i>{msg_text}</i>", kb_home(p))
        return

    # ── Charts ───────────────────────────────────────────────────

    if d.startswith("chart:"):
        if not is_premium_active(p):
            await dash(premium_text(p), kb_premium(p))
            return
        try:
            days_back = int(d.split(":")[1])
        except (IndexError, ValueError):
            days_back = 7
        chart = text_chart(p, days_back)
        await dash(chart, kb_charts(p))
        return

    # ── Reminders ────────────────────────────────────────────────

    if d.startswith("rem:"):
        action = d.split(":", 1)[1]
        if action == "toggle":
            p.reminders_enabled = not p.reminders_enabled
        elif action.startswith("int:"):
            try:
                delta = int(action.split(":")[1])
                p.reminder_interval_mins = max(15, min(480, p.reminder_interval_mins + delta))
            except (IndexError, ValueError):
                pass
        elif action.startswith("qs:"):
            try:
                p.quiet_start_hour = (p.quiet_start_hour + int(action.split(":")[1])) % 24
            except (IndexError, ValueError):
                pass
        elif action.startswith("qe:"):
            try:
                p.quiet_end_hour = (p.quiet_end_hour + int(action.split(":")[1])) % 24
            except (IndexError, ValueError):
                pass
        elif action == "add":
            p.state = State.AWAIT_FIXED_TIME
            save_profile(p)
            await dash(
                reminders_text(p) + f"\n\n{ui(p, 'fixed_time_prompt')}",
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_REMINDERS)]])
            )
            return
        elif action == "rm":
            if p.fixed_reminders:
                removed = p.fixed_reminders.pop()
                await query.answer(f"{ui(p, 'log_removed').capitalize()} {removed.label()}", show_alert=False)
            else:
                await query.answer(ui(p, "no_fixed_remove"), show_alert=True)
        save_profile(p)
        await dash(reminders_text(p), kb_reminders(p))
        return

    # ── Settings ─────────────────────────────────────────────────

    if d.startswith("cfg:"):
        action = d.split(":", 1)[1]
        if action.startswith("goal:"):
            try:
                delta = int(action.split(":")[1])
                p.daily_goal_ml = max(500, min(6000, p.daily_goal_ml + delta))
                if "customizer" not in p.achievements:
                    p.achievements.append("customizer")
            except (IndexError, ValueError):
                pass
            save_profile(p)
        elif action == "unit":
            p.unit = "oz" if p.unit == "ml" else "ml"
            if "customizer" not in p.achievements:
                p.achievements.append("customizer")
            save_profile(p)
        elif action == "language":
            await dash(ui(p, "choose_language_short"), kb_language(p))
            return
        elif action == "activity":
            await dash(s(p, "ask_activity"), kb_activity(p.language))
            return
        elif action == "goal_custom":
            p.state = State.AWAIT_CUSTOM_GOAL
            save_profile(p)
            await dash(
                "🎯 <b>Set custom daily goal (ml)</b>\n\n"
                "Type a value between <b>500</b> and <b>6000</b>.\n"
                "Example: <code>2450</code>",
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_SETTINGS)]])
            )
            return
        elif action == "city":
            p.state = State.AWAIT_CITY_UPDATE
            save_profile(p)
            await dash(
                "📍 <b>Type your city</b>\n\n"
                "I use it for weather-based goal adjustments.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_SETTINGS)]
                ])
            )
            return
        elif action == "recalc":
            p.state = State.AWAIT_RECALC_WEIGHT
            save_profile(p)
            await dash(
                ui(p, "recalc_prompt"),
                InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_cancel"), callback_data=CB_NAV_SETTINGS)]])
            )
            return
        await dash(settings_text(p), kb_settings(p))
        return

    # ── Delete ───────────────────────────────────────────────────

    if d.startswith("delete:"):
        action = d.split(":", 1)[1]
        if action == "today":
            clear_day(p.telegram_id, today)
            await dash(home_text(p, today) + f"\n\n<i>{ui(p, 'today_cleared')}</i>", kb_home(p))
        elif action == "day_list":
            history = get_history_totals(p.telegram_id, 14)
            days = sorted(history.keys(), reverse=True)[:14]
            if not days:
                await query.answer(ui(p, "no_history_delete"), show_alert=True)
                return
            btns = [[InlineKeyboardButton(
                f"🗑 {day}  ({p.fmt(history[day])})",
                callback_data=f"delete:day:{day}"
            )] for day in days]
            btns.append([InlineKeyboardButton(t(p, "btn_back"), callback_data=CB_NAV_DELETE)])
            await dash(ui(p, "choose_day_delete"), InlineKeyboardMarkup(btns))
        elif action.startswith("day:"):
            day_key = action.split(":", 1)[1]
            clear_day(p.telegram_id, day_key)
            await dash(f"{s(p, 'nav_delete')}\n\n{ui(p, 'day_deleted', day=day_key)}", kb_delete(p))
        elif action == "all_confirm":
            await dash(
                ui(p, "wipe_all_confirm"),
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(p, "btn_yes_wipe"), callback_data=CB_DELETE_ALL_DO),
                     InlineKeyboardButton(t(p, "btn_cancel"),    callback_data=CB_NAV_DELETE)]
                ])
            )
        elif action == "all_do":
            clear_all_logs(p.telegram_id)
            p.streak_days = 0
            p.best_streak = 0
            p.total_ml_ever = 0
            p.achievements.clear()
            save_profile(p)
            await dash(home_text(p, today) + f"\n\n<i>{ui(p, 'all_wiped')}</i>", kb_home(p))
        elif action == "account_confirm":
            await dash(
                ui(p, "delete_account_confirm"),
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(p, "btn_delete_account_confirm"), callback_data=CB_DELETE_ACCOUNT_DO),
                     InlineKeyboardButton(t(p, "btn_cancel"),                callback_data=CB_NAV_SETTINGS)]
                ])
            )
        elif action == "account_do":
            cleanup_ids: List[int] = []
            if query.message:
                cleanup_ids.append(query.message.message_id)
            try:
                await query.answer(ui(p, "account_deleting"), show_alert=False)
            except Exception:
                pass
            try:
                await query.edit_message_text(ui(p, "account_deleting"), reply_markup=None)
            except Exception:
                pass
            clear_all_logs(p.telegram_id)
            conn = db_connect()
            conn.execute("DELETE FROM users WHERE telegram_id=?", (p.telegram_id,))
            conn.commit()
            conn.close()
            if p.telegram_id in _profile_cache:
                del _profile_cache[p.telegram_id]
            try:
                await context.bot.unpin_all_chat_messages(chat_id=chat_id)
            except Exception:
                pass
            try:
                deleted_msg = await context.bot.send_message(chat_id=chat_id, text=ui(p, "account_deleted"))
                cleanup_ids.append(deleted_msg.message_id)
            except Exception:
                pass
            if cleanup_ids:
                cleanup_map = context.bot_data.setdefault("cleanup_after_delete", {})
                cleanup_map[chat_id] = cleanup_ids
        return

    # ── Premium ──────────────────────────────────────────────────

    if d == CB_PREM_START_TRIAL:
        if p.trial_used:
            await query.answer(ui(p, "trial_used"), show_alert=True)
            return
        expiry = (datetime.utcnow() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")
        p.is_premium = True
        p.trial_used = True
        p.trial_expiry = expiry
        p.premium_expiry = expiry
        save_profile(p)
        trial_text = trial_activated_text(p, expiry)
        await dash(trial_text, kb_back(p))
        return

    if d == CB_PREM_BUY:
        try:
            invoice_msg = await context.bot.send_invoice(
                chat_id=chat_id,
                title="AquaBot Premium — Lifetime",
                description=(
                    "Smart context-aware reminders · Live weather-based goals · "
                    "Catch-up mode · Weekly reports · Charts — Pay once, yours forever."
                ),
                payload="premium_lifetime",
                currency="XTR",
                prices=[LabeledPrice("AquaBot Lifetime Premium", PREMIUM_STARS)],
                provider_token="",
            )
            context.user_data["invoice_msg_id"] = invoice_msg.message_id
        except TelegramError as e:
            logger.error("Invoice send failed for %d: %s", chat_id, e)
            await query.answer(ui(p, "payment_open_error"), show_alert=True)
        return


# ─────────────────────────────────────────────────────────────────
#  ONBOARDING FINISH
# ─────────────────────────────────────────────────────────────────

TRIAL_STARTED_MSG: Dict[str, str] = {
    "en": (
        "🎁 <b>Your 3-day Premium trial is now live!</b>\n"
        "Smart reminders, weather goals, charts — all on until {expiry}.\n"
        "Tap ⭐ Premium any time to see what's included."
    ),
    "es": (
        "🎁 <b>¡Tu prueba Premium de 3 días está activa!</b>\n"
        "Recordatorios inteligentes, meta por clima, gráficos — todo activo hasta {expiry}.\n"
        "Pulsa ⭐ Premium cuando quieras para ver qué incluye."
    ),
    "de": (
        "🎁 <b>Dein 3-Tage-Premium-Test ist jetzt aktiv!</b>\n"
        "Smarte Erinnerungen, Wetterziel, Diagramme — alles aktiv bis {expiry}.\n"
        "Tippe jederzeit auf ⭐ Premium, um zu sehen, was enthalten ist."
    ),
    "fr": (
        "🎁 <b>Ton essai Premium de 3 jours est lancé !</b>\n"
        "Rappels intelligents, objectif météo, graphiques — tout actif jusqu'au {expiry}.\n"
        "Appuie sur ⭐ Premium à tout moment pour voir ce qui est inclus."
    ),
    "ru": (
        "🎁 <b>Твой пробный период Premium на 3 дня активирован!</b>\n"
        "Умные напоминания, цель по погоде, графики — всё работает до {expiry}.\n"
        "Нажми ⭐ Premium в любое время, чтобы увидеть, что входит."
    ),
    "uk": (
        "🎁 <b>Ваш пробний період Premium на 3 дні активовано!</b>\n"
        "Розумні нагадування, мета за погодою, графіки — все працює до {expiry}.\n"
        "Натисніть ⭐ Premium в будь-який час, щоб побачити, що входить."
    ),
}

SETUP_DONE_MSG: Dict[str, str] = {
    "en":  "✅ <b>All set!</b>  Goal: <b>{goal}</b>  ·  Reminders every <b>{interval}</b>",
    "es":  "✅ <b>¡Listo!</b>  Meta: <b>{goal}</b>  ·  Cada <b>{interval}</b>",
    "de":  "✅ <b>Fertig!</b>  Ziel: <b>{goal}</b>  ·  Alle <b>{interval}</b>",
    "fr":  "✅ <b>C'est parti !</b>  Objectif : <b>{goal}</b>  ·  Toutes les <b>{interval}</b>",
    "ru":  "✅ <b>Готово!</b>  Цель: <b>{goal}</b>  ·  Напоминания каждые <b>{interval}</b>",
    "uk":  "✅ <b>Готово!</b>  Мета: <b>{goal}</b>  ·  Нагадування кожні <b>{interval}</b>",
}


async def finish_onboard(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    p: UserProfile,
    reply_msg: Message,
) -> None:
    p.daily_goal_ml = calc_goal(p.weight_kg, p.activity_level)
    p.state = State.IDLE

    tz = get_tz(p.timezone)
    today = today_str(tz)
    p.last_date_str = today
    save_profile(p)
    await send_dashboard(context, chat_id, p, today)
    logger.info(
        "User %d onboarded. Goal: %dml. Trial: %s",
        chat_id, p.daily_goal_ml, p.trial_expiry,
    )


# ─────────────────────────────────────────────────────────────────
#  PAYMENT HANDLERS
# ─────────────────────────────────────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.pre_checkout_query:
        await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    p = load_profile(chat_id)

    if not update.message.successful_payment:
        return
    if update.message.successful_payment.invoice_payload != "premium_lifetime":
        return

    p.is_premium = True
    p.premium_expiry = "lifetime"
    p.subscription_active = True
    p.trial_expiry = ""  # clear trial — user has lifetime now
    save_profile(p)

    tz = get_tz(p.timezone)
    today = today_str(tz)

    # Delete the "Payment received" system message
    try:
        await update.message.delete()
    except Exception:
        pass

    # Try to delete the invoice message (usually just before payment confirmation)
    inv_id = context.user_data.pop("invoice_msg_id", None)
    if inv_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=inv_id)
        except Exception:
            pass

    # Rich feedback — show what they got
    await send_dashboard(
        context, chat_id, p, today,
        premium_activated_text(p),
        InlineKeyboardMarkup([[InlineKeyboardButton(t(p, "btn_home"), callback_data=CB_NAV_HOME)]])
    )
    logger.info("User %d upgraded to lifetime premium", chat_id)


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        logger.error("Set TELEGRAM_BOT_TOKEN environment variable and re-run.")
        return

    db_init()
    logger.info("Starting AquaBot v4.0...")

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    for cmd, fn in [
        ("start",         cmd_start),
        ("stars",         cmd_stars),
        ("water",         cmd_water),
        ("settz",         cmd_settz),
        ("help",          cmd_help),
        ("admin",         cmd_admin),
        ("broadcast",     cmd_broadcast),
        ("grant_premium", cmd_grant_premium),
        ("user_info",     cmd_user_info),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    jq = app.job_queue
    if jq:
        import datetime as _dt
        jq.run_repeating(_global_reminder_job, interval=60, first=10, name="global_reminders")
        jq.run_daily(
            _weekly_report_job,
            time=_dt.time(19, 30, 0, tzinfo=pytz.utc),
            days=(6,),
            name="weekly_reports",
        )
        logger.info("Jobs scheduled: global_reminders (every 60s), weekly_reports (Sun 19:30 UTC)")
    else:
        logger.warning(
            "JobQueue unavailable. Install with: pip install 'python-telegram-bot[job-queue]'"
        )

    logger.info("AquaBot v4.0 running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
