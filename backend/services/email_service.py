"""
Email notification service — send deployment status emails via Gmail API.

Uses a GCP service account with domain-wide delegation to send emails
through the Gmail API.  Gracefully degrades when the service account is
not configured (logs a warning instead of crashing).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from jinja2 import Template

from config import Settings

logger = logging.getLogger("webdeploy.email_service")

# Gmail API scope required for sending emails
_GMAIL_SEND_SCOPE = ["https://www.googleapis.com/auth/gmail.send"]

# ── Shared CSS for report templates ─────────────────────────────────
_REPORT_CSS = """\
  body { margin: 0; padding: 0; background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .container { max-width: 750px; margin: 40px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .header { padding: 24px 32px; color: #ffffff; }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .header p { margin: 6px 0 0; font-size: 13px; opacity: 0.85; }
  .body { padding: 28px 32px; color: #1f2937; }
  .summary-row { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .summary-card { flex: 1; min-width: 100px; background: #f9fafb; border-radius: 8px; padding: 14px 10px; text-align: center; border: 1px solid #e5e7eb; }
  .summary-card .number { font-size: 26px; font-weight: 700; color: #2563EB; }
  .summary-card .number.blue { color: #2563EB; }
  .summary-card .number.gray { color: #6b7280; }
  .summary-card .number.green { color: #059669; }
  .summary-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }
  .section-title { font-size: 14px; font-weight: 600; color: #374151; margin: 24px 0 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th { background: #f9fafb; padding: 8px 10px; text-align: left; font-size: 11px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #e5e7eb; }
  td { padding: 8px 10px; font-size: 13px; color: #374151; border-bottom: 1px solid #f3f4f6; }
  tr:hover td { background: #f9fafb; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge-yes { background: #dcfce7; color: #166534; }
  .badge-no { background: #f3f4f6; color: #6b7280; }
  .badge-blue { background: #dbeafe; color: #1e40af; }
  .badge-gray { background: #f3f4f6; color: #374151; }
  .footer { padding: 16px 32px; background: #f9fafb; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; }
"""

# ── HTML email template (deployment notifications) ──────────────────
_EMAIL_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WebDeploy Notification</title>
<style>
  body { margin: 0; padding: 0; background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .container { max-width: 600px; margin: 40px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .header { padding: 24px 32px; color: #ffffff; }
  .header-success { background: linear-gradient(135deg, #10b981, #059669); }
  .header-failed { background: linear-gradient(135deg, #ef4444, #dc2626); }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .header p { margin: 6px 0 0; font-size: 13px; opacity: 0.85; }
  .body { padding: 28px 32px; color: #1f2937; }
  .field { margin-bottom: 16px; }
  .field-label { font-size: 12px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .field-value { font-size: 15px; color: #111827; }
  .field-value a { color: #2563eb; text-decoration: none; }
  .field-value a:hover { text-decoration: underline; }
  .summary-box { background: #f9fafb; border-left: 4px solid #6366f1; padding: 14px 18px; margin: 20px 0; border-radius: 0 6px 6px 0; font-size: 14px; line-height: 1.6; color: #374151; }
  .error-box { background: #fef2f2; border-left: 4px solid #ef4444; padding: 14px 18px; margin: 20px 0; border-radius: 0 6px 6px 0; font-size: 14px; line-height: 1.6; color: #991b1b; font-family: monospace; white-space: pre-wrap; word-break: break-word; }
  .footer { padding: 16px 32px; background: #f9fafb; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; }
</style>
</head>
<body>
<div class="container">
  <div class="header {{ 'header-success' if success else 'header-failed' }}">
    <h1>{{ '&#9989;' if success else '&#10060;' }} {{ website_name }} &mdash; Deployment {{ 'Succeeded' if success else 'Failed' }}</h1>
    <p>{{ mode | upper }} mode &bull; {{ timestamp }}</p>
  </div>

  <div class="body">
    <div class="field">
      <div class="field-label">Website</div>
      <div class="field-value">{{ website_name }}</div>
    </div>

    <div class="field">
      <div class="field-label">Mode</div>
      <div class="field-value">{{ mode | upper }}</div>
    </div>

    <div class="field">
      <div class="field-label">Status</div>
      <div class="field-value">{{ 'SUCCESS' if success else 'FAILED' }}</div>
    </div>

    {% if success and live_url %}
    <div class="field">
      <div class="field-label">Live URL</div>
      <div class="field-value"><a href="{{ live_url }}">{{ live_url }}</a></div>
    </div>
    {% endif %}

    {% if ai_summary %}
    <div class="field">
      <div class="field-label">AI Summary</div>
      <div class="summary-box">{{ ai_summary }}</div>
    </div>
    {% endif %}

    {% if not success and error_message %}
    <div class="field">
      <div class="field-label">Error Details</div>
      <div class="error-box">{{ error_message }}</div>
    </div>
    {% endif %}
  </div>

  <div class="footer">
    Sent by <strong>WebDeploy</strong> &bull; {{ timestamp }}
  </div>
</div>
</body>
</html>
""")


# ── Daily report template (enhanced with AI split) ──────────────────
_DAILY_REPORT_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rapport Quotidien WebDeploy</title>
<style>""" + _REPORT_CSS + """</style>
</head>
<body>
<div class="container">
  <div class="header" style="background: linear-gradient(135deg, #2563EB, #1d4ed8);">
    <h1>&#128202; Rapport Quotidien WebDeploy</h1>
    <p>{{ report_date }}</p>
  </div>

  <div class="body">
    <div class="summary-row">
      <div class="summary-card">
        <div class="number">{{ total_deployments }}</div>
        <div class="label">Total</div>
      </div>
      <div class="summary-card">
        <div class="number blue">{{ with_ai_count }}</div>
        <div class="label">Avec IA</div>
      </div>
      <div class="summary-card">
        <div class="number gray">{{ without_ai_count }}</div>
        <div class="label">Sans IA</div>
      </div>
      <div class="summary-card">
        <div class="number">{{ total_sites }}</div>
        <div class="label">Sites</div>
      </div>
      <div class="summary-card">
        <div class="number">{{ total_deployers }}</div>
        <div class="label">D&eacute;ployeurs</div>
      </div>
    </div>

    {% if deployers %}
    <div class="section-title">Activit&eacute; par D&eacute;ployeur</div>
    <table>
      <thead>
        <tr>
          <th>D&eacute;ployeur</th>
          <th>Total</th>
          <th>Avec IA</th>
          <th>Sans IA</th>
          <th>Sites</th>
          <th>Modes</th>
          <th>Co&ucirc;t AI</th>
        </tr>
      </thead>
      <tbody>
        {% for d in deployers %}
        <tr>
          <td>{{ d.name }}</td>
          <td><strong>{{ d.count }}</strong></td>
          <td><span class="badge badge-blue">{{ d.with_ai }}</span></td>
          <td><span class="badge badge-gray">{{ d.without_ai }}</span></td>
          <td>{{ d.sites }}</td>
          <td>{{ d.modes }}</td>
          <td>{{ d.ai_cost }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p style="text-align:center; color:#6b7280; padding:24px 0;">Aucun d&eacute;ploiement aujourd'hui.</p>
    {% endif %}
  </div>

  <div class="footer">
    Envoy&eacute; par <strong>WebDeploy</strong> &bull; Rapport automatique quotidien
  </div>
</div>
</body>
</html>
""")


# ── Period report template (weekly / monthly) ───────────────────────
_PERIOD_REPORT_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rapport {{ report_type_label }} WebDeploy</title>
<style>""" + _REPORT_CSS + """</style>
</head>
<body>
<div class="container">
  <div class="header" style="background: linear-gradient(135deg, {{ header_color_start }}, {{ header_color_end }});">
    <h1>&#128202; Rapport {{ report_type_label }} WebDeploy</h1>
    <p>{{ period_label }}</p>
  </div>

  <div class="body">
    <div class="summary-row">
      <div class="summary-card">
        <div class="number">{{ stats.total_count }}</div>
        <div class="label">Total Pipelines</div>
      </div>
      <div class="summary-card">
        <div class="number blue">{{ stats.with_ai_count }}</div>
        <div class="label">Avec IA</div>
      </div>
      <div class="summary-card">
        <div class="number gray">{{ stats.without_ai_count }}</div>
        <div class="label">Sans IA</div>
      </div>
      <div class="summary-card">
        <div class="number green">{{ stats.average_per_day }}</div>
        <div class="label">Moy/Jour</div>
      </div>
      <div class="summary-card">
        <div class="number">{{ stats.per_deployer | length }}</div>
        <div class="label">D&eacute;ployeurs</div>
      </div>
    </div>

    {% if stats.daily_breakdown %}
    <div class="section-title">D&eacute;tail Journalier</div>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Jour</th>
          <th>Total</th>
          <th>Avec IA</th>
          <th>Sans IA</th>
        </tr>
      </thead>
      <tbody>
        {% for day in stats.daily_breakdown %}
        <tr>
          <td>{{ day.date }}</td>
          <td>{{ day.day_name }}</td>
          <td><strong>{{ day.total }}</strong></td>
          <td><span class="badge badge-blue">{{ day.with_ai }}</span></td>
          <td><span class="badge badge-gray">{{ day.without_ai }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}

    {% if stats.per_deployer %}
    <div class="section-title">Activit&eacute; par D&eacute;ployeur</div>
    <table>
      <thead>
        <tr>
          <th>D&eacute;ployeur</th>
          <th>Email</th>
          <th>Total</th>
          <th>Avec IA</th>
          <th>Sans IA</th>
          <th>Sites</th>
          <th>Co&ucirc;t AI</th>
        </tr>
      </thead>
      <tbody>
        {% for d in stats.per_deployer %}
        <tr>
          <td>{{ d.name }}</td>
          <td>{{ d.email }}</td>
          <td><strong>{{ d.total }}</strong></td>
          <td><span class="badge badge-blue">{{ d.with_ai }}</span></td>
          <td><span class="badge badge-gray">{{ d.without_ai }}</span></td>
          <td>{{ d.websites | join(', ') }}</td>
          <td>{{ d.ai_cost }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}

    {% if stats.per_status %}
    <div class="section-title">Distribution par Statut</div>
    <div class="summary-row">
      {% for status, count in stats.per_status.items() %}
      <div class="summary-card">
        <div class="number" style="color: {{ '#059669' if status == 'success' else '#ef4444' if status == 'failed' else '#3b82f6' if status == 'running' else '#6b7280' }};">{{ count }}</div>
        <div class="label">{{ status | capitalize }}</div>
      </div>
      {% endfor %}
    </div>
    {% endif %}

    {% if stats.ai_tokens.input_tokens > 0 or stats.ai_tokens.output_tokens > 0 %}
    <div class="section-title">Utilisation IA</div>
    <div class="summary-row">
      <div class="summary-card">
        <div class="number blue">{{ '{:,}'.format(stats.ai_tokens.input_tokens) }}</div>
        <div class="label">Tokens Input</div>
      </div>
      <div class="summary-card">
        <div class="number blue">{{ '{:,}'.format(stats.ai_tokens.output_tokens) }}</div>
        <div class="label">Tokens Output</div>
      </div>
      <div class="summary-card">
        <div class="number green">${{ '%.4f' % stats.ai_tokens.estimated_cost_usd }}</div>
        <div class="label">Co&ucirc;t Estim&eacute;</div>
      </div>
    </div>
    {% endif %}
  </div>

  <div class="footer">
    Envoy&eacute; par <strong>WebDeploy</strong> &bull; Rapport automatique {{ report_type_label | lower }}
  </div>
</div>
</body>
</html>
""")


# ── Personalized deployer report template ───────────────────────────
_PERSONALIZED_REPORT_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Votre Rapport WebDeploy</title>
<style>""" + _REPORT_CSS + """</style>
</head>
<body>
<div class="container">
  <div class="header" style="background: linear-gradient(135deg, #6366f1, #4f46e5);">
    <h1>&#128100; Votre Rapport de D&eacute;ploiements</h1>
    <p>{{ deployer_name }} &bull; {{ period_label }}</p>
  </div>

  <div class="body">
    <div class="summary-row">
      <div class="summary-card">
        <div class="number">{{ deployer.total }}</div>
        <div class="label">Vos Pipelines</div>
      </div>
      <div class="summary-card">
        <div class="number blue">{{ deployer.with_ai }}</div>
        <div class="label">Avec IA</div>
      </div>
      <div class="summary-card">
        <div class="number gray">{{ deployer.without_ai }}</div>
        <div class="label">Sans IA</div>
      </div>
      <div class="summary-card">
        <div class="number green">{{ deployer.ai_cost }}</div>
        <div class="label">Co&ucirc;t IA</div>
      </div>
    </div>

    {% if deployer.websites %}
    <div class="section-title">Vos Sites D&eacute;ploy&eacute;s</div>
    <table>
      <thead>
        <tr><th>Site</th><th>Modes</th></tr>
      </thead>
      <tbody>
        {% for site in deployer.websites %}
        <tr><td>{{ site }}</td><td>{{ deployer.modes | join(', ') }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}

    <div class="section-title">Comparaison avec l'&Eacute;quipe</div>
    <div class="summary-row">
      <div class="summary-card">
        <div class="number">{{ overall.total_count }}</div>
        <div class="label">&Eacute;quipe Total</div>
      </div>
      <div class="summary-card">
        <div class="number">{{ overall.average_per_day }}</div>
        <div class="label">Moy/Jour &Eacute;quipe</div>
      </div>
      <div class="summary-card">
        <div class="number" style="color: #6366f1;">{{ pct_of_team }}%</div>
        <div class="label">Votre Part</div>
      </div>
    </div>
  </div>

  <div class="footer">
    Envoy&eacute; par <strong>WebDeploy</strong> &bull; Rapport personnalis&eacute;
  </div>
</div>
</body>
</html>
""")


# ── Approval request email template ───────────────────────────────────
_APPROVAL_REQUEST_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nouvelle demande d'acc&egrave;s WebDeploy</title>
<style>
  body { margin: 0; padding: 0; background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .container { max-width: 600px; margin: 40px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .header { padding: 24px 32px; background: linear-gradient(135deg, #f59e0b, #d97706); color: #ffffff; }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .body { padding: 28px 32px; color: #1f2937; }
  .field { margin-bottom: 16px; }
  .field-label { font-size: 12px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .field-value { font-size: 15px; color: #111827; }
  .actions { display: flex; gap: 12px; margin-top: 24px; }
  .btn { display: inline-block; padding: 12px 28px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px; text-align: center; }
  .btn-approve { background: #059669; color: #ffffff; }
  .btn-reject { background: #dc2626; color: #ffffff; }
  .footer { padding: 16px 32px; background: #f9fafb; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>&#128100; Nouvelle demande d'acc&egrave;s</h1>
  </div>
  <div class="body">
    <div class="field">
      <div class="field-label">Nom</div>
      <div class="field-value">{{ display_name }}</div>
    </div>
    <div class="field">
      <div class="field-label">Email</div>
      <div class="field-value">{{ user_email }}</div>
    </div>
    <div class="field">
      <div class="field-label">R&ocirc;le demand&eacute;</div>
      <div class="field-value">{{ requested_role }}</div>
    </div>
    <div class="actions">
      <a href="{{ approve_url }}" class="btn btn-approve">&#9989; Approuver</a>
      <a href="{{ reject_url }}" class="btn btn-reject">&#10060; Rejeter</a>
    </div>
  </div>
  <div class="footer">
    Envoy&eacute; par <strong>WebDeploy</strong>
  </div>
</div>
</body>
</html>
""")


# ── Quota increase request email template ───────────────────────────
_QUOTA_REQUEST_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Demande d'augmentation de quota</title>
<style>
  body { margin: 0; padding: 0; background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .container { max-width: 600px; margin: 40px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .header { padding: 24px 32px; background: linear-gradient(135deg, #6366f1, #4338ca); color: #ffffff; }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .body { padding: 28px 32px; color: #1f2937; }
  .field { margin-bottom: 16px; }
  .field-label { font-size: 12px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .field-value { font-size: 15px; color: #111827; }
  .box { background: #f9fafb; border-left: 4px solid #6366f1; padding: 14px 18px; margin: 12px 0; border-radius: 0 6px 6px 0; font-size: 13px; color: #374151; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0; }
  td { padding: 6px 10px; font-size: 13px; border-bottom: 1px solid #f3f4f6; }
  td.k { color: #6b7280; width: 55%; }
  td.v { color: #111827; font-weight: 600; text-align: right; }
  .cta { display: inline-block; margin-top: 16px; padding: 10px 24px; background: #6366f1; color: #ffffff; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px; }
  .footer { padding: 16px 32px; background: #f9fafb; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>&#128200; Demande d'augmentation de quota</h1>
  </div>
  <div class="body">
    <div class="field">
      <div class="field-label">Utilisateur</div>
      <div class="field-value">{{ display_name }} &lt;{{ user_email }}&gt;</div>
    </div>
    <div class="field">
      <div class="field-label">R&ocirc;le actuel</div>
      <div class="field-value">{{ role }}</div>
    </div>

    <div class="field-label" style="margin-top:18px;">Quota actuel &rarr; demand&eacute;</div>
    <table>
      {% for k, label in fields %}
      <tr>
        <td class="k">{{ label }}</td>
        <td class="v">{{ current.get(k, '-') }} &rarr; <span style="color:#4338ca;">{{ requested.get(k, '-') }}</span></td>
      </tr>
      {% endfor %}
    </table>

    {% if reason %}
    <div class="field-label" style="margin-top:18px;">Justification</div>
    <div class="box">{{ reason }}</div>
    {% endif %}

    <a href="{{ admin_url }}" class="cta">Ouvrir le tableau des quotas</a>
  </div>
  <div class="footer">
    Envoy&eacute; par <strong>WebDeploy</strong>
  </div>
</div>
</body>
</html>
""")


# ── Status notification email template ────────────────────────────────
_STATUS_NOTIFICATION_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WebDeploy - Statut de votre compte</title>
<style>
  body { margin: 0; padding: 0; background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
  .container { max-width: 600px; margin: 40px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .header { padding: 24px 32px; color: #ffffff; }
  .header-approved { background: linear-gradient(135deg, #10b981, #059669); }
  .header-rejected { background: linear-gradient(135deg, #ef4444, #dc2626); }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .body { padding: 28px 32px; color: #1f2937; font-size: 15px; line-height: 1.6; }
  .footer { padding: 16px 32px; background: #f9fafb; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; }
</style>
</head>
<body>
<div class="container">
  <div class="header {{ 'header-approved' if user_status == 'approved' else 'header-rejected' }}">
    <h1>{{ '&#9989;' if user_status == 'approved' else '&#10060;' }} Votre compte WebDeploy</h1>
  </div>
  <div class="body">
    <p>Bonjour {{ display_name }},</p>
    {% if user_status == 'approved' %}
    <p>Votre demande d'acc&egrave;s a &eacute;t&eacute; <strong>approuv&eacute;e</strong>.</p>
    <p>R&ocirc;le attribu&eacute; : <strong>{{ role }}</strong></p>
    <p>Vous pouvez maintenant vous connecter et utiliser la plateforme.</p>
    {% else %}
    <p>Votre demande d'acc&egrave;s a &eacute;t&eacute; <strong>refus&eacute;e</strong>.</p>
    <p>Si vous pensez qu'il s'agit d'une erreur, veuillez contacter l'administrateur.</p>
    {% endif %}
  </div>
  <div class="footer">
    Envoy&eacute; par <strong>WebDeploy</strong>
  </div>
</div>
</body>
</html>
""")


class EmailService:
    """Send deployment notification emails via the Gmail API (service account)."""

    def __init__(
        self,
        log_callback: Optional[Callable] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._log = log_callback or (lambda msg, **kw: None)
        self._settings = settings or Settings()

    # ── Public API ────────────────────────────────────────────────────

    async def send_notification(
        self,
        website_name: str,
        mode: str,
        success: bool,
        live_url: Optional[str] = None,
        error_message: Optional[str] = None,
        ai_summary: Optional[str] = None,
        recipients: Optional[list[str]] = None,
    ) -> bool:
        """Send a deployment notification email."""
        to_emails = recipients or self._settings.notification_emails_list
        if not to_emails:
            self._log("No notification recipients configured — skipping email", level="WARNING", step="NOTIFY")
            return False

        if not self._is_gmail_configured():
            self._log("Gmail API not configured — skipping email", level="WARNING", step="NOTIFY")
            return False

        self._log(f"Sending notification email to {len(to_emails)} recipient(s)", level="INFO", step="NOTIFY")

        try:
            msg = self._build_message(
                website_name=website_name, mode=mode, success=success,
                live_url=live_url, error_message=error_message,
                ai_summary=ai_summary, to_emails=to_emails,
            )
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log("Notification email sent successfully", level="INFO", step="NOTIFY")
            return True
        except Exception as exc:
            self._log(f"Error sending notification email: {exc}", level="ERROR", step="NOTIFY")
            logger.exception("Gmail API sending failed")
            return False

    # ── Approval Emails ─────────────────────────────────────────────────

    async def send_approval_request(
        self,
        *,
        admin_email: str,
        display_name: str,
        user_email: str,
        requested_role: str,
        approve_url: str,
        reject_url: str,
    ) -> bool:
        """Send an approval request email to the admin."""
        if not admin_email or not self._is_gmail_configured():
            return False

        role_labels = {
            "simple_user": "Utilisateur Simple",
            "super_user": "Super Utilisateur",
            "admin": "Administrateur",
        }

        try:
            subject = f"[WebDeploy] Nouvelle demande d'accès — {display_name}"
            html_body = _APPROVAL_REQUEST_TEMPLATE.render(
                display_name=display_name,
                user_email=user_email,
                requested_role=role_labels.get(requested_role, requested_role),
                approve_url=approve_url,
                reject_url=reject_url,
            )
            msg = self._build_html_email(subject, html_body, [admin_email])
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log("Approval request sent to admin", level="INFO")
            return True
        except Exception as exc:
            self._log(f"Failed to send approval request: {exc}", level="ERROR")
            logger.exception("Approval request email failed")
            return False

    async def send_status_notification(
        self,
        *,
        to_email: str,
        display_name: str,
        user_status: str,
        role: str | None = None,
    ) -> bool:
        """Notify a user about their approval/rejection."""
        if not to_email or not self._is_gmail_configured():
            return False

        role_labels = {
            "simple_user": "Utilisateur Simple",
            "super_user": "Super Utilisateur",
            "admin": "Administrateur",
        }

        try:
            status_label = "approuvé" if user_status == "approved" else "refusé"
            subject = f"[WebDeploy] Votre compte a été {status_label}"
            html_body = _STATUS_NOTIFICATION_TEMPLATE.render(
                display_name=display_name or to_email,
                user_status=user_status,
                role=role_labels.get(role, role) if role else "",
            )
            msg = self._build_html_email(subject, html_body, [to_email])
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log(f"Status notification sent to {to_email}", level="INFO")
            return True
        except Exception as exc:
            self._log(f"Failed to send status notification: {exc}", level="ERROR")
            logger.exception("Status notification email failed")
            return False

    # ── Quota increase request ─────────────────────────────────────────

    async def send_quota_increase_request(
        self,
        *,
        admin_emails: list[str],
        display_name: str,
        user_email: str,
        role: str,
        current_quota: dict,
        requested_quota: dict,
        reason: str,
        admin_url: str,
    ) -> bool:
        """Notify admins that a user has requested a quota increase."""
        recipients = [e for e in (admin_emails or []) if e]
        if not recipients or not self._is_gmail_configured():
            return False

        role_labels = {
            "simple_user": "Utilisateur Simple",
            "super_user": "Super Utilisateur",
            "admin": "Administrateur",
        }
        fields = [
            ("max_deployments_per_day", "Deploiements / jour"),
            ("max_concurrent_deployments", "Deploiements simultanes"),
            ("max_total_deployments", "Total deploiements"),
            ("max_zip_size_mb", "Taille ZIP max (MB)"),
        ]

        try:
            subject = f"[WebDeploy] Demande de quota — {display_name or user_email}"
            html_body = _QUOTA_REQUEST_TEMPLATE.render(
                display_name=display_name or user_email,
                user_email=user_email,
                role=role_labels.get(role, role),
                current=current_quota or {},
                requested=requested_quota or {},
                reason=reason or "",
                fields=fields,
                admin_url=admin_url,
            )
            msg = self._build_html_email(subject, html_body, recipients)
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log(f"Quota request sent to {len(recipients)} admin(s)", level="INFO")
            return True
        except Exception as exc:
            self._log(f"Failed to send quota request: {exc}", level="ERROR")
            logger.exception("Quota request email failed")
            return False

    # ── Daily Report ──────────────────────────────────────────────────

    async def send_daily_report(
        self,
        *,
        report_date: str,
        total_deployments: int,
        total_sites: int,
        total_deployers: int,
        deployers: list[dict],
        recipients: list[str],
        with_ai_count: int = 0,
        without_ai_count: int = 0,
        average_per_day: float = 0.0,
    ) -> bool:
        """Send a daily deployment summary report (enhanced with AI split)."""
        if not recipients or not self._is_gmail_configured():
            return False

        try:
            subject = f"[WebDeploy] Rapport Quotidien — {report_date}"
            html_body = _DAILY_REPORT_TEMPLATE.render(
                report_date=report_date,
                total_deployments=total_deployments,
                with_ai_count=with_ai_count,
                without_ai_count=without_ai_count,
                total_sites=total_sites,
                total_deployers=total_deployers,
                deployers=deployers,
            )
            msg = self._build_html_email(subject, html_body, recipients)
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log("Daily report sent successfully", level="INFO", step="REPORT")
            return True
        except Exception as exc:
            self._log(f"Failed to send daily report: {exc}", level="ERROR", step="REPORT")
            logger.exception("Daily report email failed")
            return False

    # ── Period Report (weekly / monthly / custom) ─────────────────────

    async def send_period_report(
        self,
        *,
        report_type: str,
        period_label: str,
        stats: dict,
        recipients: list[str],
    ) -> bool:
        """Send a period summary report (weekly, monthly, or custom)."""
        if not recipients or not self._is_gmail_configured():
            return False

        type_labels = {
            "weekly": "Hebdomadaire",
            "monthly": "Mensuel",
            "custom": "Personnalis\u00e9",
        }
        type_label = type_labels.get(report_type, report_type.capitalize())

        type_colors = {
            "weekly": ("#059669", "#047857"),
            "monthly": ("#7c3aed", "#6d28d9"),
            "custom": ("#2563EB", "#1d4ed8"),
        }
        color_start, color_end = type_colors.get(report_type, ("#2563EB", "#1d4ed8"))

        # Convert stats dict to a namespace-like object for template access
        class StatsProxy:
            def __init__(self, d):
                self._d = d
            def __getattr__(self, name):
                return self._d.get(name)

        try:
            subject = f"[WebDeploy] Rapport {type_label} — {period_label}"
            html_body = _PERIOD_REPORT_TEMPLATE.render(
                report_type_label=type_label,
                period_label=period_label,
                stats=StatsProxy(stats),
                header_color_start=color_start,
                header_color_end=color_end,
            )
            msg = self._build_html_email(subject, html_body, recipients)
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log(f"{type_label} report sent successfully", level="INFO", step="REPORT")
            return True
        except Exception as exc:
            self._log(f"Failed to send {type_label} report: {exc}", level="ERROR", step="REPORT")
            logger.exception("%s report email failed", type_label)
            return False

    # ── Personalized Report (per deployer) ────────────────────────────

    async def send_personalized_report(
        self,
        *,
        deployer_info: dict,
        period_label: str,
        overall_stats: dict,
    ) -> bool:
        """Send a personalized report to a specific deployer."""
        email = deployer_info.get("email", "")
        if not email or not self._is_gmail_configured():
            return False

        name = deployer_info.get("name", email)
        total_team = overall_stats.get("total_count", 1) or 1
        pct = round(deployer_info.get("total", 0) / total_team * 100, 1)

        class StatsProxy:
            def __init__(self, d):
                self._d = d
            def __getattr__(self, name):
                return self._d.get(name)

        try:
            subject = f"[WebDeploy] Votre Rapport — {period_label}"
            html_body = _PERSONALIZED_REPORT_TEMPLATE.render(
                deployer_name=name,
                deployer=StatsProxy(deployer_info),
                period_label=period_label,
                overall=StatsProxy(overall_stats),
                pct_of_team=pct,
            )
            msg = self._build_html_email(subject, html_body, [email])
            await asyncio.to_thread(self._send_via_gmail, msg)
            self._log(f"Personalized report sent to {email}", level="INFO", step="REPORT")
            return True
        except Exception as exc:
            self._log(f"Failed to send personalized report to {email}: {exc}", level="ERROR", step="REPORT")
            return False

    # ── Private helpers ───────────────────────────────────────────────

    def _is_gmail_configured(self) -> bool:
        """Check whether Gmail API credentials are available."""
        import os
        has_creds = (
            self._settings.GOOGLE_APPLICATION_CREDENTIALS
            and os.path.isfile(self._settings.GOOGLE_APPLICATION_CREDENTIALS)
        )
        if not has_creds:
            try:
                import google.auth
                google.auth.default(scopes=_GMAIL_SEND_SCOPE)
                has_creds = True
            except Exception:
                has_creds = False
        return bool(has_creds and self._settings.GMAIL_DELEGATED_USER)

    def _get_gmail_service(self):
        """Build an authorised Gmail API service using the service account."""
        import os
        creds_path = self._settings.GOOGLE_APPLICATION_CREDENTIALS
        if creds_path and os.path.isfile(creds_path):
            credentials = service_account.Credentials.from_service_account_file(
                creds_path, scopes=_GMAIL_SEND_SCOPE,
            )
        else:
            _alt_paths = [
                "/app/secrets/service-account.json",
                "/secrets/service-account.json",
                "/app/service-account.json",
            ]
            sa_path = None
            for p in _alt_paths:
                if os.path.isfile(p):
                    sa_path = p
                    break

            if sa_path:
                credentials = service_account.Credentials.from_service_account_file(
                    sa_path, scopes=_GMAIL_SEND_SCOPE,
                )
            else:
                import google.auth
                credentials, _ = google.auth.default(scopes=_GMAIL_SEND_SCOPE)

        if not hasattr(credentials, "with_subject"):
            raise RuntimeError(
                "Gmail domain-wide delegation requires a service-account key file. "
                "ADC / compute-engine credentials do not support with_subject(). "
                "Mount the service-account JSON via Secret Manager or copy it into the container."
            )
        delegated = credentials.with_subject(self._settings.GMAIL_DELEGATED_USER)
        delegated.refresh(Request())
        return build("gmail", "v1", credentials=delegated, cache_discovery=False)

    def _send_via_gmail(self, msg: MIMEMultipart) -> None:
        """Send a MIME message through the Gmail API (runs in thread)."""
        service = self._get_gmail_service()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()

    def _build_html_email(
        self, subject: str, html_body: str, to_emails: list[str],
    ) -> MIMEMultipart:
        """Build a MIME message with HTML body."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._settings.GMAIL_DELEGATED_USER or self._settings.NOTIFICATION_FROM_EMAIL
        msg["To"] = ", ".join(to_emails)
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        return msg

    def _build_message(
        self,
        website_name: str,
        mode: str,
        success: bool,
        live_url: Optional[str],
        error_message: Optional[str],
        ai_summary: Optional[str],
        to_emails: list[str],
    ) -> MIMEMultipart:
        """Build the MIME email message with HTML body for deployment notifications."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if success:
            subject = f"[WebDeploy] \u2705 {website_name} deployed successfully \u2014 {mode} mode"
        else:
            subject = f"[WebDeploy] \u274c {website_name} deployment failed \u2014 {mode} mode"

        html_body = _EMAIL_TEMPLATE.render(
            website_name=website_name, mode=mode, success=success,
            live_url=live_url, error_message=error_message,
            ai_summary=ai_summary, timestamp=timestamp,
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._settings.GMAIL_DELEGATED_USER or self._settings.NOTIFICATION_FROM_EMAIL
        msg["To"] = ", ".join(to_emails)

        if success:
            plain = (
                f"WebDeploy Notification\n\nWebsite: {website_name}\n"
                f"Mode: {mode.upper()}\nStatus: SUCCESS\nURL: {live_url or 'N/A'}\n"
            )
        else:
            plain = (
                f"WebDeploy Notification\n\nWebsite: {website_name}\n"
                f"Mode: {mode.upper()}\nStatus: FAILED\nError: {error_message or 'Unknown error'}\n"
            )
        if ai_summary:
            plain += f"\nAI Summary:\n{ai_summary}\n"
        plain += f"\nTimestamp: {timestamp}\n"

        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        return msg
